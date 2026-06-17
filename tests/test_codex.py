from __future__ import annotations
import json
import sqlite3

import asyncio

from llm_archive.ingestors.codex import (
    _detect_error,
    _extract_text,
    _find_session_file,
    _load_thread_map,
    _map_codex_tool_name,
    _normalize_codex_command,
    _parse_codex_function_call,
    _parse_codex_patch,
    _parse_session,
    CodexIngestor,
)


class TestNormalizeCodexCommand:
    def test_strips_bash_lc_prefix(self):
        assert _normalize_codex_command("/bin/bash -lc ls -la") == "ls -la"

    def test_strips_quotes(self):
        assert _normalize_codex_command('"echo hello"') == "echo hello"

    def test_strips_single_quotes(self):
        assert _normalize_codex_command("'echo hello'") == "echo hello"

    def test_plain_command(self):
        assert _normalize_codex_command("git status") == "git status"

    def test_strips_bash_then_quotes(self):
        result = _normalize_codex_command("/bin/bash -lc 'cd /tmp && ls'")
        assert result == "cd /tmp && ls"


class TestMapCodexToolName:
    def test_known_mappings(self):
        assert _map_codex_tool_name("exec_command") == "Bash"
        assert _map_codex_tool_name("apply_patch") == "Edit"
        assert _map_codex_tool_name("read_file") == "Read"
        assert _map_codex_tool_name("write_file") == "Write"
        assert _map_codex_tool_name("list_directory") == "Glob"
        assert _map_codex_tool_name("search_files") == "Grep"
        assert _map_codex_tool_name("run_terminal_command") == "Bash"

    def test_unknown_passes_through(self):
        assert _map_codex_tool_name("custom_tool") == "custom_tool"


class TestDetectError:
    def test_error_patterns(self):
        assert _detect_error("Error: something failed")
        assert _detect_error("exit code 1")
        assert _detect_error("FAILED")
        assert _detect_error("command not found: foo")
        assert _detect_error("no such file or directory")
        assert _detect_error("permission denied")

    def test_no_error(self):
        assert not _detect_error("Success!")
        assert not _detect_error("")

    def test_case_insensitive(self):
        assert _detect_error("ERROR: bad")
        assert _detect_error("Failed to connect")


class TestParseCodexFunctionCall:
    def test_exec_command(self):
        args = json.dumps({"cmd": "ls -la", "workdir": "/tmp"})
        tc = _parse_codex_function_call("exec_command", args, "call1")
        assert tc is not None
        assert tc.name == "Bash"
        assert tc.input["command"] == "ls -la"
        assert tc.input["workdir"] == "/tmp"

    def test_exec_command_no_workdir(self):
        args = json.dumps({"cmd": "git status"})
        tc = _parse_codex_function_call("exec_command", args, "call2")
        assert tc is not None
        assert tc.name == "Bash"
        assert "workdir" not in tc.input

    def test_apply_patch_edit(self):
        args = json.dumps({"file_path": "foo.py", "old_string": "a", "new_string": "b"})
        tc = _parse_codex_function_call("apply_patch", args, "call3")
        assert tc is not None
        assert tc.name == "Edit"
        assert tc.input["file_path"] == "foo.py"

    def test_apply_patch_write(self):
        patch = "*** Add File: new.py\n+hello world"
        tc = _parse_codex_function_call("apply_patch", patch, "call4")
        assert tc is not None
        assert tc.name == "Edit"
        assert tc.input["file_path"] == "new.py"
        assert tc.input["content"] == "hello world"

    def test_apply_patch_raw_string(self):
        patch = "*** Add File: new.py\n+hello world"
        tc = _parse_codex_function_call("apply_patch", patch, "call5")
        assert tc is not None
        assert tc.name == "Edit"
        assert tc.input["file_path"] == "new.py"
        assert tc.input["content"] == "hello world"

    def test_unknown_tool_passthrough(self):
        args = json.dumps({"key": "val"})
        tc = _parse_codex_function_call("custom_tool", args, "call6")
        assert tc is not None
        assert tc.name == "custom_tool"
        assert tc.input == {"key": "val"}

    def test_invalid_json_args(self):
        tc = _parse_codex_function_call("read_file", "not json", "call7")
        assert tc is not None
        assert tc.input is None


