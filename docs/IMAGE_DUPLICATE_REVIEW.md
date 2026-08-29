# Image Duplicate Review

`scripts/review_image_duplicates.py` is the operator workflow for approximate
image matches. Approximate candidates remain separate until a reviewer either
rejects or verifies a pair.

## Canonical groups

`met-galaxy_image_asset_canonical` stores one row per retained loser asset. Its
`canonicalAssetId` always points directly to the deterministic group root. A
root is an image asset whose ID is absent from the mapping table's `assetId`
column.

The verifier chooses the lowest image asset ID in the complete connected
verified group. It flattens every loser directly to that root, relinks all
artworks from the losers, synchronizes their URL and image embedding, and marks
the reviewed candidate edges. Search, similarity, and PCA queries should only
select image assets absent from `met-galaxy_image_asset_canonical.assetId`.

The loser asset rows, full-size keys, thumbnail keys, hash bands, embeddings,
and S3 objects are not deleted or modified. Candidate rows are also retained as
the review and group history. Ingestion-attempt rows remain unchanged because
they are historical records of the original processing decision.

## Commands

Apply the schema migration before using the workflow:

```sh
npm run db:migrate
```

List or export unresolved candidates:

```sh
npm run review-image-duplicates -- list --limit 100
npm run review-image-duplicates -- export \
  --output migration-reports/duplicate-review.json
```

Always inspect a merge plan before committing it:

```sh
npm run review-image-duplicates -- verify --candidate-id 123 --dry-run
npm run review-image-duplicates -- verify --candidate-id 123
```

Reject a false positive:

```sh
npm run review-image-duplicates -- reject --candidate-id 123 --dry-run
npm run review-image-duplicates -- reject --candidate-id 123
```

Pairs can also be addressed without looking up the candidate row ID:

```sh
npm run review-image-duplicates -- verify --asset-ids 100 200 --dry-run
```

Every mutation uses a serializable transaction and a database advisory lock.
Dry runs execute the same statements and roll the transaction back. Repeating
the same rejection or verified merge is idempotent. A merge is refused if it
would join a previously rejected pair.

Run the exact consistency audit after each review batch:

```sh
npm run review-image-duplicates -- audit
```

The audit reports candidate status counts, loser/root mapping counts, and exact
counts for self or chained mappings, links that still point to losers,
conflicting candidate states, retained losers missing object keys, referenced
roots missing embeddings, and artwork/asset embedding mismatches.
