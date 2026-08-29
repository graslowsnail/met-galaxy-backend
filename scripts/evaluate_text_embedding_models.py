#!/usr/bin/env python3

import argparse
import json
import math
import os
import sys
import time
from collections import Counter
from pathlib import Path

import numpy as np
import psycopg2
from dotenv import load_dotenv
from openai import OpenAI
from psycopg2.extras import RealDictCursor

load_dotenv()

ARTWORK_TABLE = '"met-galaxy_artwork"'
IMAGE_ASSET_TABLE = '"met-galaxy_image_asset"'
MODELS = ("text-embedding-3-small", "text-embedding-3-large")
EMBEDDING_DIMENSIONS = 1536
MAX_TEXT_CHARACTERS = 24000
MAX_BATCH_CHARACTERS = 300000
MAX_DOCUMENTS = 5000
MAX_REVIEWED_QUERIES = 250
CATEGORIES = {"broad", "narrow", "visual", "metadata", "exact-name"}
DEFAULT_JUDGMENTS = Path("evaluation/search-relevance.json")


def database_url():
    configured = os.getenv("DATABASE_URL")
    if not configured:
        raise RuntimeError("DATABASE_URL is required")
    return configured


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


def load_judgments(path):
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise RuntimeError("judgment file must be an object with version 1")
    raw_judgments = payload.get("judgments")
    if not isinstance(raw_judgments, list):
        raise RuntimeError("judgment file must contain a judgments array")

    reviewed = []
    skipped = []
    seen_queries = set()
    for index, judgment in enumerate(raw_judgments):
        location = f"judgments[{index}]"
        if not isinstance(judgment, dict):
            raise RuntimeError(f"{location} must be an object")
        query = judgment.get("query")
        category = judgment.get("category")
        relevant_ids = judgment.get("relevantCanonicalAssetIds")
        is_reviewed = judgment.get("reviewed")
        if not isinstance(query, str) or not query.strip():
            raise RuntimeError(f"{location}.query must be a non-empty string")
        query = query.strip()
        normalized_query = query.casefold()
        if normalized_query in seen_queries:
            raise RuntimeError(f"duplicate query in judgment file: {query}")
        seen_queries.add(normalized_query)
        if category not in CATEGORIES:
            raise RuntimeError(
                f"{location}.category must be one of {sorted(CATEGORIES)}"
            )
        if not isinstance(is_reviewed, bool):
            raise RuntimeError(f"{location}.reviewed must be a boolean")
        if not isinstance(relevant_ids, list):
            raise RuntimeError(
                f"{location}.relevantCanonicalAssetIds must be an array"
            )
        if any(
            isinstance(asset_id, bool)
            or not isinstance(asset_id, int)
            or asset_id < 1
            for asset_id in relevant_ids
        ):
            raise RuntimeError(
                f"{location}.relevantCanonicalAssetIds must contain "
                "positive integers"
            )
        unique_ids = list(dict.fromkeys(relevant_ids))
        if not is_reviewed or not unique_ids:
            skipped.append(
                {
                    "query": query,
                    "category": category,
                    "reason": (
                        "unreviewed"
                        if not is_reviewed
                        else "no relevant canonical assets"
                    ),
                }
            )
            continue
        reviewed.append(
            {
                "query": query,
                "category": category,
                "relevantCanonicalAssetIds": unique_ids,
            }
        )

    if len(reviewed) > MAX_REVIEWED_QUERIES:
        raise RuntimeError(
            f"reviewed query count exceeds safety limit "
            f"{MAX_REVIEWED_QUERIES}"
        )
    return reviewed, skipped, len(raw_judgments)


