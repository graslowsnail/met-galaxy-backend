import { Router } from "express";
import { db } from "../db/index.js";
import { artworks, imageAssets } from "../db/schema.js";
import { sql } from "drizzle-orm";
import { getFullImageUrl, getGraphImageUrl, getImageSource } from "../lib/imageUrls.js";
import {
  hash32, mulberry32, gaussianVector, lerp, smoothstep,
  add, scale, normalize, pcaDirectionalBias
} from "../lib/fieldVectors.js";

const router = Router();
let maxArtworkId: number | null = null;

type CanonicalPoolArtwork = {
  id: number;
  imageAssetId: number;
  objectId: number;
  title: string | null;
  artist: string | null;
  localImageUrl: string | null;
  primaryImage: string | null;
  primaryImageSmall: string | null;
  sim?: number;
};

const artworkExclusion = (excludedIds: number[]) => excludedIds.length > 0
  ? sql`AND artwork.id NOT IN (${sql.join(excludedIds.map(id => sql`${id}`), sql`, `)})`
  : sql``;

const getCanonicalSimilarityPool = async (
  vector: string,
  targetAssetId: number,
  limit: number,
  excludedIds: number[],
) => db.transaction(async (tx) => {
  await tx.execute(sql`SET LOCAL hnsw.ef_search = 100`);
  await tx.execute(sql`SET LOCAL hnsw.iterative_scan = 'strict_order'`);
  await tx.execute(sql`SET LOCAL hnsw.max_scan_tuples = 50000`);
  await tx.execute(sql`SET LOCAL enable_seqscan = off`);
  const rows = await tx.execute(sql`
    WITH nearest AS MATERIALIZED (
      SELECT
        asset.id AS "imageAssetId",
        asset."imageEmbedding" <=> ${vector}::vector AS distance
      FROM "met-galaxy_image_asset" asset
      LEFT JOIN "met-galaxy_image_asset_canonical" mapping
        ON mapping."assetId" = asset.id
      WHERE asset.id <> ${targetAssetId}
        AND asset."processingStatus" = 'ready'
        AND asset."imageEmbedding" IS NOT NULL
        AND mapping."assetId" IS NULL
      ORDER BY asset."imageEmbedding" <=> ${vector}::vector
      LIMIT ${limit}
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
      1 - nearest.distance AS sim
    FROM nearest
    CROSS JOIN LATERAL (
      SELECT artwork.*
      FROM "met-galaxy_artwork" artwork
      WHERE artwork."imageAssetId" = nearest."imageAssetId"
        AND artwork."localImageUrl" IS NOT NULL
        AND artwork."localImageUrl" <> ''
        ${artworkExclusion(excludedIds)}
      ORDER BY artwork.id
      LIMIT 1
    ) artwork
    ORDER BY nearest.distance, artwork.id
  `);
  return Array.from(rows) as CanonicalPoolArtwork[];
});

const getCanonicalRandomPool = async (
  seed: number,
  targetAssetId: number,
  limit: number,
  excludedIds: number[],
) => {
  if (maxArtworkId === null) {
    const result = await db.execute(sql`SELECT MAX(id) AS id FROM "met-galaxy_artwork"`);
    maxArtworkId = Number((Array.from(result)[0] as { id?: number } | undefined)?.id ?? 0);
  }
  if (maxArtworkId === 0) return [];

  const anchors = Array.from({ length: limit * 2 }, (_, position) => ({
    position,
    id: 1 + (hash32(seed, position) % maxArtworkId!),
  }));
  const requestedValues = sql.join(
    anchors.map((anchor) => sql`(${anchor.position}, ${anchor.id})`),
    sql`, `,
  );
  const rows = await db.execute(sql`
    WITH requested(position, id) AS (VALUES ${requestedValues})
    SELECT
      artwork.id,
      artwork."imageAssetId",
      artwork."objectId",
      artwork.title,
      artwork.artist,
      artwork."localImageUrl",
      artwork."primaryImage",
      artwork."primaryImageSmall"
    FROM requested
    JOIN "met-galaxy_artwork" artwork
      ON artwork.id = requested.id::integer
    JOIN "met-galaxy_image_asset" asset
      ON asset.id = artwork."imageAssetId"
    LEFT JOIN "met-galaxy_image_asset_canonical" mapping
      ON mapping."assetId" = asset.id
    WHERE artwork."imageAssetId" <> ${targetAssetId}
      AND artwork."localImageUrl" IS NOT NULL
      AND artwork."localImageUrl" <> ''
      AND asset."processingStatus" = 'ready'
      AND asset."imageEmbedding" IS NOT NULL
      AND mapping."assetId" IS NULL
      ${artworkExclusion(excludedIds)}
    ORDER BY requested.position::integer
    LIMIT ${limit}
  `);
  return Array.from(rows) as CanonicalPoolArtwork[];
};

