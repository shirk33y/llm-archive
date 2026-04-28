"""Tests for ChatGPT ingestor sync logic.

Skip decision: known thread is skipped if |api_updated_at - db_updated_at| <= MAX_TIMESTAMP_DELTA_MS.
New threads (not in DB) are always fetched.
Early termination when an entire page is all skipped.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from llm_archive.ingestors.chatgpt import (
    ChatGPTIngestor,
    _parse_timestamp,
    _extract_message_text,
)
from llm_archive.schema import IngestedThread, IngestedMessage

_MAX_DELTA = ChatGPTIngestor.MAX_TIMESTAMP_DELTA_MS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _conv(conv_id: str, update_time: float) -> dict:
    """Build a minimal conversation summary as returned by the list endpoint."""
    return {
        "id": conv_id,
        "title": f"Conv {conv_id}",
        "create_time": update_time - 100,
        "update_time": update_time,
    }


def _detail(conv_id: str) -> dict:
    """Build a minimal conversation detail as returned by /conversation/{id}."""
    return {
        "title": f"Conv {conv_id}",
        "mapping": {
            "node1": {
                "message": {
                    "id": f"{conv_id}-msg1",
                    "author": {"role": "user"},
                    "content": {"parts": ["Hello"]},
                    "create_time": 1000.0,
                }
            },
            "node2": {
                "message": {
                    "id": f"{conv_id}-msg2",
                    "author": {"role": "assistant"},
                    "content": {"parts": ["Hi there"]},
                    "create_time": 2000.0,
                }
            },
        },
    }


class FakeResponse:
    def __init__(self, data: dict, status_code: int = 200):
        self._data = data
        self.status_code = status_code

    def json(self) -> dict:
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


def _make_list_response(items: list[dict], remaining: int = 0) -> FakeResponse:
    return FakeResponse({"items": items, "remaining": remaining})


async def _collect(gen: AsyncIterator) -> list:
    return [item async for item in gen]


def _make_ingestor(
    pages: list[list[dict]],
    details: dict[str, dict] | None = None,
) -> ChatGPTIngestor:
    """Return a ChatGPTIngestor with mocked HTTP calls."""
    ingestor = ChatGPTIngestor.__new__(ChatGPTIngestor)
    ingestor._message_limiter = MagicMock()
    ingestor._message_limiter.get_and_apply_delay.return_value = 0
    ingestor._message_limiter.get_delay.return_value = 0

    async def fake_fetch_conversations(client, headers, offset, limit):
        page_idx = offset // limit
        if page_idx >= len(pages):
            return _make_list_response([])
        page = pages[page_idx]
        future_items = sum(len(p) for p in pages[page_idx + 1:])
        return _make_list_response(page, remaining=future_items)

    async def fake_fetch_thread(client, conv, headers, on_conversation_progress=None, total_fetched=0):
        conv_id = conv["id"]
        detail = (details or {}).get(conv_id, _detail(conv_id))
        messages = []
        for node in detail.get("mapping", {}).values():
            msg = node.get("message")
            if not msg:
                continue
            role = msg.get("author", {}).get("role", "")
            if role not in ("user", "assistant"):
                continue
            content = _extract_message_text(msg)
            if content.strip():
                messages.append(IngestedMessage(
                    id=f"chatgpt:{msg['id']}",
                    thread_id=f"chatgpt:{conv_id}",
                    role=role,
                    content=content,
                    created_at=_parse_timestamp(msg.get("create_time")),
                ))
        if not messages:
            return None
        return IngestedThread(
            id=f"chatgpt:{conv_id}",
            source_id="chatgpt",
            title=conv.get("title"),
            created_at=_parse_timestamp(conv.get("create_time")),
            updated_at=_parse_timestamp(conv.get("update_time")),
            messages=messages,
        )

    ingestor._get_token_via_cdp = AsyncMock(return_value=("fake-token", {}))
    ingestor._fetch_conversations = fake_fetch_conversations
    ingestor._fetch_thread = fake_fetch_thread
    return ingestor


# ---------------------------------------------------------------------------
# _parse_timestamp
# ---------------------------------------------------------------------------

class TestParseTimestamp:
    def test_float_seconds(self):
        assert _parse_timestamp(1_700_000_000.0) == 1_700_000_000_000

    def test_int_seconds(self):
        assert _parse_timestamp(1_700_000_000) == 1_700_000_000_000

    def test_none(self):
        assert _parse_timestamp(None) is None

    def test_iso_string_z(self):
        ts = _parse_timestamp("2024-01-15T12:00:00Z")
        assert ts is not None
        assert ts > 0

    def test_iso_string_offset(self):
        ts = _parse_timestamp("2024-01-15T12:00:00+00:00")
        assert ts is not None

    def test_invalid_string(self):
        assert _parse_timestamp("not-a-date") is None

    def test_numeric_string(self):
        assert _parse_timestamp("1700000000.5") == 1_700_000_000_500


# ---------------------------------------------------------------------------
# Sync decision logic
# ---------------------------------------------------------------------------

# Reference timestamps
BASE_TS_S = 1_705_276_800.0       # 2024-01-15T00:00:00Z in seconds
BASE_TS_MS = int(BASE_TS_S * 1000)


class TestNewThreadsAlwaysFetched:
    def test_new_thread_fetched(self):
        pages = [[_conv("abc", BASE_TS_S)]]
        ingestor = _make_ingestor(pages)

        threads = asyncio.run(_collect(ingestor.threads(existing_thread_ids={})))

        assert len(threads) == 1
        assert threads[0].id == "chatgpt:abc"

    def test_multiple_new_threads_all_fetched(self):
        pages = [[_conv("a", BASE_TS_S), _conv("b", BASE_TS_S + 1000)]]
        ingestor = _make_ingestor(pages)

        threads = asyncio.run(_collect(ingestor.threads(existing_thread_ids={})))

        assert len(threads) == 2


class TestKnownThreadSkipping:
    def test_same_timestamp_skipped(self):
        """db_updated_at == api_updated_at → skip."""
        pages = [[_conv("abc", BASE_TS_S)]]
        ingestor = _make_ingestor(pages)

        threads = asyncio.run(_collect(ingestor.threads(
            existing_thread_ids={"chatgpt:abc": BASE_TS_MS},
        )))

        assert threads == []

    def test_small_delta_skipped(self):
        """delta = 1 hour → within 24h → skip."""
        api_ts_ms = BASE_TS_MS + 3_600_000  # 1 hour later
        pages = [[_conv("abc", api_ts_ms / 1000)]]
        ingestor = _make_ingestor(pages)

        threads = asyncio.run(_collect(ingestor.threads(
            existing_thread_ids={"chatgpt:abc": BASE_TS_MS},
        )))

        assert threads == []

    def test_exactly_max_delta_skipped(self):
        """delta == MAX_TIMESTAMP_DELTA_MS → skip (boundary)."""
        api_ts_ms = BASE_TS_MS + _MAX_DELTA
        pages = [[_conv("abc", api_ts_ms / 1000)]]
        ingestor = _make_ingestor(pages)

        threads = asyncio.run(_collect(ingestor.threads(
            existing_thread_ids={"chatgpt:abc": BASE_TS_MS},
        )))

        assert threads == []

    def test_over_max_delta_refetched(self):
        """delta > MAX_TIMESTAMP_DELTA_MS → re-fetch."""
        api_ts_ms = BASE_TS_MS + _MAX_DELTA + 1
        pages = [[_conv("abc", api_ts_ms / 1000)]]
        ingestor = _make_ingestor(pages)

        threads = asyncio.run(_collect(ingestor.threads(
            existing_thread_ids={"chatgpt:abc": BASE_TS_MS},
        )))

        assert len(threads) == 1
        assert threads[0].id == "chatgpt:abc"

    def test_phantom_bump_within_24h_skipped(self):
        """ChatGPT bumps timestamp by a few hours → still within delta → skip."""
        bump_ms = BASE_TS_MS + 6 * 3_600_000  # 6 hours bump
        pages = [[_conv("abc", bump_ms / 1000)]]
        ingestor = _make_ingestor(pages)

        threads = asyncio.run(_collect(ingestor.threads(
            existing_thread_ids={"chatgpt:abc": BASE_TS_MS},
        )))

        assert threads == []

    def test_mixed_known_and_new(self):
        """Known-within-delta skipped, new fetched, known-over-delta refetched."""
        big_delta_ms = BASE_TS_MS + _MAX_DELTA + 1000
        pages = [[
            _conv("new", BASE_TS_S),                    # not in DB → always fetch
            _conv("over_delta", big_delta_ms / 1000),   # known, big delta → re-fetch
            _conv("within_delta", BASE_TS_S + 1000),    # known, small delta → skip
        ]]
        ingestor = _make_ingestor(pages)

        existing = {
            "chatgpt:over_delta": BASE_TS_MS,
            "chatgpt:within_delta": BASE_TS_MS,
        }

        threads = asyncio.run(_collect(ingestor.threads(existing_thread_ids=existing)))

        fetched_ids = {t.id for t in threads}
        assert "chatgpt:new" in fetched_ids
        assert "chatgpt:over_delta" in fetched_ids
        assert "chatgpt:within_delta" not in fetched_ids

    def test_known_no_db_timestamp_refetched(self):
        """Known thread but db_updated_at is None → can't compare → re-fetch."""
        pages = [[_conv("abc", BASE_TS_S)]]
        ingestor = _make_ingestor(pages)

        threads = asyncio.run(_collect(ingestor.threads(
            existing_thread_ids={"chatgpt:abc": None},
        )))

        assert len(threads) == 1


