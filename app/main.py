from __future__ import annotations

import asyncio
import base64
import datetime as dt
import hashlib
import html as html_lib
import json
import logging
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
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import ACCESS_TOKEN_TTL_SECONDS, create_access_token, require_current_user, resolve_current_user, verify_google_id_token
from app.config import settings
from app.database import get_db_session
from app import openrouter
from app.storage import storage

logger = logging.getLogger(__name__)

# Giới hạn chương EPUB chỉ khi client gửi `enforceMaxChapters=true` (import nhiều / batch).
MOD_EPUB_MAX_CHAPTERS = 4000


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
    ]

    async with engine.begin() as conn:
        for ddl in ddl_statements:
            try:
                await conn.execute(text(ddl))
            except Exception:
                if ddl.strip().lower().startswith("create extension"):
                    continue
                raise

    migration_ddls = [
        'ALTER TABLE "Novel" DROP CONSTRAINT IF EXISTS "Novel_seriesId_fkey"',
        'ALTER TABLE "Novel" DROP COLUMN IF EXISTS "seriesId"',
        'DROP TABLE IF EXISTS "Series" CASCADE',
        'ALTER TABLE "Bookmark" ADD COLUMN IF NOT EXISTS "markedAsRead" BOOLEAN NOT NULL DEFAULT false',
        'UPDATE "Novel" SET rating = rating * 2 WHERE "ratingCount" > 0 AND rating > 0 AND rating <= 5',
        'DROP TABLE IF EXISTS "Comment" CASCADE',
        'DROP TABLE IF EXISTS "UserRecommendationDoc" CASCADE',
        'DROP TABLE IF EXISTS "EditorRecommendationDoc" CASCADE',
        '''
        CREATE TABLE IF NOT EXISTS "NovelRating" (
            id TEXT PRIMARY KEY,
            "userId" TEXT NOT NULL,
            "novelId" TEXT NOT NULL,
            score DOUBLE PRECISION NOT NULL,
            "createdAt" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            "updatedAt" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE("userId", "novelId")
        )
        ''',
        'CREATE INDEX IF NOT EXISTS "NovelRating_novelId_idx" ON "NovelRating"("novelId")',
    ]
    async with engine.begin() as conn:
        for ddl in migration_ddls:
            try:
                await conn.execute(text(ddl))
            except Exception:
                pass


app = FastAPI(title=settings.app_name, lifespan=lifespan)




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
                'n."updatedAt", COALESCE(SUM(v.views), 0)::int AS aggregated_views '
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
                '"totalChapters", status, description, "bookmarkCount", "updatedAt" '
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
                '"totalChapters", status, description, "bookmarkCount", "updatedAt" '
                'FROM "Novel" '
                'ORDER BY "updatedAt" DESC '
                'LIMIT :take'
            ),
            {"take": take},
        )
    ).mappings().all()

    return [_home_novel_from_row(dict(row)) for row in rows]


async def _fetch_latest_chapter_map(db: AsyncSession, novel_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not novel_ids:
        return {}
    chapter_rows = (
        await db.execute(
            text(
                'SELECT DISTINCT ON ("novelId") "novelId", number, title, "createdAt" '
                'FROM "ChapterMeta" '
                'WHERE "novelId" = ANY(:novel_ids) '
                'ORDER BY "novelId", "createdAt" DESC NULLS LAST, number DESC'
            ),
            {"novel_ids": novel_ids},
        )
    ).mappings().all()
    return {
        str(row["novelId"]): {
            "number": row.get("number"),
            "title": row.get("title"),
            "createdAt": _iso(row.get("createdAt")),
        }
        for row in chapter_rows
    }


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
                '"totalChapters", status, description, "bookmarkCount", "updatedAt" '
                'FROM "Novel" '
                'WHERE id = ANY(:novel_ids)'
            ),
            {"novel_ids": latest_novel_ids},
        )
    ).mappings().all()

    novel_map = {row["id"]: _home_novel_from_row(dict(row)) for row in novel_rows}
    ordered = [novel_map[novel_id] for novel_id in latest_novel_ids if novel_id in novel_map]
    picked = ordered[:take]

    for novel in picked:
        novel["latestChapter"] = latest_chapter_map.get(novel["id"])

    return picked


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


def _bookmark_shelf_status(
    last_chapter_number: int | None,
    total_chapters: int | None,
    read_chapters: list[int] | None,
    marked_as_read: bool = False,
) -> str:
    if marked_as_read:
        return "completed"
    has_progress = last_chapter_number is not None or bool(read_chapters)
    total = int(total_chapters or 0)
    if has_progress and total > 0 and last_chapter_number is not None and last_chapter_number >= total:
        return "completed"
    if has_progress:
        return "reading"
    return "saved"


def _serialize_bookmark_row(row: Any) -> dict[str, Any]:
    read_chapters = list(row["readChapters"] or [])
    last_chapter_number = row["lastChapterNumber"]
    total_chapters = int(row["novel_total_chapters"] or 0)
    marked_as_read = bool(row.get("markedAsRead"))
    return {
        "id": row["id"],
        "novelId": row["novelId"],
        "lastChapterId": row["lastChapterId"],
        "lastChapterNumber": last_chapter_number,
        "readChapters": read_chapters,
        "markedAsRead": marked_as_read,
        "shelfStatus": _bookmark_shelf_status(
            last_chapter_number, total_chapters, read_chapters, marked_as_read
        ),
        "novel": {
            "id": row["novel_id"],
            "title": row["novel_title"],
            "slug": row["novel_slug"],
            "authorName": row["novel_author_name"],
            "coverUrl": row["novel_cover_url"],
            "status": row["novel_status"],
            "totalChapters": total_chapters,
            "rating": float(row["novel_rating"] or 0),
            "ratingCount": int(row["novel_rating_count"] or 0),
        },
    }


