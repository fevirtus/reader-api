from __future__ import annotations

import asyncio
import base64
import datetime as dt
import hashlib
import json
import os
import random
import re
import secrets
import tempfile
import time
import uuid
import zipfile
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import boto3
from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
import httpx
from fastapi.responses import Response
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import ACCESS_TOKEN_TTL_SECONDS, create_access_token, require_current_user
from app.config import settings
from app.database import get_db_session
from app.storage import storage


@asynccontextmanager
async def lifespan(_: FastAPI):
    if str(settings.auto_schema_bootstrap).lower() in {"1", "true", "yes", "on"}:
        await _ensure_migration_tables()
    yield


async def _ensure_migration_tables() -> None:
    from app.database import engine

    ddl_statements = [
        'CREATE EXTENSION IF NOT EXISTS unaccent',
        '''
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
        )
        ''',
        '''
        CREATE UNIQUE INDEX IF NOT EXISTS "SourceAsset_sha256_key" ON "SourceAsset"(sha256)
        ''',
        '''
        ALTER TABLE "SourceAsset"
            ADD COLUMN IF NOT EXISTS search_name TEXT,
            ADD COLUMN IF NOT EXISTS size_bytes BIGINT,
            ADD COLUMN IF NOT EXISTS mtime_epoch BIGINT,
            ADD COLUMN IF NOT EXISTS "lastScannedAt" TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS review_status TEXT NOT NULL DEFAULT 'discovered',
            ADD COLUMN IF NOT EXISTS review_payload JSONB
        ''',
        '''
        CREATE INDEX IF NOT EXISTS "SourceAsset_search_name_idx" ON "SourceAsset"(search_name)
        ''',
        '''
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
        )
        ''',
        '''
        CREATE TABLE IF NOT EXISTS "ImportJob" (
            id TEXT PRIMARY KEY,
            "sourceAssetId" TEXT NOT NULL REFERENCES "SourceAsset"(id) ON DELETE CASCADE,
            status TEXT NOT NULL DEFAULT 'pending',
            error TEXT,
            "createdAt" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            "updatedAt" TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        ''',
        '''
        CREATE TABLE IF NOT EXISTS "ChapterContentRef" (
            "chapterId" TEXT PRIMARY KEY,
            "txtHref" TEXT NOT NULL,
            "rawHtmlHref" TEXT NOT NULL,
            "contentHash" TEXT NOT NULL,
            "createdAt" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            "updatedAt" TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        ''',
        '''
        CREATE TABLE IF NOT EXISTS "ChapterMeta" (
            id TEXT PRIMARY KEY,
            "novelId" TEXT NOT NULL,
            number INT NOT NULL,
            title TEXT,
            views INT NOT NULL DEFAULT 0,
            "createdAt" TIMESTAMPTZ,
            UNIQUE("novelId", number)
        )
        ''',
        '''
        CREATE TABLE IF NOT EXISTS "ImportCandidateChapter" (
            id TEXT PRIMARY KEY,
            "jobId" TEXT NOT NULL,
            "candidateNumber" INT NOT NULL,
            "candidateTitle" TEXT,
            "candidateHash" TEXT,
            "matchedChapterId" TEXT,
            action TEXT NOT NULL,
            reason TEXT,
            "createdAt" TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        ''',
        '''
        CREATE TABLE IF NOT EXISTS "AssetNovelMapping" (
            id TEXT PRIMARY KEY,
            "sourceAssetId" TEXT NOT NULL REFERENCES "SourceAsset"(id) ON DELETE CASCADE,
            "novelId" TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            note TEXT,
            "createdAt" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            "updatedAt" TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        ''',
        '''
        CREATE TABLE IF NOT EXISTS "UserRecommendationDoc" (
            id TEXT PRIMARY KEY,
            "userId" TEXT NOT NULL,
            "novelId" TEXT NOT NULL,
            content TEXT,
            "createdAt" TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        ''',
        '''
        CREATE UNIQUE INDEX IF NOT EXISTS "UserRecommendationDoc_user_novel_key"
        ON "UserRecommendationDoc"("userId", "novelId")
        ''',
        '''
        CREATE TABLE IF NOT EXISTS "EditorRecommendationDoc" (
            id TEXT PRIMARY KEY,
            "editorId" TEXT NOT NULL,
            "novelId" TEXT NOT NULL,
            content TEXT,
            "createdAt" TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        ''',
        '''
        CREATE INDEX IF NOT EXISTS "EditorRecommendationDoc_novel_idx"
        ON "EditorRecommendationDoc"("novelId")
        ''',
    ]

    async with engine.begin() as conn:
        for ddl in ddl_statements:
            try:
                await conn.execute(text(ddl))
            except Exception:
                if ddl.strip().lower().startswith("create extension"):
                    continue
                raise


app = FastAPI(title=settings.app_name, lifespan=lifespan)


@app.middleware("http")
async def disable_legacy_import_routes(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/import") and path != "/api/import/uploads/preview":
        return Response(
            content=json.dumps({"detail": "Legacy import endpoints are removed"}),
            status_code=410,
            media_type="application/json",
        )
    return await call_next(request)

_IMPORT_TASKS: set[asyncio.Task[Any]] = set()


def _normalized_search_name(value: str) -> str:
    raw = (value or "").replace("\\", "/")
    base = raw.split("/")[-1]
    stem = base.rsplit(".", 1)[0]
    return _norm_title(stem)


app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _new_id(prefix: str = "") -> str:
    token = uuid.uuid4().hex
    return f"{prefix}{token}" if prefix else token


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone.utc)
        return value.isoformat()
    if isinstance(value, dt.date):
        return dt.datetime.combine(value, dt.time(0, 0, tzinfo=dt.timezone.utc)).isoformat()
    return str(value)


def _shuffle_rows[T](rows: list[T]) -> list[T]:
    copied = list(rows)
    random.shuffle(copied)
    return copied


def _collapse_series_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    picked_series: set[str] = set()
    output: list[dict[str, Any]] = []

    for row in rows:
        series_id = row.get("seriesId")
        if not series_id:
            output.append(row)
            continue

        if series_id in picked_series:
            continue

        picked_series.add(series_id)
        output.append(row)

    return output


def _fill_unique_rows(rows: list[dict[str, Any]], fallback: list[dict[str, Any]], target: int) -> list[dict[str, Any]]:
    picked: set[str] = set()
    output: list[dict[str, Any]] = []

    for row in [*rows, *fallback]:
        row_id = str(row.get("id") or "")
        if not row_id or row_id in picked:
            continue
        picked.add(row_id)
        output.append(row)
        if len(output) >= target:
            return output

    return output


def _home_novel_from_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "slug": row["slug"],
        "title": row["title"],
        "authorName": row["authorName"],
        "coverColor": row.get("coverColor"),
        "coverUrl": row.get("coverUrl"),
        "rating": float(row.get("rating") or 0),
        "views": int(row.get("views") or 0),
        "totalChapters": int(row.get("totalChapters") or 0),
        "status": row.get("status") or "Đang ra",
        "description": row.get("description") or "",
        "bookmarkCount": int(row.get("bookmarkCount") or 0),
        "seriesId": row.get("seriesId"),
        "updatedAt": _iso(row.get("updatedAt")),
    }


async def _fetch_home_ranking_rows(
    db: AsyncSession,
    *,
    since: dt.date | None = None,
    take: int = 300,
) -> list[dict[str, Any]]:
    where_sql = 'WHERE v.day >= :since' if since else ''
    params: dict[str, Any] = {"take": take}
    if since:
        params["since"] = since

    rows = (
        await db.execute(
            text(
                'SELECT n.id, n.slug, n.title, n."authorName", n."coverColor", n."coverUrl", '
                'n.rating, n.views, n."totalChapters", n.status, n.description, n."bookmarkCount", '
                'n."seriesId", n."updatedAt", COALESCE(SUM(v.views), 0)::int AS aggregated_views '
                'FROM "NovelViewDaily" v '
                'JOIN "Novel" n ON n.id = v."novelId" '
                f'{where_sql} '
                'GROUP BY n.id '
                'ORDER BY aggregated_views DESC, n."updatedAt" DESC '
                'LIMIT :take'
            ),
            params,
        )
    ).mappings().all()

    return [
        {
            "id": row["id"],
            "seriesId": row["seriesId"],
            "aggregatedViews": int(row.get("aggregated_views") or 0),
            "novel": _home_novel_from_row(dict(row)),
        }
        for row in rows
    ]


async def _fetch_home_popular_fallback(db: AsyncSession, *, take: int = 400) -> list[dict[str, Any]]:
    rows = (
        await db.execute(
            text(
                'SELECT id, slug, title, "authorName", "coverColor", "coverUrl", rating, views, '
                '"totalChapters", status, description, "bookmarkCount", "seriesId", "updatedAt" '
                'FROM "Novel" '
                'ORDER BY views DESC, "updatedAt" DESC '
                'LIMIT :take'
            ),
            {"take": take},
        )
    ).mappings().all()

    return [
        {
            "id": row["id"],
            "seriesId": row["seriesId"],
            "aggregatedViews": int(row.get("views") or 0),
            "novel": _home_novel_from_row(dict(row)),
        }
        for row in rows
    ]


async def _fetch_home_random_pool(db: AsyncSession, *, take: int = 420) -> list[dict[str, Any]]:
    rows = (
        await db.execute(
            text(
                'SELECT id, slug, title, "authorName", "coverColor", "coverUrl", rating, views, '
                '"totalChapters", status, description, "bookmarkCount", "seriesId", "updatedAt" '
                'FROM "Novel" '
                'ORDER BY "updatedAt" DESC '
                'LIMIT :take'
            ),
            {"take": take},
        )
    ).mappings().all()

    return [_home_novel_from_row(dict(row)) for row in rows]


async def _fetch_home_manual_recommendations(db: AsyncSession) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    try:
        editor_rows = (
            await db.execute(
                text('SELECT id, "editorId", "novelId", content, "createdAt" FROM "EditorRecommendationDoc" ORDER BY "createdAt" DESC LIMIT 2000')
            )
        ).mappings().all()
        user_rows = (
            await db.execute(
                text('SELECT id, "userId", "novelId", content, "createdAt" FROM "UserRecommendationDoc" ORDER BY "createdAt" DESC LIMIT 5000')
            )
        ).mappings().all()
    except Exception:
        return [], []
    editor_docs = [dict(r) for r in editor_rows]
    user_docs = [dict(r) for r in user_rows]

    novel_ids = list(
        {
            str(item.get("novelId"))
            for item in [*editor_docs, *user_docs]
            if item.get("novelId")
        }
    )
    editor_ids = list({str(item.get("editorId")) for item in editor_docs if item.get("editorId")})

    if not novel_ids:
        return [], []

    novel_rows = (
        await db.execute(
            text(
                'SELECT id, slug, title, "authorName", "coverUrl", rating '
                'FROM "Novel" '
                'WHERE id = ANY(:novel_ids)'
            ),
            {"novel_ids": novel_ids},
        )
    ).mappings().all()
    editor_rows = []
    if editor_ids:
        editor_rows = (
            await db.execute(
                text('SELECT id, name FROM "User" WHERE id = ANY(:editor_ids)'),
                {"editor_ids": editor_ids},
            )
        ).mappings().all()

    novel_map = {
        row["id"]: {
            "id": row["id"],
            "slug": row["slug"],
            "title": row["title"],
            "authorName": row["authorName"],
            "coverUrl": row.get("coverUrl"),
            "rating": float(row.get("rating") or 0),
        }
        for row in novel_rows
    }
    editor_map = {row["id"]: row.get("name") or "Biên tập viên" for row in editor_rows}

    recommend_count_map: dict[str, int] = {}
    for doc in [*editor_docs, *user_docs]:
        novel_id = str(doc.get("novelId") or "")
        if not novel_id:
            continue
        recommend_count_map[novel_id] = recommend_count_map.get(novel_id, 0) + 1

    top_items = [
        {"novel": novel_map[novel_id], "recommendCount": count}
        for novel_id, count in recommend_count_map.items()
        if novel_id in novel_map
    ]
    top_items.sort(
        key=lambda item: (
            -item["recommendCount"],
            -float(item["novel"].get("rating") or 0),
        )
    )

    editor_items: list[dict[str, Any]] = []
    for doc in editor_docs:
        novel_id = str(doc.get("novelId") or "")
        if novel_id not in novel_map:
            continue
        editor_items.append(
            {
                "novel": novel_map[novel_id],
                "editorName": editor_map.get(str(doc.get("editorId") or ""), "Biên tập viên"),
                "recommendCount": recommend_count_map.get(novel_id, 0),
                "createdAt": _iso(doc.get("createdAt")),
            }
        )

    editor_items.sort(
        key=lambda item: (
            -item["recommendCount"],
            item["createdAt"] or "",
        ),
        reverse=False,
    )
    editor_items.reverse()

    for item in editor_items:
        item.pop("createdAt", None)

    return top_items, editor_items


async def _fetch_home_recent_comments(db: AsyncSession, *, take: int = 10) -> list[dict[str, Any]]:
    rows = (
        await db.execute(
            text(
                'SELECT c.id, c.content, c."createdAt", u.name AS username, n.slug AS novel_slug, n.title AS novel_title '
                'FROM "Comment" c '
                'JOIN "User" u ON u.id = c."userId" '
                'JOIN "Novel" n ON n.id = c."novelId" '
                'ORDER BY c."createdAt" DESC '
                'LIMIT :take'
            ),
            {"take": take},
        )
    ).mappings().all()

    return [
        {
            "id": row["id"],
            "content": row["content"],
            "createdAt": _iso(row["createdAt"]),
            "user": {"name": row.get("username")},
            "novel": {"slug": row["novel_slug"], "title": row["novel_title"]},
        }
        for row in rows
    ]


async def _fetch_home_latest_novels(db: AsyncSession, *, take: int = 5) -> list[dict[str, Any]]:
    recent_chapters = (
        await db.execute(
            text(
                'SELECT id, "novelId", number, title, "createdAt" '
                'FROM "ChapterMeta" ORDER BY "createdAt" DESC NULLS LAST, id DESC LIMIT 400'
            )
        )
    ).mappings().all()

    latest_novel_ids: list[str] = []
    latest_seen_ids: set[str] = set()
    latest_chapter_map: dict[str, dict[str, Any]] = {}

    for row in recent_chapters:
        novel_id = str(row.get("novelId") or "").strip()
        if not novel_id or novel_id in latest_seen_ids:
            continue
        latest_seen_ids.add(novel_id)
        latest_novel_ids.append(novel_id)
        latest_chapter_map[novel_id] = {
            "number": row.get("number"),
            "title": row.get("title"),
            "createdAt": _iso(row.get("createdAt")),
        }
        if len(latest_novel_ids) >= 500:
            break

    if not latest_novel_ids:
        return []

    novel_rows = (
        await db.execute(
            text(
                'SELECT id, slug, title, "authorName", "coverColor", "coverUrl", rating, views, '
                '"totalChapters", status, description, "bookmarkCount", "seriesId", "updatedAt" '
                'FROM "Novel" '
                'WHERE id = ANY(:novel_ids)'
            ),
            {"novel_ids": latest_novel_ids},
        )
    ).mappings().all()

    novel_map = {row["id"]: _home_novel_from_row(dict(row)) for row in novel_rows}
    ordered = [novel_map[novel_id] for novel_id in latest_novel_ids if novel_id in novel_map]
    collapsed = _collapse_series_rows(ordered)[:take]

    for novel in collapsed:
        novel["latestChapter"] = latest_chapter_map.get(novel["id"])

    return collapsed


def _to_hot_slide(row: dict[str, Any], source: str) -> dict[str, Any]:
    novel = row["novel"]
    return {
        "id": novel["id"],
        "slug": novel["slug"],
        "title": novel["title"],
        "authorName": novel["authorName"],
        "description": novel["description"],
        "coverUrl": novel["coverUrl"],
        "totalChapters": novel["totalChapters"],
        "rating": novel["rating"],
        "views": row["aggregatedViews"],
        "status": novel["status"],
        "hotSource": source,
    }


async def _load_bookmark_with_novel(db: AsyncSession, user_id: str, novel_id: str) -> dict[str, Any] | None:
    result = await db.execute(
        text(
            'SELECT b.id, b."novelId", b."lastChapterId", b."lastChapterNumber", '
            'b."readChapters", '
            'n.id AS novel_id, n.title AS novel_title, n.slug AS novel_slug, n."authorName" AS novel_author_name, '
            'n."coverUrl" AS novel_cover_url, n.status AS novel_status, n."totalChapters" AS novel_total_chapters, '
            'n.rating AS novel_rating, n."ratingCount" AS novel_rating_count '
            'FROM "Bookmark" b '
            'JOIN "Novel" n ON n.id = b."novelId" '
            'WHERE b."userId" = :user_id AND b."novelId" = :novel_id '
            'LIMIT 1'
        ),
        {"user_id": user_id, "novel_id": novel_id},
    )
    row = result.mappings().first()
    if not row:
        return None

    return {
        "id": row["id"],
        "novelId": row["novelId"],
        "lastChapterId": row["lastChapterId"],
        "lastChapterNumber": row["lastChapterNumber"],
        "readChapters": row["readChapters"] or [],
        "novel": {
            "id": row["novel_id"],
            "title": row["novel_title"],
            "slug": row["novel_slug"],
            "authorName": row["novel_author_name"],
            "coverUrl": row["novel_cover_url"],
            "status": row["novel_status"],
            "totalChapters": row["novel_total_chapters"],
            "rating": float(row["novel_rating"] or 0),
            "ratingCount": int(row["novel_rating_count"] or 0),
        },
    }


async def _update_reading_progress(
    db: AsyncSession,
    user_id: str,
    novel_id: str,
    chapter_id: str,
    chapter_number: int,
) -> dict[str, Any]:
    row = (
        await db.execute(
            text(
                'SELECT id, "readChapters", "hasCountedView" FROM "Bookmark" '
                'WHERE "userId" = :user_id AND "novelId" = :novel_id LIMIT 1'
            ),
            {"user_id": user_id, "novel_id": novel_id},
        )
    ).mappings().first()

    read_chapters = list((row["readChapters"] if row else []) or [])
    has_counted_view = bool(row["hasCountedView"] if row else False)

    if chapter_number not in read_chapters:
        read_chapters.append(chapter_number)

    should_increment_view = len(read_chapters) >= 5 and not has_counted_view
    if should_increment_view:
        has_counted_view = True

    if row:
        await db.execute(
            text(
                'UPDATE "Bookmark" '
                'SET "lastChapterId" = :chapter_id, "lastChapterNumber" = :chapter_number, '
                '"readChapters" = :read_chapters, "hasCountedView" = :has_counted_view '
                'WHERE id = :bookmark_id'
            ),
            {
                "chapter_id": chapter_id,
                "chapter_number": chapter_number,
                "read_chapters": read_chapters,
                "has_counted_view": has_counted_view,
                "bookmark_id": row["id"],
            },
        )
    else:
        await db.execute(
            text(
                'INSERT INTO "Bookmark"(id, "userId", "novelId", "lastChapterId", "lastChapterNumber", '
                '"readChapters", "hasCountedView", "createdAt") '
                'VALUES (:id, :user_id, :novel_id, :chapter_id, :chapter_number, :read_chapters, :has_counted_view, NOW())'
            ),
            {
                "id": _new_id("bm_"),
                "user_id": user_id,
                "novel_id": novel_id,
                "chapter_id": chapter_id,
                "chapter_number": chapter_number,
                "read_chapters": read_chapters,
                "has_counted_view": has_counted_view,
            },
        )

    if should_increment_view:
        day = dt.datetime.now(dt.timezone.utc).date()
        await db.execute(
            text('UPDATE "Novel" SET views = views + 1 WHERE id = :novel_id'),
            {"novel_id": novel_id},
        )
        await db.execute(
            text(
                'INSERT INTO "NovelViewDaily"(id, "novelId", day, views, "createdAt", "updatedAt") '
                'VALUES (:id, :novel_id, :day, 1, NOW(), NOW()) '
                'ON CONFLICT ("novelId", day) DO UPDATE '
                'SET views = "NovelViewDaily".views + 1, "updatedAt" = NOW()'
            ),
            {"id": _new_id("nvd_"), "novel_id": novel_id, "day": day},
        )

    bookmark = await _load_bookmark_with_novel(db, user_id, novel_id)
    return {"status": "updated", "bookmark": bookmark}


@app.get("/api/health")
async def healthcheck(db: AsyncSession = Depends(get_db_session)):
    db_ok = False

    try:
        await db.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False

    status = "ok" if db_ok else "degraded"
    return {
        "status": status,
        "service": settings.app_name,
        "environment": settings.app_env,
        "checks": {"postgres": db_ok},
    }


_VN_ORDER: dict[str, tuple[int, int]] = {
    **{c: (1, i) for i, c in enumerate("aàảãáạ")},
    **{c: (2, i) for i, c in enumerate("ăằẳẵắặ")},
    **{c: (3, i) for i, c in enumerate("âầẩẫấậ")},
    "b": (4, 0), "c": (5, 0), "d": (6, 0), "đ": (7, 0),
    **{c: (8, i) for i, c in enumerate("eèẻẽéẹ")},
    **{c: (9, i) for i, c in enumerate("êềểễếệ")},
    "g": (10, 0), "h": (11, 0),
    **{c: (12, i) for i, c in enumerate("iìỉĩíị")},
    "k": (13, 0), "l": (14, 0), "m": (15, 0), "n": (16, 0),
    **{c: (17, i) for i, c in enumerate("oòỏõóọ")},
    **{c: (18, i) for i, c in enumerate("ôồổỗốộ")},
    **{c: (19, i) for i, c in enumerate("ơờởỡớợ")},
    "p": (20, 0), "q": (21, 0), "r": (22, 0), "s": (23, 0), "t": (24, 0),
    **{c: (25, i) for i, c in enumerate("uùủũúụ")},
    **{c: (26, i) for i, c in enumerate("ưừửữứự")},
    "v": (27, 0), "x": (28, 0),
    **{c: (29, i) for i, c in enumerate("yỳỷỹýỵ")},
}


def _vn_sort_key(s: str) -> list[tuple[int, int]]:
    return [_VN_ORDER.get(c, (ord(c), 0)) for c in s.lower()]


@app.get("/api/genres")
async def list_genres(db: AsyncSession = Depends(get_db_session)):
    result = await db.execute(
        text(
            'SELECT g.id, g.name, g.slug, g.description, g.icon, COUNT(ng."novelId")::int AS "novelCount" '
            'FROM "Genre" g '
            'LEFT JOIN "NovelGenre" ng ON ng."genreId" = g.id '
            'GROUP BY g.id'
        )
    )
    rows = [dict(row) for row in result.mappings().all()]
    rows.sort(key=lambda r: _vn_sort_key(r["name"]))
    return rows


@app.get("/api/genres/{slug}")
async def get_genre_by_slug(slug: str, db: AsyncSession = Depends(get_db_session)):
    result = await db.execute(
        text('SELECT id, name, slug, description, icon FROM "Genre" WHERE slug = :slug LIMIT 1'),
        {"slug": slug},
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Genre not found")
    return dict(row)


@app.get("/api/mod/the-loai")
async def mod_list_genres(
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_current_user),
):
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")
    rows = (
        await db.execute(
            text('SELECT id, name, slug, description, icon FROM "Genre" ORDER BY name ASC')
        )
    ).mappings().all()
    return [dict(r) for r in rows]


@app.post("/api/mod/the-loai")
async def mod_create_genre(
    payload: dict[str, Any] = Body(...),
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_current_user),
):
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")
    name = " ".join((str(payload.get("name") or "")).split()).strip()
    if not name:
        raise HTTPException(status_code=400, detail="Tên thể loại không hợp lệ")
    slug = _norm_title(name).replace(" ", "-")[:120] or _new_id("genre_")

    existing = (
        await db.execute(
            text('SELECT id, name, slug, description, icon FROM "Genre" WHERE lower(name) = :name OR slug = :slug LIMIT 1'),
            {"name": name.lower(), "slug": slug},
        )
    ).mappings().first()
    if existing:
        return dict(existing)

    row = (
        await db.execute(
            text(
                'INSERT INTO "Genre" (id, name, slug, description, icon) '
                'VALUES (:id, :name, :slug, :description, :icon) '
                'RETURNING id, name, slug, description, icon'
            ),
            {
                "id": _new_id("genre_"),
                "name": name,
                "slug": slug,
                "description": payload.get("description"),
                "icon": payload.get("icon"),
            },
        )
    ).mappings().first()
    await db.commit()
    return dict(row) if row else {}


