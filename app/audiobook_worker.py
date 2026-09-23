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
import sys
import tempfile
import time
from pathlib import Path

from sqlalchemy import text

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
            ORDER BY a.attempts, e."createdAt", c.number LIMIT 1 FOR UPDATE OF a SKIP LOCKED"""),
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
    source = await asyncio.to_thread(storage.read_text, job["txtHref"])
    if hashlib.sha256(source.encode()).hexdigest() != job["sourceHash"]:
        raise ValueError("Chapter changed before rendering")
    # Store alongside the chapter text, without trusting IDs as path components.
    parent = storage._resolve(job["txtHref"]).parent
    destination = (
        parent / "audio" / digest(job["chapterId"]) / job["editionId"] / job["id"] / "chapter.m4a"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="render-", dir=destination.parent) as tmp:
        tmp = Path(tmp)
        (tmp / "source.txt").write_text(source, encoding="utf-8")
        voice = next(v["modelVoice"] for v in VOICES if v["id"] == job["voiceId"])
        # Inference is isolated: a hung native runtime can be killed and retried safely.
        await run_process(
            os.getenv("AUDIOBOOK_SYNTH_PYTHON", sys.executable),
            "-m",
            "app.audiobook_synthesize",
            str(tmp / "source.txt"),
            str(tmp / "raw.wav"),
            voice,
            timeout=int(os.getenv("AUDIOBOOK_RENDER_TIMEOUT", "3600")),
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
            str(tmp / "chapter.m4a"),
        )
        duration = await probe(tmp / "chapter.m4a")
        checksum = await asyncio.to_thread(file_hash, tmp / "chapter.m4a")
        size = (tmp / "chapter.m4a").stat().st_size
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
            os.replace(tmp / "chapter.m4a", destination)
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


def file_hash(path):
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


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


async def main():
    await ensure_audio_schema()
    # Separate connection owns the global lock for the lifetime of this worker.
    async with engine.connect() as lock:
        if not (await lock.execute(text("SELECT pg_try_advisory_lock(72592026)"))).scalar_one():
            raise RuntimeError("Another Audio book worker is active")
        async with SessionLocal() as db:
            await db.execute(
                text("UPDATE \"AudioBookAsset\" SET status='queued' WHERE status='rendering'")
            )
            await db.commit()
        last_reconcile = 0
        while True:
            try:
                # Detect lost ownership before starting another job.
                await lock.execute(text("SELECT 1"))
                if time.monotonic() - last_reconcile > 60:
                    await reconcile()
                    last_reconcile = time.monotonic()
                job = await claim()
                if job:
                    try:
                        await render(job)
                    except Exception:
                        log.exception("Render failed: %s", job["id"])
                        async with SessionLocal() as db:
                            await db.execute(
                                text("""UPDATE "AudioBookAsset" SET status='failed',
                                error='Không thể tạo audio. Hệ thống sẽ thử lại tối đa 3 lần.',
                                "retryAt"=NOW()+INTERVAL '5 minutes',"updatedAt"=NOW()
                                WHERE id=:id"""),
                                {"id": job["id"]},
                            )
                            await db.commit()
                else:
                    await export_one()
                    await cleanup_orphans()
                    await asyncio.sleep(15)
            except Exception:
                # Exit on loss of DB lock/connection; Kubernetes restarts and reacquires it.
                log.exception("Worker interrupted")
                raise


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