def representative_cte(searchable_only):
    eligibility = ""
    if searchable_only:
        eligibility = """
          AND artwork."txtVec" IS NOT NULL
          AND artwork."imgVec" IS NOT NULL
          AND artwork."localImageUrl" IS NOT NULL
          AND artwork."localImageUrl" <> ''
          AND asset."processingStatus" = 'ready'
        """
    return f"""
        WITH ranked AS (
            SELECT
                artwork.id,
                artwork."imageAssetId" AS image_asset_id,
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
                (
                    artwork."txtVec" IS NOT NULL
                    AND artwork."imgVec" IS NOT NULL
                    AND artwork."localImageUrl" IS NOT NULL
                    AND artwork."localImageUrl" <> ''
                    AND asset."processingStatus" = 'ready'
                ) AS is_searchable,
                ROW_NUMBER() OVER (
                    PARTITION BY artwork."imageAssetId"
                    ORDER BY
                        (
                            artwork."txtVec" IS NOT NULL
                            AND artwork."imgVec" IS NOT NULL
                            AND artwork."localImageUrl" IS NOT NULL
                            AND artwork."localImageUrl" <> ''
                            AND asset."processingStatus" = 'ready'
                        ) DESC,
                        (
                            (artwork.title IS NOT NULL)::int
                            + (artwork.artist IS NOT NULL)::int
                            + (artwork.date IS NOT NULL)::int
                            + (artwork.medium IS NOT NULL)::int
                            + (artwork.department IS NOT NULL)::int
                            + (artwork.culture IS NOT NULL)::int
                            + (artwork.classification IS NOT NULL)::int
                            + (artwork.description IS NOT NULL)::int
                            + (
                                artwork."artistNationality" IS NOT NULL
                            )::int
                        ) DESC,
                        artwork.id
                ) AS asset_rank
            FROM {ARTWORK_TABLE} artwork
            JOIN {IMAGE_ASSET_TABLE} asset
              ON asset.id = artwork."imageAssetId"
            WHERE artwork."imageAssetId" IS NOT NULL
            {eligibility}
        ),
        representatives AS (
            SELECT
                *,
                COALESCE(
                    NULLIF(BTRIM(department), ''),
                    '(unknown)'
                ) AS stratum
            FROM ranked
            WHERE asset_rank = 1
        )
    """


def fetch_documents(connection, judged_asset_ids, limit, seed):
    with connection.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute(
            representative_cte(False)
            + """
            SELECT *
            FROM representatives
            WHERE image_asset_id = ANY(%s::int[])
            ORDER BY image_asset_id
            """,
            (judged_asset_ids,),
        )
        judged_rows = list(cursor.fetchall())

        found_judged_ids = {
            row["image_asset_id"]
            for row in judged_rows
        }
        supplemental_limit = limit - len(judged_rows)
        if supplemental_limit < 0:
            raise RuntimeError(
                f"{len(judged_rows)} judged canonical assets exceed "
                f"--limit {limit}"
            )
        cursor.execute(
            representative_cte(True)
            + """
            , stratified AS (
                SELECT
                    *,
                    ROW_NUMBER() OVER (
                        PARTITION BY stratum
                        ORDER BY
                            MD5(image_asset_id::text || %s),
                            image_asset_id
                    ) AS stratum_rank
                FROM representatives
                WHERE NOT (image_asset_id = ANY(%s::int[]))
            )
            SELECT *
            FROM stratified
            ORDER BY
                stratum_rank,
                MD5(stratum || %s),
                image_asset_id
            LIMIT %s
            """,
            (
                seed,
                list(found_judged_ids),
                seed,
                supplemental_limit,
            ),
        )
        sampled_rows = list(cursor.fetchall())

    documents = []
    for row in judged_rows + sampled_rows:
        text = create_artwork_text(row)
        if text:
            documents.append(
                {
                    "artworkId": row["id"],
                    "canonicalAssetId": row["image_asset_id"],
                    "department": row["stratum"],
                    "isSearchable": row["is_searchable"],
                    "isJudged": row["image_asset_id"] in found_judged_ids,
                    "text": text,
                }
            )
    selected_ids = {
        document["canonicalAssetId"]
        for document in documents
    }
    return documents, selected_ids


def embed_texts(client, model, texts, batch_size, label):
    vectors = np.empty(
        (len(texts), EMBEDDING_DIMENSIONS),
        dtype=np.float32,
    )
    total_tokens = 0
    started_at = time.monotonic()
    start = 0
    while start < len(texts):
        end = start
        batch_characters = 0
        while end < len(texts) and end - start < batch_size:
            next_characters = len(texts[end])
            if (
                end > start
                and batch_characters + next_characters
                > MAX_BATCH_CHARACTERS
            ):
                break
            batch_characters += next_characters
            end += 1
        batch = texts[start:end]
        response = client.embeddings.create(
            model=model,
            input=batch,
            dimensions=EMBEDDING_DIMENSIONS,
            encoding_format="float",
        )
        returned = sorted(response.data, key=lambda item: item.index)
        if len(returned) != len(batch):
            raise RuntimeError(
                f"{model} returned an unexpected embedding count"
            )
        batch_vectors = np.asarray(
            [item.embedding for item in returned],
            dtype=np.float32,
        )
        if batch_vectors.shape != (len(batch), EMBEDDING_DIMENSIONS):
            raise RuntimeError(
                f"{model} returned an unexpected embedding shape "
                f"{batch_vectors.shape}"
            )
        if not np.isfinite(batch_vectors).all():
            raise RuntimeError(f"{model} returned a non-finite embedding")
        vectors[start:start + len(batch)] = batch_vectors
        if response.usage:
            total_tokens += response.usage.total_tokens
        print(
            f"{model} {label}: {start + len(batch)}/{len(texts)}",
            file=sys.stderr,
            flush=True,
        )
        start = end
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    if np.any(norms == 0):
        raise RuntimeError(f"{model} returned a zero-length embedding")
    vectors /= norms
    return vectors, {
        "inputs": len(texts),
        "tokens": total_tokens,
        "durationMs": round((time.monotonic() - started_at) * 1000),
    }


