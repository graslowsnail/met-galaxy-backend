#!/usr/bin/env python3

import argparse
import concurrent.futures
import io
import json
import logging
import os
import socket
import time
import uuid
from dataclasses import asdict, dataclass
from functools import lru_cache

import boto3
import psycopg2
from dotenv import load_dotenv
from PIL import Image, UnidentifiedImageError
from psycopg2.extras import RealDictCursor, execute_values

load_dotenv()

LOGGER = logging.getLogger("image-embedding")

ASSET_TABLE = '"met-galaxy_image_asset"'
ARTWORK_TABLE = '"met-galaxy_artwork"'
INGESTION_TABLE = '"met-galaxy_image_ingestion"'
OUTBOX_TABLE = '"met-galaxy_image_embedding_outbox"'


def database_url():
    configured = os.getenv("DATABASE_URL")
    if configured:
        return configured

    secret_arn = os.getenv("DATABASE_SECRET_ARN")
    if not secret_arn:
        raise RuntimeError("DATABASE_URL or DATABASE_SECRET_ARN is required")

    response = service_client(
        "secretsmanager",
        os.getenv("AWS_REGION", "us-east-1"),
    ).get_secret_value(SecretId=secret_arn)
    secret = response["SecretString"]
    try:
        parsed = json.loads(secret)
    except json.JSONDecodeError:
        return secret
    if not parsed.get("DATABASE_URL"):
        raise RuntimeError("database secret does not contain DATABASE_URL")
    return parsed["DATABASE_URL"]


@dataclass(frozen=True)
class Settings:
    database_url: str
    aws_region: str
    s3_bucket_name: str
    queue_url: str
    lease_seconds: int
    max_attempts: int
    max_image_bytes: int
    download_concurrency: int

    @classmethod
    def from_env(cls):
        queue_url = os.getenv("IMAGE_EMBEDDING_QUEUE_URL", "")
        return cls(
            database_url=database_url(),
            aws_region=os.getenv("AWS_REGION", "us-east-1"),
            s3_bucket_name=os.getenv(
                "S3_BUCKET_NAME",
                "met-artworks-images",
            ),
            queue_url=queue_url,
            lease_seconds=int(
                os.getenv("IMAGE_EMBEDDING_LEASE_SECONDS", "900")
            ),
            max_attempts=int(
                os.getenv("IMAGE_EMBEDDING_MAX_ATTEMPTS", "5")
            ),
            max_image_bytes=int(
                os.getenv(
                    "IMAGE_EMBEDDING_MAX_IMAGE_BYTES",
                    str(20 * 1024 * 1024),
                )
            ),
            download_concurrency=int(
                os.getenv("IMAGE_EMBEDDING_DOWNLOAD_CONCURRENCY", "10")
            ),
        )


@dataclass
class EmbeddingResult:
    image_asset_id: int | None
    outcome: str
    attempt_count: int = 0
    artwork_count: int = 0
    duration_ms: int = 0
    error: str | None = None


@lru_cache(maxsize=8)
def aws_client(service_name, region, profile):
    session = boto3.Session(
        profile_name=profile if profile else None,
        region_name=region,
    )
    return session.client(service_name)


def service_client(service_name, region):
    return aws_client(
        service_name,
        region,
        os.getenv("AWS_PROFILE"),
    )


def connect(settings):
    return psycopg2.connect(settings.database_url)


def worker_id():
    configured = os.getenv("IMAGE_EMBEDDING_WORKER_ID")
    if configured:
        return configured
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


def retry_delay_seconds(attempt_count):
    return min(30 * (2 ** max(attempt_count - 1, 0)), 900)


def load_model(model_name, pretrained, device):
    import open_clip
    import torch

    resolved_device = device
    if device == "auto":
        if torch.cuda.is_available():
            resolved_device = "cuda"
        elif torch.backends.mps.is_available():
            resolved_device = "mps"
        else:
            resolved_device = "cpu"

    model, _, preprocess = open_clip.create_model_and_transforms(
        model_name,
        pretrained=pretrained,
        device=resolved_device,
    )
    model.eval()
    return model, preprocess, resolved_device


