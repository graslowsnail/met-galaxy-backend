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
    
    // Parse and validate query parameters
    const count = Math.min(Math.max(parseInt(req.query.count as string) || 5, 1), 500);
    const seed = parseInt(req.query.seed as string) || Math.floor(Math.random() * 1000000);
    const withImages = req.query.withImages !== 'false'; // Default true
    

    // Use seed for consistent randomization - set seed first, then random order
    await db.execute(sql`SELECT setseed(${seed / 1000000.0})`);
    
    const result = await db
      .select({
        id: artworks.id,
        objectId: artworks.objectId,
        title: artworks.title,
        artist: artworks.artist,
        date: artworks.date,
        department: artworks.department,
        creditLine: artworks.creditLine,
        description: artworks.description,
        localImageUrl: artworks.localImageUrl,
        primaryImage: artworks.primaryImage,
        primaryImageSmall: artworks.primaryImageSmall,
        objectUrl: artworks.objectUrl,
      })
      .from(artworks)
      .where(sql`"localImageUrl" IS NOT NULL AND "localImageUrl" != '' AND "imgVec" IS NOT NULL`)
      // CRITICAL FIX: Add deterministic secondary ordering by ID to ensure stable order
      .orderBy(sql`RANDOM(), id ASC`)
      .limit(count);

    console.log(`🎲 [RANDOM] ${result.length} artworks | seed=${seed} | ${Date.now() - startTime}ms`);

    // Transform the data to match API spec
    const transformedData = result.map(artwork => ({
      id: artwork.id,
      objectId: artwork.objectId,
      title: artwork.title,
      artist: artwork.artist,
      date: artwork.date,
      department: artwork.department,
      creditLine: artwork.creditLine,
      description: artwork.description,
      imageUrl: getImageUrl(artwork),
      originalImageUrl: artwork.primaryImage,
      imageSource: getImageSource(artwork),
      objectUrl: artwork.objectUrl,
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
    console.error(`❌ [RANDOM] Request failed after ${Date.now() - startTime}ms:`, error instanceof Error ? error.message : 'Unknown error');
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
    
    const artworkId = parseInt(req.params.id);
    const count = 160; // Fixed at 50 as requested
    
    if (!artworkId || artworkId <= 0) {
      return res.status(400).json({
        success: false,
        error: 'Invalid artwork ID'
      });
    }
    

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

    // Convert the target embedding to a string format for PostgreSQL
    const targetVectorString = `[${target.imgVec!.join(',')}]`;

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

    console.log(`🔍 [SIMILAR] ${similarArtworks.length} artworks for ID ${artworkId} | ${Date.now() - startTime}ms`);

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
    console.error(`❌ [SIMILAR] Request failed after ${Date.now() - startTime}ms:`, error instanceof Error ? error.message : 'Unknown error');
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
    console.error(`❌ [LEGACY] Request failed:`, error instanceof Error ? error.message : 'Unknown error');
    res.status(500).json({
      success: false,
      error: 'Failed to fetch artworks',
      message: error instanceof Error ? error.message : 'Unknown error'
    });
  }
});

export default router;
