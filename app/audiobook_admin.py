"""Moderator-only Audio book operations. No source text is read or deleted here."""

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.audiobook_status import queue_status
from app.audiobook_voices import MODEL_VERSION
from app.audiobooks import queue_edition
from app.auth import require_mod_user
from app.database import get_db_session

router = APIRouter(prefix="/api/audiobooks/admin", tags=["Audio book administration"])

# Only obsolete versions with a CURRENT ready replacement and no saved progress.
OBSOLETE = """a."editionId"=:eid AND a.status='ready'
 AND a."updatedAt" < NOW()-INTERVAL '7 days'
 AND EXISTS (SELECT 1 FROM "AudioBookAsset" b JOIN "ChapterContentRef" r
 ON r."chapterId"=b."chapterId" WHERE b."editionId"=a."editionId"
 AND b."chapterId"=a."chapterId" AND b.status='ready' AND b.id<>a.id
 AND b."sourceHash"=r."contentHash" AND b."modelVersion"=:model)
 AND NOT EXISTS (SELECT 1 FROM "AudioBookProgress" p WHERE p."assetId"=a.id)"""


@router.get("")
async def overview(
    request: Request,
    q: str = Query("", max_length=160),
    page: int = Query(1, ge=1),
    db: AsyncSession = Depends(get_db_session),
):
    await require_mod_user(request, db)
    params = {"q": "%" + q + "%", "offset": (page - 1) * 20, "model": MODEL_VERSION}
    count = (
        await db.execute(
            text("""SELECT COUNT(*) FROM "AudioBookEdition" e
      JOIN "Novel" n ON n.id=e."novelId" WHERE n.title ILIKE :q"""),
            params,
        )
    ).scalar_one()
    rows = (
        (
            await db.execute(
                text("""SELECT e.id, e."novelId", e."voiceId", n.title,
      (SELECT COUNT(*) FROM "ChapterMeta" c WHERE c."novelId"=n.id) AS total,
      (SELECT COUNT(DISTINCT a."chapterId") FROM "AudioBookAsset" a
       WHERE a."editionId"=e.id AND a.status='ready') AS ready,
      (SELECT COALESCE(SUM(bytes),0) FROM "AudioBookAsset" a WHERE a."editionId"=e.id)
       AS "audioBytes",
      (SELECT COALESCE(SUM(bytes),0) FROM "AudioBookExport" x WHERE x."editionId"=e.id)
       AS "exportBytes",
      COALESCE(s.queued,0) AS queued, COALESCE(s.rendering,0) AS rendering,
      COALESCE(s.failed,0) AS failed, COALESCE(s.current,0) AS current
      FROM "AudioBookEdition" e JOIN "Novel" n ON n.id=e."novelId"
      LEFT JOIN LATERAL (SELECT
       COUNT(*) FILTER (WHERE a.status='queued') AS queued,
       COUNT(*) FILTER (WHERE a.status='rendering') AS rendering,
       COUNT(*) FILTER (WHERE a.status='failed') AS failed,
       COUNT(*) FILTER (WHERE a.status='ready') AS current
       FROM "AudioBookAsset" a JOIN "ChapterContentRef" r ON r."chapterId"=a."chapterId"
       WHERE a."editionId"=e.id AND a."sourceHash"=r."contentHash" AND a."modelVersion"=:model)
       s ON TRUE
      WHERE n.title ILIKE :q ORDER BY e."createdAt" DESC,e.id LIMIT 20 OFFSET :offset"""),
                params,
            )
        )
        .mappings()
        .all()
    )
    sizes = (
        (
            await db.execute(
                text('''SELECT
      (SELECT COALESCE(SUM(bytes),0) FROM "AudioBookAsset") AS "audioBytes",
      (SELECT COALESCE(SUM(bytes),0) FROM "AudioBookExport") AS "exportBytes",
      (SELECT COALESCE(SUM(bytes),0) FROM "AudioBookGarbage") AS "cleanupBytes"''')
            )
        )
        .mappings()
        .one()
    )
    items = []
    for row in rows:
        item = dict(row)
        args = {"eid": row["id"], "model": MODEL_VERSION}
        clean = (
            (
                await db.execute(
                    text(
                        "SELECT COUNT(*) AS files, COALESCE(SUM(bytes),0) AS bytes "
                        f'FROM "AudioBookAsset" a WHERE {OBSOLETE}'
                    ),
                    args,
                )
            )
            .mappings()
            .one()
        )
        item["cleanup"] = dict(clean)
        item["active"] = [
            dict(r)
            for r in (
                await db.execute(
                    text("""SELECT c.number, a.attempts,
          EXTRACT(EPOCH FROM (NOW()-a."updatedAt"))::int AS "elapsedSeconds"
          FROM "AudioBookAsset" a JOIN "ChapterMeta" c ON c.id=a."chapterId"
          WHERE a."editionId"=:eid AND a.status='rendering' """),
                    args,
                )
            ).mappings()
        ]
        item["errors"] = [
            dict(r)
            for r in (
                await db.execute(
                    text("""SELECT c.number,a.attempts,a.error,a."retryAt"
          FROM "AudioBookAsset" a JOIN "ChapterMeta" c ON c.id=a."chapterId"
          JOIN "ChapterContentRef" r ON r."chapterId"=c.id
          WHERE a."editionId"=:eid AND a.status='failed' AND a."sourceHash"=r."contentHash"
          AND a."modelVersion"=:model ORDER BY c.number LIMIT 5"""),
                    args,
                )
            ).mappings()
        ]
        items.append(item)
    return {
        "items": items,
        "total": count,
        "page": page,
        "pageSize": 20,
        "storage": dict(sizes),
        "queue": await queue_status(),
    }


