"""MOD panel API routes — all require MOD or ADMIN role."""
from __future__ import annotations

import datetime as dt
import io
import mimetypes
import os
import re
import tempfile
import uuid
from typing import Any

import boto3
import ebooklib
import html2text
import httpx
from bson import ObjectId
from ebooklib import epub as epublib
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from pymongo import UpdateOne
from pydantic import BaseModel
from slugify import slugify
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_mod_user
from app.config import settings
from app.database import get_db_session, mongo_db

router = APIRouter(prefix="/api", tags=["mod"])

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MAX_RECOMMENDATIONS_PER_EDITOR = 5


def _to_iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone.utc)
        return value.isoformat()
    if isinstance(value, dt.date):
        return dt.datetime.combine(value, dt.time(0, 0, tzinfo=dt.timezone.utc)).isoformat()
    return str(value)


def _new_cuid() -> str:
    return "c" + uuid.uuid4().hex[:24]


def _r2_client():
    return boto3.client(
        "s3",
        endpoint_url=f"https://{settings.r2_account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=settings.r2_access_key_id,
        aws_secret_access_key=settings.r2_secret_access_key,
        region_name="auto",
    )


def _r2_key_from_url(url: str | None) -> str | None:
    if not url:
        return None
    base = (settings.r2_public_base_url or "").rstrip("/")
    if base and url.startswith(base + "/"):
        return url[len(base) + 1 :]
    return None


async def _upload_to_r2(data: bytes, content_type: str, key_prefix: str = "covers", file_hint: str = "") -> str:
    ext = mimetypes.guess_extension(content_type.split(";")[0].strip()) or ""
    if not ext and file_hint:
        ext = os.path.splitext(file_hint)[1]
    key = f"{key_prefix}/{uuid.uuid4().hex}{ext}"

    def _sync():
        _r2_client().put_object(
            Bucket=settings.r2_bucket_name,
            Key=key,
            Body=data,
            ContentType=content_type,
        )

    import asyncio

    await asyncio.get_event_loop().run_in_executor(None, _sync)
    base = (settings.r2_public_base_url or "").rstrip("/")
    return f"{base}/{key}"


async def _delete_from_r2(url: str | None) -> bool:
    key = _r2_key_from_url(url)
    if not key:
        return False

    def _sync():
        _r2_client().delete_object(Bucket=settings.r2_bucket_name, Key=key)

    import asyncio

    await asyncio.get_event_loop().run_in_executor(None, _sync)
    return True


async def _sync_total_chapters(db: AsyncSession, novel_id: str) -> int:
    count_result = await mongo_db.chapters.count_documents({"novelId": novel_id})
    await db.execute(
        text('UPDATE "Novel" SET "totalChapters" = :count WHERE id = :id'),
        {"count": count_result, "id": novel_id},
    )
    await db.commit()
    return count_result


def _slugify_unique(base: str) -> str:
    return slugify(base, allow_unicode=False) or base.lower().replace(" ", "-")


def _series_row_to_dict(row) -> dict:
    d = dict(row.mappings() if hasattr(row, "mappings") else row)
    return d


def _is_admin(user: dict) -> bool:
    return user.get("role") == "ADMIN"


def _novel_ownership_filter(user: dict) -> str:
    """SQL fragment for ownership check."""
    if _is_admin(user):
        return ""  # no extra condition
    return 'AND (n."uploaderId" = :user_id OR n."uploaderId" IS NULL)'


# ---------------------------------------------------------------------------
# DEV route
# ---------------------------------------------------------------------------


@router.get("/dev/make-mod")
async def dev_make_mod(
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_mod_user),
):
    if settings.app_env == "production":
        raise HTTPException(status_code=404, detail="Not found")
    result = await db.execute(
        text('UPDATE "User" SET role = \'MOD\' WHERE email = :email RETURNING id, email, role'),
        {"email": user["email"]},
    )
    await db.commit()
    row = result.mappings().first()
    return dict(row) if row else {}


# ---------------------------------------------------------------------------
# /api/mod/the-loai (genres)
# ---------------------------------------------------------------------------


@router.get("/mod/the-loai")
async def mod_list_genres(
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_mod_user),
):
    result = await db.execute(text('SELECT id, name, slug, description, icon FROM "Genre" ORDER BY name ASC'))
    return [dict(r) for r in result.mappings()]


class GenreCreateBody(BaseModel):
    name: str
    description: str | None = None


@router.post("/mod/the-loai", status_code=201)
async def mod_create_genre(
    body: GenreCreateBody,
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_mod_user),
):
    slug = _slugify_unique(body.name)
    try:
        result = await db.execute(
            text(
                'INSERT INTO "Genre" (id, name, slug, description) VALUES (:id, :name, :slug, :desc) '
                "RETURNING id, name, slug, description"
            ),
            {"id": _new_cuid(), "name": body.name, "slug": slug, "desc": body.description},
        )
        await db.commit()
        row = result.mappings().first()
        if not row:
            raise HTTPException(status_code=500, detail="Không thể tạo thể loại")
        return dict(row)
    except Exception as e:
        await db.rollback()
        if "unique" in str(e).lower() or "23505" in str(e):
            raise HTTPException(status_code=400, detail="Thể loại đã tồn tại")
        raise


@router.delete("/mod/the-loai")
async def mod_delete_genre(
    id: str = Query(...),
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_mod_user),
):
    await db.execute(text('DELETE FROM "Genre" WHERE id = :id'), {"id": id})
    await db.commit()
    return {"success": True}


# ---------------------------------------------------------------------------
# /api/mod/series
# ---------------------------------------------------------------------------


@router.get("/mod/series")
async def mod_list_series(
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_mod_user),
):
    if _is_admin(user):
        result = await db.execute(
            text(
                'SELECT s.id, s.name, s.slug, s.description, COUNT(n.id) AS "novelCount" '
                'FROM "Series" s LEFT JOIN "Novel" n ON n."seriesId" = s.id '
                "GROUP BY s.id ORDER BY s.name ASC"
            )
        )
    else:
        result = await db.execute(
            text(
                'SELECT s.id, s.name, s.slug, s.description, COUNT(n.id) AS "novelCount" '
                'FROM "Series" s LEFT JOIN "Novel" n ON n."seriesId" = s.id '
                'WHERE EXISTS (SELECT 1 FROM "Novel" n2 WHERE n2."seriesId" = s.id '
                'AND (n2."uploaderId" = :uid OR n2."uploaderId" IS NULL)) '
                "GROUP BY s.id ORDER BY s.name ASC"
            ),
            {"uid": user["id"]},
        )
    return [dict(r) for r in result.mappings()]


class SeriesBody(BaseModel):
    name: str
    description: str | None = None


@router.post("/mod/series", status_code=201)
async def mod_create_series(
    body: SeriesBody,
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_mod_user),
):
    # Idempotent: return existing if same name
    existing = await db.execute(
        text('SELECT id, name, slug, description FROM "Series" WHERE LOWER(name) = LOWER(:name) LIMIT 1'),
        {"name": body.name},
    )
    row = existing.mappings().first()
    if row:
        return dict(row)

    slug = _slugify_unique(body.name)
    # Ensure slug uniqueness
    count_result = await db.execute(
        text('SELECT COUNT(*) FROM "Series" WHERE slug LIKE :pat'), {"pat": f"{slug}%"}
    )
    count = count_result.scalar() or 0
    if count > 0:
        slug = f"{slug}-{int(count) + 1}"

    result = await db.execute(
        text(
            'INSERT INTO "Series" (id, name, slug, description, "createdAt", "updatedAt") '
            "VALUES (:id, :name, :slug, :desc, NOW(), NOW()) RETURNING id, name, slug, description"
        ),
        {"id": _new_cuid(), "name": body.name, "slug": slug, "desc": body.description},
    )
    await db.commit()
    row = result.mappings().first()
    if not row:
        raise HTTPException(status_code=500, detail="Không thể tạo series")
    return dict(row)


class SeriesUpdateBody(BaseModel):
    id: str
    name: str
    description: str | None = None


@router.put("/mod/series")
async def mod_update_series(
    body: SeriesUpdateBody,
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_mod_user),
):
    dup = await db.execute(
        text('SELECT id FROM "Series" WHERE LOWER(name) = LOWER(:name) AND id != :id LIMIT 1'),
        {"name": body.name, "id": body.id},
    )
    if dup.mappings().first():
        raise HTTPException(status_code=400, detail="Tên series đã tồn tại")
    slug = _slugify_unique(body.name)
    result = await db.execute(
        text(
            'UPDATE "Series" SET name = :name, slug = :slug, description = :desc, "updatedAt" = NOW() '
            "WHERE id = :id RETURNING id, name, slug, description"
        ),
        {"name": body.name, "slug": slug, "desc": body.description, "id": body.id},
    )
    await db.commit()
    row = result.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Không tìm thấy series")
    return dict(row)


@router.delete("/mod/series")
async def mod_delete_series(
    id: str = Query(...),
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_mod_user),
):
    count_result = await db.execute(
        text('SELECT COUNT(*) FROM "Novel" WHERE "seriesId" = :id'), {"id": id}
    )
    if (count_result.scalar() or 0) > 0:
        raise HTTPException(status_code=409, detail="Series đang có truyện, không thể xóa")
    await db.execute(text('DELETE FROM "Series" WHERE id = :id'), {"id": id})
    await db.commit()
    return {"success": True}


# ---------------------------------------------------------------------------
# Helpers for novel routes
# ---------------------------------------------------------------------------


async def _resolve_series_id(
    db: AsyncSession,
    series_id_input: str | None,
    series_name_input: str | None,
    user: dict,
) -> str | None:
    if series_id_input:
        if not _is_admin(user):
            owns = await db.execute(
                text(
                    'SELECT 1 FROM "Series" s WHERE s.id = :sid AND EXISTS '
                    '(SELECT 1 FROM "Novel" n WHERE n."seriesId" = s.id AND (n."uploaderId" = :uid OR n."uploaderId" IS NULL))'
                ),
                {"sid": series_id_input, "uid": user["id"]},
            )
            if not owns.first():
                raise HTTPException(status_code=403, detail="Không có quyền dùng series này")
        return series_id_input
    if series_name_input and series_name_input.strip():
        existing = await db.execute(
            text('SELECT id FROM "Series" WHERE LOWER(name) = LOWER(:name) LIMIT 1'),
            {"name": series_name_input.strip()},
        )
        row = existing.mappings().first()
        if row:
            return row["id"]
        # Create new
        base_slug = _slugify_unique(series_name_input)
        count_result = await db.execute(
            text('SELECT COUNT(*) FROM "Series" WHERE slug LIKE :pat'), {"pat": f"{base_slug}%"}
        )
        cnt = count_result.scalar() or 0
        slug = base_slug if cnt == 0 else f"{base_slug}-{int(cnt) + 1}"
        new_id = _new_cuid()
        await db.execute(
            text(
                'INSERT INTO "Series" (id, name, slug, "createdAt", "updatedAt") VALUES (:id, :name, :slug, NOW(), NOW())'
            ),
            {"id": new_id, "name": series_name_input.strip(), "slug": slug},
        )
        return new_id
    return None


