import { sql } from "drizzle-orm";
import { db } from "../db/index.js";
import { timelineYearSql, type TimelineRange } from "./timeline.js";

type YearCount = { year: number; count: number };
let cachedYears: { rows: YearCount[]; expiresAt: number } | undefined;
let pendingYears: Promise<YearCount[]> | undefined;
const CACHE_TTL_MS = 5 * 60 * 1000;

async function getYearCounts(): Promise<YearCount[]> {
  if (cachedYears && cachedYears.expiresAt > Date.now()) return cachedYears.rows;
  if (pendingYears) return pendingYears;

  pendingYears = db.execute(sql`
    SELECT ${timelineYearSql()}::integer AS year, COUNT(*)::integer AS count
    FROM "met-galaxy_artwork" artwork
    WHERE ${timelineYearSql()} IS NOT NULL
      AND artwork."imageAssetId" IS NOT NULL
      AND artwork."localImageUrl" IS NOT NULL
      AND artwork."localImageUrl" <> ''
    GROUP BY ${timelineYearSql()}
    ORDER BY year
  `).then((result) => {
    const rows = Array.from(result) as YearCount[];
    cachedYears = { rows, expiresAt: Date.now() + CACHE_TTL_MS };
    return rows;
  }).finally(() => {
    pendingYears = undefined;
  });
  return pendingYears;
}

export async function getTimelineSummary(range: TimelineRange | null) {
  const years = await getYearCounts();
  const currentYear = new Date().getUTCFullYear();
  const buckets = Array.from(
    { length: Math.floor((currentYear + 10000) / 100) + 1 },
    (_, index) => ({ fromYear: -10000 + index * 100, count: 0 }),
  );
  const deepTimeBuckets: Array<{ fromYear: number; count: number }> = [];
  let minYear: number | null = null;
  let maxYear: number | null = null;
  let total = 0;
  let selectedCount = 0;

  for (const { year, count } of years) {
    if (year <= currentYear) {
      minYear ??= year;
      maxYear = year;
      total += count;
      if (!range || (year >= range.fromYear && year <= range.toYear)) selectedCount += count;
    }
    if (year < -10000) {
      deepTimeBuckets.push({ fromYear: year, count });
    } else {
      const bucket = buckets[Math.floor((year + 10000) / 100)];
      if (bucket) bucket.count += count;
    }
  }
  return { minYear, maxYear, total, selectedCount, buckets, deepTimeBuckets };
}
