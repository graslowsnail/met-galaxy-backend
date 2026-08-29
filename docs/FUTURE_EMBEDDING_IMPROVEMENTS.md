# Future Image and Embedding Roadmap

**Status:** Complete for the accepted searchable corpus. Priorities 0–6 are
implemented, the selected production models are fixed, and the final search
path uses metadata vectors, matching OpenCLIP visual vectors, exact keywords,
and reciprocal-rank fusion.

This roadmap covers image hosting, normalization, duplicate detection, collection backfill, embeddings, search, and PCA generation. Future agents should take the highest incomplete priority whose prerequisites are satisfied.

Do not begin the full collection download or recompute all embeddings until Priorities 0–3 are complete. Doing so earlier would create image files, URLs, and vectors that may need to be replaced again.

## Current State

- The database contains 336,300 artwork records with Met image URLs.
- 33,387 images have been copied to S3 and occupy 4.48 GB, averaging about 134 KB each.
- 33,383 legacy artworks completed canonical migration and have image
  embeddings. Four legacy records have documented terminal source failures.
- The searchable subset is heavily skewed toward The American Wing, Asian Art, and Arms and Armor.
- Image embeddings use OpenCLIP `ViT-L-14` with 768 dimensions.
- Metadata embeddings use OpenAI `text-embedding-3-small` with 1,536 dimensions.
- The accepted searchable corpus contains 295,480 artworks with both image and
  metadata embeddings, with zero pending leases or terminal metadata failures.
- The canonical PCA cohort contains 288,359 ready root image assets after
  excluding verified duplicate losers.
- Typed search combines metadata, OpenCLIP visual-semantic, and exact-keyword
  rankings with weighted reciprocal rank fusion.
- The existing 33,387 objects still use predictable `artworks/{id}.jpg` keys pending migration; new canonical assets use content-addressed, versioned keys.
- The S3 bucket is private and the API returns CloudFront URLs for all 33,387 existing hosted images.

## Non-Negotiable Data Rules

- Preserve every Met artwork record unless a separate metadata audit proves that the record itself is invalid.
- Treat artwork records and image assets as separate concepts. Multiple legitimate records may share one canonical image.
- Automatically merge only exact image matches. Approximate visual matches require verification before they are collapsed.
- Archive redundant S3 objects only after every artwork points to a verified canonical image and a rollback path exists.
- Do not use the four-dimensional PCA coordinates for duplicate detection. PCA is a lossy visualization.

## Priority 0 — Lock the Storage and Data Architecture

**Why this comes first:** The schema, object-key strategy, and public delivery path affect every later download. Settling them first avoids another S3 migration.

### Work

- [x] Add an `image_assets` model for canonical image data, including the S3 key, MIME type, dimensions, byte size, SHA-256 digest, normalized-pixel digest, perceptual hash, processing status, and image embedding.
- [x] Add an artwork-to-image association so multiple Met records can reference one image asset.
- [x] Define duplicate states such as `unique`, `exact_duplicate`, `review_candidate`, and `verified_duplicate`.
- [x] Define idempotent ingestion states so interrupted jobs can resume without duplicate uploads or embeddings.
- [x] Replace predictable public object URLs with private S3 objects served through CloudFront Origin Access Control or approve an explicitly documented alternative.
- [x] Decide the final object-key structure, cache headers, CORS policy, retention policy, and rollback window.
- [x] Add AWS budget alerts before substantially increasing the image count.

### Recorded Decisions

