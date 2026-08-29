#!/usr/bin/env python3

import argparse
import json
import math
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


CATEGORIES = {"broad", "narrow", "visual", "metadata", "exact-name"}
MODES = {
    "metadataBaseline": {
        "w_metadata": 1,
        "w_visual": 0,
        "w_keyword": 0,
    },
    "fused": {
        "w_metadata": 1,
        "w_visual": 1,
        "w_keyword": 1.25,
    },
}
MAX_RESPONSE_BYTES = 10 * 1024 * 1024


class EvaluationError(Exception):
    pass


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Compare metadata-only and fused artwork search against reviewed "
            "canonical-asset relevance judgments."
        ),
    )
    parser.add_argument(
        "--judgments",
        type=Path,
        default=Path("evaluation/search-relevance.json"),
        help="Human-reviewed relevance JSON.",
    )
    parser.add_argument(
        "--base-url",
        default="http://localhost:8080",
        help="Backend origin, without the artwork search path.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("evaluation/search-evaluation-report.json"),
        help="Destination for the JSON evaluation report.",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=50,
        help="Results requested from each search mode (10-100).",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=30,
        help="Per-request timeout.",
    )
    parser.add_argument(
        "--max-queries",
        type=int,
        help="Optional cap on reviewed queries, in file order.",
    )
    parser.add_argument(
        "--fused-metadata-weight",
        type=float,
        default=1,
    )
    parser.add_argument(
        "--fused-visual-weight",
        type=float,
        default=1,
    )
    parser.add_argument(
        "--fused-keyword-weight",
        type=float,
        default=1.25,
    )
    parser.add_argument(
        "--rrf-k",
        type=int,
        default=60,
    )
    args = parser.parse_args()

    if not 10 <= args.count <= 100:
        parser.error("--count must be between 10 and 100")
    if not 0 < args.timeout_seconds <= 300:
        parser.error("--timeout-seconds must be greater than 0 and at most 300")
    if args.max_queries is not None and args.max_queries < 1:
        parser.error("--max-queries must be at least 1")
    for name in (
        "fused_metadata_weight",
        "fused_visual_weight",
        "fused_keyword_weight",
    ):
        value = getattr(args, name)
        if not 0 <= value <= 10:
            parser.error(f"--{name.replace('_', '-')} must be between 0 and 10")
    if not 1 <= args.rrf_k <= 1000:
        parser.error("--rrf-k must be between 1 and 1000")
    if (
        args.fused_metadata_weight
        + args.fused_visual_weight
        + args.fused_keyword_weight
        == 0
    ):
        parser.error("at least one fused search weight must be greater than 0")
    return args


def load_judgments(path):
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise EvaluationError(f"judgment file not found: {path}") from error
    except json.JSONDecodeError as error:
        raise EvaluationError(
            f"invalid judgment JSON at line {error.lineno}: {error.msg}",
        ) from error

    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise EvaluationError("judgment JSON must be an object with version 1")
    judgments = payload.get("judgments")
    if not isinstance(judgments, list):
        raise EvaluationError("judgment JSON must contain a judgments array")

    reviewed = []
    skipped = []
    seen_queries = set()
    for index, judgment in enumerate(judgments):
        location = f"judgments[{index}]"
        if not isinstance(judgment, dict):
            raise EvaluationError(f"{location} must be an object")

        query = judgment.get("query")
        category = judgment.get("category")
        relevant_ids = judgment.get("relevantCanonicalAssetIds")
        notes = judgment.get("notes")
        is_reviewed = judgment.get("reviewed")

        if not isinstance(query, str) or len(query.strip()) < 2:
            raise EvaluationError(f"{location}.query must contain at least 2 characters")
        query = query.strip()
        if query in seen_queries:
            raise EvaluationError(f"duplicate query in judgment file: {query!r}")
        seen_queries.add(query)
        if category not in CATEGORIES:
            raise EvaluationError(
                f"{location}.category must be one of {sorted(CATEGORIES)}",
            )
        if not isinstance(relevant_ids, list):
            raise EvaluationError(
                f"{location}.relevantCanonicalAssetIds must be an array",
            )
        if any(type(asset_id) is not int or asset_id <= 0 for asset_id in relevant_ids):
            raise EvaluationError(
                f"{location}.relevantCanonicalAssetIds must contain positive integers",
            )
        if len(set(relevant_ids)) != len(relevant_ids):
            raise EvaluationError(
                f"{location}.relevantCanonicalAssetIds contains duplicates",
            )
        if not isinstance(notes, str):
            raise EvaluationError(f"{location}.notes must be a string")
        if type(is_reviewed) is not bool:
            raise EvaluationError(f"{location}.reviewed must be a boolean")

        normalized = {
            "query": query,
            "category": category,
            "relevantCanonicalAssetIds": relevant_ids,
            "notes": notes,
        }
        if not is_reviewed:
            skipped.append({**normalized, "reason": "unreviewed"})
        elif not relevant_ids:
            raise EvaluationError(
                f"{location} is reviewed but has no relevant canonical asset IDs",
            )
        else:
            reviewed.append(normalized)

    return reviewed, skipped


