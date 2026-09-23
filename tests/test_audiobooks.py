"""Audio book integration tests on the same explicitly disposable database as sync tests."""

import asyncio
import hashlib
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import test_offline_sync as legacy
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.audiobook_worker import claim, export_one, render
from app.audiobooks import AudioRequest, edition_manifest, ensure_audio_schema, request_audio
from app.database import SessionLocal, engine
from app.main import app
from app.storage import storage


class AudioBookTests(legacy.OfflineSyncTests):
    async def asyncSetUp(self):
        await engine.dispose()
        async with engine.begin() as c:
            for name in [
                "AudioBookProgress",
                "AudioBookRequest",
                "AudioBookExport",
                "AudioBookAsset",
                "AudioBookEdition",
            ]:
                await c.execute(text(f'DROP TABLE IF EXISTS "{name}" CASCADE'))
        await super().asyncSetUp()
        async with engine.begin() as c:
            await c.execute(text('ALTER TABLE "ChapterContentRef" ADD COLUMN "txtHref" TEXT'))
        await ensure_audio_schema()
        self.auth = patch(
            "app.audiobooks.require_current_user", AsyncMock(return_value={"id": "user"})
        )
        self.auth.start()
        self.temp = tempfile.TemporaryDirectory()
        self.old_root = storage.root
        storage.root = Path(self.temp.name).resolve()

    async def asyncTearDown(self):
        self.auth.stop()
        storage.root = self.old_root
        self.temp.cleanup()
        await super().asyncTearDown()

    async def request_book(self, voice="anh-khoi"):
        async with SessionLocal() as db:
            return await request_audio("novel", AudioRequest(voiceId=voice), None, db)

    async def test_slow_cleanup_does_not_block_claiming_a_chapter(self):
        from app.audiobook_worker import background_tasks, consume_queue

        await self.request_book()
        cleanup_started = asyncio.Event()
        cleanup_cancelled = asyncio.Event()
        claimed = []

        async def stuck_cleanup():
            cleanup_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_cancelled.set()

        async def rendered(job):
            await cleanup_started.wait()
            self.assertFalse(cleanup_cancelled.is_set())
            claimed.append(job)
            raise asyncio.CancelledError()  # End the infinite consumer after the first claim.

        with (
            patch("app.audiobook_worker.cleanup_orphans", side_effect=stuck_cleanup),
            patch("app.audiobook_worker.render_one_preview", AsyncMock(return_value=False)),
            patch("app.audiobook_worker.render", side_effect=rendered),
        ):
            async with background_tasks():
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(consume_queue(AsyncMock()), timeout=3)
        self.assertEqual(len(claimed), 1)
        self.assertTrue(cleanup_cancelled.is_set())

    async def test_operator_status_reports_queue_backoff_and_active_chapter(self):
        from app.audiobook_status import queue_status

        await self.request_book()
        job = await claim()
        result = await queue_status()
        self.assertEqual(result["counts"], {"queued": 49, "rendering": 1})
        self.assertEqual(result["eligible"], 49)
        self.assertEqual(result["rendering"][0]["assetId"], job["id"])
        self.assertEqual(result["rendering"][0]["chapter"], 1)
        async with SessionLocal() as db:
            await db.execute(
                text("""UPDATE "AudioBookAsset" SET status='failed',
                "retryAt"=NOW()+INTERVAL '5 minutes' WHERE id=:id"""),
                {"id": job["id"]},
            )
            await db.commit()
        self.assertEqual((await queue_status())["backoff"], 1)
        async with SessionLocal() as db:
            await db.execute(
                text('UPDATE "AudioBookAsset" SET attempts=3 WHERE id=:id'), {"id": job["id"]}
            )
            await db.commit()
        result = await queue_status()
        self.assertEqual(result["backoff"], 0)
        self.assertEqual(result["exhausted"], 1)

    async def test_voice_catalog_and_cached_preview_range(self):
        from app.audiobook_voices import PREVIEW_REVISION, preview_directory

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            catalog = (await client.get("/api/audiobooks/voices")).json()
            self.assertEqual(len(catalog["voices"]), 23)
            self.assertEqual(len({v["id"] for v in catalog["voices"]}), 23)
            self.assertEqual([v["id"] for v in catalog["voices"] if v["default"]], ["anh-khoi"])
            self.assertTrue(all(v["previewUrl"] is None for v in catalog["voices"]))
            self.assertTrue(all("modelVoice" not in v for v in catalog["voices"]))
            folder = preview_directory(storage.root)
            folder.mkdir(parents=True)
            (folder / "anh-khoi.m4a").write_bytes(b"0123456789")
            catalog = (await client.get("/api/audiobooks/voices")).json()
            url = catalog["voices"][0]["previewUrl"]
            response = await client.get(url, headers={"Range": "bytes=2-5"})
            self.assertEqual(response.status_code, 206)
            self.assertEqual(response.content, b"2345")
            self.assertIn("immutable", response.headers["cache-control"])
            self.assertEqual((await client.head(url)).status_code, 200)
            self.assertEqual(
                (await client.get(url.replace(PREVIEW_REVISION, "old"))).status_code, 404
            )
            self.assertEqual(
                (await client.get(url.replace("anh-khoi", "unknown"))).status_code, 404
            )
        async with SessionLocal() as db:
            self.assertEqual(
                (await db.execute(text('SELECT COUNT(*) FROM "AudioBookAsset"'))).scalar_one(), 0
            )

    async def test_voice_preview_generated_once_and_failure_skips_to_next(self):
        import app.audiobook_worker as worker
        from app.audiobook_voices import preview_directory

        worker._preview_failures.clear()
        calls = []

        async def synth(source, output, voice):
            calls.append(voice)
            await worker.run_process(
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=0.3",
                str(output),
            )

        with patch.object(worker.synth_runtime, "render", synth):
            self.assertTrue(await worker.render_one_preview())
            first = preview_directory(storage.root) / "anh-khoi.m4a"
            stamp = first.stat().st_mtime_ns
            self.assertTrue(await worker.render_one_preview())
            self.assertEqual(first.stat().st_mtime_ns, stamp)
            self.assertEqual(calls, ["Anh Khôi", "Minh Đức"])
        with patch.object(
            worker.synth_runtime, "render", AsyncMock(side_effect=RuntimeError("test"))
        ):
            with self.assertLogs("audiobook-worker", level="ERROR"):
                await worker.render_one_preview()
        with patch.object(worker.synth_runtime, "render", synth):
            await worker.render_one_preview()
            self.assertEqual(calls[-1], "Thái Sơn")
        worker._preview_failures.clear()

    async def test_audio_duplicate_requests_and_parallel_voices(self):
        first = await self.request_book()
        second = await self.request_book()
        other = await self.request_book("ngoc-linh")
        self.assertEqual(first["id"], second["id"])
        self.assertNotEqual(first["id"], other["id"])
        async with SessionLocal() as db:
            self.assertEqual(
                (await db.execute(text('SELECT COUNT(*) FROM "AudioBookAsset"'))).scalar_one(), 100
            )

    async def test_audio_revision_retains_old_file_and_range(self):
        book = await self.request_book()
        asset = book["chapters"][0]["requestedAssetId"]
        (storage.root / "fixture.m4a").write_bytes(b"0123456789")
        async with SessionLocal() as db:
            await db.execute(
                text(
                    """UPDATE "AudioBookAsset" SET status='ready',
                    href='fixture.m4a',sha256='abc',bytes=10,duration=2
                    WHERE id=:id"""
                ),
                {"id": asset},
            )
            old = await edition_manifest(db, book["id"])
            await db.execute(
                text(
                    """UPDATE "ChapterContentRef" SET "contentHash"='new' WHERE "chapterId"='c1' """
                )
            )
            await db.commit()
        changed = await self.request_book()
        self.assertTrue(changed["chapters"][0]["hasUpdate"])
        self.assertEqual(changed["chapters"][0]["assetId"], asset)
        self.assertEqual(old["revision"], changed["revision"])
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                "/api/audiobooks/assets/" + asset, headers={"Range": "bytes=2-5"}
            )
            self.assertEqual(response.status_code, 206)
            self.assertEqual(response.content, b"2345")
            self.assertEqual(response.headers["content-range"], "bytes 2-5/10")
            response = await client.head("/api/audiobooks/assets/" + asset)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, b"")
            self.assertEqual(
                (
                    await client.get(
                        "/api/audiobooks/assets/" + asset, headers={"Range": "bytes=50-"}
                    )
                ).status_code,
                416,
            )

    async def test_audio_claim_retry_and_chapter_deletion(self):
        await self.request_book()
        first, second = await claim(), await claim()
        self.assertNotEqual(first["id"], second["id"])
        async with SessionLocal() as db:
            await db.execute(
                text('DELETE FROM "ChapterMeta" WHERE id=:id'), {"id": first["chapterId"]}
            )
            await db.commit()
            self.assertIsNone(
                (
                    await db.execute(
                        text('SELECT id FROM "AudioBookAsset" WHERE id=:id'), {"id": first["id"]}
                    )
                ).first()
            )

    async def test_retry_after_backoff_does_not_wait_behind_the_entire_book(self):
        await self.request_book()
        first = await claim()
        async with SessionLocal() as db:
            await db.execute(
                text("""UPDATE "AudioBookAsset" SET status='failed',
                "retryAt"=NOW()+INTERVAL '5 minutes' WHERE id=:id"""),
                {"id": first["id"]},
            )
            await db.commit()
        self.assertEqual((await claim())["number"], 2)
        async with SessionLocal() as db:
            await db.execute(
                text('UPDATE "AudioBookAsset" SET "retryAt"=NOW() WHERE id=:id'),
                {"id": first["id"]},
            )
            await db.commit()
        retry = await claim()
        self.assertEqual(retry["id"], first["id"])
        self.assertEqual(retry["attempts"], 1)  # The real attempt counter is preserved.

    async def test_audio_render_publish_and_full_export(self):
        content = "Xin chào. Đây là bản thử nghiệm."
        storage.write_text("novel-novel/1.txt", content)
        async with SessionLocal() as db:
            await db.execute(text("DELETE FROM \"ChapterMeta\" WHERE id<>'c1'"))
            await db.execute(
                text(
                    """UPDATE "ChapterContentRef" SET "contentHash"=:hash,
                    "txtHref"='novel-novel/1.txt'
                    WHERE "chapterId"='c1' """
                ),
                {"hash": hashlib.sha256(content.encode()).hexdigest()},
            )
            await db.commit()
        book = await self.request_book()
        job = await claim()
        from app.audiobook_worker import run_process as real_process

        async def synth(source, output, voice):
            self.assertFalse(Path(output).is_relative_to(storage.root))
            await real_process(
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=0.3",
                str(output),
            )

        with patch("app.audiobook_worker.synth_runtime.render", synth):
            await render(job)
            self.assertTrue(await export_one())
        async with SessionLocal() as db:
            result = await edition_manifest(db, book["id"])
            self.assertEqual(result["readyCount"], 1)
            self.assertIsNotNone(result["export"])
            self.assertFalse(result["export"]["hasUpdate"])
        self.assertFalse(await export_one())

    async def test_failed_upload_does_not_replace_published_audio(self):
        from app.audiobook_worker import publish_audio

        target = storage.root / "chapter.m4a"
        target.write_bytes(b"original audio")
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "audio.m4a"
            source.write_bytes(b"new audio")
            with self.assertRaisesRegex(ValueError, "checksum"):
                publish_audio(source, target, "wrong-checksum")
            self.assertEqual(target.read_bytes(), b"original audio")
            self.assertEqual(list(storage.root.glob(".publish-*")), [])
            publish_audio(source, target, hashlib.sha256(b"new audio").hexdigest())
            self.assertEqual(target.read_bytes(), b"new audio")

    async def test_audio_progress_does_not_rewind_or_change_legacy(self):
        book = await self.request_book()
        asset = book["chapters"][0]["requestedAssetId"]
        async with SessionLocal() as db:
            await db.execute(
                text("UPDATE \"AudioBookAsset\" SET status='ready',duration=100 WHERE id=:id"),
                {"id": asset},
            )
            await db.commit()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            body = {
                "editionId": book["id"],
                "assetId": asset,
                "position": 50,
                "eventId": "new",
                "occurredAt": "2025-01-02T00:00:00Z",
            }
            self.assertEqual(
                (await client.post("/api/audiobooks/progress/novel", json=body)).status_code, 200
            )
            body.update(position=5, eventId="old", occurredAt="2025-01-01T00:00:00Z")
            self.assertEqual(
                (await client.post("/api/audiobooks/progress/novel", json=body)).json()[
                    "acknowledgedEventId"
                ],
                "old",
            )
            self.assertEqual(
                (await client.get("/api/audiobooks/progress/novel")).json()["progress"]["position"],
                50,
            )
        async with SessionLocal() as db:
            self.assertEqual(
                (await db.execute(text('SELECT COUNT(*) FROM "Bookmark"'))).scalar_one(), 0
            )
