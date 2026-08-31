import { Router } from "express";
import { sql } from "drizzle-orm";
import { db } from "../db/index.js";
import { getFullImageUrl, getGraphImageUrl, getImageSource } from "../lib/imageUrls.js";
import { parseTimelineRange, timelineFilter, timelineYearSql } from "../lib/timeline.js";
import { likeIpRateLimit, likeVoterRateLimit, requireTrustedOrigin } from "../middleware/likeGuards.js";

const router = Router();
const VOTER_ID_PATTERN = /^[a-zA-Z0-9_-]{16,128}$/;

type LikedArtworkRow = {
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
  primaryImageSmall: string | null;
  objectUrl: string | null;
  objectBeginDate: number | null;
  objectEndDate: number | null;
  timelineYear: number | null;
  likeCount: number;
  liked: boolean;
};

const parseArtworkId = (value: unknown) => {
  if (typeof value !== "string") return null;
  const artworkId = Number.parseInt(value, 10);
  return Number.isSafeInteger(artworkId) && artworkId > 0 ? artworkId : null;
};

const parseVoterId = (value: unknown) => (
  typeof value === "string" && VOTER_ID_PATTERN.test(value) ? value : null
);

const getLikeState = async (artworkId: number, voterId: string) => {
  const result = await db.execute(sql`
    SELECT
      COUNT(*)::integer AS "likeCount",
      BOOL_OR("voterId" = ${voterId}) AS liked
    FROM "met-galaxy_artwork_like"
    WHERE "artworkId" = ${artworkId}
  `);
  const row = Array.from(result)[0] as {
    likeCount: number;
    liked: boolean | null;
  };
  return {
    artworkId,
    likeCount: row.likeCount,
    liked: row.liked ?? false,
  };
};

router.get("/likes/most", async (req, res) => {
  const count = Math.min(Math.max(Number.parseInt(req.query.count as string, 10) || 20, 1), 50);
  const voterId = req.query.voterId === undefined
    ? null
    : parseVoterId(req.query.voterId);
  if (req.query.voterId !== undefined && !voterId) {
    return res.status(400).json({ success: false, error: "Invalid voter ID" });
  }
  const range = parseTimelineRange(req.query.fromYear, req.query.toYear);
  if (range === "invalid") return res.status(400).json({ success: false, error: "Invalid timeline range" });

  try {
    const result = await db.execute(sql`
      WITH ranked AS (
        SELECT
          like_row."artworkId",
          COUNT(*)::integer AS "likeCount",
          MIN(like_row."createdAt") AS "firstLikedAt"
        FROM "met-galaxy_artwork_like" like_row
        JOIN "met-galaxy_artwork" artwork
          ON artwork.id = like_row."artworkId"
        WHERE TRUE ${timelineFilter(range)}
        GROUP BY like_row."artworkId"
        ORDER BY "likeCount" DESC, "firstLikedAt", like_row."artworkId"
        LIMIT ${count}
      )
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
        artwork."primaryImageSmall",
        artwork."objectUrl",
        artwork."objectBeginDate",
        artwork."objectEndDate",
        ${timelineYearSql()} AS "timelineYear",
        ranked."likeCount",
        ${voterId
          ? sql`EXISTS (
              SELECT 1
              FROM "met-galaxy_artwork_like" own_like
              WHERE own_like."artworkId" = artwork.id
                AND own_like."voterId" = ${voterId}
            )`
          : sql`false`} AS liked
      FROM ranked
      JOIN "met-galaxy_artwork" artwork
        ON artwork.id = ranked."artworkId"
        AND artwork."imageAssetId" IS NOT NULL
        AND artwork."localImageUrl" IS NOT NULL
        AND artwork."localImageUrl" <> ''
      ORDER BY ranked."likeCount" DESC, ranked."firstLikedAt", artwork.id
    `);
    const rows = Array.from(result) as LikedArtworkRow[];

    res.json({
      success: true,
      data: rows.map((artwork) => ({
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
        imageSource: getImageSource(artwork),
        objectUrl: artwork.objectUrl,
        objectBeginDate: artwork.objectBeginDate,
        objectEndDate: artwork.objectEndDate,
        timelineYear: artwork.timelineYear,
        likeCount: artwork.likeCount,
        liked: artwork.liked,
      })),
    });
  } catch (error) {
    const nested = (error as { cause?: unknown })?.cause;
    const cause = nested instanceof Error ? nested.message : null;
    console.error("[MOST-LIKED] Request failed:", cause ?? (error instanceof Error ? error.message : "Unknown error"));
    res.status(500).json({ success: false, error: "Failed to fetch most liked artworks" });
  }
});

router.get("/likes/:artworkId", async (req, res) => {
  const artworkId = parseArtworkId(req.params.artworkId);
  const voterId = parseVoterId(req.query.voterId);
  if (!artworkId || !voterId) {
    return res.status(400).json({ success: false, error: "Invalid artwork or voter ID" });
  }

  try {
    res.json({ success: true, data: await getLikeState(artworkId, voterId) });
  } catch (error) {
    console.error("[LIKE-STATUS] Request failed:", error instanceof Error ? error.message : "Unknown error");
    res.status(500).json({ success: false, error: "Failed to fetch like status" });
  }
});

router.post("/likes/:artworkId", requireTrustedOrigin, likeIpRateLimit, likeVoterRateLimit, async (req, res) => {
  const artworkId = parseArtworkId(req.params.artworkId);
  const voterId = parseVoterId(req.body?.voterId);
  if (!artworkId || !voterId) {
    return res.status(400).json({ success: false, error: "Invalid artwork or voter ID" });
  }

  try {
    await db.execute(sql`
      INSERT INTO "met-galaxy_artwork_like" ("artworkId", "voterId")
      SELECT ${artworkId}, ${voterId}
      FROM "met-galaxy_artwork"
      WHERE id = ${artworkId}
      ON CONFLICT ("artworkId", "voterId") DO NOTHING
    `);
    res.json({ success: true, data: await getLikeState(artworkId, voterId) });
  } catch (error) {
    console.error("[LIKE] Request failed:", error instanceof Error ? error.message : "Unknown error");
    res.status(500).json({ success: false, error: "Failed to like artwork" });
  }
});

router.delete("/likes/:artworkId", requireTrustedOrigin, likeIpRateLimit, likeVoterRateLimit, async (req, res) => {
  const artworkId = parseArtworkId(req.params.artworkId);
  const voterId = parseVoterId(req.body?.voterId);
  if (!artworkId || !voterId) {
    return res.status(400).json({ success: false, error: "Invalid artwork or voter ID" });
  }

  try {
    await db.execute(sql`
      DELETE FROM "met-galaxy_artwork_like"
      WHERE "artworkId" = ${artworkId}
        AND "voterId" = ${voterId}
    `);
    res.json({ success: true, data: await getLikeState(artworkId, voterId) });
  } catch (error) {
    console.error("[UNLIKE] Request failed:", error instanceof Error ? error.message : "Unknown error");
    res.status(500).json({ success: false, error: "Failed to unlike artwork" });
  }
});

export default router;
