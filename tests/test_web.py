from __future__ import annotations

import pytest

from llm_archive.ingestors.web import parse_timestamp, should_skip_conversation

SEC = 1705314600
MS = 1705314600000


class TestParseTimestamp:
    @pytest.mark.parametrize(
        ("val", "expected"),
        [
            (None, None),
            (0, 0),
            (SEC, MS),
            (float(SEC), MS),
            (MS, MS),
            (1e12, int(1e12 * 1000)),
            (1e12 + 1, int(1e12 + 1)),
            ("2024-01-15T10:30:00Z", MS),
            ("2024-01-15T10:30:00+00:00", MS),
            ("2024-01-15T10:30:00.123Z", MS + 123),
            (str(SEC), MS),
            ("", None),
            ("garbage", None),
        ],
    )
    def test(self, val, expected):
        assert parse_timestamp(val) == expected

    def test_bool(self):
        assert parse_timestamp(False) == 0
        assert parse_timestamp(True) == 1000


class TestShouldSkipConversation:
    @pytest.mark.parametrize(
        ("thread_id", "updated_at", "existing", "expected"),
        [
            ("t1", 1000, {"t2"}, False),
            ("t1", 1000, {"t1"}, True),
            ("t1", 1000, {"t2": 500}, False),
            ("t1", 1000, {"t1": 500}, False),
            ("t1", 1000, {"t1": 1000}, True),
            ("t1", 1000, {"t1": 2000}, True),
            ("t1", None, {"t1": 500}, True),
            ("t1", 1000, {"t1": None}, True),
            ("t1", None, {"t1": None}, True),
        ],
    )
    def test(self, thread_id, updated_at, existing, expected):
        assert should_skip_conversation(thread_id, updated_at, existing) == expected

    def test_empty_set(self):
        assert should_skip_conversation("t1", 1000, set()) is False
        assert should_skip_conversation("t1", 1000, {}) is False
