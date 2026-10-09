# -*- coding: utf-8 -*-
"""Проверка приватного AI-сервиса: доступ по сети, чат, стабильность под нагрузкой, лимиты.

  python tools/check.py [--url http://192.168.1.10:5300] [--key lsk_…] [--clients 6] [--skip-brute]

Ключ по умолчанию берётся из data/first-key.txt. Для проверки лимитов скрипт создаёт временные ключи
(через app/keys.py, т.е. запускать его нужно на самом сервере) и удаляет их в конце.
Отчёт сохраняется в data/check-report.json.
"""
import json
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
import keys  # noqa: E402

ARGS = sys.argv[1:]
arg = lambda n, d=None: ARGS[ARGS.index(n) + 1] if n in ARGS else d
URL = arg("--url", "http://127.0.0.1:5300").rstrip("/")
KEY = arg("--key") or (ROOT / "data" / "first-key.txt").read_text(encoding="utf-8").strip()
CLIENTS = int(arg("--clients", "6"))
REPORT = {"url": URL, "started": time.time(), "checks": []}


def call(method, path, body=None, key=KEY, raw_body=None, timeout=300):
    """→ (status, json | None, headers, seconds)."""
    data = raw_body if raw_body is not None else (json.dumps(body).encode("utf-8") if body is not None else None)
    req = urllib.request.Request(URL + path, data=data, method=method)
    if key:
        req.add_header("Authorization", "Bearer " + key)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            txt = r.read().decode("utf-8")
            return r.status, (json.loads(txt) if r.headers.get_content_type() == "application/json" else txt), dict(r.headers), time.time() - t0
    except urllib.error.HTTPError as e:
        txt = e.read().decode("utf-8", "replace")
        try:
            j = json.loads(txt)
        except json.JSONDecodeError:
            j = {"raw": txt[:200]}
        return e.code, j, dict(e.headers), time.time() - t0


def check(name, ok, detail):
    REPORT["checks"].append({"name": name, "ok": bool(ok), "detail": detail})
    print("{} {:42} {}".format("✔" if ok else "✘", name, detail), flush=True)
    return ok


def chat(message, key=KEY, **kw):
    return call("POST", "/api/chat", dict({"message": message}, **kw), key)


def err(j):
    return (j or {}).get("error", {}).get("code") if isinstance(j, dict) else None


# ──────────────────────────────── проверки ────────────────────────────────

def t_network():
    s, j, _, dt = call("GET", "/health", key=None, timeout=10)
    check("доступ по сети: /health без ключа", s == 200 and j.get("ok"),
          "{} · {} · Ollama {} · модель в памяти: {} · {:.2f} с".format(URL, s, "ok" if j.get("ok") else "нет",
                                                                     j.get("model_loaded"), dt))
    REPORT["health"] = j


def t_auth():
    s1, j1, _, _ = call("GET", "/api/me", key=None)
    s2, j2, _, _ = call("GET", "/api/me", key="lsk_wrong-key")
    s3, j3, h3, _ = call("GET", "/api/me")
    check("без ключа и с чужим ключом → 401", s1 == 401 and s2 == 401, "{} {} / {} {}".format(s1, err(j1), s2, err(j2)))
    check("с ключом → 200 + лимиты в заголовках", s3 == 200 and "X-RateLimit-Limit" in h3,
          "ключ «{}», X-RateLimit-Limit={}, лимиты {}".format(j3.get("name"), h3.get("X-RateLimit-Limit"), j3.get("limits")))
    REPORT["limits"] = j3.get("limits")


def t_chat():
    s, j, _, dt = chat("Запомни кодовое слово: БИРЮЗА. Ответь одним словом: «запомнил».", max_tokens=20, temperature=0)
    ok = check("чат: ответ модели", s == 200 and j.get("reply"), "{} · «{}» · {:.1f} с · {}".format(
        s, (j.get("reply") or "")[:60], dt, j.get("usage")))
    if not ok:
        return
    cid = j["chat_id"]
    s, j2, _, dt = chat("Какое кодовое слово я просил запомнить? Ответь одним словом.", chat_id=cid, max_tokens=20, temperature=0)
    check("чат: история диалога на сервере", s == 200 and "бирюз" in (j2.get("reply") or "").lower(),
          "«{}» · {:.1f} с".format((j2.get("reply") or "")[:60], dt))
    s, j3, _, _ = call("GET", "/api/chats/" + cid)
    check("чат: сохранён у ключа (GET /api/chats/<id>)", s == 200 and len(j3["chat"]["messages"]) == 4,
          "{} сообщений".format(len(j3["chat"]["messages"])) if s == 200 else s)
    call("DELETE", "/api/chats/" + cid)