- Canonical assets store separate full-size and graph-thumbnail S3 keys rather than public URLs.
- `encodedSha256` identifies the stored canonical WebP. Each artwork ingestion stores its original downloaded-byte `sourceSha256` so alternate source encodings are not lost when they share one canonical asset.
- Processing advances through `pending_upload`, `pending_embedding`, and `ready`. The current stage remains retryable when `lastError` is populated.
- Artwork ingestion advances through `pending`, leased `processing`, `awaiting_embedding`, and `complete`, with explicit `retryable_failure` and `terminal_failure` outcomes. One ingestion row per artwork prevents duplicate queue records.
- Ingestion and asset workers use expiring leases, attempt counters, and next-attempt timestamps. Expired work can be reclaimed, while unique asset keys and digests prevent repeated uploads and embeddings from creating duplicate canonical assets.
- Artworks use a nullable, indexed `imageAssetId` foreign key for the primary-image association. Asset deletion is restricted while artworks reference it.
- Artwork associations use `unique`, `exact_duplicate`, or `verified_duplicate`. Approximate matches remain separate image assets while represented by ordered, unique candidate pairs with `review_candidate`, `verified_duplicate`, or `rejected` review status.
- Rejected candidate pairs are retained so later jobs do not repeatedly suggest the same false positive. Approximate candidates are never merged automatically.
- Existing artwork image URLs and embeddings remain in place during migration to preserve API compatibility.
- Perceptual-hash algorithms and derivative dimensions remain unresolved until the Priority 1 benchmark; their asset fields are nullable until then.
- CloudFront distribution `E3B3NJO0AQL0KT` serves the private bucket through signed OAC requests at `https://d2pvxr3eb77vb4.cloudfront.net`. The AWS-managed caching and Simple CORS policies are used.
- Canonical keys use `assets/v1/{digest-prefix}/{normalized-pixel-sha256}/full.webp` and `graph.webp`. The `v1` namespace changes if encoding settings change incompatibly.
- Canonical objects use `image/webp` and `Cache-Control: public, max-age=31536000, immutable`. S3 versioning is enabled; noncurrent versions are retained for 30 days and incomplete multipart uploads for 7 days.
- The account-wide `met-galaxy-monthly` AWS cost budget is $60, with actual alerts at 50%, 80%, and 100% and a forecasted alert at 100%. Priority 1 measurements should replace this preliminary limit if needed.

### Completion Gate

- [x] The schema migration and image-serving design are reviewed.
- [x] One artwork can reference a canonical image asset without breaking the existing API.
- [x] The final S3 and CloudFront path can be used by the downloader without another URL migration.

### Priority 0 Verification

- Updated 33,387 database image URLs from direct S3 to CloudFront; 0 direct-S3 database URLs and 0 update failures remain.
- An uncached legacy object returns `200` through CloudFront with CORS headers, while the same direct S3 URL returns `403`.
- S3 reports all four public-access blocks enabled and a non-public bucket policy restricted to the CloudFront distribution.
- A temporary artwork-to-canonical-asset link returned successfully through the existing similarity API with its CloudFront URL; the verification rows were then removed.
- The legacy downloader now writes CloudFront URLs, uses the SDK default credential chain, and applies immutable cache headers.

## Priority 1 — Benchmark WebP and Duplicate Fingerprints

**Why this comes next:** Encoding quality and duplicate thresholds must be measured before they are embedded into the bulk ingestion worker.

### Work

- [x] Select a representative 1,000-image sample containing photographs, paintings, line art, text, transparency, small images, and very large images.
- [x] Calculate a SHA-256 digest from the original downloaded bytes.
- [x] Decode, auto-orient, and normalize each image before calculating a normalized-pixel digest and perceptual hash.
- [x] Compare full-size WebP quality settings such as 82, 85, and 88.
- [x] Compare 512 px and 768 px graph-thumbnail derivatives.
- [x] Record output size, encoding duration, dimensions, perceptual quality, and obvious artifacts.
- [x] Test duplicate thresholds against a human-reviewed set containing exact copies, recompressed copies, resized copies, crops, detail shots, and different photographs of the same artwork.
- [x] Decide whether original source files need archival retention after verification.

### Benchmark Decisions

- The reproducible 1,000-image run processed 151,483,244 source bytes across all 19 eligible departments; 15 failed URLs were replaced, with 0 unresolved sample failures.
- Full-size WebP quality 82 saved 24.54%, with mean SSIM 0.96996, P05 SSIM 0.93799, and no blocking artifact in the worst-case full-image and crop review sheets.
- The 512 px graph derivative used 31,103,934 bytes versus 62,371,656 bytes for 768 px. Select 512 px at WebP quality 85.
- Use source-byte SHA-256 and normalized RGBA-pixel SHA-256 for automatic exact matches.
- Use pHash64 distance ≤8 and dHash64 distance ≤4 only to create review candidates. Never automatically merge perceptual matches.
- The controlled transformation set retained all 150 exact, recompressed, and resized controls and admitted 0 unrelated controls at the selected combined threshold.
- Human review approved full WebP quality 82, 512 px graph thumbnails at quality 85, exact SHA-256 automatic merges only, and the combined pHash ≤8 plus dHash ≤4 review-candidate threshold.
- Do not permanently retain new source downloads after canonical outputs and links are verified. Keep the existing JPEG collection through Priority 3 and the 30-day rollback window.
- Full measurements and fixed settings are recorded in `docs/IMAGE_INGESTION_BENCHMARK.md`.

