import { Router } from "express";
import { db } from "../db/index.js";
import { artworks } from "../db/schema.js";
import { sql } from "drizzle-orm";
import OpenAI from "openai";

const router = Router();

const embeddingCache = new Map<
  string,
  { embedding: number[]; expiresAt: number }
>();
const EMBEDDING_CACHE_TTL_MS = 30 * 60 * 1000;
const MAX_EMBEDDING_CACHE_SIZE = 100;

async function embedQuery(
  text: string,
  apiKey: string
): Promise<{ embedding: number[]; cacheHit: boolean }> {
  const cached = embeddingCache.get(text);
  if (cached && cached.expiresAt > Date.now()) {
    embeddingCache.delete(text);
    embeddingCache.set(text, cached);
    return { embedding: cached.embedding, cacheHit: true };
  }

  if (cached) {
    embeddingCache.delete(text);
  }

  const openai = new OpenAI({ apiKey });
  const response = await openai.embeddings.create({
    model: "text-embedding-3-small",
    input: text,
  });
  const embedding = response.data[0]!.embedding;

  if (embeddingCache.size >= MAX_EMBEDDING_CACHE_SIZE) {
    const oldestKey = embeddingCache.keys().next().value;
    if (oldestKey !== undefined) {
      embeddingCache.delete(oldestKey);
    }
  }

  embeddingCache.set(text, {
    embedding,
    expiresAt: Date.now() + EMBEDDING_CACHE_TTL_MS,
  });

  return { embedding, cacheHit: false };
}

function encodeCursor(offset: number, query: string): string {
  return Buffer.from(JSON.stringify({ offset, query })).toString("base64url");
}

function decodeCursor(cursor: string, query: string): number | null {
  try {
    const decoded = JSON.parse(
      Buffer.from(cursor, "base64url").toString("utf8")
    ) as { offset?: unknown; query?: unknown };

    if (
      decoded.query !== query ||
      typeof decoded.offset !== "number" ||
      !Number.isSafeInteger(decoded.offset) ||
      decoded.offset < 0
    ) {
      return null;
    }

    return decoded.offset;
  } catch {
    return null;
  }
}

router.get("/search", async (req, res) => {
  const startTime = Date.now();

  try {
    const q = typeof req.query.q === "string" ? req.query.q.trim() : "";
    const requestedCount =
      typeof req.query.count === "string"
        ? Number.parseInt(req.query.count, 10)
        : 100;
    const count = Math.min(
      Math.max(Number.isNaN(requestedCount) ? 100 : requestedCount, 1),
      100
    );

    if (!q || q.length < 2) {
      return res
        .status(400)
        .json({ success: false, error: "Query must be at least 2 characters" });
    }

    const apiKey = process.env.OPENAI_API_KEY;
    if (!apiKey) {
      return res
        .status(503)
        .json({ success: false, error: "Search is not configured" });
    }

    const sanitized = q.slice(0, 500);
    const cursor =
      typeof req.query.cursor === "string" ? req.query.cursor : null;
    const offset = cursor ? decodeCursor(cursor, sanitized) : 0;

    if (offset === null) {
      return res.status(400).json({
        success: false,
        error: "Invalid search cursor",
      });
    }

    const embedStart = Date.now();
    const { embedding: queryVector, cacheHit } = await embedQuery(
      sanitized,
      apiKey
    );
    const embedTime = Date.now() - embedStart;

    const vectorString = `[${queryVector.join(",")}]`;

    const searchStart = Date.now();
    const results = await db.transaction(async (tx) => {
      await tx.execute(sql`SET LOCAL hnsw.ef_search = 100`);
      await tx.execute(sql`SET LOCAL hnsw.iterative_scan = 'strict_order'`);
      await tx.execute(sql`SET LOCAL hnsw.max_scan_tuples = 50000`);

      return tx
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
          similarity:
            sql<number>`1 - ("txtVec" <=> ${vectorString}::vector)`.as(
              "similarity"
            ),
        })
        .from(artworks)
        .where(
          sql`"txtVec" IS NOT NULL AND "imgVec" IS NOT NULL AND "localImageUrl" IS NOT NULL AND "localImageUrl" != ''`
        )
        .orderBy(sql`"txtVec" <=> ${vectorString}::vector`)
        .limit(count + 1)
        .offset(offset);
    });
    const searchTime = Date.now() - searchStart;
    const hasMore = results.length > count;
    const pageResults = results.slice(0, count);

    const data = pageResults.map((artwork) => ({
      id: artwork.id,
      objectId: artwork.objectId,
      title: artwork.title,
      artist: artwork.artist,
      date: artwork.date,
      department: artwork.department,
      culture: artwork.culture,
      medium: artwork.medium,
      creditLine: artwork.creditLine,
      description: artwork.description,
      imageUrl: artwork.localImageUrl,
      originalImageUrl: artwork.primaryImage,
      imageSource: "s3",
      objectUrl: artwork.objectUrl,
      similarity: artwork.similarity,
    }));

    const totalTime = Date.now() - startTime;
    console.log(
      `[SEARCH] q="${sanitized}" offset=${offset} | ${data.length} results | embed=${embedTime}ms${cacheHit ? " cached" : ""} search=${searchTime}ms total=${totalTime}ms`
    );

    res.json({
      success: true,
      data,
      meta: {
        query: sanitized,
        count: data.length,
        hasMore,
        nextCursor: hasMore
          ? encodeCursor(offset + data.length, sanitized)
          : null,
        timing: {
          embed: `${embedTime}ms`,
          search: `${searchTime}ms`,
          total: `${totalTime}ms`,
        },
      },
    });
  } catch (error) {
    console.error(
      `[SEARCH] Failed after ${Date.now() - startTime}ms:`,
      error instanceof Error ? error.message : "Unknown error"
    );
    res.status(500).json({
      success: false,
      error: "Search failed",
    });
  }
});

export default router;
