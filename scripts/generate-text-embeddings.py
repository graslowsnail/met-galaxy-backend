#!/usr/bin/env python3

import argparse
import fcntl
import json
import math
import os
import socket
import time
import uuid

import psycopg2
from dotenv import load_dotenv
from openai import OpenAI
from psycopg2.extras import RealDictCursor, execute_values

load_dotenv()

ARTWORK_TABLE = '"met-galaxy_artwork"'
DEFAULT_MODEL = "text-embedding-3-small"
EMBEDDING_DIMENSIONS = 1536
MAX_TEXT_CHARACTERS = 24000
TOKEN_RATE_LIMIT_WINDOW_SECONDS = 60
MAX_EMBEDDING_BATCH_ESTIMATED_TOKENS = 180000


def database_url():
    configured = os.getenv("DATABASE_URL")
    if not configured:
        raise RuntimeError("DATABASE_URL is required")
    return configured


def worker_id():
    configured = os.getenv("TEXT_EMBEDDING_WORKER_ID")
    if configured:
        return configured
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


def vector_literal(values):
    return f"[{','.join(format(value, '.9g') for value in values)}]"


class TokenRateLimiter:
    def __init__(self, state_file, tokens_per_minute):
        self.state_file = state_file
        self.tokens_per_minute = tokens_per_minute

    def reserve(self, texts):
        estimated_tokens = sum(
            max(1, math.ceil(len(text) / 2.5) + 2) for text in texts
        )
        if estimated_tokens > self.tokens_per_minute:
            raise RuntimeError(
                "embedding batch exceeds the configured token budget"
            )

        while True:
            now = time.time()
            with open(self.state_file, "a+", encoding="utf-8") as state:
                fcntl.flock(state.fileno(), fcntl.LOCK_EX)
                state.seek(0)
                try:
                    reservations = json.load(state)
                except json.JSONDecodeError:
                    reservations = []
                reservations = [
                    item
                    for item in reservations
                    if item[0] > now - TOKEN_RATE_LIMIT_WINDOW_SECONDS
                ]
                used_tokens = sum(item[1] for item in reservations)
                if used_tokens + estimated_tokens <= self.tokens_per_minute:
                    reservations.append([now, estimated_tokens])
                    state.seek(0)
                    state.truncate()
                    json.dump(reservations, state, separators=(",", ":"))
                    fcntl.flock(state.fileno(), fcntl.LOCK_UN)
                    return
                retry_after = min(item[0] for item in reservations)
                fcntl.flock(state.fileno(), fcntl.LOCK_UN)
            time.sleep(max(0.1, retry_after + TOKEN_RATE_LIMIT_WINDOW_SECONDS - now))


def split_embedding_batches(rows, texts):
    batch_rows = []
    batch_texts = []
    estimated_tokens = 0
    for row, text in zip(rows, texts, strict=True):
        text_tokens = max(1, math.ceil(len(text) / 2.5) + 2)
        if (
            batch_rows
            and estimated_tokens + text_tokens
            > MAX_EMBEDDING_BATCH_ESTIMATED_TOKENS
        ):
            yield batch_rows, batch_texts
            batch_rows = []
            batch_texts = []
            estimated_tokens = 0
        batch_rows.append(row)
        batch_texts.append(text)
        estimated_tokens += text_tokens
    if batch_rows:
        yield batch_rows, batch_texts


def create_artwork_text(row):
    fields = [
        ("Object ID", row["object_id"]),
        ("Title", row["title"]),
        ("Artist", row["artist"]),
        ("Date", row["date"]),
        ("Medium", row["medium"]),
        ("Department", row["department"]),
        ("Culture", row["culture"]),
        ("Classification", row["classification"]),
        ("Description", row["description"]),
        ("Nationality", row["artist_nationality"]),
    ]
    parts = [
        f"{label}: {value}"
        for label, value in fields
        if value is not None and str(value).strip()
    ]
    if not parts:
        return None
    return " | ".join(parts)[:MAX_TEXT_CHARACTERS]


def claim_batch(
    connection,
    *,
    owner,
    batch_size,
    max_attempts,
    lease_seconds,
):
    with connection.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute(
            f"""
            WITH candidates AS (
                SELECT id
                FROM {ARTWORK_TABLE}
                WHERE "txtVec" IS NULL
                  AND "imgVec" IS NOT NULL
                  AND "imageAssetId" IS NOT NULL
                  AND "txtVecAttemptCount" < %s
                  AND (
                    "txtVecNextAttemptAt" IS NULL
                    OR "txtVecNextAttemptAt" <= CURRENT_TIMESTAMP
                  )
                  AND (
                    "txtVecLeaseExpiresAt" IS NULL
                    OR "txtVecLeaseExpiresAt" <= CURRENT_TIMESTAMP
                  )
                ORDER BY id
                FOR UPDATE SKIP LOCKED
                LIMIT %s
            )
            UPDATE {ARTWORK_TABLE} AS artwork
            SET
                "txtVecLeaseOwner" = %s,
                "txtVecLeaseExpiresAt" = CURRENT_TIMESTAMP
                    + (%s * INTERVAL '1 second'),
                "txtVecAttemptCount" = "txtVecAttemptCount" + 1,
                "txtVecLastError" = NULL
            FROM candidates
            WHERE artwork.id = candidates.id
            RETURNING
                artwork.id,
                artwork."objectId" AS object_id,
                artwork.title,
                artwork.artist,
                artwork.date,
                artwork.medium,
                artwork.department,
                artwork.culture,
                artwork.classification,
                artwork.description,
                artwork."artistNationality" AS artist_nationality,
                artwork."txtVecAttemptCount" AS attempt_count
            """,
            (
                max_attempts,
                batch_size,
                owner,
                lease_seconds,
            ),
        )
        rows = cursor.fetchall()
    connection.commit()
    return rows