class TestParseCodexPatch:
    def test_add_file(self):
        patch = "*** Add File: hello.py\n+print('hello')"
        result = _parse_codex_patch(patch)
        assert result["file_path"] == "hello.py"
        assert result["content"] == "print('hello')"

    def test_update_file(self):
        patch = "*** Update File: foo.py\n-old\n+new\n context"
        result = _parse_codex_patch(patch)
        assert result["file_path"] == "foo.py"
        assert "old" in result["old_string"]
        assert "new" in result["new_string"]
        assert "context" in result["old_string"]
        assert "context" in result["new_string"]

    def test_skip_at_markers(self):
        patch = "*** Update File: a.py\n@@ -1,3 +1,3 @@\n-old\n+new"
        result = _parse_codex_patch(patch)
        assert "@@" not in result["old_string"]
        assert "@@" not in result["new_string"]


class TestExtractText:
    def test_string_input(self):
        assert _extract_text("hello") == "hello"

    def test_non_string_non_list(self):
        assert _extract_text(42) == "42"

    def test_text_blocks(self):
        content = [
            {"type": "text", "text": "hello"},
            {"type": "input_text", "text": "world"},
        ]
        assert _extract_text(content) == "hello\n\nworld"

    def test_empty_text_skipped(self):
        content = [
            {"type": "text", "text": ""},
            {"type": "text", "text": "real"},
        ]
        assert _extract_text(content) == "real"

    def test_unknown_type_skipped(self):
        content = [
            {"type": "image", "url": "http://x"},
            {"type": "text", "text": "ok"},
        ]
        assert _extract_text(content) == "ok"

    def test_non_dict_items(self):
        content = ["plain text", {"type": "text", "text": "more"}]
        assert _extract_text(content) == "plain text\n\nmore"


class TestParseSession:
    def test_user_and_assistant_messages(self, tmp_path):
        session_file = tmp_path / "session.jsonl"
        entries = [
            {"type": "response_item", "timestamp": 1700000000, "payload": {"type": "message", "role": "user", "content": "hello"}, "uuid": "m1"},
            {"type": "response_item", "timestamp": 1700000001, "payload": {"type": "message", "role": "assistant", "content": "hi there"}, "uuid": "m2"},
        ]
        session_file.write_text("\n".join(json.dumps(e) for e in entries))
        result = _parse_session(session_file, "abc123", {"title": "My Session"})
        assert result is not None
        assert result.title == "My Session"
        assert len(result.messages) == 2
        assert result.messages[0].role == "user"
        assert result.messages[1].role == "assistant"

    def test_developer_messages_skipped(self, tmp_path):
        session_file = tmp_path / "session.jsonl"
        entries = [
            {"type": "response_item", "timestamp": 1700000000, "payload": {"type": "message", "role": "developer", "content": "system"}, "uuid": "m1"},
            {"type": "response_item", "timestamp": 1700000001, "payload": {"type": "message", "role": "user", "content": "hello"}, "uuid": "m2"},
        ]
        session_file.write_text("\n".join(json.dumps(e) for e in entries))
        result = _parse_session(session_file, "abc123", {})
        assert result is not None
        assert len(result.messages) == 1

    def test_function_call_and_output(self, tmp_path):
        session_file = tmp_path / "session.jsonl"
        entries = [
            {"type": "response_item", "timestamp": 1700000000, "payload": {"type": "message", "role": "user", "content": "run it"}, "uuid": "m1"},
            {"type": "response_item", "timestamp": 1700000001, "payload": {"type": "function_call", "name": "exec_command", "arguments": json.dumps({"cmd": "ls"}), "call_id": "c1"}, "uuid": "m2"},
            {"type": "response_item", "timestamp": 1700000002, "payload": {"type": "function_call_output", "call_id": "c1", "output": "file1.txt"}, "uuid": "m3"},
        ]
        session_file.write_text("\n".join(json.dumps(e) for e in entries))
        result = _parse_session(session_file, "abc123", {})
        assert result is not None
        assert len(result.messages) == 3

    def test_empty_session_returns_none(self, tmp_path):
        session_file = tmp_path / "session.jsonl"
        session_file.write_text("")
        result = _parse_session(session_file, "abc123", {})
        assert result is None

    def test_nonexistent_file_returns_none(self, tmp_path):
        result = _parse_session(tmp_path / "nope.jsonl", "abc123", {})
        assert result is None

    def test_title_from_first_user_message(self, tmp_path):
        session_file = tmp_path / "session.jsonl"
        entries = [
            {"type": "response_item", "timestamp": 1700000000, "payload": {"type": "message", "role": "user", "content": "This is a very long message that should be truncated"}, "uuid": "m1"},
        ]
        session_file.write_text("\n".join(json.dumps(e) for e in entries))
        result = _parse_session(session_file, "abc123", {})
        assert result is not None
        assert result.title == "This is a very long message that should be truncated"


