"""Single bounded render worker; run separately from the HTTP API.

The session advisory lock prevents two replicas from saturating the homelab.
A crashed process loses its lock; the next worker safely retries unfinished jobs.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import shutil
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import text

from app.audiobook_runtime import SynthRuntime
from app.audiobook_status import queue_status
from app.audiobook_voices import PREVIEW_TEXT, preview_directory
from app.audiobooks import (
    MODEL_VERSION,
    VOICES,
    digest,
    edition_manifest,
    ensure_audio_schema,
    queue_edition,
)
from app.database import SessionLocal, engine
from app.storage import storage

log = logging.getLogger("audiobook-worker")
synth_runtime = SynthRuntime()
_activity = {"phase": "starting", "assetId": None, "since": time.monotonic()}
_maintenance_started = None


def phase(name, asset_id=None):
    _activity.update(phase=name, assetId=asset_id, since=time.monotonic())
    log.info("Worker phase=%s asset=%s", name, asset_id or "-")


async def heartbeat():
    while True:
        try:
            status = await queue_status()
            log.info(
                "Queue heartbeat phase=%s asset=%s elapsed=%.0fs counts=%s "
                "eligible=%s backoff=%s exhausted=%s cleanupElapsed=%s",
                _activity["phase"],
                _activity["assetId"] or "-",
                time.monotonic() - _activity["since"],
                status["counts"],
                status["eligible"],
                status["backoff"],
                status["exhausted"],
                round(time.monotonic() - _maintenance_started)
                if _maintenance_started is not None
                else "idle",
            )
        except Exception:
            log.exception("Queue heartbeat failed")
        await asyncio.sleep(30)


async def maintenance_loop():
    global _maintenance_started
    while True:
        _maintenance_started = time.monotonic()
        log.info("Audio cleanup started in background")
        try:
            await cleanup_orphans()
            log.info(
                "Audio cleanup finished elapsed=%.1fs", time.monotonic() - _maintenance_started
            )
        except Exception:
            log.exception("Audio cleanup failed; render queue continues")
        finally:
            _maintenance_started = None
        await asyncio.sleep(3600)


@asynccontextmanager
async def background_tasks():
    # Exactly one cleanup scan at a time, never awaited by the queue consumer.
    tasks = [
        asyncio.create_task(maintenance_loop()),
        asyncio.create_task(heartbeat()),
        asyncio.create_task(requested_cleanup_loop()),
    ]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def run_process(*args, timeout=3600):
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    try:
        _, error = await asyncio.wait_for(proc.communicate(), timeout)
    except BaseException:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        raise
    if proc.returncode:
        raise RuntimeError(
            f"Process failed ({proc.returncode}): {error.decode(errors='replace')[-1500:]}"
        )


async def probe(path):
    proc = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
        stdout=asyncio.subprocess.PIPE,
    )
    out, _ = await asyncio.wait_for(proc.communicate(), 30)
    duration = float(out.strip())
    if proc.returncode or not 0 < duration < 86400:
        raise ValueError("Invalid audio duration")
    return duration


async def reconcile():
    async with SessionLocal() as db:
        editions = (
            (await db.execute(text('SELECT * FROM "AudioBookEdition" ORDER BY "createdAt"')))
            .mappings()
            .all()
        )
        for edition in editions:
            await queue_edition(db, edition)
        # Old pending revisions no longer represent the source; ready snapshots are retained.
        await db.execute(
            text("""UPDATE "AudioBookAsset" a SET status='superseded'
            FROM "ChapterContentRef" r WHERE r."chapterId"=a."chapterId"
            AND a.status IN ('queued','failed') AND
            (a."sourceHash"<>r."contentHash" OR a."modelVersion"<>:model)"""),
            {"model": MODEL_VERSION},
        )
        await db.commit()


async def claim():
    async with SessionLocal() as db:
        row = (
            (
                await db.execute(
                    text("""SELECT a.*, e."voiceId", e."novelId", c.number, r."txtHref"
            FROM "AudioBookAsset" a JOIN "AudioBookEdition" e ON e.id=a."editionId"
            JOIN "ChapterMeta" c ON c.id=a."chapterId"
            JOIN "ChapterContentRef" r ON r."chapterId"=c.id
            WHERE a.status IN ('queued','failed') AND a.attempts<3 AND a."retryAt"<=NOW()
            AND a."sourceHash"=r."contentHash" AND a."modelVersion"=:model
            ORDER BY c.number, e."createdAt", a.attempts LIMIT 1 FOR UPDATE OF a SKIP LOCKED"""),
                    {"model": MODEL_VERSION},
                )
            )
            .mappings()
            .first()
        )
        if not row:
            return None
        await db.execute(
            text("""UPDATE "AudioBookAsset" SET status='rendering',attempts=attempts+1,
            "updatedAt"=NOW(),error=NULL WHERE id=:id"""),
            {"id": row["id"]},
        )
        await db.commit()
        return dict(row)


async def render(job):
    phase("reading-source", job["id"])
    source = await asyncio.to_thread(storage.read_text, job["txtHref"])
    if hashlib.sha256(source.encode()).hexdigest() != job["sourceHash"]:
        raise ValueError("Chapter changed before rendering")
    # Store alongside the chapter text, without trusting IDs as path components.
    parent = storage._resolve(job["txtHref"]).parent
    destination = (
        parent / "audio" / digest(job["chapterId"]) / job["editionId"] / job["id"] / "chapter.m4a"
    )
    # WAV headers are repeatedly seeked/rewritten; keep intermediate I/O off NFS.
    with tempfile.TemporaryDirectory(prefix="reader-render-") as tmp:
        tmp = Path(tmp)
        (tmp / "source.txt").write_text(source, encoding="utf-8")
        voice = next(v["modelVoice"] for v in VOICES if v["id"] == job["voiceId"])
        # Reuse a warm model; a timeout/crash kills only the inference process.
        started = time.monotonic()
        phase("synthesizing", job["id"])
        await synth_runtime.render(tmp / "source.txt", tmp / "raw.wav", voice)
        phase("encoding", job["id"])
        await run_process(
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-i",
            str(tmp / "raw.wav"),
            "-c:a",
            "aac",
            "-b:a",
            "64k",
            "-ar",
            "24000",
            "-ac",
            "1",
            "-movflags",
            "+faststart",
            str(tmp / "chapter.m4a"),
        )
        duration = await probe(tmp / "chapter.m4a")
        elapsed = time.monotonic() - started
        log.info(
            "Rendered asset %s: %.1fs audio in %.1fs, RTF %.3f",
            job["id"],
            duration,
            elapsed,
            elapsed / duration,
        )
        checksum = await asyncio.to_thread(file_hash, tmp / "chapter.m4a")
        size = (tmp / "chapter.m4a").stat().st_size
        phase("publishing", job["id"])
        # Lock the chapter while publishing; deleted or changed sources must not publish.
        async with SessionLocal() as db:
            current = (
                (
                    await db.execute(
                        text("""SELECT a.id,a.attempts,a.status,r."contentHash"
                FROM "AudioBookAsset" a
                JOIN "ChapterMeta" c ON c.id=a."chapterId"
                JOIN "ChapterContentRef" r ON r."chapterId"=c.id
                WHERE a.id=:id FOR UPDATE OF c,r,a"""),
                        {"id": job["id"]},
                    )
                )
                .mappings()
                .first()
            )
            if (
                not current
                or current["attempts"] != job["attempts"] + 1
                or current["status"] != "rendering"
            ):
                return
            if current["contentHash"] != job["sourceHash"]:
                await db.execute(
                    text("UPDATE \"AudioBookAsset\" SET status='superseded' WHERE id=:id"),
                    {"id": job["id"]},
                )
                await db.commit()
                return
            await asyncio.to_thread(publish_audio, tmp / "chapter.m4a", destination, checksum)
            await db.execute(
                text("""UPDATE "AudioBookAsset" SET status='ready',href=:href,sha256=:sha,
                bytes=:bytes,duration=:duration,"updatedAt"=NOW(),error=NULL WHERE id=:id"""),
                {
                    "id": job["id"],
                    "href": str(destination.relative_to(storage.root)),
                    "sha": checksum,
                    "bytes": size,
                    "duration": duration,
                },
            )
            await db.commit()
        log.info(
            "Render ready asset=%s chapter=%s voice=%s bytes=%s audioSeconds=%.1f",
            job["id"],
            job["number"],
            job["voiceId"],
            size,
            duration,
        )


def file_hash(path):
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def publish_audio(source, destination, checksum):
    """Copy only a finished encoded file to NAS, verify it, then atomically publish."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=".publish-", dir=destination.parent, delete=False
    ) as out:
        staged = Path(out.name)
        try:
            with open(source, "rb") as src:
                shutil.copyfileobj(src, out)
            out.flush()
            os.fsync(out.fileno())
        except BaseException:
            staged.unlink(missing_ok=True)
            raise
    try:
        if file_hash(staged) != checksum:
            raise ValueError("Audio upload checksum mismatch")
        os.replace(staged, destination)
    finally:
        staged.unlink(missing_ok=True)


