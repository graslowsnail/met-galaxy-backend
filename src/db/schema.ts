import { sql } from "drizzle-orm";
import { bigint, boolean, check, customType, index, integer, pgEnum, pgTableCreator, primaryKey, serial, text, timestamp, uniqueIndex, varchar, vector } from "drizzle-orm/pg-core";

/**
 * This is an example of how to use the multi-project schema feature of Drizzle ORM. Use the same
 * database instance for multiple projects.
 *
 * @see https://orm.drizzle.team/docs/goodies#multi-project-schema
 */
export const createTable = pgTableCreator((name) => `met-galaxy_${name}`);
const tsvector = customType<{ data: string }>({
  dataType() {
    return "tsvector";
  },
});

export const imageAssetProcessingStatus = pgEnum("image_asset_processing_status", [
  "pending_upload",
  "pending_embedding",
  "ready",
]);

export const imageAssociationDuplicateState = pgEnum(
  "image_association_duplicate_state",
  ["unique", "exact_duplicate", "verified_duplicate"],
);

export const imageDuplicateCandidateStatus = pgEnum(
  "image_duplicate_candidate_status",
  ["review_candidate", "verified_duplicate", "rejected"],
);

export const imageIngestionStatus = pgEnum("image_ingestion_status", [
  "pending",
  "processing",
  "awaiting_embedding",
  "complete",
  "retryable_failure",
  "terminal_failure",
]);

export const imageIngestionAttemptOutcome = pgEnum(
  "image_ingestion_attempt_outcome",
  [
    "new_asset",
    "exact_duplicate",
    "already_linked",
    "retryable_failure",
    "terminal_failure",
  ],
);

export const imageEmbeddingOutboxStatus = pgEnum(
  "image_embedding_outbox_status",
  ["pending", "dispatched"],
);

export const imageHashAlgorithm = pgEnum("image_hash_algorithm", [
  "phash64",
  "dhash64",
]);

export const imageAssets = createTable(
  "image_asset",
  {
    id: serial("id").primaryKey(),
    fullS3Key: text("fullS3Key").unique(),
    thumbnailS3Key: text("thumbnailS3Key").unique(),
    mimeType: varchar("mimeType", { length: 100 }),
    width: integer("width"),
    height: integer("height"),
    byteSize: bigint("byteSize", { mode: "number" }),
    encodedSha256: varchar("encodedSha256", { length: 64 }).unique(),
    thumbnailEncodedSha256: varchar("thumbnailEncodedSha256", { length: 64 }),
    normalizedPixelSha256: varchar("normalizedPixelSha256", { length: 64 }).unique(),
    perceptualHash: text("perceptualHash"),
    perceptualHashAlgorithm: varchar("perceptualHashAlgorithm", { length: 100 }),
    differenceHash: text("differenceHash"),
    differenceHashAlgorithm: varchar("differenceHashAlgorithm", { length: 100 }),
    thumbnailByteSize: bigint("thumbnailByteSize", { mode: "number" }),
    processingStatus: imageAssetProcessingStatus("processingStatus")
      .default("pending_upload")
      .notNull(),
    processingAttemptCount: integer("processingAttemptCount")
      .default(0)
      .notNull(),
    processingNextAttemptAt: timestamp("processingNextAttemptAt", {
      withTimezone: true,
    }),
    processingLeaseOwner: text("processingLeaseOwner"),
    processingLeaseExpiresAt: timestamp("processingLeaseExpiresAt", {
      withTimezone: true,
    }),
    imageEmbedding: vector("imageEmbedding", { dimensions: 768 }),
    lastError: text("lastError"),
    createdAt: timestamp("createdAt", { withTimezone: true })
      .default(sql`CURRENT_TIMESTAMP`)
      .notNull(),
    updatedAt: timestamp("updatedAt", { withTimezone: true })
      .default(sql`CURRENT_TIMESTAMP`)
      .notNull(),
  },
  (table) => [
    index("idx_image_assets_embedding_hnsw")
      .using("hnsw", table.imageEmbedding.op("vector_cosine_ops"))
      .with({ m: 16, ef_construction: 64 })
      .where(
        sql`${table.imageEmbedding} IS NOT NULL AND ${table.processingStatus} = 'ready'`,
      ),
    index("idx_image_assets_processing_queue").on(
      table.processingStatus,
      table.processingNextAttemptAt,
    ),
    check(
      "chk_image_assets_processing_attempt_count",
      sql`${table.processingAttemptCount} >= 0`,
    ),
  ],
);

