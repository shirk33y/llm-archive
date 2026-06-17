from __future__ import annotations

from llm_archive.ingestors.opencode import _map_tool_name, _parse_tool_part


def _make_tool_part(
    call_id: str = "call_func_123_1",
    tool: str = "bash",
    command: str = "echo hi",
    output: str = "hi",
    status: str = "completed",
    exit_code: int = 0,
) -> dict:
    part: dict = {
        "type": "tool",
        "callID": call_id,
        "tool": tool,
        "state": {
            "status": status,
            "input": {"command": command, "description": "test"},
            "output": output,
            "metadata": {"exit": exit_code},
        },
    }
    return part


def test_parse_bash_tool():
    data = _make_tool_part()
    tp = _parse_tool_part(data)
    assert tp is not None
    assert tp.kind == "tool_call"
    tc = tp.tool_call
    assert tc is not None
    assert tc.name == "Bash"
    assert tc.tool_use_id == "call_func_123_1"
    assert tc.input == {"command": "echo hi"}
    assert tc.result == "hi"
    assert not tc.is_error


def test_parse_read_tool():
    data = _make_tool_part(tool="read", command="cat foo.txt")
    data["state"]["input"] = {"file_path": "/tmp/test.txt"}
    tp = _parse_tool_part(data)
    assert tp.tool_call.name == "Read"
    assert tp.tool_call.input == {"file_path": "/tmp/test.txt"}


def test_map_tool_name():
    assert _map_tool_name("bash") == "Bash"
    assert _map_tool_name("read") == "Read"
    assert _map_tool_name("write") == "Write"
    assert _map_tool_name("edit") == "Edit"
    assert _map_tool_name("glob") == "Glob"
    assert _map_tool_name("grep") == "Grep"
    assert _map_tool_name("webfetch") == "WebFetch"
    assert _map_tool_name("websearch") == "WebSearch"
    assert _map_tool_name("task") == "Task"
    assert _map_tool_name("todowrite") == "TodoWrite"
    assert _map_tool_name("codesearch") == "Grep"
    assert _map_tool_name("unknown_tool") == "unknown_tool"


def test_detect_error_from_status():
    data = _make_tool_part(status="error", output="Something went wrong")
    tp = _parse_tool_part(data)
    assert tp.tool_call.is_error
    assert "Something went wrong" in tp.tool_call.result


def test_detect_error_from_exit_code():
    data = _make_tool_part(exit_code=1, output="command not found")
    tp = _parse_tool_part(data)
    assert tp.tool_call.is_error


def test_no_error_on_success():
    data = _make_tool_part(exit_code=0, output="done")
    tp = _parse_tool_part(data)
    assert not tp.tool_call.is_error


def test_bash_workdir_normalized():
    data = _make_tool_part()
    data["state"]["input"]["workdir"] = "/home/user/project"
    data["state"]["input"]["command"] = "npm test"
    tp = _parse_tool_part(data)
    assert tp.tool_call.input == {"command": "cd /home/user/project && npm test"}


def test_bash_no_workdir_preserves_command():
    data = _make_tool_part()
    tp = _parse_tool_part(data)
    assert tp.tool_call.input == {"command": "echo hi"}


def test_tool_use_id_preserved():
    data = _make_tool_part(call_id="call_function_xyz_42")
    tp = _parse_tool_part(data)
    assert tp.tool_call.tool_use_id == "call_function_xyz_42"


def test_result_not_truncated():
    long_output = "line\n" * 1000
    data = _make_tool_part(output=long_output)
    tp = _parse_tool_part(data)
    assert len(tp.tool_call.result) == len(long_output)


def test_mcp_tool_preserved():
    data = _make_tool_part(tool="codebase-memory-mcp_search_graph",
                           command="search", output="results")
    data["state"]["input"] = {"query": "test"}
    tp = _parse_tool_part(data)
    assert tp.tool_call.name == "codebase-memory-mcp_search_graph"
