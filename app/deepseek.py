from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_LLM_ARTIFACT_RE = re.compile(
    r"<\s*/?\s*(?:pad|unk|s|bos|eos|mask|sep|cls|im_start|im_end)\s*>|<\|[^|>]*\|>",
    re.IGNORECASE,
)
_CORRUPTED_DE_FRAGMENT_RE = re.compile(r"(?<=\S)\s+de\s+(?=\S)", re.IGNORECASE)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…。])\s+")
_SHORT_DESC_MIN_SENTENCES = 4
_SHORT_DESC_MAX_SENTENCES = 12
_SHORT_DESC_MIN_SENTENCE_LEN = 10
_SUGGEST_MAX_TOKENS = 1800


def sanitize_llm_vietnamese_text(text: str) -> str:
    if not text:
        return ""
    cleaned = _LLM_ARTIFACT_RE.sub("", text)
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", cleaned)
    lines: list[str] = []
    for raw_line in cleaned.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = re.sub(r"\s+", " ", raw_line).strip(" ,;:-")
        if line:
            lines.append(line)
    return "\n".join(lines)


def _split_sentences(text: str) -> list[str]:
    compact = re.sub(r"\s+", " ", (text or "").strip())
    if not compact:
        return []
    return [part.strip() for part in _SENTENCE_SPLIT_RE.split(compact) if part.strip()]


def normalize_short_description_sentences(text: str) -> str:
    """Normalize to one sentence per line for consistent storage/UI."""
    return "\n".join(_split_sentences(text))


def validate_short_description(text: str) -> str | None:
    """Return rejection reason, or None when the description looks publishable."""
    if not text.strip():
        return "empty"

    if _LLM_ARTIFACT_RE.search(text):
        return "artifact_tokens"
    if re.search(r"<[^>\s]+>", text):
        return "residual_markup"
    if _CORRUPTED_DE_FRAGMENT_RE.search(text):
        return "corrupted_word_fragment"

    sentences = _split_sentences(text)
    if len(sentences) < _SHORT_DESC_MIN_SENTENCES:
        return "too_few_sentences"
    if len(sentences) > _SHORT_DESC_MAX_SENTENCES:
        return "too_many_sentences"

    for sentence in sentences:
        if len(sentence) < _SHORT_DESC_MIN_SENTENCE_LEN:
            return "sentence_too_short"
        if sentence.endswith("..."):
            return "truncated_sentence"
        if re.search(r"\s{2,}", sentence):
            return "irregular_spacing"

    compact = re.sub(r"\s+", "", text)
    if not compact:
        return "empty"
    letter_count = len(re.findall(r"[A-Za-zÀ-ỹ0-9]", compact))
    if letter_count / len(compact) < 0.82:
        return "low_readable_ratio"

    return None


def _short_description_quality_rules() -> str:
    return (
        "shortDescription quality rules (strict): "
        "Write 8-10 complete Vietnamese sentences; one sentence per line separated by newline. "
        "Use correct Vietnamese spelling, diacritics, and natural grammar. "
        "Every sentence must be a full clause — never cut off mid-word or mid-phrase. "
        "Output plain Vietnamese prose only: no HTML, markdown, placeholders, or special tokens "
        "(never output <pad>, <|...|>, <unk>, or similar). "
        "Do not leave corrupted fragments such as standalone 'de' between Vietnamese words. "
        "Prefer common Vietnamese words; avoid random English except proper nouns already in the title. "
        "Self-check spelling and coherence before returning JSON."
    )


def _parse_http_json(raw: str) -> Any:
    text = (raw or "").strip()
    if not text:
        raise ValueError("empty DeepSeek response body")

    done_idx = text.find("data: [DONE]")
    if done_idx != -1:
        text = text[:done_idx].rstrip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        obj, _end = decoder.raw_decode(text)
        return obj


def _collect_sse_payloads(raw: str) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        chunk = line[5:].strip()
        if not chunk or chunk == "[DONE]":
            continue
        try:
            parsed = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            payloads.append(parsed)
    return payloads


