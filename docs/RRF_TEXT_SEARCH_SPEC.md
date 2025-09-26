# RRF Text Search API - Implementation Specification

**Status:** 🚧 To Be Implemented  
**Version:** 1.0.0  
**Date:** 2025-01-16

## Overview

The RRF Text Search API provides intelligent text-based artwork search using Reciprocal Rank Fusion (RRF) to combine two complementary search approaches:

1. **Text-to-Metadata Search**: Query against `txtVec` embeddings (metadata semantic search)
2. **Text-to-Image Search**: Query against `imgVec` embeddings using CLIP text tower (cross-modal search)

This dual approach enables both precise metadata matching ("paintings by Monet from 1890s") and semantic visual matching ("blue landscapes with water").

## Architecture

### Stack
- **Backend**: Node.js + Express + TypeScript  
- **ORM**: Drizzle  
- **Database**: PostgreSQL with pgvector extension  
- **ML Models**: 
  - **Image Embeddings**: CLIP ViT-L/14 (768-dimensional, existing)
  - **Text Embeddings**: TBD - OpenAI text-embedding-3-small (1536-dim) or local model
- **Python Environment**: Existing venv with CLIP dependencies

### Core Components

#### 1. Database Schema Extension
```sql
-- Add text embedding column to existing artwork table
ALTER TABLE "met-galaxy_artwork" 
ADD COLUMN "txtVec" vector(1536); -- Dimension TBD based on chosen model

-- Add HNSW index for text embeddings
CREATE INDEX idx_artworks_txtvec_hnsw 
ON "met-galaxy_artwork" USING hnsw ("txtVec" vector_cosine_ops)
WITH (m = 16, ef_construction = 64);
```

#### 2. Text Embedding Generation (`scripts/generate-text-embeddings.py`)
```python
# Generate embeddings from combined metadata fields:
# title + artist + date + medium + department + culture + description
```

#### 3. RRF Search Route (`src/routes/artworks.ts`)
```typescript
GET /api/artworks/search
```

#### 4. Text Encoding Service
- **Option A**: OpenAI API integration
- **Option B**: Local HuggingFace model (sentence-transformers)

## API Specification

### Endpoint
```
GET /api/artworks/search
```

### Parameters
| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `q` | string | Yes | - | Search query text |
| `count` | integer | No | 50 | Results to return (1-100) |
| `w_text` | float | No | 1.0 | Weight for text-to-metadata search |
| `w_image` | float | No | 1.0 | Weight for text-to-image search |
| `k_rrf` | integer | No | 60 | RRF parameter (higher = less tail influence) |
| `threshold` | float | No | 0.0 | Minimum similarity threshold |

### Response Format
```json
{
  "success": true,
  "data": [
    {
      "id": 123,
      "objectId": 456789,
      "title": "The Starry Night",
      "artist": "Vincent van Gogh",
      "date": "1889",
      "department": "European Paintings",
      "culture": "Dutch",
      "imageUrl": "https://met-artworks-images.s3.amazonaws.com/artworks/123.jpg",
      "originalImageUrl": "https://images.metmuseum.org/...",
      "imageSource": "s3",
      "objectUrl": "https://www.metmuseum.org/art/collection/search/436532",
      "rrfScore": 0.1342,
      "subscores": {
        "textRank": 3,
        "imageRank": 11,
        "textSimilarity": 0.85,
        "imageSimilarity": 0.72
      }
    }
  ],
  "meta": {
    "query": "starry night painting",
    "count": 50,
    "weights": { "text": 1.0, "image": 1.0 },
    "k_rrf": 60,
    "timing": {
      "textEmbed": "25ms",
      "textSearch": "120ms", 
      "imageSearch": "130ms",
      "fusion": "1ms",
      "total": "276ms"
    }
  }
}
```

## RRF Algorithm Implementation

### Core RRF Formula
```typescript
// For each artwork d, compute RRF score across both result lists
function computeRRFScore(
  textRank: number | null,    // Rank in text search (1-based, null if not found)
  imageRank: number | null,   // Rank in image search (1-based, null if not found)
  wText: number = 1.0,        // Text weight
  wImage: number = 1.0,       // Image weight
  k: number = 60              // RRF parameter
): number {
  let score = 0;
  
  if (textRank !== null) {
    score += wText * (1 / (k + textRank));
  }
  
  if (imageRank !== null) {
    score += wImage * (1 / (k + imageRank));
  }
  
  return score;
}
```

