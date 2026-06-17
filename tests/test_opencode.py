from __future__ import annotations
import json
import sqlite3

from llm_archive.ingestors.opencode import (
    _build_thread,
    _map_tool_name,
    _parse_tool_part,
    OpenCodeIngestor,
)


class TestMapToolName:
    def test_known_mappings(self):
        assert _map_tool_name("bash") == "Bash"
        assert _map_tool_name("read") == "Read"
        assert _map_tool_name("write") == "Write"
        assert _map_tool_name("edit") == "Edit"
        assert _map_tool_name("patch") == "Edit"
        assert _map_tool_name("glob") == "Glob"
        assert _map_tool_name("grep") == "Grep"
        assert _map_tool_name("task") == "Task"
        assert _map_tool_name("todowrite") == "TodoWrite"

    def test_unknown_passthrough(self):
        assert _map_tool_name("custom") == "custom"


class TestParseToolPart:
    def test_bash_with_workdir(self):
        data = {
            "tool": "bash",
            "callID": "c1",
            "state": {
                "input": {"command": "ls -la", "workdir": "/tmp"},
                "output": "file1.txt\nfile2.txt",
                "status": "success",
            },
        }
        part = _parse_tool_part(data)
        assert part is not None
        assert part.tool_call.name == "Bash"
        assert part.tool_call.input["command"] == "cd /tmp && ls -la"

    def test_bash_without_workdir(self):
        data = {
            "tool": "bash",
            "callID": "c2",
            "state": {
                "input": {"command": "git status"},
                "output": "nothing to commit",
                "status": "success",
            },
        }
        part = _parse_tool_part(data)
        assert part is not None
        assert part.tool_call.input["command"] == "git status"

    def test_error_from_status(self):
        data = {
            "tool": "bash",
            "callID": "c3",
            "state": {
                "input": {"command": "bad_cmd"},
                "output": "Error: command not found",
                "status": "error",
            },
        }
        part = _parse_tool_part(data)
        assert part is not None
        assert part.tool_call.is_error is True

    def test_error_from_exit_code(self):
        data = {
            "tool": "bash",
            "callID": "c4",
            "state": {
                "input": {"command": "false"},
                "output": "",
                "status": "success",
                "metadata": {"exit": 1},
            },
        }
        part = _parse_tool_part(data)
        assert part is not None
        assert part.tool_call.is_error is True

    def test_non_bash_tool(self):
        data = {
            "tool": "read",
            "callID": "c5",
            "state": {
                "input": {"file_path": "/tmp/foo.py"},
                "output": "content here",
                "status": "success",
            },
        }
        part = _parse_tool_part(data)
        assert part is not None
        assert part.tool_call.name == "Read"
        assert part.tool_call.input == {"file_path": "/tmp/foo.py"}

    def test_none_output_treated_as_empty(self):
        data = {
            "tool": "bash",
            "callID": "c6",
            "state": {
                "input": {"command": "ls"},
                "output": None,
                "status": "success",
            },
        }
        part = _parse_tool_part(data)
        assert part is not None
        assert part.tool_call.result is None


def _create_opencode_db(db_path, sessions=None, messages=None, parts=None):
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE session (id TEXT, title TEXT, time_created INTEGER, time_updated INTEGER)")
    con.execute("CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, data TEXT)")
    con.execute("CREATE TABLE part (id TEXT, message_id TEXT, time_created INTEGER, data TEXT)")

    for s in sessions or []:
        con.execute("INSERT INTO session VALUES (?, ?, ?, ?)", s)
    for m in messages or []:
        con.execute("INSERT INTO message VALUES (?, ?, ?, ?)", m)
    for p in parts or []:
        con.execute("INSERT INTO part VALUES (?, ?, ?, ?)", p)
    con.commit()
    con.close()


