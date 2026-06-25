from __future__ import annotations

from llm_archive.dev_mode import DevMode, reexec


def test_dev_mode_disabled_when_watch_false():
    from llm_archive.config import AppConfig

    config = AppConfig()
    assert config.dev.watch is False
    assert DevMode.from_config(config, reload=reexec) is None


def test_dev_mode_from_config_creates_instance(tmp_path, monkeypatch):
    from llm_archive.config import AppConfig, DevConfig

    monkeypatch.setattr("llm_archive.dev_mode.config_path", lambda: tmp_path / "config.toml")
    cfg = tmp_path / "config.toml"
    cfg.write_text("")

    config = AppConfig(dev=DevConfig(watch=True, debounce_ms=500))
    mode = DevMode.from_config(config, reload=reexec)
    assert mode is not None
    assert mode._debounce_s == 0.5


def test_dev_mode_poll_detects_change(tmp_path, monkeypatch):
    import time

    from llm_archive.config import AppConfig, DevConfig

    monkeypatch.setattr("llm_archive.dev_mode.config_path", lambda: tmp_path / "config.toml")
    cfg = tmp_path / "config.toml"
    cfg.write_text("key = 1")

    config = AppConfig(dev=DevConfig(watch=True, debounce_ms=100))
    mode = DevMode.from_config(config, reload=reexec)
    assert mode is not None
    mode.start()

    time.sleep(0.015)
    cfg.write_text("key = 2")
    assert mode.poll()


def test_reexec_defined():
    assert callable(reexec)
    assert reexec.__name__ == "reexec"
