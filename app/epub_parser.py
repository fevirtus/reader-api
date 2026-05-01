from __future__ import annotations

from pathlib import Path
from typing import Any

import html2text
from ebooklib import ITEM_DOCUMENT
from ebooklib import epub as epublib


def _html_to_text(html_content: str) -> str:
    h = html2text.HTML2Text()
    h.ignore_links = True
    h.ignore_images = True
    h.ignore_emphasis = False
    h.body_width = 0
    return h.handle(html_content).strip()


def build_chapters_from_epub(epub_path: Path) -> list[dict[str, Any]]:
    book = epublib.read_epub(str(epub_path), options={"ignore_ncx": False})
    out: list[dict[str, Any]] = []
    idx = 1
    for item in book.get_items_of_type(ITEM_DOCUMENT):
        content = item.get_content().decode("utf-8", errors="replace")
        txt = _html_to_text(content)
        if not txt:
            continue
        out.append(
            {
                "number": idx,
                "title": item.get_name() or f"Chapter {idx}",
                "content": content,
                "txt": txt,
            }
        )
        idx += 1
    return out