def t_stream():
    req = urllib.request.Request(URL + "/api/chat", data=json.dumps({"message": "Назови три цвета радуги.", "stream": True,
                                                                      "max_tokens": 40}).encode("utf-8"), method="POST")
    req.add_header("Authorization", "Bearer " + KEY)
    req.add_header("Content-Type", "application/json")
    t0, first, types = time.time(), None, []
    with urllib.request.urlopen(req, timeout=300) as r:
        for line in r:
            ev = json.loads(line)
            types.append(ev["type"])
            if ev["type"] == "token" and first is None:
                first = time.time() - t0
            if ev["type"] == "meta":
                cid = ev["chat_id"]
    check("стриминг NDJSON: meta → token… → done", types[0] == "meta" and types[-1] == "done" and "token" in types,
          "{} токен-событий, первый токен через {:.2f} с, всего {:.1f} с".format(types.count("token"), first or 0, time.time() - t0))
    call("DELETE", "/api/chats/" + cid)


def t_load():
    """Несколько клиентов одновременно, каждый со своим ключом: все должны получить ответ, GPU — по очереди."""
    names = ["load-{}".format(i) for i in range(CLIENTS)]
    ks = [keys.add(n, rpm=30, concurrency=2) for n in names]
    results, lock = [], threading.Lock()
    topics = ["чай", "кофе", "море", "горы", "снег", "лес", "город", "книга"]

    def client(i):
        for n in range(2):
            for attempt in range(4):   # вежливый клиент: на 503 ждёт Retry-After и повторяет
                s, j, h, dt = chat("Одно предложение про {}.".format(topics[(i + n) % len(topics)]), ks[i], max_tokens=60)
                with lock:
                    results.append({"client": i, "status": s, "code": err(j), "s": round(dt, 2), "attempt": attempt,
                                    "queue_s": (j.get("usage") or {}).get("queue_s") if s == 200 else None})
                if s != 503:
                    break
                time.sleep(float(h.get("Retry-After") or 5))
            if s == 200:
                call("DELETE", "/api/chats/" + j["chat_id"], key=ks[i])

    t0 = time.time()
    th = [threading.Thread(target=client, args=(i,)) for i in range(CLIENTS)]
    [t.start() for t in th]
    [t.join() for t in th]
    wall = time.time() - t0
    for n in names:
        keys.remove(n)
    ok = [r for r in results if r["status"] == 200]
    busy = [r for r in results if r["code"] == "queue_full"]
    other = [r for r in results if r["status"] != 200 and r["code"] != "queue_full"]
    lat = [r["s"] for r in ok]
    qs = [r["queue_s"] for r in ok if r["queue_s"] is not None]
    REPORT["load"] = {"clients": CLIENTS, "requests": len(results), "ok": len(ok), "wall_s": round(wall, 1), "results": results,
                      "latency_median_s": statistics.median(lat) if lat else None, "latency_max_s": max(lat) if lat else None,
                      "queue_max_s": max(qs) if qs else None}
    REPORT["load"].update(queue_full=len(busy), errors=len(other))
    check("стабильность: {} клиентов × 2 запроса параллельно".format(CLIENTS), len(ok) == CLIENTS * 2 and not other,
          "ответов {}/{}, отказов «очередь полна» (503, повтор после Retry-After) {}, других ошибок {}, медиана {:.1f} с, "
          "максимум {:.1f} с, макс. ожидание в очереди {:.1f} с, всё за {:.0f} с"
          .format(len(ok), CLIENTS * 2, len(busy), len(other), REPORT["load"]["latency_median_s"] or 0,
                  REPORT["load"]["latency_max_s"] or 0, REPORT["load"]["queue_max_s"] or 0, wall))


def t_rate_limit():
    k = keys.add("test-rpm", rpm=3, concurrency=1)
    try:
        out = []
        for i in range(5):
            s, j, h, _ = chat("Скажи «ок».", k, max_tokens=3)
            out.append((s, err(j), h.get("Retry-After"), h.get("X-RateLimit-Remaining")))
            if s == 200:
                call("DELETE", "/api/chats/" + j["chat_id"], key=k)
        codes = [o[0] for o in out]
        check("rate limit: 5 запросов при лимите 3/мин", codes == [200, 200, 200, 429, 429] and out[3][1] == "rate_limited"
              and out[3][2], "коды {}, Retry-After {} с, осталось {}".format(codes, out[3][2], [o[3] for o in out]))
    finally:
        keys.remove("test-rpm")

    k = keys.add("test-par", rpm=30, concurrency=1)
    try:
        res = []

        def one():
            s, j, _, _ = chat("Перечисли пять фруктов.", k, max_tokens=60)
            res.append((s, err(j)))
            if s == 200:
                call("DELETE", "/api/chats/" + j["chat_id"], key=k)
        th = [threading.Thread(target=one) for _ in range(2)]
        th[0].start(); time.sleep(0.3); th[1].start()
        [t.join() for t in th]
        check("параллельность: 2 запроса одного ключа при лимите 1", sorted(r[0] for r in res) == [200, 429]
              and any(r[1] == "too_many_parallel" for r in res), str(res))
    finally:
        keys.remove("test-par")