def escape_metadata(value):
    return re.sub(r"([\\=;#])", r"\\\1", str(value)).replace("\n", " ").replace("\r", " ")


async def export_one():
    """Only after the render queue drains. Reuse encoded audio; never synthesize it twice."""
    async with SessionLocal() as db:
        ids = (
            (await db.execute(text('SELECT id FROM "AudioBookEdition" ORDER BY "createdAt"')))
            .scalars()
            .all()
        )
        for eid in ids:
            manifest = await edition_manifest(db, eid)
            chapters = manifest["chapters"]
            if not chapters or any(c["status"] != "ready" or c["hasUpdate"] for c in chapters):
                continue
            if manifest["export"] and not manifest["export"]["hasUpdate"]:
                continue
            assets = []
            for chapter in chapters:
                href = (
                    await db.execute(
                        text('SELECT href FROM "AudioBookAsset" WHERE id=:id'),
                        {"id": chapter["assetId"]},
                    )
                ).scalar_one()
                assets.append(storage._resolve(href))
            revision = manifest["revision"]
            target = (
                storage.root
                / f"novel-{manifest['novelId']}"
                / "audiobook"
                / eid
                / "exports"
                / revision
            )
            # Retain the same containment check as chapter files.
            target = storage._resolve(str(target.relative_to(storage.root)))
            target.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="export-", dir=target) as tmp:
                tmp = Path(tmp)
                # Numbered symlinks avoid ffmpeg concat quoting of filesystem/user-controlled names.
                for i, path in enumerate(assets):
                    (tmp / f"{i}.m4a").symlink_to(path)
                (tmp / "list.txt").write_text(
                    "\n".join(f"file '{i}.m4a'" for i in range(len(assets)))
                )
                metadata = [";FFMETADATA1", "title=" + escape_metadata(manifest["title"])]
                cursor = 0
                for c in chapters:
                    end = cursor + round(c["duration"] * 1000)
                    metadata += [
                        "[CHAPTER]",
                        "TIMEBASE=1/1000",
                        f"START={cursor}",
                        f"END={end}",
                        "title=" + escape_metadata(c["title"] or f"Chương {c['number']}"),
                    ]
                    cursor = end
                (tmp / "metadata.txt").write_text("\n".join(metadata), encoding="utf-8")
                await run_process(
                    "ffmpeg",
                    "-v",
                    "error",
                    "-y",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    str(tmp / "list.txt"),
                    "-i",
                    str(tmp / "metadata.txt"),
                    "-map_metadata",
                    "1",
                    "-map_chapters",
                    "1",
                    "-c:a",
                    "copy",
                    "-movflags",
                    "+faststart",
                    str(tmp / "complete.m4b"),
                )
                await probe(tmp / "complete.m4b")
                os.replace(tmp / "complete.m4b", target / "complete.m4b")
            export_id = digest([eid, revision])
            await db.execute(
                text("""INSERT INTO "AudioBookExport"
                (id,"editionId",revision,href,bytes,"chapterCount")
                VALUES (:id,:eid,:rev,:href,:bytes,:count) ON CONFLICT DO NOTHING"""),
                {
                    "id": export_id,
                    "eid": eid,
                    "rev": revision,
                    "href": str((target / "complete.m4b").relative_to(storage.root)),
                    "bytes": (target / "complete.m4b").stat().st_size,
                    "count": len(chapters),
                },
            )
            await db.commit()
            return True
    return False


