/* Общие части экранов: шапка, баннеры, прогресс по источникам, «Где мои файлы», «Доступ». */
(function () {
  'use strict';
  const App = window.App;
  const { esc, fmt } = App;

  const SOURCE_NAMES = { disk: 'Обычный Диск', unlim: 'Безлимит (фото с телефона)' };
  const STATE_BADGE = {
    idle: ['', 'готов'], off: ['', 'выключен'], checking: ['run', 'проверяю доступ'],
    listing: ['run', 'получаю список файлов'], downloading: ['run', 'качается'],
    waiting: ['wait', 'ждём Яндекса'], done: ['ok', 'готово'], needs_action: ['err', 'нужно ваше действие'],
    stopped: ['', 'на паузе'], error: ['err', 'ошибка'],
  };
  App.SOURCE_NAMES = SOURCE_NAMES;

  // ── Шапка: три чипа — Диск, Безлимит, Папка ──
  App.renderHeader = (s, offline) => {
    const chips = App.byId('chips');
    if (!chips) return;
    if (offline || !s) {
      App.html(chips, `<span class="chip err"><span class="dot"></span>${offline
        ? 'Программа не отвечает — окно программы закрыто?' : 'Подключаюсь…'}</span>`);
      return;
    }
    const tok = s.token || {};
    const coo = s.cookies || {};
    const unlimSrc = (s.rescue && s.rescue.sources && s.rescue.sources.unlim) || {};
    const disk = tok.ok ? ['ok', `Диск: подключён${tok.login ? ` (${tok.login})` : ''}`]
      : tok.unreachable ? ['warn', 'Диск: Яндекс не ответил'] : ['err', 'Диск: нужен ключ'];
    const unlim = unlimSrc.action === 'cookies' ? ['err', 'Безлимит: обновите доступ']
      : coo.ok ? ['ok', 'Безлимит: подключён'] : ['', 'Безлимит: не подключён'];
    const dir = (s.config || {}).download_dir || '';
    const folder = (s.config || {}).download_dir_set ? ['ok', `Папка: ${dir}`] : ['warn', 'Папка: выберите'];
    const chip = ([cls, text], field) =>
      `<button type="button" class="chip ${cls}" data-action="go-access" data-field="${field}" title="${esc(text)}">` +
      `<span class="dot"></span>${esc(text)}</button>`;
    App.html(chips, chip(disk, 'token') + chip(unlim, 'cookies') + chip(folder, 'folder'));
    App.text(App.byId('brand-version'), `версия ${s.app_version || ''}${s.demo ? ` · демо: ${s.demo}` : ''}`);
  };

  // ── Кнопки для «нужно ваше действие» ──
  App.actionButtons = (action, s) => {
    if (!action) return '';
    return {
      token: '<button class="primary" data-action="go-access" data-field="token">Получить новый ключ</button>',
      cookies: '<button class="primary" data-action="go-access" data-field="cookies">Обновить доступ</button>',
      free_space: '<button class="primary" data-action="rescue-start">Я освободил место — продолжить</button>' +
        '<button data-action="go-access" data-field="folder">Выбрать другую папку</button>',
      send_logs: '<a class="btn primary" href="/api/logs/bundle">Скачать отчёт для поддержки</a>' +
        `<a class="btn" href="${esc((s && s.telegram_url) || 'https://t.me/davronsv')}" target="_blank" rel="noopener">Написать в Telegram</a>`,
    }[action.id] || '';
  };

  // ── Баннер «нужно ваше действие»: только пока другая часть ещё качается,
  //    иначе действие показывает сама главная карточка ──
  App.actionBanner = (action, s) => {
    if (!action || action.id === 'setup' || !(s && s.rescue && s.rescue.running)) return '';
    return `<div class="banner action"><span class="icon">⚠️</span><div>
      <h3>${esc(action.title)}</h3><p>${esc(action.text)}</p>
      <div class="row actions">${App.actionButtons(action, s)}</div></div></div>`;
  };

  // ── Спокойная плашка: ничего делать не нужно ──
  App.noticeBanner = (notice) => notice
    ? `<div class="banner notice"><span class="icon">ℹ️</span><div><h3>${esc(notice.title)}</h3><p>${esc(notice.text)}</p></div></div>`
    : '';

  App.safetyBanner = () => `<div class="banner safety"><span class="icon">🛟</span><div>
    <h3>Пока не всё скачано — ничего не удаляйте с Яндекс.Диска</h3>
    <p>И не нажимайте «Освободить место» в приложении Яндекса на телефоне: файлы на молчащих серверах не потеряны,
    они временно недоступны, и программа докачает их сама.</p></div></div>`;

  // ── Прогресс одного источника ──
  App.sourceBlock = (name, src) => {
    src = src || {};
    const [cls, label] = !src.configured ? ['', 'не подключён'] : (STATE_BADGE[src.state] || ['', src.state || '']);
    const head = `<div class="source-head"><div>
        <div class="source-title">${SOURCE_NAMES[name]}</div>
        <div class="source-sub">${name === 'disk' ? 'по ключу доступа к Диску' : 'по доступу к Безлимиту'}</div>
      </div><span class="badge ${cls}">${esc(label)}</span></div>`;
    if (!src.configured) {
      return `<div class="card source">${head}
        <p class="muted">Не подключён. ${name === 'disk'
          ? 'Чтобы скачать обычные файлы, нужен ключ доступа к Диску.'
          : 'Чтобы скачать фото, загруженные с телефона в безлимит, нужен доступ к Безлимиту.'}</p>
        <div class="row"><button data-action="go-access" data-field="${name === 'disk' ? 'token' : 'cookies'}">Подключить</button></div></div>`;
    }
    const f = src.files || {};
    const b = src.bytes || {};
    const selected = f.selected || 0;
    if (!f.total) {
      return `<div class="card source">${head}
        <p class="muted">Список файлов ещё не собран — программа получит его при запуске.</p>
        <div class="activity">${esc(src.activity || src.reason || '')}</div></div>`;
    }
    const seg = (cls2, n) => `<span class="${cls2}" style="width:${fmt.pct(n, selected).toFixed(2)}%"></span>`;
    const bar = `<div class="bar" role="img" aria-label="Скачано ${f.done} из ${selected}">${
      seg('done', f.done)}${seg('work', f.in_progress)}${seg('wait', f.deferred)}${seg('fail', f.failed)}</div>`;
    const legend = [
      `<span><i class="l-done"></i>скачано ${fmt.num(f.done)}</span>`,
      f.pending ? `<span><i class="l-pend"></i>в очереди ${fmt.num(f.pending + (f.in_progress || 0))}</span>` : '',
      f.deferred ? `<span><i class="l-wait"></i>ждут Яндекса ${fmt.num(f.deferred)}</span>` : '',
      f.failed ? `<span><i class="l-fail"></i>не скачались ${fmt.num(f.failed)}</span>` : '',
      f.preview_only ? `<span><i class="l-prev"></i>есть превью ${fmt.num(f.preview_only)}</span>` : '',
    ].join('');
    const speed = src.state === 'downloading' && src.speed_bps
      ? ` · ${fmt.speed(src.speed_bps)}${src.eta_sec ? ` · осталось ≈ ${fmt.duration(src.eta_sec)}` : ''}` : '';
    const activity = src.state === 'waiting' && src.next_retry_at
      ? `Следующая попытка в ${fmt.hhmm(src.next_retry_at)}`
      : src.state === 'needs_action' ? (src.reason || 'Нужно ваше действие — подробности выше')
        : (src.activity || src.reason || '');
    return `<div class="card source">${head}
      <div class="big-number">${fmt.num(f.done)} <small>из ${fmt.files(selected)}</small></div>
      <div class="muted small">${fmt.bytes(b.done)} из ${fmt.bytes(b.selected || b.total)}${speed}</div>
      ${bar}<div class="legend">${legend}</div>
      <div class="activity">${esc(activity)}</div>
      <div><a href="#/details/${name}">Подробнее →</a></div></div>`;
  };

  // ── Где мои файлы ──
  App.whereFiles = (s) => {
    const dir = (s.config || {}).download_dir || '';
    const r = s.rescue || {};
    const disk = (r.sources || {}).disk || {};
    const unlim = (r.sources || {}).unlim || {};
    const size = ((disk.folder || {}).size_bytes || 0);
    const previews = ((disk.files || {}).preview_only || 0) + ((unlim.files || {}).preview_only || 0);
    const free = s.free_space && s.free_space.free != null ? ` · свободно на диске ${fmt.bytes(s.free_space.free)}` : '';
    return `<div class="card"><h3>Где мои файлы</h3>
      <div class="path-box"><code>${esc(dir)}</code></div>
      <p class="small muted" style="margin-top:8px">Обычный Диск — с сохранением папок, фото из Безлимита — в подпапке «_unlim»${
        previews ? ', превью — в подпапке «_превью»' : ''}. Уже в папке: ${fmt.bytes(size)}${free}.</p>
      <div class="row" style="margin-top:12px">
        <button data-action="open-folder" data-which="download">Открыть папку</button>
        <button data-action="copy-path" data-path="${esc(dir)}">Скопировать путь</button>
        ${previews ? '<button data-action="open-folder" data-which="previews">Открыть превью</button>' : ''}
      </div></div>`;
  };

  // ── «Доступ»: ключ Диска, папка, доступ к Безлимиту (общий для настройки и «Настроек») ──
  const POLYGON_URL = 'https://yandex.ru/dev/disk/poligon/';
  const FORMS = {
    token: `<div class="access-form" data-form="token">
        <ol class="small muted" style="padding-left:18px">
          <li>Нажмите «Открыть страницу Яндекса» и войдите в аккаунт, файлы которого спасаете.</li>
          <li>Нажмите там «Получить OAuth-токен» и скопируйте длинную строку целиком.</li>
          <li>Вставьте её сюда и нажмите «Проверить и сохранить».</li>
        </ol>
        <div class="row" style="margin-top:10px"><a class="btn" href="${POLYGON_URL}" target="_blank" rel="noopener">Открыть страницу Яндекса</a></div>
        <label class="field"><span>Ключ доступа (токен)</span>
          <input type="password" data-field="token-input" autocomplete="off" spellcheck="false" placeholder="y0_…"></label>
        <div class="row"><button class="primary" data-action="token-save">Проверить и сохранить</button>
          <span class="small muted" data-field="token-result"></span></div></div>`,
    folder: `<div class="access-form" data-form="folder">
        <label class="field"><span>Куда сохранять файлы — полный путь к папке</span>
          <input type="text" data-field="folder-input" spellcheck="false" placeholder="D:\\YandexBackup"></label>
        <p class="tiny">Лучше отдельная папка на диске, где хватит места. Корень диска (C:\\) выбрать нельзя.</p>
        <div class="row" style="margin-top:8px"><button class="primary" data-action="folder-save">Сохранить папку</button>
          <span class="small muted" data-field="folder-result"></span></div></div>`,
    cookies: `<div class="access-form" data-form="cookies">
        <ol class="small muted" style="padding-left:18px">
          <li>Установите в браузер расширение Cookie-Editor.</li>
          <li>Откройте disk.yandex.ru и войдите в свой аккаунт.</li>
          <li>Нажмите на значок Cookie-Editor → Export → Export as JSON.</li>
          <li>Вставьте скопированное сюда и нажмите «Сохранить доступ».</li>
        </ol>
        <p class="small" style="margin-top:6px"><a href="/guide" target="_blank" rel="noopener">Подробная инструкция с картинками</a></p>
        <label class="field"><span>Данные входа (JSON из Cookie-Editor)</span>
          <textarea data-field="cookies-input" spellcheck="false" placeholder='[{"name":"Session_id", …}]'></textarea></label>
        <div class="row"><button class="primary" data-action="cookies-save">Сохранить доступ</button>
          <span class="small muted" data-field="cookies-result"></span></div></div>`,
  };
  const ROW_TITLES = { token: 'Ключ доступа к Диску', folder: 'Папка для файлов', cookies: 'Доступ к Безлимиту' };

  App.Access = {
    // kinds — какие строки показать; open — какие формы сразу раскрыты
    mount(el, kinds = ['token', 'folder', 'cookies'], open = [], { buttons = true } = {}) {
      el.setAttribute('data-access', '1');
      el.dataset.buttons = buttons ? '1' : '0';
      el.innerHTML = kinds.map((k) => `<div class="access-row" data-row="${k}">
          <div class="what">${ROW_TITLES[k]}</div>
          <div class="state" data-field="${k}-state">…</div>
          <div class="row" data-field="${k}-buttons"></div>
          <div class="${open.includes(k) ? '' : 'hidden'}" data-wrap="${k}" style="width:100%">${FORMS[k]}</div>
        </div>`).join('');
    },
    update(el, s) {
      if (!el || !s) return;
      const q = (sel) => el.querySelector(sel);
      const running = !!(s.pipeline && s.pipeline.running);
      const withButtons = el.dataset.buttons !== '0';
      const tok = s.token || {};
      if (q('[data-row="token"]')) {
        const acc = tok.account || {};
        App.text(q('[data-field="token-state"]'), tok.ok
          ? `Работает: ${tok.login || ''}${acc.total ? `, занято ${fmt.bytes(acc.used)} из ${fmt.bytes(acc.total)}` : ''}${
            acc.photo_unlim ? `, в Безлимите ${fmt.bytes(acc.photo_unlim)}` : ''}`
          : tok.unreachable ? 'Яндекс сейчас не ответил на проверку — ключ, скорее всего, в порядке'
            : 'Не задан — без него обычный Диск не скачать');
        App.html(q('[data-field="token-buttons"]'), !withButtons ? '' : tok.ok || tok.unreachable
          ? '<button class="small" data-action="toggle-form" data-form-kind="token">Заменить ключ</button>' +
            '<button class="small danger" data-action="token-clear">Удалить ключ</button>'
          : '<button class="small primary" data-action="toggle-form" data-form-kind="token">Ввести ключ</button>');
      }
      if (q('[data-row="folder"]')) {
        const cfg = s.config || {};
        const free = s.free_space && s.free_space.free != null ? ` · свободно ${fmt.bytes(s.free_space.free)}` : '';
        App.text(q('[data-field="folder-state"]'), `${cfg.download_dir || '—'}${cfg.download_dir_set ? '' : ' (по умолчанию)'}${free}`);
        App.html(q('[data-field="folder-buttons"]'), !withButtons ? '' : running
          ? '<span class="tiny">Сменить папку можно после остановки скачивания</span>'
          : '<button class="small" data-action="toggle-form" data-form-kind="folder">Сменить папку</button>');
        const input = q('[data-field="folder-input"]');
        if (input && !input.value && document.activeElement !== input) input.value = cfg.download_dir || '';
      }
      if (q('[data-row="cookies"]')) {
        const coo = s.cookies || {};
        App.text(q('[data-field="cookies-state"]'), coo.ok
          ? `Подключён${coo.count ? ` (${coo.count} записей)` : ''}`
          : 'Не подключён — нужен, чтобы скачать фото, загруженные с телефона');
        App.html(q('[data-field="cookies-buttons"]'), !withButtons ? '' : coo.ok
          ? '<button class="small" data-action="toggle-form" data-form-kind="cookies">Обновить доступ</button>' +
            '<button class="small danger" data-action="cookies-clear">Удалить доступ</button>'
          : '<button class="small primary" data-action="toggle-form" data-form-kind="cookies">Подключить</button>');
      }
    },
    open(el, kind) {
      const wrap = el && el.querySelector(`[data-wrap="${kind}"]`);
      if (!wrap) return;
      wrap.classList.remove('hidden');
      const input = wrap.querySelector('input,textarea');
      if (input) input.focus();
    },
  };

  const accessRoot = (btn) => btn.closest('[data-access]');
  const field = (root, name) => root.querySelector(`[data-field="${name}"]`);

  App.action('toggle-form', (btn) => {
    const wrap = accessRoot(btn).querySelector(`[data-wrap="${btn.dataset.formKind}"]`);
    wrap.classList.toggle('hidden');
    if (!wrap.classList.contains('hidden')) {
      const input = wrap.querySelector('input,textarea');
      if (input) input.focus();
    }
  });

  App.action('token-save', (btn) => App.run(btn, async () => {
    const root = accessRoot(btn);
    const token = field(root, 'token-input').value.trim();
    const out = field(root, 'token-result');
    if (!token) return { ok: false, error: 'Вставьте ключ — длинную строку со страницы Яндекса' };
    App.text(out, 'Проверяю…');
    const check = await App.api.post('/api/config/token/check', { token });
    const c = check.data || {};
    if (!c.ok && !c.unreachable) {
      App.text(out, '');
      return { ok: false, error: `Ключ не подошёл: ${c.msg || check.error || 'Яндекс его не принял'}. Скопируйте его заново целиком.` };
    }
    const saved = await App.api.post('/api/config/token', { token });
    if (!saved.ok) { App.text(out, ''); return saved; }
    field(root, 'token-input').value = '';
    App.text(out, c.ok ? `Сохранено: ${c.login}` : 'Сохранено. Яндекс сейчас не ответил на проверку — проверю позже.');
    root.dispatchEvent(new CustomEvent('access-saved', { bubbles: true, detail: { kind: 'token' } }));
    return { ok: true, data: { msg: 'Ключ сохранён' } };
  }));

  App.action('token-clear', async (btn) => {
    const yes = await App.confirm({ title: 'Удалить ключ доступа?', danger: true, ok: 'Удалить ключ',
      text: 'Обычный Диск перестанет скачиваться, пока вы не введёте ключ снова. Скачанные файлы останутся.' });
    if (yes) App.run(btn, () => App.api.post('/api/config/token/clear'));
  });

  App.action('folder-save', (btn) => App.run(btn, async () => {
    const root = accessRoot(btn);
    const dir = field(root, 'folder-input').value.trim();
    if (!dir) return { ok: false, error: 'Укажите папку — например D:\\YandexBackup' };
    const res = await App.api.post('/api/config/download_dir', { download_dir: dir });
    if (res.ok) {
      App.text(field(root, 'folder-result'), 'Сохранено');
      root.dispatchEvent(new CustomEvent('access-saved', { bubbles: true, detail: { kind: 'folder' } }));
      return { ok: true, data: { msg: 'Папка сохранена' } };
    }
    return res;
  }));

  App.action('cookies-save', (btn) => App.run(btn, async () => {
    const root = accessRoot(btn);
    const raw = field(root, 'cookies-input').value.trim();
    if (!raw) return { ok: false, error: 'Вставьте данные из Cookie-Editor (Export → Export as JSON)' };
    let cookies;
    try { cookies = JSON.parse(raw); } catch (e) {
      return { ok: false, error: 'Это не похоже на экспорт Cookie-Editor: скопируйте через Export → Export as JSON' };
    }
    if (!Array.isArray(cookies)) return { ok: false, error: 'Нужен список из Cookie-Editor (Export as JSON)' };
    const res = await App.api.post('/api/cookies', { cookies });
    if (res.ok) {
      field(root, 'cookies-input').value = '';
      App.text(field(root, 'cookies-result'), 'Сохранено');
      root.dispatchEvent(new CustomEvent('access-saved', { bubbles: true, detail: { kind: 'cookies' } }));
      return { ok: true, data: { msg: 'Доступ к Безлимиту сохранён' } };
    }
    return res;
  }));

  App.action('cookies-clear', async (btn) => {
    const yes = await App.confirm({ title: 'Удалить доступ к Безлимиту?', danger: true, ok: 'Удалить доступ',
      text: 'Фото из Безлимита перестанут скачиваться, пока вы не подключите доступ снова. Скачанное останется.' });
    if (yes) App.run(btn, () => App.api.post('/api/cookies/clear'));
  });

  // ── Общие действия ──
  App.action('go-access', (btn) => {
    App.pendingAccessFocus = btn.dataset.field;
    App.go('#/settings');
  });
  App.action('go-setup', () => App.go('#/setup'));
  App.action('rescue-start', (btn) => App.run(btn, () => App.api.post('/api/rescue/start')));
  App.action('rescue-stop', async (btn) => {
    const yes = await App.confirm({ title: 'Остановить скачивание?', ok: 'Остановить',
      text: 'Уже скачанное сохранено. «Продолжить» докачает остальное без повторов.' });
    if (yes) App.run(btn, () => App.api.post('/api/start/stop'));
  });
  App.action('retry-now', (btn) => App.run(btn, () => App.api.post('/api/rescue/retry_now')));
  App.action('retry-failed', (btn) => App.run(btn, () =>
    App.api.post('/api/rescue/retry_failed', { source: btn.dataset.source || 'all' })));
  App.action('open-folder', (btn) => App.run(btn, () =>
    App.api.post('/api/open_folder', { which: btn.dataset.which || 'download' })));
  App.action('copy-path', async (btn) => {
    try {
      await navigator.clipboard.writeText(btn.dataset.path || '');
      App.toast('Путь скопирован', 'ok');
    } catch (e) {
      App.toast(btn.dataset.path || '', 'info', 8000);
    }
  });
})();
