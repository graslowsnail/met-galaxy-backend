# Field Chunk API - Implementation Specification

**Status:** ✅ Implemented  
**Version:** 1.0.0  
**Date:** 2025-08-22

## Overview

The Field Chunk API provides a deterministic, directional similarity system that returns artwork chunks as a pure function of `(focalId, chunkX, chunkY)`. The system creates a "field" around each artwork where:

- **Near the center** (small radius): Results are strongly similar to the focal artwork
- **Farther out**: Results incorporate drift and randomness
- **Direction matters**: Movement in +X/−X/+Y/−Y directions follows PCA-encoded semantic directions

## Architecture

### Stack
- **Backend**: Node.js + Express + TypeScript
- **ORM**: Drizzle
- **Database**: PostgreSQL with pgvector extension
- **ML**: Python with scikit-learn for PCA computation
- **Embeddings**: CLIP ViT-L/14 (768-dimensional, L2 normalized)

### Core Components

#### 1. Vector Operations Library (`src/lib/fieldVectors.ts`)
```typescript
// Key functions
hash32(...nums: number[])           // Deterministic hashing
mulberry32(seed: number)            // Seeded PRNG
gaussian(rng: () => number)         // Gaussian noise generation
pcaDirectionalBias(theta, t)        // PCA-based directional bias
normalize(v: Float32Array)          // Vector normalization
smoothstep(edge0, edge1, x)         // Smooth interpolation
```

#### 2. Field Chunk Route (`src/routes/fieldChunk.ts`)
```typescript
GET /api/artworks/field-chunk
```

#### 3. PCA Basis Generator (`scripts/pca_build.py`)
- Processes embeddings in batches using IncrementalPCA
- Generates 4 PCA components 
- Saves normalized basis to `pca_basis.json`

## API Specification

### Endpoint
```
GET /api/artworks/field-chunk
```

### Parameters
| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `targetId` | integer | Yes | ID of focal artwork |
| `chunkX` | integer | Yes | X coordinate in field |
| `chunkY` | integer | Yes | Y coordinate in field |
| `count` | integer | No | Items to return (1-50, default: 20) |
| `seed` | integer | No | Global seed override (default: 0) |
| `exclude` | string | No | Comma-separated IDs to exclude |

### Response Format
```json
{
  "success": true,
  "meta": {
    "targetId": 123,
    "chunk": { "x": -3, "y": 8 },
    "r": 8.54,
    "theta": 1.93,
    "t": 0.62,
    "weights": {
      "sim": 0.145,
      "drift": 0.471,
      "rand": 0.384
    },
    "seed": 41739219
  },
  "data": [
    {
      "id": 9876,
      "objectId": 777,
      "title": "Artwork Title",
      "artist": "Artist Name",
      "imageUrl": "s3://...",
      "originalImageUrl": "https://...",
      "imageSource": "s3|met_small|met_original",
      "similarity": 0.71,
      "source": "drift"
    }
  ],
  "responseTime": "217ms"
}
```

## Algorithm Details

### Field Coordinates
```typescript
const r = Math.hypot(chunkX, chunkY)        // Radius from center
const theta = Math.atan2(chunkY, chunkX)    // Angle
const t = smoothstep(1.5, 12.0, r)         // Temperature (0=sim, 1=rand)
```

### Deterministic Sampling
```typescript
const seed = hash32(targetId, chunkX, chunkY, globalSeed)
const rng = mulberry32(seed)
```

### Vector Transformation
```typescript
// Original embedding
v = normalize(target.imgVec)

// Add directional bias from PCA
bias = pcaDirectionalBias(theta, t)  // α(t) * dir(θ)
alpha = lerp(0.0, 0.35, t)

// Add Gaussian noise
sigma = lerp(0.05, 0.35, t)
eps = gaussianVector(dimension, rng)

// Final query vector
v' = normalize(v + bias + sigma * eps)
```

### Sampling Pools
| Pool | Size | Query Vector | Purpose |
|------|------|--------------|---------|
| **SIM_TIGHT** | 200 | `v` (original) | High similarity matches |
| **SIM_DRIFT** | 400 | `v'` (drifted) | Directionally biased matches |
| **RAND** | 800 | N/A | Random artworks |

### Pool Weights
```typescript
const wSim = (1 - t)²               // High at center
const wDrift = 2 * t * (1 - t)      // Peak at medium distance  
const wRand = t²                    // High at periphery
```

### Selection Process
1. Normalize weights: `pSim = wSim/sum`, `pDrift = wDrift/sum`
2. For each item needed:
   - Generate `u = rng()`
   - If `u < pSim`: choose from SIM_TIGHT
   - Else if `u < pSim + pDrift`: choose from SIM_DRIFT  
   - Else: choose from RAND
3. Fallback to other pools if selected pool is exhausted

## Database Schema

### Tables
```sql
-- Existing artwork table
"met-galaxy_artwork" (
  id integer PRIMARY KEY,
  objectId integer NOT NULL,
  title text,
  artist text,
  localImageUrl varchar(1000),    -- S3 URLs preferred
  primaryImage varchar(1000),     -- Met museum URLs
  primaryImageSmall varchar(1000),
  imgVec vector(768),             -- CLIP embeddings
  -- ... other fields
)
```

### Indexes
```sql
-- HNSW index for fast similarity queries
CREATE INDEX idx_artworks_imgvec_hnsw 
ON "met-galaxy_artwork" USING hnsw ("imgVec" vector_cosine_ops);
```

## File Structure