### Planning Estimate

- Equivalent-quality WebP files should be roughly 25–34% smaller than the current JPEGs.
- The current 4.48 GB should become approximately 2.96–3.36 GB.
- At 300,000 images, the current 37.5 GB projection should become approximately 24.8–28.1 GB.
- The main benefit is reduced frontend transfer and faster image loading, not S3 storage savings.

### Completion Gate

- [x] The WebP quality, thumbnail dimensions, fingerprint algorithms, and duplicate thresholds are recorded as fixed ingestion settings.
- [x] The reviewed sample has an acceptably low false-positive duplicate rate.

## Priority 2 — Build the One-Pass Ingestion Worker

**Why this precedes the remaining download:** Each Met image should be fetched once and leave the pipeline normalized, fingerprinted, deduplicated, converted, stored, and resumable.

### Processing Order

1. Fetch the source image once with bounded retries and respectful concurrency.
2. Calculate the original-byte SHA-256 digest while streaming the response.
3. Decode, auto-orient, and normalize the image.
4. Calculate the normalized-pixel digest and perceptual hash.
5. Link exact matches to the existing canonical image without uploading or embedding another copy.
6. Store approximate matches as review candidates. Do not automatically merge them.
7. Encode the selected full-size WebP and graph-thumbnail derivative.
8. Upload new canonical assets with the correct file extension, MIME type, and cache headers.
9. Link the artwork record to the canonical image asset and persist the processing status.
10. Queue only new canonical images for OpenCLIP image embedding.

### Work

- [x] Implement the pipeline as an idempotent worker with durable progress tracking.
- [x] Distribute work across departments instead of processing only sequential IDs.
- [x] Add concurrency limits so the job does not overload the Met image host, S3, or Neon.
- [x] Record downloads, exact duplicates, review candidates, conversion failures, upload failures, retries, bytes read, bytes written, and estimated cost.
- [x] Provide a dry-run mode that records decisions without changing database links or deleting objects.

SQS with an ARM64 Lambda worker is deployed for downloading and conversion.
The event source limits concurrency to four. OpenCLIP generation remains a
separate batched GPU job, with new canonical assets dispatched through a FIFO
embedding queue and durable database outbox.

### Completion Gate

- [x] The worker can stop and resume without duplicate side effects.
- [x] Reprocessing the same sample produces no additional S3 objects or embeddings.
- [x] Failed records remain retryable and do not block the rest of the queue.

### Priority 2 Verification

- A six-artwork, six-department dry run recorded 595,384 source bytes and
  projected 577,406 encoded bytes while creating 0 ingestion rows, assets,
  artwork links, or S3 objects.
- The live representative runs created 8 canonical assets, 9 artwork links,
  exactly 16 objects and 16 object versions totaling 951,072 bytes, and 8
  unique OpenCLIP FIFO messages.
- A normalized-pixel exact match linked two artworks to one asset even though
  the two downloaded source digests and byte sizes differed. No second object
  or embedding message was created.
- Four completed artworks reprocessed with 0 downloads, 0 writes, and 0 new
  outbox rows.
- A Met 404 remained retryable while a concurrently processed valid artwork
  completed. A reserved asset also resumed after an upload-permission failure.
- All 1,000 benchmark originals produced exactly the same pHash64 and dHash64
  values in the production worker as in the Priority 1 `ImageHash` run.
- The deployed Lambda cold start was 2,639 ms, peak memory was 148 MB, and its
  first representative ingestion completed in 1,788 ms.
- Detailed operations, recovery behavior, and exact metrics are recorded in
  `docs/IMAGE_INGESTION_WORKER.md`.