@app.put("/api/mod/the-loai")
async def mod_update_genre(
    payload: dict[str, Any] = Body(...),
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_current_user),
):
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")
    genre_id = str(payload.get("id") or "").strip()
    if not genre_id:
        raise HTTPException(status_code=400, detail="id là bắt buộc")
    name = " ".join((str(payload.get("name") or "")).split()).strip()
    if not name:
        raise HTTPException(status_code=400, detail="Tên thể loại không hợp lệ")
    slug = _norm_title(name).replace(" ", "-")[:120] or genre_id

    row = (
        await db.execute(
            text(
                'UPDATE "Genre" SET name = :name, slug = :slug, description = :description, icon = :icon '
                'WHERE id = :id RETURNING id, name, slug, description, icon'
            ),
            {
                "id": genre_id,
                "name": name,
                "slug": slug,
                "description": payload.get("description"),
                "icon": payload.get("icon"),
            },
        )
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Genre not found")
    await db.commit()
    return dict(row)


@app.delete("/api/mod/the-loai")
async def mod_delete_genre(
    id: str,
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_current_user),
):
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")

    await db.execute(text('DELETE FROM "NovelGenre" WHERE "genreId" = :id'), {"id": id})
    row = (
        await db.execute(
            text('DELETE FROM "Genre" WHERE id = :id RETURNING id, name'),
            {"id": id},
        )
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Genre not found")
    await db.commit()
    return {"id": row["id"], "name": row["name"], "deleted": True}


@app.post("/api/mod/the-loai/merge")
async def mod_merge_genre(
    payload: dict[str, Any] = Body(...),
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_current_user),
):
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")
    source_id = str(payload.get("sourceId") or "").strip()
    target_id = str(payload.get("targetId") or "").strip()
    if not source_id or not target_id:
        raise HTTPException(status_code=400, detail="sourceId và targetId là bắt buộc")
    if source_id == target_id:
        raise HTTPException(status_code=400, detail="sourceId và targetId phải khác nhau")

    src = (await db.execute(text('SELECT id FROM "Genre" WHERE id = :id LIMIT 1'), {"id": source_id})).mappings().first()
    tgt = (await db.execute(text('SELECT id FROM "Genre" WHERE id = :id LIMIT 1'), {"id": target_id})).mappings().first()
    if not src or not tgt:
        raise HTTPException(status_code=404, detail="Genre not found")

    await db.execute(
        text(
            'INSERT INTO "NovelGenre" ("novelId", "genreId") '
            'SELECT "novelId", :target_id FROM "NovelGenre" WHERE "genreId" = :source_id '
            'ON CONFLICT ("novelId", "genreId") DO NOTHING'
        ),
        {"source_id": source_id, "target_id": target_id},
    )
    await db.execute(text('DELETE FROM "NovelGenre" WHERE "genreId" = :source_id'), {"source_id": source_id})
    await db.execute(text('DELETE FROM "Genre" WHERE id = :source_id'), {"source_id": source_id})
    await db.commit()
    return {"merged": True, "sourceId": source_id, "targetId": target_id}


async def _ensure_unique_slug(db: AsyncSession, *, table: str, slug: str, current_id: str | None = None) -> str:
    base = slug or _new_id("slug_")
    candidate = base
    idx = 1
    while True:
        row = (await db.execute(text(f'SELECT id FROM "{table}" WHERE slug = :slug LIMIT 1'), {"slug": candidate})).mappings().first()
        if not row:
            return candidate
        if current_id and str(row.get("id") or "") == current_id:
            return candidate
        idx += 1
        candidate = f"{base}-{idx}"


async def _resolve_series_id(
    db: AsyncSession,
    *,
    series_id: str | None,
    series_name: str | None,
) -> str | None:
    _ = db
    _ = series_name
    sid = str(series_id or "").strip()
    return sid or None


async def _set_novel_genres(db: AsyncSession, novel_id: str, genre_ids: list[str]) -> None:
    clean_ids = [str(g).strip() for g in (genre_ids or []) if str(g).strip()]
    await db.execute(text('DELETE FROM "NovelGenre" WHERE "novelId" = :novel_id'), {"novel_id": novel_id})
    if not clean_ids:
        return
    valid_rows = (
        await db.execute(
            text('SELECT id FROM "Genre" WHERE id = ANY(:ids)'),
            {"ids": clean_ids},
        )
    ).mappings().all()
    for r in valid_rows:
        await db.execute(
            text('INSERT INTO "NovelGenre" ("novelId", "genreId") VALUES (:novel_id, :genre_id) ON CONFLICT DO NOTHING'),
            {"novel_id": novel_id, "genre_id": str(r["id"])},
        )


async def _delete_novel_by_id(db: AsyncSession, novel_id: str) -> bool:
    novel_row = (
        await db.execute(
            text('SELECT id, "coverUrl" FROM "Novel" WHERE id = :id LIMIT 1'),
            {"id": novel_id},
        )
    ).mappings().first()
    if not novel_row:
        return False

    chapter_rows = (
        await db.execute(
            text(
                'SELECT m.id, m.number, c."txtHref", c."rawHtmlHref" '
                'FROM "ChapterMeta" m '
                'LEFT JOIN "ChapterContentRef" c ON c."chapterId" = m.id '
                'WHERE m."novelId" = :novel_id'
            ),
            {"novel_id": novel_id},
        )
    ).mappings().all()

    for row in chapter_rows:
        txt_href = str(row.get("txtHref") or "").strip()
        raw_href = str(row.get("rawHtmlHref") or "").strip()
        if txt_href:
            try:
                storage.delete_href(txt_href)
            except Exception:
                pass
        if raw_href:
            try:
                storage.delete_href(raw_href)
            except Exception:
                pass

    chapter_ids = [str(r["id"]) for r in chapter_rows]
    if chapter_ids:
        await db.execute(text('DELETE FROM "ChapterContentRef" WHERE "chapterId" = ANY(:chapter_ids)'), {"chapter_ids": chapter_ids})

    cover_key = _r2_key_from_cover_url(str(novel_row.get("coverUrl") or ""))
    if cover_key:
        _delete_r2_key(cover_key)

    await db.execute(text('DELETE FROM "ChapterMeta" WHERE "novelId" = :novel_id'), {"novel_id": novel_id})
    await db.execute(text('DELETE FROM "NovelGenre" WHERE "novelId" = :novel_id'), {"novel_id": novel_id})
    await db.execute(text('DELETE FROM "Bookmark" WHERE "novelId" = :novel_id'), {"novel_id": novel_id})
    await db.execute(text('DELETE FROM "Comment" WHERE "novelId" = :novel_id'), {"novel_id": novel_id})
    await db.execute(text('DELETE FROM "NovelViewDaily" WHERE "novelId" = :novel_id'), {"novel_id": novel_id})
    deleted = (await db.execute(text('DELETE FROM "Novel" WHERE id = :id RETURNING id'), {"id": novel_id})).mappings().first()
    return bool(deleted)





@app.get("/api/mod/truyen")
async def mod_list_novels(
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_current_user),
):
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")
    rows = (
        await db.execute(
            text(
                'SELECT n.id, n.title, n.slug, n."authorName", n.status, n."totalChapters", n."coverUrl", '
                'NULL::text AS series_id, NULL::text AS series_name, NULL::text AS series_slug '
                'FROM "Novel" n '
                'ORDER BY n."updatedAt" DESC, n."createdAt" DESC'
            )
        )
    ).mappings().all()
    return [
        {
            "id": r["id"],
            "title": r["title"],
            "slug": r["slug"],
            "authorName": r.get("authorName") or "",
            "status": r.get("status") or "Đang ra",
            "totalChapters": int(r.get("totalChapters") or 0),
            "coverUrl": r.get("coverUrl"),
            "series": (
                {"id": r["series_id"], "name": r["series_name"], "slug": r["series_slug"]}
                if r.get("series_id")
                else None
            ),
        }
        for r in rows
    ]


@app.get("/api/mod/truyen/{novel_id}")
async def mod_get_novel_detail(
    novel_id: str,
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_current_user),
):
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")
    row = (
        await db.execute(
            text(
                'SELECT n.id, n.title, n.slug, n."authorName", n."originalTitle", n."originalAuthorName", '
                'n.description, n."coverUrl", n.status, n."totalChapters", '
                'NULL::text AS series_id, NULL::text AS series_name, NULL::text AS series_slug '
                'FROM "Novel" n WHERE n.id = :id LIMIT 1'
            ),
            {"id": novel_id},
        )
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Novel not found")
    genre_rows = (
        await db.execute(
            text('SELECT g.id, g.name, g.slug FROM "NovelGenre" ng JOIN "Genre" g ON g.id = ng."genreId" WHERE ng."novelId" = :id ORDER BY g.name ASC'),
            {"id": novel_id},
        )
    ).mappings().all()
    return {
        "id": row["id"],
        "title": row["title"],
        "slug": row["slug"],
        "authorName": row.get("authorName") or "",
        "originalTitle": row.get("originalTitle") or "",
        "originalAuthorName": row.get("originalAuthorName") or "",
        "description": row.get("description") or "",
        "coverUrl": row.get("coverUrl"),
        "status": row.get("status") or "Đang ra",
        "totalChapters": int(row.get("totalChapters") or 0),
        "series": (
            {"id": row["series_id"], "name": row["series_name"], "slug": row["series_slug"]}
            if row.get("series_id")
            else None
        ),
        "genres": [dict(g) for g in genre_rows],
    }


@app.post("/api/mod/truyen")
async def mod_create_novel(
    payload: ModNovelPayload,
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_current_user),
):
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")
    title = " ".join((payload.title or "").split()).strip()
    author_name = " ".join((payload.authorName or "").split()).strip()
    if not title or not author_name:
        raise HTTPException(status_code=400, detail="title và authorName là bắt buộc")

    slug_base = _norm_title(title).replace(" ", "-")[:120] or _new_id("n_")
    slug = await _ensure_unique_slug(db, table="Novel", slug=slug_base)
    resolved_series_id = await _resolve_series_id(db, series_id=payload.seriesId, series_name=payload.seriesName)

    novel_id = _new_id("n_")
    row = (
        await db.execute(
            text(
                'INSERT INTO "Novel" (id, title, slug, "authorName", "originalTitle", "originalAuthorName", description, "coverUrl", status, "seriesId", "totalChapters", views, rating, "ratingCount", "bookmarkCount", "createdAt", "updatedAt") '
                'VALUES (:id,:title,:slug,:author,:original_title,:original_author,:description,:cover_url,:status,:series_id,0,0,0,0,0,NOW(),NOW()) '
                'RETURNING id, title, slug, "authorName", status, "totalChapters", "coverUrl"'
            ),
            {
                "id": novel_id,
                "title": title,
                "slug": slug,
                "author": author_name,
                "original_title": (payload.originalTitle or "").strip() or None,
                "original_author": (payload.originalAuthorName or "").strip() or None,
                "description": (payload.description or "").strip(),
                "cover_url": (payload.coverUrl or "").strip() or None,
                "status": (payload.status or "Đang ra").strip() or "Đang ra",
                "series_id": resolved_series_id,
            },
        )
    ).mappings().first()
    await _set_novel_genres(db, novel_id, payload.genreIds or [])
    await db.commit()
    return dict(row) if row else {}


@app.put("/api/mod/truyen")
async def mod_update_novel(
    payload: ModNovelPayload,
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_current_user),
):
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")
    novel_id = str(payload.id or "").strip()
    if not novel_id:
        raise HTTPException(status_code=400, detail="id là bắt buộc")

    current = (
        await db.execute(text('SELECT id, title, slug, "seriesId" FROM "Novel" WHERE id = :id LIMIT 1'), {"id": novel_id})
    ).mappings().first()
    if not current:
        raise HTTPException(status_code=404, detail="Novel not found")

    next_title = " ".join((payload.title or str(current.get("title") or "")).split()).strip()
    next_author = " ".join((payload.authorName or "").split()).strip()
    if not next_title:
        raise HTTPException(status_code=400, detail="title không hợp lệ")
    if payload.authorName is not None and not next_author:
        raise HTTPException(status_code=400, detail="authorName không hợp lệ")

    slug_base = _norm_title(next_title).replace(" ", "-")[:120] or str(current.get("slug") or _new_id("n_"))
    next_slug = await _ensure_unique_slug(db, table="Novel", slug=slug_base, current_id=novel_id)

    use_series_name = payload.seriesName is not None and str(payload.seriesName).strip() != ""
    if use_series_name:
        next_series_id = await _resolve_series_id(db, series_id=None, series_name=payload.seriesName)
    elif payload.seriesId is not None:
        next_series_id = await _resolve_series_id(db, series_id=payload.seriesId, series_name=None)
    else:
        next_series_id = current.get("seriesId")

    row = (
        await db.execute(
            text(
                'UPDATE "Novel" SET '
                'title = :title, slug = :slug, '
                '"authorName" = COALESCE(:author_name, "authorName"), '
                '"originalTitle" = :original_title, "originalAuthorName" = :original_author, '
                'description = :description, "coverUrl" = :cover_url, '
                'status = COALESCE(:status, status), "seriesId" = :series_id, "updatedAt" = NOW() '
                'WHERE id = :id '
                'RETURNING id, title, slug, "authorName", status, "totalChapters", "coverUrl"'
            ),
            {
                "id": novel_id,
                "title": next_title,
                "slug": next_slug,
                "author_name": next_author or None,
                "original_title": (payload.originalTitle or "").strip() or None,
                "original_author": (payload.originalAuthorName or "").strip() or None,
                "description": (payload.description or "").strip(),
                "cover_url": (payload.coverUrl or "").strip() or None,
                "status": (payload.status or "").strip() or None,
                "series_id": next_series_id,
            },
        )
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Novel not found")
    if payload.genreIds is not None:
        await _set_novel_genres(db, novel_id, payload.genreIds)
    await db.commit()
    return dict(row)


@app.delete("/api/mod/truyen")
async def mod_delete_novel(
    id: str,
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_current_user),
):
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")
    deleted = await _delete_novel_by_id(db, id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Novel not found")
    await db.commit()
    return {"id": id, "deleted": True}


@app.post("/api/mod/truyen/bulk")
async def mod_bulk_novel_action(
    payload: dict[str, Any] = Body(...),
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_current_user),
):
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")
    action = str(payload.get("action") or "delete").strip().lower() or "delete"
    raw_ids = payload.get("ids")
    if not isinstance(raw_ids, list):
        raw_ids = payload.get("novelIds")
    ids = [str(i).strip() for i in (raw_ids or []) if str(i).strip()]
    if action != "delete":
        raise HTTPException(status_code=400, detail="Unsupported bulk action")
    if not ids:
        raise HTTPException(status_code=400, detail="ids is required")
    deleted_count = 0
    for novel_id in ids:
        if await _delete_novel_by_id(db, novel_id):
            deleted_count += 1
    await db.commit()
    return {"action": action, "deletedCount": deleted_count}


@app.get("/api/mod/truyen/missing")
async def mod_list_missing_novels(
    missing: str = "author,cover,description,genres",
    q: str = "",
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_current_user),
):
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")

    keys = {k.strip() for k in missing.split(",") if k.strip()}
    filters: list[str] = []
    if "author" in keys:
        filters.append('(COALESCE(TRIM(n."authorName"), \'\') = \'\')')
    if "cover" in keys:
        filters.append('(COALESCE(TRIM(n."coverUrl"), \'\') = \'\')')
    if "description" in keys:
        filters.append('(COALESCE(TRIM(n.description), \'\') = \'\')')
    if "genres" in keys:
        filters.append('(NOT EXISTS (SELECT 1 FROM "NovelGenre" ng2 WHERE ng2."novelId" = n.id))')

    where_parts: list[str] = []
    params: dict[str, Any] = {}
    if filters:
        where_parts.append(f"({' OR '.join(filters)})")
    if q.strip():
        params["q"] = f"%{q.strip()}%"
        where_parts.append('(n.title ILIKE :q OR n.slug ILIKE :q OR n."authorName" ILIKE :q)')
    where_sql = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""

    rows = (
        await db.execute(
            text(
                'SELECT n.id, n.title, n.slug, n."authorName", n."coverUrl", n.description, n."totalChapters", n."updatedAt", '
                'NULL::text AS series_id, NULL::text AS series_name, NULL::text AS series_slug '
                'FROM "Novel" n '
                f'{where_sql} '
                'ORDER BY n."updatedAt" DESC, n.title ASC LIMIT 2000'
            ),
            params,
        )
    ).mappings().all()

    novel_ids = [str(r["id"]) for r in rows]
    genre_map: dict[str, list[dict[str, Any]]] = {nid: [] for nid in novel_ids}
    if novel_ids:
        genre_rows = (
            await db.execute(
                text('SELECT ng."novelId", g.id, g.name, g.slug FROM "NovelGenre" ng JOIN "Genre" g ON g.id = ng."genreId" WHERE ng."novelId" = ANY(:novel_ids) ORDER BY g.name ASC'),
                {"novel_ids": novel_ids},
            )
        ).mappings().all()
        for g in genre_rows:
            genre_map[str(g["novelId"])].append({"id": g["id"], "name": g["name"], "slug": g["slug"]})

    items: list[dict[str, Any]] = []
    for r in rows:
        genres = genre_map.get(str(r["id"]), [])
        author_blank = not str(r.get("authorName") or "").strip()
        cover_blank = not str(r.get("coverUrl") or "").strip()
        desc_blank = not str(r.get("description") or "").strip()
        genre_blank = len(genres) == 0
        items.append(
            {
                "id": r["id"],
                "title": r["title"],
                "slug": r["slug"],
                "authorName": r.get("authorName") or "",
                "coverUrl": r.get("coverUrl"),
                "description": r.get("description") or "",
                "totalChapters": int(r.get("totalChapters") or 0),
                "updatedAt": _iso(r.get("updatedAt")),
                "series": (
                    {"id": r["series_id"], "name": r["series_name"], "slug": r["series_slug"]}
                    if r.get("series_id")
                    else None
                ),
                "genres": genres,
                "missing": {
                    "author": author_blank,
                    "cover": cover_blank,
                    "description": desc_blank,
                    "genres": genre_blank,
                },
            }
        )

    return {"items": items}


@app.patch("/api/mod/truyen/missing")
async def mod_patch_missing_novels(
    payload: ModNovelMissingBulkPatchPayload,
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_current_user),
):
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")

    updated_count = 0
    failures: list[dict[str, Any]] = []

    for item in payload.updates:
        novel_id = str(item.id or "").strip()
        if not novel_id:
            failures.append({"id": item.id, "error": "id không hợp lệ"})
            continue
        try:
            exists = (await db.execute(text('SELECT id FROM "Novel" WHERE id = :id LIMIT 1'), {"id": novel_id})).mappings().first()
            if not exists:
                failures.append({"id": novel_id, "error": "Novel not found"})
                continue

            await db.execute(
                text(
                    'UPDATE "Novel" SET '
                    '"authorName" = COALESCE(:author_name, "authorName"), '
                    '"coverUrl" = COALESCE(:cover_url, "coverUrl"), '
                    'description = COALESCE(:description, description), '
                    '"updatedAt" = NOW() '
                    'WHERE id = :id'
                ),
                {
                    "id": novel_id,
                    "author_name": (item.authorName or "").strip() or None,
                    "cover_url": (item.coverUrl or "").strip() or None,
                    "description": (item.description or "").strip() or None,
                },
            )
            if item.genreIds is not None:
                await _set_novel_genres(db, novel_id, item.genreIds)
            updated_count += 1
        except Exception as exc:
            failures.append({"id": novel_id, "error": str(exc)})

    await db.commit()
    return {
        "updatedCount": updated_count,
        "failureCount": len(failures),
        "failures": failures,
    }


@app.get("/api/mod/overview")
async def mod_overview(
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_current_user),
):
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")
    novel_count = (await db.execute(text('SELECT COUNT(*)::int FROM "Novel"'))).scalar_one()
    total_views = (await db.execute(text('SELECT COALESCE(SUM(views),0)::int FROM "Novel"'))).scalar_one()
    comment_count = (await db.execute(text('SELECT COUNT(*)::int FROM "Comment"'))).scalar_one()
    series_count = 0
    return {
        "novelCount": int(novel_count or 0),
        "totalViews": int(total_views or 0),
        "commentCount": int(comment_count or 0),
        "seriesCount": int(series_count or 0),
    }


async def _ensure_editor_recommendation_table(db: AsyncSession) -> None:
    await db.execute(
        text(
            'CREATE TABLE IF NOT EXISTS "EditorRecommendationDoc" ('
            'id TEXT PRIMARY KEY, '
            '"editorId" TEXT NOT NULL, '
            '"novelId" TEXT NOT NULL, '
            'content TEXT, '
            '"createdAt" TIMESTAMPTZ NOT NULL DEFAULT NOW()'
            ')'
        )
    )
    await db.execute(
        text('CREATE INDEX IF NOT EXISTS "EditorRecommendationDoc_novel_idx" ON "EditorRecommendationDoc"("novelId")')
    )
    await db.commit()


@app.get("/api/mod/de-cu")
async def mod_list_recommendations(
    q: str = "",
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_current_user),
):
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")
    await _ensure_editor_recommendation_table(db)

    docs = (
        await db.execute(
            text('SELECT id, "editorId", "novelId", "createdAt" FROM "EditorRecommendationDoc" ORDER BY "createdAt" DESC LIMIT 5000')
        )
    ).mappings().all()
    novel_ids = list({str(d.get("novelId") or "") for d in docs if d.get("novelId")})
    editor_ids = list({str(d.get("editorId") or "") for d in docs if d.get("editorId")})

    novel_map: dict[str, dict[str, Any]] = {}
    if novel_ids:
        rows = (
            await db.execute(
                text('SELECT id, title, slug, "authorName", "coverUrl", status, "totalChapters" FROM "Novel" WHERE id = ANY(:ids)'),
                {"ids": novel_ids},
            )
        ).mappings().all()
        novel_map = {str(r["id"]): dict(r) for r in rows}

    editor_map: dict[str, str] = {}
    if editor_ids:
        rows = (
            await db.execute(
                text('SELECT id, name FROM "User" WHERE id = ANY(:ids)'),
                {"ids": editor_ids},
            )
        ).mappings().all()
        editor_map = {str(r["id"]): str(r.get("name") or "Biên tập viên") for r in rows}

    rec_count_map: dict[str, int] = {}
    for d in docs:
        nid = str(d.get("novelId") or "")
        if not nid:
            continue
        rec_count_map[nid] = rec_count_map.get(nid, 0) + 1

    items: list[dict[str, Any]] = []
    for d in docs:
        nid = str(d.get("novelId") or "")
        if nid not in novel_map:
            continue
        eid = str(d.get("editorId") or "")
        items.append(
            {
                "id": str(d.get("id")),
                "createdAt": _iso(d.get("createdAt")),
                "recommendCount": int(rec_count_map.get(nid, 0)),
                "novel": novel_map[nid],
                "editor": {"id": eid, "name": editor_map.get(eid, "Biên tập viên")},
            }
        )

    summary = [
        {"novel": novel_map[nid], "recommendCount": int(count)}
        for nid, count in rec_count_map.items()
        if nid in novel_map
    ]
    summary.sort(key=lambda x: (-int(x["recommendCount"]), str(x["novel"].get("title") or "")))

    params: dict[str, Any] = {}
    where_sql = ""
    if q.strip():
        params["q"] = f"%{q.strip()}%"
        where_sql = 'WHERE title ILIKE :q OR slug ILIKE :q OR "authorName" ILIKE :q'
    candidates_rows = (
        await db.execute(
            text(
                f'SELECT id, title, slug, "authorName", "coverUrl", status, "totalChapters" FROM "Novel" '
                f'{where_sql} ORDER BY "updatedAt" DESC LIMIT 100'
            ),
            params,
        )
    ).mappings().all()
    my_editor_id = str(user.get("id") or "")
    my_novel_ids = {str(d.get("novelId") or "") for d in docs if str(d.get("editorId") or "") == my_editor_id}
    candidates = []
    for r in candidates_rows:
        nid = str(r["id"])
        candidates.append(
            {
                **dict(r),
                "alreadyRecommended": nid in my_novel_ids,
                "recommendCount": int(rec_count_map.get(nid, 0)),
            }
        )

    my_count = sum(1 for d in docs if str(d.get("editorId") or "") == my_editor_id)
    return {
        "items": items,
        "summary": summary,
        "candidates": candidates,
        "myNovelIds": list(my_novel_ids),
        "currentUser": {
            "id": my_editor_id,
            "role": str(user.get("role") or "USER"),
            "recommendationCount": my_count,
            "maxRecommendationCount": 5,
        },
    }


@app.post("/api/mod/de-cu")
async def mod_create_recommendation(
    payload: dict[str, Any] = Body(...),
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_current_user),
):
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")
    await _ensure_editor_recommendation_table(db)
    novel_id = str(payload.get("novelId") or "").strip()
    if not novel_id:
        raise HTTPException(status_code=400, detail="novelId is required")
    novel_exists = (await db.execute(text('SELECT id FROM "Novel" WHERE id = :id LIMIT 1'), {"id": novel_id})).mappings().first()
    if not novel_exists:
        raise HTTPException(status_code=404, detail="Novel not found")
    editor_id = str(user.get("id") or "")
    existing = (
        await db.execute(
            text('SELECT id FROM "EditorRecommendationDoc" WHERE "editorId" = :editor_id AND "novelId" = :novel_id LIMIT 1'),
            {"editor_id": editor_id, "novel_id": novel_id},
        )
    ).mappings().first()
    if existing:
        raise HTTPException(status_code=409, detail="Bạn đã đề cử truyện này")
    my_count = (
        await db.execute(
            text('SELECT COUNT(*)::int FROM "EditorRecommendationDoc" WHERE "editorId" = :editor_id'),
            {"editor_id": editor_id},
        )
    ).scalar_one()
    if str(user.get("role") or "") != "ADMIN" and int(my_count or 0) >= 5:
        raise HTTPException(status_code=400, detail="Đã đạt giới hạn đề cử")
    rec_id = _new_id("erec_")
    await db.execute(
        text('INSERT INTO "EditorRecommendationDoc" (id, "editorId", "novelId", "createdAt") VALUES (:id,:editor_id,:novel_id,NOW())'),
        {"id": rec_id, "editor_id": editor_id, "novel_id": novel_id},
    )
    await db.commit()
    return {"id": rec_id, "novelId": novel_id}


