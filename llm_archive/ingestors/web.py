from __future__ import annotations

from datetime import datetime


def parse_timestamp(ts) -> int | None:
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return int(ts) if ts > 1e12 else int(ts * 1000)
    if isinstance(ts, str):
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return int(dt.timestamp() * 1000)
        except Exception:
            try:
                v = float(ts)
                return int(v) if v > 1e12 else int(v * 1000)
            except Exception:
                return None
    return None


def should_skip_conversation(
    thread_id: str,
    updated_at: int | None,
    existing_thread_ids: set[str] | dict[str, int],
) -> bool:
    """Check if a conversation should be skipped (already synced and up-to-date).
    
    Args:
        thread_id: The prefixed thread ID to check.
        updated_at: API-reported timestamp (ms), or None if unknown.
        existing_thread_ids: Known thread IDs from DB.
            - If a set: thread_id presence → skip.
            - If a dict: db_ts >= api_ts (or any None) → skip.
    
    Returns:
        True if the conversation can be skipped (no re-fetch needed).
    """
    if thread_id not in existing_thread_ids:
        return False
    if isinstance(existing_thread_ids, dict):
        db_ts = existing_thread_ids.get(thread_id)
        if db_ts is None or updated_at is None or db_ts >= updated_at:
            return True
        return False
    return True
