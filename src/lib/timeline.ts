import { sql, type SQL } from "drizzle-orm";

export type TimelineRange = { fromYear: number; toYear: number };
export const timelineYearSql = (alias = "artwork"): SQL => sql.raw(`CASE WHEN ${alias}."objectBeginDate" IS NULL THEN ${alias}."objectEndDate" WHEN ${alias}."objectEndDate" IS NULL THEN ${alias}."objectBeginDate" ELSE floor((least(${alias}."objectBeginDate", ${alias}."objectEndDate") + greatest(${alias}."objectBeginDate", ${alias}."objectEndDate")) / 2.0)::integer END`);

export function parseTimelineRange(from: unknown, to: unknown): TimelineRange | null | "invalid" {
  if (from === undefined && to === undefined) return null;
  const fromYear = Number(from);
  const toYear = Number(to);
  const currentYear = new Date().getUTCFullYear();
  if (!Number.isSafeInteger(fromYear) || !Number.isSafeInteger(toYear) || fromYear > toYear || toYear > currentYear) return "invalid";
  return { fromYear, toYear };
}

export const timelineFilter = (range: TimelineRange | null, alias = "artwork"): SQL => range
  ? sql`AND ${timelineYearSql(alias)} BETWEEN ${range.fromYear} AND ${range.toYear}`
  : sql``;
