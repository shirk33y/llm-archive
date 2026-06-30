from __future__ import annotations

import os
from unittest.mock import MagicMock, patch


def _mock_app() -> MagicMock:
    app = MagicMock()
    app.console.size.width = 80
    app.suspend = MagicMock()
    app.suspend.__enter__ = lambda s: None
    app.suspend.__exit__ = lambda *a: None
    return app


def _make_data(
    thread_id="test:t1",
    source_id="chatgpt",
    title="Test",
    updated_at=2000000,
):
    return {
        "thread": {
            "id": thread_id,
            "source_id": source_id,
            "title": title,
            "updated_at": updated_at,
        },
        "messages": [],
    }


def test_open_pager_creates_stub_for_missing_cache(tmp_path):
    from llm_archive.tui import _open_thread_pager
    from llm_archive.export import thread_md_path

    md_dir = tmp_path / "exports"
    con = MagicMock()
    data = _make_data(thread_id="test:t1", source_id="chatgpt", updated_at=2000000)
    mock_app = _mock_app()

    with (
        patch("llm_archive.config.load_config") as mock_load,
        patch("llm_archive.glow.is_available", return_value=True),
        patch("llm_archive.glow.view", return_value=0) as mock_view,
        patch("llm_archive.export.write_thread") as mock_write,
    ):
        mock_load.return_value.export.dir = str(md_dir)
        mock_load.return_value.export.auto = True

        _open_thread_pager(mock_app, data, con)

        expected = thread_md_path("chatgpt", "test:t1", mock_load.return_value)
        mock_write.assert_not_called()
        assert expected.read_text().startswith("<!-- thread:test:t1 source:chatgpt -->")
        mock_view.assert_called_once_with(expected, width=78)


def test_opening_screen_prints_centered_title():
    from llm_archive.tui import _show_opening_screen

    with (
        patch("shutil.get_terminal_size", return_value=os.terminal_size((80, 24))),
        patch("builtins.print") as mock_print,
    ):
        _show_opening_screen("headless battle seams (@explore subagent)", 80)

    output = "".join(
        "".join(str(arg) for arg in call.args)
        for call in mock_print.call_args_list
    )
    assert "\x1b[40m\x1b[37m\x1b[2J\x1b[H" in output
    assert 'Opening "headless battle seams (@explore subagent)"' in output


def test_open_pager_skips_write_when_cache_fresh(tmp_path):
    from llm_archive.tui import _open_thread_pager

    md_dir = tmp_path / "exports"
    md_dir.mkdir(parents=True, exist_ok=True)
    md_path = md_dir / "chatgpt_test:t2.md"
    md_path.write_text("fresh content")
    old_mtime = md_path.stat().st_mtime

    con = MagicMock()
    data = _make_data(
        thread_id="test:t2",
        source_id="chatgpt",
        updated_at=int(old_mtime * 1000),
    )
    mock_app = _mock_app()

    with (
        patch("llm_archive.config.load_config") as mock_load,
        patch("llm_archive.glow.is_available", return_value=True),
        patch("llm_archive.glow.view", return_value=0) as mock_view,
        patch("llm_archive.export.write_thread") as mock_write,
    ):
        mock_load.return_value.export.dir = str(md_dir)
        mock_load.return_value.export.auto = True

        _open_thread_pager(mock_app, data, con)

        mock_write.assert_not_called()
        mock_view.assert_called_once()


def test_open_pager_writes_when_cache_stale(tmp_path):
    from llm_archive.tui import _open_thread_pager

    md_dir = tmp_path / "exports"
    md_dir.mkdir(parents=True, exist_ok=True)
    md_path = md_dir / "chatgpt_test:t3.md"
    md_path.write_text("stale content")

    con = MagicMock()
    data = _make_data(
        thread_id="test:t3",
        source_id="chatgpt",
        updated_at=99999999999999,
    )
    mock_app = _mock_app()

    with (
        patch("llm_archive.config.load_config") as mock_load,
        patch("llm_archive.glow.is_available", return_value=True),
        patch("llm_archive.glow.view", return_value=0) as mock_view,
        patch("llm_archive.export.write_thread") as mock_write,
    ):
        mock_load.return_value.export.dir = str(md_dir)
        mock_load.return_value.export.auto = True

        _open_thread_pager(mock_app, data, con)

        mock_write.assert_not_called()
        mock_view.assert_called_once()


def test_open_pager_falls_back_to_rich_when_glow_unavailable(tmp_path):
    from llm_archive.tui import _open_thread_pager

    con = MagicMock()
    data = _make_data(thread_id="test:t4", source_id="chatgpt", updated_at=1000)
    mock_app = _mock_app()

    with (
        patch("llm_archive.glow.is_available", return_value=False),
        patch("llm_archive.export.write_thread") as mock_write,
        patch("shutil.which", return_value=None),
        patch("llm_archive.tui._render_thread_content", return_value="rendered"),
        patch("builtins.print") as mock_print,
    ):
        _open_thread_pager(mock_app, data, con)

        mock_write.assert_not_called()
        mock_print.assert_called_once_with("rendered")


def test_open_pager_uses_less_for_huge_file(tmp_path):
    from llm_archive.tui import _open_thread_pager
    from llm_archive.export import thread_md_path
    from llm_archive import glow

    md_dir = tmp_path / "exports"
    md_dir.mkdir(parents=True, exist_ok=True)

    con = MagicMock()
    data = _make_data(
        thread_id="test:huge",
        source_id="chatgpt",
        updated_at=99999999999999,
    )
    mock_app = _mock_app()

    with (
        patch("llm_archive.config.load_config") as mock_load,
        patch("llm_archive.glow.is_available", return_value=True),
        patch("llm_archive.glow.view") as mock_view,
        patch("subprocess.run") as mock_run,
    ):
        mock_load.return_value.export.dir = str(md_dir)
        mock_load.return_value.export.auto = True
        thread_md_path("chatgpt", "test:huge", mock_load.return_value).write_bytes(
            b"x" * (glow.MAX_SIZE + 1)
        )

        _open_thread_pager(mock_app, data, con)

        mock_view.assert_not_called()
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd[0].endswith("/less") or cmd[0] == "less"
        assert "-R" in cmd
