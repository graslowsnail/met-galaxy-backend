-- Create partial HNSW index for eligible artworks only
-- This matches the exact WHERE clause used in field-chunks API
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_artworks_imgvec_eligible 
ON "met-galaxy_artwork" USING hnsw ("imgVec" vector_cosine_ops) 
WHERE "imgVec" IS NOT NULL AND "localImageUrl" IS NOT NULL AND "localImageUrl" != '';