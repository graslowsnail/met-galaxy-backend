import { Router } from 'express';
import { db } from '../db/index.js';
import { artworks, imageAssets } from '../db/schema.js';
import { sql } from 'drizzle-orm';
import { getFullImageUrl, getGraphImageUrl, getImageSource } from '../lib/imageUrls.js';
import { hash32 } from '../lib/fieldVectors.js';
import { parseTimelineRange, timelineFilter, timelineYearSql, type TimelineRange } from '../lib/timeline.js';

import { getTimelineSummary } from '../lib/timelineSummary.js';
import { requestArtworkMetadata } from '../lib/artworkMetadata.js';

const router = Router();
let maxEligibleArtworkId: number | null = null;

type ChunkRequest = {
  x: number;
  y: number;
};

type SampledArtwork = {
  chunkKey: string;
  position: number;
  id: number;
  imageAssetId: number;
  objectId: number;
  title: string | null;
  artist: string | null;
  date: string | null;
  department: string | null;
  culture?: string | null;
  medium?: string | null;
  creditLine: string | null;
  accessionNumber?: string | null;
  description: string | null;
  localImageUrl: string;
  primaryImage: string | null;
  primaryImageSmall: string | null;
  objectUrl: string | null;
  objectBeginDate?: number | null;
  objectEndDate?: number | null;
  timelineYear?: number | null;
};

const transformArtwork = (artwork: Omit<SampledArtwork, 'chunkKey' | 'position'>) => ({
  id: artwork.id,
  canonicalAssetId: artwork.imageAssetId,
  objectId: artwork.objectId,
  title: artwork.title,
  artist: artwork.artist,
  date: artwork.date,
  department: artwork.department,
  culture: artwork.culture ?? null,
  medium: artwork.medium ?? null,
  creditLine: artwork.creditLine,
  accessionNumber: artwork.accessionNumber ?? null,
  description: artwork.description,
  imageUrl: getGraphImageUrl(artwork),
  originalImageUrl: getFullImageUrl(artwork),
  imageSource: getImageSource(artwork),
  objectUrl: artwork.objectUrl,
  objectBeginDate: artwork.objectBeginDate ?? null,
  objectEndDate: artwork.objectEndDate ?? null,
  timelineYear: artwork.timelineYear ?? null,
  hasEmbedding: true,
});

