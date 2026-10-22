/* Общие части экранов: подключения в меню, баннеры, источники с этапами, лента событий,
   последние файлы, кольцо прогресса, «Где мои файлы», «Доступ». */
(function () {
  'use strict';
  const App = window.App;
  const { esc, fmt } = App;
  const icon = App.icon;

  const SOURCE_TITLE = { disk: 'Обычный Диск', unlim: 'Безлимит' };
  const SOURCE_SUB = { disk: 'ваши папки и файлы', unlim: 'фото и видео, загруженные с телефона' };
  const SOURCE_ICON = { disk: 'hdd', unlim: 'phone' };
  const SOURCE_TAG = { disk: 'Диск', unlim: 'Безлимит' };
  const STATE_BADGE = {
    idle: ['', 'готов'], off: ['', 'выключен'], checking: ['run', 'проверяю доступ'],
    listing: ['run', 'собираю список'], downloading: ['run', 'качается'],
    waiting: ['wait', 'ждёт Яндекса'], done: ['ok', 'готово'], needs_action: ['err', 'нужно действие'],
    stopped: ['', 'на паузе'], error: ['err', 'ошибка'],
  };
  App.SOURCE_NAMES = { disk: 'Обычный Диск', unlim: 'Безлимит (фото с телефона)' };
  App.SOURCE_TAG = SOURCE_TAG;
  const cap = (s) => (s ? s.charAt(0).toUpperCase() + s.slice(1) : s);

  // ── Подключения в боковом меню: Диск, Безлимит, Папка ──
  App.renderHeader = (s, offline) => {
    const chips = App.byId('chips');
    if (!chips) return;
    paintThanks();
    if (offline || !s) {
      App.html(chips, `<div class="chip err"><span class="chip-label"><span class="dot"></span>${
        offline ? 'Программа не отвечает' : 'Подключаюсь…'}</span>
        <span class="chip-value">${offline ? 'окно программы закрыто?' : 'секунду'}</span></div>`);
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
      `<span class="chip-label"><span class="dot"></span>${label}</span>` +
      `<span class="chip-value">${esc(field === 'folder' ? fmt.shortPath(value, 30) : value)}</span></button>`;
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
      folders: `<button class="${p}" data-action="go-folders">${icon('folder')}Выбрать папки</button>`,
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

  // ── Тихая строка под главной карточкой: пока не всё скачано, ничего не удалять ──
  App.safetyNote = () => `<p class="safety-note">${icon('alert')}<span>Не удаляйте файлы с Диска и не нажимайте
    в приложении Яндекса «Освободить место», пока всё не скачается.</span></p>`;

  // ── Кольцо прогресса: зелёное — скачано, жёлтое — ждёт Яндекса, красное — не скачалось ──
  App.ring = ({ done = 0, wait = 0, fail = 0, total = 0, label = '', caption = '', spin = false }) => {
    const R = 54, C = 2 * Math.PI * R, MIN = 3;
    const circle = (cls, len, start) =>
      `<circle class="${cls}" cx="64" cy="64" r="${R}" stroke-dasharray="${len.toFixed(1)} ${C.toFixed(1)}" stroke-dashoffset="${(-start).toFixed(1)}"/>`;
    let arcs = '';
    if (spin) {
      arcs = circle('arc-spin', C * 0.22, 0);
    } else if (total > 0) {
      // Маленькая доля всё равно видна (3 единицы), между частями — тонкий зазор
      const parts = [['arc-done', done], ['arc-wait', wait], ['arc-fail', fail]].filter(([, n]) => n > 0);
      const lens = parts.map(([, n]) => Math.max(MIN, Math.min(1, n / total) * C));
      const sum = lens.reduce((a, b) => a + b, 0);
      if (sum > C) lens[lens.indexOf(Math.max(...lens))] -= sum - C;
      const gap = parts.length > 1 ? 1.5 : 0;
      let start = 0;
      arcs = parts.map(([cls], i) => {
        const out = circle(cls, Math.max(0, lens[i] - gap), start);
        start += lens[i];
        return out;
      }).join('');
    }
    // «20%» — число крупно, знак процента мельче
    const m = /^(.*\d)%$/.exec(label);
    const big = m ? `${esc(m[1])}<span class="pct-sign">%</span>` : esc(label);
    const long = String(m ? m[1] : label).length > 4 ? ' long' : '';
    return `<div class="ring" role="img" aria-label="${esc(`${label} ${caption}`)}">
      <svg viewBox="0 0 128 128"><circle class="track" cx="64" cy="64" r="${R}"/>${arcs}</svg>
      <div class="center"><div class="pct${long}">${big}</div><div class="cap">${esc(caption)}</div></div></div>`;
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
    return `<ol class="steps" aria-label="Этапы">${STEP_NAMES.map((name, i) => {
      if (i < m.at) {
        return `<li class="done"><span class="st-bar"></span><span class="st-name">${icon('check')}${name}</span></li>`;
      }
      if (i === m.at) {
        return `<li class="${m.mode}" aria-current="step"><span class="st-bar"></span><span class="st-name">${name}</span>` +
          `${m.sub ? `<span class="st-sub">${esc(m.sub)}</span>` : ''}</li>`;
      }
      return `<li><span class="st-bar"></span><span class="st-name">${name}</span></li>`;
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

  // ── «Список файлов собран в 14:05 · Обновить список»: список программа помнит между запусками ──
  const RELIST_STATES = new Set(['idle', 'waiting', 'done', 'stopped']);
  function whenText(ts) {
    const d = new Date(ts * 1000);
    const now = new Date();
    const yesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);
    const hm = fmt.hhmm(ts);
    if (d.toDateString() === now.toDateString()) return `в ${hm}`;
    if (d.toDateString() === yesterday.toDateString()) return `вчера в ${hm}`;
    return `${String(d.getDate()).padStart(2, '0')}.${String(d.getMonth() + 1).padStart(2, '0')} в ${hm}`;
  }
  function listedLine(name, src) {
    if (!src.listed_at || src.state === 'listing') return '';
    const hint = 'Программа сама собирает список заново раз в 6 часов. Нажмите, если ' +
      (name === 'unlim' ? 'с телефона только что загрузились новые фото' : 'только что добавили файлы на Диск');
    const btn = RELIST_STATES.has(src.state)
      ? `<span aria-hidden="true">·</span><button class="link" data-action="relist" data-source="${name}" title="${esc(hint)}">Обновить список</button>` : '';
    return `<div class="listed"><span>Список собран ${whenText(src.listed_at)}</span>${btn}</div>`;
  }

  // ── Карточка источника ──
  App.sourceBlock = (name, src, r) => {
    src = src || {};
    const [badgeCls, badge] = !src.configured ? ['', 'не подключён'] : (STATE_BADGE[src.state] || ['', src.state || '']);
    const head = `<div class="source-head"><span class="icon-box">${icon(SOURCE_ICON[name])}</span>
      <div class="grow"><div class="source-title">${SOURCE_TITLE[name]}</div><div class="source-sub">${SOURCE_SUB[name]}</div></div>
      <span class="badge ${badgeCls}">${esc(badge)}</span></div>`;
    if (!src.configured) {
      return `<div class="card source">${head}
        <p class="muted">${name === 'disk' ? 'Чтобы скачать обычные файлы, нужен ключ доступа к Диску.'
          : 'Чтобы скачать фото, загруженные с телефона в безлимит, нужен доступ к Безлимиту.'}</p>
        <div class="row"><button data-action="go-access" data-field="${name === 'disk' ? 'token' : 'cookies'}">${icon('key')}${
          name === 'disk' ? 'Подключить Диск' : 'Подключить Безлимит'}</button></div></div>`;
    }
    const steps = r && r.started_at ? App.steps(src) : '';
    const [nowCls, nowIcon, nowText] = nowLine(src);
    const activity = `<div class="activity ${nowCls}">${icon(nowIcon)}<span>${esc(nowText)}</span></div>`;
    const foot = `<div class="source-foot">${listedLine(name, src) || '<span></span>'}
      <a class="more" href="#/details/${name}">Подробнее ${icon('chevron')}</a></div>`;
    const f = src.files || {};
    const b = src.bytes || {};
    if (!f.total) return `<div class="card source">${head}${steps}${activity}${foot}</div>`;
    const selected = f.selected || 0;
    const seg = (cls, n) => `<span class="${cls}" style="width:${fmt.pct(n, selected).toFixed(2)}%"></span>`;
    const queue = (f.pending || 0) + (f.in_progress || 0);
    const bar = `<div class="bar" role="img" aria-label="Скачано ${f.done} из ${selected}">${
      seg('done', f.done)}${seg('work', queue)}${seg('wait', f.deferred)}${seg('fail', f.failed)}</div>`;
    const legend = [
      `<span><i class="l-done"></i>скачано <b>${fmt.num(f.done)}</b></span>`,
      queue ? `<span><i class="l-pend"></i>в очереди <b>${fmt.num(queue)}</b></span>` : '',
      f.deferred ? `<span><i class="l-wait"></i>ждут Яндекса <b>${fmt.num(f.deferred)}</b></span>` : '',
      f.failed ? `<span><i class="l-fail"></i>не скачались <b>${fmt.num(f.failed)}</b></span>` : '',
      f.preview_only ? `<span><i class="l-prev"></i>есть превью <b>${fmt.num(f.preview_only)}</b></span>` : '',
    ].join('');
    // Куски строки не рвутся внутри: на телефоне «осталось ≈ | 1 ч 25 мин» читалось плохо
    const speed = src.state === 'downloading' && src.speed_bps
      ? ` · <span>${fmt.speed(src.speed_bps)}</span>${src.eta_sec ? ` · <span>осталось ≈ ${fmt.duration(src.eta_sec)}</span>` : ''}` : '';
    return `<div class="card source">${head}${steps}
      <div><div class="progress-line"><div class="big-number"><b>${fmt.num(f.done)}</b> из ${fmt.filesOf(selected)}</div>
        <span class="pct-badge">${fmt.pctText(f.done, selected)}</span></div>
        <div class="bytes-line"><span>${fmt.bytes(b.done)} из ${fmt.bytes(b.selected || b.total)}</span>${speed}</div>
        <div style="margin-top:10px">${bar}</div><div class="legend">${legend}</div>${foldersLine(name, src)}</div>
      ${activity}${foot}</div>`;
  };

  // «Скачаны целиком: 2 из 6 папок» — такие папки можно переносить, не дожидаясь остальных
  function foldersLine(name, src) {
    const fl = src.folders;
    if (name !== 'disk' || !fl || fl.total < 2) return '';
    const text = fl.complete >= fl.total ? 'Все папки скачаны целиком'
      : `Скачаны целиком: <b>${fmt.num(fl.complete)}</b> из ${fmt.num(fl.total)} ${fmt.plural(fl.total, 'папки', 'папок', 'папок')}`;
    return `<a class="folders-line" href="#/details/disk">${icon('folder')}<span>${text}</span>${icon('chevron')}</a>`;
  }

  // ── «Места может не хватить»: осталось скачать больше, чем свободно на диске с папкой ──
  App.spaceBanner = (r) => {
    const sp = r && r.space;
    const act = r && r.action && r.action.id;
    if (!sp || !sp.short || act === 'free_space' || act === 'setup') return '';
    const btn = r.running ? ''
      : `<div class="row actions"><button data-action="go-access" data-field="folder">${icon('folder')}Выбрать другую папку</button></div>`;
    return `<div class="banner safety"><span class="b-icon">${icon('hdd')}</span><div>
      <h3>Места на диске может не хватить</h3>
      <p>Осталось скачать ${fmt.bytes(sp.need)}, а на диске с папкой свободно ${fmt.bytes(sp.free)}. Когда место
        закончится, программа остановится и попросит выбрать другую папку — например, на внешнем диске. Скачанное
        останется где лежит и второй раз качаться не будет.</p>${btn}</div></div>`;
  };

  // ── «Вышла версия X.Y.Z» (/api/status.update): только подсказка — программа сама
  //    ничего не скачивает. На Главной её можно скрыть до следующей версии ──
  App.UPDATE_ZIP_URL = 'https://github.com/PXSYS13/yadisk_backup/archive/refs/heads/main.zip';
  const RELEASES_URL = 'https://github.com/PXSYS13/yadisk_backup/releases/latest';
  const UPDATE_KEY = 'yadisk-update-hidden';
  let updateMemory = '';
  App.updateHidden = {
    get() {
      try {
        const v = localStorage.getItem(UPDATE_KEY);
        if (typeof v === 'string') updateMemory = v;
      } catch (e) { /* без хранилища помним до перезагрузки страницы */ }
      return updateMemory;
    },
    save(version) {
      updateMemory = String(version || '');
      try { localStorage.setItem(UPDATE_KEY, updateMemory); } catch (e) { /* то же */ }
    },
  };

  App.updateBanner = (u, hideable = false) => {
    if (!u || !u.latest) return '';
    const hide = hideable
      ? `<button class="link" data-action="update-hide" data-version="${esc(u.latest)}">Скрыть до следующей версии</button>` : '';
    return `<section class="banner update" aria-label="Новая версия программы"><span class="b-icon">${icon('sparkles')}</span>
      <div><h3>Вышла версия ${esc(u.latest)}</h3>
        <p>У вас версия ${esc(u.current)}. Скачайте архив, закройте чёрное окно программы и распакуйте архив
          поверх старой папки с заменой файлов, затем запустите программу снова. Ключ, данные входа, папка
          и прогресс скачивания сохранятся.</p>
        <div class="row actions">
          <a class="btn" href="${esc(App.UPDATE_ZIP_URL)}" target="_blank" rel="noopener">${icon('download')}Скачать новую версию</a>
          <a class="btn" href="${esc(u.url || RELEASES_URL)}" target="_blank" rel="noopener">${icon('external')}Что нового</a>
          ${hide}
        </div></div></section>`;
  };

  // ── Лента «Что происходит» ──
  App.feedCard = (r) => {
    const items = ((r && r.events) || []).slice(0, 8).map((e) => `<li class="${esc(e.kind)}">
        <time>${fmt.hhmm(e.at)}</time><span class="f-dot"></span>
        <div class="f-text">${esc(e.text)}${e.source ? `<span class="f-src">${SOURCE_TAG[e.source] || ''}</span>` : ''}</div></li>`).join('');
    return `<div class="card"><div class="card-head"><div class="grow"><h3>Что происходит</h3>
      <span class="tiny">Главные события, новые сверху</span></div></div>
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
      const when = f.at ? (now - f.at < 3600 ? fmt.ago(now - f.at, f.at) : fmt.hhmm(f.at)) : '';
      return `<li><span class="f-ico">${icon(kind)}</span>
        <div class="f-main"><span class="f-name" title="${esc(f.name)}">${esc(f.name)}</span>
        <span class="f-meta">${fmt.bytes(f.size)} · ${SOURCE_TAG[f.source]}</span></div>
        ${when ? `<span class="f-time">${esc(when)}</span>` : ''}</li>`;
    }).join('');
    if (!rows) return '';
    return `<div class="card"><div class="card-head"><div class="grow"><h3>Последние скачанные</h3>
      <span class="tiny">Эти файлы уже лежат в вашей папке</span></div></div>
      <ul class="files">${rows}</ul></div>`;
  };

  // ── Где мои файлы ──
  App.whereFiles = (s) => {
    const dir = (s.config || {}).download_dir || '';
    const r = s.rescue || {};
    const disk = (r.sources || {}).disk || {};
    const unlim = (r.sources || {}).unlim || {};
    const size = fmt.folderSize(disk.folder);
    const previews = ((disk.files || {}).preview_only || 0) + ((unlim.files || {}).preview_only || 0);
    const links = ((s.links || {}).items || []).length;
    const free = s.free_space && s.free_space.free != null ? ` · свободно ${fmt.bytes(s.free_space.free)}` : '';
    return `<div class="card"><div class="card-head"><div class="grow"><h3>Где мои файлы</h3>
      <span class="tiny">Уже в папке: ${size}${free}</span></div></div>
      <div class="path-box" title="${esc(dir)}">${icon('folder')}<code>${esc(dir)}</code></div>
      <p class="tiny" style="margin-top:10px">Обычный Диск — с сохранением ваших папок, фото из Безлимита — в подпапке «_unlim»${
        previews ? ', превью — в подпапке «_превью»' : ''}${links ? ', скачанное по ссылкам — в подпапке «_ссылки»' : ''}.</p>
      <div class="row" style="margin-top:14px;gap:6px">
        <button class="small" data-action="open-folder" data-which="download">${icon('folder')}Открыть папку</button>
        <button class="small ghost" data-action="copy-path" data-path="${esc(dir)}">${icon('copy')}Скопировать путь</button>
        ${previews ? `<button class="small ghost" data-action="open-folder" data-which="previews">${icon('image')}Открыть превью</button>` : ''}
      </div></div>`;
  };

  // ── «Нас поддерживают» ──
  const CURRENCY = { RUB: '₽', USD: '$', EUR: '€', KZT: '₸', UAH: '₴', BYN: 'Br' };
  App.money = (amount, currency) => (amount == null ? '' : `${String(amount).includes('.')
    ? Number(amount).toLocaleString('ru-RU', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
    : fmt.num(amount)} ${CURRENCY[currency] || currency || '₽'}`);
  App.initials = (name) => String(name || '?').replace(/\./g, '').split(/\s+/).filter(Boolean).slice(0, 2)
    .map((p) => p.charAt(0).toUpperCase()).join('') || '?';
  App.avatar = (name, i, small = false) =>
    `<span class="avatar t${i % 5}${small ? ' sm' : ''}" aria-hidden="true">${esc(App.initials(name))}</span>`;
  // «сегодня», «вчера», «3 дня назад», «2 недели назад», иначе «12 сентября»
  App.dayAgo = (iso) => {
    if (!iso) return '';
    const [y, mo, d] = iso.split('-').map(Number);
    const then = new Date(y, mo - 1, d);
    const now = new Date();
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const days = Math.round((today - then) / 86400000);
    if (days <= 0) return 'сегодня';
    if (days === 1) return 'вчера';
    if (days < 7) return `${days} ${fmt.plural(days, 'день', 'дня', 'дней')} назад`;
    if (days < 28) {
      const w = Math.floor(days / 7);
      return w === 1 ? 'неделю назад' : `${w} ${fmt.plural(w, 'неделю', 'недели', 'недель')} назад`;
    }
    return then.toLocaleDateString('ru-RU', { day: 'numeric', month: 'long', ...(y !== now.getFullYear() ? { year: 'numeric' } : {}) });
  };

  // Тихая строка внизу главной: «Проект живёт благодаря вам: Анна П., Сергей и ещё 45»
  App.thanksStrip = () => {
    const sp = App.supporters;
    if (!sp) return '';
    const list = sp.supporters || [];
    if (!list.length) {
      return `<aside class="thanks-strip" aria-label="Поддержка проекта"><div class="thanks-who">
        <p>Программа бесплатная и живёт на поддержке тех, кому она помогла. Имена поддержавших появляются здесь.</p></div>
        <a href="#/support">${icon('heart')}Поддержать</a></aside>`;
    }
    const seen = new Set();
    const first = list.filter((x) => x.name !== 'Аноним' && !seen.has(x.name) && seen.add(x.name)).slice(0, 3);
    const rest = Math.max(0, (sp.people || list.length) - first.length);
    const names = first.length ? first.map((x) => esc(x.name)).join(', ') : '';
    const text = names
      ? `Проект живёт благодаря вам: ${names}${rest ? ` и ещё ${fmt.num(rest)}` : ''}`
      : `Проект живёт благодаря вам: спасибо ${fmt.num(rest)} ${fmt.plural(rest, 'человеку', 'людям', 'людям')}`;
    return `<aside class="thanks-strip" aria-label="Поддержка проекта"><div class="thanks-who">
      ${first.length ? `<span class="avatars">${first.map((x, i) => App.avatar(x.name, i, true)).join('')}</span>` : ''}
      <p>${text}</p></div><a href="#/support">${icon('heart')}Поддержать</a></aside>`;
  };

  function paintThanks() {
    const link = App.byId('side-thanks');
    if (!link) return;
    const people = (App.supporters && App.supporters.people) || 0;
    link.classList.toggle('hidden', !people);
    if (people) App.text(App.byId('side-thanks-text'), `Спасибо ${fmt.num(people)} ${fmt.plural(people, 'помощнику', 'помощникам', 'помощникам')}`);
  }

  // ── «Помогло? Поддержите» — в момент результата: после «Готово» и на круглых числах ──
  // Каждый порог спрашиваем один раз: «Не сейчас» закрывает его, «Уже поддерживаю» — навсегда
  App.DONATE_MILESTONES = [1000, 10000, 50000, 100000];
  const DONATE_KEY = 'yadisk-donate';
  let donateMemory = { donated: false, closed: [] };
  App.donateSeen = {
    get() {
      try {
        const v = JSON.parse(localStorage.getItem(DONATE_KEY) || 'null');
        if (v && Array.isArray(v.closed)) donateMemory = { donated: !!v.donated, closed: v.closed.slice(-50) };
      } catch (e) { /* без хранилища помним до перезагрузки страницы */ }
      return donateMemory;
    },
    save(next) {
      donateMemory = next;
      try { localStorage.setItem(DONATE_KEY, JSON.stringify(next)); } catch (e) { /* то же */ }
    },
  };

  App.donateMoment = (r, seen) => {
    const sm = (r && r.summary) || {};
    const files = sm.downloaded_files || 0;
    if (!r || !seen || seen.donated || !files || r.action) return null;
    const reached = App.DONATE_MILESTONES.filter((m) => files >= m);
    const milestone = reached.length ? reached[reached.length - 1] : 0;
    const key = `m-${milestone}`;
    if (seen.closed.includes(key)) return null;
    const moment = { key, milestone, files, bytes: sm.downloaded_bytes || 0 };
    if (r.state === 'done') return { ...moment, kind: 'done' };
    if ((r.state === 'running' || r.state === 'waiting') && milestone) return { ...moment, kind: 'milestone' };
    return null;
  };

  App.donateCard = (m, url) => {
    if (!m) return '';
    // В «Готово» цифры уже крупно написаны выше — здесь не повторяем
    const title = m.kind === 'done' ? 'Программа помогла? Поддержите её'
      : `Уже ${fmt.files(m.milestone)} на вашем компьютере`;
    return `<section class="banner donate" aria-label="Поддержать проект"><span class="b-icon">${icon('heart')}</span>
      <div><h3>${esc(title)}</h3>
        <p>Программа бесплатная, её делает один человек в свободное время. Любая сумма помогает её развивать,
          а ваше имя появится в «Нас поддерживают».</p>
        <div class="row actions">
          <a class="btn primary" href="${esc(url || 'https://www.donationalerts.com/r/davronsvv')}" target="_blank" rel="noopener"
            data-action="donate-open" data-key="${esc(m.key)}">${icon('heart')}Поддержать проект</a>
          <button data-action="donate-later" data-key="${esc(m.key)}">Не сейчас</button>
          <button class="link" data-action="donate-done">Уже поддерживаю</button>
        </div></div></section>`;
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
        <p class="tiny">Лучше отдельная папка на диске, где хватит места, — подойдёт и внешний диск, например
          E:\\Яндекс Диск. Корень диска (C:\\ или E:\\) выбрать нельзя. Если сменить папку посреди скачивания,
          скачанное останется в старой папке, а остальное докачается в новую — второй раз ничего не качается.</p>
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
  const ROW_ICON = { token: 'key', folder: 'folder', cookies: 'image' };

  App.Access = {
    // kinds — какие строки показать; open — какие формы сразу раскрыты
    mount(el, kinds = ['token', 'folder', 'cookies'], open = [], { buttons = true } = {}) {
      el.setAttribute('data-access', '1');
      el.dataset.buttons = buttons ? '1' : '0';
      el.innerHTML = kinds.map((k) => `<div class="access-row" data-row="${k}">
          <div class="what"><span class="icon-box">${icon(ROW_ICON[k])}</span><div class="what-text">
            <div class="what-title">${ROW_TITLES[k]}</div>
            <div class="state" data-field="${k}-line"><span class="dot" data-field="${k}-dot"></span><span data-field="${k}-state">…</span></div>
          </div></div>
          <div class="row" data-field="${k}-buttons"></div>
          <div class="${open.includes(k) ? '' : 'hidden'}" data-wrap="${k}" style="width:100%">${FORMS[k]}</div>
        </div>`).join('');
    },
    update(el, s) {
      if (!el || !s) return;
      const q = (sel) => el.querySelector(sel);
      // Цветная точка у состояния: ok — работает, wait — проверить не удалось, err — нужно действие
      const tone = (k, cls) => {
        const dot = q(`[data-field="${k}-dot"]`);
        if (dot && dot.className !== `dot ${cls}`) dot.className = `dot ${cls}`;
        const line = q(`[data-field="${k}-line"]`);
        if (line) line.classList.toggle('err', cls === 'err');
      };
      const actionId = (((s.rescue || {}).action) || {}).id;
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
        tone('token', tok.ok ? 'ok' : tok.unreachable ? 'wait' : actionId === 'token' ? 'err' : '');
        App.html(q('[data-field="token-buttons"]'), !withButtons ? '' : tok.ok || tok.unreachable
          ? '<button class="small" data-action="toggle-form" data-form-kind="token">Заменить ключ</button>' +
            '<button class="small danger" data-action="token-clear">Удалить ключ</button>'
          : `<button class="small primary" data-action="toggle-form" data-form-kind="token">${icon('key')}Ввести ключ</button>`);
      }
      if (q('[data-row="folder"]')) {
        const cfg = s.config || {};
        const fsp = s.free_space || {};
        const space = (s.rescue || {}).space || {};
        const free = fsp.free != null ? ` · свободно ${fmt.bytes(fsp.free)}` : '';
        const short = space.short ? `<span class="note">Осталось скачать ${fmt.bytes(space.need)} — всё не поместится</span>` : '';
        const fat = fsp.fat ? `<span class="note">Диск в формате ${esc(fsp.fs || 'FAT32')}: файлы больше 4 ГБ на него не запишутся</span>` : '';
        App.html(q('[data-field="folder-state"]'),
          `${esc(cfg.download_dir || '—')}${cfg.download_dir_set ? '' : ' (по умолчанию)'}${free}${short}${fat}`);
        tone('folder', actionId === 'free_space' ? 'err' : short || fat || !cfg.download_dir_set ? 'wait' : 'ok');
        const linksRunning = !!(s.links && s.links.running);
        App.html(q('[data-field="folder-buttons"]'), !withButtons ? '' : running || linksRunning
          ? `<span class="tiny">Сменить папку можно после остановки скачивания${running ? '' : ' по ссылкам'}</span>`
          : '<button class="small" data-action="toggle-form" data-form-kind="folder">Сменить папку</button>');
        const input = q('[data-field="folder-input"]');
        if (input && !input.value && document.activeElement !== input) input.value = cfg.download_dir || '';
      }
      if (q('[data-row="cookies"]')) {
        const coo = s.cookies || {};
        App.text(q('[data-field="cookies-state"]'), coo.ok
          ? `Подключён${coo.count ? ` (${fmt.num(coo.count)} ${fmt.plural(coo.count, 'запись', 'записи', 'записей')})` : ''}`
          : 'Не подключён — нужен, чтобы скачать фото, загруженные с телефона');
        const unlimAction = ((((s.rescue || {}).sources || {}).unlim) || {}).action;
        tone('cookies', unlimAction === 'cookies' ? 'err' : coo.ok ? 'ok' : '');
        App.html(q('[data-field="cookies-buttons"]'), !withButtons ? '' : coo.ok
          ? '<button class="small" data-action="toggle-form" data-form-kind="cookies">Обновить доступ</button>' +
            '<button class="small danger" data-action="cookies-clear">Удалить доступ</button>'
          : `<button class="small primary" data-action="toggle-form" data-form-kind="cookies">${icon('image')}Подключить Безлимит</button>`);
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
      const fsp = (res.data || {}).free_space || {};
      const fat = fsp.fat ? ` Диск в формате ${fsp.fs || 'FAT32'}: файлы больше 4 ГБ на него не запишутся — лучше диск в NTFS или exFAT.` : '';
      App.text(field(root, 'folder-result'), `Сохранено.${fat}`);
      root.dispatchEvent(new CustomEvent('access-saved', { bubbles: true, detail: { kind: 'folder' } }));
      return { ok: true, data: { msg: `Папка сохранена.${fat}` } };
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
  // Уже в «Настройках» — только показываем нужное место: заход на экран заново сбросил бы
  // несохранённый выбор файлов и папок
  const toSettings = () => {
    if (App.state.route === 'settings') App.screens.settings.reveal();
    else App.go('#/settings');
  };
  App.action('go-access', (btn) => {
    App.pendingAccessFocus = btn.dataset.field;
    toSettings();
  });
  App.action('go-folders', () => {
    App.pendingFoldersFocus = true;
    toSettings();
  });
  App.action('go-setup', () => App.go('#/setup'));
  App.action('rescue-start', (btn) => App.run(btn, () => App.api.post('/api/rescue/start')));
  App.action('rescue-stop', async (btn) => {
    const yes = await App.confirm({ title: 'Остановить скачивание?', ok: 'Остановить',
      text: 'Уже скачанное сохранено. «Продолжить» докачает остальное без повторов.' });
    if (yes) App.run(btn, () => App.api.post('/api/start/stop'));
  });
  App.action('retry-now', (btn) => App.run(btn, () => App.api.post('/api/rescue/retry_now')));
  App.action('relist', (btn) => App.run(btn, () =>
    App.api.post('/api/rescue/relist', { source: btn.dataset.source })));
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
