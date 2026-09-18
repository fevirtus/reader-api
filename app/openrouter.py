from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_MODEL_CACHE: dict[str, Any] = {"expires_at": 0.0, "catalog": None}
_CACHE_TTL_SECONDS = 600
_MODEL_COOLDOWN: dict[str, float] = {}
_COOLDOWN_HTTP_STATUSES = frozenset({400, 401, 402, 404, 429})

_EXCLUDED_ID_FRAGMENTS = ("vision", "image", "audio", "realtime", "embedding", "moderation")

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


def classify_model_tier(item: dict[str, Any]) -> str:
    pricing = item.get("pricing") or {}
    prompt = str(pricing.get("prompt") or "0").strip()
    completion = str(pricing.get("completion") or "0").strip()
    if prompt == "0" and completion == "0":
        return "free"
    return "paid"


def is_text_chat_model(item: dict[str, Any]) -> bool:
    model_id = str(item.get("id") or "").strip()
    if not model_id:
        return False
    low = model_id.lower()
    if any(fragment in low for fragment in _EXCLUDED_ID_FRAGMENTS):
        return False

    arch = item.get("architecture") or {}
    output_modalities = arch.get("output_modalities") or []
    if output_modalities and not all(str(m).lower() == "text" for m in output_modalities):
        return False

    modality = str(arch.get("modality") or "").lower()
    if modality and "text->text" not in modality and "text" not in modality:
        return False

    return True


def _supports_structured_output(item: dict[str, Any]) -> bool:
    params = item.get("supported_parameters") or []
    normalized = {str(p).lower() for p in params}
    return "response_format" in normalized or "structured_outputs" in normalized


def _model_family(model_id: str) -> str:
    low = model_id.lower()
    if "gpt" in low or low.startswith("openai/"):
        return "openai"
    if "deepseek" in low or low.startswith("ds/") or "/ds/" in low:
        return "deepseek"
    if "claude" in low or "anthropic" in low:
        return "claude"
    if "gemini" in low or "google" in low:
        return "gemini"
    return "other"


def _cooldown_seconds() -> float:
    return float(max(60, int(settings.router_model_cooldown_seconds or 1200)))


def _mark_model_cooldown(model_id: str) -> None:
    mid = (model_id or "").strip()
    if not mid:
        return
    _MODEL_COOLDOWN[mid] = time.time() + _cooldown_seconds()


def _is_model_cooling(model_id: str) -> bool:
    mid = (model_id or "").strip()
    if not mid:
        return False
    until = _MODEL_COOLDOWN.get(mid)
    if until is None:
        return False
    if until <= time.time():
        _MODEL_COOLDOWN.pop(mid, None)
        return False
    return True


def _serialize_model_item(item: dict[str, Any]) -> dict[str, Any]:
    pricing = item.get("pricing") or {}
    return {
        "id": str(item.get("id") or ""),
        "name": str(item.get("name") or item.get("id") or ""),
        "contextLength": int(item.get("context_length") or 0),
        "pricing": {
            "prompt": str(pricing.get("prompt") or "0"),
            "completion": str(pricing.get("completion") or "0"),
        },
        "tier": classify_model_tier(item),
        "supportsStructuredOutput": _supports_structured_output(item),
    }


def _parse_http_json(raw: str) -> Any:
    text = (raw or "").strip()
    if not text:
        raise ValueError("empty OpenRouter response body")

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