# ---------------------------------------------------------------------------
# /api/mod/truyen
# ---------------------------------------------------------------------------


@router.get("/mod/overview")
async def mod_overview(
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_mod_user),
):
    if _is_admin(user):
        novel_count = (await db.execute(text('SELECT COUNT(*)::int FROM "Novel"'))).scalar() or 0
        total_views = (await db.execute(text('SELECT COALESCE(SUM(views), 0)::bigint FROM "Novel"'))).scalar() or 0
        comment_count = (await db.execute(text('SELECT COUNT(*)::int FROM "Comment"'))).scalar() or 0
        series_count = (await db.execute(text('SELECT COUNT(*)::int FROM "Series"'))).scalar() or 0
    else:
        novel_count = (
            await db.execute(
                text('SELECT COUNT(*)::int FROM "Novel" WHERE "uploaderId" = :uid OR "uploaderId" IS NULL'),
                {"uid": user["id"]},
            )
        ).scalar() or 0
        total_views = (
            await db.execute(
                text('SELECT COALESCE(SUM(views), 0)::bigint FROM "Novel" WHERE "uploaderId" = :uid OR "uploaderId" IS NULL'),
                {"uid": user["id"]},
            )
        ).scalar() or 0
        comment_count = (
            await db.execute(
                text(
                    'SELECT COUNT(c.id)::int '
                    'FROM "Comment" c '
                    'JOIN "Novel" n ON n.id = c."novelId" '
                    'WHERE n."uploaderId" = :uid OR n."uploaderId" IS NULL'
                ),
                {"uid": user["id"]},
            )
        ).scalar() or 0
        series_count = (
            await db.execute(
                text(
                    'SELECT COUNT(s.id)::int '
                    'FROM "Series" s '
                    'WHERE EXISTS ('
                    '  SELECT 1 FROM "Novel" n '
                    '  WHERE n."seriesId" = s.id AND (n."uploaderId" = :uid OR n."uploaderId" IS NULL)'
                    ') OR NOT EXISTS ('
                    '  SELECT 1 FROM "Novel" n2 WHERE n2."seriesId" = s.id'
                    ')'
                ),
                {"uid": user["id"]},
            )
        ).scalar() or 0

    return {
        "novelCount": int(novel_count),
        "totalViews": int(total_views),
        "commentCount": int(comment_count),
        "seriesCount": int(series_count),
    }


@router.get("/mod/truyen")
async def mod_list_novels(
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_mod_user),
):
    if _is_admin(user):
        result = await db.execute(
            text(
                'SELECT n.id, n.title, n.slug, n."authorName", n."coverUrl", n.status, '
                'n."totalChapters", n."createdAt", n."updatedAt", n."seriesId", '
                's.id AS series_id, s.name AS series_name, s.slug AS series_slug '
                'FROM "Novel" n LEFT JOIN "Series" s ON s.id = n."seriesId" '
                'ORDER BY n."updatedAt" DESC'
            )
        )
    else:
        result = await db.execute(
            text(
                'SELECT n.id, n.title, n.slug, n."authorName", n."coverUrl", n.status, '
                'n."totalChapters", n."createdAt", n."updatedAt", n."seriesId", '
                's.id AS series_id, s.name AS series_name, s.slug AS series_slug '
                'FROM "Novel" n LEFT JOIN "Series" s ON s.id = n."seriesId" '
                'WHERE n."uploaderId" = :uid OR n."uploaderId" IS NULL '
                'ORDER BY n."updatedAt" DESC'
            ),
            {"uid": user["id"]},
        )
    rows = result.mappings().all()
    novels = []
    for r in rows:
        d = dict(r)
        series = None
        if d.get("series_id"):
            series = {"id": d["series_id"], "name": d["series_name"], "slug": d["series_slug"]}
        novels.append({
            "id": d["id"], "title": d["title"], "slug": d["slug"],
            "authorName": d["authorName"], "coverUrl": d["coverUrl"],
            "status": d["status"], "totalChapters": d["totalChapters"],
            "createdAt": str(d["createdAt"]), "updatedAt": str(d["updatedAt"]),
            "seriesId": d["seriesId"], "series": series,
        })
    return novels


class NovelCreateBody(BaseModel):
    title: str
    originalTitle: str | None = None
    authorName: str = ""
    originalAuthorName: str | None = None
    description: str = ""
    coverUrl: str | None = None
    genreIds: list[str] = []
    seriesId: str | None = None
    seriesName: str | None = None
    status: str = "Đang ra"


@router.post("/mod/truyen", status_code=201)
async def mod_create_novel(
    body: NovelCreateBody,
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_mod_user),
):
    series_id = await _resolve_series_id(db, body.seriesId, body.seriesName, user)
    base_slug = _slugify_unique(body.title)
    count_result = await db.execute(
        text('SELECT COUNT(*) FROM "Novel" WHERE slug LIKE :pat'), {"pat": f"{base_slug}%"}
    )
    cnt = count_result.scalar() or 0
    slug = base_slug if cnt == 0 else f"{base_slug}-{int(cnt) + 1}"
    novel_id = _new_cuid()
    await db.execute(
        text(
            'INSERT INTO "Novel" (id, title, "originalTitle", slug, "authorName", "originalAuthorName", '
            'description, "coverUrl", status, "uploaderId", "seriesId", "createdAt", "updatedAt") '
            "VALUES (:id, :title, :ot, :slug, :author, :oauthor, :desc, :cover, :status, :uid, :sid, NOW(), NOW())"
        ),
        {
            "id": novel_id, "title": body.title, "ot": body.originalTitle, "slug": slug,
            "author": body.authorName, "oauthor": body.originalAuthorName,
            "desc": body.description, "cover": body.coverUrl, "status": body.status,
            "uid": user["id"], "sid": series_id,
        },
    )
    if body.genreIds:
        for gid in body.genreIds:
            await db.execute(
                text('INSERT INTO "NovelGenre" ("novelId", "genreId") VALUES (:nid, :gid) ON CONFLICT DO NOTHING'),
                {"nid": novel_id, "gid": gid},
            )
    await db.commit()
    result = await db.execute(text('SELECT * FROM "Novel" WHERE id = :id'), {"id": novel_id})
    row = result.mappings().first()
    if not row:
        raise HTTPException(status_code=500, detail="Không thể tạo truyện")
    return dict(row)


class NovelUpdateBody(BaseModel):
    id: str
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


@router.put("/mod/truyen")
async def mod_update_novel(
    body: NovelUpdateBody,
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_mod_user),
):
    # Ownership check
    if not _is_admin(user):
        owns = await db.execute(
            text('SELECT id, "seriesId" FROM "Novel" WHERE id = :id AND ("uploaderId" = :uid OR "uploaderId" IS NULL)'),
            {"id": body.id, "uid": user["id"]},
        )
    else:
        owns = await db.execute(text('SELECT id, "seriesId" FROM "Novel" WHERE id = :id'), {"id": body.id})
    novel_row = owns.mappings().first()
    if not novel_row:
        raise HTTPException(status_code=404, detail="Không tìm thấy truyện hoặc không có quyền")

    series_id = await _resolve_series_id(db, body.seriesId, body.seriesName, user)

    # Get series siblings
    series_novel_ids: list[str] = [body.id]
    current_series_id = series_id if series_id is not None else novel_row.get("seriesId")
    if current_series_id:
        sib = await db.execute(
            text('SELECT id FROM "Novel" WHERE "seriesId" = :sid'), {"sid": current_series_id}
        )
        series_novel_ids = [r["id"] for r in sib.mappings()]
        if body.id not in series_novel_ids:
            series_novel_ids.append(body.id)

    # Shared fields (apply to all series siblings)
    shared_fields: dict[str, Any] = {}
    if body.originalTitle is not None:
        shared_fields["originalTitle"] = body.originalTitle
    if body.authorName is not None:
        shared_fields["authorName"] = body.authorName
    if body.originalAuthorName is not None:
        shared_fields["originalAuthorName"] = body.originalAuthorName
    if body.description is not None:
        shared_fields["description"] = body.description
    if body.status is not None:
        shared_fields["status"] = body.status

    # Own fields
    own_fields: dict[str, Any] = {}
    if body.title is not None:
        own_fields["title"] = body.title
    if body.coverUrl is not None:
        own_fields["coverUrl"] = body.coverUrl

    if series_id is not None:
        own_fields["seriesId"] = series_id

    # Update shared fields across siblings
    if shared_fields and len(series_novel_ids) > 1:
        set_clause = ", ".join(f'"{k}" = :{k}' for k in shared_fields)
        ids_param = ",".join(f"'{sid}'" for sid in series_novel_ids)
        await db.execute(
            text(f'UPDATE "Novel" SET {set_clause}, "updatedAt" = NOW() WHERE id IN ({ids_param})'),
            shared_fields,
        )
    else:
        own_fields.update(shared_fields)

    if own_fields:
        set_clause = ", ".join(f'"{k}" = :{k}' for k in own_fields)
        await db.execute(
            text(f'UPDATE "Novel" SET {set_clause}, "updatedAt" = NOW() WHERE id = :id'),
            {**own_fields, "id": body.id},
        )

    if body.genreIds is not None:
        for sid in series_novel_ids:
            await db.execute(text('DELETE FROM "NovelGenre" WHERE "novelId" = :nid'), {"nid": sid})
            for gid in body.genreIds:
                await db.execute(
                    text('INSERT INTO "NovelGenre" ("novelId", "genreId") VALUES (:nid, :gid) ON CONFLICT DO NOTHING'),
                    {"nid": sid, "gid": gid},
                )

    await db.commit()
    result = await db.execute(text('SELECT * FROM "Novel" WHERE id = :id'), {"id": body.id})
    row = result.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Không tìm thấy truyện")
    return dict(row)


@router.delete("/mod/truyen")
async def mod_delete_novel(
    id: str = Query(...),
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_mod_user),
):
    if not _is_admin(user):
        owns = await db.execute(
            text('SELECT id, "coverUrl", "seriesId" FROM "Novel" WHERE id = :id AND ("uploaderId" = :uid OR "uploaderId" IS NULL)'),
            {"id": id, "uid": user["id"]},
        )
    else:
        owns = await db.execute(
            text('SELECT id, "coverUrl", "seriesId" FROM "Novel" WHERE id = :id'), {"id": id}
        )
    novel = owns.mappings().first()
    if not novel:
        raise HTTPException(status_code=404, detail="Không tìm thấy truyện hoặc không có quyền")

    await mongo_db.chapters.delete_many({"novelId": id})
    await db.execute(text('DELETE FROM "Novel" WHERE id = :id'), {"id": id})
    await db.commit()

    await _delete_from_r2(novel.get("coverUrl"))

    # Cleanup empty series
    if novel.get("seriesId"):
        cnt = await db.execute(
            text('SELECT COUNT(*) FROM "Novel" WHERE "seriesId" = :sid'), {"sid": novel["seriesId"]}
        )
        if (cnt.scalar() or 0) == 0:
            await db.execute(text('DELETE FROM "Series" WHERE id = :id'), {"id": novel["seriesId"]})
            await db.commit()

    return {"success": True}


