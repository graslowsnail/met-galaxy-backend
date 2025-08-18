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

// GET /api/artworks/random - Optimized random artworks for grid
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
      .where(sql`"localImageUrl" IS NOT NULL AND "localImageUrl" != ''`)
      .orderBy(sql`RANDOM()`)
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