export const imageDuplicateCandidates = createTable(
  "image_duplicate_candidate",
  {
    id: serial("id").primaryKey(),
    imageAssetAId: integer("imageAssetAId")
      .notNull()
      .references(() => imageAssets.id, { onDelete: "restrict" }),
    imageAssetBId: integer("imageAssetBId")
      .notNull()
      .references(() => imageAssets.id, { onDelete: "restrict" }),
    status: imageDuplicateCandidateStatus("status")
      .default("review_candidate")
      .notNull(),
    createdAt: timestamp("createdAt", { withTimezone: true })
      .default(sql`CURRENT_TIMESTAMP`)
      .notNull(),
    reviewedAt: timestamp("reviewedAt", { withTimezone: true }),
    perceptualHashDistance: integer("perceptualHashDistance"),
    differenceHashDistance: integer("differenceHashDistance"),
  },
  (table) => [
    uniqueIndex("uq_image_duplicate_candidate_pair").on(
      table.imageAssetAId,
      table.imageAssetBId,
    ),
    index("idx_image_duplicate_candidate_status").on(table.status),
    check(
      "chk_image_duplicate_candidate_order",
      sql`${table.imageAssetAId} < ${table.imageAssetBId}`,
    ),
  ],
);

export const imageAssetCanonicalMappings = createTable(
  "image_asset_canonical",
  {
    assetId: integer("assetId")
      .primaryKey()
      .references(() => imageAssets.id, { onDelete: "restrict" }),
    canonicalAssetId: integer("canonicalAssetId")
      .notNull()
      .references(() => imageAssets.id, { onDelete: "restrict" }),
    createdAt: timestamp("createdAt", { withTimezone: true })
      .default(sql`CURRENT_TIMESTAMP`)
      .notNull(),
    updatedAt: timestamp("updatedAt", { withTimezone: true })
      .default(sql`CURRENT_TIMESTAMP`)
      .notNull(),
  },
  (table) => [
    index("idx_image_asset_canonical_root").on(table.canonicalAssetId),
    check(
      "chk_image_asset_canonical_not_self",
      sql`${table.assetId} <> ${table.canonicalAssetId}`,
    ),
  ],
);

export const imagePerceptualHashBands = createTable(
  "image_perceptual_hash_band",
  {
    imageAssetId: integer("imageAssetId")
      .notNull()
      .references(() => imageAssets.id, { onDelete: "cascade" }),
    algorithm: imageHashAlgorithm("algorithm").notNull(),
    bandIndex: integer("bandIndex").notNull(),
    bandValue: varchar("bandValue", { length: 16 }).notNull(),
  },
  (table) => [
    primaryKey({
      columns: [table.imageAssetId, table.algorithm, table.bandIndex],
    }),
    index("idx_image_hash_bands_lookup").on(
      table.algorithm,
      table.bandIndex,
      table.bandValue,
    ),
    check("chk_image_hash_band_index", sql`${table.bandIndex} >= 0`),
  ],
);

export const imageEmbeddingOutbox = createTable(
  "image_embedding_outbox",
  {
    imageAssetId: integer("imageAssetId")
      .primaryKey()
      .references(() => imageAssets.id, { onDelete: "cascade" }),
    status: imageEmbeddingOutboxStatus("status").default("pending").notNull(),
    attemptCount: integer("attemptCount").default(0).notNull(),
    nextAttemptAt: timestamp("nextAttemptAt", { withTimezone: true }),
    messageId: text("messageId"),
    lastError: text("lastError"),
    createdAt: timestamp("createdAt", { withTimezone: true })
      .default(sql`CURRENT_TIMESTAMP`)
      .notNull(),
    dispatchedAt: timestamp("dispatchedAt", { withTimezone: true }),
  },
  (table) => [
    index("idx_image_embedding_outbox_dispatch").on(
      table.status,
      table.nextAttemptAt,
    ),
    check(
      "chk_image_embedding_outbox_attempt_count",
      sql`${table.attemptCount} >= 0`,
    ),
  ],
);

