CREATE TYPE "public"."image_ingestion_status" AS ENUM('pending', 'processing', 'awaiting_embedding', 'complete', 'retryable_failure', 'terminal_failure');--> statement-breakpoint
CREATE TABLE "met-galaxy_image_ingestion" (
	"artworkId" integer PRIMARY KEY NOT NULL,
	"sourceUrl" text NOT NULL,
	"sourceSha256" varchar(64),
	"status" "image_ingestion_status" DEFAULT 'pending' NOT NULL,
	"attemptCount" integer DEFAULT 0 NOT NULL,
	"nextAttemptAt" timestamp with time zone,
	"leaseOwner" text,
	"leaseExpiresAt" timestamp with time zone,
	"lastError" text,
	"createdAt" timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
	"updatedAt" timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
	"completedAt" timestamp with time zone,
	CONSTRAINT "chk_image_ingestions_attempt_count" CHECK ("met-galaxy_image_ingestion"."attemptCount" >= 0)
);
--> statement-breakpoint
ALTER TABLE "met-galaxy_image_asset" ADD COLUMN "processingAttemptCount" integer DEFAULT 0 NOT NULL;--> statement-breakpoint
ALTER TABLE "met-galaxy_image_asset" ADD COLUMN "processingNextAttemptAt" timestamp with time zone;--> statement-breakpoint
ALTER TABLE "met-galaxy_image_asset" ADD COLUMN "processingLeaseOwner" text;--> statement-breakpoint
ALTER TABLE "met-galaxy_image_asset" ADD COLUMN "processingLeaseExpiresAt" timestamp with time zone;--> statement-breakpoint
ALTER TABLE "met-galaxy_image_ingestion" ADD CONSTRAINT "met-galaxy_image_ingestion_artworkId_met-galaxy_artwork_id_fk" FOREIGN KEY ("artworkId") REFERENCES "public"."met-galaxy_artwork"("id") ON DELETE restrict ON UPDATE no action;--> statement-breakpoint
CREATE INDEX "idx_image_ingestions_claim" ON "met-galaxy_image_ingestion" USING btree ("status","nextAttemptAt","leaseExpiresAt");--> statement-breakpoint
CREATE INDEX "idx_image_ingestions_source_sha256" ON "met-galaxy_image_ingestion" USING btree ("sourceSha256");--> statement-breakpoint
CREATE INDEX "idx_image_assets_processing_queue" ON "met-galaxy_image_asset" USING btree ("processingStatus","processingNextAttemptAt");--> statement-breakpoint
ALTER TABLE "met-galaxy_image_asset" ADD CONSTRAINT "chk_image_assets_processing_attempt_count" CHECK ("met-galaxy_image_asset"."processingAttemptCount" >= 0);