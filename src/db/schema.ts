import { sql } from "drizzle-orm";
import { index, pgTableCreator, text, integer, timestamp, varchar, boolean } from "drizzle-orm/pg-core";

/**
 * This is an example of how to use the multi-project schema feature of Drizzle ORM. Use the same
 * database instance for multiple projects.
 *
 * @see https://orm.drizzle.team/docs/goodies#multi-project-schema
 */
export const createTable = pgTableCreator((name) => `met-galaxy_${name}`);

// Met Museum artwork schema with larger field sizes
export const artworks = createTable("artwork", {
  id: integer("id").primaryKey(),
  objectId: integer("object_id").notNull(),
  title: text("title"), // Changed from varchar(500) to text
  artist: text("artist"), // Changed from varchar(500) to text
  date: varchar("date", { length: 200 }), // Increased from 100
  medium: text("medium"), // Changed from varchar(500) to text
  primaryImage: varchar("primary_image", { length: 1000 }),
  department: varchar("department", { length: 300 }), // Increased from 200
  culture: varchar("culture", { length: 300 }), // Increased from 200
  createdAt: timestamp("created_at", { withTimezone: true }),
  additionalImages: text("additional_images"), // JSON array as text
  objectUrl: varchar("object_url", { length: 1000 }), // Increased from 500
  isHighlight: boolean("is_highlight"),
  artistDisplayBio: text("artist_display_bio"),
  objectBeginDate: integer("object_begin_date"),
  objectEndDate: integer("object_end_date"),
  creditLine: text("credit_line"),
  classification: varchar("classification", { length: 500 }), // Increased from 200
  artistNationality: varchar("artist_nationality", { length: 500 }), // Increased from 200
  primaryImageSmall: varchar("primary_image_small", { length: 1000 }),
  description: text("description"),
  importedAt: timestamp("imported_at", { withTimezone: true })
    .default(sql`CURRENT_TIMESTAMP`)
    .notNull(),
});

// Export the schema for drizzle-kit
export * from 'drizzle-orm/pg-core';