### Search Process
```typescript
async function performRRFSearch(query: string, options: SearchOptions) {
  const startTime = Date.now();
  
  // 1. Generate embeddings
  const embedStart = Date.now();
  const [textEmbedding, imageEmbedding] = await Promise.all([
    generateTextEmbedding(query),      // For txtVec search
    generateCLIPTextEmbedding(query)   // For imgVec search
  ]);
  const embedTime = Date.now() - embedStart;
  
  // 2. Parallel vector searches
  const searchStart = Date.now();
  const [textResults, imageResults] = await Promise.all([
    searchTextVectors(textEmbedding, options.count * 2),
    searchImageVectors(imageEmbedding, options.count * 2)
  ]);
  const searchTime = Date.now() - searchStart;
  
  // 3. RRF Fusion
  const fusionStart = Date.now();
  const fusedResults = fuseResults(textResults, imageResults, options);
  const fusionTime = Date.now() - fusionStart;
  
  return {
    results: fusedResults.slice(0, options.count),
    timing: {
      embed: embedTime,
      search: searchTime,
      fusion: fusionTime,
      total: Date.now() - startTime
    }
  };
}
```

## Database Schema Changes

### Current Schema (Existing)
```typescript
export const artworks = createTable("artwork", {
  id: integer("id").primaryKey(),
  objectId: integer("objectId").notNull(),
  title: text("title"),
  artist: text("artist"), 
  date: varchar("date", { length: 200 }),
  medium: text("medium"),
  department: varchar("department", { length: 300 }),
  culture: varchar("culture", { length: 300 }),
  description: text("description"),
  imgVec: vector("imgVec", { dimensions: 768 }), // ✅ Already exists
  // ... other fields
});
```

### Required Schema Addition
```typescript
export const artworks = createTable("artwork", {
  // ... existing fields ...
  imgVec: vector("imgVec", { dimensions: 768 }), // ✅ Existing
  txtVec: vector("txtVec", { dimensions: 1536 }), // 🆕 Add this field
  // ... other fields ...
});
```

### Migration Script
```sql
-- Add text embedding column
ALTER TABLE "met-galaxy_artwork" 
ADD COLUMN "txtVec" vector(1536);

-- Add HNSW index for efficient similarity search
CREATE INDEX idx_artworks_txtvec_hnsw 
ON "met-galaxy_artwork" USING hnsw ("txtVec" vector_cosine_ops)
WITH (m = 16, ef_construction = 64);

-- Update drizzle migration
-- File: drizzle/0004_add_text_embeddings.sql
```

## Text Embedding Generation

### Metadata Combination Strategy
```python
def create_artwork_text(artwork):
    """Combine artwork metadata into searchable text."""
    parts = []
    
    # Core identification
    if artwork.title:
        parts.append(f"Title: {artwork.title}")
    if artwork.artist:
        parts.append(f"Artist: {artwork.artist}")
    
    # Temporal and cultural context  
    if artwork.date:
        parts.append(f"Date: {artwork.date}")
    if artwork.culture:
        parts.append(f"Culture: {artwork.culture}")
    if artwork.artist_nationality:
        parts.append(f"Nationality: {artwork.artist_nationality}")
        
    # Physical and categorical
    if artwork.medium:
        parts.append(f"Medium: {artwork.medium}")
    if artwork.department:
        parts.append(f"Department: {artwork.department}")
    if artwork.classification:
        parts.append(f"Type: {artwork.classification}")
    
    # Descriptive content
    if artwork.description:
        parts.append(f"Description: {artwork.description}")
    
    return " | ".join(parts)

# Example output:
# "Title: The Starry Night | Artist: Vincent van Gogh | Date: 1889 | 
#  Culture: Dutch | Medium: Oil on canvas | Department: European Paintings | 
#  Type: Painting | Description: A swirling night sky over a village..."
```

