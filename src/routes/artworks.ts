import { Router } from 'express';
import { db } from '../db/index.js';
import { artworks, imageAssets } from '../db/schema.js';
import { sql } from 'drizzle-orm';
import { getFullImageUrl, getGraphImageUrl, getImageSource } from '../lib/imageUrls.js';

const router = Router();

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
        imageAssetId: artworks.imageAssetId,
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
      .limit(count * 2);

    const seenAssets = new Set<number>();
    const canonicalResult = result.filter((artwork) => {
      if (
        artwork.imageAssetId === null
        || seenAssets.has(artwork.imageAssetId)
      ) {
        return false;
      }
      seenAssets.add(artwork.imageAssetId);
      return true;
    }).slice(0, count);

    console.log(`🎲 [RANDOM] ${result.length} artworks | seed=${seed} | ${Date.now() - startTime}ms`);

    // Transform the data to match API spec
    const transformedData = canonicalResult.map(artwork => ({
      id: artwork.id,
      canonicalAssetId: artwork.imageAssetId,
      objectId: artwork.objectId,
      title: artwork.title,
      artist: artwork.artist,
      date: artwork.date,
      department: artwork.department,
      creditLine: artwork.creditLine,
      description: artwork.description,
      imageUrl: getGraphImageUrl(artwork),
      originalImageUrl: getFullImageUrl(artwork),
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
        imageAssetId: artworks.imageAssetId,
      })
      .from(artworks)
      .where(sql`id = ${artworkId} AND "imageAssetId" IS NOT NULL AND "localImageUrl" IS NOT NULL AND "localImageUrl" != ''`)
      .limit(1);

    if (targetArtwork.length === 0) {
      return res.status(404).json({
        success: false,
        error: 'Artwork not found or missing S3 image/embedding'
      });
    }

    const target = targetArtwork[0];

    const [targetAsset] = await db
      .select({ imageEmbedding: imageAssets.imageEmbedding })
      .from(imageAssets)
      .where(
        sql`${imageAssets.id} = ${target.imageAssetId}
          AND ${imageAssets.processingStatus} = 'ready'
          AND ${imageAssets.imageEmbedding} IS NOT NULL`,
      )
      .limit(1);

    if (!targetAsset?.imageEmbedding) {
      return res.status(404).json({
        success: false,
        error: 'Artwork canonical image is missing its embedding'
      });
    }

    const targetVectorString = `[${targetAsset.imageEmbedding.join(',')}]`;
    const similarArtworks = await db.transaction(async (tx) => {
      await tx.execute(sql`SET LOCAL hnsw.ef_search = 200`);
      await tx.execute(sql`SET LOCAL hnsw.iterative_scan = 'strict_order'`);
      await tx.execute(sql`SET LOCAL enable_seqscan = off`);
      const rows = await tx.execute(sql`
        WITH nearest AS MATERIALIZED (
          SELECT
            asset.id AS "imageAssetId",
            asset."imageEmbedding" <=> ${targetVectorString}::vector
              AS distance
          FROM "met-galaxy_image_asset" asset
          LEFT JOIN "met-galaxy_image_asset_canonical" mapping
            ON mapping."assetId" = asset.id
          WHERE asset.id <> ${target.imageAssetId}
            AND asset."processingStatus" = 'ready'
            AND asset."imageEmbedding" IS NOT NULL
            AND mapping."assetId" IS NULL
          ORDER BY asset."imageEmbedding" <=> ${targetVectorString}::vector
          LIMIT ${count}
        )
        SELECT
          artwork.id,
          nearest."imageAssetId",
          artwork."objectId",
          artwork.title,
          artwork.artist,
          artwork."localImageUrl",
          artwork."primaryImage",
          artwork."primaryImageSmall",
          1 - nearest.distance AS similarity
        FROM nearest
        CROSS JOIN LATERAL (
          SELECT artwork.*
          FROM "met-galaxy_artwork" artwork
          WHERE artwork."imageAssetId" = nearest."imageAssetId"
            AND artwork."localImageUrl" IS NOT NULL
            AND artwork."localImageUrl" <> ''
          ORDER BY artwork.id
          LIMIT 1
        ) artwork
        ORDER BY nearest.distance, artwork.id
      `);
      return Array.from(rows) as Array<{
        id: number;
        imageAssetId: number;
        objectId: number;
        title: string | null;
        artist: string | null;
        localImageUrl: string | null;
        primaryImage: string | null;
        primaryImageSmall: string | null;
        similarity: number;
      }>;
    });

    console.log(`🔍 [SIMILAR] ${similarArtworks.length} artworks for ID ${artworkId} | ${Date.now() - startTime}ms`);

    // Transform the data and mark the original
    const transformedData = similarArtworks.map(artwork => ({
      id: artwork.id,
      canonicalAssetId: artwork.imageAssetId,
      objectId: artwork.objectId,
      title: artwork.title,
      artist: artwork.artist,
      imageUrl: getGraphImageUrl(artwork),
      originalImageUrl: getFullImageUrl(artwork),
      imageSource: getImageSource(artwork),
      original: false,
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

router.get('/canonical/:assetId', async (req, res) => {
  const assetId = Number.parseInt(req.params.assetId, 10);
  if (!Number.isSafeInteger(assetId) || assetId <= 0) {
    return res.status(400).json({
      success: false,
      error: 'Invalid canonical asset ID',
    });
  }

  try {
    const resolvedAssets = await db.execute(sql`
      SELECT
        root.id,
        root.width,
        root.height,
        root."mimeType",
        root."processingStatus",
        requested.id AS "requestedAssetId"
      FROM "met-galaxy_image_asset" requested
      LEFT JOIN "met-galaxy_image_asset_canonical" mapping
        ON mapping."assetId" = requested.id
      JOIN "met-galaxy_image_asset" root
        ON root.id = COALESCE(mapping."canonicalAssetId", requested.id)
      WHERE requested.id = ${assetId}
      LIMIT 1
    `);
    const asset = Array.from(resolvedAssets)[0] as {
      id: number;
      width: number | null;
      height: number | null;
      mimeType: string | null;
      processingStatus: string;
      requestedAssetId: number;
    } | undefined;

    if (!asset) {
      return res.status(404).json({
        success: false,
        error: 'Canonical image asset not found',
      });
    }

    const linkedArtworks = await db
      .select({
        id: artworks.id,
        objectId: artworks.objectId,
        title: artworks.title,
        artist: artworks.artist,
        date: artworks.date,
        department: artworks.department,
        culture: artworks.culture,
        medium: artworks.medium,
        creditLine: artworks.creditLine,
        description: artworks.description,
        localImageUrl: artworks.localImageUrl,
        primaryImage: artworks.primaryImage,
        objectUrl: artworks.objectUrl,
        duplicateState: artworks.imageDuplicateState,
      })
      .from(artworks)
      .where(sql`${artworks.imageAssetId} = ${asset.id}`)
      .orderBy(artworks.id);

    res.json({
      success: true,
      data: {
        ...asset,
        imageUrl: linkedArtworks[0]?.localImageUrl ?? null,
        artworks: linkedArtworks,
      },
      meta: {
        requestedAssetId: asset.requestedAssetId,
        canonicalAssetId: asset.id,
        resolvedFromMergedAsset: asset.requestedAssetId !== asset.id,
        linkedArtworkCount: linkedArtworks.length,
      },
    });
  } catch (error) {
    console.error(
      '[CANONICAL] Request failed:',
      error instanceof Error ? error.message : 'Unknown error',
    );
    res.status(500).json({
      success: false,
      error: 'Failed to fetch canonical image asset',
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