## Priority 3 — Migrate and Audit the Existing 33,387 Images

**Why this runs before the full backfill:** The existing collection is large enough to expose duplicate and conversion problems while remaining inexpensive to rerun.

### Work

- [x] Read each existing S3 object once; do not download it from the Met again.
- [x] Populate the new image-asset associations and fingerprint fields.
- [x] Create the selected WebP outputs.
- [x] Automatically group exact byte and normalized-pixel matches.
- [x] Produce a report of perceptual-hash and OpenCLIP nearest-neighbor candidates.
- [x] Review a representative candidate sample before applying approximate duplicate groups.
- [x] Confirm that every successfully migrated artwork still resolves to an
  image; record four pre-existing invalid-source exceptions.
- [x] Keep the old objects through the agreed rollback window.
- [x] Retain redundant legacy objects outside the active delivery path instead
  of performing a destructive archival operation as part of this project.

Use the original 768-dimensional OpenCLIP vectors only to generate a small nearest-neighbor candidate set. Verify those candidates with perceptual-hash distance and pixel similarity rather than treating semantic similarity as proof of duplication.

### Completion Gate

- [x] All 33,387 existing images have a migration status.
- [x] Search and detail views resolve the new WebP URLs successfully.
- [x] Duplicate metrics, WebP savings, failures, and unresolved review candidates are documented.
- [x] The migration can be rolled back without losing a Met artwork record.

Archival remains intentionally deferred until the 30-day rollback window ends
on August 29, 2026. The retained legacy objects are not required by the
Priority 4 ingestion path.

## Priority 4 — Download and Embed the Remaining Collection

**Why this waits:** The remaining approximately 302,900 images should enter the final pipeline instead of creating another temporary dataset.

### Work

- [x] Queue eligible missing artwork IDs through the completed ingestion worker
  until the accepted visual corpus was closed.
- [x] Monitor source-host errors and adjust concurrency rather than retrying aggressively.
- [x] Preserve unresolved duplicate review candidates as separate assets; do
  not automatically merge approximate matches.
- [x] Generate OpenCLIP embeddings only for new canonical image assets.
- [x] Generate metadata embeddings for artwork records that do not already have them.
- [x] Track coverage by department and record the accepted searchable cohort.
- [x] Record the accepted final corpus, canonical cohort, unresolved duplicate
  candidates, embedding coverage, failures, and infrastructure cost decision.

At approximately eight images per second, the download portion is expected to take roughly 10–14 hours if the Met host and worker remain stable. A reasonable preliminary budget is $20–$60 for ingestion and embedding, but the Priority 1 benchmark should replace this estimate before the full run.

### Completion Gate

- Every eligible artwork is either searchable or has a recorded terminal failure reason.
- The final counts reconcile across artwork records, canonical image assets, S3 objects, and embeddings.

The accepted visual corpus was closed at 295,480 image-searchable artworks. All
295,480 also have metadata embeddings; no metadata work remains pending or
terminally failed. Further visual backfill was intentionally declined after the
AWS compute run.

## Priority 5 — Make Search and the PCA Graph Duplicate-Aware

**Why this follows the migration:** Query behavior should use verified canonical groups and complete embeddings rather than temporary duplicate heuristics.

### Work

- [x] Return at most one canonical image per duplicate group in search and similarity results.
- [x] Exclude the focal image's duplicate group from its similarity neighbors.
- [x] Preserve access to every linked Met artwork record from the canonical image detail view.
- [x] Build the PCA projection from canonical image embeddings only.
- [x] Rebuild the image and text HNSW indexes after the backfill is complete.
- [x] Regenerate the PCA projection only after the canonical dataset and embeddings are stable.

### Completion Gate

- Verified duplicate images do not appear as repeated visual results.
- Linked Met records remain discoverable.
- Search, similarity navigation, and the PCA graph use the same canonical-image rules.

### Priority 5 Index and PCA Verification

- Migration `0012_smiling_orphan` records the production text HNSW definition
  in the Drizzle schema ledger.
- The corrected 2,257 MB text HNSW index and 1,126 MB canonical-image HNSW index
  were rebuilt concurrently, vacuumed, analyzed, and selected by representative
  query plans.
