-- Add HNSW index for text embedding similarity search
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_artworks_txtvec_eligible
ON "met-galaxy_artwork" USING hnsw ("txtVec" vector_cosine_ops)
WITH (m = 16, ef_construction = 64)
WHERE "txtVec" IS NOT NULL
  AND "imgVec" IS NOT NULL
  AND "localImageUrl" IS NOT NULL
  AND "localImageUrl" != '';

DROP INDEX CONCURRENTLY IF EXISTS idx_artworks_txtvec_hnsw;
