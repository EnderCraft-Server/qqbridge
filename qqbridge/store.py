"""SQLite persistence: message log, member cache, audit trail."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id INTEGER,
  at REAL NOT NULL, group_id TEXT, user_id TEXT, sender TEXT,
  message_id TEXT, text TEXT, segments TEXT, is_self INTEGER DEFAULT 0, mentions_me INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS msg_group ON messages(group_id, at);
CREATE INDEX IF NOT EXISTS msg_text ON messages(text);

CREATE TABLE IF NOT EXISTS members(
  group_id TEXT, user_id TEXT, nickname TEXT, card TEXT, role TEXT, updated REAL,
  PRIMARY KEY(group_id, user_id));

CREATE TABLE IF NOT EXISTS groups(
  group_id TEXT PRIMARY KEY, name TEXT, member_count INTEGER, updated REAL);

CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL,
  actor TEXT, action TEXT, target TEXT, params TEXT, state TEXT, result TEXT);

CREATE TABLE IF NOT EXISTS dedupe(
  key TEXT PRIMARY KEY, at REAL NOT NULL, result TEXT);

CREATE TABLE IF NOT EXISTS state(
  key TEXT PRIMARY KEY, value TEXT NOT NULL, updated REAL);
"""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        with self.connect() as db:
            db.executescript(SCHEMA)

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    # ---------- messages ----------
    def add_message(self, rec: dict):
        with self._lock, self.connect() as db:
            db.execute(
                "INSERT INTO messages(event_id,at,group_id,user_id,sender,message_id,text,segments,is_self,mentions_me)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (rec["id"], rec["at"], rec["group_id"], rec["user_id"], rec["sender"],
                 rec["message_id"], rec["text"], json.dumps(rec["segments"], ensure_ascii=False),
                 1 if rec["is_self"] else 0, 1 if rec.get("mentions_me") else 0),
            )

    def recent(self, group_id: str, limit: int = 30) -> list:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM messages WHERE group_id=? ORDER BY at DESC LIMIT ?",
                (group_id, limit),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def recent_events(self, limit: int = 200) -> list:
        """最近的消息（跨群），按 event_id 升序，形状和 EventBus 里的记录一致。

        给启动回填用：进程重启后环形缓冲是空的，控制台会显示「暂无消息」，
        看起来像机器人没在收消息 —— 其实库里全都有，只是没灌回去。
        """
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM messages WHERE event_id IS NOT NULL"
                " ORDER BY event_id DESC LIMIT ?", (int(limit),)).fetchall()
        out = []
        for row in reversed(rows):
            d = dict(row)
            try:
                segments = json.loads(d.get("segments") or "[]")
            except ValueError:
                segments = []
            out.append({
                "id": d.get("event_id") or 0,
                "at": d.get("at") or 0.0,
                "platform": "qq",
                "message_type": "group",
                "group_id": str(d.get("group_id") or ""),
                "user_id": str(d.get("user_id") or ""),
                "sender": d.get("sender") or "",
                "role": "",
                "message_id": str(d.get("message_id") or ""),
                "text": d.get("text") or "",
                "segments": segments,
                "is_self": bool(d.get("is_self")),
                "mentions_me": bool(d.get("mentions_me")),
                "keyword_hit": None,
            })
        return out

    def search(self, query: str, group_id: str = "", limit: int = 20) -> list:
        sql = "SELECT * FROM messages WHERE text LIKE ?"
        args = [f"%{query}%"]
        if group_id:
            sql += " AND group_id=?"
            args.append(group_id)
        sql += " ORDER BY at DESC LIMIT ?"
        args.append(limit)
        with self.connect() as db:
            return [dict(r) for r in db.execute(sql, args).fetchall()]

    # ---------- members / groups ----------
    def cache_members(self, group_id: str, rows: list):
        with self._lock, self.connect() as db:
            db.executemany(
                "INSERT INTO members(group_id,user_id,nickname,card,role,updated) VALUES(?,?,?,?,?,?)"
                " ON CONFLICT(group_id,user_id) DO UPDATE SET nickname=excluded.nickname,"
                " card=excluded.card, role=excluded.role, updated=excluded.updated",
                [(group_id, str(r.get("user_id")), r.get("nickname") or "", r.get("card") or "",
                  r.get("role") or "", time.time()) for r in rows],
            )

    def members(self, group_id: str) -> list:
        with self.connect() as db:
            return [dict(r) for r in db.execute(
                "SELECT * FROM members WHERE group_id=? ORDER BY role, user_id", (group_id,)).fetchall()]

    def member(self, group_id: str, user_id: str):
        with self.connect() as db:
            row = db.execute("SELECT * FROM members WHERE group_id=? AND user_id=?",
                             (group_id, str(user_id))).fetchone()
        return dict(row) if row else None

    def cache_groups(self, rows: list):
        with self._lock, self.connect() as db:
            db.executemany(
                "INSERT INTO groups(group_id,name,member_count,updated) VALUES(?,?,?,?)"
                " ON CONFLICT(group_id) DO UPDATE SET name=excluded.name,"
                " member_count=excluded.member_count, updated=excluded.updated",
                [(str(r.get("group_id")), r.get("group_name") or "", r.get("member_count") or 0, time.time())
                 for r in rows],
            )

    def groups(self) -> list:
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM groups ORDER BY group_id").fetchall()]

    # ---------- audit + idempotency ----------
    def audit(self, actor: str, action: str, target: str, params: dict, state: str, result: str = ""):
        with self._lock, self.connect() as db:
            db.execute("INSERT INTO audit(at,actor,action,target,params,state,result) VALUES(?,?,?,?,?,?,?)",
                       (time.time(), actor, action, target, json.dumps(params, ensure_ascii=False), state, result))

    def audit_tail(self, limit: int = 30) -> list:
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]

    def seen(self, key: str):
        with self.connect() as db:
            row = db.execute("SELECT result FROM dedupe WHERE key=?", (key,)).fetchone()
        return json.loads(row["result"]) if row else None

    def remember(self, key: str, result):
        with self._lock, self.connect() as db:
            db.execute("INSERT OR REPLACE INTO dedupe(key,at,result) VALUES(?,?,?)",
                       (key, time.time(), json.dumps(result, ensure_ascii=False)))

    # ---------- durable scalars ----------
    def max_event_id(self) -> int:
        """Highest event id ever persisted. Used to seed the bus sequence on first run."""
        try:
            with self.connect() as db:
                row = db.execute("SELECT MAX(event_id) AS m FROM messages").fetchone()
            return int(row["m"] or 0)
        except (sqlite3.Error, TypeError, ValueError):
            return 0

    def get_state(self, key: str, default=None):
        """Read a persisted scalar. Returns default when absent or unreadable."""
        try:
            with self.connect() as db:
                row = db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
            if row is None:
                return default
            return json.loads(row["value"])
        except (sqlite3.Error, ValueError):
            return default

    def set_state(self, key: str, value):
        """Persist a scalar. Best-effort: a write failure must never break ingest."""
        try:
            with self._lock, self.connect() as db:
                db.execute("INSERT OR REPLACE INTO state(key,value,updated) VALUES(?,?,?)",
                           (key, json.dumps(value), time.time()))
        except sqlite3.Error:
            pass