const sampleChunks = async (
  chunks: ChunkRequest[],
  count: number,
  globalSeed: number,
  range: TimelineRange | null = null,
) => {
  if (maxEligibleArtworkId === null) {
    const maxIdResult = await db.execute(sql`
      SELECT artwork.id
      FROM "met-galaxy_artwork" artwork
      WHERE artwork."localImageUrl" IS NOT NULL
        AND artwork."localImageUrl" <> ''
        AND artwork."imgVec" IS NOT NULL
        AND artwork."imageAssetId" IS NOT NULL
      ORDER BY artwork.id DESC
      LIMIT 1
    `);
    maxEligibleArtworkId = Number(
      (Array.from(maxIdResult)[0] as { id?: number } | undefined)?.id ?? 0,
    );
  }
  const maxId = maxEligibleArtworkId;
  if (maxId === 0) return new Map<string, ReturnType<typeof transformArtwork>[]>();

  const candidatesPerChunk = count * 2;
  const anchors = chunks.flatMap((chunk) =>
    Array.from({ length: candidatesPerChunk }, (_, position) => ({
      chunkKey: `${chunk.x},${chunk.y}`,
      position,
      anchor: 1 + (hash32(chunk.x, chunk.y, globalSeed, position) % maxId),
    })),
  );
  const requestedValues = sql.join(
    anchors.map((anchor) => sql`(${anchor.chunkKey}, ${anchor.position}, ${anchor.anchor})`),
    sql`, `,
  );
  const sampledResult = range ? await db.execute(sql`
    WITH requested("chunkKey", position, anchor, "anchorYear") AS (
      VALUES ${sql.join(anchors.map((anchor) => sql`(${anchor.chunkKey}, ${anchor.position}, ${anchor.anchor}, ${range.fromYear + (hash32(anchor.position, anchor.anchor, globalSeed) % (range.toYear - range.fromYear + 1))})`), sql`, `)}
    )
    SELECT requested."chunkKey", requested.position, artwork.*
    FROM requested
    JOIN LATERAL (
      SELECT artwork.id, artwork."imageAssetId", artwork."objectId", artwork.title, artwork.artist,
        artwork.date, artwork.department, artwork."creditLine", artwork.description, artwork."localImageUrl",
        artwork."primaryImage", artwork."primaryImageSmall", artwork."objectUrl", artwork."objectBeginDate",
        artwork."objectEndDate", ${timelineYearSql()} AS "timelineYear"
      FROM "met-galaxy_artwork" artwork
      WHERE ${timelineYearSql()} BETWEEN ${range.fromYear} AND ${range.toYear}
        AND (${timelineYearSql()}, artwork.id) >= (requested."anchorYear"::integer, requested.anchor::integer)
        AND artwork."localImageUrl" IS NOT NULL AND artwork."localImageUrl" <> ''
        AND artwork."imgVec" IS NOT NULL AND artwork."imageAssetId" IS NOT NULL
      ORDER BY ${timelineYearSql()}, artwork.id LIMIT 1
    ) artwork ON TRUE
    ORDER BY requested."chunkKey", requested.position::integer
  `) : await db.execute(sql`
    WITH requested("chunkKey", position, anchor) AS (
      VALUES ${requestedValues}
    )
    SELECT
      requested."chunkKey",
      requested.position,
      artwork.id,
      artwork."imageAssetId",
      artwork."objectId",
      artwork.title,
      artwork.artist,
      artwork.date,
      artwork.department,
      artwork."creditLine",
      artwork.description,
      artwork."localImageUrl",
      artwork."primaryImage",
      artwork."primaryImageSmall",
      artwork."objectUrl"
      , artwork."objectBeginDate", artwork."objectEndDate", ${timelineYearSql()} AS "timelineYear"
    FROM requested
    JOIN "met-galaxy_artwork" artwork
      ON artwork.id = requested.anchor::integer
      AND artwork."localImageUrl" IS NOT NULL
      AND artwork."localImageUrl" <> ''
      AND artwork."imgVec" IS NOT NULL
      AND artwork."imageAssetId" IS NOT NULL
      ${timelineFilter(range)}
    ORDER BY requested."chunkKey", requested.position::integer
  `);

  const grouped = new Map<string, ReturnType<typeof transformArtwork>[]>();
  const seenAssets = new Map<string, Set<number>>();
  for (const row of Array.from(sampledResult) as SampledArtwork[]) {
    const artworksForChunk = grouped.get(row.chunkKey) ?? [];
    if (artworksForChunk.length >= count) continue;
    const chunkAssets = seenAssets.get(row.chunkKey) ?? new Set<number>();
    if (chunkAssets.has(row.imageAssetId)) continue;
    chunkAssets.add(row.imageAssetId);
    seenAssets.set(row.chunkKey, chunkAssets);
    artworksForChunk.push(transformArtwork(row));
    grouped.set(row.chunkKey, artworksForChunk);
  }
  return grouped;
};