### Embedding Script Structure
```python
# scripts/generate-text-embeddings.py
import os
import psycopg2
import openai  # or transformers for local model
from dotenv import load_dotenv

BATCH_SIZE = 100
MODEL_NAME = "text-embedding-3-small"  # or local model

def generate_text_embeddings():
    # 1. Fetch artworks without txtVec
    artworks = fetch_artworks_needing_text_embeddings()
    
    # 2. Process in batches
    for batch in chunks(artworks, BATCH_SIZE):
        texts = [create_artwork_text(artwork) for artwork in batch]
        embeddings = embed_texts(texts)
        
        # 3. Update database
        update_text_embeddings(batch, embeddings)

def embed_texts(texts):
    # Option A: OpenAI API
    response = openai.embeddings.create(
        model=MODEL_NAME,
        input=texts
    )
    return [data.embedding for data in response.data]
    
    # Option B: Local model (sentence-transformers)
    # model = SentenceTransformer('all-MiniLM-L6-v2')
    # return model.encode(texts).tolist()
```

## Implementation Phases

### Phase 1: Foundation (Week 1)
**Goal**: Set up basic dual embedding infrastructure

**Tasks**:
- [ ] Add `txtVec` column to schema + migration
- [ ] Choose text embedding model (OpenAI vs local)
- [ ] Create text embedding generation script
- [ ] Generate embeddings for 1000 test artworks

**Deliverables**:
- Updated schema with `txtVec` field
- Text embedding generation pipeline
- 1k artworks with both `imgVec` and `txtVec`

**Acceptance**: 
- Database has both embedding types
- Can generate text embeddings from metadata

### Phase 2: Search Implementation (Week 2)
**Goal**: Build RRF search endpoint

**Tasks**:
- [ ] Implement text embedding service (API or local)
- [ ] Create dual vector search functions
- [ ] Build RRF fusion algorithm
- [ ] Create `/api/artworks/search` endpoint
- [ ] Add comprehensive error handling

**Deliverables**:
- Working search endpoint
- RRF fusion implementation
- Performance timing instrumentation

**Acceptance**:
- Query "Japanese art" returns relevant results
- Response includes timing breakdown
- Both text and image search contribute to results

### Phase 3: Optimization & Testing (Week 3)
**Goal**: Optimize performance and validate quality

**Tasks**:
- [ ] Generate embeddings for full dataset
- [ ] Tune RRF parameters (k, weights)
- [ ] Create test query evaluation set
- [ ] Optimize database queries and indexes
- [ ] Add caching layer if needed

**Deliverables**:
- Full dataset with embeddings
- Tuned RRF parameters
- Performance benchmarks
- Quality evaluation results

**Acceptance**:
- <800ms median response time
- ≥80% relevance on test queries
- Handles edge cases gracefully

## File Structure

```
src/
├── db/
│   └── schema.ts                    # ✅ Add txtVec field
├── routes/
│   └── artworks.ts                  # 🆕 Add /search endpoint
├── lib/
│   ├── textEmbedding.ts            # 🆕 Text embedding service
│   ├── rrfFusion.ts                # 🆕 RRF algorithm
│   └── fieldVectors.ts             # ✅ Existing
└── types/
    └── search.ts                   # 🆕 Search types

scripts/
├── generate-text-embeddings.py     # 🆕 Text embedding generation
├── generate-embeddings.py          # ✅ Existing (image)
└── requirements.txt                # ✅ Update if needed

drizzle/
└── 0004_add_text_embeddings.sql    # 🆕 Schema migration

docs/
└── RRF_TEXT_SEARCH_SPEC.md         # 🆕 This document
```

## Configuration

### Environment Variables
```bash
# Text Embedding Service
TEXT_EMBEDDING_PROVIDER=openai  # or "local"
OPENAI_API_KEY=sk-...           # If using OpenAI

# RRF Default Parameters  
RRF_K_DEFAULT=60
RRF_W_TEXT_DEFAULT=1.0
RRF_W_IMAGE_DEFAULT=1.0

# Performance Tuning
SEARCH_RESULT_LIMIT=200         # Internal limit before RRF
EMBEDDING_BATCH_SIZE=100        # For generation
```