class TestEarlyTermination:
    def test_early_termination_on_all_skipped_page(self):
        page1 = [_conv("new1", BASE_TS_S)]
        page2 = [_conv(f"old{i}", BASE_TS_S) for i in range(100)]  # all known, same ts
        page3 = [_conv(f"never{i}", BASE_TS_S) for i in range(100)]  # should not be reached

        pages = [page1, page2, page3]
        ingestor = _make_ingestor(pages)

        existing = {
            **{f"chatgpt:old{i}": BASE_TS_MS for i in range(100)},
            **{f"chatgpt:never{i}": BASE_TS_MS for i in range(100)},
        }

        threads = asyncio.run(_collect(ingestor.threads(existing_thread_ids=existing)))

        fetched_ids = {t.id for t in threads}
        assert "chatgpt:new1" in fetched_ids
        assert all(f"chatgpt:never{i}" not in fetched_ids for i in range(100))

    def test_no_early_termination_when_page_has_unknown(self):
        page1 = [_conv(f"known{i}", BASE_TS_S) for i in range(99)] + [_conv("unknown", BASE_TS_S)]
        page2 = [_conv("should_reach", BASE_TS_S)]

        pages = [page1, page2]
        ingestor = _make_ingestor(pages)

        existing = {f"chatgpt:known{i}": BASE_TS_MS for i in range(99)}

        threads = asyncio.run(_collect(ingestor.threads(existing_thread_ids=existing)))

        fetched_ids = {t.id for t in threads}
        assert "chatgpt:unknown" in fetched_ids
        assert "chatgpt:should_reach" in fetched_ids

    def test_partial_last_page_terminates_naturally(self):
        page1 = [_conv(f"known{i}", BASE_TS_S) for i in range(50)]  # partial page

        pages = [page1]
        ingestor = _make_ingestor(pages)

        existing = {f"chatgpt:known{i}": BASE_TS_MS for i in range(50)}

        threads = asyncio.run(_collect(ingestor.threads(existing_thread_ids=existing)))

        assert threads == []


