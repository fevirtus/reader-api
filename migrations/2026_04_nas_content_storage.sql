CREATE EXTENSION IF NOT EXISTS unaccent;

CREATE TABLE IF NOT EXISTS "SourceAsset" (
    id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    opf_identifier TEXT,
    title TEXT,
    author TEXT,
    status TEXT NOT NULL DEFAULT 'discovered',
    "createdAt" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    "updatedAt" TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS "SourceAsset_sha256_key" ON "SourceAsset"(sha256);

CREATE TABLE IF NOT EXISTS "ImportJob" (
    id TEXT PRIMARY KEY,
    "sourceAssetId" TEXT NOT NULL REFERENCES "SourceAsset"(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    "createdAt" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    "updatedAt" TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS "ChapterContentRef" (
    "chapterId" TEXT PRIMARY KEY,
    "txtHref" TEXT NOT NULL,
    "rawHtmlHref" TEXT NOT NULL,
    "contentHash" TEXT NOT NULL,
    "createdAt" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    "updatedAt" TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS "AssetNovelMapping" (
    id TEXT PRIMARY KEY,
    "sourceAssetId" TEXT NOT NULL REFERENCES "SourceAsset"(id) ON DELETE CASCADE,
    "novelId" TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    note TEXT,
    "createdAt" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    "updatedAt" TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
