# -*- coding: utf-8 -*-
"""SQLite: чаты (у каждого ключа свои) и журнал запросов (без текста сообщений)."""
import sqlite3
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "service.db"
_lock = threading.Lock()
_conn = None


def _db():
    global _conn
    if _conn is None:
        DB.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(DB, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS chats (id TEXT PRIMARY KEY, owner TEXT, title TEXT, model TEXT,
                                              created REAL, updated REAL);
            CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT, role TEXT,
                                                 content TEXT, ts REAL, meta TEXT);
            CREATE INDEX IF NOT EXISTS messages_chat ON messages(chat_id, id);
            CREATE TABLE IF NOT EXISTS requests (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, owner TEXT, ip TEXT,
                                                 path TEXT, status INTEGER, code TEXT, latency_ms INTEGER,
                                                 queue_s REAL, prompt_tokens INTEGER, answer_tokens INTEGER);
        """)
    return _conn


def create_chat(owner, title, model):
    cid, now = uuid.uuid4().hex[:12], time.time()
    with _lock:
        _db().execute("INSERT INTO chats VALUES (?,?,?,?,?,?)", (cid, owner, title[:80], model, now, now))
        _db().commit()
    return cid


def get_chat(owner, cid):
    with _lock:
        c = _db().execute("SELECT * FROM chats WHERE id=? AND owner=?", (cid, owner)).fetchone()
        if not c:
            return None
        msgs = _db().execute("SELECT role, content, ts, meta FROM messages WHERE chat_id=? ORDER BY id", (cid,)).fetchall()
    return dict(c, messages=[dict(m) for m in msgs])


def list_chats(owner):
    with _lock:
        rows = _db().execute("SELECT c.id, c.title, c.model, c.updated, COUNT(m.id) AS n FROM chats c "
                             "LEFT JOIN messages m ON m.chat_id=c.id WHERE c.owner=? GROUP BY c.id "
                             "ORDER BY c.updated DESC LIMIT 100", (owner,)).fetchall()
    return [dict(r) for r in rows]


def delete_chat(owner, cid):
    with _lock:
        n = _db().execute("DELETE FROM chats WHERE id=? AND owner=?", (cid, owner)).rowcount
        if n:
            _db().execute("DELETE FROM messages WHERE chat_id=?", (cid,))
        _db().commit()
    return bool(n)


def add_message(cid, role, content, meta=None):
    now = time.time()
    with _lock:
        _db().execute("INSERT INTO messages (chat_id, role, content, ts, meta) VALUES (?,?,?,?,?)",
                      (cid, role, content, now, meta))
        _db().execute("UPDATE chats SET updated=? WHERE id=?", (now, cid))
        _db().commit()


def log_request(owner, ip, path, status, code=None, latency_ms=0, queue_s=None, prompt_tokens=None, answer_tokens=None):
    with _lock:
        _db().execute("INSERT INTO requests (ts, owner, ip, path, status, code, latency_ms, queue_s, prompt_tokens, "
                      "answer_tokens) VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (time.time(), owner, ip, path, status, code, latency_ms, queue_s, prompt_tokens, answer_tokens))
        _db().commit()


def stats(since_s=3600):
    """Сводка журнала за последний час: по статусам, клиентам и времени ответа."""
    t = time.time() - since_s
    with _lock:
        by_status = [dict(r) for r in _db().execute(
            "SELECT status, code, COUNT(*) AS n FROM requests WHERE ts>? GROUP BY status, code ORDER BY n DESC", (t,))]
        by_owner = [dict(r) for r in _db().execute(
            "SELECT owner, ip, COUNT(*) AS n, SUM(status=200) AS ok, ROUND(AVG(CASE WHEN status=200 THEN latency_ms END)) "
            "AS avg_ms FROM requests WHERE ts>? GROUP BY owner, ip ORDER BY n DESC", (t,))]
        recent = [dict(r) for r in _db().execute(
            "SELECT ts, owner, ip, path, status, code, latency_ms, queue_s, prompt_tokens, answer_tokens FROM requests "
            "ORDER BY id DESC LIMIT 30")]
    return {"since_s": since_s, "by_status": by_status, "by_client": by_owner, "recent": recent}
