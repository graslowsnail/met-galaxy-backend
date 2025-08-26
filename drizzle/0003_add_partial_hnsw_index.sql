-- Add partial HNSW index for eligible artworks
-- This optimizes vector similarity queries by only indexing rows that meet eligibility criteria
-- Replaces the general HNSW index for better performance on filtered queries

-- Drop the old general HNSW index since we're replacing it with a partial one
DROP INDEX IF EXISTS idx_artworks_imgvec_hnsw;

-- Create partial HNSW index that exactly matches the field-chunk API eligibility predicate
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_artworks_imgvec_eligible 
ON "met-galaxy_artwork" USING hnsw ("imgVec" vector_cosine_ops) 
WHERE "imgVec" IS NOT NULL AND "localImageUrl" IS NOT NULL AND "localImageUrl" != '';

-- Also create supporting index for localImageUrl filtering
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_artworks_localimage_notnull 
ON "met-galaxy_artwork" ("localImageUrl") 
WHERE "localImageUrl" IS NOT NULL AND "localImageUrl" != '';