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
    // Сколько уже в папке (rescue.sources.*.folder): «больше 31,0 ГБ», если папка
    // большая и программа не успела посчитать все файлы
    folderSize: (folder) => `${(folder || {}).size_partial ? 'больше ' : ''}${App.fmt.bytes((folder || {}).size_bytes || 0)}`,
    pct: (part, total) => (total > 0 ? Math.max(0, Math.min(100, (part / total) * 100)) : 0),
    // «0,4%», «42%», «100%» — доли процента видны, чтобы начало не выглядело как ноль
    pctText(part, total) {
      if (!(total > 0)) return '—';
      if (part >= total) return '100%';
      const p = (part / total) * 100;
      if (p > 0 && p < 1) return `${p.toFixed(1).replace('.', ',')}%`;
      return `${Math.min(99, Math.floor(p))}%`;
    },
    hhmmss(ts) {
      if (!ts) return '';
      return new Date(ts * 1000).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    },
    // Сколько прошло: «только что», «12 сек назад», «3 мин назад», иначе время
    ago(sec, ts) {
      if (sec == null || !isFinite(sec)) return '';
      sec = Math.max(0, Math.round(sec));
      if (sec < 5) return 'только что';
      if (sec < 60) return `${sec} сек назад`;
      if (sec < 3600) return `${Math.floor(sec / 60)} мин назад`;
      return ts ? `в ${App.fmt.hhmm(ts)}` : `${Math.floor(sec / 3600)} ч назад`;
    },
    // Длинный путь сокращается посередине: диск и последние папки видны — C:\…\Фото\2024
    shortPath(path, max = 32) {
      path = String(path || '');
      if (path.length <= max) return path;
      const sep = path.includes('\\') ? '\\' : '/';
      const parts = path.split(sep);
      if (parts.length > 2) {
        const head = parts[0] || sep;
        let tail = parts[parts.length - 1];
        for (let i = parts.length - 2; i > 0; i--) {
          const next = `${parts[i]}${sep}${tail}`;
          if (head.length + next.length + 3 > max) break;
          tail = next;
        }
        const out = `${head}${head.endsWith(sep) ? '' : sep}…${sep}${tail}`;
        if (out.length <= max + 2) return out;
      }
      const keep = max - 1;
      return `${path.slice(0, Math.ceil(keep / 2))}…${path.slice(path.length - Math.floor(keep / 2))}`;
    },
    // «за последнюю минуту», «за последние 5 минут»
    lastMinutes(sec) {
      const m = Math.max(1, Math.round((sec || 0) / 60));
      return m === 1 ? 'за последнюю минуту' : `за последние ${m} ${plural(m, 'минуту', 'минуты', 'минут')}`;
    },
  };

  // ── Иконки: контурные, 24×24, цвет берут у текста ──
  const ICONS = {
    home: '<path d="m3 10 9-7 9 7v10a2 2 0 0 1-2 2h-4v-7H9v7H5a2 2 0 0 1-2-2z"/>',
    sliders: '<path d="M4 21v-7"/><path d="M4 10V3"/><path d="M12 21v-9"/><path d="M12 8V3"/><path d="M20 21v-5"/><path d="M20 12V3"/><path d="M2 14h4"/><path d="M10 8h4"/><path d="M18 16h4"/>',
    grid: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/>',
    help: '<circle cx="12" cy="12" r="10"/><path d="M9.1 9a3 3 0 0 1 5.8 1c0 2-3 3-3 3"/><path d="M12 17h.01"/>',
    heart: '<path d="M19 14c1.5-1.5 3-3.2 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.8 0-3 .5-4.5 2-1.5-1.5-2.7-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.3 1.5 4 3 5.5l7 7z"/>',
    download: '<path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M5 21h14"/>',
    check: '<path d="M20 6 9 17l-5-5"/>',
    x: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
    alert: '<path d="m21.7 18-8-14a2 2 0 0 0-3.5 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.7-3"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
    info: '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/>',
    clock: '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>',
    folder: '<path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.7-.9l-.8-1.2A2 2 0 0 0 7.9 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2z"/>',
    cloud: '<path d="M17.5 19H9a7 7 0 1 1 6.7-9h1.8a4.5 4.5 0 1 1 0 9z"/>',
    phone: '<rect x="5" y="2" width="14" height="20" rx="2.5"/><path d="M12 18h.01"/>',
    image: '<rect x="3" y="3" width="18" height="18" rx="2.5"/><circle cx="9" cy="9" r="2"/><path d="m21 15-3.1-3.1a2 2 0 0 0-2.8 0L6 21"/>',
    video: '<path d="m16 13 5.2 3.1a.5.5 0 0 0 .8-.4V8.3a.5.5 0 0 0-.8-.4L16 11"/><rect x="2" y="6" width="14" height="12" rx="2"/>',
    file: '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/>',
    key: '<circle cx="7.5" cy="15.5" r="5.5"/><path d="m21 2-9.6 9.6"/><path d="m15.5 7.5 3 3L22 7l-3-3"/>',
    drive: '<path d="M22 12H2"/><path d="M5.5 5.1 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.5-6.9A2 2 0 0 0 16.8 4H7.2a2 2 0 0 0-1.7 1.1z"/><path d="M6 16h.01"/><path d="M10 16h.01"/>',
    refresh: '<path d="M3 12a9 9 0 0 1 9-9 9.8 9.8 0 0 1 6.7 2.7L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.8 9.8 0 0 1-6.7-2.7L3 16"/><path d="M8 16H3v5"/>',
    stop: '<rect x="6" y="6" width="12" height="12" rx="2"/>',
    play: '<path d="M7 4.5v15a1 1 0 0 0 1.5.9l12-7.5a1 1 0 0 0 0-1.8l-12-7.5A1 1 0 0 0 7 4.5z"/>',
    copy: '<rect x="8" y="8" width="14" height="14" rx="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>',
    external: '<path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
    shield: '<path d="M20 13c0 5-3.5 7.5-7.7 9a1 1 0 0 1-.6 0C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.2-2.7a1.2 1.2 0 0 1 1.6 0C14.5 3.8 17 5 19 5a1 1 0 0 1 1 1z"/><path d="m9 12 2 2 4-4"/>',
    activity: '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
    list: '<path d="M8 6h13"/><path d="M8 12h13"/><path d="M8 18h13"/><path d="M3 6h.01"/><path d="M3 12h.01"/><path d="M3 18h.01"/>',
    server: '<rect x="2" y="3" width="20" height="8" rx="2"/><rect x="2" y="13" width="20" height="8" rx="2"/><path d="M6 7h.01"/><path d="M6 17h.01"/>',
    sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2"/><path d="M12 20v2"/><path d="m4.9 4.9 1.4 1.4"/><path d="m17.7 17.7 1.4 1.4"/><path d="M2 12h2"/><path d="M20 12h2"/><path d="m6.3 17.7-1.4 1.4"/><path d="m19.1 4.9-1.4 1.4"/>',
    moon: '<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9z"/>',
    arrow: '<path d="M5 12h14"/><path d="m12 5 7 7-7 7"/>',
    back: '<path d="M19 12H5"/><path d="m12 19-7-7 7-7"/>',
    send: '<path d="m22 2-7 20-4-9-9-4z"/><path d="M22 2 11 13"/>',
    book: '<path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/>',
    lifebuoy: '<circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="4"/><path d="m4.9 4.9 4.3 4.3"/><path d="m14.8 9.2 4.3-4.3"/><path d="m14.8 14.8 4.3 4.3"/><path d="m9.2 14.8-4.3 4.3"/>',
    sparkles: '<path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z"/><path d="M19 17v4"/><path d="M17 19h4"/>',
    trash: '<path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>',
    hourglass: '<path d="M5 22h14"/><path d="M5 2h14"/><path d="M17 22v-4.2a2 2 0 0 0-.6-1.4L12 12l-4.4 4.4a2 2 0 0 0-.6 1.4V22"/><path d="M7 2v4.2a2 2 0 0 0 .6 1.4L12 12l4.4-4.4a2 2 0 0 0 .6-1.4V2"/>',
    power: '<path d="M12 2v10"/><path d="M18.4 6.6a9 9 0 1 1-12.8 0"/>',
    chevron: '<path d="m9 18 6-6-6-6"/>',
    users: '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.9"/><path d="M16 3.1a4 4 0 0 1 0 7.8"/>',
    link: '<path d="M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7.1-7.1l-1.7 1.7"/><path d="M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7.1 7.1l1.7-1.7"/>',
    'cloud-down': '<path d="M7 18a4.5 4.5 0 0 1-.6-8.96A6 6 0 0 1 18 8.5a4 4 0 0 1-.5 9.5"/><path d="M12 11v8"/><path d="m9 16 3 3 3-3"/>',
    hdd: '<path d="M22 12H2"/><path d="M5.5 5.1 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.5-6.9A2 2 0 0 0 16.8 4H7.2a2 2 0 0 0-1.7 1.1z"/><path d="M6 16h.01"/><path d="M10 16h.01"/>',
    code: '<path d="m16 18 6-6-6-6"/><path d="m8 6-6 6 6 6"/>',
    'shield-check': '<path d="M20 13c0 5-3.5 7.5-7.7 9a1 1 0 0 1-.6 0C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.2-2.7a1.2 1.2 0 0 1 1.6 0C14.5 3.8 17 5 19 5a1 1 0 0 1 1 1z"/><path d="m9 12 2 2 4-4"/>',
  };
  App.icon = (name, cls = '') =>
    `<svg class="i${cls ? ` ${cls}` : ''}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ` +
    `stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name] || ICONS.info}</svg>`;
  // <span data-icon="home"></span> в разметке → иконка
  App.hydrateIcons = (root = document) => {
    for (const el of root.querySelectorAll('[data-icon]')) {
      if (!el.firstElementChild) el.innerHTML = App.icon(el.dataset.icon);
    }
  };

  // ── Тема: светлая или тёмная; выбор помнит браузер ──
  App.theme = {
    get: () => document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light',
    set(theme) {
      document.documentElement.setAttribute('data-theme', theme);
      try { localStorage.setItem('yadisk-theme', theme); } catch (e) { /* приватный режим — тема до перезагрузки */ }
      App.theme.paint();
    },
    paint() {
      const btn = App.byId('theme-toggle');
      if (!btn) return;
      const dark = App.theme.get() === 'dark';
      const label = dark ? 'Светлая тема' : 'Тёмная тема';
      App.html(btn, `${App.icon(dark ? 'sun' : 'moon')}<span>${label}</span>`);
      btn.setAttribute('aria-label', label);
    },
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
  App.action('theme-toggle', () => App.theme.set(App.theme.get() === 'dark' ? 'light' : 'dark'));
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
  // ── «Нас поддерживают»: список ведёт автор на GitHub, программа держит его час ──
  App.supporters = null;
  App.loadSupporters = async () => {
    const res = await App.api.get('/api/supporters' + (App.demo ? '?demo=1' : ''));
    if (res.ok) {
      App.supporters = res.data;
      App.render();
    }
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