@app.delete("/api/mod/de-cu")
async def mod_delete_recommendation(
    id: str,
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_current_user),
):
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")
    await _ensure_editor_recommendation_table(db)
    row = (
        await db.execute(
            text('SELECT id, "editorId" FROM "EditorRecommendationDoc" WHERE id = :id LIMIT 1'),
            {"id": id},
        )
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Recommendation not found")
    is_admin = str(user.get("role") or "") == "ADMIN"
    if not is_admin and str(row.get("editorId") or "") != str(user.get("id") or ""):
        raise HTTPException(status_code=403, detail="Forbidden")
    await db.execute(text('DELETE FROM "EditorRecommendationDoc" WHERE id = :id'), {"id": id})
    await db.commit()
    return {"id": id, "deleted": True}


async def _upsert_chapter_content(chapter_id: str, novel_id: str, number: int, content: str, db: AsyncSession) -> None:
    txt_href = f"novel-{novel_id}/{number}.txt"
    raw_href = f"novel-{novel_id}/{number}.raw.html"
    txt = str(content or "")
    await asyncio.to_thread(storage.write_text, txt_href, txt)
    await asyncio.to_thread(storage.write_text, raw_href, txt)
    h = hashlib.sha256(txt.encode("utf-8")).hexdigest()
    await db.execute(
        text(
            'INSERT INTO "ChapterContentRef" ("chapterId", "txtHref", "rawHtmlHref", "contentHash") '
            'VALUES (:id,:txt,:raw,:hash) '
            'ON CONFLICT ("chapterId") DO UPDATE SET "txtHref"=EXCLUDED."txtHref", "rawHtmlHref"=EXCLUDED."rawHtmlHref", "contentHash"=EXCLUDED."contentHash", "updatedAt"=NOW()'
        ),
        {"id": chapter_id, "txt": txt_href, "raw": raw_href, "hash": h},
    )


@app.get("/api/mod/chuong")
async def mod_list_chapters(
    novelId: str,
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_current_user),
):
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")
    skip = (page - 1) * limit
    rows = (
        await db.execute(
            text('SELECT id, number, title, views, "createdAt" FROM "ChapterMeta" WHERE "novelId" = :novel_id ORDER BY number ASC OFFSET :skip LIMIT :limit'),
            {"novel_id": novelId, "skip": skip, "limit": limit},
        )
    ).mappings().all()
    total = (await db.execute(text('SELECT COUNT(*)::int FROM "ChapterMeta" WHERE "novelId" = :novel_id'), {"novel_id": novelId})).scalar_one()
    return {
        "chapters": [
            {
                "id": r["id"],
                "_id": r["id"],
                "number": int(r.get("number") or 0),
                "title": r.get("title") or "",
                "views": int(r.get("views") or 0),
                "createdAt": _iso(r.get("createdAt")),
                "volumeNumber": None,
                "volumeTitle": None,
                "volumeChapterNumber": None,
            }
            for r in rows
        ],
        "totalChapters": int(total or 0),
        "totalPages": (int(total or 0) + limit - 1) // limit if total else 0,
        "currentPage": page,
    }


@app.get("/api/mod/chuong/{chapter_id}")
async def mod_get_chapter_detail(
    chapter_id: str,
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_current_user),
):
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")
    row = (
        await db.execute(
            text('SELECT id, "novelId", number, title, views, "createdAt" FROM "ChapterMeta" WHERE id = :id LIMIT 1'),
            {"id": chapter_id},
        )
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Chapter not found")
    content = await _resolve_chapter_content(chapter_id, db) or ""
    return {
        "id": row["id"],
        "_id": row["id"],
        "novelId": row["novelId"],
        "number": int(row.get("number") or 0),
        "title": row.get("title") or "",
        "content": content,
        "views": int(row.get("views") or 0),
        "createdAt": _iso(row.get("createdAt")),
        "volumeNumber": None,
        "volumeTitle": None,
        "volumeChapterNumber": None,
    }


@app.post("/api/mod/chuong")
async def mod_create_chapter(
    payload: ModChapterPayload,
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_current_user),
):
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")
    novel_exists = (await db.execute(text('SELECT id FROM "Novel" WHERE id = :id LIMIT 1'), {"id": payload.novelId})).mappings().first()
    if not novel_exists:
        raise HTTPException(status_code=404, detail="Novel not found")
    existing = (
        await db.execute(
            text('SELECT id FROM "ChapterMeta" WHERE "novelId" = :novel_id AND number = :number LIMIT 1'),
            {"novel_id": payload.novelId, "number": payload.number},
        )
    ).mappings().first()
    if existing:
        raise HTTPException(status_code=409, detail="Chapter number already exists")
    cid = _new_id("cmeta_")
    await db.execute(
        text('INSERT INTO "ChapterMeta" (id, "novelId", number, title, views, "createdAt") VALUES (:id,:novel,:num,:title,0,NOW())'),
        {"id": cid, "novel": payload.novelId, "num": payload.number, "title": payload.title.strip()},
    )
    await _upsert_chapter_content(cid, payload.novelId, payload.number, payload.content, db)
    await db.execute(text('UPDATE "Novel" SET "totalChapters" = (SELECT COUNT(*) FROM "ChapterMeta" WHERE "novelId" = :novel_id), "updatedAt" = NOW() WHERE id = :novel_id'), {"novel_id": payload.novelId})
    await db.commit()
    return {"id": cid, "created": True}


@app.put("/api/mod/chuong")
async def mod_update_chapter(
    payload: ModChapterPayload,
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_current_user),
):
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")
    chapter_id = str(payload.id or "").strip()
    if not chapter_id:
        raise HTTPException(status_code=400, detail="id is required")
    row = (
        await db.execute(
            text('SELECT id, "novelId" FROM "ChapterMeta" WHERE id = :id LIMIT 1'),
            {"id": chapter_id},
        )
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Chapter not found")
    await db.execute(
        text('UPDATE "ChapterMeta" SET number = :num, title = :title WHERE id = :id'),
        {"id": chapter_id, "num": payload.number, "title": payload.title.strip()},
    )
    await _upsert_chapter_content(chapter_id, str(row["novelId"]), payload.number, payload.content, db)
    await db.execute(text('UPDATE "Novel" SET "totalChapters" = (SELECT COUNT(*) FROM "ChapterMeta" WHERE "novelId" = :novel_id), "updatedAt" = NOW() WHERE id = :novel_id'), {"novel_id": row["novelId"]})
    await db.commit()
    return {"id": chapter_id, "updated": True}


@app.delete("/api/mod/chuong")
async def mod_delete_chapter(
    id: str,
    novelId: str,
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_current_user),
):
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")
    await db.execute(text('DELETE FROM "ChapterContentRef" WHERE "chapterId" = :id'), {"id": id})
    row = (
        await db.execute(
            text('DELETE FROM "ChapterMeta" WHERE id = :id AND "novelId" = :novel_id RETURNING id'),
            {"id": id, "novel_id": novelId},
        )
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Chapter not found")
    await db.execute(text('UPDATE "Novel" SET "totalChapters" = (SELECT COUNT(*) FROM "ChapterMeta" WHERE "novelId" = :novel_id), "updatedAt" = NOW() WHERE id = :novel_id'), {"novel_id": novelId})
    await db.commit()
    return {"id": id, "deleted": True}


@app.post("/api/mod/chuong/bulk-delete")
async def mod_bulk_delete_chapters(
    payload: ModChapterBulkDeletePayload,
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_current_user),
):
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")
    from_num = min(payload.fromNumber, payload.toNumber)
    to_num = max(payload.fromNumber, payload.toNumber)
    ids = (
        await db.execute(
            text('SELECT id FROM "ChapterMeta" WHERE "novelId" = :novel_id AND number BETWEEN :from_num AND :to_num'),
            {"novel_id": payload.novelId, "from_num": from_num, "to_num": to_num},
        )
    ).mappings().all()
    chapter_ids = [str(r["id"]) for r in ids]
    if chapter_ids:
        await db.execute(text('DELETE FROM "ChapterContentRef" WHERE "chapterId" = ANY(:ids)'), {"ids": chapter_ids})
    deleted_count = (
        await db.execute(
            text('DELETE FROM "ChapterMeta" WHERE "novelId" = :novel_id AND number BETWEEN :from_num AND :to_num RETURNING id'),
            {"novel_id": payload.novelId, "from_num": from_num, "to_num": to_num},
        )
    ).mappings().all()
    await db.execute(text('UPDATE "Novel" SET "totalChapters" = (SELECT COUNT(*) FROM "ChapterMeta" WHERE "novelId" = :novel_id), "updatedAt" = NOW() WHERE id = :novel_id'), {"novel_id": payload.novelId})
    await db.commit()
    return {"deletedCount": len(deleted_count)}


@app.put("/api/mod/chuong/optimize")
async def mod_optimize_chapters(
    payload: ModChapterOptimizePayload,
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_current_user),
):
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")
    modified = 0
    for item in payload.updates:
        row = (
            await db.execute(
                text('SELECT id FROM "ChapterMeta" WHERE id = :id AND "novelId" = :novel_id LIMIT 1'),
                {"id": item.id, "novel_id": payload.novelId},
            )
        ).mappings().first()
        if not row:
            continue
        await db.execute(
            text('UPDATE "ChapterMeta" SET number = :number, title = :title WHERE id = :id'),
            {"id": item.id, "number": item.number, "title": item.title},
        )
        modified += 1
    await db.execute(text('UPDATE "Novel" SET "totalChapters" = (SELECT COUNT(*) FROM "ChapterMeta" WHERE "novelId" = :novel_id), "updatedAt" = NOW() WHERE id = :novel_id'), {"novel_id": payload.novelId})
    await db.commit()
    return {"modifiedCount": modified}


@app.post("/api/mod/chuong/global-replace")
async def mod_global_replace_chapters(
    payload: ModChapterGlobalReplacePayload,
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_current_user),
):
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")
    rows = (
        await db.execute(
            text('SELECT id, number, title FROM "ChapterMeta" WHERE "novelId" = :novel_id ORDER BY number ASC'),
            {"novel_id": payload.novelId},
        )
    ).mappings().all()
    flags = 0 if payload.matchCase else re.IGNORECASE
    previews: list[dict[str, Any]] = []
    updated = 0
    for r in rows:
        cid = str(r["id"])
        content = await _resolve_chapter_content(cid, db) or ""
        new_content = content
        if payload.action == "replace":
            find_text = str(payload.findText or "")
            if not find_text:
                continue
            pattern = re.compile(re.escape(find_text), flags)
            new_content = pattern.sub(str(payload.replaceText or ""), content)
        elif payload.action == "trash":
            for tw in payload.trashWords:
                if not str(tw).strip():
                    continue
                pattern = re.compile(re.escape(str(tw)), flags)
                new_content = pattern.sub("", new_content)
        else:
            raise HTTPException(status_code=400, detail="Unsupported action")

        if new_content == content:
            continue
        if payload.preview:
            previews.append(
                {
                    "chapterId": cid,
                    "number": int(r.get("number") or 0),
                    "title": str(r.get("title") or ""),
                    "snippet": new_content[:240],
                }
            )
            if len(previews) >= 50:
                break
        else:
            await _upsert_chapter_content(cid, payload.novelId, int(r.get("number") or 0), new_content, db)
            updated += 1
    if payload.preview:
        return {"previews": previews}
    await db.commit()
    return {"updatedChapters": updated}


@app.get("/api/mod/truyen/{novel_id}/trash-words")
async def mod_get_trash_words(
    novel_id: str,
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_current_user),
):
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")
    row = (
        await db.execute(text('SELECT "trashWords" FROM "Novel" WHERE id = :id LIMIT 1'), {"id": novel_id})
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Novel not found")
    return {"trashWords": list(row.get("trashWords") or [])}


@app.put("/api/mod/truyen/{novel_id}/trash-words")
async def mod_set_trash_words(
    novel_id: str,
    payload: ModNovelTrashWordsPayload,
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_current_user),
):
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")
    clean = [str(w).strip() for w in payload.trashWords if str(w).strip()]
    row = (
        await db.execute(
            text('UPDATE "Novel" SET "trashWords" = :words, "updatedAt" = NOW() WHERE id = :id RETURNING id'),
            {"id": novel_id, "words": clean},
        )
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Novel not found")
    await db.commit()
    return {"novelId": novel_id, "trashWords": clean}


@app.post("/api/mod/upload-cover")
async def mod_upload_cover(
    file: UploadFile = File(...),
    user: dict = Depends(require_current_user),
):
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Empty file")
    ext = ".jpg"
    ct = (file.content_type or "").lower()
    if "png" in ct:
        ext = ".png"
    elif "webp" in ct:
        ext = ".webp"
    elif "jpeg" in ct or "jpg" in ct:
        ext = ".jpg"
    url = _upload_cover_bytes_to_r2(content, ext, key_prefix=f"mod-cover-{_new_id()}")
    if not url:
        raise HTTPException(status_code=500, detail="Upload failed")
    return {"url": url}


@app.post("/api/mod/epub")
async def mod_epub_upload(
    file: UploadFile = File(...),
    preview: str | None = Form(default=None),
    splitMode: str | None = Form(default=None),
    chapterRegex: str | None = Form(default=None),
    title: str | None = Form(default=None),
    authorName: str | None = Form(default=None),
    description: str | None = Form(default=None),
    seriesMode: str | None = Form(default=None),
    seriesId: str | None = Form(default=None),
    seriesName: str | None = Form(default=None),
    replaceExisting: str | None = Form(default=None),
    appendTargetNovelId: str | None = Form(default=None),
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_current_user),
):
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty EPUB")

    suffix = ".epub"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(raw)
        tmp_path = Path(tmp.name)

    try:
        mode = "regex" if (splitMode or "").lower() == "regex" else "toc"
        pattern = (chapterRegex or "").strip() or None
        chapters = _epub_extract_with_mode(tmp_path, mode, pattern)
        epub_meta = _extract_epub_metadata(tmp_path)
        inferred_title = str(epub_meta.get("title") or Path(file.filename or "novel").stem)
        inferred_author = str(epub_meta.get("author") or "Unknown")
        inferred_desc = str(epub_meta.get("description") or "")
        inferred_genres = [str(g).strip() for g in (epub_meta.get("genres") or []) if str(g).strip()]

        base_title = " ".join((title or inferred_title).split()).strip() or "Untitled"
        base_author = " ".join((authorName or inferred_author).split()).strip() or "Unknown"
        base_desc = (description if description is not None else inferred_desc).strip()
        cover_extracted = _extract_epub_cover(tmp_path) or _extract_epub_cover_from_zip(tmp_path)
        has_cover = bool(cover_extracted)
        cover_preview_data_url: str | None = None
        uploaded_cover_url: str | None = None
        if cover_extracted:
            cover_bytes, cover_ext = cover_extracted
            cover_ext = _guess_image_extension(cover_bytes)
            mime = _mime_from_extension(cover_ext)
            cover_preview_data_url = f"data:{mime};base64,{base64.b64encode(cover_bytes).decode('ascii')}"
            uploaded_cover_url = _upload_cover_bytes_to_r2(cover_bytes, cover_ext, key_prefix=f"epub-cover-{_new_id()}")

        if str(preview or "").lower() == "true":
            return {
                "preview": True,
                "fileName": file.filename or "upload.epub",
                "splitMode": mode,
                "detectedStructureType": "standard",
                "hasCoverFromEpub": has_cover,
                "coverPreviewDataUrl": cover_preview_data_url,
                "parserInfo": {
                    "splitMode": mode,
                    "chapterRegexUsed": pattern,
                    "sourceSections": len(chapters),
                    "chaptersDetected": len(chapters),
                    "chaptersFinal": len(chapters),
                    "insertedMissingChapters": len([c for c in chapters if c.get("is_placeholder")]),
                    "detectedMaxChapterNumber": max([int(c.get("number") or 0) for c in chapters], default=0),
                    "detectedNumberAssignments": len([c for c in chapters if int(c.get("number") or 0) > 0]),
                },
                "novel": {
                    "title": base_title,
                    "authorName": base_author,
                    "description": base_desc,
                    "detectedGenres": inferred_genres,
                    "totalChapters": len(chapters),
                },
                "chaptersPreview": [
                    {
                        "number": int(c.get("number") or 0),
                        "title": str(c.get("title") or ""),
                        "isPlaceholder": bool(c.get("is_placeholder") or False),
                        "volumeNumber": None,
                        "volumeTitle": None,
                        "volumeChapterNumber": None,
                        "excerpt": str(c.get("txt") or "")[:200],
                    }
                    for c in chapters[:30]
                ],
            }

        target_novel_id = str(appendTargetNovelId or "").strip()
        if target_novel_id:
            exists = (await db.execute(text('SELECT id FROM "Novel" WHERE id = :id LIMIT 1'), {"id": target_novel_id})).mappings().first()
            if not exists:
                raise HTTPException(status_code=404, detail="Target novel not found")
            added = 0
            replaced = 0
            for ch in chapters:
                num = int(ch.get("number") or 0)
                if num <= 0:
                    continue
                existing_ch = (
                    await db.execute(
                        text('SELECT id FROM "ChapterMeta" WHERE "novelId" = :novel_id AND number = :num LIMIT 1'),
                        {"novel_id": target_novel_id, "num": num},
                    )
                ).mappings().first()
                if existing_ch:
                    await db.execute(text('UPDATE "ChapterMeta" SET title = :title WHERE id = :id'), {"id": existing_ch["id"], "title": str(ch.get("title") or f"Chapter {num}")})
                    if not bool(ch.get("is_placeholder") or False):
                        await _upsert_chapter_content(str(existing_ch["id"]), target_novel_id, num, str(ch.get("txt") or ""), db)
                    replaced += 1
                else:
                    cid = _new_id("cmeta_")
                    await db.execute(text('INSERT INTO "ChapterMeta" (id, "novelId", number, title, views, "createdAt") VALUES (:id,:novel,:num,:title,0,NOW())'), {"id": cid, "novel": target_novel_id, "num": num, "title": str(ch.get("title") or f"Chapter {num}")})
                    if not bool(ch.get("is_placeholder") or False):
                        await _upsert_chapter_content(cid, target_novel_id, num, str(ch.get("txt") or ""), db)
                    added += 1
            await db.execute(text('UPDATE "Novel" SET "totalChapters" = (SELECT COUNT(*) FROM "ChapterMeta" WHERE "novelId" = :novel_id), "updatedAt" = NOW() WHERE id = :novel_id'), {"novel_id": target_novel_id})
            await db.commit()
            return {
                "novelId": target_novel_id,
                "parserInfo": {"chaptersFinal": len(chapters)},
                "added": added,
                "replaced": replaced,
            }

        existing_by_title = (
            await db.execute(
                text('SELECT id, title, slug FROM "Novel" WHERE lower(title) = :title LIMIT 1'),
                {"title": base_title.lower()},
            )
        ).mappings().first()
        should_replace = str(replaceExisting or "").lower() in {"1", "true", "yes", "on"}
        if existing_by_title and not should_replace:
            return Response(
                content=json.dumps(
                    {
                        "code": "DUPLICATE_TITLE",
                        "error": "Truyện đã tồn tại",
                        "canReplace": True,
                        "existingNovel": {
                            "id": existing_by_title["id"],
                            "title": existing_by_title["title"],
                            "slug": existing_by_title["slug"],
                        },
                    }
                ),
                status_code=409,
                media_type="application/json",
            )

        target_series_id: str | None = None
        sm = str(seriesMode or "none").lower()
        if sm == "existing":
            target_series_id = await _resolve_series_id(db, series_id=seriesId, series_name=None)
        elif sm == "new":
            target_series_id = await _resolve_series_id(db, series_id=None, series_name=seriesName)

        if existing_by_title and should_replace:
            novel_id = str(existing_by_title["id"])
            await db.execute(text('DELETE FROM "ChapterContentRef" WHERE "chapterId" IN (SELECT id FROM "ChapterMeta" WHERE "novelId" = :novel_id)'), {"novel_id": novel_id})
            await db.execute(text('DELETE FROM "ChapterMeta" WHERE "novelId" = :novel_id'), {"novel_id": novel_id})
            await db.execute(
                text('UPDATE "Novel" SET "authorName" = :author, description = :desc, "coverUrl" = COALESCE(:cover, "coverUrl"), "seriesId" = :series_id, "updatedAt" = NOW() WHERE id = :id'),
                {
                    "id": novel_id,
                    "author": base_author,
                    "desc": base_desc,
                    "cover": uploaded_cover_url,
                    "series_id": target_series_id,
                },
            )
        else:
            novel_id = _new_id("n_")
            slug = await _ensure_unique_slug(db, table="Novel", slug=_norm_title(base_title).replace(" ", "-")[:120] or novel_id)
            await db.execute(
                text('INSERT INTO "Novel" (id, title, slug, "authorName", description, "coverUrl", status, "seriesId", "totalChapters", views, rating, "ratingCount", "bookmarkCount", "createdAt", "updatedAt") VALUES (:id,:title,:slug,:author,:desc,:cover,:status,:series_id,0,0,0,0,0,NOW(),NOW())'),
                {
                    "id": novel_id,
                    "title": base_title,
                    "slug": slug,
                    "author": base_author,
                    "desc": base_desc,
                    "cover": uploaded_cover_url,
                    "status": "Đang ra",
                    "series_id": target_series_id,
                },
            )

        for ch in chapters:
            num = int(ch.get("number") or 0)
            if num <= 0:
                continue
            cid = _new_id("cmeta_")
            await db.execute(text('INSERT INTO "ChapterMeta" (id, "novelId", number, title, views, "createdAt") VALUES (:id,:novel,:num,:title,0,NOW())'), {"id": cid, "novel": novel_id, "num": num, "title": str(ch.get("title") or f"Chapter {num}")})
            if not bool(ch.get("is_placeholder") or False):
                await _upsert_chapter_content(cid, novel_id, num, str(ch.get("txt") or ""), db)

        await db.execute(text('UPDATE "Novel" SET "totalChapters" = (SELECT COUNT(*) FROM "ChapterMeta" WHERE "novelId" = :novel_id), "updatedAt" = NOW() WHERE id = :novel_id'), {"novel_id": novel_id})
        await db.commit()
        return {
            "novelId": novel_id,
            "replaced": bool(existing_by_title and should_replace),
            "totalChapters": len(chapters),
            "parserInfo": {"chaptersFinal": len(chapters)},
        }
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass


