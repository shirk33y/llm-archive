from __future__ import annotations

from pathlib import Path

from llm_archive.export import (
    _escape_body,
    render_thread,
    thread_md_path,
    write_thread,
    backfill,
    export_dir,
)
from llm_archive.config import AppConfig


def _make_thread(id: str, source: str, title: str, n_msgs: int = 2) -> dict:
    return {
        "thread": {
            "id": id,
            "source_id": source,
            "title": title,
            "created_at": 1700000000000 + 1000 * n_msgs,
            "updated_at": 1700000100000 + 1000 * n_msgs,
        },
        "messages": [
            {
                "id": i + 1,
                "role": "user" if i % 2 == 0 else "assistant",
                "created_at": 1700000000000 + i * 5000,
                "parts": [
                    {"kind": "text", "text": f"Message {i+1} content", "visible": True},
                ],
            }
            for i in range(n_msgs)
        ],
    }


def test_escape_body_hashes():
    body = "## heading\nline\n# also heading"
    assert _escape_body(body) == "\\## heading\nline\n\\# also heading"


def test_escape_body_no_hashes():
    body = "plain text\nwith **bold**"
    assert _escape_body(body) == body


def test_escape_body_html_comment():
    body = "text <!-- hidden --> more"
    result = _escape_body(body)
    assert "<!--" not in result
    assert "<!&#45;" in result


def test_escape_body_comment_close():
    body = "text --> after"
    result = _escape_body(body)
    assert "-->" not in result
    assert "&#45;&#45;>" in result


def test_render_thread_simple():
    td = _make_thread("test:1", "claude", "Hello")
    out = render_thread(td)

    assert "<!-- thread:test:1 source:claude -->" in out
    assert "# Hello" in out
    assert "## user · 3" in out
    assert "## assistant · 4" in out
    assert "Message 1 content" in out
    assert "Message 2 content" in out


def test_render_thread_hides_invisible_parts():
    td = _make_thread("test:2", "opencode", "Hidden")
    td["messages"][0]["parts"][0]["visible"] = False
    out = render_thread(td)
    assert "Message 1 content" not in out


def test_render_thread_escape_hash_in_body():
    td = _make_thread("test:3", "claude", "Hash")
    td["messages"][0]["parts"][0]["text"] = "some text\n" "# pseudo-heading\n" "## another pseudo\n" "more text"
    out = render_thread(td)
    assert "\\# pseudo-heading" in out
    assert "\\## another pseudo" in out


def test_render_thread_escape_html_comment():
    td = _make_thread("test:4", "chatgpt", "Comment")
    td["messages"][0]["parts"][0]["text"] = "hello <!-- comment --> world"
    out = render_thread(td)
    assert "hello <!&#45; comment &#45;&#45;> world" in out


def test_render_thread_tool_call():
    td = _make_thread("test:5", "codex", "Tools")
    td["messages"][0]["parts"] = [
        {"kind": "text", "text": "Let me search", "visible": True},
        {"kind": "tool_call", "text": "search(\"foo\")", "visible": True, "tool_name": "search"},
        {"kind": "tool_result", "text": "found bar", "visible": True, "tool_is_error": 0},
    ]
    out = render_thread(td)
    assert "**▸ search**" in out
    assert 'search("foo")' in out
    assert "**◀ result**" in out
    assert "found bar" in out


def test_render_thread_error_result():
    td = _make_thread("test:6", "deepseek", "Error")
    td["messages"][0]["parts"] = [
        {"kind": "tool_call", "text": "cmd", "visible": True, "tool_name": "bash"},
        {"kind": "tool_result", "text": "exit 1", "visible": True, "tool_is_error": 1},
    ]
    out = render_thread(td)
    assert "**▸ bash**" in out
    assert "**◀ error**" in out


def test_render_thread_reasoning():
    td = _make_thread("test:7", "claude", "Reason")
    td["messages"][0]["parts"] = [
        {"kind": "reasoning", "text": "I need to think about this", "visible": True},
        {"kind": "text", "text": "Answer", "visible": True},
    ]
    out = render_thread(td)
    assert "*reasoning:*" in out
    assert "I need to think about this" in out


def test_export_dir_default():
    dir = export_dir()
    assert dir.name == "exports"


def test_export_dir_config():
    config = AppConfig()
    config.export.dir = "/tmp/llm-test-md"
    dir = export_dir(config)
    assert dir == Path("/tmp/llm-test-md")


def test_thread_md_path():
    config = AppConfig()
    config.export.dir = "/tmp/llm-test-md"
    path = thread_md_path("claude", "abc:123", config)
    assert path == Path("/tmp/llm-test-md/claude_abc:123.md")


def test_write_thread(tmp_path, con):
    from llm_archive import db
    from llm_archive.schema import IngestedThread, IngestedMessage, IngestedPart

    thread = IngestedThread(
        id="write:test",
        source_id="claude",
        title="Write test",
        created_at=1700000000000,
        updated_at=1700000100000,
        messages=[
            IngestedMessage(
                id=1,
                thread_id="write:test",
                role="user",
                created_at=1700000000000,
                content="foo",
                parts=[
                    IngestedPart(kind="text", text="hello world"),
                ],
            ),
        ],
    )
    db.save_thread(con, thread)

    config = AppConfig()
    config.export.dir = str(tmp_path / "exports")

    result = write_thread(con, "write:test", "claude", config)
    assert result is not None
    assert result.exists()

    content = result.read_text()
    assert "<!-- thread:write:test source:claude -->" in content
    assert "hello world" in content


def test_write_thread_freshness(tmp_path, con):
    from llm_archive import db
    from llm_archive.schema import IngestedThread, IngestedMessage, IngestedPart

    thread = IngestedThread(
        id="fresh:test",
        source_id="gemini",
        title="Fresh test",
        created_at=1700000000000,
        updated_at=1700000100000,
        messages=[
            IngestedMessage(
                id=1,
                thread_id="fresh:test",
                role="user",
                created_at=1700000000000,
                content="foo",
                parts=[IngestedPart(kind="text", text="first version")],
            ),
        ],
    )
    db.save_thread(con, thread)

    config = AppConfig()
    config.export.dir = str(tmp_path / "exports")

    path = write_thread(con, "fresh:test", "gemini", config)
    assert path is not None
    content1 = path.read_text()
    assert "first version" in content1

    path2 = write_thread(con, "fresh:test", "gemini", config)
    assert path2 == path
    assert path2.read_text() == content1

    path3 = write_thread(con, "fresh:test", "gemini", config, force=True)
    assert path3 == path


def test_backfill(tmp_path, con):
    from llm_archive import db
    from llm_archive.schema import IngestedThread, IngestedMessage, IngestedPart

    for i in range(3):
        t = IngestedThread(
            id=f"backfill:{i}",
            source_id="claude",
            title=f"Thread {i}",
            created_at=1700000000000 + i,
            updated_at=1700000100000 + i,
            messages=[
                IngestedMessage(
                    id=i * 1000 + 1,
                    thread_id=f"backfill:{i}",
                    role="user",
                    created_at=1700000000000 + i,
                    content="foo",
                    parts=[IngestedPart(kind="text", text=f"content {i}")],
                ),
            ],
        )
        db.save_thread(con, t)

    config = AppConfig()
    config.export.dir = str(tmp_path / "exports")

    count = backfill(con, config=config)
    assert count == 3
    for i in range(3):
        p = thread_md_path("claude", f"backfill:{i}", config)
        assert p.exists()
        assert f"content {i}" in p.read_text()