@router.get("/mod/truyen/{novel_id}/trash-words")
async def mod_get_trash_words(
    novel_id: str,
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_mod_user),
):
    if _is_admin(user):
        result = await db.execute(
            text('SELECT "trashWords", "uploaderId" FROM "Novel" WHERE id = :id'), {"id": novel_id}
        )
    else:
        result = await db.execute(
            text('SELECT "trashWords", "uploaderId" FROM "Novel" WHERE id = :id AND ("uploaderId" = :uid OR "uploaderId" IS NULL)'),
            {"id": novel_id, "uid": user["id"]},
        )
    row = result.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Không tìm thấy truyện")
    return {"trashWords": row["trashWords"] or []}


class TrashWordsBody(BaseModel):
    trashWords: list[str]


@router.put("/mod/truyen/{novel_id}/trash-words")
async def mod_update_trash_words(
    novel_id: str,
    body: TrashWordsBody,
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_mod_user),
):
    if not isinstance(body.trashWords, list):
        raise HTTPException(status_code=400, detail="trashWords phải là mảng")
    if _is_admin(user):
        await db.execute(
            text('UPDATE "Novel" SET "trashWords" = :words WHERE id = :id'),
            {"words": body.trashWords, "id": novel_id},
        )
    else:
        await db.execute(
            text('UPDATE "Novel" SET "trashWords" = :words WHERE id = :id AND ("uploaderId" = :uid OR "uploaderId" IS NULL)'),
            {"words": body.trashWords, "id": novel_id, "uid": user["id"]},
        )
    await db.commit()
    return {"trashWords": body.trashWords}


class BulkNovelBody(BaseModel):
    action: str
    ids: list[str]


@router.post("/mod/truyen/bulk")
async def mod_bulk_novels(
    body: BulkNovelBody,
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_mod_user),
):
    if body.action != "delete":
        raise HTTPException(status_code=400, detail="Chỉ hỗ trợ action=delete")

    if _is_admin(user):
        result = await db.execute(
            text('SELECT id, "coverUrl" FROM "Novel" WHERE id = ANY(:ids)'),
            {"ids": body.ids},
        )
    else:
        result = await db.execute(
            text('SELECT id, "coverUrl" FROM "Novel" WHERE id = ANY(:ids) AND ("uploaderId" = :uid OR "uploaderId" IS NULL)'),
            {"ids": body.ids, "uid": user["id"]},
        )
    accessible = result.mappings().all()
    accessible_ids = [r["id"] for r in accessible]
    if not accessible_ids:
        return {"deletedCount": 0}

    await mongo_db.chapters.delete_many({"novelId": {"$in": accessible_ids}})
    await db.execute(
            text('DELETE FROM "Novel" WHERE id = ANY(:ids)'), {"ids": accessible_ids}
    )
    await db.commit()

    import asyncio
    await asyncio.gather(*[_delete_from_r2(r.get("coverUrl")) for r in accessible])

    return {"deletedCount": len(accessible_ids)}


@router.get("/mod/truyen/missing")
async def mod_novels_missing(
    q: str = Query(""),
    missing: str = Query(""),
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_mod_user),
):
    conds = ['1=1']
    params: dict[str, Any] = {}

    if not _is_admin(user):
        conds.append('(n."uploaderId" = :uid OR n."uploaderId" IS NULL)')
        params["uid"] = user["id"]

    if q.strip():
        conds.append('(n.title ILIKE :q OR n."authorName" ILIKE :q)')
        params["q"] = f"%{q.strip()}%"

    missing_keys = [k.strip() for k in missing.split(",") if k.strip()]
    missing_conds = []
    for key in missing_keys:
        if key == "author":
            missing_conds.append("(n.\"authorName\" = '' OR n.\"authorName\" IS NULL)")
        elif key == "cover":
            missing_conds.append("(n.\"coverUrl\" IS NULL OR n.\"coverUrl\" = '')")
        elif key == "description":
            missing_conds.append("(n.description = '' OR n.description IS NULL)")
        elif key == "genres":
            missing_conds.append('NOT EXISTS (SELECT 1 FROM "NovelGenre" ng WHERE ng."novelId" = n.id)')

    if missing_conds:
        conds.append("(" + " OR ".join(missing_conds) + ")")

    where = " AND ".join(conds)
    result = await db.execute(
        text(
            f'SELECT n.id, n.title, n.slug, n."authorName", n."coverUrl", n.status, '
            f'n."totalChapters", n."updatedAt" '
            f'FROM "Novel" n WHERE {where} ORDER BY n."updatedAt" DESC LIMIT 600'
        ),
        params,
    )
    return {"items": [dict(r) for r in result.mappings()]}


class MissingPatchItem(BaseModel):
    id: str
    authorName: str | None = None
    coverUrl: str | None = None
    description: str | None = None
    genreIds: list[str] | None = None


class MissingPatchBody(BaseModel):
    updates: list[MissingPatchItem]


@router.patch("/mod/truyen/missing")
async def mod_patch_missing(
    body: MissingPatchBody,
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_mod_user),
):
    if len(body.updates) > 200:
        raise HTTPException(status_code=400, detail="Tối đa 200 bản ghi")

    ids = [u.id for u in body.updates]
    if _is_admin(user):
        access_result = await db.execute(
            text('SELECT id FROM "Novel" WHERE id = ANY(:ids)'), {"ids": ids}
        )
    else:
        access_result = await db.execute(
            text('SELECT id FROM "Novel" WHERE id = ANY(:ids) AND ("uploaderId" = :uid OR "uploaderId" IS NULL)'),
            {"ids": ids, "uid": user["id"]},
        )
    accessible_ids = {r["id"] for r in access_result.mappings()}

    updated = 0
    for item in body.updates:
        if item.id not in accessible_ids:
            continue
        fields: dict[str, Any] = {}
        if item.authorName is not None:
            fields["authorName"] = item.authorName
        if item.coverUrl is not None:
            fields["coverUrl"] = item.coverUrl
        if item.description is not None:
            fields["description"] = item.description
        if fields:
            set_clause = ", ".join(f'"{k}" = :{k}' for k in fields)
            await db.execute(
                text(f'UPDATE "Novel" SET {set_clause}, "updatedAt" = NOW() WHERE id = :id'),
                {**fields, "id": item.id},
            )
        if item.genreIds is not None:
            await db.execute(text('DELETE FROM "NovelGenre" WHERE "novelId" = :nid'), {"nid": item.id})
            for gid in item.genreIds:
                await db.execute(
                    text('INSERT INTO "NovelGenre" ("novelId", "genreId") VALUES (:nid, :gid) ON CONFLICT DO NOTHING'),
                    {"nid": item.id, "gid": gid},
                )
        updated += 1

    await db.commit()
    return {"updatedCount": updated}


@router.get("/mod/truyen/{novel_id}")
async def mod_get_novel(
    novel_id: str,
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_mod_user),
):
    if _is_admin(user):
        result = await db.execute(
            text('SELECT * FROM "Novel" WHERE id = :id'), {"id": novel_id}
        )
    else:
        result = await db.execute(
            text('SELECT * FROM "Novel" WHERE id = :id AND ("uploaderId" = :uid OR "uploaderId" IS NULL)'),
            {"id": novel_id, "uid": user["id"]},
        )
    row = result.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Không tìm thấy truyện")

    novel = dict(row)

    genres_result = await db.execute(
        text('SELECT g.id, g.name, g.slug FROM "NovelGenre" ng JOIN "Genre" g ON g.id = ng."genreId" WHERE ng."novelId" = :nid'),
        {"nid": novel_id},
    )
    novel["genres"] = [dict(r) for r in genres_result.mappings()]

    if novel.get("seriesId"):
        s_result = await db.execute(
            text('SELECT id, name, slug, description FROM "Series" WHERE id = :id LIMIT 1'),
            {"id": novel["seriesId"]},
        )
        novel["series"] = dict(s_result.mappings().first() or {})

    return novel


# ---------------------------------------------------------------------------
# /api/mod/chuong (chapters)
# ---------------------------------------------------------------------------


@router.get("/mod/chuong")
async def mod_list_chapters(
    novelId: str = Query(...),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db_session),
):
    skip = (page - 1) * limit
    cursor = mongo_db.chapters.find({"novelId": novelId}, {"content": 0}).sort("number", 1).skip(skip).limit(limit)
    chapters = []
    async for doc in cursor:
        chapter_id = str(doc.pop("_id"))
        # Keep backward compatibility for clients expecting either `id` or `_id`.
        doc["id"] = chapter_id
        doc["_id"] = chapter_id
        chapters.append(doc)
    total = await mongo_db.chapters.count_documents({"novelId": novelId})
    return {
        "chapters": chapters,
        "totalChapters": total,
        "totalPages": (total + limit - 1) // limit,
        "currentPage": page,
    }


@router.get("/mod/chuong/{chapter_id}")
async def mod_get_chapter(
    chapter_id: str,
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_mod_user),
):
    try:
        oid = ObjectId(chapter_id)
    except Exception:
        raise HTTPException(status_code=400, detail="ID không hợp lệ")

    doc = await mongo_db.chapters.find_one({"_id": oid})
    if not doc:
        raise HTTPException(status_code=404, detail="Không tìm thấy chương")

    # Ownership check
    if not _is_admin(user):
        owns = await db.execute(
            text('SELECT id FROM "Novel" WHERE id = :nid AND ("uploaderId" = :uid OR "uploaderId" IS NULL)'),
            {"nid": doc["novelId"], "uid": user["id"]},
        )
        if not owns.first():
            raise HTTPException(status_code=403, detail="Không có quyền")

    chapter_id = str(doc.pop("_id"))
    doc["id"] = chapter_id
    doc["_id"] = chapter_id
    return doc


class ChapterCreateBody(BaseModel):
    novelId: str
    number: int
    title: str
    content: str
    volumeNumber: int | None = None
    volumeTitle: str | None = None
    volumeChapterNumber: int | None = None


