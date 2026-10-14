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
    if (!problems.total) { App.html(box, '<p class="muted">Всё, что программа уже пробовала, скачалось. 🎉</p>'); return; }
    const reasons = Object.entries(problems.reasons || {}).sort((a, b) => b[1] - a[1])
      .map(([text, n]) => `<li>${esc(text)} — <b>${fmt.files(n)}</b></li>`).join('');
    const failed = problems.items.filter((x) => x.state === 'failed').length;
    const rows = problems.items.map((x) => `<tr>
        <td class="name">${esc(x.name)}${x.preview ? ' <span class="badge">есть превью</span>' : ''}</td>
        <td>${fmt.bytes(x.size)}</td>
        <td><span class="badge ${x.state === 'deferred' ? 'wait' : 'err'}">${x.state === 'deferred' ? 'ждёт Яндекса' : 'не скачался'}</span></td>
        <td class="small muted">${esc(x.state === 'deferred' ? 'сервер Яндекса не отвечает' : x.reason)}</td></tr>`).join('');
    App.html(box, `<ul class="small" style="padding-left:18px;margin-bottom:12px">${reasons}</ul>
      ${failed ? `<div class="row" style="margin-bottom:12px"><button class="primary" data-action="retry-failed" data-source="${source}">Повторить не скачавшиеся</button>
        <span class="tiny">Файлы, которые ждут Яндекса, программа повторяет сама.</span></div>` : ''}
      <div class="scroll"><table class="list"><thead><tr><th>Файл</th><th>Размер</th><th>Состояние</th><th>Почему</th></tr></thead>
      <tbody>${rows}</tbody></table></div>
      ${problems.total > problems.items.length ? `<p class="tiny" style="margin-top:8px">Показаны первые ${problems.items.length} из ${fmt.num(problems.total)}.</p>` : ''}`);
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
      el.innerHTML = `<p><a href="#/home">← На главную</a></p>
        <h1 style="font-size:24px;margin:10px 0 14px" id="det-title">Подробнее</h1>
        <div class="stack">
          <div class="card"><div class="counters" id="det-counters"></div><p class="small muted" id="det-hosts" style="margin-top:12px"></p></div>
          <div class="card"><h2>Что не скачалось и почему</h2><div id="det-problems"></div></div>
          <div class="card"><h2>Последние скачанные</h2><div id="det-recent"></div></div>
          <div class="card"><details><summary>Для опытных</summary><div class="body">
            <div class="row" style="margin-bottom:10px">
              <button data-action="details-relist" id="det-relist">Собрать список файлов заново</button>
              <a class="btn" href="/api/logs/bundle">Скачать отчёт для поддержки</a></div>
            <div class="console" id="det-log"></div></div></details></div>
        </div>`;
    },
    show(params) {
      source = params.source === 'unlim' ? 'unlim' : 'disk';
      problems = null;
      App.text(byId('det-title'), App.SOURCE_NAMES[source]);
      paintProblems();
      loadProblems();
    },
    update(s) {
      const src = ((s.rescue || {}).sources || {})[source] || {};
      const f = src.files || {};
      const c = (v, l) => `<div class="counter"><div class="v">${fmt.num(v)}</div><div class="l">${l}</div></div>`;
      App.html(byId('det-counters'), c(f.selected, 'выбрано') + c(f.done, 'скачано') +
        c((f.pending || 0) + (f.in_progress || 0), 'в очереди') + c(f.deferred, 'ждут Яндекса') +
        c(f.failed, 'не скачались') + c(f.excluded, 'не выбраны') + c(f.preview_only, 'есть превью'));
      App.text(byId('det-hosts'), src.storage_hosts_down
        ? `Файлы, которые ждут Яндекса, лежат на ${fmt.num(src.storage_hosts_down)} серверах хранения, которые сейчас не отвечают. Программа повторяет их сама.`
        : '');
      const db = (s[source] || {}).db || {};
      const recent = (db.recent || []).filter((r) => r.status === 'downloaded');
      App.html(byId('det-recent'), recent.length
        ? `<table class="list"><tbody>${recent.map((r) => `<tr><td class="name">${esc(String(r.path).split('/').pop())}</td><td>${fmt.bytes(r.size)}</td></tr>`).join('')}</tbody></table>`
        : '<p class="muted">Пока ничего.</p>');
      const running = !!(s.pipeline && s.pipeline.running);
      byId('det-relist').disabled = running;
      App.text(byId('det-log'), taskLog(s));
      if (Date.now() - problemsAt > 20000) loadProblems();
    },
  };

  App.action('details-relist', async (btn) => {
    const yes = await App.confirm({ title: 'Собрать список файлов заново?', ok: 'Собрать',
      text: 'Программа заново спросит у Яндекса, какие файлы есть. Скачанное не пропадёт и второй раз качаться не будет.' });
    if (yes) App.run(btn, () => App.api.post(source === 'disk' ? '/api/disk/scan' : '/api/unlim/collect'));
  });
})();
