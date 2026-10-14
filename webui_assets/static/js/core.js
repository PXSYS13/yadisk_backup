/* Ядро интерфейса 2.0: состояние, запросы, маршруты, опрос статуса, действия.
   Обычный скрипт без сборки: всё живёт в пространстве имён window.App. */
(function () {
  'use strict';

  const App = {
    state: { status: null, route: 'home', params: {}, offline: false },
    screens: {},
    actions: {},
    demo: new URLSearchParams(location.search).get('demo'),
  };
  window.App = App;

  // ── DOM и экранирование ──
  App.byId = (id) => document.getElementById(id);
  App.esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  App.html = (el, markup) => { if (el && el.innerHTML !== markup) el.innerHTML = markup; };
  App.text = (el, value) => { if (el && el.textContent !== String(value)) el.textContent = String(value); };

  // ── Форматирование по-русски ──
  const plural = (n, one, few, many) => {
    n = Math.abs(Math.trunc(n));
    if (n % 10 === 1 && n % 100 !== 11) return one;
    if (n % 10 >= 2 && n % 10 <= 4 && (n % 100 < 12 || n % 100 > 14)) return few;
    return many;
  };
  App.fmt = {
    plural,
    num: (n) => Math.trunc(n || 0).toLocaleString('ru-RU'),
    files: (n) => `${App.fmt.num(n)} ${plural(n || 0, 'файл', 'файла', 'файлов')}`,
    bytes(n) {
      n = Number(n) || 0;
      const units = ['Б', 'КБ', 'МБ', 'ГБ', 'ТБ'];
      let i = 0;
      while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
      const digits = i >= 2 ? 1 : 0;
      return `${n.toFixed(digits).replace('.', ',')} ${units[i]}`;
    },
    hhmm(ts) {
      if (!ts) return '';
      return new Date(ts * 1000).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
    },
    duration(sec) {
      if (sec == null || !isFinite(sec)) return '';
      sec = Math.max(0, Math.round(sec));
      const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60);
      if (d) return `${d} ${plural(d, 'день', 'дня', 'дней')} ${h} ч`;
      if (h) return `${h} ч ${m} мин`;
      if (m) return `${m} мин`;
      return 'меньше минуты';
    },
    speed: (bps) => (bps ? `${App.fmt.bytes(bps)}/с` : ''),
    pct: (part, total) => (total > 0 ? Math.max(0, Math.min(100, (part / total) * 100)) : 0),
  };

  // ── Запросы к программе ──
  async function request(method, url, body) {
    let resp;
    try {
      resp = await fetch(url, {
        method,
        headers: body !== undefined ? { 'Content-Type': 'application/json' } : {},
        body: body !== undefined ? JSON.stringify(body) : undefined,
      });
    } catch (e) {
      return { ok: false, error: 'Программа не отвечает. Чёрное окно программы закрыто? Запустите её снова.' };
    }
    let data = null;
    try { data = await resp.json(); } catch (e) { /* не JSON */ }
    if (!resp.ok) {
      const detail = data && (data.detail || data.msg);
      return { ok: false, data, error: typeof detail === 'string' ? detail : `Ошибка ${resp.status}` };
    }
    if (data && data.ok === false) return { ok: false, data, error: data.msg || 'Не получилось' };
    return { ok: true, data };
  }
  App.api = {
    get: (url) => request('GET', url),
    post: (url, body = {}) => request('POST', url, body),
  };

  // Кнопка «занята», пока идёт запрос; сообщение — уведомлением
  App.run = async (btn, fn, { okToast = true } = {}) => {
    if (btn) btn.disabled = true;
    try {
      const res = await fn();
      if (res && res.ok === false) App.toast(res.error || 'Не получилось', 'err');
      else if (res && okToast && res.data && res.data.msg) App.toast(res.data.msg, 'ok');
      App.refresh();
      return res;
    } finally {
      if (btn) btn.disabled = false;
    }
  };

  // ── Уведомления и диалог ──
  let toastTimer = null;
  App.toast = (msg, kind = 'info', ms = 4500) => {
    const el = App.byId('toast');
    if (!el) return;
    el.textContent = msg;
    el.className = kind === 'info' ? '' : kind;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.add('hidden'), ms);
  };

  App.confirm = ({ title, text, ok = 'Да', cancel = 'Отмена', danger = false }) => new Promise((resolve) => {
    const root = App.byId('dialog-root');
    root.innerHTML = `<div class="dialog-backdrop" data-dialog-close="1">
      <div class="dialog" role="dialog" aria-modal="true" aria-labelledby="dialog-title">
        <h3 id="dialog-title">${App.esc(title)}</h3>
        <p>${App.esc(text)}</p>
        <div class="row actions">
          <button type="button" data-dialog-answer="0">${App.esc(cancel)}</button>
          <button type="button" class="${danger ? 'danger' : 'primary'}" data-dialog-answer="1">${App.esc(ok)}</button>
        </div>
      </div></div>`;
    const close = (answer) => {
      document.removeEventListener('keydown', onKey);
      root.innerHTML = '';
      resolve(answer);
    };
    const onKey = (e) => { if (e.key === 'Escape') close(false); };
    document.addEventListener('keydown', onKey);
    root.querySelector('.dialog-backdrop').addEventListener('click', (e) => {
      const btn = e.target.closest('[data-dialog-answer]');
      if (btn) close(btn.dataset.dialogAnswer === '1');
      else if (e.target.dataset.dialogClose) close(false);
    });
    const okBtn = root.querySelector('[data-dialog-answer="1"]');
    if (okBtn) okBtn.focus();
  });

  // ── Действия: <button data-action="имя"> → App.actions.имя(кнопка, событие) ──
  App.action = (name, fn) => { App.actions[name] = fn; };
  document.addEventListener('click', (ev) => {
    const el = ev.target.closest('[data-action]');
    if (!el || el.disabled) return;
    const fn = App.actions[el.dataset.action];
    if (!fn) { console.warn('нет действия', el.dataset.action); return; }
    // Флажок должен переключиться сам — его не трогаем
    const toggle = el.tagName === 'INPUT' && (el.type === 'checkbox' || el.type === 'radio');
    if (!toggle) ev.preventDefault();
    fn(el, ev);
  });

  // ── Маршруты: #/home, #/settings, #/details/disk … ──
  App.go = (hash) => { if (location.hash !== hash) location.hash = hash; else App.route(); };
  App.route = () => {
    const parts = (location.hash || '#/home').replace(/^#\/?/, '').split('/');
    const route = App.screens[parts[0]] ? parts[0] : 'home';
    const params = { source: parts[1] || '' };
    App.state.route = route;
    App.state.params = params;
    for (const [name, screen] of Object.entries(App.screens)) {
      const el = App.byId(`screen-${name}`);
      if (!el) continue;
      const active = name === route;
      el.classList.toggle('hidden', !active);
      if (active) {
        if (!screen._mounted) { screen.mount(el); screen._mounted = true; }
        if (screen.show) screen.show(params);
        if (App.state.status) screen.update(App.state.status, params);
      }
    }
    for (const a of document.querySelectorAll('#tabs a')) {
      const tabRoute = a.dataset.route;
      const owner = route === 'details' || route === 'setup' ? 'home' : route;
      a.classList.toggle('active', tabRoute === owner);
    }
    window.scrollTo({ top: 0 });
  };
  window.addEventListener('hashchange', App.route);

  // ── Опрос статуса ──
  let pollTimer = null;
  let inFlight = false;
  App.refresh = async () => {
    if (inFlight) return;
    inFlight = true;
    try {
      const url = '/api/status' + (App.demo ? `?demo=${encodeURIComponent(App.demo)}` : '');
      const res = await App.api.get(url);
      App.state.offline = !res.ok;
      if (res.ok) App.state.status = res.data;
      App.render();
    } finally {
      inFlight = false;
    }
  };
  App.render = () => {
    const s = App.state.status;
    if (App.renderHeader) App.renderHeader(s, App.state.offline);
    const screen = App.screens[App.state.route];
    if (s && screen && screen._mounted) screen.update(s, App.state.params);
  };
  App.startPolling = () => {
    const tick = async () => {
      await App.refresh();
      pollTimer = setTimeout(tick, document.hidden ? 6000 : 1500);
    };
    clearTimeout(pollTimer);
    tick();
  };
})();
