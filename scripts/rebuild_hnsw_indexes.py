#!/usr/bin/env python3

import argparse
import fcntl
import json
import os
import sys
from pathlib import Path

import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import RealDictCursor

TEXT_INDEX = "idx_artworks_txtvec_eligible"
TEXT_REBUILD_INDEX = "idx_artworks_txtvec_eligible_rebuild"
IMAGE_INDEX = "idx_image_assets_embedding_hnsw"
LOCK_PATH = Path("/tmp/met-galaxy-hnsw-rebuild.lock")


def database_url():
    load_dotenv()
    configured = os.getenv("DATABASE_URL")
    if not configured:
        raise RuntimeError("DATABASE_URL is required")
    return configured


def connect():
    connection = psycopg2.connect(
        database_url(), application_name="met-galaxy-hnsw-rebuild"
    )
    connection.autocommit = True
    return connection


def fetch_one(connection, query, parameters=()):
    with connection.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute(query, parameters)
        row = cursor.fetchone()
    return dict(row) if row else None


def fetch_all(connection, query, parameters=()):
    with connection.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute(query, parameters)
        return [dict(row) for row in cursor.fetchall()]


def execute(connection, statement, parameters=()):
    with connection.cursor() as cursor:
        cursor.execute(statement, parameters)


def embedding_stats(connection):
    return fetch_one(
        connection,
        """
        SELECT
          COUNT(*) FILTER (
            WHERE "imgVec" IS NOT NULL
              AND "imageAssetId" IS NOT NULL
          ) AS image_searchable,
          COUNT(*) FILTER (
            WHERE "imgVec" IS NOT NULL
              AND "imageAssetId" IS NOT NULL
              AND "txtVec" IS NOT NULL
          ) AS fully_searchable,
          COUNT(*) FILTER (
            WHERE "imgVec" IS NOT NULL
              AND "imageAssetId" IS NOT NULL
              AND "txtVec" IS NULL
              AND "txtVecAttemptCount" < 5
          ) AS pending,
          COUNT(*) FILTER (
            WHERE "imgVec" IS NOT NULL
              AND "imageAssetId" IS NOT NULL
              AND "txtVec" IS NULL
              AND "txtVecAttemptCount" >= 5
          ) AS terminal_failures,
          COUNT(*) FILTER (
            WHERE "txtVecLeaseExpiresAt" > CURRENT_TIMESTAMP
          ) AS leased
        FROM "met-galaxy_artwork"
        """,
    )


def index_details(connection):
    return fetch_all(
        connection,
        """
        SELECT
          indexes.tablename,
          indexes.indexname,
          indexes.indexdef,
          pg_size_pretty(pg_relation_size(indexes.indexname::regclass)) AS size,
          pg_relation_size(indexes.indexname::regclass) AS size_bytes,
          catalog.indisready AS ready,
          catalog.indisvalid AS valid,
          COALESCE(stats.idx_scan, 0) AS scans
        FROM pg_indexes indexes
        JOIN pg_class index_class
          ON index_class.relname = indexes.indexname
        JOIN pg_namespace namespace
          ON namespace.oid = index_class.relnamespace
         AND namespace.nspname = indexes.schemaname
        JOIN pg_index catalog
          ON catalog.indexrelid = index_class.oid
        LEFT JOIN pg_stat_user_indexes stats
          ON stats.indexrelid = index_class.oid
        WHERE indexes.schemaname = current_schema()
          AND indexes.indexname IN (%s, %s, %s)
        ORDER BY indexes.indexname
        """,
        (IMAGE_INDEX, TEXT_INDEX, TEXT_REBUILD_INDEX),
    )


def build_settings(connection):
    return fetch_one(
        connection,
        """
        SELECT
          current_setting('server_version') AS postgres_version,
          (SELECT extversion FROM pg_extension WHERE extname = 'vector')
            AS pgvector_version,
          current_setting('maintenance_work_mem') AS maintenance_work_mem,
          current_setting('max_parallel_maintenance_workers')
            AS max_parallel_maintenance_workers,
          current_setting('statement_timeout') AS statement_timeout
        """,
    )


def active_index_builds(connection):
    return fetch_all(
        connection,
        """
        SELECT
          pid,
          command,
          COALESCE(relid::regclass::text, '') AS table_name,
          COALESCE(index_relid::regclass::text, '') AS index_name,
          phase,
          blocks_done,
          blocks_total,
          tuples_done,
          tuples_total
        FROM pg_stat_progress_create_index
        ORDER BY pid
        """,
    )