router.post('/random-chunks', async (req, res) => {
  const startTime = Date.now();
  const chunks = Array.isArray(req.body?.chunks) ? req.body.chunks : [];
  const count = Math.min(Math.max(Number.parseInt(req.body?.count, 10) || 20, 1), 50);
  const globalSeed = Number.isSafeInteger(req.body?.seed) ? req.body.seed : 0;
  const range = parseTimelineRange(req.body?.fromYear, req.body?.toYear);
  if (range === "invalid") return res.status(400).json({ success: false, error: 'Invalid timeline range' });
  const validChunks: ChunkRequest[] = chunks
    .filter((chunk: unknown): chunk is ChunkRequest => {
      if (!chunk || typeof chunk !== 'object') return false;
      const candidate = chunk as ChunkRequest;
      return Number.isSafeInteger(candidate.x) && Number.isSafeInteger(candidate.y);
    })
    .slice(0, 24);

  if (validChunks.length === 0) {
    return res.status(400).json({ success: false, error: 'At least one valid chunk is required' });
  }

  try {
    const grouped = await sampleChunks(validChunks, count, globalSeed, range);
    const data = Object.fromEntries(
      validChunks.map((chunk: ChunkRequest) => [`${chunk.x},${chunk.y}`, grouped.get(`${chunk.x},${chunk.y}`) ?? []]),
    );
    const responseTime = Date.now() - startTime;
    console.log(`🎲 [RANDOM-CHUNKS] ${validChunks.length} chunks | ${responseTime}ms`);
    res.json({
      success: true,
      data,
      meta: { chunkCount: validChunks.length, count, responseTime: `${responseTime}ms` },
    });
  } catch (error) {
    const nested = (error as { cause?: unknown })?.cause;
    const cause = nested instanceof Error ? nested.message : null;
    console.error('[RANDOM-CHUNKS] Request failed:', cause ?? (error instanceof Error ? error.message : 'Unknown error'));
    res.status(500).json({ success: false, error: 'Failed to fetch random artwork chunks' });
  }
});

router.get('/timeline-summary', async (req, res) => {
  const range = parseTimelineRange(req.query.fromYear, req.query.toYear);
  if (range === "invalid") return res.status(400).json({ success: false, error: 'Invalid timeline range' });
  try {
    res.json({ success: true, data: await getTimelineSummary(range) });
  } catch (error) {
    res.status(500).json({ success: false, error: 'Failed to load timeline summary' });
  }
});

router.post('/by-ids', async (req, res) => {
  const ids = Array.isArray(req.body?.ids) ? req.body.ids : [];
  const validIds = ids
    .filter((id: unknown): id is number => (
      typeof id === 'number' && Number.isSafeInteger(id) && id > 0
    ))
    .slice(0, 100);

  if (validIds.length === 0 || validIds.length !== ids.length) {
    return res.status(400).json({
      success: false,
      error: 'One to 100 valid artwork IDs are required',
    });
  }

  try {
    const requestedValues = sql.join(
      validIds.map((id: number, position: number) => sql`(${position}, ${id})`),
      sql`, `,
    );
    const result = await db.execute(sql`
      WITH requested(position, id) AS (
        VALUES ${requestedValues}
      )
      SELECT
        requested.position,
        artwork.id,
        artwork."imageAssetId",
        artwork."objectId",
        artwork.title,
        artwork.artist,
        artwork.date,
        artwork.department,
        artwork.culture,
        artwork.medium,
        artwork."creditLine",
        artwork.description,
        artwork."localImageUrl",
        artwork."primaryImage",
        artwork."primaryImageSmall",
        artwork."objectUrl"
        , artwork."objectBeginDate", artwork."objectEndDate", ${timelineYearSql()} AS "timelineYear"
      FROM requested
      JOIN "met-galaxy_artwork" artwork
        ON artwork.id = requested.id::integer
        AND artwork."imageAssetId" IS NOT NULL
        AND artwork."localImageUrl" IS NOT NULL
        AND artwork."localImageUrl" <> ''
      ORDER BY requested.position::integer
    `);
    const rows = Array.from(result) as Array<SampledArtwork & { position: number }>;

    if (rows.length !== validIds.length) {
      return res.status(404).json({
        success: false,
        error: 'One or more artworks were not found',
      });
    }

    res.json({
      success: true,
      data: rows.map(transformArtwork),
    });
  } catch (error) {
    console.error(
      '[ARTWORKS-BY-IDS] Request failed:',
      error instanceof Error ? error.message : 'Unknown error',
    );
    res.status(500).json({
      success: false,
      error: 'Failed to fetch artworks',
    });
  }
});

