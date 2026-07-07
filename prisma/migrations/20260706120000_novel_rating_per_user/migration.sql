-- One rating per user per novel; novel.rating is the average across users.
CREATE TABLE IF NOT EXISTS "NovelRating" (
    "id" TEXT NOT NULL,
    "userId" TEXT NOT NULL,
    "novelId" TEXT NOT NULL,
    "score" DOUBLE PRECISION NOT NULL,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" TIMESTAMP(3) NOT NULL,

    CONSTRAINT "NovelRating_pkey" PRIMARY KEY ("id")
);

CREATE UNIQUE INDEX IF NOT EXISTS "NovelRating_userId_novelId_key" ON "NovelRating"("userId", "novelId");
CREATE INDEX IF NOT EXISTS "NovelRating_novelId_idx" ON "NovelRating"("novelId");

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'NovelRating_userId_fkey'
    ) THEN
        ALTER TABLE "NovelRating"
            ADD CONSTRAINT "NovelRating_userId_fkey"
            FOREIGN KEY ("userId") REFERENCES "User"("id") ON DELETE CASCADE ON UPDATE CASCADE;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'NovelRating_novelId_fkey'
    ) THEN
        ALTER TABLE "NovelRating"
            ADD CONSTRAINT "NovelRating_novelId_fkey"
            FOREIGN KEY ("novelId") REFERENCES "Novel"("id") ON DELETE CASCADE ON UPDATE CASCADE;
    END IF;
END $$;

-- Recompute cached aggregates from per-user ratings (legacy inflated counts are dropped).
UPDATE "Novel" n
SET
    rating = COALESCE(stats.avg_score, 0),
    "ratingCount" = COALESCE(stats.cnt, 0)
FROM (
    SELECT
        "novelId",
        AVG(score) AS avg_score,
        COUNT(*)::int AS cnt
    FROM "NovelRating"
    GROUP BY "novelId"
) stats
WHERE n.id = stats."novelId";

UPDATE "Novel"
SET rating = 0, "ratingCount" = 0
WHERE id NOT IN (SELECT DISTINCT "novelId" FROM "NovelRating");