@app.get("/api/truyen")
async def get_novel_by_query(
    slug: str,
    db: AsyncSession = Depends(get_db_session),
):
    row = (
        await db.execute(
            text('SELECT id, title, slug, "authorName", "coverUrl", status, "totalChapters" FROM "Novel" WHERE id = :slug OR slug = :slug LIMIT 1'),
            {"slug": slug},
        )
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Novel not found")
    return dict(row)


@app.get("/api/novels/browse")
async def browse_novels(
    q: str = "",
    genre: str = "",
    status: str = "",
    sort: str = "latest",
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=20, ge=1, le=500),
    collapse_series: bool = Query(default=False),
    db: AsyncSession = Depends(get_db_session),
):
    skip = (page - 1) * limit
    outer_order_clause = {
        "popular": 'views DESC',
        "rating": 'rating DESC',
        "name": 'title ASC',
        "latest": '"updatedAt" DESC',
    }.get(sort, '"updatedAt" DESC')
    inner_order_clause = {
        "popular": 'n.views DESC',
        "rating": 'n.rating DESC',
        "name": 'n.title ASC',
        "latest": 'n."updatedAt" DESC',
    }.get(sort, 'n."updatedAt" DESC')

    where_parts: list[str] = []
    params: dict[str, Any] = {"skip": skip, "limit": limit}

    if status:
        where_parts.append('n.status = :status')
        params["status"] = status

    if genre:
        where_parts.append(
            'EXISTS (SELECT 1 FROM "NovelGenre" ng JOIN "Genre" g ON g.id = ng."genreId" '
            'WHERE ng."novelId" = n.id AND g.slug = :genre)'
        )
        params["genre"] = genre

    if q.strip():
        params["q"] = f"%{q.strip()}%"
        where_parts.append(
            '(n.title ILIKE :q OR n."originalTitle" ILIKE :q OR n."authorName" ILIKE :q '
            'OR n."originalAuthorName" ILIKE :q)'
        )

    where_sql = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""

    base_select = (
        'n.id, n.title, n.slug, n."originalTitle", n."authorName", n."coverUrl", n."coverColor", '
        'n.status, n."totalChapters", n.views, n.rating, n."ratingCount", n."bookmarkCount", '
        'n."seriesId", NULL::text AS series_id, NULL::text AS series_name, NULL::text AS series_slug, n."updatedAt"'
    )
    base_from = (
        'FROM "Novel" n '
    )

    if collapse_series:
        # DISTINCT ON picks the best novel per series (most recent), then outer query sorts+paginates
        inner_sql = (
            f'SELECT DISTINCT ON (COALESCE(n."seriesId", n.id)) {base_select} '
            f'{base_from}'
            f'{where_sql} '
            f'ORDER BY COALESCE(n."seriesId", n.id), n."updatedAt" DESC'
        )
        total_count = (
            await db.execute(
                text(f'SELECT COUNT(*)::int FROM ({inner_sql}) AS _c'),
                params,
            )
        ).scalar_one()
        rows = (
            await db.execute(
                text(
                    f'SELECT * FROM ({inner_sql}) AS collapsed '
                    f'ORDER BY {outer_order_clause} '
                    f'OFFSET :skip LIMIT :limit'
                ),
                params,
            )
        ).mappings().all()
    else:
        total_count = (
            await db.execute(
                text(f'SELECT COUNT(*)::int {base_from}{where_sql}'),
                params,
            )
        ).scalar_one()
        rows = (
            await db.execute(
                text(
                    f'SELECT {base_select} '
                    f'{base_from}'
                    f'{where_sql} '
                    f'ORDER BY {inner_order_clause} '
                    f'OFFSET :skip LIMIT :limit'
                ),
                params,
            )
        ).mappings().all()

    novel_ids = [row["id"] for row in rows]
    genre_map: dict[str, list[dict[str, str]]] = {novel_id: [] for novel_id in novel_ids}
    if novel_ids:
        genre_rows = (
            await db.execute(
                text(
                    'SELECT ng."novelId", g.id, g.name, g.slug '
                    'FROM "NovelGenre" ng '
                    'JOIN "Genre" g ON g.id = ng."genreId" '
                    'WHERE ng."novelId" = ANY(:novel_ids) '
                    'ORDER BY g.name ASC'
                ),
                {"novel_ids": novel_ids},
            )
        ).mappings().all()
        for row in genre_rows:
            genre_map[row["novelId"]].append(
                {"id": row["id"], "name": row["name"], "slug": row["slug"]}
            )

    chapter_map: dict[str, dict[str, Any]] = {}

    items: list[dict[str, Any]] = []
    for row in rows:
        items.append(
            {
                "id": row["id"],
                "title": row["title"],
                "slug": row["slug"],
                "originalTitle": row["originalTitle"],
                "authorName": row["authorName"],
                "coverUrl": row["coverUrl"],
                "coverColor": row["coverColor"],
                "status": row["status"],
                "totalChapters": row["totalChapters"],
                "views": row["views"],
                "rating": float(row["rating"] or 0),
                "ratingCount": row["ratingCount"],
                "bookmarkCount": row["bookmarkCount"],
                "seriesId": row["seriesId"],
                "series": (
                    {
                        "id": row["series_id"],
                        "name": row["series_name"],
                        "slug": row["series_slug"],
                    }
                    if row["series_id"]
                    else None
                ),
                "genres": genre_map.get(row["id"], []),
                "updatedAt": _iso(row["updatedAt"]),
                "latestChapter": chapter_map.get(row["id"]),
            }
        )

    total_pages = (total_count + limit - 1) // limit if total_count else 0
    return {
        "items": items,
        "totalCount": total_count,
        "totalPages": total_pages,
        "currentPage": page,
    }


@app.get("/api/novels/{id_or_slug}")
async def get_novel_detail(id_or_slug: str, db: AsyncSession = Depends(get_db_session)):
    row = (
        await db.execute(
            text(
                'SELECT n.id, n.title, n.slug, n."originalTitle", n."authorName", n."originalAuthorName", '
                'n.description, n."coverUrl", n."coverColor", n.status, n."totalChapters", n.views, n.rating, '
                'n."ratingCount", n."bookmarkCount", n."seriesId", n."createdAt", n."updatedAt", '
                'NULL::text AS series_id, NULL::text AS series_name, NULL::text AS series_slug '
                'FROM "Novel" n '
                'WHERE n.id = :value OR n.slug = :value '
                'LIMIT 1'
            ),
            {"value": id_or_slug},
        )
    ).mappings().first()

    if not row:
        raise HTTPException(status_code=404, detail="Novel not found")

    genres = (
        await db.execute(
            text(
                'SELECT g.id, g.name, g.slug '
                'FROM "NovelGenre" ng '
                'JOIN "Genre" g ON g.id = ng."genreId" '
                'WHERE ng."novelId" = :novel_id '
                'ORDER BY g.name ASC'
            ),
            {"novel_id": row["id"]},
        )
    ).mappings().all()

    series = None
    if row["seriesId"]:
        series_novels = (
            await db.execute(
                text(
                    'SELECT id, title, slug, "totalChapters", status, "coverUrl" '
                    'FROM "Novel" '
                    'WHERE "seriesId" = :series_id '
                    'ORDER BY title ASC'
                ),
                {"series_id": row["seriesId"]},
            )
        ).mappings().all()

        series = {
            "id": row["series_id"],
            "name": row["series_name"],
            "slug": row["series_slug"],
            "novels": [dict(item) for item in series_novels],
        }

    return {
        "id": row["id"],
        "title": row["title"],
        "slug": row["slug"],
        "originalTitle": row["originalTitle"],
        "authorName": row["authorName"],
        "originalAuthorName": row["originalAuthorName"],
        "description": row["description"],
        "coverUrl": row["coverUrl"],
        "coverColor": row["coverColor"],
        "status": row["status"],
        "totalChapters": row["totalChapters"],
        "views": row["views"],
        "rating": float(row["rating"] or 0),
        "ratingCount": row["ratingCount"],
        "bookmarkCount": row["bookmarkCount"],
        "seriesId": row["seriesId"],
        "series": series,
        "genres": [dict(item) for item in genres],
        "createdAt": _iso(row["createdAt"]),
        "updatedAt": _iso(row["updatedAt"]),
    }


@app.get("/api/truyen/{novel_id}/chapters")
async def get_novel_chapters(
    novel_id: str,
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db_session),
):
    skip = (page - 1) * limit
    chapters = (
        await db.execute(
            text(
                'SELECT id, number, title, views, "createdAt" '
                'FROM "ChapterMeta" WHERE "novelId" = :novel_id ORDER BY number ASC OFFSET :skip LIMIT :limit'
            ),
            {"novel_id": novel_id, "skip": skip, "limit": limit},
        )
    ).mappings().all()
    total_chapters = (
        await db.execute(text('SELECT COUNT(*)::int FROM "ChapterMeta" WHERE "novelId" = :novel_id'), {"novel_id": novel_id})
    ).scalar_one()

    return {
        "chapters": [
            {
                "id": str(item.get("id")),
                "number": item.get("number"),
                "title": item.get("title"),
                "views": item.get("views", 0),
                "createdAt": _iso(item.get("createdAt")),
            }
            for item in chapters
        ],
        "totalChapters": total_chapters,
        "totalPages": (total_chapters + limit - 1) // limit if total_chapters else 0,
        "currentPage": page,
    }


@app.get("/api/truyen/{novel_id}/chapters/by-number/{chapter_number}")
async def get_chapter_by_number(novel_id: str, chapter_number: int, db: AsyncSession = Depends(get_db_session)):
    chapter = (
        await db.execute(
            text(
                'SELECT id, "novelId", number, title, views, "createdAt" '
                'FROM "ChapterMeta" WHERE "novelId" = :novel_id AND number = :number LIMIT 1'
            ),
            {"novel_id": novel_id, "number": chapter_number},
        )
    ).mappings().first()
    if not chapter:
        raise HTTPException(status_code=404, detail="Chapter not found")

    prev_chapter = (
        await db.execute(
            text('SELECT number FROM "ChapterMeta" WHERE "novelId" = :novel_id AND number = :number LIMIT 1'),
            {"novel_id": novel_id, "number": chapter_number - 1},
        )
    ).mappings().first()
    next_chapter = (
        await db.execute(
            text('SELECT number FROM "ChapterMeta" WHERE "novelId" = :novel_id AND number = :number LIMIT 1'),
            {"novel_id": novel_id, "number": chapter_number + 1},
        )
    ).mappings().first()
    max_chapter = (
        await db.execute(text('SELECT COUNT(*)::int FROM "ChapterMeta" WHERE "novelId" = :novel_id'), {"novel_id": novel_id})
    ).scalar_one()

    await db.execute(text('UPDATE "ChapterMeta" SET views = views + 1 WHERE id = :id'), {"id": chapter["id"]})
    await db.commit()

    chapter_id = str(chapter.get("id"))
    content = await _resolve_chapter_content(chapter_id, db)

    return {
        "id": str(chapter.get("id")),
        "novelId": chapter.get("novelId"),
        "number": chapter.get("number"),
        "title": chapter.get("title"),
        "content": content,
        "views": int(chapter.get("views") or 0) + 1,
        "createdAt": _iso(chapter.get("createdAt")),
        "prevChapterNumber": prev_chapter.get("number") if prev_chapter else None,
        "nextChapterNumber": next_chapter.get("number") if next_chapter else None,
        "maxChapter": max_chapter,
    }


@app.get("/api/chapters/{chapter_id}")
async def get_chapter_detail(chapter_id: str, db: AsyncSession = Depends(get_db_session)):
    chapter = (
        await db.execute(
            text(
                'SELECT id, "novelId", number, title, views, "createdAt" '
                'FROM "ChapterMeta" WHERE id = :id LIMIT 1'
            ),
            {"id": chapter_id},
        )
    ).mappings().first()
    if not chapter:
        raise HTTPException(status_code=404, detail="Chapter not found")

    prev_chapter = (
        await db.execute(
            text('SELECT id, number FROM "ChapterMeta" WHERE "novelId" = :novel_id AND number = :number LIMIT 1'),
            {"novel_id": chapter.get("novelId"), "number": int(chapter.get("number") or 0) - 1},
        )
    ).mappings().first()
    next_chapter = (
        await db.execute(
            text('SELECT id, number FROM "ChapterMeta" WHERE "novelId" = :novel_id AND number = :number LIMIT 1'),
            {"novel_id": chapter.get("novelId"), "number": int(chapter.get("number") or 0) + 1},
        )
    ).mappings().first()

    content = await _resolve_chapter_content(chapter_id, db)

    return {
        "id": str(chapter.get("id")),
        "novelId": chapter.get("novelId"),
        "number": chapter.get("number"),
        "title": chapter.get("title"),
        "content": content,
        "views": chapter.get("views", 0),
        "createdAt": _iso(chapter.get("createdAt")),
        "prevChapterId": str(prev_chapter.get("id")) if prev_chapter else None,
        "prevChapterNumber": prev_chapter.get("number") if prev_chapter else None,
        "nextChapterId": str(next_chapter.get("id")) if next_chapter else None,
        "nextChapterNumber": next_chapter.get("number") if next_chapter else None,
    }


@app.get("/api/truyen/suggest")
async def suggest_novels(q: str = "", db: AsyncSession = Depends(get_db_session)):
    keyword = q.strip()
    if len(keyword) < 2:
        return []

    rows = (
        await db.execute(
            text(
                'SELECT n.id, n.title, n.slug, n."authorName", n."coverUrl", NULL::text AS series_id, NULL::text AS series_name '
                'FROM "Novel" n '
                'WHERE n.title ILIKE :q OR n."authorName" ILIKE :q '
                'ORDER BY n.views DESC, n."updatedAt" DESC '
                'LIMIT 8'
            ),
            {"q": f"%{keyword}%"},
        )
    ).mappings().all()

    return [
        {
            "id": row["id"],
            "title": row["title"],
            "slug": row["slug"],
            "authorName": row["authorName"],
            "coverUrl": row["coverUrl"],
            "series": (
                {"id": row["series_id"], "name": row["series_name"]}
                if row["series_id"]
                else None
            ),
        }
        for row in rows
    ]


class RatePayload(BaseModel):
    score: float = Field(ge=1, le=5)


class ModGenrePayload(BaseModel):
    id: str | None = None
    name: str
    description: str | None = None
    icon: str | None = None


class ModGenreMergePayload(BaseModel):
    sourceId: str
    targetId: str


class SourceAssetApprovePayload(BaseModel):
    status: str = Field(pattern="^(approved|rejected|review_required)$")


class ImportJobCreatePayload(BaseModel):
    sourceAssetId: str


class ImportJobApplyMappingPayload(BaseModel):
    novelId: str
    overwrite: bool = False


class ImportJobManualMapPayload(BaseModel):
    novelId: str
    sourceChapterNumber: int = Field(ge=1)
    targetChapterId: str
    overwrite: bool = True


class ImportJobCompletePayload(BaseModel):
    force: bool = False


class ImportApplyPayload(BaseModel):
    novelId: str
    replaceMode: str = "none"  # none | selected | range
    selectedChapterNumbers: list[int] = []
    rangeStart: int | None = None
    rangeEnd: int | None = None


class SourceAssetUpsertPayload(BaseModel):
    path: str
    sha256: str
    opfIdentifier: str | None = None
    title: str | None = None
    author: str | None = None


class SourceAssetReviewPayload(BaseModel):
    title: str | None = None
    author: str | None = None
    shortDescription: str | None = None
    genres: list[str] = []
    splitMode: str = Field(default="toc", pattern="^(toc|regex)$")
    chapterStartPattern: str | None = None
    targetMode: str = Field(default="new", pattern="^(new|existing)$")
    novelId: str | None = None
    replaceExisting: bool = False


class SourceAssetParsePreviewPayload(BaseModel):
    splitMode: str = Field(default="toc", pattern="^(toc|regex)$")
    chapterStartPattern: str | None = None


class SourceAssetStartImportPayload(BaseModel):
    replaceExisting: bool = False
    forceNovelId: str | None = None
    splitMode: str = Field(default="toc", pattern="^(toc|regex)$")
    chapterStartPattern: str | None = None


class SourceAssetAiSuggestPayload(BaseModel):
    splitMode: str = Field(default="toc", pattern="^(toc|regex)$")
    chapterStartPattern: str | None = None


class ModNovelPayload(BaseModel):
    id: str | None = None
    title: str | None = None
    originalTitle: str | None = None
    authorName: str | None = None
    originalAuthorName: str | None = None
    description: str | None = None
    coverUrl: str | None = None
    status: str | None = None
    genreIds: list[str] | None = None
    seriesId: str | None = None
    seriesName: str | None = None


class ModNovelBulkPayload(BaseModel):
    action: str
    ids: list[str]


class ModNovelMissingUpdatePayload(BaseModel):
    id: str
    authorName: str | None = None
    coverUrl: str | None = None
    description: str | None = None
    genreIds: list[str] | None = None


class ModNovelMissingBulkPatchPayload(BaseModel):
    updates: list[ModNovelMissingUpdatePayload]


class ModChapterPayload(BaseModel):
    id: str | None = None
    novelId: str
    number: int
    title: str
    content: str
    volumeNumber: int | None = None
    volumeTitle: str | None = None
    volumeChapterNumber: int | None = None


class ModChapterBulkDeletePayload(BaseModel):
    novelId: str
    fromNumber: int
    toNumber: int


class ModChapterOptimizeItem(BaseModel):
    id: str
    title: str
    number: int


class ModChapterOptimizePayload(BaseModel):
    novelId: str
    updates: list[ModChapterOptimizeItem]


class ModChapterGlobalReplacePayload(BaseModel):
    novelId: str
    action: str
    findText: str | None = None
    replaceText: str | None = None
    trashWords: list[str] = []
    matchCase: bool = False
    preview: bool = False


class ModNovelTrashWordsPayload(BaseModel):
    trashWords: list[str] = []


def _norm_title(v: str) -> str:
    s = (v or "").strip().lower()
    frm = "áàảãạăắằẳẵặâấầẩẫậéèẻẽẹêếềểễệíìỉĩịóòỏõọôốồổỗộơớờởỡợúùủũụưứừửữựýỳỷỹỵđ"
    to = "aaaaaaaaaaaaaaaaaeeeeeeeeeeeiiiiiooooooooooooooooouuuuuuuuuuuyyyyyd"
    s = s.translate(str.maketrans(frm, to))
    s = "".join(ch for ch in s if ch.isalnum() or ch.isspace())
    return " ".join(s.split())


def _title_score(a: str, b: str) -> float:
    return SequenceMatcher(None, _norm_title(a), _norm_title(b)).ratio()


def _asset_file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _derive_chapter_title(txt: str, fallback: str, number: int) -> str:
    lines = [line.strip().lstrip("#").strip() for line in txt.splitlines() if line.strip()]
    chapter_re = re.compile(r"^(?:chuong|ch\.?|chapter|hoi|quyen|phan|tap)\s*\d+(?:[\.:\-\)]\s*|\s+).+", re.IGNORECASE)
    chapter_num_re = re.compile(r"^(?:chuong|ch\.?|chapter|hoi|quyen|phan|tap)\s*\d+", re.IGNORECASE)

    for line in lines[:12]:
        normalized = _norm_title(line)
        if not normalized:
            continue
        if chapter_re.match(normalized):
            return line
        if chapter_num_re.match(normalized):
            return line

    if lines:
        first = lines[0]
        if len(first) <= 160 and len(first.split()) >= 3:
            # Prefer human-readable first heading over EPUB internal filename.
            if "/" in fallback or fallback.lower().endswith(".xhtml"):
                return first
            return first

    if fallback and "/" not in fallback and not fallback.lower().endswith(".xhtml"):
        return fallback
    return f"Chương {number}"


def _extract_title_chapter_number(title: str) -> int | None:
    normalized = _norm_title(title or "")
    if not normalized:
        return None
    m = re.search(r"(?:chuong|ch\.?|chapter|hoi|quyen|phan|tap)\s*(\d+)", normalized, re.IGNORECASE)
    if not m:
        return None
    try:
        number = int(m.group(1))
        return number if number > 0 else None
    except Exception:
        return None


