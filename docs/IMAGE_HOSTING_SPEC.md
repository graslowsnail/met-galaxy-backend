# Met Museum Image Hosting Project Specification

## Project Overview

**Goal**: Download Met Museum images to our own hosting to avoid rate limiting when displaying hundreds of images simultaneously.

**Current State**: 
- PostgreSQL database with 336k+ Met Museum artworks
- Each artwork has `primaryImage` URL pointing to Met's API
- Frontend displays hundreds of images at once, risking API rate limiting

**Target Scale**: 
- Phase 1: 10,000 images
- Phase 2: 336,000 images (full database)

## Database Schema

### Current Table Structure
```sql
-- Assuming table name: artworks
-- Current column with Met's image URL:
primaryImage VARCHAR(500) -- e.g., "https://collectionapi.metmuseum.org/api/collection/v1/iiif/33/13579/restricted"
```

### Required Schema Changes
```sql
-- Add new column for our hosted images
ALTER TABLE artworks ADD COLUMN local_image_url VARCHAR(500);

## Technical Architecture

### Storage Strategy
**Phase 1 (10k images)**: AWS S3 direct URLs
- Simple setup, immediate implementation
- URLs format: `https://your-bucket.s3.amazonaws.com/artworks/{artwork_id}.jpg`

**Phase 2 (336k images)**: AWS S3 + CloudFront CDN
- Better performance, lower costs
- URLs format: `https://d1234.cloudfront.net/artworks/{artwork_id}.jpg`

### Cost Estimates
- **Storage**: ~$0.023/GB/month (S3 Standard)
- **10k images**: ~5GB = $0.115/month
- **336k images**: ~168GB = $3.86/month
- **Bandwidth**: ~$0.09/GB (S3) vs ~$0.085/GB (CloudFront)

## Implementation Plan

### Step 1: AWS Setup
1. Create S3 bucket: `met-artworks-images`
2. Configure bucket for public read access
3. Set up IAM user with S3 permissions
4. Install AWS SDK: `npm install @aws-sdk/client-s3`

### Step 2: Database Migration
```sql
-- Add local image URL column
ALTER TABLE artworks ADD COLUMN local_image_url VARCHAR(500);

-- Add index for performance
CREATE INDEX idx_artworks_local_image_url ON artworks(local_image_url);
```

### Step 3: Image Download Script
Create `scripts/download-images.js`:

```javascript
const { S3Client, PutObjectCommand } = require('@aws-sdk/client-s3');
const fetch = require('node-fetch');
const { Pool } = require('pg');

// Configuration
const BATCH_SIZE = 15; // Conservative batch size
const DELAY_BETWEEN_BATCHES = 4000; // 4 seconds
const RANDOM_DELAY_RANGE = 2000; // 0-2 seconds random delay

// AWS S3 setup
const s3Client = new S3Client({
  region: 'us-east-1',
  credentials: {
    accessKeyId: process.env.AWS_ACCESS_KEY_ID,
    secretAccessKey: process.env.AWS_SECRET_ACCESS_KEY,
  }
});

// Database setup
const pool = new Pool({
  connectionString: process.env.DATABASE_URL,
});

async function downloadImage(url) {
  const response = await fetch(url, {
    headers: {
      'User-Agent': 'Met-Artwork-Downloader/1.0 (Educational Project)',
    }
  });
  
  if (!response.ok) {
    throw new Error(`Failed to download ${url}: ${response.status}`);
  }
  
  return response.buffer();
}

async function uploadToS3(imageBuffer, key) {
  const command = new PutObjectCommand({
    Bucket: 'met-artworks-images',
    Key: key,
    Body: imageBuffer,
    ContentType: 'image/jpeg',
    ACL: 'public-read',
  });
  
  await s3Client.send(command);
  return `https://met-artworks-images.s3.amazonaws.com/${key}`;
}

async function processBatch(artworks) {
  const promises = artworks.map(async (artwork) => {
    try {
      console.log(`Downloading: ${artwork.id} - ${artwork.primaryImage}`);
      
      const imageBuffer = await downloadImage(artwork.primaryImage);
      const s3Key = `artworks/${artwork.id}.jpg`;
      const s3Url = await uploadToS3(imageBuffer, s3Key);
      
      // Update database
      await pool.query(
        'UPDATE artworks SET local_image_url = $1 WHERE id = $2',
        [s3Url, artwork.id]
      );
      
      console.log(`✅ Success: ${artwork.id}`);
      return { success: true, id: artwork.id };
    } catch (error) {
      console.error(`❌ Failed: ${artwork.id} - ${error.message}`);
      return { success: false, id: artwork.id, error: error.message };
    }
  });
  
  return Promise.all(promises);
}

