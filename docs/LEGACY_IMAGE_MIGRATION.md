# Legacy Image Migration

## Scope

Priority 3 migrates the 33,387 existing `artworks/{id}.jpg` objects into the
canonical `assets/v1/` layout. It never fetches these images from the Met and
does not delete or archive a legacy object.

Each migration message carries its exact legacy S3 key. The shared ingestion
worker:

- reads that S3 object once;
- computes the source, normalized-pixel, pHash64, and dHash64 fingerprints;
- creates or links the canonical WebP asset;
- copies the artwork's existing 768-dimensional OpenCLIP vector to a new canonical asset;
- updates the artwork to the CloudFront WebP URL;
- records exact duplicate and review-candidate decisions; and
- marks the ingestion complete without queuing another embedding when an existing vector was reused.

Up to 15 legacy artworks lack an existing vector. A new canonical asset for
one of those records remains `awaiting_embedding` and uses the normal unique
embedding outbox; an exact match can reuse an already-ready canonical asset.

## Commands

Run a department-balanced dry run:

```bash
AWS_PROFILE=met-galaxy \
  npm run ingest-images -- work --legacy --dry-run --limit 20 --concurrency 4
```

Seed and enqueue a bounded batch:

```bash
AWS_PROFILE=met-galaxy \
  npm run ingest-images -- seed-legacy --limit 100 --enqueue
```

Generate reconciliation totals and the rollback manifest:

```bash
AWS_PROFILE=met-galaxy \
  venv/bin/python scripts/audit_legacy_image_migration.py
```

Add the OpenCLIP nearest-neighbor report, pixel similarity, and visual review
sheets:

```bash
AWS_PROFILE=met-galaxy \
  venv/bin/python scripts/audit_legacy_image_migration.py \
    --build-candidates \
    --neighbors 5 \
    --review-count 50
```

Generated reports are written to the ignored
`migration-reports/priority3/` directory.

## Rollback

The audit writes `rollback-manifest.jsonl` with every artwork ID, legacy S3
key, previous delivery URL, canonical asset ID, and canonical URL.

Validate the current links against that manifest:

```bash
venv/bin/python scripts/rollback_legacy_image_migration.py
```

Apply the association rollback only when intentionally required:

```bash
venv/bin/python scripts/rollback_legacy_image_migration.py --apply
```

The rollback is one database transaction. It restores legacy CloudFront URLs,
clears canonical artwork associations, and resets ingestion rows to `pending`.
It does not delete canonical assets, embeddings, candidate records, outbox
rows, or either set of S3 objects.

## Representative Verification

- Six-object dry run across six departments: 2,309,494 source bytes read,
  1,126,564 canonical bytes projected, and no ingestion rows, links, or
  objects created.
- Six-object local live run: 6/6 complete and ready, with all six existing
  OpenCLIP vectors reused.
- Ten-object deployed Lambda run: 10/10 complete and ready, no DLQ entries,
  and no new embedding messages.
- Reprocessing the first six returned `already_linked` with 0 downloads,
  writes, object versions, or outbox rows.
- Every sampled legacy JPEG remained readable after its artwork switched to
  the canonical WebP URL.
- The API similarity endpoint returned the migrated canonical WebP URL, and
  the URL returned `image/webp` through CloudFront.

## Full Migration Results

The frozen 33,387-artwork cohort reconciled on July 30, 2026:

- 33,383 artworks are complete, linked, ready, and embedded; no successful
  link is missing an image embedding.
- Four records are terminal: two legacy objects were empty and their Met
  object records now return 404, while two current Met image URLs also return
  404. The artwork metadata records remain intact.
- 32,050 canonical root assets represent the completed cohort after verified
  duplicate mappings. There are 1,303 exact-duplicate artwork links.
- The legacy prefix remains unchanged at 33,387 objects and 4,477,499,853
  bytes. The canonical prefix contains 64,144 full and graph objects totaling
  3,467,096,830 bytes, including the eight Priority 2 verification assets.
  This is a conservative 1,010,403,023-byte (22.57%) reduction.
- The migration recorded 1,371 perceptual-review candidates. A representative
  50-pair review verified 14 duplicates and rejected 36 false positives;
  1,321 candidates remain separate and unresolved.
- Recorded ingestion cost was $0.400296. The frozen-cohort attempt window ran
  from 00:39:13 through 03:23:31 UTC, including targeted recovery work.
- The rollback manifest preserves all 33,387 legacy keys and URLs. It validates
  all 33,383 current canonical links with zero mismatches; the four terminal
  rows remain unlinked.
- S3 reconciliation exactly matches the database byte totals and object
  counts. Canonical API, similarity, detail, and CloudFront WebP delivery
  checks passed.

Legacy objects remain in place through August 29, 2026, the end of the 30-day
rollback window. Archival or deletion is a separate post-window operation and
is not performed by the migration tooling.
