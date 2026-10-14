"""Shared persistent settings, conversation records, timeline and export helpers."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .memory import SCHEMA_VERSION, _conn, backup_database

TABLES = frozenset({"memories", "missions", "mission_steps", "approvals", "events", "audit_log",
                   "settings", "tasks", "notifications", "schedules", "messages", "tool_runs",
                   "entities", "relationships"})


def _defaults() -> dict:
    return {"name": "", "autonomy": "assisted", "simulation": False, "speak": True,
            "reduced_motion": False, "provider": "openai", "model": os.getenv("MAKMA_MODEL", "gpt-6-astra"),
            "allowed_directories": [str(Path(__file__).resolve().parents[1] / "workspace")],
            "screen_permission": False, "onboarded": False}


def get_settings() -> dict:
    result = _defaults()
    with _conn() as conn:
        for row in conn.execute("SELECT key,value_json FROM settings"):
            result[row["key"]] = json.loads(row["value_json"])
    return result


def patch_settings(patch: dict) -> dict:
    defaults = _defaults()
    unknown = set(patch) - set(defaults)
    if unknown:
        raise ValueError("Unknown settings: " + ", ".join(sorted(unknown)))
    for key, value in patch.items():
        if type(value) is not type(defaults[key]):
            raise ValueError(f"Invalid value type for {key}")
    if "autonomy" in patch and patch["autonomy"] not in {"manual", "assisted", "autonomous", "lockdown"}:
        raise ValueError("Invalid autonomy mode")
    if "provider" in patch and patch["provider"] not in {"openai", "offline"}:
        raise ValueError("Unknown model provider")
    if "allowed_directories" in patch:
        paths = patch["allowed_directories"]
        if not all(isinstance(path, str) and path.strip() for path in paths):
            raise ValueError("Allowed directories must be nonempty path strings")
        patch = dict(patch, allowed_directories=[str(Path(path).expanduser().resolve()) for path in paths])
    with _conn() as conn:
        conn.executemany("INSERT INTO settings(key,value_json) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
                         [(key, json.dumps(value, ensure_ascii=False)) for key, value in patch.items()])
    return get_settings()


def emit(type: str, summary: str, payload: dict | None = None) -> dict:
    with _conn() as conn:
        cur = conn.execute("INSERT INTO events(type,summary,payload_json) VALUES(?,?,?)",
                           (type, summary, json.dumps(payload or {}, ensure_ascii=False, default=str)))
        row = conn.execute("SELECT * FROM events WHERE id=?", (cur.lastrowid,)).fetchone()
    return dict(row)


def rows(table: str, limit: int = 100) -> list[dict]:
    if table not in TABLES:
        raise ValueError("Unsupported table")
    order = "key" if table == "settings" else "created_at DESC, id DESC" if table == "tool_runs" else "id DESC"
    with _conn() as conn:
        records = conn.execute(f"SELECT * FROM {table} ORDER BY {order} LIMIT ?", (max(1, min(int(limit), 10000)),)).fetchall()
    return [dict(row) for row in records]


def backup() -> str:
    return str(backup_database())


def export_data() -> dict:
    with _conn() as conn:
        conn.execute("BEGIN")
        tables = {table: [dict(row) for row in conn.execute(f"SELECT * FROM {table}")] for table in sorted(TABLES)}
    return {"schema_version": SCHEMA_VERSION, "exported_at": datetime.now(timezone.utc).isoformat(), "tables": tables}


def add_message(conversation_id: str, role: str, content: str) -> dict:
    if role not in {"user", "assistant", "system", "tool"}:
        raise ValueError("Invalid conversation role")
    with _conn() as conn:
        cur = conn.execute("INSERT INTO messages(conversation_id,role,content) VALUES(?,?,?)", (conversation_id, role, content))
        row = conn.execute("SELECT * FROM messages WHERE id=?", (cur.lastrowid,)).fetchone()
    return dict(row)


def list_messages(conversation_id: str, limit: int = 30) -> list[dict]:
    with _conn() as conn:
        result = conn.execute("SELECT * FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT ?", (conversation_id, max(1, min(int(limit), 1000)))).fetchall()
    return [dict(row) for row in reversed(result)]


def clear_messages(conversation_id: str) -> dict:
    with _conn() as conn:
        count = conn.execute("DELETE FROM messages WHERE conversation_id=?", (conversation_id,)).rowcount
    return {"ok": True, "deleted": count}
