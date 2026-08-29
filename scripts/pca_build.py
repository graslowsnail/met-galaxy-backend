#!/usr/bin/env python3

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import psycopg2
from dotenv import load_dotenv
from sklearn.decomposition import IncrementalPCA

COMPONENT_COUNT = 4
EMBEDDING_DIMENSION = 768
DEFAULT_BATCH_SIZE = 8192
DEFAULT_OUTPUT_PATH = Path("pca_basis.json")


def database_url():
    load_dotenv()
    configured = os.getenv("DATABASE_URL")
    if not configured:
        raise RuntimeError("DATABASE_URL is required")
    return configured


def connect():
    connection = psycopg2.connect(
        database_url(), application_name="met-galaxy-pca-build"
    )
    connection.set_session(readonly=True, autocommit=True)
    return connection


def canonical_stats(cursor):
    cursor.execute(
        """
        SELECT
          COUNT(*) AS sample_count,
          MIN(asset.id) AS min_asset_id,
          MAX(asset.id) AS max_asset_id
        FROM "met-galaxy_image_asset" asset
        WHERE asset."imageEmbedding" IS NOT NULL
          AND asset."processingStatus" = 'ready'
          AND NOT EXISTS (
            SELECT 1
            FROM "met-galaxy_image_asset_canonical" mapping
            WHERE mapping."assetId" = asset.id
          )
        """
    )
    sample_count, min_asset_id, max_asset_id = cursor.fetchone()
    return {
        "sample_count": sample_count,
        "min_asset_id": min_asset_id,
        "max_asset_id": max_asset_id,
    }


def parse_vector(value):
    if isinstance(value, str):
        vector = np.fromstring(value.strip("[]"), sep=",", dtype=np.float32)
    else:
        vector = np.asarray(value, dtype=np.float32)
    if vector.shape != (EMBEDDING_DIMENSION,):
        raise RuntimeError(
            f"expected a {EMBEDDING_DIMENSION}-dimensional embedding, "
            f"received shape {vector.shape}"
        )
    if not np.isfinite(vector).all():
        raise RuntimeError("encountered a non-finite image embedding")
    return vector


def fetch_batch(cursor, after_id, limit):
    cursor.execute(
        """
        SELECT asset.id, asset."imageEmbedding"
        FROM "met-galaxy_image_asset" asset
        WHERE asset.id > %s
          AND asset."imageEmbedding" IS NOT NULL
          AND asset."processingStatus" = 'ready'
          AND NOT EXISTS (
            SELECT 1
            FROM "met-galaxy_image_asset_canonical" mapping
            WHERE mapping."assetId" = asset.id
          )
        ORDER BY asset.id
        LIMIT %s
        """,
        (after_id, limit),
    )
    rows = cursor.fetchall()
    if not rows:
        return None, after_id
    return np.stack([parse_vector(row[1]) for row in rows]), rows[-1][0]


def normalize_embeddings(batch):
    norms = np.linalg.norm(batch, axis=1, keepdims=True)
    if np.any(norms <= 1e-12):
        raise RuntimeError("encountered a zero-length image embedding")
    return batch / norms


def stabilize_component_signs(components):
    stabilized = components.copy()
    for index, component in enumerate(stabilized):
        pivot = int(np.argmax(np.abs(component)))
        if component[pivot] < 0:
            stabilized[index] *= -1
    return stabilized


def build_basis(cursor, sample_count, batch_size):
    if sample_count < COMPONENT_COUNT:
        raise RuntimeError(
            f"at least {COMPONENT_COUNT} canonical embeddings are required"
        )

    pca = IncrementalPCA(
        n_components=COMPONENT_COUNT,
        batch_size=batch_size,
    )
    after_id = 0
    processed = 0

    while processed < sample_count:
        remaining = sample_count - processed
        limit = (
            remaining
            if remaining <= batch_size + COMPONENT_COUNT - 1
            else batch_size
        )
        batch, after_id = fetch_batch(cursor, after_id, limit)
        if batch is None:
            break
        pca.partial_fit(normalize_embeddings(batch))
        processed += len(batch)
        print(
            json.dumps(
                {
                    "phase": "fit",
                    "processed": processed,
                    "total": sample_count,
                    "percent": round(100 * processed / sample_count, 2),
                    "lastAssetId": after_id,
                }
            ),
            flush=True,
        )

    if processed != sample_count:
        raise RuntimeError(
            f"canonical dataset changed during PCA build: expected "
            f"{sample_count}, processed {processed}"
        )

    components = stabilize_component_signs(
        pca.components_.astype(np.float32)
    )
    components /= np.linalg.norm(components, axis=1, keepdims=True)
    return components, pca.explained_variance_ratio_, processed


