from __future__ import annotations

import json
import logging
import time
from typing import Optional

from llm_archive import db

logger = logging.getLogger("llm_archive.summarize")

SYSTEM_PROMPT = (
    "Summarize this conversation in JSON with 4 tiers:\n"
    "- tiny: one-line topic (max 80 chars)\n"
    "- small: 2-3 sentence recap (max 300 chars)\n"
    "- medium: detailed paragraph with key decisions and outcomes (max 1000 chars)\n"
    "- large: comprehensive summary including struggles, decisions, and patterns (max 2000 chars)\n"
    'Respond with ONLY valid JSON, no markdown: {"tiny": "...", "small": "...", "medium": "...", "large": "..."}'
)


def extract_thread_text(con, thread_id: str, max_chars: int = 8000) -> str:
    title_row = con.execute("SELECT title FROM threads WHERE id=?", (thread_id,)).fetchone()
    title = (title_row["title"] or "") if title_row else ""

    rows = con.execute(
        """
        SELECT m.role, p.text
        FROM messages m
        JOIN message_parts p ON p.message_id = m.id
        WHERE m.thread_id = ?
          AND p.kind = 'text'
          AND length(p.text) > 10
          AND m.role IN ('user', 'assistant')
        ORDER BY m.created_at, p.ord
        LIMIT 60
        """,
        (thread_id,),
    ).fetchall()

    parts = []
    if title:
        parts.append(f"Title: {title}")
    chars = 0
    for row in rows:
        text = (row["text"] or "").strip()
        if text:
            chunk = text[:400]
            chars += len(chunk)
            parts.append(f"{row['role']}: {chunk}")
            if chars >= max_chars:
                break

    return "\n\n".join(parts)


def summarize_thread(
    con,
    thread_id: str,
    model: str = "ollama/qwen2.5:7b",
) -> dict | None:
    text = extract_thread_text(con, thread_id)
    if not text.strip():
        return None

    prompt = f"{SYSTEM_PROMPT}\n\nConversation:\n{text}"

    try:
        import litellm

        r = litellm.completion(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            timeout=180,
        )
        raw = r.choices[0].message.content.strip()
    except Exception:
        logger.exception(f"summarize: error for {thread_id}")
        return None

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        try:
            start = raw.index("{")
            end = raw.rindex("}") + 1
            return json.loads(raw[start:end])
        except (ValueError, json.JSONDecodeError):
            logger.warning(f"summarize: failed to parse JSON for {thread_id}: {raw[:200]}")
            return None


def auto_summarize(
    con,
    source_id: Optional[str] = None,
    model: str = "ollama/qwen2.5:7b",
    min_new_messages: int = 3,
) -> int:
    threads = db.threads_needing_summary(con, source_id, min_new_messages=min_new_messages)
    if not threads:
        return 0

    done = 0
    errors = 0
    for t in threads:
        tid = t["id"]
        result = summarize_thread(con, tid, model)
        if result:
            db.upsert_thread_summary(
                con,
                tid,
                result.get("tiny", ""),
                result.get("small", ""),
                result.get("medium", ""),
                result.get("large", ""),
                model,
                int(time.time() * 1000),
            )
            done += 1
        else:
            errors += 1

    logger.info(f"auto_summarize: {done} done, {errors} errors")
    return done