def query_metrics(ranked_ids, relevant_ids):
    relevant = set(relevant_ids)
    top_five = ranked_ids[:5]
    top_ten = ranked_ids[:10]
    precision_at_five = sum(
        asset_id in relevant
        for asset_id in top_five
    ) / 5
    recall_at_ten = sum(
        asset_id in relevant
        for asset_id in top_ten
    ) / len(relevant)
    reciprocal_rank = 0.0
    for rank, asset_id in enumerate(ranked_ids, start=1):
        if asset_id in relevant:
            reciprocal_rank = 1 / rank
            break
    dcg = sum(
        1 / math.log2(rank + 1)
        for rank, asset_id in enumerate(top_ten, start=1)
        if asset_id in relevant
    )
    ideal_hits = min(len(relevant), 10)
    ideal_dcg = sum(
        1 / math.log2(rank + 1)
        for rank in range(1, ideal_hits + 1)
    )
    return {
        "precisionAt5": precision_at_five,
        "recallAt10": recall_at_ten,
        "mrr": reciprocal_rank,
        "ndcgAt10": dcg / ideal_dcg if ideal_dcg else 0,
    }


def mean_metrics(results):
    metric_names = ("precisionAt5", "recallAt10", "mrr", "ndcgAt10")
    return {
        metric: round(
            sum(result[metric] for result in results) / len(results),
            6,
        )
        for metric in metric_names
    }


def evaluate_model(
    client,
    model,
    documents,
    judgments,
    batch_size,
):
    started_at = time.monotonic()
    document_vectors, document_usage = embed_texts(
        client,
        model,
        [document["text"] for document in documents],
        batch_size,
        "documents",
    )
    query_vectors, query_usage = embed_texts(
        client,
        model,
        [judgment["query"] for judgment in judgments],
        batch_size,
        "queries",
    )
    canonical_ids = np.asarray(
        [document["canonicalAssetId"] for document in documents],
        dtype=np.int64,
    )
    scores = query_vectors @ document_vectors.T

    query_results = []
    for index, judgment in enumerate(judgments):
        order = np.lexsort((canonical_ids, -scores[index]))
        ranked_ids = canonical_ids[order].tolist()
        metrics = query_metrics(
            ranked_ids,
            judgment["relevantCanonicalAssetIds"],
        )
        query_results.append(
            {
                "query": judgment["query"],
                "category": judgment["category"],
                "relevantCanonicalAssetIds": (
                    judgment["relevantCanonicalAssetIds"]
                ),
                "top10CanonicalAssetIds": ranked_ids[:10],
                **{
                    key: round(value, 6)
                    for key, value in metrics.items()
                },
            }
        )

    categories = {}
    for category in sorted(CATEGORIES):
        category_results = [
            result
            for result in query_results
            if result["category"] == category
        ]
        if category_results:
            categories[category] = {
                "queries": len(category_results),
                **mean_metrics(category_results),
            }
    return {
        "dimensions": EMBEDDING_DIMENSIONS,
        "metrics": mean_metrics(query_results),
        "categoryMetrics": categories,
        "usage": {
            "documents": document_usage,
            "queries": query_usage,
            "tokens": (
                document_usage["tokens"]
                + query_usage["tokens"]
            ),
            "durationMs": round(
                (time.monotonic() - started_at) * 1000
            ),
        },
        "queryResults": query_results,
    }


def metric_delta(model_results):
    small = model_results[MODELS[0]]["metrics"]
    large = model_results[MODELS[1]]["metrics"]
    return {
        metric: round(large[metric] - small[metric], 6)
        for metric in small
    }


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Read-only comparison of OpenAI metadata embedding models "
            "against reviewed canonical search judgments"
        )
    )
    parser.add_argument(
        "--judgments",
        type=Path,
        default=DEFAULT_JUDGMENTS,
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=1000,
        help=(
            "maximum total candidate documents, including every judged "
            "canonical asset (default: 1000, maximum: 5000)"
        ),
    )
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument(
        "--seed",
        default="priority6-text-model-evaluation-v1",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="make paid OpenAI embedding calls",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate and sample without OpenAI calls (the default)",
    )
    return parser.parse_args()