def read_json_response(response):
    body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise EvaluationError("search response exceeded 10 MiB")
    try:
        return json.loads(body)
    except json.JSONDecodeError as error:
        raise EvaluationError("search endpoint returned invalid JSON") from error


def search(endpoint, query, count, timeout_seconds, parameters):
    query_parameters = {
        "q": query,
        "count": count,
        **parameters,
    }
    request = Request(
        f"{endpoint}?{urlencode(query_parameters)}",
        headers={
            "Accept": "application/json",
            "User-Agent": "met-galaxy-search-evaluator/1",
        },
    )
    started_at = time.perf_counter()
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            payload = read_json_response(response)
    except HTTPError as error:
        try:
            payload = read_json_response(error)
            message = payload.get("error") if isinstance(payload, dict) else None
        except EvaluationError:
            message = None
        raise EvaluationError(
            f"HTTP {error.code}: {message or error.reason}",
        ) from error
    except URLError as error:
        raise EvaluationError(f"request failed: {error.reason}") from error
    except TimeoutError as error:
        raise EvaluationError("request timed out") from error
    latency_ms = round((time.perf_counter() - started_at) * 1000, 3)

    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise EvaluationError("search endpoint did not return a successful response")
    results = payload.get("data")
    metadata = payload.get("meta")
    if not isinstance(results, list) or not isinstance(metadata, dict):
        raise EvaluationError("search response is missing data or meta")

    canonical_ids = []
    for index, result in enumerate(results):
        if not isinstance(result, dict):
            raise EvaluationError(f"search result {index} is not an object")
        asset_id = result.get("canonicalAssetId")
        if type(asset_id) is not int or asset_id <= 0:
            raise EvaluationError(
                f"search result {index} has no valid canonicalAssetId",
            )
        canonical_ids.append(asset_id)

    duplicates = sorted(
        asset_id
        for asset_id in set(canonical_ids)
        if canonical_ids.count(asset_id) > 1
    )
    return {
        "canonicalAssetIds": canonical_ids,
        "duplicateCanonicalAssetIds": duplicates,
        "availableModes": metadata.get("availableModes", []),
        "degradedModes": metadata.get("degradedModes", []),
        "serverTiming": metadata.get("timing"),
        "latencyMs": latency_ms,
    }


def calculate_metrics(returned_ids, relevant_ids):
    relevant = set(relevant_ids)
    top_five = returned_ids[:5]
    top_ten = returned_ids[:10]
    precision_at_five = sum(asset_id in relevant for asset_id in top_five) / 5
    recall_at_ten = sum(asset_id in relevant for asset_id in top_ten) / len(relevant)

    reciprocal_rank = 0
    for rank, asset_id in enumerate(returned_ids, start=1):
        if asset_id in relevant:
            reciprocal_rank = 1 / rank
            break

    dcg_at_ten = sum(
        1 / math.log2(rank + 1)
        for rank, asset_id in enumerate(top_ten, start=1)
        if asset_id in relevant
    )
    ideal_count = min(len(relevant), 10)
    ideal_dcg = sum(
        1 / math.log2(rank + 1)
        for rank in range(1, ideal_count + 1)
    )
    return {
        "precisionAt5": round(precision_at_five, 6),
        "recallAt10": round(recall_at_ten, 6),
        "mrr": round(reciprocal_rank, 6),
        "ndcgAt10": round(dcg_at_ten / ideal_dcg, 6),
    }


def percentile(values, proportion):
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * proportion) - 1)
    return ordered[index]