### Tuning Parameters
```typescript
// RRF Configuration
const RRF_CONFIG = {
  k: 60,              // Higher = less influence from low-ranked results
  wText: 1.0,         // Text search weight
  wImage: 1.0,        // Image search weight
  maxResults: 200,    // Internal search limit
  minThreshold: 0.0   // Minimum similarity threshold
};

// Performance Limits
const PERFORMANCE_LIMITS = {
  maxCount: 100,      // Maximum results per request
  timeout: 10000,     // 10 second timeout
  cacheTime: 300      // 5 minute cache
};
```

## Text Embedding Model Decision

### Option A: OpenAI text-embedding-3-small
**Pros**:
- ✅ High quality, proven performance
- ✅ 1536 dimensions (manageable size)
- ✅ Fast API response times
- ✅ No local infrastructure needed

**Cons**:
- ❌ Cost per embedding (~$0.00002 per 1k tokens)
- ❌ External API dependency
- ❌ Rate limits (3000 RPM)

**Cost Estimate**: 336k artworks × ~200 tokens avg × $0.00002 = ~$1.34 one-time

### Option B: Local sentence-transformers
**Pros**:
- ✅ No ongoing costs
- ✅ No rate limits
- ✅ Full control and privacy
- ✅ Can run offline

**Cons**:
- ❌ Lower quality than OpenAI
- ❌ Larger model size (>400MB)
- ❌ Slower inference
- ❌ More complex deployment

**Recommended**: Start with OpenAI for quality, evaluate local models later.

## Performance Expectations

### Response Time Targets
- **Text Embedding**: 25-50ms (OpenAI API)
- **Text Vector Search**: 50-150ms (pgvector HNSW)
- **Image Vector Search**: 50-150ms (existing performance)
- **RRF Fusion**: 1-5ms (in-memory computation)
- **Total Response**: 200-400ms target, <800ms maximum

### Throughput Estimates
- **Concurrent Requests**: 50-100 QPS
- **Database Connections**: 10-20 pool size
- **Memory Usage**: ~100MB for embeddings cache
- **Storage**: ~2GB for 336k text vectors (1536 × 4 bytes)

## Quality Evaluation

### Test Query Categories
```typescript
const TEST_QUERIES = {
  // Metadata-heavy (should favor txtVec)
  metadata: [
    "paintings by van gogh from 1889",
    "japanese woodblock prints edo period", 
    "american impressionist landscapes",
    "ancient greek pottery red figure"
  ],
  
  // Visual-heavy (should favor imgVec)
  visual: [
    "blue landscapes with water",
    "portraits of women in profile",
    "abstract geometric compositions",
    "still life with flowers"
  ],
  
  // Hybrid (should use both)
  hybrid: [
    "french impressionist water lily paintings",
    "renaissance religious paintings with gold",
    "modern abstract sculptures in bronze",
    "asian landscape paintings with mountains"
  ]
};
```

### Success Metrics
- **Relevance**: ≥80% of top-5 results judged relevant
- **Coverage**: Both search types contribute to final results
- **Performance**: P50 < 400ms, P95 < 800ms
- **Diversity**: Results span different time periods/cultures when appropriate

## Error Handling

### Common Error Scenarios
| Error | Cause | HTTP | Response | Mitigation |
|-------|-------|------|----------|------------|
| Empty query | Missing `q` parameter | 400 | "Query required" | Validate input |
| Embedding timeout | API/model slow | 500 | "Search temporarily unavailable" | Retry logic |
| No results | High threshold | 200 | Empty results array | Lower threshold |
| Database error | Connection/index issue | 500 | "Search failed" | Connection pooling |

### Graceful Degradation
```typescript
// Fallback strategies
if (textEmbeddingFails) {
  // Fall back to image-only search
  return performImageOnlySearch(query);
}

if (oneSearchFails) {
  // Continue with single search type
  return performSingleModeSearch(workingResults);
}

if (bothSearchesFail) {
  // Fall back to basic text matching
  return performBasicTextSearch(query);
}
```

## Monitoring & Observability

