ALTER TABLE "met-galaxy_artwork" ADD COLUMN "searchDocument" "tsvector" GENERATED ALWAYS AS (
      setweight(to_tsvector('simple', coalesce("title", '')), 'A')
      || setweight(to_tsvector('simple', coalesce("artist", '')), 'A')
      || setweight(to_tsvector('simple', coalesce("classification", '')), 'B')
      || setweight(to_tsvector('simple', coalesce("culture", '')), 'B')
      || setweight(to_tsvector('simple', coalesce("department", '')), 'C')
      || setweight(to_tsvector('simple', coalesce("medium", '')), 'C')
      || setweight(to_tsvector('simple', "id"::text), 'A')
      || setweight(to_tsvector('simple', "objectId"::text), 'A')
    ) STORED;--> statement-breakpoint
CREATE INDEX "idx_artworks_search_document_gin" ON "met-galaxy_artwork" USING gin ("searchDocument");--> statement-breakpoint
CREATE INDEX "idx_artworks_object_id" ON "met-galaxy_artwork" USING btree ("objectId");