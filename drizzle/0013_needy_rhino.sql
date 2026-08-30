CREATE TABLE "met-galaxy_artwork_like" (
	"artworkId" integer NOT NULL,
	"voterId" varchar(128) NOT NULL,
	"createdAt" timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT "pk_artwork_likes_artwork_voter" PRIMARY KEY("artworkId","voterId")
);
--> statement-breakpoint
ALTER TABLE "met-galaxy_artwork_like" ADD CONSTRAINT "met-galaxy_artwork_like_artworkId_met-galaxy_artwork_id_fk" FOREIGN KEY ("artworkId") REFERENCES "public"."met-galaxy_artwork"("id") ON DELETE cascade ON UPDATE no action;