def _normalize_chapter_sequence(chapters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not chapters:
        return []

    normalized_items: list[dict[str, Any]] = []
    prev_number = 0
    for idx, ch in enumerate(chapters, start=1):
        detected_number = _extract_title_chapter_number(str(ch.get("title") or ""))
        if detected_number is None:
            mapped_number = prev_number + 1 if prev_number > 0 else idx
        else:
            mapped_number = detected_number if detected_number > prev_number else (prev_number + 1)

        txt = str(ch.get("txt") or "").strip()
        raw_html = str(ch.get("raw_html") or "").strip()
        title = str(ch.get("title") or f"Chương {mapped_number}").strip()

        for missing in range(prev_number + 1, mapped_number):
            normalized_items.append(
                {
                    "number": missing,
                    "title": f"Chương {missing}",
                    "raw_html": "",
                    "txt": "",
                    "is_placeholder": True,
                }
            )

        normalized_items.append(
            {
                "number": mapped_number,
                "title": title,
                "raw_html": raw_html,
                "txt": txt,
                "is_placeholder": False,
            }
        )
        prev_number = mapped_number

    return normalized_items


def _extract_epub_chapters(epub_path: Path) -> list[dict[str, Any]]:
    from app.epub_parser import build_chapters_from_epub

    extracted = build_chapters_from_epub(epub_path)

    chapters: list[dict[str, Any]] = []
    for idx, ch in enumerate(extracted, start=1):
        content = str(ch.get("content") or "")
        if not content.strip():
            continue
        txt = str(ch.get("txt") or "").strip()
        title = _derive_chapter_title(txt, str(ch.get("title") or f"Chapter {idx}"), idx)
        chapters.append(
            {
                "number": int(ch.get("number") or idx),
                "title": title,
                "raw_html": content,
                "txt": txt,
            }
        )
    return chapters


def _is_toc_or_intro(chapter: dict[str, Any]) -> bool:
    title = _norm_title(str(chapter.get("title") or ""))
    txt = _norm_title(str(chapter.get("txt") or "")[:500])
    combined = f"{title} {txt}".strip()
    if not combined:
        return True

    if any(token in combined for token in ["muc luc", "table of contents", "contents", " nav xhtml", "toc"]):
        return True

    title_intro_markers = ["gioi thieu", "mo dau", "loi mo dau", "tom tat", "description", "synopsis", "preface"]
    if any(token in title for token in title_intro_markers):
        return True

    chapter_like = re.search(r"\b(chuong|chapter|hoi|quyen|phan|tap|chuong\s*\d+|chapter\s*\d+)\b", combined)
    if chapter_like:
        return False

    intro_markers = ["gioi thieu", "mo dau", "loi mo dau", "tom tat", "mo ta", "description", "synopsis", "preface"]
    if any(token in combined for token in intro_markers):
        return True

    # Very short non-chapter sections are likely front/back matter.
    if len(str(chapter.get("txt") or "").strip()) < 300:
        return True

    return False


def _filter_toc_chapters(chapters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not chapters:
        return []

    filtered = [ch for ch in chapters if not _is_toc_or_intro(ch)]
    if not filtered:
        # fallback: only drop obvious TOC files to avoid empty result
        filtered = [
            ch for ch in chapters
            if "muc luc" not in _norm_title(str(ch.get("txt") or "")[:500])
            and "table of contents" not in _norm_title(str(ch.get("txt") or "")[:500])
        ]

    out: list[dict[str, Any]] = []
    for idx, ch in enumerate(filtered, start=1):
        out.append({
            "number": idx,
            "title": str(ch.get("title") or f"Chapter {idx}"),
            "raw_html": str(ch.get("raw_html") or ""),
            "txt": str(ch.get("txt") or ""),
        })
    return out


def _extract_epub_chapters_by_regex(epub_path: Path, chapter_start_pattern: str) -> list[dict[str, Any]]:
    chapters = _extract_epub_chapters(epub_path)
    pattern = chapter_start_pattern.strip()
    if not pattern:
        return chapters
    re_compiled = re.compile(pattern, re.IGNORECASE | re.MULTILINE)

    merged: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for ch in chapters:
        title = str(ch.get("title") or "")
        txt = str(ch.get("txt") or "")
        raw_html = str(ch.get("raw_html") or "")
        starts = bool(re_compiled.search(title)) or bool(re_compiled.search(txt))
        if starts:
            if current:
                merged.append(current)
            current = {
                "number": len(merged) + 1,
                "title": title or f"Chapter {len(merged) + 1}",
                "txt": txt,
                "raw_html": raw_html,
            }
        else:
            if current is None:
                # Ignore front/back matter before first real chapter match.
                continue
            current["txt"] = f"{current['txt']}\n\n{txt}".strip()
            current["raw_html"] = f"{current['raw_html']}\n{raw_html}".strip()
    if current:
        merged.append(current)
    return merged if merged else chapters


def _chapter_preview_samples(chapters: list[dict[str, Any]], sample_size: int = 10) -> list[dict[str, Any]]:
    if not chapters:
        return []

    head = chapters[:sample_size]
    if len(chapters) <= sample_size * 2:
        middle = chapters[sample_size:]
        tail = []
    else:
        mid_start = max((len(chapters) // 2) - (sample_size // 2), sample_size)
        middle = chapters[mid_start:mid_start + sample_size]
        tail = chapters[-sample_size:]

    seen: set[int] = set()
    out: list[dict[str, Any]] = []
    for group, label in [(head, "head"), (middle, "middle"), (tail, "tail")]:
        for ch in group:
            number = int(ch.get("number") or 0)
            if number in seen:
                continue
            seen.add(number)
            txt = str(ch.get("txt") or "")
            out.append(
                {
                    "bucket": label,
                    "number": number,
                    "title": str(ch.get("title") or ""),
                    "chars": len(txt),
                    "preview": txt[:280] if txt else ("(placeholder - no content)" if ch.get("is_placeholder") else ""),
                    "isPlaceholder": bool(ch.get("is_placeholder") or False),
                }
            )
    return out


def _epub_extract_with_mode(epub_path: Path, split_mode: str, chapter_start_pattern: str | None) -> list[dict[str, Any]]:
    if split_mode == "regex":
        default_vi_regex = r"^\s*(?:[#>*\-\[]\s*)*(?:ch(?:u\.?|ương|uong)?|chapter|hồi|hoi|quyển|quyen|phần|phan|tập|tap)\s*\d+(?:[\.:\-\)]\s*|\s+).+$"
        effective_pattern = chapter_start_pattern or default_vi_regex
        try:
            return _normalize_chapter_sequence(_extract_epub_chapters_by_regex(epub_path, effective_pattern))
        except re.error as exc:
            raise HTTPException(status_code=400, detail=f"Invalid chapterStartPattern: {exc}") from exc
    return _normalize_chapter_sequence(_filter_toc_chapters(_extract_epub_chapters(epub_path)))


async def _ensure_genre_ids(db: AsyncSession, names: list[str]) -> list[str]:
    out: list[str] = []
    for raw_name in names:
        name = " ".join((raw_name or "").split()).strip()
        if not name:
            continue
        slug = _norm_title(name).replace(" ", "-")[:120] or _new_id("genre_")
        existing = (
            await db.execute(
                text('SELECT id FROM "Genre" WHERE lower(name) = :name OR slug = :slug LIMIT 1'),
                {"name": name.lower(), "slug": slug},
            )
        ).mappings().first()
        if existing:
            out.append(str(existing["id"]))
            continue
        gid = _new_id("genre_")
        await db.execute(
            text('INSERT INTO "Genre" (id, name, slug, description, icon) VALUES (:id, :name, :slug, NULL, NULL)'),
            {"id": gid, "name": name, "slug": slug},
        )
        out.append(gid)
    return out


def _ensure_genre_ids_sync(db: Any, names: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw_name in names:
        name = " ".join((raw_name or "").split()).strip()
        if not name:
            continue
        slug = _norm_title(name).replace(" ", "-")[:120] or _new_id("genre_")
        if slug in seen:
            continue
        seen.add(slug)
        existing = db.execute(
            text('SELECT id FROM "Genre" WHERE lower(name) = :name OR slug = :slug LIMIT 1'),
            {"name": name.lower(), "slug": slug},
        ).mappings().first()
        if existing:
            out.append(str(existing["id"]))
            continue
        gid = _new_id("genre_")
        db.execute(
            text('INSERT INTO "Genre" (id, name, slug, description, icon) VALUES (:id, :name, :slug, NULL, NULL)'),
            {"id": gid, "name": name, "slug": slug},
        )
        out.append(gid)
    return out


def _build_ai_genre_suggestions(chapters: list[dict[str, Any]]) -> list[str]:
    hay = " ".join([str(ch.get("title") or "") + " " + str(ch.get("txt") or "")[:800] for ch in chapters[:8]]).lower()
    mapping = [
        ("tiên hiệp", ["tu tiên", "linh khí", "đan điền", "nguyên anh"]),
        ("kiếm hiệp", ["kiếm", "giang hồ", "môn phái", "võ công"]),
        ("đô thị", ["thành phố", "công ty", "tổng tài", "đô thị"]),
        ("hệ thống", ["hệ thống", "nhiệm vụ", "kỹ năng", "điểm thưởng"]),
        ("huyền huyễn", ["ma pháp", "huyền", "long", "thần"]),
        ("xuyên không", ["xuyên", "trùng sinh", "trở về", "quá khứ"]),
        ("ngôn tình", ["tình yêu", "hôn", "nam chính", "nữ chính"]),
        ("trinh thám", ["vụ án", "hung thủ", "điều tra", "manh mối"]),
    ]
    picked: list[str] = []
    for genre, keys in mapping:
        if any(k in hay for k in keys):
            picked.append(genre)
        if len(picked) >= 6:
            break
    if not picked:
        picked = ["tiểu thuyết"]
    return picked[:6]


def _build_ai_description(title: str, author: str | None, chapters: list[dict[str, Any]]) -> str:
    first = (str(chapters[0].get("txt") or "")[:180] if chapters else "").strip()
    author_text = author or "Tác giả chưa rõ"
    if first:
        return f"{title} của {author_text} mở ra câu chuyện với nhịp đọc cuốn hút, tập trung vào hành trình nhân vật chính và các bước ngoặt liên tiếp. Bối cảnh được triển khai rõ nét, phù hợp cho độc giả thích theo dõi mạch truyện dài hơi."
    return f"{title} là tác phẩm của {author_text}, có nhịp truyện rõ ràng và dễ theo dõi theo từng chương. Nội dung phù hợp để đọc liên tục với mạch phát triển ổn định."


def _extract_epub_cover(epub_path: Path) -> tuple[bytes, str] | None:
    from ebooklib import ITEM_COVER, ITEM_IMAGE
    from ebooklib import epub as epublib

    try:
        book = epublib.read_epub(str(epub_path), options={"ignore_ncx": False})
    except Exception:
        return None

    try:
        direct_cover = book.get_cover()
        if direct_cover and len(direct_cover) >= 2:
            cover_bytes = direct_cover[1]
            if cover_bytes:
                name = str(direct_cover[0] or "").lower()
                ext = ".jpg"
                if name.endswith(".png"):
                    ext = ".png"
                elif name.endswith(".webp"):
                    ext = ".webp"
                return cover_bytes, ext
    except Exception:
        pass

    for item in book.get_items():
        try:
            media_type = str(getattr(item, "media_type", "") or "")
            name = str(getattr(item, "file_name", "") or getattr(item, "get_name", lambda: "")() or "").lower()
            item_type = item.get_type() if hasattr(item, "get_type") else None

            is_image = media_type.startswith("image/") or item_type == ITEM_IMAGE
            is_cover = item_type == ITEM_COVER or "cover" in name
            if not is_image:
                continue

            data = item.get_content() if hasattr(item, "get_content") else b""
            if not data:
                continue

            # Prefer explicit cover first, otherwise fallback to first image.
            if is_cover or not name:
                ext = ".jpg"
                if media_type == "image/png":
                    ext = ".png"
                elif media_type == "image/webp":
                    ext = ".webp"
                return data, ext
        except Exception:
            continue

    # Fallback: first image in book package.
    for item in book.get_items():
        try:
            media_type = str(getattr(item, "media_type", "") or "")
            if not media_type.startswith("image/"):
                continue
            data = item.get_content() if hasattr(item, "get_content") else b""
            if not data:
                continue
            ext = ".jpg"
            if media_type == "image/png":
                ext = ".png"
            elif media_type == "image/webp":
                ext = ".webp"
            return data, ext
        except Exception:
            continue
    return None


def _extract_epub_cover_from_zip(epub_path: Path) -> tuple[bytes, str] | None:
    try:
        with zipfile.ZipFile(epub_path, "r") as zf:
            names = zf.namelist()
            lower_map = {name.lower(): name for name in names}
            preferred = [
                "cover.jpg", "cover.jpeg", "cover.png", "cover.webp",
                "images/cover.jpg", "images/cover.jpeg", "images/cover.png", "images/cover.webp",
                "oebps/cover.jpg", "oebps/cover.jpeg", "oebps/cover.png", "oebps/cover.webp",
            ]
            for candidate in preferred:
                actual = lower_map.get(candidate)
                if not actual:
                    continue
                data = zf.read(actual)
                if data:
                    return data, _guess_image_extension(data)

            for name in names:
                low = name.lower()
                if not low.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
                    continue
                if "cover" not in low:
                    continue
                data = zf.read(name)
                if data:
                    return data, _guess_image_extension(data)
    except Exception:
        return None
    return None


def _extract_epub_metadata(epub_path: Path) -> dict[str, Any]:
    from ebooklib import epub as epublib

    try:
        book = epublib.read_epub(str(epub_path), options={"ignore_ncx": False})
    except Exception:
        return {"title": None, "author": None, "description": None, "genres": []}

    def _first_text(namespace: str, key: str) -> str | None:
        try:
            values = book.get_metadata(namespace, key)
        except Exception:
            values = []
        for value in values or []:
            raw = value[0] if isinstance(value, tuple) else value
            text_value = str(raw or "").strip()
            if text_value:
                return text_value
        return None

    title = _first_text("DC", "title")
    author = _first_text("DC", "creator")
    description = _first_text("DC", "description")

    subjects: list[str] = []
    try:
        for value in book.get_metadata("DC", "subject") or []:
            raw = value[0] if isinstance(value, tuple) else value
            text_value = str(raw or "").strip()
            if text_value:
                subjects.append(text_value)
    except Exception:
        pass

    result = {
        "title": title,
        "author": author,
        "description": description,
        "genres": subjects[:8],
    }
    if result["title"] or result["author"] or result["description"] or result["genres"]:
        return result

    try:
        with zipfile.ZipFile(epub_path, "r") as zf:
            container_xml = zf.read("META-INF/container.xml")
            croot = ET.fromstring(container_xml)
            rootfile = croot.find('.//{*}rootfile')
            if rootfile is None:
                return result
            opf_path = rootfile.attrib.get("full-path")
            if not opf_path:
                return result
            opf_xml = zf.read(opf_path)
            oroot = ET.fromstring(opf_xml)
            t = oroot.find('.//{*}title')
            a = oroot.find('.//{*}creator')
            d = oroot.find('.//{*}description')
            s = oroot.findall('.//{*}subject')
            title2 = (t.text or "").strip() if t is not None and t.text else None
            author2 = (a.text or "").strip() if a is not None and a.text else None
            desc2 = (d.text or "").strip() if d is not None and d.text else None
            genres2 = [str(x.text or "").strip() for x in s if x is not None and str(x.text or "").strip()][:8]
            return {
                "title": title2,
                "author": author2,
                "description": desc2,
                "genres": genres2,
            }
    except Exception:
        return result

    return result


def _guess_image_extension(image_bytes: bytes) -> str:
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if image_bytes.startswith(b"RIFF") and b"WEBP" in image_bytes[:16]:
        return ".webp"
    if image_bytes.startswith(b"GIF87a") or image_bytes.startswith(b"GIF89a"):
        return ".gif"
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    return ".jpg"


def _mime_from_extension(ext: str) -> str:
    if ext == ".png":
        return "image/png"
    if ext == ".webp":
        return "image/webp"
    if ext == ".gif":
        return "image/gif"
    return "image/jpeg"


def _resolve_epub_source_path(asset_path: str, sha256_hint: str | None = None) -> Path | None:
    raw = str(asset_path or "").strip()
    if not raw:
        return None

    direct = Path(raw)
    if direct.exists():
        return direct

    root = Path(settings.epub_source_root)
    candidate = root / raw
    if candidate.exists():
        return candidate

    normalized = raw.replace("\\", "/")
    candidate2 = root / normalized
    if candidate2.exists():
        return candidate2

    basename = Path(normalized).name
    if basename:
        try:
            matches = list(root.rglob(basename))
            if matches:
                return matches[0]
        except Exception:
            pass

    if sha256_hint:
        target_sha = str(sha256_hint).strip().lower()
        if target_sha:
            try:
                for candidate in root.rglob("*.epub"):
                    try:
                        if _asset_file_sha256(candidate).lower() == target_sha:
                            return candidate
                    except Exception:
                        continue
            except Exception:
                pass

    return None


def _extract_epub_preview_payload(epub_path: Path) -> dict[str, Any]:
    cover = _extract_epub_cover(epub_path) or _extract_epub_cover_from_zip(epub_path)
    cover_bytes: bytes | None = None
    cover_ext: str | None = None
    cover_data_url: str | None = None
    if cover:
        cover_bytes, cover_ext = cover
        cover_ext = _guess_image_extension(cover_bytes)
        mime = _mime_from_extension(cover_ext)
        cover_data_url = f"data:{mime};base64,{base64.b64encode(cover_bytes).decode('ascii')}"

    meta = _extract_epub_metadata(epub_path)
    title = str(meta.get("title") or "").strip() or epub_path.stem
    author = str(meta.get("author") or "").strip() or "Unknown"
    description = str(meta.get("description") or "").strip()
    genres = [str(g).strip() for g in (meta.get("genres") or []) if str(g).strip()][:8]

    return {
        "coverFound": bool(cover_bytes),
        "coverBytes": cover_bytes,
        "coverExt": cover_ext,
        "coverPreviewDataUrl": cover_data_url,
        "title": title,
        "author": author,
        "description": description,
        "genres": genres,
    }


def _upload_cover_bytes_to_r2(image_bytes: bytes, extension: str, *, key_prefix: str) -> str | None:
    if not image_bytes:
        return None
    if (
        not settings.r2_account_id
        or not settings.r2_access_key_id
        or not settings.r2_secret_access_key
        or not settings.r2_bucket_name
    ):
        return None

    try:
        s3 = boto3.client(
            "s3",
            endpoint_url=f"https://{settings.r2_account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=settings.r2_access_key_id,
            aws_secret_access_key=settings.r2_secret_access_key,
            region_name="auto",
        )
        key = f"covers/{key_prefix}-{int(time.time() * 1000)}{extension}"
        content_type = "image/jpeg"
        if extension == ".png":
            content_type = "image/png"
        elif extension == ".webp":
            content_type = "image/webp"

        s3.put_object(
            Bucket=settings.r2_bucket_name,
            Key=key,
            Body=image_bytes,
            ContentType=content_type,
            CacheControl="public, max-age=31536000, immutable",
        )

        base = (settings.r2_public_base_url or "").rstrip("/")
        if base:
            return f"{base}/{key}"
        return key
    except Exception:
        return None


def _upload_cover_to_r2(image_bytes: bytes, extension: str, *, source_asset_id: str) -> str | None:
    return _upload_cover_bytes_to_r2(
        image_bytes,
        extension,
        key_prefix=f"import-cover-{source_asset_id}",
    )


def _r2_key_from_cover_url(cover_url: str | None) -> str | None:
    raw = str(cover_url or "").strip()
    if not raw:
        return None
    if raw.startswith("covers/"):
        return raw
    base = (settings.r2_public_base_url or "").rstrip("/")
    if base and raw.startswith(base + "/"):
        key = raw[len(base) + 1 :]
        return key or None
    return None


def _delete_r2_key(key: str | None) -> bool:
    target = str(key or "").strip()
    if not target:
        return False
    if (
        not settings.r2_account_id
        or not settings.r2_access_key_id
        or not settings.r2_secret_access_key
        or not settings.r2_bucket_name
    ):
        return False
    try:
        s3 = boto3.client(
            "s3",
            endpoint_url=f"https://{settings.r2_account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=settings.r2_access_key_id,
            aws_secret_access_key=settings.r2_secret_access_key,
            region_name="auto",
        )
        s3.delete_object(Bucket=settings.r2_bucket_name, Key=target)
        return True
    except Exception:
        return False


def _map_genres_to_existing(candidates: list[str], existing_genres: list[str], *, limit: int = 6) -> list[str]:
    existing_clean = [g.strip() for g in existing_genres if g and g.strip()]
    existing_norm = [(_norm_title(g), g) for g in existing_clean]

    output: list[str] = []
    used_norm: set[str] = set()
    for raw in candidates:
        name = (raw or "").strip()
        if not name:
            continue
        cand_norm = _norm_title(name)
        if not cand_norm:
            continue

        best_name = name
        best_score = 0.0
        for ex_norm, ex_name in existing_norm:
            if cand_norm == ex_norm:
                best_name = ex_name
                best_score = 1.0
                break
            score = SequenceMatcher(None, cand_norm, ex_norm).ratio()
            if score > best_score:
                best_score = score
                best_name = ex_name

        # Snap to existing genre when similarity is strong.
        final_name = best_name if best_score >= 0.86 else name
        final_norm = _norm_title(final_name)
        if final_norm in used_norm:
            continue
        used_norm.add(final_norm)
        output.append(final_name)
        if len(output) >= limit:
            break

    return output


_ROUTER_MODEL_CACHE: dict[str, Any] = {"expires_at": 0.0, "models": []}


async def _router_pick_models() -> list[str]:
    api_key = (settings.router_api_key or "").strip()

    now = time.time()
    if _ROUTER_MODEL_CACHE.get("expires_at", 0.0) > now:
        return list(_ROUTER_MODEL_CACHE.get("models") or [])

    candidates: list[tuple[int, str]] = []
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(
                f"{str(settings.router_base_url).rstrip('/')}/models",
                headers=headers,
            )
        response.raise_for_status()
        for item in (response.json().get("data") or []):
            model_id = str(item.get("id") or "").strip()
            if not model_id:
                continue
            low = model_id.lower()
            if any(x in low for x in ["vision", "image", "audio", "realtime", "embedding", "moderation"]):
                continue
            score = 0
            if "gpt-5.5" in low:
                score += 1000
            elif "gpt-5" in low:
                score += 900
            elif "claude" in low:
                score += 700
            elif "gemini" in low:
                score += 650
            else:
                score += 100
            candidates.append((score, model_id))
    except Exception:
        candidates = []

    candidates.sort(key=lambda x: x[0], reverse=True)
    picked = [m for _, m in candidates[:6]]
    _ROUTER_MODEL_CACHE["models"] = picked
    _ROUTER_MODEL_CACHE["expires_at"] = now + 600
    return picked


async def _router_ai_suggest(
    title: str,
    author: str,
    chapters: list[dict[str, Any]],
    existing_genres: list[str],
) -> dict[str, Any] | None:
    api_key = (settings.router_api_key or "").strip()

    samples: list[str] = []
    if chapters:
        picks = [chapters[0]]
        if len(chapters) > 2:
            picks.append(chapters[len(chapters) // 2])
        if len(chapters) > 1:
            picks.append(chapters[-1])
        for ch in picks:
            snippet = str(ch.get("txt") or "")[:1200]
            samples.append(f"Chapter {ch.get('number')}: {ch.get('title')}\n{snippet}")

    system_prompt = (
        "You are a Vietnamese fiction metadata assistant. "
        "Return ONLY valid JSON (no markdown, no explanation) with exactly keys: genres, shortDescription, confidence. "
        "genres must be an array of 1-6 concise Vietnamese labels. "
        "Prefer selecting from existingGenres when semantically close; create new genres only when no close match exists. "
        "Do not output duplicates, slug format, or punctuation-only variants. "
        "shortDescription must be 6-7 Vietnamese sentences, each sentence on a new line using newline characters. "
        "Match tone and diction to the likely genre and make it emotionally engaging to increase reader curiosity. "
        "No major spoilers, no quotes. "
        "confidence must be a number from 0 to 1. "
        "If uncertain, use broader/common genres rather than inventing niche ones."
    )
    user_prompt = {
        "title": title,
        "author": author,
        "chapterSamples": samples,
        "existingGenres": existing_genres,
        "requirements": {
            "maxGenres": 6,
            "allowNewGenres": True,
            "preferExistingGenres": True,
            "language": "vi",
        },
    }

    base_payload = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_prompt, ensure_ascii=False)},
        ],
        "temperature": 0.3,
        "max_tokens": 500,
        "response_format": {"type": "json_object"},
    }

    models = await _router_pick_models()
    if not models:
        return None
    headers = {
        "Content-Type": "application/json",
        "HTTP-Referer": "http://localhost:3000",
        "X-Title": "reader-import-ai-suggest",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    for model_id in models:
        payload = dict(base_payload)
        payload["model"] = model_id
        try:
            async with httpx.AsyncClient(timeout=45.0) as client:
                response = await client.post(
                    f"{str(settings.router_base_url).rstrip('/')}/chat/completions",
                    headers=headers,
                    json=payload,
                )
            response.raise_for_status()
            data = response.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            parsed = json.loads(content) if isinstance(content, str) else {}
            raw_genres = [str(g).strip() for g in (parsed.get("genres") or []) if str(g).strip()][:6]
            genres = _map_genres_to_existing(raw_genres, existing_genres, limit=6)
            short_description = str(parsed.get("shortDescription") or "").strip()
            try:
                confidence = float(parsed.get("confidence") or 0.0)
            except Exception:
                confidence = 0.0
            confidence = max(0.0, min(1.0, confidence))
            if not short_description or not genres:
                continue
            return {"suggestedGenres": genres, "shortDescription": short_description, "confidence": confidence, "model": model_id}
        except Exception:
            continue
    return None


async def _resolve_chapter_content(chapter_id: str, db: AsyncSession) -> str | None:
    ref_row = (
        await db.execute(
            text('SELECT "txtHref" FROM "ChapterContentRef" WHERE "chapterId" = :chapter_id LIMIT 1'),
            {"chapter_id": chapter_id},
        )
    ).mappings().first()

    if ref_row:
        try:
            return storage.read_text(ref_row["txtHref"])
        except Exception:
            return None
    return None


@app.get("/api/import/assets")
async def list_source_assets(
    status: str | None = None,
    unconvertedOnly: bool = Query(default=False),
    q: str | None = None,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db_session),
):
    where_parts: list[str] = []
    params: dict[str, Any] = {"limit": limit}
    if status:
        where_parts.append('s.status = :status')
        params["status"] = status

    if unconvertedOnly:
        where_parts.append(
            '(s.status <> :asset_completed_status AND NOT EXISTS (SELECT 1 FROM "ImportJob" j WHERE j."sourceAssetId" = s.id AND j.status = :completed_status))'
        )
        params["asset_completed_status"] = "completed"
        params["completed_status"] = "completed"

    if q and q.strip():
        raw = q.strip().lower()
        where_parts.append(
            '(unaccent(lower(s.path)) ILIKE unaccent(:q_raw) OR lower(s.path) ILIKE :q_raw)'
        )
        params["q_raw"] = f"%{raw}%"

    where_sql = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
    rows = (
        await db.execute(
            text(
                f'SELECT id, path, sha256, opf_identifier, title, author, status, "createdAt", "updatedAt" '
                f'FROM "SourceAsset" s {where_sql} ORDER BY s."updatedAt" DESC OFFSET :offset LIMIT :limit'
            ),
            {**params, "offset": offset},
        )
    ).mappings().all()
    novels = (await db.execute(text('SELECT id, title FROM "Novel"'))).mappings().all()
    out: list[dict[str, Any]] = []
    normalized_query = _norm_title(q or "")
    query_tokens = [t for t in normalized_query.split(" ") if t]
    for r in rows:
        item = dict(r)
        base = (item.get("path") or "").split("/")[-1].rsplit(".", 1)[0]
        normalized_path = _norm_title(str(item.get("path") or ""))
        if query_tokens and not all(tok in normalized_path for tok in query_tokens):
            continue
        best = {"id": None, "score": 0.0}
        for n in novels:
            sc = _title_score(base, str(n.get("title") or ""))
            if sc > best["score"]:
                best = {"id": n.get("id"), "score": sc}
        item["matchedNovelId"] = best["id"]
        item["matchScore"] = round(best["score"], 4)
        item["converted"] = best["score"] >= 0.9
        if unconvertedOnly and item["converted"]:
            continue
        out.append(item)
    return out


@app.get("/api/import/assets/search")
async def search_source_assets(
    q: str,
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=20, ge=1, le=100),
    status: str | None = None,
    db: AsyncSession = Depends(get_db_session),
):
    query = _normalized_search_name(q)
    if len(query) < 2:
        return {"items": [], "pagination": {"page": page, "limit": limit, "total": 0, "totalPages": 0}}

    where = ['search_name IS NOT NULL']
    params: dict[str, Any] = {
        "q_prefix": f"{query}%",
        "q_like": f"%{query}%",
        "offset": (page - 1) * limit,
        "limit": limit,
    }
    if status:
        where.append('status = :status')
        params["status"] = status

    where_sql = " AND ".join(where)
    total = (
        await db.execute(
            text(f'SELECT COUNT(*)::int FROM "SourceAsset" WHERE {where_sql} AND (search_name LIKE :q_prefix OR search_name ILIKE :q_like)'),
            params,
        )
    ).scalar_one()
    rows = (
        await db.execute(
            text(
                f'SELECT id, path, title, author, status, "updatedAt" '
                f'FROM "SourceAsset" '
                f'WHERE {where_sql} AND (search_name LIKE :q_prefix OR search_name ILIKE :q_like) '
                f'ORDER BY CASE WHEN search_name LIKE :q_prefix THEN 0 ELSE 1 END, "updatedAt" DESC '
                f'OFFSET :offset LIMIT :limit'
            ),
            params,
        )
    ).mappings().all()

    total_pages = max((total + limit - 1) // limit, 1) if total else 0
    return {
        "items": [dict(row) for row in rows],
        "pagination": {"page": page, "limit": limit, "total": int(total), "totalPages": total_pages},
    }


@app.get("/api/import/assets/{asset_id}/preview-metadata")
async def preview_source_asset_metadata(
    asset_id: str,
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_current_user),
):
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")
    row = (
        await db.execute(
            text(
                'SELECT id, path, title, author, status, review_status, review_payload, sha256, "updatedAt" '
                'FROM "SourceAsset" WHERE id = :id LIMIT 1'
            ),
            {"id": asset_id},
        )
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Source asset not found")

    path = str(row["path"])
    base = path.split("/")[-1].rsplit(".", 1)[0]
    source_path = _resolve_epub_source_path(path, str(row.get("sha256") or ""))
    preview = _extract_epub_preview_payload(source_path) if source_path else None
    return {
        "asset": {**dict(row), "coverDetected": bool(preview and preview.get("coverFound"))},
        "suggested": {
            "title": (preview.get("title") if preview else None) or row.get("title") or base,
            "author": (preview.get("author") if preview else None) or row.get("author") or "Unknown",
            "shortDescription": (preview.get("description") if preview else None) or None,
            "genres": (preview.get("genres") if preview else None) or [],
        },
        "debug": {
            "sourcePathResolved": str(source_path) if source_path else None,
            "sourcePathExists": bool(source_path and source_path.exists()),
            "coverFound": bool(preview and preview.get("coverFound")),
            "coverExt": preview.get("coverExt") if preview else None,
            "titleFromEpub": preview.get("title") if preview else None,
            "authorFromEpub": preview.get("author") if preview else None,
        },
    }


@app.post("/api/import/uploads/preview")
async def upload_epub_and_preview(
    file: UploadFile = File(...),
    user: dict = Depends(require_current_user),
):
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty EPUB")

    suffix = ".epub"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(raw)
        tmp_path = Path(tmp.name)

    try:
        preview = _extract_epub_preview_payload(tmp_path)
        return {
            "suggested": {
                "title": preview.get("title"),
                "author": preview.get("author"),
                "shortDescription": preview.get("description") or None,
                "genres": preview.get("genres") or [],
            },
            "coverDetected": bool(preview.get("coverFound")),
            "coverPreviewDataUrl": preview.get("coverPreviewDataUrl"),
        }
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass


@app.post("/api/mod/epub/ai-suggest")
async def mod_epub_ai_suggest(
    file: UploadFile = File(...),
    splitMode: str | None = Form(default=None),
    chapterRegex: str | None = Form(default=None),
    title: str | None = Form(default=None),
    authorName: str | None = Form(default=None),
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_current_user),
):
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty EPUB")

    with tempfile.NamedTemporaryFile(delete=False, suffix=".epub") as tmp:
        tmp.write(raw)
        tmp_path = Path(tmp.name)

    try:
        mode = "regex" if (splitMode or "").lower() == "regex" else "toc"
        pattern = (chapterRegex or "").strip() or None
        chapters = _epub_extract_with_mode(tmp_path, mode, pattern)
        meta = _extract_epub_metadata(tmp_path)
        resolved_title = " ".join((title or str(meta.get("title") or tmp_path.stem)).split()).strip() or tmp_path.stem
        resolved_author = " ".join((authorName or str(meta.get("author") or "Unknown")).split()).strip() or "Unknown"
        existing_genres = [
            str(r.get("name") or "")
            for r in (await db.execute(text('SELECT name FROM "Genre" ORDER BY name ASC'))).mappings().all()
            if str(r.get("name") or "").strip()
        ]

        ai_result = await _router_ai_suggest(resolved_title, resolved_author, chapters, existing_genres)
        if ai_result:
            return {
                "suggestedGenres": ai_result["suggestedGenres"][:6],
                "shortDescription": ai_result["shortDescription"],
                "confidence": ai_result["confidence"],
                "source": "router_dynamic",
                "model": ai_result.get("model"),
            }

        fallback_genres = _map_genres_to_existing(_build_ai_genre_suggestions(chapters), existing_genres, limit=6)
        fallback_desc = _build_ai_description(resolved_title, resolved_author, chapters)
        return {
            "suggestedGenres": fallback_genres[:6],
            "shortDescription": fallback_desc,
            "confidence": 0.62,
            "source": "rule_based_fallback",
        }
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass


@app.get("/api/import/assets/{asset_id}/preview-cover")
async def preview_source_asset_cover(
    asset_id: str,
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_current_user),
):
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")
    row = (
        await db.execute(text('SELECT id, path, sha256 FROM "SourceAsset" WHERE id = :id LIMIT 1'), {"id": asset_id})
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Source asset not found")

    source_path = _resolve_epub_source_path(str(row["path"]), str(row.get("sha256") or ""))
    if not source_path:
        raise HTTPException(status_code=400, detail="EPUB source file not found")
    preview = _extract_epub_preview_payload(source_path)
    cover_bytes = preview.get("coverBytes") if preview else None
    if not cover_bytes:
        raise HTTPException(status_code=404, detail="Cover not found in EPUB")
    ext = str(preview.get("coverExt") or ".jpg")
    media_type = _mime_from_extension(ext)
    return Response(content=cover_bytes, media_type=media_type)


@app.post("/api/import/assets/{asset_id}/upload-cover")
async def upload_source_asset_cover(
    asset_id: str,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_current_user),
):
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")

    row = (
        await db.execute(text('SELECT id, review_payload FROM "SourceAsset" WHERE id = :id LIMIT 1'), {"id": asset_id})
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Source asset not found")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="File cover rong")
    ext = ".jpg"
    ct = (file.content_type or "").lower()
    if "png" in ct:
        ext = ".png"
    elif "webp" in ct:
        ext = ".webp"
    elif "jpeg" in ct or "jpg" in ct:
        ext = ".jpg"

    cover_url = _upload_cover_bytes_to_r2(content, ext, key_prefix=f"manual-cover-{asset_id}")
    if not cover_url:
        raise HTTPException(status_code=500, detail="Upload cover that bai")

    review_payload = row.get("review_payload") or {}
    if isinstance(review_payload, str):
        try:
            review_payload = json.loads(review_payload)
        except Exception:
            review_payload = {}
    review_payload["manualCoverUrl"] = cover_url

    await db.execute(
        text('UPDATE "SourceAsset" SET review_payload = CAST(:review_payload AS jsonb), "updatedAt" = NOW() WHERE id = :id'),
        {"id": asset_id, "review_payload": json.dumps(review_payload)},
    )
    await db.commit()

    return {"assetId": asset_id, "coverUrl": cover_url, "uploaded": True}


@app.post("/api/import/assets/{asset_id}/review")
async def review_source_asset(
    asset_id: str,
    payload: SourceAssetReviewPayload,
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_current_user),
):
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")

    if payload.targetMode == "existing" and not payload.novelId:
        raise HTTPException(status_code=400, detail="novelId is required when targetMode=existing")

    row = (
        await db.execute(
            text(
                'UPDATE "SourceAsset" SET title = COALESCE(:title, title), author = COALESCE(:author, author), '
                'review_status = :review_status, review_payload = CAST(:review_payload AS jsonb), status = :status, "updatedAt" = NOW() '
                'WHERE id = :id RETURNING id, path, title, author, status, review_status, review_payload, "updatedAt"'
            ),
            {
                "id": asset_id,
                "title": payload.title,
                "author": payload.author,
                "review_status": "reviewed",
                "review_payload": json.dumps(payload.model_dump()),
                "status": "approved",
            },
        )
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Source asset not found")
    await db.commit()
    return dict(row)


@app.post("/api/import/assets/{asset_id}/ai-suggest")
async def ai_suggest_source_asset(
    asset_id: str,
    payload: SourceAssetAiSuggestPayload,
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_current_user),
):
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")

    row = (
        await db.execute(text('SELECT id, path, title, author FROM "SourceAsset" WHERE id = :id LIMIT 1'), {"id": asset_id})
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Source asset not found")

    source_path = _resolve_epub_source_path(str(row["path"]))
    if not source_path or not source_path.exists():
        raise HTTPException(status_code=400, detail="EPUB source file not found")

    chapters = _epub_extract_with_mode(source_path, payload.splitMode, payload.chapterStartPattern)
    title = str(row.get("title") or source_path.stem)
    author = str(row.get("author") or "Unknown")
    existing_genres = [
        str(r.get("name") or "")
        for r in (await db.execute(text('SELECT name FROM "Genre" ORDER BY name ASC'))).mappings().all()
        if str(r.get("name") or "").strip()
    ]

    ai_result = await _router_ai_suggest(title, author, chapters, existing_genres)
    if ai_result:
        return {
            "assetId": asset_id,
            "suggestedGenres": ai_result["suggestedGenres"][:6],
            "shortDescription": ai_result["shortDescription"],
            "confidence": ai_result["confidence"],
            "source": "router_dynamic",
            "model": ai_result.get("model"),
            "existingGenresCount": len(existing_genres),
        }

    genres = _build_ai_genre_suggestions(chapters)
    genres = _map_genres_to_existing(genres, existing_genres, limit=6)
    description = _build_ai_description(title, author, chapters)
    return {
        "assetId": asset_id,
        "suggestedGenres": genres[:6],
        "shortDescription": description,
        "confidence": 0.62,
        "source": "rule_based_fallback",
        "existingGenresCount": len(existing_genres),
    }


@app.post("/api/import/assets/{asset_id}/parse-preview")
async def parse_preview_source_asset(
    asset_id: str,
    payload: SourceAssetParsePreviewPayload,
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_current_user),
):
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")
    row = (
        await db.execute(text('SELECT id, path FROM "SourceAsset" WHERE id = :id LIMIT 1'), {"id": asset_id})
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Source asset not found")
    source_path = _resolve_epub_source_path(str(row["path"]))
    if not source_path or not source_path.exists():
        raise HTTPException(status_code=400, detail="EPUB source file not found")

    chapters = _epub_extract_with_mode(source_path, payload.splitMode, payload.chapterStartPattern)
    return {
        "assetId": asset_id,
        "splitMode": payload.splitMode,
        "chapterCount": len(chapters),
        "sample": _chapter_preview_samples(chapters, sample_size=10),
        "warnings": [] if len(chapters) >= 3 else ["chapter_count_too_low"],
    }


def _run_import_session_task(session_id: str) -> None:
    from app.database import SessionLocal

    async def _run() -> None:
        db = SessionLocal()
        try:
            row = (
                await db.execute(
                    text(
                        'SELECT s.id, s."sourceAssetId", s."novelId", s.status, a.path, a.review_payload, a.title, a.author '
                        'FROM "ImportSession" s JOIN "SourceAsset" a ON a.id = s."sourceAssetId" WHERE s.id = :id LIMIT 1'
                    ),
                    {"id": session_id},
                )
            ).mappings().first()
            if not row:
                return

            await db.execute(
                text('UPDATE "ImportSession" SET status = :st, phase = :ph, "progressPct" = :pct, "updatedAt" = NOW() WHERE id = :id'),
                {"id": session_id, "st": "processing", "ph": "prepare", "pct": 5.0},
            )
            await db.commit()

            source_path = _resolve_epub_source_path(str(row["path"]))
            if not source_path or not source_path.exists():
                await db.execute(
                    text('UPDATE "ImportSession" SET status = :st, phase = :ph, log = :log, "updatedAt" = NOW() WHERE id = :id'),
                    {"id": session_id, "st": "failed", "ph": "prepare", "log": "EPUB source file not found"},
                )
                await db.commit()
                return

            review_payload = row.get("review_payload") or {}
            if isinstance(review_payload, str):
                try:
                    review_payload = json.loads(review_payload)
                except Exception:
                    review_payload = {}

            cover_url: str | None = str(review_payload.get("manualCoverUrl") or "").strip() or None
            if not cover_url:
                cover_extracted = _extract_epub_cover(source_path)
                if cover_extracted:
                    cover_bytes, cover_ext = cover_extracted
                    cover_url = _upload_cover_to_r2(cover_bytes, cover_ext, source_asset_id=str(row["sourceAssetId"]))

            split_mode = str(review_payload.get("splitMode") or "toc")
            chapter_start_pattern = review_payload.get("chapterStartPattern")
            target_mode = str(review_payload.get("targetMode") or "new")
            replace_existing = bool(review_payload.get("replaceExisting") or False)

            await db.execute(
                text('UPDATE "ImportSession" SET phase = :ph, "progressPct" = :pct, "updatedAt" = NOW() WHERE id = :id'),
                {"id": session_id, "ph": "parse", "pct": 20.0},
            )
            await db.commit()
            await db.execute(
                text('UPDATE "ImportSession" SET log = :log, "updatedAt" = NOW() WHERE id = :id'),
                {"id": session_id, "log": "parsing epub"},
            )
            await db.commit()
            chapters = await asyncio.wait_for(
                asyncio.to_thread(_epub_extract_with_mode, source_path, split_mode, chapter_start_pattern),
                timeout=180,
            )

            novel_id = row.get("novelId")
            if not novel_id and target_mode == "existing":
                novel_id = review_payload.get("novelId")
            if novel_id:
                novel_exists = (await db.execute(text('SELECT id FROM "Novel" WHERE id = :id LIMIT 1'), {"id": novel_id})).mappings().first()
                if not novel_exists:
                    raise RuntimeError("Target novel not found")
            if not novel_id:
                base_title = str(review_payload.get("title") or row.get("title") or source_path.stem)
                slug = _norm_title(base_title).replace(" ", "-")[:120] or _new_id("n_")
                existing_slug_row = (
                    await db.execute(text('SELECT id FROM "Novel" WHERE slug = :slug LIMIT 1'), {"slug": slug})
                ).mappings().first()
                if existing_slug_row:
                    slug = f"{slug}-{_new_id()[:8]}"
                novel_id = _new_id("n_")
                await db.execute(
                    text('INSERT INTO "Novel" (id, title, slug, "authorName", description, "coverUrl", status, "totalChapters", views, rating, "ratingCount", "bookmarkCount", "createdAt", "updatedAt") VALUES (:id,:title,:slug,:author,:desc,:cover_url,:status,0,0,0,0,0,NOW(),NOW())'),
                    {
                        "id": novel_id,
                        "title": base_title,
                        "slug": slug,
                        "author": str(review_payload.get("author") or row.get("author") or "Unknown"),
                        "desc": str(review_payload.get("shortDescription") or ""),
                        "cover_url": cover_url,
                        "status": "Đang ra",
                    },
                )
            elif cover_url:
                await db.execute(
                    text('UPDATE "Novel" SET "coverUrl" = COALESCE("coverUrl", :cover_url), "updatedAt" = NOW() WHERE id = :id'),
                    {"id": novel_id, "cover_url": cover_url},
                )

            genres = [str(g) for g in (review_payload.get("genres") or [])]
            if genres:
                genre_ids = await _ensure_genre_ids(db, genres)
                for gid in genre_ids:
                    await db.execute(
                        text('INSERT INTO "NovelGenre" ("novelId", "genreId") VALUES (:novel_id, :genre_id) ON CONFLICT DO NOTHING'),
                        {"novel_id": novel_id, "genre_id": gid},
                    )

            await db.execute(
                text('UPDATE "ImportSession" SET phase = :ph, "progressPct" = :pct, "novelId" = :novel_id, "updatedAt" = NOW() WHERE id = :id'),
                {"id": session_id, "ph": "write_nas", "pct": 50.0, "novel_id": novel_id},
            )
            await db.commit()

            added = 0
            replaced = 0
            skipped = 0
            failed = 0
            last_error: str | None = None
            asset_id = str(row["sourceAssetId"])
            total_chapters = max(1, len(chapters))
            await db.execute(
                text('UPDATE "ImportSession" SET phase = :ph, log = :log, "updatedAt" = NOW() WHERE id = :id'),
                {"id": session_id, "ph": "write_nas", "log": f"writing chapters 0/{total_chapters}"},
            )
            await db.commit()

            async def _write_storage_text(href: str, content: str) -> None:
                await asyncio.wait_for(asyncio.to_thread(storage.write_text, href, content), timeout=20)

            for idx, ch in enumerate(chapters, start=1):
                processed_this = False
                try:
                    async with db.begin_nested():
                        num = int(ch.get("number") or 0)
                        if num <= 0:
                            failed += 1
                            continue
                        is_placeholder = bool(ch.get("is_placeholder") or False)
                        existing = (
                            await db.execute(
                                text('SELECT id FROM "ChapterMeta" WHERE "novelId" = :novel_id AND number = :num LIMIT 1'),
                                {"novel_id": novel_id, "num": num},
                            )
                        ).mappings().first()

                        if existing:
                            if replace_existing:
                                await db.execute(text('UPDATE "ChapterMeta" SET title = :title WHERE id = :id'), {"id": existing["id"], "title": str(ch.get("title") or f"Chapter {num}")})
                                if not is_placeholder:
                                    txt_href = f"{asset_id}/{num}.txt"
                                    raw_href = f"{asset_id}/{num}.raw.html"
                                    txt = str(ch.get("txt") or "")
                                    await _write_storage_text(txt_href, txt)
                                    await _write_storage_text(raw_href, str(ch.get("raw_html") or ""))
                                    h = hashlib.sha256(txt.encode("utf-8")).hexdigest()
                                    await db.execute(text('INSERT INTO "ChapterContentRef" ("chapterId","txtHref","rawHtmlHref","contentHash") VALUES (:id,:txt,:raw,:hash) ON CONFLICT ("chapterId") DO UPDATE SET "txtHref"=EXCLUDED."txtHref", "rawHtmlHref"=EXCLUDED."rawHtmlHref", "contentHash"=EXCLUDED."contentHash", "updatedAt"=NOW()'), {"id": existing["id"], "txt": txt_href, "raw": raw_href, "hash": h})
                                else:
                                    await db.execute(text('DELETE FROM "ChapterContentRef" WHERE "chapterId" = :id'), {"id": existing["id"]})
                                replaced += 1
                                processed_this = True
                            else:
                                skipped += 1
                                processed_this = True
                            continue

                        cid = _new_id("cmeta_")
                        await db.execute(text('INSERT INTO "ChapterMeta" (id, "novelId", number, title, views, "createdAt") VALUES (:id,:novel,:num,:title,0,NOW())'), {"id": cid, "novel": novel_id, "num": num, "title": str(ch.get("title") or f"Chapter {num}")})
                        if not is_placeholder:
                            txt_href = f"{asset_id}/{num}.txt"
                            raw_href = f"{asset_id}/{num}.raw.html"
                            txt = str(ch.get("txt") or "")
                            await _write_storage_text(txt_href, txt)
                            await _write_storage_text(raw_href, str(ch.get("raw_html") or ""))
                            h = hashlib.sha256(txt.encode("utf-8")).hexdigest()
                            await db.execute(text('INSERT INTO "ChapterContentRef" ("chapterId","txtHref","rawHtmlHref","contentHash") VALUES (:id,:txt,:raw,:hash)'), {"id": cid, "txt": txt_href, "raw": raw_href, "hash": h})
                        added += 1
                        processed_this = True
                except Exception as exc:
                    failed += 1
                    processed_this = True
                    if last_error is None:
                        last_error = str(exc)

                if not processed_this:
                    skipped += 1
                    processed_this = True

                if idx % 10 == 0 or idx == total_chapters:
                    progress = 50.0 + (float(idx) / float(total_chapters)) * 45.0
                    processed = added + replaced + skipped + failed
                    await db.execute(
                        text('UPDATE "ImportSession" SET "progressPct" = :pct, log = :log, "updatedAt" = NOW() WHERE id = :id'),
                        {"id": session_id, "pct": min(progress, 95.0), "log": f"writing chapters {processed}/{total_chapters}"},
                    )
                    await db.commit()

            await db.execute(text('UPDATE "Novel" SET description = COALESCE(:desc, description), "totalChapters" = (SELECT COUNT(*) FROM "ChapterMeta" WHERE "novelId" = :novel_id), "updatedAt" = NOW() WHERE id = :novel_id'), {"novel_id": novel_id, "desc": review_payload.get("shortDescription")})
            await db.execute(
                text('UPDATE "ImportSession" SET status = :st, phase = :ph, "progressPct" = :pct, "resultJson" = CAST(:result AS jsonb), "updatedAt" = NOW() WHERE id = :id'),
                {
                    "id": session_id,
                    "st": "completed",
                    "ph": "finalize",
                    "pct": 100.0,
                    "result": json.dumps({"parsed": len(chapters), "added": added, "replaced": replaced, "skipped": skipped, "failed": failed, "novelId": novel_id, "lastError": last_error}),
                },
            )
            await db.execute(
                text('UPDATE "SourceAsset" SET status = :status, review_status = :review_status, "updatedAt" = NOW() WHERE id = :id'),
                {"id": row["sourceAssetId"], "status": "completed" if failed == 0 else "review_required", "review_status": "imported" if failed == 0 else "reviewed"},
            )
            await db.commit()
        except Exception as exc:
            try:
                await db.rollback()
                await db.execute(
                    text('UPDATE "ImportSession" SET status = :st, log = :log, "updatedAt" = NOW() WHERE id = :id'),
                    {"id": session_id, "st": "failed", "log": str(exc)},
                )
                await db.commit()
            except Exception:
                pass
        finally:
            await db.close()

    return _run()


@app.post("/api/import/assets/{asset_id}/start-import")
async def start_import_source_asset(
    asset_id: str,
    payload: SourceAssetStartImportPayload,
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_current_user),
):
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")
    asset = (
        await db.execute(text('SELECT id, status, review_payload FROM "SourceAsset" WHERE id = :id LIMIT 1'), {"id": asset_id})
    ).mappings().first()
    if not asset:
        raise HTTPException(status_code=404, detail="Source asset not found")

    review_payload = asset.get("review_payload") or {}
    if isinstance(review_payload, str):
        try:
            review_payload = json.loads(review_payload)
        except Exception:
            review_payload = {}
    review_payload["replaceExisting"] = bool(payload.replaceExisting)
    review_payload["splitMode"] = payload.splitMode
    review_payload["chapterStartPattern"] = payload.chapterStartPattern

    await db.execute(
        text('UPDATE "SourceAsset" SET review_payload = CAST(:review_payload AS jsonb), "updatedAt" = NOW() WHERE id = :id'),
        {"id": asset_id, "review_payload": json.dumps(review_payload)},
    )

    session_id = _new_id("is_")
    await db.execute(
        text(
            'INSERT INTO "ImportSession" (id, "sourceAssetId", "novelId", status, phase, "progressPct", log, "resultJson", "createdBy") '
            'VALUES (:id, :asset, :novel, :status, :phase, :pct, :log, :result, :created_by)'
        ),
        {
            "id": session_id,
            "asset": asset_id,
            "novel": payload.forceNovelId,
            "status": "pending",
            "phase": "prepare",
            "pct": 0.0,
            "log": None,
            "result": None,
            "created_by": str(user.get("id") or ""),
        },
    )
    await db.commit()

    task = asyncio.create_task(_run_import_session_task(session_id), name=f"import-session-{session_id}")
    _IMPORT_TASKS.add(task)
    task.add_done_callback(lambda t: _IMPORT_TASKS.discard(t))
    return {"sessionId": session_id, "status": "pending", "phase": "prepare", "progressPct": 0}


@app.get("/api/import/sessions/{session_id}")
async def get_import_session(
    session_id: str,
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_current_user),
):
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")

    row = (
        await db.execute(
            text(
                'SELECT id, "sourceAssetId", "novelId", status, phase, "progressPct", log, "resultJson", "createdAt", "updatedAt" '
                'FROM "ImportSession" WHERE id = :id LIMIT 1'
            ),
            {"id": session_id},
        )
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Import session not found")
    out = dict(row)
    result = out.get("resultJson")
    if isinstance(result, str):
        try:
            out["resultJson"] = json.loads(result)
        except Exception:
            out["resultJson"] = None
    return out


class ConvertAssetPayload(BaseModel):
    assetId: str


@app.post("/api/import/convert")
async def convert_asset(
    payload: ConvertAssetPayload,
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_current_user),
):
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")
    asset = (
        await db.execute(text('SELECT id, path FROM "SourceAsset" WHERE id = :id LIMIT 1'), {"id": payload.assetId})
    ).mappings().first()
    if not asset:
        raise HTTPException(status_code=404, detail="Source asset not found")

    base = str(asset.get("path") or "").split("/")[-1].rsplit(".", 1)[0]
    novels = (await db.execute(text('SELECT id, title FROM "Novel"'))).mappings().all()
    best = {"id": None, "score": 0.0}
    for n in novels:
        sc = _title_score(base, str(n.get("title") or ""))
        if sc > best["score"]:
            best = {"id": n.get("id"), "score": sc}
    novel_id = best["id"]
    if not novel_id:
        novel_id = _new_id("n_")
        slug = _norm_title(base).replace(" ", "-")[:120] or novel_id
        await db.execute(
            text('INSERT INTO "Novel" (id, title, slug, "authorName", description, status, "totalChapters", views, rating, "ratingCount", "bookmarkCount", "createdAt", "updatedAt") VALUES (:id,:title,:slug,:author,:desc,:status,0,0,0,0,0,NOW(),NOW())'),
            {"id": novel_id, "title": base, "slug": slug, "author": "Unknown", "desc": "", "status": "Đang ra"},
        )

    source_path = Path(settings.epub_source_root) / str(asset["path"])
    if not source_path.exists():
        raise HTTPException(status_code=400, detail="EPUB source file not found")
    chapters = _extract_epub_chapters(source_path)
    added = 0
    for ch in chapters:
        num = int(ch.get("number") or 0)
        if num <= 0:
            continue
        existing = (await db.execute(text('SELECT id FROM "ChapterMeta" WHERE "novelId" = :novel_id AND number = :num LIMIT 1'), {"novel_id": novel_id, "num": num})).mappings().first()
        if existing:
            continue
        cid = _new_id("c_")
        txt_href = f"{payload.assetId}/{num}.txt"
        raw_href = f"{payload.assetId}/{num}.raw.html"
        txt = str(ch.get("txt") or "")
        storage.write_text(txt_href, txt)
        storage.write_text(raw_href, str(ch.get("raw_html") or ""))
        h = hashlib.sha256(txt.encode("utf-8")).hexdigest()
        await db.execute(text('INSERT INTO "ChapterMeta" (id, "novelId", number, title, views, "createdAt") VALUES (:id,:novel,:num,:title,0,NOW())'), {"id": cid, "novel": novel_id, "num": num, "title": str(ch.get("title") or f"Chapter {num}")})
        await db.execute(text('INSERT INTO "ChapterContentRef" ("chapterId","txtHref","rawHtmlHref","contentHash") VALUES (:id,:txt,:raw,:hash)'), {"id": cid, "txt": txt_href, "raw": raw_href, "hash": h})
        added += 1
    await db.execute(text('UPDATE "Novel" SET "totalChapters" = (SELECT COUNT(*) FROM "ChapterMeta" WHERE "novelId" = :novel_id), "updatedAt" = NOW() WHERE id = :novel_id'), {"novel_id": novel_id})
    await db.commit()
    return {"assetId": payload.assetId, "novelId": novel_id, "added": added, "done": True}


