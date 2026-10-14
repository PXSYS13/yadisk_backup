/* Сортировка: сначала «Посмотреть, что получится» (сколько папок исчезнет, примеры переносов),
   потом «Разложить файлы» с подтверждением. Тексты честно говорят: папки, как на Диске, пропадут. */
(function () {
  'use strict';
  const App = window.App;
  const { esc, fmt, byId } = App;

  let reportFor = null;   // путь, для которого есть отчёт
  let lastSummary = null;
  let lastResult = null;
  let clickedAt = 0;      // когда нажали кнопку — журнал более старой задачи не читаем

  function parseLog(lines) {
    let summary = null, result = null, progress = '';
    for (const line of lines || []) {
      const s = line.indexOf('organize_summary | ');
      if (s !== -1) { try { summary = JSON.parse(line.slice(s + 19)); } catch (e) { /* пропускаем */ } }
      const r = line.indexOf('organize_result | ');
      if (r !== -1) { try { result = JSON.parse(line.slice(r + 18)); } catch (e) { /* пропускаем */ } }
      const m = /progress:\s*(\d+)\/(\d+)/.exec(line);
      if (m) progress = `${m[1]} из ${m[2]}`;
      if (/Сканирую/.test(line)) progress = progress || 'смотрю файлы…';
    }
    return { summary, result, progress };
  }

  // «Исчезнут 37 папок»
  const foldersText = (n) =>
    `${fmt.plural(n, 'Исчезнет', 'Исчезнут', 'Исчезнут')} ${fmt.num(n)} ${fmt.plural(n, 'папка', 'папки', 'папок')}`;

  function summaryHtml(sum) {
    const shelves = Object.entries(sum.shelves || {}).map(([name, v]) =>
      `<tr><td>${esc(name)}</td><td>${fmt.files(v.count)}</td><td>${fmt.bytes(v.bytes)}</td></tr>`).join('');
    const examples = (sum.examples || []).map((e) =>
      `<tr><td class="name">${esc(e.from)}</td><td class="name">${esc(e.to)}</td></tr>`).join('');
    const n = sum.folders || 0;
    return `<p>В папке <b>${fmt.files(sum.total)}</b> (${fmt.bytes(sum.total_bytes)}).
      Дубликатов: <b>${fmt.files(sum.duplicates)}</b>${sum.duplicates ? ` (${fmt.bytes(sum.duplicates_bytes)}) — они уедут в папку «_duplicates», а не удалятся` : ''}.</p>
      ${n ? `<p style="margin-top:8px"><b>${foldersText(n)}</b> — файлы из них переедут в папки по типам, а опустевшие папки программа удалит.</p>` : ''}
      ${examples ? `<div class="section-title" style="margin-top:18px">Например</div>
      <table class="list"><thead><tr><th>Сейчас</th><th>После сортировки</th></tr></thead><tbody>${examples}</tbody></table>` : ''}
      <div class="section-title" style="margin-top:18px">Куда что ляжет</div>
      <table class="list"><thead><tr><th>Папка</th><th>Файлов</th><th>Размер</th></tr></thead><tbody>${shelves}</tbody></table>`;
  }

  App.screens.sort = {
    mount(el) {
      el.innerHTML = `<div class="page-head"><h1>Сортировка</h1>
          <p>Файлы Диска уже лежат в тех же папках, что на Яндекс.Диске, — для этого ничего делать не нужно.
          Сортировка нужна, только если вы хотите собрать все файлы в общие папки по типам. Сначала программа
          покажет, что получится.</p></div>
        <div class="stack">
        <div class="card"><div class="card-head"><span class="icon-box">${App.icon('grid')}</span>
          <div class="grow"><h2>Разложить файлы по типам</h2></div></div>
          <p class="lead">Программа соберёт файлы из всех папок в общие папки «Фото», «Видео», «Документы», «Архивы»
          и другие — фото и видео ещё и по годам и месяцам. Одинаковые файлы уедут в папку «_duplicates».
          Папки «_превью», «_ссылки» и служебные не трогаются.</p>
          <div class="banner safety" style="margin:14px 0 18px;box-shadow:none"><span class="b-icon">${App.icon('alert')}</span><div>
            <h3>Папки, как на Диске, исчезнут</h3>
            <p>Сейчас файлы лежат так же, как на Яндекс.Диске. После сортировки, например, «Отчёт.docx» из папки
            «Загрузки → Проект» окажется просто в папке «Документы», а картинка из той же папки — в папке «Фото».
            Опустевшие папки программа удалит. Сами файлы не удаляются, но вернуть всё как было автоматически нельзя.</p>
          </div></div>
          <label class="field"><span>Какую папку разложить</span>
            <input type="text" id="sort-path" spellcheck="false"></label>
          <div class="row" style="margin-top:6px">
            <button class="primary" data-action="sort-report" id="sort-report-btn">${App.icon('list')}Посмотреть, что получится</button>
            <button data-action="sort-apply" id="sort-apply-btn" disabled>${App.icon('grid')}Разложить файлы</button>
            <button class="hidden" data-action="sort-stop" id="sort-stop-btn">${App.icon('stop')}Остановить</button>
          </div>
          <p class="tiny" id="sort-note" style="margin-top:10px"></p></div>
        <div class="card hidden" id="sort-out-card"><h2 id="sort-out-title">Что получится</h2><div id="sort-out"></div></div>
      </div>`;
    },
    update(s) {
      const input = byId('sort-path');
      if (!input.value && document.activeElement !== input) input.value = (s.config || {}).download_dir || '';
      const task = (s.tasks || {}).organize;
      const busy = !!(task && task.running);
      const rescueOn = !!(s.pipeline && s.pipeline.running);
      const startedMs = task ? Date.now() - (task.elapsed || 0) * 1000 : 0;
      const fresh = task && (!clickedAt || startedMs >= clickedAt - 3000);
      const parsed = fresh ? parseLog(task.log_tail) : { summary: null, result: null, progress: '' };
      if (parsed.summary) lastSummary = parsed.summary;
      if (parsed.result) lastResult = parsed.result;
      byId('sort-report-btn').disabled = busy;
      byId('sort-apply-btn').disabled = busy || rescueOn || !lastSummary || reportFor !== input.value.trim();
      byId('sort-stop-btn').classList.toggle('hidden', !busy);
      App.text(byId('sort-note'), busy ? `Идёт… ${parsed.progress}`
        : rescueOn ? 'Разложить файлы можно после окончания скачивания — иначе программа скачает их заново.'
          : lastSummary ? '' : 'Сначала посмотрите, что получится, — так ничего не переместится случайно.');
      const card = byId('sort-out-card');
      if (lastResult && !busy) {
        card.classList.remove('hidden');
        App.text(byId('sort-out-title'), 'Готово');
        App.html(byId('sort-out'), `<p>Перемещено: <b>${fmt.files(lastResult.moved)}</b>. Ошибок: ${fmt.num(lastResult.errors)}.
          Удалено пустых папок: ${fmt.num(lastResult.cleaned)}.</p>`);
      } else if (lastSummary) {
        card.classList.remove('hidden');
        App.text(byId('sort-out-title'), 'Что получится');
        App.html(byId('sort-out'), summaryHtml(lastSummary));
      }
    },
  };

  App.action('sort-report', (btn) => {
    lastSummary = null; lastResult = null;
    const path = byId('sort-path').value.trim();
    if (!path) { App.toast('Укажите папку', 'err'); return; }
    reportFor = path;
    clickedAt = Date.now();
    App.run(btn, () => App.api.post('/api/organize', { input: path, action: 'report-only' }), { okToast: false });
  });

  App.action('sort-apply', async (btn) => {
    const path = byId('sort-path').value.trim();
    const n = (lastSummary && lastSummary.folders) || 0;
    const yes = await App.confirm({ title: 'Разложить файлы по типам?', ok: 'Разложить файлы', danger: true,
      text: `Файлы из «${path}» переедут в общие папки «Фото», «Видео», «Документы» и другие — папок, как на Яндекс.Диске, больше не будет.` +
        (n ? ` ${foldersText(n)}: программа удалит их, когда они опустеют.` : '') +
        ' Сами файлы не удаляются, одинаковые уедут в «_duplicates». Вернуть всё как было автоматически нельзя.' });
    if (!yes) return;
    lastResult = null;
    clickedAt = Date.now();
    App.run(btn, () => App.api.post('/api/organize', { input: path, action: 'move' }), { okToast: false });
  });

  App.action('sort-stop', (btn) => App.run(btn, () => App.api.post('/api/organize/stop')));
})();