class TestBuildThread:
    def test_basic_thread(self, tmp_path):
        db_path = tmp_path / "opencode.db"
        sessions = [("s1", "My Session", 1000, 2000)]
        messages = [
            ("m1", "s1", 1500, json.dumps({"role": "user", "model": {"providerID": "anthropic", "modelID": "claude-3"}})),
            ("m2", "s1", 1600, json.dumps({"role": "assistant", "model": {"providerID": "anthropic", "modelID": "claude-3"}})),
        ]
        parts = [
            ("p1", "m1", 1500, json.dumps({"type": "text", "text": "hello"})),
            ("p2", "m2", 1600, json.dumps({"type": "text", "text": "hi there"})),
        ]
        _create_opencode_db(db_path, sessions, messages, parts)

        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        sess = con.execute("SELECT * FROM session WHERE id=?", ("s1",)).fetchone()
        thread = _build_thread(con, sess)
        con.close()

        assert thread is not None
        assert thread.id == "opencode:s1"
        assert thread.title == "My Session"
        assert len(thread.messages) == 2
        assert thread.messages[0].role == "user"
        assert thread.messages[1].role == "assistant"
        assert thread.messages[1].metadata.get("model") == "anthropic/claude-3"

    def test_tool_part(self, tmp_path):
        db_path = tmp_path / "opencode.db"
        sessions = [("s1", "Session", 1000, 2000)]
        messages = [
            ("m1", "s1", 1500, json.dumps({"role": "user"})),
            ("m2", "s1", 1600, json.dumps({"role": "assistant"})),
        ]
        parts = [
            ("p1", "m1", 1500, json.dumps({"type": "text", "text": "list files"})),
            ("p2", "m2", 1600, json.dumps({"type": "tool", "tool": "bash", "callID": "c1", "state": {"input": {"command": "ls"}, "output": "file1", "status": "success"}})),
            ("p3", "m2", 1601, json.dumps({"type": "text", "text": "done"})),
        ]
        _create_opencode_db(db_path, sessions, messages, parts)

        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        sess = con.execute("SELECT * FROM session WHERE id=?", ("s1",)).fetchone()
        thread = _build_thread(con, sess)
        con.close()

        assert thread is not None
        assert len(thread.messages) == 2
        tool_parts = [p for p in thread.messages[1].parts if p.kind == "tool_call"]
        assert len(tool_parts) == 1
        assert tool_parts[0].tool_call.name == "Bash"

    def test_no_messages_returns_none(self, tmp_path):
        db_path = tmp_path / "opencode.db"
        sessions = [("s1", "Empty", 1000, 2000)]
        _create_opencode_db(db_path, sessions)

        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        sess = con.execute("SELECT * FROM session WHERE id=?", ("s1",)).fetchone()
        thread = _build_thread(con, sess)
        con.close()

        assert thread is None

    def test_reasoning_part(self, tmp_path):
        db_path = tmp_path / "opencode.db"
        sessions = [("s1", "Session", 1000, 2000)]
        messages = [
            ("m1", "s1", 1500, json.dumps({"role": "assistant"})),
        ]
        parts = [
            ("p1", "m1", 1500, json.dumps({"type": "reasoning", "reasoning": "let me think"})),
            ("p2", "m1", 1501, json.dumps({"type": "text", "text": "answer"})),
        ]
        _create_opencode_db(db_path, sessions, messages, parts)

        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        sess = con.execute("SELECT * FROM session WHERE id=?", ("s1",)).fetchone()
        thread = _build_thread(con, sess)
        con.close()

        assert thread is not None
        reasoning_parts = [p for p in thread.messages[0].parts if p.kind == "reasoning"]
        assert len(reasoning_parts) == 1


class TestOpenCodeIngestor:
    def test_count_threads_no_db(self, tmp_path):
        import asyncio
        ingestor = OpenCodeIngestor(db_path=tmp_path / "nope.db")
        count = asyncio.run(ingestor.count_threads())
        assert count == 0

    def test_requires_auth(self):
        import asyncio
        ingestor = OpenCodeIngestor()
        assert asyncio.run(ingestor.requires_auth()) is False

    def test_count_threads_with_db(self, tmp_path):
        import asyncio
        db_path = tmp_path / "opencode.db"
        _create_opencode_db(db_path, [("s1", "T", 1, 2)])
        ingestor = OpenCodeIngestor(db_path=db_path)
        count = asyncio.run(ingestor.count_threads())
        assert count == 1