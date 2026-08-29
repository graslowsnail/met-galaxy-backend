#!/usr/bin/env python3

import argparse
import concurrent.futures
import io
import json
import math
import os
import tempfile
from pathlib import Path

import boto3
import numpy as np
import psycopg2
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageOps
from psycopg2.extras import RealDictCursor
from psycopg2.extras import execute_values
from skimage.metrics import structural_similarity

load_dotenv()

ARTWORK_TABLE = '"met-galaxy_artwork"'
ASSET_TABLE = '"met-galaxy_image_asset"'
INGESTION_TABLE = '"met-galaxy_image_ingestion"'
ATTEMPT_TABLE = '"met-galaxy_image_ingestion_attempt"'
CANDIDATE_TABLE = '"met-galaxy_image_duplicate_candidate"'
BAND_TABLE = '"met-galaxy_image_perceptual_hash_band"'
CANONICAL_TABLE = '"met-galaxy_image_asset_canonical"'


def settings():
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required")
    return {
        "database_url": database_url,
        "region": os.getenv("AWS_REGION", "us-east-1"),
        "bucket": os.getenv("S3_BUCKET_NAME", "met-artworks-images"),
        "cdn": os.getenv(
            "IMAGE_CDN_BASE_URL",
            "https://d2pvxr3eb77vb4.cloudfront.net",
        ).rstrip("/"),
    }


def aws_session(configuration):
    profile = os.getenv("AWS_PROFILE")
    return boto3.Session(
        profile_name=profile if profile else None,
        region_name=configuration["region"],
    )


def load_cohort_manifest(path):
    records = []
    artwork_ids = set()
    with path.open() as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            record = json.loads(line)
            artwork_id = record.get("artwork_id")
            legacy_s3_key = record.get("legacy_s3_key")
            legacy_url = record.get("legacy_url")
            if not isinstance(artwork_id, int):
                raise RuntimeError(
                    f"{path}:{line_number} has no integer artwork_id"
                )
            if (
                not isinstance(legacy_s3_key, str)
                or not legacy_s3_key.startswith("artworks/")
            ):
                raise RuntimeError(
                    f"{path}:{line_number} has no legacy artworks/ key"
                )
            if not isinstance(legacy_url, str) or not legacy_url:
                raise RuntimeError(
                    f"{path}:{line_number} has no legacy_url"
                )
            if artwork_id in artwork_ids:
                raise RuntimeError(
                    f"{path}:{line_number} repeats artwork {artwork_id}"
                )
            artwork_ids.add(artwork_id)
            records.append(record)
    if not records:
        raise RuntimeError(f"cohort manifest is empty: {path}")
    return records


def refresh_cohort_links(connection, records, path):
    artwork_ids = [record["artwork_id"] for record in records]
    with connection.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute(
            f"""
            SELECT
                artwork.id AS artwork_id,
                artwork."imageAssetId" AS image_asset_id,
                artwork."localImageUrl" AS canonical_url,
                ingestion.status AS ingestion_status
            FROM {ARTWORK_TABLE} artwork
            LEFT JOIN {INGESTION_TABLE} ingestion
              ON ingestion."artworkId" = artwork.id
            WHERE artwork.id = ANY(%s)
            ORDER BY artwork.id
            """,
            (artwork_ids,),
        )
        current_rows = {
            row["artwork_id"]: dict(row) for row in cursor.fetchall()
        }

    missing_artwork_ids = sorted(set(artwork_ids) - set(current_rows))
    if missing_artwork_ids:
        raise RuntimeError(
            "cohort artworks are missing from the database: "
            f"{missing_artwork_ids[:20]}"
        )
    if len(current_rows) != len(records):
        raise RuntimeError(
            "cohort refresh did not return exactly one row per artwork"
        )

    refreshed = []
    refreshed_links = 0
    linked_records = 0
    terminal_records = 0
    for record in records:
        current = current_rows[record["artwork_id"]]
        updated = dict(record)
        if current["ingestion_status"] == "terminal_failure":
            terminal_records += 1
        elif current["image_asset_id"] is not None:
            if not current["canonical_url"]:
                raise RuntimeError(
                    "linked cohort artwork has no canonical URL: "
                    f"{record['artwork_id']}"
                )
            linked_records += 1
            if (
                updated.get("image_asset_id")
                != current["image_asset_id"]
                or updated.get("canonical_url")
                != current["canonical_url"]
            ):
                refreshed_links += 1
            updated["image_asset_id"] = current["image_asset_id"]
            updated["canonical_url"] = current["canonical_url"]
        refreshed.append(updated)

    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as output:
            temporary_path = Path(output.name)
            for record in refreshed:
                output.write(json.dumps(record, separators=(",", ":")))
                output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary_path, path.stat().st_mode & 0o777)

        validated = load_cohort_manifest(temporary_path)
        if len(validated) != len(records):
            raise RuntimeError(
                "refreshed cohort size differs from frozen cohort size"
            )
        for before, after in zip(records, validated, strict=True):
            for field in (
                "artwork_id",
                "legacy_s3_key",
                "legacy_url",
            ):
                if before.get(field) != after.get(field):
                    raise RuntimeError(
                        f"cohort refresh changed frozen field {field}"
                    )
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

    return refreshed, {
        "records": len(refreshed),
        "linkedRecords": linked_records,
        "terminalRecords": terminal_records,
        "refreshedLinks": refreshed_links,
        "preservedUnlinkedRecords": (
            len(refreshed) - linked_records
        ),
    }


