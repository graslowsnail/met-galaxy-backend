#!/usr/bin/env python3

import argparse
import concurrent.futures
import hashlib
import io
import json
import logging
import math
import os
import socket
import statistics
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache

import boto3
import psycopg2
import requests
from botocore.exceptions import ClientError
from dotenv import load_dotenv
from PIL import Image, ImageOps, UnidentifiedImageError
from psycopg2.extras import RealDictCursor, execute_values
from psycopg2.pool import ThreadedConnectionPool

load_dotenv()

LOGGER = logging.getLogger("image-ingestion")

FULL_WEBP_QUALITY = 82
GRAPH_THUMBNAIL_SIZE = 512
GRAPH_WEBP_QUALITY = 85
PHASH_THRESHOLD = 8
DHASH_THRESHOLD = 4
PHASH_BAND_COUNT = PHASH_THRESHOLD + 1
DHASH_BAND_COUNT = DHASH_THRESHOLD + 1
IMAGE_CACHE_CONTROL = "public, max-age=31536000, immutable"
USER_AGENT = "Met-Galaxy-Image-Ingestion/1.0 (https://openmetropolitan.com)"
MET_OBJECT_API_BASE_URL = (
    "https://collectionapi.metmuseum.org/public/collection/v1/objects"
)
PHASH_IMAGE_SIZE = 32
PHASH_SIZE = 8
_DCT_ROWS = [
    [
        2
        * math.cos(
            math.pi
            * frequency
            * (2 * position + 1)
            / (2 * PHASH_IMAGE_SIZE)
        )
        for position in range(PHASH_IMAGE_SIZE)
    ]
    for frequency in range(PHASH_SIZE)
]

ARTWORK_TABLE = '"met-galaxy_artwork"'
ASSET_TABLE = '"met-galaxy_image_asset"'
INGESTION_TABLE = '"met-galaxy_image_ingestion"'
ATTEMPT_TABLE = '"met-galaxy_image_ingestion_attempt"'
CANDIDATE_TABLE = '"met-galaxy_image_duplicate_candidate"'
BAND_TABLE = '"met-galaxy_image_perceptual_hash_band"'
OUTBOX_TABLE = '"met-galaxy_image_embedding_outbox"'
_DB_POOL = None
_DB_POOL_LOCK = threading.Lock()


def database_url():
    configured = os.getenv("DATABASE_URL")
    if configured:
        return configured

    secret_arn = os.getenv("DATABASE_SECRET_ARN")
    if not secret_arn:
        raise RuntimeError("DATABASE_URL or DATABASE_SECRET_ARN is required")

    region = os.getenv("AWS_REGION", "us-east-1")
    profile = os.getenv("AWS_PROFILE")
    session = boto3.Session(
        profile_name=profile if profile else None,
        region_name=region,
    )
    response = session.client("secretsmanager").get_secret_value(
        SecretId=secret_arn
    )
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
    image_cdn_base_url: str
    ingestion_queue_url: str | None
    embedding_queue_url: str | None
    fetch_retries: int
    max_attempts: int
    lease_seconds: int
    max_source_bytes: int
    request_connect_timeout: int
    request_read_timeout: int

    @classmethod
    def from_env(cls):
        return cls(
            database_url=database_url(),
            aws_region=os.getenv("AWS_REGION", "us-east-1"),
            s3_bucket_name=os.getenv(
                "S3_BUCKET_NAME",
                "met-artworks-images",
            ),
            image_cdn_base_url=os.getenv(
                "IMAGE_CDN_BASE_URL",
                "https://d2pvxr3eb77vb4.cloudfront.net",
            ).rstrip("/"),
            ingestion_queue_url=os.getenv("IMAGE_INGESTION_QUEUE_URL"),
            embedding_queue_url=os.getenv("IMAGE_EMBEDDING_QUEUE_URL"),
            fetch_retries=int(os.getenv("IMAGE_INGESTION_FETCH_RETRIES", "3")),
            max_attempts=int(os.getenv("IMAGE_INGESTION_MAX_ATTEMPTS", "5")),
            lease_seconds=int(
                os.getenv("IMAGE_INGESTION_LEASE_SECONDS", "900")
            ),
            max_source_bytes=int(
                os.getenv(
                    "IMAGE_INGESTION_MAX_SOURCE_BYTES",
                    str(50 * 1024 * 1024),
                )
            ),
            request_connect_timeout=int(
                os.getenv("IMAGE_INGESTION_CONNECT_TIMEOUT_SECONDS", "10")
            ),
            request_read_timeout=int(
                os.getenv("IMAGE_INGESTION_READ_TIMEOUT_SECONDS", "45")
            ),
        )


@dataclass
class DownloadedSource:
    content: bytes
    sha256: str
    byte_size: int
    attempt_count: int


@dataclass
class ProcessedImage:
    source_sha256: str
    source_byte_size: int
    download_attempt_count: int
    normalized_pixel_sha256: str
    perceptual_hash: str
    difference_hash: str
    width: int
    height: int
    full_webp: bytes
    thumbnail_webp: bytes
    full_sha256: str
    thumbnail_sha256: str


@dataclass
class WorkResult:
    artwork_id: int
    outcome: str
    dry_run: bool
    image_asset_id: int | None = None
    review_candidate_count: int = 0
    source_bytes: int = 0
    written_bytes: int = 0
    download_attempts: int = 0
    estimated_cost_micro_usd: int = 0
    error_stage: str | None = None
    error: str | None = None
    retry_after_seconds: int | None = None


class PipelineFailure(Exception):
    def __init__(
        self,
        stage,
        message,
        *,
        retryable=True,
        download_attempt_count=0,
        source_byte_size=0,
    ):
        super().__init__(message)
        self.stage = stage
        self.retryable = retryable
        self.download_attempt_count = download_attempt_count
        self.source_byte_size = source_byte_size


def connect(settings):
    pool_size = int(os.getenv("IMAGE_INGESTION_DB_POOL_SIZE", "0"))
    global _DB_POOL
    for attempt in range(5):
        try:
            if pool_size <= 0:
                return psycopg2.connect(settings.database_url)
            if _DB_POOL is None:
                with _DB_POOL_LOCK:
                    if _DB_POOL is None:
                        _DB_POOL = ThreadedConnectionPool(
                            1,
                            pool_size,
                            settings.database_url,
                        )
            return _DB_POOL.getconn()
        except psycopg2.OperationalError:
            if attempt == 4:
                raise
            time.sleep(2**attempt)


def close_connection(connection):
    if _DB_POOL is None:
        connection.close()
        return
    _DB_POOL.putconn(connection, close=bool(connection.closed))


def close_connection_pool():
    global _DB_POOL
    if _DB_POOL is not None:
        _DB_POOL.closeall()
        _DB_POOL = None


@lru_cache(maxsize=8)
def aws_client(service_name, region, profile):
    session = boto3.Session(
        profile_name=profile if profile else None,
        region_name=region,
    )
    return session.client(service_name)


def service_client(service_name, settings):
    return aws_client(
        service_name,
        settings.aws_region,
        os.getenv("AWS_PROFILE"),
    )


def worker_id():
    configured = os.getenv("IMAGE_INGESTION_WORKER_ID")
    if configured:
        return configured
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


def digest_bytes(content):
    return hashlib.sha256(content).hexdigest()


def normalized_pixel_digest(image):
    rgba = image.convert("RGBA")
    digest = hashlib.sha256()
    digest.update(f"{rgba.width}x{rgba.height}:RGBA:".encode())
    digest.update(rgba.tobytes())
    return digest.hexdigest()


def alpha_present(image):
    if image.mode not in ("RGBA", "LA") and "transparency" not in image.info:
        return False
    return image.convert("RGBA").getchannel("A").getextrema()[0] < 255


def composite_rgb(image):
    rgba = image.convert("RGBA")
    background = Image.new("RGBA", rgba.size, "white")
    return Image.alpha_composite(background, rgba).convert("RGB")


def hash_bits(bits):
    bit_string = "".join("1" if bit else "0" for bit in bits)
    return f"{int(bit_string, 2):016x}"


def perceptual_hash64(image):
    grayscale = image.convert("L").resize(
        (PHASH_IMAGE_SIZE, PHASH_IMAGE_SIZE),
        Image.Resampling.LANCZOS,
    )
    pixels = list(grayscale.get_flattened_data())
    vertical = []
    for frequency in range(PHASH_SIZE):
        weights = _DCT_ROWS[frequency]
        vertical.append(
            [
                sum(
                    pixels[row * PHASH_IMAGE_SIZE + column]
                    * weights[row]
                    for row in range(PHASH_IMAGE_SIZE)
                )
                for column in range(PHASH_IMAGE_SIZE)
            ]
        )

    low_frequency = []
    for row in range(PHASH_SIZE):
        for frequency in range(PHASH_SIZE):
            weights = _DCT_ROWS[frequency]
            low_frequency.append(
                2
                * sum(
                    vertical[row][column] * weights[column]
                    for column in range(PHASH_IMAGE_SIZE)
                )
            )
    median = statistics.median(low_frequency)
    return hash_bits(value > median for value in low_frequency)


def difference_hash64(image):
    grayscale = image.convert("L").resize(
        (PHASH_SIZE + 1, PHASH_SIZE),
        Image.Resampling.LANCZOS,
    )
    pixels = list(grayscale.get_flattened_data())
    row_width = PHASH_SIZE + 1
    return hash_bits(
        pixels[row * row_width + column + 1]
        > pixels[row * row_width + column]
        for row in range(PHASH_SIZE)
        for column in range(PHASH_SIZE)
    )


def encode_webp(image, quality):
    output = io.BytesIO()
    image.save(
        output,
        format="WEBP",
        quality=quality,
        method=4,
        exact=True,
    )
    return output.getvalue()


