from __future__ import annotations

from unittest.mock import MagicMock, PropertyMock, patch


def _mock_app() -> MagicMock:
    app = MagicMock()
    app.console.size.width = 80
    app.suspend = MagicMock()
    app.suspend.__enter__ = lambda s: None
    app.suspend.__exit__ = lambda *a: None
    return app


def _make_screen(thread_id="test:t1", source_id="chatgpt", title="Test", updated_at=2000000, con=None):
    from llm_archive.tui import ShowScreen
    screen = ShowScreen.__new__(ShowScreen)
    screen.thread_data = {
        "thread": {"id": thread_id, "source_id": source_id, "title": title, "updated_at": updated_at},
        "messages": [],
    }
    screen.con = con or MagicMock()
    screen._verbose = False
    screen._summary_idx = 0
    return screen


def _write_side(source_id, thread_id, config, md_dir):
    from llm_archive.export import thread_md_path
    path = thread_md_path(source_id, thread_id, config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("rendered content")
    return path


def test_open_pager_writes_missing_cache(tmp_path):
    from llm_archive.tui import ShowScreen
    from llm_archive.export import thread_md_path

    md_dir = tmp_path / "exports"
    screen = _make_screen(thread_id="test:t1", source_id="chatgpt", updated_at=2000000)
    mock_app = _mock_app()

    with (
        patch.object(ShowScreen, "app", new_callable=PropertyMock, return_value=mock_app),
        patch("llm_archive.config.load_config") as mock_load,
        patch("llm_archive.glow.is_available", return_value=True),
        patch("llm_archive.glow.view", return_value=0) as mock_view,
        patch("llm_archive.export.write_thread") as mock_write,
    ):
        mock_load.return_value.export.dir = str(md_dir)
        mock_load.return_value.export.auto = True
        mock_write.side_effect = lambda con, tid, sid, config, force=True: _write_side(sid, tid, config, md_dir)

        screen._open_pager()

        expected = thread_md_path("chatgpt", "test:t1", mock_load.return_value)
        mock_write.assert_called_once_with(
            screen.con, "test:t1", "chatgpt", mock_load.return_value, force=True
        )
        mock_view.assert_called_once_with(expected, width=78)


def test_open_pager_skips_write_when_cache_fresh(tmp_path):
    from llm_archive.tui import ShowScreen

    md_dir = tmp_path / "exports"
    md_dir.mkdir(parents=True, exist_ok=True)
    md_path = md_dir / "chatgpt_test:t2.md"
    md_path.write_text("fresh content")
    old_mtime = md_path.stat().st_mtime

    screen = _make_screen(
        thread_id="test:t2",
        source_id="chatgpt",
        updated_at=int(old_mtime * 1000),
    )
    mock_app = _mock_app()

    with (
        patch.object(ShowScreen, "app", new_callable=PropertyMock, return_value=mock_app),
        patch("llm_archive.config.load_config") as mock_load,
        patch("llm_archive.glow.is_available", return_value=True),
        patch("llm_archive.glow.view", return_value=0) as mock_view,
        patch("llm_archive.export.write_thread") as mock_write,
    ):
        mock_load.return_value.export.dir = str(md_dir)
        mock_load.return_value.export.auto = True

        screen._open_pager()

        mock_write.assert_not_called()
        mock_view.assert_called_once()


def test_open_pager_writes_when_cache_stale(tmp_path):
    from llm_archive.tui import ShowScreen

    md_dir = tmp_path / "exports"
    md_dir.mkdir(parents=True, exist_ok=True)
    md_path = md_dir / "chatgpt_test:t3.md"
    md_path.write_text("stale content")

    screen = _make_screen(
        thread_id="test:t3",
        source_id="chatgpt",
        updated_at=99999999999999,
    )
    mock_app = _mock_app()

    with (
        patch.object(ShowScreen, "app", new_callable=PropertyMock, return_value=mock_app),
        patch("llm_archive.config.load_config") as mock_load,
        patch("llm_archive.glow.is_available", return_value=True),
        patch("llm_archive.glow.view", return_value=0) as mock_view,
        patch("llm_archive.export.write_thread") as mock_write,
    ):
        mock_load.return_value.export.dir = str(md_dir)
        mock_load.return_value.export.auto = True
        mock_write.side_effect = lambda con, tid, sid, config, force=True: _write_side(sid, tid, config, md_dir)

        screen._open_pager()

        mock_write.assert_called_once_with(
            screen.con, "test:t3", "chatgpt", mock_load.return_value, force=True
        )
        mock_view.assert_called_once()


def test_open_pager_falls_back_to_rich_when_glow_unavailable(tmp_path):
    screen = _make_screen(
        thread_id="test:t4",
        source_id="chatgpt",
        updated_at=1000,
    )

    with (
        patch("llm_archive.glow.is_available", return_value=False),
        patch("llm_archive.export.write_thread") as mock_write,
        patch("shutil.which", return_value=None),
        patch("builtins.print") as mock_print,
        patch.object(screen, "_render_content", return_value="rendered"),
    ):
        screen._open_pager()

        mock_write.assert_not_called()
        mock_print.assert_called_once_with("rendered")
