CREATE TABLE IF NOT EXISTS "ChapterMeta" (
    id TEXT PRIMARY KEY,
    "novelId" TEXT NOT NULL,
    number INT NOT NULL,
    title TEXT,
    views INT NOT NULL DEFAULT 0,
    "createdAt" TIMESTAMPTZ,
    UNIQUE("novelId", number)
);

CREATE INDEX IF NOT EXISTS "ChapterMeta_novel_number_idx" ON "ChapterMeta"("novelId", number);