def process_source(downloaded):
    try:
        with Image.open(io.BytesIO(downloaded.content)) as source:
            source.load()
            oriented = ImageOps.exif_transpose(source)
            if oriented.width <= 0 or oriented.height <= 0:
                raise ValueError("decoded image has invalid dimensions")

            has_alpha = alpha_present(oriented)
            encoding_source = oriented.convert("RGBA" if has_alpha else "RGB")
            hash_source = composite_rgb(oriented)
            normalized_sha256 = normalized_pixel_digest(oriented)
            perceptual_hash = perceptual_hash64(hash_source)
            difference_hash = difference_hash64(hash_source)
            full_webp = encode_webp(
                encoding_source,
                FULL_WEBP_QUALITY,
            )
            thumbnail = encoding_source.copy()
            thumbnail.thumbnail(
                (GRAPH_THUMBNAIL_SIZE, GRAPH_THUMBNAIL_SIZE),
                Image.Resampling.LANCZOS,
            )
            thumbnail_webp = encode_webp(
                thumbnail,
                GRAPH_WEBP_QUALITY,
            )

            return ProcessedImage(
                source_sha256=downloaded.sha256,
                source_byte_size=downloaded.byte_size,
                download_attempt_count=downloaded.attempt_count,
                normalized_pixel_sha256=normalized_sha256,
                perceptual_hash=perceptual_hash,
                difference_hash=difference_hash,
                width=oriented.width,
                height=oriented.height,
                full_webp=full_webp,
                thumbnail_webp=thumbnail_webp,
                full_sha256=digest_bytes(full_webp),
                thumbnail_sha256=digest_bytes(thumbnail_webp),
            )
    except (UnidentifiedImageError, OSError, ValueError) as error:
        raise PipelineFailure(
            "conversion",
            str(error),
            download_attempt_count=downloaded.attempt_count,
            source_byte_size=downloaded.byte_size,
        ) from error


def retry_delay_seconds(attempt):
    return min(60 * (2 ** max(attempt - 1, 0)), 6 * 60 * 60)


def fetch_source(source_url, settings):
    last_error = None
    downloaded_bytes = 0

    for attempt in range(1, settings.fetch_retries + 1):
        try:
            with requests.get(
                source_url,
                headers={"User-Agent": USER_AGENT},
                timeout=(
                    settings.request_connect_timeout,
                    settings.request_read_timeout,
                ),
                stream=True,
            ) as response:
                retryable_status = (
                    response.status_code == 404
                    or response.status_code in (408, 409, 425, 429)
                    or response.status_code >= 500
                )
                if not response.ok:
                    raise PipelineFailure(
                        "download",
                        f"HTTP {response.status_code}: {response.reason}",
                        retryable=retryable_status,
                        download_attempt_count=attempt,
                    )

                declared_size = response.headers.get("Content-Length")
                if (
                    declared_size
                    and int(declared_size) > settings.max_source_bytes
                ):
                    raise PipelineFailure(
                        "download",
                        "source exceeds configured byte limit",
                        retryable=False,
                        download_attempt_count=attempt,
                    )

                content = io.BytesIO()
                digest = hashlib.sha256()
                downloaded_bytes = 0
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    downloaded_bytes += len(chunk)
                    if downloaded_bytes > settings.max_source_bytes:
                        raise PipelineFailure(
                            "download",
                            "source exceeds configured byte limit",
                            retryable=False,
                            download_attempt_count=attempt,
                            source_byte_size=downloaded_bytes,
                        )
                    digest.update(chunk)
                    content.write(chunk)

                if downloaded_bytes == 0:
                    raise PipelineFailure(
                        "download",
                        "source response was empty",
                        download_attempt_count=attempt,
                    )

                return DownloadedSource(
                    content=content.getvalue(),
                    sha256=digest.hexdigest(),
                    byte_size=downloaded_bytes,
                    attempt_count=attempt,
                )
        except PipelineFailure as error:
            last_error = error
            if not error.retryable:
                raise
        except requests.RequestException as error:
            last_error = PipelineFailure(
                "download",
                str(error),
                download_attempt_count=attempt,
                source_byte_size=downloaded_bytes,
            )

        if attempt < settings.fetch_retries:
            time.sleep(min(2 ** (attempt - 1), 4))

    if last_error:
        raise last_error
    raise PipelineFailure("download", "source download failed")


def fetch_source_candidates(source_urls, settings):
    attempted_urls = []
    failures = []
    attempt_count = 0
    source_byte_size = 0

    for source_url in source_urls:
        if not source_url or source_url in attempted_urls:
            continue
        attempted_urls.append(source_url)
        try:
            downloaded = fetch_source(source_url, settings)
            return (
                DownloadedSource(
                    content=downloaded.content,
                    sha256=downloaded.sha256,
                    byte_size=downloaded.byte_size,
                    attempt_count=(
                        attempt_count + downloaded.attempt_count
                    ),
                ),
                source_url,
            )
        except PipelineFailure as error:
            failures.append(error)
            attempt_count += error.download_attempt_count
            source_byte_size += error.source_byte_size

    if not failures:
        raise PipelineFailure(
            "download",
            "artwork has no image source URL",
            retryable=False,
        )
    raise PipelineFailure(
        "download",
        "; ".join(str(error) for error in failures),
        retryable=any(error.retryable for error in failures),
        download_attempt_count=attempt_count,
        source_byte_size=source_byte_size,
    )


def refresh_met_image_urls(object_id, settings):
    try:
        response = requests.get(
            f"{MET_OBJECT_API_BASE_URL}/{object_id}",
            headers={"User-Agent": USER_AGENT},
            timeout=(
                settings.request_connect_timeout,
                settings.request_read_timeout,
            ),
        )
    except requests.RequestException as error:
        raise PipelineFailure(
            "download",
            f"Met object API request failed: {error}",
            download_attempt_count=1,
        ) from error

    if not response.ok:
        raise PipelineFailure(
            "download",
            f"Met object API HTTP {response.status_code}",
            retryable=response.status_code != 404,
            download_attempt_count=1,
        )
    try:
        payload = response.json()
    except requests.JSONDecodeError as error:
        raise PipelineFailure(
            "download",
            "Met object API returned invalid JSON",
            download_attempt_count=1,
        ) from error

    primary_url = payload.get("primaryImage")
    fallback_url = payload.get("primaryImageSmall")
    if not primary_url and not fallback_url:
        raise PipelineFailure(
            "download",
            "Met object API record has no image URL",
            retryable=False,
            download_attempt_count=1,
        )
    return primary_url, fallback_url


def fetch_s3_source(source_s3_key, settings):
    try:
        response = service_client("s3", settings).get_object(
            Bucket=settings.s3_bucket_name,
            Key=source_s3_key,
        )
        content = io.BytesIO()
        digest = hashlib.sha256()
        downloaded_bytes = 0
        for chunk in response["Body"].iter_chunks(chunk_size=64 * 1024):
            if not chunk:
                continue
            downloaded_bytes += len(chunk)
            if downloaded_bytes > settings.max_source_bytes:
                raise PipelineFailure(
                    "download",
                    "legacy source exceeds configured byte limit",
                    retryable=False,
                    download_attempt_count=1,
                    source_byte_size=downloaded_bytes,
                )
            digest.update(chunk)
            content.write(chunk)
        if downloaded_bytes == 0:
            raise PipelineFailure(
                "download",
                "legacy S3 object was empty",
                retryable=False,
                download_attempt_count=1,
            )
        return DownloadedSource(
            content=content.getvalue(),
            sha256=digest.hexdigest(),
            byte_size=downloaded_bytes,
            attempt_count=1,
        )
    except PipelineFailure:
        raise
    except ClientError as error:
        code = error.response.get("Error", {}).get("Code", "unknown")
        raise PipelineFailure(
            "download",
            f"S3 {code} for {source_s3_key}",
            retryable=code not in ("AccessDenied", "NoSuchKey"),
            download_attempt_count=1,
        ) from error


def asset_keys(normalized_sha256):
    digest = normalized_sha256.lower()
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("normalized digest must be 64 lowercase hex characters")
    prefix = f"assets/v1/{digest[:2]}/{digest}"
    return f"{prefix}/full.webp", f"{prefix}/graph.webp"


def split_hash_bands(hash_value, band_count):
    bits = f"{int(hash_value, 16):064b}"
    base_size, remainder = divmod(64, band_count)
    bands = []
    cursor = 0
    for index in range(band_count):
        width = base_size + (1 if index < remainder else 0)
        band_bits = bits[cursor : cursor + width]
        bands.append((index, f"{width}:{int(band_bits, 2):x}"))
        cursor += width
    return bands


def hamming_distance(left, right):
    return (int(left, 16) ^ int(right, 16)).bit_count()


def get_artwork(connection, artwork_id):
    with connection.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute(
            f"""
            SELECT
                artwork.id,
                artwork."objectId" AS object_id,
                artwork."primaryImage" AS source_url,
                artwork."primaryImageSmall" AS fallback_source_url,
                artwork."localImageUrl" AS local_image_url,
                artwork."imageAssetId" AS image_asset_id,
                artwork."imgVec" AS image_embedding,
                asset."processingStatus" AS asset_status,
                asset."fullS3Key" AS full_s3_key,
                ingestion.status AS ingestion_status,
                ingestion."lastError" AS ingestion_last_error
            FROM {ARTWORK_TABLE} artwork
            LEFT JOIN {ASSET_TABLE} asset
              ON asset.id = artwork."imageAssetId"
            LEFT JOIN {INGESTION_TABLE} ingestion
              ON ingestion."artworkId" = artwork.id
            WHERE artwork.id = %s
            """,
            (artwork_id,),
        )
        return cursor.fetchone()


