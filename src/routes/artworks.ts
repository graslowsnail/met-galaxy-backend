import { Router } from 'express';
import { db } from '../db/index.js';
import { artworks } from '../db/schema.js';

const router = Router();

// GET /api/artworks - Fetch 10 artworks
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
