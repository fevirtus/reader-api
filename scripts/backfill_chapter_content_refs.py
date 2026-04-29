from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path
from bson import ObjectId
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings
from app.database import SessionLocal, mongo_db
from app.storage import storage


async def backfill(limit: int, dry_run: bool, after_id: str | None, state_file: str | None) -> None:
    query = {
        "$or": [
            {"content": {"$exists": True, "$type": "string", "$ne": ""}},
            {"contentHtml": {"$exists": True, "$type": "string", "$ne": ""}},
        ]
    }
    if after_id:
        query["_id"] = {"$gt": ObjectId(after_id)}

    docs = (
        await mongo_db["chapters"]
        .find(query, {"content": 1, "contentHtml": 1})
        .sort("_id", 1)
        .limit(limit)
        .to_list(limit)
    )

    mapped = 0
    skipped = 0
    async with SessionLocal() as db:
        for doc in docs:
            chapter_id = str(doc.get("_id") or "")
            if not chapter_id:
                skipped += 1
                continue

            exists = (
                await db.execute(
                    text('SELECT "chapterId" FROM "ChapterContentRef" WHERE "chapterId" = :id LIMIT 1'),
                    {"id": chapter_id},
                )
            ).mappings().first()
            if exists:
                skipped += 1
                continue

            txt = str(doc.get("content") or "").strip()
            raw_html = str(doc.get("contentHtml") or doc.get("content") or "")
            if not txt:
                skipped += 1
                continue

            txt_href = f"legacy/{chapter_id}.txt"
            raw_href = f"legacy/{chapter_id}.raw.html"
            content_hash = hashlib.sha256(txt.encode("utf-8")).hexdigest()

            if not dry_run:
                storage.write_text(txt_href, txt)
                storage.write_text(raw_href, raw_html)
                await db.execute(
                    text(
                        'INSERT INTO "ChapterContentRef" ("chapterId", "txtHref", "rawHtmlHref", "contentHash") '
                        'VALUES (:chapter_id, :txt_href, :raw_href, :hash) '
                        'ON CONFLICT ("chapterId") DO NOTHING'
                    ),
                    {
                        "chapter_id": chapter_id,
                        "txt_href": txt_href,
                        "raw_href": raw_href,
                        "hash": content_hash,
                    },
                )
            mapped += 1

        if not dry_run:
            await db.commit()

    last_id = str(docs[-1]["_id"]) if docs else None
    summary = {
        "scanned": len(docs),
        "mapped": mapped,
        "skipped": skipped,
        "dryRun": dry_run,
        "contentRoot": settings.nas_content_root,
        "nextAfterId": last_id,
    }
    if state_file and last_id and not dry_run:
        Path(state_file).write_text(json.dumps({"afterId": last_id}, ensure_ascii=True), encoding="utf-8")
    print(summary)


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill ChapterContentRef from Mongo chapters")
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--after-id", type=str, default="")
    parser.add_argument("--state-file", type=str, default="")
    args = parser.parse_args()
    after_id = args.after_id.strip() or None
    state_file = args.state_file.strip() or None
    if state_file and not after_id:
        p = Path(state_file)
        if p.exists():
            try:
                after_id = json.loads(p.read_text(encoding="utf-8")).get("afterId")
            except Exception:
                after_id = None
    asyncio.run(backfill(limit=args.limit, dry_run=args.dry_run, after_id=after_id, state_file=state_file))


if __name__ == "__main__":
    main()
