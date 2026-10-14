import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from makma import memory, store


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    path = tmp_path / "makma.db"
    monkeypatch.setenv("MAKMA_DB_PATH", str(path))
    monkeypatch.delenv("MAKMA_EMBEDDING_MODEL", raising=False)
    memory.init_db()
    return path


def test_v2_migration_preserves_and_backs_up(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    monkeypatch.setenv("MAKMA_DB_PATH", str(path))
    conn = sqlite3.connect(path)
    conn.executescript("""CREATE TABLE memories(id INTEGER PRIMARY KEY,kind TEXT,content TEXT,created_at TEXT);
        INSERT INTO memories VALUES(17,'note','legacy fact','2025-01-01 00:00:00');
        CREATE TABLE approvals(id INTEGER PRIMARY KEY,tool_name TEXT,args_json TEXT,risk_level INTEGER,
            status TEXT,result_json TEXT,created_at TEXT,resolved_at TEXT);
        INSERT INTO approvals VALUES(9,'type_text','{}',2,'pending',NULL,'2025-01-01',NULL);""")
    conn.close()
    memory.init_db()
    memory.init_db()
    backups = list((tmp_path / "backups").glob("*pre-v3*.db"))
    assert len(backups) == 1
    conn = sqlite3.connect(backups[0])
    assert conn.execute("SELECT content FROM memories WHERE id=17").fetchone()[0] == "legacy fact"
    assert "category" not in {r[1] for r in conn.execute("PRAGMA table_info(memories)")}
    conn.close()
    assert memory.recall("legacy")[0]["id"] == 17
    with memory._conn() as conn:
        assert conn.execute("SELECT status FROM approvals WHERE id=9").fetchone()[0] == "pending"
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_connections_close_and_transactions_rollback(isolated_db):
    with memory._conn() as conn:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")
    with pytest.raises(RuntimeError):
        with memory._conn() as conn:
            conn.execute("INSERT INTO tasks(title) VALUES('must roll back')")
            raise RuntimeError("interrupt")
    assert store.rows("tasks") == []


def test_memory_filter_expiry_rank_and_duplicate(isolated_db):
    item = memory.remember("Interview with Ada at Acme", category="people", metadata={"company": "Acme"}, importance=0.9)
    duplicate = memory.remember("  interview WITH ada at acme  ", category="people")
    assert duplicate["duplicate"] and duplicate["id"] == item["id"]
    memory.remember("Ada expired detail", expires_at="2020-01-01T00:00:00Z")
    memory.remember("Ada hobby", category="semantic")
    result = memory.recall("Ada", category="people", metadata={"company": "Acme"})
    assert [r["id"] for r in result] == [item["id"]]
    assert result[0]["retrieval_method"] == "fts+lexical-hash"
    assert all("expired" not in row["content"] for row in memory.recall("Ada"))
    assert memory.recall("zzzzunrelated") == []
    assert not memory.remember("bad", importance=1.1)["ok"]


def test_concurrent_duplicate_suppression(isolated_db):
    with ThreadPoolExecutor(max_workers=5) as pool:
        result = list(pool.map(lambda _: memory.remember("single persisted fact"), range(10)))
    assert len({row["id"] for row in result}) == 1


def test_update_and_delete_keep_fts_consistent(isolated_db):
    item = memory.remember("original vocabulary")
    assert memory.update_memory(item["id"], "replacement phrase", "project")["ok"]
    assert memory.recall("original") == []
    assert memory.recall("replacement")[0]["category"] == "project"
    assert memory.forget(item["id"])["ok"]
    assert memory.recall("replacement") == []


def test_missions_dependencies_foreign_keys_and_idempotency(isolated_db):
    mission = memory.create_mission("Ship", "Release local build", "high")
    with memory._conn() as conn:
        conn.execute("INSERT INTO mission_steps(mission_id,title,idempotency_key) VALUES(?,?,?)", (mission["id"], "Build", "build-once"))
    with pytest.raises(sqlite3.IntegrityError):
        with memory._conn() as conn:
            conn.execute("INSERT INTO mission_steps(mission_id,title,idempotency_key) VALUES(?,?,?)", (mission["id"], "Again", "build-once"))
    with pytest.raises(sqlite3.IntegrityError):
        with memory._conn() as conn:
            conn.execute("INSERT INTO mission_steps(mission_id,title) VALUES(9999,'orphan')")
    assert memory.update_mission(mission["id"], "completed", 100)["ok"]
    assert memory.list_missions("completed")[0]["progress"] == 100


def test_settings_messages_export_and_backup(isolated_db):
    assert store.patch_settings({"name": "Ada", "simulation": True})["simulation"] is True
    with pytest.raises(ValueError):
        store.patch_settings({"simulation": "false"})
    with pytest.raises(ValueError):
        store.rows("tasks; DROP TABLE tasks")
    store.add_message("a", "user", "hello")
    store.add_message("a", "assistant", "ready")
    store.add_message("b", "user", "private")
    assert [r["content"] for r in store.list_messages("a")] == ["hello", "ready"]
    assert store.clear_messages("a")["deleted"] == 2
    assert len(store.list_messages("b")) == 1
    store.emit("test", "event saved", {"key": 1})
    assert json.loads(store.rows("events")[0]["payload_json"])["key"] == 1
    exported = store.export_data()
    assert exported["schema_version"] == 3 and exported["tables"]["messages"][0]["content"] == "private"
    backup = Path(store.backup())
    assert backup.is_file()
    conn = sqlite3.connect(backup)
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 1
    conn.close()


def test_newer_database_is_rejected(isolated_db):
    with memory._conn() as conn:
        conn.execute("PRAGMA user_version=99")
    with pytest.raises(RuntimeError, match="newer"):
        memory.init_db()
