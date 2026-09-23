"""Audio book V2. Additive API; independent from device TTS and legacy progress."""

from __future__ import annotations

import datetime as dt
import hashlib
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_current_user
from app.database import engine, get_db_session
from app.storage import storage

router = APIRouter(prefix="/api/audiobooks", tags=["Audio book"])
MODEL_VERSION = "vieneu-3.7.1-61b85e3-normalizer1-aac64"
VOICES = [
    {"id": "anh-khoi", "name": "Anh Khôi", "modelVoice": "Anh Khôi", "default": True},
    {"id": "ngoc-linh", "name": "Ngọc Linh", "modelVoice": "Ngọc Linh", "default": False},
]
DDL = [
    """CREATE TABLE IF NOT EXISTS "AudioBookEdition" (
        id TEXT PRIMARY KEY, "novelId" TEXT NOT NULL REFERENCES "Novel"(id) ON DELETE CASCADE,
        "voiceId" TEXT NOT NULL, "createdAt" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE("novelId", "voiceId"))""",
    """CREATE TABLE IF NOT EXISTS "AudioBookAsset" (
        id TEXT PRIMARY KEY, "editionId" TEXT NOT NULL
        REFERENCES "AudioBookEdition"(id) ON DELETE CASCADE,
        "chapterId" TEXT NOT NULL REFERENCES "ChapterMeta"(id) ON DELETE CASCADE,
        "sourceHash" TEXT NOT NULL, "modelVersion" TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued', attempts INT NOT NULL DEFAULT 0,
        "retryAt" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        "updatedAt" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        href TEXT, sha256 TEXT, bytes BIGINT, duration DOUBLE PRECISION, error TEXT,
        UNIQUE("editionId", "chapterId", "sourceHash", "modelVersion"))""",
    """CREATE INDEX IF NOT EXISTS "AudioBookAsset_queue_idx"
       ON "AudioBookAsset"(status, "retryAt")""",
    """CREATE TABLE IF NOT EXISTS "AudioBookRequest" (
        "userId" TEXT NOT NULL REFERENCES "User"(id) ON DELETE CASCADE,
        "editionId" TEXT NOT NULL REFERENCES "AudioBookEdition"(id) ON DELETE CASCADE,
        "updatedAt" TIMESTAMPTZ NOT NULL DEFAULT NOW(), PRIMARY KEY("userId", "editionId"))""",
    """CREATE TABLE IF NOT EXISTS "AudioBookExport" (
        id TEXT PRIMARY KEY, "editionId" TEXT NOT NULL
        REFERENCES "AudioBookEdition"(id) ON DELETE CASCADE,
        revision TEXT NOT NULL, href TEXT NOT NULL, bytes BIGINT NOT NULL,
        "chapterCount" INT NOT NULL, "createdAt" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE("editionId", revision))""",
    """CREATE TABLE IF NOT EXISTS "AudioBookProgress" (
        "userId" TEXT NOT NULL REFERENCES "User"(id) ON DELETE CASCADE,
        "novelId" TEXT NOT NULL REFERENCES "Novel"(id) ON DELETE CASCADE,
        "editionId" TEXT NOT NULL, "assetId" TEXT NOT NULL,
        position DOUBLE PRECISION NOT NULL, "eventId" TEXT NOT NULL,
        "occurredAt" TIMESTAMPTZ NOT NULL, PRIMARY KEY("userId", "novelId"))""",
]


async def ensure_audio_schema():
    async with engine.begin() as conn:
        for sql in DDL:
            await conn.execute(text(sql))


def digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


async def queue_edition(db, edition):
    """Idempotent reconciliation; metadata hashes only, never reads chapter files in API."""
    await db.execute(
        text("""INSERT INTO "AudioBookAsset"
        (id,"editionId","chapterId","sourceHash","modelVersion")
        SELECT md5(:eid || ':' || c.id || ':' || r."contentHash" || ':' || :model),
            :eid,c.id,r."contentHash",:model
        FROM "ChapterMeta" c JOIN "ChapterContentRef" r ON r."chapterId"=c.id
        WHERE c."novelId"=:nid AND r."contentHash" IS NOT NULL
        ON CONFLICT DO NOTHING"""),
        {"eid": edition["id"], "nid": edition["novelId"], "model": MODEL_VERSION},
    )