async def _load_bookmark_with_novel(db: AsyncSession, user_id: str, novel_id: str) -> dict[str, Any] | None:
    result = await db.execute(
        text(
            'SELECT b.id, b."novelId", b."lastChapterId", b."lastChapterNumber", '
            'b."readChapters", b."markedAsRead", '
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

    return _serialize_bookmark_row(row)


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
        if raw_href and raw_href != txt_href:
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
                'SELECT n.id, n.title, n.slug, n."authorName", n.status, n."totalChapters", n."coverUrl" '
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
        }
        for r in rows
    ]


@app.get("/api/mod/truyen/by-title")
async def mod_novel_exists_by_title(
    title: str = Query(..., min_length=1, max_length=500),
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_current_user),
):
    """Dùng cho batch import: so khớp `lower(title)` giống logic import EPUB."""
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")
    norm = " ".join(str(title).split()).strip() or ""
    if not norm:
        return {"exists": False}
    row = (
        await db.execute(
            text('SELECT id, title, slug FROM "Novel" WHERE lower(title) = :title LIMIT 1'),
            {"title": norm.lower()},
        )
    ).mappings().first()
    if not row:
        return {"exists": False}
    return {
        "exists": True,
        "novel": {
            "id": str(row["id"]),
            "title": str(row.get("title") or ""),
            "slug": str(row.get("slug") or ""),
        },
    }


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
                'n.description, n."coverUrl", n.status, n."totalChapters" '
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

    novel_id = _new_id("n_")
    row = (
        await db.execute(
            text(
                'INSERT INTO "Novel" (id, title, slug, "authorName", "originalTitle", "originalAuthorName", description, "coverUrl", status, "totalChapters", views, rating, "ratingCount", "bookmarkCount", "createdAt", "updatedAt") '
                'VALUES (:id,:title,:slug,:author,:original_title,:original_author,:description,:cover_url,:status,0,0,0,0,0,NOW(),NOW()) '
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
            },
        )
    ).mappings().first()
    await _set_novel_genres(db, novel_id, payload.genreIds or [])
    await db.commit()
    return dict(row) if row else {}


@app.put("/api/mod/truyen")
async def mod_update_novel(
    payload: dict[str, Any] = Body(...),
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_current_user),
):
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")
    parsed = ModNovelPayload.model_validate(payload)
    novel_id = str(parsed.id or "").strip()
    if not novel_id:
        raise HTTPException(status_code=400, detail="id là bắt buộc")

    current = (
        await db.execute(text('SELECT id, title, slug FROM "Novel" WHERE id = :id LIMIT 1'), {"id": novel_id})
    ).mappings().first()
    if not current:
        raise HTTPException(status_code=404, detail="Novel not found")

    next_title = " ".join((parsed.title or str(current.get("title") or "")).split()).strip()
    next_author = " ".join((parsed.authorName or "").split()).strip()
    if not next_title:
        raise HTTPException(status_code=400, detail="title không hợp lệ")
    if parsed.authorName is not None and not next_author:
        raise HTTPException(status_code=400, detail="authorName không hợp lệ")

    slug_base = _norm_title(next_title).replace(" ", "-")[:120] or str(current.get("slug") or _new_id("n_"))
    next_slug = await _ensure_unique_slug(db, table="Novel", slug=slug_base, current_id=novel_id)

    row = (
        await db.execute(
            text(
                'UPDATE "Novel" SET '
                'title = :title, slug = :slug, '
                '"authorName" = COALESCE(:author_name, "authorName"), '
                '"originalTitle" = :original_title, "originalAuthorName" = :original_author, '
                'description = :description, "coverUrl" = :cover_url, '
                'status = COALESCE(:status, status), "updatedAt" = NOW() '
                'WHERE id = :id '
                'RETURNING id, title, slug, "authorName", status, "totalChapters", "coverUrl"'
            ),
            {
                "id": novel_id,
                "title": next_title,
                "slug": next_slug,
                "author_name": next_author or None,
                "original_title": (parsed.originalTitle or "").strip() or None,
                "original_author": (parsed.originalAuthorName or "").strip() or None,
                "description": (parsed.description or "").strip(),
                "cover_url": (parsed.coverUrl or "").strip() or None,
                "status": (parsed.status or "").strip() or None,
            },
        )
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Novel not found")
    if parsed.genreIds is not None:
        await _set_novel_genres(db, novel_id, parsed.genreIds)
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


@app.get("/api/mod/overview")
async def mod_overview(
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_current_user),
):
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")
    novel_count = (await db.execute(text('SELECT COUNT(*)::int FROM "Novel"'))).scalar_one()
    total_views = (await db.execute(text('SELECT COALESCE(SUM(views),0)::int FROM "Novel"'))).scalar_one()
    return {
        "novelCount": int(novel_count or 0),
        "totalViews": int(total_views or 0),
    }



