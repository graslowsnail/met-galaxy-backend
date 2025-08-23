-- Add HNSW index for pgvector similarity search
-- This index optimizes cosine similarity queries on the imgVec column
CREATE INDEX IF NOT EXISTS idx_artworks_imgvec_hnsw 
ON "met-galaxy_artwork" USING hnsw ("imgVec" vector_cosine_ops);