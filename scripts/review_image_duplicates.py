#!/usr/bin/env python3

import argparse
import csv
import hashlib
import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import RealDictCursor

load_dotenv()

ARTWORK_TABLE = '"met-galaxy_artwork"'
ASSET_TABLE = '"met-galaxy_image_asset"'
CANDIDATE_TABLE = '"met-galaxy_image_duplicate_candidate"'
CANONICAL_TABLE = '"met-galaxy_image_asset_canonical"'
INGESTION_TABLE = '"met-galaxy_image_ingestion"'
OUTBOX_TABLE = '"met-galaxy_image_embedding_outbox"'
MERGE_LOCK_ID = 4_607_632_118
STATUSES = ("review_candidate", "verified_duplicate", "rejected")


class ReviewError(RuntimeError):
    pass


@dataclass(frozen=True)
class Configuration:
    database_url: str
    cdn_base_url: str


def configuration():
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise ReviewError("DATABASE_URL is required")
    return Configuration(
        database_url=database_url,
        cdn_base_url=os.getenv(
            "IMAGE_CDN_BASE_URL",
            "https://d2pvxr3eb77vb4.cloudfront.net",
        ).rstrip("/"),
    )


def json_default(value):
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def emit(payload, output=sys.stdout):
    print(
        json.dumps(
            payload,
            default=json_default,
            indent=2,
            sort_keys=True,
        ),
        file=output,
    )


def connect(settings):
    connection = psycopg2.connect(settings.database_url)
    connection.autocommit = False
    return connection


def begin_resolution(connection):
    with connection.cursor() as cursor:
        cursor.execute(
            "SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"
        )
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", (MERGE_LOCK_ID,))


def normalize_pair(asset_a_id, asset_b_id):
    if asset_a_id == asset_b_id:
        raise ReviewError("candidate assets must be different")
    return tuple(sorted((asset_a_id, asset_b_id)))


def candidate_selector(args):
    if args.candidate_id is not None:
        return "candidate.id = %s", (args.candidate_id,)
    asset_a_id, asset_b_id = normalize_pair(
        args.asset_a_id,
        args.asset_b_id,
    )
    return (
        'candidate."imageAssetAId" = %s '
        'AND candidate."imageAssetBId" = %s',
        (asset_a_id, asset_b_id),
    )


def fetch_candidate(connection, args, lock=False):
    condition, parameters = candidate_selector(args)
    lock_clause = "FOR UPDATE" if lock else ""
    with connection.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute(
            f"""
            SELECT
                candidate.id,
                candidate."imageAssetAId" AS image_asset_a_id,
                candidate."imageAssetBId" AS image_asset_b_id,
                candidate.status,
                candidate."perceptualHashDistance"
                    AS perceptual_hash_distance,
                candidate."differenceHashDistance"
                    AS difference_hash_distance,
                candidate."createdAt" AS created_at,
                candidate."reviewedAt" AS reviewed_at
            FROM {CANDIDATE_TABLE} candidate
            WHERE {condition}
            {lock_clause}
            """,
            parameters,
        )
        candidate = cursor.fetchone()
    if candidate is None:
        if args.candidate_id is not None:
            identifier = f"id {args.candidate_id}"
        else:
            identifier = (
                f"pair {args.asset_a_id}/{args.asset_b_id}"
            )
        raise ReviewError(f"duplicate candidate {identifier} was not found")
    return dict(candidate)