@router.post("/mod/chuong", status_code=201)
async def mod_create_chapter(
    body: ChapterCreateBody,
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_mod_user),
):
    try:
        # Validate input
        if not body.title or not body.title.strip():
            raise HTTPException(status_code=400, detail="Tiêu đề chương không được trống")
        if not body.content or not body.content.strip():
            raise HTTPException(status_code=400, detail="Nội dung chương không được trống")
        if body.number <= 0:
            raise HTTPException(status_code=400, detail="Số chương phải > 0")
        
        # Ownership check - MOD can edit default novels (uploaderId IS NULL) or their own
        if not _is_admin(user):
            owns = await db.execute(
                text('SELECT id FROM "Novel" WHERE id = :nid AND ("uploaderId" = :uid OR "uploaderId" IS NULL)'),
                {"nid": body.novelId, "uid": user["id"]},
            )
            if not owns.first():
                raise HTTPException(status_code=403, detail="Không có quyền")

        # Novel existence check
        novel_check = await db.execute(
            text('SELECT id FROM "Novel" WHERE id = :nid'),
            {"nid": body.novelId},
        )
        if not novel_check.first():
            raise HTTPException(status_code=404, detail="Truyện không tồn tại")

        existing = await mongo_db.chapters.find_one({"novelId": body.novelId, "number": body.number})
        if existing:
            raise HTTPException(status_code=400, detail=f"Chương {body.number} đã tồn tại")

        doc = {
            "novelId": body.novelId,
            "number": body.number,
            "title": body.title.strip(),
            "content": body.content.strip(),
            "volumeNumber": body.volumeNumber,
            "volumeTitle": body.volumeTitle,
            "volumeChapterNumber": body.volumeChapterNumber,
        }
        
        # Insert into MongoDB with error handling
        try:
            result = await mongo_db.chapters.insert_one(doc)
            if not result.inserted_id:
                raise Exception("MongoDB insert_one không trả về inserted_id")
            inserted_chapter_id = str(result.inserted_id)
        except Exception as mongo_err:
            raise HTTPException(status_code=500, detail=f"Lỗi MongoDB: {str(mongo_err)}")
        
        # Sync total chapters count to PostgreSQL
        try:
            total = await _sync_total_chapters(db, body.novelId)
        except Exception as pg_err:
            # ⚠️ MongoDB has inserted but PostgreSQL update failed!
            # Log this critical state for manual recovery
            error_detail = f"⚠️ INCONSISTENT STATE: Chapter inserted in MongoDB (ID: {inserted_chapter_id}) but PostgreSQL sync failed: {str(pg_err)}"
            print(f"[CRITICAL] {error_detail}")
            # Still raise error so client knows something went wrong
            raise HTTPException(status_code=500, detail=f"Dữ liệu has được lưu nhưng không thêm vào tổng số. Vui lòng liên hệ admin. {str(pg_err)}")
        
        # Prepare response
        doc["id"] = inserted_chapter_id
        doc.pop("_id", None)
        
        return doc
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        error_msg = str(e)
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Lỗi khi lưu chương: {error_msg}")


class ChapterUpdateBody(BaseModel):
    id: str
    novelId: str
    number: int
    title: str
    content: str
    volumeNumber: int | None = None
    volumeTitle: str | None = None
    volumeChapterNumber: int | None = None


@router.put("/mod/chuong")
async def mod_update_chapter(
    body: ChapterUpdateBody,
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_mod_user),
):
    # Validate input
    if not body.title or not body.title.strip():
        raise HTTPException(status_code=400, detail="Tiêu đề chương không được trống")
    if not body.content or not body.content.strip():
        raise HTTPException(status_code=400, detail="Nội dung chương không được trống")
    if body.number <= 0:
        raise HTTPException(status_code=400, detail="Số chương phải > 0")
    
    # Ownership check - MOD can edit default novels (uploaderId IS NULL) or their own
    if not _is_admin(user):
        owns = await db.execute(
            text('SELECT id FROM "Novel" WHERE id = :nid AND ("uploaderId" = :uid OR "uploaderId" IS NULL)'),
            {"nid": body.novelId, "uid": user["id"]},
        )
        if not owns.first():
            raise HTTPException(status_code=403, detail="Không có quyền")

    try:
        oid = ObjectId(body.id)
    except Exception:
        raise HTTPException(status_code=400, detail="ID không hợp lệ")

    update_data = {
        "number": body.number,
        "title": body.title.strip(),
        "content": body.content.strip(),
        "volumeNumber": body.volumeNumber,
        "volumeTitle": body.volumeTitle,
        "volumeChapterNumber": body.volumeChapterNumber,
    }
    result = await mongo_db.chapters.find_one_and_update(
        {"_id": oid, "novelId": body.novelId},
        {"$set": update_data},
        return_document=True,
    )
    if not result:
        raise HTTPException(status_code=404, detail="Không tìm thấy chương")
    result["id"] = str(result.pop("_id"))
    return result


@router.delete("/mod/chuong")
async def mod_delete_chapter(
    id: str = Query(...),
    novelId: str = Query(...),
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_mod_user),
):
    if not _is_admin(user):
        owns = await db.execute(
            text('SELECT id FROM "Novel" WHERE id = :nid AND ("uploaderId" = :uid OR "uploaderId" IS NULL)'),
            {"nid": novelId, "uid": user["id"]},
        )
        if not owns.first():
            raise HTTPException(status_code=403, detail="Không có quyền")

    try:
        oid = ObjectId(id)
    except Exception:
        raise HTTPException(status_code=400, detail="ID không hợp lệ")

    result = await mongo_db.chapters.find_one_and_delete({"_id": oid, "novelId": novelId})
    if not result:
        raise HTTPException(status_code=404, detail="Không tìm thấy chương")

    total = await _sync_total_chapters(db, novelId)
    return {"success": True, "totalChapters": total}


class BulkDeleteChaptersBody(BaseModel):
    novelId: str
    fromNumber: int
    toNumber: int


@router.post("/mod/chuong/bulk-delete")
async def mod_bulk_delete_chapters(
    body: BulkDeleteChaptersBody,
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_mod_user),
):
    if not _is_admin(user):
        owns = await db.execute(
            text('SELECT id FROM "Novel" WHERE id = :nid AND ("uploaderId" = :uid OR "uploaderId" IS NULL)'),
            {"nid": body.novelId, "uid": user["id"]},
        )
        if not owns.first():
            raise HTTPException(status_code=403, detail="Không có quyền")

    delete_result = await mongo_db.chapters.delete_many({
        "novelId": body.novelId,
        "number": {"$gte": body.fromNumber, "$lte": body.toNumber},
    })
    total = await _sync_total_chapters(db, body.novelId)
    return {"success": True, "deletedCount": delete_result.deleted_count, "totalChapters": total}


class GlobalReplaceBody(BaseModel):
    novelId: str
    action: str = "replace"
    findText: str | None = None
    replaceText: str | None = None
    matchCase: bool = False
    trashWords: Any = None
    preview: bool = False


@router.post("/mod/chuong/global-replace")
async def mod_global_replace(
    body: GlobalReplaceBody,
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_mod_user),
):
    if not _is_admin(user):
        owns = await db.execute(
            text('SELECT id FROM "Novel" WHERE id = :nid AND ("uploaderId" = :uid OR "uploaderId" IS NULL)'),
            {"nid": body.novelId, "uid": user["id"]},
        )
        if not owns.first():
            raise HTTPException(status_code=403, detail="Không có quyền")

    def _build_patterns() -> list[tuple[re.Pattern, str]]:
        pairs = []
        if body.action == "trash":
            words = body.trashWords or []
            if isinstance(words, str):
                words = [w.strip() for w in words.split(",") if w.strip()]
            for w in words:
                escaped = re.escape(w)
                flags = 0 if body.matchCase else re.IGNORECASE
                pairs.append((re.compile(escaped, flags), ""))
        else:
            if body.findText:
                escaped = re.escape(body.findText)
                flags = 0 if body.matchCase else re.IGNORECASE
                pairs.append((re.compile(escaped, flags), body.replaceText or ""))
        return pairs

    patterns = _build_patterns()
    if not patterns:
        return {"message": "Không có pattern nào để thay thế", "updatedChapters": 0}

    cursor = mongo_db.chapters.find({"novelId": body.novelId}).sort("number", 1)
    updated_count = 0
    previews = []

    async for chap in cursor:
        original = chap.get("content", "")
        new_content = original
        for pattern, replacement in patterns:
            new_content = pattern.sub(replacement, new_content)

        if new_content == original:
            continue

        if body.preview:
            if len(previews) < 5:
                previews.append({
                    "number": chap.get("number"),
                    "title": chap.get("title"),
                    "snippet": new_content[:300],
                })
            updated_count += 1
        else:
            await mongo_db.chapters.update_one({"_id": chap["_id"]}, {"$set": {"content": new_content}})
            updated_count += 1

    return {"message": "Hoàn thành", "updatedChapters": updated_count, "previews": previews}


class OptimizeItem(BaseModel):
    id: str
    number: int
    title: str


class OptimizeBody(BaseModel):
    novelId: str
    updates: list[OptimizeItem]


@router.put("/mod/chuong/optimize")
async def mod_optimize_chapters(
    body: OptimizeBody,
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_mod_user),
):
    if not _is_admin(user):
        owns = await db.execute(
            text('SELECT id FROM "Novel" WHERE id = :nid AND ("uploaderId" = :uid OR "uploaderId" IS NULL)'),
            {"nid": body.novelId, "uid": user["id"]},
        )
        if not owns.first():
            raise HTTPException(status_code=403, detail="Không có quyền")

    ops = []
    for item in body.updates:
        try:
            oid = ObjectId(item.id)
        except Exception:
            continue
        ops.append(
            UpdateOne(
                {"_id": oid, "novelId": body.novelId},
                {"$set": {"number": item.number, "title": item.title.strip()}},
            )
        )

    if not ops:
        return {"matchedCount": 0, "modifiedCount": 0}

    try:
        result = await mongo_db.chapters.bulk_write(ops, ordered=False)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Không thể tối ưu hàng loạt: {e}")

    return {"matchedCount": result.matched_count, "modifiedCount": result.modified_count}


# ---------------------------------------------------------------------------
# /api/mod/de-cu (editor recommendations)
# ---------------------------------------------------------------------------