def validate_args(args):
    if args.execute and args.dry_run:
        raise RuntimeError("--execute and --dry-run cannot be combined")
    if args.limit < 10 or args.limit > MAX_DOCUMENTS:
        raise RuntimeError(
            f"--limit must be between 10 and {MAX_DOCUMENTS}"
        )
    if args.batch_size < 1 or args.batch_size > 100:
        raise RuntimeError("--batch-size must be between 1 and 100")
    if args.timeout <= 0:
        raise RuntimeError("--timeout must be positive")
    if not args.seed.strip():
        raise RuntimeError("--seed must not be empty")


def main():
    args = parse_args()
    validate_args(args)
    judgments, skipped, source_query_count = load_judgments(args.judgments)
    judged_asset_ids = sorted(
        {
            asset_id
            for judgment in judgments
            for asset_id in judgment["relevantCanonicalAssetIds"]
        }
    )

    connection = psycopg2.connect(database_url())
    try:
        connection.set_session(readonly=True)
        documents, selected_ids = fetch_documents(
            connection,
            judged_asset_ids,
            args.limit,
            args.seed,
        )
    finally:
        connection.close()

    missing_judged_ids = sorted(set(judged_asset_ids) - selected_ids)
    unsearchable_judged_ids = sorted(
        document["canonicalAssetId"]
        for document in documents
        if document["isJudged"] and not document["isSearchable"]
    )
    strata = Counter(
        document["department"]
        for document in documents
        if not document["isJudged"]
    )
    document_characters = sum(
        len(document["text"])
        for document in documents
    )
    query_characters = sum(
        len(judgment["query"])
        for judgment in judgments
    )
    per_model_estimated_tokens = math.ceil(
        (document_characters + query_characters) / 4
    )
    report = {
        "dryRun": not args.execute,
        "judgmentFile": str(args.judgments),
        "judgments": {
            "sourceQueries": source_query_count,
            "reviewedQueries": len(judgments),
            "skippedQueries": len(skipped),
            "skipped": skipped,
            "uniqueJudgedCanonicalAssets": len(judged_asset_ids),
            "missingCanonicalAssetIds": missing_judged_ids,
            "unsearchableCanonicalAssetIds": unsearchable_judged_ids,
        },
        "candidateSet": {
            "limit": args.limit,
            "documents": len(documents),
            "judgedDocuments": sum(
                document["isJudged"]
                for document in documents
            ),
            "sampledDocuments": sum(
                not document["isJudged"]
                for document in documents
            ),
            "seed": args.seed,
            "sampledDepartmentCounts": dict(sorted(strata.items())),
        },
        "projectedInput": {
            "charactersPerModel": (
                document_characters + query_characters
            ),
            "approximateTokensPerModel": per_model_estimated_tokens,
            "approximateTokensBothModels": (
                per_model_estimated_tokens * len(MODELS)
            ),
        },
        "limitations": [
            (
                "Metrics rank only the deterministic bounded candidate "
                "sample, not the full production corpus."
            ),
            (
                "Only listed canonical assets are treated as relevant; "
                "unjudged results are counted as non-relevant."
            ),
            (
                "The comparison isolates metadata embeddings and does not "
                "measure keyword, OpenCLIP, or reciprocal-rank fusion."
            ),
        ],
    }

    if args.execute:
        if not judgments:
            raise RuntimeError(
                "live evaluation requires at least one reviewed judgment "
                "with a relevant canonical asset"
            )
        if missing_judged_ids:
            raise RuntimeError(
                "live evaluation requires every judged canonical asset; "
                f"missing IDs: {missing_judged_ids}"
            )
        if unsearchable_judged_ids:
            raise RuntimeError(
                "live evaluation requires every judged canonical asset "
                "to be production-searchable; unsearchable IDs: "
                f"{unsearchable_judged_ids}"
            )
        if not os.getenv("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY is required with --execute")
        client = OpenAI(
            api_key=os.getenv("OPENAI_API_KEY"),
            max_retries=2,
            timeout=args.timeout,
        )
        model_results = {}
        for model in MODELS:
            model_results[model] = evaluate_model(
                client,
                model,
                documents,
                judgments,
                args.batch_size,
            )
        report["models"] = model_results
        report["largeMinusSmall"] = metric_delta(model_results)

    output = json.dumps(report, indent=2)
    if args.output:
        if not args.output.parent.exists():
            raise RuntimeError(
                f"output directory does not exist: {args.output.parent}"
            )
        args.output.write_text(output + "\n", encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
