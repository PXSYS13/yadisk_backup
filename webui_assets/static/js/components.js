/* Общие части экранов: подключения в меню, баннеры, источники с этапами, лента событий,
   последние файлы, кольцо прогресса, «Где мои файлы», «Доступ». */
(function () {
  'use strict';
  const App = window.App;
  const { esc, fmt } = App;
  const icon = App.icon;

  const SOURCE_TITLE = { disk: 'Обычный Диск', unlim: 'Безлимит' };
  const SOURCE_SUB = { disk: 'ваши папки и файлы', unlim: 'фото и видео, загруженные с телефона' };
  const SOURCE_ICON = { disk: ['cloud', ''], unlim: ['phone', 'violet'] };
  const SOURCE_TAG = { disk: 'Диск', unlim: 'Безлимит' };
  const STATE_BADGE = {
    idle: ['plain', 'готов'], off: ['plain', 'выключен'], checking: ['run', 'проверяю доступ'],
    listing: ['run', 'получаю список'], downloading: ['run', 'качается'],
    waiting: ['wait', 'ждём Яндекса'], done: ['ok', 'готово'], needs_action: ['err', 'нужно действие'],
    stopped: ['plain', 'на паузе'], error: ['err', 'ошибка'],
  };
  App.SOURCE_NAMES = { disk: 'Обычный Диск', unlim: 'Безлимит (фото с телефона)' };
  App.SOURCE_TAG = SOURCE_TAG;
  const cap = (s) => (s ? s.charAt(0).toUpperCase() + s.slice(1) : s);

  // ── Подключения в боковом меню: Диск, Безлимит, Папка ──
  App.renderHeader = (s, offline) => {
    const chips = App.byId('chips');
    if (!chips) return;
    if (offline || !s) {
      App.html(chips, `<div class="chip err"><span class="dot"></span><span class="chip-text">
        <span class="chip-label">${offline ? 'Программа не отвечает' : 'Подключаюсь…'}</span>
        <span class="chip-value">${offline ? 'окно программы закрыто?' : 'секунду'}</span></span></div>`);
      return;
    }
    const tok = s.token || {};
    const coo = s.cookies || {};
    const unlimSrc = ((s.rescue || {}).sources || {}).unlim || {};
    const disk = tok.ok ? ['ok', `подключён${tok.login ? ` · ${tok.login}` : ''}`]
      : tok.unreachable ? ['warn', 'Яндекс не ответил на проверку'] : ['err', 'нужен ключ доступа'];
    const unlim = unlimSrc.action === 'cookies' ? ['err', 'обновите доступ']
      : coo.ok ? ['ok', 'подключён'] : ['', 'не подключён'];
    const cfg = s.config || {};
    const folder = cfg.download_dir_set ? ['ok', cfg.download_dir || ''] : ['warn', 'выберите папку'];
    const chip = ([cls, value], label, field) =>
      `<button type="button" class="chip ${cls}" data-action="go-access" data-field="${field}" title="${esc(`${label}: ${value}`)}">` +
      `<span class="dot"></span><span class="chip-text"><span class="chip-label">${label}</span>` +
      `<span class="chip-value">${esc(value)}</span></span></button>`;
    App.html(chips, chip(disk, 'Диск', 'token') + chip(unlim, 'Безлимит', 'cookies') + chip(folder, 'Папка', 'folder'));
    App.text(App.byId('brand-version'), `версия ${s.app_version || ''}${s.demo ? ` · демо: ${s.demo}` : ''}`);
  };

  // ── Кнопки для «нужно ваше действие» ──
  App.actionButtons = (action, s, big = false) => {
    if (!action) return '';
    const p = big ? 'primary big' : 'primary';
    return {
      token: `<button class="${p}" data-action="go-access" data-field="token">${icon('key')}Получить новый ключ</button>`,
      cookies: `<button class="${p}" data-action="go-access" data-field="cookies">${icon('refresh')}Обновить доступ</button>`,
      free_space: `<button class="${p}" data-action="rescue-start">${icon('play')}${
        action.variant === 'folder' ? 'Попробовать снова' : 'Я освободил место — продолжить'}</button>` +
        `<button data-action="go-access" data-field="folder">${icon('folder')}Выбрать другую папку</button>`,
      send_logs: `<a class="btn ${p}" href="/api/logs/bundle">${icon('download')}Скачать отчёт для поддержки</a>` +
        `<a class="btn" href="${esc((s && s.telegram_url) || 'https://t.me/davronsv')}" target="_blank" rel="noopener">${icon('send')}Написать в Telegram</a>`,
    }[action.id] || '';
  };

  // ── Баннер «нужно ваше действие»: только пока другая часть ещё качается,
  //    иначе действие показывает сама главная карточка ──
  App.actionBanner = (action, s) => {
    if (!action || action.id === 'setup' || !(s && s.rescue && s.rescue.running)) return '';
    return `<div class="banner action"><span class="b-icon">${icon('alert')}</span><div>
      <h3>${esc(action.title)}</h3><p>${esc(action.text)}</p>
      <div class="row actions">${App.actionButtons(action, s)}</div></div></div>`;
  };

  // ── Спокойная плашка: ничего делать не нужно ──
  App.noticeBanner = (notice) => notice
    ? `<div class="banner notice"><span class="b-icon">${icon(notice.kind === 'storage_down' ? 'server' : 'info')}</span>
        <div><h3>${esc(notice.title)}</h3><p>${esc(notice.text)}</p></div></div>`
    : '';

  App.safetyBanner = () => `<div class="banner safety"><span class="b-icon">${icon('shield')}</span><div>
    <h3>Пока не всё скачано — ничего не удаляйте с Яндекс.Диска</h3>
    <p>И не нажимайте «Освободить место» в приложении Яндекса на телефоне: файлы на молчащих серверах не потеряны,
    они временно недоступны, и программа докачает их сама.</p></div></div>`;

  // ── Кольцо прогресса: зелёное — скачано, жёлтое — ждёт Яндекса, красное — не скачалось ──
  App.ring = ({ done = 0, wait = 0, fail = 0, total = 0, label = '', caption = '', spin = false }) => {
    const R = 52, C = 2 * Math.PI * R;
    const len = (n) => (total > 0 ? Math.max(0, Math.min(1, n / total)) * C : 0);
    const d = len(done), w = len(wait), f = len(fail);
    const arc = (cls, l, off) => (l >= 0.5
      ? `<circle class="${cls}" cx="60" cy="60" r="${R}" stroke-dasharray="${l.toFixed(1)} ${C.toFixed(1)}" stroke-dashoffset="${(-off).toFixed(1)}"/>`
      : '');
    const arcs = spin
      ? `<circle class="arc-spin" cx="60" cy="60" r="${R}" stroke-dasharray="${(C * 0.22).toFixed(1)} ${C.toFixed(1)}"/>`
      : arc('arc-done', d, 0) + arc('arc-wait', w, d) + arc('arc-fail', f, d + w);
    return `<div class="ring" role="img" aria-label="${esc(`${label} ${caption}`)}">
      <svg viewBox="0 0 120 120"><circle class="track" cx="60" cy="60" r="${R}"/>${arcs}</svg>
      <div class="center"><div class="pct">${esc(label)}</div><div class="cap">${esc(caption)}</div></div></div>`;
  };

  // ── Этапы источника: доступ → список файлов → скачивание → готово ──
  const STEP_NAMES = ['Доступ', 'Список файлов', 'Скачивание', 'Готово'];
  function stepModel(src) {
    const listed = ((src.files || {}).total || 0) > 0;
    switch (src.state) {
      case 'checking': return { at: 0, mode: 'current' };
      case 'listing': return { at: 1, mode: 'current' };
      case 'downloading': return { at: 2, mode: 'current' };
      case 'waiting': return { at: 2, mode: 'wait', sub: src.next_retry_at ? `повтор в ${fmt.hhmm(src.next_retry_at)}` : 'ждём Яндекса' };
      case 'done': return { at: 4, mode: 'done' };
      case 'needs_action': case 'error': return { at: listed ? 2 : 0, mode: 'err', sub: 'нужно действие' };
      case 'stopped': return { at: listed ? 2 : 1, mode: 'paused', sub: 'на паузе' };
      default: return null;
    }
  }
  App.steps = (src) => {
    const m = stepModel(src || {});
    if (!m) return '';
    const dots = { current: '', wait: icon('clock'), err: icon('alert'), paused: icon('stop') };
    return `<ol class="steps">${STEP_NAMES.map((name, i) => {
      if (i < m.at) return `<li class="done"><span class="st-dot">${icon('check')}</span><span>${name}</span></li>`;
      if (i === m.at) {
        return `<li class="${m.mode}"><span class="st-dot">${dots[m.mode]}</span><span>${name}</span>` +
          `${m.sub ? `<span class="st-sub">${esc(m.sub)}</span>` : ''}</li>`;
      }
      return `<li><span class="st-dot">${i + 1}</span><span>${name}</span></li>`;
    }).join('')}</ol>`;
  };

  // ── «Сейчас»: что источник делает в эту минуту — простыми словами ──
  function nowLine(src) {
    const f = src.files || {};
    const rc = src.recent;
    switch (src.state) {
      case 'checking': return ['live', 'activity', 'Проверяю доступ к Яндексу…'];
      case 'listing':
        return ['live', 'list', src.listing_found
          ? `Получаю список файлов — уже найдено ${fmt.num(src.listing_found)}` : 'Получаю у Яндекса список файлов…'];
      case 'downloading':
        if (rc && rc.checked) {
          const deferred = Math.max(0, rc.checked - rc.done);
          return ['live', 'activity', `${cap(fmt.lastMinutes(rc.window_sec))}: скачано ${fmt.num(rc.done)}` +
            (deferred ? `, отложено до повтора ${fmt.num(deferred)}` : '')];
        }
        if (rc) return ['live', 'hourglass', `${cap(fmt.lastMinutes(rc.window_sec))} новых файлов нет: Яндекс отвечает медленно, программа ждёт`];
        return ['live', 'activity', 'Скачиваю файлы…'];
      case 'waiting':
        return ['wait', 'clock', `${src.wait_kind === 'deferred' || !src.reason ? 'Всё доступное скачано' : src.reason}. ` +
          `Следующая попытка в ${fmt.hhmm(src.next_retry_at)}`];
      case 'done': return ['ok', 'check', f.failed ? `Готово. Не скачались: ${fmt.files(f.failed)}` : 'Готово: все файлы сохранены'];
      case 'needs_action': case 'error': return ['err', 'alert', src.reason || 'Нужно ваше действие — подробности выше'];
      case 'stopped': return ['', 'stop', 'На паузе. Всё скачанное сохранено'];
      default: return ['', 'info', f.total ? 'Готов к скачиванию' : 'Список файлов программа получит при запуске'];
    }
  }

  // ── Карточка источника ──
  App.sourceBlock = (name, src, r) => {
    src = src || {};
    const [badgeCls, badge] = !src.configured ? ['plain', 'не подключён'] : (STATE_BADGE[src.state] || ['plain', src.state || '']);
    const [ico, icoCls] = SOURCE_ICON[name];
    const head = `<div class="source-head"><span class="icon-box ${icoCls}">${icon(ico)}</span>
      <div class="grow"><div class="source-title">${SOURCE_TITLE[name]}</div><div class="source-sub">${SOURCE_SUB[name]}</div></div>
      <span class="badge ${badgeCls}">${esc(badge)}</span></div>`;
    if (!src.configured) {
      return `<div class="card source">${head}
        <p class="muted">${name === 'disk' ? 'Чтобы скачать обычные файлы, нужен ключ доступа к Диску.'
          : 'Чтобы скачать фото, загруженные с телефона в безлимит, нужен доступ к Безлимиту.'}</p>
        <div class="row"><button data-action="go-access" data-field="${name === 'disk' ? 'token' : 'cookies'}">${icon('key')}Подключить</button></div></div>`;
    }
    const steps = r && r.started_at ? App.steps(src) : '';
    const [nowCls, nowIcon, nowText] = nowLine(src);
    const activity = `<div class="activity ${nowCls}">${icon(nowIcon)}<span>${esc(nowText)}</span></div>`;
    const f = src.files || {};
    const b = src.bytes || {};
    if (!f.total) return `<div class="card source">${head}${steps}${activity}</div>`;
    const selected = f.selected || 0;
    const seg = (cls, n) => `<span class="${cls}" style="width:${fmt.pct(n, selected).toFixed(2)}%"></span>`;
    const bar = `<div class="bar" role="img" aria-label="Скачано ${f.done} из ${selected}">${
      seg('done', f.done)}${seg('work', f.in_progress)}${seg('wait', f.deferred)}${seg('fail', f.failed)}</div>`;
    const legend = [
      `<span><i class="l-done"></i>скачано ${fmt.num(f.done)}</span>`,
      f.pending || f.in_progress ? `<span><i class="l-pend"></i>в очереди ${fmt.num((f.pending || 0) + (f.in_progress || 0))}</span>` : '',
      f.deferred ? `<span><i class="l-wait"></i>ждут Яндекса ${fmt.num(f.deferred)}</span>` : '',
      f.failed ? `<span><i class="l-fail"></i>не скачались ${fmt.num(f.failed)}</span>` : '',
      f.preview_only ? `<span><i class="l-prev"></i>есть превью ${fmt.num(f.preview_only)}</span>` : '',
    ].join('');
    const speed = src.state === 'downloading' && src.speed_bps
      ? ` · ${fmt.speed(src.speed_bps)}${src.eta_sec ? ` · осталось ≈ ${fmt.duration(src.eta_sec)}` : ''}` : '';
    const last = (src.recent_files || [])[0];
    const lastText = last ? `Последний скачанный: ${last.name}${last.at ? ` · ${fmt.hhmm(last.at)}` : ''}` : '';
    return `<div class="card source">${head}${steps}
      <div><div class="progress-line"><div class="big-number">${fmt.num(f.done)} <small>из ${fmt.files(selected)}</small></div>
        <span class="pct-badge">${fmt.pctText(f.done, selected)}</span></div>
        <div class="muted small" style="margin-top:4px">${fmt.bytes(b.done)} из ${fmt.bytes(b.selected || b.total)}${speed}</div></div>
      ${bar}<div class="legend">${legend}</div>
      ${activity}
      <div class="source-foot"><span title="${esc(lastText)}">${esc(lastText)}</span>
        <a class="more" href="#/details/${name}">Подробнее ${icon('arrow')}</a></div></div>`;
  };

  // ── Лента «Что происходит» ──
  const EVENT_ICON = { ok: 'check', run: 'activity', wait: 'clock', warn: 'alert', err: 'alert', info: 'info' };
  App.feedCard = (r) => {
    const items = ((r && r.events) || []).slice(0, 8).map((e) => `<li class="${esc(e.kind)}">
        <time>${fmt.hhmm(e.at)}</time><span class="f-dot">${icon(EVENT_ICON[e.kind] || 'info')}</span>
        <div class="f-text">${esc(e.text)}${e.source ? `<span class="f-src">${SOURCE_TAG[e.source] || ''}</span>` : ''}</div></li>`).join('');
    return `<div class="card"><div class="card-head"><span class="icon-box">${icon('activity')}</span>
      <div class="grow"><h3>Что происходит</h3><span class="tiny">Главные события, новые сверху</span></div></div>
      ${items ? `<ul class="feed">${items}</ul>` : `<div class="empty">${icon('hourglass')}Событий пока нет</div>`}</div>`;
  };

  // ── Последние скачанные файлы — видно, что файлы правда появляются в папке ──
  const IMG = /\.(jpe?g|png|heic|heif|gif|webp|bmp|tiff?|dng|raw|cr2|nef|arw)$/i;
  const VID = /\.(mp4|mov|avi|mkv|3gp|m4v|webm|mts|wmv)$/i;
  App.recentCard = (r) => {
    const src = (r && r.sources) || {};
    const now = (r && r.now) || Date.now() / 1000;
    const list = [];
    for (const name of ['disk', 'unlim']) {
      for (const f of ((src[name] || {}).recent_files || [])) list.push(Object.assign({ source: name }, f));
    }
    list.sort((a, b) => (b.at || 0) - (a.at || 0));
    const rows = list.slice(0, 6).map((f) => {
      const kind = IMG.test(f.name) ? 'image' : VID.test(f.name) ? 'video' : 'file';
      const when = f.at ? fmt.ago(now - f.at, f.at) : '';
      return `<li><span class="f-ico ${kind === 'image' ? '' : kind}">${icon(kind)}</span>
        <div class="f-main"><span class="f-name" title="${esc(f.name)}">${esc(f.name)}</span>
        <span class="f-meta">${fmt.bytes(f.size)}${when ? ` · ${when}` : ''} · ${SOURCE_TAG[f.source]}</span></div></li>`;
    }).join('');
    if (!rows) return '';
    return `<div class="card"><div class="card-head"><span class="icon-box ok">${icon('check')}</span>
      <div class="grow"><h3>Последние скачанные</h3><span class="tiny">Эти файлы уже лежат в вашей папке</span></div>
      <button class="small" data-action="open-folder" data-which="download">${icon('folder')}Открыть</button></div>
      <ul class="files">${rows}</ul></div>`;
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
    return `<div class="card"><div class="card-head"><span class="icon-box info">${icon('folder')}</span>
      <div class="grow"><h3>Где мои файлы</h3><span class="tiny">Уже в папке: ${fmt.bytes(size)}${free}</span></div></div>
      <div class="path-box">${icon('drive')}<code>${esc(dir)}</code></div>
      <p class="small muted" style="margin-top:10px">Обычный Диск — с сохранением ваших папок, фото из Безлимита — в подпапке «_unlim»${
        previews ? ', превью — в подпапке «_превью»' : ''}.</p>
      <div class="row" style="margin-top:14px">
        <button data-action="open-folder" data-which="download">${icon('folder')}Открыть папку</button>
        <button data-action="copy-path" data-path="${esc(dir)}">${icon('copy')}Скопировать путь</button>
        ${previews ? `<button data-action="open-folder" data-which="previews">${icon('image')}Открыть превью</button>` : ''}
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
        <div class="row" style="margin-top:12px"><a class="btn" href="${POLYGON_URL}" target="_blank" rel="noopener">${icon('external')}Открыть страницу Яндекса</a></div>
        <label class="field"><span>Ключ доступа (токен)</span>
          <input type="password" data-field="token-input" autocomplete="off" spellcheck="false" placeholder="y0_…"></label>
        <div class="row"><button class="primary" data-action="token-save">${icon('check')}Проверить и сохранить</button>
          <span class="small muted" data-field="token-result"></span></div></div>`,
    folder: `<div class="access-form" data-form="folder">
        <label class="field"><span>Куда сохранять файлы — полный путь к папке</span>
          <input type="text" data-field="folder-input" spellcheck="false" placeholder="D:\\YandexBackup"></label>
        <p class="tiny">Лучше отдельная папка на диске, где хватит места. Корень диска (C:\\) выбрать нельзя.</p>
        <div class="row" style="margin-top:10px"><button class="primary" data-action="folder-save">${icon('check')}Сохранить папку</button>
          <span class="small muted" data-field="folder-result"></span></div></div>`,
    cookies: `<div class="access-form" data-form="cookies">
        <ol class="small muted" style="padding-left:18px">
          <li>Установите в браузер расширение Cookie-Editor.</li>
          <li>Откройте disk.yandex.ru и войдите в свой аккаунт.</li>
          <li>Нажмите на значок Cookie-Editor → Export → Export as JSON.</li>
          <li>Вставьте скопированное сюда и нажмите «Сохранить доступ».</li>
        </ol>
        <p class="small" style="margin-top:8px"><a href="/guide" target="_blank" rel="noopener">Подробная инструкция с картинками</a></p>
        <label class="field"><span>Данные входа (JSON из Cookie-Editor)</span>
          <textarea data-field="cookies-input" spellcheck="false" placeholder='[{"name":"Session_id", …}]'></textarea></label>
        <div class="row"><button class="primary" data-action="cookies-save">${icon('check')}Сохранить доступ</button>
          <span class="small muted" data-field="cookies-result"></span></div></div>`,
  };
  const ROW_TITLES = { token: 'Ключ доступа к Диску', folder: 'Папка для файлов', cookies: 'Доступ к Безлимиту' };
  const ROW_ICON = { token: ['key', ''], folder: ['folder', 'info'], cookies: ['phone', 'violet'] };

  App.Access = {
    // kinds — какие строки показать; open — какие формы сразу раскрыты
    mount(el, kinds = ['token', 'folder', 'cookies'], open = [], { buttons = true } = {}) {
      el.setAttribute('data-access', '1');
      el.dataset.buttons = buttons ? '1' : '0';
      el.innerHTML = kinds.map((k) => `<div class="access-row" data-row="${k}">
          <div class="what"><span class="icon-box ${ROW_ICON[k][1]}">${icon(ROW_ICON[k][0])}</span>${ROW_TITLES[k]}</div>
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
          : `<button class="small primary" data-action="toggle-form" data-form-kind="token">${icon('key')}Ввести ключ</button>`);
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
          : `<button class="small primary" data-action="toggle-form" data-form-kind="cookies">${icon('phone')}Подключить</button>`);
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
