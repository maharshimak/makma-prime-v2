"""Versioned local persistence with backwards-compatible v2 memory functions."""
import hashlib
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .embeddings import embed, similarity, tokens

DB_PATH = Path(__file__).resolve().parents[1] / "data" / "makma.db"
SCHEMA_VERSION = 3
_migration_lock = threading.RLock()
CATEGORIES = {"working", "episodic", "semantic", "procedural", "project", "people", "mission", "note"}


def database_path() -> Path:
    return Path(os.getenv("MAKMA_DB_PATH", str(DB_PATH))).expanduser().resolve()


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def _conn():
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15, factory=ClosingConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def backup_database(label: str = "manual") -> Path:
    """SQLite backup includes committed WAL pages and produces a consistent copy."""
    path = database_path()
    folder = path.parent / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = folder / f"{path.stem}-{label}-{stamp}.db"
    with _conn() as source:
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
        finally:
            target.close()
    return destination


def init_db():
    with _migration_lock:
        with _conn() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            existing = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='memories'").fetchone()
        if version > SCHEMA_VERSION:
            raise RuntimeError("Database is newer than this Mak'ma build; refusing to downgrade.")
        if version == SCHEMA_VERSION:
            return
        if existing:
            backup_database("pre-v3")
        with _conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL DEFAULT 'note',
                    content TEXT NOT NULL, created_at DATETIME DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS missions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, objective TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active', priority TEXT NOT NULL DEFAULT 'normal',
                    progress INTEGER NOT NULL DEFAULT 0, created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS approvals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, tool_name TEXT NOT NULL, args_json TEXT NOT NULL,
                    risk_level INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending', result_json TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP, resolved_at DATETIME);
                CREATE TABLE IF NOT EXISTS mission_steps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, mission_id INTEGER NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
                    title TEXT NOT NULL, tool_name TEXT, args_json TEXT NOT NULL DEFAULT '{}',
                    depends_json TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'pending',
                    result_json TEXT, approval_id INTEGER REFERENCES approvals(id), idempotency_key TEXT UNIQUE,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL, summary TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, tool_name TEXT NOT NULL, args_json TEXT NOT NULL,
                    risk_level INTEGER NOT NULL, status TEXT NOT NULL, result_json TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, status TEXT DEFAULT 'pending',
                    priority TEXT DEFAULT 'normal', due_at TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, summary TEXT,
                    priority TEXT DEFAULT 'informational', status TEXT DEFAULT 'unread', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS schedules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, run_at TEXT,
                    interval_seconds INTEGER, status TEXT DEFAULT 'active', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL, role TEXT NOT NULL,
                    content TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS tool_runs (
                    id TEXT PRIMARY KEY, tool_name TEXT NOT NULL, status TEXT NOT NULL,
                    result_json TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS entities (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, type TEXT DEFAULT 'other',
                    metadata_json TEXT DEFAULT '{}', UNIQUE(name, type));
                CREATE TABLE IF NOT EXISTS relationships (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
                    relation TEXT NOT NULL, target_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
                    source TEXT DEFAULT 'user', confidence REAL DEFAULT 1.0, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(source_id, relation, target_id));
                CREATE INDEX IF NOT EXISTS idx_mission_status ON missions(status);
                CREATE INDEX IF NOT EXISTS idx_approval_status ON approvals(status);
                CREATE INDEX IF NOT EXISTS idx_steps_mission ON mission_steps(mission_id);
                CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, id);
                CREATE INDEX IF NOT EXISTS idx_schedule_status ON schedules(status, run_at);
            """)
            columns = {r[1] for r in conn.execute("PRAGMA table_info(memories)")}
            additions = {
                "category": "TEXT NOT NULL DEFAULT 'note'", "summary": "TEXT DEFAULT ''",
                "importance": "REAL NOT NULL DEFAULT 0.5", "confidence": "REAL NOT NULL DEFAULT 1.0",
                "source": "TEXT NOT NULL DEFAULT 'user'", "expires_at": "TEXT", "updated_at": "TEXT",
                "last_accessed": "TEXT", "metadata_json": "TEXT NOT NULL DEFAULT '{}'",
                "content_hash": "TEXT", "embedding_json": "TEXT", "embedding_model": "TEXT",
            }
            for column, definition in additions.items():
                if column not in columns:
                    conn.execute(f"ALTER TABLE memories ADD COLUMN {column} {definition}")
            conn.execute("UPDATE memories SET updated_at=created_at WHERE updated_at IS NULL")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_hash ON memories(content_hash, category)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_category ON memories(category)")
            for row in conn.execute("SELECT id, kind, content FROM memories WHERE content_hash IS NULL").fetchall():
                conn.execute("UPDATE memories SET content_hash=?, category=? WHERE id=?", (_digest(row["content"]), row["kind"] if row["kind"] in CATEGORIES else "note", row["id"]))
            try:
                conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(content, kind, content='memories', content_rowid='id')")
                conn.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
                conn.execute("""CREATE TRIGGER IF NOT EXISTS memories_fts_insert AFTER INSERT ON memories BEGIN
                    INSERT INTO memories_fts(rowid,content,kind) VALUES(new.id,new.content,new.kind); END""")
                conn.execute("""CREATE TRIGGER IF NOT EXISTS memories_fts_delete AFTER DELETE ON memories BEGIN
                    INSERT INTO memories_fts(memories_fts,rowid,content,kind) VALUES('delete',old.id,old.content,old.kind); END""")
                conn.execute("""CREATE TRIGGER IF NOT EXISTS memories_fts_update AFTER UPDATE OF content,kind ON memories BEGIN
                    INSERT INTO memories_fts(memories_fts,rowid,content,kind) VALUES('delete',old.id,old.content,old.kind);
                    INSERT INTO memories_fts(rowid,content,kind) VALUES(new.id,new.content,new.kind); END""")
            except sqlite3.OperationalError as error:
                if "no such module: fts5" not in str(error).lower():
                    raise
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


def _digest(content: str) -> str:
    return hashlib.sha256(" ".join(content.casefold().split()).encode("utf-8")).hexdigest()


def _item(row) -> dict:
    item = dict(row)
    item["metadata"] = json.loads(item.pop("metadata_json", "{}"))
    item.pop("embedding_json", None)
    item.pop("content_hash", None)
    return item


def remember(content: str, kind: str = "note", *, category: Optional[str] = None,
             importance: float = 0.5, confidence: float = 1.0, source: str = "user",
             expires_at: Optional[str] = None, metadata: Optional[dict] = None, summary: str = "") -> Dict:
    content = content.strip()
    if not content:
        return {"ok": False, "error": "Memory is empty."}
    category = category or (kind if kind in CATEGORIES else "note")
    if category not in CATEGORIES:
        return {"ok": False, "error": "Unknown memory category."}
    if not 0 <= importance <= 1 or not 0 <= confidence <= 1:
        return {"ok": False, "error": "Importance and confidence must be between 0 and 1."}
    if expires_at:
        try:
            dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            expires_at = dt.replace(tzinfo=dt.tzinfo or timezone.utc).astimezone(timezone.utc).isoformat()
        except ValueError:
            return {"ok": False, "error": "Expiry must be an ISO 8601 timestamp."}
    model, vector = embed(content)
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM memories WHERE content_hash=? AND category=? AND (expires_at IS NULL OR julianday(expires_at)>julianday('now')) ORDER BY id LIMIT 1", (_digest(content), category)).fetchone()
        if row:
            return {"ok": True, **_item(row), "duplicate": True}
        cur = conn.execute("""INSERT INTO memories(kind,content,category,summary,importance,confidence,source,
            expires_at,metadata_json,content_hash,embedding_json,embedding_model,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)""", (
            kind[:40], content, category, summary, importance, confidence, source, expires_at,
            json.dumps(metadata or {}, ensure_ascii=False), _digest(content), json.dumps(vector), model))
        row = conn.execute("SELECT * FROM memories WHERE id=?", (cur.lastrowid,)).fetchone()
        return {"ok": True, **_item(row), "duplicate": False}


def recall(query: str = "", limit: int = 12, *, category: Optional[str] = None,
           metadata: Optional[dict] = None, kind: Optional[str] = None) -> List[Dict]:
    query = query.strip()
    limit = max(1, min(int(limit), 100))
    model, query_vector = embed(query) if query else ("browse", [])
    query_tokens = set(tokens(query))
    filters = "(expires_at IS NULL OR julianday(expires_at)>julianday('now'))"
    args = []
    if category:
        filters += " AND category=?"
        args.append(category)
    if kind:
        filters += " AND kind=?"
        args.append(kind)
    with _conn() as conn:
        # Bound vector scanning; FTS still searches the entire archive.
        candidates = {row["id"]: dict(row) for row in conn.execute(f"SELECT * FROM memories WHERE {filters} ORDER BY id DESC LIMIT 2000", args)}
        fts_ids = set()
        if query_tokens:
            match = " OR ".join('"' + token + '"' for token in sorted(query_tokens))
            try:
                for row in conn.execute(f"SELECT m.* FROM memories m JOIN memories_fts f ON f.rowid=m.id WHERE memories_fts MATCH ? AND {filters} ORDER BY bm25(memories_fts) LIMIT 200", [match, *args]):
                    candidates[row["id"]] = dict(row)
                    fts_ids.add(row["id"])
            except sqlite3.OperationalError as error:
                if "no such table: memories_fts" not in str(error).lower():
                    raise
        scored = []
        for row in candidates.values():
            if metadata and any(json.loads(row["metadata_json"]).get(key) != value for key, value in metadata.items()):
                continue
            overlap = len(query_tokens & set(tokens(row["content"] + " " + row["kind"]))) / max(len(query_tokens), 1)
            vector_score = 0.0
            if query and row["embedding_model"] == model and row["embedding_json"]:
                vector_score = max(0.0, similarity(query_vector, json.loads(row["embedding_json"])))
            semantic = model.startswith("semantic-local:")
            # Hash collisions must not turn unrelated notes into apparent matches.
            if query and not overlap and row["id"] not in fts_ids and not (semantic and vector_score >= 0.35):
                continue
            try:
                created = datetime.fromisoformat(row["created_at"]).replace(tzinfo=timezone.utc)
                days = max(0, (datetime.now(timezone.utc) - created).total_seconds() / 86400)
            except (TypeError, ValueError):
                days = 365
            relevance = (0.6 * vector_score + 0.4 * overlap) if semantic else (0.2 * vector_score + 0.8 * overlap)
            score = 0.75 * relevance + 0.15 * row["importance"] + 0.10 / (1 + days / 30)
            row["score"] = round(score, 6)
            row["retrieval_method"] = ("semantic-local+fts" if semantic else "fts+lexical-hash") if query else "browse"
            scored.append(row)
        scored.sort(key=lambda row: (row["score"], row["id"]), reverse=True)
        selected = scored[:limit]
        conn.executemany("UPDATE memories SET last_accessed=CURRENT_TIMESTAMP WHERE id=?", [(row["id"],) for row in selected])
    return [_item(row) for row in selected]


def forget(memory_id: int) -> dict:
    with _conn() as conn:
        deleted = conn.execute("DELETE FROM memories WHERE id=?", (int(memory_id),)).rowcount
    return {"ok": bool(deleted), "id": int(memory_id)}


delete_memory = forget


def update_memory(memory_id: int, content: str, kind: Optional[str] = None) -> dict:
    content = content.strip()
    if not content:
        return {"ok": False, "error": "Memory is empty."}
    model, vector = embed(content)
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute("SELECT * FROM memories WHERE id=?", (int(memory_id),)).fetchone()
        if not current:
            return {"ok": False, "error": "Memory not found."}
        category = kind if kind in CATEGORIES else current["category"]
        duplicate = conn.execute("SELECT id FROM memories WHERE content_hash=? AND category=? AND id<>?", (_digest(content), category, int(memory_id))).fetchone()
        if duplicate:
            return {"ok": False, "error": "An identical memory already exists.", "duplicate_id": duplicate["id"]}
        conn.execute("""UPDATE memories SET content=?,kind=?,category=?,content_hash=?,embedding_json=?,
            embedding_model=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""", (content, (kind or current["kind"])[:40],
            category, _digest(content), json.dumps(vector), model, int(memory_id)))
        updated = conn.execute("SELECT * FROM memories WHERE id=?", (int(memory_id),)).fetchone()
    return {"ok": True, **_item(updated)}


def create_mission(title: str, objective: str, priority: str = "normal") -> Dict:
    title, objective = title.strip(), objective.strip()
    if not title or not objective:
        return {"ok": False, "error": "Mission title and objective are required."}
    priority = priority.lower().strip()
    if priority not in {"low", "normal", "high", "critical"}:
        priority = "normal"
    with _conn() as conn:
        cur = conn.execute("INSERT INTO missions(title,objective,priority) VALUES(?,?,?)", (title[:120], objective, priority))
        return {"ok": True, "id": cur.lastrowid, "title": title[:120], "objective": objective, "priority": priority, "status": "active", "progress": 0}


def list_missions(status: Optional[str] = None, limit: int = 25) -> List[Dict]:
    limit = max(1, min(int(limit), 1000))
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM missions WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit)).fetchall() if status else conn.execute("SELECT * FROM missions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]


def update_mission(mission_id: int, status: Optional[str] = None, progress: Optional[int] = None) -> Dict:
    fields, values = [], []
    if status is not None:
        status = status.lower().strip()
        if status not in {"draft", "active", "paused", "waiting", "blocked", "needs_approval", "completed", "failed", "cancelled"}:
            return {"ok": False, "error": "Invalid mission status."}
        fields.append("status=?")
        values.append(status)
    if progress is not None:
        progress = max(0, min(100, int(progress)))
        fields.append("progress=?")
        values.append(progress)
    if not fields:
        return {"ok": False, "error": "Nothing to update."}
    fields.append("updated_at=CURRENT_TIMESTAMP")
    values.append(int(mission_id))
    with _conn() as conn:
        cur = conn.execute(f"UPDATE missions SET {', '.join(fields)} WHERE id=?", values)
        if not cur.rowcount:
            return {"ok": False, "error": "Mission not found."}
    return {"ok": True, "mission_id": int(mission_id), "status": status, "progress": progress}