def preflight(connection):
    stats = embedding_stats(connection)
    builds = active_index_builds(connection)
    indexes = index_details(connection)
    errors = []

    if stats["image_searchable"] != stats["fully_searchable"]:
        errors.append("not every image-searchable artwork has a text embedding")
    if stats["pending"]:
        errors.append("text embeddings are still pending")
    if stats["terminal_failures"]:
        errors.append("terminal text-embedding failures remain")
    if stats["leased"]:
        errors.append("text-embedding leases are still active")
    if builds:
        errors.append("another index build is already active")

    image = next(
        (index for index in indexes if index["indexname"] == IMAGE_INDEX), None
    )
    if not image or not image["ready"] or not image["valid"]:
        errors.append("the canonical image HNSW index is missing or invalid")

    return {
        "ready": not errors,
        "errors": errors,
        "embeddings": stats,
        "settings": build_settings(connection),
        "active_index_builds": builds,
        "indexes": indexes,
    }


def text_index_definition_matches(index):
    if not index or not index["ready"] or not index["valid"]:
        return False
    definition = index["indexdef"].lower()
    required = (
        "using hnsw",
        '"txtvec" vector_cosine_ops',
        '"txtvec" is not null',
        '"imgvec" is not null',
        '"imageassetid" is not null',
        '"localimageurl" is not null',
        "m='16'",
        "ef_construction='64'",
    )
    return all(fragment in definition for fragment in required)


def configure_build(connection, maintenance_work_mem, parallel_workers):
    if maintenance_work_mem:
        fetch_one(
            connection,
            "SELECT set_config('maintenance_work_mem', %s, false) AS value",
            (maintenance_work_mem,),
        )
    if parallel_workers is not None:
        fetch_one(
            connection,
            """
            SELECT set_config(
              'max_parallel_maintenance_workers', %s, false
            ) AS value
            """,
            (str(parallel_workers),),
        )


def rebuild_text_index(connection):
    indexes = index_details(connection)
    replacement = next(
        (
            index
            for index in indexes
            if index["indexname"] == TEXT_REBUILD_INDEX
        ),
        None,
    )

    if replacement and not text_index_definition_matches(replacement):
        print(f"Dropping unusable replacement index {TEXT_REBUILD_INDEX}.", flush=True)
        execute(
            connection,
            f'DROP INDEX CONCURRENTLY IF EXISTS "{TEXT_REBUILD_INDEX}"',
        )
        replacement = None

    if not replacement:
        print(f"Building replacement text index {TEXT_REBUILD_INDEX}.", flush=True)
        execute(
            connection,
            f"""
            CREATE INDEX CONCURRENTLY "{TEXT_REBUILD_INDEX}"
            ON "met-galaxy_artwork"
            USING hnsw ("txtVec" vector_cosine_ops)
            WITH (m = 16, ef_construction = 64)
            WHERE "txtVec" IS NOT NULL
              AND "imgVec" IS NOT NULL
              AND "imageAssetId" IS NOT NULL
              AND "localImageUrl" IS NOT NULL
              AND "localImageUrl" <> ''
            """,
        )

    replacement = next(
        (
            index
            for index in index_details(connection)
            if index["indexname"] == TEXT_REBUILD_INDEX
        ),
        None,
    )
    if not text_index_definition_matches(replacement):
        raise RuntimeError("replacement text index is not ready and valid")

    print("Swapping in the replacement text index.", flush=True)
    execute(connection, f'DROP INDEX CONCURRENTLY IF EXISTS "{TEXT_INDEX}"')
    execute(
        connection,
        f'ALTER INDEX "{TEXT_REBUILD_INDEX}" RENAME TO "{TEXT_INDEX}"',
    )
    execute(connection, 'ANALYZE "met-galaxy_artwork"')


def rebuild_image_index(connection):
    image = next(
        (
            index
            for index in index_details(connection)
            if index["indexname"] == IMAGE_INDEX
        ),
        None,
    )
    if not image or not image["ready"] or not image["valid"]:
        raise RuntimeError("canonical image HNSW index is missing or invalid")

    print(f"Rebuilding canonical image index {IMAGE_INDEX}.", flush=True)
    execute(connection, f'REINDEX INDEX CONCURRENTLY "{IMAGE_INDEX}"')
    execute(connection, 'ANALYZE "met-galaxy_image_asset"')


