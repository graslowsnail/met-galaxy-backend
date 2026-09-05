import { Router } from "express";
import { sql } from "drizzle-orm";
import OpenAI from "openai";
import { db } from "../db/index.js";
import { getFullImageUrl, getGraphImageUrl } from "../lib/imageUrls.js";
import { parseTimelineRange, timelineYearSql, type TimelineRange } from "../lib/timeline.js";

const router = Router();

type CachedEmbedding = {
  embedding: number[];
  expiresAt: number;
};

type SearchCandidate = {
  id: number;
  imageAssetId: number;
  timelineYear: number | null;
  similarity: number;
  exactMatch?: boolean;
};

type ArtworkDetails = {
  id: number;
  objectId: number;
  title: string | null;
  artist: string | null;
  date: string | null;
  department: string | null;
  culture: string | null;
  medium: string | null;
  creditLine: string | null;
  description: string | null;
  localImageUrl: string;
  primaryImage: string | null;
  objectUrl: string | null;
};

type SearchMode = "metadata" | "visual" | "keyword";

type RankedSearchResult = SearchCandidate & {
  rrfScore: number;
  ranks: Partial<Record<SearchMode, number>>;
  similarities: Partial<Record<SearchMode, number>>;
  representativeContribution: number;
  representativeExactMatch: boolean;
};

const metadataEmbeddingCache = new Map<string, CachedEmbedding>();
const visualEmbeddingCache = new Map<string, CachedEmbedding>();
const pendingMetadataEmbeddings = new Map<string, Promise<EmbeddingResult>>();
const pendingVisualEmbeddings = new Map<string, Promise<EmbeddingResult>>();
type EmbeddingResult = { embedding: number[]; cacheHit: boolean };
let metadataClient: OpenAI | undefined;
const EMBEDDING_CACHE_TTL_MS = 30 * 60 * 1000;
const MAX_EMBEDDING_CACHE_SIZE = 100;
const METADATA_EMBEDDING_MODEL = "text-embedding-3-small";
const METADATA_EMBEDDING_DIMENSIONS = 1536;
const VISUAL_EMBEDDING_MODEL = "ViT-L-14";
const VISUAL_EMBEDDING_PRETRAINED = "openai";
const VISUAL_EMBEDDING_DIMENSIONS = 768;

function getCachedEmbedding(
  cache: Map<string, CachedEmbedding>,
  text: string,
): number[] | null {
  const cached = cache.get(text);
  if (!cached) {
    return null;
  }
  if (cached.expiresAt <= Date.now()) {
    cache.delete(text);
    return null;
  }
  cache.delete(text);
  cache.set(text, cached);
  return cached.embedding;
}

function cacheEmbedding(
  cache: Map<string, CachedEmbedding>,
  text: string,
  embedding: number[],
): void {
  if (cache.size >= MAX_EMBEDDING_CACHE_SIZE) {
    const oldestKey = cache.keys().next().value;
    if (oldestKey !== undefined) {
      cache.delete(oldestKey);
    }
  }
  cache.set(text, {
    embedding,
    expiresAt: Date.now() + EMBEDDING_CACHE_TTL_MS,
  });
}

async function embedMetadataQuery(
  text: string,
  apiKey: string,
): Promise<{ embedding: number[]; cacheHit: boolean }> {
  return getQueryEmbedding(metadataEmbeddingCache, pendingMetadataEmbeddings, text, async () => {
    metadataClient ??= new OpenAI({ apiKey, timeout: 5000, maxRetries: 0 });
    const response = await metadataClient.embeddings.create({
      model: METADATA_EMBEDDING_MODEL,
      input: text,
      dimensions: METADATA_EMBEDDING_DIMENSIONS,
    });
    const embedding = response.data[0]?.embedding;
    if (!embedding || embedding.length !== METADATA_EMBEDDING_DIMENSIONS
      || embedding.some((value) => !Number.isFinite(value))) {
      throw new Error("Metadata service returned an invalid embedding");
    }
    return embedding;
  });
}