@app.post("/api/import/assets/auto-review")
async def auto_review_assets(
    limit: int = Query(default=1000, ge=1, le=10000),
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_current_user),
):
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")

    def normalize(v: str) -> str:
        v = (v or "").strip().lower()
        frm = "áàảãạăắằẳẵặâấầẩẫậéèẻẽẹêếềểễệíìỉĩịóòỏõọôốồổỗộơớờởỡợúùủũụưứừửữựýỳỷỹỵđ"
        to = "aaaaaaaaaaaaaaaaaeeeeeeeeeeeiiiiiooooooooooooooooouuuuuuuuuuuyyyyyd"
        v = v.translate(str.maketrans(frm, to))
        return "".join(ch for ch in v if ch.isalnum() or ch.isspace()).strip()

    novel_rows = (
        await db.execute(text('SELECT id, title, slug FROM "Novel"'))
    ).mappings().all()
    known = {normalize(str(r.get("title") or "")) for r in novel_rows}
    known.update({normalize(str(r.get("slug") or "")) for r in novel_rows})

    assets = (
        await db.execute(
            text('SELECT id, path, status FROM "SourceAsset" WHERE status IN (:d,:a,:r) ORDER BY "updatedAt" DESC LIMIT :limit'),
            {"d": "discovered", "a": "approved", "r": "review_required", "limit": limit},
        )
    ).mappings().all()

    approved = 0
    review_required = 0
    for a in assets:
        path = str(a.get("path") or "")
        base = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        folder = path.split("/", 1)[0] if "/" in path else base
        key = normalize(base)
        alt = normalize(folder)
        status = "approved" if (key in known or alt in known) else "review_required"
        await db.execute(
            text('UPDATE "SourceAsset" SET status = :status, "updatedAt" = NOW() WHERE id = :id'),
            {"id": a["id"], "status": status},
        )
        if status == "approved":
            approved += 1
        else:
            review_required += 1

    await db.commit()
    return {"processed": len(assets), "approved": approved, "reviewRequired": review_required}