def _parse_completion_body(raw: str, *, model_id: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if not text:
        raise ValueError("empty OpenRouter response body")

    if text.startswith("data:") or "\ndata:" in text:
        payloads = _collect_sse_payloads(text)
        if payloads:
            return _merge_streaming_completion(payloads)

    data = _parse_http_json(text)
    if not isinstance(data, dict):
        raise ValueError(f"OpenRouter response is not an object for model={model_id}")
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


def _extract_assistant_content(completion: dict[str, Any], model_id: str) -> str:
    choice = (completion.get("choices") or [{}])[0] or {}
    message = choice.get("message") or {}
    family = _model_family(model_id)

    content = _normalize_message_content(message.get("content"))
    if content:
        return content

    if family == "deepseek":
        reasoning = str(message.get("reasoning_content") or "").strip()
        if reasoning:
            parsed = _parse_json_object(reasoning)
            if parsed:
                return json.dumps(parsed, ensure_ascii=False)
            tail = reasoning[-4000:]
            parsed = _parse_json_object(tail)
            if parsed:
                return json.dumps(parsed, ensure_ascii=False)

    if family == "gemini":
        parts = message.get("parts")
        if isinstance(parts, list):
            return _normalize_message_content(parts)

    return ""


def _parse_suggest_result(completion: dict[str, Any], model_id: str) -> dict[str, Any] | None:
    content = _extract_assistant_content(completion, model_id)
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


def _api_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    api_key = (settings.router_api_key or "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _chat_headers() -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "HTTP-Referer": "http://localhost:3000",
        "X-Title": "reader-import-ai-suggest",
    }
    api_key = (settings.router_api_key or "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _partition_models(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    free_models: list[dict[str, Any]] = []
    paid_models: list[dict[str, Any]] = []
    for item in items:
        if not is_text_chat_model(item):
            continue
        tier = classify_model_tier(item)
        if tier == "free":
            free_models.append(item)
        else:
            paid_models.append(item)

    def sort_key(item: dict[str, Any]) -> tuple[int, str]:
        structured = 0 if _supports_structured_output(item) else 1
        return (structured, str(item.get("id") or ""))

    free_models.sort(key=sort_key)
    paid_models.sort(key=sort_key)
    return free_models, paid_models


def _take_serialized_models(
    raw_items: list[dict[str, Any]],
    *,
    limit: int,
    require_structured: bool,
) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    selected: list[dict[str, Any]] = []
    for item in raw_items:
        if require_structured and not _supports_structured_output(item):
            continue
        selected.append(_serialize_model_item(item))
        if len(selected) >= limit:
            break
    return selected


def _empty_catalog() -> dict[str, Any]:
    return {
        "free": [],
        "paid": [],
        "selectedForSuggest": [],
        "cacheExpiresAt": None,
    }


async def fetch_models_catalog(*, force_refresh: bool = False) -> dict[str, Any]:
    now = time.time()
    cached = _MODEL_CACHE.get("catalog")
    if not force_refresh and cached and _MODEL_CACHE.get("expires_at", 0.0) > now:
        return cached

    api_key = (settings.router_api_key or "").strip()
    if not api_key:
        logger.warning("OpenRouter API key missing; model catalog unavailable")
        return _empty_catalog()

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(
                f"{str(settings.router_base_url).rstrip('/')}/models",
                headers=_api_headers(),
                params={"sort": "intelligence-high-to-low"},
            )
        response.raise_for_status()
        models_payload = _parse_http_json(response.text)
        raw_items = [item for item in (models_payload.get("data") or []) if isinstance(item, dict)]
    except Exception as exc:
        logger.warning("OpenRouter models list failed: %s", exc)
        if cached:
            logger.warning("OpenRouter using stale model catalog after list failure")
            return cached
        return _empty_catalog()

    if not raw_items:
        logger.warning("OpenRouter models list returned empty")
        if cached:
            logger.warning("OpenRouter using stale model catalog after empty list")
            return cached
        return _empty_catalog()

    free_raw, paid_raw = _partition_models(raw_items)
    free_limit = max(1, int(settings.router_free_pick_limit or 5))
    paid_limit = max(0, int(settings.router_paid_pick_limit or 4))

    free_models = _take_serialized_models(free_raw, limit=free_limit, require_structured=True)
    paid_models = _take_serialized_models(paid_raw, limit=paid_limit, require_structured=True)
    if not free_models and not paid_models:
        logger.info("OpenRouter no structured-output models; falling back to any text-chat models")
        free_models = _take_serialized_models(free_raw, limit=free_limit, require_structured=False)
        paid_models = _take_serialized_models(paid_raw, limit=paid_limit, require_structured=False)

    if not free_models and not paid_models:
        logger.warning("OpenRouter catalog partitioned to zero models")
        if cached:
            return cached
        return _empty_catalog()

    selected = [m["id"] for m in free_models] + [m["id"] for m in paid_models]
    catalog = {
        "free": free_models,
        "paid": paid_models,
        "selectedForSuggest": selected,
        "cacheExpiresAt": now + _CACHE_TTL_SECONDS,
    }
    _MODEL_CACHE["catalog"] = catalog
    _MODEL_CACHE["expires_at"] = catalog["cacheExpiresAt"]
    return catalog


async def pick_models_for_suggest() -> list[tuple[str, str, bool]]:
    """Return (model_id, tier, supports_structured_output), skipping cooldown models."""
    catalog = await fetch_models_catalog()
    picked: list[tuple[str, str, bool]] = []
    for item in (catalog.get("free") or []) + (catalog.get("paid") or []):
        model_id = str(item.get("id") or "").strip()
        if not model_id:
            continue
        if _is_model_cooling(model_id):
            continue
        tier = str(item.get("tier") or "paid")
        supports = bool(item.get("supportsStructuredOutput"))
        picked.append((model_id, tier, supports))
    return picked


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


def _record_skip(skip_counts: dict[str, int], reason: str) -> None:
    skip_counts[reason] = skip_counts.get(reason, 0) + 1


async def ai_suggest_epub(
    title: str,
    author: str,
    chapters: list[dict[str, Any]],
    existing_genres: list[str],
    *,
    genre_hints: list[str] | None = None,
    map_genres: Callable[[list[str], list[str]], list[str]],
) -> dict[str, Any] | None:
    api_key = (settings.router_api_key or "").strip()
    if not api_key:
        logger.warning("OpenRouter API key missing; skipping AI suggest")
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

    base_payload = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_prompt, ensure_ascii=False)},
        ],
        "temperature": 0.25,
        "max_tokens": _SUGGEST_MAX_TOKENS,
    }

    models = await pick_models_for_suggest()
    if not models:
        logger.info("openrouter ai-suggest done tried=0 result=fallback skips={'no_models': 1}")
        return None

    skip_counts: dict[str, int] = {}
    tried = 0

    for model_id, tier, supports_structured in models:
        tried += 1
        payload = dict(base_payload)
        payload["model"] = model_id
        if supports_structured:
            payload["response_format"] = {"type": "json_object"}
        family = _model_family(model_id)
        try:
            async with httpx.AsyncClient(timeout=45.0) as client:
                response = await client.post(
                    f"{str(settings.router_base_url).rstrip('/')}/chat/completions",
                    headers=_chat_headers(),
                    json=payload,
                )
            if response.status_code >= 400:
                reason = f"status_{response.status_code}"
                _record_skip(skip_counts, reason)
                if response.status_code in _COOLDOWN_HTTP_STATUSES:
                    _mark_model_cooldown(model_id)
                logger.info(
                    "openrouter ai-suggest skip model=%s tier=%s family=%s status=%s body=%s",
                    model_id,
                    tier,
                    family,
                    response.status_code,
                    (response.text or "")[:240],
                )
                continue
            completion = _parse_completion_body(response.text, model_id=model_id)
            parsed = _parse_suggest_result(completion, model_id)
            if not parsed:
                _record_skip(skip_counts, "unparseable_content")
                logger.info(
                    "openrouter ai-suggest skip model=%s tier=%s family=%s reason=unparseable_content",
                    model_id,
                    tier,
                    family,
                )
                continue
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
                _record_skip(skip_counts, "empty_fields")
                logger.info(
                    "openrouter ai-suggest skip model=%s tier=%s family=%s reason=empty_fields genres=%s desc_len=%s",
                    model_id,
                    tier,
                    family,
                    len(genres),
                    len(short_description),
                )
                continue
            if quality_issue:
                _record_skip(skip_counts, f"quality_{quality_issue}")
                logger.info(
                    "openrouter ai-suggest skip model=%s tier=%s family=%s reason=quality_%s desc_len=%s",
                    model_id,
                    tier,
                    family,
                    quality_issue,
                    len(short_description),
                )
                continue
            short_description = normalize_short_description_sentences(short_description)
            logger.info(
                "openrouter ai-suggest done tried=%s result=%s tier=%s skips=%s",
                tried,
                model_id,
                tier,
                skip_counts or {},
            )
            return {
                "suggestedGenres": genres,
                "shortDescription": short_description,
                "confidence": confidence,
                "model": model_id,
                "modelTier": tier,
                "suggestedStatus": novel_status,
            }
        except Exception as exc:
            _record_skip(skip_counts, "exception")
            _mark_model_cooldown(model_id)
            logger.info(
                "openrouter ai-suggest skip model=%s tier=%s family=%s reason=exception err=%s",
                model_id,
                tier,
                family,
                exc,
            )
            continue

    logger.info(
        "openrouter ai-suggest done tried=%s result=fallback skips=%s",
        tried,
        skip_counts or {},
    )
    return None