async function getQueryEmbedding(
  cache: Map<string, CachedEmbedding>,
  pending: Map<string, Promise<EmbeddingResult>>,
  text: string,
  create: () => Promise<number[]>,
): Promise<EmbeddingResult> {
  const cached = getCachedEmbedding(cache, text);
  if (cached) return { embedding: cached, cacheHit: true };
  const existing = pending.get(text);
  if (existing) return existing;
  const request = create().then((embedding) => {
    cacheEmbedding(cache, text, embedding);
    return { embedding, cacheHit: false };
  }).finally(() => pending.delete(text));
  pending.set(text, request);
  return request;
}

async function embedVisualQuery(
  text: string,
  serviceUrl: string,
  authToken: string | undefined,
): Promise<{ embedding: number[]; cacheHit: boolean }> {
  return getQueryEmbedding(visualEmbeddingCache, pendingVisualEmbeddings, text, async () => {
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
    };
    if (authToken) {
      headers.Authorization = `Bearer ${authToken}`;
    }

    const response = await fetch(new URL("/embed", serviceUrl), {
      method: "POST",
      headers,
      body: JSON.stringify({ text }),
      signal: AbortSignal.timeout(5000),
    });
    if (!response.ok) {
      throw new Error(`OpenCLIP text service returned ${response.status}`);
    }

    const payload = (await response.json()) as {
      embedding?: unknown;
      dimensions?: unknown;
      model?: unknown;
      pretrained?: unknown;
    };
    if (
      payload.model !== VISUAL_EMBEDDING_MODEL
      || payload.pretrained !== VISUAL_EMBEDDING_PRETRAINED
      || payload.dimensions !== VISUAL_EMBEDDING_DIMENSIONS
      || !Array.isArray(payload.embedding)
      || payload.embedding.length !== VISUAL_EMBEDDING_DIMENSIONS
      || payload.embedding.some(
        (value) => typeof value !== "number" || !Number.isFinite(value),
      )
    ) {
      throw new Error("OpenCLIP text service returned an invalid embedding");
    }

    return payload.embedding as number[];
  });
}

function parseNumber(
  value: unknown,
  fallback: number,
  minimum: number,
  maximum: number,
): number {
  if (typeof value !== "string" || value.trim() === "") {
    return fallback;
  }
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) {
    return fallback;
  }
  return Math.min(Math.max(parsed, minimum), maximum);
}

function encodeCursor(offset: number, signature: string): string {
  return Buffer.from(JSON.stringify({ offset, signature })).toString(
    "base64url",
  );
}

function decodeCursor(cursor: string, signature: string): number | null {
  try {
    const decoded = JSON.parse(
      Buffer.from(cursor, "base64url").toString("utf8"),
    ) as { offset?: unknown; signature?: unknown };
    if (
      decoded.signature !== signature
      || typeof decoded.offset !== "number"
      || !Number.isSafeInteger(decoded.offset)
      || decoded.offset < 0
    ) {
      return null;
    }
    return decoded.offset;
  } catch {
    return null;
  }
}

async function metadataSearch(
  embedding: number[],
  limit: number,
): Promise<SearchCandidate[]> {
  const vector = `[${embedding.join(",")}]`;
  return db.transaction(async (tx) => {
    await tx.execute(sql`SELECT
      set_config('hnsw.ef_search', '100', true),
      set_config('hnsw.iterative_scan', 'strict_order', true),
      set_config('hnsw.max_scan_tuples', '50000', true),
      set_config('enable_sort', 'off', true)
    `);
    const rows = await tx.execute(sql`
      WITH nearest AS MATERIALIZED (
        SELECT
          artwork.id,
          artwork."imageAssetId",
          ${timelineYearSql()} AS "timelineYear",
          artwork."txtVec" <=> ${vector}::vector AS distance
        FROM "met-galaxy_artwork" artwork
        WHERE artwork."imageAssetId" IS NOT NULL
          AND artwork."txtVec" IS NOT NULL
          AND artwork."imgVec" IS NOT NULL
          AND artwork."localImageUrl" IS NOT NULL
          AND artwork."localImageUrl" <> ''
        ORDER BY artwork."txtVec" <=> ${vector}::vector
        LIMIT ${Math.min(limit * 4, 50000)}
      ),
      canonical AS (
        SELECT DISTINCT ON ("imageAssetId")
          *
        FROM nearest
        ORDER BY "imageAssetId", distance, id
      )
      SELECT
        id,
        "imageAssetId",
        "timelineYear",
        1 - distance AS similarity
      FROM canonical
      ORDER BY distance, id
      LIMIT ${limit}
    `);
    return Array.from(rows) as SearchCandidate[];
  });
}

