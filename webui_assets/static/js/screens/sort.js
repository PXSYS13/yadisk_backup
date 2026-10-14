/* Сортировка: сначала «Посмотреть, что получится», потом «Разложить файлы» с подтверждением. */
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

  function summaryHtml(sum) {
    const shelves = Object.entries(sum.shelves || {}).map(([name, v]) =>
      `<tr><td>${esc(name)}</td><td>${fmt.files(v.count)}</td><td>${fmt.bytes(v.bytes)}</td></tr>`).join('');
    return `<p>В папке <b>${fmt.files(sum.total)}</b> (${fmt.bytes(sum.total_bytes)}).
      Дубликатов: <b>${fmt.files(sum.duplicates)}</b>${sum.duplicates ? ` (${fmt.bytes(sum.duplicates_bytes)}) — они уедут в «_duplicates», ничего не удаляется` : ''}.</p>
      <table class="list" style="margin-top:10px"><thead><tr><th>Папка</th><th>Файлов</th><th>Размер</th></tr></thead><tbody>${shelves}</tbody></table>`;
  }

  App.screens.sort = {
    mount(el) {
      el.innerHTML = `<div class="page-head"><h1>Сортировка</h1>
          <p>Когда всё скачается, программа может разложить файлы по папкам. Сначала покажет, что получится.</p></div>
        <div class="stack">
        <div class="card"><div class="card-head"><span class="icon-box">${App.icon('grid')}</span>
          <div class="grow"><h2>Разложить скачанное по папкам</h2></div></div>
          <p class="lead">Программа разложит файлы по папкам «Фото», «Видео», «Документы», «Архивы» и другим — фото и видео
          ещё и по годам. Одинаковые файлы уедут в папку «_duplicates». <b>Ничего не удаляется.</b>
          Превью и служебные папки не трогаются.</p>
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
    const yes = await App.confirm({ title: 'Разложить файлы по папкам?', ok: 'Разложить',
      text: `Файлы в «${path}» переедут в папки по типам. Отменить это автоматически нельзя, но ничего не удаляется: дубликаты уедут в «_duplicates».` });
    if (!yes) return;
    lastResult = null;
    clickedAt = Date.now();
    App.run(btn, () => App.api.post('/api/organize', { input: path, action: 'move' }), { okToast: false });
  });

  App.action('sort-stop', (btn) => App.run(btn, () => App.api.post('/api/organize/stop')));
})();