def ensure_ingestion(connection, artwork):
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            INSERT INTO {INGESTION_TABLE} (
                "artworkId",
                "sourceUrl",
                status
            )
            VALUES (%s, %s, 'pending')
            ON CONFLICT ("artworkId") DO UPDATE
            SET
                "sourceUrl" = EXCLUDED."sourceUrl",
                "updatedAt" = CURRENT_TIMESTAMP
            WHERE {INGESTION_TABLE}.status IN (
                'pending',
                'processing',
                'retryable_failure',
                'terminal_failure'
            )
            """,
            (artwork["id"], artwork["source_url"]),
        )
    connection.commit()


def claim_ingestion(connection, artwork_id, owner, settings):
    lease_expires_at = datetime.now(UTC) + timedelta(
        seconds=settings.lease_seconds
    )
    with connection.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute(
            f"""
            UPDATE {INGESTION_TABLE}
            SET
                status = 'processing',
                "attemptCount" = "attemptCount" + 1,
                "leaseOwner" = %s,
                "leaseExpiresAt" = %s,
                "nextAttemptAt" = NULL,
                "lastError" = NULL,
                "updatedAt" = CURRENT_TIMESTAMP
            WHERE "artworkId" = %s
              AND (
                status IN ('pending', 'retryable_failure')
                OR (
                    status = 'processing'
                    AND "leaseExpiresAt" < CURRENT_TIMESTAMP
                )
              )
              AND (
                "nextAttemptAt" IS NULL
                OR "nextAttemptAt" <= CURRENT_TIMESTAMP
              )
            RETURNING "attemptCount"
            """,
            (owner, lease_expires_at, artwork_id),
        )
        row = cursor.fetchone()
    connection.commit()
    return row["attemptCount"] if row else None


def update_source_digest(
    connection,
    artwork_id,
    source_sha256,
    source_url=None,
):
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            UPDATE {INGESTION_TABLE}
            SET
                "sourceSha256" = %s,
                "sourceUrl" = COALESCE(%s, "sourceUrl"),
                "updatedAt" = CURRENT_TIMESTAMP
            WHERE "artworkId" = %s
            """,
            (source_sha256, source_url, artwork_id),
        )
    connection.commit()


def update_artwork_image_urls(
    connection,
    artwork_id,
    primary_url,
    fallback_url,
):
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            UPDATE {ARTWORK_TABLE}
            SET
                "primaryImage" = %s,
                "primaryImageSmall" = %s
            WHERE id = %s
              AND (
                "primaryImage" IS DISTINCT FROM %s
                OR "primaryImageSmall" IS DISTINCT FROM %s
              )
            """,
            (
                primary_url,
                fallback_url,
                artwork_id,
                primary_url,
                fallback_url,
            ),
        )
    connection.commit()


def find_source_exact_asset(connection, source_sha256, artwork_id):
    with connection.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute(
            f"""
            SELECT
                asset.id,
                asset."processingStatus" AS processing_status,
                asset."fullS3Key" AS full_s3_key,
                asset."normalizedPixelSha256" AS normalized_pixel_sha256,
                asset."perceptualHash" AS perceptual_hash,
                asset."differenceHash" AS difference_hash
            FROM {INGESTION_TABLE} ingestion
            JOIN {ARTWORK_TABLE} artwork
              ON artwork.id = ingestion."artworkId"
            JOIN {ASSET_TABLE} asset
              ON asset.id = artwork."imageAssetId"
            WHERE ingestion."sourceSha256" = %s
              AND ingestion."artworkId" <> %s
              AND asset."processingStatus" IN ('pending_embedding', 'ready')
            ORDER BY asset.id
            LIMIT 1
            """,
            (source_sha256, artwork_id),
        )
        return cursor.fetchone()


def find_normalized_exact_asset(connection, normalized_sha256):
    with connection.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute(
            f"""
            SELECT
                id,
                "processingStatus" AS processing_status,
                "fullS3Key" AS full_s3_key,
                "normalizedPixelSha256" AS normalized_pixel_sha256,
                "perceptualHash" AS perceptual_hash,
                "differenceHash" AS difference_hash,
                "processingLeaseOwner" AS processing_lease_owner,
                "processingLeaseExpiresAt" AS processing_lease_expires_at
            FROM {ASSET_TABLE}
            WHERE "normalizedPixelSha256" = %s
            """,
            (normalized_sha256,),
        )
        return cursor.fetchone()


def candidate_asset_ids(connection, perceptual_hash, difference_hash, exclude_id):
    phash_bands = split_hash_bands(perceptual_hash, PHASH_BAND_COUNT)
    dhash_bands = split_hash_bands(difference_hash, DHASH_BAND_COUNT)

    def conditions(alias, bands):
        return " OR ".join(
            f'({alias}."bandIndex" = %s AND {alias}."bandValue" = %s)'
            for _ in bands
        )

    parameters = []
    for band in phash_bands:
        parameters.extend(band)
    for band in dhash_bands:
        parameters.extend(band)
    parameters.append(exclude_id or -1)

    with connection.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute(
            f"""
            SELECT DISTINCT
                asset.id,
                asset."perceptualHash" AS perceptual_hash,
                asset."differenceHash" AS difference_hash
            FROM {ASSET_TABLE} asset
            JOIN {BAND_TABLE} phash_band
              ON phash_band."imageAssetId" = asset.id
             AND phash_band.algorithm = 'phash64'
            JOIN {BAND_TABLE} dhash_band
              ON dhash_band."imageAssetId" = asset.id
             AND dhash_band.algorithm = 'dhash64'
            WHERE ({conditions("phash_band", phash_bands)})
              AND ({conditions("dhash_band", dhash_bands)})
              AND asset.id <> %s
              AND asset."perceptualHash" IS NOT NULL
              AND asset."differenceHash" IS NOT NULL
            """,
            parameters,
        )
        possible = cursor.fetchall()

    candidates = []
    for asset in possible:
        phash_distance = hamming_distance(
            perceptual_hash,
            asset["perceptual_hash"],
        )
        dhash_distance = hamming_distance(
            difference_hash,
            asset["difference_hash"],
        )
        if (
            phash_distance <= PHASH_THRESHOLD
            and dhash_distance <= DHASH_THRESHOLD
        ):
            candidates.append(
                {
                    "image_asset_id": asset["id"],
                    "perceptual_hash_distance": phash_distance,
                    "difference_hash_distance": dhash_distance,
                }
            )
    return sorted(candidates, key=lambda item: item["image_asset_id"])


def insert_hash_bands(cursor, image_asset_id, perceptual_hash, difference_hash):
    rows = [
        (image_asset_id, "phash64", band_index, band_value)
        for band_index, band_value in split_hash_bands(
            perceptual_hash,
            PHASH_BAND_COUNT,
        )
    ]
    rows.extend(
        (
            image_asset_id,
            "dhash64",
            band_index,
            band_value,
        )
        for band_index, band_value in split_hash_bands(
            difference_hash,
            DHASH_BAND_COUNT,
        )
    )
    execute_values(
        cursor,
        f"""
        INSERT INTO {BAND_TABLE} (
            "imageAssetId",
            algorithm,
            "bandIndex",
            "bandValue"
        )
        VALUES %s
        ON CONFLICT DO NOTHING
        """,
        rows,
    )


def persist_hash_bands(
    connection,
    image_asset_id,
    perceptual_hash,
    difference_hash,
):
    with connection.cursor() as cursor:
        insert_hash_bands(
            cursor,
            image_asset_id,
            perceptual_hash,
            difference_hash,
        )
    connection.commit()


def insert_review_candidates(cursor, image_asset_id, candidates):
    for candidate in candidates:
        left_id, right_id = sorted(
            (image_asset_id, candidate["image_asset_id"])
        )
        cursor.execute(
            f"""
            INSERT INTO {CANDIDATE_TABLE} (
                "imageAssetAId",
                "imageAssetBId",
                status,
                "perceptualHashDistance",
                "differenceHashDistance"
            )
            VALUES (%s, %s, 'review_candidate', %s, %s)
            ON CONFLICT ("imageAssetAId", "imageAssetBId") DO UPDATE
            SET
                "perceptualHashDistance" = LEAST(
                    COALESCE(
                        {CANDIDATE_TABLE}."perceptualHashDistance",
                        EXCLUDED."perceptualHashDistance"
                    ),
                    EXCLUDED."perceptualHashDistance"
                ),
                "differenceHashDistance" = LEAST(
                    COALESCE(
                        {CANDIDATE_TABLE}."differenceHashDistance",
                        EXCLUDED."differenceHashDistance"
                    ),
                    EXCLUDED."differenceHashDistance"
                )
            """,
            (
                left_id,
                right_id,
                candidate["perceptual_hash_distance"],
                candidate["difference_hash_distance"],
            ),
        )


def record_attempt(
    cursor,
    *,
    artwork_id,
    dry_run,
    outcome,
    duration_ms,
    source_sha256=None,
    normalized_pixel_sha256=None,
    perceptual_hash=None,
    difference_hash=None,
    matched_image_asset_id=None,
    candidates=None,
    download_attempt_count=0,
    source_byte_size=None,
    full_byte_size=None,
    thumbnail_byte_size=None,
    estimated_cost_micro_usd=0,
    error_stage=None,
    error=None,
):
    candidate_ids = [
        candidate["image_asset_id"] for candidate in (candidates or [])
    ]
    cursor.execute(
        f"""
        INSERT INTO {ATTEMPT_TABLE} (
            "artworkId",
            "dryRun",
            outcome,
            "sourceSha256",
            "normalizedPixelSha256",
            "perceptualHash",
            "differenceHash",
            "matchedImageAssetId",
            "reviewCandidateImageAssetIds",
            "reviewCandidateCount",
            "downloadAttemptCount",
            "sourceByteSize",
            "fullByteSize",
            "thumbnailByteSize",
            "estimatedCostMicroUsd",
            "durationMs",
            "errorStage",
            error
        )
        VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s
        )
        """,
        (
            artwork_id,
            dry_run,
            outcome,
            source_sha256,
            normalized_pixel_sha256,
            perceptual_hash,
            difference_hash,
            matched_image_asset_id,
            json.dumps(candidate_ids),
            len(candidate_ids),
            download_attempt_count,
            source_byte_size,
            full_byte_size,
            thumbnail_byte_size,
            estimated_cost_micro_usd,
            duration_ms,
            error_stage,
            error,
        ),
    )


def ingestion_status_for_asset(asset_status):
    return "complete" if asset_status == "ready" else "awaiting_embedding"


def promote_existing_embedding(connection, asset, image_embedding):
    if image_embedding is None or asset["processing_status"] == "ready":
        return asset
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            UPDATE {ASSET_TABLE}
            SET
                "imageEmbedding" = COALESCE(
                    "imageEmbedding",
                    %s::vector
                ),
                "processingStatus" = 'ready',
                "lastError" = NULL,
                "updatedAt" = CURRENT_TIMESTAMP
            WHERE id = %s
            """,
            (image_embedding, asset["id"]),
        )
        cursor.execute(
            f"""
            UPDATE {INGESTION_TABLE} ingestion
            SET
                status = 'complete',
                "completedAt" = CURRENT_TIMESTAMP,
                "updatedAt" = CURRENT_TIMESTAMP
            FROM {ARTWORK_TABLE} artwork
            WHERE artwork.id = ingestion."artworkId"
              AND artwork."imageAssetId" = %s
              AND ingestion.status = 'awaiting_embedding'
            """,
            (asset["id"],),
        )
    connection.commit()
    return {**asset, "processing_status": "ready"}