async def _upsert_chapter_content(chapter_id: str, novel_id: str, number: int, content: str, db: AsyncSession) -> None:
    txt_href = f"novel-{novel_id}/{number}.txt"
    txt = str(content or "")
    # Một file duy nhất trên NAS: nội dung đọc hiển thị qua txtHref; rawHtmlHref trùng href để tránh ghi đôi (NAS chậm).
    await asyncio.to_thread(storage.write_text, txt_href, txt)
    raw_href = txt_href
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
    payload: dict[str, Any] = Body(...),
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_current_user),
):
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")
    parsed = ModChapterPayload.model_validate(payload)
    chapter_id = str(parsed.id or "").strip()
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
        {"id": chapter_id, "num": parsed.number, "title": parsed.title.strip()},
    )
    await _upsert_chapter_content(chapter_id, str(row["novelId"]), parsed.number, parsed.content, db)
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
    payload: dict[str, Any] = Body(...),
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_current_user),
):
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")
    parsed = ModChapterBulkDeletePayload.model_validate(payload)
    from_num = min(parsed.fromNumber, parsed.toNumber)
    to_num = max(parsed.fromNumber, parsed.toNumber)
    ids = (
        await db.execute(
            text('SELECT id FROM "ChapterMeta" WHERE "novelId" = :novel_id AND number BETWEEN :from_num AND :to_num'),
            {"novel_id": parsed.novelId, "from_num": from_num, "to_num": to_num},
        )
    ).mappings().all()
    chapter_ids = [str(r["id"]) for r in ids]
    if chapter_ids:
        await db.execute(text('DELETE FROM "ChapterContentRef" WHERE "chapterId" = ANY(:ids)'), {"ids": chapter_ids})
    deleted_count = (
        await db.execute(
            text('DELETE FROM "ChapterMeta" WHERE "novelId" = :novel_id AND number BETWEEN :from_num AND :to_num RETURNING id'),
            {"novel_id": parsed.novelId, "from_num": from_num, "to_num": to_num},
        )
    ).mappings().all()
    await db.execute(text('UPDATE "Novel" SET "totalChapters" = (SELECT COUNT(*) FROM "ChapterMeta" WHERE "novelId" = :novel_id), "updatedAt" = NOW() WHERE id = :novel_id'), {"novel_id": parsed.novelId})
    await db.commit()
    return {"deletedCount": len(deleted_count)}


@app.post("/api/mod/chuong/normalize-titles/preview")
async def mod_normalize_chapter_titles_preview(
    payload: dict[str, Any] = Body(...),
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_current_user),
):
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")

    parsed = ModNormalizeTitlesPreviewPayload.model_validate(payload)
    novel_id = parsed.novelId.strip()
    if not novel_id:
        raise HTTPException(status_code=400, detail="novelId is required")

    rows = (
        await db.execute(
            text('SELECT id, number, title FROM "ChapterMeta" WHERE "novelId" = :novel_id ORDER BY number ASC'),
            {"novel_id": novel_id},
        )
    ).mappings().all()

    items: list[dict[str, Any]] = []
    for row in rows:
        chapter_id = str(row["id"])
        number = int(row.get("number") or 0)
        current_title = str(row.get("title") or "").strip()
        content = await _resolve_chapter_content(chapter_id, db) or ""
        suggested_title = _infer_chapter_title_from_content(content, number, current_title).strip()
        if not suggested_title or suggested_title == current_title:
            continue
        if parsed.overwriteGenericOnly and not _is_generic_chapter_title(current_title, number):
            continue
        items.append(
            {
                "id": chapter_id,
                "number": number,
                "currentTitle": current_title,
                "suggestedTitle": suggested_title,
            }
        )

    return {
        "novelId": novel_id,
        "scannedCount": len(rows),
        "changeCount": len(items),
        "items": items,
    }


