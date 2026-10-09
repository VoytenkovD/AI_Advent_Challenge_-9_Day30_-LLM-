# -*- coding: utf-8 -*-
"""Приватный AI-сервис: HTTP API + веб-чат поверх локальной Ollama. Только стандартная библиотека.

Ollama слушает только 127.0.0.1. В сеть смотрит этот шлюз: он проверяет API-ключ, лимиты и размер контекста,
ставит запросы в очередь к единственной видеокарте и хранит чаты каждого ключа отдельно.

  GET  /health                     без ключа: жив ли сервис, загружена ли модель, очередь
  GET  /api/me                     ключ, лимиты и сколько осталось в текущей минуте
  GET  /api/models                 модели, которые отдаёт сервис
  POST /api/chat                   {message, chat_id?, model?, temperature?, max_tokens?, stream?}
  GET  /api/chats · GET/DELETE /api/chats/<id>
  GET  /api/admin/stats            только для ключа администратора: очередь, отказы, журнал за час
"""
import ipaddress
import json
import mimetypes
import socket
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP = Path(__file__).resolve().parent
sys.path.insert(0, str(APP))

import keys      # noqa: E402
import limits    # noqa: E402
import ollama    # noqa: E402
import store     # noqa: E402

ROOT = APP.parent
WEB = ROOT / "web"
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
LIM = CFG["limits"]
GEN = CFG["generation"]
LIMITER = limits.Limiter(LIM)
STARTED = time.time()


class ApiError(Exception):
    def __init__(self, status, code, message, retry_after=None):
        super().__init__(message)
        self.status, self.code, self.message, self.retry_after = status, code, message, retry_after


def say(text):
    """Вывод в консоль. Без консоли (запуск фоновой службой) sys.stdout равен None — тогда молчим, а не падаем."""
    try:
        if sys.stdout:
            sys.stdout.write(text + "\n")
            sys.stdout.flush()
    except (OSError, ValueError):
        pass


def lan_addresses():
    out = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith(("127.", "169.254.")):
                out.add(ip)
    except OSError:
        pass
    return sorted(out, key=lambda ip: (not ipaddress.ip_address(ip).is_private, ip))


