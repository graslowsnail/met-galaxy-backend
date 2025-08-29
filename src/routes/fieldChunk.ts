import { Router } from "express";
import { db } from "../db/index.js";
import { artworks } from "../db/schema.js";
import { sql } from "drizzle-orm";
import {
  hash32, mulberry32, gaussianVector, lerp, smoothstep,
  add, scale, normalize, pcaDirectionalBias
} from "../lib/fieldVectors.js";

const router = Router();

const getImageUrl = (a: any) => a.localImageUrl || a.primaryImageSmall || a.primaryImage || null;
const getImageSource = (a: any) => a.localImageUrl ? "s3" : (a.primaryImageSmall ? "met_small" : (a.primaryImage ? "met_original" : null));

const seedToPgFloat = (seed: number) => (seed >>> 0) / 4294967296;

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
      title: artworks.title,
      artist: artworks.artist,
      imgVec: artworks.imgVec,
      localImageUrl: artworks.localImageUrl,
      primaryImage: artworks.primaryImage,
      primaryImageSmall: artworks.primaryImageSmall,
    }).from(artworks)
     .where(sql`id = ${targetId} AND "imgVec" IS NOT NULL AND "localImageUrl" IS NOT NULL AND "localImageUrl" != ''`)
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
    const notTarget = sql`id != ${targetId}`;

    // Pools
    const simTight = await db.select({
      id: artworks.id, objectId: artworks.objectId, title: artworks.title, artist: artworks.artist,
      localImageUrl: artworks.localImageUrl, primaryImage: artworks.primaryImage, primaryImageSmall: artworks.primaryImageSmall,
      sim: sql<number>`1 - ("imgVec" <=> ${vStr}::vector)`
    }).from(artworks)
     .where(sql`"imgVec" IS NOT NULL AND "localImageUrl" IS NOT NULL AND "localImageUrl" != '' AND ${notTarget}`)
     .orderBy(sql`"imgVec" <=> ${vStr}::vector`)
     .limit(200);

    const simDrift = await db.select({
      id: artworks.id, objectId: artworks.objectId, title: artworks.title, artist: artworks.artist,
      localImageUrl: artworks.localImageUrl, primaryImage: artworks.primaryImage, primaryImageSmall: artworks.primaryImageSmall,
      sim: sql<number>`1 - ("imgVec" <=> ${vpStr}::vector)`
    }).from(artworks)
     .where(sql`"imgVec" IS NOT NULL AND "localImageUrl" IS NOT NULL AND "localImageUrl" != '' AND ${notTarget}`)
     .orderBy(sql`"imgVec" <=> ${vpStr}::vector`)
     .limit(400);

    await db.execute(sql`SELECT setseed(${seedToPgFloat(seed)})`);
    const randPool = await db.select({
      id: artworks.id, objectId: artworks.objectId, title: artworks.title, artist: artworks.artist,
      localImageUrl: artworks.localImageUrl, primaryImage: artworks.primaryImage, primaryImageSmall: artworks.primaryImageSmall
    }).from(artworks)
     .where(sql`"imgVec" IS NOT NULL AND "localImageUrl" IS NOT NULL AND "localImageUrl" != '' AND ${notTarget}`)
     .orderBy(sql`RANDOM(), id ASC`)
     .limit(800);

    const wSim = (1 - t) * (1 - t);
    const wDrift = 2 * t * (1 - t);
    const wRand = t * t;
    const sum = wSim + wDrift + wRand || 1;
    const pSim = wSim / sum, pDrift = wDrift / sum;

    const used = new Set<number>(excludeSet);
    const out: any[] = [];
    
    // Spatial partitioning for close chunks to prevent duplicates
    const spatialOffset = r < 2 ? hash32(chunkX + 100, chunkY + 100) % 50 : 0;
    
    const simTightQ = simTight.slice(spatialOffset).concat(simTight.slice(0, spatialOffset)).map(x => ({...x, source: 'sim'}));
    const simDriftQ = simDrift.slice(spatialOffset).concat(simDrift.slice(0, spatialOffset)).map(x => ({...x, source: 'drift'}));
    const randQ     = randPool.map(x => ({...x, source: 'rand'}));

    const takeNext = (q: any[]) => {
      while (q.length) {
        const p = q.shift();
        if (!used.has(p.id)) return p;
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
      out.push(cand);
    }

    const data = out.map(p => ({
      id: p.id,
      objectId: p.objectId,
      title: p.title,
      artist: p.artist,
      imageUrl: getImageUrl(p),
      originalImageUrl: p.primaryImage,
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
      title: artworks.title,
      artist: artworks.artist,
      imgVec: artworks.imgVec,
      localImageUrl: artworks.localImageUrl,
      primaryImage: artworks.primaryImage,
      primaryImageSmall: artworks.primaryImageSmall,
    }).from(artworks)
     .where(sql`id = ${targetId} AND "imgVec" IS NOT NULL AND "localImageUrl" IS NOT NULL AND "localImageUrl" != ''`)
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
    
    // Build SQL exclusion clause
    const excludeClause = globalExcludes.size > 0 
      ? sql`AND id NOT IN (${sql.join(Array.from(globalExcludes), sql`, `)})`
      : sql``;

    // Sort chunks by distance for better similarity distribution
    const sortedChunks = chunks.map((chunk, index) => ({
      ...chunk,
      originalIndex: index,
      r: Math.hypot(chunk.x, chunk.y)
    })).sort((a, b) => a.r - b.r);

    // Scale pool sizes based on chunk count
    const simTightLimit = Math.min(500, chunks.length * 125);
    const simDriftLimit = Math.min(800, chunks.length * 200);
    const randLimit = Math.min(1200, chunks.length * 300);

    // Generate global pools
    const vStr = `[${Array.from(v).join(',')}]`;
    const notTarget = sql`id != ${targetId}`;

    const globalSimTight = await db.select({
      id: artworks.id, objectId: artworks.objectId, title: artworks.title, artist: artworks.artist,
      localImageUrl: artworks.localImageUrl, primaryImage: artworks.primaryImage, primaryImageSmall: artworks.primaryImageSmall,
      sim: sql<number>`1 - ("imgVec" <=> ${vStr}::vector)`
    }).from(artworks)
     .where(sql`"imgVec" IS NOT NULL AND "localImageUrl" IS NOT NULL AND "localImageUrl" != '' AND ${notTarget} ${excludeClause}`)
     .orderBy(sql`"imgVec" <=> ${vStr}::vector`)
     .limit(simTightLimit);

    // Process each chunk
    const results: Record<string, any> = {};
    const overallT = chunks.reduce((sum, chunk) => sum + smoothstep(1.5, 12.0, Math.hypot(chunk.x, chunk.y)), 0) / chunks.length;

    for (let chunkIndex = 0; chunkIndex < sortedChunks.length; chunkIndex++) {
      const chunk = sortedChunks[chunkIndex];
      const { x: chunkX, y: chunkY } = chunk;
      
      // Field coordinates
      const r = Math.hypot(chunkX, chunkY);
      const t = smoothstep(1.5, 12.0, r);
      const theta = Math.atan2(chunkY, chunkX);

      // Chunk-specific seed and RNG
      const seed = hash32(targetId, chunkX, chunkY, globalSeed);
      const rng = mulberry32(seed);

      // Generate perturbed vector for this chunk
      const bias = pcaDirectionalBias(theta, t);
      const sigma = lerp(0.05, 0.35, t);
      const eps = gaussianVector(d, rng);
      const vprime = normalize(add(add(v, bias), scale(eps, sigma)));
      const vpStr = `[${Array.from(vprime).join(',')}]`;

      // Generate drift pool for this chunk
      const chunkSimDrift = await db.select({
        id: artworks.id, objectId: artworks.objectId, title: artworks.title, artist: artworks.artist,
        localImageUrl: artworks.localImageUrl, primaryImage: artworks.primaryImage, primaryImageSmall: artworks.primaryImageSmall,
        sim: sql<number>`1 - ("imgVec" <=> ${vpStr}::vector)`
      }).from(artworks)
       .where(sql`"imgVec" IS NOT NULL AND "localImageUrl" IS NOT NULL AND "localImageUrl" != '' AND ${notTarget} ${excludeClause}`)
       .orderBy(sql`"imgVec" <=> ${vpStr}::vector`)
       .limit(Math.min(400, simDriftLimit));

      // Generate random pool for this chunk
      await db.execute(sql`SELECT setseed(${seedToPgFloat(seed)})`);
      const chunkRandPool = await db.select({
        id: artworks.id, objectId: artworks.objectId, title: artworks.title, artist: artworks.artist,
        localImageUrl: artworks.localImageUrl, primaryImage: artworks.primaryImage, primaryImageSmall: artworks.primaryImageSmall
      }).from(artworks)
       .where(sql`"imgVec" IS NOT NULL AND "localImageUrl" IS NOT NULL AND "localImageUrl" != '' AND ${notTarget} ${excludeClause}`)
       .orderBy(sql`RANDOM(), id ASC`)
       .limit(Math.min(800, randLimit));

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
      const chunkOut: any[] = [];

      const takeNext = (q: any[]) => {
        while (q.length) {
          const p = q.shift();
          if (!chunkUsed.has(p.id)) return p;
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
        globalUsed.add(cand.id); // Update global used set for cross-chunk deduplication
        chunkOut.push(cand);
      }

      // Format chunk data
      const chunkData = chunkOut.map(p => ({
        id: p.id,
        objectId: p.objectId,
        title: p.title,
        artist: p.artist,
        imageUrl: getImageUrl(p),
        originalImageUrl: p.primaryImage,
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