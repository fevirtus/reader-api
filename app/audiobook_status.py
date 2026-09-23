"""Read-only operator view: python -m app.audiobook_status (inside an API pod)."""

import asyncio
import json

from sqlalchemy import text

from app.audiobook_voices import MODEL_VERSION
from app.database import SessionLocal, engine


async def queue_status():
    async with SessionLocal() as db:
        counts = dict(
            (
                await db.execute(
                    text('SELECT status, COUNT(*) FROM "AudioBookAsset" GROUP BY status')
                )
            ).all()
        )
        waiting = (
            (
                await db.execute(
                    text('''SELECT
            COUNT(*) FILTER (WHERE status IN ('queued','failed') AND attempts<3
                AND "retryAt"<=NOW() AND a."sourceHash"=r."contentHash"
                AND a."modelVersion"=:model) AS eligible,
            COUNT(*) FILTER (WHERE status='failed' AND attempts<3
                AND "retryAt">NOW()) AS backoff,
            COUNT(*) FILTER (WHERE status='failed' AND attempts>=3) AS exhausted,
            MIN(a."updatedAt") FILTER (WHERE status='queued') AS "oldestQueuedAt"
            FROM "AudioBookAsset" a LEFT JOIN "ChapterContentRef" r
            ON r."chapterId"=a."chapterId"'''),
                    {"model": MODEL_VERSION},
                )
            )
            .mappings()
            .one()
        )
        active = (
            (
                await db.execute(
                    text("""SELECT a.id AS "assetId", e."novelId",
            e."voiceId", c.number AS chapter, a.attempts, a."updatedAt" AS "startedAt",
            EXTRACT(EPOCH FROM (NOW()-a."updatedAt"))::int AS "elapsedSeconds"
            FROM "AudioBookAsset" a
            JOIN "AudioBookEdition" e ON e.id=a."editionId"
            JOIN "ChapterMeta" c ON c.id=a."chapterId"
            WHERE a.status='rendering' ORDER BY a."updatedAt" LIMIT 10""")
                )
            )
            .mappings()
            .all()
        )
        lock = (
            await db.execute(
                text("""SELECT EXISTS (SELECT 1 FROM pg_locks
            WHERE locktype='advisory' AND classid=0 AND objid=72592026
            AND objsubid=1 AND granted)""")
            )
        ).scalar_one()
        return {
            "counts": counts,
            **dict(waiting),
            "workerLockHeld": lock,
            "rendering": [dict(row) for row in active],
        }


async def main():
    try:
        print(json.dumps(await queue_status(), ensure_ascii=False, indent=2, default=str))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