class Handler(BaseHTTPRequestHandler):
    server_version = "PrivateLLM/1.0"
    timeout = 30   # медленный или зависший клиент не держит поток дольше 30 с при чтении запроса

    def log_message(self, fmt, *args):   # без текста запросов в консоли — только метод, путь и код
        say("{} {} {}".format(time.strftime("%H:%M:%S"), self.client_address[0], fmt % args))

    # ───────────── ответы ─────────────
    def _send(self, status, body, ctype="application/json; charset=utf-8", headers=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (headers or {}).items():
            self.send_header(k, str(v))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status, obj, headers=None):
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), headers=headers)

    def _error(self, e, key=None):
        headers = {"Retry-After": e.retry_after} if e.retry_after else {}
        if key:
            headers.update(self._rate_headers(key))
        self._json(e.status, {"error": {"code": e.code, "message": e.message, "retry_after": e.retry_after}}, headers)

    def _rate_headers(self, key):
        u = LIMITER.usage(key)
        return {"X-RateLimit-Limit": u["rpm"], "X-RateLimit-Remaining": u["remaining"], "X-RateLimit-Reset": u["reset_s"]}

    # ───────────── разбор запроса ─────────────
    @property
    def ip(self):
        return self.client_address[0]

    def _auth(self):
        h = self.headers.get("Authorization", "")
        token = h[7:].strip() if h.lower().startswith("bearer ") else (self.headers.get("X-API-Key") or "").strip()
        k = keys.find(token)
        if k:   # верный ключ проходит и с заблокированного IP: за одним NAT перебор не должен отключать всех
            return k
        LIMITER.check_ip(self.ip)   # 429, если с этого IP за минуту уже много неверных ключей
        LIMITER.auth_failed(self.ip)
        raise ApiError(401, "unauthorized", "Нужен API-ключ: заголовок Authorization: Bearer <ключ>.")

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > LIM["max_body_bytes"]:
            raise ApiError(413, "body_too_large", "Тело запроса больше {} байт.".format(LIM["max_body_bytes"]))
        try:
            data = json.loads(self.rfile.read(n).decode("utf-8") or "{}") if n else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ApiError(400, "bad_json", "Тело запроса — не JSON.")
        if not isinstance(data, dict):
            raise ApiError(400, "bad_json", "Ожидается JSON-объект.")
        return data

    def _route(self, method):
        path = urllib.parse.urlparse(self.path).path
        key, t0 = None, time.time()
        try:
            if path == "/health" and method == "GET":
                return self._json(200, health())
            if path.startswith("/api/"):
                key = self._auth()
                return self._api(method, path, key, t0)
            if method != "GET":
                raise ApiError(405, "method_not_allowed", "Метод не поддерживается.")
            return self._static(path)
        except (ApiError, limits.LimitError) as e:
            if path.startswith("/api/") and (key or e.status != 401):   # статика и чужие 401 журнал не засоряют
                store.log_request(key["name"] if key else None, self.ip, path, e.status, e.code,
                                  round((time.time() - t0) * 1000))
            return self._error(e, key)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as e:   # noqa: BLE001 — сервис не должен падать из-за одного запроса
            store.log_request(key["name"] if key else None, self.ip, path, 500, "internal")
            return self._json(500, {"error": {"code": "internal", "message": "{}: {}".format(type(e).__name__, e)}})

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_DELETE(self):
        self._route("DELETE")

    # ───────────── API ─────────────
    def _api(self, method, path, key, t0):
        rh = self._rate_headers(key)
        if path == "/api/me" and method == "GET":
            return self._json(200, {"name": key["name"], "admin": key["admin"], "usage": LIMITER.usage(key),
                                    "limits": public_limits()}, rh)
        if path == "/api/models" and method == "GET":
            return self._json(200, {"default": CFG["default_model"], "models": CFG["models"]}, rh)
        if path == "/api/chats" and method == "GET":
            return self._json(200, {"chats": store.list_chats(key["name"])}, rh)
        if path.startswith("/api/chats/"):
            cid = path[len("/api/chats/"):]
            if method == "GET":
                c = store.get_chat(key["name"], cid)
                if not c:
                    raise ApiError(404, "chat_not_found", "Чат не найден.")
                return self._json(200, {"chat": c}, rh)
            if method == "DELETE":
                if not store.delete_chat(key["name"], cid):
                    raise ApiError(404, "chat_not_found", "Чат не найден.")
                return self._json(200, {"deleted": cid}, rh)
        if path == "/api/chat" and method == "POST":
            return self._chat(key, t0)
        if path == "/api/admin/stats" and method == "GET":
            if not key["admin"]:
                raise ApiError(403, "forbidden", "Только для ключа администратора.")
            return self._json(200, admin_stats(), rh)
        raise ApiError(404, "not_found", "Нет такого метода API.")

    def _chat(self, key, t0):
        data = self._body()
        message = (data.get("message") or "").strip() if isinstance(data.get("message"), str) else ""
        if not message:
            raise ApiError(400, "empty_message", "Пустое сообщение (поле message).")
        if len(message) > LIM["max_message_chars"]:
            raise ApiError(413, "message_too_long", "Сообщение длиннее {} символов ({}).".format(
                LIM["max_message_chars"], len(message)))
        model = data.get("model") or CFG["default_model"]
        if model not in CFG["models"]:
            raise ApiError(400, "unknown_model", "Модель {} не обслуживается. Доступны: {}.".format(
                model, ", ".join(CFG["models"])))
        try:
            max_tokens = int(data.get("max_tokens") or GEN["max_tokens"])
            temperature = float(data.get("temperature") if data.get("temperature") is not None else GEN["temperature"])
        except (TypeError, ValueError):
            raise ApiError(400, "bad_params", "max_tokens и temperature должны быть числами.")
        if not 1 <= max_tokens <= LIM["max_output_tokens"]:
            raise ApiError(400, "max_tokens_out_of_range", "max_tokens должен быть от 1 до {}.".format(LIM["max_output_tokens"]))
        if not 0 <= temperature <= 1.5:
            raise ApiError(400, "temperature_out_of_range", "temperature должна быть от 0 до 1.5.")
        stream = bool(data.get("stream"))

        cid = data.get("chat_id")
        if cid:
            chat = store.get_chat(key["name"], cid)
            if not chat:
                raise ApiError(404, "chat_not_found", "Чат не найден.")
            history = [{"role": m["role"], "content": m["content"]} for m in chat["messages"]]
        else:
            history = []
        messages, dropped, est = limits.fit_context(CFG["system_prompt"], history, message,
                                                    LIM["max_context_tokens"], max_tokens)

        release_key = LIMITER.acquire(key)      # 429: частота или параллельность ключа
        try:
            release_gpu, waited = LIMITER.gpu_slot()   # 503: очередь полна или ждали слишком долго
            try:
                if not cid:
                    cid = store.create_chat(key["name"], message.splitlines()[0][:60], model)
                store.add_message(cid, "user", message)
                meta = {"chat_id": cid, "model": model, "queue_s": waited, "history_dropped": dropped,
                        "prompt_tokens_est": est, "max_tokens": max_tokens}
                options = {"num_ctx": GEN["num_ctx"], "num_predict": max_tokens, "temperature": temperature}
                try:
                    events, close = ollama.chat_stream(model, messages, options)
                except ollama.OllamaError as e:
                    raise ApiError(502, "model_unavailable", str(e))
                if stream:
                    self._stream(key, cid, meta, events, close, t0)
                else:
                    text, stats = "", {}
                    try:
                        for kind, val in events:
                            if kind == "token":
                                text += val
                            else:
                                stats = val
                    except ollama.OllamaError as e:
                        raise ApiError(502, "model_error", str(e))
                    self._finish(key, cid, text, stats, meta, t0)
                    self._json(200, {"chat_id": cid, "reply": text, "model": model,
                                     "usage": usage_of(stats, meta, t0)}, self._rate_headers(key))
            finally:
                release_gpu()
        finally:
            release_key()

    def _finish(self, key, cid, text, stats, meta, t0, stopped=False):
        store.add_message(cid, "assistant", text, json.dumps(dict(stats, stopped=stopped), ensure_ascii=False))
        store.log_request(key["name"], self.ip, "/api/chat", 200, "stopped" if stopped else None,
                          round((time.time() - t0) * 1000), meta["queue_s"], stats.get("prompt_tokens"),
                          stats.get("answer_tokens"))

    def _stream(self, key, cid, meta, events, close, t0):
        """NDJSON: {"type":"meta"} → {"type":"token","text"}… → {"type":"done","usage"}. Обрыв клиента останавливает модель."""
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        for k, v in self._rate_headers(key).items():
            self.send_header(k, str(v))
        self.end_headers()
        self.close_connection = True

        def put(obj):
            self.wfile.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
            self.wfile.flush()

        text, stats = "", {}
        try:
            put(dict(meta, type="meta"))
            for kind, val in events:
                if kind == "token":
                    text += val
                    put({"type": "token", "text": val})
                else:
                    stats = val
            put({"type": "done", "usage": usage_of(stats, meta, t0)})
            self._finish(key, cid, text, stats, meta, t0)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            close()   # клиент ушёл — освобождаем видеокарту сразу
            self._finish(key, cid, text, stats, meta, t0, stopped=True)
        except ollama.OllamaError as e:
            try:
                put({"type": "error", "error": {"code": "model_error", "message": str(e)}})
            except OSError:
                pass

    # ───────────── статика ─────────────
    def _static(self, path):
        f = (WEB / ("index.html" if path in ("/", "") else path.lstrip("/"))).resolve()
        if not str(f).startswith(str(WEB.resolve())) or not f.is_file():
            raise ApiError(404, "not_found", "Нет такой страницы.")
        ctype = mimetypes.guess_type(str(f))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        self._send(200, f.read_bytes(), ctype, {"Cache-Control": "no-cache"})


