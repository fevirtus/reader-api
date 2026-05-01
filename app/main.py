from __future__ import annotations

import datetime as dt
import hashlib
import random
import secrets
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
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
            "volumeNumber" INT,
            "volumeTitle" TEXT,
            "volumeChapterNumber" INT,
            "createdAt" TIMESTAMPTZ,
            UNIQUE("novelId", number)
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


@app.get("/api/home")
async def get_home_data(db: AsyncSession = Depends(get_db_session)):
    today = dt.datetime.now(dt.timezone.utc).date()
    week_start = today - dt.timedelta(days=7)
    month_start = today - dt.timedelta(days=30)

    weekly_raw = await _fetch_home_ranking_rows(db, since=week_start, take=600)
    monthly_raw = await _fetch_home_ranking_rows(db, since=month_start, take=600)
    all_time_raw = await _fetch_home_ranking_rows(db, take=800)
    popular_fallback = await _fetch_home_popular_fallback(db, take=400)
    random_pool = await _fetch_home_random_pool(db, take=420)
    top_recommendations, editor_recommendations = await _fetch_home_manual_recommendations(db)
    recent_comments = await _fetch_home_recent_comments(db, take=10)
    latest_novels = await _fetch_home_latest_novels(db, take=5)

    weekly_ranking = _fill_unique_rows(_collapse_series_rows(weekly_raw), _collapse_series_rows(popular_fallback), 5)
    monthly_ranking = _fill_unique_rows(_collapse_series_rows(monthly_raw), _collapse_series_rows(popular_fallback), 5)
    all_time_ranking = _fill_unique_rows(_collapse_series_rows(all_time_raw), _collapse_series_rows(popular_fallback), 5)

    hot_primary = [_to_hot_slide(item, "week") for item in weekly_ranking[:5]] + [_to_hot_slide(item, "month") for item in monthly_ranking[:5]]
    hot_fallback = [_to_hot_slide(item, "all") for item in all_time_ranking[:8]]
    hot_slides = _fill_unique_rows(hot_primary, hot_fallback, 10)

    used_hot_ids = {item["id"] for item in hot_slides}
    random_candidates = [item for item in _collapse_series_rows(_shuffle_rows(random_pool)) if item["id"] not in used_hot_ids]
    random_novels = _fill_unique_rows(random_candidates, _shuffle_rows(random_pool), 12)

    return {
        "hotSlides": hot_slides,
        "randomNovels": random_novels,
        "recommendedByCountItems": top_recommendations,
        "editorRecommendedItems": editor_recommendations,
        "weeklyRanking": weekly_ranking,
        "monthlyRanking": monthly_ranking,
        "allTimeRanking": all_time_ranking,
        "latestNovels": latest_novels,
        "recentComments": recent_comments,
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
            'OR n."originalAuthorName" ILIKE :q OR s.name ILIKE :q)'
        )

    where_sql = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""

    base_select = (
        'n.id, n.title, n.slug, n."originalTitle", n."authorName", n."coverUrl", n."coverColor", '
        'n.status, n."totalChapters", n.views, n.rating, n."ratingCount", n."bookmarkCount", '
        'n."seriesId", s.id AS series_id, s.name AS series_name, s.slug AS series_slug, n."updatedAt"'
    )
    base_from = (
        'FROM "Novel" n '
        'LEFT JOIN "Series" s ON s.id = n."seriesId" '
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
                's.id AS series_id, s.name AS series_name, s.slug AS series_slug '
                'FROM "Novel" n '
                'LEFT JOIN "Series" s ON s.id = n."seriesId" '
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
                'SELECT id, number, title, views, "volumeNumber", "volumeTitle", "volumeChapterNumber", "createdAt" '
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
                "volumeNumber": item.get("volumeNumber"),
                "volumeTitle": item.get("volumeTitle"),
                "volumeChapterNumber": item.get("volumeChapterNumber"),
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
                'SELECT id, "novelId", number, title, views, "volumeNumber", "volumeTitle", "volumeChapterNumber", "createdAt" '
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
    content = await _resolve_chapter_content(chapter_id, None, db)

    return {
        "id": str(chapter.get("id")),
        "novelId": chapter.get("novelId"),
        "number": chapter.get("number"),
        "title": chapter.get("title"),
        "content": content,
        "views": int(chapter.get("views") or 0) + 1,
        "volumeNumber": chapter.get("volumeNumber"),
        "volumeTitle": chapter.get("volumeTitle"),
        "volumeChapterNumber": chapter.get("volumeChapterNumber"),
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
                'SELECT id, "novelId", number, title, views, "volumeNumber", "volumeTitle", "volumeChapterNumber", "createdAt" '
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

    content = await _resolve_chapter_content(chapter_id, None, db)

    return {
        "id": str(chapter.get("id")),
        "novelId": chapter.get("novelId"),
        "number": chapter.get("number"),
        "title": chapter.get("title"),
        "content": content,
        "views": chapter.get("views", 0),
        "volumeNumber": chapter.get("volumeNumber"),
        "volumeTitle": chapter.get("volumeTitle"),
        "volumeChapterNumber": chapter.get("volumeChapterNumber"),
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
                'SELECT n.id, n.title, n.slug, n."authorName", n."coverUrl", s.id AS series_id, s.name AS series_name '
                'FROM "Novel" n '
                'LEFT JOIN "Series" s ON s.id = n."seriesId" '
                'WHERE n.title ILIKE :q OR n."authorName" ILIKE :q OR s.name ILIKE :q '
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


class SourceAssetUpsertPayload(BaseModel):
    path: str
    sha256: str
    opfIdentifier: str | None = None
    title: str | None = None
    author: str | None = None


def _asset_file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _extract_epub_chapters(epub_path: Path) -> list[dict[str, Any]]:
    from app.epub_parser import build_chapters_from_epub

    extracted = build_chapters_from_epub(epub_path)

    chapters: list[dict[str, Any]] = []
    for idx, ch in enumerate(extracted, start=1):
        content = str(ch.get("content") or "")
        if not content.strip():
            continue
        chapters.append(
            {
                "number": int(ch.get("number") or idx),
                "title": str(ch.get("title") or f"Chapter {idx}"),
                "raw_html": content,
                "txt": str(ch.get("txt") or "").strip(),
            }
        )
    return chapters


async def _resolve_chapter_content(chapter_id: str, mongo_fallback: str | None, db: AsyncSession) -> str | None:
    ref_row = (
        await db.execute(
            text('SELECT "txtHref" FROM "ChapterContentRef" WHERE "chapterId" = :chapter_id LIMIT 1'),
            {"chapter_id": chapter_id},
        )
    ).mappings().first()

    if settings.chapter_content_mode == "mongo_first":
        if mongo_fallback:
            return mongo_fallback
        if ref_row:
            try:
                return storage.read_text(ref_row["txtHref"])
            except Exception:
                return mongo_fallback
        return mongo_fallback

    if ref_row:
        try:
            return storage.read_text(ref_row["txtHref"])
        except Exception:
            return mongo_fallback
    return mongo_fallback


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
    return [dict(r) for r in rows]


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


@app.post("/api/import/discover")
async def discover_epub_assets(
    limit: int = Query(default=200, ge=1, le=2000),
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_current_user),
):
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden")

    root = Path(settings.epub_source_root)
    if not root.exists():
        raise HTTPException(status_code=400, detail=f"EPUB source root not found: {settings.epub_source_root}")

    found = sorted(root.rglob("*.epub"))[:limit]
    discovered = 0
    updated = 0

    for epub_path in found:
        sha256 = _asset_file_sha256(epub_path)
        rel_path = str(epub_path.relative_to(root))
        existing = (
            await db.execute(text('SELECT id FROM "SourceAsset" WHERE sha256 = :sha LIMIT 1'), {"sha": sha256})
        ).mappings().first()

        if existing:
            await db.execute(
                text('UPDATE "SourceAsset" SET path = :path, "updatedAt" = NOW() WHERE id = :id'),
                {"id": existing["id"], "path": rel_path},
            )
            updated += 1
            continue

        await db.execute(
            text(
                'INSERT INTO "SourceAsset" (id, path, sha256, status) VALUES (:id, :path, :sha, :status)'
            ),
            {"id": _new_id("asset_"), "path": rel_path, "sha": sha256, "status": "discovered"},
        )
        discovered += 1

    await db.commit()
    return {"scanned": len(found), "discovered": discovered, "updated": updated}


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

    docs = (
        await db.execute(
            text(
                'SELECT id, "novelId", "createdAt" FROM "UserRecommendationDoc" '
                'WHERE "userId" = :user_id ORDER BY "createdAt" DESC LIMIT 1000'
            ),
            {"user_id": user["id"]},
        )
    ).mappings().all()

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

    existing = (
        await db.execute(
            text('SELECT id FROM "UserRecommendationDoc" WHERE "userId" = :user_id AND "novelId" = :novel_id LIMIT 1'),
            {"user_id": user["id"], "novel_id": payload.novelId},
        )
    ).mappings().first()
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

    existing = (
        await db.execute(
            text('SELECT id FROM "UserRecommendationDoc" WHERE "userId" = :user_id AND "novelId" = :novel_id LIMIT 1'),
            {"user_id": user["id"], "novel_id": novelId},
        )
    ).mappings().first()
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