@router.get("/mod/de-cu")
async def mod_list_recommendations(
    q: str = Query(""),
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_mod_user),
):
    recs = await mongo_db.editorrecommendations.find({}).sort("createdAt", -1).limit(1000).to_list(1000)

    # Collect novel and editor IDs
    novel_ids = list({str(r["novelId"]) for r in recs if r.get("novelId")})
    editor_ids = list({str(r["editorId"]) for r in recs if r.get("editorId")})

    novels_map: dict[str, dict] = {}
    if novel_ids:
        n_result = await db.execute(
            text(
                'SELECT id, title, slug, "coverUrl", "authorName", status, "totalChapters" '
                'FROM "Novel" WHERE id = ANY(:ids)'
            ),
            {"ids": novel_ids},
        )
        for r in n_result.mappings():
            novels_map[r["id"]] = dict(r)

    editors_map: dict[str, dict] = {}
    if editor_ids:
        e_result = await db.execute(
            text('SELECT id, name, email FROM "User" WHERE id = ANY(:ids)'),
            {"ids": editor_ids},
        )
        for r in e_result.mappings():
            editors_map[r["id"]] = dict(r)

    recommend_count_map: dict[str, int] = {}
    for r in recs:
        novel_id = str(r.get("novelId") or "")
        if novel_id:
            recommend_count_map[novel_id] = recommend_count_map.get(novel_id, 0) + 1

    items = []
    for r in recs:
        novel_id = str(r.get("novelId", ""))
        editor_id = str(r.get("editorId", ""))
        novel = novels_map.get(novel_id)
        editor = editors_map.get(editor_id)
        if not novel or not editor:
            continue

        items.append(
            {
                "id": str(r["_id"]),
                "createdAt": _to_iso(r.get("createdAt")),
                "recommendCount": recommend_count_map.get(novel_id, 0),
                "novel": {
                    "id": novel["id"],
                    "title": novel["title"],
                    "slug": novel["slug"],
                    "authorName": novel.get("authorName") or "",
                    "coverUrl": novel.get("coverUrl"),
                    "status": novel.get("status") or "Đang ra",
                    "totalChapters": int(novel.get("totalChapters") or 0),
                },
                "editor": {
                    "id": editor["id"],
                    "name": editor.get("name") or "Biên tập viên",
                },
            }
        )

    summary = []
    for novel_id, recommend_count in recommend_count_map.items():
        novel = novels_map.get(novel_id)
        if not novel:
            continue
        summary.append(
            {
                "novel": {
                    "id": novel["id"],
                    "title": novel["title"],
                    "slug": novel["slug"],
                    "authorName": novel.get("authorName") or "",
                    "coverUrl": novel.get("coverUrl"),
                    "status": novel.get("status") or "Đang ra",
                    "totalChapters": int(novel.get("totalChapters") or 0),
                },
                "recommendCount": recommend_count,
            }
        )
    summary.sort(key=lambda item: item["recommendCount"], reverse=True)

    # Search candidates
    candidates = []
    if q.strip():
        c_result = await db.execute(
            text(
                'SELECT id, title, slug, "authorName", "coverUrl", status, "totalChapters" '
                'FROM "Novel" WHERE title ILIKE :q OR "authorName" ILIKE :q OR slug ILIKE :q LIMIT 20'
            ),
            {"q": f"%{q}%"},
        )
        candidates = []
        for r in c_result.mappings():
            row = dict(r)
            novel_id = row["id"]
            already_recommended = any(
                str(item.get("novelId")) == novel_id and str(item.get("editorId")) == user["id"]
                for item in recs
            )
            candidates.append(
                {
                    "id": row["id"],
                    "title": row["title"],
                    "slug": row["slug"],
                    "authorName": row.get("authorName") or "",
                    "coverUrl": row.get("coverUrl"),
                    "status": row.get("status") or "Đang ra",
                    "totalChapters": int(row.get("totalChapters") or 0),
                    "alreadyRecommended": already_recommended,
                    "recommendCount": int(recommend_count_map.get(novel_id, 0)),
                }
            )

    my_novel_result = await db.execute(
        text('SELECT id FROM "Novel" WHERE "uploaderId" = :uid OR "uploaderId" IS NULL'),
        {"uid": user["id"]},
    )
    my_novel_ids = [r["id"] for r in my_novel_result.mappings()]

    current_user_rec_count = sum(1 for item in recs if str(item.get("editorId")) == user["id"])

    return {
        "items": items,
        "summary": summary,
        "candidates": candidates,
        "myNovelIds": my_novel_ids,
        "currentUser": {
            "id": user["id"],
            "role": user.get("role"),
            "recommendationCount": current_user_rec_count,
            "maxRecommendationCount": MAX_RECOMMENDATIONS_PER_EDITOR,
        },
    }


class DeduBody(BaseModel):
    novelId: str


@router.post("/mod/de-cu", status_code=201)
async def mod_create_recommendation(
    body: DeduBody,
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_mod_user),
):
    novel = await db.execute(text('SELECT id FROM "Novel" WHERE id = :id'), {"id": body.novelId})
    if not novel.first():
        raise HTTPException(status_code=404, detail="Không tìm thấy truyện")

    existing = await mongo_db.editorrecommendations.find_one({"novelId": body.novelId, "editorId": user["id"]})
    if existing:
        raise HTTPException(status_code=400, detail="Đã đề cử truyện này rồi")

    count = await mongo_db.editorrecommendations.count_documents({"editorId": user["id"]})
    if count >= MAX_RECOMMENDATIONS_PER_EDITOR:
        raise HTTPException(status_code=400, detail=f"Mỗi editor tối đa {MAX_RECOMMENDATIONS_PER_EDITOR} đề cử")

    import datetime as _dt
    result = await mongo_db.editorrecommendations.insert_one({
        "novelId": body.novelId,
        "editorId": user["id"],
        "createdAt": _dt.datetime.now(_dt.timezone.utc),
    })
    return {"id": str(result.inserted_id), "novelId": body.novelId, "editorId": user["id"]}


@router.delete("/mod/de-cu")
async def mod_delete_recommendation(
    id: str = Query(...),
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_mod_user),
):
    try:
        oid = ObjectId(id)
    except Exception:
        raise HTTPException(status_code=400, detail="ID không hợp lệ")

    rec = await mongo_db.editorrecommendations.find_one({"_id": oid})
    if not rec:
        raise HTTPException(status_code=404, detail="Không tìm thấy đề cử")

    if not _is_admin(user) and str(rec.get("editorId")) != user["id"]:
        raise HTTPException(status_code=403, detail="Không có quyền xóa đề cử này")

    await mongo_db.editorrecommendations.delete_one({"_id": oid})
    return {"success": True}


# ---------------------------------------------------------------------------
# /api/mod/upload-cover
# ---------------------------------------------------------------------------


@router.post("/mod/upload-cover")
async def mod_upload_cover(
    file: UploadFile = File(...),
    user: dict = Depends(require_mod_user),
):
    if not (file.content_type or "").startswith("image/"):
        raise HTTPException(status_code=400, detail="File phải là ảnh")
    data = await file.read()
    url = await _upload_to_r2(data, file.content_type or "image/jpeg", "covers/manual", file.filename or "")
    return {"url": url}


# ---------------------------------------------------------------------------
# /api/mod/epub — EPUB upload and parse
# ---------------------------------------------------------------------------

_REGEX_PRESETS = {
    "vi_chuong_hoi": r"^\s*(?:[#>*\-]+\s*)?(?:(?:Ch(?:ương|uong)|H(?:ồi|oi)|Ti(?:ết|et)|Ph(?:ần|an)|Th(?:ứ|u)|Quy(?:ển|en)|Ch\.)\s*\d+(?:\.\d+)?|第\s*\d+\s*章)[^\n]{0,120}$",
    "mix_chapter": r"^\s*(?:[#>*\-]+\s*)?(?:(?:Ch(?:ương|uong)|H(?:ồi|oi)|Ti(?:ết|et)|Ph(?:ần|an)|Th(?:ứ|u)|Quy(?:ển|en)|Ch\.|Chapter|ch(?:apter)?)\s*\d+(?:\.\d+)?|第\s*\d+\s*章)[^\n]{0,120}$",
    "numeric_only": r"^\s*(?:[#>*\-]+\s*)?\d+(?:\.\d+)?\s*[\.:\-\]\)]?(?:\s+|$)[^\n]{0,120}$",
}

_REGEX_PRESET_ALIASES = {
    "vi_chuong": "vi_chuong_hoi",
    "en_chapter": "mix_chapter",
    "bracket_chapter": "mix_chapter",
}


def _epub_html_to_text(html_content: str) -> str:
    h = html2text.HTML2Text()
    h.ignore_links = True
    h.ignore_images = True
    h.body_width = 0
    return h.handle(html_content).strip()


def _extract_chapter_number(title: str) -> int | None:
    m = re.search(
        r"(?:(?:Ch(?:ương|uong)|H(?:ồi|oi)|Ti(?:ết|et)|Ph(?:ần|an)|Th(?:ứ|u)|Quy(?:ển|en)|Chapter|CHAPTER|Ch\.)\s*|第\s*)(\d+)(?:\s*章)?",
        title or "",
        re.IGNORECASE,
    )
    if not m:
        return None
    try:
        n = int(m.group(1))
        return n if n > 0 else None
    except Exception:
        return None


def _chapter_heading_score(line: str) -> int:
    s = re.sub(r"\s+", " ", (line or "").strip())
    s = re.sub(r"^\s*(?:[#>*\-]+\s*)+", "", s)
    if len(s) < 4 or len(s) > 140:
        return -10

    number = _extract_chapter_number(s)
    if number is None:
        return -10

    score = 0

    if number <= 5000:
        score += 3
    else:
        score -= 2

    m = re.match(
        r"^(?:(?:Ch(?:ương|uong)|H(?:ồi|oi)|Ti(?:ết|et)|Ph(?:ần|an)|Th(?:ứ|u)|Quy(?:ển|en)|Chapter|CHAPTER|Ch\.)\s*\d+(?:\.\d+)?|第\s*\d+\s*章)(.*)$",
        s,
        re.IGNORECASE,
    )
    if not m:
        return -10

    score += 3

    tail = (m.group(1) or "").strip()
    if not tail:
        return score + 1

    # Strong signal: explicit chapter delimiter after number (e.g. "Chương 81: ...").
    if re.match(
        r"^(?:Ch(?:ương|uong)|H(?:ồi|oi)|Ti(?:ết|et)|Ph(?:ần|an)|Th(?:ứ|u)|Quy(?:ển|en)|Chapter|CHAPTER|Ch\.)\s*\d+(?:\.\d+)?\s*[:：]",
        s,
        re.IGNORECASE,
    ):
        score += 4

    word_count = len([w for w in re.split(r"\s+", tail) if w])
    punct_count = len(re.findall(r"[,:;!?]", tail))

    if tail[0] in ':.-–—)]}"\'”’»':
        score += 2
    elif tail[0] in '!?,;':
        # Likely a sentence line, not chapter heading.
        score -= 6
    else:
        score -= 1

    lower_tail = tail.lower()
    bad_prefixes = (
        "và ", "va ", "là ", "la ", "của ", "cua ", "đã ", "da ", "đang ",
        "thì ", "thi ", "vì ", "vi ", "nên ", "nen ", "trong ", "với ", "voi ",
        "được ", "duoc ", "khi ", "theo ", "ở ", "o ",
    )
    if lower_tail.startswith(bad_prefixes):
        score -= 6

    # Title-like tail should usually start with uppercase/digit/quote/bracket.
    if re.match(r"^[A-ZÀ-Ỹ0-9\"'“‘\(\[\{]", tail):
        score += 2
    elif re.match(r"^[a-zà-ỹ]", tail):
        score -= 2

    if len(tail) > 90:
        score -= 2

    # Penalize highly prose-like tails.
    if "," in tail and len(tail) > 40:
        score -= 1

    # Without a clear delimiter, keep only short title-like tails.
    if tail[0] not in ':.-–—)]}"\'”’»' and (word_count > 10 or punct_count >= 2):
        score -= 4

    # Long sentence-like heading is suspicious.
    if word_count > 16:
        score -= 4

    return score