@app.put("/api/mod/chuong/optimize")
async def mod_optimize_chapters(
    payload: dict[str, Any] = Body(...),
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_current_user),
):
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")
    parsed = ModChapterOptimizePayload.model_validate(payload)
    modified = 0
    renumbered: list[tuple[str, int, int]] = []
    for item in parsed.updates:
        row = (
            await db.execute(
                text('SELECT id, number FROM "ChapterMeta" WHERE id = :id AND "novelId" = :novel_id LIMIT 1'),
                {"id": item.id, "novel_id": parsed.novelId},
            )
        ).mappings().first()
        if not row:
            continue
        old_number = int(row.get("number") or 0)
        await db.execute(
            text('UPDATE "ChapterMeta" SET number = :number, title = :title WHERE id = :id'),
            {"id": item.id, "number": item.number, "title": item.title},
        )
        if old_number > 0 and int(item.number) > 0 and old_number != int(item.number):
            renumbered.append((str(item.id), old_number, int(item.number)))
        modified += 1

    for chapter_id, old_number, new_number in renumbered:
        content = await _resolve_chapter_content(chapter_id, db)
        if content is None:
            continue
        await _upsert_chapter_content(chapter_id, parsed.novelId, new_number, content, db)
    await db.execute(text('UPDATE "Novel" SET "totalChapters" = (SELECT COUNT(*) FROM "ChapterMeta" WHERE "novelId" = :novel_id), "updatedAt" = NOW() WHERE id = :novel_id'), {"novel_id": parsed.novelId})
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
    chapterTag: str | None = Form(default=None),
    title: str | None = Form(default=None),
    originalTitle: str | None = Form(default=None),
    authorName: str | None = Form(default=None),
    originalAuthorName: str | None = Form(default=None),
    description: str | None = Form(default=None),
    genreIds: str | None = Form(default=None),
    status: str | None = Form(default=None),
    replaceExisting: str | None = Form(default=None),
    appendTargetNovelId: str | None = Form(default=None),
    enforceMaxChapters: str | None = Form(default=None),
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
        mode = _resolve_epub_split_mode(splitMode)
        pattern = (chapterRegex or "").strip() or None
        effective_tag = _normalize_chapter_html_tag(chapterTag) if mode == "tag" else None
        source_sections = _extract_epub_chapters(tmp_path)
        sections_after_filter = _filter_toc_chapters(source_sections) if mode == "toc" else source_sections
        chapters = _epub_extract_with_mode(tmp_path, mode, pattern, effective_tag)
        epub_meta = _extract_epub_metadata(tmp_path)
        inferred_title = str(epub_meta.get("title") or Path(file.filename or "novel").stem)
        inferred_author = str(epub_meta.get("author") or "Unknown")
        inferred_desc = str(epub_meta.get("description") or "")
        inferred_genres = [str(g).strip() for g in (epub_meta.get("genres") or []) if str(g).strip()]

        base_title = " ".join((title or inferred_title).split()).strip() or "Untitled"
        base_original_title = " ".join((originalTitle or "").split()).strip()
        base_author = " ".join((authorName or inferred_author).split()).strip() or "Unknown"
        base_original_author = " ".join((originalAuthorName or "").split()).strip()
        base_desc = (description if description is not None else inferred_desc).strip()
        base_status = " ".join((status or "Đang ra").split()).strip() or "Đang ra"
        parsed_genre_ids = [g.strip() for g in str(genreIds or "").split(",") if g.strip()]

        n_chapters = len(chapters)
        enforce_cap = str(enforceMaxChapters or "").lower() in {"1", "true", "yes", "on"}
        if enforce_cap and n_chapters > MOD_EPUB_MAX_CHAPTERS:
            if str(preview or "").lower() == "true":
                n_ch = n_chapters
                # Vẫn trả mẫu chương để mod xác nhận tách TOC/regex đúng; chỉ chặn import do chính sách.
                cover_blocked = _extract_epub_cover(tmp_path) or _extract_epub_cover_from_zip(tmp_path)
                has_cover_b = bool(cover_blocked)
                cover_data_url_b: str | None = None
                if cover_blocked:
                    cover_bytes_b, cover_ext_b = cover_blocked
                    cover_ext_b = _guess_image_extension(cover_bytes_b)
                    mime_b = _mime_from_extension(cover_ext_b)
                    cover_data_url_b = f"data:{mime_b};base64,{base64.b64encode(cover_bytes_b).decode('ascii')}"
                return {
                    "preview": True,
                    "importBlocked": True,
                    "importBlockedReason": (
                        f"Vượt giới hạn {MOD_EPUB_MAX_CHAPTERS} chương sau khi tách "
                        f"(phát hiện {n_ch} chương)."
                    ),
                    "fileName": file.filename or "upload.epub",
                    "splitMode": mode,
                    "detectedStructureType": "standard",
                    "hasCoverFromEpub": has_cover_b,
                    "coverPreviewDataUrl": cover_data_url_b,
                    "parserInfo": {
                        "splitMode": mode,
                        "chapterRegexUsed": pattern if mode == "regex" else None,
                        "chapterTagUsed": effective_tag if mode == "tag" else None,
                        "sourceSections": len(source_sections),
                        "sectionsAfterFilter": len(sections_after_filter),
                        "sectionsDroppedByFilter": max(0, len(source_sections) - len(sections_after_filter)),
                        "chaptersDetected": n_ch,
                        "chaptersFinal": n_ch,
                        "insertedMissingChapters": len([c for c in chapters if c.get("is_placeholder")]),
                        "detectedMaxChapterNumber": max([int(c.get("number") or 0) for c in chapters], default=0),
                        "detectedNumberAssignments": len([c for c in chapters if int(c.get("number") or 0) > 0]),
                        "policySkippedHeavyScan": True,
                    },
                    "novel": {
                        "title": base_title,
                        "authorName": base_author,
                        "description": base_desc,
                        "detectedGenres": inferred_genres,
                        "totalChapters": n_ch,
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
                    "sample": _chapter_preview_samples(chapters, sample_size=10),
                }
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Quá giới hạn {MOD_EPUB_MAX_CHAPTERS} chương sau khi tách "
                    f"(hiện {n_chapters} chương)."
                ),
            )

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
                    "chapterRegexUsed": pattern if mode == "regex" else None,
                    "chapterTagUsed": effective_tag if mode == "tag" else None,
                    "sourceSections": len(source_sections),
                    "sectionsAfterFilter": len(sections_after_filter),
                    "sectionsDroppedByFilter": max(0, len(source_sections) - len(sections_after_filter)),
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
                "sample": _chapter_preview_samples(chapters, sample_size=10),
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

        if existing_by_title and should_replace:
            novel_id = str(existing_by_title["id"])
            await db.execute(text('DELETE FROM "ChapterContentRef" WHERE "chapterId" IN (SELECT id FROM "ChapterMeta" WHERE "novelId" = :novel_id)'), {"novel_id": novel_id})
            await db.execute(text('DELETE FROM "ChapterMeta" WHERE "novelId" = :novel_id'), {"novel_id": novel_id})
            await db.execute(
                text('UPDATE "Novel" SET "authorName" = :author, description = :desc, "coverUrl" = COALESCE(:cover, "coverUrl"), "updatedAt" = NOW() WHERE id = :id'),
                {
                    "id": novel_id,
                    "author": base_author,
                    "desc": base_desc,
                    "cover": uploaded_cover_url,
                },
            )
            await db.execute(
                text('UPDATE "Novel" SET "originalTitle" = :original_title, "originalAuthorName" = :original_author, status = :status, "updatedAt" = NOW() WHERE id = :id'),
                {
                    "id": novel_id,
                    "original_title": base_original_title or None,
                    "original_author": base_original_author or None,
                    "status": base_status,
                },
            )
            if parsed_genre_ids:
                await _set_novel_genres(db, novel_id, parsed_genre_ids)
        else:
            novel_id = _new_id("n_")
            slug = await _ensure_unique_slug(db, table="Novel", slug=_norm_title(base_title).replace(" ", "-")[:120] or novel_id)
            await db.execute(
                text('INSERT INTO "Novel" (id, title, slug, "authorName", "originalTitle", "originalAuthorName", description, "coverUrl", status, "totalChapters", views, rating, "ratingCount", "bookmarkCount", "createdAt", "updatedAt") VALUES (:id,:title,:slug,:author,:original_title,:original_author,:desc,:cover,:status,0,0,0,0,0,NOW(),NOW())'),
                {
                    "id": novel_id,
                    "title": base_title,
                    "slug": slug,
                    "author": base_author,
                    "original_title": base_original_title or None,
                    "original_author": base_original_author or None,
                    "desc": base_desc,
                    "cover": uploaded_cover_url,
                    "status": base_status,
                },
            )
            if parsed_genre_ids:
                await _set_novel_genres(db, novel_id, parsed_genre_ids)

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
    db: AsyncSession = Depends(get_db_session),
):
    skip = (page - 1) * limit
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
        'n."updatedAt"'
    )
    base_from = (
        'FROM "Novel" n '
    )

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

    chapter_map = await _fetch_latest_chapter_map(db, novel_ids)

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
async def get_novel_detail(
    id_or_slug: str,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
):
    row = (
        await db.execute(
            text(
                'SELECT n.id, n.title, n.slug, n."originalTitle", n."authorName", n."originalAuthorName", '
                'n.description, n."coverUrl", n."coverColor", n.status, n."totalChapters", n.views, n.rating, '
                'n."ratingCount", n."bookmarkCount", n."createdAt", n."updatedAt" '
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

    user = await resolve_current_user(db, request)
    user_rating = None
    if user:
        user_rating = await _load_user_novel_rating(db, user["id"], row["id"])

    payload = {
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
        "genres": [dict(item) for item in genres],
        "createdAt": _iso(row["createdAt"]),
        "updatedAt": _iso(row["updatedAt"]),
    }
    if user_rating is not None:
        payload["userRating"] = user_rating
    return payload


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
                'SELECT n.id, n.title, n.slug, n."authorName", n."coverUrl" '
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
        }
        for row in rows
    ]


class RatePayload(BaseModel):
    score: float = Field(ge=1, le=10)


async def _ensure_novel_rating_table(db: AsyncSession) -> None:
    await db.execute(
        text(
            '''
            CREATE TABLE IF NOT EXISTS "NovelRating" (
                id TEXT PRIMARY KEY,
                "userId" TEXT NOT NULL,
                "novelId" TEXT NOT NULL,
                score DOUBLE PRECISION NOT NULL,
                "createdAt" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                "updatedAt" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE("userId", "novelId")
            )
            '''
        )
    )
    await db.execute(
        text('CREATE INDEX IF NOT EXISTS "NovelRating_novelId_idx" ON "NovelRating"("novelId")')
    )


async def _recalculate_novel_rating(db: AsyncSession, novel_id: str) -> tuple[float, int]:
    await _ensure_novel_rating_table(db)
    row = (
        await db.execute(
            text(
                'SELECT COALESCE(AVG(score), 0) AS avg_score, COUNT(*)::int AS cnt '
                'FROM "NovelRating" WHERE "novelId" = :novel_id'
            ),
            {"novel_id": novel_id},
        )
    ).mappings().first()
    avg_score = float(row["avg_score"] or 0)
    count = int(row["cnt"] or 0)
    await db.execute(
        text('UPDATE "Novel" SET rating = :rating, "ratingCount" = :count WHERE id = :novel_id'),
        {"rating": avg_score, "count": count, "novel_id": novel_id},
    )
    return avg_score, count


async def _load_user_novel_rating(db: AsyncSession, user_id: str, novel_id: str) -> float | None:
    try:
        await _ensure_novel_rating_table(db)
        row = (
            await db.execute(
                text(
                    'SELECT score FROM "NovelRating" WHERE "userId" = :user_id AND "novelId" = :novel_id LIMIT 1'
                ),
                {"user_id": user_id, "novel_id": novel_id},
            )
        ).mappings().first()
    except Exception:
        logger.exception("Failed to load user novel rating user=%s novel=%s", user_id, novel_id)
        return None
    if not row:
        return None
    return float(row["score"])


class ModGenrePayload(BaseModel):
    id: str | None = None
    name: str
    description: str | None = None
    icon: str | None = None


class ModGenreMergePayload(BaseModel):
    sourceId: str
    targetId: str


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


class ModNovelBulkPayload(BaseModel):
    action: str
    ids: list[str]


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


class ModNormalizeTitlesPreviewPayload(BaseModel):
    novelId: str
    overwriteGenericOnly: bool = True


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


_CHAPTER_HEADING_PREFIX = r"(?:chuong|ch\.?|chapter|hoi|quyen|phan|tap)"
_CHAPTER_WITH_SUBTITLE_RE = re.compile(
    rf"^{_CHAPTER_HEADING_PREFIX}\s*\d+(?:[\.:\-\)]\s*|\s+).+",
    re.IGNORECASE,
)
_CHAPTER_NUM_ONLY_RE = re.compile(
    rf"^{_CHAPTER_HEADING_PREFIX}\s*(\d+)\s*[:\-\.]?\s*$",
    re.IGNORECASE,
)
_CHAPTER_NUM_PREFIX_RE = re.compile(rf"^{_CHAPTER_HEADING_PREFIX}\s*(\d+)", re.IGNORECASE)
_CHAPTER_INLINE_SUBTITLE_RE = re.compile(
    r"^(?:Chương|Ch\.?|Chapter|Hồi|Quyển|Phần|Tập)\s*\d+(?:[\.:\-\)]\s*|\s+)(.+)$",
    re.IGNORECASE,
)


def _looks_like_body_paragraph(line: str) -> bool:
    s = line.strip()
    if not s:
        return True
    if len(s) > 200:
        return True
    if len(s) > 90 and s.endswith((".", "…", "!", "?", "。")):
        return True
    if len(s.split()) >= 10:
        return True
    low = s.lower()
    if re.match(r"^(đoàn|hắn|nàng|anh|cô|tôi|người|sau khi|khi đó|trong|ngoài|bên|cả|một|hai|ba)\s", low):
        if len(s.split()) >= 8:
            return True
    return False


def _is_plausible_subtitle_line(line: str) -> bool:
    s = line.strip()
    if not s or len(s) < 2 or len(s) > 160:
        return False
    normalized = _norm_title(s)
    if _CHAPTER_WITH_SUBTITLE_RE.match(normalized) or _CHAPTER_NUM_ONLY_RE.match(normalized):
        return False
    if _looks_like_body_paragraph(s):
        return False
    if re.search(r"https?://", s, re.IGNORECASE):
        return False
    return True


def _is_generic_chapter_title(title: str, number: int) -> bool:
    current = (title or "").strip()
    if not current:
        return True
    n = int(number or 0)
    if n <= 0:
        return False
    if re.fullmatch(rf"Chương\s*{n}\s*", current, re.IGNORECASE):
        return True
    if re.fullmatch(rf"Ch\.?\s*{n}\s*", current, re.IGNORECASE):
        return True
    if re.fullmatch(rf"Chapter\s*{n}\s*", current, re.IGNORECASE):
        return True
    return _norm_title(current) == _norm_title(f"Chương {n}")


def _infer_chapter_title_from_content(txt: str, number: int, fallback: str = "") -> str:
    lines = [line.strip().lstrip("#").strip() for line in (txt or "").splitlines() if line.strip()]

    for idx, line in enumerate(lines[:15]):
        normalized = _norm_title(line)
        if not normalized:
            continue

        inline = _CHAPTER_INLINE_SUBTITLE_RE.match(line.strip())
        if inline:
            subtitle = (inline.group(1) or "").strip()
            if subtitle:
                return subtitle

        if _CHAPTER_WITH_SUBTITLE_RE.match(normalized):
            return line.strip()

        if _CHAPTER_NUM_PREFIX_RE.match(normalized):
            if _CHAPTER_NUM_ONLY_RE.match(normalized):
                if idx + 1 < len(lines):
                    next_line = lines[idx + 1].strip()
                    if _is_plausible_subtitle_line(next_line):
                        return next_line
                return line.strip()
            return line.strip()

    if lines:
        first = lines[0].strip()
        if _is_plausible_subtitle_line(first):
            if "/" in (fallback or "") or str(fallback or "").lower().endswith(".xhtml"):
                return first
            if len(first.split()) >= 2:
                return first

    cleaned_fallback = (fallback or "").strip()
    if cleaned_fallback and "/" not in cleaned_fallback and not cleaned_fallback.lower().endswith(".xhtml"):
        if not _is_generic_chapter_title(cleaned_fallback, number):
            return cleaned_fallback
    return f"Chương {number}"


def _derive_chapter_title(txt: str, fallback: str, number: int) -> str:
    return _infer_chapter_title_from_content(txt, number, fallback)


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


def _resolve_epub_split_mode(split_mode: str | None) -> str:
    raw = (split_mode or "toc").strip().lower()
    if raw == "regex":
        return "regex"
    if raw in {"tag", "html_tag", "html-tag", "htmltag"}:
        return "tag"
    return "toc"


def _normalize_chapter_html_tag(tag: str | None) -> str:
    cleaned = (tag or "a").strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9]*", cleaned):
        raise HTTPException(
            status_code=400,
            detail="chapterTag must be a simple HTML tag name (letters/digits), e.g. a, h2",
        )
    return cleaned


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