def store_embeddings(connection, owner, rows, embeddings):
    updates = [
        (row["id"], vector_literal(embedding), owner)
        for row, embedding in zip(rows, embeddings, strict=True)
    ]
    with connection.cursor() as cursor:
        execute_values(
            cursor,
            f"""
            UPDATE {ARTWORK_TABLE} AS artwork
            SET
                "txtVec" = batch.embedding::vector,
                "txtVecLeaseOwner" = NULL,
                "txtVecLeaseExpiresAt" = NULL,
                "txtVecNextAttemptAt" = NULL,
                "txtVecLastError" = NULL
            FROM (VALUES %s) AS batch(id, embedding, owner)
            WHERE artwork.id = batch.id
              AND artwork."txtVecLeaseOwner" = batch.owner
            """,
            updates,
            template="(%s, %s, %s)",
            page_size=len(updates),
        )
    connection.commit()


def record_empty_metadata(connection, owner, rows, max_attempts):
    ids = [row["id"] for row in rows]
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            UPDATE {ARTWORK_TABLE}
            SET
                "txtVecAttemptCount" = %s,
                "txtVecLeaseOwner" = NULL,
                "txtVecLeaseExpiresAt" = NULL,
                "txtVecNextAttemptAt" = NULL,
                "txtVecLastError" =
                    'metadata embedding: no searchable metadata'
            WHERE id = ANY(%s)
              AND "txtVecLeaseOwner" = %s
            """,
            (max_attempts, ids, owner),
        )
    connection.commit()


def record_failure(connection, owner, rows, max_attempts, error):
    updates = []
    for row in rows:
        terminal = row["attempt_count"] >= max_attempts
        delay = (
            None
            if terminal
            else min(30 * (2 ** max(row["attempt_count"] - 1, 0)), 900)
        )
        updates.append(
            (
                row["id"],
                delay,
                f"metadata embedding: {type(error).__name__}: {error}",
                owner,
            )
        )
    with connection.cursor() as cursor:
        execute_values(
            cursor,
            f"""
            UPDATE {ARTWORK_TABLE} AS artwork
            SET
                "txtVecLeaseOwner" = NULL,
                "txtVecLeaseExpiresAt" = NULL,
                "txtVecNextAttemptAt" = CASE
                    WHEN batch.delay_seconds IS NULL THEN NULL
                    ELSE CURRENT_TIMESTAMP
                        + (
                            batch.delay_seconds
                            * INTERVAL '1 second'
                        )
                END,
                "txtVecLastError" = batch.error
            FROM (VALUES %s) AS batch(
                id,
                delay_seconds,
                error,
                owner
            )
            WHERE artwork.id = batch.id
              AND artwork."txtVecLeaseOwner" = batch.owner
            """,
            updates,
            template="(%s, %s, %s, %s)",
            page_size=len(updates),
        )
    connection.commit()
    return sum(
        row["attempt_count"] >= max_attempts
        for row in rows
    )


def embed_batch(client, model, texts):
    response = client.embeddings.create(
        model=model,
        input=texts,
        dimensions=EMBEDDING_DIMENSIONS,
        encoding_format="float",
    )
    embeddings = [
        item.embedding
        for item in sorted(response.data, key=lambda item: item.index)
    ]
    total_tokens = response.usage.total_tokens if response.usage else 0
    return embeddings, total_tokens


def run_worker(args):
    connection = psycopg2.connect(database_url())
    client = OpenAI(
        api_key=os.getenv("OPENAI_API_KEY"),
        max_retries=2,
        timeout=args.timeout,
    )
    rate_limiter = TokenRateLimiter(
        args.token_rate_limit_file,
        args.tokens_per_minute,
    )
    owner = worker_id()
    processed = 0
    embedded = 0
    terminal_failures = 0
    retryable_failures = 0
    total_tokens = 0
    started_at = time.monotonic()

    try:
        while processed < args.limit:
            rows = claim_batch(
                connection,
                owner=owner,
                batch_size=min(args.batch_size, args.limit - processed),
                max_attempts=args.max_attempts,
                lease_seconds=args.lease_seconds,
            )
            if not rows:
                break

            valid_rows = []
            texts = []
            empty_rows = []
            for row in rows:
                text = create_artwork_text(row)
                if text:
                    valid_rows.append(row)
                    texts.append(text)
                else:
                    empty_rows.append(row)

            if empty_rows:
                record_empty_metadata(
                    connection,
                    owner,
                    empty_rows,
                    args.max_attempts,
                )
                terminal_failures += len(empty_rows)

            if valid_rows:
                for batch_rows, batch_texts in split_embedding_batches(
                    valid_rows,
                    texts,
                ):
                    try:
                        rate_limiter.reserve(batch_texts)
                        embeddings, token_count = embed_batch(
                            client,
                            args.model,
                            batch_texts,
                        )
                        if len(embeddings) != len(batch_rows):
                            raise RuntimeError(
                                "OpenAI returned an unexpected embedding count"
                            )
                        store_embeddings(
                            connection,
                            owner,
                            batch_rows,
                            embeddings,
                        )
                        embedded += len(batch_rows)
                        total_tokens += token_count
                    except Exception as error:
                        connection.rollback()
                        terminal = record_failure(
                            connection,
                            owner,
                            batch_rows,
                            args.max_attempts,
                            error,
                        )
                        terminal_failures += terminal
                        retryable_failures += len(batch_rows) - terminal

            processed += len(rows)
            print(
                json.dumps(
                    {
                        "processed": processed,
                        "embedded": embedded,
                        "retryableFailures": retryable_failures,
                        "terminalFailures": terminal_failures,
                        "tokens": total_tokens,
                    },
                    separators=(",", ":"),
                ),
                flush=True,
            )
    finally:
        connection.close()

    duration_ms = round((time.monotonic() - started_at) * 1000)
    return {
        "processed": processed,
        "embedded": embedded,
        "retryableFailures": retryable_failures,
        "terminalFailures": terminal_failures,
        "tokens": total_tokens,
        "durationMs": duration_ms,
        "artworksPerSecond": (
            round(processed / (duration_ms / 1000), 3)
            if duration_ms
            else 0
        ),
    }


def stats(max_attempts):
    connection = psycopg2.connect(database_url())
    try:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                f"""
                SELECT
                    COUNT(*) FILTER (
                        WHERE "imageAssetId" IS NOT NULL
                          AND "imgVec" IS NOT NULL
                    )::int AS image_searchable,
                    COUNT(*) FILTER (
                        WHERE "imageAssetId" IS NOT NULL
                          AND "imgVec" IS NOT NULL
                          AND "txtVec" IS NOT NULL
                    )::int AS fully_searchable,
                    COUNT(*) FILTER (
                        WHERE "imageAssetId" IS NOT NULL
                          AND "imgVec" IS NOT NULL
                          AND "txtVec" IS NULL
                          AND "txtVecAttemptCount" < %s
                    )::int AS pending,
                    COUNT(*) FILTER (
                        WHERE "imageAssetId" IS NOT NULL
                          AND "imgVec" IS NOT NULL
                          AND "txtVec" IS NULL
                          AND "txtVecAttemptCount" >= %s
                    )::int AS terminal_failures,
                    COUNT(*) FILTER (
                        WHERE "txtVecLeaseExpiresAt"
                            > CURRENT_TIMESTAMP
                    )::int AS leased
                FROM {ARTWORK_TABLE}
                """,
                (max_attempts, max_attempts),
            )
            result = cursor.fetchone()
        connection.commit()
    finally:
        connection.close()
    return result


def parse_args():
    parser = argparse.ArgumentParser(
        description="Durable metadata embedding worker"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    work_parser = subparsers.add_parser("work")
    work_parser.add_argument("--limit", type=int, default=1000000)
    work_parser.add_argument("--batch-size", type=int, default=100)
    work_parser.add_argument("--max-attempts", type=int, default=5)
    work_parser.add_argument("--lease-seconds", type=int, default=300)
    work_parser.add_argument("--timeout", type=float, default=90)
    work_parser.add_argument(
        "--tokens-per-minute",
        type=int,
        default=950000,
    )
    work_parser.add_argument(
        "--token-rate-limit-file",
        default="/tmp/met-galaxy-text-embedding-rate-limit.json",
    )
    work_parser.add_argument("--model", default=DEFAULT_MODEL)

    stats_parser = subparsers.add_parser("stats")
    stats_parser.add_argument("--max-attempts", type=int, default=5)
    return parser.parse_args()


def main():
    args = parse_args()
    if args.command == "stats":
        print(json.dumps(stats(args.max_attempts), indent=2))
        return
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is required")
    if (
        args.limit < 1
        or args.batch_size < 1
        or args.batch_size > 100
        or args.max_attempts < 1
        or args.lease_seconds < 1
        or args.tokens_per_minute < 1
    ):
        raise RuntimeError(
            "limit, attempts, and lease must be positive; "
            "batch-size must be 1-100"
        )
    print(json.dumps(run_worker(args), indent=2))


if __name__ == "__main__":
    main()
