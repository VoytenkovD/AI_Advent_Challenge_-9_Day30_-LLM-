# -*- coding: utf-8 -*-
"""Клиент локальной Ollama. Только стандартная библиотека.

Приложение принципиально работает без облака: адрес Ollama обязан быть локальным
(localhost / 127.0.0.1 / ::1), иначе сервер не стартует.
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
# эти модели не умеют вести диалог: эмбеддинги и реранкеры из прошлых проектов
NOT_CHAT = ("embed", "bge-", "rerank", "nomic-", "mxbai-")


class OllamaError(RuntimeError):
    pass


def assert_local():
    host = urllib.parse.urlparse(OLLAMA_URL).hostname
    if host not in LOCAL_HOSTS:
        raise SystemExit("OLLAMA_URL={} — не локальный адрес. Приложение работает только с локальной LLM.".format(OLLAMA_URL))


def _req(path, body=None, timeout=15):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(OLLAMA_URL + path, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise OllamaError("Ollama {}: {}".format(e.code, e.read().decode("utf-8", "replace")[:300]))
    except urllib.error.URLError as e:
        raise OllamaError("Ollama недоступна ({}): {}".format(OLLAMA_URL, e.reason))


def status():
    out = {"url": OLLAMA_URL, "local": True, "ok": False}
    try:
        out["version"] = _req("/api/version", timeout=3)["version"]
        out["ok"] = True
        out["loaded"] = [{"name": m["name"], "size_mb": round(m.get("size", 0) / 2**20),
                          "vram_mb": round(m.get("size_vram", 0) / 2**20),
                          "gpu": round(100 * m.get("size_vram", 0) / max(1, m.get("size", 1))),
                          "ctx": m.get("context_length")}
                         for m in _req("/api/ps", timeout=3).get("models", [])]
    except OllamaError as e:
        out["error"] = str(e)
    return out


def models():
    """Модели для чата: всё из /api/tags, кроме эмбеддингов и реранкеров."""
    out = []
    for m in _req("/api/tags").get("models", []):
        if any(x in m["name"].lower() for x in NOT_CHAT):
            continue
        d = m.get("details", {})
        out.append({"name": m["name"], "size_gb": round(m.get("size", 0) / 2**30, 1), "params": d.get("parameter_size"),
                    "quant": d.get("quantization_level"), "family": d.get("family")})
    return sorted(out, key=lambda m: m["name"])


def chat_stream(model, messages, options):
    """Генератор событий стримингового /api/chat: ("token", текст) … ("done", статистика).
    Возвращает также функцию close(): закрытие соединения останавливает генерацию в Ollama."""
    body = json.dumps({"model": model, "messages": messages, "stream": True, "options": options}).encode("utf-8")
    req = urllib.request.Request(OLLAMA_URL + "/api/chat", data=body, headers={"Content-Type": "application/json"})
    t0 = time.time()  # до запроса: Ollama отвечает заголовками только после загрузки модели
    try:
        resp = urllib.request.urlopen(req, timeout=600)
    except urllib.error.HTTPError as e:
        raise OllamaError("Ollama {}: {}".format(e.code, e.read().decode("utf-8", "replace")[:300]))
    except urllib.error.URLError as e:
        raise OllamaError("Ollama недоступна ({}): {}".format(OLLAMA_URL, e.reason))

    def events():
        first = None
        try:
            for line in resp:
                ev = json.loads(line)
                if ev.get("error"):
                    raise OllamaError(ev["error"])
                piece = (ev.get("message") or {}).get("content", "")
                if piece:
                    if first is None:
                        first = time.time() - t0
                    yield "token", piece
                if ev.get("done"):
                    n, d = ev.get("eval_count", 0), ev.get("eval_duration", 0)
                    yield "done", {"prompt_tokens": ev.get("prompt_eval_count", 0), "answer_tokens": n,
                                   "tokens_per_s": round(n / (d / 1e9), 1) if d else 0,
                                   "ttft_s": round(first or 0, 2), "load_s": round(ev.get("load_duration", 0) / 1e9, 2),
                                   "total_s": round(time.time() - t0, 2), "done_reason": ev.get("done_reason")}
                    return
        finally:
            resp.close()

    return events(), resp.close


def complete(model, messages, options=None, timeout=60):
    """Короткий запрос без стриминга (например, название чата)."""
    res = _req("/api/chat", {"model": model, "messages": messages, "stream": False, "options": options or {}}, timeout)
    return (res.get("message") or {}).get("content", "").strip()
