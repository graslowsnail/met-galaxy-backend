ALTER TABLE "met-galaxy_artwork" ADD COLUMN "txtVecAttemptCount" integer DEFAULT 0 NOT NULL;--> statement-breakpoint
ALTER TABLE "met-galaxy_artwork" ADD COLUMN "txtVecNextAttemptAt" timestamp with time zone;--> statement-breakpoint
ALTER TABLE "met-galaxy_artwork" ADD COLUMN "txtVecLeaseOwner" text;--> statement-breakpoint
ALTER TABLE "met-galaxy_artwork" ADD COLUMN "txtVecLeaseExpiresAt" timestamp with time zone;--> statement-breakpoint
ALTER TABLE "met-galaxy_artwork" ADD COLUMN "txtVecLastError" text;--> statement-breakpoint
CREATE INDEX "idx_artworks_txt_vec_queue" ON "met-galaxy_artwork" USING btree ("txtVecAttemptCount","txtVecNextAttemptAt","txtVecLeaseExpiresAt");--> statement-breakpoint
ALTER TABLE "met-galaxy_artwork" ADD CONSTRAINT "chk_artworks_txt_vec_attempt_count" CHECK ("met-galaxy_artwork"."txtVecAttemptCount" >= 0);