class TestSha1Stop:
    """After first sha1 match, stop fetching details for known threads.
    Collect their api timestamps via on_skip_timestamps for bulk DB update.
    Unknown (new) threads still fetched after sha1 stop.
    """

    def _make_ingestor_with_sha1_stop(self, pages, details=None):
        """Ingestor where store_thread returns False (sha1 match) for known threads."""
        ingestor = _make_ingestor(pages, details)
        return ingestor

    def test_stop_on_sha1_match_skips_remaining_known(self):
        """After sha1 match, remaining known threads on page and next pages not fetched."""
        big_delta = _MAX_DELTA + 1000
        # page: first known (sha1 match), then more known (should be skipped)
        page1 = [
            _conv("first", (BASE_TS_MS + big_delta) / 1000),
            _conv("second", (BASE_TS_MS + big_delta) / 1000),
            _conv("third", (BASE_TS_MS + big_delta) / 1000),
        ]
        page2 = [_conv(f"p2c{i}", (BASE_TS_MS + big_delta) / 1000) for i in range(100)]
        ingestor = _make_ingestor([page1, page2])

        existing = {
            "chatgpt:first": BASE_TS_MS,
            "chatgpt:second": BASE_TS_MS,
            "chatgpt:third": BASE_TS_MS,
            **{f"chatgpt:p2c{i}": BASE_TS_MS for i in range(100)},
        }

        fetch_count = [0]
        original_fetch = ingestor._fetch_thread

        async def counting_fetch(client, conv, headers, **kw):
            fetch_count[0] += 1
            return await original_fetch(client, conv, headers, **kw)

        ingestor._fetch_thread = counting_fetch

        sha1_calls = [0]

        def store_thread(thread):
            sha1_calls[0] += 1
            return False  # simulate sha1 match every time

        threads = asyncio.run(_collect(ingestor.threads(
            existing_thread_ids=existing,
            store_thread=store_thread,
        )))

        # Only first thread fetched (sha1 match stops the rest)
        assert fetch_count[0] == 1
        assert len(threads) == 1
        assert threads[0].id == "chatgpt:first"

    def test_skip_timestamps_collected_after_sha1_stop(self):
        """on_skip_timestamps called with api timestamps of skipped known threads."""
        big_delta = _MAX_DELTA + 1000
        api_ts = BASE_TS_MS + big_delta
        page1 = [
            _conv("first", api_ts / 1000),
            _conv("second", api_ts / 1000),
            _conv("third", api_ts / 1000),
        ]
        ingestor = _make_ingestor([page1])

        existing = {
            "chatgpt:first": BASE_TS_MS,
            "chatgpt:second": BASE_TS_MS,
            "chatgpt:third": BASE_TS_MS,
        }

        collected: dict[str, int] = {}

        def store_thread(thread):
            return False  # sha1 match

        def on_skip_timestamps(updates):
            collected.update(updates)

        asyncio.run(_collect(ingestor.threads(
            existing_thread_ids=existing,
            store_thread=store_thread,
            on_skip_timestamps=on_skip_timestamps,
        )))

        # second and third should be in skip timestamps (first was fetched)
        assert "chatgpt:second" in collected
        assert "chatgpt:third" in collected
        assert collected["chatgpt:second"] == api_ts
        assert collected["chatgpt:third"] == api_ts

    def test_new_threads_fetched_after_sha1_stop(self):
        """Unknown threads always fetched even after sha1 stop."""
        big_delta = _MAX_DELTA + 1000
        api_ts = BASE_TS_MS + big_delta
        page1 = [
            _conv("known", api_ts / 1000),  # known → sha1 stop
            _conv("new1", BASE_TS_S),        # new → must fetch
            _conv("new2", BASE_TS_S),        # new → must fetch
        ]
        ingestor = _make_ingestor([page1])

        existing = {"chatgpt:known": BASE_TS_MS}

        def store_thread(thread):
            return False  # sha1 match

        threads = asyncio.run(_collect(ingestor.threads(
            existing_thread_ids=existing,
            store_thread=store_thread,
        )))

        fetched_ids = {t.id for t in threads}
        assert "chatgpt:known" in fetched_ids
        assert "chatgpt:new1" in fetched_ids
        assert "chatgpt:new2" in fetched_ids

    def test_sha1_stop_spans_multiple_pages(self):
        """After sha1 stop, continue paginating to collect timestamps across pages."""
        big_delta = _MAX_DELTA + 1000
        api_ts = BASE_TS_MS + big_delta
        # page1: 100 items, first triggers sha1 stop, rest get timestamps collected
        page1 = [_conv(f"p1c{i}", api_ts / 1000) for i in range(100)]
        page2 = [_conv(f"p2c{i}", api_ts / 1000) for i in range(100)]
        ingestor = _make_ingestor([page1, page2])

        existing = {
            **{f"chatgpt:p1c{i}": BASE_TS_MS for i in range(100)},
            **{f"chatgpt:p2c{i}": BASE_TS_MS for i in range(100)},
        }

        collected: dict[str, int] = {}

        def store_thread(thread):
            return False  # sha1 match on first fetch

        def on_skip_timestamps(updates):
            collected.update(updates)

        asyncio.run(_collect(ingestor.threads(
            existing_thread_ids=existing,
            store_thread=store_thread,
            on_skip_timestamps=on_skip_timestamps,
        )))

        # page1: first item fetched+sha1matched, remaining 99 get timestamps
        # page2: all 100 get timestamps
        assert len(collected) == 199
        assert all(collected.get(f"chatgpt:p1c{i}") == api_ts for i in range(1, 100))
        assert all(collected.get(f"chatgpt:p2c{i}") == api_ts for i in range(100))


    def test_sha1_stop_triggered_when_api_ts_newer(self):
        """sha1_stop must trigger even when api_updated_at > db_updated_at.
        Regression: force=True based on updated_at comparison prevented sha1 match detection."""
        big_delta = _MAX_DELTA + 1000
        api_ts = BASE_TS_MS + big_delta  # api is newer than db
        page1 = [
            _conv("first", api_ts / 1000),
            _conv("second", api_ts / 1000),
        ]
        ingestor = _make_ingestor([page1])

        existing = {
            "chatgpt:first": BASE_TS_MS,
            "chatgpt:second": BASE_TS_MS,
        }

        fetch_count = [0]
        original_fetch = ingestor._fetch_thread

        async def counting_fetch(client, conv, headers, **kw):
            fetch_count[0] += 1
            return await original_fetch(client, conv, headers, **kw)

        ingestor._fetch_thread = counting_fetch

        # store_thread must NOT force-write when api_ts > db_ts — sha1 is authoritative
        def store_thread(thread):
            from llm_archive import db as db_mod
            import sqlite3, tempfile
            # Just return False to simulate sha1 match (content unchanged)
            return False

        threads = asyncio.run(_collect(ingestor.threads(
            existing_thread_ids=existing,
            store_thread=store_thread,
        )))

        # sha1 match on first → stop → only one fetch
        assert fetch_count[0] == 1


