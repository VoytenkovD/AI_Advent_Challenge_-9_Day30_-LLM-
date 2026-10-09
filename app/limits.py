# -*- coding: utf-8 -*-
"""Ограничения сервиса: частота запросов, параллельность, очередь к видеокарте, размер контекста.

  rate limit     — не больше rpm запросов к модели за скользящие 60 с на ключ → 429 + Retry-After
  concurrency    — не больше N одновременных генераций на ключ → 429
  очередь к GPU  — видеокарта одна (gpu_slots генераций одновременно), остальные ждут в очереди;
                   очередь полна или ожидание дольше queue_timeout_s → 503 + Retry-After
  max context    — сообщение длиннее лимита → 413; длинная история обрезается со старых сообщений,
                   чтобы промпт + ответ поместились в окно модели (num_ctx)
  перебор ключей — больше auth_fail_per_min неверных ключей с одного IP за минуту → 429
"""
import collections
import math
import re
import threading
import time


class LimitError(Exception):
    def __init__(self, status, code, message, retry_after=None):
        super().__init__(message)
        self.status, self.code, self.message, self.retry_after = status, code, message, retry_after


# ─────────────────────────── оценка токенов ───────────────────────────
# Точный токенизатор Ollama по HTTP недоступен, поэтому оценка с запасом: у qwen2.5 русский текст ≈ 2,5–3,5 символа
# на токен, английский ≈ 4. Берём 2,5 символа для кириллицы и 3,5 для остального + служебные токены на сообщение.
CYR = re.compile(r"[А-Яа-яЁё]")


def estimate_tokens(text):
    text = text or ""
    cyr = len(CYR.findall(text))
    return math.ceil(cyr / 2.5 + (len(text) - cyr) / 3.5)


def message_tokens(m):
    return estimate_tokens(m["content"]) + 4


def fit_context(system, history, message, max_context, max_output):
    """Собирает сообщения для модели так, чтобы промпт + ответ поместились в окно.
    Возвращает (messages, dropped, prompt_tokens_estimate). Старые реплики истории отбрасываются первыми."""
    budget = max_context - max_output
    sys_msg = {"role": "system", "content": system}
    new = {"role": "user", "content": message}
    base = message_tokens(sys_msg) + message_tokens(new)
    if base > budget:
        raise LimitError(413, "context_too_large",
                         "Сообщение не помещается в контекст модели: ~{} токенов при лимите {} (окно {} − ответ до {})."
                         .format(base, budget, max_context, max_output))
    kept, used = [], base
    for m in reversed(history):
        t = message_tokens(m)
        if used + t > budget:
            break
        kept.append(m)
        used += t
    kept.reverse()
    if kept and kept[0]["role"] == "assistant":   # история начинается с вопроса пользователя
        used -= message_tokens(kept[0])
        kept = kept[1:]
    return [sys_msg] + kept + [new], len(history) - len(kept), used


# ─────────────────────────── ключи, очередь, перебор ───────────────────────────

class Limiter:
    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.calls = collections.defaultdict(collections.deque)    # ключ → время запросов за 60 с
        self.active = collections.Counter()                        # ключ → генераций сейчас
        self.auth_fail = collections.defaultdict(collections.deque)
        self.gpu = threading.BoundedSemaphore(cfg["gpu_slots"])
        self.waiting = 0
        self.running = 0
        self.served = 0
        self.rejected = collections.Counter()

    def _rpm(self, key):
        return key.get("rpm") or self.cfg["default_rpm"]

    def _conc(self, key):
        return key.get("concurrency") or self.cfg["default_concurrency"]

    def usage(self, key):
        now = time.time()
        with self.lock:
            q = self.calls[key["name"]]
            while q and q[0] <= now - 60:
                q.popleft()
            reset = round(q[0] + 60 - now, 1) if q else 0
            return {"rpm": self._rpm(key), "used": len(q), "remaining": max(0, self._rpm(key) - len(q)),
                    "reset_s": reset, "concurrency": self._conc(key), "active": self.active[key["name"]]}

    def reject(self, code):
        with self.lock:
            self.rejected[code] += 1

    def auth_failed(self, ip):
        now = time.time()
        with self.lock:
            q = self.auth_fail[ip]
            q.append(now)
            while q and q[0] <= now - 60:
                q.popleft()

    def check_ip(self, ip):
        now = time.time()
        with self.lock:
            q = self.auth_fail[ip]
            while q and q[0] <= now - 60:
                q.popleft()
            if len(q) >= self.cfg["auth_fail_per_min"]:
                self.rejected["auth_blocked"] += 1
                raise LimitError(429, "auth_blocked", "Слишком много неверных ключей с вашего адреса — подождите минуту.",
                                 round(q[0] + 60 - now) + 1)

    def acquire(self, key):
        """Пропускает запрос по частоте и параллельности ключа. Возвращает функцию release()."""
        name, now = key["name"], time.time()
        with self.lock:
            q = self.calls[name]
            while q and q[0] <= now - 60:
                q.popleft()
            if len(q) >= self._rpm(key):
                self.rejected["rate_limited"] += 1
                raise LimitError(429, "rate_limited", "Лимит {} запросов в минуту для ключа «{}» исчерпан.".format(
                    self._rpm(key), name), math.ceil(q[0] + 60 - now))
            if self.active[name] >= self._conc(key):
                self.rejected["too_many_parallel"] += 1
                raise LimitError(429, "too_many_parallel", "У ключа «{}» уже {} запроса в работе — дождитесь ответа.".format(
                    name, self.active[name]), 5)
            q.append(now)
            self.active[name] += 1
        done = [False]

        def release():
            with self.lock:
                if not done[0]:
                    done[0] = True
                    self.active[name] -= 1
        return release

    def gpu_slot(self):
        """Ждёт свободную видеокарту. Возвращает (release, waited_s)."""
        t0 = time.time()
        with self.lock:
            if self.waiting >= self.cfg["queue_size"]:
                self.rejected["queue_full"] += 1
                raise LimitError(503, "queue_full", "Сервер занят: в очереди уже {} запросов.".format(self.waiting), 15)
            self.waiting += 1
        try:
            ok = self.gpu.acquire(timeout=self.cfg["queue_timeout_s"])
        finally:
            with self.lock:
                self.waiting -= 1
        if not ok:
            self.reject("queue_timeout")
            raise LimitError(503, "queue_timeout", "Сервер занят дольше {} с — повторите позже.".format(
                self.cfg["queue_timeout_s"]), 30)
        with self.lock:
            self.running += 1
        done = [False]

        def release():
            if not done[0]:
                done[0] = True
                with self.lock:
                    self.running -= 1
                    self.served += 1
                self.gpu.release()
        return release, round(time.time() - t0, 2)

    def snapshot(self):
        with self.lock:
            return {"running": self.running, "waiting": self.waiting, "served": self.served,
                    "rejected": dict(self.rejected), "gpu_slots": self.cfg["gpu_slots"], "queue_size": self.cfg["queue_size"]}