def claim_asset(connection, settings, image_asset_id, owner):
    with connection.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute(
            f"""
            UPDATE {ASSET_TABLE}
            SET
                "processingLeaseOwner" = %s,
                "processingLeaseExpiresAt" = CURRENT_TIMESTAMP
                    + (%s * INTERVAL '1 second'),
                "processingAttemptCount" = "processingAttemptCount" + 1,
                "lastError" = NULL,
                "updatedAt" = CURRENT_TIMESTAMP
            WHERE id = %s
              AND "processingStatus" = 'pending_embedding'
              AND "processingAttemptCount" < %s
              AND (
                "processingNextAttemptAt" IS NULL
                OR "processingNextAttemptAt" <= CURRENT_TIMESTAMP
              )
              AND (
                "processingLeaseExpiresAt" IS NULL
                OR "processingLeaseExpiresAt" <= CURRENT_TIMESTAMP
              )
            RETURNING
                id,
                "fullS3Key" AS full_s3_key,
                "thumbnailS3Key" AS thumbnail_s3_key,
                "processingAttemptCount" AS attempt_count
            """,
            (
                owner,
                settings.lease_seconds,
                image_asset_id,
                settings.max_attempts,
            ),
        )
        claimed = cursor.fetchone()
        if claimed:
            connection.commit()
            return "claimed", claimed

        connection.rollback()
        cursor.execute(
            f"""
            SELECT
                id,
                "processingStatus" AS processing_status,
                "processingAttemptCount" AS attempt_count,
                "processingLeaseExpiresAt" AS lease_expires_at,
                "processingNextAttemptAt" AS next_attempt_at,
                "lastError" AS last_error
            FROM {ASSET_TABLE}
            WHERE id = %s
            """,
            (image_asset_id,),
        )
        asset = cursor.fetchone()
    connection.commit()

    if asset is None:
        return "missing", None
    if asset["processing_status"] == "ready":
        return "ready", asset
    if asset["attempt_count"] >= settings.max_attempts:
        return "terminal", asset
    return "deferred", asset


def read_s3_image(settings, s3_key):
    response = service_client("s3", settings.aws_region).get_object(
        Bucket=settings.s3_bucket_name,
        Key=s3_key,
    )
    body = response["Body"].read(settings.max_image_bytes + 1)
    if len(body) > settings.max_image_bytes:
        raise RuntimeError("canonical image exceeds configured byte limit")
    if not body:
        raise RuntimeError("canonical image was empty")
    return body


def prepare_asset(settings, preprocess, claimed):
    s3_key = claimed["thumbnail_s3_key"] or claimed["full_s3_key"]
    if not s3_key:
        raise RuntimeError("canonical asset has no S3 key")
    content = read_s3_image(settings, s3_key)
    try:
        with Image.open(io.BytesIO(content)) as image:
            tensor = preprocess(image.convert("RGB"))
    except UnidentifiedImageError as error:
        raise RuntimeError("canonical object is not a readable image") from error
    return tensor


def encode_batch(model, tensors, device):
    import torch

    batch = torch.stack(tensors).to(device)
    with torch.inference_mode():
        features = model.encode_image(batch)
        features = features / features.norm(dim=-1, keepdim=True)
    return features.float().cpu().numpy().tolist()


def vector_literal(values):
    return f"[{','.join(format(value, '.9g') for value in values)}]"


def finalize_embeddings(connection, owner, items, embeddings):
    updates = [
        (item["image_asset_id"], vector_literal(embedding), owner)
        for item, embedding in zip(items, embeddings, strict=True)
    ]
    asset_ids = [item[0] for item in updates]

    with connection.cursor() as cursor:
        execute_values(
            cursor,
            f"""
            UPDATE {ASSET_TABLE} AS asset
            SET
                "imageEmbedding" = batch.embedding::vector,
                "processingStatus" = 'ready',
                "processingLeaseOwner" = NULL,
                "processingLeaseExpiresAt" = NULL,
                "processingNextAttemptAt" = NULL,
                "lastError" = NULL,
                "updatedAt" = CURRENT_TIMESTAMP
            FROM (VALUES %s) AS batch(id, embedding, owner)
            WHERE asset.id = batch.id
              AND asset."processingStatus" = 'pending_embedding'
              AND asset."processingLeaseOwner" = batch.owner
            """,
            updates,
            template="(%s, %s, %s)",
            page_size=len(updates),
            fetch=False,
        )
        cursor.execute(
            f"""
            UPDATE {ARTWORK_TABLE} AS artwork
            SET "imgVec" = asset."imageEmbedding"
            FROM {ASSET_TABLE} AS asset
            WHERE artwork."imageAssetId" = asset.id
              AND asset.id = ANY(%s)
              AND asset."processingStatus" = 'ready'
            """,
            (asset_ids,),
        )
        artwork_count = cursor.rowcount
        cursor.execute(
            f"""
            UPDATE {INGESTION_TABLE} AS ingestion
            SET
                status = 'complete',
                "completedAt" = CURRENT_TIMESTAMP,
                "lastError" = NULL,
                "updatedAt" = CURRENT_TIMESTAMP
            FROM {ARTWORK_TABLE} AS artwork
            WHERE ingestion."artworkId" = artwork.id
              AND artwork."imageAssetId" = ANY(%s)
              AND ingestion.status = 'awaiting_embedding'
            """,
            (asset_ids,),
        )
        cursor.execute(
            f"""
            DELETE FROM {OUTBOX_TABLE}
            WHERE "imageAssetId" = ANY(%s)
            """,
            (asset_ids,),
        )
    connection.commit()
    return artwork_count