async def requested_cleanup_loop():
    while True:
        try:
            await cleanup_requested()
        except Exception:
            log.exception("Requested cleanup failed; will retry")
        await asyncio.sleep(30)


async def cleanup_requested():
    """Durable, bounded deletion of retired audio; never touches chapter text."""
    async with SessionLocal() as db:
        rows = (
            (
                await db.execute(
                    text('SELECT href FROM "AudioBookGarbage" ORDER BY "createdAt" LIMIT 100')
                )
            )
            .scalars()
            .all()
        )
        for href in rows:
            # Guard against accidental source-content deletion even if metadata is corrupt.
            if (
                not href.startswith("novel-")
                or "/audio/" not in href
                or not href.endswith("/chapter.m4a")
            ):
                log.error("Refusing unexpected audio cleanup path")
                continue
            referenced = (
                await db.execute(
                    text("""SELECT 1 FROM "AudioBookAsset" WHERE href=:href
                UNION ALL SELECT 1 FROM "AudioBookExport" WHERE href=:href LIMIT 1"""),
                    {"href": href},
                )
            ).first()
            if referenced:
                continue
            await asyncio.to_thread(storage.delete_href, href)
            await db.execute(
                text('DELETE FROM "AudioBookGarbage" WHERE href=:href'), {"href": href}
            )
            await db.commit()
        if rows:
            log.info("Requested audio cleanup processed files=%s", len(rows))


