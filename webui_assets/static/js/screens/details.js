/* Подробнее о Диске или Безлимите: счётчики, что не скачалось и почему, последние файлы. */
(function () {
  'use strict';
  const App = window.App;
  const { esc, fmt, byId } = App;

  let source = 'disk';
  let problems = null;
  let problemsAt = 0;

  async function loadProblems() {
    problemsAt = Date.now();
    const res = await App.api.get(`/api/${source}/failed?limit=300`);
    problems = res.ok ? res.data : { items: [], reasons: {}, total: 0, error: res.error };
    paintProblems();
  }

  function paintProblems() {
    const box = byId('det-problems');
    if (!box) return;
    if (!problems) { App.html(box, '<p class="muted">Загружаю…</p>'); return; }
    if (!problems.total) {
      App.html(box, `<div class="empty">${App.icon('check')}Всё, что программа уже пробовала, скачалось.</div>`);
      return;
    }
    const reasons = Object.entries(problems.reasons || {}).sort((a, b) => b[1] - a[1])
      .map(([text, n]) => `<li>${esc(text)} — <b>${fmt.files(n)}</b></li>`).join('');
    const failed = problems.items.filter((x) => x.state === 'failed').length;
    const rows = problems.items.map((x) => `<tr>
        <td class="name">${esc(x.name)}${x.preview ? ' <span class="badge">есть превью</span>' : ''}</td>
        <td>${fmt.bytes(x.size)}</td>
        <td><span class="badge ${x.state === 'deferred' ? 'wait' : 'err'}">${x.state === 'deferred' ? 'ждёт Яндекса' : 'не скачался'}</span></td>
        <td class="small muted">${esc(x.state === 'deferred' ? 'сервер Яндекса не отвечает' : x.reason)}</td></tr>`).join('');
    App.html(box, `<ul class="reasons">${reasons}</ul>
      ${failed ? `<div class="row" style="margin-bottom:14px"><button class="primary" data-action="retry-failed" data-source="${source}">${App.icon('refresh')}Повторить не скачавшиеся</button>
        <span class="tiny">Файлы, которые ждут Яндекса, программа повторяет сама.</span></div>` : ''}
      <div class="scroll"><table class="list"><thead><tr><th>Файл</th><th>Размер</th><th>Состояние</th><th>Почему</th></tr></thead>
      <tbody>${rows}</tbody></table></div>
      ${problems.total > problems.items.length ? `<p class="tiny" style="margin-top:8px">Показаны первые ${problems.items.length} из ${fmt.num(problems.total)}.</p>` : ''}`);
  }

  // ── «Папки»: какие папки Диска уже скачаны целиком — их можно переносить на другой диск ──
  let fpPath = 'disk:/';
  let folders = null;
  let foldersAt = 0;

  async function loadFolders(path) {
    if (path !== undefined) fpPath = path;
    foldersAt = Date.now();
    const want = fpPath;
    const res = await App.api.get(`/api/disk/folder_progress?path=${encodeURIComponent(want)}${App.demo ? '&demo=1' : ''}`);
    if (want !== fpPath) return; // пока ждали ответа, открыли другую папку
    folders = res.ok ? res.data : { error: res.error || 'Не получилось загрузить папки' };
    paintFolders();
  }

  function crumbs(path) {
    const parts = path.replace(/^disk:\/*/, '').split('/').filter(Boolean);
    if (!parts.length) return '<nav class="crumbs" aria-label="Папка"><b>Весь Диск</b></nav>';
    let acc = 'disk:';
    const items = parts.map((p, i) => {
      acc += `/${p}`;
      return i === parts.length - 1 ? `<b>${esc(p)}</b>`
        : `<button class="link" data-action="fp-open" data-path="${esc(acc)}">${esc(p)}</button>`;
    });
    return `<nav class="crumbs" aria-label="Папка"><button class="link" data-action="fp-open" data-path="disk:/">Весь Диск</button>` +
      items.map((x) => `<span aria-hidden="true">›</span>${x}`).join('') + '</nav>';
  }

  function folderBadge(f) {
    if (!f.files) return ['', 'не выбрана'];
    if (f.complete) return ['ok', 'скачана целиком'];
    if (f.failed) return ['err', `не скачались: ${fmt.num(f.failed)}`];
    if (f.deferred) return ['wait', `ждут Яндекса: ${fmt.num(f.deferred)}`];
    return ['', `в очереди: ${fmt.num(f.waiting)}`];
  }

  function folderRow(f, title) {
    const [cls, text] = folderBadge(f);
    const seg = (c, n) => `<span class="${c}" style="width:${fmt.pct(n, f.files).toFixed(2)}%"></span>`;
    const bar = f.files ? `<div class="bar thin" role="img" aria-label="Скачано ${f.done} из ${f.files}">${
      seg('done', f.done)}${seg('work', f.waiting)}${seg('wait', f.deferred)}${seg('fail', f.failed)}</div>` : '';
    const label = `${App.icon(title ? 'file' : 'folder')}<span class="fp-label">${esc(title || f.name)}</span>`;
    const name = f.has_sub
      ? `<button class="link fp-name" data-action="fp-open" data-path="${esc(f.path)}" title="Показать папки внутри">${label}${App.icon('chevron')}</button>`
      : `<span class="fp-name">${label}</span>`;
    const meta = f.files ? `${fmt.num(f.done)} из ${fmt.files(f.files)} · ${fmt.bytes(f.bytes_done)} из ${fmt.bytes(f.bytes)}`
      : `${fmt.files(f.excluded)} — не выбраны в «Настройках»`;
    let tail = '';
    if (f.complete && f.on_disk) {
      tail = `<button class="small ghost" data-action="open-disk-folder" data-path="${esc(f.path)}">${App.icon('folder')}Открыть папку</button>`;
    } else if (f.complete && f.on_disk === false) {
      tail = '<span class="tiny">в папке скачивания её уже нет</span>';
    }
    return `<li class="fp-row"><div class="fp-top">${name}<span class="badge ${cls}">${esc(text)}</span></div>${bar}
      <div class="fp-meta"><span>${meta}</span>${tail}</div></li>`;
  }

  function paintFolders() {
    const box = byId('det-folders');
    if (!box) return;
    if (!folders) { App.html(box, '<p class="muted">Загружаю…</p>'); return; }
    if (folders.error) { App.html(box, `<p class="muted">${esc(folders.error)}</p>`); return; }
    const root = (folders.path || fpPath) === 'disk:/';
    const head = crumbs(folders.path || fpPath);
    if (folders.empty) {
      App.html(box, `${head}<div class="empty">${App.icon('hourglass')}${root
        ? 'Список файлов Диска ещё не собран — папки появятся после запуска.' : 'В этой папке нет файлов из списка.'}</div>`);
      return;
    }
    const sum = !folders.total ? '' : folders.complete === folders.total
      ? `<p class="fp-sum ok">${App.icon('check')}<span>Все папки здесь скачаны целиком</span></p>`
      : `<p class="fp-sum"><span>Скачаны целиком: <b>${fmt.num(folders.complete)}</b> из ${fmt.num(folders.total)} ${
        fmt.plural(folders.total, 'папки', 'папок', 'папок')}</span></p>`;
    const rows = (folders.folders || []).map((f) => folderRow(f)).join('') +
      (folders.here ? folderRow(folders.here, root ? 'Файлы в корне Диска' : 'Файлы прямо в этой папке') : '');
    const more = folders.more ? `<p class="tiny">И ещё ${fmt.num(folders.more)} — показаны первые ${fmt.num((folders.folders || []).length)} по алфавиту.</p>` : '';
    App.html(box, `${head}${sum}<ul class="fp-list">${rows}</ul>${more}`);
  }

  function taskLog(s) {
    const tasks = s.tasks || {};
    const keys = source === 'disk' ? ['disk'] : Object.keys(tasks).filter((k) => k.startsWith('unlim')).sort();
    const lines = [];
    for (const k of keys) {
      const t = tasks[k];
      if (t && t.log_tail && t.log_tail.length) lines.push(`── ${k} ──`, ...t.log_tail.slice(-40));
    }
    return lines.join('\n') || 'Пока пусто — журнал появится, когда начнётся скачивание.';
  }

  App.screens.details = {
    mount(el) {
      const head = (ic, cls, title, sub) => `<div class="card-head"><span class="icon-box ${cls}">${App.icon(ic)}</span>
        <div class="grow"><h2>${title}</h2>${sub ? `<span class="tiny">${sub}</span>` : ''}</div></div>`;
      el.innerHTML = `<a class="back" href="#/home">${App.icon('back')}На главную</a>
        <div class="page-head"><h1 id="det-title">Подробнее</h1><p id="det-sub"></p></div>
        <div class="stack">
          <div class="card"><div id="det-steps"></div><div class="counters" id="det-counters" style="margin-top:18px"></div>
            <p class="small muted" id="det-hosts" style="margin-top:14px"></p></div>
          <div class="card" id="det-folders-card">${head('folder', 'ok', 'Папки',
            'Скачанную целиком папку можно перенести на другой диск — программа помнит, что уже скачала, и не будет качать её заново')}
            <div id="det-folders"></div></div>
          <div class="card">${head('alert', 'wait', 'Что не скачалось и почему', 'Файлы, которые ждут Яндекса, программа повторяет сама')}<div id="det-problems"></div></div>
          <div class="card">${head('check', 'ok', 'Последние скачанные', 'Эти файлы уже лежат в вашей папке')}<div id="det-recent"></div></div>
          <div class="card"><details><summary>Для опытных: журнал и пересбор списка</summary><div class="body">
            <div class="row" style="margin-bottom:12px">
              <button data-action="details-relist" id="det-relist">${App.icon('refresh')}Собрать список файлов заново</button>
              <a class="btn" href="/api/logs/bundle">${App.icon('download')}Скачать отчёт для поддержки</a></div>
            <div class="console" id="det-log"></div></div></details></div>
        </div>`;
    },
    show(params) {
      source = params.source === 'unlim' ? 'unlim' : 'disk';
      problems = null;
      App.text(byId('det-title'), App.SOURCE_NAMES[source]);
      paintProblems();
      loadProblems();
      // У Безлимита папок нет: все фото лежат в одной папке «_unlim»
      byId('det-folders-card').classList.toggle('hidden', source !== 'disk');
      if (source === 'disk') {
        folders = null;
        paintFolders();
        loadFolders('disk:/');
      }
    },
    update(s) {
      const r = s.rescue || {};
      const src = (r.sources || {})[source] || {};
      const f = src.files || {};
      App.text(byId('det-sub'), source === 'disk' ? 'Ваши папки и файлы с Диска — по ключу доступа.'
        : 'Фото и видео, которые телефон загружал в безлимит, — по доступу к Безлимиту.');
      App.html(byId('det-steps'), r.started_at ? App.steps(src) : '');
      const c = (cls, v, l) => `<div class="counter ${cls}"><div class="v">${fmt.num(v)}</div><div class="l">${l}</div></div>`;
      App.html(byId('det-counters'), c('', f.selected, 'выбрано') + c('ok', f.done, 'скачано') +
        c('', (f.pending || 0) + (f.in_progress || 0), 'в очереди') + c(f.deferred ? 'wait' : '', f.deferred, 'ждут Яндекса') +
        c(f.failed ? 'err' : '', f.failed, 'не скачались') + c('', f.excluded, 'не выбраны') + c('', f.preview_only, 'есть превью'));
      App.text(byId('det-hosts'), src.storage_hosts_down
        ? `Файлы, которые ждут Яндекса, лежат на ${fmt.num(src.storage_hosts_down)} серверах хранения, которые сейчас не отвечают. Программа повторяет их сама.`
        : '');
      const db = (s[source] || {}).db || {};
      const recent = (db.recent || []).filter((x) => x.status === 'downloaded');
      App.html(byId('det-recent'), recent.length
        ? `<table class="list"><tbody>${recent.map((x) => `<tr><td class="name">${esc(String(x.path).split('/').pop())}</td>
            <td>${fmt.bytes(x.size)}</td><td class="tiny">${x.at ? fmt.hhmm(x.at) : ''}</td></tr>`).join('')}</tbody></table>`
        : `<div class="empty">${App.icon('hourglass')}Пока ничего.</div>`);
      const running = !!(s.pipeline && s.pipeline.running);
      byId('det-relist').disabled = running;
      App.text(byId('det-log'), taskLog(s));
      if (Date.now() - problemsAt > 20000) loadProblems();
      if (source === 'disk' && Date.now() - foldersAt > 30000) loadFolders();
    },
  };

  App.action('fp-open', (btn) => {
    folders = null;
    fpPath = btn.dataset.path || 'disk:/';
    paintFolders();
    loadFolders();
  });
  App.action('open-disk-folder', (btn) => App.run(btn, () =>
    App.api.post('/api/open_folder', { which: 'disk_folder', path: btn.dataset.path })));

  App.action('details-relist', async (btn) => {
    const yes = await App.confirm({ title: 'Собрать список файлов заново?', ok: 'Собрать',
      text: 'Программа заново спросит у Яндекса, какие файлы есть. Скачанное не пропадёт и второй раз качаться не будет.' });
    if (yes) App.run(btn, () => App.api.post(source === 'disk' ? '/api/disk/scan' : '/api/unlim/collect'));
  });
})();