def database_summary(connection, artwork_ids=None):
    if artwork_ids is None:
        ingestion_filter = (
            '"sourceUrl" LIKE \'s3://%%/artworks/%%\'',
            (),
        )
        qualified_ingestion_filter = (
            'ingestion."sourceUrl" LIKE \'s3://%%/artworks/%%\'',
            (),
        )
    else:
        ingestion_filter = ('"artworkId" = ANY(%s)', (artwork_ids,))
        qualified_ingestion_filter = (
            'ingestion."artworkId" = ANY(%s)',
            (artwork_ids,),
        )

    with connection.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute(
            f"""
            SELECT status, COUNT(*)::int AS count
            FROM {INGESTION_TABLE}
            WHERE {ingestion_filter[0]}
            GROUP BY status
            ORDER BY status
            """,
            ingestion_filter[1],
        )
        statuses = {
            row["status"]: row["count"] for row in cursor.fetchall()
        }
        cursor.execute(
            f"""
            SELECT
                COUNT(*)::int AS total,
                COUNT(*) FILTER (
                    WHERE artwork."imageAssetId" IS NOT NULL
                )::int AS linked,
                COUNT(*) FILTER (
                    WHERE artwork."imageDuplicateState" = 'exact_duplicate'
                )::int AS exact_duplicates,
                COUNT(DISTINCT artwork."imageAssetId") FILTER (
                    WHERE artwork."imageAssetId" IS NOT NULL
                )::int AS canonical_assets,
                COUNT(*) FILTER (
                    WHERE asset."processingStatus" = 'ready'
                )::int AS ready_links,
                COUNT(*) FILTER (
                    WHERE artwork."imageAssetId" IS NOT NULL
                      AND asset."imageEmbedding" IS NULL
                )::int AS links_without_embedding,
                COUNT(*) FILTER (
                    WHERE artwork."localImageUrl" LIKE %s
                )::int AS legacy_urls_remaining
            FROM {INGESTION_TABLE} ingestion
            JOIN {ARTWORK_TABLE} artwork
              ON artwork.id = ingestion."artworkId"
            LEFT JOIN {ASSET_TABLE} asset
              ON asset.id = artwork."imageAssetId"
            WHERE {qualified_ingestion_filter[0]}
            """,
            (
                "%/artworks/%.jpg",
                *qualified_ingestion_filter[1],
            ),
        )
        links = cursor.fetchone()
        cursor.execute(
            f"""
            SELECT
                COUNT(*)::int AS attempts,
                COUNT(*) FILTER (
                    WHERE attempt.outcome = 'new_asset'
                )::int AS new_assets,
                COUNT(*) FILTER (
                    WHERE attempt.outcome = 'exact_duplicate'
                )::int AS exact_duplicates,
                COUNT(*) FILTER (
                    WHERE attempt.outcome = 'retryable_failure'
                )::int AS retryable_failures,
                COUNT(*) FILTER (
                    WHERE attempt.outcome = 'terminal_failure'
                )::int AS terminal_failures,
                COALESCE(SUM(attempt."sourceByteSize"), 0)::bigint
                    AS source_bytes,
                COALESCE(
                    SUM(
                        attempt."fullByteSize"
                        + attempt."thumbnailByteSize"
                    ) FILTER (
                        WHERE attempt.outcome = 'new_asset'
                    ),
                    0
                )::bigint AS canonical_bytes,
                COALESCE(
                    SUM(attempt."estimatedCostMicroUsd"),
                    0
                )::bigint AS estimated_cost_micro_usd,
                MIN(attempt."createdAt") AS started_at,
                MAX(attempt."createdAt") AS last_attempt_at
            FROM {ATTEMPT_TABLE} attempt
            JOIN {INGESTION_TABLE} ingestion
              ON ingestion."artworkId" = attempt."artworkId"
            WHERE {qualified_ingestion_filter[0]}
              AND NOT attempt."dryRun"
            """,
            qualified_ingestion_filter[1],
        )
        attempts = cursor.fetchone()
        if artwork_ids is None:
            cursor.execute(
                f"""
                SELECT
                    COUNT(*)::int AS review_candidates,
                    COUNT(*) FILTER (
                        WHERE status = 'review_candidate'
                    )::int AS unresolved_candidates
                FROM {CANDIDATE_TABLE}
                """
            )
        else:
            cursor.execute(
                f"""
                WITH cohort_assets AS (
                    SELECT DISTINCT
                        COALESCE(
                            mapping."canonicalAssetId",
                            artwork."imageAssetId"
                        ) AS image_asset_id
                    FROM {ARTWORK_TABLE} artwork
                    LEFT JOIN {CANONICAL_TABLE} mapping
                      ON mapping."assetId" = artwork."imageAssetId"
                    WHERE artwork.id = ANY(%s)
                      AND artwork."imageAssetId" IS NOT NULL
                )
                SELECT
                    COUNT(*)::int AS review_candidates,
                    COUNT(*) FILTER (
                        WHERE candidate.status = 'review_candidate'
                    )::int AS unresolved_candidates
                FROM {CANDIDATE_TABLE} candidate
                LEFT JOIN {CANONICAL_TABLE} map_a
                  ON map_a."assetId" = candidate."imageAssetAId"
                LEFT JOIN {CANONICAL_TABLE} map_b
                  ON map_b."assetId" = candidate."imageAssetBId"
                WHERE COALESCE(
                    map_a."canonicalAssetId",
                    candidate."imageAssetAId"
                ) IN (SELECT image_asset_id FROM cohort_assets)
                  AND COALESCE(
                    map_b."canonicalAssetId",
                    candidate."imageAssetBId"
                ) IN (SELECT image_asset_id FROM cohort_assets)
                """,
                (artwork_ids,),
            )
        candidates = cursor.fetchone()
    return {
        "statuses": statuses,
        "links": links,
        "attempts": attempts,
        "candidates": candidates,
    }


