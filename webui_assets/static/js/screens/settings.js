/* Настройки: доступ, что скачивать, дополнительно, опасная зона. */
(function () {
  'use strict';
  const App = window.App;
  const { esc, fmt, byId } = App;

  let counts = { disk: null, unlim: null };
  let formDirty = false;

  async function loadCounts() {
    const [d, u] = await Promise.all([App.api.get('/api/disk/preview'), App.api.get('/api/unlim/preview')]);
    counts = { disk: d.ok ? d.data : null, unlim: u.ok ? u.data : null };
    if (App.state.status) paintWhat(App.state.status, true);
  }

  function countFor(cat) {
    let n = 0, b = 0;
    for (const src of ['disk', 'unlim']) {
      const c = counts[src] && counts[src].categories && counts[src].categories[cat];
      if (c) { n += c.count || 0; b += c.bytes || 0; }
    }
    return n ? ` — ${fmt.files(n)}, ${fmt.bytes(b)}` : '';
  }

  function paintWhat(s, force) {
    if (formDirty && !force) return;
    const st = s.settings || {};
    const labels = st.category_labels || {};
    const running = !!(s.pipeline && s.pipeline.running);
    const sources = st.sources || ['disk', 'unlim'];
    const cats = st.categories;  // null — всё
    const allChecked = !cats;
    const srcBox = (k, title) => `<label class="check"><input type="checkbox" data-field="src-${k}" ${
      sources.includes(k) ? 'checked' : ''} ${running ? 'disabled' : ''}><span><b>${title}</b></span></label>`;
    const catBox = (k) => `<label class="check"><input type="checkbox" data-field="cat" value="${esc(k)}" ${
      allChecked || cats.includes(k) ? 'checked' : ''} ${running ? 'disabled' : ''}><span><b>${esc(labels[k] || k)}</b>
      <span class="tiny">${esc(countFor(k))}</span></span></label>`;
    App.html(byId('set-what'), `
      <div class="section-title">Источники</div>
      <div class="checks">${srcBox('disk', 'Обычный Диск')}${srcBox('unlim', 'Безлимит (фото с телефона)')}</div>
      <div class="section-title" style="margin-top:16px">Типы файлов</div>
      <div class="checks">${Object.keys(labels).map(catBox).join('')}</div>
      <div class="row" style="margin-top:14px">
        <button class="primary" data-action="settings-save-what" ${running ? 'disabled' : ''}>Сохранить</button>
        <button class="link" data-action="settings-all" ${running ? 'disabled' : ''}>Отметить всё</button>
        <span class="tiny">${running ? 'Изменить можно после остановки скачивания.' : 'По умолчанию программа качает всё.'}</span>
      </div>`);
  }

  function paintExtra(s) {
    const st = s.settings || {};
    for (const [field, value] of [['gentle', st.gentle], ['previews', st.previews]]) {
      const input = byId('set-root').querySelector(`[data-field="opt-${field}"]`);
      if (input && document.activeElement !== input) input.checked = !!value;
    }
    const running = !!(s.pipeline && s.pipeline.running);
    for (const btn of byId('set-root').querySelectorAll('[data-danger]')) btn.disabled = running;
    App.text(byId('set-danger-note'), running ? 'Сначала остановите скачивание.' : '');
  }

  App.screens.settings = {
    mount(el) {
      const head = (ic, cls, title, sub) => `<div class="card-head"><span class="icon-box ${cls}">${App.icon(ic)}</span>
        <div class="grow"><h2>${title}</h2><span class="tiny">${sub}</span></div></div>`;
      el.innerHTML = `<div class="page-head"><h1>Настройки</h1>
          <p>Доступ к Яндексу, что скачивать и дополнительные параметры. Менять можно в любой момент.</p></div>
        <div class="stack" id="set-root">
        <div class="card">${head('key', '', 'Доступ', 'Ключ к Диску, папка для файлов и доступ к Безлимиту. Всё хранится только на этом компьютере.')}
          <div id="set-access"></div></div>
        <div class="card">${head('list', 'ok', 'Что скачивать', 'Снимите галочки с того, что не нужно. Остальное программа скачает.')}
          <div id="set-what"></div></div>
        <div class="card">${head('sliders', 'plain', 'Дополнительно', 'Обычно менять ничего не нужно.')}
          <div class="switch"><div><b>Бережный режим</b>
              <p class="small muted">Вдвое меньше одновременных скачиваний. Включите, если Яндекс часто отвечает
              «слишком много запросов» или медленный интернет.</p></div>
            <input type="checkbox" data-field="opt-gentle" data-action="settings-toggle" data-option="gentle"></div>
          <div class="switch"><div><b>Страховочные превью фото</b>
              <p class="small muted">Пока оригинал фото недоступен, сохранять его уменьшенную копию (до 1280 пикселей)
              в папку «_превью». Оригинал докачается сам, когда Яндекс его отдаст.</p></div>
            <input type="checkbox" data-field="opt-previews" data-action="settings-toggle" data-option="previews"></div>
        </div>
        <div class="card"><details><summary>Опасная зона — нужна, только если что-то пошло совсем не так</summary><div class="body">
          <p class="small">Программа забудет список файлов и при следующем запуске соберёт его заново.
          Скачанные файлы в папке <b>не удаляются</b>. Нужно, только если что-то пошло совсем не так.</p>
          <div class="row" style="margin-top:12px">
            <button class="danger" data-danger="1" data-action="forget-list" data-source="disk">Забыть список файлов Диска</button>
            <button class="danger" data-danger="1" data-action="forget-list" data-source="unlim">Забыть список файлов Безлимита</button>
          </div><p class="tiny" id="set-danger-note"></p></div></details></div>
      </div>`;
      App.Access.mount(byId('set-access'));
      byId('set-what').addEventListener('change', () => { formDirty = true; });
    },
    show() {
      formDirty = false;
      loadCounts();
      const focus = App.pendingAccessFocus;
      App.pendingAccessFocus = null;
      if (focus) {
        setTimeout(() => {
          App.Access.open(byId('set-access'), focus);
          const row = byId('set-access').querySelector(`[data-row="${focus}"]`);
          if (row) row.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }, 50);
      }
    },
    update(s) {
      App.Access.update(byId('set-access'), s);
      paintWhat(s, false);
      paintExtra(s);
    },
  };

  App.action('settings-all', () => {
    for (const box of byId('set-what').querySelectorAll('input[type=checkbox]')) box.checked = true;
    formDirty = true;
  });

  App.action('settings-save-what', (btn) => App.run(btn, async () => {
    const root = byId('set-what');
    const sources = ['disk', 'unlim'].filter((k) => root.querySelector(`[data-field="src-${k}"]`).checked);
    const all = [...root.querySelectorAll('[data-field="cat"]')];
    const cats = all.filter((b) => b.checked).map((b) => b.value);
    if (!sources.length) return { ok: false, error: 'Выберите хотя бы одно: Диск или Безлимит' };
    if (!cats.length) return { ok: false, error: 'Выберите хотя бы один тип файлов' };
    const body = cats.length === all.length ? { sources, all_categories: true } : { sources, categories: cats };
    const res = await App.api.post('/api/settings', body);
    if (res.ok) { formDirty = false; return { ok: true, data: { msg: 'Сохранено' } }; }
    return res;
  }));

  App.action('settings-toggle', (input) => {
    // Флажок: событие click приходит до смены состояния — читаем после
    setTimeout(() => App.run(null, () => App.api.post('/api/settings', { [input.dataset.option]: input.checked })), 0);
  });

  App.action('forget-list', async (btn) => {
    const src = btn.dataset.source;
    const yes = await App.confirm({ title: `Забыть список файлов ${src === 'disk' ? 'Диска' : 'Безлимита'}?`,
      text: 'Скачанные файлы останутся в папке. При следующем запуске программа соберёт список заново и не будет качать то, что уже лежит на месте.',
      ok: 'Забыть список', danger: true });
    if (yes) App.run(btn, () => App.api.post(`/api/${src}/clear`));
  });
})();