// GET /api/artworks/random - Random artworks with stable indexed sampling
router.get('/random', async (req, res) => {
  const startTime = Date.now();

  try {
    const count = Math.min(Math.max(parseInt(req.query.count as string) || 5, 1), 500);
    const seed = parseInt(req.query.seed as string) || Math.floor(Math.random() * 1000000);
    const chunkKey = `${seed},0`;
    const sampled = await sampleChunks([{ x: seed, y: 0 }], count, seed);
    const transformedData = sampled.get(chunkKey) ?? [];
    const responseTime = Date.now() - startTime;
    console.log(`🎲 [RANDOM] ${transformedData.length} artworks | seed=${seed} | ${responseTime}ms`);

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
        objectBeginDate: artworks.objectBeginDate,
        objectEndDate: artworks.objectEndDate,
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

// GET /api/artworks/ids - paged id list for sitemap generation.
// Must stay above '/:id', which would otherwise match 'ids'.
const SITEMAP_PAGE_SIZE = 50_000;

router.get('/ids', async (req, res) => {
  const requestedLimit = Number.parseInt(String(req.query.limit ?? ''), 10);
  const requestedOffset = Number.parseInt(String(req.query.offset ?? ''), 10);
  const limit = Number.isSafeInteger(requestedLimit) && requestedLimit > 0
    ? Math.min(requestedLimit, SITEMAP_PAGE_SIZE)
    : SITEMAP_PAGE_SIZE;
  const offset = Number.isSafeInteger(requestedOffset) && requestedOffset > 0
    ? requestedOffset
    : 0;

  try {
    const eligible = sql`${artworks.imageAssetId} IS NOT NULL
      AND ${artworks.localImageUrl} IS NOT NULL
      AND ${artworks.localImageUrl} <> ''`;

    const [totals] = await db
      .select({ total: sql<number>`count(*)::int` })
      .from(artworks)
      .where(eligible);

    const rows = await db
      .select({ id: artworks.id })
      .from(artworks)
      .where(eligible)
      .orderBy(artworks.id)
      .limit(limit)
      .offset(offset);

    res.json({
      success: true,
      data: {
        ids: rows.map((row) => row.id),
        total: totals?.total ?? 0,
        pageSize: SITEMAP_PAGE_SIZE,
      },
    });
  } catch (error) {
    console.error(
      '[ARTWORK IDS] Request failed:',
      error instanceof Error ? error.message : 'Unknown error',
    );
    res.status(500).json({
      success: false,
      error: 'Failed to fetch artwork ids',
    });
  }
});

router.get('/:id', async (req, res) => {
  const artworkId = Number.parseInt(req.params.id, 10);
  if (!Number.isSafeInteger(artworkId) || artworkId <= 0) {
    return res.status(400).json({
      success: false,
      error: 'Invalid artwork ID',
    });
  }

  try {
    const [artwork] = await db
      .select({
        id: artworks.id,
        imageAssetId: artworks.imageAssetId,
        objectId: artworks.objectId,
        title: artworks.title,
        artist: artworks.artist,
        date: artworks.date,
        department: artworks.department,
        culture: artworks.culture,
        medium: artworks.medium,
        creditLine: artworks.creditLine,
        accessionNumber: artworks.accessionNumber,
        description: artworks.description,
        localImageUrl: artworks.localImageUrl,
        primaryImage: artworks.primaryImage,
        primaryImageSmall: artworks.primaryImageSmall,
        objectUrl: artworks.objectUrl,
        metMetadataFetchedAt: artworks.metMetadataFetchedAt,
      })
      .from(artworks)
      .where(sql`${artworks.id} = ${artworkId}
        AND ${artworks.imageAssetId} IS NOT NULL
        AND ${artworks.localImageUrl} IS NOT NULL
        AND ${artworks.localImageUrl} <> ''`)
      .limit(1);

    if (!artwork || artwork.imageAssetId === null || artwork.localImageUrl === null) {
      return res.status(404).json({
        success: false,
        error: 'Artwork not found',
      });
    }

    res.setHeader('Cache-Control', 'no-store');
    res.json({
      success: true,
      data: transformArtwork({
        ...artwork,
        imageAssetId: artwork.imageAssetId,
        localImageUrl: artwork.localImageUrl,
      }),
      meta: requestArtworkMetadata(artwork),
    });
  } catch (error) {
    console.error(
      '[ARTWORK] Request failed:',
      error instanceof Error ? error.message : 'Unknown error',
    );
    res.status(500).json({
      success: false,
      error: 'Failed to fetch artwork',
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