def s3_prefix_summary(s3_client, bucket, prefix):
    paginator = s3_client.get_paginator("list_objects_v2")
    object_count = 0
    byte_size = 0
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        objects = page.get("Contents", [])
        object_count += len(objects)
        byte_size += sum(item["Size"] for item in objects)
    return {"objects": object_count, "bytes": byte_size}


def write_rollback_manifest(connection, output_path, cdn_base_url):
    with connection.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute(
            f"""
            SELECT
                ingestion."artworkId" AS artwork_id,
                REPLACE(
                    ingestion."sourceUrl",
                    's3://met-artworks-images/',
                    ''
                ) AS legacy_s3_key,
                artwork."imageAssetId" AS image_asset_id,
                artwork."localImageUrl" AS canonical_url
            FROM {INGESTION_TABLE} ingestion
            JOIN {ARTWORK_TABLE} artwork
              ON artwork.id = ingestion."artworkId"
            WHERE ingestion."sourceUrl"
                LIKE 's3://met-artworks-images/artworks/%'
            ORDER BY ingestion."artworkId"
            """
        )
        with output_path.open("w") as output:
            for row in cursor:
                record = dict(row)
                record["legacy_url"] = (
                    f"{cdn_base_url}/{record['legacy_s3_key']}"
                )
                output.write(json.dumps(record, separators=(",", ":")))
                output.write("\n")