class TestExistingThreadIdsFormats:
    def test_set_format_known_no_ts_refetched(self):
        """Plain set has no db timestamps → db_ts=None → re-fetch."""
        pages = [[_conv("abc", BASE_TS_S)]]
        ingestor = _make_ingestor(pages)

        threads = asyncio.run(_collect(ingestor.threads(
            existing_thread_ids={"chatgpt:abc"},  # plain set
        )))

        assert len(threads) == 1

    def test_dict_format_within_delta_skipped(self):
        pages = [[_conv("abc", BASE_TS_S)]]
        ingestor = _make_ingestor(pages)

        threads = asyncio.run(_collect(ingestor.threads(
            existing_thread_ids={"chatgpt:abc": BASE_TS_MS},
        )))

        assert threads == []

    def test_none_existing_treats_all_as_new(self):
        pages = [[_conv("abc", BASE_TS_S)]]
        ingestor = _make_ingestor(pages)

        threads = asyncio.run(_collect(ingestor.threads(existing_thread_ids=None)))

        assert len(threads) == 1


class TestMultiPagePagination:
    def test_all_pages_fetched_when_new_threads_present(self):
        pages = [
            [_conv(f"p1c{i}", BASE_TS_S) for i in range(100)],
            [_conv(f"p2c{i}", BASE_TS_S) for i in range(100)],
            [_conv(f"p3c{i}", BASE_TS_S) for i in range(50)],
        ]
        ingestor = _make_ingestor(pages)

        threads = asyncio.run(_collect(ingestor.threads(existing_thread_ids={})))

        assert len(threads) == 250

    def test_empty_page_stops_pagination(self):
        pages = [[_conv("abc", BASE_TS_S)], []]
        ingestor = _make_ingestor(pages)

        threads = asyncio.run(_collect(ingestor.threads(existing_thread_ids={})))

        assert len(threads) == 1