// Met Museum artwork schema with larger field sizes
export const artworks = createTable(
  "artwork",
  {
    id: integer("id").primaryKey(),
    objectId: integer("objectId").notNull(),
    title: text("title"), // Changed from varchar(500) to text
    artist: text("artist"), // Changed from varchar(500) to text
    date: varchar("date", { length: 200 }), // Increased from 100
    medium: text("medium"), // Changed from varchar(500) to text
    primaryImage: varchar("primaryImage", { length: 1000 }),
    localImageUrl: varchar("localImageUrl", { length: 1000 }),
    imageAssetId: integer("imageAssetId").references(() => imageAssets.id, {
      onDelete: "restrict",
    }),
    imageDuplicateState: imageAssociationDuplicateState("imageDuplicateState"),
    imgVec: vector("imgVec", { dimensions: 768 }), // CLIP ViT-L/14 embeddings
    txtVec: vector("txtVec", { dimensions: 1536 }), // Text embeddings for metadata search
    txtVecAttemptCount: integer("txtVecAttemptCount").default(0).notNull(),
    txtVecNextAttemptAt: timestamp("txtVecNextAttemptAt", {
      withTimezone: true,
    }),
    txtVecLeaseOwner: text("txtVecLeaseOwner"),
    txtVecLeaseExpiresAt: timestamp("txtVecLeaseExpiresAt", {
      withTimezone: true,
    }),
    txtVecLastError: text("txtVecLastError"),
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
    accessionNumber: varchar("accessionNumber", { length: 100 }),
    classification: varchar("classification", { length: 500 }), // Increased from 200
    artistNationality: varchar("artistNationality", { length: 500 }), // Increased from 200
    primaryImageSmall: varchar("primaryImageSmall", { length: 1000 }),
    description: text("description"),
    searchDocument: tsvector("searchDocument").generatedAlwaysAs(sql`
      setweight(to_tsvector('simple', coalesce("title", '')), 'A')
      || setweight(to_tsvector('simple', coalesce("artist", '')), 'A')
      || setweight(to_tsvector('simple', coalesce("classification", '')), 'B')
      || setweight(to_tsvector('simple', coalesce("culture", '')), 'B')
      || setweight(to_tsvector('simple', coalesce("department", '')), 'C')
      || setweight(to_tsvector('simple', coalesce("medium", '')), 'C')
      || setweight(to_tsvector('simple', "id"::text), 'A')
      || setweight(to_tsvector('simple', "objectId"::text), 'A')
    `),
    importedAt: timestamp("importedAt", { withTimezone: true })
      .default(sql`CURRENT_TIMESTAMP`)
      .notNull(),
    metMetadataFetchedAt: timestamp("metMetadataFetchedAt", { withTimezone: true }),
  },
  (table) => [
    index("idx_artworks_image_asset_id").on(table.imageAssetId),
    index("idx_artworks_txtvec_eligible")
      .using("hnsw", table.txtVec.op("vector_cosine_ops"))
      .with({ m: 16, ef_construction: 64 })
      .where(
        sql`${table.txtVec} IS NOT NULL AND ${table.imgVec} IS NOT NULL AND ${table.imageAssetId} IS NOT NULL AND ${table.localImageUrl} IS NOT NULL AND ${table.localImageUrl} <> ''`,
      ),
    index("idx_artworks_txt_vec_queue").on(
      table.txtVecAttemptCount,
      table.txtVecNextAttemptAt,
      table.txtVecLeaseExpiresAt,
    ),
    index("idx_artworks_search_document_gin").using(
      "gin",
      table.searchDocument,
    ),
    index("idx_artworks_object_id").on(table.objectId),
    index("idx_artworks_timeline_year_id")
      .on(sql`(CASE WHEN ${table.objectBeginDate} IS NULL THEN ${table.objectEndDate} WHEN ${table.objectEndDate} IS NULL THEN ${table.objectBeginDate} ELSE floor((least(${table.objectBeginDate}, ${table.objectEndDate}) + greatest(${table.objectBeginDate}, ${table.objectEndDate})) / 2.0)::integer END)`, table.id)
      .where(sql`${table.imageAssetId} IS NOT NULL AND ${table.localImageUrl} IS NOT NULL AND ${table.localImageUrl} <> ''`),
    check(
      "chk_artworks_txt_vec_attempt_count",
      sql`${table.txtVecAttemptCount} >= 0`,
    ),
  ],
);