async function visualSearch(
  embedding: number[],
  limit: number,
): Promise<SearchCandidate[]> {
  const vector = `[${embedding.join(",")}]`;
  return db.transaction(async (tx) => {
    await tx.execute(sql`SELECT
      set_config('hnsw.ef_search', '100', true),
      set_config('hnsw.iterative_scan', 'strict_order', true),
      set_config('hnsw.max_scan_tuples', '50000', true),
      set_config('enable_seqscan', 'off', true)
    `);
    const rows = await tx.execute(sql`
      WITH nearest AS MATERIALIZED (
        SELECT
          asset.id AS "imageAssetId",
          asset."imageEmbedding" <=> ${vector}::vector AS distance
        FROM "met-galaxy_image_asset" asset
        LEFT JOIN "met-galaxy_image_asset_canonical" mapping
          ON mapping."assetId" = asset.id
        WHERE asset."processingStatus" = 'ready'
          AND asset."imageEmbedding" IS NOT NULL
          AND mapping."assetId" IS NULL
        ORDER BY asset."imageEmbedding" <=> ${vector}::vector
        LIMIT ${limit}
      )
      SELECT
        artwork.id,
        nearest."imageAssetId",
        artwork."timelineYear",
        1 - nearest.distance AS similarity
      FROM nearest
      CROSS JOIN LATERAL (
        SELECT artwork.id, ${timelineYearSql()} AS "timelineYear"
        FROM "met-galaxy_artwork" artwork
        WHERE artwork."imageAssetId" = nearest."imageAssetId"
          AND artwork."localImageUrl" IS NOT NULL
          AND artwork."localImageUrl" <> ''
        ORDER BY artwork.id
        LIMIT 1
      ) artwork
      ORDER BY nearest.distance, artwork.id
    `);
    return Array.from(rows) as SearchCandidate[];
  });
}

async function keywordSearch(
  query: string,
  limit: number,
): Promise<SearchCandidate[]> {
  const parsedIdentifier = /^\d+$/.test(query) ? Number(query) : null;
  const identifier =
    parsedIdentifier !== null
    && Number.isSafeInteger(parsedIdentifier)
    && parsedIdentifier <= 2_147_483_647
      ? parsedIdentifier
      : null;
  const rows = await db.execute(sql`
    WITH search_query AS (
      SELECT websearch_to_tsquery('simple', ${query}) AS query
    ),
    ranked AS MATERIALIZED (
      SELECT
        artwork.id,
        artwork."imageAssetId",
        ${timelineYearSql()} AS "timelineYear",
        COALESCE(
          (
            lower(artwork.title) = lower(${query})
            OR lower(artwork.artist) = lower(${query})
            OR lower(artwork.culture) = lower(${query})
            OR lower(artwork.classification) = lower(${query})
            OR (
              ${identifier}::integer IS NOT NULL
              AND (
                artwork.id = ${identifier}
                OR artwork."objectId" = ${identifier}
              )
            )
          ),
          false
        ) AS "exactMatch",
        ts_rank_cd(artwork."searchDocument", search_query.query, 32)
          + CASE WHEN lower(artwork.title) = lower(${query}) THEN 4 ELSE 0 END
          + CASE WHEN lower(artwork.artist) = lower(${query}) THEN 3 ELSE 0 END
          + CASE WHEN lower(artwork.culture) = lower(${query}) THEN 2 ELSE 0 END
          + CASE
              WHEN lower(artwork.classification) = lower(${query})
              THEN 2
              ELSE 0
            END
          + CASE
              WHEN ${identifier}::integer IS NOT NULL
               AND (
                 artwork.id = ${identifier}
                 OR artwork."objectId" = ${identifier}
               )
              THEN 5
              ELSE 0
            END AS relevance
      FROM "met-galaxy_artwork" artwork
      CROSS JOIN search_query
      WHERE artwork."imageAssetId" IS NOT NULL
        AND artwork."imgVec" IS NOT NULL
        AND artwork."localImageUrl" IS NOT NULL
        AND artwork."localImageUrl" <> ''
        AND (
          artwork."searchDocument" @@ search_query.query
          OR (
            ${identifier}::integer IS NOT NULL
            AND (
              artwork.id = ${identifier}
              OR artwork."objectId" = ${identifier}
            )
          )
        )
      ORDER BY relevance DESC, artwork.id
      LIMIT ${Math.min(limit * 4, 50000)}
    ),
    canonical AS (
      SELECT DISTINCT ON ("imageAssetId")
        *
      FROM ranked
      ORDER BY "imageAssetId", "exactMatch" DESC, relevance DESC, id
    )
    SELECT
      id,
      "imageAssetId",
      "timelineYear",
      "exactMatch",
      relevance AS similarity
    FROM canonical
    ORDER BY "exactMatch" DESC, relevance DESC, id
    LIMIT ${limit}
  `);
  return Array.from(rows) as SearchCandidate[];
}

