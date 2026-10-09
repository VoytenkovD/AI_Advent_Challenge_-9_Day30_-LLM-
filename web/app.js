// Веб-чат приватного AI-сервиса. Ключ хранится в localStorage этого браузера и уходит только на этот сервер.
(function () {
  'use strict';
  var $ = function (id) { return document.getElementById(id); };
  var S = { key: null, me: null, chats: [], chatId: null, abort: null, adminTimer: null };

  function lsGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function lsSet(k, v) { try { if (v == null) localStorage.removeItem(k); else localStorage.setItem(k, v); } catch (e) {} }
  function h(tag, attrs, kids) {
    var n = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) { if (k === 'text') n.textContent = attrs[k]; else if (k === 'class') n.className = attrs[k]; else n.setAttribute(k, attrs[k]); });
    [].concat(kids || []).forEach(function (c) { if (c != null) n.appendChild(typeof c === 'string' ? document.createTextNode(c) : c); });
    return n;
  }
  function clear(n) { while (n.firstChild) n.removeChild(n.firstChild); return n; }
  function fmtTime(ts) { return new Date(ts * 1000).toLocaleTimeString('ru-RU'); }

  function api(path, opts) {
    opts = opts || {};
    var init = { method: opts.method || (opts.body ? 'POST' : 'GET'), headers: { 'Authorization': 'Bearer ' + S.key } };
    if (opts.body) { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(opts.body); }
    if (opts.signal) init.signal = opts.signal;
    return fetch(path, init).then(function (r) {
      updateLimits(r);
      if (opts.raw) return r;
      return r.json().then(function (j) {
        if (!r.ok) { var e = new Error((j.error && j.error.message) || r.statusText); e.status = r.status; e.code = j.error && j.error.code; throw e; }
        return j;
      });
    });
  }

  function updateLimits(r) {
    var lim = r.headers.get('X-RateLimit-Limit'), rem = r.headers.get('X-RateLimit-Remaining');
    if (lim) $('limits').textContent = 'запросов: ' + rem + '/' + lim + ' в мин';
  }

  // ───────── вход ─────────
  function showLogin(msg) {
    $('app').hidden = true; $('login').hidden = false; $('login-error').textContent = msg || ''; $('key-input').focus();
  }
  $('login-form').addEventListener('submit', function (e) {
    e.preventDefault();
    S.key = $('key-input').value.trim();
    start().then(function () { lsSet('key', S.key); }).catch(function (err) { showLogin(err.message); });
  });
  $('logout').addEventListener('click', function () { lsSet('key', null); S.key = null; showLogin(); });

  function start() {
    return api('/api/me').then(function (me) {
      S.me = me;
      $('login').hidden = true; $('app').hidden = false;
      $('me').textContent = 'ключ: ' + me.name + (me.admin ? ' (администратор)' : '') + ' · контекст ' + me.limits.max_context_tokens + ' ток.';
      $('admin-btn').hidden = !me.admin;
      $('maxtok').max = me.limits.max_output_tokens;
      return Promise.all([loadModels(), loadChats()]);
    }).then(function () { health(); });
  }

  function loadModels() {
    return api('/api/models').then(function (r) {
      var sel = clear($('model'));
      r.models.forEach(function (m) { sel.appendChild(h('option', { value: m, text: m })); });
      sel.value = lsGet('model') || r.default;
    });
  }
  $('model').addEventListener('change', function () { lsSet('model', $('model').value); });

  function health() {
    fetch('/health').then(function (r) { return r.json(); }).then(function (s) {
      var d = $('health'); d.className = 'dot ' + (s.ok ? 'ok' : 'bad');
      d.title = (s.ok ? 'Ollama работает' : 'Ollama недоступна') + ' · модель ' + s.model + (s.model_loaded ? ' в памяти' : ' не загружена') +
        ' · в работе ' + s.queue.running + ', в очереди ' + s.queue.waiting;
    }).catch(function () { $('health').className = 'dot bad'; });
  }
  setInterval(function () { if (S.key) health(); }, 10000);

  // ───────── чаты ─────────
  function loadChats() {
    return api('/api/chats').then(function (r) { S.chats = r.chats; renderChats(); });
  }
  function renderChats() {
    var box = clear($('chat-list'));
    if (!S.chats.length) box.appendChild(h('div', { class: 'muted small', text: 'Чатов пока нет' }));
    S.chats.forEach(function (c) {
      var del = h('button', { title: 'удалить', text: '✕' });
      var item = h('div', { class: 'chat-item' + (c.id === S.chatId ? ' active' : '') }, [h('span', { text: c.title || 'без названия' }), del]);
      item.addEventListener('click', function () { openChat(c.id); });
      del.addEventListener('click', function (e) {
        e.stopPropagation();
        if (!confirm('Удалить чат «' + c.title + '»?')) return;
        api('/api/chats/' + c.id, { method: 'DELETE' }).then(function () { if (S.chatId === c.id) newChat(); return loadChats(); });
      });
      box.appendChild(item);
    });
  }
  function newChat() { S.chatId = null; clear($('messages')).appendChild(h('div', { class: 'empty', text: 'Новый чат. Модель работает на этом сервере, данные никуда не уходят.' })); renderChats(); $('input').focus(); }
  $('new-chat').addEventListener('click', function () { showChat(); newChat(); });

  function openChat(id) {
    showChat();
    api('/api/chats/' + id).then(function (r) {
      S.chatId = id; renderChats();
      var box = clear($('messages'));
      r.chat.messages.forEach(function (m) {
        var meta = null;
        try { meta = m.meta ? JSON.parse(m.meta) : null; } catch (e) {}
        box.appendChild(msgNode(m.role, m.content, meta));
      });
      box.scrollTop = box.scrollHeight;
    }).catch(function (e) { alert(e.message); });
  }

  function msgNode(role, text, meta) {
    var body = h('div', { class: 'msg__body' });
    if (role === 'assistant') body.appendChild(MD.render(text || '')); else body.textContent = text;
    var n = h('div', { class: 'msg msg--' + role }, [h('div', { class: 'msg__who', text: role === 'user' ? 'вы' : 'модель' }), body]);
    if (meta) n.appendChild(metaNode(meta));
    return n;
  }
  function metaNode(u) {
    var parts = [];
    if (u.answer_tokens != null) parts.push(u.answer_tokens + ' ток.');
    if (u.tokens_per_s) parts.push(u.tokens_per_s + ' ток/с');
    if (u.total_s != null) parts.push(u.total_s + ' с');
    if (u.queue_s) parts.push('очередь ' + u.queue_s + ' с');
    if (u.history_dropped) parts.push('из контекста убрано старых сообщений: ' + u.history_dropped);
    if (u.done_reason === 'length') parts.push('ответ обрезан по max tokens');
    if (u.stopped) parts.push('остановлено');
    return h('div', { class: 'msg__meta', text: parts.join(' · ') });
  }

  // ───────── отправка со стримингом ─────────
  function setBusy(b) { $('send').hidden = b; $('stop').hidden = !b; $('input').disabled = b; }
  $('stop').addEventListener('click', function () { if (S.abort) S.abort.abort(); });
  $('input').addEventListener('keydown', function (e) { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); $('composer').requestSubmit(); } });
  $('input').addEventListener('input', function () {
    var n = $('input').value.length, max = S.me ? S.me.limits.max_message_chars : 0;
    $('counter').textContent = n ? n + ' / ' + max : '';
    $('counter').style.color = n > max ? 'var(--bad)' : '';
  });

  $('composer').addEventListener('submit', function (e) {
    e.preventDefault();
    var text = $('input').value.trim();
    if (!text || S.abort) return;
    var box = $('messages');
    var empty = box.querySelector('.empty'); if (empty) empty.remove();
    box.appendChild(msgNode('user', text));
    var body = h('div', { class: 'msg__body cursor' });
    var node = h('div', { class: 'msg msg--assistant' }, [h('div', { class: 'msg__who', text: 'модель' }), body]);
    box.appendChild(node); box.scrollTop = box.scrollHeight;
    $('input').value = ''; $('counter').textContent = '';
    S.abort = new AbortController(); setBusy(true);
    var answer = '';

    api('/api/chat', { body: { message: text, chat_id: S.chatId, model: $('model').value, stream: true,
                               temperature: parseFloat($('temp').value), max_tokens: parseInt($('maxtok').value, 10) },
                       signal: S.abort.signal, raw: true })
      .then(function (r) {
        if (!r.ok) return r.json().then(function (j) { throw new Error(j.error.message + (j.error.retry_after ? ' (повторите через ' + j.error.retry_after + ' с)' : '')); });
        var reader = r.body.getReader(), dec = new TextDecoder(), buf = '';
        function pump() {
          return reader.read().then(function (res) {
            if (res.done) return;
            buf += dec.decode(res.value, { stream: true });
            var lines = buf.split('\n'); buf = lines.pop();
            lines.forEach(function (line) {
              if (!line.trim()) return;
              var ev = JSON.parse(line);
              if (ev.type === 'meta') { S.chatId = ev.chat_id; if (ev.queue_s > 1) body.setAttribute('data-q', ev.queue_s); }
              else if (ev.type === 'token') { answer += ev.text; clear(body).appendChild(MD.render(answer)); box.scrollTop = box.scrollHeight; }
              else if (ev.type === 'done') node.appendChild(metaNode(ev.usage));
              else if (ev.type === 'error') node.appendChild(h('div', { class: 'msg__err', text: ev.error.message }));
            });
            return pump();
          });
        }
        return pump();
      })
      .catch(function (err) {
        if (err.name === 'AbortError') node.appendChild(metaNode({ stopped: true }));
        else node.appendChild(h('div', { class: 'msg__err', text: err.message }));
      })
      .then(function () {
        body.classList.remove('cursor'); S.abort = null; setBusy(false); $('input').focus();
        loadChats();
      });
  });

  // ───────── панель сервиса (администратор) ─────────
  function showChat() { $('admin-view').hidden = true; $('chat-view').hidden = false; clearInterval(S.adminTimer); }
  $('admin-close').addEventListener('click', showChat);
  $('admin-btn').addEventListener('click', function () {
    $('chat-view').hidden = true; $('admin-view').hidden = false; loadAdmin();
    clearInterval(S.adminTimer); S.adminTimer = setInterval(loadAdmin, 3000);
  });

  function table(cols, rows) {
    var t = h('table', null, h('tr', null, cols.map(function (c) { return h('th', { text: c[0] }); })));
    rows.forEach(function (r) {
      t.appendChild(h('tr', null, cols.map(function (c) {
        var v = c[1](r);
        return h('td', { class: c[2] || '', text: v == null ? '—' : String(v) });
      })));
    });
    return t;
  }
  function sCls(s) { return 's' + String(s)[0]; }

  function loadAdmin() {
    api('/api/admin/stats').then(function (d) {
      var q = d.queue, rej = Object.keys(q.rejected).reduce(function (a, k) { return a + q.rejected[k]; }, 0);
      var loaded = (d.ollama.loaded || []).map(function (m) { return m.name + ' ' + m.gpu + '% GPU'; }).join(', ') || 'не загружена';
      var cards = clear($('admin-cards'));
      [['в работе / очередь', q.running + ' / ' + q.waiting], ['обслужено', q.served], ['отказов (лимиты)', rej],
       ['модель в памяти', loaded], ['работает', Math.round(d.uptime_s / 60) + ' мин']].forEach(function (c) {
        cards.appendChild(h('div', { class: 'card' }, [h('b', { text: String(c[1]) }), h('span', { class: 'muted small', text: c[0] })]));
      });
      clear($('admin-keys')).appendChild(table([['ключ', function (k) { return k.name + (k.admin ? ' ★' : ''); }], ['префикс', function (k) { return k.hint; }],
        ['запросов в мин', function (k) { return k.usage.used + ' / ' + k.usage.rpm; }, 'num'], ['параллельно', function (k) { return k.usage.active + ' / ' + k.usage.concurrency; }, 'num']], d.keys));
      var st = clear($('admin-status'));
      st.appendChild(table([['код', function (r) { return r.status; }], ['причина', function (r) { return r.code; }], ['запросов', function (r) { return r.n; }, 'num']], d.log.by_status));
      if (rej) st.appendChild(h('div', { class: 'muted small', text: 'Отказы с момента запуска: ' + Object.keys(q.rejected).map(function (k) { return k + ' ' + q.rejected[k]; }).join(', ') }));
      clear($('admin-clients')).appendChild(table([['ключ', function (r) { return r.owner; }], ['IP', function (r) { return r.ip; }], ['запросов', function (r) { return r.n; }, 'num'],
        ['успешных', function (r) { return r.ok; }, 'num'], ['среднее время, с', function (r) { return r.avg_ms == null ? null : (r.avg_ms / 1000).toFixed(1); }, 'num']], d.log.by_client));
      var rt = table([['время', function (r) { return fmtTime(r.ts); }], ['ключ', function (r) { return r.owner; }], ['IP', function (r) { return r.ip; }],
        ['путь', function (r) { return r.path; }], ['код', function (r) { return r.status + (r.code ? ' ' + r.code : ''); }], ['время, с', function (r) { return (r.latency_ms / 1000).toFixed(1); }, 'num'],
        ['очередь, с', function (r) { return r.queue_s; }, 'num'], ['токены', function (r) { return r.prompt_tokens == null ? null : r.prompt_tokens + ' → ' + r.answer_tokens; }, 'num']], d.log.recent);
      Array.prototype.forEach.call(rt.querySelectorAll('tr'), function (tr, i) { if (i) tr.children[4].className = sCls(d.log.recent[i - 1].status); });
      clear($('admin-recent')).appendChild(rt);
    }).catch(function (e) { clearInterval(S.adminTimer); alert(e.message); });
  }

  // ───────── старт ─────────
  S.key = lsGet('key');
  if (S.key) start().catch(function (e) { showLogin(e.status === 401 ? 'Ключ не подходит' : e.message); });
  else showLogin();
})();
