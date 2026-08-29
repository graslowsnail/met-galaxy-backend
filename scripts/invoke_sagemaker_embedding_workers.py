#!/usr/bin/env python3

import argparse
import concurrent.futures
import json
import os
import threading
import time

import boto3
from botocore.config import Config


def parse_args():
    parser = argparse.ArgumentParser(
        description="Drive parallel SageMaker image-embedding workers"
    )
    parser.add_argument(
        "--endpoint",
        default="met-galaxy-image-embedding-gpu",
    )
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--refill-size", type=int, default=5000)
    parser.add_argument("--idle-rounds", type=int, default=2)
    parser.add_argument("--max-consecutive-errors", type=int, default=12)
    return parser.parse_args()


def invoke_worker(client, args, worker_number, totals, lock):
    empty_rounds = 0
    consecutive_errors = 0
    while empty_rounds < args.idle_rounds:
        try:
            response = client.invoke_endpoint(
                EndpointName=args.endpoint,
                ContentType="application/json",
                Accept="application/json",
                Body=json.dumps(
                    {
                        "limit": args.limit,
                        "batchSize": args.batch_size,
                        "refillSize": args.refill_size,
                    },
                    separators=(",", ":"),
                ),
            )
            result = json.loads(response["Body"].read())
            processed = int(result.get("processed", 0))
            consecutive_errors = 0
            empty_rounds = empty_rounds + 1 if processed == 0 else 0
            with lock:
                totals["processed"] += processed
                totals["artworkUpdates"] += int(
                    result.get("artworkUpdates", 0)
                )
                for outcome, count in result.get("outcomes", {}).items():
                    totals["outcomes"][outcome] = (
                        totals["outcomes"].get(outcome, 0) + int(count)
                    )
                print(
                    json.dumps(
                        {
                            "worker": worker_number,
                            "latest": result,
                            "total": totals,
                        },
                        separators=(",", ":"),
                    ),
                    flush=True,
                )
        except Exception as error:
            consecutive_errors += 1
            with lock:
                totals["errors"] += 1
                print(
                    json.dumps(
                        {
                            "worker": worker_number,
                            "error": f"{type(error).__name__}: {error}",
                            "total": totals,
                        },
                        separators=(",", ":"),
                    ),
                    flush=True,
                )
            if consecutive_errors >= args.max_consecutive_errors:
                raise RuntimeError(
                    f"worker {worker_number} stopped after "
                    f"{consecutive_errors} consecutive errors"
                ) from error
            time.sleep(5)


def main():
    args = parse_args()
    if (
        args.workers < 1
        or args.workers > 16
        or args.limit < 1
        or args.limit > 500
        or args.batch_size < 1
        or args.batch_size > 10
        or args.refill_size < 0
        or args.refill_size > 5000
        or args.idle_rounds < 1
        or args.max_consecutive_errors < 1
    ):
        raise RuntimeError("worker and invocation limits are out of range")

    client = boto3.Session(
        profile_name=os.getenv("AWS_PROFILE") or None,
        region_name=os.getenv("AWS_REGION", "us-east-1"),
    ).client(
        "sagemaker-runtime",
        config=Config(
            connect_timeout=10,
            read_timeout=70,
            retries={"max_attempts": 3, "mode": "adaptive"},
        ),
    )
    totals = {
        "processed": 0,
        "artworkUpdates": 0,
        "outcomes": {},
        "errors": 0,
    }
    lock = threading.Lock()
    started_at = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=args.workers
    ) as executor:
        futures = [
            executor.submit(
                invoke_worker,
                client,
                args,
                worker_number,
                totals,
                lock,
            )
            for worker_number in range(1, args.workers + 1)
        ]
        for future in futures:
            future.result()
    totals["durationMs"] = round((time.monotonic() - started_at) * 1000)
    print(json.dumps(totals, indent=2))


if __name__ == "__main__":
    main()