function fuseRankings(
  rankings: Array<{
    mode: SearchMode;
    weight: number;
    results: SearchCandidate[];
  }>,
  k: number,
): RankedSearchResult[] {
  const fused = new Map<number, RankedSearchResult>();
  for (const ranking of rankings) {
    ranking.results.forEach((candidate, index) => {
      const rank = index + 1;
      const contribution = ranking.weight / (k + rank);
      const exactMatch =
        ranking.mode === "keyword" && candidate.exactMatch === true;
      let existing = fused.get(candidate.imageAssetId);
      if (!existing) {
        existing = {
          ...candidate,
          rrfScore: 0,
          ranks: {},
          similarities: {},
          representativeContribution: contribution,
          representativeExactMatch: exactMatch,
        };
      } else if (
        (exactMatch && !existing.representativeExactMatch)
        || (
          exactMatch === existing.representativeExactMatch
          && contribution > existing.representativeContribution
        )
      ) {
        Object.assign(existing, candidate);
        existing.representativeContribution = contribution;
        existing.representativeExactMatch = exactMatch;
      }
      existing.rrfScore += contribution;
      existing.ranks[ranking.mode] = rank;
      existing.similarities[ranking.mode] = Number(candidate.similarity);
      fused.set(candidate.imageAssetId, existing);
    });
  }
  return [...fused.values()].sort(
    (left, right) =>
      Number(right.representativeExactMatch)
        - Number(left.representativeExactMatch)
      || right.rrfScore - left.rrfScore
      || (left.ranks.keyword ?? Number.MAX_SAFE_INTEGER)
        - (right.ranks.keyword ?? Number.MAX_SAFE_INTEGER)
      || left.imageAssetId - right.imageAssetId,
  );
}

type SearchWeights = Record<SearchMode, number>;
type SearchSnapshot = {
  results: RankedSearchResult[];
  details: Map<number, ArtworkDetails>;
  pendingDetails: Map<string, Promise<void>>;
  candidateLimit: number;
  canExpand: boolean;
  expiresAt: number;
  availableModes: SearchMode[];
  degradedModes: SearchMode[];
  modeErrors: Partial<Record<SearchMode, string>>;
  embeddingCache: { metadata: boolean; visual: boolean };
  timing: { embed: number; search: number; fusion: number };
};

const searchCache = new Map<string, SearchSnapshot>();
const pendingSearches = new Map<string, Promise<SearchSnapshot>>();
const MAX_SEARCH_CANDIDATES = 5000;
const SEARCH_CACHE_TTL_MS = 5 * 60 * 1000;
const DEGRADED_CACHE_TTL_MS = 10000;
const MAX_SEARCH_CACHE_ENTRIES = 32;
const MAX_CACHED_RESULTS = 30000;

