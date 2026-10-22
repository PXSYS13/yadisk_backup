/* По ссылке: скачать папку или файл по публичной ссылке Яндекс Диска (disk.yandex.ru/d/…). */
(function () {
  'use strict';
  const App = window.App;
  const { esc, fmt, byId } = App;
  const icon = App.icon;
  // Столько секунд без новых строк — «последний отклик давно», как на Главной
  const STALE_SEC = 180;

  const BADGE = {
    listing: ['run', 'список файлов'], downloading: ['run', 'качается'], waiting: ['wait', 'ждёт Яндекса'],
    limited: ['wait', 'лимит Яндекса'], paused: ['', 'на паузе'], done: ['ok', 'готово'],
    done_partial: ['wait', 'готово не всё'], error: ['err', 'не работает'],
  };
  const ICON_BOX = { error: 'err', done: 'ok', done_partial: 'wait', waiting: 'wait', limited: 'wait' };

  function progressHtml(it) {
    const f = it.files;
    if (!f.total) return '';
    const total = Math.max(f.total, 1);
    const pct = (n) => `${((100 * n) / total).toFixed(2)}%`;
    const waiting = f.deferred + f.limited;
    const legend = [`<span><i class="l-done"></i>Скачано ${fmt.num(f.done)} из ${fmt.files(f.total)}${
      it.bytes.total ? ` · ${fmt.bytes(it.bytes.done)} из ${fmt.bytes(it.bytes.total)}` : ''}</span>`];
    if (waiting) legend.push(`<span><i class="l-wait"></i>ждут Яндекса ${fmt.num(waiting)}</span>`);
    if (f.queue) legend.push(`<span><i class="l-pend"></i>в очереди ${fmt.num(f.queue)}</span>`);
    if (f.failed) legend.push(`<span><i class="l-fail"></i>не скачались ${fmt.num(f.failed)}</span>`);
    return `<div class="bar"><span class="done" style="width:${pct(f.done)}"></span>` +
      `<span class="wait" style="width:${pct(waiting)}"></span><span class="fail" style="width:${pct(f.failed)}"></span></div>` +
      `<div class="legend">${legend.join('')}</div>`;
  }

  function buttonsHtml(it) {
    const b = [`<button class="small" data-action="links-open" data-id="${it.id}">${icon('folder')}Открыть папку</button>`];
    if (['waiting', 'limited', 'error', 'done_partial', 'paused'].includes(it.state)) {
      b.push(`<button class="small" data-action="links-retry-now" data-id="${it.id}">${icon('refresh')}Попробовать сейчас</button>`);
    }
    if (it.state === 'done' || it.state === 'done_partial') {
      b.push(`<button class="small" data-action="links-relist" data-id="${it.id}">${icon('list')}Проверить новые файлы</button>`);
    }
    b.push(`<button class="small ghost" data-action="links-remove" data-id="${it.id}" data-name="${esc(it.name)}">` +
      `${icon('trash')}Убрать из списка</button>`);
    return b.join('');
  }

  // Карточки по id: обновляются только изменившиеся части — кнопки не пересоздаются на каждом опросе
  function paintList(items) {
    const list = byId('links-list');
    const ids = new Set(items.map((it) => String(it.id)));
    for (const el of [...list.querySelectorAll('[data-link]')]) if (!ids.has(el.dataset.link)) el.remove();
    let prev = null;
    for (const it of items) {
      let el = list.querySelector(`[data-link="${it.id}"]`);
      if (!el) {
        el = document.createElement('div');
        el.className = 'link-item';
        el.dataset.link = String(it.id);
        el.innerHTML = `<div class="source-head"><span class="icon-box" data-field="icon"></span>
          <div class="grow"><div class="source-title" data-field="name"></div><div class="source-sub" data-field="status"></div></div>
          <span data-field="badge"></span></div><div class="link-progress" data-field="progress"></div>
          <div class="row" data-field="buttons"></div>`;
      }
      const want = prev ? prev.nextElementSibling : list.firstElementChild;
      if (want !== el) list.insertBefore(el, want);
      prev = el;
      const part = (name) => el.querySelector(`[data-field="${name}"]`);
      const boxCls = `icon-box ${ICON_BOX[it.state] || ''}`.trim();
      if (part('icon').className !== boxCls) part('icon').className = boxCls;
      App.html(part('icon'), icon(it.kind === 'file' ? 'file' : 'folder'));
      App.text(part('name'), it.name);
      App.text(part('status'), it.status_text);
      const [cls, label] = BADGE[it.state] || ['', it.state];
      App.html(part('badge'), `<span class="badge ${cls}">${esc(label)}</span>`);
      App.html(part('progress'), progressHtml(it));
      App.html(part('buttons'), buttonsHtml(it));
    }
    byId('links-empty').classList.toggle('hidden', items.length > 0);
  }

  function beatHtml(L, now) {
    if (L.state === 'waiting') {
      return `<span class="heartbeat">${icon('clock')}Ждём Яндекса${L.next_retry_at ? ` — следующая попытка в ${esc(fmt.hhmm(L.next_retry_at))}` : ''}</span>`;
    }
    if (!L.running) return L.state === 'paused' ? `<span class="heartbeat">${icon('hourglass')}На паузе</span>` : '';
    if (!L.last_activity_at) return `<span class="heartbeat">${icon('activity')}Запускаю…</span>`;
    const ago = now - L.last_activity_at;
    if (ago > STALE_SEC) {
      return `<span class="heartbeat stale">${icon('hourglass')}Последний отклик ${esc(fmt.ago(ago, L.last_activity_at))} — ` +
        'Яндекс отвечает медленно или качается большой файл</span>';
    }
    return `<span class="heartbeat">${icon('activity')}Программа работает · последнее действие ${esc(fmt.ago(ago, L.last_activity_at))}</span>`;
  }

  function actionsHtml(L) {
    if (L.running) {
      return (L.state === 'waiting' ? `<button class="small" data-action="links-retry-now">${icon('refresh')}Попробовать сейчас</button>` : '') +
        `<button class="small" data-action="links-stop">${icon('stop')}Остановить</button>`;
    }
    return L.state === 'paused' ? `<button class="small primary" data-action="links-resume">${icon('play')}Продолжить</button>` : '';
  }

  App.screens.links = {
    mount(el) {
      el.innerHTML = `<div class="page-head"><h1>По ссылке</h1>
          <p>Прислали ссылку на папку или альбом Яндекс Диска? Программа скачает файлы по одному — даже сейчас, когда
          «Скачать всё» в браузере не работает. Ключ доступа не нужен.</p></div>
        <div class="stack">
        <div class="card"><div class="card-head"><span class="icon-box">${icon('link')}</span>
            <div class="grow"><h2>Добавить ссылку</h2>
            <span class="tiny">Ссылка на папку, подпапку, один файл или альбом — disk.yandex.ru/d/… или /a/…</span></div></div>
          <form class="link-form" id="links-form" autocomplete="off">
            <label class="sr-only" for="links-url">Публичная ссылка Яндекс Диска</label>
            <input type="text" id="links-url" spellcheck="false" inputmode="url" placeholder="https://disk.yandex.ru/d/…">
            <button class="primary" data-action="links-add" id="links-add-btn">${icon('download')}Скачать</button>
          </form>
          <p class="tiny" id="links-note" style="margin-top:10px">Файлы лягут в папку «_ссылки» внутри папки скачивания, с теми же
            подпапками, что по ссылке. В альбомах к именам добавляется ID фото, чтобы сохранить файлы с одинаковыми именами.
            Ссылки с паролем пока не поддерживаются.</p></div>
        <div class="card"><div class="card-head"><span class="icon-box info">${icon('list')}</span>
            <div class="grow"><h2>Ссылки</h2><span id="links-beat"></span></div>
            <div class="row" id="links-actions"></div></div>
          <div id="links-problem"></div>
          <p class="empty" id="links-empty">${icon('info')}Пока нет ссылок — вставьте ссылку выше и нажмите «Скачать».</p>
          <div class="stack" id="links-list"></div></div>
        <div class="card" id="links-where"><div class="card-head"><span class="icon-box plain">${icon('folder')}</span>
            <div class="grow"><h3>Куда скачивается</h3></div></div>
          <div class="path-box">${icon('drive')}<code id="links-folder"></code></div>
          <div class="row" style="margin-top:14px">
            <button data-action="links-open">${icon('folder')}Открыть папку</button>
            <button data-action="copy-path" id="links-copy">${icon('copy')}Скопировать путь</button>
          </div></div>
        </div>`;
      // Enter в поле ссылки нажимает «Скачать» сам (кнопка — первая в форме); отправку формы гасим
      byId('links-form').addEventListener('submit', (e) => e.preventDefault());
    },
    update(s) {
      const L = s.links || { items: [], state: 'idle' };
      const now = (s.rescue && s.rescue.now) || Date.now() / 1000;
      App.html(byId('links-beat'), beatHtml(L, now));
      App.html(byId('links-actions'), actionsHtml(L));
      App.html(byId('links-problem'), L.problem ? `<div class="banner action"><span class="b-icon">${icon('alert')}</span><div>
          <h3>${esc(L.problem.text)}</h3><p>Освободите место или выберите другую папку — скачивание продолжится с того же места.</p>
          <div class="row actions"><button class="primary" data-action="links-resume">${icon('play')}Попробовать снова</button>
          <button data-action="go-access" data-field="folder">${icon('folder')}Выбрать другую папку</button></div></div></div>` : '');
      paintList(L.items || []);
      App.text(byId('links-folder'), L.folder || '');
      byId('links-copy').dataset.path = L.folder || '';
    },
  };

  App.action('links-add', (btn) => {
    const input = byId('links-url');
    const url = input.value.trim();
    if (!url) {
      App.toast('Вставьте ссылку на папку или файл Яндекс Диска', 'err');
      input.focus();
      return;
    }
    App.run(btn, async () => {
      const res = await App.api.post('/api/links/add', { url });
      if (res.ok) input.value = '';
      return res;
    });
  });

  App.action('links-open', (btn) => App.run(btn, () => App.api.post('/api/open_folder',
    btn.dataset.id ? { which: 'links', link_id: Number(btn.dataset.id) } : { which: 'links' })));

  App.action('links-retry-now', (btn) => App.run(btn, () => App.api.post('/api/links/retry_now',
    btn.dataset.id ? { id: Number(btn.dataset.id) } : {})));

  App.action('links-relist', (btn) => App.run(btn, () => App.api.post('/api/links/relist', { id: Number(btn.dataset.id) })));

  App.action('links-resume', (btn) => App.run(btn, () => App.api.post('/api/links/resume')));

  App.action('links-stop', async (btn) => {
    const yes = await App.confirm({ title: 'Остановить скачивание по ссылкам?', ok: 'Остановить',
      text: 'Уже скачанное сохранено. «Продолжить» докачает остальное.' });
    if (yes) App.run(btn, () => App.api.post('/api/links/stop'));
  });

  App.action('links-remove', async (btn) => {
    const yes = await App.confirm({ title: `Убрать «${btn.dataset.name || 'ссылку'}» из списка?`, ok: 'Убрать из списка',
      text: 'Файлы, которые уже скачались, останутся в папке. Программа просто перестанет качать эту ссылку.' });
    if (yes) App.run(btn, () => App.api.post('/api/links/remove', { id: Number(btn.dataset.id) }));
  });
})();