def link_existing_asset(
    connection,
    settings,
    *,
    artwork_id,
    source_sha256,
    asset,
    dry_run,
    started_at,
    downloaded,
):
    duration_ms = round((time.monotonic() - started_at) * 1000)
    if dry_run:
        with connection.cursor() as cursor:
            record_attempt(
                cursor,
                artwork_id=artwork_id,
                dry_run=True,
                outcome="exact_duplicate",
                duration_ms=duration_ms,
                source_sha256=source_sha256,
                normalized_pixel_sha256=asset.get(
                    "normalized_pixel_sha256"
                ),
                perceptual_hash=asset.get("perceptual_hash"),
                difference_hash=asset.get("difference_hash"),
                matched_image_asset_id=asset["id"],
                download_attempt_count=downloaded.attempt_count,
                source_byte_size=downloaded.byte_size,
            )
        connection.commit()
    else:
        canonical_url = (
            f"{settings.image_cdn_base_url}/{asset['full_s3_key']}"
        )
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                UPDATE {ARTWORK_TABLE} AS artwork
                SET
                    "imageAssetId" = %s,
                    "imageDuplicateState" = 'exact_duplicate',
                    "localImageUrl" = %s,
                    "imgVec" = asset."imageEmbedding"
                FROM {ASSET_TABLE} AS asset
                WHERE artwork.id = %s
                  AND asset.id = %s
                """,
                (
                    asset["id"],
                    canonical_url,
                    artwork_id,
                    asset["id"],
                ),
            )
            cursor.execute(
                f"""
                UPDATE {INGESTION_TABLE}
                SET
                    "sourceSha256" = %s,
                    status = %s,
                    "leaseOwner" = NULL,
                    "leaseExpiresAt" = NULL,
                    "lastError" = NULL,
                    "completedAt" = CASE
                        WHEN %s = 'complete' THEN CURRENT_TIMESTAMP
                        ELSE NULL
                    END,
                    "updatedAt" = CURRENT_TIMESTAMP
                WHERE "artworkId" = %s
                """,
                (
                    source_sha256,
                    ingestion_status_for_asset(asset["processing_status"]),
                    ingestion_status_for_asset(asset["processing_status"]),
                    artwork_id,
                ),
            )
            record_attempt(
                cursor,
                artwork_id=artwork_id,
                dry_run=False,
                outcome="exact_duplicate",
                duration_ms=duration_ms,
                source_sha256=source_sha256,
                normalized_pixel_sha256=asset.get(
                    "normalized_pixel_sha256"
                ),
                perceptual_hash=asset.get("perceptual_hash"),
                difference_hash=asset.get("difference_hash"),
                matched_image_asset_id=asset["id"],
                download_attempt_count=downloaded.attempt_count,
                source_byte_size=downloaded.byte_size,
            )
        connection.commit()

    return WorkResult(
        artwork_id=artwork_id,
        outcome="exact_duplicate",
        dry_run=dry_run,
        image_asset_id=asset["id"],
        source_bytes=downloaded.byte_size,
        download_attempts=downloaded.attempt_count,
    )


def object_matches(
    s3_client,
    settings,
    key,
    *,
    expected_sha256,
    expected_normalized_sha256,
):
    try:
        response = s3_client.head_object(
            Bucket=settings.s3_bucket_name,
            Key=key,
        )
    except ClientError as error:
        status = error.response.get("ResponseMetadata", {}).get(
            "HTTPStatusCode"
        )
        if status == 404:
            return False
        raise PipelineFailure("upload", str(error)) from error

    metadata = response.get("Metadata", {})
    if (
        metadata.get("sha256") != expected_sha256
        or metadata.get("normalized-sha256") != expected_normalized_sha256
        or response.get("ContentType") != "image/webp"
    ):
        raise PipelineFailure(
            "upload",
            f"existing object metadata mismatch at {key}",
            retryable=False,
        )
    return True


def upload_derivative(
    s3_client,
    settings,
    *,
    key,
    content,
    encoded_sha256,
    normalized_sha256,
    derivative,
):
    if object_matches(
        s3_client,
        settings,
        key,
        expected_sha256=encoded_sha256,
        expected_normalized_sha256=normalized_sha256,
    ):
        return False

    try:
        s3_client.put_object(
            Bucket=settings.s3_bucket_name,
            Key=key,
            Body=content,
            ContentType="image/webp",
            CacheControl=IMAGE_CACHE_CONTROL,
            Metadata={
                "sha256": encoded_sha256,
                "normalized-sha256": normalized_sha256,
                "derivative": derivative,
                "settings-version": "v1",
            },
        )
    except ClientError as error:
        raise PipelineFailure("upload", str(error)) from error
    return True


def reserve_asset(connection, processed, owner, settings):
    full_s3_key, thumbnail_s3_key = asset_keys(
        processed.normalized_pixel_sha256
    )
    lease_expires_at = datetime.now(UTC) + timedelta(
        seconds=settings.lease_seconds
    )

    with connection.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute(
            f"""
            INSERT INTO {ASSET_TABLE} (
                "fullS3Key",
                "thumbnailS3Key",
                "mimeType",
                width,
                height,
                "byteSize",
                "thumbnailByteSize",
                "encodedSha256",
                "thumbnailEncodedSha256",
                "normalizedPixelSha256",
                "perceptualHash",
                "perceptualHashAlgorithm",
                "differenceHash",
                "differenceHashAlgorithm",
                "processingStatus",
                "processingAttemptCount",
                "processingLeaseOwner",
                "processingLeaseExpiresAt"
            )
            VALUES (
                %s, %s, 'image/webp', %s, %s, %s, %s, %s, %s, %s,
                %s, 'imagehash-phash64', %s, 'imagehash-dhash64',
                'pending_upload', 1, %s, %s
            )
            ON CONFLICT ("normalizedPixelSha256") DO NOTHING
            RETURNING id
            """,
            (
                full_s3_key,
                thumbnail_s3_key,
                processed.width,
                processed.height,
                len(processed.full_webp),
                len(processed.thumbnail_webp),
                processed.full_sha256,
                processed.thumbnail_sha256,
                processed.normalized_pixel_sha256,
                processed.perceptual_hash,
                processed.difference_hash,
                owner,
                lease_expires_at,
            ),
        )
        created = cursor.fetchone()
    connection.commit()

    if created:
        return {
            "id": created["id"],
            "created": True,
            "full_s3_key": full_s3_key,
            "thumbnail_s3_key": thumbnail_s3_key,
        }

    existing = find_normalized_exact_asset(
        connection,
        processed.normalized_pixel_sha256,
    )
    if not existing:
        raise PipelineFailure(
            "deduplicate",
            "canonical asset conflict could not be resolved",
        )

    if existing["processing_status"] != "pending_upload":
        return {
            **existing,
            "created": False,
            "thumbnail_s3_key": thumbnail_s3_key,
        }

    with connection.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute(
            f"""
            UPDATE {ASSET_TABLE}
            SET
                "processingLeaseOwner" = %s,
                "processingLeaseExpiresAt" = %s,
                "processingAttemptCount" =
                    "processingAttemptCount" + 1,
                "updatedAt" = CURRENT_TIMESTAMP
            WHERE id = %s
              AND (
                "processingLeaseOwner" IS NULL
                OR "processingLeaseExpiresAt" < CURRENT_TIMESTAMP
                OR "processingLeaseOwner" = %s
              )
            RETURNING id
            """,
            (owner, lease_expires_at, existing["id"], owner),
        )
        claimed = cursor.fetchone()
    connection.commit()

    if not claimed:
        raise PipelineFailure(
            "deduplicate",
            "canonical asset is being uploaded by another worker",
        )
    return {
        **existing,
        "created": False,
        "full_s3_key": full_s3_key,
        "thumbnail_s3_key": thumbnail_s3_key,
    }


def estimate_new_asset_cost_micro_usd(written_bytes):
    put_request_cost = 10
    first_month_storage = round((written_bytes / 1_000_000_000) * 0.023 * 1_000_000)
    return put_request_cost + first_month_storage


def release_failed_asset(connection, asset_id, owner, failure):
    if asset_id is None:
        return
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            UPDATE {ASSET_TABLE}
            SET
                "processingLeaseOwner" = NULL,
                "processingLeaseExpiresAt" = NULL,
                "processingNextAttemptAt" = CURRENT_TIMESTAMP
                    + INTERVAL '1 minute',
                "lastError" = %s,
                "updatedAt" = CURRENT_TIMESTAMP
            WHERE id = %s
              AND "processingStatus" = 'pending_upload'
              AND "processingLeaseOwner" = %s
            """,
            (f"{failure.stage}: {failure}", asset_id, owner),
        )
    connection.commit()