def candidate_rows(connection, status, limit, offset):
    parameters = []
    condition = ""
    if status != "all":
        condition = "WHERE candidate.status = %s"
        parameters.append(status)
    parameters.extend((limit, offset))

    with connection.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute(
            f"""
            SELECT
                candidate.id,
                candidate."imageAssetAId" AS image_asset_a_id,
                COALESCE(
                    map_a."canonicalAssetId",
                    candidate."imageAssetAId"
                ) AS canonical_asset_a_id,
                candidate."imageAssetBId" AS image_asset_b_id,
                COALESCE(
                    map_b."canonicalAssetId",
                    candidate."imageAssetBId"
                ) AS canonical_asset_b_id,
                candidate.status,
                candidate."perceptualHashDistance"
                    AS perceptual_hash_distance,
                candidate."differenceHashDistance"
                    AS difference_hash_distance,
                CASE
                    WHEN asset_a."imageEmbedding" IS NOT NULL
                    THEN 1 - (
                        asset_a."imageEmbedding"
                        <=> asset_b."imageEmbedding"
                    )
                    ELSE NULL
                END AS cosine_similarity,
                asset_a."thumbnailS3Key" AS thumbnail_s3_key_a,
                asset_b."thumbnailS3Key" AS thumbnail_s3_key_b,
                artwork_a.id AS representative_artwork_a_id,
                artwork_a.title AS representative_title_a,
                artwork_b.id AS representative_artwork_b_id,
                artwork_b.title AS representative_title_b,
                candidate."createdAt" AS created_at,
                candidate."reviewedAt" AS reviewed_at
            FROM {CANDIDATE_TABLE} candidate
            JOIN {ASSET_TABLE} asset_a
              ON asset_a.id = candidate."imageAssetAId"
            JOIN {ASSET_TABLE} asset_b
              ON asset_b.id = candidate."imageAssetBId"
            LEFT JOIN {CANONICAL_TABLE} map_a
              ON map_a."assetId" = candidate."imageAssetAId"
            LEFT JOIN {CANONICAL_TABLE} map_b
              ON map_b."assetId" = candidate."imageAssetBId"
            LEFT JOIN LATERAL (
                SELECT artwork.id, artwork.title
                FROM {ARTWORK_TABLE} artwork
                WHERE artwork."imageAssetId" = COALESCE(
                    map_a."canonicalAssetId",
                    candidate."imageAssetAId"
                )
                ORDER BY artwork.id
                LIMIT 1
            ) artwork_a ON TRUE
            LEFT JOIN LATERAL (
                SELECT artwork.id, artwork.title
                FROM {ARTWORK_TABLE} artwork
                WHERE artwork."imageAssetId" = COALESCE(
                    map_b."canonicalAssetId",
                    candidate."imageAssetBId"
                )
                ORDER BY artwork.id
                LIMIT 1
            ) artwork_b ON TRUE
            {condition}
            ORDER BY
                CASE candidate.status
                    WHEN 'review_candidate' THEN 0
                    WHEN 'verified_duplicate' THEN 1
                    ELSE 2
                END,
                candidate."perceptualHashDistance" NULLS LAST,
                candidate."differenceHashDistance" NULLS LAST,
                candidate.id
            LIMIT %s OFFSET %s
            """,
            parameters,
        )
        rows = [dict(row) for row in cursor.fetchall()]
        cursor.execute(
            f"""
            SELECT status, COUNT(*)::int AS count
            FROM {CANDIDATE_TABLE}
            GROUP BY status
            ORDER BY status
            """
        )
        counts = {
            status: 0 for status in STATUSES
        }
        counts.update(
            {row["status"]: row["count"] for row in cursor.fetchall()}
        )

    for row in rows:
        if row["cosine_similarity"] is not None:
            row["cosine_similarity"] = float(
                row["cosine_similarity"]
            )
    return rows, counts


def list_candidates(connection, args):
    rows, counts = candidate_rows(
        connection,
        args.status,
        args.limit,
        args.offset,
    )
    return {
        "command": "list",
        "filters": {
            "status": args.status,
            "limit": args.limit,
            "offset": args.offset,
        },
        "countsByStatus": counts,
        "returned": len(rows),
        "candidates": rows,
    }