def _is_likely_chapter_heading(line: str) -> bool:
    return _chapter_heading_score(line) >= 6


def _is_front_matter_title(title: str) -> bool:
    s = re.sub(r"\s+", " ", (title or "").strip()).lower()
    if not s:
        return True

    return bool(
        re.search(
            r"\b(mục lục|muc luc|contents?|toc|giới thiệu|gioi thieu|lời mở đầu|loi mo dau|foreword|preface|about|summary|tóm tắt|tom tat|tên truyện|ten truyen|book title)\b",
            s,
            re.IGNORECASE,
        )
    )


def _looks_like_toc_content(content: str) -> bool:
    lines = [ln.strip() for ln in (content or "").splitlines() if ln.strip()]
    if len(lines) < 8:
        return False

    heading_like = 0
    for ln in lines:
        if _extract_chapter_number(ln) is not None and len(ln) <= 140:
            heading_like += 1

    ratio = heading_like / max(1, len(lines))
    return heading_like >= 6 and ratio >= 0.18


def _postprocess_extracted_chapters(chapters: list[dict], split_mode: str) -> list[dict]:
    """Clean extracted chapters by removing front matter and TOC-like sections."""
    if not chapters:
        return chapters

    normalized: list[dict] = []
    for c in chapters:
        title = re.sub(r"\s+", " ", str(c.get("title") or "")).strip()
        content = str(c.get("content") or "").strip()
        if not content:
            continue
        normalized.append({
            **c,
            "title": title,
            "content": content,
            "number": c.get("number") if isinstance(c.get("number"), int) else _extract_chapter_number(title),
        })

    if not normalized:
        return []

    # Remove obvious TOC/front-matter entries globally.
    filtered = []
    for c in normalized:
        title = c.get("title") or ""
        content = c.get("content") or ""
        if _is_front_matter_title(title):
            continue
        if _looks_like_toc_content(content):
            continue
        filtered.append(c)

    if not filtered:
        return []

    # Drop leading entries until first confident chapter heading appears.
    first_confident_idx = None
    for i, c in enumerate(filtered):
        title = c.get("title") or ""
        detected_num = _extract_chapter_number(title)
        if detected_num is not None and not _is_front_matter_title(title):
            first_confident_idx = i
            break

    if first_confident_idx is not None and first_confident_idx > 0:
        leading = filtered[:first_confident_idx]
        # Only trim when leading items look like front matter.
        if all(_is_front_matter_title(item.get("title") or "") or _looks_like_toc_content(item.get("content") or "") for item in leading):
            filtered = filtered[first_confident_idx:]

    if split_mode == "regex":
        # Deduplicate by chapter number for regex mode, prefer richer content and non-front-matter title.
        by_number: dict[int, dict] = {}
        tail: list[dict] = []
        for c in filtered:
            n = c.get("number")
            if isinstance(n, int) and n > 0:
                prev = by_number.get(n)
                if prev is None:
                    by_number[n] = c
                else:
                    prev_bad = _is_front_matter_title(prev.get("title") or "")
                    cur_bad = _is_front_matter_title(c.get("title") or "")
                    if (prev_bad and not cur_bad) or len((c.get("content") or "")) > len((prev.get("content") or "")):
                        by_number[n] = c
            else:
                tail.append(c)

        ordered = [by_number[k] for k in sorted(by_number.keys())] + tail
        # Reassign chapter number for any unnumbered tails to keep data consistent.
        max_n = max([c.get("number") for c in ordered if isinstance(c.get("number"), int)] or [0])
        for c in ordered:
            if not isinstance(c.get("number"), int) or c.get("number") <= 0:
                max_n += 1
                c["number"] = max_n
        return ordered

    # TOC mode: keep original order after filtering; normalize numbering sequentially.
    for i, c in enumerate(filtered, start=1):
        c["number"] = i
    return filtered


def _normalize_chapter_preview(chapters: list[dict]) -> tuple[list[dict], dict]:
    """Sort chapters by detected number and insert placeholders for gaps (preview only)."""
    by_number: dict[int, dict] = {}
    others: list[dict] = []

    for c in chapters:
        n = c.get("number")
        if isinstance(n, int) and n > 0:
            prev = by_number.get(n)
            if prev is None or len((c.get("content") or "")) > len((prev.get("content") or "")):
                by_number[n] = c
        else:
            others.append(c)

    if not by_number:
        return chapters, {
            "insertedMissingChapters": 0,
            "detectedMaxChapterNumber": len(chapters),
            "chaptersFinal": len(chapters),
        }

    sorted_numbers = sorted(by_number.keys())
    out: list[dict] = []
    inserted_missing = 0
    expected = sorted_numbers[0]

    MAX_FILL_GAP = 5

    for n in sorted_numbers:
        gap = n - expected
        # Only fill small gaps so preview remains readable even if parser misses many headings.
        if 0 < gap <= MAX_FILL_GAP:
            while expected < n:
                inserted_missing += 1
                out.append({
                    "number": expected,
                    "title": f"Chương {expected} (Thiếu)",
                    "content": "",
                    "isPlaceholder": True,
                })
                expected += 1
        elif gap > MAX_FILL_GAP:
            expected = n

        item = dict(by_number[n])
        item["isPlaceholder"] = bool(item.get("isPlaceholder", False))
        out.append(item)
        expected = n + 1

    # Keep non-numbered chapters at the end for manual review.
    for c in others:
        item = dict(c)
        item["isPlaceholder"] = bool(item.get("isPlaceholder", False))
        out.append(item)

    stats = {
        "insertedMissingChapters": inserted_missing,
        "detectedMaxChapterNumber": sorted_numbers[-1],
        "chaptersFinal": len(out),
    }
    return out, stats


def _build_chapters_from_toc(book: epublib.EpubBook) -> list[dict]:
    """Extract chapters following the EPUB TOC order."""
    chapters: list[dict] = []
    num = 1
    seen_hrefs: set[str] = set()

    def _process_item(item) -> str | None:
        if isinstance(item, epublib.EpubHtml):
            return item.get_body_content().decode("utf-8", errors="replace") if item.get_body_content() else None
        return None

    def _append_from_href(href: str, title: str | None):
        nonlocal num
        base_href = (href or "").split("#")[0]
        if not base_href:
            return
        if base_href in seen_hrefs:
            return
        item = book.get_item_with_href(base_href)
        if item and isinstance(item, epublib.EpubHtml):
            content = item.get_body_content()
            if content:
                text = _epub_html_to_text(content.decode("utf-8", errors="replace"))
                if text.strip():
                    seen_hrefs.add(base_href)
                    chapters.append({
                        "number": num,
                        "title": (title or item.title or f"Chương {num}").strip(),
                        "content": text,
                    })
                    num += 1

    def _walk(toc_items):
        nonlocal num
        for entry in toc_items:
            if isinstance(entry, tuple):
                section, children = entry
                # Many EPUBs store usable title/href in section, children may be empty.
                section_href = getattr(section, "href", "") if section is not None else ""
                section_title = getattr(section, "title", None) if section is not None else None
                if section_href:
                    _append_from_href(section_href, section_title)
                _walk(children)
            elif isinstance(entry, epublib.Link):
                _append_from_href(entry.href, entry.title)
            elif isinstance(entry, epublib.EpubHtml):
                file_name = (entry.file_name or "").split("#")[0]
                if file_name and file_name in seen_hrefs:
                    continue
                content = entry.get_body_content()
                if content:
                    text = _epub_html_to_text(content.decode("utf-8", errors="replace"))
                    if text.strip():
                        if file_name:
                            seen_hrefs.add(file_name)
                        chapters.append({
                            "number": num,
                            "title": entry.title or f"Chương {num}",
                            "content": text,
                        })
                        num += 1

    _walk(book.toc)

    # Detect broken TOC extraction and fallback to spine if needed.
    use_spine_fallback = False
    if not chapters:
        use_spine_fallback = True
    else:
        non_empty_spine_items = 0
        for item_id, _linear in book.spine:
            item = book.get_item_with_id(item_id)
            if item and isinstance(item, epublib.EpubHtml) and item.get_body_content():
                non_empty_spine_items += 1

        if len(chapters) <= 1 and non_empty_spine_items >= 3:
            use_spine_fallback = True

        # If most chapters are effectively duplicated content, TOC is likely not usable.
        content_sigs = [re.sub(r"\s+", " ", (c.get("content") or "").strip())[:1200] for c in chapters]
        if len(content_sigs) >= 3:
            unique_ratio = len(set(content_sigs)) / max(1, len(content_sigs))
            if unique_ratio < 0.6:
                use_spine_fallback = True

    if use_spine_fallback:
        chapters = []
        num = 1
        seen_hrefs = set()
        for item_id, _linear in book.spine:
            item = book.get_item_with_id(item_id)
            if item and isinstance(item, epublib.EpubHtml):
                file_name = (item.file_name or "").split("#")[0]
                if file_name and file_name in seen_hrefs:
                    continue
                content = item.get_body_content()
                if content:
                    text = _epub_html_to_text(content.decode("utf-8", errors="replace"))
                    if text.strip():
                        if file_name:
                            seen_hrefs.add(file_name)
                        chapters.append({
                            "number": num,
                            "title": item.title or f"Chương {num}",
                            "content": text,
                        })
                        num += 1

    return chapters


def _build_chapters_from_regex(full_text: str, pattern: str) -> list[dict]:
    """Split full book text by regex chapter headers."""
    try:
        compiled = re.compile(pattern, re.MULTILINE | re.UNICODE | re.IGNORECASE)
    except re.error:
        raise HTTPException(status_code=400, detail="Regex không hợp lệ")

    all_matches = list(compiled.finditer(full_text))
    if not all_matches:
        return []

    # Heuristic filtering for built-in chapter presets to avoid false positives
    # where normal body lines accidentally start with "Chương <n> ...".
    scored_matches: list[dict] = [
        {
            "match": m,
            "title": m.group(0).strip(),
            "score": _chapter_heading_score(m.group(0)),
            "detected": _extract_chapter_number(m.group(0).strip()),
        }
        for m in all_matches
    ]

    filtered_scored = [row for row in scored_matches if row["score"] >= 5]

    # If the heuristic is too strict (e.g. custom regex with special formats),
    # fallback to original matches to preserve user control.
    use_filtered = len(filtered_scored) >= 3 and len(filtered_scored) >= max(3, int(len(scored_matches) * 0.25))
    if use_filtered:
        selected_rows = filtered_scored
    else:
        selected_rows = scored_matches

    chapters = []
    next_auto_number = 1
    for i, row in enumerate(selected_rows):
        m = row["match"]
        start = m.start()
        end = selected_rows[i + 1]["match"].start() if i + 1 < len(selected_rows) else len(full_text)
        title = row["title"]
        content = full_text[start:end].strip()
        detected = row["detected"]
        score = row["score"]

        # Keep real chapter number if detectable; fallback to sequential.
        if detected is not None:
            number = detected
            if detected >= next_auto_number:
                next_auto_number = detected + 1
        else:
            number = next_auto_number
            next_auto_number += 1

        chapters.append({"number": number, "title": title, "content": content, "headingScore": score})

    return chapters