def finalize_new_asset(
    connection,
    settings,
    *,
    artwork_id,
    processed,
    asset,
    candidates,
    started_at,
    estimated_cost_micro_usd,
    image_embedding=None,
):
    duration_ms = round((time.monotonic() - started_at) * 1000)
    asset_status = "ready" if image_embedding is not None else "pending_embedding"
    ingestion_status = (
        "complete" if image_embedding is not None else "awaiting_embedding"
    )
    canonical_url = (
        f"{settings.image_cdn_base_url}/{asset['full_s3_key']}"
    )
    with connection.cursor() as cursor:
        insert_hash_bands(
            cursor,
            asset["id"],
            processed.perceptual_hash,
            processed.difference_hash,
        )
        insert_review_candidates(cursor, asset["id"], candidates)
        cursor.execute(
            f"""
            UPDATE {ASSET_TABLE}
            SET
                "processingStatus" = %s,
                "imageEmbedding" = COALESCE(
                    "imageEmbedding",
                    %s::vector
                ),
                "processingLeaseOwner" = NULL,
                "processingLeaseExpiresAt" = NULL,
                "processingNextAttemptAt" = NULL,
                "lastError" = NULL,
                "updatedAt" = CURRENT_TIMESTAMP
            WHERE id = %s
            """,
            (asset_status, image_embedding, asset["id"]),
        )
        if image_embedding is None:
            cursor.execute(
                f"""
                INSERT INTO {OUTBOX_TABLE} ("imageAssetId")
                VALUES (%s)
                ON CONFLICT ("imageAssetId") DO NOTHING
                """,
                (asset["id"],),
            )
        cursor.execute(
            f"""
            UPDATE {ARTWORK_TABLE} AS artwork
            SET
                "imageAssetId" = %s,
                "imageDuplicateState" = 'unique',
                "localImageUrl" = %s,
                "imgVec" = asset."imageEmbedding"
            FROM {ASSET_TABLE} AS asset
            WHERE artwork.id = %s
              AND asset.id = %s
            """,
            (
                asset["id"],
                canonical_url,
                artwork_id,
                asset["id"],
            ),
        )
        cursor.execute(
            f"""
            UPDATE {INGESTION_TABLE}
            SET
                "sourceSha256" = %s,
                status = %s,
                "leaseOwner" = NULL,
                "leaseExpiresAt" = NULL,
                "lastError" = NULL,
                "completedAt" = CASE
                    WHEN %s = 'complete' THEN CURRENT_TIMESTAMP
                    ELSE NULL
                END,
                "updatedAt" = CURRENT_TIMESTAMP
            WHERE "artworkId" = %s
            """,
            (
                processed.source_sha256,
                ingestion_status,
                ingestion_status,
                artwork_id,
            ),
        )
        record_attempt(
            cursor,
            artwork_id=artwork_id,
            dry_run=False,
            outcome="new_asset",
            duration_ms=duration_ms,
            source_sha256=processed.source_sha256,
            normalized_pixel_sha256=processed.normalized_pixel_sha256,
            perceptual_hash=processed.perceptual_hash,
            difference_hash=processed.difference_hash,
            matched_image_asset_id=asset["id"],
            candidates=candidates,
            download_attempt_count=processed.download_attempt_count,
            source_byte_size=processed.source_byte_size,
            full_byte_size=len(processed.full_webp),
            thumbnail_byte_size=len(processed.thumbnail_webp),
            estimated_cost_micro_usd=estimated_cost_micro_usd,
        )
    connection.commit()


def record_dry_run(
    connection,
    *,
    artwork_id,
    processed,
    exact_asset,
    candidates,
    started_at,
):
    duration_ms = round((time.monotonic() - started_at) * 1000)
    outcome = "exact_duplicate" if exact_asset else "new_asset"
    estimated_cost = (
        0
        if exact_asset
        else estimate_new_asset_cost_micro_usd(
            len(processed.full_webp) + len(processed.thumbnail_webp)
        )
    )
    with connection.cursor() as cursor:
        record_attempt(
            cursor,
            artwork_id=artwork_id,
            dry_run=True,
            outcome=outcome,
            duration_ms=duration_ms,
            source_sha256=processed.source_sha256,
            normalized_pixel_sha256=processed.normalized_pixel_sha256,
            perceptual_hash=processed.perceptual_hash,
            difference_hash=processed.difference_hash,
            matched_image_asset_id=(
                exact_asset["id"] if exact_asset else None
            ),
            candidates=candidates,
            download_attempt_count=processed.download_attempt_count,
            source_byte_size=processed.source_byte_size,
            full_byte_size=len(processed.full_webp),
            thumbnail_byte_size=len(processed.thumbnail_webp),
            estimated_cost_micro_usd=estimated_cost,
        )
    connection.commit()
    return WorkResult(
        artwork_id=artwork_id,
        outcome=outcome,
        dry_run=True,
        image_asset_id=exact_asset["id"] if exact_asset else None,
        review_candidate_count=len(candidates),
        source_bytes=processed.source_byte_size,
        written_bytes=(
            0
            if exact_asset
            else len(processed.full_webp) + len(processed.thumbnail_webp)
        ),
        download_attempts=processed.download_attempt_count,
        estimated_cost_micro_usd=estimated_cost,
    )


def record_already_linked(connection, artwork, dry_run, started_at):
    duration_ms = round((time.monotonic() - started_at) * 1000)
    with connection.cursor() as cursor:
        if not dry_run:
            status = ingestion_status_for_asset(artwork["asset_status"])
            cursor.execute(
                f"""
                UPDATE {ARTWORK_TABLE} AS artwork
                SET "imgVec" = asset."imageEmbedding"
                FROM {ASSET_TABLE} AS asset
                WHERE artwork.id = %s
                  AND asset.id = artwork."imageAssetId"
                  AND artwork."imgVec"
                      IS DISTINCT FROM asset."imageEmbedding"
                """,
                (artwork["id"],),
            )
            cursor.execute(
                f"""
                UPDATE {INGESTION_TABLE}
                SET
                    status = %s,
                    "leaseOwner" = NULL,
                    "leaseExpiresAt" = NULL,
                    "lastError" = NULL,
                    "completedAt" = CASE
                        WHEN %s = 'complete' THEN CURRENT_TIMESTAMP
                        ELSE NULL
                    END,
                    "updatedAt" = CURRENT_TIMESTAMP
                WHERE "artworkId" = %s
                """,
                (status, status, artwork["id"]),
            )
        record_attempt(
            cursor,
            artwork_id=artwork["id"],
            dry_run=dry_run,
            outcome="already_linked",
            duration_ms=duration_ms,
            matched_image_asset_id=artwork["image_asset_id"],
        )
    connection.commit()
    return WorkResult(
        artwork_id=artwork["id"],
        outcome="already_linked",
        dry_run=dry_run,
        image_asset_id=artwork["image_asset_id"],
    )


def handle_failure(
    connection,
    *,
    artwork_id,
    dry_run,
    started_at,
    failure,
    ingestion_attempt,
    source_sha256=None,
    normalized_pixel_sha256=None,
    perceptual_hash=None,
    difference_hash=None,
    matched_image_asset_id=None,
    full_byte_size=None,
    thumbnail_byte_size=None,
):
    terminal = not failure.retryable or (
        not dry_run
        and ingestion_attempt is not None
        and ingestion_attempt >= SETTINGS.max_attempts
    )
    outcome = "terminal_failure" if terminal else "retryable_failure"
    retry_after = (
        None
        if terminal or dry_run
        else retry_delay_seconds(ingestion_attempt or 1)
    )
    duration_ms = round((time.monotonic() - started_at) * 1000)

    with connection.cursor() as cursor:
        if not dry_run:
            cursor.execute(
                f"""
                UPDATE {INGESTION_TABLE}
                SET
                    status = %s,
                    "nextAttemptAt" = CASE
                        WHEN %s IS NULL THEN NULL
                        ELSE CURRENT_TIMESTAMP
                            + (%s * INTERVAL '1 second')
                    END,
                    "leaseOwner" = NULL,
                    "leaseExpiresAt" = NULL,
                    "lastError" = %s,
                    "updatedAt" = CURRENT_TIMESTAMP
                WHERE "artworkId" = %s
                """,
                (
                    outcome,
                    retry_after,
                    retry_after,
                    f"{failure.stage}: {failure}",
                    artwork_id,
                ),
            )
        record_attempt(
            cursor,
            artwork_id=artwork_id,
            dry_run=dry_run,
            outcome=outcome,
            duration_ms=duration_ms,
            source_sha256=source_sha256,
            normalized_pixel_sha256=normalized_pixel_sha256,
            perceptual_hash=perceptual_hash,
            difference_hash=difference_hash,
            matched_image_asset_id=matched_image_asset_id,
            download_attempt_count=failure.download_attempt_count,
            source_byte_size=failure.source_byte_size or None,
            full_byte_size=full_byte_size,
            thumbnail_byte_size=thumbnail_byte_size,
            error_stage=failure.stage,
            error=str(failure),
        )
    connection.commit()
    return WorkResult(
        artwork_id=artwork_id,
        outcome=outcome,
        dry_run=dry_run,
        source_bytes=failure.source_byte_size,
        download_attempts=failure.download_attempt_count,
        error_stage=failure.stage,
        error=str(failure),
        retry_after_seconds=retry_after,
    )


