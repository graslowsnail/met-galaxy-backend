import { sql } from "drizzle-orm";
import { index, pgTableCreator, text, integer, timestamp, varchar, boolean, vector } from "drizzle-orm/pg-core";

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
  objectId: integer("objectId").notNull(),
  title: text("title"), // Changed from varchar(500) to text
  artist: text("artist"), // Changed from varchar(500) to text
  date: varchar("date", { length: 200 }), // Increased from 100
  medium: text("medium"), // Changed from varchar(500) to text
  primaryImage: varchar("primaryImage", { length: 1000 }),
  localImageUrl: varchar("localImageUrl", { length: 1000 }),
  imgVec: vector("imgVec", { dimensions: 768 }), // CLIP ViT-L/14 embeddings
  department: varchar("department", { length: 300 }), // Increased from 200
  culture: varchar("culture", { length: 300 }), // Increased from 200
  createdAt: timestamp("createdAt", { withTimezone: true }),
  additionalImages: text("additionalImages"), // JSON array as text
  objectUrl: varchar("objectUrl", { length: 1000 }), // Increased from 500
  isHighlight: boolean("isHighlight"),
  artistDisplayBio: text("artistDisplayBio"),
  objectBeginDate: integer("objectBeginDate"),
  objectEndDate: integer("objectEndDate"),
  creditLine: text("creditLine"),
  classification: varchar("classification", { length: 500 }), // Increased from 200
  artistNationality: varchar("artistNationality", { length: 500 }), // Increased from 200
  primaryImageSmall: varchar("primaryImageSmall", { length: 1000 }),
  description: text("description"),
  importedAt: timestamp("importedAt", { withTimezone: true })
    .default(sql`CURRENT_TIMESTAMP`)
    .notNull(),
});


