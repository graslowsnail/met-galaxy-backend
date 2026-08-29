CREATE TABLE "met-galaxy_image_asset_canonical" (
	"assetId" integer PRIMARY KEY NOT NULL,
	"canonicalAssetId" integer NOT NULL,
	"createdAt" timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
	"updatedAt" timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT "chk_image_asset_canonical_not_self" CHECK ("met-galaxy_image_asset_canonical"."assetId" <> "met-galaxy_image_asset_canonical"."canonicalAssetId")
);
--> statement-breakpoint
ALTER TABLE "met-galaxy_image_asset_canonical" ADD CONSTRAINT "met-galaxy_image_asset_canonical_assetId_met-galaxy_image_asset_id_fk" FOREIGN KEY ("assetId") REFERENCES "public"."met-galaxy_image_asset"("id") ON DELETE restrict ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "met-galaxy_image_asset_canonical" ADD CONSTRAINT "met-galaxy_image_asset_canonical_canonicalAssetId_met-galaxy_image_asset_id_fk" FOREIGN KEY ("canonicalAssetId") REFERENCES "public"."met-galaxy_image_asset"("id") ON DELETE restrict ON UPDATE no action;--> statement-breakpoint
CREATE INDEX "idx_image_asset_canonical_root" ON "met-galaxy_image_asset_canonical" USING btree ("canonicalAssetId");