def export_candidates(connection, args):
    rows, counts = candidate_rows(
        connection,
        args.status,
        args.limit,
        args.offset,
    )
    output_path = Path(args.output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if args.format == "json":
        document = {
            "schemaVersion": 1,
            "generatedAt": datetime.now(UTC),
            "filters": {
                "status": args.status,
                "limit": args.limit,
                "offset": args.offset,
            },
            "countsByStatus": counts,
            "candidates": rows,
        }
        output_path.write_text(
            json.dumps(
                document,
                default=json_default,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
    else:
        fieldnames = list(rows[0]) if rows else [
            "id",
            "image_asset_a_id",
            "canonical_asset_a_id",
            "image_asset_b_id",
            "canonical_asset_b_id",
            "status",
        ]
        with output_path.open("w", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    content = output_path.read_bytes()
    return {
        "command": "export",
        "format": args.format,
        "output": str(output_path),
        "bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "returned": len(rows),
        "countsByStatus": counts,
    }


def reject_candidate(connection, args):
    begin_resolution(connection)
    candidate = fetch_candidate(connection, args, lock=True)
    if candidate["status"] == "verified_duplicate":
        raise ReviewError(
            "a verified duplicate candidate cannot be rejected"
        )

    asset_a_id = candidate["image_asset_a_id"]
    asset_b_id = candidate["image_asset_b_id"]
    with connection.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute(
            f"""
            SELECT
                COALESCE(map_a."canonicalAssetId", %s) AS root_a,
                COALESCE(map_b."canonicalAssetId", %s) AS root_b
            FROM (SELECT 1) seed
            LEFT JOIN {CANONICAL_TABLE} map_a
              ON map_a."assetId" = %s
            LEFT JOIN {CANONICAL_TABLE} map_b
              ON map_b."assetId" = %s
            """,
            (asset_a_id, asset_b_id, asset_a_id, asset_b_id),
        )
        roots = cursor.fetchone()
        if roots["root_a"] == roots["root_b"]:
            raise ReviewError(
                "candidate assets already belong to one verified group"
            )

        changed = candidate["status"] != "rejected"
        cursor.execute(
            f"""
            UPDATE {CANDIDATE_TABLE}
            SET
                status = 'rejected',
                "reviewedAt" = COALESCE(
                    "reviewedAt",
                    CURRENT_TIMESTAMP
                )
            WHERE id = %s
              AND (
                status <> 'rejected'
                OR "reviewedAt" IS NULL
              )
            RETURNING "reviewedAt" AS reviewed_at
            """,
            (candidate["id"],),
        )
        updated = cursor.fetchone()

    if args.dry_run:
        connection.rollback()
    else:
        connection.commit()
    return {
        "command": "reject",
        "dryRun": args.dry_run,
        "committed": not args.dry_run,
        "candidateId": candidate["id"],
        "assetIds": [asset_a_id, asset_b_id],
        "previousStatus": candidate["status"],
        "status": "rejected",
        "changed": changed or updated is not None,
        "reviewedAt": (
            updated["reviewed_at"]
            if updated
            else candidate["reviewed_at"]
        ),
    }


def verified_component_ids(connection, asset_a_id, asset_b_id):
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            WITH RECURSIVE edges(asset_a_id, asset_b_id) AS (
                SELECT
                    "imageAssetAId",
                    "imageAssetBId"
                FROM {CANDIDATE_TABLE}
                WHERE status = 'verified_duplicate'
                UNION
                SELECT "assetId", "canonicalAssetId"
                FROM {CANONICAL_TABLE}
                UNION
                SELECT %s::integer, %s::integer
            ),
            directed(source_id, destination_id) AS (
                SELECT asset_a_id, asset_b_id FROM edges
                UNION
                SELECT asset_b_id, asset_a_id FROM edges
            ),
            component(asset_id) AS (
                SELECT %s::integer
                UNION
                SELECT directed.destination_id
                FROM directed
                JOIN component
                  ON component.asset_id = directed.source_id
            )
            SELECT asset_id
            FROM component
            ORDER BY asset_id
            """,
            (asset_a_id, asset_b_id, asset_a_id),
        )
        return [row[0] for row in cursor.fetchall()]


def load_component_assets(connection, asset_ids):
    with connection.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute(
            f"""
            SELECT
                id,
                "fullS3Key" AS full_s3_key,
                "thumbnailS3Key" AS thumbnail_s3_key,
                "processingStatus" AS processing_status,
                ("imageEmbedding" IS NOT NULL) AS has_embedding
            FROM {ASSET_TABLE}
            WHERE id = ANY(%s)
            ORDER BY id
            FOR UPDATE
            """,
            (asset_ids,),
        )
        assets = [dict(row) for row in cursor.fetchall()]
    found_ids = {asset["id"] for asset in assets}
    missing_ids = sorted(set(asset_ids) - found_ids)
    if missing_ids:
        raise ReviewError(
            f"verified group references missing assets: {missing_ids}"
        )
    return assets


def rejected_conflicts(connection, asset_ids, target_id):
    with connection.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute(
            f"""
            SELECT
                id,
                "imageAssetAId" AS image_asset_a_id,
                "imageAssetBId" AS image_asset_b_id
            FROM {CANDIDATE_TABLE}
            WHERE status = 'rejected'
              AND id <> %s
              AND "imageAssetAId" = ANY(%s)
              AND "imageAssetBId" = ANY(%s)
            ORDER BY id
            """,
            (target_id, asset_ids, asset_ids),
        )
        return [dict(row) for row in cursor.fetchall()]


def current_mappings(connection, asset_ids):
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT "assetId", "canonicalAssetId"
            FROM {CANONICAL_TABLE}
            WHERE "assetId" = ANY(%s)
            ORDER BY "assetId"
            """,
            (asset_ids,),
        )
        return dict(cursor.fetchall())


def choose_embedding_donor(assets, survivor_id):
    ready = [
        asset["id"]
        for asset in assets
        if asset["processing_status"] == "ready"
        and asset["has_embedding"]
    ]
    if survivor_id in ready:
        return survivor_id
    return min(ready) if ready else None


def merge_candidate(connection, settings, args):
    begin_resolution(connection)
    candidate = fetch_candidate(connection, args, lock=True)
    if candidate["status"] == "rejected":
        raise ReviewError(
            "a rejected candidate cannot be verified without a new review"
        )

    asset_a_id = candidate["image_asset_a_id"]
    asset_b_id = candidate["image_asset_b_id"]
    asset_ids = verified_component_ids(
        connection,
        asset_a_id,
        asset_b_id,
    )
    assets = load_component_assets(connection, asset_ids)
    conflicts = rejected_conflicts(
        connection,
        asset_ids,
        candidate["id"],
    )
    if conflicts:
        conflict_ids = [conflict["id"] for conflict in conflicts]
        raise ReviewError(
            "verified merge conflicts with rejected candidate IDs "
            f"{conflict_ids}"
        )

    survivor_id = min(asset_ids)
    survivor = next(
        asset for asset in assets if asset["id"] == survivor_id
    )
    if not survivor["full_s3_key"]:
        raise ReviewError(
            f"deterministic survivor asset {survivor_id} has no full S3 key"
        )
    loser_ids = [
        asset_id for asset_id in asset_ids if asset_id != survivor_id
    ]
    donor_id = choose_embedding_donor(assets, survivor_id)
    canonical_url = (
        f"{settings.cdn_base_url}/{survivor['full_s3_key']}"
    )
    mappings_before = current_mappings(connection, asset_ids)
    mapping_changes = sum(
        mappings_before.get(loser_id) != survivor_id
        for loser_id in loser_ids
    ) + int(survivor_id in mappings_before)

    with connection.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute(
            f"""
            SELECT
                id,
                "imageAssetId" AS image_asset_id,
                "imageDuplicateState" AS duplicate_state
            FROM {ARTWORK_TABLE}
            WHERE "imageAssetId" = ANY(%s)
              AND "imageAssetId" <> %s
            ORDER BY id
            FOR UPDATE
            """,
            (asset_ids, survivor_id),
        )
        moved_artworks = [dict(row) for row in cursor.fetchall()]
        moved_artwork_ids = [
            artwork["id"] for artwork in moved_artworks
        ]
        moved_by_asset = {}
        for artwork in moved_artworks:
            asset_id = artwork["image_asset_id"]
            moved_by_asset[asset_id] = (
                moved_by_asset.get(asset_id, 0) + 1
            )

        cursor.execute(
            f"""
            DELETE FROM {CANONICAL_TABLE}
            WHERE "assetId" = %s
            """,
            (survivor_id,),
        )
        for loser_id in loser_ids:
            cursor.execute(
                f"""
                INSERT INTO {CANONICAL_TABLE} (
                    "assetId",
                    "canonicalAssetId"
                )
                VALUES (%s, %s)
                ON CONFLICT ("assetId") DO UPDATE
                SET
                    "canonicalAssetId" = EXCLUDED."canonicalAssetId",
                    "updatedAt" = CURRENT_TIMESTAMP
                WHERE
                    {CANONICAL_TABLE}."canonicalAssetId"
                    IS DISTINCT FROM EXCLUDED."canonicalAssetId"
                """,
                (loser_id, survivor_id),
            )

        if donor_id is not None:
            cursor.execute(
                f"""
                UPDATE {ASSET_TABLE} AS survivor
                SET
                    "imageEmbedding" = donor."imageEmbedding",
                    "processingStatus" = 'ready',
                    "processingNextAttemptAt" = NULL,
                    "processingLeaseOwner" = NULL,
                    "processingLeaseExpiresAt" = NULL,
                    "lastError" = NULL,
                    "updatedAt" = CURRENT_TIMESTAMP
                FROM {ASSET_TABLE} AS donor
                WHERE survivor.id = %s
                  AND donor.id = %s
                  AND (
                    survivor."imageEmbedding"
                        IS DISTINCT FROM donor."imageEmbedding"
                    OR survivor."processingStatus" <> 'ready'
                    OR survivor."lastError" IS NOT NULL
                  )
                """,
                (survivor_id, donor_id),
            )
            survivor_embedding_changed = cursor.rowcount
        else:
            cursor.execute(
                f"""
                UPDATE {ASSET_TABLE}
                SET
                    "processingStatus" = 'pending_embedding',
                    "processingAttemptCount" = 0,
                    "processingNextAttemptAt" = NULL,
                    "processingLeaseOwner" = NULL,
                    "processingLeaseExpiresAt" = NULL,
                    "lastError" = NULL,
                    "updatedAt" = CURRENT_TIMESTAMP
                WHERE id = %s
                  AND (
                    "processingStatus" <> 'pending_embedding'
                    OR "processingAttemptCount" <> 0
                    OR "processingNextAttemptAt" IS NOT NULL
                    OR "processingLeaseOwner" IS NOT NULL
                    OR "processingLeaseExpiresAt" IS NOT NULL
                    OR "lastError" IS NOT NULL
                  )
                """,
                (survivor_id,),
            )
            survivor_embedding_changed = cursor.rowcount
            cursor.execute(
                f"""
                INSERT INTO {OUTBOX_TABLE} (
                    "imageAssetId",
                    status,
                    "attemptCount",
                    "nextAttemptAt",
                    "lastError"
                )
                VALUES (%s, 'pending', 0, NULL, NULL)
                ON CONFLICT ("imageAssetId") DO UPDATE
                SET
                    status = 'pending',
                    "attemptCount" = 0,
                    "nextAttemptAt" = NULL,
                    "messageId" = NULL,
                    "lastError" = NULL,
                    "dispatchedAt" = NULL
                WHERE
                    {OUTBOX_TABLE}.status <> 'pending'
                    OR {OUTBOX_TABLE}."attemptCount" <> 0
                    OR {OUTBOX_TABLE}."nextAttemptAt" IS NOT NULL
                    OR {OUTBOX_TABLE}."messageId" IS NOT NULL
                    OR {OUTBOX_TABLE}."lastError" IS NOT NULL
                    OR {OUTBOX_TABLE}."dispatchedAt" IS NOT NULL
                """,
                (survivor_id,),
            )
            embedding_outbox_queued = cursor.rowcount
        if donor_id is not None:
            embedding_outbox_queued = 0

        if moved_artwork_ids:
            cursor.execute(
                f"""
                UPDATE {ARTWORK_TABLE} AS artwork
                SET
                    "imageAssetId" = %s,
                    "imageDuplicateState" = 'verified_duplicate',
                    "localImageUrl" = %s,
                    "imgVec" = survivor."imageEmbedding"
                FROM {ASSET_TABLE} AS survivor
                WHERE artwork.id = ANY(%s)
                  AND survivor.id = %s
                """,
                (
                    survivor_id,
                    canonical_url,
                    moved_artwork_ids,
                    survivor_id,
                ),
            )
            artworks_relinked = cursor.rowcount
        else:
            artworks_relinked = 0

        cursor.execute(
            f"""
            UPDATE {ARTWORK_TABLE} AS artwork
            SET
                "localImageUrl" = %s,
                "imgVec" = survivor."imageEmbedding"
            FROM {ASSET_TABLE} AS survivor
            WHERE artwork."imageAssetId" = %s
              AND survivor.id = %s
              AND (
                artwork."localImageUrl" IS DISTINCT FROM %s
                OR artwork."imgVec"
                    IS DISTINCT FROM survivor."imageEmbedding"
              )
            """,
            (
                canonical_url,
                survivor_id,
                survivor_id,
                canonical_url,
            ),
        )
        root_associations_synchronized = cursor.rowcount

        if donor_id is not None:
            cursor.execute(
                f"""
                UPDATE {INGESTION_TABLE}
                SET
                    status = 'complete',
                    "completedAt" = COALESCE(
                        "completedAt",
                        CURRENT_TIMESTAMP
                    ),
                    "leaseOwner" = NULL,
                    "leaseExpiresAt" = NULL,
                    "nextAttemptAt" = NULL,
                    "lastError" = NULL,
                    "updatedAt" = CURRENT_TIMESTAMP
                WHERE "artworkId" IN (
                    SELECT id
                    FROM {ARTWORK_TABLE}
                    WHERE "imageAssetId" = %s
                )
                  AND status <> 'terminal_failure'
                  AND (
                    status = 'awaiting_embedding'
                    OR "artworkId" = ANY(%s)
                  )
                  AND (
                    status <> 'complete'
                    OR "completedAt" IS NULL
                    OR "leaseOwner" IS NOT NULL
                    OR "leaseExpiresAt" IS NOT NULL
                    OR "nextAttemptAt" IS NOT NULL
                    OR "lastError" IS NOT NULL
                  )
                """,
                (survivor_id, moved_artwork_ids),
            )
            ingestion_rows_updated = cursor.rowcount
        elif moved_artwork_ids:
            cursor.execute(
                f"""
                UPDATE {INGESTION_TABLE}
                SET
                    status = 'awaiting_embedding',
                    "completedAt" = NULL,
                    "leaseOwner" = NULL,
                    "leaseExpiresAt" = NULL,
                    "nextAttemptAt" = NULL,
                    "lastError" = NULL,
                    "updatedAt" = CURRENT_TIMESTAMP
                WHERE "artworkId" = ANY(%s)
                  AND status <> 'terminal_failure'
                """,
                (moved_artwork_ids,),
            )
            ingestion_rows_updated = cursor.rowcount
        else:
            ingestion_rows_updated = 0

        cursor.execute(
            f"""
            UPDATE {CANDIDATE_TABLE}
            SET
                status = 'verified_duplicate',
                "reviewedAt" = COALESCE(
                    "reviewedAt",
                    CURRENT_TIMESTAMP
                )
            WHERE "imageAssetAId" = ANY(%s)
              AND "imageAssetBId" = ANY(%s)
              AND (
                status = 'review_candidate'
                OR (
                    status = 'verified_duplicate'
                    AND "reviewedAt" IS NULL
                )
              )
            """,
            (asset_ids, asset_ids),
        )
        candidates_reviewed = cursor.rowcount

    if args.dry_run:
        connection.rollback()
    else:
        connection.commit()

    changed = any(
        (
            mapping_changes,
            artworks_relinked,
            root_associations_synchronized,
            survivor_embedding_changed,
            embedding_outbox_queued,
            ingestion_rows_updated,
            candidates_reviewed,
        )
    )
    return {
        "command": "verify",
        "dryRun": args.dry_run,
        "committed": not args.dry_run,
        "candidateId": candidate["id"],
        "previousStatus": candidate["status"],
        "status": "verified_duplicate",
        "componentAssetIds": asset_ids,
        "survivorAssetId": survivor_id,
        "loserAssetIds": loser_ids,
        "retainedLoserAssetCount": len(loser_ids),
        "retainedLoserS3Keys": sum(
            bool(asset["full_s3_key"])
            + bool(asset["thumbnail_s3_key"])
            for asset in assets
            if asset["id"] != survivor_id
        ),
        "embeddingDonorAssetId": donor_id,
        "embeddingReady": donor_id is not None,
        "embeddingOutboxQueued": embedding_outbox_queued,
        "mappingRowsChanged": mapping_changes,
        "artworksRelinked": artworks_relinked,
        "artworksRelinkedByPreviousAsset": moved_by_asset,
        "rootAssociationsSynchronized": (
            root_associations_synchronized
        ),
        "ingestionRowsUpdated": ingestion_rows_updated,
        "candidatesReviewed": candidates_reviewed,
        "changed": changed,
    }


def scalar_counts(connection):
    statements = {
        "candidateCounts": f"""
            SELECT status, COUNT(*)::int AS count
            FROM {CANDIDATE_TABLE}
            GROUP BY status
            ORDER BY status
        """,
        "mappingSummary": f"""
            SELECT
                COUNT(*)::int AS loser_assets,
                COUNT(DISTINCT "canonicalAssetId")::int
                    AS canonical_roots
            FROM {CANONICAL_TABLE}
        """,
        "invariants": f"""
            SELECT
                (
                    SELECT COUNT(*)::int
                    FROM {CANONICAL_TABLE} mapping
                    WHERE mapping."assetId" = mapping."canonicalAssetId"
                ) AS self_mappings,
                (
                    SELECT COUNT(*)::int
                    FROM {CANONICAL_TABLE} mapping
                    JOIN {CANONICAL_TABLE} parent
                      ON parent."assetId" = mapping."canonicalAssetId"
                ) AS chained_mappings,
                (
                    SELECT COUNT(*)::int
                    FROM {ARTWORK_TABLE} artwork
                    JOIN {CANONICAL_TABLE} mapping
                      ON mapping."assetId" = artwork."imageAssetId"
                ) AS artworks_linked_to_losers,
                (
                    SELECT COUNT(*)::int
                    FROM {CANDIDATE_TABLE} candidate
                    LEFT JOIN {CANONICAL_TABLE} map_a
                      ON map_a."assetId" = candidate."imageAssetAId"
                    LEFT JOIN {CANONICAL_TABLE} map_b
                      ON map_b."assetId" = candidate."imageAssetBId"
                    WHERE candidate.status = 'verified_duplicate'
                      AND COALESCE(
                          map_a."canonicalAssetId",
                          candidate."imageAssetAId"
                      ) <> COALESCE(
                          map_b."canonicalAssetId",
                          candidate."imageAssetBId"
                      )
                ) AS verified_pairs_with_different_roots,
                (
                    SELECT COUNT(*)::int
                    FROM {CANDIDATE_TABLE} candidate
                    LEFT JOIN {CANONICAL_TABLE} map_a
                      ON map_a."assetId" = candidate."imageAssetAId"
                    LEFT JOIN {CANONICAL_TABLE} map_b
                      ON map_b."assetId" = candidate."imageAssetBId"
                    WHERE candidate.status = 'rejected'
                      AND COALESCE(
                          map_a."canonicalAssetId",
                          candidate."imageAssetAId"
                      ) = COALESCE(
                          map_b."canonicalAssetId",
                          candidate."imageAssetBId"
                      )
                ) AS rejected_pairs_inside_groups,
                (
                    SELECT COUNT(*)::int
                    FROM {CANDIDATE_TABLE} candidate
                    LEFT JOIN {CANONICAL_TABLE} map_a
                      ON map_a."assetId" = candidate."imageAssetAId"
                    LEFT JOIN {CANONICAL_TABLE} map_b
                      ON map_b."assetId" = candidate."imageAssetBId"
                    WHERE candidate.status = 'review_candidate'
                      AND COALESCE(
                          map_a."canonicalAssetId",
                          candidate."imageAssetAId"
                      ) = COALESCE(
                          map_b."canonicalAssetId",
                          candidate."imageAssetBId"
                      )
                ) AS unresolved_pairs_inside_groups,
                (
                    SELECT COUNT(*)::int
                    FROM {CANONICAL_TABLE} mapping
                    JOIN {ASSET_TABLE} loser
                      ON loser.id = mapping."assetId"
                    WHERE loser."fullS3Key" IS NULL
                       OR loser."thumbnailS3Key" IS NULL
                ) AS loser_assets_without_object_keys,
                (
                    SELECT COUNT(*)::int
                    FROM (
                        SELECT DISTINCT artwork."imageAssetId"
                        FROM {ARTWORK_TABLE} artwork
                        JOIN {ASSET_TABLE} asset
                          ON asset.id = artwork."imageAssetId"
                        WHERE artwork."imageAssetId" IS NOT NULL
                          AND (
                            asset."processingStatus" <> 'ready'
                            OR asset."imageEmbedding" IS NULL
                          )
                    ) unready
                ) AS referenced_roots_without_embeddings,
                (
                    SELECT COUNT(*)::int
                    FROM {ARTWORK_TABLE} artwork
                    JOIN {ASSET_TABLE} asset
                      ON asset.id = artwork."imageAssetId"
                    WHERE artwork."imgVec"
                        IS DISTINCT FROM asset."imageEmbedding"
                ) AS artwork_embedding_mismatches
        """,
    }
    with connection.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute(statements["candidateCounts"])
        candidate_counts = {status: 0 for status in STATUSES}
        candidate_counts.update(
            {row["status"]: row["count"] for row in cursor.fetchall()}
        )
        cursor.execute(statements["mappingSummary"])
        mapping = dict(cursor.fetchone())
        cursor.execute(statements["invariants"])
        invariants = dict(cursor.fetchone())
    return candidate_counts, mapping, invariants


def audit(connection):
    candidate_counts, mapping, invariants = scalar_counts(connection)
    healthy = all(value == 0 for value in invariants.values())
    return {
        "command": "audit",
        "candidateCounts": candidate_counts,
        "mapping": mapping,
        "invariants": invariants,
        "healthy": healthy,
    }


def add_candidate_identifier(parser):
    identifier = parser.add_mutually_exclusive_group(required=True)
    identifier.add_argument("--candidate-id", type=int)
    identifier.add_argument(
        "--asset-ids",
        type=int,
        nargs=2,
        metavar=("ASSET_A_ID", "ASSET_B_ID"),
    )


def validate_candidate_identifier(args):
    if args.asset_ids:
        args.asset_a_id, args.asset_b_id = args.asset_ids
    else:
        args.asset_a_id = None
        args.asset_b_id = None


def positive_int(value):
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def nonnegative_int(value):
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must be nonnegative")
    return parsed


def parser():
    root = argparse.ArgumentParser(
        description=(
            "Review, reject, and transactionally merge image duplicate "
            "candidates."
        )
    )
    commands = root.add_subparsers(dest="command", required=True)

    list_parser = commands.add_parser(
        "list",
        help="list candidates as JSON",
    )
    list_parser.add_argument(
        "--status",
        choices=("all", *STATUSES),
        default="review_candidate",
    )
    list_parser.add_argument("--limit", type=positive_int, default=100)
    list_parser.add_argument("--offset", type=nonnegative_int, default=0)

    export_parser = commands.add_parser(
        "export",
        help="export candidates to JSON or CSV",
    )
    export_parser.add_argument("--output", required=True)
    export_parser.add_argument(
        "--format",
        choices=("json", "csv"),
        default="json",
    )
    export_parser.add_argument(
        "--status",
        choices=("all", *STATUSES),
        default="review_candidate",
    )
    export_parser.add_argument(
        "--limit",
        type=positive_int,
        default=10_000,
    )
    export_parser.add_argument(
        "--offset",
        type=nonnegative_int,
        default=0,
    )

    reject_parser = commands.add_parser(
        "reject",
        help="reject one review candidate",
    )
    add_candidate_identifier(reject_parser)
    reject_parser.add_argument("--dry-run", action="store_true")

    verify_parser = commands.add_parser(
        "verify",
        help="verify and merge one candidate's complete duplicate group",
    )
    add_candidate_identifier(verify_parser)
    verify_parser.add_argument("--dry-run", action="store_true")

    commands.add_parser(
        "audit",
        help="report exact candidate, mapping, and consistency counts",
    )
    return root


def main():
    args = parser().parse_args()
    if args.command in ("reject", "verify"):
        validate_candidate_identifier(args)

    settings = configuration()
    connection = connect(settings)
    try:
        if args.command == "list":
            result = list_candidates(connection, args)
        elif args.command == "export":
            result = export_candidates(connection, args)
        elif args.command == "reject":
            result = reject_candidate(connection, args)
        elif args.command == "verify":
            result = merge_candidate(
                connection,
                settings,
                args,
            )
        else:
            result = audit(connection)
        emit(result)
    except Exception as error:
        connection.rollback()
        emit(
            {
                "command": args.command,
                "error": str(error),
                "errorType": type(error).__name__,
            },
            output=sys.stderr,
        )
        return 1
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