def hamming_distance(left, right):
    return (int(left, 16) ^ int(right, 16)).bit_count()


def rebuild_hash_candidates(connection):
    with connection.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute(
            f"""
            WITH phash_pairs AS (
                SELECT DISTINCT
                    left_band."imageAssetId" AS image_asset_a_id,
                    right_band."imageAssetId" AS image_asset_b_id
                FROM {BAND_TABLE} left_band
                JOIN {BAND_TABLE} right_band
                  ON right_band.algorithm = left_band.algorithm
                 AND right_band."bandIndex" = left_band."bandIndex"
                 AND right_band."bandValue" = left_band."bandValue"
                 AND right_band."imageAssetId"
                    > left_band."imageAssetId"
                WHERE left_band.algorithm = 'phash64'
            ),
            dhash_pairs AS (
                SELECT DISTINCT
                    left_band."imageAssetId" AS image_asset_a_id,
                    right_band."imageAssetId" AS image_asset_b_id
                FROM {BAND_TABLE} left_band
                JOIN {BAND_TABLE} right_band
                  ON right_band.algorithm = left_band.algorithm
                 AND right_band."bandIndex" = left_band."bandIndex"
                 AND right_band."bandValue" = left_band."bandValue"
                 AND right_band."imageAssetId"
                    > left_band."imageAssetId"
                WHERE left_band.algorithm = 'dhash64'
            )
            SELECT
                phash_pairs.image_asset_a_id,
                phash_pairs.image_asset_b_id,
                asset_a."perceptualHash" AS perceptual_hash_a,
                asset_b."perceptualHash" AS perceptual_hash_b,
                asset_a."differenceHash" AS difference_hash_a,
                asset_b."differenceHash" AS difference_hash_b
            FROM phash_pairs
            JOIN dhash_pairs USING (
                image_asset_a_id,
                image_asset_b_id
            )
            JOIN {ASSET_TABLE} asset_a
              ON asset_a.id = phash_pairs.image_asset_a_id
            JOIN {ASSET_TABLE} asset_b
              ON asset_b.id = phash_pairs.image_asset_b_id
            """
        )
        potential_pairs = cursor.fetchall()

    matches = []
    for pair in potential_pairs:
        perceptual_distance = hamming_distance(
            pair["perceptual_hash_a"],
            pair["perceptual_hash_b"],
        )
        difference_distance = hamming_distance(
            pair["difference_hash_a"],
            pair["difference_hash_b"],
        )
        if perceptual_distance <= 8 and difference_distance <= 4:
            matches.append(
                (
                    pair["image_asset_a_id"],
                    pair["image_asset_b_id"],
                    perceptual_distance,
                    difference_distance,
                )
            )

    if matches:
        with connection.cursor() as cursor:
            execute_values(
                cursor,
                f"""
                INSERT INTO {CANDIDATE_TABLE} (
                    "imageAssetAId",
                    "imageAssetBId",
                    status,
                    "perceptualHashDistance",
                    "differenceHashDistance"
                )
                VALUES %s
                ON CONFLICT (
                    "imageAssetAId",
                    "imageAssetBId"
                ) DO UPDATE
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
                [
                    (
                        image_asset_a_id,
                        image_asset_b_id,
                        "review_candidate",
                        perceptual_distance,
                        difference_distance,
                    )
                    for (
                        image_asset_a_id,
                        image_asset_b_id,
                        perceptual_distance,
                        difference_distance,
                    ) in matches
                ],
                page_size=1000,
            )
        connection.commit()
    return {
        "potentialPairs": len(potential_pairs),
        "thresholdMatches": len(matches),
    }


def openclip_candidate_chunk(
    database_url,
    representatives,
    neighbors,
):
    connection = psycopg2.connect(database_url)
    rows = []
    try:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute("SET hnsw.ef_search = 100")
            cursor.execute("SET hnsw.iterative_scan = 'strict_order'")
            cursor.execute("SET hnsw.max_scan_tuples = 50000")
            cursor.execute("SET enable_sort = off")
            for representative in representatives:
                cursor.execute(
                    f"""
                    SELECT
                        asset.id AS image_asset_id,
                        (
                            SELECT MIN(artwork.id)
                            FROM {ARTWORK_TABLE} artwork
                            WHERE artwork."imageAssetId" = asset.id
                        ) AS artwork_id,
                        1 - (
                            asset."imageEmbedding" <=> %s::vector
                        ) AS cosine_similarity
                    FROM {ASSET_TABLE} asset
                    LEFT JOIN {CANONICAL_TABLE} mapping
                      ON mapping."assetId" = asset.id
                    WHERE asset."imageEmbedding" IS NOT NULL
                      AND asset."processingStatus" = 'ready'
                      AND asset.id <> %s
                      AND mapping."assetId" IS NULL
                    ORDER BY asset."imageEmbedding" <=> %s::vector
                    LIMIT %s
                    """,
                    (
                        representative["image_embedding"],
                        representative["image_asset_id"],
                        representative["image_embedding"],
                        neighbors,
                    ),
                )
                for neighbor in cursor.fetchall():
                    rows.append(
                        {
                            "image_asset_a_id": (
                                representative["image_asset_id"]
                            ),
                            "artwork_a_id": representative["artwork_id"],
                            "image_asset_b_id": neighbor["image_asset_id"],
                            "artwork_b_id": neighbor["artwork_id"],
                            "cosine_similarity": float(
                                neighbor["cosine_similarity"]
                            ),
                        }
                    )
    finally:
        connection.close()
    return rows


def openclip_candidates(
    connection,
    database_url,
    neighbors,
    concurrency,
    artwork_ids=None,
):
    if artwork_ids is None:
        cohort_filter = (
            'ingestion."sourceUrl" '
            "LIKE 's3://met-artworks-images/artworks/%%'",
            (),
        )
    else:
        cohort_filter = ("artwork.id = ANY(%s)", (artwork_ids,))

    with connection.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute(
            f"""
            SELECT
                DISTINCT ON (asset.id)
                asset.id AS image_asset_id,
                artwork.id AS artwork_id,
                asset."imageEmbedding"::text AS image_embedding
            FROM {ASSET_TABLE} asset
            JOIN {ARTWORK_TABLE} artwork
              ON artwork."imageAssetId" = asset.id
            JOIN {INGESTION_TABLE} ingestion
              ON ingestion."artworkId" = artwork.id
            LEFT JOIN {CANONICAL_TABLE} mapping
              ON mapping."assetId" = asset.id
            WHERE {cohort_filter[0]}
              AND asset."processingStatus" = 'ready'
              AND asset."imageEmbedding" IS NOT NULL
              AND mapping."assetId" IS NULL
            ORDER BY asset.id, artwork.id
            """,
            cohort_filter[1],
        )
        representatives = cursor.fetchall()
    connection.commit()

    worker_count = max(1, min(concurrency, len(representatives)))
    chunk_size = math.ceil(len(representatives) / worker_count)
    chunks = [
        representatives[start : start + chunk_size]
        for start in range(0, len(representatives), chunk_size)
    ]
    rows = []
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=worker_count
    ) as executor:
        futures = [
            executor.submit(
                openclip_candidate_chunk,
                database_url,
                chunk,
                neighbors,
            )
            for chunk in chunks
        ]
        for future in concurrent.futures.as_completed(futures):
            rows.extend(future.result())
            print(
                json.dumps(
                    {
                        "openclipRepresentatives": len(representatives),
                        "candidateRows": len(rows),
                        "workersCompleted": sum(
                            completed.done() for completed in futures
                        ),
                        "workers": worker_count,
                    },
                    separators=(",", ":"),
                ),
                flush=True,
            )

    pairs = {}
    for row in rows:
        reversed_pair = (
            row["image_asset_a_id"] > row["image_asset_b_id"]
        )
        left, right = sorted(
            (row["image_asset_a_id"], row["image_asset_b_id"])
        )
        key = (left, right)
        candidate = {
            **row,
            "image_asset_a_id": left,
            "image_asset_b_id": right,
            "artwork_a_id": (
                row["artwork_b_id"]
                if reversed_pair
                else row["artwork_a_id"]
            ),
            "artwork_b_id": (
                row["artwork_a_id"]
                if reversed_pair
                else row["artwork_b_id"]
            ),
            "cosine_similarity": float(row["cosine_similarity"]),
        }
        if (
            key not in pairs
            or candidate["cosine_similarity"]
            > pairs[key]["cosine_similarity"]
        ):
            pairs[key] = candidate
    return sorted(
        pairs.values(),
        key=lambda item: item["cosine_similarity"],
        reverse=True,
    )


def hash_candidate_map(connection):
    with connection.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute(
            f"""
            SELECT
                candidate."imageAssetAId" AS image_asset_a_id,
                candidate."imageAssetBId" AS image_asset_b_id,
                candidate.status,
                candidate."perceptualHashDistance"
                    AS perceptual_hash_distance,
                candidate."differenceHashDistance"
                    AS difference_hash_distance,
                (
                    SELECT MIN(id)
                    FROM {ARTWORK_TABLE}
                    WHERE "imageAssetId" = candidate."imageAssetAId"
                ) AS artwork_a_id,
                (
                    SELECT MIN(id)
                    FROM {ARTWORK_TABLE}
                    WHERE "imageAssetId" = candidate."imageAssetBId"
                ) AS artwork_b_id,
                CASE
                    WHEN asset_a."imageEmbedding" IS NOT NULL
                     AND asset_b."imageEmbedding" IS NOT NULL
                    THEN 1 - (
                        asset_a."imageEmbedding"
                        <=> asset_b."imageEmbedding"
                    )
                    ELSE NULL
                END AS cosine_similarity
            FROM {CANDIDATE_TABLE} candidate
            JOIN {ASSET_TABLE} asset_a
              ON asset_a.id = candidate."imageAssetAId"
            JOIN {ASSET_TABLE} asset_b
              ON asset_b.id = candidate."imageAssetBId"
            ORDER BY
                candidate."perceptualHashDistance",
                candidate."differenceHashDistance",
                candidate."imageAssetAId",
                candidate."imageAssetBId"
            """
        )
        candidates = {}
        for row in cursor.fetchall():
            candidate = dict(row)
            if candidate["cosine_similarity"] is not None:
                candidate["cosine_similarity"] = float(
                    candidate["cosine_similarity"]
                )
            candidates[
                (
                    candidate["image_asset_a_id"],
                    candidate["image_asset_b_id"],
                )
            ] = candidate
        return candidates


def asset_thumbnail_keys(connection, asset_ids):
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT id, "thumbnailS3Key"
            FROM {ASSET_TABLE}
            WHERE id = ANY(%s)
            """,
            (list(asset_ids),),
        )
        return dict(cursor.fetchall())