def process_artwork(
    artwork_id,
    *,
    dry_run=False,
    owner=None,
    source_s3_key=None,
):
    started_at = time.monotonic()
    owner = owner or worker_id()
    connection = connect(SETTINGS)
    ingestion_attempt = None
    downloaded = None
    processed = None
    asset = None

    try:
        artwork = get_artwork(connection, artwork_id)
        if not artwork:
            raise PipelineFailure(
                "claim",
                f"artwork {artwork_id} does not exist",
                retryable=False,
            )
        source_reference = (
            f"s3://{SETTINGS.s3_bucket_name}/{source_s3_key}"
            if source_s3_key
            else (
                artwork["source_url"]
                or artwork["fallback_source_url"]
            )
        )
        if not source_reference:
            raise PipelineFailure(
                "claim",
                "artwork has no primary image URL",
                retryable=False,
            )
        if artwork["image_asset_id"] is not None:
            return record_already_linked(
                connection,
                artwork,
                dry_run,
                started_at,
            )
        if (
            not dry_run
            and artwork["ingestion_status"] == "terminal_failure"
        ):
            return WorkResult(
                artwork_id=artwork_id,
                outcome="terminal_failure",
                dry_run=False,
                error_stage="claim",
                error=artwork["ingestion_last_error"],
            )

        if not dry_run:
            ensure_ingestion(
                connection,
                {
                    "id": artwork["id"],
                    "source_url": source_reference,
                },
            )
            ingestion_attempt = claim_ingestion(
                connection,
                artwork_id,
                owner,
                SETTINGS,
            )
            if ingestion_attempt is None:
                return WorkResult(
                    artwork_id=artwork_id,
                    outcome="retryable_failure",
                    dry_run=False,
                    error_stage="claim",
                    error="ingestion is leased or not due",
                    retry_after_seconds=30,
                )

        downloaded_source_url = source_reference
        if source_s3_key:
            downloaded = fetch_s3_source(source_s3_key, SETTINGS)
        else:
            try:
                downloaded, downloaded_source_url = (
                    fetch_source_candidates(
                        (
                            artwork["source_url"],
                            artwork["fallback_source_url"],
                        ),
                        SETTINGS,
                    )
                )
            except PipelineFailure as initial_error:
                try:
                    refreshed_primary_url, refreshed_fallback_url = (
                        refresh_met_image_urls(
                            artwork["object_id"],
                            SETTINGS,
                        )
                    )
                except PipelineFailure as refresh_error:
                    raise PipelineFailure(
                        "download",
                        (
                            f"configured URLs failed ({initial_error}); "
                            f"refresh failed ({refresh_error})"
                        ),
                        retryable=refresh_error.retryable,
                        download_attempt_count=(
                            initial_error.download_attempt_count
                            + refresh_error.download_attempt_count
                        ),
                        source_byte_size=initial_error.source_byte_size,
                    ) from refresh_error

                refreshed_urls = (
                    refreshed_primary_url,
                    refreshed_fallback_url,
                )
                configured_urls = (
                    artwork["source_url"],
                    artwork["fallback_source_url"],
                )
                if refreshed_urls == configured_urls:
                    raise PipelineFailure(
                        "download",
                        (
                            f"configured URLs failed ({initial_error}); "
                            "the Met object API returned the same URLs"
                        ),
                        retryable=initial_error.retryable,
                        download_attempt_count=(
                            initial_error.download_attempt_count + 1
                        ),
                        source_byte_size=initial_error.source_byte_size,
                    ) from initial_error
                try:
                    downloaded, downloaded_source_url = (
                        fetch_source_candidates(
                            refreshed_urls,
                            SETTINGS,
                        )
                    )
                except PipelineFailure as refreshed_error:
                    raise PipelineFailure(
                        "download",
                        (
                            f"configured URLs failed ({initial_error}); "
                            f"refreshed URLs failed ({refreshed_error})"
                        ),
                        retryable=refreshed_error.retryable,
                        download_attempt_count=(
                            initial_error.download_attempt_count
                            + 1
                            + refreshed_error.download_attempt_count
                        ),
                        source_byte_size=(
                            initial_error.source_byte_size
                            + refreshed_error.source_byte_size
                        ),
                    ) from refreshed_error
                if not dry_run:
                    update_artwork_image_urls(
                        connection,
                        artwork_id,
                        refreshed_primary_url,
                        refreshed_fallback_url,
                    )
        if not dry_run:
            update_source_digest(
                connection,
                artwork_id,
                downloaded.sha256,
                source_url=downloaded_source_url,
            )

        exact_source = find_source_exact_asset(
            connection,
            downloaded.sha256,
            artwork_id,
        )
        if exact_source:
            if not dry_run and source_s3_key:
                exact_source = promote_existing_embedding(
                    connection,
                    exact_source,
                    artwork["image_embedding"],
                )
            return link_existing_asset(
                connection,
                SETTINGS,
                artwork_id=artwork_id,
                source_sha256=downloaded.sha256,
                asset=exact_source,
                dry_run=dry_run,
                started_at=started_at,
                downloaded=downloaded,
            )

        processed = process_source(downloaded)
        exact_normalized = find_normalized_exact_asset(
            connection,
            processed.normalized_pixel_sha256,
        )
        if (
            exact_normalized
            and exact_normalized["processing_status"]
            in ("pending_embedding", "ready")
        ):
            if not dry_run and source_s3_key:
                exact_normalized = promote_existing_embedding(
                    connection,
                    exact_normalized,
                    artwork["image_embedding"],
                )
            return link_existing_asset(
                connection,
                SETTINGS,
                artwork_id=artwork_id,
                source_sha256=processed.source_sha256,
                asset=exact_normalized,
                dry_run=dry_run,
                started_at=started_at,
                downloaded=downloaded,
            )

        if dry_run:
            candidates = candidate_asset_ids(
                connection,
                processed.perceptual_hash,
                processed.difference_hash,
                None,
            )
            return record_dry_run(
                connection,
                artwork_id=artwork_id,
                processed=processed,
                exact_asset=exact_normalized,
                candidates=candidates,
                started_at=started_at,
            )

        asset = reserve_asset(
            connection,
            processed,
            owner,
            SETTINGS,
        )
        if not asset["created"] and asset.get("processing_status") in (
            "pending_embedding",
            "ready",
        ):
            if source_s3_key:
                asset = promote_existing_embedding(
                    connection,
                    asset,
                    artwork["image_embedding"],
                )
            return link_existing_asset(
                connection,
                SETTINGS,
                artwork_id=artwork_id,
                source_sha256=processed.source_sha256,
                asset=asset,
                dry_run=False,
                started_at=started_at,
                downloaded=downloaded,
            )

        persist_hash_bands(
            connection,
            asset["id"],
            processed.perceptual_hash,
            processed.difference_hash,
        )
        s3_client = service_client("s3", SETTINGS)
        uploaded_full = upload_derivative(
            s3_client,
            SETTINGS,
            key=asset["full_s3_key"],
            content=processed.full_webp,
            encoded_sha256=processed.full_sha256,
            normalized_sha256=processed.normalized_pixel_sha256,
            derivative="full",
        )
        uploaded_thumbnail = upload_derivative(
            s3_client,
            SETTINGS,
            key=asset["thumbnail_s3_key"],
            content=processed.thumbnail_webp,
            encoded_sha256=processed.thumbnail_sha256,
            normalized_sha256=processed.normalized_pixel_sha256,
            derivative="graph",
        )
        candidates = candidate_asset_ids(
            connection,
            processed.perceptual_hash,
            processed.difference_hash,
            asset["id"],
        )
        written_bytes = len(processed.full_webp) + len(
            processed.thumbnail_webp
        )
        estimated_cost = (
            estimate_new_asset_cost_micro_usd(written_bytes)
            if uploaded_full or uploaded_thumbnail
            else 0
        )
        finalize_new_asset(
            connection,
            SETTINGS,
            artwork_id=artwork_id,
            processed=processed,
            asset=asset,
            candidates=candidates,
            started_at=started_at,
            estimated_cost_micro_usd=estimated_cost,
            image_embedding=(
                artwork["image_embedding"] if source_s3_key else None
            ),
        )
        return WorkResult(
            artwork_id=artwork_id,
            outcome="new_asset",
            dry_run=False,
            image_asset_id=asset["id"],
            review_candidate_count=len(candidates),
            source_bytes=processed.source_byte_size,
            written_bytes=written_bytes,
            download_attempts=processed.download_attempt_count,
            estimated_cost_micro_usd=estimated_cost,
        )
    except PipelineFailure as failure:
        connection.rollback()
        if downloaded and failure.download_attempt_count == 0:
            failure.download_attempt_count = downloaded.attempt_count
        if downloaded and failure.source_byte_size == 0:
            failure.source_byte_size = downloaded.byte_size
        release_failed_asset(
            connection,
            asset["id"] if asset else None,
            owner,
            failure,
        )
        return handle_failure(
            connection,
            artwork_id=artwork_id,
            dry_run=dry_run,
            started_at=started_at,
            failure=failure,
            ingestion_attempt=ingestion_attempt,
            source_sha256=(
                processed.source_sha256
                if processed
                else downloaded.sha256 if downloaded else None
            ),
            normalized_pixel_sha256=(
                processed.normalized_pixel_sha256 if processed else None
            ),
            perceptual_hash=(
                processed.perceptual_hash if processed else None
            ),
            difference_hash=(
                processed.difference_hash if processed else None
            ),
            matched_image_asset_id=asset["id"] if asset else None,
            full_byte_size=(
                len(processed.full_webp) if processed else None
            ),
            thumbnail_byte_size=(
                len(processed.thumbnail_webp) if processed else None
            ),
        )
    except Exception as error:
        connection.rollback()
        failure = PipelineFailure(
            "internal",
            f"{type(error).__name__}: {error}",
            download_attempt_count=(
                downloaded.attempt_count if downloaded else 0
            ),
            source_byte_size=downloaded.byte_size if downloaded else 0,
        )
        LOGGER.exception("unhandled ingestion error for artwork %s", artwork_id)
        release_failed_asset(
            connection,
            asset["id"] if asset else None,
            owner,
            failure,
        )
        return handle_failure(
            connection,
            artwork_id=artwork_id,
            dry_run=dry_run,
            started_at=started_at,
            failure=failure,
            ingestion_attempt=ingestion_attempt,
            source_sha256=downloaded.sha256 if downloaded else None,
            normalized_pixel_sha256=(
                processed.normalized_pixel_sha256 if processed else None
            ),
            perceptual_hash=(
                processed.perceptual_hash if processed else None
            ),
            difference_hash=(
                processed.difference_hash if processed else None
            ),
            matched_image_asset_id=asset["id"] if asset else None,
            full_byte_size=(
                len(processed.full_webp) if processed else None
            ),
            thumbnail_byte_size=(
                len(processed.thumbnail_webp) if processed else None
            ),
        )
    finally:
        close_connection(connection)


def select_balanced_artworks(
    connection,
    limit,
    include_hosted=False,
    retry_terminal=False,
):
    hosted_filter = (
        ""
        if include_hosted
        else """
          AND (
            artwork."localImageUrl" IS NULL
            OR artwork."localImageUrl" = ''
          )
        """
    )
    with connection.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute(
            f"""
            WITH eligible AS (
                SELECT
                    artwork.id,
                    artwork."primaryImage" AS source_url,
                    COALESCE(artwork.department, 'Unknown') AS department,
                    ROW_NUMBER() OVER (
                        PARTITION BY COALESCE(
                            artwork.department,
                            'Unknown'
                        )
                        ORDER BY artwork.id
                    ) AS department_position
                FROM {ARTWORK_TABLE} artwork
                LEFT JOIN {INGESTION_TABLE} ingestion
                  ON ingestion."artworkId" = artwork.id
                WHERE artwork."primaryImage" IS NOT NULL
                  AND artwork."primaryImage" <> ''
                  AND artwork."imageAssetId" IS NULL
                  {hosted_filter}
                  AND (
                    ingestion."artworkId" IS NULL
                    OR ingestion.status IN (
                        'pending',
                        'retryable_failure'
                    )
                    OR (%s AND ingestion.status = 'terminal_failure')
                  )
            )
            SELECT id, source_url, department
            FROM eligible
            ORDER BY department_position, department, id
            LIMIT %s
            """,
            (retry_terminal, limit),
        )
        return cursor.fetchall()