def basis_payload(components, explained_variance_ratio, stats, batch_size):
    return {
        "basis": components.tolist(),
        "explained_variance_ratio": explained_variance_ratio.tolist(),
        "n_samples": stats["sample_count"],
        "n_components": COMPONENT_COUNT,
        "embedding_dim": EMBEDDING_DIMENSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": {
            "table": "met-galaxy_image_asset",
            "embedding_column": "imageEmbedding",
            "canonical_only": True,
            "processing_status": "ready",
            "min_asset_id": stats["min_asset_id"],
            "max_asset_id": stats["max_asset_id"],
            "batch_size": batch_size,
        },
    }


def verify_payload(payload, expected_sample_count=None):
    errors = []
    components = np.asarray(payload.get("basis"), dtype=np.float64)
    ratios = np.asarray(
        payload.get("explained_variance_ratio"), dtype=np.float64
    )

    if components.shape != (COMPONENT_COUNT, EMBEDDING_DIMENSION):
        errors.append(
            f"basis shape is {components.shape}, expected "
            f"({COMPONENT_COUNT}, {EMBEDDING_DIMENSION})"
        )
    elif not np.isfinite(components).all():
        errors.append("basis contains non-finite values")
    else:
        norms = np.linalg.norm(components, axis=1)
        if not np.allclose(norms, 1.0, atol=1e-5):
            errors.append("basis components are not unit length")
        gram = components @ components.T
        if not np.allclose(gram, np.eye(COMPONENT_COUNT), atol=1e-4):
            errors.append("basis components are not orthogonal")

    if ratios.shape != (COMPONENT_COUNT,):
        errors.append("explained variance ratio count does not match")
    elif not np.isfinite(ratios).all() or np.any(ratios < 0):
        errors.append("explained variance ratios are invalid")

    if payload.get("n_components") != COMPONENT_COUNT:
        errors.append("n_components does not match")
    if payload.get("embedding_dim") != EMBEDDING_DIMENSION:
        errors.append("embedding_dim does not match")
    if (
        expected_sample_count is not None
        and payload.get("n_samples") != expected_sample_count
    ):
        errors.append(
            f"n_samples is {payload.get('n_samples')}, expected "
            f"{expected_sample_count}"
        )
    if payload.get("source", {}).get("canonical_only") is not True:
        errors.append("source is not marked canonical-only")

    return {
        "verified": not errors,
        "errors": errors,
        "n_samples": payload.get("n_samples"),
        "n_components": payload.get("n_components"),
        "embedding_dim": payload.get("embedding_dim"),
        "explained_variance_ratio": payload.get("explained_variance_ratio"),
        "explained_variance_total": (
            float(ratios.sum()) if ratios.shape == (COMPONENT_COUNT,) else None
        ),
    }


def write_atomic(output_path, payload):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_name(f".{output_path.name}.tmp")
    with temporary_path.open("w", encoding="utf-8") as output:
        json.dump(payload, output, indent=2)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary_path, output_path)


def build(output_path, batch_size):
    connection = connect()
    try:
        with connection.cursor() as cursor:
            stats = canonical_stats(cursor)
            print(json.dumps({"phase": "preflight", **stats}), flush=True)
            components, ratios, processed = build_basis(
                cursor,
                stats["sample_count"],
                batch_size,
            )
            if processed != stats["sample_count"]:
                raise RuntimeError("PCA sample count does not match preflight")
            final_stats = canonical_stats(cursor)
            if final_stats != stats:
                raise RuntimeError("canonical dataset changed during PCA build")
    finally:
        connection.close()

    payload = basis_payload(components, ratios, stats, batch_size)
    verification = verify_payload(payload, stats["sample_count"])
    if not verification["verified"]:
        raise RuntimeError(
            "PCA verification failed: " + "; ".join(verification["errors"])
        )
    write_atomic(output_path, payload)
    print(
        json.dumps(
            {
                "phase": "complete",
                "output": str(output_path),
                **verification,
            },
            indent=2,
        ),
        flush=True,
    )


def verify_file(output_path, compare_database):
    with output_path.open("r", encoding="utf-8") as source:
        payload = json.load(source)
    expected_sample_count = None
    if compare_database:
        connection = connect()
        try:
            with connection.cursor() as cursor:
                expected_sample_count = canonical_stats(cursor)["sample_count"]
        finally:
            connection.close()
    result = verify_payload(payload, expected_sample_count)
    print(json.dumps(result, indent=2), flush=True)
    if not result["verified"]:
        raise RuntimeError("PCA verification failed")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Build a canonical-only PCA basis for the field graph."
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=("build", "verify"),
        default="build",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_PATH,
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
    )
    parser.add_argument(
        "--compare-database",
        action="store_true",
        help="Compare the saved sample count with the live canonical dataset.",
    )
    args = parser.parse_args()
    if args.batch_size < COMPONENT_COUNT:
        parser.error(f"--batch-size must be at least {COMPONENT_COUNT}")
    return args


def main():
    args = parse_args()
    if args.command == "verify":
        verify_file(args.output, args.compare_database)
    else:
        build(args.output, args.batch_size)


if __name__ == "__main__":
    main()