### Key Metrics
```typescript
// Performance metrics
const METRICS = {
  searchLatency: histogram(['p50', 'p95', 'p99']),
  embeddingLatency: histogram(['text', 'image']),
  rrfFusionTime: histogram(),
  searchVolume: counter(['query_type']),
  errorRate: counter(['error_type']),
  
  // Quality metrics  
  resultCount: histogram(),
  clickThroughRate: gauge(),
  noResultsRate: gauge(),
  
  // Resource metrics
  databaseConnections: gauge(),
  embeddingCacheHits: counter(),
  openaiApiUsage: counter()
};
```

### Logging Strategy
```typescript
// Request logging
console.log({
  query: truncate(query, 100),
  count,
  weights: { wText, wImage },
  timing: {
    textSearch: textSearchTime,
    imageSearch: imageSearchTime, 
    fusion: fusionTime,
    total: totalTime
  },
  results: {
    textContrib: textResults.length,
    imageContrib: imageResults.length,
    finalCount: finalResults.length
  }
});
```

## Security & Rate Limiting

### Input Validation
```typescript
// Query sanitization
const sanitizeQuery = (query: string): string => {
  return query
    .trim()
    .slice(0, 500)  // Max length
    .replace(/[<>]/g, ''); // Basic XSS prevention
};

// Parameter validation
const validateSearchParams = (params: SearchParams) => {
  if (!params.q || params.q.length < 2) {
    throw new ValidationError('Query must be at least 2 characters');
  }
  
  if (params.count && (params.count < 1 || params.count > 100)) {
    throw new ValidationError('Count must be between 1 and 100');
  }
};
```

### Rate Limiting
```typescript
// Per-IP rate limiting
const searchRateLimit = rateLimit({
  windowMs: 60 * 1000,    // 1 minute
  max: 30,                // 30 searches per minute
  message: 'Search rate limit exceeded'
});

// OpenAI API rate limiting
const embeddingQueue = new PQueue({
  concurrency: 10,        // Max concurrent requests
  interval: 1000,         // Per second
  intervalCap: 50         // Max per interval
});
```

## Future Enhancements

### Planned Features
- [ ] **Query expansion**: Automatically expand queries with synonyms
- [ ] **Faceted search**: Filter by date ranges, departments, cultures
- [ ] **Semantic clustering**: Group similar results together
- [ ] **Personalization**: Learn from user interactions
- [ ] **Multi-language**: Support queries in multiple languages

### Advanced RRF Features
- [ ] **Dynamic weights**: Adjust weights based on query type
- [ ] **Query classification**: Route different query types optimally
- [ ] **Result diversity**: Ensure diverse results across cultures/periods
- [ ] **Temporal search**: Specialized handling for date/period queries

### Performance Optimizations
- [ ] **Embedding caching**: Cache frequently used embeddings
- [ ] **Query caching**: Cache popular search results
- [ ] **Approximate search**: Use faster approximate algorithms
- [ ] **Precomputed similarities**: Cache common similarity computations

---

## Implementation Checklist

### Database & Schema
- [ ] Add `txtVec` vector column to artwork table
- [ ] Create HNSW index for text embeddings
- [ ] Test index performance with sample data
- [ ] Create database migration script

### Text Embedding Pipeline
- [ ] Choose embedding model (OpenAI vs local)
- [ ] Implement metadata text combination logic
- [ ] Create embedding generation script
- [ ] Generate embeddings for test dataset
- [ ] Validate embedding quality and coverage

### Search Implementation
- [ ] Create text embedding service wrapper
- [ ] Implement dual vector search functions  
- [ ] Build RRF fusion algorithm
- [ ] Create search API endpoint
- [ ] Add comprehensive error handling
- [ ] Implement request validation and sanitization

### Testing & Optimization
- [ ] Create test query evaluation set
- [ ] Benchmark search performance
- [ ] Tune RRF parameters (k, weights)
- [ ] Load test with concurrent requests
- [ ] Validate search result quality

### Monitoring & Production
- [ ] Add performance metrics and logging
- [ ] Implement rate limiting
- [ ] Create monitoring dashboards
- [ ] Document deployment procedures
- [ ] Set up alerting for failures

---

**Ready for implementation!** This specification provides a complete roadmap for building the RRF text search system that will enable rich, intelligent artwork discovery. 🎨✨