```
src/
├── lib/
│   └── fieldVectors.ts          # Vector operations & PCA loading
├── routes/
│   ├── artworks.ts              # Existing routes
│   └── fieldChunk.ts            # Field chunk endpoint
└── index.ts                     # Server with PCA basis loading

scripts/
├── pca_build.py                 # PCA basis generation
└── requirements.txt             # Python dependencies

drizzle/
└── 0002_add_pgvector_index.sql  # Database index migration

docs/
└── FIELD_CHUNK_SPEC.md          # This specification

test_field_chunk.js              # Test script
pca_basis.json                   # Generated PCA basis (created by script)
```

## Configuration

### Tuning Constants
```typescript
// Temperature ramp
const r0 = 1.5, r1 = 12.0

// Bias strength  
const alphaMin = 0.0, alphaMax = 0.35

// Noise scale
const sigmaMin = 0.05, sigmaMax = 0.35

// Pool sizes
const SIM_TIGHT_SIZE = 200
const SIM_DRIFT_SIZE = 400  
const RAND_SIZE = 800

// Response limits
const MAX_COUNT = 50
```

### Environment Variables
```bash
# Optional overrides
PCA_BASIS_PATH="./pca_basis.json"
FIELD_POOL_SIZES='{"sim": 200, "drift": 400, "rand": 800}'
```

## Setup & Deployment

### 1. Prerequisites
- PostgreSQL with pgvector extension
- Node.js 18+
- Python 3.8+ with venv
- Existing artwork data with embeddings

### 2. Installation
```bash
# Install Python dependencies
pip install -r scripts/requirements.txt

# Generate PCA basis (run once)
python scripts/pca_build.py

# Apply database index
psql -d your_db -f drizzle/0002_add_pgvector_index.sql

# Start server
npm run dev
```

### 3. Verification
```bash
# Test endpoint
node test_field_chunk.js

# Manual test
curl "http://localhost:8080/api/artworks/field-chunk?targetId=123&chunkX=2&chunkY=-3&count=10"
```

## Performance Characteristics

### Expected Response Times
- **Cold start**: 200-500ms (first query after restart)
- **Warm queries**: 50-200ms (with proper indexing)
- **Heavy load**: Scales with database connection pool

### Memory Usage
- **PCA basis**: ~24KB for 768d × 4 components
- **Per request**: ~50KB peak (vector operations)

### Determinism Guarantees
- ✅ Same `(targetId, chunkX, chunkY, seed)` → identical results
- ✅ Cross-session consistency (seeded PRNG)
- ✅ Database order independence (secondary sort by ID)

## Testing

### Unit Tests (Recommended)
- Hash function determinism: `hash32(1,2,3) === hash32(1,2,3)`
- RNG reproducibility: `mulberry32(42)` sequence
- Vector operations: normalization, addition, scaling
- Smoothstep function: edge cases and monotonicity

### Integration Tests
```javascript
// Fixed seed test
const response1 = await fetch('...&seed=42');
const response2 = await fetch('...&seed=42');
assert.deepEqual(response1.data, response2.data);

// Distance effect test  
const center = await fetch('...&chunkX=0&chunkY=0');
const far = await fetch('...&chunkX=10&chunkY=10');
assert(center.meta.t < far.meta.t);
```

### Load Testing
- Target: 100 concurrent requests
- Monitor: Database connection pool, memory usage
- Optimize: Index warm-up, connection pooling

## Security & Limits

### Input Validation
- ✅ Integer parameter parsing
- ✅ Count clamping (1-50)
- ✅ SQL injection prevention (parameterized queries)

### Rate Limiting (Recommended)
```javascript
// Per IP: 60 requests per minute
// Per route: /field-chunk specific limits
```

### CORS Configuration
- ✅ Configured for development/production origins
- ✅ Credentials support for authenticated requests

## Monitoring & Observability

### Logging
```javascript
// Request logs include:
console.log({
  targetId, chunkX, chunkY, r, theta, t,
  poolCounts: { sim: simTight.length, drift: simDrift.length, rand: randPool.length },
  responseTime: `${Date.now() - start}ms`
});
```

### Metrics (Recommended)
- Request latency (p50, p95, p99)
- Pool utilization rates  
- Error rates by error type
- PCA basis load success/failure

## Error Handling

### Common Error Cases
| Error | HTTP | Cause | Solution |
|-------|------|-------|----------|
| Bad params | 400 | Missing/invalid targetId, chunkX, chunkY | Validate client input |
| Target not found | 404 | Invalid targetId or missing embedding | Check artwork exists |
| PCA basis missing | 500 | pca_basis.json not found | Run pca_build.py |
| Database error | 500 | Connection/query failure | Check DB health |

### Graceful Degradation
- Missing PCA basis → Warning logged, endpoint returns 500
- Empty pools → Fallback to other pools
- Partial results → Return available items vs. failing completely

## Future Enhancements

### Planned Features
- [ ] Multiple PCA basis support (per-category)
- [ ] Dynamic pool size adjustment
- [ ] Caching layer for popular chunks
- [ ] WebSocket streaming for large results

### Performance Optimizations
- [ ] Precomputed similarity matrices
- [ ] GPU-accelerated vector operations
- [ ] Distributed embedding storage
- [ ] Query result caching with Redis

### Algorithm Improvements
- [ ] Adaptive temperature curves
- [ ] Multi-scale directional encoding  
- [ ] Learned similarity metrics
- [ ] User preference integration

---

## Contact & Support

For questions about this implementation, see:
- Code documentation in source files
- Test script: `test_field_chunk.js`
- Original specification: Design document provided