def padded_grayscale(image, size=256):
    image = ImageOps.exif_transpose(image).convert("L")
    image.thumbnail((size, size), Image.Resampling.LANCZOS)
    canvas = Image.new("L", (size, size), 255)
    canvas.paste(
        image,
        ((size - image.width) // 2, (size - image.height) // 2),
    )
    return canvas


def load_thumbnail(s3_client, bucket, key):
    response = s3_client.get_object(Bucket=bucket, Key=key)
    with Image.open(io.BytesIO(response["Body"].read())) as image:
        image.load()
        return image.convert("RGB")


def add_pixel_similarity(candidates, images):
    for candidate in candidates:
        left = padded_grayscale(images[candidate["image_asset_a_id"]])
        right = padded_grayscale(images[candidate["image_asset_b_id"]])
        candidate["pixel_ssim"] = float(
            structural_similarity(
                np.asarray(left),
                np.asarray(right),
                data_range=255,
            )
        )


def write_contact_sheets(candidates, images, output_dir):
    card_width = 1040
    card_height = 286
    rows_per_sheet = 10
    draw_font = None

    for existing_sheet in output_dir.glob("candidate-review-*.jpg"):
        existing_sheet.unlink()

    for page_start in range(0, len(candidates), rows_per_sheet):
        page_candidates = candidates[
            page_start : page_start + rows_per_sheet
        ]
        sheet = Image.new(
            "RGB",
            (card_width, card_height * len(page_candidates)),
            "white",
        )
        draw = ImageDraw.Draw(sheet)
        for row_index, candidate in enumerate(page_candidates):
            top = row_index * card_height
            left_image = images[candidate["image_asset_a_id"]].copy()
            right_image = images[candidate["image_asset_b_id"]].copy()
            left_image.thumbnail((256, 256), Image.Resampling.LANCZOS)
            right_image.thumbnail((256, 256), Image.Resampling.LANCZOS)
            sheet.paste(left_image, (0, top))
            sheet.paste(right_image, (266, top))
            similarity = (
                f"{candidate['cosine_similarity']:.5f}"
                if candidate["cosine_similarity"] is not None
                else "n/a"
            )
            label = (
                f"assets {candidate['image_asset_a_id']} / "
                f"{candidate['image_asset_b_id']}\n"
                f"OpenCLIP {similarity}  "
                f"SSIM {candidate['pixel_ssim']:.5f}\n"
                f"pHash {candidate.get('perceptual_hash_distance')}  "
                f"dHash {candidate.get('difference_hash_distance')}  "
                f"status {candidate.get('status')}"
            )
            draw.multiline_text(
                (532, top + 20),
                label,
                fill="black",
                font=draw_font,
                spacing=8,
            )
        page_number = page_start // rows_per_sheet + 1
        sheet.save(
            output_dir / f"candidate-review-{page_number:02d}.jpg",
            quality=90,
        )


def build_candidate_report(
    connection,
    s3_client,
    configuration,
    output_dir,
    neighbors,
    review_count,
    concurrency,
    artwork_ids=None,
):
    openclip = openclip_candidates(
        connection,
        configuration["database_url"],
        neighbors,
        concurrency,
        artwork_ids,
    )
    hash_candidates = hash_candidate_map(connection)
    all_candidates = {
        (
            candidate["image_asset_a_id"],
            candidate["image_asset_b_id"],
        ): candidate
        for candidate in openclip
    }
    for candidate in openclip:
        candidate.update(
            hash_candidates.get(
                (
                    candidate["image_asset_a_id"],
                    candidate["image_asset_b_id"],
                ),
                {
                    "status": None,
                    "perceptual_hash_distance": None,
                    "difference_hash_distance": None,
                },
            )
        )
    for key, hash_candidate in hash_candidates.items():
        if key in all_candidates:
            continue
        all_candidates[key] = hash_candidate

    hash_first = sorted(
        (
            candidate
            for candidate in all_candidates.values()
            if candidate["status"] == "review_candidate"
        ),
        key=lambda item: (
            item["perceptual_hash_distance"],
            item["difference_hash_distance"],
            -(item["cosine_similarity"] or 0),
        ),
    )
    selected = hash_first[:review_count]
    selected_keys = {
        (
            candidate["image_asset_a_id"],
            candidate["image_asset_b_id"],
        )
        for candidate in selected
    }
    for candidate in openclip:
        key = (
            candidate["image_asset_a_id"],
            candidate["image_asset_b_id"],
        )
        if len(selected) >= review_count:
            break
        if key not in selected_keys:
            selected.append(candidate)
            selected_keys.add(key)

    asset_ids = {
        asset_id
        for candidate in selected
        for asset_id in (
            candidate["image_asset_a_id"],
            candidate["image_asset_b_id"],
        )
    }
    thumbnail_keys = asset_thumbnail_keys(connection, asset_ids)
    images = {
        asset_id: load_thumbnail(
            s3_client,
            configuration["bucket"],
            thumbnail_keys[asset_id],
        )
        for asset_id in asset_ids
    }
    add_pixel_similarity(selected, images)
    write_contact_sheets(selected, images, output_dir)

    report = {
        "openclipPairCount": len(openclip),
        "hashCandidateCount": len(hash_candidates),
        "reviewSampleCount": len(selected),
        "reviewSample": selected,
        "topOpenclipCandidates": openclip[:1000],
    }
    (output_dir / "duplicate-candidates.json").write_text(
        json.dumps(report, indent=2, default=str)
    )
    return {
        "openclipPairCount": report["openclipPairCount"],
        "hashCandidateCount": report["hashCandidateCount"],
        "reviewSampleCount": report["reviewSampleCount"],
        "reportPath": str(output_dir / "duplicate-candidates.json"),
    }


def parse_args():
    parser = argparse.ArgumentParser(
        description="Audit the Priority 3 legacy image migration"
    )
    parser.add_argument(
        "--output-dir",
        default="migration-reports/priority3",
    )
    cohort = parser.add_mutually_exclusive_group()
    cohort.add_argument(
        "--cohort-manifest",
        help=(
            "frozen JSONL cohort manifest; defaults to "
            "<output-dir>/rollback-manifest.jsonl when that file exists"
        ),
    )
    cohort.add_argument(
        "--source-url-cohort",
        action="store_true",
        help=(
            "ignore an existing manifest and select the legacy cohort from "
            "current s3://.../artworks/... ingestion source URLs"
        ),
    )
    parser.add_argument(
        "--refresh-cohort-links",
        action="store_true",
        help=(
            "atomically refresh current canonical link fields in the loaded "
            "frozen manifest; terminal rows remain unchanged"
        ),
    )
    parser.add_argument("--build-candidates", action="store_true")
    parser.add_argument("--rebuild-hash-candidates", action="store_true")
    parser.add_argument("--neighbors", type=int, default=5)
    parser.add_argument("--review-count", type=int, default=50)
    parser.add_argument("--openclip-concurrency", type=int, default=8)
    return parser.parse_args()


def main():
    args = parse_args()
    configuration = settings()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    default_manifest_path = output_dir / "rollback-manifest.jsonl"
    cohort_manifest_path = (
        Path(args.cohort_manifest)
        if args.cohort_manifest
        else default_manifest_path
    )
    cohort_records = None
    if not args.source_url_cohort:
        if cohort_manifest_path.exists():
            cohort_records = load_cohort_manifest(cohort_manifest_path)
        elif args.cohort_manifest:
            raise RuntimeError(
                f"cohort manifest does not exist: {cohort_manifest_path}"
            )
    if args.refresh_cohort_links and cohort_records is None:
        raise RuntimeError(
            "--refresh-cohort-links requires an existing cohort manifest"
        )
    artwork_ids = (
        [record["artwork_id"] for record in cohort_records]
        if cohort_records is not None
        else None
    )
    connection = psycopg2.connect(configuration["database_url"])
    s3_client = aws_session(configuration).client("s3")

    try:
        hash_rebuild = (
            rebuild_hash_candidates(connection)
            if args.rebuild_hash_candidates
            else None
        )
        summary = database_summary(connection, artwork_ids)
        summary["cohort"] = {
            "source": (
                "manifest"
                if cohort_records is not None
                else "current_legacy_source_urls"
            ),
            "records": (
                len(cohort_records)
                if cohort_records is not None
                else summary["links"]["total"]
            ),
            "manifestPath": (
                str(cohort_manifest_path)
                if cohort_records is not None
                else None
            ),
        }
        if hash_rebuild is not None:
            summary["hashCandidateRebuild"] = hash_rebuild
        summary["s3"] = {
            "legacy": s3_prefix_summary(
                s3_client,
                configuration["bucket"],
                "artworks/",
            ),
            "canonical": s3_prefix_summary(
                s3_client,
                configuration["bucket"],
                "assets/v1/",
            ),
        }
        if cohort_records is None:
            write_rollback_manifest(
                connection,
                default_manifest_path,
                configuration["cdn"],
            )
            summary["cohort"]["manifestPath"] = str(
                default_manifest_path
            )
            summary["cohort"]["manifestWritten"] = True
        else:
            summary["cohort"]["manifestWritten"] = False
        if args.build_candidates:
            summary["candidateReport"] = build_candidate_report(
                connection,
                s3_client,
                configuration,
                output_dir,
                args.neighbors,
                args.review_count,
                args.openclip_concurrency,
                artwork_ids,
            )
        if args.refresh_cohort_links:
            cohort_records, refresh_summary = refresh_cohort_links(
                connection,
                cohort_records,
                cohort_manifest_path,
            )
            summary["cohort"]["linkRefresh"] = refresh_summary
            summary["cohort"]["manifestWritten"] = True
        (output_dir / "summary.json").write_text(
            json.dumps(summary, indent=2, default=str)
        )
        print(json.dumps(summary, indent=2, default=str))
    finally:
        connection.close()


if __name__ == "__main__":
    main()