def plan_indexes(connection, table, vector_column, predicate):
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT "{vector_column}"::text
            FROM "{table}"
            WHERE {predicate}
            LIMIT 1
            """
        )
        row = cursor.fetchone()
        if not row:
            raise RuntimeError(f"no eligible vectors exist in {table}")

        cursor.execute("BEGIN")
        try:
            cursor.execute("SET LOCAL enable_seqscan = off")
            cursor.execute("SET LOCAL enable_sort = off")
            cursor.execute(
                f"""
                EXPLAIN (FORMAT JSON)
                SELECT id
                FROM "{table}"
                WHERE {predicate}
                ORDER BY "{vector_column}" <=> %s::vector
                LIMIT 20
                """,
                (row[0],),
            )
            plan = cursor.fetchone()[0]
        finally:
            cursor.execute("ROLLBACK")

    names = set()

    def visit(value):
        if isinstance(value, dict):
            if value.get("Index Name"):
                names.add(value["Index Name"])
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(plan)
    return sorted(names)


def verify(connection):
    indexes = index_details(connection)
    text = next(
        (index for index in indexes if index["indexname"] == TEXT_INDEX), None
    )
    image = next(
        (index for index in indexes if index["indexname"] == IMAGE_INDEX), None
    )
    text_plan_indexes = plan_indexes(
        connection,
        "met-galaxy_artwork",
        "txtVec",
        '"txtVec" IS NOT NULL '
        'AND "imgVec" IS NOT NULL '
        'AND "imageAssetId" IS NOT NULL '
        'AND "localImageUrl" IS NOT NULL '
        'AND "localImageUrl" <> \'\'',
    )
    image_plan_indexes = plan_indexes(
        connection,
        "met-galaxy_image_asset",
        "imageEmbedding",
        '"imageEmbedding" IS NOT NULL AND "processingStatus" = \'ready\'',
    )
    errors = []

    if not text_index_definition_matches(text):
        errors.append("text HNSW definition or validity does not match")
    if not image or not image["ready"] or not image["valid"]:
        errors.append("canonical image HNSW index is missing or invalid")
    if TEXT_INDEX not in text_plan_indexes:
        errors.append("the representative text query did not use the text HNSW index")
    if IMAGE_INDEX not in image_plan_indexes:
        errors.append("the representative image query did not use the image HNSW index")

    return {
        "verified": not errors,
        "errors": errors,
        "embeddings": embedding_stats(connection),
        "indexes": indexes,
        "text_plan_indexes": text_plan_indexes,
        "image_plan_indexes": image_plan_indexes,
    }


def vacuum(connection):
    print("Vacuuming and analyzing artwork vectors.", flush=True)
    execute(connection, 'VACUUM (ANALYZE) "met-galaxy_artwork"')
    print("Vacuuming and analyzing canonical image vectors.", flush=True)
    execute(connection, 'VACUUM (ANALYZE) "met-galaxy_image_asset"')


def rebuild(connection, maintenance_work_mem, parallel_workers):
    report = preflight(connection)
    if not report["ready"]:
        raise RuntimeError("preflight failed: " + "; ".join(report["errors"]))

    configure_build(connection, maintenance_work_mem, parallel_workers)
    print(json.dumps({"preflight": report}, indent=2, default=str), flush=True)
    rebuild_text_index(connection)
    rebuild_image_index(connection)
    vacuum(connection)
    result = verify(connection)
    print(json.dumps(result, indent=2, default=str), flush=True)
    if not result["verified"]:
        raise RuntimeError("post-rebuild verification failed")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Safely rebuild and verify the Met Galaxy HNSW indexes."
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=("preflight", "status", "verify", "rebuild"),
        default="preflight",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Required to run the mutating rebuild command.",
    )
    parser.add_argument(
        "--maintenance-work-mem",
        help="Optional session maintenance_work_mem, such as 2GB.",
    )
    parser.add_argument(
        "--parallel-workers",
        type=int,
        help="Optional session max_parallel_maintenance_workers.",
    )
    args = parser.parse_args()
    if args.parallel_workers is not None and not 0 <= args.parallel_workers <= 32:
        parser.error("--parallel-workers must be between 0 and 32")
    if args.command == "rebuild" and not args.execute:
        parser.error("rebuild requires --execute")
    return args


def main():
    args = parse_args()
    connection = connect()
    try:
        if args.command == "status":
            print(
                json.dumps(
                    {
                        "embeddings": embedding_stats(connection),
                        "active_index_builds": active_index_builds(connection),
                        "indexes": index_details(connection),
                    },
                    indent=2,
                    default=str,
                )
            )
        elif args.command == "verify":
            result = verify(connection)
            print(json.dumps(result, indent=2, default=str))
            if not result["verified"]:
                sys.exit(1)
        elif args.command == "rebuild":
            with LOCK_PATH.open("w") as lock_file:
                try:
                    fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as error:
                    raise RuntimeError(
                        "another local HNSW rebuild is already running"
                    ) from error
                rebuild(
                    connection,
                    args.maintenance_work_mem,
                    args.parallel_workers,
                )
        else:
            report = preflight(connection)
            print(json.dumps(report, indent=2, default=str))
            if not report["ready"]:
                sys.exit(1)
    finally:
        connection.close()


if __name__ == "__main__":
    main()
