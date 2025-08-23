import { Router } from 'express';
import { db } from '../db/index.js';
import { artworks } from '../db/schema.js';
import { sql } from 'drizzle-orm';

const router = Router();

// Helper function to determine image URL priority
const getImageUrl = (artwork: any) => {
  if (artwork.localImageUrl) return artwork.localImageUrl;
  if (artwork.primaryImageSmall) return artwork.primaryImageSmall;
  if (artwork.primaryImage) return artwork.primaryImage;
  return null;
};

// Helper function to determine image source
const getImageSource = (artwork: any) => {
  if (artwork.localImageUrl) return 's3';
  if (artwork.primaryImageSmall) return 'met_small';
  if (artwork.primaryImage) return 'met_original';
  return null;
};

// GET /api/artworks/random - Optimized random artworks for grid with STABLE ordering
router.get('/random', async (req, res) => {
  const startTime = Date.now();
  
  try {
    console.log('Starting /random endpoint...');
    
    // Parse and validate query parameters
    const count = Math.min(Math.max(parseInt(req.query.count as string) || 5, 1), 500);
    const seed = parseInt(req.query.seed as string) || Math.floor(Math.random() * 1000000);
    const withImages = req.query.withImages !== 'false'; // Default true
    
    console.log(`Requesting ${count} artworks with seed ${seed}`);

    // Use seed for consistent randomization - set seed first, then random order
    await db.execute(sql`SELECT setseed(${seed / 1000000.0})`);
    
    const result = await db
      .select({
        id: artworks.id,
        objectId: artworks.objectId,
        title: artworks.title,
        artist: artworks.artist,
        localImageUrl: artworks.localImageUrl,
        primaryImage: artworks.primaryImage,
        primaryImageSmall: artworks.primaryImageSmall,
      })
      .from(artworks)
      .where(sql`"localImageUrl" IS NOT NULL AND "localImageUrl" != '' AND "imgVec" IS NOT NULL`)
      // CRITICAL FIX: Add deterministic secondary ordering by ID to ensure stable order
      .orderBy(sql`RANDOM(), id ASC`)
      .limit(count);

    console.log(`Database query returned ${result.length} results`);

    // Transform the data to match API spec
    const transformedData = result.map(artwork => ({
      id: artwork.id,
      objectId: artwork.objectId,
      title: artwork.title,
      artist: artwork.artist,
      imageUrl: getImageUrl(artwork),
      originalImageUrl: artwork.primaryImage,
      imageSource: getImageSource(artwork),
      hasEmbedding: true, // Always true since we filter for imgVec
    }));

    const responseTime = Date.now() - startTime;

    res.json({
      success: true,
      data: transformedData,
      meta: {
        count: transformedData.length,
        seed: seed,
        responseTime: `${responseTime}ms`
      }
    });

  } catch (error) {
    console.error('Error fetching random artworks:', error);
    res.status(500).json({
      success: false,
      error: 'Failed to fetch random artworks',
      message: error instanceof Error ? error.message : 'Unknown error'
    });
  }
});

// GET /api/artworks/similar/:id - Find similar artworks using CLIP embeddings
router.get('/similar/:id', async (req, res) => {
  const startTime = Date.now();
  
  try {
    console.log('Starting /similar endpoint...');
    
    const artworkId = parseInt(req.params.id);
    const count = 160; // Fixed at 50 as requested
    
    if (!artworkId || artworkId <= 0) {
      return res.status(400).json({
        success: false,
        error: 'Invalid artwork ID'
      });
    }
    
    console.log(`Finding ${count} similar artworks to ID ${artworkId}`);

    // First, get the target artwork and its embedding
    const targetArtwork = await db
      .select({
        id: artworks.id,
        objectId: artworks.objectId,
        title: artworks.title,
        artist: artworks.artist,
        localImageUrl: artworks.localImageUrl,
        primaryImage: artworks.primaryImage,
        primaryImageSmall: artworks.primaryImageSmall,
        imgVec: artworks.imgVec,
      })
      .from(artworks)
      .where(sql`id = ${artworkId} AND "localImageUrl" IS NOT NULL AND "localImageUrl" != '' AND "imgVec" IS NOT NULL`)
      .limit(1);

    if (targetArtwork.length === 0) {
      return res.status(404).json({
        success: false,
        error: 'Artwork not found or missing S3 image/embedding'
      });
    }

    const target = targetArtwork[0];
    console.log(`Target artwork found: "${target.title}" by ${target.artist}`);

    // Convert the target embedding to a string format for PostgreSQL
    const targetVectorString = `[${target.imgVec!.join(',')}]`;
    console.log(`Target vector length: ${target.imgVec!.length}`);

    // Find similar artworks using cosine similarity
    const similarArtworks = await db
      .select({
        id: artworks.id,
        objectId: artworks.objectId,
        title: artworks.title,
        artist: artworks.artist,
        localImageUrl: artworks.localImageUrl,
        primaryImage: artworks.primaryImage,
        primaryImageSmall: artworks.primaryImageSmall,
        similarity: sql<number>`1 - ("imgVec" <=> ${targetVectorString}::vector)`.as('similarity'),
      })
      .from(artworks)
      .where(sql`"localImageUrl" IS NOT NULL AND "localImageUrl" != '' AND "imgVec" IS NOT NULL`)
      .orderBy(sql`"imgVec" <=> ${targetVectorString}::vector`)
      .limit(count);

    console.log(`Database query returned ${similarArtworks.length} results`);

    // Transform the data and mark the original
    const transformedData = similarArtworks.map(artwork => ({
      id: artwork.id,
      objectId: artwork.objectId,
      title: artwork.title,
      artist: artwork.artist,
      imageUrl: getImageUrl(artwork),
      originalImageUrl: artwork.primaryImage,
      imageSource: getImageSource(artwork),
      original: artwork.id === artworkId,
      similarity: artwork.similarity,
    }));

    const responseTime = Date.now() - startTime;

    res.json({
      success: true,
      data: transformedData,
      meta: {
        targetId: artworkId,
        targetTitle: target.title,
        targetArtist: target.artist,
        count: transformedData.length,
        responseTime: `${responseTime}ms`
      }
    });

  } catch (error) {
    console.error('Error fetching similar artworks:', error);
    res.status(500).json({
      success: false,
      error: 'Failed to fetch similar artworks',
      message: error instanceof Error ? error.message : 'Unknown error'
    });
  }
});

// GET /api/artworks - Legacy endpoint (kept for backwards compatibility)
router.get('/', async (req, res) => {
  try {
    const result = await db
      .select()
      .from(artworks)
      .limit(10);

    res.json({
      success: true,
      data: result,
      message: 'Artworks retrieved successfully'
    });
  } catch (error) {
    console.error('Error fetching artworks:', error);
    res.status(500).json({
      success: false,
      error: 'Failed to fetch artworks',
      message: error instanceof Error ? error.message : 'Unknown error'
    });
  }
});

export default router;