async function rankSearch(
  query: string,
  weights: SearchWeights,
  rrfK: number,
  range: TimelineRange | null,
  candidateLimit: number,
): Promise<SearchSnapshot> {
  const modeErrors: Partial<Record<SearchMode, string>> = {};
  const embeddingCache = { metadata: false, visual: false };
  const timing = { embed: 0, search: 0, fusion: 0 };
  const recordError = (mode: SearchMode, stage: string, error: unknown) => {
    const message = error instanceof Error ? error.message : "Unknown error";
    modeErrors[mode] = `${stage}: ${message}`;
    console.error(`[SEARCH] ${mode} ${stage} failed: ${message}`);
  };
  const search = async (mode: SearchMode, run: () => Promise<SearchCandidate[]>) => {
    const startedAt = Date.now();
    try {
      return await run();
    } catch (error) {
      recordError(mode, "search", error);
      return [];
    } finally {
      timing.search = Math.max(timing.search, Date.now() - startedAt);
    }
  };
  const semanticSearch = async (mode: "metadata" | "visual"): Promise<SearchCandidate[]> => {
    if (weights[mode] <= 0) return [];
    const configuration = mode === "metadata"
      ? process.env.OPENAI_API_KEY : process.env.OPENCLIP_TEXT_EMBEDDING_URL;
    if (!configuration) {
      modeErrors[mode] = `configuration: ${mode === "metadata" ? "OPENAI_API_KEY" : "OPENCLIP_TEXT_EMBEDDING_URL"} is not configured`;
      return [];
    }
    const startedAt = Date.now();
    let embedded: EmbeddingResult;
    try {
      embedded = mode === "metadata"
        ? await embedMetadataQuery(query, configuration)
        : await embedVisualQuery(query, configuration, process.env.OPENCLIP_TEXT_AUTH_TOKEN);
      embeddingCache[mode] = embedded.cacheHit;
    } catch (error) {
      recordError(mode, "embedding", error);
      return [];
    } finally {
      timing.embed = Math.max(timing.embed, Date.now() - startedAt);
    }
    return search(mode, () => mode === "metadata"
      ? metadataSearch(embedded.embedding, candidateLimit)
      : visualSearch(embedded.embedding, candidateLimit));
  };

  const [metadata, visual, keyword] = await Promise.all([
    semanticSearch("metadata"),
    semanticSearch("visual"),
    weights.keyword > 0 ? search("keyword", () => keywordSearch(query, candidateLimit)) : [],
  ]);
  const rankings = [
    { mode: "metadata" as const, weight: weights.metadata, results: metadata },
    { mode: "visual" as const, weight: weights.visual, results: visual },
    { mode: "keyword" as const, weight: weights.keyword, results: keyword },
  ].filter((ranking) => ranking.weight > 0 && ranking.results.length > 0);
  const fusionStartedAt = Date.now();
  let results = fuseRankings(rankings, rrfK);
  if (range) results = results.filter((item) => item.timelineYear !== null
    && item.timelineYear >= range.fromYear && item.timelineYear <= range.toYear);
  timing.fusion = Date.now() - fusionStartedAt;
  const degradedModes = (Object.keys(weights) as SearchMode[])
    .filter((mode) => modeErrors[mode] !== undefined);
  return {
    results,
    details: new Map(),
    pendingDetails: new Map(),
    candidateLimit,
    canExpand: candidateLimit < MAX_SEARCH_CANDIDATES
      && rankings.some((ranking) => ranking.results.length === candidateLimit),
    expiresAt: Date.now() + (degradedModes.length ? DEGRADED_CACHE_TTL_MS : SEARCH_CACHE_TTL_MS),
    availableModes: rankings.map((ranking) => ranking.mode),
    degradedModes,
    modeErrors,
    embeddingCache,
    timing,
  };
}

function cacheSearch(signature: string, snapshot: SearchSnapshot) {
  const now = Date.now();
  for (const [key, entry] of searchCache) {
    if (entry.expiresAt <= now) searchCache.delete(key);
  }
  // Do not let a service outage turn into a long-lived empty search result.
  if (snapshot.results.length === 0 && snapshot.degradedModes.length > 0) return;
  searchCache.delete(signature);
  searchCache.set(signature, snapshot);
  let resultCount = [...searchCache.values()].reduce((sum, entry) => sum + entry.results.length, 0);
  while (searchCache.size > MAX_SEARCH_CACHE_ENTRIES || resultCount > MAX_CACHED_RESULTS) {
    const oldestKey = searchCache.keys().next().value!;
    resultCount -= searchCache.get(oldestKey)!.results.length;
    searchCache.delete(oldestKey);
  }
}