class TestSaveThreadUpdatedAtOnSha1Match:
    def _make_db(self):
        import sqlite3, tempfile
        from llm_archive import db
        path = Path(tempfile.mktemp(suffix=".db"))
        con = db.connect(path)
        return con

    def _make_thread(self, thread_id: str, content: str, updated_at: int) -> IngestedThread:
        return IngestedThread(
            id=thread_id,
            source_id="chatgpt",
            title="Test",
            created_at=1000,
            updated_at=updated_at,
            messages=[IngestedMessage(
                id=f"{thread_id}:msg1",
                thread_id=thread_id,
                role="user",
                content=content,
                created_at=1000,
            )],
        )

    def test_updated_at_bumped_on_sha1_match(self):
        from llm_archive import db

        con = self._make_db()
        thread = self._make_thread("chatgpt:abc", "Hello", updated_at=1000)
        assert db.save_thread(con, thread) is True

        thread2 = self._make_thread("chatgpt:abc", "Hello", updated_at=9999)
        assert db.save_thread(con, thread2) is False  # sha1 match → not written

        row = con.execute("SELECT updated_at FROM threads WHERE id=?", ("chatgpt:abc",)).fetchone()
        assert row["updated_at"] == 9999

    def test_updated_at_not_decreased_on_sha1_match(self):
        from llm_archive import db

        con = self._make_db()
        thread = self._make_thread("chatgpt:abc", "Hello", updated_at=9999)
        db.save_thread(con, thread)

        thread2 = self._make_thread("chatgpt:abc", "Hello", updated_at=1000)
        db.save_thread(con, thread2)

        row = con.execute("SELECT updated_at FROM threads WHERE id=?", ("chatgpt:abc",)).fetchone()
        assert row["updated_at"] == 9999  # unchanged