export const artworkLikes = createTable(
  "artwork_like",
  {
    artworkId: integer("artworkId")
      .notNull()
      .references(() => artworks.id, { onDelete: "cascade" }),
    voterId: varchar("voterId", { length: 128 }).notNull(),
    createdAt: timestamp("createdAt", { withTimezone: true })
      .default(sql`CURRENT_TIMESTAMP`)
      .notNull(),
  },
  (table) => [
    primaryKey({
      name: "pk_artwork_likes_artwork_voter",
      columns: [table.artworkId, table.voterId],
    }),
  ],
);

export const imageIngestions = createTable(
  "image_ingestion",
  {
    artworkId: integer("artworkId")
      .primaryKey()
      .references(() => artworks.id, { onDelete: "restrict" }),
    sourceUrl: text("sourceUrl").notNull(),
    sourceSha256: varchar("sourceSha256", { length: 64 }),
    status: imageIngestionStatus("status").default("pending").notNull(),
    attemptCount: integer("attemptCount").default(0).notNull(),
    nextAttemptAt: timestamp("nextAttemptAt", { withTimezone: true }),
    leaseOwner: text("leaseOwner"),
    leaseExpiresAt: timestamp("leaseExpiresAt", { withTimezone: true }),
    lastError: text("lastError"),
    createdAt: timestamp("createdAt", { withTimezone: true })
      .default(sql`CURRENT_TIMESTAMP`)
      .notNull(),
    updatedAt: timestamp("updatedAt", { withTimezone: true })
      .default(sql`CURRENT_TIMESTAMP`)
      .notNull(),
    completedAt: timestamp("completedAt", { withTimezone: true }),
  },
  (table) => [
    index("idx_image_ingestions_claim").on(
      table.status,
      table.nextAttemptAt,
      table.leaseExpiresAt,
    ),
    index("idx_image_ingestions_source_sha256").on(table.sourceSha256),
    check(
      "chk_image_ingestions_attempt_count",
      sql`${table.attemptCount} >= 0`,
    ),
  ],
);

export const imageIngestionAttempts = createTable(
  "image_ingestion_attempt",
  {
    id: serial("id").primaryKey(),
    artworkId: integer("artworkId")
      .notNull()
      .references(() => artworks.id, { onDelete: "restrict" }),
    dryRun: boolean("dryRun").default(false).notNull(),
    outcome: imageIngestionAttemptOutcome("outcome").notNull(),
    sourceSha256: varchar("sourceSha256", { length: 64 }),
    normalizedPixelSha256: varchar("normalizedPixelSha256", { length: 64 }),
    perceptualHash: varchar("perceptualHash", { length: 16 }),
    differenceHash: varchar("differenceHash", { length: 16 }),
    matchedImageAssetId: integer("matchedImageAssetId").references(
      () => imageAssets.id,
      { onDelete: "restrict" },
    ),
    reviewCandidateImageAssetIds: text("reviewCandidateImageAssetIds"),
    reviewCandidateCount: integer("reviewCandidateCount").default(0).notNull(),
    downloadAttemptCount: integer("downloadAttemptCount").default(0).notNull(),
    sourceByteSize: bigint("sourceByteSize", { mode: "number" }),
    fullByteSize: bigint("fullByteSize", { mode: "number" }),
    thumbnailByteSize: bigint("thumbnailByteSize", { mode: "number" }),
    estimatedCostMicroUsd: bigint("estimatedCostMicroUsd", { mode: "number" })
      .default(0)
      .notNull(),
    durationMs: integer("durationMs").notNull(),
    errorStage: varchar("errorStage", { length: 100 }),
    error: text("error"),
    createdAt: timestamp("createdAt", { withTimezone: true })
      .default(sql`CURRENT_TIMESTAMP`)
      .notNull(),
  },
  (table) => [
    index("idx_image_ingestion_attempt_artwork").on(
      table.artworkId,
      table.createdAt,
    ),
    index("idx_image_ingestion_attempt_outcome").on(
      table.outcome,
      table.createdAt,
    ),
    check(
      "chk_image_ingestion_attempt_candidate_count",
      sql`${table.reviewCandidateCount} >= 0`,
    ),
    check(
      "chk_image_ingestion_attempt_download_count",
      sql`${table.downloadAttemptCount} >= 0`,
    ),
    check(
      "chk_image_ingestion_attempt_duration",
      sql`${table.durationMs} >= 0`,
    ),
    check(
      "chk_image_ingestion_attempt_cost",
      sql`${table.estimatedCostMicroUsd} >= 0`,
    ),
  ],
);