class TestLoadThreadMap:
    def test_loads_threads(self, tmp_path):
        db_path = tmp_path / "state.sqlite"
        con = sqlite3.connect(str(db_path))
        con.execute("CREATE TABLE threads (id TEXT, title TEXT, created_at INTEGER, updated_at INTEGER, model TEXT, model_provider TEXT, preview TEXT)")
        con.execute("INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?)", ("t1", "Title", 1000, 2000, "gpt-4", "openai", "preview"))
        con.commit()
        con.close()

        result = _load_thread_map(db_path)
        assert "t1" in result
        assert result["t1"]["title"] == "Title"

    def test_with_since_filter(self, tmp_path):
        db_path = tmp_path / "state.sqlite"
        con = sqlite3.connect(str(db_path))
        con.execute("CREATE TABLE threads (id TEXT, title TEXT, created_at INTEGER, updated_at INTEGER, model TEXT, model_provider TEXT, preview TEXT)")
        con.execute("INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?)", ("t1", "Old", 1000, 2000, "gpt-4", "openai", "preview"))
        con.execute("INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?)", ("t2", "New", 1000, 5000, "gpt-4", "openai", "preview"))
        con.commit()
        con.close()

        result = _load_thread_map(db_path, since=3000)
        assert "t1" not in result
        assert "t2" in result


class TestFindSessionFile:
    def test_finds_file(self, tmp_path):
        session_dir = tmp_path / "sessions"
        session_dir.mkdir()
        (session_dir / "rollout-123-abc.jsonl").write_text("{}")
        result = _find_session_file(session_dir, "abc")
        assert result is not None
        assert result.name == "rollout-123-abc.jsonl"

    def test_returns_none_if_not_found(self, tmp_path):
        result = _find_session_file(tmp_path, "nonexistent")
        assert result is None


class TestCodexIngestor:
    def test_count_threads_no_db(self, tmp_path):
        ingestor = CodexIngestor(state_db=tmp_path / "nope.sqlite", sessions_root=tmp_path)
        count = asyncio.run(ingestor.count_threads())
        assert count == 0

    def test_requires_auth(self):
        ingestor = CodexIngestor()
        assert asyncio.run(ingestor.requires_auth()) is False

    def test_count_threads_with_db(self, tmp_path):
        db_path = tmp_path / "state.sqlite"
        con = sqlite3.connect(str(db_path))
        con.execute("CREATE TABLE threads (id TEXT, title TEXT, created_at INTEGER, updated_at INTEGER, model TEXT, model_provider TEXT, preview TEXT)")
        con.execute("INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?)", ("t1", "T", 1, 2, "m", "p", "x"))
        con.commit()
        con.close()

        ingestor = CodexIngestor(state_db=db_path, sessions_root=tmp_path)
        count = asyncio.run(ingestor.count_threads())
        assert count == 1