def _merge_streaming_completion(payloads: list[dict[str, Any]]) -> dict[str, Any]:
    merged: dict[str, Any] = {"choices": [{"message": {"role": "assistant", "content": ""}}]}
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    for payload in payloads:
        for choice in payload.get("choices") or []:
            delta = choice.get("delta") or {}
            message = choice.get("message") or {}
            for key, bucket in (
                ("content", content_parts),
                ("reasoning_content", reasoning_parts),
            ):
                piece = delta.get(key)
                if piece is None:
                    piece = message.get(key)
                if piece:
                    bucket.append(str(piece))
    if content_parts:
        merged["choices"][0]["message"]["content"] = "".join(content_parts)
    if reasoning_parts:
        merged["choices"][0]["message"]["reasoning_content"] = "".join(reasoning_parts)
    return merged


def _parse_completion_body(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if not text:
        raise ValueError("empty DeepSeek response body")

    if text.startswith("data:") or "\ndata:" in text:
        payloads = _collect_sse_payloads(text)
        if payloads:
            return _merge_streaming_completion(payloads)

    data = _parse_http_json(text)
    if not isinstance(data, dict):
        raise ValueError("DeepSeek response is not an object")
    return data


def _strip_json_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.IGNORECASE)
        stripped = re.sub(r"\s*```$", "", stripped)
    return stripped.strip()


def _parse_json_object(text: str) -> dict[str, Any] | None:
    candidate = _strip_json_fences(text)
    if not candidate:
        return None
    try:
        parsed = json.loads(candidate)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    try:
        decoder = json.JSONDecoder()
        obj, _end = decoder.raw_decode(candidate)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{[\s\S]*\}", candidate)
    if not match:
        return None
    try:
        obj = json.loads(match.group(0))
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def _normalize_message_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str) and item.strip():
                parts.append(item.strip())
            elif isinstance(item, dict):
                if item.get("type") == "text":
                    text = str(item.get("text") or "").strip()
                    if text:
                        parts.append(text)
                elif "text" in item:
                    text = str(item.get("text") or "").strip()
                    if text:
                        parts.append(text)
        return "\n".join(parts).strip()
    return str(content).strip()


def _extract_assistant_content(completion: dict[str, Any]) -> str:
    choice = (completion.get("choices") or [{}])[0] or {}
    message = choice.get("message") or {}

    content = _normalize_message_content(message.get("content"))
    if content:
        return content

    # deepseek-reasoner puts the chain-of-thought in reasoning_content and can
    # leave content empty when it embeds the final JSON there instead.
    reasoning = str(message.get("reasoning_content") or "").strip()
    if reasoning:
        parsed = _parse_json_object(reasoning)
        if parsed:
            return json.dumps(parsed, ensure_ascii=False)
        tail = reasoning[-4000:]
        parsed = _parse_json_object(tail)
        if parsed:
            return json.dumps(parsed, ensure_ascii=False)

    return ""


def _parse_suggest_result(completion: dict[str, Any]) -> dict[str, Any] | None:
    content = _extract_assistant_content(completion)
    if not content:
        return None
    return _parse_json_object(content)


def normalize_vietnamese_novel_status(raw: str | None) -> str:
    allowed = ("Đang ra", "Hoàn thành", "Tạm ngưng")
    s = " ".join((raw or "").split()).strip()
    if s in allowed:
        return s
    low = s.lower()
    if any(k in low for k in ("hoàn", "full", "complete", "end", "kết thúc")):
        return "Hoàn thành"
    if any(k in low for k in ("tạm ngưng", "drop", "hiatus", "đình chỉ")):
        return "Tạm ngưng"
    return "Đang ra"


