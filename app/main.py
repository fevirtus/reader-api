from __future__ import annotations

import datetime as dt
import random
import secrets
import uuid
from contextlib import asynccontextmanager
from typing import Any

from bson import ObjectId
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import ACCESS_TOKEN_TTL_SECONDS, create_access_token, require_current_user
from app.routers import mod
from app.config import settings
from app.database import get_db_session, mongo_client, mongo_db


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    mongo_client.close()


app = FastAPI(title=settings.app_name, lifespan=lifespan)

app.include_router(mod.router)

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
    editor_docs = await mongo_db["editorrecommendations"].find({}).sort("createdAt", -1).limit(2000).to_list(2000)
    user_docs = await mongo_db["userrecommendations"].find({}).sort("createdAt", -1).limit(5000).to_list(5000)

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
    recent_chapters = await mongo_db["chapters"].find(
        {},
        {"novelId": 1, "number": 1, "title": 1, "createdAt": 1},
    ).sort("_id", -1).limit(400).to_list(400)

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
    mongo_ok = False

    try:
        await db.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False

    try:
        await mongo_db.command("ping")
        mongo_ok = True
    except Exception:
        mongo_ok = False

    status = "ok" if db_ok and mongo_ok else "degraded"
    return {
        "status": status,
        "service": settings.app_name,
        "environment": settings.app_env,
        "checks": {"postgres": db_ok, "mongodb": mongo_ok},
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
):
    skip = (page - 1) * limit
    chapters_cursor = (
        mongo_db["chapters"]
        .find({"novelId": novel_id}, {"title": 1, "number": 1, "createdAt": 1, "views": 1, "volumeNumber": 1, "volumeTitle": 1, "volumeChapterNumber": 1})
        .sort("number", 1)
        .skip(skip)
        .limit(limit)
    )
    chapters = await chapters_cursor.to_list(length=limit)
    total_chapters = await mongo_db["chapters"].count_documents({"novelId": novel_id})

    return {
        "chapters": [
            {
                "id": str(item.get("_id")),
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
async def get_chapter_by_number(novel_id: str, chapter_number: int):
    chapter = await mongo_db["chapters"].find_one({"novelId": novel_id, "number": chapter_number})
    if not chapter:
        raise HTTPException(status_code=404, detail="Chapter not found")

    prev_chapter = await mongo_db["chapters"].find_one(
        {"novelId": novel_id, "number": chapter_number - 1},
        {"number": 1},
    )
    next_chapter = await mongo_db["chapters"].find_one(
        {"novelId": novel_id, "number": chapter_number + 1},
        {"number": 1},
    )
    max_chapter = await mongo_db["chapters"].count_documents({"novelId": novel_id})

    await mongo_db["chapters"].update_one({"_id": chapter["_id"]}, {"$inc": {"views": 1}})

    return {
        "id": str(chapter.get("_id")),
        "novelId": chapter.get("novelId"),
        "number": chapter.get("number"),
        "title": chapter.get("title"),
        "content": chapter.get("content"),
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
async def get_chapter_detail(chapter_id: str):
    try:
        object_id = ObjectId(chapter_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid chapter id") from exc

    chapter = await mongo_db["chapters"].find_one({"_id": object_id})
    if not chapter:
        raise HTTPException(status_code=404, detail="Chapter not found")

    prev_chapter = await mongo_db["chapters"].find_one(
        {"novelId": chapter.get("novelId"), "number": chapter.get("number", 0) - 1},
        {"number": 1},
    )
    next_chapter = await mongo_db["chapters"].find_one(
        {"novelId": chapter.get("novelId"), "number": chapter.get("number", 0) + 1},
        {"number": 1},
    )

    return {
        "id": str(chapter.get("_id")),
        "novelId": chapter.get("novelId"),
        "number": chapter.get("number"),
        "title": chapter.get("title"),
        "content": chapter.get("content"),
        "views": chapter.get("views", 0),
        "volumeNumber": chapter.get("volumeNumber"),
        "volumeTitle": chapter.get("volumeTitle"),
        "volumeChapterNumber": chapter.get("volumeChapterNumber"),
        "createdAt": _iso(chapter.get("createdAt")),
        "prevChapterId": str(prev_chapter.get("_id")) if prev_chapter else None,
        "prevChapterNumber": prev_chapter.get("number") if prev_chapter else None,
        "nextChapterId": str(next_chapter.get("_id")) if next_chapter else None,
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
    user = await require_current_user(db, request)
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
    user = await require_current_user(db, request)

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
    user = await require_current_user(db, request)
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
    user = await require_current_user(db, request)
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
    user = await require_current_user(db, request)
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
    user = await require_current_user(db, request)
    return {
        "id": user["id"],
        "email": user.get("email"),
        "name": user.get("name"),
        "image": user.get("image"),
        "role": user.get("role", "USER"),
    }


@app.get("/api/user/settings")
async def get_user_settings(request: Request, db: AsyncSession = Depends(get_db_session)):
    user = await require_current_user(db, request)
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
    user = await require_current_user(db, request)

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
    user = await require_current_user(db, request)

    docs = (
        await mongo_db["userrecommendations"]
        .find({"userId": user["id"]})
        .sort("createdAt", -1)
        .limit(1000)
        .to_list(length=1000)
    )

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
                "id": str(doc.get("_id")),
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
    user = await require_current_user(db, request)

    novel_exists = (
        await db.execute(
            text('SELECT id FROM "Novel" WHERE id = :novel_id LIMIT 1'),
            {"novel_id": payload.novelId},
        )
    ).scalar_one_or_none()
    if not novel_exists:
        raise HTTPException(status_code=404, detail="Truyện không tồn tại")

    existing = await mongo_db["userrecommendations"].find_one(
        {"userId": user["id"], "novelId": payload.novelId}, {"_id": 1}
    )
    if existing:
        raise HTTPException(status_code=409, detail="Bạn đã đề cử truyện này rồi")

    now = dt.datetime.now(dt.timezone.utc)
    result = await mongo_db["userrecommendations"].insert_one(
        {
            "userId": user["id"],
            "novelId": payload.novelId,
            "createdAt": now,
            "updatedAt": now,
        }
    )

    await db.execute(
        text('UPDATE "Novel" SET "bookmarkCount" = "bookmarkCount" + 1 WHERE id = :novel_id'),
        {"novel_id": payload.novelId},
    )
    await db.commit()

    return {"id": str(result.inserted_id), "novelId": payload.novelId}


@app.delete("/api/user/recommendations")
async def delete_recommendation(
    request: Request,
    novelId: str = Query(...),
    db: AsyncSession = Depends(get_db_session),
):
    user = await require_current_user(db, request)

    existing = await mongo_db["userrecommendations"].find_one(
        {"userId": user["id"], "novelId": novelId}, {"_id": 1}
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Bạn chưa đề cử truyện này")

    await mongo_db["userrecommendations"].delete_one({"_id": existing["_id"]})
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
    user = await require_current_user(db, request)
    return {
        "user": {
            "id": user["id"],
            "email": user.get("email"),
            "name": user.get("name"),
            "image": user.get("image"),
            "role": user.get("role", "USER"),
        }
    }