@app.post("/api/import/assets/upsert")
async def upsert_source_asset(
    payload: SourceAssetUpsertPayload,
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_current_user),
):
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")

    existing = (
        await db.execute(text('SELECT id, path, sha256 FROM "SourceAsset" WHERE sha256 = :sha256 LIMIT 1'), {"sha256": payload.sha256})
    ).mappings().first()

    if existing:
        row = (
            await db.execute(
                text(
                    'UPDATE "SourceAsset" SET path = :path, opf_identifier = :opf, title = :title, author = :author, '
                    '"updatedAt" = NOW() WHERE id = :id '
                    'RETURNING id, path, sha256, status, "updatedAt"'
                ),
                {
                    "id": existing["id"],
                    "path": payload.path,
                    "opf": payload.opfIdentifier,
                    "title": payload.title,
                    "author": payload.author,
                },
            )
        ).mappings().first()
    else:
        new_id = _new_id("asset_")
        row = (
            await db.execute(
                text(
                    'INSERT INTO "SourceAsset" (id, path, sha256, opf_identifier, title, author, status) '
                    'VALUES (:id, :path, :sha256, :opf, :title, :author, :status) '
                    'RETURNING id, path, sha256, status, "updatedAt"'
                ),
                {
                    "id": new_id,
                    "path": payload.path,
                    "sha256": payload.sha256,
                    "opf": payload.opfIdentifier,
                    "title": payload.title,
                    "author": payload.author,
                    "status": "discovered",
                },
            )
        ).mappings().first()

    await db.commit()
    return dict(row) if row else {}


@app.post("/api/import/assets/{asset_id}/approve")
async def approve_source_asset(
    asset_id: str,
    payload: SourceAssetApprovePayload,
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_current_user),
):
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")
    row = (
        await db.execute(
            text(
                'UPDATE "SourceAsset" SET status = :status, "updatedAt" = NOW() '
                'WHERE id = :id RETURNING id, status, "updatedAt"'
            ),
            {"id": asset_id, "status": payload.status},
        )
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Source asset not found")
    await db.commit()
    return dict(row)


@app.post("/api/import/assets/{asset_id}/mark-converted")
async def mark_source_asset_converted(
    asset_id: str,
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_current_user),
):
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")

    row = (
        await db.execute(
            text('UPDATE "SourceAsset" SET status = :status, "updatedAt" = NOW() WHERE id = :id RETURNING id, status'),
            {"id": asset_id, "status": "completed"},
        )
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Source asset not found")

    await db.execute(
        text(
            'INSERT INTO "ImportJob" (id, "sourceAssetId", status, error) '
            'VALUES (:id, :asset_id, :status, :error)'
        ),
        {
            "id": _new_id("job_"),
            "asset_id": asset_id,
            "status": "completed",
            "error": "marked_converted_manually",
        },
    )
    await db.commit()
    return {"id": row["id"], "status": row["status"], "marked": True}


@app.post("/api/import/assets/{asset_id}/unmark-converted")
async def unmark_source_asset_converted(
    asset_id: str,
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_current_user),
):
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")

    row = (
        await db.execute(
            text('UPDATE "SourceAsset" SET status = :status, "updatedAt" = NOW() WHERE id = :id RETURNING id, status'),
            {"id": asset_id, "status": "discovered"},
        )
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Source asset not found")

    await db.execute(
        text('DELETE FROM "ImportJob" WHERE "sourceAssetId" = :asset_id AND status = :status'),
        {"asset_id": asset_id, "status": "completed"},
    )
    await db.commit()
    return {"id": row["id"], "status": row["status"], "unmarked": True}


@app.post("/api/import/jobs")
async def create_import_job(
    payload: ImportJobCreatePayload,
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_current_user),
):
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")

    source_row = (
        await db.execute(
            text('SELECT id, status FROM "SourceAsset" WHERE id = :id LIMIT 1'),
            {"id": payload.sourceAssetId},
        )
    ).mappings().first()
    if not source_row:
        raise HTTPException(status_code=404, detail="Source asset not found")
    if source_row["status"] != "approved":
        raise HTTPException(status_code=400, detail="Source asset must be approved")

    job_id = _new_id("job_")
    await db.execute(
        text('INSERT INTO "ImportJob" (id, "sourceAssetId", status) VALUES (:id, :asset_id, :status)'),
        {"id": job_id, "asset_id": payload.sourceAssetId, "status": "pending"},
    )
    await db.commit()
    return {"id": job_id, "sourceAssetId": payload.sourceAssetId, "status": "pending"}


@app.get("/api/import/jobs/{job_id}")
async def get_import_job(job_id: str, db: AsyncSession = Depends(get_db_session)):
    row = (
        await db.execute(
            text(
                'SELECT j.id, j."sourceAssetId", j.status, j.error, j."createdAt", j."updatedAt" '
                'FROM "ImportJob" j WHERE j.id = :id LIMIT 1'
            ),
            {"id": job_id},
        )
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Import job not found")
    return dict(row)


@app.post("/api/import/jobs/{job_id}/run")
async def run_import_job(
    job_id: str,
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_current_user),
):
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")

    job = (
        await db.execute(
            text(
                'SELECT j.id, j."sourceAssetId", s.path, s.status AS source_status '
                'FROM "ImportJob" j JOIN "SourceAsset" s ON s.id = j."sourceAssetId" '
                'WHERE j.id = :id LIMIT 1'
            ),
            {"id": job_id},
        )
    ).mappings().first()
    if not job:
        raise HTTPException(status_code=404, detail="Import job not found")

    source_path = Path(settings.epub_source_root) / str(job["path"])
    if not source_path.exists():
        await db.execute(
            text('UPDATE "ImportJob" SET status = :status, error = :err, "updatedAt" = NOW() WHERE id = :id'),
            {"id": job_id, "status": "failed", "err": "EPUB file not found"},
        )
        await db.commit()
        raise HTTPException(status_code=400, detail="EPUB source file not found")

    await db.execute(
        text('UPDATE "ImportJob" SET status = :status, error = NULL, "updatedAt" = NOW() WHERE id = :id'),
        {"id": job_id, "status": "processing"},
    )
    await db.commit()

    try:
        chapters = _extract_epub_chapters(source_path)
        if not chapters:
            raise RuntimeError("No readable chapters extracted from EPUB")

        for chapter in chapters:
            base = f"{job['sourceAssetId']}/{chapter['number']}"
            txt_write = storage.write_text(f"{base}.txt", chapter["txt"])
            storage.write_text(f"{base}.raw.html", chapter["raw_html"])
            # missing mapping to canonical chapter ids: keep in review_required queue

        await db.execute(
            text('UPDATE "ImportJob" SET status = :status, error = :err, "updatedAt" = NOW() WHERE id = :id'),
            {
                "id": job_id,
                "status": "review_required",
                "err": f"missing_mapping:{len(chapters)}_chapters_ready",
            },
        )
        await db.commit()
        return {"id": job_id, "status": "review_required", "chaptersExtracted": len(chapters)}
    except Exception as exc:
        await db.execute(
            text('UPDATE "ImportJob" SET status = :status, error = :err, "updatedAt" = NOW() WHERE id = :id'),
            {"id": job_id, "status": "failed", "err": str(exc)},
        )
        await db.commit()
        raise HTTPException(status_code=500, detail="Import job failed") from exc


@app.post("/api/import/jobs/{job_id}/preview")
async def preview_import_job(
    job_id: str,
    payload: ImportApplyPayload,
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_current_user),
):
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")

    job = (await db.execute(text('SELECT id, "sourceAssetId" FROM "ImportJob" WHERE id = :id LIMIT 1'), {"id": job_id})).mappings().first()
    if not job:
        raise HTTPException(status_code=404, detail="Import job not found")
    asset_id = str(job["sourceAssetId"])
    asset_dir = Path(settings.nas_content_root) / asset_id
    if not asset_dir.exists():
        raise HTTPException(status_code=400, detail="Converted content folder not found")

    await db.execute(text('DELETE FROM "ImportCandidateChapter" WHERE "jobId" = :job_id'), {"job_id": job_id})
    created = 0
    for txt_file in sorted(asset_dir.glob("*.txt")):
        token = txt_file.stem
        if not token.isdigit():
            continue
        num = int(token)
        title = txt_file.with_suffix(".raw.html").name
        chapter = (await db.execute(text('SELECT id FROM "ChapterMeta" WHERE "novelId" = :novel_id AND number = :num LIMIT 1'), {"novel_id": payload.novelId, "num": num})).mappings().first()
        action = "add" if not chapter else "replace"
        txt = storage.read_text(f"{asset_id}/{num}.txt")
        h = hashlib.sha256(txt.encode("utf-8")).hexdigest()
        await db.execute(
            text(
                'INSERT INTO "ImportCandidateChapter" (id, "jobId", "candidateNumber", "candidateTitle", "candidateHash", "matchedChapterId", action, reason) '
                'VALUES (:id,:job,:num,:title,:hash,:matched,:action,:reason)'
            ),
            {
                "id": _new_id("ic_"),
                "job": job_id,
                "num": num,
                "title": title,
                "hash": h,
                "matched": chapter["id"] if chapter else None,
                "action": action,
                "reason": "number_match" if chapter else "missing_in_novel",
            },
        )
        created += 1
    await db.commit()
    return {"jobId": job_id, "novelId": payload.novelId, "candidates": created}


@app.post("/api/import/jobs/{job_id}/apply")
async def apply_import_job(
    job_id: str,
    payload: ImportApplyPayload,
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_current_user),
):
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")
    job = (await db.execute(text('SELECT id, "sourceAssetId" FROM "ImportJob" WHERE id = :id LIMIT 1'), {"id": job_id})).mappings().first()
    if not job:
        raise HTTPException(status_code=404, detail="Import job not found")
    asset_id = str(job["sourceAssetId"])

    rows = (await db.execute(text('SELECT id, "candidateNumber", "matchedChapterId", action FROM "ImportCandidateChapter" WHERE "jobId" = :job ORDER BY "candidateNumber"'), {"job": job_id})).mappings().all()
    added = 0
    replaced = 0
    for r in rows:
        num = int(r["candidateNumber"])
        do_replace = False
        if payload.replaceMode == "selected" and num in payload.selectedChapterNumbers:
            do_replace = True
        if payload.replaceMode == "range" and payload.rangeStart and payload.rangeEnd and payload.rangeStart <= num <= payload.rangeEnd:
            do_replace = True
        txt_href = f"{asset_id}/{num}.txt"
        raw_href = f"{asset_id}/{num}.raw.html"
        txt = storage.read_text(txt_href)
        h = hashlib.sha256(txt.encode("utf-8")).hexdigest()
        if r["matchedChapterId"] and do_replace:
            await db.execute(text('UPDATE "ChapterMeta" SET title = :title WHERE id = :id'), {"id": r["matchedChapterId"], "title": f"Chapter {num}"})
            await db.execute(text('INSERT INTO "ChapterContentRef" ("chapterId", "txtHref", "rawHtmlHref", "contentHash") VALUES (:id,:txt,:raw,:hash) ON CONFLICT ("chapterId") DO UPDATE SET "txtHref"=EXCLUDED."txtHref", "rawHtmlHref"=EXCLUDED."rawHtmlHref", "contentHash"=EXCLUDED."contentHash", "updatedAt"=NOW()'), {"id": r["matchedChapterId"], "txt": txt_href, "raw": raw_href, "hash": h})
            replaced += 1
        elif not r["matchedChapterId"]:
            cid = _new_id("cmeta_")
            await db.execute(text('INSERT INTO "ChapterMeta" (id, "novelId", number, title, views, "createdAt") VALUES (:id,:novel,:num,:title,0,NOW())'), {"id": cid, "novel": payload.novelId, "num": num, "title": f"Chapter {num}"})
            await db.execute(text('INSERT INTO "ChapterContentRef" ("chapterId", "txtHref", "rawHtmlHref", "contentHash") VALUES (:id,:txt,:raw,:hash)'), {"id": cid, "txt": txt_href, "raw": raw_href, "hash": h})
            added += 1
    await db.commit()
    return {"jobId": job_id, "added": added, "replaced": replaced}


@app.post("/api/import/jobs/{job_id}/apply-mapping")
async def apply_import_job_mapping(
    job_id: str,
    payload: ImportJobApplyMappingPayload,
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_current_user),
):
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")

    job = (
        await db.execute(
            text('SELECT id, "sourceAssetId" FROM "ImportJob" WHERE id = :id LIMIT 1'),
            {"id": job_id},
        )
    ).mappings().first()
    if not job:
        raise HTTPException(status_code=404, detail="Import job not found")

    asset_id = str(job["sourceAssetId"])
    asset_dir = Path(settings.nas_content_root) / asset_id
    if not asset_dir.exists():
        raise HTTPException(status_code=400, detail="Converted content folder not found")

    txt_files = sorted(asset_dir.glob("*.txt"))
    mapped = 0
    missing = 0

    for txt_file in txt_files:
        chapter_token = txt_file.stem
        if not chapter_token.isdigit():
            continue
        chapter_number = int(chapter_token)
        chapter = (
            await db.execute(
                text('SELECT id FROM "ChapterMeta" WHERE "novelId" = :novel_id AND number = :number LIMIT 1'),
                {"novel_id": payload.novelId, "number": chapter_number},
            )
        ).mappings().first()
        if not chapter:
            missing += 1
            continue

        chapter_id = str(chapter.get("id"))
        txt_href = f"{asset_id}/{chapter_number}.txt"
        raw_href = f"{asset_id}/{chapter_number}.raw.html"
        content_hash = hashlib.sha256(storage.read_text(txt_href).encode("utf-8")).hexdigest()

        if payload.overwrite:
            await db.execute(
                text(
                    'INSERT INTO "ChapterContentRef" ("chapterId", "txtHref", "rawHtmlHref", "contentHash") '
                    'VALUES (:chapter_id, :txt_href, :raw_href, :hash) '
                    'ON CONFLICT ("chapterId") DO UPDATE '
                    'SET "txtHref" = EXCLUDED."txtHref", "rawHtmlHref" = EXCLUDED."rawHtmlHref", '
                    '"contentHash" = EXCLUDED."contentHash", "updatedAt" = NOW()'
                ),
                {
                    "chapter_id": chapter_id,
                    "txt_href": txt_href,
                    "raw_href": raw_href,
                    "hash": content_hash,
                },
            )
        else:
            await db.execute(
                text(
                    'INSERT INTO "ChapterContentRef" ("chapterId", "txtHref", "rawHtmlHref", "contentHash") '
                    'VALUES (:chapter_id, :txt_href, :raw_href, :hash) '
                    'ON CONFLICT ("chapterId") DO NOTHING'
                ),
                {
                    "chapter_id": chapter_id,
                    "txt_href": txt_href,
                    "raw_href": raw_href,
                    "hash": content_hash,
                },
            )
        mapped += 1

    status = "completed" if missing == 0 else "review_required"
    await db.execute(
        text(
            'INSERT INTO "AssetNovelMapping" (id, "sourceAssetId", "novelId", status, note) '
            'VALUES (:id, :asset_id, :novel_id, :status, :note)'
        ),
        {
            "id": _new_id("map_"),
            "asset_id": asset_id,
            "novel_id": payload.novelId,
            "status": status,
            "note": f"mapped={mapped},missing={missing}",
        },
    )
    await db.execute(
        text('UPDATE "ImportJob" SET status = :status, error = :err, "updatedAt" = NOW() WHERE id = :id'),
        {
            "id": job_id,
            "status": status,
            "err": None if missing == 0 else f"missing_mapping:{missing}",
        },
    )
    await db.commit()

    return {"jobId": job_id, "sourceAssetId": asset_id, "mapped": mapped, "missing": missing, "status": status}


@app.get("/api/import/review-required")
async def list_review_required_jobs(
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_current_user),
):
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")

    rows = (
        await db.execute(
            text(
                'SELECT j.id, j."sourceAssetId", j.status, j.error, j."updatedAt", s.path, s.title, s.author '
                'FROM "ImportJob" j JOIN "SourceAsset" s ON s.id = j."sourceAssetId" '
                'WHERE j.status = :status ORDER BY j."updatedAt" DESC LIMIT :limit'
            ),
            {"status": "review_required", "limit": limit},
        )
    ).mappings().all()
    return [dict(r) for r in rows]


@app.get("/api/import/jobs/{job_id}/missing-mappings")
async def get_missing_mappings(
    job_id: str,
    novelId: str,
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_current_user),
):
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")

    job = (
        await db.execute(text('SELECT id, "sourceAssetId" FROM "ImportJob" WHERE id = :id LIMIT 1'), {"id": job_id})
    ).mappings().first()
    if not job:
        raise HTTPException(status_code=404, detail="Import job not found")

    asset_id = str(job["sourceAssetId"])
    asset_dir = Path(settings.nas_content_root) / asset_id
    if not asset_dir.exists():
        raise HTTPException(status_code=400, detail="Converted content folder not found")

    missing: list[dict[str, Any]] = []
    for txt_file in sorted(asset_dir.glob("*.txt")):
        token = txt_file.stem
        if not token.isdigit():
            continue
        chapter_number = int(token)
        chapter = (
            await db.execute(
                text('SELECT id FROM "ChapterMeta" WHERE "novelId" = :novel_id AND number = :number LIMIT 1'),
                {"novel_id": novelId, "number": chapter_number},
            )
        ).mappings().first()
        if not chapter:
            missing.append(
                {
                    "sourceChapterNumber": chapter_number,
                    "txtHref": f"{asset_id}/{chapter_number}.txt",
                    "rawHtmlHref": f"{asset_id}/{chapter_number}.raw.html",
                }
            )
    return {"jobId": job_id, "sourceAssetId": asset_id, "novelId": novelId, "missing": missing}


@app.post("/api/import/jobs/{job_id}/manual-map")
async def manual_map_chapter(
    job_id: str,
    payload: ImportJobManualMapPayload,
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_current_user),
):
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")

    job = (
        await db.execute(text('SELECT id, "sourceAssetId" FROM "ImportJob" WHERE id = :id LIMIT 1'), {"id": job_id})
    ).mappings().first()
    if not job:
        raise HTTPException(status_code=404, detail="Import job not found")

    asset_id = str(job["sourceAssetId"])
    txt_href = f"{asset_id}/{payload.sourceChapterNumber}.txt"
    raw_href = f"{asset_id}/{payload.sourceChapterNumber}.raw.html"
    try:
        content = storage.read_text(txt_href)
    except Exception as exc:
        raise HTTPException(status_code=404, detail="Source chapter content not found") from exc

    target = (
        await db.execute(
            text('SELECT id FROM "ChapterMeta" WHERE id = :id AND "novelId" = :novel_id LIMIT 1'),
            {"id": payload.targetChapterId, "novel_id": payload.novelId},
        )
    ).mappings().first()
    if not target:
        raise HTTPException(status_code=404, detail="Target chapter not found for novel")

    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    if payload.overwrite:
        await db.execute(
            text(
                'INSERT INTO "ChapterContentRef" ("chapterId", "txtHref", "rawHtmlHref", "contentHash") '
                'VALUES (:chapter_id, :txt_href, :raw_href, :hash) '
                'ON CONFLICT ("chapterId") DO UPDATE '
                'SET "txtHref" = EXCLUDED."txtHref", "rawHtmlHref" = EXCLUDED."rawHtmlHref", '
                '"contentHash" = EXCLUDED."contentHash", "updatedAt" = NOW()'
            ),
            {
                "chapter_id": payload.targetChapterId,
                "txt_href": txt_href,
                "raw_href": raw_href,
                "hash": content_hash,
            },
        )
    else:
        await db.execute(
            text(
                'INSERT INTO "ChapterContentRef" ("chapterId", "txtHref", "rawHtmlHref", "contentHash") '
                'VALUES (:chapter_id, :txt_href, :raw_href, :hash) ON CONFLICT ("chapterId") DO NOTHING'
            ),
            {
                "chapter_id": payload.targetChapterId,
                "txt_href": txt_href,
                "raw_href": raw_href,
                "hash": content_hash,
            },
        )
    await db.commit()
    return {
        "jobId": job_id,
        "sourceAssetId": asset_id,
        "sourceChapterNumber": payload.sourceChapterNumber,
        "targetChapterId": payload.targetChapterId,
        "status": "mapped",
    }


@app.get("/api/import/jobs/{job_id}/source-chapter-preview")
async def preview_source_chapter(
    job_id: str,
    chapterNumber: int = Query(..., ge=1),
    includeRawHtml: bool = Query(default=False),
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_current_user),
):
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")

    job = (
        await db.execute(text('SELECT id, "sourceAssetId" FROM "ImportJob" WHERE id = :id LIMIT 1'), {"id": job_id})
    ).mappings().first()
    if not job:
        raise HTTPException(status_code=404, detail="Import job not found")

    asset_id = str(job["sourceAssetId"])
    txt_href = f"{asset_id}/{chapterNumber}.txt"
    raw_href = f"{asset_id}/{chapterNumber}.raw.html"
    try:
        txt_content = storage.read_text(txt_href)
    except Exception as exc:
        raise HTTPException(status_code=404, detail="Source chapter not found") from exc

    response: dict[str, Any] = {
        "jobId": job_id,
        "sourceAssetId": asset_id,
        "chapterNumber": chapterNumber,
        "txtHref": txt_href,
        "rawHtmlHref": raw_href,
        "txt": txt_content,
    }
    if includeRawHtml:
        try:
            response["rawHtml"] = storage.read_text(raw_href)
        except Exception:
            response["rawHtml"] = None
    return response


@app.post("/api/import/jobs/{job_id}/complete")
async def complete_import_job(
    job_id: str,
    payload: ImportJobCompletePayload,
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_current_user),
):
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")

    row = (
        await db.execute(
            text('SELECT id, "sourceAssetId", status FROM "ImportJob" WHERE id = :id LIMIT 1'),
            {"id": job_id},
        )
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Import job not found")

    if row["status"] not in ("review_required", "completed") and not payload.force:
        raise HTTPException(status_code=400, detail="Job is not ready for completion")

    await db.execute(
        text('UPDATE "ImportJob" SET status = :status, error = NULL, "updatedAt" = NOW() WHERE id = :id'),
        {"id": job_id, "status": "completed"},
    )
    await db.execute(
        text(
            'UPDATE "AssetNovelMapping" SET status = :status, "updatedAt" = NOW() '
            'WHERE "sourceAssetId" = :asset_id AND status != :status'
        ),
        {"asset_id": row["sourceAssetId"], "status": "completed"},
    )
    await db.execute(
        text('UPDATE "SourceAsset" SET status = :status, review_status = :review_status, "updatedAt" = NOW() WHERE id = :id'),
        {"id": row["sourceAssetId"], "status": "completed", "review_status": "imported"},
    )
    await db.commit()
    return {"jobId": job_id, "status": "completed", "sourceAssetId": row["sourceAssetId"]}


@app.get("/api/import/jobs/{job_id}/mapping-progress")
async def get_mapping_progress(
    job_id: str,
    novelId: str,
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_current_user),
):
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")

    job = (
        await db.execute(text('SELECT id, "sourceAssetId", status, error FROM "ImportJob" WHERE id = :id LIMIT 1'), {"id": job_id})
    ).mappings().first()
    if not job:
        raise HTTPException(status_code=404, detail="Import job not found")

    asset_id = str(job["sourceAssetId"])
    asset_dir = Path(settings.nas_content_root) / asset_id
    if not asset_dir.exists():
        raise HTTPException(status_code=400, detail="Converted content folder not found")

    total = 0
    mapped = 0
    missing_numbers: list[int] = []
    for txt_file in sorted(asset_dir.glob("*.txt")):
        token = txt_file.stem
        if not token.isdigit():
            continue
        chapter_number = int(token)
        total += 1
        chapter = (
            await db.execute(
                text('SELECT id FROM "ChapterMeta" WHERE "novelId" = :novel_id AND number = :number LIMIT 1'),
                {"novel_id": novelId, "number": chapter_number},
            )
        ).mappings().first()
        if not chapter:
            missing_numbers.append(chapter_number)
            continue

        chapter_id = str(chapter.get("id"))
        ref = (
            await db.execute(
                text('SELECT "chapterId" FROM "ChapterContentRef" WHERE "chapterId" = :id LIMIT 1'),
                {"id": chapter_id},
            )
        ).mappings().first()
        if ref:
            mapped += 1
        else:
            missing_numbers.append(chapter_number)

    missing = max(total - mapped, 0)
    percent = 100.0 if total == 0 else round((mapped / total) * 100, 2)
    return {
        "jobId": job_id,
        "sourceAssetId": asset_id,
        "novelId": novelId,
        "jobStatus": job["status"],
        "jobError": job.get("error"),
        "totalSourceChapters": total,
        "mappedChapters": mapped,
        "missingChapters": missing,
        "progressPercent": percent,
        "missingChapterNumbers": missing_numbers,
    }


