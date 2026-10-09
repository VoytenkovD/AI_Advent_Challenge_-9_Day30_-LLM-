# -*- coding: utf-8 -*-
"""API-ключи сервиса. В data/keys.json хранится только SHA-256 ключа, сам ключ показывается один раз при создании.

  python app/keys.py list
  python app/keys.py add phone [--rpm 10] [--concurrency 2] [--admin]
  python app/keys.py remove phone
"""
import hashlib
import hmac
import json
import secrets
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KEYS_FILE = ROOT / "data" / "keys.json"
FIRST_KEY_FILE = ROOT / "data" / "first-key.txt"
PREFIX = "lsk_"   # local service key
_lock = threading.Lock()


def _hash(key):
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def load():
    if not KEYS_FILE.is_file():
        return []
    return json.loads(KEYS_FILE.read_text(encoding="utf-8"))


def _save(items):
    KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
    KEYS_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")


def add(name, rpm=None, concurrency=None, admin=False):
    """Создаёт ключ и возвращает его (единственный раз, когда ключ виден целиком)."""
    with _lock:
        items = load()
        if any(k["name"] == name for k in items):
            raise ValueError("Ключ с именем «{}» уже есть".format(name))
        key = PREFIX + secrets.token_urlsafe(24)
        items.append({"name": name, "hash": _hash(key), "hint": key[:8] + "…", "rpm": rpm, "concurrency": concurrency,
                      "admin": bool(admin), "created": time.time()})
        _save(items)
        return key


def remove(name):
    with _lock:
        items = load()
        rest = [k for k in items if k["name"] != name]
        if len(rest) == len(items):
            raise ValueError("Ключа «{}» нет".format(name))
        _save(rest)


def find(key):
    """Ключ из заголовка → запись о ключе или None. Сравнение хэшей за постоянное время."""
    if not key or not key.startswith(PREFIX):
        return None
    h = _hash(key)
    for k in load():
        if hmac.compare_digest(k["hash"], h):
            return k
    return None


def ensure_first():
    """При первом запуске создаёт ключ администратора и кладёт его в data/first-key.txt."""
    if load():
        return None
    key = add("owner", admin=True)
    FIRST_KEY_FILE.write_text(key + "\n", encoding="utf-8")
    return key


if __name__ == "__main__":
    args = sys.argv[1:]
    opt = lambda n: int(args[args.index(n) + 1]) if n in args else None
    if not args or args[0] == "list":
        for k in load():
            print("{:12} {:14} rpm={} concurrency={}{}".format(k["name"], k["hint"], k["rpm"] or "по умолчанию",
                                                             k["concurrency"] or "по умолчанию", " admin" if k["admin"] else ""))
    elif args[0] == "add" and len(args) > 1:
        print(add(args[1], opt("--rpm"), opt("--concurrency"), "--admin" in args))
        print("Сохраните ключ: он показан один раз, на сервере хранится только его хэш.")
    elif args[0] == "remove" and len(args) > 1:
        remove(args[1])
        print("удалён")
    else:
        print(__doc__)
