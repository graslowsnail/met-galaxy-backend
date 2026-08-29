# Image Ingestion Worker

## Deployed Architecture

The Priority 2 worker is deployed in `us-east-1` as:

- Standard SQS queue `met-galaxy-image-ingestion` with a five-receive dead-letter queue.
- ARM64 Lambda `met-galaxy-image-ingestion`, using a scan-on-push immutable ECR image.
- Event-source maximum concurrency of sixteen and one artwork per Lambda
  invocation. This remains below the 32-way local migration run and is
  monitored for source-host throttling.
- Secrets Manager for the Neon connection string.
- Private S3 writes under `assets/v1/`, delivered through the existing CloudFront distribution.
- Database outbox rows dispatched to the FIFO queue `met-galaxy-image-embedding.fifo`.

OpenCLIP remains a separate GPU batch. Lambda downloads, normalizes,
fingerprints, deduplicates, encodes, uploads, links, and dispatches; it does
not run CPU OpenCLIP inference.

The CloudFormation sources are in `infra/image-ingestion/`.

## Fixed Processing Contract

For every artwork, the worker:

1. Claims the durable ingestion row with an expiring lease.
2. Fetches the Met primary image with three bounded attempts, then tries the
   small derivative when the original is unavailable or exceeds 50 MB.
3. Streams the source-byte SHA-256 while enforcing a 50 MB limit.
4. Auto-orients and computes the SHA-256 of the normalized RGBA dimensions and pixels.
5. Computes pHash64 and dHash64.
6. Links source-byte or normalized-pixel SHA-256 matches without another upload or embedding message.
7. Creates review candidates only when pHash distance is at most 8 and dHash distance is at most 4.
8. Encodes full WebP quality 82 and a 512 px WebP quality 85 graph derivative.
9. Writes both immutable objects as `image/webp`, verifies their metadata on resume, and links the artwork.
10. Inserts one unique embedding-outbox row only for a new canonical asset.

Approximate matches are never automatically merged. Nine pHash bands and five
dHash bands provide a complete candidate prefilter for the selected Hamming
thresholds without scanning every canonical asset.

The worker's dependency-free pHash and dHash implementations produced the
same hashes as `ImageHash` for all 1,000 Priority 1 benchmark originals.

## Idempotency and Recovery

- One ingestion row per artwork prevents duplicate durable jobs.
- Expired ingestion and asset leases are reclaimable.
- Canonical keys derive from the normalized-pixel digest.
- Unique normalized and encoded digests resolve concurrent insert races.
- Before a PUT, the worker checks existing object content type and digest metadata. A retry therefore skips a completed derivative.
- The embedding outbox has one row per canonical asset. Dispatch retries cannot create another durable embedding request.
- Dispatched embedding rows whose messages remain unprocessed for 30 minutes
  are eligible for idempotent redispatch. Completed asset rows are removed
  from the operational outbox.
- Retryable failures receive exponential database backoff and SQS partial-batch retry behavior. Other records continue independently.
- Persistent failures become terminal after five durable attempts.

If both stored Met URLs fail, the worker refreshes the artwork through the
current Met object API and retries changed primary and small-image URLs. A
successful refresh updates the stored metadata URLs. A removed object or an
object with no current image is recorded as a non-retryable source failure.

Dry runs write only attempt telemetry. They do not create ingestion rows,
canonical assets, artwork links, S3 objects, candidate pairs, or embedding
messages.

## Operations

Select a department-balanced batch:

```bash
npm run ingest-images -- seed --limit 100
```

Select and enqueue:

```bash
npm run ingest-images -- seed --limit 100 --enqueue
```

Normal seeding excludes recorded terminal failures. Retry them only after the
source condition has changed:

```bash
npm run ingest-images -- seed --limit 100 --enqueue --retry-terminal
```

Run a local dry run:

```bash
npm run ingest-images -- work --dry-run --limit 20 --concurrency 4
```

Run pending database jobs locally:

```bash
npm run ingest-images -- work --limit 20 --concurrency 4
```

Dispatch new canonical assets to OpenCLIP:

```bash
npm run ingest-images -- dispatch-embeddings --limit 100
```

Show durable totals:

```bash
npm run ingest-images -- stats
```

The Lambda receives SQS events through `lambda_handler`; local and deployed
execution use the same processing function.

## Priority 2 Verification

Dry-run audit:

- 6 artworks from 6 departments.
- 595,384 source bytes read and 577,406 encoded bytes projected.
- 0 ingestion rows, 0 assets, 0 links, and 0 S3 objects created.

Live audit:

- 8 canonical assets and 9 artwork links.
- 1 normalized-pixel exact duplicate linked despite different source-byte digests and sizes.
- 2,117,103 source bytes read across live attempts.
- 951,072 canonical bytes written as exactly 16 S3 objects and 16 object versions.
- 112 hash-band rows, exactly 14 per canonical asset.
- 8 unique embedding-outbox rows dispatched and 8 FIFO messages.
- 0 approximate candidate pairs in the representative sample.
- 4 completed artworks reprocessed with 0 downloads, 0 writes, and 0 additional outbox rows.
- A persistent Met 404 became retryable while a concurrent valid artwork completed.
- A deliberately exposed upload-permission failure retained its reserved asset and completed on retry after the scoped IAM correction.
- Estimated S3 PUT plus first-month storage cost: 101 micro-USD ($0.000101). Lambda, SQS, CloudFront, and downstream embedding costs are excluded.

The deployed lean Lambda cold-started in 2,639 ms, used 148 MB of its 2,048 MB
allocation, and completed the first representative ingestion in 1,788 ms.
The resulting CloudFront URL returned `206 image/webp`, and S3 metadata matched
the stored encoded and normalized SHA-256 digests.
