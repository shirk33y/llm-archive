from __future__ import annotations
from pathlib import Path

from llm_archive.browser_profiles import BrowserProfile, _browser_name, _chromium_roots, _unique


class TestBrowserName:
    def test_firefox(self):
        assert _browser_name(Path("/home/user/.mozilla/firefox")) == "firefox"

    def test_waterfox(self):
        assert _browser_name(Path("/home/user/.waterfox")) == "waterfox"

    def test_librewolf(self):
        assert _browser_name(Path("/home/user/.librewolf")) == "librewolf"


class TestChromiumRoots:
    def test_returns_list_of_tuples(self):
        roots = _chromium_roots()
        assert isinstance(roots, list)
        for item in roots:
            assert isinstance(item, tuple)
            assert len(item) == 2
            assert isinstance(item[0], str)
            assert isinstance(item[1], Path)

    def test_contains_chrome(self):
        roots = _chromium_roots()
        browsers = [r[0] for r in roots]
        assert "chrome" in browsers


class TestUnique:
    def test_deduplicates_by_path(self):
        p1 = BrowserProfile("chrome", "chromium", "Default", Path("/tmp/Default"))
        p2 = BrowserProfile("chrome", "chromium", "Default", Path("/tmp/Default"))
        result = _unique([p1, p2])
        assert len(result) == 1

    def test_preserves_different_paths(self):
        p1 = BrowserProfile("chrome", "chromium", "Default", Path("/tmp/Default"))
        p2 = BrowserProfile("chrome", "chromium", "Profile1", Path("/tmp/Profile1"))
        result = _unique([p1, p2])
        assert len(result) == 2

    def test_empty_list(self):
        assert _unique([]) == []