async function main() {
  try {
    // Get artworks without local images
    const result = await pool.query(`
      SELECT id, primaryImage 
      FROM artworks 
      WHERE primaryImage IS NOT NULL 
        AND local_image_url IS NULL
      LIMIT 10000
    `);
    
    const artworks = result.rows;
    console.log(`Found ${artworks.length} artworks to process`);
    
    // Process in batches
    for (let i = 0; i < artworks.length; i += BATCH_SIZE) {
      const batch = artworks.slice(i, i + BATCH_SIZE);
      console.log(`\n--- Processing batch ${Math.floor(i/BATCH_SIZE) + 1}/${Math.ceil(artworks.length/BATCH_SIZE)} ---`);
      
      const results = await processBatch(batch);
      const successes = results.filter(r => r.success).length;
      console.log(`Batch complete: ${successes}/${batch.length} successful`);
      
      // Delay before next batch
      if (i + BATCH_SIZE < artworks.length) {
        const delay = DELAY_BETWEEN_BATCHES + Math.random() * RANDOM_DELAY_RANGE;
        console.log(`Waiting ${Math.round(delay)}ms before next batch...`);
        await new Promise(resolve => setTimeout(resolve, delay));
      }
    }
    
    console.log('\n🎉 Download process complete!');
  } catch (error) {
    console.error('Fatal error:', error);
  } finally {
    await pool.end();
  }
}

main();
```

### Step 4: API Updates
Update your API endpoints to use local URLs:

```typescript
// In your API response logic
const getImageUrl = (artwork: Artwork) => {
  return artwork.local_image_url || artwork.primaryImage;
};

// Update your API response
const apiResponse = {
  ...artwork,
  imageUrl: getImageUrl(artwork), // Use local URL if available
  originalImageUrl: artwork.primaryImage, // Keep original for reference
};
```

### Step 5: Frontend Updates
Update your frontend to use the new image URL:

```typescript
// In your use-artworks hook or component
const imageUrl = artwork.imageUrl || artwork.primaryImage;
```

## Environment Variables

Create `.env` file:
```env
# AWS Configuration
AWS_ACCESS_KEY_ID=your_access_key
AWS_SECRET_ACCESS_KEY=your_secret_key
AWS_REGION=us-east-1

# Database
DATABASE_URL=postgresql://user:password@localhost:5432/met_database

# S3 Bucket
S3_BUCKET_NAME=met-artworks-images
```

## Rate Limiting Strategy

### Conservative Approach
- **Batch size**: 15 images per batch
- **Delay between batches**: 4 seconds
- **Random delay**: 0-2 seconds additional
- **User-Agent**: Respectful identification
- **Error handling**: Stop on 429 responses

### Monitoring
- Log all download attempts
- Track success/failure rates
- Monitor for rate limit responses
- Implement exponential backoff on errors

## Testing Strategy

### Phase 1: Small Scale Test
1. Test with 10 images first
2. Verify S3 uploads work
3. Verify database updates work
4. Test API response changes

### Phase 2: Medium Scale Test
1. Test with 100 images
2. Monitor rate limiting
3. Verify performance

### Phase 3: Full Scale
1. Process 10k images
2. Monitor costs and performance
3. Plan for 336k scale

## Future Enhancements

### Phase 2 Optimizations
1. **Add CloudFront CDN** for better performance
2. **Image optimization**: Resize to common display sizes
3. **WebP conversion**: Reduce file sizes
4. **Multiple sizes**: Thumbnail, medium, large variants

### Monitoring & Maintenance
1. **Health checks**: Verify image availability
2. **Cost monitoring**: Track S3 usage
3. **Error recovery**: Re-download failed images
4. **New artwork sync**: Process new additions

## File Structure

```
project/
├── scripts/
│   ├── download-images.js
│   ├── setup-s3.js
│   └── verify-images.js
├── src/
│   ├── api/
│   │   └── artworks.ts (updated)
│   └── utils/
│       └── image-urls.ts
├── .env
└── IMAGE_HOSTING_SPEC.md
```

## Success Criteria

- [ ] S3 bucket created and configured
- [ ] Database schema updated
- [ ] Download script processes 10k images successfully
- [ ] API returns local image URLs
- [ ] Frontend displays images from local hosting
- [ ] No rate limiting issues during normal usage
- [ ] Cost monitoring in place

## Notes

- Start conservative with rate limiting
- Monitor Met's API response headers for rate limit info
- Keep original Met URLs as fallback
- Consider implementing retry logic for failed downloads
- Document any changes to Met's API structure
