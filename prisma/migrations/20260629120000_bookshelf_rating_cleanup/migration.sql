-- Add markedAsRead to Bookmark
ALTER TABLE "Bookmark" ADD COLUMN IF NOT EXISTS "markedAsRead" BOOLEAN NOT NULL DEFAULT false;

-- Migrate rating scale from 1-5 to 1-10
UPDATE "Novel" SET rating = rating * 2 WHERE "ratingCount" > 0 AND rating > 0 AND rating <= 5;

-- Drop removed features
DROP TABLE IF EXISTS "Comment" CASCADE;
DROP TABLE IF EXISTS "UserRecommendationDoc" CASCADE;
DROP TABLE IF EXISTS "EditorRecommendationDoc" CASCADE;