async def edition_manifest(db, eid):
    edition = (
        (
            await db.execute(
                text("""SELECT e.*, n.title FROM "AudioBookEdition" e
        JOIN "Novel" n ON n.id=e."novelId" WHERE e.id=:id"""),
                {"id": eid},
            )
        )
        .mappings()
        .first()
    )
    if not edition:
        raise HTTPException(404, "Không tìm thấy bản Audio book")
    rows = (
        (
            await db.execute(
                text("""SELECT c.id, c.number, c.title, r."contentHash",
        latest.id AS "requestedAssetId", latest.status,
        ready.id AS "assetId", ready."sourceHash", ready.sha256, ready.bytes, ready.duration
        FROM "ChapterMeta" c LEFT JOIN "ChapterContentRef" r ON r."chapterId"=c.id
        LEFT JOIN "AudioBookAsset" latest ON latest."chapterId"=c.id
            AND latest."editionId"=:eid AND latest."sourceHash"=r."contentHash"
            AND latest."modelVersion"=:model
        LEFT JOIN LATERAL (SELECT a.* FROM "AudioBookAsset" a
            WHERE a."chapterId"=c.id AND a."editionId"=:eid AND a.status='ready'
            ORDER BY (a."sourceHash"=r."contentHash" AND a."modelVersion"=:model) DESC,
                a."updatedAt" DESC LIMIT 1) ready ON true
        WHERE c."novelId"=:nid ORDER BY c.number, c.id"""),
                {"eid": eid, "nid": edition["novelId"], "model": MODEL_VERSION},
            )
        )
        .mappings()
        .all()
    )
    chapters = []
    for row in rows:
        c = dict(row)
        c["status"] = c["status"] or "queued"
        c["hasUpdate"] = bool(c["assetId"] and c["assetId"] != c["requestedAssetId"])
        c["url"] = f"/api/audiobooks/assets/{c['assetId']}" if c["assetId"] else None
        chapters.append(c)
    revision = digest(
        [{k: c[k] for k in ("id", "number", "title", "assetId", "sha256")} for c in chapters]
    )
    export = (
        (
            await db.execute(
                text("""SELECT id,revision,"chapterCount",bytes FROM "AudioBookExport"
        WHERE "editionId"=:id ORDER BY "createdAt" DESC LIMIT 1"""),
                {"id": eid},
            )
        )
        .mappings()
        .first()
    )
    return {
        "id": eid,
        "novelId": edition["novelId"],
        "title": edition["title"],
        "voiceId": edition["voiceId"],
        "revision": revision,
        "chapters": chapters,
        "readyCount": sum(bool(c["assetId"]) for c in chapters),
        "export": (
            {
                **dict(export),
                "url": f"/api/audiobooks/exports/{export['id']}",
                "hasUpdate": export["revision"] != revision,
            }
            if export
            else None
        ),
    }


@router.get("/voices")
async def voices():
    return {"voices": [{k: v for k, v in voice.items() if k != "modelVoice"} for voice in VOICES]}


@router.get("/novels/{novel_id}")
async def editions(novel_id: str, db: AsyncSession = Depends(get_db_session)):
    novel = (
        (await db.execute(text('SELECT title FROM "Novel" WHERE id=:id'), {"id": novel_id}))
        .mappings()
        .first()
    )
    if not novel:
        raise HTTPException(404, "Không tìm thấy truyện")
    rows = (
        (
            await db.execute(
                text('SELECT id FROM "AudioBookEdition" WHERE "novelId"=:nid ORDER BY "createdAt"'),
                {"nid": novel_id},
            )
        )
        .scalars()
        .all()
    )
    return {
        "novelId": novel_id,
        "title": novel["title"],
        "editions": [await edition_manifest(db, eid) for eid in rows],
    }


@router.get("/editions/{edition_id}")
async def manifest(edition_id: str, db: AsyncSession = Depends(get_db_session)):
    return await edition_manifest(db, edition_id)


class AudioRequest(BaseModel):
    voiceId: str = "anh-khoi"


@router.post("/novels/{novel_id}/requests", status_code=202)
async def request_audio(
    novel_id: str,
    payload: AudioRequest,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
):
    user = await require_current_user(request, db)
    if payload.voiceId not in {v["id"] for v in VOICES}:
        raise HTTPException(400, "Giọng không được hỗ trợ")
    if not (
        await db.execute(text('SELECT id FROM "Novel" WHERE id=:id'), {"id": novel_id})
    ).first():
        raise HTTPException(404, "Không tìm thấy truyện")
    # Serialize quota and dedup checks even for simultaneous requests from different devices.
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:uid, 0))"),
        {"uid": "audio:" + user["id"]},
    )
    eid = digest([novel_id, payload.voiceId])
    existing = (
        await db.execute(
            text('SELECT 1 FROM "AudioBookRequest" WHERE "userId"=:uid AND "editionId"=:eid'),
            {"uid": user["id"], "eid": eid},
        )
    ).first()
    count = (
        await db.execute(
            text("""SELECT COUNT(*) FROM "AudioBookRequest"
        WHERE "userId"=:uid AND "updatedAt">NOW()-INTERVAL '1 day' """),
            {"uid": user["id"]},
        )
    ).scalar_one()
    if not existing and count >= 5:
        raise HTTPException(429, "Bạn đã yêu cầu 5 bản giọng hôm nay. Vui lòng thử lại ngày mai.")
    await db.execute(
        text("""INSERT INTO "AudioBookEdition" (id,"novelId","voiceId")
        VALUES (:id,:nid,:voice) ON CONFLICT DO NOTHING"""),
        {"id": eid, "nid": novel_id, "voice": payload.voiceId},
    )
    await db.execute(
        text("""INSERT INTO "AudioBookRequest" ("userId","editionId") VALUES (:uid,:eid)
        ON CONFLICT DO NOTHING"""),
        {"uid": user["id"], "eid": eid},
    )
    await queue_edition(db, {"id": eid, "novelId": novel_id})
    await db.commit()
    return await edition_manifest(db, eid)


