import { Router } from "express";
import { db } from "../db/index.js";
import { artworks } from "../db/schema.js";
import { sql } from "drizzle-orm";
import OpenAI from "openai";

const router = Router();

async function embedQuery(text: string, apiKey: string): Promise<number[]> {
  const openai = new OpenAI({ apiKey });
  const response = await openai.embeddings.create({
    model: "text-embedding-3-small",
    input: text,
  });
  return response.data[0]!.embedding;
}

router.get("/search", async (req, res) => {
  const startTime = Date.now();

  try {
    const q = typeof req.query.q === "string" ? req.query.q.trim() : "";
    const requestedCount =
      typeof req.query.count === "string"
        ? Number.parseInt(req.query.count, 10)
        : 50;
    const count = Math.min(
      Math.max(Number.isNaN(requestedCount) ? 50 : requestedCount, 1),
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

    const embedStart = Date.now();
    const queryVector = await embedQuery(sanitized, apiKey);
    const embedTime = Date.now() - embedStart;

    const vectorString = `[${queryVector.join(",")}]`;

    const searchStart = Date.now();
    const results = await db
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
      .limit(count);
    const searchTime = Date.now() - searchStart;

    const data = results.map((artwork) => ({
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
      `[SEARCH] q="${sanitized}" | ${data.length} results | embed=${embedTime}ms search=${searchTime}ms total=${totalTime}ms`
    );

    res.json({
      success: true,
      data,
      meta: {
        query: sanitized,
        count: data.length,
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
