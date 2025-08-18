# Similarity API - Frontend Integration Spec

## Endpoint
```
GET /api/artworks/similar/:id
```

## Purpose
Get 50 visually similar artworks to a clicked artwork using CLIP embeddings. Perfect for "similar artworks" grid view.

## Parameters
- **`:id`** (required) - The artwork ID that was clicked
- **No query parameters** - Always returns exactly 50 results

## Request Examples
```typescript
// When user clicks artwork with ID 123
const response = await fetch('/api/artworks/similar/123');
const data = await response.json();
```

## Response Structure
```typescript
interface SimilarityResponse {
  success: boolean;
  data: SimilarArtwork[];
  meta: {
    targetId: number;
    targetTitle: string;
    targetArtist: string;
    count: number;
    responseTime: string;
  };
}

interface SimilarArtwork {
  id: number;
  objectId: number;
  title: string;
  artist: string;
  imageUrl: string;           // S3 URL, ready to use
  originalImageUrl: string;   // Met Museum URL (for reference)
  imageSource: "s3";          // Always "s3" (we filter for S3 only)
  original: boolean;          // true for the clicked artwork, false for similar ones
  similarity: number;         // 0-1, higher = more similar
}
```

## Sample Response
```json
{
  "success": true,
  "data": [
    {
      "id": 123,
      "objectId": 45678,
      "title": "Starry Night",
      "artist": "Vincent van Gogh",
      "imageUrl": "https://met-artworks-images.s3.amazonaws.com/artworks/123.jpg",
      "originalImageUrl": "https://images.metmuseum.org/CRDImages/ep/original/DT1975.jpg",
      "imageSource": "s3",
      "original": true,
      "similarity": 1.0
    },
    {
      "id": 124,
      "objectId": 45679,
      "title": "Wheatfield with Cypresses",
      "artist": "Vincent van Gogh",
      "imageUrl": "https://met-artworks-images.s3.amazonaws.com/artworks/124.jpg",
      "originalImageUrl": "https://images.metmuseum.org/CRDImages/ep/original/DT1567.jpg",
      "imageSource": "s3",
      "original": false,
      "similarity": 0.94
    }
  ],
  "meta": {
    "targetId": 123,
    "targetTitle": "Starry Night",
    "targetArtist": "Vincent van Gogh",
    "count": 50,
    "responseTime": "150ms"
  }
}
```

## Error Responses

### 400 - Invalid ID
```json
{
  "success": false,
  "error": "Invalid artwork ID"
}
```

### 404 - Not Found
```json
{
  "success": false,
  "error": "Artwork not found or missing S3 image/embedding"
}
```

### 500 - Server Error
```json
{
  "success": false,
  "error": "Failed to fetch similar artworks",
  "message": "Database connection timeout"
}
```

## Frontend Usage

### React Hook
```typescript
function useSimilarArtworks(artworkId: number | null) {
  return useQuery({
    queryKey: ['artworks', 'similar', artworkId],
    queryFn: async () => {
      if (!artworkId) return null;
      const response = await fetch(`/api/artworks/similar/${artworkId}`);
      if (!response.ok) throw new Error('Failed to fetch similar artworks');
      return response.json() as SimilarityResponse;
    },
    enabled: !!artworkId,
    staleTime: 5 * 60 * 1000, // 5 minutes
  });
}
```

### Grid Implementation
```typescript
function SimilarityGrid({ clickedArtworkId }: { clickedArtworkId: number }) {
  const { data, isLoading, error } = useSimilarArtworks(clickedArtworkId);
  
  if (isLoading) return <LoadingSpinner />;
  if (error) return <ErrorMessage />;
  if (!data?.success) return <ErrorMessage message={data?.error} />;

  // Find the original artwork for center display
  const originalArtwork = data.data.find(artwork => artwork.original);
  const similarArtworks = data.data.filter(artwork => !artwork.original);

  return (
    <div className="similarity-grid">
      {/* Center: Original artwork */}
      <div className="original-artwork">
        <img src={originalArtwork?.imageUrl} alt={originalArtwork?.title} />
        <div className="label">You clicked this</div>
      </div>
      
      {/* Around: Similar artworks */}
      <div className="similar-artworks">
        {similarArtworks.map(artwork => (
          <div key={artwork.id} className="artwork-item">
            <img src={artwork.imageUrl} alt={artwork.title} />
            <div className="similarity">{(artwork.similarity * 100).toFixed(0)}% similar</div>
          </div>
        ))}
      </div>
    </div>
  );
}
```

### State Management
```typescript
// In your main grid component
const [selectedArtworkId, setSelectedArtworkId] = useState<number | null>(null);
const [showSimilarity, setShowSimilarity] = useState(false);

// When user clicks an artwork
function handleArtworkClick(artworkId: number) {
  setSelectedArtworkId(artworkId);
  setShowSimilarity(true);
}

// Render similarity view
{showSimilarity && selectedArtworkId && (
  <SimilarityGrid 
    clickedArtworkId={selectedArtworkId}
    onClose={() => setShowSimilarity(false)}
  />
)}
```

## Performance Notes
- **Expected response time**: 100-200ms
- **Always 50 results**: No pagination needed
- **Cache-friendly**: Same ID always returns same results
- **S3 images only**: No rate limiting concerns

## Visual Similarity Quality
- **High quality**: CLIP ViT-L/14 embeddings (768 dimensions)
- **Semantic understanding**: Finds artworks with similar:
  - Colors and composition
  - Subject matter and style
  - Artistic techniques
  - Visual elements and mood

## Error Handling Tips
```typescript
// Always check success field
if (!response.success) {
  console.error('API Error:', response.error);
  // Show user-friendly message
  return;
}

// Handle missing embeddings gracefully
if (response.error?.includes('missing S3 image/embedding')) {
  // This artwork doesn't have similarity search available
  showMessage('Similarity search not available for this artwork');
}
```

---

**Ready to integrate!** This endpoint will power your similarity view perfectly. 🎨✨