def t_context():
    lim = REPORT["limits"]
    s, j, _, _ = chat("я" * (lim["max_message_chars"] + 1))
    check("max context: сообщение длиннее лимита символов → 413", s == 413 and err(j) == "message_too_long", "{} {}".format(s, err(j)))
    s, j, _, _ = chat("слово " * (lim["max_message_chars"] // 6 - 5))
    check("max context: сообщение не влезает в окно → 413", s == 413 and err(j) == "context_too_large",
          "{} {}: {}".format(s, err(j), (j.get("error") or {}).get("message", "")[:110]))
    s, j, _, _ = chat("Привет", max_tokens=lim["max_output_tokens"] + 1)
    check("max tokens больше лимита → 400", s == 400 and err(j) == "max_tokens_out_of_range", "{} {}".format(s, err(j)))
    s, j, _, _ = call("POST", "/api/chat", raw_body=b"{" + b" " * 70000 + b"}")
    check("тело запроса больше лимита → 413", s == 413 and err(j) == "body_too_large", "{} {}".format(s, err(j)))
    s, j, _, _ = chat("Привет", model="gpt-4")
    check("неизвестная модель → 400", s == 400 and err(j) == "unknown_model", "{} {}".format(s, err(j)))

    # длинный диалог: старые сообщения отбрасываются, промпт остаётся в окне модели
    block = ("Это длинная заметка для проверки окна контекста. " * 40).strip()   # ~550 токенов
    cid, dropped, prompt = None, [], []
    for i in range(6):
        s, j, _, _ = chat("Заметка {}: {} Ответь одним словом: принято.".format(i + 1, block), chat_id=cid, max_tokens=8)
        if s != 200:
            check("max context: длинная история", False, "{} {}".format(s, err(j)))
            return
        cid = j["chat_id"]
        dropped.append(j["usage"]["history_dropped"])
        prompt.append(j["usage"]["prompt_tokens"])
    call("DELETE", "/api/chats/" + cid)
    check("max context: длинная история обрезается со старых", max(dropped) > 0 and max(prompt) <= lim["max_context_tokens"],
          "отброшено сообщений по шагам {}, промпт по данным Ollama {} токенов (окно {})".format(
              dropped, prompt, lim["max_context_tokens"]))
    REPORT["history"] = {"dropped": dropped, "prompt_tokens": prompt}


def t_brute():
    codes = []
    for i in range(22):
        s, j, _, _ = call("GET", "/api/me", key="lsk_guess{}".format(i))
        codes.append(s)
    # 2 неверных ключа уже были в t_auth: лимит считает все неудачи с IP за минуту
    first = codes.index(429) if 429 in codes else None
    s, _, _, _ = call("GET", "/api/me")
    check("перебор ключей: 22 неверных ключа с одного IP", first is not None and set(codes[first:]) == {429}
          and codes[:first] == [401] * first and s == 200,
          "401 × {}, затем 429 × {} (неверные ключи с IP блокируются на минуту); верный ключ с того же IP → {}".format(
              first, len(codes) - (first or 0), s))


def wait_unblocked():
    """Если прошлый прогон закончился проверкой перебора ключей, IP ещё заблокирован — ждём Retry-After."""
    s, j, h, _ = call("GET", "/api/me", key="lsk_probe")
    if s == 429 and err(j) == "auth_blocked":
        print("IP заблокирован после прошлой проверки перебора ключей — жду минуту, пока окно очистится", flush=True)
        time.sleep(61)


if __name__ == "__main__":
    wait_unblocked()
    t_network()
    t_auth()
    t_chat()
    t_stream()
    t_load()
    t_rate_limit()
    t_context()
    if "--skip-brute" not in ARGS:
        t_brute()
    REPORT["finished"] = time.time()
    passed = sum(c["ok"] for c in REPORT["checks"])
    print("\nИтого: {}/{} проверок пройдено".format(passed, len(REPORT["checks"])))
    out = ROOT / "data" / "check-report.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(REPORT, ensure_ascii=False, indent=1), encoding="utf-8")
    sys.exit(0 if passed == len(REPORT["checks"]) else 1)
