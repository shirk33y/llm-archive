from __future__ import annotations
import sqlite3

from llm_archive.sync import _source_thread_count


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