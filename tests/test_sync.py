from __future__ import annotations
import sqlite3

from llm_archive.sync import _do_ingest, _source_thread_count


class TestSourceThreadCount:
    def test_counts_threads_for_source(self, tmp_path):
        db_path = tmp_path / "test.db"
        con = sqlite3.connect(str(db_path))
        con.execute("CREATE TABLE threads (id TEXT, source_id TEXT, updated_at INTEGER)")
        con.execute("INSERT INTO threads VALUES (?, ?, ?)", ("t1", "claudecode", 1000))
        con.execute("INSERT INTO threads VALUES (?, ?, ?)", ("t2", "claudecode", 2000))
        con.execute("INSERT INTO threads VALUES (?, ?, ?)", ("t3", "deepseek", 3000))
        con.commit()

        assert _source_thread_count(con, "claudecode") == 2
        assert _source_thread_count(con, "deepseek") == 1
        assert _source_thread_count(con, "chatgpt") == 0
        con.close()

    def test_empty_table(self, tmp_path):
        db_path = tmp_path / "test.db"
        con = sqlite3.connect(str(db_path))
        con.execute("CREATE TABLE threads (id TEXT, source_id TEXT, updated_at INTEGER)")
        con.commit()

        assert _source_thread_count(con, "claudecode") == 0
        con.close()


async def test_do_ingest_triggers_export(tmp_path):
    from llm_archive import db
    from llm_archive.config import AppConfig
    from llm_archive.export import thread_md_path
    from llm_archive.ingestors.dummy import DummyIngestor

    db_path = tmp_path / "test.db"
    con = db.connect(db_path)

    config = AppConfig()
    config.export.dir = str(tmp_path / "exports")
    config.export.auto = True

    ingestor = DummyIngestor()
    ok = await _do_ingest(con, ingestor, since=None, force=True, config=config)
    assert ok is True

    md_path = thread_md_path("dummy", "dummy:e2e-canary", config)
    assert md_path.exists()
    content = md_path.read_text()
    assert "<!-- thread:dummy:e2e-canary source:dummy -->" in content
    assert "service e2e probe" in content
    assert "ack" in content