def _epub_extract_with_mode(
    epub_path: Path,
    split_mode: str,
    chapter_start_pattern: str | None,
    chapter_tag: str | None = None,
) -> list[dict[str, Any]]:
    if split_mode == "regex":
        default_vi_regex = r"^\s*(?:[#>*\-\[]\s*)*(?:ch(?:u\.?|ương|uong)?|chapter|hồi|hoi|quyển|quyen|phần|phan|tập|tap)\s*\d+(?:[\.:\-\)]\s*|\s+).+$"
        effective_pattern = chapter_start_pattern or default_vi_regex
        try:
            return _normalize_chapter_sequence(_extract_epub_chapters_by_regex(epub_path, effective_pattern))
        except re.error as exc:
            raise HTTPException(status_code=400, detail=f"Invalid chapterStartPattern: {exc}") from exc
    if split_mode == "tag":
        from app.epub_parser import build_merged_html_from_epub, extract_chapters_by_html_tag

        effective_tag = _normalize_chapter_html_tag(chapter_tag)
        merged, tag_stats = extract_chapters_by_html_tag(epub_path, effective_tag)
        if not merged:
            merged_html = build_merged_html_from_epub(epub_path)
            tag_opens = int(tag_stats.get("tagOpens") or 0)
            if not merged_html.strip():
                detail = "EPUB không có nội dung HTML trong các file document."
            elif tag_opens == 0:
                detail = (
                    f"Không tìm thấy thẻ <{effective_tag}> trong EPUB. "
                    f"Thử thẻ khác (h2, h1, p) hoặc chế độ TOC/Regex."
                )
            else:
                filtered = int(tag_stats.get("tagOpensFiltered") or 0)
                extra = f" (đã lọc bỏ {filtered} thẻ <a> không giống mục chương)" if filtered else ""
                detail = (
                    f"Tìm thấy {tag_opens} thẻ <{effective_tag}>{extra} "
                    f"nhưng không tạo được chương có nội dung. Thử thẻ khác hoặc TOC/Regex."
                )
            raise HTTPException(status_code=400, detail=detail)
        return _normalize_chapter_sequence(merged)
    return _normalize_chapter_sequence(_extract_epub_chapters(epub_path))


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
    snippets: list[str] = []
    if chapters:
        picks = [chapters[0]]
        if len(chapters) > 2:
            picks.append(chapters[len(chapters) // 2])
        if len(chapters) > 1 and chapters[-1] not in picks:
            picks.append(chapters[-1])
        for ch in picks:
            text = str(ch.get("txt") or "")[:280].strip()
            if text:
                snippets.append(text)
    author_text = author or "Tác giả chưa rõ"
    context = snippets[0] if snippets else ""
    lines = [
        f"{title} của {author_text} mở ra một thế giới với nhịp kể chuyện cuốn hút, tập trung vào hành trình và lựa chọn của nhân vật chính.",
        "Bối cảnh được triển khai rõ nét qua từng chương, tạo cảm giác tiến triển liên tục và dễ theo dõi dài hơi.",
    ]
    if context:
        lines.append("Mạch truyện gợi mở nhiều xung đột và điểm nhấn cảm xúc, phù hợp độc giả thích khám phá dần cốt lõi câu chuyện.")
    else:
        lines.append("Nội dung phù hợp để đọc liên tục với mạch phát triển ổn định theo từng chương.")
    lines.append("Tác phẩm hướng tới trải nghiệm đọc mượt, có không khí riêng và tiềm năng giữ chân người đọc từ những chương đầu.")
    return "\n".join(lines)


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


@app.get("/api/mod/openrouter/models")
async def mod_openrouter_models(
    user: dict = Depends(require_current_user),
):
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")
    catalog = await openrouter.fetch_models_catalog()
    return {
        "free": catalog.get("free") or [],
        "paid": catalog.get("paid") or [],
        "selectedForSuggest": catalog.get("selectedForSuggest") or [],
        "cacheExpiresAt": catalog.get("cacheExpiresAt"),
    }


@app.post("/api/mod/epub/ai-suggest")
async def mod_epub_ai_suggest(
    file: UploadFile = File(...),
    splitMode: str | None = Form(default=None),
    chapterRegex: str | None = Form(default=None),
    chapterTag: str | None = Form(default=None),
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
        mode = _resolve_epub_split_mode(splitMode)
        pattern = (chapterRegex or "").strip() or None
        effective_tag = _normalize_chapter_html_tag(chapterTag) if mode == "tag" else None
        source_sections = _extract_epub_chapters(tmp_path)
        sections_after_filter = _filter_toc_chapters(source_sections) if mode == "toc" else source_sections
        chapters = _epub_extract_with_mode(tmp_path, mode, pattern, effective_tag)
        meta = _extract_epub_metadata(tmp_path)
        resolved_title = " ".join((title or str(meta.get("title") or tmp_path.stem)).split()).strip() or tmp_path.stem
        resolved_author = " ".join((authorName or str(meta.get("author") or "Unknown")).split()).strip() or "Unknown"
        existing_genres = [
            str(r.get("name") or "")
            for r in (await db.execute(text('SELECT name FROM "Genre" ORDER BY name ASC'))).mappings().all()
            if str(r.get("name") or "").strip()
        ]

        ai_result = await openrouter.ai_suggest_epub(
            resolved_title,
            resolved_author,
            chapters,
            existing_genres,
            genre_hints=list(meta.get("genres") or []),
            map_genres=lambda candidates, existing: _map_genres_to_existing(candidates, existing, limit=6),
        )
        if ai_result:
            return {
                "suggestedGenres": ai_result["suggestedGenres"][:6],
                "shortDescription": ai_result["shortDescription"],
                "confidence": ai_result["confidence"],
                "source": "openrouter",
                "model": ai_result.get("model"),
                "modelTier": ai_result.get("modelTier"),
                "suggestedStatus": ai_result.get("suggestedStatus") or "Đang ra",
            }

        fallback_genres = _map_genres_to_existing(_build_ai_genre_suggestions(chapters), existing_genres, limit=6)
        fallback_desc = _build_ai_description(resolved_title, resolved_author, chapters)
        return {
            "suggestedGenres": fallback_genres[:6],
            "shortDescription": fallback_desc,
            "confidence": 0.62,
            "source": "rule_based_fallback",
            "suggestedStatus": "Đang ra",
        }
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass


@app.get("/api/truyen/{novel_id}/rate")
async def get_user_novel_rating(
    novel_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
):
    user = await require_current_user(request, db)
    novel_exists = (
        await db.execute(text('SELECT id FROM "Novel" WHERE id = :novel_id LIMIT 1'), {"novel_id": novel_id})
    ).mappings().first()
    if not novel_exists:
        raise HTTPException(status_code=404, detail="Novel not found")

    await _ensure_novel_rating_table(db)
    user_rating = await _load_user_novel_rating(db, user["id"], novel_id)
    return {"userRating": user_rating}


@app.post("/api/truyen/{novel_id}/rate")
async def rate_novel(
    novel_id: str,
    payload: RatePayload,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
):
    user = await require_current_user(request, db)

    novel_exists = (
        await db.execute(text('SELECT id FROM "Novel" WHERE id = :novel_id LIMIT 1'), {"novel_id": novel_id})
    ).mappings().first()
    if not novel_exists:
        raise HTTPException(status_code=404, detail="Novel not found")

    await _ensure_novel_rating_table(db)
    existing = (
        await db.execute(
            text(
                'SELECT id FROM "NovelRating" WHERE "userId" = :user_id AND "novelId" = :novel_id LIMIT 1'
            ),
            {"user_id": user["id"], "novel_id": novel_id},
        )
    ).mappings().first()

    if existing:
        await db.execute(
            text(
                'UPDATE "NovelRating" SET score = :score, "updatedAt" = NOW() WHERE id = :rating_id'
            ),
            {"score": payload.score, "rating_id": existing["id"]},
        )
    else:
        await db.execute(
            text(
                'INSERT INTO "NovelRating"(id, "userId", "novelId", score, "createdAt", "updatedAt") '
                'VALUES (:id, :user_id, :novel_id, :score, NOW(), NOW())'
            ),
            {
                "id": _new_id("nr_"),
                "user_id": user["id"],
                "novel_id": novel_id,
                "score": payload.score,
            },
        )

    avg_rating, rating_count = await _recalculate_novel_rating(db, novel_id)
    await db.commit()

    return {
        "rating": avg_rating,
        "ratingCount": rating_count,
        "userRating": payload.score,
    }



@app.get("/api/user/bookmarks")
async def list_bookmarks(
    request: Request,
    shelfStatus: str | None = None,
    db: AsyncSession = Depends(get_db_session),
):
    user = await require_current_user(request, db)

    rows = (
        await db.execute(
            text(
                'SELECT b.id, b."novelId", b."lastChapterId", b."lastChapterNumber", b."readChapters", b."markedAsRead", '
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

    bookmarks = [_serialize_bookmark_row(row) for row in rows]
    status_filter = (shelfStatus or "").strip().lower()
    if status_filter in {"reading", "completed", "saved"}:
        bookmarks = [bookmark for bookmark in bookmarks if bookmark["shelfStatus"] == status_filter]
    return bookmarks


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

    if action == "markAsRead":
        if existing:
            await db.execute(
                text('UPDATE "Bookmark" SET "markedAsRead" = true WHERE id = :bookmark_id'),
                {"bookmark_id": existing["id"]},
            )
        else:
            await db.execute(
                text(
                    'INSERT INTO "Bookmark"(id, "userId", "novelId", "readChapters", "hasCountedView", "markedAsRead", "createdAt") '
                    'VALUES (:id, :user_id, :novel_id, :read_chapters, false, true, NOW())'
                ),
                {
                    "id": _new_id("bm_"),
                    "user_id": user["id"],
                    "novel_id": payload.novelId,
                    "read_chapters": [],
                },
            )
            await db.execute(
                text('UPDATE "Novel" SET "bookmarkCount" = "bookmarkCount" + 1 WHERE id = :novel_id'),
                {"novel_id": payload.novelId},
            )
        await db.commit()
        bookmark = await _load_bookmark_with_novel(db, user["id"], payload.novelId)
        return {"status": "marked", "bookmark": bookmark}

    if action == "unmarkAsRead":
        if not existing:
            raise HTTPException(status_code=404, detail="Bookmark not found")
        await db.execute(
            text('UPDATE "Bookmark" SET "markedAsRead" = false WHERE id = :bookmark_id'),
            {"bookmark_id": existing["id"]},
        )
        await db.commit()
        bookmark = await _load_bookmark_with_novel(db, user["id"], payload.novelId)
        return {"status": "unmarked", "bookmark": bookmark}

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


class MobileLoginPayload(BaseModel):
    googleIdToken: str


@app.post("/api/auth/mobile-login")
async def mobile_login(payload: MobileLoginPayload, db: AsyncSession = Depends(get_db_session)):
    if not payload.googleIdToken.strip():
        raise HTTPException(status_code=400, detail="googleIdToken is required")

    id_info = verify_google_id_token(payload.googleIdToken)

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