def usage_of(stats, meta, t0):
    return {"prompt_tokens": stats.get("prompt_tokens"), "answer_tokens": stats.get("answer_tokens"),
            "tokens_per_s": stats.get("tokens_per_s"), "queue_s": meta["queue_s"],
            "history_dropped": meta["history_dropped"], "done_reason": stats.get("done_reason"),
            "total_s": round(time.time() - t0, 2)}


def public_limits():
    return {k: LIM[k] for k in ("max_context_tokens", "max_output_tokens", "max_message_chars", "queue_size",
                                "queue_timeout_s", "gpu_slots")}


def health():
    st = ollama.status()
    q = LIMITER.snapshot()
    loaded = [m["name"] for m in st.get("loaded", [])]
    return {"ok": st["ok"], "service": "private-llm", "model": CFG["default_model"],
            "model_loaded": CFG["default_model"] in loaded, "queue": {"running": q["running"], "waiting": q["waiting"]},
            "uptime_s": round(time.time() - STARTED)}


def admin_stats():
    return {"queue": LIMITER.snapshot(), "ollama": ollama.status(), "log": store.stats(),
            "keys": [{"name": k["name"], "hint": k["hint"], "admin": k["admin"],
                      "usage": LIMITER.usage(k)} for k in keys.load()],
            "limits": dict(public_limits(), default_rpm=LIM["default_rpm"], default_concurrency=LIM["default_concurrency"]),
            "uptime_s": round(time.time() - STARTED)}


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32


def main():
    ollama.OLLAMA_URL = CFG["ollama_url"].rstrip("/")
    ollama.assert_local()
    first = keys.ensure_first()
    host, port = CFG["host"], CFG["port"]
    try:
        srv = Server((host, port), Handler)
    except OSError as e:
        say("Не удалось занять порт {}: {}. Возможно, сервис уже запущен.".format(port, e))
        sys.exit(1)
    st = ollama.status()
    say("Приватный AI-сервис · модель {} · Ollama {} ({})".format(
        CFG["default_model"], st.get("version", "недоступна"), CFG["ollama_url"]))
    say("  на этом компьютере:  http://127.0.0.1:{}/".format(port))
    if host in ("0.0.0.0", ""):
        for ip in lan_addresses():
            say("  в сети:              http://{}:{}/".format(ip, port))
    if first:
        say("\nСоздан ключ администратора (сохранён в data/first-key.txt):\n  {}\n".format(first))
    say("Лимиты: {rpm} запросов/мин и {c} параллельных на ключ, контекст {ctx} токенов, ответ до {out}, очередь {q}."
          .format(rpm=LIM["default_rpm"], c=LIM["default_concurrency"], ctx=LIM["max_context_tokens"],
                  out=LIM["max_output_tokens"], q=LIM["queue_size"]))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