def select_legacy_artworks(connection, limit, retry_terminal=False):
    legacy_prefix = f"{SETTINGS.image_cdn_base_url}/artworks/"
    with connection.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute(
            f"""
            WITH eligible AS (
                SELECT
                    artwork.id,
                    artwork."localImageUrl" AS local_image_url,
                    COALESCE(artwork.department, 'Unknown') AS department,
                    ROW_NUMBER() OVER (
                        PARTITION BY COALESCE(
                            artwork.department,
                            'Unknown'
                        )
                        ORDER BY artwork.id
                    ) AS department_position
                FROM {ARTWORK_TABLE} artwork
                LEFT JOIN {INGESTION_TABLE} ingestion
                  ON ingestion."artworkId" = artwork.id
                WHERE artwork."localImageUrl" LIKE %s
                  AND artwork."imageAssetId" IS NULL
                  AND (
                    ingestion."artworkId" IS NULL
                    OR ingestion.status IN (
                        'pending',
                        'retryable_failure'
                    )
                    OR (%s AND ingestion.status = 'terminal_failure')
                  )
            )
            SELECT id, local_image_url, department
            FROM eligible
            ORDER BY department_position, department, id
            LIMIT %s
            """,
            (f"{legacy_prefix}%", retry_terminal, limit),
        )
        rows = cursor.fetchall()

    artworks = []
    cdn_prefix = f"{SETTINGS.image_cdn_base_url}/"
    for row in rows:
        source_s3_key = row["local_image_url"][len(cdn_prefix) :]
        if (
            not source_s3_key.startswith("artworks/")
            or ".." in source_s3_key
        ):
            raise RuntimeError(
                f"invalid legacy object key for artwork {row['id']}"
            )
        artworks.append(
            {
                "id": row["id"],
                "source_url": (
                    f"s3://{SETTINGS.s3_bucket_name}/{source_s3_key}"
                ),
                "source_s3_key": source_s3_key,
                "department": row["department"],
            }
        )
    return artworks


def seed_ingestions(connection, artworks):
    rows = [
        (artwork["id"], artwork["source_url"])
        for artwork in artworks
    ]
    if not rows:
        return
    with connection.cursor() as cursor:
        execute_values(
            cursor,
            f"""
            INSERT INTO {INGESTION_TABLE} (
                "artworkId",
                "sourceUrl",
                status
            )
            VALUES %s
            ON CONFLICT ("artworkId") DO UPDATE
            SET
                "sourceUrl" = EXCLUDED."sourceUrl",
                status = CASE
                    WHEN {INGESTION_TABLE}.status = 'terminal_failure'
                        THEN 'pending'::image_ingestion_status
                    ELSE {INGESTION_TABLE}.status
                END,
                "attemptCount" = CASE
                    WHEN {INGESTION_TABLE}.status = 'terminal_failure'
                        THEN 0
                    ELSE {INGESTION_TABLE}."attemptCount"
                END,
                "nextAttemptAt" = CASE
                    WHEN {INGESTION_TABLE}.status = 'terminal_failure'
                        THEN NULL
                    ELSE {INGESTION_TABLE}."nextAttemptAt"
                END,
                "lastError" = CASE
                    WHEN {INGESTION_TABLE}.status = 'terminal_failure'
                        THEN NULL
                    ELSE {INGESTION_TABLE}."lastError"
                END,
                "completedAt" = CASE
                    WHEN {INGESTION_TABLE}.status = 'terminal_failure'
                        THEN NULL
                    ELSE {INGESTION_TABLE}."completedAt"
                END,
                "updatedAt" = CURRENT_TIMESTAMP
            """,
            rows,
            template="(%s, %s, 'pending')",
        )
    connection.commit()


def enqueue_artworks(sqs_client, queue_url, artworks):
    batches = [
        artworks[index : index + 10]
        for index in range(0, len(artworks), 10)
    ]

    def send_batch(batch):
        response = sqs_client.send_message_batch(
            QueueUrl=queue_url,
            Entries=[
                {
                    "Id": str(artwork["id"]),
                    "MessageBody": json.dumps(
                        {
                            "artworkId": artwork["id"],
                            "sourceUrl": artwork["source_url"],
                            "department": artwork["department"],
                            **(
                                {
                                    "sourceType": "legacy_s3",
                                    "sourceS3Key": artwork["source_s3_key"],
                                }
                                if artwork.get("source_s3_key")
                                else {}
                            ),
                        },
                        separators=(",", ":"),
                    ),
                }
                for artwork in batch
            ],
        )
        failures = response.get("Failed", [])
        if failures:
            raise RuntimeError(f"SQS batch failures: {failures}")
        return len(response.get("Successful", []))

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        return sum(executor.map(send_batch, batches))