@router.post("/mod/epub")
async def mod_upload_epub(
    request: Request,
    file: UploadFile = File(...),
    preview: str = Form("false"),
    splitMode: str = Form("toc"),
    seriesMode: str = Form("none"),
    seriesId: str = Form(""),
    seriesName: str = Form(""),
    replaceExisting: str = Form("false"),
    appendTargetNovelId: str = Form(""),
    title: str = Form(""),
    authorName: str = Form(""),
    description: str = Form(""),
    regexInput: str = Form(""),
    regexPreset: str = Form(""),
    chapterRegex: str = Form(""),
    chapterRegexPreset: str = Form(""),
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_mod_user),
):
    preview_only = preview.lower() == "true"
    replace_existing = replaceExisting.lower() == "true"

    if not file.filename or not file.filename.lower().endswith(".epub"):
        raise HTTPException(status_code=400, detail="File phải có định dạng .epub")

    epub_data = await file.read()

    # Parse EPUB
    try:
        book = epublib.read_epub(io.BytesIO(epub_data), options={"ignore_ncx": False})
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Không thể đọc file EPUB: {e}")

    meta_title = title.strip() or (book.get_metadata("DC", "title") or [("",)])[0][0]
    meta_author = authorName.strip() or (book.get_metadata("DC", "creator") or [("",)])[0][0]
    meta_description = description.strip() or (book.get_metadata("DC", "description") or [("",)])[0][0]

    # Extract chapters
    preset_key: str = ""
    pattern: str = ""
    preview_chapters: list[dict] = []
    preview_stats = {
        "insertedMissingChapters": 0,
        "detectedMaxChapterNumber": 0,
        "chaptersFinal": 0,
    }

    if splitMode == "regex":
        # Keep backward compatibility with old web client payload keys.
        preset_key = (chapterRegexPreset or regexPreset or "").strip()
        preset_key = _REGEX_PRESET_ALIASES.get(preset_key, preset_key)
        pattern = (chapterRegex or regexInput or "").strip() or _REGEX_PRESETS.get(preset_key, "")
        if not pattern:
            raise HTTPException(status_code=400, detail="Cần nhập regex hoặc chọn preset")
        # Get full text from all spine items
        full_texts = []
        for item_id, _ in book.spine:
            item = book.get_item_with_id(item_id)
            if item and isinstance(item, epublib.EpubHtml):
                content = item.get_body_content()
                if content:
                    full_texts.append(_epub_html_to_text(content.decode("utf-8", errors="replace")))
        full_text = "\n".join(full_texts)
        chapters = _build_chapters_from_regex(full_text, pattern)
        chapters = _postprocess_extracted_chapters(chapters, "regex")
        preview_chapters, preview_stats = _normalize_chapter_preview(chapters)
    else:
        chapters = _build_chapters_from_toc(book)
        chapters = _postprocess_extracted_chapters(chapters, "toc")
        preview_chapters = chapters
        preview_stats = {
            "insertedMissingChapters": 0,
            "detectedMaxChapterNumber": len(chapters),
            "chaptersFinal": len(chapters),
        }

    if not chapters:
        raise HTTPException(status_code=400, detail="Không tìm thấy chương nào trong file EPUB")

    # Extract cover
    cover_url: str | None = None
    for item in book.get_items_of_type(ebooklib.ITEM_COVER):
        cover_url = await _upload_to_r2(item.get_content(), item.media_type or "image/jpeg", "covers", "cover.jpg")
        break
    if not cover_url:
        for item in book.get_items_of_type(ebooklib.ITEM_IMAGE):
            if "cover" in (item.file_name or "").lower():
                cover_url = await _upload_to_r2(item.get_content(), item.media_type or "image/jpeg", "covers", item.file_name)
                break

    if preview_only:
        parser_info = {
            "splitMode": splitMode,
            "chapterRegexUsed": pattern if splitMode == "regex" else None,
            "regexPreset": preset_key if splitMode == "regex" else None,
            "sourceSections": len(book.spine or []),
            "chaptersDetected": len(chapters),
            "chaptersFinal": preview_stats["chaptersFinal"],
            "insertedMissingChapters": preview_stats["insertedMissingChapters"],
            "detectedMaxChapterNumber": preview_stats["detectedMaxChapterNumber"],
        }
        return {
            "preview": True,
            "fileName": file.filename,
            "splitMode": splitMode,
            "detectedStructureType": "standard",
            "hasCoverFromEpub": bool(cover_url),
            "parserInfo": parser_info,
            "novel": {
                "title": meta_title,
                "authorName": meta_author,
                "description": meta_description,
                "detectedGenres": [],
                "totalChapters": preview_stats["chaptersFinal"],
            },
            "chaptersPreview": [
                {
                    "number": c.get("number", i + 1),
                    "title": c.get("title") or f"Chương {i + 1}",
                    "isPlaceholder": bool(c.get("isPlaceholder", False)),
                    "volumeNumber": None,
                    "volumeTitle": None,
                    "volumeChapterNumber": None,
                    "excerpt": (c.get("content") or "")[:180],
                }
                for i, c in enumerate(preview_chapters[:30])
            ],
            "totalChapters": preview_stats["chaptersFinal"],
            # Keep this field lightweight to avoid oversized responses through proxy.
            "sampleChapters": [
                {
                    "number": c.get("number", i + 1),
                    "title": c.get("title") or f"Chương {i + 1}",
                    "excerpt": (c.get("content") or "")[:240],
                    "isPlaceholder": bool(c.get("isPlaceholder", False)),
                }
                for i, c in enumerate(preview_chapters[:30])
            ],
            "meta": {"title": meta_title, "authorName": meta_author, "description": meta_description, "coverUrl": cover_url},
        }

    # Check duplicate title - ONLY when creating new novel
    # When appending (appendTargetNovelId exists), we don't check duplicate
    # because we already know which novel to append to
    if not appendTargetNovelId:  # Create mode only
        dup = await db.execute(
            text('SELECT id FROM "Novel" WHERE LOWER(title) = LOWER(:title) LIMIT 1'),
            {"title": meta_title},
        )
        if dup.first() and not replace_existing:
            raise HTTPException(status_code=409, detail="Truyện đã tồn tại", headers={"X-Error-Code": "DUPLICATE_TITLE"})

    # Extract genres from metadata
    genre_names: list[str] = []
    for meta_key in ("subject", "genre", "tag", "category"):
        for (value, _) in (book.get_metadata("DC", meta_key) or []):
            if value:
                genre_names.extend([v.strip() for v in value.split(",") if v.strip()])

    genre_ids: list[str] = []
    for gn in genre_names:
        g_result = await db.execute(
            text('SELECT id FROM "Genre" WHERE LOWER(name) = LOWER(:name) LIMIT 1'), {"name": gn}
        )
        row = g_result.mappings().first()
        if row:
            genre_ids.append(row["id"])

    # Determine mode and validate
    if appendTargetNovelId:
        # APPEND MODE: adding chapters to existing novel
        # Verify novel exists and user owns it
        novel_check = await db.execute(
            text('SELECT id, "uploaderId" FROM "Novel" WHERE id = :nid'),
            {"nid": appendTargetNovelId}
        )
        novel_row = novel_check.mappings().first()
        if not novel_row:
            raise HTTPException(status_code=404, detail="Không tìm thấy truyện đích")
        
        # Permission check
        if not _is_admin(user) and novel_row["uploaderId"] != user["id"]:
            raise HTTPException(status_code=403, detail="Không có quyền sửa truyện này")
        
        novel_id = appendTargetNovelId
        
        if replace_existing:
            # Replace mode: delete existing chapters and update novel metadata
            await db.execute(
                text(
                    'UPDATE "Novel" SET title = :title, "authorName" = :author, description = :desc, '
                    '"coverUrl" = COALESCE(:cover, "coverUrl"), "updatedAt" = NOW() WHERE id = :id'
                ),
                {"title": meta_title, "author": meta_author, "desc": meta_description, "cover": cover_url, "id": novel_id},
            )
            # Replace genres
            await db.execute(text('DELETE FROM "NovelGenre" WHERE "novelId" = :nid'), {"nid": novel_id})
            for gid in genre_ids:
                await db.execute(
                    text('INSERT INTO "NovelGenre" ("novelId", "genreId") VALUES (:nid, :gid) ON CONFLICT DO NOTHING'),
                    {"nid": novel_id, "gid": gid},
                )
            # Delete existing chapters to make room for new ones
            await mongo_db.chapters.delete_many({"novelId": novel_id})
        # else: append mode - just add chapters without modifying novel metadata
    else:
        # CREATE MODE: creating new novel with chapters from EPUB
        # Resolve series
        series_id = None
        if seriesMode == "existing" and seriesId:
            series_id = await _resolve_series_id(db, seriesId, None, user)
        elif seriesMode == "new" and seriesName:
            series_id = await _resolve_series_id(db, None, seriesName, user)

        base_slug = _slugify_unique(meta_title)
        count_result = await db.execute(
            text('SELECT COUNT(*) FROM "Novel" WHERE slug LIKE :pat'), {"pat": f"{base_slug}%"}
        )
        cnt = count_result.scalar() or 0
        slug = base_slug if cnt == 0 else f"{base_slug}-{int(cnt) + 1}"

        novel_id = _new_cuid()
        await db.execute(
            text(
                'INSERT INTO "Novel" (id, title, "originalTitle", slug, "authorName", description, '
                '"coverUrl", status, "uploaderId", "seriesId", "createdAt", "updatedAt") '
                "VALUES (:id, :title, NULL, :slug, :author, :desc, :cover, 'Đang ra', :uid, :sid, NOW(), NOW())"
            ),
            {
                "id": novel_id, "title": meta_title, "slug": slug,
                "author": meta_author, "desc": meta_description,
                "cover": cover_url, "uid": user["id"], "sid": series_id,
            },
        )
        for gid in genre_ids:
            await db.execute(
                text('INSERT INTO "NovelGenre" ("novelId", "genreId") VALUES (:nid, :gid) ON CONFLICT DO NOTHING'),
                {"nid": novel_id, "gid": gid},
            )

    # Insert chapters to MongoDB
    # For append mode: upsert by chapter number so existing chapters are overwritten correctly.
    if appendTargetNovelId:
        best_by_number: dict[int, dict] = {}
        for c in chapters:
            n = int(c.get("number") or 0)
            if n <= 0:
                continue
            prev = best_by_number.get(n)
            if prev is None or len((c.get("content") or "")) >= len((prev.get("content") or "")):
                best_by_number[n] = c

        ops = []
        for n in sorted(best_by_number.keys()):
            c = best_by_number[n]
            ops.append(
                UpdateOne(
                    {"novelId": novel_id, "number": n},
                    {
                        "$set": {
                            "title": c.get("title"),
                            "content": c.get("content"),
                            "volumeNumber": c.get("volumeNumber"),
                            "volumeTitle": c.get("volumeTitle"),
                            "volumeChapterNumber": c.get("volumeChapterNumber"),
                        }
                    },
                    upsert=True,
                )
            )

        if ops:
            await mongo_db.chapters.bulk_write(ops, ordered=False)
    else:
        docs = [
            {
                "novelId": novel_id,
                "number": c["number"],
                "title": c["title"],
                "content": c["content"],
                "volumeNumber": c.get("volumeNumber"),
                "volumeTitle": c.get("volumeTitle"),
                "volumeChapterNumber": c.get("volumeChapterNumber"),
            }
            for c in chapters
        ]
        if docs:
            await mongo_db.chapters.insert_many(docs)

    total = await _sync_total_chapters(db, novel_id)
    await db.commit()

    return {
        "novelId": novel_id,
        "title": meta_title,
        "totalChapters": total,
        "coverUrl": cover_url,
    }