router.get("/field-chunk", async (req, res) => {
  const start = Date.now();
  try {
    const targetId = parseInt(req.query.targetId as string);
    const chunkX   = parseInt(req.query.chunkX as string);
    const chunkY   = parseInt(req.query.chunkY as string);
    const count    = Math.min(Math.max(parseInt(req.query.count as string) || 20, 1), 50);
    const globalSeed = req.query.seed ? parseInt(req.query.seed as string) : 0;

    if (!targetId || Number.isNaN(chunkX) || Number.isNaN(chunkY)) {
      return res.status(400).json({ success: false, error: "Bad params: targetId, chunkX, and chunkY are required" });
    }

    // target
    const [target] = await db.select({
      id: artworks.id,
      imageAssetId: artworks.imageAssetId,
      title: artworks.title,
      artist: artworks.artist,
      imgVec: imageAssets.imageEmbedding,
      localImageUrl: artworks.localImageUrl,
      primaryImage: artworks.primaryImage,
      primaryImageSmall: artworks.primaryImageSmall,
    }).from(artworks)
     .innerJoin(imageAssets, sql`${imageAssets.id} = ${artworks.imageAssetId}`)
     .where(sql`${artworks.id} = ${targetId}
       AND ${imageAssets.processingStatus} = 'ready'
       AND ${imageAssets.imageEmbedding} IS NOT NULL
       AND ${artworks.localImageUrl} IS NOT NULL
       AND ${artworks.localImageUrl} != ''`)
     .limit(1);

    if (!target) return res.status(404).json({ success: false, error: "Target not found or missing embedding/image" });

    const v = normalize(Float32Array.from(target.imgVec as number[]));
    const d = v.length;

    // field coords
    const r = Math.hypot(chunkX, chunkY);
    const t = smoothstep(1.5, 12.0, r);
    const theta = Math.atan2(chunkY, chunkX);

    // seed & rng
    const seed = hash32(targetId, chunkX, chunkY, globalSeed);
    const rng  = mulberry32(seed);

    // v' = v + bias + sigma*eps
    const bias = pcaDirectionalBias(theta, t);
    const sigma = lerp(0.05, 0.35, t);
    const eps = gaussianVector(d, rng);
    const vprime = normalize(add(add(v, bias), scale(eps, sigma)));

    const vStr  = `[${Array.from(v).join(',')}]`;
    const vpStr = `[${Array.from(vprime).join(',')}]`;

    // parse excludes
    const excludeSet = new Set<number>([targetId]);
    if (typeof req.query.exclude === 'string') {
      for (const s of (req.query.exclude as string).split(',')) {
        const n = parseInt(s); if (!Number.isNaN(n)) excludeSet.add(n);
      }
    }
    const excludedIds = Array.from(excludeSet);

    // Pools
    const [simTight, simDrift, randPool] = await Promise.all([
      getCanonicalSimilarityPool(vStr, target.imageAssetId!, 100, excludedIds),
      getCanonicalSimilarityPool(vpStr, target.imageAssetId!, 80, excludedIds),
      getCanonicalRandomPool(seed, target.imageAssetId!, 80, excludedIds),
    ]);

    const wSim = (1 - t) * (1 - t);
    const wDrift = 2 * t * (1 - t);
    const wRand = t * t;
    const sum = wSim + wDrift + wRand || 1;
    const pSim = wSim / sum, pDrift = wDrift / sum;

    const used = new Set<number>(excludeSet);
    const usedAssets = new Set<number>([target.imageAssetId!]);
    const out: any[] = [];
    
    // Spatial partitioning for close chunks to prevent duplicates
    const spatialOffset = r < 2 ? hash32(chunkX + 100, chunkY + 100) % 50 : 0;
    
    const simTightQ = simTight.slice(spatialOffset).concat(simTight.slice(0, spatialOffset)).map(x => ({...x, source: 'sim'}));
    const simDriftQ = simDrift.slice(spatialOffset).concat(simDrift.slice(0, spatialOffset)).map(x => ({...x, source: 'drift'}));
    const randQ     = randPool.map(x => ({...x, source: 'rand'}));

    const takeNext = (q: any[]) => {
      while (q.length) {
        const p = q.shift();
        if (!used.has(p.id) && !usedAssets.has(p.imageAssetId)) return p;
      }
      return null;
    };

    for (let i = 0; i < count; i++) {
      const u = rng();
      let choice: 'sim' | 'drift' | 'rand';
      if (u < pSim) choice = 'sim';
      else if (u < pSim + pDrift) choice = 'drift';
      else choice = 'rand';

      let cand = null;
      if (choice === 'sim')   cand = takeNext(simTightQ) || takeNext(simDriftQ) || takeNext(randQ);
      else if (choice === 'drift') cand = takeNext(simDriftQ) || takeNext(simTightQ) || takeNext(randQ);
      else cand = takeNext(randQ) || takeNext(simDriftQ) || takeNext(simTightQ);
      if (!cand) break;
      used.add(cand.id);
      usedAssets.add(cand.imageAssetId);
      out.push(cand);
    }

    const data = out.map(p => ({
      id: p.id,
      canonicalAssetId: p.imageAssetId,
      objectId: p.objectId,
      title: p.title,
      artist: p.artist,
      imageUrl: getGraphImageUrl(p),
      originalImageUrl: getFullImageUrl(p),
      imageSource: getImageSource(p),
      similarity: typeof p.sim === 'number' ? p.sim : null,
      source: p.source
    }));

    const responseTime = Date.now() - start;
    console.log(`🌌 [FIELD-CHUNK] ${data.length} artworks | (${chunkX},${chunkY}) r=${Math.round(r * 100) / 100} | ${responseTime}ms`);
    
    res.json({
      success: true,
      meta: { 
        targetId, 
        chunk: { x: chunkX, y: chunkY }, 
        r: Math.round(r * 100) / 100, 
        theta: Math.round(theta * 100) / 100, 
        t: Math.round(t * 100) / 100, 
        weights: { 
          sim: Math.round(wSim * 1000) / 1000, 
          drift: Math.round(wDrift * 1000) / 1000, 
          rand: Math.round(wRand * 1000) / 1000 
        }, 
        seed 
      },
      data,
      responseTime: `${responseTime}ms`
    });
  } catch (err: any) {
    console.error(`❌ [FIELD-CHUNK] Request failed after ${Date.now() - start}ms:`, err.message);
    res.status(500).json({ success: false, error: 'field-chunk failed', message: err.message });
  }
});