def record_failure(
    connection,
    settings,
    image_asset_id,
    owner,
    attempt_count,
    error,
):
    terminal = attempt_count >= settings.max_attempts
    delay = None if terminal else retry_delay_seconds(attempt_count)
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            UPDATE {ASSET_TABLE}
            SET
                "processingLeaseOwner" = NULL,
                "processingLeaseExpiresAt" = NULL,
                "processingNextAttemptAt" = CASE
                    WHEN %s IS NULL THEN NULL
                    ELSE CURRENT_TIMESTAMP
                        + (%s * INTERVAL '1 second')
                END,
                "lastError" = %s,
                "updatedAt" = CURRENT_TIMESTAMP
            WHERE id = %s
              AND "processingLeaseOwner" = %s
            """,
            (
                delay,
                delay,
                f"embedding: {type(error).__name__}: {error}",
                image_asset_id,
                owner,
            ),
        )
    connection.commit()
    return terminal, delay


def delete_message(sqs_client, queue_url, message):
    sqs_client.delete_message(
        QueueUrl=queue_url,
        ReceiptHandle=message["ReceiptHandle"],
    )


def defer_message(sqs_client, queue_url, message, delay):
    sqs_client.change_message_visibility(
        QueueUrl=queue_url,
        ReceiptHandle=message["ReceiptHandle"],
        VisibilityTimeout=delay,
    )


def receive_batch(sqs_client, settings, batch_size):
    response = sqs_client.receive_message(
        QueueUrl=settings.queue_url,
        MaxNumberOfMessages=min(batch_size, 10),
        WaitTimeSeconds=5,
        VisibilityTimeout=settings.lease_seconds,
        AttributeNames=["ApproximateReceiveCount"],
    )
    return response.get("Messages", [])


def process_message_batch(
    settings,
    model,
    preprocess,
    device,
    messages,
    owner,
):
    started_at = time.monotonic()
    sqs_client = service_client("sqs", settings.aws_region)
    connection = connect(settings)
    claimed_items = []
    results = []

    try:
        for message in messages:
            try:
                payload = json.loads(message["Body"])
                image_asset_id = int(payload["imageAssetId"])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                delete_message(sqs_client, settings.queue_url, message)
                results.append(
                    EmbeddingResult(
                        image_asset_id=None,
                        outcome="terminal_failure",
                        error=f"invalid queue message: {error}",
                    )
                )
                continue

            state, asset = claim_asset(
                connection,
                settings,
                image_asset_id,
                owner,
            )
            if state == "ready":
                delete_message(sqs_client, settings.queue_url, message)
                results.append(
                    EmbeddingResult(
                        image_asset_id=image_asset_id,
                        outcome="already_ready",
                        attempt_count=asset["attempt_count"],
                    )
                )
                continue
            if state in ("missing", "terminal"):
                delete_message(sqs_client, settings.queue_url, message)
                results.append(
                    EmbeddingResult(
                        image_asset_id=image_asset_id,
                        outcome="terminal_failure",
                        attempt_count=(
                            asset["attempt_count"] if asset else 0
                        ),
                        error=(
                            "image asset does not exist"
                            if state == "missing"
                            else asset["last_error"]
                        ),
                    )
                )
                continue
            if state == "deferred":
                defer_message(sqs_client, settings.queue_url, message, 30)
                results.append(
                    EmbeddingResult(
                        image_asset_id=image_asset_id,
                        outcome="deferred",
                        attempt_count=asset["attempt_count"],
                    )
                )
                continue

            claimed_items.append(
                {
                    "message": message,
                    "image_asset_id": image_asset_id,
                    "attempt_count": asset["attempt_count"],
                    "asset": asset,
                }
            )

        def prepare(item):
            return prepare_asset(settings, preprocess, item["asset"])

        prepared_items = []
        tensors = []
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=settings.download_concurrency
        ) as executor:
            futures = {
                executor.submit(prepare, item): item
                for item in claimed_items
            }
            for future, item in futures.items():
                try:
                    tensors.append(future.result())
                    prepared_items.append(item)
                except Exception as error:
                    terminal, delay = record_failure(
                        connection,
                        settings,
                        item["image_asset_id"],
                        owner,
                        item["attempt_count"],
                        error,
                    )
                    if terminal:
                        delete_message(
                            sqs_client,
                            settings.queue_url,
                            item["message"],
                        )
                    else:
                        defer_message(
                            sqs_client,
                            settings.queue_url,
                            item["message"],
                            delay,
                        )
                    results.append(
                        EmbeddingResult(
                            image_asset_id=item["image_asset_id"],
                            outcome=(
                                "terminal_failure"
                                if terminal
                                else "retryable_failure"
                            ),
                            attempt_count=item["attempt_count"],
                            error=str(error),
                        )
                    )

        if prepared_items:
            try:
                embeddings = encode_batch(model, tensors, device)
                artwork_count = finalize_embeddings(
                    connection,
                    owner,
                    prepared_items,
                    embeddings,
                )
                for item in prepared_items:
                    delete_message(
                        sqs_client,
                        settings.queue_url,
                        item["message"],
                    )
                    results.append(
                        EmbeddingResult(
                            image_asset_id=item["image_asset_id"],
                            outcome="embedded",
                            attempt_count=item["attempt_count"],
                        )
                    )
                if results:
                    results[-1].artwork_count = artwork_count
            except Exception as error:
                connection.rollback()
                for item in prepared_items:
                    terminal, delay = record_failure(
                        connection,
                        settings,
                        item["image_asset_id"],
                        owner,
                        item["attempt_count"],
                        error,
                    )
                    if terminal:
                        delete_message(
                            sqs_client,
                            settings.queue_url,
                            item["message"],
                        )
                    else:
                        defer_message(
                            sqs_client,
                            settings.queue_url,
                            item["message"],
                            delay,
                        )
                    results.append(
                        EmbeddingResult(
                            image_asset_id=item["image_asset_id"],
                            outcome=(
                                "terminal_failure"
                                if terminal
                                else "retryable_failure"
                            ),
                            attempt_count=item["attempt_count"],
                            error=str(error),
                        )
                    )
    finally:
        connection.close()

    duration_ms = round((time.monotonic() - started_at) * 1000)
    for result in results:
        result.duration_ms = duration_ms
    return results


def run_queue(
    settings,
    model,
    preprocess,
    device,
    limit,
    batch_size,
    refill_size,
    idle_polls,
):
    if not settings.queue_url:
        raise RuntimeError("IMAGE_EMBEDDING_QUEUE_URL is required")
    sqs_client = service_client("sqs", settings.aws_region)
    owner = worker_id()
    results = []
    consecutive_idle_polls = 0

    while len(results) < limit:
        messages = receive_batch(
            sqs_client,
            settings,
            min(batch_size, limit - len(results)),
        )
        if not messages:
            if refill_size > 0:
                refill = enqueue_pending(settings, refill_size)
                if refill["enqueued"] > 0:
                    consecutive_idle_polls = 0
                    continue
            consecutive_idle_polls += 1
            if consecutive_idle_polls >= idle_polls:
                break
            continue
        consecutive_idle_polls = 0
        batch_results = process_message_batch(
            settings,
            model,
            preprocess,
            device,
            messages,
            owner,
        )
        results.extend(batch_results)
        LOGGER.info(
            json.dumps(
                [asdict(result) for result in batch_results],
                separators=(",", ":"),
            )
        )
    return results


def enqueue_pending(settings, limit):
    if not settings.queue_url:
        raise RuntimeError("IMAGE_EMBEDDING_QUEUE_URL is required")
    connection = connect(settings)
    sqs_client = service_client("sqs", settings.aws_region)
    sent = 0
    failed = 0
    try:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                f"""
                DELETE FROM {OUTBOX_TABLE} AS outbox
                USING {ASSET_TABLE} AS asset
                WHERE outbox."imageAssetId" = asset.id
                  AND asset."processingStatus" = 'ready'
                """
            )
            cursor.execute(
                f"""
                SELECT
                    asset.id,
                    asset."fullS3Key" AS full_s3_key,
                    asset."thumbnailS3Key" AS thumbnail_s3_key,
                    outbox."attemptCount" AS dispatch_attempt_count
                FROM {OUTBOX_TABLE} outbox
                JOIN {ASSET_TABLE} asset
                  ON asset.id = outbox."imageAssetId"
                WHERE asset."processingStatus" = 'pending_embedding'
                  AND asset."processingAttemptCount" < %s
                  AND (
                    (
                      outbox.status = 'pending'
                      AND (
                        outbox."nextAttemptAt" IS NULL
                        OR outbox."nextAttemptAt" <= CURRENT_TIMESTAMP
                      )
                    )
                    OR (
                      outbox.status = 'dispatched'
                      AND outbox."dispatchedAt"
                        <= CURRENT_TIMESTAMP - INTERVAL '30 minutes'
                      AND (
                        asset."processingLeaseExpiresAt" IS NULL
                        OR asset."processingLeaseExpiresAt"
                          <= CURRENT_TIMESTAMP
                      )
                    )
                  )
                ORDER BY outbox."createdAt", asset.id
                FOR UPDATE OF outbox SKIP LOCKED
                LIMIT %s
                """,
                (settings.max_attempts, limit),
            )
            assets = cursor.fetchall()

            for batch_start in range(0, len(assets), 10):
                batch = assets[batch_start : batch_start + 10]
                response = sqs_client.send_message_batch(
                    QueueUrl=settings.queue_url,
                    Entries=[
                        {
                            "Id": str(asset["id"]),
                            "MessageBody": json.dumps(
                                {
                                    "imageAssetId": asset["id"],
                                    "fullS3Key": asset["full_s3_key"],
                                    "thumbnailS3Key": (
                                        asset["thumbnail_s3_key"]
                                    ),
                                },
                                separators=(",", ":"),
                            ),
                            **(
                                {
                            "MessageGroupId": (
                                f"openclip-{asset['id'] % 64:02d}"
                            ),
                            "MessageDeduplicationId": (
                                f"{asset['id']}-"
                                f"{asset['dispatch_attempt_count'] + 1}"
                            ),
                        }
                        if settings.queue_url.endswith(".fifo")
                        else {}
                            ),
                        }
                        for asset in batch
                    ],
                )
                successful = {
                    int(item["Id"]): item["MessageId"]
                    for item in response.get("Successful", [])
                }
                failures = {
                    int(item["Id"]): (
                        f"{item.get('Code', 'unknown')}: "
                        f"{item.get('Message', 'SQS send failed')}"
                    )
                    for item in response.get("Failed", [])
                }
                if successful:
                    execute_values(
                        cursor,
                        f"""
                        UPDATE {OUTBOX_TABLE} AS outbox
                        SET
                            status = 'dispatched',
                            "attemptCount" = "attemptCount" + 1,
                            "messageId" = batch.message_id,
                            "dispatchedAt" = CURRENT_TIMESTAMP,
                            "nextAttemptAt" = NULL,
                            "lastError" = NULL
                        FROM (VALUES %s) AS batch(
                            image_asset_id,
                            message_id
                        )
                        WHERE outbox."imageAssetId"
                            = batch.image_asset_id
                        """,
                        list(successful.items()),
                        template="(%s, %s)",
                        page_size=len(successful),
                    )
                if failures:
                    execute_values(
                        cursor,
                        f"""
                        UPDATE {OUTBOX_TABLE} AS outbox
                        SET
                            status = 'pending',
                            "attemptCount" = "attemptCount" + 1,
                            "nextAttemptAt" = CURRENT_TIMESTAMP
                                + INTERVAL '30 seconds',
                            "lastError" = batch.error
                        FROM (VALUES %s) AS batch(
                            image_asset_id,
                            error
                        )
                        WHERE outbox."imageAssetId"
                            = batch.image_asset_id
                        """,
                        list(failures.items()),
                        template="(%s, %s)",
                        page_size=len(failures),
                    )
                sent += len(successful)
                failed += len(failures)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {
        "selected": len(assets),
        "enqueued": sent,
        "failed": failed,
    }


def database_stats(settings):
    connection = connect(settings)
    try:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                f"""
                SELECT
                    COUNT(*) FILTER (
                        WHERE "processingStatus" = 'pending_embedding'
                          AND "processingAttemptCount" < %s
                    )::int AS pending,
                    COUNT(*) FILTER (
                        WHERE "processingStatus" = 'pending_embedding'
                          AND "processingAttemptCount" >= %s
                    )::int AS terminal_failures,
                    COUNT(*) FILTER (
                        WHERE "processingStatus" = 'ready'
                    )::int AS ready,
                    COUNT(*) FILTER (
                        WHERE "processingLeaseExpiresAt"
                            > CURRENT_TIMESTAMP
                    )::int AS leased
                FROM {ASSET_TABLE}
                """,
                (settings.max_attempts, settings.max_attempts),
            )
            assets = cursor.fetchone()
            cursor.execute(
                f"""
                SELECT
                    COUNT(*) FILTER (
                        WHERE artwork."imageAssetId" IS NOT NULL
                          AND artwork."imgVec" IS NULL
                    )::int AS linked_without_embedding,
                    COUNT(*) FILTER (
                        WHERE artwork."imageAssetId" IS NOT NULL
                          AND artwork."imgVec" IS NOT NULL
                    )::int AS linked_with_embedding
                FROM {ARTWORK_TABLE} artwork
                """
            )
            artworks = cursor.fetchone()
        connection.commit()
    finally:
        connection.close()

    queue = {}
    if settings.queue_url:
        response = service_client(
            "sqs",
            settings.aws_region,
        ).get_queue_attributes(
            QueueUrl=settings.queue_url,
            AttributeNames=[
                "ApproximateNumberOfMessages",
                "ApproximateNumberOfMessagesNotVisible",
            ],
        )
        queue = response["Attributes"]
    return {"assets": assets, "artworks": artworks, "queue": queue}


def summarize(results, duration_ms):
    outcomes = {}
    for result in results:
        outcomes[result.outcome] = outcomes.get(result.outcome, 0) + 1
    return {
        "processed": len(results),
        "outcomes": outcomes,
        "artworkUpdates": sum(item.artwork_count for item in results),
        "durationMs": duration_ms,
        "imagesPerSecond": (
            round(len(results) / (duration_ms / 1000), 3)
            if duration_ms
            else 0
        ),
    }


def parse_args():
    parser = argparse.ArgumentParser(
        description="Batched canonical OpenCLIP embedding worker"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    work_parser = subparsers.add_parser("work")
    work_parser.add_argument("--limit", type=int, default=1000)
    work_parser.add_argument("--batch-size", type=int, default=10)
    work_parser.add_argument("--device", default="auto")
    work_parser.add_argument("--model", default="ViT-L-14")
    work_parser.add_argument("--pretrained", default="openai")
    work_parser.add_argument("--refill-size", type=int, default=0)
    work_parser.add_argument("--idle-polls", type=int, default=1)

    enqueue_parser = subparsers.add_parser("enqueue-pending")
    enqueue_parser.add_argument("--limit", type=int, default=1000000)

    subparsers.add_parser("stats")
    return parser.parse_args()


def main():
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    args = parse_args()
    settings = Settings.from_env()

    if args.command == "enqueue-pending":
        print(json.dumps(enqueue_pending(settings, args.limit), indent=2))
        return
    if args.command == "stats":
        print(json.dumps(database_stats(settings), indent=2, default=str))
        return

    if (
        args.limit < 1
        or args.batch_size < 1
        or args.batch_size > 10
        or args.refill_size < 0
        or args.idle_polls < 1
    ):
        raise RuntimeError(
            "limit and idle-polls must be positive, batch-size must be "
            "1-10, and refill-size must be nonnegative"
        )
    model, preprocess, device = load_model(
        args.model,
        args.pretrained,
        args.device,
    )
    print(
        json.dumps(
            {
                "device": device,
                "model": args.model,
                "pretrained": args.pretrained,
                "batchSize": args.batch_size,
            }
        )
    )
    started_at = time.monotonic()
    results = run_queue(
        settings,
        model,
        preprocess,
        device,
        args.limit,
        args.batch_size,
        args.refill_size,
        args.idle_polls,
    )
    duration_ms = round((time.monotonic() - started_at) * 1000)
    print(json.dumps(summarize(results, duration_ms), indent=2))


if __name__ == "__main__":
    main()