@router.api_route("/assets/{asset_id}", methods=["GET", "HEAD"])
async def audio_file(asset_id: str, db: AsyncSession = Depends(get_db_session)):
    row = (
        (
            await db.execute(
                text("SELECT href,sha256 FROM \"AudioBookAsset\" WHERE id=:id AND status='ready'"),
                {"id": asset_id},
            )
        )
        .mappings()
        .first()
    )
    if not row or not storage._resolve(row["href"]).is_file():
        raise HTTPException(404, "Audio chưa sẵn sàng")
    await db.close()  # Streaming must not hold a PostgreSQL connection.
    return FileResponse(
        storage._resolve(row["href"]),
        media_type="audio/mp4",
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "ETag": f'"{row["sha256"]}"',
        },
    )


@router.api_route("/exports/{export_id}", methods=["GET", "HEAD"])
async def export_file(export_id: str, db: AsyncSession = Depends(get_db_session)):
    row = (
        (
            await db.execute(
                text('SELECT href FROM "AudioBookExport" WHERE id=:id'), {"id": export_id}
            )
        )
        .mappings()
        .first()
    )
    if not row or not storage._resolve(row["href"]).is_file():
        raise HTTPException(404, "Bản tổng hợp chưa sẵn sàng")
    await db.close()  # Streaming must not hold a PostgreSQL connection.
    return FileResponse(
        storage._resolve(row["href"]), media_type="audio/mp4", filename="audio-book.m4b"
    )


class Progress(BaseModel):
    userId: str | None = None
    editionId: str
    assetId: str
    position: float = Field(ge=0, le=86400, allow_inf_nan=False)
    eventId: str = Field(min_length=1, max_length=100)
    occurredAt: dt.datetime


@router.get("/progress/{novel_id}")
async def get_progress(novel_id: str, request: Request, db: AsyncSession = Depends(get_db_session)):
    user = await require_current_user(request, db)
    row = (
        (
            await db.execute(
                text('SELECT * FROM "AudioBookProgress" WHERE "userId"=:uid AND "novelId"=:nid'),
                {"uid": user["id"], "nid": novel_id},
            )
        )
        .mappings()
        .first()
    )
    return {"progress": dict(row) if row else None}


@router.post("/progress/{novel_id}")
async def save_progress(
    novel_id: str, payload: Progress, request: Request, db: AsyncSession = Depends(get_db_session)
):
    user = await require_current_user(request, db)
    if payload.userId is not None and payload.userId != user["id"]:
        raise HTTPException(409, "Tài khoản đã thay đổi; giữ tiến độ cho tài khoản ban đầu")
    asset = (
        await db.execute(
            text("""SELECT a.duration FROM "AudioBookAsset" a
        JOIN "AudioBookEdition" e ON e.id=a."editionId"
        WHERE a.id=:aid AND e.id=:eid AND e."novelId"=:nid AND a.status='ready' """),
            {"aid": payload.assetId, "eid": payload.editionId, "nid": novel_id},
        )
    ).first()
    if not asset:
        raise HTTPException(404, "Bản audio không còn tồn tại")
    now = dt.datetime.now(dt.timezone.utc)
    at = min(
        payload.occurredAt.replace(tzinfo=dt.timezone.utc)
        if payload.occurredAt.tzinfo is None
        else payload.occurredAt,
        now,
    )
    await db.execute(
        text("""INSERT INTO "AudioBookProgress"
        ("userId","novelId","editionId","assetId",position,"eventId","occurredAt")
        VALUES (:uid,:nid,:eid,:aid,:pos,:event,:at)
        ON CONFLICT ("userId","novelId") DO UPDATE SET "editionId"=EXCLUDED."editionId",
        "assetId"=EXCLUDED."assetId",position=EXCLUDED.position,"eventId"=EXCLUDED."eventId" 
        ,"occurredAt"=EXCLUDED."occurredAt"
        WHERE (EXCLUDED."occurredAt",EXCLUDED."eventId") >
        ("AudioBookProgress"."occurredAt","AudioBookProgress"."eventId")"""),
        {
            "uid": user["id"],
            "nid": novel_id,
            "eid": payload.editionId,
            "aid": payload.assetId,
            "pos": min(payload.position, asset[0]),
            "event": payload.eventId,
            "at": at,
        },
    )
    await db.commit()
    return {"acknowledgedEventId": payload.eventId}