# ---------------------------------------------------------------------------
# /api/mod/ai-tools
# ---------------------------------------------------------------------------


@router.get("/mod/ai-tools/novel-search")
async def mod_ai_novel_search(
    q: str = Query(""),
    page: int = Query(1, ge=1),
    missing: str = Query(""),
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_mod_user),
):
    if not q.strip() and missing.lower() != "true":
        return {"novels": [], "hasMore": False}

    skip = (page - 1) * 24
    conds = []
    params: dict[str, Any] = {"skip": skip}

    if not _is_admin(user):
        conds.append('(n."uploaderId" = :uid OR n."uploaderId" IS NULL)')
        params["uid"] = user["id"]

    if q.strip():
        conds.append('(n.title ILIKE :q OR n."authorName" ILIKE :q)')
        params["q"] = f"%{q.strip()}%"

    if missing.lower() == "true":
        conds.append(
            "(n.\"authorName\" IN ('', 'Chưa rõ') OR n.description IN ('', 'Chưa có giới thiệu', 'Không có giới thiệu'))"
        )

    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    result = await db.execute(
        text(
            f'SELECT n.id, n.title, n.slug, n."authorName", n."coverUrl", n.status, n."totalChapters" '
            f'FROM "Novel" n {where} ORDER BY n."updatedAt" DESC LIMIT 25 OFFSET :skip'
        ),
        params,
    )
    rows = result.mappings().all()
    return {"novels": [dict(r) for r in rows[:24]], "hasMore": len(rows) == 25}


@router.get("/mod/ai-tools/test-connection")
async def mod_ai_test_connection(user: dict = Depends(require_mod_user)):
    import time

    api_key = settings.deepseek_key
    if not api_key:
        raise HTTPException(status_code=503, detail="DEEPSEEK_KEY chưa được cấu hình")

    model = settings.deepseek_model or "deepseek-chat"
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://api.deepseek.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "temperature": 0.1,
                    "max_tokens": 10,
                    "messages": [{"role": "user", "content": "Ping! Please reply with 'pong'."}],
                },
            )
            resp.raise_for_status()
            data = resp.json()
            reply = data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return {"success": False, "message": str(e), "latencyMs": int((time.monotonic() - start) * 1000), "model": model}

    return {"success": True, "message": reply, "latencyMs": int((time.monotonic() - start) * 1000), "model": model}


def _build_enrich_prompt(novel: dict) -> str:
    return f"""Bạn là chuyên gia tìm kiếm thông tin web tiểu thuyết Trung Quốc/Hàn Quốc.
Hãy tìm kiếm thông tin chính xác cho truyện sau trên Qidian, JJWXC, NovelUpdates hoặc các nguồn uy tín khác.

Tiêu đề: {novel.get('title', '')}
Tên gốc: {novel.get('originalTitle', '') or 'Chưa rõ'}
Tác giả: {novel.get('authorName', '') or 'Chưa rõ'}
Thể loại: {novel.get('genres', '')}
Mô tả hiện tại: {(novel.get('description') or '')[:500]}

Trả về JSON với schema:
{{"results": [{{"title": "", "originalTitle": "", "authorName": "", "originalAuthorName": "",
"description": "", "coverUrl": "", "status": "Đang ra|Hoàn thành|Tạm ngưng",
"genresSuggested": [], "firstPublishYear": null, "confidence": 1-99, "source": "", "sourceUrl": ""}}]}}

Tối đa 3 kết quả, sắp xếp theo độ tin cậy giảm dần."""


@router.get("/mod/ai-tools/novel-enrich")
async def mod_ai_enrich(
    novelId: str = Query(...),
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_mod_user),
):
    novel_result = await db.execute(
        text('SELECT n.*, array_agg(g.name) AS genre_names FROM "Novel" n LEFT JOIN "NovelGenre" ng ON ng."novelId" = n.id LEFT JOIN "Genre" g ON g.id = ng."genreId" WHERE n.id = :id GROUP BY n.id'),
        {"id": novelId},
    )
    row = novel_result.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Không tìm thấy truyện")

    novel = dict(row)
    novel["genres"] = ", ".join(g for g in (novel.get("genre_names") or []) if g)

    prompt = _build_enrich_prompt(novel)
    attempts = []

    # DeepSeek
    api_key = settings.deepseek_key
    if api_key:
        model = settings.deepseek_model or "deepseek-chat"
        for max_tokens, timeout_s in [(1300, 60), (900, 45)]:
            try:
                async with httpx.AsyncClient(timeout=timeout_s) as client:
                    resp = await client.post(
                        "https://api.deepseek.com/v1/chat/completions",
                        headers={"Authorization": f"Bearer {api_key}"},
                        json={
                            "model": model, "temperature": 0.2, "max_tokens": max_tokens,
                            "response_format": {"type": "json_object"},
                            "messages": [
                                {"role": "system", "content": "You are a helpful assistant. Return only valid JSON."},
                                {"role": "user", "content": prompt},
                            ],
                        },
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    content = data["choices"][0]["message"]["content"]
                    import json as _json
                    parsed = _json.loads(content)
                    results = parsed.get("results", [])
                    attempts.append({"provider": "deepseek", "model": model, "success": True})
                    return {
                        "novel": novel, "provider": "deepseek", "model": model,
                        "attempts": attempts, "count": len(results), "results": results,
                    }
            except Exception as e:
                attempts.append({"provider": "deepseek", "model": model, "success": False, "error": str(e)})

    # OpenRouter fallback
    or_key = settings.openrouter_key
    if or_key and not (settings.openrouter_paused or "").lower().startswith("true"):
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                models_resp = await client.get(
                    "https://openrouter.ai/api/v1/models",
                    headers={"Authorization": f"Bearer {or_key}"},
                )
                all_models = models_resp.json().get("data", [])
                free_models = [m["id"] for m in all_models if m.get("id", "").endswith(":free")]
        except Exception:
            free_models = []

        for free_model in free_models[:5]:
            try:
                async with httpx.AsyncClient(timeout=22) as client:
                    resp = await client.post(
                        "https://openrouter.ai/api/v1/chat/completions",
                        headers={
                            "Authorization": f"Bearer {or_key}",
                            "HTTP-Referer": "http://localhost:3000",
                            "X-Title": "reader-mod-ai-tool",
                        },
                        json={
                            "model": free_model, "temperature": 0.2, "max_tokens": 1400,
                            "messages": [
                                {"role": "system", "content": "Return only valid JSON."},
                                {"role": "user", "content": prompt},
                            ],
                        },
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    content = data["choices"][0]["message"]["content"]
                    import json as _json
                    parsed = _json.loads(content)
                    results = parsed.get("results", [])
                    attempts.append({"provider": "openrouter", "model": free_model, "success": True})
                    return {
                        "novel": novel, "provider": "openrouter", "model": free_model,
                        "attempts": attempts, "count": len(results), "results": results,
                    }
            except Exception as e:
                attempts.append({"provider": "openrouter", "model": free_model, "success": False, "error": str(e)})

    return {"novel": novel, "provider": None, "model": None, "attempts": attempts, "count": 0, "results": []}


class BatchEnrichBody(BaseModel):
    novelIds: list[str]


@router.post("/mod/ai-tools/novel-enrich-batch")
async def mod_ai_enrich_batch(
    body: BatchEnrichBody,
    db: AsyncSession = Depends(get_db_session),
    user: dict = Depends(require_mod_user),
):
    import json as _json
    import time

    if len(body.novelIds) > 20:
        raise HTTPException(status_code=400, detail="Tối đa 20 truyện mỗi lần")

    result = await db.execute(
        text(
            'SELECT id, title, "originalTitle", "authorName", "originalAuthorName", description '
            'FROM "Novel" WHERE id = ANY(:ids)'
        ),
        {"ids": body.novelIds},
    )
    novels = [dict(r) for r in result.mappings()]
    if not novels:
        raise HTTPException(status_code=404, detail="Không tìm thấy truyện nào")

    def _build_batch_prompt(novels_list: list[dict]) -> str:
        parts = []
        for n in novels_list:
            parts.append(
                f"[ID: {n['id']}]\nTên: {n.get('title','')}\nTên gốc: {n.get('originalTitle','') or 'Chưa rõ'}\n"
                f"Tác giả: {n.get('authorName','') or 'Chưa rõ'}\nMô tả: {(n.get('description') or '')[:1500]}\n---"
            )
        return (
            "Bạn là chuyên gia tìm thông tin tiểu thuyết Trung/Hàn. Tìm thông tin cho các truyện sau:\n\n"
            + "\n".join(parts)
            + '\n\nTrả về JSON: {"results": [{"id":"...", "originalTitle":"...", "authorName":"...", "originalAuthorName":"...", "description":"...", "coverUrl":"...", "status":"..."}]}'
        )

    api_key = settings.deepseek_key
    if not api_key:
        raise HTTPException(status_code=503, detail="DEEPSEEK_KEY chưa được cấu hình")

    model = settings.deepseek_model or "deepseek-chat"
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            resp = await client.post(
                "https://api.deepseek.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model, "temperature": 0.2, "max_tokens": 2500,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {"role": "system", "content": "output only valid standard JSON object."},
                        {"role": "user", "content": _build_batch_prompt(novels)},
                    ],
                },
            )
            resp.raise_for_status()
            data = resp.json()
            parsed = _json.loads(data["choices"][0]["message"]["content"])
            results = parsed.get("results", [])
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"AI API lỗi: {e}")

    latency_ms = int((time.monotonic() - start) * 1000)
    return {
        "success": True, "latencyMs": latency_ms, "model": model,
        "count": len(results), "results": results, "sourceNovels": novels,
    }
