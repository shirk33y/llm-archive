from __future__ import annotations

from contextlib import contextmanager
import re
from typing import Iterator
from pathlib import Path

from llm_archive import db
from llm_archive.config import AppConfig
from llm_archive.logging import get_logger

logger = get_logger("export")

_HASH_LINE_RE = re.compile(r"^#{1,6}(\s|$)", re.MULTILINE)
_HTML_OPEN_RE = re.compile(r"<!--")
_HTML_CLOSE_RE = re.compile(r"-->")
_MSG_MARKER_RE = re.compile(r"^<!-- msg:", re.MULTILINE)

try:
    import fcntl
except ImportError:  # pragma: no cover - platform fallback
    fcntl = None


def _default_export_dir() -> Path:
    return db.DB_PATH.parent / "exports"


def export_dir(config: AppConfig | None = None) -> Path:
    if config and config.export.dir:
        return Path(config.export.dir).expanduser()
    return _default_export_dir()


def thread_md_path(source_id: str, thread_id: str, config: AppConfig | None = None) -> Path:
    prefix = f"{source_id}:"
    if thread_id.startswith(prefix):
        thread_id = thread_id[len(prefix):]
    return export_dir(config) / f"{source_id}_{thread_id}.md"


def _export_lock_path(config: AppConfig | None = None) -> Path:
    return export_dir(config) / ".export.lock"


@contextmanager
def _export_lock(config: AppConfig | None = None) -> Iterator[None]:
    lock_path = _export_lock_path(config)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock:
        if fcntl is not None:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _escape_hashes(text: str) -> str:
    return _HASH_LINE_RE.sub(lambda m: "\\" + m.group(0), text)


def _escape_html_comments(text: str) -> str:
    text = _HTML_OPEN_RE.sub("<!&#45;", text)
    text = _HTML_CLOSE_RE.sub("&#45;&#45;>", text)
    return text


def _escape_body(text: str) -> str:
    text = _escape_hashes(text)
    text = _escape_html_comments(text)
    return text


def _render_messages(messages: list[dict], msg_num_start: int = 1) -> str:
    from llm_archive.ids import to_base53

    lines: list[str] = []
    for i, msg in enumerate(messages, msg_num_start):
        role = msg.get("role", "unknown")
        short = to_base53(i)
        ts = msg.get("created_at", 0)
        ts_str = ""
        if ts:
            from datetime import datetime, timezone

            dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
            ts_str = f" · {dt.strftime('%Y-%m-%d %H:%M')}"

        lines.append(f"<!-- msg:{short} role:{role}{ts_str} -->")
        lines.append(f"## {role} · {short}{ts_str}")
        lines.append("")

        parts = msg.get("parts", [])
        for part in parts:
            if not part.get("visible", True):
                continue
            kind = part.get("kind", "text")
            text = part.get("text", "")
            if not text:
                continue

            if kind == "text":
                lines.append(_escape_body(text))
                lines.append("")
            elif kind == "tool_call":
                tool = part.get("tool_name", "?")
                lines.append(_escape_body(f"**▸ {tool}**\n\n{text}"))
                lines.append("")
            elif kind == "tool_result":
                is_err = part.get("tool_is_error", 0)
                marker = "◀ error" if is_err else "◀ result"
                lines.append(_escape_body(f"**{marker}**\n\n{text}"))
                lines.append("")
            elif kind == "reasoning":
                lines.append(_escape_body(f"*reasoning:* {text}"))
                lines.append("")
            else:
                lines.append(_escape_body(f"[{kind}] {text}"))
                lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_thread(thread_data: dict, msg_num_start: int = 1) -> str:
    from datetime import datetime, timezone

    t = thread_data["thread"]
    source = t.get("source_id", "?")
    thread_id = t.get("id", "?")
    title = t.get("title") or "untitled"
    created = t.get("created_at", 0)
    updated = t.get("updated_at", 0)

    lines: list[str] = []
    lines.append(f"<!-- thread:{thread_id} source:{source} -->")
    lines.append(f"# {title}")
    if created:
        dt = datetime.fromtimestamp(created / 1000, tz=timezone.utc)
        lines.append(f"<!-- created: {dt.isoformat()} -->")
    if updated:
        dt = datetime.fromtimestamp(updated / 1000, tz=timezone.utc)
        lines.append(f"<!-- updated: {dt.isoformat()} -->")
    lines.append("")
    lines.append(_render_messages(thread_data.get("messages", []), msg_num_start).rstrip())
    return "\n".join(lines).rstrip() + "\n"


def _exported_message_count(path: Path) -> int | None:
    try:
        text = path.read_text()
    except OSError:
        return None
    return len(_MSG_MARKER_RE.findall(text))


def render_thread_append(thread_data: dict, start_index: int = 0) -> str:
    messages = thread_data.get("messages", [])
    if start_index < 0 or start_index >= len(messages):
        return ""
    return _render_messages(messages[start_index:], msg_num_start=start_index + 1)


def write_thread(
    con,
    thread_id: str,
    source_id: str | None = None,
    config: AppConfig | None = None,
    force: bool = False,
    thread_data: dict | None = None,
) -> Path | None:
    if thread_data is None:
        row = con.execute(
            "SELECT id, source_id, title, created_at, updated_at FROM threads WHERE id=?",
            (thread_id,),
        ).fetchone()
        if not row:
            return None
        thread = dict(row)
        thread_data = db._fetch_thread_data(con, thread)
    else:
        thread = dict(thread_data["thread"])

    if source_id is None:
        source_id = str(thread.get("source_id", "unknown"))

    path = thread_md_path(source_id, thread_id, config)
    with _export_lock(config):
        if path.exists():
            exported_count = _exported_message_count(path)
            total_count = len(thread_data.get("messages", []))
            if exported_count is not None:
                if exported_count >= total_count:
                    return path
                tail = render_thread_append(thread_data, start_index=exported_count)
                if tail:
                    with path.open("a") as f:
                        f.write(tail)
                    logger.debug(f"appended {thread_id} -> {path}")
                    return path

        content = render_thread(thread_data)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        logger.debug(f"exported {thread_id} -> {path}")
        return path


def backfill(
    con,
    source_id: str | None = None,
    config: AppConfig | None = None,
    force: bool = False,
) -> int:
    if source_id:
        rows = con.execute(
            "SELECT id, source_id, updated_at FROM threads WHERE source_id=? ORDER BY updated_at",
            (source_id,),
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT id, source_id, updated_at FROM threads ORDER BY source_id, updated_at"
        ).fetchall()

    count = 0
    for row in rows:
        result = write_thread(con, row["id"], row["source_id"], config, force=force)
        if result is not None:
            count += 1
    return count
