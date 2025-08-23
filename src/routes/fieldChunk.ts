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
      responseTime: `${Date.now() - start}ms`
    });
  } catch (err: any) {
    console.error('field-chunk error', err);
    res.status(500).json({ success: false, error: 'field-chunk failed', message: err.message });
  }
});

export default router;