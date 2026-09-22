# ruff: noqa: E402
"""Integration tests: TEST_DATABASE_URL must point to a disposable PostgreSQL DB."""

import asyncio
import datetime as dt
import os
import tempfile
import unittest
import uuid
from unittest.mock import AsyncMock, patch
from urllib.parse import urlparse

URL = os.environ.get("TEST_DATABASE_URL", "")
if (
    not URL
    or urlparse(URL).hostname not in {"127.0.0.1", "localhost"}
    or urlparse(URL).path != "/reader_sync_test"
):
    raise unittest.SkipTest(
        "Set TEST_DATABASE_URL to the disposable localhost reader_sync_test database"
    )
os.environ["DATABASE_URL"] = URL
_temp_content = tempfile.TemporaryDirectory(prefix="reader-sync-test-")
os.environ["NAS_CONTENT_ROOT"] = _temp_content.name

from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.database import SessionLocal, engine
from app.main import _update_reading_progress, app, download_manifest
from app.offline_sync import SyncOperation, ensure_sync_schema, sync_operation


@unittest.skipUnless(
    URL and "reader_sync_test" in URL, "Requires disposable reader_sync_test database"
)
class OfflineSyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Separate connections/event loops for each test.
        await engine.dispose()
        self.schema = "test_" + uuid.uuid4().hex
        self.uid = "user"
        self.at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
        self.patch = patch(
            "app.offline_sync.require_current_user", AsyncMock(return_value={"id": self.uid})
        )
        self.patch.start()
        async with engine.begin() as c:
            # This database is dedicated to this suite; no production URL is accepted.
            for table in [
                "ReaderSyncClock",
                "Bookmark",
                "ChapterContentRef",
                "ChapterMeta",
                "NovelViewDaily",
                "Novel",
                "User",
            ]:
                await c.execute(text(f'DROP TABLE IF EXISTS "{table}" CASCADE'))
            for sql in [
                'CREATE TABLE "User" (id TEXT PRIMARY KEY)',
                """CREATE TABLE "Novel" (id TEXT PRIMARY KEY,
                    title TEXT DEFAULT 'Novel', slug TEXT DEFAULT 'novel',
                   "authorName" TEXT DEFAULT 'Author', "coverUrl" TEXT,
                    status TEXT DEFAULT 'ONGOING',
                   "totalChapters" INT DEFAULT 100, rating FLOAT DEFAULT 0,
                    "ratingCount" INT DEFAULT 0,
                   "bookmarkCount" INT DEFAULT 0, views INT DEFAULT 0)""",
                """CREATE TABLE "Bookmark" (id TEXT PRIMARY KEY, "userId" TEXT, "novelId" TEXT,
                   "lastChapterId" TEXT, "lastChapterNumber" INT, "readChapters" INT[] DEFAULT '{}',
                   "hasCountedView" BOOLEAN DEFAULT false, "markedAsRead" BOOLEAN DEFAULT false,
                   "createdAt" TIMESTAMPTZ DEFAULT NOW(), UNIQUE("userId","novelId"))""",
                """CREATE TABLE "ChapterMeta" (id TEXT PRIMARY KEY, "novelId" TEXT,
                    number INT, title TEXT)""",
                """CREATE TABLE "ChapterContentRef" ("chapterId" TEXT PRIMARY KEY,
                    "contentHash" TEXT)""",
                """CREATE TABLE "NovelViewDaily" (id TEXT PRIMARY KEY, "novelId" TEXT, day DATE,
                   views INT, "createdAt" TIMESTAMPTZ, "updatedAt" TIMESTAMPTZ,
                    UNIQUE("novelId",day))""",
                """INSERT INTO "User" VALUES ('user')""",
                """INSERT INTO "Novel" (id) VALUES ('novel')""",
                """INSERT INTO "ChapterMeta" SELECT 'c'||i,'novel',i,'Chapter '||i
                    FROM generate_series(1,50) i""",
                '''INSERT INTO "ChapterContentRef" SELECT id,'hash-'||id FROM "ChapterMeta"''',
            ]:
                await c.execute(text(sql))
        await ensure_sync_schema()

    async def asyncTearDown(self):
        self.patch.stop()
        await engine.dispose()

    async def send(self, kind="progress", chapter=1, seconds=0, event_id=None):
        event_id = event_id or str(uuid.uuid4())
        async with SessionLocal() as db:
            return await sync_operation(
                SyncOperation(
                    eventId=event_id,
                    kind=kind,
                    novelId="novel",
                    chapterId=f"c{chapter}",
                    chapterNumber=chapter,
                    occurredAt=self.at + dt.timedelta(seconds=seconds),
                ),
                None,
                db,
            )

    async def test_stale_progress_merges_history_without_rewinding(self):
        await self.send(chapter=30, seconds=30)
        result = await self.send(chapter=20, seconds=20)
        self.assertEqual(result["bookmark"]["lastChapterNumber"], 30)
        self.assertEqual(set(result["bookmark"]["readChapters"]), {20, 30})

    async def test_newer_reread_can_move_backwards(self):
        await self.send(chapter=30, seconds=30)
        result = await self.send(chapter=2, seconds=40)
        self.assertEqual(result["bookmark"]["lastChapterNumber"], 2)

    async def test_simultaneous_first_writes_keep_history_and_count_once(self):
        await asyncio.gather(*(self.send(chapter=i, seconds=i) for i in range(1, 11)))
        async with SessionLocal() as db:
            row = (await db.execute(text('SELECT * FROM "Bookmark"'))).mappings().one()
            self.assertEqual(set(row["readChapters"]), set(range(1, 11)))
            self.assertEqual(row["lastChapterNumber"], 10)
            self.assertEqual((await db.execute(text('SELECT views FROM "Novel"'))).scalar_one(), 1)

    async def test_delete_tombstone_blocks_older_progress_and_retry(self):
        await self.send(chapter=5, seconds=5)
        removed = await self.send(kind="remove", seconds=20)
        self.assertIsNone(removed["bookmark"])
        old = await self.send(chapter=10, seconds=10)
        self.assertIsNone(old["bookmark"])
        newer = await self.send(chapter=2, seconds=30)
        self.assertEqual(newer["bookmark"]["lastChapterNumber"], 2)

    async def test_deleted_chapter_is_acknowledged_without_poisoning_queue(self):
        result = await self.send(chapter=999)
        self.assertEqual(result["status"], "chapter_deleted")
        self.assertIn("acknowledgedEventId", result)
        self.assertIsNone(result["bookmark"])

    async def test_legacy_online_update_wins_over_old_offline_operation(self):
        async with SessionLocal() as db:
            await _update_reading_progress(db, self.uid, "novel", "c40", 40)
            await db.commit()
        result = await self.send(chapter=10)
        self.assertEqual(result["bookmark"]["lastChapterNumber"], 40)
        self.assertIn(10, result["bookmark"]["readChapters"])

    async def test_web_bookmark_http_contract_and_offline_interop(self):
        # Exact payloads sent by reader/lib/bookmark-context.tsx, without sync fields.
        with patch("app.main.require_current_user", AsyncMock(return_value={"id": self.uid})):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/user/bookmarks",
                    json={
                        "action": "updateProgress",
                        "novelId": "novel",
                        "lastChapterId": "c40",
                        "lastChapterNumber": 40,
                    },
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["status"], "updated")
                self.assertEqual(response.json()["bookmark"]["lastChapterNumber"], 40)
                await self.send(chapter=10)
                response = await client.get("/api/user/bookmarks")
                self.assertEqual(response.status_code, 200)
                bookmarks = response.json()
                self.assertIsInstance(bookmarks, list)
                self.assertEqual(bookmarks[0]["lastChapterNumber"], 40)
                self.assertEqual(set(bookmarks[0]["readChapters"]), {10, 40})
                self.assertEqual(bookmarks[0]["novel"]["id"], "novel")
                response = await client.post(
                    "/api/user/bookmarks",
                    json={
                        "action": "markAsRead",
                        "novelId": "novel",
                    },
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["status"], "marked")
                self.assertTrue(response.json()["bookmark"]["markedAsRead"])
                response = await client.get("/api/user/bookmarks?shelfStatus=completed")
                self.assertEqual(len(response.json()), 1)
                response = await client.delete("/api/user/bookmarks/novel")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {"status": "removed"})
                self.assertIsNone((await self.send(chapter=20))["bookmark"])
                self.assertEqual((await client.get("/api/user/bookmarks")).json(), [])

    async def test_existing_progress_endpoint_accepts_original_payload(self):
        with patch("app.main.require_current_user", AsyncMock(return_value={"id": self.uid})):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/user/reading-progress",
                    json={
                        "novelId": "novel",
                        "chapterId": "c2",
                        "chapterNumber": 2,
                    },
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["status"], "updated")
                self.assertEqual(response.json()["bookmark"]["lastChapterId"], "c2")

    async def test_retry_after_commit_is_idempotent(self):
        event_id = "same-event"
        first = await self.send(chapter=30, seconds=30, event_id=event_id)
        again = await self.send(chapter=30, seconds=30, event_id=event_id)
        self.assertEqual(first["bookmark"], again["bookmark"])
        async with SessionLocal() as db:
            self.assertEqual(
                (await db.execute(text('SELECT "bookmarkCount" FROM "Novel"'))).scalar_one(), 1
            )

    async def test_manifest_revision_changes_on_edit_or_delete(self):
        async with SessionLocal() as db:
            initial = await download_manifest("novel", db)
            await db.execute(
                text(
                    'UPDATE "ChapterContentRef" SET "contentHash"=\'changed\' '
                    "WHERE \"chapterId\"='c1'"
                )
            )
            edited = await download_manifest("novel", db)
            self.assertNotEqual(initial["revision"], edited["revision"])
            await db.execute(text("DELETE FROM \"ChapterMeta\" WHERE id='c2'"))
            deleted = await download_manifest("novel", db)
            self.assertNotEqual(edited["revision"], deleted["revision"])


if __name__ == "__main__":
    unittest.main()