async function getSearchSnapshot(
  signature: string,
  required: number,
  initialLimit: number,
  compute: (limit: number) => Promise<SearchSnapshot>,
) {
  let snapshot = searchCache.get(signature);
  if (snapshot && snapshot.expiresAt <= Date.now()) {
    searchCache.delete(signature);
    snapshot = undefined;
  }
  let computed = false;
  while (!snapshot || (snapshot.results.length < required && snapshot.canExpand)) {
    const existing = pendingSearches.get(signature);
    if (existing) {
      snapshot = await existing;
      continue;
    }
    const previous = snapshot;
    const limit = previous
      ? Math.min(Math.max(previous.candidateLimit * 2, initialLimit), MAX_SEARCH_CANDIDATES)
      : initialLimit;
    const request = compute(limit).then((next) => {
      if (previous) {
        // Keep the existing order when extending a pool so later pages cannot repeat earlier tiles.
        const seen = new Set(previous.results.map((item) => item.imageAssetId));
        next.results = [...previous.results, ...next.results.filter((item) => !seen.has(item.imageAssetId))];
        next.details = previous.details;
        next.pendingDetails = previous.pendingDetails;
        next.expiresAt = Math.min(next.expiresAt, previous.expiresAt);
      }
      cacheSearch(signature, next);
      return next;
    }).finally(() => pendingSearches.delete(signature));
    pendingSearches.set(signature, request);
    snapshot = await request;
    computed = true;
  }
  if (searchCache.has(signature)) {
    searchCache.delete(signature);
    searchCache.set(signature, snapshot);
  }
  return { snapshot, cacheHit: !computed };
}

async function hydratePage(snapshot: SearchSnapshot, page: RankedSearchResult[]) {
  const missing = page.filter((item) => !snapshot.details.has(item.id));
  if (missing.length > 0) {
    const key = missing.map((item) => item.id).join(",");
    let request = snapshot.pendingDetails.get(key);
    if (!request) {
      request = db.execute(sql`
        SELECT artwork.id, artwork."objectId", artwork.title, artwork.artist,
          artwork.date, artwork.department, artwork.culture, artwork.medium,
          artwork."creditLine", artwork.description, artwork."localImageUrl",
          artwork."primaryImage", artwork."objectUrl"
        FROM "met-galaxy_artwork" artwork
        WHERE artwork.id IN (${sql.join(missing.map((item) => sql`${item.id}`), sql`, `)})
      `).then((rows) => {
        for (const row of Array.from(rows) as ArtworkDetails[]) snapshot.details.set(row.id, row);
      }).finally(() => snapshot.pendingDetails.delete(key));
      snapshot.pendingDetails.set(key, request);
    }
    await request;
  }
  return page.flatMap((item) => {
    const details = snapshot.details.get(item.id);
    return details ? [{ ...details, ...item }] : [];
  });
}