def aggregate_mode(query_results, mode):
    successful = [
        query["modes"][mode]
        for query in query_results
        if "metrics" in query["modes"][mode]
    ]
    failures = [
        {
            "query": query["query"],
            "error": query["modes"][mode]["error"],
        }
        for query in query_results
        if "error" in query["modes"][mode]
    ]
    if not successful:
        return {
            "evaluatedQueries": 0,
            "failedQueries": len(failures),
            "metrics": None,
            "latencyMs": None,
            "degradedQueries": 0,
            "failures": failures,
        }

    latencies = [result["latencyMs"] for result in successful]
    metrics = {
        metric: round(
            statistics.fmean(result["metrics"][metric] for result in successful),
            6,
        )
        for metric in ("precisionAt5", "recallAt10", "mrr", "ndcgAt10")
    }
    return {
        "evaluatedQueries": len(successful),
        "failedQueries": len(failures),
        "metrics": metrics,
        "latencyMs": {
            "mean": round(statistics.fmean(latencies), 3),
            "median": round(statistics.median(latencies), 3),
            "p95": round(percentile(latencies, 0.95), 3),
            "min": round(min(latencies), 3),
            "max": round(max(latencies), 3),
        },
        "degradedQueries": sum(bool(result["degradedModes"]) for result in successful),
        "failures": failures,
    }


def metric_deltas(baseline, fused):
    baseline_metrics = baseline.get("metrics")
    fused_metrics = fused.get("metrics")
    if baseline_metrics is None or fused_metrics is None:
        return None
    return {
        metric: round(fused_metrics[metric] - baseline_metrics[metric], 6)
        for metric in ("precisionAt5", "recallAt10", "mrr", "ndcgAt10")
    }


def write_report(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(f"{path.suffix}.tmp")
    temporary_path.write_text(
        json.dumps(report, indent=2, sort_keys=False, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(path)


def main():
    args = parse_args()
    try:
        reviewed, skipped = load_judgments(args.judgments)
    except EvaluationError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    if args.max_queries is not None:
        reviewed = reviewed[: args.max_queries]

    endpoint = f"{args.base_url.rstrip('/')}/api/artworks/search"
    modes = {
        "metadataBaseline": MODES["metadataBaseline"],
        "fused": {
            "w_metadata": args.fused_metadata_weight,
            "w_visual": args.fused_visual_weight,
            "w_keyword": args.fused_keyword_weight,
            "k_rrf": args.rrf_k,
        },
    }
    query_results = []
    for judgment in reviewed:
        mode_results = {}
        for mode, parameters in modes.items():
            try:
                result = search(
                    endpoint,
                    judgment["query"],
                    args.count,
                    args.timeout_seconds,
                    parameters,
                )
                mode_results[mode] = {
                    **result,
                    "metrics": calculate_metrics(
                        result["canonicalAssetIds"],
                        judgment["relevantCanonicalAssetIds"],
                    ),
                }
            except EvaluationError as error:
                mode_results[mode] = {"error": str(error)}
        query_results.append(
            {
                **judgment,
                "modes": mode_results,
            },
        )

    baseline = aggregate_mode(query_results, "metadataBaseline")
    fused = aggregate_mode(query_results, "fused")
    failed_requests = baseline["failedQueries"] + fused["failedQueries"]
    degraded_fused_requests = fused["degradedQueries"]
    if not reviewed:
        status = "no-reviewed-judgments"
    elif failed_requests:
        status = "completed-with-errors"
    elif degraded_fused_requests:
        status = "completed-with-degradation"
    else:
        status = "completed"

    report = {
        "version": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "configuration": {
            "judgments": str(args.judgments),
            "endpoint": endpoint,
            "count": args.count,
            "timeoutSeconds": args.timeout_seconds,
            "modes": modes,
            "metricDefinitions": {
                "precisionAt5": "relevant canonical assets in ranks 1-5 divided by 5",
                "recallAt10": "relevant canonical assets in ranks 1-10 divided by all judged relevant assets",
                "mrr": "reciprocal rank of the first relevant canonical asset in the requested result set",
                "ndcgAt10": "binary-relevance normalized discounted cumulative gain through rank 10",
                "latencyMs": "client-observed wall-clock HTTP request latency",
            },
        },
        "summary": {
            "reviewedQueries": len(reviewed),
            "skippedQueries": len(skipped),
            "metadataBaseline": baseline,
            "fused": fused,
            "fusedMinusMetadataBaseline": metric_deltas(baseline, fused),
        },
        "queries": query_results,
        "skipped": skipped,
    }
    try:
        write_report(args.output, report)
    except OSError as error:
        print(f"error: could not write report: {error}", file=sys.stderr)
        return 2

    print(
        json.dumps(
            {
                "status": status,
                "reviewedQueries": len(reviewed),
                "skippedQueries": len(skipped),
                "failedRequests": failed_requests,
                "degradedFusedRequests": degraded_fused_requests,
                "report": str(args.output),
            },
            indent=2,
        ),
    )
    return 1 if failed_requests or degraded_fused_requests else 0


if __name__ == "__main__":
    raise SystemExit(main())
