DO $$
BEGIN
    CREATE EXTENSION IF NOT EXISTS pg_trgm;
EXCEPTION
    WHEN insufficient_privilege THEN NULL;
END;
$$;

ALTER TABLE "SourceAsset"
    ADD COLUMN IF NOT EXISTS search_name TEXT,
    ADD COLUMN IF NOT EXISTS size_bytes BIGINT,
    ADD COLUMN IF NOT EXISTS mtime_epoch BIGINT,
    ADD COLUMN IF NOT EXISTS "lastScannedAt" TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS review_status TEXT NOT NULL DEFAULT 'discovered',
    ADD COLUMN IF NOT EXISTS review_payload JSONB;

UPDATE "SourceAsset"
SET search_name = lower(regexp_replace(path, '[^a-zA-Z0-9\s]', ' ', 'g'))
WHERE search_name IS NULL;

CREATE INDEX IF NOT EXISTS "SourceAsset_search_name_idx" ON "SourceAsset"(search_name);
CREATE INDEX IF NOT EXISTS "SourceAsset_search_name_trgm_idx" ON "SourceAsset" USING GIN (search_name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS "SourceAsset_status_updatedAt_idx" ON "SourceAsset"(status, "updatedAt" DESC);

CREATE TABLE IF NOT EXISTS "ImportSession" (
    id TEXT PRIMARY KEY,
    "sourceAssetId" TEXT NOT NULL REFERENCES "SourceAsset"(id) ON DELETE CASCADE,
    "novelId" TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    phase TEXT NOT NULL DEFAULT 'prepare',
    "progressPct" DOUBLE PRECISION NOT NULL DEFAULT 0,
    log TEXT,
    "resultJson" JSONB,
    "createdBy" TEXT,
    "createdAt" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    "updatedAt" TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS "ImportSession_asset_status_idx" ON "ImportSession"("sourceAssetId", status, "updatedAt" DESC);
