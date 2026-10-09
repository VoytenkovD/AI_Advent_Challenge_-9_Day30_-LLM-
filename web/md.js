// Безопасный мини-рендерер Markdown: строит DOM-узлы, никакого innerHTML с текстом модели.
// Поддержка: ```код``` (с кнопкой копирования), `код`, **жирный**, *курсив*, # заголовки, списки, > цитаты, ссылки http(s).
(function () {
  'use strict';

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function inline(parent, text) {
    var re = /(`[^`]+`)|(\*\*[^*]+\*\*)|(\*[^*\s][^*]*\*)|(\[[^\]]+\]\(https?:\/\/[^)\s]+\))/g, last = 0, m;
    while ((m = re.exec(text))) {
      if (m.index > last) parent.appendChild(document.createTextNode(text.slice(last, m.index)));
      var t = m[0];
      if (m[1]) parent.appendChild(el('code', 'ic', t.slice(1, -1)));
      else if (m[2]) parent.appendChild(el('strong', null, t.slice(2, -2)));
      else if (m[3]) parent.appendChild(el('em', null, t.slice(1, -1)));
      else {
        var mm = t.match(/^\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)$/);
        var a = el('a', null, mm[1]); a.href = mm[2]; a.target = '_blank'; a.rel = 'noopener noreferrer';
        parent.appendChild(a);
      }
      last = re.lastIndex;
    }
    if (last < text.length) parent.appendChild(document.createTextNode(text.slice(last)));
  }

  function codeBlock(lang, code, open) {
    var wrap = el('div', 'code');
    var head = el('div', 'code__head');
    head.appendChild(el('span', null, lang || 'text'));
    var btn = el('button', 'code__copy', 'copy');
    btn.type = 'button';
    btn.addEventListener('click', function () {
      navigator.clipboard && navigator.clipboard.writeText(code).then(function () {
        btn.textContent = 'copied'; setTimeout(function () { btn.textContent = 'copy'; }, 1200);
      });
    });
    head.appendChild(btn);
    wrap.appendChild(head);
    wrap.appendChild(el('pre', open ? 'open' : null, code));
    return wrap;
  }

  // простые формулы LaTeX (\( n \), \times, \frac{a}{b}) → обычный текст: в терминале LaTeX выглядит мусором
  var TEX = { '\\times': '×', '\\cdot': '·', '\\leq': '≤', '\\le': '≤', '\\geq': '≥', '\\ge': '≥', '\\neq': '≠',
    '\\ne': '≠', '\\approx': '≈', '\\infty': '∞', '\\ldots': '…', '\\cdots': '…', '\\dots': '…', '\\pi': 'π', '\\sum': 'Σ',
    '\\sqrt': '√', '\\pm': '±', '\\to': '→', '\\rightarrow': '→', '\\in': '∈', '\\alpha': 'α', '\\beta': 'β', '\\Delta': 'Δ' };
  function untex(s) {
    return s.replace(/\\\(\s*([\s\S]*?)\s*\\\)/g, '$1').replace(/\\\[\s*([\s\S]*?)\s*\\\]/g, '$1')
      .replace(/\\frac\{([^{}]*)\}\{([^{}]*)\}/g, '($1)/($2)').replace(/\\text\{([^{}]*)\}/g, '$1')
      .replace(/\\[a-zA-Z]+/g, function (m) { return TEX[m] || m; });
  }

  function render(src) {
    var root = el('div', 'md');
    var raw = src.replace(/\r/g, '').split('\n'), inCode = false;
    var lines = raw.map(function (l) {   // формулы правим вне блоков кода
      if (/^\s*```/.test(l)) { inCode = !inCode; return l; }
      return inCode ? l : untex(l);
    });
    var i = 0, para = [], list = null;
    function flushPara() {
      if (!para.length) return;
      var p = el('p');
      para.forEach(function (l, k) { if (k) p.appendChild(el('br')); inline(p, l); });
      root.appendChild(p); para = [];
    }
    function flushList() { list = null; }
    while (i < lines.length) {
      var line = lines[i];
      var fence = line.match(/^\s*```\s*([\w+#.-]*)\s*$/);
      if (fence) {
        flushPara(); flushList();
        var buf = [], closed = false;
        i++;
        while (i < lines.length) {
          if (/^\s*```\s*$/.test(lines[i])) { closed = true; break; }
          buf.push(lines[i]); i++;
        }
        root.appendChild(codeBlock(fence[1], buf.join('\n'), !closed));
        i++;
        continue;
      }
      var h = line.match(/^(#{1,4})\s+(.*)$/);
      var li = line.match(/^\s*(?:[-*•]|(\d+)[.)])\s+(.*)$/);
      var q = line.match(/^>\s?(.*)$/);
      if (h) { flushPara(); flushList(); var hn = el('h' + Math.min(4, h[1].length + 1)); inline(hn, h[2]); root.appendChild(hn); }
      else if (li) {
        flushPara();
        var tag = li[1] ? 'ol' : 'ul';
        if (!list || list.tagName.toLowerCase() !== tag) { list = el(tag); root.appendChild(list); }
        var item = el('li'); inline(item, li[2]); list.appendChild(item);
      }
      else if (q) { flushPara(); flushList(); var bq = el('blockquote'); inline(bq, q[1]); root.appendChild(bq); }
      else if (!line.trim()) { flushPara(); flushList(); }
      else { flushList(); para.push(line); }
      i++;
    }
    flushPara();
    return root;
  }

  window.MD = { render: render };
})();