@app.post("/api/import/jobs/{job_id}/auto-complete")
async def auto_complete_import_job(
    job_id: str,
    novelId: str,
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_current_user),
):
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")

    job = (
        await db.execute(text('SELECT id, "sourceAssetId" FROM "ImportJob" WHERE id = :id LIMIT 1'), {"id": job_id})
    ).mappings().first()
    if not job:
        raise HTTPException(status_code=404, detail="Import job not found")

    asset_id = str(job["sourceAssetId"])
    asset_dir = Path(settings.nas_content_root) / asset_id
    if not asset_dir.exists():
        raise HTTPException(status_code=400, detail="Converted content folder not found")

    total = 0
    mapped = 0
    for txt_file in sorted(asset_dir.glob("*.txt")):
        token = txt_file.stem
        if not token.isdigit():
            continue
        chapter_number = int(token)
        total += 1
        chapter = (
            await db.execute(
                text('SELECT id FROM "ChapterMeta" WHERE "novelId" = :novel_id AND number = :number LIMIT 1'),
                {"novel_id": novelId, "number": chapter_number},
            )
        ).mappings().first()
        if not chapter:
            continue
        chapter_id = str(chapter.get("id"))
        ref = (
            await db.execute(
                text('SELECT "chapterId" FROM "ChapterContentRef" WHERE "chapterId" = :id LIMIT 1'),
                {"id": chapter_id},
            )
        ).mappings().first()
        if ref:
            mapped += 1

    if total == 0:
        raise HTTPException(status_code=400, detail="No source chapter files found")
    if mapped != total:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot auto-complete: mapped {mapped}/{total}. Resolve missing mappings first.",
        )

    await db.execute(
        text('UPDATE "ImportJob" SET status = :status, error = NULL, "updatedAt" = NOW() WHERE id = :id'),
        {"id": job_id, "status": "completed"},
    )
    await db.execute(
        text(
            'UPDATE "AssetNovelMapping" SET status = :status, "updatedAt" = NOW() '
            'WHERE "sourceAssetId" = :asset_id AND "novelId" = :novel_id'
        ),
        {"status": "completed", "asset_id": asset_id, "novel_id": novelId},
    )
    await db.commit()
    return {"jobId": job_id, "sourceAssetId": asset_id, "novelId": novelId, "status": "completed", "mapped": mapped, "total": total}


@app.delete("/api/import/jobs/{job_id}")
async def delete_import_job(
    job_id: str,
    removeContent: bool = Query(default=True),
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_current_user),
):
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")

    row = (
        await db.execute(
            text('SELECT id, "sourceAssetId" FROM "ImportJob" WHERE id = :id LIMIT 1'),
            {"id": job_id},
        )
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Import job not found")

    source_asset_id = str(row["sourceAssetId"])
    removed_files = 0
    removed_dir = False

    if removeContent:
        asset_dir = Path(settings.nas_content_root) / source_asset_id
        if asset_dir.exists() and asset_dir.is_dir():
            files = list(asset_dir.glob("**/*"))
            for p in files:
                if p.is_file():
                    p.unlink(missing_ok=True)
                    removed_files += 1
            for p in sorted(asset_dir.glob("**/*"), key=lambda x: len(x.parts), reverse=True):
                if p.is_dir():
                    p.rmdir()
            asset_dir.rmdir()
            removed_dir = True

    await db.execute(text('DELETE FROM "ImportJob" WHERE id = :id'), {"id": job_id})
    await db.execute(text('DELETE FROM "AssetNovelMapping" WHERE "sourceAssetId" = :asset_id'), {"asset_id": source_asset_id})
    await db.commit()

    return {
        "jobId": job_id,
        "deleted": True,
        "sourceAssetId": source_asset_id,
        "removeContent": removeContent,
        "removedFiles": removed_files,
        "removedDir": removed_dir,
    }


@app.post("/api/truyen/{novel_id}/rate")
async def rate_novel(novel_id: str, payload: RatePayload, db: AsyncSession = Depends(get_db_session)):
    row = (
        await db.execute(
            text(
                'UPDATE "Novel" '
                'SET "ratingCount" = "ratingCount" + 1, '
                'rating = ((rating * "ratingCount") + :score) / ("ratingCount" + 1) '
                'WHERE id = :novel_id '
                'RETURNING rating, "ratingCount"'
            ),
            {"score": payload.score, "novel_id": novel_id},
        )
    ).mappings().first()

    if not row:
        raise HTTPException(status_code=404, detail="Novel not found")

    await db.commit()
    return {"rating": float(row["rating"] or 0), "ratingCount": int(row["ratingCount"] or 0)}


@app.get("/api/truyen/{novel_id}/comments")
async def list_comments(
    novel_id: str,
    chapterId: str | None = None,
    scope: str | None = None,
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=20, ge=1, le=50),
    db: AsyncSession = Depends(get_db_session),
):
    skip = (page - 1) * limit
    if chapterId:
        where_sql = 'c."novelId" = :novel_id AND c."chapterId" = :chapter_id'
        params = {"novel_id": novel_id, "chapter_id": chapterId, "skip": skip, "limit": limit}
    elif scope == "chapter":
        where_sql = 'c."novelId" = :novel_id AND c."chapterId" IS NOT NULL'
        params = {"novel_id": novel_id, "skip": skip, "limit": limit}
    else:
        where_sql = 'c."novelId" = :novel_id AND c."chapterId" IS NULL'
        params = {"novel_id": novel_id, "skip": skip, "limit": limit}

    rows = (
        await db.execute(
            text(
                f'SELECT c.id, c."userId", c."novelId", c."chapterId", c.content, c."createdAt", '
                f'u.name AS username, u.image AS avatar_url '
                f'FROM "Comment" c '
                f'JOIN "User" u ON u.id = c."userId" '
                f'WHERE {where_sql} '
                f'ORDER BY c."createdAt" DESC '
                f'OFFSET :skip LIMIT :limit'
            ),
            params,
        )
    ).mappings().all()

    total_count = (
        await db.execute(
            text(f'SELECT COUNT(*)::int FROM "Comment" c WHERE {where_sql}'),
            {k: v for k, v in params.items() if k in {"novel_id", "chapter_id"}},
        )
    ).scalar_one()

    return {
        "comments": [
            {
                "id": row["id"],
                "userId": row["userId"],
                "username": row["username"] or "User",
                "avatarUrl": row["avatar_url"],
                "novelId": row["novelId"],
                "chapterId": row["chapterId"],
                "content": row["content"],
                "createdAt": _iso(row["createdAt"]),
            }
            for row in rows
        ],
        "totalCount": total_count,
        "totalPages": (total_count + limit - 1) // limit if total_count else 0,
        "currentPage": page,
    }


class CommentPayload(BaseModel):
    content: str
    chapterId: str | None = None


@app.post("/api/truyen/{novel_id}/comments")
async def create_comment(
    novel_id: str,
    payload: CommentPayload,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
):
    user = await require_current_user(request, db)
    content = payload.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="Content is required")

    row = (
        await db.execute(
            text(
                'INSERT INTO "Comment"(id, content, "userId", "novelId", "chapterId", "createdAt", "updatedAt") '
                'VALUES (:id, :content, :user_id, :novel_id, :chapter_id, NOW(), NOW()) '
                'RETURNING id, content, "createdAt"'
            ),
            {
                "id": _new_id("cmt_"),
                "content": content,
                "user_id": user["id"],
                "novel_id": novel_id,
                "chapter_id": payload.chapterId,
            },
        )
    ).mappings().first()

    await db.commit()

    return {
        "id": row["id"],
        "userId": user["id"],
        "username": user.get("name") or "User",
        "avatarColor": user.get("image") or "bg-primary",
        "novelId": novel_id,
        "chapterId": payload.chapterId,
        "content": row["content"],
        "createdAt": _iso(row["createdAt"]),
    }


@app.get("/api/user/bookmarks")
async def list_bookmarks(request: Request, db: AsyncSession = Depends(get_db_session)):
    user = await require_current_user(request, db)

    rows = (
        await db.execute(
            text(
                'SELECT b.id, b."novelId", b."lastChapterId", b."lastChapterNumber", b."readChapters", '
                'n.id AS novel_id, n.title AS novel_title, n.slug AS novel_slug, n."authorName" AS novel_author_name, '
                'n."coverUrl" AS novel_cover_url, n.status AS novel_status, n."totalChapters" AS novel_total_chapters, '
                'n.rating AS novel_rating, n."ratingCount" AS novel_rating_count '
                'FROM "Bookmark" b '
                'JOIN "Novel" n ON n.id = b."novelId" '
                'WHERE b."userId" = :user_id '
                'ORDER BY b."createdAt" DESC'
            ),
            {"user_id": user["id"]},
        )
    ).mappings().all()

    return [
        {
            "id": row["id"],
            "novelId": row["novelId"],
            "lastChapterId": row["lastChapterId"],
            "lastChapterNumber": row["lastChapterNumber"],
            "readChapters": row["readChapters"] or [],
            "novel": {
                "id": row["novel_id"],
                "title": row["novel_title"],
                "slug": row["novel_slug"],
                "authorName": row["novel_author_name"],
                "coverUrl": row["novel_cover_url"],
                "status": row["novel_status"],
                "totalChapters": row["novel_total_chapters"],
                "rating": float(row["novel_rating"] or 0),
                "ratingCount": int(row["novel_rating_count"] or 0),
            },
        }
        for row in rows
    ]


class BookmarkPayload(BaseModel):
    action: str | None = None
    novelId: str
    lastChapterId: str | None = None
    lastChapterNumber: int | None = None


@app.post("/api/user/bookmarks")
async def upsert_bookmark(payload: BookmarkPayload, request: Request, db: AsyncSession = Depends(get_db_session)):
    user = await require_current_user(request, db)
    action = (payload.action or "").strip()

    existing = (
        await db.execute(
            text(
                'SELECT id FROM "Bookmark" WHERE "userId" = :user_id AND "novelId" = :novel_id LIMIT 1'
            ),
            {"user_id": user["id"], "novel_id": payload.novelId},
        )
    ).mappings().first()

    if action == "toggle":
        if existing:
            await db.execute(
                text('DELETE FROM "Bookmark" WHERE id = :bookmark_id'),
                {"bookmark_id": existing["id"]},
            )
            await db.execute(
                text('UPDATE "Novel" SET "bookmarkCount" = GREATEST("bookmarkCount" - 1, 0) WHERE id = :novel_id'),
                {"novel_id": payload.novelId},
            )
            await db.commit()
            return {"status": "removed"}

        await db.execute(
            text(
                'INSERT INTO "Bookmark"(id, "userId", "novelId", "lastChapterId", "lastChapterNumber", '
                '"readChapters", "hasCountedView", "createdAt") '
                'VALUES (:id, :user_id, :novel_id, :last_chapter_id, :last_chapter_number, :read_chapters, false, NOW())'
            ),
            {
                "id": _new_id("bm_"),
                "user_id": user["id"],
                "novel_id": payload.novelId,
                "last_chapter_id": payload.lastChapterId,
                "last_chapter_number": payload.lastChapterNumber,
                "read_chapters": [payload.lastChapterNumber] if payload.lastChapterNumber else [],
            },
        )
        await db.execute(
            text('UPDATE "Novel" SET "bookmarkCount" = "bookmarkCount" + 1 WHERE id = :novel_id'),
            {"novel_id": payload.novelId},
        )
        await db.commit()
        bookmark = await _load_bookmark_with_novel(db, user["id"], payload.novelId)
        return {"status": "added", "bookmark": bookmark}

    if action == "updateProgress":
        if payload.lastChapterId is None or payload.lastChapterNumber is None:
            raise HTTPException(status_code=400, detail="Missing chapter info")
        data = await _update_reading_progress(
            db,
            user["id"],
            payload.novelId,
            payload.lastChapterId,
            payload.lastChapterNumber,
        )
        await db.commit()
        return data

    if existing:
        bookmark = await _load_bookmark_with_novel(db, user["id"], payload.novelId)
        return bookmark

    await db.execute(
        text(
            'INSERT INTO "Bookmark"(id, "userId", "novelId", "lastChapterId", "lastChapterNumber", '
            '"readChapters", "hasCountedView", "createdAt") '
            'VALUES (:id, :user_id, :novel_id, :last_chapter_id, :last_chapter_number, :read_chapters, false, NOW())'
        ),
        {
            "id": _new_id("bm_"),
            "user_id": user["id"],
            "novel_id": payload.novelId,
            "last_chapter_id": payload.lastChapterId,
            "last_chapter_number": payload.lastChapterNumber,
            "read_chapters": [payload.lastChapterNumber] if payload.lastChapterNumber else [],
        },
    )
    await db.execute(
        text('UPDATE "Novel" SET "bookmarkCount" = "bookmarkCount" + 1 WHERE id = :novel_id'),
        {"novel_id": payload.novelId},
    )
    await db.commit()
    return await _load_bookmark_with_novel(db, user["id"], payload.novelId)


@app.delete("/api/user/bookmarks/{novel_id}")
async def delete_bookmark(novel_id: str, request: Request, db: AsyncSession = Depends(get_db_session)):
    user = await require_current_user(request, db)
    row = (
        await db.execute(
            text(
                'SELECT id FROM "Bookmark" WHERE "userId" = :user_id AND "novelId" = :novel_id LIMIT 1'
            ),
            {"user_id": user["id"], "novel_id": novel_id},
        )
    ).mappings().first()

    if not row:
        raise HTTPException(status_code=404, detail="Bookmark not found")

    await db.execute(text('DELETE FROM "Bookmark" WHERE id = :id'), {"id": row["id"]})
    await db.execute(
        text('UPDATE "Novel" SET "bookmarkCount" = GREATEST("bookmarkCount" - 1, 0) WHERE id = :novel_id'),
        {"novel_id": novel_id},
    )
    await db.commit()
    return {"status": "removed"}


class ReadingProgressPayload(BaseModel):
    novelId: str
    chapterId: str
    chapterNumber: int
    progress: float | None = None


@app.post("/api/user/reading-progress")
async def update_reading_progress(
    payload: ReadingProgressPayload,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
):
    user = await require_current_user(request, db)
    result = await _update_reading_progress(
        db,
        user["id"],
        payload.novelId,
        payload.chapterId,
        payload.chapterNumber,
    )
    await db.commit()
    return result


@app.get("/api/user/profile")
async def profile(request: Request, db: AsyncSession = Depends(get_db_session)):
    user = await require_current_user(request, db)
    return {
        "id": user["id"],
        "email": user.get("email"),
        "name": user.get("name"),
        "image": user.get("image"),
        "role": user.get("role", "USER"),
    }


@app.get("/api/user/settings")
async def get_user_settings(request: Request, db: AsyncSession = Depends(get_db_session)):
    user = await require_current_user(request, db)
    row = (
        await db.execute(
            text(
                'SELECT "fontSize", "lineHeight", "letterSpacing", "fontFamily" '
                'FROM "UserSetting" WHERE "userId" = :user_id LIMIT 1'
            ),
            {"user_id": user["id"]},
        )
    ).mappings().first()

    if not row:
        return {}

    return {
        "fontSize": float(row["fontSize"]),
        "lineHeight": float(row["lineHeight"]),
        "letterSpacing": float(row["letterSpacing"]),
        "fontFamily": row["fontFamily"],
    }


class UserSettingsPayload(BaseModel):
    fontSize: float | None = None
    lineHeight: float | None = None
    letterSpacing: float | None = None
    fontFamily: str | None = None


@app.post("/api/user/settings")
async def save_user_settings(
    payload: UserSettingsPayload,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
):
    user = await require_current_user(request, db)

    existing = (
        await db.execute(
            text('SELECT id FROM "UserSetting" WHERE "userId" = :user_id LIMIT 1'),
            {"user_id": user["id"]},
        )
    ).mappings().first()

    font_size = payload.fontSize if payload.fontSize is not None else 18
    line_height = payload.lineHeight if payload.lineHeight is not None else 1.8
    letter_spacing = payload.letterSpacing if payload.letterSpacing is not None else 0
    font_family = payload.fontFamily if payload.fontFamily is not None else "font-serif"

    if existing:
        await db.execute(
            text(
                'UPDATE "UserSetting" SET '
                '"fontSize" = COALESCE(:font_size, "fontSize"), '
                '"lineHeight" = COALESCE(:line_height, "lineHeight"), '
                '"letterSpacing" = COALESCE(:letter_spacing, "letterSpacing"), '
                '"fontFamily" = COALESCE(:font_family, "fontFamily") '
                'WHERE id = :setting_id'
            ),
            {
                "font_size": payload.fontSize,
                "line_height": payload.lineHeight,
                "letter_spacing": payload.letterSpacing,
                "font_family": payload.fontFamily,
                "setting_id": existing["id"],
            },
        )
    else:
        await db.execute(
            text(
                'INSERT INTO "UserSetting"(id, "userId", "fontSize", "lineHeight", "letterSpacing", "fontFamily") '
                'VALUES (:id, :user_id, :font_size, :line_height, :letter_spacing, :font_family)'
            ),
            {
                "id": _new_id("uset_"),
                "user_id": user["id"],
                "font_size": font_size,
                "line_height": line_height,
                "letter_spacing": letter_spacing,
                "font_family": font_family,
            },
        )

    await db.commit()
    return {
        "fontSize": font_size,
        "lineHeight": line_height,
        "letterSpacing": letter_spacing,
        "fontFamily": font_family,
    }


@app.get("/api/user/recommendations")
async def list_recommendations(request: Request, db: AsyncSession = Depends(get_db_session)):
    user = await require_current_user(request, db)

    try:
        docs = (
            await db.execute(
                text(
                    'SELECT id, "novelId", "createdAt" FROM "UserRecommendationDoc" '
                    'WHERE "userId" = :user_id ORDER BY "createdAt" DESC LIMIT 1000'
                ),
                {"user_id": user["id"]},
            )
        ).mappings().all()
    except Exception:
        return []

    novel_ids = list({doc.get("novelId") for doc in docs if doc.get("novelId")})
    novel_map: dict[str, dict[str, Any]] = {}

    if novel_ids:
        rows = (
            await db.execute(
                text(
                    'SELECT id, title, slug, "authorName", "coverUrl", status, "totalChapters" '
                    'FROM "Novel" WHERE id = ANY(:novel_ids)'
                ),
                {"novel_ids": novel_ids},
            )
        ).mappings().all()
        novel_map = {row["id"]: dict(row) for row in rows}

    items: list[dict[str, Any]] = []
    for doc in docs:
        novel_id = doc.get("novelId")
        if not novel_id or novel_id not in novel_map:
            continue
        items.append(
            {
                "id": str(doc.get("id")),
                "novelId": novel_id,
                "createdAt": _iso(doc.get("createdAt")),
                "novel": novel_map[novel_id],
            }
        )

    return items


class RecommendationPayload(BaseModel):
    novelId: str


@app.post("/api/user/recommendations")
async def create_recommendation(
    payload: RecommendationPayload,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
):
    user = await require_current_user(request, db)

    novel_exists = (
        await db.execute(
            text('SELECT id FROM "Novel" WHERE id = :novel_id LIMIT 1'),
            {"novel_id": payload.novelId},
        )
    ).scalar_one_or_none()
    if not novel_exists:
        raise HTTPException(status_code=404, detail="Truyện không tồn tại")

    try:
        existing = (
            await db.execute(
                text('SELECT id FROM "UserRecommendationDoc" WHERE "userId" = :user_id AND "novelId" = :novel_id LIMIT 1'),
                {"user_id": user["id"], "novel_id": payload.novelId},
            )
        ).mappings().first()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Recommendation service is initializing") from exc
    if existing:
        raise HTTPException(status_code=409, detail="Bạn đã đề cử truyện này rồi")

    now = dt.datetime.now(dt.timezone.utc)
    rec_id = _new_id("urec_")
    await db.execute(
        text(
            'INSERT INTO "UserRecommendationDoc" (id, "userId", "novelId", "createdAt") '
            'VALUES (:id, :user_id, :novel_id, :created_at)'
        ),
        {"id": rec_id, "user_id": user["id"], "novel_id": payload.novelId, "created_at": now},
    )

    await db.execute(
        text('UPDATE "Novel" SET "bookmarkCount" = "bookmarkCount" + 1 WHERE id = :novel_id'),
        {"novel_id": payload.novelId},
    )
    await db.commit()

    return {"id": rec_id, "novelId": payload.novelId}


@app.delete("/api/user/recommendations")
async def delete_recommendation(
    request: Request,
    novelId: str = Query(...),
    db: AsyncSession = Depends(get_db_session),
):
    user = await require_current_user(request, db)

    try:
        existing = (
            await db.execute(
                text('SELECT id FROM "UserRecommendationDoc" WHERE "userId" = :user_id AND "novelId" = :novel_id LIMIT 1'),
                {"user_id": user["id"], "novel_id": novelId},
            )
        ).mappings().first()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Recommendation service is initializing") from exc
    if not existing:
        raise HTTPException(status_code=404, detail="Bạn chưa đề cử truyện này")

    await db.execute(
        text('DELETE FROM "UserRecommendationDoc" WHERE id = :id'),
        {"id": existing["id"]},
    )
    await db.execute(
        text('UPDATE "Novel" SET "bookmarkCount" = GREATEST("bookmarkCount" - 1, 0) WHERE id = :novel_id'),
        {"novel_id": novelId},
    )
    await db.commit()

    return {"success": True}


class MobileLoginPayload(BaseModel):
    googleIdToken: str


@app.post("/api/auth/mobile-login")
async def mobile_login(payload: MobileLoginPayload, db: AsyncSession = Depends(get_db_session)):
    if not payload.googleIdToken.strip():
        raise HTTPException(status_code=400, detail="googleIdToken is required")

    allowed_client_ids = settings.google_client_id_list

    try:
        id_info = google_id_token.verify_oauth2_token(
            payload.googleIdToken,
            google_requests.Request(),
            None,
        )
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid Google token") from exc

    aud = (id_info.get("aud") or "").strip()
    if allowed_client_ids and aud not in set(allowed_client_ids):
        raise HTTPException(status_code=401, detail="Invalid Google token audience")

    email = id_info.get("email")
    if not email:
        raise HTTPException(status_code=401, detail="Unable to extract email from token")

    name = id_info.get("name")
    picture = id_info.get("picture")
    google_sub = id_info.get("sub")

    user = (
        await db.execute(
            text('SELECT id, email, name, image, role FROM "User" WHERE email = :email LIMIT 1'),
            {"email": email},
        )
    ).mappings().first()

    if not user:
        user_id = _new_id("usr_")
        await db.execute(
            text(
                'INSERT INTO "User"(id, email, name, image, "emailVerified", role) '
                'VALUES (:id, :email, :name, :image, NOW(), :role)'
            ),
            {
                "id": user_id,
                "email": email,
                "name": name,
                "image": picture,
                "role": "USER",
            },
        )
        user = {"id": user_id, "email": email, "name": name, "image": picture, "role": "USER"}

    if google_sub:
        account_exists = (
            await db.execute(
                text(
                    'SELECT id FROM "Account" WHERE provider = :provider AND "providerAccountId" = :provider_account_id LIMIT 1'
                ),
                {"provider": "google", "provider_account_id": google_sub},
            )
        ).scalar_one_or_none()
        if not account_exists:
            await db.execute(
                text(
                    'INSERT INTO "Account"(id, "userId", type, provider, "providerAccountId") '
                    'VALUES (:id, :user_id, :type, :provider, :provider_account_id)'
                ),
                {
                    "id": _new_id("acc_"),
                    "user_id": user["id"],
                    "type": "oauth",
                    "provider": "google",
                    "provider_account_id": google_sub,
                },
            )

    await db.commit()

    access_token = create_access_token(user["id"])
    refresh_token = secrets.token_hex(40)

    return {
        "accessToken": access_token,
        "refreshToken": refresh_token,
        "expiresIn": ACCESS_TOKEN_TTL_SECONDS,
        "user": {
            "id": user["id"],
            "email": user.get("email"),
            "name": user.get("name"),
            "image": user.get("image"),
            "role": user.get("role", "USER"),
        },
    }


@app.get("/api/auth/session")
async def auth_session(request: Request, db: AsyncSession = Depends(get_db_session)):
    user = await require_current_user(request, db)
    return {
        "user": {
            "id": user["id"],
            "email": user.get("email"),
            "name": user.get("name"),
            "image": user.get("image"),
            "role": user.get("role", "USER"),
        }
    }
