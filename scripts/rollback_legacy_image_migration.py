#!/usr/bin/env python3

import argparse
import json
import os
from pathlib import Path

import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import execute_values

load_dotenv()

ARTWORK_TABLE = '"met-galaxy_artwork"'
INGESTION_TABLE = '"met-galaxy_image_ingestion"'


def parse_args():
    parser = argparse.ArgumentParser(
        description="Validate or apply the Priority 3 artwork-link rollback"
    )
    parser.add_argument(
        "--manifest",
        default="migration-reports/priority3/rollback-manifest.jsonl",
    )
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def load_manifest(path):
    records = []
    with path.open() as source:
        for line in source:
            record = json.loads(line)
            if (
                record["image_asset_id"] is not None
                and record["legacy_s3_key"].startswith("artworks/")
            ):
                records.append(record)
    return records


def main():
    args = parse_args()
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required")
    manifest_path = Path(args.manifest)
    records = load_manifest(manifest_path)

    with psycopg2.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT COUNT(*)
                FROM {ARTWORK_TABLE}
                WHERE id = ANY(%s)
                """,
                ([record["artwork_id"] for record in records],),
            )
            existing = cursor.fetchone()[0]
            cursor.execute(
                f"""
                SELECT COUNT(*)
                FROM {ARTWORK_TABLE} artwork
                JOIN (
                    SELECT *
                    FROM unnest(%s::integer[], %s::integer[])
                        AS expected(artwork_id, image_asset_id)
                ) expected
                  ON expected.artwork_id = artwork.id
                WHERE artwork."imageAssetId"
                    IS DISTINCT FROM expected.image_asset_id
                """,
                (
                    [record["artwork_id"] for record in records],
                    [record["image_asset_id"] for record in records],
                ),
            )
            mismatched = cursor.fetchone()[0]

        report = {
            "manifest": str(manifest_path),
            "records": len(records),
            "existingArtworks": existing,
            "mismatchedCurrentLinks": mismatched,
            "applied": False,
        }
        if not args.apply:
            print(json.dumps(report, indent=2))
            return
        if existing != len(records) or mismatched:
            raise RuntimeError(
                "rollback validation failed; regenerate or inspect the manifest"
            )

        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TEMP TABLE image_migration_rollback (
                    artwork_id integer PRIMARY KEY,
                    image_asset_id integer NOT NULL,
                    legacy_url text NOT NULL
                ) ON COMMIT DROP
                """
            )
            execute_values(
                cursor,
                """
                INSERT INTO image_migration_rollback (
                    artwork_id,
                    image_asset_id,
                    legacy_url
                )
                VALUES %s
                """,
                [
                    (
                        record["artwork_id"],
                        record["image_asset_id"],
                        record["legacy_url"],
                    )
                    for record in records
                ],
            )
            cursor.execute(
                f"""
                UPDATE {ARTWORK_TABLE} artwork
                SET
                    "localImageUrl" = rollback.legacy_url,
                    "imageAssetId" = NULL,
                    "imageDuplicateState" = NULL
                FROM image_migration_rollback rollback
                WHERE artwork.id = rollback.artwork_id
                  AND artwork."imageAssetId" = rollback.image_asset_id
                """
            )
            updated_artworks = cursor.rowcount
            cursor.execute(
                f"""
                UPDATE {INGESTION_TABLE} ingestion
                SET
                    status = 'pending',
                    "nextAttemptAt" = NULL,
                    "leaseOwner" = NULL,
                    "leaseExpiresAt" = NULL,
                    "lastError" = NULL,
                    "completedAt" = NULL,
                    "updatedAt" = CURRENT_TIMESTAMP
                FROM image_migration_rollback rollback
                WHERE ingestion."artworkId" = rollback.artwork_id
                """
            )
            reset_ingestions = cursor.rowcount

        report.update(
            {
                "applied": True,
                "updatedArtworks": updated_artworks,
                "resetIngestions": reset_ingestions,
            }
        )
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
