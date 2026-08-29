CREATE TYPE "public"."image_embedding_outbox_status" AS ENUM('pending', 'dispatched');--> statement-breakpoint
CREATE TYPE "public"."image_hash_algorithm" AS ENUM('phash64', 'dhash64');--> statement-breakpoint
CREATE TYPE "public"."image_ingestion_attempt_outcome" AS ENUM('new_asset', 'exact_duplicate', 'already_linked', 'retryable_failure', 'terminal_failure');--> statement-breakpoint
CREATE TABLE "met-galaxy_image_embedding_outbox" (
	"imageAssetId" integer PRIMARY KEY NOT NULL,
	"status" "image_embedding_outbox_status" DEFAULT 'pending' NOT NULL,
	"attemptCount" integer DEFAULT 0 NOT NULL,
	"nextAttemptAt" timestamp with time zone,
	"messageId" text,
	"lastError" text,
	"createdAt" timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
	"dispatchedAt" timestamp with time zone,
	CONSTRAINT "chk_image_embedding_outbox_attempt_count" CHECK ("met-galaxy_image_embedding_outbox"."attemptCount" >= 0)
);
--> statement-breakpoint
CREATE TABLE "met-galaxy_image_ingestion_attempt" (
	"id" serial PRIMARY KEY NOT NULL,
	"artworkId" integer NOT NULL,
	"dryRun" boolean DEFAULT false NOT NULL,
	"outcome" "image_ingestion_attempt_outcome" NOT NULL,
	"sourceSha256" varchar(64),
	"normalizedPixelSha256" varchar(64),
	"perceptualHash" varchar(16),
	"differenceHash" varchar(16),
	"matchedImageAssetId" integer,
	"reviewCandidateImageAssetIds" text,
	"reviewCandidateCount" integer DEFAULT 0 NOT NULL,
	"downloadAttemptCount" integer DEFAULT 0 NOT NULL,
	"sourceByteSize" bigint,
	"fullByteSize" bigint,
	"thumbnailByteSize" bigint,
	"estimatedCostMicroUsd" bigint DEFAULT 0 NOT NULL,
	"durationMs" integer NOT NULL,
	"errorStage" varchar(100),
	"error" text,
	"createdAt" timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT "chk_image_ingestion_attempt_candidate_count" CHECK ("met-galaxy_image_ingestion_attempt"."reviewCandidateCount" >= 0),
	CONSTRAINT "chk_image_ingestion_attempt_download_count" CHECK ("met-galaxy_image_ingestion_attempt"."downloadAttemptCount" >= 0),
	CONSTRAINT "chk_image_ingestion_attempt_duration" CHECK ("met-galaxy_image_ingestion_attempt"."durationMs" >= 0),
	CONSTRAINT "chk_image_ingestion_attempt_cost" CHECK ("met-galaxy_image_ingestion_attempt"."estimatedCostMicroUsd" >= 0)
);
--> statement-breakpoint
CREATE TABLE "met-galaxy_image_perceptual_hash_band" (
	"imageAssetId" integer NOT NULL,
	"algorithm" "image_hash_algorithm" NOT NULL,
	"bandIndex" integer NOT NULL,
	"bandValue" varchar(16) NOT NULL,
	CONSTRAINT "met-galaxy_image_perceptual_hash_band_imageAssetId_algorithm_bandIndex_pk" PRIMARY KEY("imageAssetId","algorithm","bandIndex"),
	CONSTRAINT "chk_image_hash_band_index" CHECK ("met-galaxy_image_perceptual_hash_band"."bandIndex" >= 0)
);
--> statement-breakpoint
ALTER TABLE "met-galaxy_image_asset" ADD COLUMN "thumbnailEncodedSha256" varchar(64);--> statement-breakpoint
ALTER TABLE "met-galaxy_image_asset" ADD COLUMN "differenceHash" text;--> statement-breakpoint
ALTER TABLE "met-galaxy_image_asset" ADD COLUMN "differenceHashAlgorithm" varchar(100);--> statement-breakpoint
ALTER TABLE "met-galaxy_image_asset" ADD COLUMN "thumbnailByteSize" bigint;--> statement-breakpoint
ALTER TABLE "met-galaxy_image_duplicate_candidate" ADD COLUMN "perceptualHashDistance" integer;--> statement-breakpoint
ALTER TABLE "met-galaxy_image_duplicate_candidate" ADD COLUMN "differenceHashDistance" integer;--> statement-breakpoint
ALTER TABLE "met-galaxy_image_embedding_outbox" ADD CONSTRAINT "met-galaxy_image_embedding_outbox_imageAssetId_met-galaxy_image_asset_id_fk" FOREIGN KEY ("imageAssetId") REFERENCES "public"."met-galaxy_image_asset"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "met-galaxy_image_ingestion_attempt" ADD CONSTRAINT "met-galaxy_image_ingestion_attempt_artworkId_met-galaxy_artwork_id_fk" FOREIGN KEY ("artworkId") REFERENCES "public"."met-galaxy_artwork"("id") ON DELETE restrict ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "met-galaxy_image_ingestion_attempt" ADD CONSTRAINT "met-galaxy_image_ingestion_attempt_matchedImageAssetId_met-galaxy_image_asset_id_fk" FOREIGN KEY ("matchedImageAssetId") REFERENCES "public"."met-galaxy_image_asset"("id") ON DELETE restrict ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "met-galaxy_image_perceptual_hash_band" ADD CONSTRAINT "met-galaxy_image_perceptual_hash_band_imageAssetId_met-galaxy_image_asset_id_fk" FOREIGN KEY ("imageAssetId") REFERENCES "public"."met-galaxy_image_asset"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
CREATE INDEX "idx_image_embedding_outbox_dispatch" ON "met-galaxy_image_embedding_outbox" USING btree ("status","nextAttemptAt");--> statement-breakpoint
CREATE INDEX "idx_image_ingestion_attempt_artwork" ON "met-galaxy_image_ingestion_attempt" USING btree ("artworkId","createdAt");--> statement-breakpoint
CREATE INDEX "idx_image_ingestion_attempt_outcome" ON "met-galaxy_image_ingestion_attempt" USING btree ("outcome","createdAt");--> statement-breakpoint
CREATE INDEX "idx_image_hash_bands_lookup" ON "met-galaxy_image_perceptual_hash_band" USING btree ("algorithm","bandIndex","bandValue");