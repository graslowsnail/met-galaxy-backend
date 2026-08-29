CREATE TYPE "public"."image_association_duplicate_state" AS ENUM('unique', 'exact_duplicate', 'verified_duplicate');--> statement-breakpoint
CREATE TYPE "public"."image_duplicate_candidate_status" AS ENUM('review_candidate', 'verified_duplicate', 'rejected');--> statement-breakpoint
CREATE TABLE "met-galaxy_image_duplicate_candidate" (
	"id" serial PRIMARY KEY NOT NULL,
	"imageAssetAId" integer NOT NULL,
	"imageAssetBId" integer NOT NULL,
	"status" "image_duplicate_candidate_status" DEFAULT 'review_candidate' NOT NULL,
	"createdAt" timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
	"reviewedAt" timestamp with time zone,
	CONSTRAINT "chk_image_duplicate_candidate_order" CHECK ("met-galaxy_image_duplicate_candidate"."imageAssetAId" < "met-galaxy_image_duplicate_candidate"."imageAssetBId")
);
--> statement-breakpoint
ALTER TABLE "met-galaxy_artwork" ADD COLUMN "imageDuplicateState" "image_association_duplicate_state";--> statement-breakpoint
ALTER TABLE "met-galaxy_image_duplicate_candidate" ADD CONSTRAINT "met-galaxy_image_duplicate_candidate_imageAssetAId_met-galaxy_image_asset_id_fk" FOREIGN KEY ("imageAssetAId") REFERENCES "public"."met-galaxy_image_asset"("id") ON DELETE restrict ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "met-galaxy_image_duplicate_candidate" ADD CONSTRAINT "met-galaxy_image_duplicate_candidate_imageAssetBId_met-galaxy_image_asset_id_fk" FOREIGN KEY ("imageAssetBId") REFERENCES "public"."met-galaxy_image_asset"("id") ON DELETE restrict ON UPDATE no action;--> statement-breakpoint
CREATE UNIQUE INDEX "uq_image_duplicate_candidate_pair" ON "met-galaxy_image_duplicate_candidate" USING btree ("imageAssetAId","imageAssetBId");--> statement-breakpoint
CREATE INDEX "idx_image_duplicate_candidate_status" ON "met-galaxy_image_duplicate_candidate" USING btree ("status");