- `pca_basis.json` was regenerated from 288,359 ready canonical root assets in
  deterministic asset-ID order. The four 768-dimensional components are finite,
  unit length, orthogonal, and explain 19.7273% of total variance.
- The duplicate audit reports 14 canonical roots and 14 retained loser assets,
  with zero self or chained mappings, artwork links to losers, conflicting
  candidate states, missing loser object keys, or artwork/asset embedding
  mismatches. The 19,921 unresolved review candidates remain separate assets as
  required.
- The audit also reports 38,346 referenced root assets without image embeddings.
  This is the accepted visual-coverage gap, not a canonical-group inconsistency;
  those records remain outside the 295,480-artwork searchable corpus.
- The index and PCA work used the existing Neon compute. It made no AWS or
  OpenAI API calls.

## Priority 6 — Improve Retrieval Quality

**Why this is later:** Model and fusion decisions should be evaluated against the complete, deduplicated collection.

### Work

- [x] Build a small human-reviewed evaluation set covering broad, narrow, visual, metadata, and exact-name queries.
- [x] Establish metrics and record the current metadata-search baseline.
- [x] Add the matching OpenCLIP text encoder for text-to-image retrieval.
- [x] Add exact-keyword retrieval for titles, artists, cultures, object types, and identifiers.
- [x] Combine visual-semantic, metadata-vector, and exact-keyword rankings with reciprocal rank fusion.
- [x] Evaluate `text-embedding-3-large` at 1,536 dimensions against `text-embedding-3-small`.

### Evaluation baseline (2026-08-29)

- The versioned evaluation set contains 15 reviewed queries, with three queries
  in each category and 304 positive canonical-asset relevance labels.
- Metadata-only search achieved Precision@5 `0.573333`, Recall@10 `0.298814`,
  MRR `0.785556`, and NDCG@10 `0.617802`.
- Current fused search achieved Precision@5 `0.680000`, Recall@10 `0.426065`,
  MRR `0.922222`, and NDCG@10 `0.783844`.
- All 30 requests completed without failure; no fused request degraded. The
  reproducible report is `evaluation/search-evaluation-report.json`.
- A deterministic 1,000-document comparison retained every one of the 304
  judged canonical assets and sampled 696 additional searchable assets across
  all departments. At 1,536 dimensions, `text-embedding-3-large` changed
  Precision@5 by `+0.026666`, Recall@10 by `-0.007851`, MRR by `-0.053038`,
  and NDCG@10 by `-0.018238` relative to `text-embedding-3-small`.
- Keep `text-embedding-3-small`; the bounded evaluation does not justify a
  full-corpus model migration. The model-comparison report is
  `evaluation/text-embedding-model-evaluation.json`.
- The API fixes metadata queries to `text-embedding-3-small` at 1,536
  dimensions and validates that visual queries come from the matching
  OpenCLIP `ViT-L-14` / `openai` text tower at 768 dimensions. A mismatched
  visual encoder is rejected instead of silently producing invalid rankings.
- Exact title, artist, culture, classification, artwork ID, and Met object ID
  matches take precedence over semantic-only RRF results. All other candidates
  retain deterministic weighted reciprocal-rank fusion across metadata,
  visual, and keyword retrieval.
- The backend and matching OpenCLIP service can be deployed together with
  `compose.search.yaml`; the OpenCLIP service is private to the Compose network.
- [x] Keep the existing OpenCLIP image model. The evaluation produced no
  evidence that would justify recomputing every canonical image vector,
  rebuilding its HNSW index, and regenerating PCA.

### Completion Gate

- [x] Retrieval changes have measured improvements on the reviewed query set.
- [x] Model, ranking, and fusion choices are documented with their cost and migration impact.

## Agent Handoff Rules

When an agent works on this roadmap:

1. Take the highest incomplete priority with satisfied prerequisites.
2. Start with the smallest representative batch before broad writes.
3. Update the checkboxes, measurements, decisions, and unresolved risks in this document.
4. Do not delete source records or S3 objects as part of an experiment.
5. Stop at the completion gate unless the request explicitly includes the next priority.
6. Report exact processed, skipped, duplicate, failed, storage, duration, and cost counts whenever a batch job runs.
