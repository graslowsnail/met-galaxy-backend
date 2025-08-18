# Met Gallery Backend API Specification

## Base URL
```
http://localhost:8080/api
```

## Authentication
None required for current endpoints.

---

## Artworks Endpoints

### GET /artworks/random
Get a random selection of artworks optimized for the draggable grid.

**URL:** `/api/artworks/random`

**Method:** `GET`

**Query Parameters:**
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `count` | integer | 200 | Number of artworks to return (1-500) |
| `seed` | integer | random | Seed for deterministic randomization |
| `withImages` | boolean | true | Only return artworks with available images |

**Response Format:**
```json
{
  "success": true,
  "data": [
    {
      "id": 1,
      "objectId": 123456,
      "title": "The Starry Night",
      "artist": "Vincent van Gogh",
      "date": "1889",
      "medium": "Oil on canvas",
      "department": "European Paintings",
      "culture": "Dutch",
      "imageUrl": "https://met-artworks-images.s3.amazonaws.com/artworks/1.jpg",
      "originalImageUrl": "https://collectionapi.metmuseum.org/api/collection/v1/iiif/436532/main-image",
      "imageSource": "s3", // "s3" | "met_small" | "met_original"
      "objectUrl": "https://www.metmuseum.org/art/collection/search/436532",
      "isHighlight": false,
      "hasEmbedding": true
    }
  ],
  "meta": {
    "count": 200,
    "seed": 12345,
    "totalAvailable": 20000,
    "withS3Images": 5000,
    "responseTime": "45ms"
  }
}
```

**Example Requests:**
```bash
# Get 200 random artworks
GET /api/artworks/random

# Get 50 artworks with specific seed for consistency
GET /api/artworks/random?count=50&seed=12345

# Get artworks (may include ones without images)
GET /api/artworks/random?count=100&withImages=false
```

**Error Responses:**
```json
{
  "success": false,
  "error": "Invalid count parameter",
  "message": "Count must be between 1 and 500"
}
```

---

### GET /artworks/similar/:id
Find artworks visually similar to a given artwork using CLIP embeddings.

**URL:** `/api/artworks/similar/:id`

**Method:** `GET`

**Path Parameters:**
| Parameter | Type | Description |
|-----------|------|-------------|
| `id` | integer | ID of the artwork to find similar images for |

**Query Parameters:**
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `count` | integer | 20 | Number of similar artworks to return (1-100) |
| `threshold` | float | 0.8 | Similarity threshold (0.0-1.0, higher = more similar) |

**Response Format:**
```json
{
  "success": true,
  "data": {
    "sourceArtwork": {
      "id": 1,
      "title": "The Starry Night",
      "artist": "Vincent van Gogh",
      "imageUrl": "https://met-artworks-images.s3.amazonaws.com/artworks/1.jpg"
    },
    "similarArtworks": [
      {
        "id": 156,
        "title": "Wheatfield with Cypresses",
        "artist": "Vincent van Gogh",
        "imageUrl": "https://met-artworks-images.s3.amazonaws.com/artworks/156.jpg",
        "similarity": 0.92,
        "distance": 0.08
      }
    ]
  },
  "meta": {
    "count": 20,
    "threshold": 0.8,
    "responseTime": "120ms"
  }
}
```

**Example Requests:**
```bash
# Find 20 similar artworks
GET /api/artworks/similar/1

# Find 10 very similar artworks
GET /api/artworks/similar/1?count=10&threshold=0.9
```

---

### GET /artworks/search
Text-based search using CLIP text tower for text-to-image search.

**URL:** `/api/artworks/search`

**Method:** `GET`

**Query Parameters:**
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `q` | string | required | Search query text |
| `count` | integer | 50 | Number of results to return (1-100) |
| `threshold` | float | 0.3 | Minimum similarity threshold |

**Response Format:**
```json
{
  "success": true,
  "data": [
    {
      "id": 234,
      "title": "Landscape with Trees",
      "artist": "Claude Monet", 
      "imageUrl": "https://met-artworks-images.s3.amazonaws.com/artworks/234.jpg",
      "similarity": 0.87,
      "matchReason": "text_to_image"
    }
  ],
  "meta": {
    "query": "landscape painting with trees",
    "count": 50,
    "threshold": 0.3,
    "responseTime": "180ms"
  }
}
```

---

## Response Patterns

### Success Response
All successful API calls return:
```json
{
  "success": true,
  "data": <response_data>,
  "meta": <metadata_object>
}
```

### Error Response
All error responses return:
```json
{
  "success": false,
  "error": "<error_type>",
  "message": "<human_readable_message>"
}
```

### Common HTTP Status Codes
- `200` - Success
- `400` - Bad Request (invalid parameters)
- `404` - Not Found (artwork ID doesn't exist)
- `500` - Internal Server Error

---

## Image URL Priority Logic

The API returns images in this priority order:
1. **S3 hosted image** (`localImageUrl`) - fastest, no rate limits
2. **Met Museum small image** (`primaryImageSmall`) - smaller, faster
3. **Met Museum original** (`primaryImage`) - highest quality, may be rate limited

The `imageSource` field indicates which source was used:
- `"s3"` - Our S3 hosted image
- `"met_small"` - Met Museum small image  
- `"met_original"` - Met Museum original image

---

## Performance Notes

- **Random endpoint**: Optimized for fast response (<100ms for 200 items)
- **Similar endpoint**: Uses pgvector for fast similarity search (<200ms)
- **Search endpoint**: Text-to-image search, slower but powerful (<500ms)
- **Caching**: Responses cached for 5 minutes for repeated requests
- **Rate limiting**: 100 requests per minute per IP

---

## Frontend Integration Examples

### React Hook Usage
```typescript
// Random artworks for grid
const { data, isLoading } = useQuery({
  queryKey: ['artworks', 'random', { count: 200, seed: 12345 }],
  queryFn: () => fetch('/api/artworks/random?count=200&seed=12345').then(r => r.json())
})

// Similar artworks
const { data: similarData } = useQuery({
  queryKey: ['artworks', 'similar', artworkId],
  queryFn: () => fetch(`/api/artworks/similar/${artworkId}?count=20`).then(r => r.json())
})
```

### Grid Integration
```typescript
// Use in your draggable grid
const imageUrl = artwork.imageUrl // Already prioritized S3 > Met
const isS3Hosted = artwork.imageSource === 's3' // For analytics
```
