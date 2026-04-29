from __future__ import annotations

import hashlib
from pathlib import Path

from app.config import settings


class NasContentStorage:
    def __init__(self, root_dir: str):
        self.root = Path(root_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, href: str) -> Path:
        rel = href.strip().lstrip("/")
        target = (self.root / rel).resolve()
        if self.root not in target.parents and target != self.root:
            raise ValueError("Invalid storage href")
        return target

    def read_text(self, href: str) -> str:
        path = self._resolve(href)
        return path.read_text(encoding="utf-8")

    def write_text(self, href: str, content: str) -> dict[str, str | int]:
        path = self._resolve(href)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        return {"href": href, "sha256": digest, "size": len(content.encode("utf-8"))}


storage = NasContentStorage(settings.nas_content_root)
