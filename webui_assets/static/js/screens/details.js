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
    const want = source;
    const res = await App.api.get(`/api/${want}/failed?limit=300`);
    if (want !== source) return; // пока ждали ответа, открыли другой источник
    const data = res.ok && res.data ? res.data : null;
    const error = data ? data.error : res.error || 'Не получилось';
    problems = error ? { items: [], reasons: {}, total: 0, failed: 0, error } : data;
    paintProblems();
  }

  function paintProblems() {
    const box = byId('det-problems');
    if (!box) return;
    if (!problems) { App.html(box, '<p class="muted">Загружаю…</p>'); return; }
    // Не «всё скачалось»: список просто не загрузился, программа спросит снова через 20 секунд
    if (problems.error) { App.html(box, `<p class="muted">Не получилось загрузить список: ${esc(problems.error)}</p>`); return; }
    if (!problems.total) {
      App.html(box, `<div class="empty">${App.icon('check')}Всё, что программа уже пробовала, скачалось.</div>`);
      return;
    }
    const reasons = Object.entries(problems.reasons || {}).sort((a, b) => b[1] - a[1])
      .map(([text, n]) => `<li>${esc(text)} — <b>${fmt.files(n)}</b></li>`).join('');
    // Все упавшие, а не только из первых строк: за тысячами отложенных их могло не быть видно
    const failed = problems.failed || 0;
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
    const meta = f.files ? `${fmt.num(f.done)} из ${fmt.filesOf(f.files)} · ${fmt.bytes(f.bytes_done)} из ${fmt.bytes(f.bytes)}`
      : `${fmt.files(f.excluded)} — ${fmt.plural(f.excluded, 'не выбран', 'не выбраны', 'не выбраны')} в «Настройках»`;
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

  // ── «Проверка скачанных файлов»: на месте ли они и совпадают ли с Яндексом ──
  // Проверка — задача в слоте своего источника (main.py verify, unlim_download.py --verify);
  // ход и итог — строки «verify_progress | {json}» и «verify_summary | {json}» в её журнале
  const VERIFY = { disk: { slot: 'disk', kind: 'disk_verify' }, unlim: { slot: 'unlim_collect', kind: 'unlim_verify' } };
  const VERIFY_KINDS = [
    ['missing', 'Нет в папке', 'нет в папке'],
    ['size', 'Размер не совпадает с Яндексом', 'другой размер'],
    ['md5', 'Содержимое не совпадает с Яндексом (MD5)', 'другое содержимое'],
    ['unreadable', 'Не удалось прочитать', 'не прочитать'],
  ];
  // Источник → конец проверки, после которой её файлы уже вернули в очередь
  const requeued = {};
  const filesGen = (n) => `${fmt.num(n)} ${fmt.plural(n, 'файла', 'файлов', 'файлов')}`;
  const shownPath = (p) => String(p || '').replace(/^disk:/, '').replace(/^\/?photounlim[:/]\/?/, '');

  function lastJson(lines, marker) {
    for (let i = lines.length - 1; i >= 0; i--) {
      const at = lines[i].indexOf(marker);
      if (at < 0) continue;
      try { return JSON.parse(lines[i].slice(at + marker.length)); } catch (e) { return null; }
    }
    return null;
  }

  // App.html сравнивает разметку с innerHTML, а разметка с SVG-иконками обратно не совпадает:
  // кнопки перерисовывались бы на каждом опросе, и нажатие могло потеряться
  function paintOnce(el, markup) {
    if (el && el._markup !== markup) {
      el._markup = markup;
      el.innerHTML = markup;
    }
  }

  function verifyRunning(p) {
    let text = 'Готовлюсь к проверке…';
    let pct = 0;
    const tail = [];
    if (p && p.stage === 'size') {
      text = `Смотрю, все ли файлы на месте: ${fmt.num(p.done)} из ${fmt.files(p.total)}`;
      pct = fmt.pct(p.done, p.total);
    } else if (p && p.stage === 'md5') {
      text = `Сверяю содержимое с Яндексом (MD5): ${fmt.bytes(p.bytes_done)} из ${fmt.bytes(p.bytes_total)}`;
      pct = fmt.pct(p.bytes_done, p.bytes_total);
      tail.push(fmt.pctText(p.bytes_done, p.bytes_total));
      if (p.eta_sec) tail.push(`осталось ≈ ${fmt.duration(p.eta_sec)}`);
    }
    if (p) tail.push(p.found ? `уже нашлось проблем: ${fmt.num(p.found)}` : 'пока всё в порядке');
    return `<div class="activity live">${App.icon('activity')}<span>${esc(text)}</span></div>
      <div class="bar verify-bar" role="img" aria-label="${esc(text)}"><span class="done" style="width:${pct.toFixed(2)}%"></span></div>
      ${tail.length ? `<p class="tiny">${esc(tail.join(' · '))}</p>` : ''}
      <p class="tiny">На большой папке это долго: программа читает каждый файл целиком. Скачивание в это время не идёт.</p>`;
  }

  function verifyResult(sum, task) {
    const when = task.finished_at ? `Проверка закончилась в ${fmt.hhmm(task.finished_at)}, заняла ${fmt.duration(sum.seconds)}.` : '';
    // Старые версии в режиме ZIP не записывали, куда сохранили файл, — такие проверить нечем
    const unchecked = sum.unchecked ? ` У ${filesGen(sum.unchecked)} программа старой версии не записала, куда их сохранила, — их проверить нельзя.` : '';
    if (!sum.checked) {
      return `<div class="empty">${App.icon('hourglass')}${esc(unchecked.trim() || 'Скачанных файлов пока нет — проверять нечего.')}</div>`;
    }
    const bad = (sum.size || 0) + (sum.md5 || 0);
    const noMd5 = sum.no_md5 ? ` У ${filesGen(sum.no_md5)} Яндекс не сообщил контрольную сумму — у них проверены только наличие и размер.` : '';
    if (!sum.missing && !bad && !sum.unreadable) {
      return `<p class="verify-head ok">${App.icon('check')}<span>Проверено ${fmt.files(sum.checked)} — всё в порядке</span></p>
        <p class="small muted">Все файлы на месте, размер и содержимое совпадают с Яндексом.${esc(noMd5 + unchecked)}</p>
        ${when ? `<p class="tiny verify-when">${esc(when)}</p>` : ''}`;
    }
    const parts = [sum.missing ? `не хватает ${filesGen(sum.missing)}` : '', bad ? `повреждено ${fmt.num(bad)}` : '',
      sum.unreadable ? `не удалось прочитать ${fmt.num(sum.unreadable)}` : ''].filter(Boolean).join(', ');
    const items = VERIFY_KINDS.filter(([k]) => sum[k]).map(([k, title]) => `<li>${esc(title)} — <b>${fmt.files(sum[k])}</b></li>`).join('');
    const notes = [];
    if (sum.all_missing) {
      notes.push('Программа не нашла на месте ни одного скачанного файла. Скорее всего, диск с папкой отключён или папку перенесли: подключите диск и проверьте ещё раз. Если вы перенесли файлы сами, качать их заново не нужно.');
    } else if (sum.missing) {
      notes.push('Файлов нет там, куда программа их скачала. Если вы сами перенесли или удалили их — всё в порядке, качать их заново не нужно.');
    }
    if (bad) notes.push('Повреждённые файлы отличаются от тех, что на Диске. Их лучше скачать заново: новая копия заменит испорченную.');
    if (sum.unreadable) notes.push('Файлы, которые не удалось прочитать, могут быть открыты в другой программе. Закройте её и проверьте ещё раз.');
    return `<p class="verify-head err">${App.icon('alert')}<span>Проверено ${fmt.files(sum.checked)}. ${esc(parts.charAt(0).toUpperCase() + parts.slice(1))}</span></p>
      <ul class="reasons">${items}</ul>
      <p class="safety-note">${App.icon('alert')}<span>${esc(notes.join(' '))}</span></p>
      ${noMd5 || unchecked ? `<p class="tiny">${esc((noMd5 + unchecked).trim())}</p>` : ''}
      ${when ? `<p class="tiny verify-when">${esc(when)}</p>` : ''}`;
  }

  function verifyList(sum) {
    const ex = sum.examples || {};
    const rows = VERIFY_KINDS.flatMap(([k, , short]) => (ex[k] || []).map((x) => {
      const detail = k === 'size' ? `на диске ${fmt.bytes(x.local_size)}, у Яндекса ${fmt.bytes(x.size)}`
        : k === 'unreadable' ? (x.error || '') : '';
      return `<tr><td class="name">${esc(shownPath(x.path))}</td>
        <td><span class="badge ${k === 'unreadable' ? 'wait' : 'err'}">${esc(short)}</span>${
          detail ? `<div class="tiny">${esc(detail)}</div>` : ''}</td></tr>`;
    }));
    if (!rows.length) return '';
    const cut = VERIFY_KINDS.some(([k]) => (sum[k] || 0) > (ex[k] || []).length);
    return `<details class="verify-list"><summary>Какие это файлы${cut ? ' — первые 50 каждого вида' : ''}</summary><div class="body">
      <div class="scroll"><table class="list"><thead><tr><th>Файл</th><th>Что не так</th></tr></thead>
      <tbody>${rows.join('')}</tbody></table></div></div></details>`;
  }

  function paintVerify(s) {
    const box = byId('det-verify');
    if (!box) return;
    const name = source;
    const t = (s.tasks || {})[VERIFY[name].slot];
    const task = t && t.kind === VERIFY[name].kind ? t : null;
    const lines = (task && task.log_tail) || [];
    // Проверять и возвращать в очередь можно, только пока программа не качает этот источник
    const why = s.pipeline && s.pipeline.running ? 'Можно, когда скачивание остановлено или закончено'
      : t && !task && t.running ? 'Сейчас собирается список файлов — проверить можно после' : '';
    const off = why ? ' disabled' : '';
    const button = (action, cls, icon, label, extra = '') =>
      `<button${cls ? ` class="${cls}"` : ''} data-action="${action}" data-source="${name}"${extra}${off}>${App.icon(icon)}${label}</button>`;
    const done = (((((s.rescue || {}).sources || {})[name] || {}).files) || {}).done || 0;
    const sum = task && !task.running ? lastJson(lines, 'verify_summary | ') : null;
    let main;
    let actions = '';
    let list = '';
    if (task && task.running) {
      main = verifyRunning(lastJson(lines, 'verify_progress | '));
      actions = `<button data-action="verify-stop" data-source="${name}">${App.icon('stop')}Остановить проверку</button>`;
    } else if (sum) {
      main = verifyResult(sum, task);
      list = verifyList(sum);
      const fixable = (sum.missing || 0) + (sum.size || 0) + (sum.md5 || 0);
      const back = requeued[name] && requeued[name].at === String(task.finished_at) ? requeued[name] : null;
      if (back) {
        main += `<p class="verify-head ok">${App.icon('check')}<span>${esc(fmt.files(back.count))} снова в очереди. ` +
          'Нажмите «Продолжить» на Главной — программа скачает их заново.</span></p>';
        actions = `<a class="btn primary" href="#/home">${App.icon('home')}На главную</a>`;
      } else if (fixable) {
        actions = button('verify-requeue', 'primary', 'download', 'Скачать заново',
          ` data-count="${fixable}" data-bytes="${sum.bad_bytes || 0}" data-at="${task.finished_at}"`);
      }
      if (sum.checked) actions += button('verify-start', '', 'refresh', 'Проверить ещё раз');
    } else if (task) {
      const code = typeof task.exit_code === 'number' ? ` (код ${task.exit_code})` : '';
      main = task.was_stopped
        ? `<div class="empty">${App.icon('stop')}Проверка остановлена, итог не сохранён. Её можно запустить заново, когда будет время.</div>`
        : `<p class="safety-note">${App.icon('alert')}<span>Проверка прервалась${code}. Запустите её снова; если это повторится, ` +
          'скачайте отчёт для поддержки ниже.</span></p>';
      actions = button('verify-start', 'primary', 'shield-check', 'Проверить скачанные файлы');
    } else if (!done) {
      main = `<div class="empty">${App.icon('hourglass')}Пока проверять нечего: скачанных файлов нет.</div>`;
    } else {
      main = `<p class="small muted">Программа сверит каждый скачанный файл с Яндексом: на месте ли он, совпадают ли
        размер и контрольная сумма (MD5). На большой папке это долго — программа читает каждый файл целиком,
        и сотни гигабайт проверяются час и больше.</p>`;
      actions = button('verify-start', 'primary', 'shield-check', 'Проверить скачанные файлы');
    }
    if (why && actions.includes(' disabled>')) actions += `<span class="tiny">${esc(why)}</span>`;
    paintOnce(box, main);
    paintOnce(byId('det-verify-actions'), actions);
    paintOnce(byId('det-verify-list'), list);
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
          <div class="card" id="det-verify-card">${head('shield-check', 'info', 'Проверка скачанных файлов',
            'Все ли файлы на месте и совпадают ли они с теми, что на Яндексе')}
            <div id="det-verify"></div><div class="row verify-actions" id="det-verify-actions"></div><div id="det-verify-list"></div></div>
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
      const hosts = src.storage_hosts_down || 0;
      App.text(byId('det-hosts'), hosts
        ? `Файлы, которые ждут Яндекса, лежат на ${fmt.num(hosts)} ${fmt.plural(hosts, 'сервере', 'серверах', 'серверах')} ` +
          `хранения, ${fmt.plural(hosts, 'который сейчас не отвечает', 'которые сейчас не отвечают', 'которые сейчас не отвечают')}. ` +
          'Программа повторяет их сама.'
        : '');
      const db = (s[source] || {}).db || {};
      const recent = (db.recent || []).filter((x) => x.status === 'downloaded');
      App.html(byId('det-recent'), recent.length
        ? `<table class="list"><tbody>${recent.map((x) => `<tr><td class="name">${esc(String(x.path).split('/').pop())}</td>
            <td>${fmt.bytes(x.size)}</td><td class="tiny">${x.at ? fmt.hhmm(x.at) : ''}</td></tr>`).join('')}</tbody></table>`
        : `<div class="empty">${App.icon('hourglass')}Пока ничего.</div>`);
      const slot = (s.tasks || {})[VERIFY[source].slot];
      byId('det-relist').disabled = !!(slot && slot.running && slot.kind === VERIFY[source].kind);
      paintVerify(s);
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

  App.action('verify-start', (btn) => App.run(btn, () =>
    App.api.post('/api/verify/start', { source: btn.dataset.source })));

  App.action('verify-stop', async (btn) => {
    const yes = await App.confirm({ title: 'Остановить проверку?', ok: 'Остановить',
      text: 'Итог проверки не сохранится — её можно будет запустить заново. Скачанные файлы это не затронет.' });
    if (yes) App.run(btn, () => App.api.post('/api/verify/stop', { source: btn.dataset.source }));
  });

  App.action('verify-requeue', async (btn) => {
    const { source: name, at } = btn.dataset;
    const count = Number(btn.dataset.count) || 0;
    const bytes = Number(btn.dataset.bytes) || 0;
    const yes = await App.confirm({ title: `Скачать заново ${fmt.files(count)}?`, ok: 'Вернуть в очередь',
      text: `Они вернутся в очередь${bytes ? ` (${fmt.bytes(bytes)})` : ''} и скачаются, когда вы нажмёте «Продолжить» ` +
        'на Главной. Испорченные копии заменятся новыми. Если какие-то из этих файлов вы сами перенесли или удалили, ' +
        'они скачаются ещё раз.' });
    if (!yes) return;
    const res = await App.run(btn, () => App.api.post('/api/verify/requeue', { source: name }));
    if (res && res.ok) {
      requeued[name] = { at, count: (res.data || {}).count || count };
      App.render();
    }
  });

  // Тот же путь, что «Обновить список» на Главной: с выбранными папками, и ход сбора виден на Главной
  App.action('details-relist', async (btn) => {
    const yes = await App.confirm({ title: 'Собрать список файлов заново?', ok: 'Собрать',
      text: 'Программа заново спросит у Яндекса, какие файлы есть, и скачает новые. Скачанное не пропадёт и второй раз качаться не будет.' });
    if (yes) App.run(btn, () => App.api.post('/api/rescue/relist', { source }));
  });
})();