// Multi-chunk endpoint
router.post("/field-chunks", async (req, res) => {
  const start = Date.now();
  try {
    const { targetId, chunks, count = 20, seed: globalSeed = 0, excludeIds = [] } = req.body;

    // Validation
    if (!targetId || typeof targetId !== 'number') {
      return res.status(400).json({ 
        success: false, 
        error: "Invalid request", 
        details: "targetId is required and must be a number" 
      });
    }

    if (!Array.isArray(chunks) || chunks.length === 0 || chunks.length > 16) {
      return res.status(400).json({
        success: false,
        error: "Invalid request",
        details: "chunks array must contain 1-16 chunk objects"
      });
    }

    // Validate chunk objects
    for (const chunk of chunks) {
      if (typeof chunk.x !== 'number' || typeof chunk.y !== 'number') {
        return res.status(400).json({
          success: false,
          error: "Invalid request",
          details: "Each chunk must have numeric x and y coordinates"
        });
      }
    }

    const actualCount = Math.min(Math.max(count, 1), 50);
    const actualExcludeIds = Array.isArray(excludeIds) ? excludeIds.filter(id => typeof id === 'number') : [];

    // Get target artwork
    const [target] = await db.select({
      id: artworks.id,
      imageAssetId: artworks.imageAssetId,
      title: artworks.title,
      artist: artworks.artist,
      imgVec: imageAssets.imageEmbedding,
      localImageUrl: artworks.localImageUrl,
      primaryImage: artworks.primaryImage,
      primaryImageSmall: artworks.primaryImageSmall,
    }).from(artworks)
     .innerJoin(imageAssets, sql`${imageAssets.id} = ${artworks.imageAssetId}`)
     .where(sql`${artworks.id} = ${targetId}
       AND ${imageAssets.processingStatus} = 'ready'
       AND ${imageAssets.imageEmbedding} IS NOT NULL
       AND ${artworks.localImageUrl} IS NOT NULL
       AND ${artworks.localImageUrl} != ''`)
     .limit(1);

    if (!target) {
      return res.status(404).json({ 
        success: false, 
        error: "Target artwork not found",
        targetId 
      });
    };

    const v = normalize(Float32Array.from(target.imgVec as number[]));
    const d = v.length;

    // Global exclusion set
    const globalExcludes = new Set<number>([targetId, ...actualExcludeIds]);
    const globalUsed = new Set<number>(globalExcludes); // Track used IDs across all chunks
    const globalUsedAssets = new Set<number>([target.imageAssetId!]);
    
    const excludedIds = Array.from(globalExcludes);

    // Sort chunks by distance for better similarity distribution
    const sortedChunks = chunks.map((chunk, index) => ({
      ...chunk,
      originalIndex: index,
      r: Math.hypot(chunk.x, chunk.y)
    })).sort((a, b) => a.r - b.r);

    const simTightLimit = Math.min(
      400,
      Math.max(actualCount * 4, chunks.length * actualCount * 2),
    );
    const perChunkPoolLimit = Math.min(160, Math.max(actualCount * 4, 60));

    // Generate global pools
    const vStr = `[${Array.from(v).join(',')}]`;
    const globalSimTightPromise = getCanonicalSimilarityPool(
      vStr,
      target.imageAssetId!,
      simTightLimit,
      excludedIds,
    );

    const preparedChunks = sortedChunks.map((chunk) => {
      const r = Math.hypot(chunk.x, chunk.y);
      const t = smoothstep(1.5, 12.0, r);
      const theta = Math.atan2(chunk.y, chunk.x);
      const seed = hash32(targetId, chunk.x, chunk.y, globalSeed);
      const rng = mulberry32(seed);
      const bias = pcaDirectionalBias(theta, t);
      const sigma = lerp(0.05, 0.35, t);
      const eps = gaussianVector(d, rng);
      const vprime = normalize(add(add(v, bias), scale(eps, sigma)));
      return {
        chunk,
        r,
        t,
        theta,
        seed,
        rng,
        vector: `[${Array.from(vprime).join(',')}]`,
      };
    });
    const pools = new Map<string, {
      drift: CanonicalPoolArtwork[];
      random: CanonicalPoolArtwork[];
    }>();
    let nextPoolIndex = 0;
    const poolWorkers = Array.from(
      { length: Math.min(6, preparedChunks.length) },
      async () => {
        while (nextPoolIndex < preparedChunks.length) {
          const prepared = preparedChunks[nextPoolIndex++];
          const drift = await getCanonicalSimilarityPool(
            prepared.vector,
            target.imageAssetId!,
            perChunkPoolLimit,
            excludedIds,
          );
          const random = await getCanonicalRandomPool(
            prepared.seed,
            target.imageAssetId!,
            perChunkPoolLimit,
            excludedIds,
          );
          pools.set(`${prepared.chunk.x},${prepared.chunk.y}`, { drift, random });
        }
      },
    );
    const [globalSimTight] = await Promise.all([
      globalSimTightPromise,
      Promise.all(poolWorkers),
    ]);

    const results: Record<string, any> = {};
    const overallT = chunks.reduce((sum, chunk) => sum + smoothstep(1.5, 12.0, Math.hypot(chunk.x, chunk.y)), 0) / chunks.length;

    for (let chunkIndex = 0; chunkIndex < preparedChunks.length; chunkIndex++) {
      const prepared = preparedChunks[chunkIndex];
      const chunk = prepared.chunk;
      const { x: chunkX, y: chunkY } = chunk;
      const { r, t, theta, rng } = prepared;
      const chunkPools = pools.get(`${chunkX},${chunkY}`)!;
      const chunkSimDrift = chunkPools.drift;
      const chunkRandPool = chunkPools.random;

      // Enhanced spatial partitioning for better deduplication
      const getSpatialOffset = (cx: number, cy: number, globalSd: number, chIdx: number) => {
        const rVal = Math.hypot(cx, cy);
        if (rVal < 3) {
          return hash32(cx + 100, cy + 100, globalSd, chIdx) % 100;
        }
        return chIdx * 25;
      };

      const spatialOffset = getSpatialOffset(chunkX, chunkY, globalSeed, chunkIndex);

      // Apply spatial offset to pools
      const simTightQ = globalSimTight.slice(spatialOffset).concat(globalSimTight.slice(0, spatialOffset)).map(x => ({...x, source: 'sim'}));
      const simDriftQ = chunkSimDrift.slice(spatialOffset).concat(chunkSimDrift.slice(0, spatialOffset)).map(x => ({...x, source: 'drift'}));
      const randQ = chunkRandPool.map(x => ({...x, source: 'rand'}));

      // Calculate weights
      const wSim = (1 - t) * (1 - t);
      const wDrift = 2 * t * (1 - t);
      const wRand = t * t;
      const sum = wSim + wDrift + wRand || 1;
      const pSim = wSim / sum, pDrift = wDrift / sum;

      // Selection logic - DB excludes global list, we need both chunk and cross-chunk deduplication
      const chunkUsed = new Set<number>(globalUsed);
      const chunkUsedAssets = new Set<number>(globalUsedAssets);
      const chunkOut: any[] = [];

      const takeNext = (q: any[]) => {
        while (q.length) {
          const p = q.shift();
          if (
            !chunkUsed.has(p.id)
            && !chunkUsedAssets.has(p.imageAssetId)
          ) return p;
        }
        return null;
      };

      for (let i = 0; i < actualCount; i++) {
        const u = rng();
        let choice: 'sim' | 'drift' | 'rand';
        if (u < pSim) choice = 'sim';
        else if (u < pSim + pDrift) choice = 'drift';
        else choice = 'rand';

        let cand = null;
        if (choice === 'sim') cand = takeNext(simTightQ) || takeNext(simDriftQ) || takeNext(randQ);
        else if (choice === 'drift') cand = takeNext(simDriftQ) || takeNext(simTightQ) || takeNext(randQ);
        else cand = takeNext(randQ) || takeNext(simDriftQ) || takeNext(simTightQ);
        
        if (!cand) break;
        
        chunkUsed.add(cand.id);
        chunkUsedAssets.add(cand.imageAssetId);
        globalUsed.add(cand.id); // Update global used set for cross-chunk deduplication
        globalUsedAssets.add(cand.imageAssetId);
        chunkOut.push(cand);
      }

      // Format chunk data
      const chunkData = chunkOut.map(p => ({
        id: p.id,
        canonicalAssetId: p.imageAssetId,
        objectId: p.objectId,
        title: p.title,
        artist: p.artist,
        imageUrl: getGraphImageUrl(p),
        originalImageUrl: getFullImageUrl(p),
        imageSource: getImageSource(p),
        similarity: typeof p.sim === 'number' ? p.sim : null,
        source: p.source
      }));

      // Store result
      const chunkKey = `${chunkX},${chunkY}`;
      results[chunkKey] = {
        chunk: { x: chunkX, y: chunkY },
        artworks: chunkData,
        meta: {
          r: Math.round(r * 100) / 100,
          theta: Math.round(theta * 100) / 100,
          t: Math.round(t * 100) / 100,
          weights: {
            sim: Math.round(wSim * 1000) / 1000,
            drift: Math.round(wDrift * 1000) / 1000,
            rand: Math.round(wRand * 1000) / 1000
          }
        }
      };
    }

    // Response
    const responseTime = Date.now() - start;
    const totalArtworks = Object.values(results).reduce((sum: number, chunk: any) => sum + chunk.artworks.length, 0);
    console.log(`🌌 [FIELD-CHUNKS] ${totalArtworks} artworks across ${chunks.length} chunks | ${responseTime}ms`);
    
    res.json({
      success: true,
      meta: {
        targetId,
        totalChunks: chunks.length,
        globalExcludes: globalExcludes.size,
        seed: globalSeed,
        t: Math.round(overallT * 100) / 100
      },
      data: results,
      responseTime: `${responseTime}ms`
    });

  } catch (err: any) {
    console.error(`❌ [FIELD-CHUNKS] Request failed after ${Date.now() - start}ms:`, err.message);
    res.status(500).json({ 
      success: false, 
      error: 'Database query failed', 
      message: err.message 
    });
  }
});

export default router;
