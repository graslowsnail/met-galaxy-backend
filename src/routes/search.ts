import { Router } from "express";
import { sql } from "drizzle-orm";
import OpenAI from "openai";
import { db } from "../db/index.js";
import { getFullImageUrl, getGraphImageUrl } from "../lib/imageUrls.js";

const router = Router();

type CachedEmbedding = {
  embedding: number[];
  expiresAt: number;
};

type SearchCandidate = {
  id: number;
  imageAssetId: number;
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
  similarity: number;
  exactMatch?: boolean;
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
  const cached = getCachedEmbedding(metadataEmbeddingCache, text);
  if (cached) {
    return { embedding: cached, cacheHit: true };
  }

  const openai = new OpenAI({ apiKey });
  const response = await openai.embeddings.create({
    model: METADATA_EMBEDDING_MODEL,
    input: text,
    dimensions: METADATA_EMBEDDING_DIMENSIONS,
  });
  const embedding = response.data[0]!.embedding;
  cacheEmbedding(metadataEmbeddingCache, text, embedding);
  return { embedding, cacheHit: false };
}

async function embedVisualQuery(
  text: string,
  serviceUrl: string,
  authToken: string | undefined,
): Promise<{ embedding: number[]; cacheHit: boolean }> {
  const cached = getCachedEmbedding(visualEmbeddingCache, text);
  if (cached) {
    return { embedding: cached, cacheHit: true };
  }

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

  const embedding = payload.embedding as number[];
  cacheEmbedding(visualEmbeddingCache, text, embedding);
  return { embedding, cacheHit: false };
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
    await tx.execute(sql`SET LOCAL hnsw.ef_search = 100`);
    await tx.execute(sql`SET LOCAL hnsw.iterative_scan = 'strict_order'`);
    await tx.execute(sql`SET LOCAL hnsw.max_scan_tuples = 50000`);
    await tx.execute(sql`SET LOCAL enable_sort = off`);
    const rows = await tx.execute(sql`
      WITH nearest AS MATERIALIZED (
        SELECT
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
          artwork."objectUrl",
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
        "objectId",
        title,
        artist,
        date,
        department,
        culture,
        medium,
        "creditLine",
        description,
        "localImageUrl",
        "primaryImage",
        "objectUrl",
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
        WHERE asset."processingStatus" = 'ready'
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
        artwork.date,
        artwork.department,
        artwork.culture,
        artwork.medium,
        artwork."creditLine",
        artwork.description,
        artwork."localImageUrl",
        artwork."primaryImage",
        artwork."objectUrl",
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
        artwork."objectUrl",
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
      "objectId",
      title,
      artist,
      date,
      department,
      culture,
      medium,
      "creditLine",
      description,
      "localImageUrl",
      "primaryImage",
      "objectUrl",
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
    const signature = JSON.stringify({ query, weights, rrfK });
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
    const openaiApiKey = process.env.OPENAI_API_KEY;
    const visualServiceUrl = process.env.OPENCLIP_TEXT_EMBEDDING_URL;
    const modeErrors: Partial<Record<SearchMode, string>> = {};
    const recordModeError = (
      mode: SearchMode,
      stage: string,
      error: unknown,
    ): void => {
      const message = error instanceof Error ? error.message : "Unknown error";
      modeErrors[mode] = `${stage}: ${message}`;
      console.error(`[SEARCH] ${mode} ${stage} failed: ${message}`);
    };
    if (weights.metadata > 0 && !openaiApiKey) {
      modeErrors.metadata = "configuration: OPENAI_API_KEY is not configured";
    }
    if (weights.visual > 0 && !visualServiceUrl) {
      modeErrors.visual =
        "configuration: OPENCLIP_TEXT_EMBEDDING_URL is not configured";
    }
    const embedStartedAt = Date.now();
    const [metadataEmbedding, visualEmbedding] = await Promise.all([
      openaiApiKey && weights.metadata > 0
        ? embedMetadataQuery(query, openaiApiKey).catch((error) => {
            recordModeError("metadata", "embedding", error);
            return null;
          })
        : Promise.resolve(null),
      visualServiceUrl && weights.visual > 0
        ? embedVisualQuery(
            query,
            visualServiceUrl,
            process.env.OPENCLIP_TEXT_AUTH_TOKEN,
          ).catch((error) => {
            recordModeError("visual", "embedding", error);
            return null;
          })
        : Promise.resolve(null),
    ]);
    const embedTime = Date.now() - embedStartedAt;

    const searchStartedAt = Date.now();
    const [metadataResults, visualResults, keywordResults] =
      await Promise.all([
        metadataEmbedding
          ? metadataSearch(metadataEmbedding.embedding, candidateLimit)
              .catch((error) => {
                recordModeError("metadata", "search", error);
                return [];
              })
          : Promise.resolve([]),
        visualEmbedding
          ? visualSearch(visualEmbedding.embedding, candidateLimit)
              .catch((error) => {
                recordModeError("visual", "search", error);
                return [];
              })
          : Promise.resolve([]),
        weights.keyword > 0
          ? keywordSearch(query, candidateLimit).catch((error) => {
              recordModeError("keyword", "search", error);
              return [];
            })
          : Promise.resolve([]),
      ]);
    const searchTime = Date.now() - searchStartedAt;

    const rankings = [
      {
        mode: "metadata" as const,
        weight: weights.metadata,
        results: metadataResults,
      },
      {
        mode: "visual" as const,
        weight: weights.visual,
        results: visualResults,
      },
      {
        mode: "keyword" as const,
        weight: weights.keyword,
        results: keywordResults,
      },
    ].filter((ranking) => ranking.weight > 0 && ranking.results.length > 0);
    const fusionStartedAt = Date.now();
    const fused = fuseRankings(rankings, rrfK);
    const page = fused.slice(offset, offset + count);
    const hasMore = fused.length > offset + count;
    const fusionTime = Date.now() - fusionStartedAt;

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

    const availableModes = rankings.map((ranking) => ranking.mode);
    const requestedModes = (Object.keys(weights) as SearchMode[])
      .filter((mode) => weights[mode] > 0);
    const degradedModes = requestedModes.filter(
      (mode) => modeErrors[mode] !== undefined,
    );
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
          ? encodeCursor(offset + data.length, signature)
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
          metadata: metadataEmbedding?.cacheHit ?? false,
          visual: visualEmbedding?.cacheHit ?? false,
        },
        timing: {
          embed: `${embedTime}ms`,
          search: `${searchTime}ms`,
          fusion: `${fusionTime}ms`,
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