def _chat_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    api_key = (settings.deepseek_api_key or "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _pick_chapter_samples(chapters: list[dict[str, Any]], *, snippet_len: int = 2000) -> list[str]:
    if not chapters:
        return []

    n = len(chapters)
    indices: list[int] = [0]
    if n > 1:
        indices.append(n // 4)
    if n > 2:
        indices.append(n // 2)
    if n > 3:
        indices.append((3 * n) // 4)
    if n > 1:
        indices.append(n - 1)

    seen: set[int] = set()
    samples: list[str] = []
    for idx in indices:
        if idx in seen:
            continue
        seen.add(idx)
        ch = chapters[idx]
        snippet = str(ch.get("txt") or "")[:snippet_len]
        samples.append(f"Chapter {ch.get('number')}: {ch.get('title')}\n{snippet}")
    return samples


async def ai_suggest_epub(
    title: str,
    author: str,
    chapters: list[dict[str, Any]],
    existing_genres: list[str],
    *,
    genre_hints: list[str] | None = None,
    map_genres: Callable[[list[str], list[str]], list[str]],
) -> dict[str, Any] | None:
    api_key = (settings.deepseek_api_key or "").strip()
    if not api_key:
        logger.warning("DeepSeek API key missing; skipping AI suggest")
        return None

    samples = _pick_chapter_samples(chapters)
    system_prompt = (
        "You are a Vietnamese fiction metadata assistant and copy editor. "
        "Return ONLY valid JSON (no markdown, no explanation) with exactly keys: genres, shortDescription, confidence, status. "
        "genres must be an array of 1-6 concise Vietnamese labels. "
        "You MAY invent NEW genre labels that are not listed in existingGenres when they fit the work better than any existing label; "
        "still prefer existingGenres when there is a clear semantic match (synonym). "
        "Do not output duplicates, slug format, or punctuation-only variants. "
        + _short_description_quality_rules()
        + " "
        "Cover setting, protagonist, core conflict, and genre tone without major spoilers. "
        "Match diction to the likely genre (tiên hiệp, đô thị, ngôn tình, trinh thám, etc.) and make it emotionally engaging. "
        "No direct quotes from chapter samples. "
        "confidence must be a number from 0 to 1. "
        "status must be EXACTLY one of these Vietnamese strings: \"Đang ra\", \"Hoàn thành\", \"Tạm ngưng\". "
        "Infer status from chapter samples and typical serialization cues; when unsure, use \"Đang ra\"."
    )
    user_prompt = {
        "title": title,
        "author": author,
        "totalChapters": len(chapters),
        "chapterSamples": samples,
        "existingGenres": existing_genres,
        "genreHints": genre_hints or [],
        "requirements": {
            "maxGenres": 6,
            "allowNewGenres": True,
            "preferExistingGenres": True,
            "allowCreatingNewGenreRecords": True,
            "language": "vi",
            "shortDescriptionSentences": "8-10",
            "vietnameseQuality": "strict_spelling_and_complete_sentences",
        },
    }

    model_id = (settings.deepseek_model or "deepseek-chat").strip() or "deepseek-chat"
    payload = {
        "model": model_id,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_prompt, ensure_ascii=False)},
        ],
        "temperature": 0.25,
        "max_tokens": _SUGGEST_MAX_TOKENS,
        "response_format": {"type": "json_object"},
    }

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{str(settings.deepseek_base_url).rstrip('/')}/chat/completions",
                headers=_chat_headers(),
                json=payload,
            )
        if response.status_code >= 400:
            logger.warning(
                "deepseek ai-suggest failed model=%s status=%s body=%s",
                model_id,
                response.status_code,
                (response.text or "")[:240],
            )
            return None

        completion = _parse_completion_body(response.text)
        parsed = _parse_suggest_result(completion)
        if not parsed:
            logger.warning("deepseek ai-suggest unparseable content model=%s", model_id)
            return None

        raw_genres = [str(g).strip() for g in (parsed.get("genres") or []) if str(g).strip()][:6]
        genres = map_genres(raw_genres, existing_genres)
        short_description = sanitize_llm_vietnamese_text(str(parsed.get("shortDescription") or ""))
        quality_issue = validate_short_description(short_description)
        novel_status = normalize_vietnamese_novel_status(str(parsed.get("status") or "").strip())
        try:
            confidence = float(parsed.get("confidence") or 0.0)
        except Exception:
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))

        if not short_description or not genres:
            logger.warning(
                "deepseek ai-suggest empty_fields model=%s genres=%s desc_len=%s",
                model_id,
                len(genres),
                len(short_description),
            )
            return None
        if quality_issue:
            logger.warning(
                "deepseek ai-suggest quality_issue=%s model=%s desc_len=%s",
                quality_issue,
                model_id,
                len(short_description),
            )
            return None

        short_description = normalize_short_description_sentences(short_description)
        logger.info("deepseek ai-suggest done model=%s", model_id)
        return {
            "suggestedGenres": genres,
            "shortDescription": short_description,
            "confidence": confidence,
            "model": model_id,
            "suggestedStatus": novel_status,
        }
    except Exception as exc:
        logger.warning("deepseek ai-suggest exception model=%s err=%s", model_id, exc)
        return None
