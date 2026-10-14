/* Главная: с одного взгляда видно, идёт ли скачивание, что уже скачано, на каком этапе
   каждая часть и что делать дальше. */
(function () {
  'use strict';
  const App = window.App;
  const { esc, fmt, byId } = App;
  const icon = App.icon;
  const cap = (s) => (s ? s.charAt(0).toUpperCase() + s.slice(1) : s);

  // Столько секунд без единой строки от задач — повод сказать, что Яндекс отвечает медленно
  const STALE_SEC = 180;

  const isSetup = (r) => !!(r.action && r.action.id === 'setup');
  const nowOf = (r) => r.now || Date.now() / 1000;

  // Обе части ещё проверяют доступ или получают список — скачивание не началось
  function preparing(r) {
    const act = Object.values(r.sources || {}).filter((x) => x.enabled);
    return r.state === 'running' && act.length > 0 && act.every((x) => x.state === 'checking' || x.state === 'listing');
  }

  function heroActions(r, s) {
    const failed = (r.summary || {}).failed_files || 0;
    switch (r.state) {
      case 'idle':
        return isSetup(r)
          ? `<button class="primary big" data-action="go-setup">Начать настройку${icon('arrow')}</button>`
          : `<button class="primary big" data-action="rescue-start">${icon('download')}Сохранить мои файлы</button>`;
      case 'running':
        return `<button data-action="rescue-stop">${icon('stop')}Остановить</button>`;
      case 'waiting':
        return `<button class="primary big" data-action="retry-now">${icon('refresh')}Попробовать сейчас</button>` +
          `<button data-action="rescue-stop">${icon('stop')}Остановить</button>`;
      case 'needs_action':
        return r.running ? `<button data-action="rescue-stop">${icon('stop')}Остановить</button>` : App.actionButtons(r.action, s, true);
      case 'done':
        return `<button class="primary big" data-action="open-folder" data-which="download">${icon('folder')}Открыть папку</button>` +
          (failed ? `<button data-action="retry-failed" data-source="all">${icon('refresh')}Повторить для ${fmt.files(failed)}</button>` : '') +
          `<button data-action="relist" data-source="all">${icon('download')}Проверить новые файлы</button>`;
      case 'stopped':
        return `<button class="primary big" data-action="rescue-start">${icon('play')}Продолжить</button>`;
      default:
        return '';
    }
  }

  // Плашка состояния: живой пульс, пока программа работает
  function pill(r, offline) {
    const p = (cls, text) => `<span class="pill ${cls}"><span class="pdot"></span>${esc(text)}</span>`;
    if (offline) return p('err', 'Программа не отвечает');
    const running = r.started_at && r.running ? nowOf(r) - r.started_at : null;
    const since = running == null ? '' : running < 60 ? ' · только начали' : ` · ${fmt.duration(running)}`;
    switch (r.state) {
      case 'running': return p('live', (preparing(r) ? 'Готовлюсь к скачиванию' : 'Идёт скачивание') + since);
      case 'waiting': {
        const kinds = Object.values(r.sources || {}).filter((x) => x.state === 'waiting').map((x) => x.wait_kind);
        const what = kinds.includes('crash') ? 'Перезапуск' : kinds.length && kinds.every((k) => k === 'outage')
          ? 'Яндекс не отвечает' : 'Ждём Яндекса';
        return p('wait', `${what}${r.next_retry_at ? ` · повтор в ${fmt.hhmm(r.next_retry_at)}` : ''}`);
      }
      case 'needs_action': return p('err', 'Нужно ваше действие');
      case 'done': return p('ok', 'Готово');
      case 'stopped': return p('', 'На паузе');
      default: return isSetup(r) ? p('', 'Нужна настройка') : p('ok', 'Всё готово к запуску');
    }
  }

  // «Программа работает · последнее действие 3 сек назад»
  function heartbeat(r, offline) {
    if (offline || !r.running || !(r.state === 'running' || r.state === 'needs_action')) return '';
    const last = r.last_activity_at;
    if (!last) return `<span class="heartbeat">${icon('activity')}Запускаю…</span>`;
    const ago = nowOf(r) - last;
    if (ago > STALE_SEC && !preparing(r)) {
      return `<span class="heartbeat stale">${icon('hourglass')}Последний отклик ${esc(fmt.ago(ago, last))} — Яндекс отвечает медленно или качается большой файл</span>`;
    }
    return `<span class="heartbeat">${icon('activity')}Программа работает · последнее действие ${esc(fmt.ago(ago, last))}</span>`;
  }

  function visual(r, offline) {
    const sm = r.summary || {};
    if (offline) return `<div class="hero-art">${icon('power')}</div>`;
    if (isSetup(r)) return `<div class="hero-art">${icon('download')}</div>`;
    if (preparing(r)) {
      const found = Object.values(r.sources || {}).reduce((n, x) => n + (x.listing_found || 0), 0);
      return App.ring({ spin: true, label: found ? fmt.num(found) : '…', caption: found ? 'файлов уже найдено' : 'получаю список' });
    }
    const total = sm.selected_files || 0;
    if (r.state === 'idle' && !sm.downloaded_files) return `<div class="hero-art">${icon('download')}</div>`;
    if (r.state === 'done' && sm.complete) return `<div class="hero-art ok">${icon('check')}</div>`;
    return App.ring({ done: sm.downloaded_files || 0, wait: sm.deferred_files || 0, fail: sm.failed_files || 0, total,
      label: fmt.pctText(sm.downloaded_files || 0, total), caption: 'сохранено' });
  }

  function tile(cls, ic, label, value, sub) {
    return `<div class="stat ${cls}"><div class="s-label">${icon(ic)}${esc(label)}</div>
      <div class="s-value">${value}</div><div class="s-sub">${sub}</div></div>`;
  }

  function stats(r, s, offline) {
    if (offline) return '';
    const sm = r.summary || {};
    const src = r.sources || {};
    if (isSetup(r)) {
      return `<ol class="how">
        <li><span class="n">1</span><div><b>Подключите Диск</b>Ключ доступа — одна строка со страницы Яндекса</div></li>
        <li><span class="n">2</span><div><b>Выберите папку</b>Куда сложить файлы на этом компьютере</div></li>
        <li><span class="n">3</span><div><b>Нажмите «Сохранить»</b>Дальше программа всё сделает сама</div></li></ol>`;
    }
    if (r.state === 'idle') {
      const part = (name, ic, title) => {
        const x = src[name] || {};
        const f = x.files || {};
        const b = x.bytes || {};
        if (!x.configured) return tile('', ic, title, '—', 'не подключён');
        if (!f.total) return tile('', ic, title, '?', 'список получу при запуске');
        return tile('', ic, title, fmt.num(f.selected), `${fmt.plural(f.selected, 'файл', 'файла', 'файлов')} · ${fmt.bytes(b.selected || b.total)}`);
      };
      const free = s.free_space && s.free_space.free != null ? fmt.bytes(s.free_space.free) : '—';
      return `<div class="stats">${part('disk', 'cloud', 'Обычный Диск')}${part('unlim', 'phone', 'Безлимит')}
        ${tile('ok', 'check', 'Уже скачано', fmt.num(sm.downloaded_files || 0), fmt.bytes(sm.downloaded_bytes || 0))}
        ${tile('', 'drive', 'Свободно', free, 'на диске с папкой')}</div>`;
    }
    if (preparing(r)) {
      const part = (name, ic, title) => {
        const x = src[name] || {};
        if (!x.enabled) return '';
        return tile('run', ic, title, x.listing_found ? fmt.num(x.listing_found) : '…',
          x.state === 'checking' ? 'проверяю доступ' : 'файлов найдено, ищу дальше');
      };
      return `<div class="stats">${part('disk', 'cloud', 'Обычный Диск')}${part('unlim', 'phone', 'Безлимит')}</div>`;
    }
    const left = sm.left_files || 0;
    const etas = Object.values(src).map((x) => x.eta_sec).filter(Boolean);
    const eta = etas.length ? Math.max(...etas) : null;
    const done = tile('ok', 'check', 'Скачано', fmt.num(sm.downloaded_files || 0),
      `${fmt.bytes(sm.downloaded_bytes || 0)} · из ${fmt.files(sm.selected_files || 0)}`);
    const wait = tile('wait', 'clock', 'Ждут Яндекса', fmt.num(sm.deferred_files || 0), sm.deferred_files
      ? (r.next_retry_at ? `повтор в ${fmt.hhmm(r.next_retry_at)}` : 'программа повторит сама') : 'таких пока нет');
    const queue = tile('', 'list', 'В очереди', fmt.num(left), left ? (eta ? `осталось ≈ ${fmt.duration(eta)}` : 'ждут своей очереди') : 'очередь пуста');
    let fourth;
    const rc = r.recent;
    if (r.state === 'running') {
      fourth = rc
        ? tile('run', 'activity', cap(fmt.lastMinutes(rc.window_sec)), `+${fmt.num(rc.checked)}`,
          `скачано ${fmt.num(rc.done)}, отложено ${fmt.num(Math.max(0, rc.checked - rc.done))}`)
        : tile('run', 'activity', 'Скорость', '…', 'считаю — нужно полминуты');
    } else if (sm.failed_files) {
      fourth = tile('err', 'x', 'Не скачались', fmt.num(sm.failed_files), 'можно повторить');
    } else if (sm.preview_only_files) {
      fourth = tile('violet', 'image', 'Есть превью', fmt.num(sm.preview_only_files), 'уменьшенные копии фото');
    } else {
      fourth = tile('', 'folder', 'В папке', fmt.bytes(((src.disk || {}).folder || {}).size_bytes || 0), 'уже на вашем диске');
    }
    return `<div class="stats">${done}${wait}${queue}${fourth}</div>`;
  }

  function folderNames(folders) {
    const names = folders.map((p) => p.replace(/^disk:\/*/, ''));
    return names.slice(0, 2).join(', ') + (names.length > 2 ? ` и ещё ${names.length - 2}` : '');
  }

  function whatLine(s) {
    const st = s.settings || {};
    const labels = st.category_labels || {};
    const folders = st.disk_folders && st.disk_folders.length ? st.disk_folders : null;
    const sources = (st.sources || ['disk', 'unlim']).map((x) => (x !== 'disk' ? 'Безлимит'
      : folders ? `Диск (папки: ${folderNames(folders)})` : 'Диск')).join(' и ');
    const cats = st.categories ? st.categories.map((c) => labels[c] || c).join(', ') : 'все файлы';
    return `Что скачиваем: ${esc(sources)} — ${esc(cats)}${st.gentle ? ' · бережный режим' : ''} · <a href="#/settings">изменить</a>`;
  }

  App.screens.home = {
    mount(el) {
      el.innerHTML = `<div id="home-banner"></div>
        <section class="card hero" id="home-hero">
          <div class="hero-top"><span id="home-pill"></span><span class="spacer"></span><span id="home-beat"></span></div>
          <div class="hero-body">
            <div id="home-visual"></div>
            <div>
              <h1 id="home-headline">Загружаю…</h1>
              <p class="sub" id="home-subline"></p>
              <div class="row actions" id="home-actions"></div>
            </div>
          </div>
          <div id="home-stats"></div>
          <p class="hero-what" id="home-what"></p>
        </section>
        <div class="mt" id="home-notice"></div>
        <div class="grid-2 mt" id="home-sources"></div>
        <div class="grid-2 mt" id="home-live"></div>
        <div class="mt" id="home-where"></div>
        <div class="mt" id="home-safety"></div>`;
    },
    update(s) {
      const r = s.rescue || {};
      const offline = App.state.offline;
      const setup = isSetup(r);
      App.html(byId('home-banner'), offline ? '' : App.actionBanner(r.action, s));
      byId('home-hero').className = `card hero state-${offline ? 'needs_action' : r.state || 'idle'}`;
      App.html(byId('home-pill'), pill(r, offline));
      App.html(byId('home-beat'), heartbeat(r, offline));
      App.html(byId('home-visual'), visual(r, offline));
      App.text(byId('home-headline'), offline ? 'Программа не отвечает' : r.headline || '');
      App.text(byId('home-subline'), offline
        ? 'Похоже, окно программы закрыто. Запустите её снова — всё скачанное сохранено, она продолжит с того же места.'
        : r.subline || '');
      App.html(byId('home-actions'), offline ? '' : heroActions(r, s));
      App.html(byId('home-stats'), stats(r, s, offline));
      App.html(byId('home-what'), setup || offline ? '' : whatLine(s));
      byId('home-what').classList.toggle('hidden', setup || offline);
      App.html(byId('home-notice'), offline ? '' : App.noticeBanner(r.notice));
      const src = r.sources || {};
      App.html(byId('home-sources'), setup ? '' : App.sourceBlock('disk', src.disk, r) + App.sourceBlock('unlim', src.unlim, r));
      const cards = [r.started_at ? App.feedCard(r) : '', setup ? '' : App.recentCard(r)].filter(Boolean);
      App.html(byId('home-live'), cards.join(''));
      byId('home-live').classList.toggle('single', cards.length === 1);
      App.html(byId('home-where'), setup ? '' : App.whereFiles(s));
      const showSafety = r.state !== 'idle' && !((r.summary || {}).complete);
      App.html(byId('home-safety'), showSafety ? App.safetyBanner() : '');
    },
  };
})();
