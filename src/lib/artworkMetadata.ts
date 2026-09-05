import { sql } from "drizzle-orm";
import type { AnyPgColumn } from "drizzle-orm/pg-core";
import { db } from "../db/index.js";
import { artworks } from "../db/schema.js";

const pending = new Map<number, Promise<void>>();
const failures = new Map<number, { attempts: number; retryAt: number }>();
const MAX_CONCURRENT_REQUESTS = 4;
const MAX_FAILURES = 500;

function fillMissing(column: AnyPgColumn, value: unknown) {
  if (typeof value !== "string" || !value.trim()) return undefined;
  return sql`CASE WHEN btrim(coalesce(${column}, '')) = '' THEN ${value.trim()} ELSE ${column} END`;
}

async function enrichArtwork(id: number, objectId: number): Promise<void> {
  try {
    const response = await fetch(
      `https://collectionapi.metmuseum.org/public/collection/v1/objects/${objectId}`,
      { signal: AbortSignal.timeout(5000) },
    );
    if (!response.ok) throw new Error(`The Met returned ${response.status}`);
    const metadata = await response.json() as Record<string, unknown>;
    if (!metadata || metadata.objectID !== objectId) throw new Error("Invalid Met artwork response");

    await db.update(artworks).set({
      title: fillMissing(artworks.title, metadata.title),
      artist: fillMissing(artworks.artist, metadata.artistDisplayName),
      date: fillMissing(artworks.date, metadata.objectDate),
      department: fillMissing(artworks.department, metadata.department),
      culture: fillMissing(artworks.culture, metadata.culture),
      medium: fillMissing(artworks.medium, metadata.medium),
      creditLine: fillMissing(artworks.creditLine, metadata.creditLine),
      accessionNumber: fillMissing(artworks.accessionNumber, metadata.accessionNumber),
      objectUrl: fillMissing(artworks.objectUrl, metadata.objectURL),
      metMetadataFetchedAt: new Date(),
    }).where(sql`${artworks.id} = ${id} AND ${artworks.metMetadataFetchedAt} IS NULL`);
    failures.delete(id);
  } catch (error) {
    const attempts = Math.min((failures.get(id)?.attempts ?? 0) + 1, 5);
    failures.delete(id);
    if (failures.size >= MAX_FAILURES) failures.delete(failures.keys().next().value!);
    failures.set(id, { attempts, retryAt: Date.now() + Math.min(60000 * 2 ** (attempts - 1), 900000) });
    console.warn(`[ARTWORK] The Met enrichment failed for object ${objectId}:`,
      error instanceof Error ? error.message : "Unknown error");
  } finally {
    pending.delete(id);
  }
}

export function requestArtworkMetadata(artwork: {
  id: number;
  objectId: number;
  metMetadataFetchedAt: Date | null;
}): { metadataStatus: "complete" | "pending" | "deferred"; retryAfterMs?: number } {
  if (artwork.metMetadataFetchedAt) return { metadataStatus: "complete" };
  if (pending.has(artwork.id)) return { metadataStatus: "pending", retryAfterMs: 1000 };
  const retryAt = failures.get(artwork.id)?.retryAt ?? 0;
  if (retryAt > Date.now()) return { metadataStatus: "deferred", retryAfterMs: retryAt - Date.now() };
  if (pending.size >= MAX_CONCURRENT_REQUESTS) return { metadataStatus: "deferred", retryAfterMs: 2000 };

  pending.set(artwork.id, enrichArtwork(artwork.id, artwork.objectId));
  return { metadataStatus: "pending", retryAfterMs: 1000 };
}