router.get("/search", async (req, res) => {
  const startedAt = Date.now();
  try {
    const rawQuery =
      typeof req.query.q === "string" ? req.query.q.trim() : "";
    if (rawQuery.length < 2) {
      return res.status(400).json({
        success: false,
        error: "Query must be at least 2 characters",
      });
    }

    const query = rawQuery.slice(0, 500);
    const range = parseTimelineRange(req.query.fromYear, req.query.toYear);
    if (range === "invalid") return res.status(400).json({ success: false, error: "Invalid timeline range" });
    const count = Math.trunc(
      parseNumber(req.query.count, 50, 1, 100),
    );
    const weights = {
      metadata: parseNumber(
        req.query.w_metadata ?? req.query.w_text,
        1,
        0,
        10,
      ),
      visual: parseNumber(
        req.query.w_visual ?? req.query.w_image,
        1,
        0,
        10,
      ),
      keyword: parseNumber(req.query.w_keyword, 1.25, 0, 10),
    };
    const rrfK = Math.trunc(
      parseNumber(req.query.k_rrf, 60, 1, 1000),
    );
    const signature = JSON.stringify({ query, weights, rrfK, range });
    const cursor =
      typeof req.query.cursor === "string" ? req.query.cursor : null;
    const offset = cursor ? decodeCursor(cursor, signature) : 0;
    if (offset === null) {
      return res.status(400).json({
        success: false,
        error: "Invalid search cursor",
      });
    }

    const candidateLimit = Math.min(
      Math.max((offset + count + 1) * 4, 200),
      5000,
    );
    const { snapshot, cacheHit } = await getSearchSnapshot(
      JSON.stringify({ signature, count }), offset + count + 1, candidateLimit,
      (limit) => rankSearch(query, weights, rrfK, range, limit),
    );
    if (res.destroyed) return;
    const rankedPage = snapshot.results.slice(offset, offset + count);
    const hydrateStartedAt = Date.now();
    const page = await hydratePage(snapshot, rankedPage);
    const hydrateTime = Date.now() - hydrateStartedAt;
    const hasMore = snapshot.results.length > offset + count;
    const { availableModes, degradedModes, modeErrors } = snapshot;
    const { embed: embedTime, search: searchTime, fusion: fusionTime } = cacheHit
      ? { embed: 0, search: 0, fusion: 0 } : snapshot.timing;

    const data = page.map((artwork) => {
      const semanticSimilarities = [
        artwork.similarities.metadata,
        artwork.similarities.visual,
      ].filter((value): value is number => value !== undefined);
      return {
        id: artwork.id,
        canonicalAssetId: artwork.imageAssetId,
        objectId: artwork.objectId,
        title: artwork.title,
        artist: artwork.artist,
        date: artwork.date,
        department: artwork.department,
        culture: artwork.culture,
        medium: artwork.medium,
        creditLine: artwork.creditLine,
        description: artwork.description,
        imageUrl: getGraphImageUrl(artwork),
        originalImageUrl: getFullImageUrl(artwork),
        imageSource: "s3",
        objectUrl: artwork.objectUrl,
        similarity:
          semanticSimilarities.length > 0
            ? Math.max(...semanticSimilarities)
            : null,
        rrfScore: artwork.rrfScore,
        exactMatch: artwork.representativeExactMatch,
        subscores: {
          metadataRank: artwork.ranks.metadata ?? null,
          visualRank: artwork.ranks.visual ?? null,
          keywordRank: artwork.ranks.keyword ?? null,
          metadataSimilarity: artwork.similarities.metadata ?? null,
          visualSimilarity: artwork.similarities.visual ?? null,
          keywordScore: artwork.similarities.keyword ?? null,
        },
      };
    });

    const totalTime = Date.now() - startedAt;
    console.log(
      `[SEARCH] q="${query.slice(0, 100)}" offset=${offset}`
      + ` results=${data.length} modes=${availableModes.join(",")}`
      + ` embed=${embedTime}ms search=${searchTime}ms`
      + ` fusion=${fusionTime}ms total=${totalTime}ms`,
    );

    res.json({
      success: true,
      data,
      meta: {
        query,
        count: data.length,
        hasMore,
        nextCursor: hasMore
          ? encodeCursor(offset + rankedPage.length, signature)
          : null,
        weights,
        rrfK,
        models: {
          metadata: {
            name: METADATA_EMBEDDING_MODEL,
            dimensions: METADATA_EMBEDDING_DIMENSIONS,
          },
          visual: {
            name: VISUAL_EMBEDDING_MODEL,
            pretrained: VISUAL_EMBEDDING_PRETRAINED,
            dimensions: VISUAL_EMBEDDING_DIMENSIONS,
          },
        },
        availableModes,
        degradedModes,
        modeErrors,
        cache: {
          ...snapshot.embeddingCache,
          results: cacheHit,
        },
        timing: {
          embed: `${embedTime}ms`,
          search: `${searchTime}ms`,
          fusion: `${fusionTime}ms`,
          hydrate: `${hydrateTime}ms`,
          total: `${totalTime}ms`,
        },
      },
    });
  } catch (error) {
    console.error(
      `[SEARCH] Failed after ${Date.now() - startedAt}ms:`,
      error instanceof Error ? error.message : "Unknown error",
    );
    res.status(500).json({
      success: false,
      error: "Search failed",
    });
  }
});

export default router;