def due_ingestion_ids(connection, limit):
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            WITH due AS (
                SELECT
                    ingestion."artworkId",
                    COALESCE(artwork.department, 'Unknown') AS department,
                    ROW_NUMBER() OVER (
                        PARTITION BY COALESCE(
                            artwork.department,
                            'Unknown'
                        )
                        ORDER BY ingestion."createdAt"
                    ) AS department_position
                FROM {INGESTION_TABLE} ingestion
                JOIN {ARTWORK_TABLE} artwork
                  ON artwork.id = ingestion."artworkId"
                WHERE (
                    ingestion.status IN ('pending', 'retryable_failure')
                    OR (
                        ingestion.status = 'processing'
                        AND ingestion."leaseExpiresAt" < CURRENT_TIMESTAMP
                    )
                )
                  AND (
                    ingestion."nextAttemptAt" IS NULL
                    OR ingestion."nextAttemptAt" <= CURRENT_TIMESTAMP
                  )
            )
            SELECT "artworkId"
            FROM due
            ORDER BY department_position, department, "artworkId"
            LIMIT %s
            """,
            (limit,),
        )
        return [row[0] for row in cursor.fetchall()]


def run_concurrent(
    artwork_ids,
    *,
    dry_run,
    concurrency,
    source_s3_keys=None,
):
    owner = worker_id()
    source_s3_keys = source_s3_keys or {}
    results = []
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=concurrency
    ) as executor:
        futures = {
            executor.submit(
                process_artwork,
                artwork_id,
                dry_run=dry_run,
                owner=owner,
                source_s3_key=source_s3_keys.get(artwork_id),
            ): artwork_id
            for artwork_id in artwork_ids
        }
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            results.append(result)
            LOGGER.info(json.dumps(asdict(result), separators=(",", ":")))
    return sorted(results, key=lambda result: result.artwork_id)


def summarize_results(results):
    summary = {
        "processed": len(results),
        "outcomes": {},
        "reviewCandidates": 0,
        "sourceBytes": 0,
        "writtenBytes": 0,
        "downloadAttempts": 0,
        "estimatedCostMicroUsd": 0,
    }
    for result in results:
        summary["outcomes"][result.outcome] = (
            summary["outcomes"].get(result.outcome, 0) + 1
        )
        summary["reviewCandidates"] += result.review_candidate_count
        summary["sourceBytes"] += result.source_bytes
        summary["writtenBytes"] += result.written_bytes
        summary["downloadAttempts"] += result.download_attempts
        summary[
            "estimatedCostMicroUsd"
        ] += result.estimated_cost_micro_usd
    summary["estimatedCostUsd"] = (
        summary["estimatedCostMicroUsd"] / 1_000_000
    )
    return summary


def receive_sqs(settings, *, limit, concurrency):
    if not settings.ingestion_queue_url:
        raise RuntimeError("IMAGE_INGESTION_QUEUE_URL is required")
    sqs_client = service_client("sqs", settings)
    results = []
    remaining = limit
    owner = worker_id()

    while remaining > 0:
        response = sqs_client.receive_message(
            QueueUrl=settings.ingestion_queue_url,
            MaxNumberOfMessages=min(10, remaining),
            WaitTimeSeconds=1,
            VisibilityTimeout=settings.lease_seconds,
            AttributeNames=["ApproximateReceiveCount"],
        )
        messages = response.get("Messages", [])
        if not messages:
            break

        def handle(message):
            payload = json.loads(message["Body"])
            result = process_artwork(
                int(payload["artworkId"]),
                owner=owner,
                source_s3_key=payload.get("sourceS3Key"),
            )
            if result.outcome == "retryable_failure":
                sqs_client.change_message_visibility(
                    QueueUrl=settings.ingestion_queue_url,
                    ReceiptHandle=message["ReceiptHandle"],
                    VisibilityTimeout=result.retry_after_seconds or 30,
                )
            else:
                sqs_client.delete_message(
                    QueueUrl=settings.ingestion_queue_url,
                    ReceiptHandle=message["ReceiptHandle"],
                )
            return result

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=concurrency
        ) as executor:
            batch_results = list(executor.map(handle, messages))
        for result in batch_results:
            LOGGER.info(json.dumps(asdict(result), separators=(",", ":")))
        results.extend(batch_results)
        remaining -= len(messages)

    return results


def dispatch_embedding_outbox(settings, limit):
    if not settings.embedding_queue_url:
        raise RuntimeError("IMAGE_EMBEDDING_QUEUE_URL is required")
    connection = connect(settings)
    sqs_client = service_client("sqs", settings)
    dispatched = 0
    failed = 0

    try:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                f"""
                SELECT
                    outbox."imageAssetId" AS image_asset_id,
                    asset."fullS3Key" AS full_s3_key,
                    asset."thumbnailS3Key" AS thumbnail_s3_key
                FROM {OUTBOX_TABLE} outbox
                JOIN {ASSET_TABLE} asset
                  ON asset.id = outbox."imageAssetId"
                WHERE outbox.status = 'pending'
                  AND (
                    outbox."nextAttemptAt" IS NULL
                    OR outbox."nextAttemptAt" <= CURRENT_TIMESTAMP
                  )
                ORDER BY outbox."createdAt"
                LIMIT %s
                """,
                (limit,),
            )
            pending = cursor.fetchall()

        for item in pending:
            try:
                arguments = {
                    "QueueUrl": settings.embedding_queue_url,
                    "MessageBody": json.dumps(
                        {
                            "imageAssetId": item["image_asset_id"],
                            "fullS3Key": item["full_s3_key"],
                            "thumbnailS3Key": item["thumbnail_s3_key"],
                        },
                        separators=(",", ":"),
                    ),
                }
                if settings.embedding_queue_url.endswith(".fifo"):
                    arguments["MessageGroupId"] = (
                        f"openclip-{item['image_asset_id'] % 64:02d}"
                    )
                    arguments["MessageDeduplicationId"] = str(
                        item["image_asset_id"]
                    )
                response = sqs_client.send_message(**arguments)
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"""
                        UPDATE {OUTBOX_TABLE}
                        SET
                            status = 'dispatched',
                            "attemptCount" = "attemptCount" + 1,
                            "messageId" = %s,
                            "lastError" = NULL,
                            "dispatchedAt" = CURRENT_TIMESTAMP
                        WHERE "imageAssetId" = %s
                          AND status = 'pending'
                        """,
                        (
                            response["MessageId"],
                            item["image_asset_id"],
                        ),
                    )
                connection.commit()
                dispatched += 1
            except Exception as error:
                connection.rollback()
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"""
                        UPDATE {OUTBOX_TABLE}
                        SET
                            "attemptCount" = "attemptCount" + 1,
                            "nextAttemptAt" = CURRENT_TIMESTAMP
                                + INTERVAL '5 minutes',
                            "lastError" = %s
                        WHERE "imageAssetId" = %s
                        """,
                        (str(error), item["image_asset_id"]),
                    )
                connection.commit()
                failed += 1
        return {"selected": len(pending), "dispatched": dispatched, "failed": failed}
    finally:
        close_connection(connection)


def database_stats(settings):
    connection = connect(settings)
    try:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                f"""
                SELECT
                    COUNT(*)::int AS attempts,
                    COUNT(*) FILTER (
                        WHERE "dryRun"
                    )::int AS dry_run_attempts,
                    COUNT(*) FILTER (
                        WHERE NOT "dryRun"
                    )::int AS live_attempts,
                    COUNT(*) FILTER (
                        WHERE outcome = 'new_asset'
                    )::int AS new_assets,
                    COUNT(*) FILTER (
                        WHERE outcome = 'exact_duplicate'
                    )::int AS exact_duplicates,
                    COUNT(*) FILTER (
                        WHERE outcome = 'already_linked'
                    )::int AS already_linked,
                    COUNT(*) FILTER (
                        WHERE outcome = 'retryable_failure'
                    )::int AS retryable_failures,
                    COUNT(*) FILTER (
                        WHERE outcome = 'terminal_failure'
                    )::int AS terminal_failures,
                    COALESCE(SUM("reviewCandidateCount"), 0)::bigint
                        AS review_candidates,
                    COALESCE(SUM("sourceByteSize"), 0)::bigint
                        AS source_bytes,
                    COALESCE(
                        SUM("fullByteSize" + "thumbnailByteSize"),
                        0
                    )::bigint AS encoded_bytes,
                    COALESCE(SUM("estimatedCostMicroUsd"), 0)::bigint
                        AS estimated_cost_micro_usd
                FROM {ATTEMPT_TABLE}
                """
            )
            attempts = cursor.fetchone()
            cursor.execute(
                f"""
                SELECT status, COUNT(*)::int AS count
                FROM {INGESTION_TABLE}
                GROUP BY status
                ORDER BY status
                """
            )
            ingestions = {
                row["status"]: row["count"] for row in cursor.fetchall()
            }
            cursor.execute(
                f"""
                SELECT status, COUNT(*)::int AS count
                FROM {OUTBOX_TABLE}
                GROUP BY status
                ORDER BY status
                """
            )
            outbox = {
                row["status"]: row["count"] for row in cursor.fetchall()
            }
        attempts["estimated_cost_usd"] = (
            attempts["estimated_cost_micro_usd"] / 1_000_000
        )
        return {
            "attempts": attempts,
            "ingestions": ingestions,
            "embeddingOutbox": outbox,
        }
    finally:
        close_connection(connection)


def lambda_handler(event, _context):
    failures = []
    for record in event.get("Records", []):
        try:
            payload = json.loads(record["body"])
            result = process_artwork(
                int(payload["artworkId"]),
                source_s3_key=payload.get("sourceS3Key"),
            )
            if result.outcome == "retryable_failure":
                failures.append({"itemIdentifier": record["messageId"]})
        except Exception:
            LOGGER.exception("failed to process SQS record")
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}


def parse_args():
    parser = argparse.ArgumentParser(
        description="Idempotent one-pass image ingestion worker"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    seed_parser = subparsers.add_parser(
        "seed",
        help="select department-balanced artworks and optionally enqueue them",
    )
    seed_parser.add_argument("--limit", type=int, required=True)
    seed_parser.add_argument("--enqueue", action="store_true")
    seed_parser.add_argument("--include-hosted", action="store_true")
    seed_parser.add_argument("--retry-terminal", action="store_true")

    legacy_seed_parser = subparsers.add_parser(
        "seed-legacy",
        help="select department-balanced legacy S3 objects and enqueue them",
    )
    legacy_seed_parser.add_argument("--limit", type=int, required=True)
    legacy_seed_parser.add_argument("--enqueue", action="store_true")
    legacy_seed_parser.add_argument("--retry-terminal", action="store_true")

    work_parser = subparsers.add_parser(
        "work",
        help="process due database or SQS jobs",
    )
    work_parser.add_argument("--limit", type=int, default=1)
    work_parser.add_argument(
        "--concurrency",
        type=int,
        default=int(os.getenv("IMAGE_INGESTION_CONCURRENCY", "4")),
    )
    work_parser.add_argument("--dry-run", action="store_true")
    work_parser.add_argument("--from-sqs", action="store_true")
    work_parser.add_argument("--legacy", action="store_true")
    work_parser.add_argument(
        "--artwork-id",
        action="append",
        type=int,
        default=[],
    )

    dispatch_parser = subparsers.add_parser(
        "dispatch-embeddings",
        help="send pending canonical assets to the OpenCLIP queue",
    )
    dispatch_parser.add_argument("--limit", type=int, default=100)

    subparsers.add_parser("stats", help="print durable ingestion metrics")
    return parser.parse_args()


def main():
    args = parse_args()

    if args.command in ("seed", "seed-legacy"):
        connection = connect(SETTINGS)
        try:
            artworks = (
                select_legacy_artworks(
                    connection,
                    args.limit,
                    retry_terminal=args.retry_terminal,
                )
                if args.command == "seed-legacy"
                else select_balanced_artworks(
                    connection,
                    args.limit,
                    include_hosted=args.include_hosted,
                    retry_terminal=args.retry_terminal,
                )
            )
            seed_ingestions(connection, artworks)
        finally:
            close_connection(connection)
        sent = 0
        if args.enqueue:
            if not SETTINGS.ingestion_queue_url:
                raise RuntimeError("IMAGE_INGESTION_QUEUE_URL is required")
            sent = enqueue_artworks(
                service_client("sqs", SETTINGS),
                SETTINGS.ingestion_queue_url,
                artworks,
            )
        print(
            json.dumps(
                {
                    "selected": len(artworks),
                    "seeded": len(artworks),
                    "enqueued": sent,
                    "sourceType": (
                        "legacy_s3"
                        if args.command == "seed-legacy"
                        else "met"
                    ),
                    "artworkIds": [
                        artwork["id"] for artwork in artworks[:20]
                    ],
                    "departments": sorted(
                        {
                            artwork["department"]
                            for artwork in artworks
                        }
                    ),
                },
                indent=2,
            )
        )
        return

    if args.command == "work":
        if args.limit < 1 or args.concurrency < 1:
            raise RuntimeError("limit and concurrency must be positive")
        if args.from_sqs and args.dry_run:
            raise RuntimeError("--from-sqs cannot be combined with --dry-run")
        if args.from_sqs and args.legacy:
            raise RuntimeError("--from-sqs cannot be combined with --legacy")
        if args.from_sqs:
            results = receive_sqs(
                SETTINGS,
                limit=args.limit,
                concurrency=args.concurrency,
            )
        else:
            source_s3_keys = {}
            if args.legacy:
                connection = connect(SETTINGS)
                try:
                    legacy_artworks = select_legacy_artworks(
                        connection,
                        args.limit,
                    )
                finally:
                    close_connection(connection)
                artwork_ids = [
                    artwork["id"] for artwork in legacy_artworks
                ]
                source_s3_keys = {
                    artwork["id"]: artwork["source_s3_key"]
                    for artwork in legacy_artworks
                }
            elif args.artwork_id:
                artwork_ids = args.artwork_id[: args.limit]
            elif args.dry_run:
                connection = connect(SETTINGS)
                try:
                    artwork_ids = [
                        item["id"]
                        for item in select_balanced_artworks(
                            connection,
                            args.limit,
                        )
                    ]
                finally:
                    close_connection(connection)
            else:
                connection = connect(SETTINGS)
                try:
                    artwork_ids = due_ingestion_ids(
                        connection,
                        args.limit,
                    )
                finally:
                    close_connection(connection)
            results = run_concurrent(
                artwork_ids,
                dry_run=args.dry_run,
                concurrency=args.concurrency,
                source_s3_keys=source_s3_keys,
            )
        print(json.dumps(summarize_results(results), indent=2))
        return

    if args.command == "dispatch-embeddings":
        print(
            json.dumps(
                dispatch_embedding_outbox(SETTINGS, args.limit),
                indent=2,
            )
        )
        return

    if args.command == "stats":
        print(json.dumps(database_stats(SETTINGS), indent=2, default=str))


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
SETTINGS = Settings.from_env()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
