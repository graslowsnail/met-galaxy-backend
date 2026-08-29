CREATE TYPE "public"."image_asset_processing_status" AS ENUM('pending_upload', 'pending_embedding', 'ready');--> statement-breakpoint
CREATE TABLE "met-galaxy_image_asset" (
	"id" serial PRIMARY KEY NOT NULL,
	"fullS3Key" text,
	"thumbnailS3Key" text,
	"mimeType" varchar(100),
	"width" integer,
	"height" integer,
	"byteSize" bigint,
	"encodedSha256" varchar(64),
	"normalizedPixelSha256" varchar(64),
	"perceptualHash" text,
	"perceptualHashAlgorithm" varchar(100),
	"processingStatus" "image_asset_processing_status" DEFAULT 'pending_upload' NOT NULL,
	"imageEmbedding" vector(768),
	"lastError" text,
	"createdAt" timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
	"updatedAt" timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT "met-galaxy_image_asset_fullS3Key_unique" UNIQUE("fullS3Key"),
	CONSTRAINT "met-galaxy_image_asset_thumbnailS3Key_unique" UNIQUE("thumbnailS3Key"),
	CONSTRAINT "met-galaxy_image_asset_encodedSha256_unique" UNIQUE("encodedSha256"),
	CONSTRAINT "met-galaxy_image_asset_normalizedPixelSha256_unique" UNIQUE("normalizedPixelSha256")
);
