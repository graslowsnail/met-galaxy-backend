-- Migration: Add text embeddings column and index
-- Date: 2025-01-16  
-- Purpose: Add txtVec column for RRF text search implementation

-- Add text embedding column to artwork table
ALTER TABLE "met-galaxy_artwork" ADD COLUMN "txtVec" vector(1536);

-- Add HNSW index for efficient text embedding similarity search
CREATE INDEX IF NOT EXISTS "idx_artworks_txtvec_hnsw" 
ON "met-galaxy_artwork" USING hnsw ("txtVec" vector_cosine_ops)
WITH (m = 16, ef_construction = 64);

-- Add comment for documentation
COMMENT ON COLUMN "met-galaxy_artwork"."txtVec" IS 'Text embeddings for metadata search (1536-dim, OpenAI text-embedding-3-small)';
COMMENT ON INDEX "idx_artworks_txtvec_hnsw" IS 'HNSW index for fast text embedding similarity search using cosine distance';