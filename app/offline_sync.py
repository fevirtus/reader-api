"""Durable per-account ordering and tombstones for offline reading edits."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_current_user
from app.database import get_db_session

router = APIRouter()
DDL = """
CREATE TABLE IF NOT EXISTS "ReaderSyncClock" (
  "userId" TEXT NOT NULL REFERENCES "User"(id) ON DELETE CASCADE,
  "novelId" TEXT NOT NULL,
  "occurredAt" TIMESTAMPTZ NOT NULL,
  "eventId" TEXT NOT NULL,
  deleted BOOLEAN NOT NULL DEFAULT false,
  PRIMARY KEY ("userId", "novelId")
)
"""


async def ensure_sync_schema():
    from app.database import engine

    async with engine.begin() as conn:
        await conn.execute(text(DDL))


async def lock_bookmark(db, user_id, novel_id):
    # Also serializes first INSERT and read-modify-write history merges.
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"{user_id}:{novel_id}"},
    )


async def write_clock(db, user_id, novel_id, occurred_at, event_id, deleted=False):
    await db.execute(
        text("""
      INSERT INTO "ReaderSyncClock" ("userId", "novelId", "occurredAt", "eventId", deleted)
      VALUES (:user_id, :novel_id, :at, :event, :deleted)
      ON CONFLICT ("userId", "novelId") DO UPDATE SET
        "occurredAt" = EXCLUDED."occurredAt", "eventId" = EXCLUDED."eventId",
        deleted = EXCLUDED.deleted
    """),
        {
            "user_id": user_id,
            "novel_id": novel_id,
            "at": occurred_at,
            "event": event_id,
            "deleted": deleted,
        },
    )


async def record_online_operation(db, user_id, novel_id, deleted=False):
    await lock_bookmark(db, user_id, novel_id)
    await write_clock(
        db, user_id, novel_id, dt.datetime.now(dt.timezone.utc), str(uuid.uuid4()), deleted
    )


def operation_wins(at, event_id, previous):
    if previous is not None and previous["eventId"] == event_id:
        return False
    return previous is None or (at, event_id) > (previous["occurredAt"], previous["eventId"])


class SyncOperation(BaseModel):
    eventId: str = Field(min_length=1, max_length=100)
    kind: Literal["progress", "markAsRead", "remove"]
    novelId: str = Field(min_length=1, max_length=200)
    occurredAt: dt.datetime
    chapterId: str | None = None
    chapterNumber: int | None = Field(default=None, ge=1)
    progress: float | None = Field(default=None, ge=0)


@router.post("/api/user/sync")
async def sync_operation(
    payload: SyncOperation, request: Request, db: AsyncSession = Depends(get_db_session)
):
    from app.main import _load_bookmark_with_novel, _new_id, _update_reading_progress

    user = await require_current_user(request, db)
    uid, nid = user["id"], payload.novelId
    await lock_bookmark(db, uid, nid)
    clock = (
        (
            await db.execute(
                text(
                    'SELECT "occurredAt", "eventId", deleted FROM "ReaderSyncClock" '
                    'WHERE "userId" = :uid AND "novelId" = :nid'
                ),
                {"uid": uid, "nid": nid},
            )
        )
        .mappings()
        .first()
    )
    now = dt.datetime.now(dt.timezone.utc)
    at = payload.occurredAt
    if at.tzinfo is None:
        at = at.replace(tzinfo=dt.timezone.utc)
    at = min(at.astimezone(dt.timezone.utc), now)
    wins = operation_wins(at, payload.eventId, clock)
    status = "applied" if wins else "merged"
    exists = (await db.execute(text('SELECT id FROM "Novel" WHERE id = :id'), {"id": nid})).first()

    if not exists:
        status = "novel_deleted"
    elif payload.kind == "progress":
        chapter = (
            (
                await db.execute(
                    text(
                        'SELECT id, number FROM "ChapterMeta" WHERE id = :id AND "novelId" = :nid'
                    ),
                    {"id": payload.chapterId, "nid": nid},
                )
            )
            .mappings()
            .first()
        )
        if not chapter:
            status = "chapter_deleted"
        elif clock and clock["deleted"] and not wins:
            status = "superseded"
        else:
            await _update_reading_progress(
                db,
                uid,
                nid,
                chapter["id"],
                chapter["number"],
                sync_managed=True,
                update_position=wins,
            )
    elif payload.kind == "remove" and wins:
        result = await db.execute(
            text('DELETE FROM "Bookmark" WHERE "userId" = :uid AND "novelId" = :nid'),
            {"uid": uid, "nid": nid},
        )
        if result.rowcount:
            await db.execute(
                text(
                    'UPDATE "Novel" SET "bookmarkCount" = GREATEST("bookmarkCount"-1,0) '
                    'WHERE id=:id'
                ),
                {"id": nid},
            )
    elif payload.kind == "markAsRead" and wins:
        existing = (
            await db.execute(
                text('SELECT id FROM "Bookmark" WHERE "userId"=:uid AND "novelId"=:nid'),
                {"uid": uid, "nid": nid},
            )
        ).first()
        if existing:
            await db.execute(
                text(
                    'UPDATE "Bookmark" SET "markedAsRead"=true '
                    'WHERE "userId"=:uid AND "novelId"=:nid'
                ),
                {"uid": uid, "nid": nid},
            )
        else:
            await db.execute(
                text("""INSERT INTO "Bookmark"
                (id,"userId","novelId","readChapters","hasCountedView","markedAsRead","createdAt")
                VALUES (:id,:uid,:nid,'{}',false,true,NOW())"""),
                {"id": _new_id("bm_"), "uid": uid, "nid": nid},
            )
            await db.execute(
                text('UPDATE "Novel" SET "bookmarkCount"="bookmarkCount"+1 WHERE id=:id'),
                {"id": nid},
            )

    if wins and status not in {"chapter_deleted", "novel_deleted"}:
        await write_clock(db, uid, nid, at, payload.eventId, payload.kind == "remove")
    bookmark = await _load_bookmark_with_novel(db, uid, nid) if exists else None
    await db.commit()
    return {"acknowledgedEventId": payload.eventId, "status": status, "bookmark": bookmark}
