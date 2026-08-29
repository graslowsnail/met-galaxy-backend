const { S3Client, PutObjectCommand } = require('@aws-sdk/client-s3');
const { Client } = require('pg');
require('dotenv').config();

// Optimized configuration after finding rate limits
const BATCH_SIZE = 25; // Reduced from 50 to avoid rate limiting
const DELAY_BETWEEN_BATCHES = 3000; // 3 seconds (vs our original 1 second)
const RANDOM_DELAY_RANGE = 1000; // 0-1 second random delay
const MAX_CONCURRENT = 5; // Reduced concurrent downloads
const AWS_REGION = process.env.AWS_REGION || 'us-east-1';
const S3_BUCKET_NAME = process.env.S3_BUCKET_NAME || 'met-artworks-images';
const IMAGE_CDN_BASE_URL = (
  process.env.IMAGE_CDN_BASE_URL || 'https://d2pvxr3eb77vb4.cloudfront.net'
).replace(/\/+$/, '');

// AWS S3 setup
const s3Client = new S3Client({
  region: AWS_REGION,
});

// Database setup
const dbClient = new Client({
  connectionString: process.env.DATABASE_URL,
});

async function downloadImage(url) {
  const response = await fetch(url, {
    headers: {
      'User-Agent': 'Met-Galaxy-Downloader/1.0 (Educational Project)',
    }
  });
  
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}: ${response.statusText}`);
  }
  
  return response.arrayBuffer();
}

async function uploadToS3(imageBuffer, key) {
  const command = new PutObjectCommand({
    Bucket: S3_BUCKET_NAME,
    Key: key,
    Body: imageBuffer,
    ContentType: 'image/jpeg',
    CacheControl: 'public, max-age=31536000, immutable',
  });
  
  await s3Client.send(command);
  return `${IMAGE_CDN_BASE_URL}/${key}`;
}

async function processImage(artwork) {
  try {
    console.log(`📥 Downloading: ${artwork.id} - ${artwork.primaryImage}`);
    
    const imageBuffer = await downloadImage(artwork.primaryImage);
    const s3Key = `artworks/${artwork.id}.jpg`;
    const s3Url = await uploadToS3(imageBuffer, s3Key);
    
    // Update database
    await dbClient.query(
      'UPDATE "met-galaxy_artwork" SET "localImageUrl" = $1 WHERE id = $2',
      [s3Url, artwork.id]
    );
    
    console.log(`✅ Success: ${artwork.id} -> ${s3Key}`);
    return { success: true, id: artwork.id, url: s3Url };
  } catch (error) {
    console.error(`❌ Failed: ${artwork.id} - ${error.message}`);
    return { success: false, id: artwork.id, error: error.message };
  }
}

async function processBatch(artworks) {
  // Process multiple images concurrently within the batch
  const chunks = [];
  for (let i = 0; i < artworks.length; i += MAX_CONCURRENT) {
    chunks.push(artworks.slice(i, i + MAX_CONCURRENT));
  }
  
  const allResults = [];
  for (const chunk of chunks) {
    const promises = chunk.map(artwork => processImage(artwork));
    const results = await Promise.all(promises);
    allResults.push(...results);
  }
  
  return allResults;
}

async function main() {
  try {
    // Connect to database
    await dbClient.connect();
    console.log('🔌 Connected to database');
    
    // Get artworks without local images
    const result = await dbClient.query(`
      SELECT id, "primaryImage" 
      FROM "met-galaxy_artwork" 
      WHERE "primaryImage" IS NOT NULL 
        AND "primaryImage" != ''
        AND ("localImageUrl" IS NULL OR "localImageUrl" = '')
      ORDER BY id
      LIMIT 10000
    `);
    
    const artworks = result.rows;
    console.log(`🎯 Found ${artworks.length} artworks to process`);
    
    if (artworks.length === 0) {
      console.log('🎉 No artworks to process - all done!');
      return;
    }
    
    let totalSuccesses = 0;
    let totalFailures = 0;
    
    // Process in batches
    for (let i = 0; i < artworks.length; i += BATCH_SIZE) {
      const batch = artworks.slice(i, i + BATCH_SIZE);
      const batchNum = Math.floor(i/BATCH_SIZE) + 1;
      const totalBatches = Math.ceil(artworks.length/BATCH_SIZE);
      
      console.log(`\n🚀 Processing batch ${batchNum}/${totalBatches} (${batch.length} images)`);
      
      const startTime = Date.now();
      const results = await processBatch(batch);
      const endTime = Date.now();
      
      const successes = results.filter(r => r.success).length;
      const failures = results.filter(r => !r.success).length;
      
      totalSuccesses += successes;
      totalFailures += failures;
      
      const duration = (endTime - startTime) / 1000;
      const rate = batch.length / duration;
      
      console.log(`📊 Batch ${batchNum} complete: ${successes}/${batch.length} successful (${rate.toFixed(1)} images/sec)`);
      console.log(`📈 Total progress: ${totalSuccesses} successes, ${totalFailures} failures`);
      
      // Delay before next batch (unless it's the last one)
      if (i + BATCH_SIZE < artworks.length) {
        const delay = DELAY_BETWEEN_BATCHES + Math.random() * RANDOM_DELAY_RANGE;
        console.log(`⏳ Waiting ${Math.round(delay)}ms before next batch...`);
        await new Promise(resolve => setTimeout(resolve, delay));
      }
    }
    
    console.log(`\n🎉 Download process complete!`);
    console.log(`📊 Final stats: ${totalSuccesses} successes, ${totalFailures} failures`);
    console.log(`💯 Success rate: ${((totalSuccesses / (totalSuccesses + totalFailures)) * 100).toFixed(1)}%`);
    
  } catch (error) {
    console.error('💥 Fatal error:', error);
  } finally {
    await dbClient.end();
    console.log('🔌 Database connection closed');
  }
}

// Handle graceful shutdown
process.on('SIGINT', async () => {
  console.log('\n⚠️  Received SIGINT, shutting down gracefully...');
  await dbClient.end();
  process.exit(0);
});

console.log('🚀 Starting Met Museum image download script...');
console.log(`⚙️  Config: ${BATCH_SIZE} images/batch, ${DELAY_BETWEEN_BATCHES}ms delay, ${MAX_CONCURRENT} concurrent per batch`);
main();
