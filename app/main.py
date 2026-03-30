from __future__ import annotations

import datetime as dt
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

from app.auth import create_access_token, require_current_user
from app.routers import mod
from app.config import settings
from app.database import get_db_session, mongo_client, mongo_db


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


@app.get("/api/genres")
async def list_genres(db: AsyncSession = Depends(get_db_session)):
    result = await db.execute(
        text(
            'SELECT g.id, g.name, g.slug, g.description, g.icon, COUNT(ng."novelId")::int AS "novelCount" '
            'FROM "Genre" g '
            'LEFT JOIN "NovelGenre" ng ON ng."genreId" = g.id '
            'GROUP BY g.id '
            'ORDER BY g.name ASC'
        )
    )
    return [dict(row) for row in result.mappings().all()]


@app.get("/api/novels/browse")
async def browse_novels(
    q: str = "",
    genre: str = "",
    status: str = "",
    sort: str = "latest",
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db_session),
):
    skip = (page - 1) * limit
    order_clause = {
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

    total_count = (
        await db.execute(
            text(
                f'SELECT COUNT(*)::int FROM "Novel" n '
                f'LEFT JOIN "Series" s ON s.id = n."seriesId" '
                f'{where_sql}'
            ),
            params,
        )
    ).scalar_one()

    rows = (
        await db.execute(
            text(
                f'SELECT n.id, n.title, n.slug, n."originalTitle", n."authorName", n."coverUrl", n."coverColor", '
                f'n.status, n."totalChapters", n.views, n.rating, n."ratingCount", n."bookmarkCount", '
                f'n."seriesId", s.id AS series_id, s.name AS series_name, s.slug AS series_slug, n."updatedAt" '
                f'FROM "Novel" n '
                f'LEFT JOIN "Series" s ON s.id = n."seriesId" '
                f'{where_sql} '
                f'ORDER BY {order_clause} '
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
    if novel_ids:
        latest_chapters = await mongo_db["chapters"].aggregate(
            [
                {"$match": {"novelId": {"$in": novel_ids}}},
                {"$sort": {"novelId": 1, "number": -1}},
                {
                    "$group": {
                        "_id": "$novelId",
                        "latestChapterNumber": {"$first": "$number"},
                        "latestChapterTitle": {"$first": "$title"},
                        "latestChapterAt": {"$first": "$createdAt"},
                    }
                },
            ]
        ).to_list(length=None)
        chapter_map = {
            item["_id"]: {
                "number": item.get("latestChapterNumber"),
                "title": item.get("latestChapterTitle"),
                "createdAt": _iso(item.get("latestChapterAt")),
            }
            for item in latest_chapters
        }

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
        .find({"novelId": novel_id}, {"title": 1, "number": 1, "createdAt": 1, "volumeNumber": 1, "volumeTitle": 1, "volumeChapterNumber": 1})
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
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=20, ge=1, le=50),
    db: AsyncSession = Depends(get_db_session),
):
    skip = (page - 1) * limit
    if chapterId:
        where_sql = 'c."novelId" = :novel_id AND c."chapterId" = :chapter_id'
        params = {"novel_id": novel_id, "chapter_id": chapterId, "skip": skip, "limit": limit}
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
        "expiresIn": 3600,
        "user": {
            "id": user["id"],
            "email": user.get("email"),
            "name": user.get("name"),
            "image": user.get("image"),
            "role": user.get("role", "USER"),
        },
    }