class TestLoadStoredToken:
    def _write_auth(self, path: Path, token: str, cookies: list[dict]) -> None:
        path.write_text(json.dumps({
            "access_token": token,
            "cookies": cookies,
        }))

    def _make_jwt(self, exp: int) -> str:
        import base64
        header = base64.urlsafe_b64encode(b'{"alg":"RS256"}').rstrip(b"=").decode()
        payload_bytes = json.dumps({"exp": exp}).encode()
        payload = base64.urlsafe_b64encode(payload_bytes).rstrip(b"=").decode()
        return f"{header}.{payload}.fakesig"

    def test_valid_token_returned(self, tmp_path):
        auth_file = tmp_path / "chatgpt.json"
        future_exp = int(time.time()) + 3600
        token = self._make_jwt(future_exp)
        self._write_auth(auth_file, token, [{"name": "session", "value": "abc"}])

        ingestor = ChatGPTIngestor.__new__(ChatGPTIngestor)

        with patch("llm_archive.ingestors.chatgpt.auth_path", return_value=auth_file):
            result = ingestor._load_stored_token()

        assert result is not None
        assert result[0] == token
        assert result[1] == {"session": "abc"}

    def test_expired_token_returns_none(self, tmp_path):
        auth_file = tmp_path / "chatgpt.json"
        past_exp = int(time.time()) - 3600
        token = self._make_jwt(past_exp)
        self._write_auth(auth_file, token, [])

        ingestor = ChatGPTIngestor.__new__(ChatGPTIngestor)

        with patch("llm_archive.ingestors.chatgpt.auth_path", return_value=auth_file):
            result = ingestor._load_stored_token()

        assert result is None

    def test_missing_file_returns_none(self, tmp_path):
        auth_file = tmp_path / "does_not_exist.json"

        ingestor = ChatGPTIngestor.__new__(ChatGPTIngestor)

        with patch("llm_archive.ingestors.chatgpt.auth_path", return_value=auth_file):
            result = ingestor._load_stored_token()

        assert result is None

    def test_no_token_in_file_returns_none(self, tmp_path):
        auth_file = tmp_path / "chatgpt.json"
        auth_file.write_text(json.dumps({"cookies": []}))

        ingestor = ChatGPTIngestor.__new__(ChatGPTIngestor)

        with patch("llm_archive.ingestors.chatgpt.auth_path", return_value=auth_file):
            result = ingestor._load_stored_token()

        assert result is None

    def test_token_without_exp_treated_as_valid(self, tmp_path):
        """If JWT has no exp claim, treat as valid (let API reject if needed)."""
        import base64
        auth_file = tmp_path / "chatgpt.json"
        header = base64.urlsafe_b64encode(b'{"alg":"RS256"}').rstrip(b"=").decode()
        payload = base64.urlsafe_b64encode(b'{"sub":"user123"}').rstrip(b"=").decode()
        token = f"{header}.{payload}.sig"
        self._write_auth(auth_file, token, [])

        ingestor = ChatGPTIngestor.__new__(ChatGPTIngestor)

        with patch("llm_archive.ingestors.chatgpt.auth_path", return_value=auth_file):
            result = ingestor._load_stored_token()

        assert result is not None
        assert result[0] == token