async def cleanup_orphans():
    # Remove only worker-owned immutable files with no DB reference after a 7-day grace period.
    async with SessionLocal() as db:
        known = set(
            (
                await db.execute(
                    text('''SELECT href FROM "AudioBookAsset" WHERE href IS NOT NULL
            UNION SELECT href FROM "AudioBookExport"''')
                )
            ).scalars()
        )

    def clean():
        for pattern in (
            "novel-*/audio/*/*/*/chapter.m4a",
            "novel-*/audiobook/*/exports/*/complete.m4b",
        ):
            for path in storage.root.glob(pattern):
                if (
                    str(path.relative_to(storage.root)) not in known
                    and path.stat().st_mtime < time.time() - 7 * 86400
                ):
                    path.unlink()

    await asyncio.to_thread(clean)


_preview_failures = {}


async def render_one_preview():
    """One shared sample per preset/revision; no inference in HTTP requests."""
    folder = preview_directory(storage.root)
    folder.mkdir(parents=True, exist_ok=True)
    for voice in VOICES:
        destination = folder / (voice["id"] + ".m4a")
        attempts, retry_at = _preview_failures.get(voice["id"], (0, 0))
        if destination.is_file() or attempts >= 3 or time.monotonic() < retry_at:
            continue
        try:
            with tempfile.TemporaryDirectory(prefix="reader-sample-") as tmp:
                tmp = Path(tmp)
                (tmp / "source.txt").write_text(PREVIEW_TEXT, encoding="utf-8")
                await asyncio.wait_for(
                    synth_runtime.render(tmp / "source.txt", tmp / "raw.wav", voice["modelVoice"]),
                    120,
                )
                await run_process(
                    "ffmpeg",
                    "-v",
                    "error",
                    "-y",
                    "-i",
                    str(tmp / "raw.wav"),
                    "-c:a",
                    "aac",
                    "-b:a",
                    "64k",
                    "-ar",
                    "24000",
                    "-ac",
                    "1",
                    "-movflags",
                    "+faststart",
                    str(tmp / "sample.m4a"),
                )
                duration = await probe(tmp / "sample.m4a")
                await asyncio.to_thread(
                    publish_audio, tmp / "sample.m4a", destination, file_hash(tmp / "sample.m4a")
                )
                log.info("Voice preview ready: %s (%.1fs)", voice["id"], duration)
            _preview_failures.pop(voice["id"], None)
        except Exception:
            _preview_failures[voice["id"]] = (attempts + 1, time.monotonic() + 300)
            log.exception("Voice preview failed: %s", voice["id"])
        return True
    return False


async def main():
    await ensure_audio_schema()
    # Separate connection owns the global lock for the lifetime of this worker.
    async with engine.connect() as lock:
        if not (await lock.execute(text("SELECT pg_try_advisory_lock(72592026)"))).scalar_one():
            raise RuntimeError("Another Audio book worker is active")
        async with SessionLocal() as db:
            await db.execute(
                text("""UPDATE "AudioBookAsset" SET status=
                    CASE WHEN attempts>=3 THEN 'failed' ELSE 'queued' END
                    WHERE status='rendering' """)
            )
            await db.commit()
        async with background_tasks():
            await consume_queue(lock)


async def consume_queue(lock):
    last_reconcile = 0
    while True:
        try:
            # Detect lost ownership before starting another job.
            await lock.execute(text("SELECT 1"))
            if time.monotonic() - last_reconcile > 60:
                phase("reconciling")
                await reconcile()
                last_reconcile = time.monotonic()
            preview_work = await render_one_preview()
            job = await claim()
            if job:
                log.info(
                    "Render claimed asset=%s novel=%s chapter=%s voice=%s attempt=%s",
                    job["id"],
                    job["novelId"],
                    job["number"],
                    job["voiceId"],
                    job["attempts"] + 1,
                )
                try:
                    await render(job)
                except Exception:
                    log.exception("Render failed: %s", job["id"])
                    async with SessionLocal() as db:
                        await db.execute(
                            text("""UPDATE "AudioBookAsset" SET status='failed',
                                error='Không thể tạo audio. Hệ thống sẽ thử lại tối đa 3 lần.',
                                "retryAt"=NOW()+INTERVAL '5 minutes',"updatedAt"=NOW()
                                WHERE id=:id AND status='rendering' AND attempts=:attempt"""),
                            {"id": job["id"], "attempt": job["attempts"] + 1},
                        )
                        await db.commit()
            else:
                phase("idle")
                await synth_runtime.release_if_idle()
                if not preview_work:
                    phase("checking-exports")
                    await export_one()
                phase("idle")
                await asyncio.sleep(0 if preview_work else 15)
        except Exception:
            # Exit on loss of DB lock/connection; Kubernetes restarts and reacquires it.
            log.exception("Worker interrupted")
            raise


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    async def run():
        try:
            await main()
        finally:
            await synth_runtime.close()

    asyncio.run(run())