async def edition_for_admin(db, eid):
    row = (
        (
            await db.execute(
                text('SELECT * FROM "AudioBookEdition" WHERE id=:eid FOR UPDATE'), {"eid": eid}
            )
        )
        .mappings()
        .first()
    )
    if not row:
        raise HTTPException(404, "Không tìm thấy bản Audio book")
    return row


@router.post("/editions/{eid}/render")
async def render_more(eid: str, request: Request, db: AsyncSession = Depends(get_db_session)):
    await require_mod_user(request, db)
    edition = await edition_for_admin(db, eid)
    added = await queue_edition(db, edition)
    result = await db.execute(
        text("""UPDATE "AudioBookAsset" a SET status='queued',attempts=0,
      "retryAt"=NOW(),error=NULL,"updatedAt"=NOW() FROM "ChapterContentRef" r
      WHERE a."editionId"=:eid AND a.status='failed' AND r."chapterId"=a."chapterId"
      AND a."sourceHash"=r."contentHash" AND a."modelVersion"=:model"""),
        {"eid": eid, "model": MODEL_VERSION},
    )
    await db.commit()
    return {"added": added, "retried": result.rowcount}


@router.post("/editions/{eid}/cleanup")
async def cleanup(eid: str, request: Request, db: AsyncSession = Depends(get_db_session)):
    await require_mod_user(request, db)
    await edition_for_admin(db, eid)
    args = {"eid": eid, "model": MODEL_VERSION}
    # Fence progress writes; recheck in a fresh statement after any lock wait.
    await db.execute(
        text(f'SELECT a.id FROM "AudioBookAsset" a WHERE {OBSOLETE} FOR UPDATE OF a'), args
    )
    rows = (
        (
            await db.execute(
                text(f'DELETE FROM "AudioBookAsset" a WHERE {OBSOLETE} RETURNING href, bytes'), args
            )
        )
        .mappings()
        .all()
    )
    for row in rows:
        if row["href"]:
            await db.execute(
                text("""INSERT INTO "AudioBookGarbage"(href,bytes)
              VALUES (:href,:bytes) ON CONFLICT DO NOTHING"""),
                {"href": row["href"], "bytes": row["bytes"] or 0},
            )
    await db.commit()
    return {"files": len(rows), "bytes": sum(r["bytes"] or 0 for r in rows), "queued": True}