# ---------------------------------------------------------------------------
# Smart pagination: tail check + on_delta_skip
# ---------------------------------------------------------------------------

class TestSmartPagination:
    """Floor check: fetch last page first, verify oldest known thread sha1.
    If verified, stop immediately after sha1_stop (no timestamp collection).
    on_delta_skip: callback for items skipped by delta check.
    """

    def test_on_total_called_from_first_page(self):
        """on_total is called with items+remaining from page 0."""
        pages = [
            [_conv(f"p1c{i}", BASE_TS_S) for i in range(100)],
            [_conv(f"p2c{i}", BASE_TS_S) for i in range(50)],
        ]
        ingestor = _make_ingestor(pages)

        totals = []
        asyncio.run(_collect(ingestor.threads(
            existing_thread_ids={},
            on_total=totals.append,
        )))

        assert totals == [150]

    def test_tail_check_fetches_last_page(self):
        """tail_check receives the oldest known thread from the last page."""
        big_delta = _MAX_DELTA + 1000
        api_ts = BASE_TS_MS + big_delta

        page1 = [_conv(f"p1c{i}", api_ts / 1000) for i in range(100)]
        page2 = [_conv(f"p2c{i}", api_ts / 1000) for i in range(100)]

        pages = [page1, page2]
        ingestor = _make_ingestor(pages)

        existing = {
            **{f"chatgpt:p1c{i}": BASE_TS_MS for i in range(100)},
            **{f"chatgpt:p2c{i}": BASE_TS_MS for i in range(100)},
        }

        tail_check_calls = []

        def tail_check(thread):
            tail_check_calls.append(thread.id)
            return True  # sha1 matches

        asyncio.run(_collect(ingestor.threads(
            existing_thread_ids=existing,
            tail_check=tail_check,
            store_thread=lambda t: False,
        )))

        # Tail check should have been called with exactly one thread from the last page
        assert len(tail_check_calls) == 1
        assert tail_check_calls[0].startswith("chatgpt:p2c")

    def test_tail_verified_stops_after_sha1_stop(self):
        """With tail verified, sha1_stop causes immediate stop — no timestamp collection."""
        big_delta = _MAX_DELTA + 1000
        api_ts = BASE_TS_MS + big_delta

        page1 = [_conv(f"p1c{i}", api_ts / 1000) for i in range(100)]
        page2 = [_conv(f"p2c{i}", api_ts / 1000) for i in range(100)]

        pages = [page1, page2]
        ingestor = _make_ingestor(pages)

        existing = {
            **{f"chatgpt:p1c{i}": BASE_TS_MS for i in range(100)},
            **{f"chatgpt:p2c{i}": BASE_TS_MS for i in range(100)},
        }

        skip_ts_calls = []

        def on_skip_timestamps(updates):
            skip_ts_calls.append(updates)

        asyncio.run(_collect(ingestor.threads(
            existing_thread_ids=existing,
            tail_check=lambda t: True,   # tail verified
            store_thread=lambda t: False, # sha1 match immediately
            on_skip_timestamps=on_skip_timestamps,
        )))

        # With tail_verified + sha1_stop: stop immediately, no timestamp collection
        assert skip_ts_calls == []

    def test_no_tail_stop_without_tail_check(self):
        """Without tail_check, sha1_stop still collects timestamps across pages."""
        big_delta = _MAX_DELTA + 1000
        api_ts = BASE_TS_MS + big_delta

        page1 = [_conv(f"p1c{i}", api_ts / 1000) for i in range(100)]
        page2 = [_conv(f"p2c{i}", api_ts / 1000) for i in range(100)]

        pages = [page1, page2]
        ingestor = _make_ingestor(pages)

        existing = {
            **{f"chatgpt:p1c{i}": BASE_TS_MS for i in range(100)},
            **{f"chatgpt:p2c{i}": BASE_TS_MS for i in range(100)},
        }

        collected: dict[str, int] = {}

        def on_skip_timestamps(updates):
            collected.update(updates)

        asyncio.run(_collect(ingestor.threads(
            existing_thread_ids=existing,
            store_thread=lambda t: False,  # sha1 match
            on_skip_timestamps=on_skip_timestamps,
            # no tail_check
        )))

        # Without tail_check, continues paginating for timestamps
        assert len(collected) > 0

    def test_tail_not_verified_continues_timestamp_collection(self):
        """If tail_check returns False, behave like no tail_check (collect timestamps)."""
        big_delta = _MAX_DELTA + 1000
        api_ts = BASE_TS_MS + big_delta

        page1 = [_conv(f"p1c{i}", api_ts / 1000) for i in range(100)]
        page2 = [_conv(f"p2c{i}", api_ts / 1000) for i in range(100)]

        pages = [page1, page2]
        ingestor = _make_ingestor(pages)

        existing = {
            **{f"chatgpt:p1c{i}": BASE_TS_MS for i in range(100)},
            **{f"chatgpt:p2c{i}": BASE_TS_MS for i in range(100)},
        }

        collected: dict[str, int] = {}

        asyncio.run(_collect(ingestor.threads(
            existing_thread_ids=existing,
            tail_check=lambda t: False,  # tail NOT verified
            store_thread=lambda t: False,
            on_skip_timestamps=lambda u: collected.update(u),
        )))

        # tail not verified → still collects timestamps
        assert len(collected) > 0

    def test_on_delta_skip_called_for_delta_skipped_items(self):
        """on_delta_skip receives count of items skipped by timestamp delta."""
        pages = [[_conv(f"c{i}", BASE_TS_S) for i in range(10)]]
        ingestor = _make_ingestor(pages)

        existing = {f"chatgpt:c{i}": BASE_TS_MS for i in range(10)}

        delta_skipped = [0]

        def on_delta_skip(count):
            delta_skipped[0] += count

        asyncio.run(_collect(ingestor.threads(
            existing_thread_ids=existing,
            on_delta_skip=on_delta_skip,
        )))

        assert delta_skipped[0] == 10

    def test_on_delta_skip_not_called_for_fetched_items(self):
        """Items that are fetched don't count toward delta_skip."""
        big_delta = _MAX_DELTA + 1000
        api_ts = BASE_TS_MS + big_delta

        page = [
            _conv("skip1", BASE_TS_S),      # within delta
            _conv("skip2", BASE_TS_S + 100), # within delta
            _conv("skip3", BASE_TS_S + 200), # within delta
            _conv("fetch1", api_ts / 1000),  # over delta → fetched
            _conv("fetch2", api_ts / 1000),  # over delta → fetched
        ]
        ingestor = _make_ingestor([page])

        existing = {
            "chatgpt:skip1": BASE_TS_MS,
            "chatgpt:skip2": BASE_TS_MS,
            "chatgpt:skip3": BASE_TS_MS,
            "chatgpt:fetch1": BASE_TS_MS,
            "chatgpt:fetch2": BASE_TS_MS,
        }

        delta_skipped = [0]

        asyncio.run(_collect(ingestor.threads(
            existing_thread_ids=existing,
            on_delta_skip=lambda c: delta_skipped.__setitem__(0, delta_skipped[0] + c),
        )))

        assert delta_skipped[0] == 3

    def test_tail_check_not_called_single_page(self):
        """Tail check skipped when total fits in one page (total <= limit)."""
        pages = [[_conv(f"c{i}", BASE_TS_S) for i in range(50)]]
        ingestor = _make_ingestor(pages)

        existing = {f"chatgpt:c{i}": BASE_TS_MS for i in range(50)}

        tail_calls = []

        asyncio.run(_collect(ingestor.threads(
            existing_thread_ids=existing,
            tail_check=lambda t: tail_calls.append(t.id) or True,
        )))

        assert tail_calls == []  # no tail check for single page
