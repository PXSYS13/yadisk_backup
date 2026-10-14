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
    },
  };

  App.action('details-relist', async (btn) => {
    const yes = await App.confirm({ title: 'Собрать список файлов заново?', ok: 'Собрать',
      text: 'Программа заново спросит у Яндекса, какие файлы есть. Скачанное не пропадёт и второй раз качаться не будет.' });
    if (yes) App.run(btn, () => App.api.post(source === 'disk' ? '/api/disk/scan' : '/api/unlim/collect'));
  });
})();
