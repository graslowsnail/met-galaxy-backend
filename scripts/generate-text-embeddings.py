#!/usr/bin/env python3
"""
Generate text embeddings for Met Museum artworks using OpenAI text-embedding-3-small.

Combines metadata fields (title, artist, date, medium, department, culture,
classification, description) into a single text and generates 1536-dim embeddings.

Usage:
    python scripts/generate-text-embeddings.py

Requires:
    OPENAI_API_KEY and DATABASE_URL in .env
"""

import argparse
import os
import sys
import time
import psycopg2
from psycopg2.extras import execute_values
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

database_url = os.getenv('DATABASE_URL')
openai_api_key = os.getenv('OPENAI_API_KEY')

if not database_url:
    print("DATABASE_URL not found in .env")
    sys.exit(1)
if not openai_api_key:
    print("OPENAI_API_KEY not found in .env")
    sys.exit(1)

BATCH_SIZE = 100
MODEL = "text-embedding-3-small"

client = OpenAI(api_key=openai_api_key)


def create_artwork_text(row):
    """Combine artwork metadata into a single searchable string."""
    fields = [
        ("Title", row[1]),
        ("Artist", row[2]),
        ("Date", row[3]),
        ("Medium", row[4]),
        ("Department", row[5]),
        ("Culture", row[6]),
        ("Classification", row[7]),
        ("Description", row[8]),
        ("Nationality", row[9]),
    ]
    parts = [f"{label}: {value}" for label, value in fields if value]
    return " | ".join(parts) if parts else None


def embed_texts(texts):
    """Call OpenAI embedding API for a batch of texts."""
    response = client.embeddings.create(model=MODEL, input=texts)
    return [item.embedding for item in response.data]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker-count", type=int, default=1)
    parser.add_argument("--worker-index", type=int, default=0)
    args = parser.parse_args()

    if args.worker_count < 1:
        parser.error("--worker-count must be at least 1")
    if args.worker_index < 0 or args.worker_index >= args.worker_count:
        parser.error("--worker-index must be between 0 and worker-count - 1")

    if 'channel_binding=require' in database_url:
        clean_url = database_url.replace('channel_binding=require', '').rstrip('&')
    else:
        clean_url = database_url

    conn = psycopg2.connect(clean_url)
    cursor = conn.cursor()
    print("Connected to database")

    cursor.execute("""
        SELECT id, title, artist, date, medium, department, culture,
               classification, description, "artistNationality"
        FROM "met-galaxy_artwork"
        WHERE "txtVec" IS NULL
          AND "imgVec" IS NOT NULL
          AND "localImageUrl" IS NOT NULL
          AND "localImageUrl" != ''
          AND MOD(id, %s) = %s
        ORDER BY id
    """, (args.worker_count, args.worker_index))

    artworks = cursor.fetchall()
    print(
        f"Worker {args.worker_index + 1}/{args.worker_count}: "
        f"found {len(artworks)} artworks needing text embeddings"
    )

    if not artworks:
        print("All artworks already have text embeddings")
        conn.close()
        return

    total_success = 0
    total_skipped = 0
    consecutive_failures = 0
    start_time = time.time()

    for i in range(0, len(artworks), BATCH_SIZE):
        batch = artworks[i:i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1
        total_batches = (len(artworks) + BATCH_SIZE - 1) // BATCH_SIZE

        texts = []
        valid_rows = []
        for row in batch:
            text = create_artwork_text(row)
            if text:
                texts.append(text)
                valid_rows.append(row)
            else:
                total_skipped += 1

        if not texts:
            print(f"Batch {batch_num}/{total_batches}: all rows skipped (no metadata)")
            continue

        try:
            embeddings = embed_texts(texts)

            updates = [
                (row[0], f"[{','.join(map(str, embedding))}]")
                for row, embedding in zip(valid_rows, embeddings)
            ]
            execute_values(
                cursor,
                """
                UPDATE "met-galaxy_artwork" AS artwork
                SET "txtVec" = batch.embedding::vector
                FROM (VALUES %s) AS batch(id, embedding)
                WHERE artwork.id = batch.id
                """,
                updates
            )

            conn.commit()
            total_success += len(embeddings)
            consecutive_failures = 0
            print(f"Batch {batch_num}/{total_batches}: {len(embeddings)} embeddings stored")

        except Exception as e:
            print(f"Batch {batch_num} failed: {e}")
            conn.rollback()
            consecutive_failures += 1
            if consecutive_failures >= 3:
                print("3 consecutive failures — stopping. Check your API key/quota.")
                break

    elapsed = time.time() - start_time
    print(f"\nDone: {total_success} embeddings generated, {total_skipped} skipped")
    print(f"Time: {elapsed:.1f}s")

    conn.close()


if __name__ == "__main__":
    main()
