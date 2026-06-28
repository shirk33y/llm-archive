from __future__ import annotations

import pytest

from llm_archive import db
from llm_archive.config import AppConfig, IngestorConfig
from llm_archive.jobs import run_sync_job
from llm_archive.schema import IngestedMessage, IngestedThread


def _thread(content: str) -> IngestedThread:
    return IngestedThread(
        id="chatgpt:t1",
        source_id="chatgpt",
        title="thread",
        created_at=1,
        updated_at=2,
        messages=[
            IngestedMessage(
                id="chatgpt:m1",
                thread_id="chatgpt:t1",
                role="user",
                content=content,
                created_at=1,
            )
        ],
    )


def _message_contents(con) -> list[str]:
    rows = con.execute(
        "SELECT content FROM messages WHERE thread_id='chatgpt:t1' ORDER BY id"
    ).fetchall()
    return [row["content"] for row in rows]


def test_save_thread_rolls_back_when_database_is_full(tmp_path):
    con = db.connect(tmp_path / "archive.db")
    db.save_thread(con, _thread("old content"))
    before = _message_contents(con)

    page_count = con.execute("PRAGMA page_count").fetchone()[0]
    con.execute(f"PRAGMA max_page_count={page_count}")

    with pytest.raises(db.ArchiveStorageFullError) as exc_info:
        db.save_thread(con, _thread("new content " * 200_000), force=True)

    assert "database or disk is full" in str(exc_info.value)

    con.execute("PRAGMA max_page_count=2147483646")
    db.set_provider_sync_failure(con, "chatgpt", "database or disk is full")

    assert _message_contents(con) == before
    assert _message_contents(con) == ["old content"]


def test_save_thread_rolls_back_on_generic_sqlite_write_failure(tmp_path):
    db_path = tmp_path / "archive.db"
    con = db.connect(db_path)
    db.save_thread(con, _thread("old content"))
    con.close()

    ro_con = db.sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    ro_con.row_factory = db.sqlite3.Row
    with pytest.raises(db.ArchiveDatabaseWriteError) as exc_info:
        db.save_thread(ro_con, _thread("new content"), force=True)
    ro_con.close()

    con = db.connect(db_path)
    assert "database write failed" in str(exc_info.value)
    assert _message_contents(con) == ["old content"]


@pytest.mark.asyncio
async def test_sync_job_records_storage_full_error(tmp_path):
    config = AppConfig(
        ingestors={
            "chatgpt": IngestorConfig(
                mode="cookies",
                enabled=True,
                sync_interval_ms=60_000,
                min_sync_interval_ms=0,
            )
        }
    )

    async def runner(source_id: str, force: bool) -> bool:
        raise db.ArchiveStorageFullError(db.storage_full_message())

    result = await run_sync_job(
        "chatgpt",
        config=config,
        runner=runner,
        db_path=tmp_path / "archive.db",
    )

    con = db.connect(tmp_path / "archive.db")
    state = db.provider_states(con)["chatgpt"]
    job = db.recent_jobs(con, 1)[0]

    assert result.status == "failed"
    assert "database or disk is full" in result.reason
    assert "database or disk is full" in state["last_error"]
    assert state["last_success_at"] is None
    assert job["reason"] == "storage_full"
    assert "database or disk is full" in job["error"]


@pytest.mark.asyncio
async def test_sync_job_records_generic_database_write_error(tmp_path):
    config = AppConfig(
        ingestors={
            "chatgpt": IngestorConfig(
                mode="cookies",
                enabled=True,
                sync_interval_ms=60_000,
                min_sync_interval_ms=0,
            )
        }
    )

    async def runner(source_id: str, force: bool) -> bool:
        raise db.ArchiveDatabaseWriteError("database write failed; transaction was rolled back")

    result = await run_sync_job(
        "chatgpt",
        config=config,
        runner=runner,
        db_path=tmp_path / "archive.db",
    )

    con = db.connect(tmp_path / "archive.db")
    state = db.provider_states(con)["chatgpt"]
    job = db.recent_jobs(con, 1)[0]

    assert result.status == "failed"
    assert "database write failed" in result.reason
    assert "database write failed" in state["last_error"]
    assert state["last_success_at"] is None
    assert job["reason"] == "database_write_failed"
    assert "database write failed" in job["error"]
