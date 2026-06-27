from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3


@dataclass(frozen=True, slots=True)
class SourceStats:
    source_id: str
    threads: int
    messages: int
    user_messages: int
    weak_titles: int
    recent_user_messages: int
    active_days: int
    title_calls: int
    title_tokens: int
    last_sync: int | None
    updated_at: int | None


@dataclass(frozen=True, slots=True)
class ArchiveStats:
    days: int
    title_step: int
    sources: tuple[SourceStats, ...]


def connect_readonly(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise FileNotFoundError(path)
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def collect_stats(
    con: sqlite3.Connection,
    days: int,
    title_step: int,
    source_id: str | None,
) -> ArchiveStats:
    rows = _source_rows(con, days, source_id)
    estimates = _title_estimates(con, days, title_step, source_id)
    sources = tuple(
        SourceStats(
            source_id=row["source_id"],
            threads=int(row["threads"] or 0),
            messages=int(row["messages"] or 0),
            user_messages=int(row["user_messages"] or 0),
            weak_titles=int(row["weak_titles"] or 0),
            recent_user_messages=int(row["recent_user_messages"] or 0),
            active_days=int(row["active_days"] or 0),
            title_calls=estimates.get(row["source_id"], (0, 0))[0],
            title_tokens=estimates.get(row["source_id"], (0, 0))[1],
            last_sync=row["last_sync"],
            updated_at=row["updated_at"],
        )
        for row in rows
        if row["source_id"] != "dummy"
    )
    return ArchiveStats(days=days, title_step=title_step, sources=sources)


def totals(stats: ArchiveStats) -> SourceStats:
    return SourceStats(
        source_id="TOTAL",
        threads=sum(source.threads for source in stats.sources),
        messages=sum(source.messages for source in stats.sources),
        user_messages=sum(source.user_messages for source in stats.sources),
        weak_titles=sum(source.weak_titles for source in stats.sources),
        recent_user_messages=sum(source.recent_user_messages for source in stats.sources),
        active_days=max((source.active_days for source in stats.sources), default=0),
        title_calls=sum(source.title_calls for source in stats.sources),
        title_tokens=sum(source.title_tokens for source in stats.sources),
        last_sync=max((source.last_sync or 0 for source in stats.sources), default=0) or None,
        updated_at=max((source.updated_at or 0 for source in stats.sources), default=0) or None,
    )


def to_json(stats: ArchiveStats) -> dict:
    total = totals(stats)
    return {
        "days": stats.days,
        "title_step": stats.title_step,
        "totals": _source_json(total),
        "sources": [_source_json(source) for source in stats.sources],
    }


def _source_json(source: SourceStats) -> dict:
    return {
        "source_id": source.source_id,
        "threads": source.threads,
        "messages": source.messages,
        "user_messages": source.user_messages,
        "weak_titles": source.weak_titles,
        "recent_user_messages": source.recent_user_messages,
        "active_days": source.active_days,
        "title_calls": source.title_calls,
        "title_tokens": source.title_tokens,
        "last_sync": source.last_sync,
        "updated_at": source.updated_at,
    }


def _source_rows(
    con: sqlite3.Connection,
    days: int,
    source_id: str | None,
) -> list[sqlite3.Row]:
    since_expr = f"-{days} days"
    return con.execute(
        """
        WITH per_thread AS (
            SELECT
                t.id,
                t.source_id,
                t.title,
                t.updated_at,
                COUNT(m.id) AS messages,
                SUM(CASE WHEN m.role = 'user' THEN 1 ELSE 0 END) AS user_messages
            FROM threads t
            LEFT JOIN messages m ON m.thread_id = t.id
            WHERE (? IS NULL OR t.source_id = ?)
            GROUP BY t.id
        ),
        recent AS (
            SELECT
                t.source_id,
                SUM(CASE WHEN m.role = 'user' THEN 1 ELSE 0 END) AS recent_user_messages,
                COUNT(DISTINCT date(m.created_at / 1000, 'unixepoch', 'localtime')) AS active_days
            FROM messages m
            JOIN threads t ON t.id = m.thread_id
            WHERE (? IS NULL OR t.source_id = ?)
              AND m.created_at >= strftime('%s', 'now', ?) * 1000
            GROUP BY t.source_id
        )
        SELECT
            s.id AS source_id,
            s.last_sync,
            COUNT(pt.id) AS threads,
            COALESCE(SUM(pt.messages), 0) AS messages,
            COALESCE(SUM(pt.user_messages), 0) AS user_messages,
            COALESCE(SUM(
                CASE
                    WHEN pt.id IS NOT NULL AND (
                        pt.title IS NULL
                        OR trim(pt.title) = ''
                        OR lower(trim(pt.title)) IN ('untitled', 'new chat')
                        OR pt.title LIKE 'New session - %'
                    )
                    THEN 1
                    ELSE 0
                END
            ), 0) AS weak_titles,
            COALESCE(MAX(recent.recent_user_messages), 0) AS recent_user_messages,
            COALESCE(MAX(recent.active_days), 0) AS active_days,
            MAX(pt.updated_at) AS updated_at
        FROM sources s
        LEFT JOIN per_thread pt ON pt.source_id = s.id
        LEFT JOIN recent ON recent.source_id = s.id
        WHERE (? IS NULL OR s.id = ?)
        GROUP BY s.id
        ORDER BY messages DESC, source_id
        """,
        (source_id, source_id, source_id, source_id, since_expr, source_id, source_id),
    ).fetchall()


def _title_estimates(
    con: sqlite3.Connection,
    days: int,
    title_step: int,
    source_id: str | None,
) -> dict[str, tuple[int, int]]:
    since_expr = f"-{days} days"
    rows = con.execute(
        """
        WITH user_msgs AS (
            SELECT
                t.source_id,
                m.id,
                m.thread_id,
                m.created_at,
                LENGTH(COALESCE(NULLIF(m.content_clean, ''), m.content)) AS chars,
                ROW_NUMBER() OVER (
                    PARTITION BY m.thread_id
                    ORDER BY m.created_at, m.id
                ) AS user_idx
            FROM messages m
            JOIN threads t ON t.id = m.thread_id
            WHERE m.role = 'user'
              AND (? IS NULL OR t.source_id = ?)
        ),
        triggers AS (
            SELECT *
            FROM user_msgs
            WHERE created_at >= strftime('%s', 'now', ?) * 1000
              AND (user_idx - 1) % ? = 0
        ),
        contexts AS (
            SELECT
                tr.source_id,
                tr.id,
                COALESCE(SUM(CASE WHEN prior.chars > 800 THEN 800 ELSE prior.chars END), 0)
                + CASE
                    WHEN tr.user_idx > 5
                    THEN CASE WHEN first.chars > 1500 THEN 1500 ELSE first.chars END
                    ELSE 0
                END AS snippet_chars
            FROM triggers tr
            JOIN user_msgs prior
              ON prior.thread_id = tr.thread_id
             AND prior.user_idx BETWEEN tr.user_idx - 4 AND tr.user_idx
            LEFT JOIN user_msgs first
              ON first.thread_id = tr.thread_id
             AND first.user_idx = 1
            GROUP BY tr.source_id, tr.id
        )
        SELECT
            source_id,
            COUNT(*) AS calls,
            ROUND(SUM(snippet_chars) / 4.0 + COUNT(*) * 500) AS tokens
        FROM contexts
        GROUP BY source_id
        """,
        (source_id, source_id, since_expr, title_step),
    ).fetchall()
    return {row["source_id"]: (int(row["calls"] or 0), int(row["tokens"] or 0)) for row in rows}
