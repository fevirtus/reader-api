from __future__ import annotations

import html as html_lib
import re
from pathlib import Path
from typing import Any

import html2text
from ebooklib import ITEM_DOCUMENT
from ebooklib import epub as epublib

_CHAPTER_MARKER_TEXT_RE = re.compile(
    r"(?:ch(?:u(?:ơng|ong))?|chapter|hồi|hoi|phần|phan|tập|tap|quyển|quyen)\s*\d+",
    re.IGNORECASE,
)


def html_to_text(html_content: str) -> str:
    h = html2text.HTML2Text()
    h.ignore_links = True
    h.ignore_images = True
    h.ignore_emphasis = False
    h.body_width = 0
    return h.handle(html_content).strip()


def _html_to_text(html_content: str) -> str:
    return html_to_text(html_content)


def build_merged_html_from_epub(epub_path: Path) -> str:
    book = epublib.read_epub(str(epub_path), options={"ignore_ncx": False})
    parts: list[str] = []
    for item in book.get_items_of_type(ITEM_DOCUMENT):
        content = item.get_content().decode("utf-8", errors="replace")
        if content.strip():
            parts.append(content)
    return "\n".join(parts)


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


def count_html_tag_opens(html: str, tag: str) -> int:
    tag_re = re.escape(tag.strip().lower())
    return len(re.findall(rf"<{tag_re}\b", html, flags=re.IGNORECASE))


def _strip_tags_to_text(fragment: str) -> str:
    return html_lib.unescape(re.sub(r"<[^>]+>", " ", fragment or "")).strip()


def _title_from_tag_opening(opening_attrs: str, fragment: str, tag: str) -> str:
    tag_re = re.escape(tag)
    for attr in ("title", "alt"):
        match = re.search(rf'{attr}\s*=\s*["\']([^"\']+)["\']', opening_attrs, flags=re.IGNORECASE)
        if match:
            title = html_lib.unescape(match.group(1)).strip()
            if title and len(title) <= 160:
                return title
    for attr in ("id", "name"):
        match = re.search(rf'{attr}\s*=\s*["\']([^"\']+)["\']', opening_attrs, flags=re.IGNORECASE)
        if match:
            title = html_lib.unescape(match.group(1)).strip()
            if title and not title.startswith("#") and len(title) <= 160:
                return title
    close_match = re.search(
        rf"<{tag_re}\b[^>]*>(.*?)</{tag_re}>",
        fragment,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not close_match:
        return ""
    inner = _strip_tags_to_text(close_match.group(1))
    if inner and len(inner) <= 160:
        return inner
    return ""


def _anchor_seems_chapter_marker(opening_attrs: str, inner_text: str) -> bool:
    text = (inner_text or "").strip()
    if text and _CHAPTER_MARKER_TEXT_RE.search(text):
        return True
    attrs = opening_attrs or ""
    if re.search(r'\bhref\s*=\s*["\'][^"\']*\.xhtml', attrs, flags=re.IGNORECASE):
        return True
    if re.search(
        r'\b(?:id|name)\s*=\s*["\'][^"\']*(?:chuong|chương|chapter|ch\d|c\d|hoi|hồi)',
        attrs,
        flags=re.IGNORECASE,
    ):
        return True
    # TOC / nav links thường có text ngắn.
    if text and len(text) <= 120:
        return True
    return False


def _derive_simple_chapter_title(txt: str, number: int) -> str:
    for line in (txt or "").splitlines():
        cleaned = line.strip()
        if cleaned:
            return cleaned[:160]
    return f"Chương {number}"


def extract_chapters_by_html_tag(
    epub_path: Path,
    tag: str,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Tách chương tại mỗi thẻ mở `<tag ...>`. Trả về (chapters, stats)."""
    merged_html = build_merged_html_from_epub(epub_path)
    stats = {"tagOpens": 0, "tagOpensUsed": 0, "tagOpensFiltered": 0}
    if not merged_html.strip():
        return [], stats

    tag_name = tag.strip().lower()
    tag_re = re.escape(tag_name)
    opener_re = re.compile(rf"<({tag_re})\b([^>]*)>", re.IGNORECASE)
    matches = list(opener_re.finditer(merged_html))
    stats["tagOpens"] = len(matches)
    if not matches:
        return [], stats

    if tag_name == "a" and len(matches) > 300:
        filtered: list[re.Match[str]] = []
        for match in matches:
            attrs = match.group(2) or ""
            rest = merged_html[match.end() : match.end() + 800]
            close = re.search(rf"</{tag_re}>", rest, flags=re.IGNORECASE)
            inner_html = rest[: close.start()] if close else rest
            inner_text = _strip_tags_to_text(inner_html)
            if _anchor_seems_chapter_marker(attrs, inner_text):
                filtered.append(match)
        if filtered:
            stats["tagOpensFiltered"] = len(matches) - len(filtered)
            matches = filtered

    chapters: list[dict[str, Any]] = []
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(merged_html)
        raw_html = merged_html[start:end].strip()
        if not raw_html:
            continue

        opening_attrs = match.group(2) or ""
        txt = html_to_text(raw_html)
        inline_title = _title_from_tag_opening(opening_attrs, raw_html, tag_name)
        number = len(chapters) + 1
        title = inline_title or _derive_simple_chapter_title(txt, number)

        # Bỏ qua anchor rỗng không có tiêu đề và không có nội dung theo sau.
        if not txt.strip() and not inline_title:
            tag_only = re.fullmatch(
                rf"<{tag_re}\b[^>]*>\s*(?:</{tag_re}>\s*)?",
                raw_html,
                flags=re.IGNORECASE | re.DOTALL,
            )
            if tag_only:
                continue

        chapters.append(
            {
                "number": number,
                "title": title,
                "raw_html": raw_html,
                "txt": txt,
            }
        )

    stats["tagOpensUsed"] = len(matches)
    return chapters, stats
