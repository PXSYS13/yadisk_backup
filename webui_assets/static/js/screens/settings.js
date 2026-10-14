/* Настройки: доступ, что скачивать (источники, типы, папки Диска), дополнительно, опасная зона. */
(function () {
  'use strict';
  const App = window.App;
  const { esc, fmt, byId } = App;

  let counts = { disk: null, unlim: null };
  let formDirty = false;

  // Выбор папок Диска. Живёт отдельно от paintWhat: дерево меняется только по действиям
  // человека, опрос статуса его не перерисовывает
  const ROOT = 'disk:/';
  const picker = {
    mode: 'all',            // all — весь Диск, some — только выбранные папки
    selected: new Set(),    // явно отмеченные папки 'disk:/…'
    loaded: new Map(),      // папка → { folders, truncated }
    loading: new Set(),
    errors: new Map(),      // папка → { msg, action }
    expanded: new Set(),
    dirty: false,
    synced: false,
    max: 50,
  };
  const folderLabel = (path) => path.replace(/^disk:\/*/, '') || 'Диск';
  const inside = (path, folder) => path === folder || path.startsWith(`${folder}/`);

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
      <div class="checks">${Object.keys(labels).map(catBox).join('')}</div>`);
  }

  // ── Папки Диска ──

  async function loadFolder(path) {
    if (picker.loaded.has(path) || picker.loading.has(path)) return;
    picker.loading.add(path);
    picker.errors.delete(path);
    const res = await App.api.get(`/api/disk/folders?path=${encodeURIComponent(path)}${App.demo ? '&demo=1' : ''}`);
    picker.loading.delete(path);
    if (res.ok) picker.loaded.set(path, { folders: res.data.folders || [], truncated: !!res.data.truncated });
    else picker.errors.set(path, { msg: res.error, action: res.data && res.data.action });
    paintFolders();
  }

  function summaryHtml() {
    const sel = [...picker.selected].sort((a, b) => a.localeCompare(b, 'ru'));
    if (!sel.length) return '<span class="tiny">Пока ничего не выбрано — отметьте папки ниже.</span>';
    return `<span class="tiny">Выбрано: ${fmt.num(sel.length)} ${fmt.plural(sel.length, 'папка', 'папки', 'папок')}</span>` +
      sel.map((p) => `<span class="folder-chip">${App.icon('folder')}<span class="name">${esc(folderLabel(p))}</span>` +
        `<button type="button" data-action="folder-unpick" data-path="${esc(p)}" title="Убрать из выбора" ` +
        `aria-label="Убрать «${esc(folderLabel(p))}» из выбора">${App.icon('x')}</button></span>`).join('');
  }

  function noteRow(depth, html, cls = '') {
    return `<div class="tree-note ${cls}" style="--depth:${depth}">${html}</div>`;
  }

  function treeHtml(path, depth) {
    const err = picker.errors.get(path);
    if (err) {
      const btn = err.action === 'token'
        ? `<button type="button" class="small" data-action="go-access" data-field="token">${App.icon('key')}Указать ключ доступа</button>`
        : `<button type="button" class="small" data-action="folders-reload" data-path="${esc(path)}">${App.icon('refresh')}Повторить</button>`;
      return noteRow(depth, `${App.icon('alert')}<span>${esc(err.msg || 'Не получилось')}</span>${btn}`, 'err');
    }
    const node = picker.loaded.get(path);
    if (!node) return noteRow(depth, `<span class="spin"></span><span>Загружаю папки…</span>`);
    if (!node.folders.length) {
      return noteRow(depth, depth ? 'Внутри нет папок' : 'На Диске нет папок — выберите «Весь Диск»');
    }
    let out = '';
    for (const f of node.folders) {
      const covered = [...picker.selected].some((p) => p !== f.path && inside(f.path, p));
      const checked = covered || picker.selected.has(f.path);
      const partial = !checked && [...picker.selected].some((p) => p.startsWith(`${f.path}/`));
      const open = picker.expanded.has(f.path);
      const badge = f.shared ? `<span class="badge violet" title="${f.owned
        ? 'Вы дали доступ к этой папке другим людям' : 'К этой папке вам дал доступ другой человек'}">общая</span>` : '';
      out += `<div class="tree-row" style="--depth:${depth}">
        <button type="button" class="twist" data-action="folder-expand" data-path="${esc(f.path)}" aria-expanded="${open}"
          aria-label="${open ? 'Свернуть' : 'Раскрыть'} «${esc(f.name)}»">${App.icon('chevron')}</button>
        <label><input type="checkbox" class="cbx" data-field="folder" value="${esc(f.path)}" ${checked ? 'checked' : ''}
          ${covered ? 'disabled' : ''} ${partial ? 'data-partial="1"' : ''}>${App.icon('folder', 'fold')}
          <span class="name">${esc(f.name)}</span>${badge}</label></div>`;
      if (open) out += treeHtml(f.path, depth + 1);
    }
    if (node.truncated) out += noteRow(depth, 'Показаны не все папки — в этой папке очень много файлов');
    return out;
  }

  function paintFolders() {
    const root = byId('set-folders');
    if (!root) return;
    for (const r of root.querySelectorAll('[data-field="folders-mode"]')) r.checked = r.value === picker.mode;
    root.querySelector('[data-field="folders-picker"]').classList.toggle('hidden', picker.mode !== 'some');
    if (picker.mode !== 'some') return;
    // Дерево перерисовывается целиком — возвращаем фокус туда, где он был
    const active = document.activeElement;
    const tree = root.querySelector('[data-field="folders-tree"]');
    const focus = active && tree.contains(active)
      ? { kind: active.dataset.action || active.dataset.field, path: active.dataset.path || active.value } : null;
    App.html(root.querySelector('[data-field="folders-summary"]'), summaryHtml());
    tree.innerHTML = treeHtml(ROOT, 0);
    for (const box of tree.querySelectorAll('[data-partial="1"]')) box.indeterminate = true;
    if (focus) {
      const el = [...tree.querySelectorAll('[data-path], [data-field="folder"]')].find((x) =>
        (x.dataset.action || x.dataset.field) === focus.kind && (x.dataset.path || x.value) === focus.path);
      if (el) el.focus();
    }
  }

  function syncFolders(st) {
    if (picker.synced || picker.dirty) return;
    picker.synced = true;
    const saved = st.disk_folders || [];
    picker.max = st.max_disk_folders || 50;
    picker.mode = saved.length ? 'some' : 'all';
    picker.selected = new Set(saved);
    // Раскрываем путь к немногим выбранным папкам, чтобы отметки было видно в дереве
    if (saved.length <= 5) {
      for (const p of saved) {
        const parts = folderLabel(p).split('/');
        for (let i = 1; i < parts.length; i++) picker.expanded.add(`disk:/${parts.slice(0, i).join('/')}`);
      }
    }
    if (picker.mode === 'some') {
      loadFolder(ROOT);
      for (const p of picker.expanded) loadFolder(p);
    }
    paintFolders();
  }

  function onFoldersChange(ev) {
    const input = ev.target;
    if (input.dataset.field === 'folders-mode') {
      picker.mode = input.value;
      picker.dirty = true;
      if (picker.mode === 'some') loadFolder(ROOT);
      paintFolders();
      return;
    }
    if (input.dataset.field !== 'folder') return;
    const path = input.value;
    if (input.checked) {
      // Отмеченная папка скачается целиком: отмеченные внутри неё уже не нужны
      const rest = [...picker.selected].filter((p) => !inside(p, path));
      if (rest.length + 1 > picker.max) {
        input.checked = false;
        App.toast(`Можно выбрать не больше ${picker.max} папок — отметьте папки уровнем выше`, 'err');
        return;
      }
      picker.selected = new Set([...rest, path]);
    } else {
      picker.selected.delete(path);
    }
    picker.dirty = true;
    paintFolders();
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
          <div id="set-what"></div>
          <div id="set-folders" class="folders">
            <div class="section-title" style="margin-top:18px">Папки Диска</div>
            <fieldset class="bare" data-field="folders-set">
              <legend class="sr-only">Какие папки Диска скачивать</legend>
              <div class="checks">
                <label class="check"><input type="radio" name="folders-mode" value="all" data-field="folders-mode">
                  <span><b>Весь Диск</b><span class="tiny block">Все папки и файлы — и общие папки, к которым вам дали доступ</span></span></label>
                <label class="check"><input type="radio" name="folders-mode" value="some" data-field="folders-mode">
                  <span><b>Только выбранные папки</b><span class="tiny block">Отметьте нужные папки — остальное скачиваться не будет</span></span></label>
              </div>
              <div class="hidden" data-field="folders-picker">
                <div class="folders-summary" data-field="folders-summary"></div>
                <div class="tree" data-field="folders-tree"></div>
                <p class="tiny" style="margin-top:8px">Значок «общая» — папка из общего доступа. На компьютере папки
                  будут разложены так же, как на Диске.</p>
              </div>
            </fieldset>
            <p class="tiny" data-field="folders-off"></p>
          </div>
          <div class="row" style="margin-top:16px">
            <button class="primary" data-action="settings-save-what" id="set-save">Сохранить</button>
            <button class="link" data-action="settings-all" id="set-all">Отметить всё</button>
            <span class="tiny" id="set-what-note"></span>
          </div></div>
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
      byId('set-folders').addEventListener('change', onFoldersChange);
    },
    show() {
      formDirty = false;
      // Несохранённый выбор папок забываем, как и галочки типов; загруженное дерево оставляем
      picker.dirty = false;
      picker.synced = false;
      picker.errors.clear();
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
      if (App.pendingFoldersFocus) {
        App.pendingFoldersFocus = false;
        setTimeout(() => byId('set-folders').scrollIntoView({ behavior: 'smooth', block: 'center' }), 50);
      }
    },
    update(s) {
      App.Access.update(byId('set-access'), s);
      paintWhat(s, false);
      paintExtra(s);
      const st = s.settings || {};
      syncFolders(st);
      const running = !!(s.pipeline && s.pipeline.running);
      const box = byId('set-folders');
      const fieldset = box.querySelector('[data-field="folders-set"]');
      if (fieldset.disabled !== running) fieldset.disabled = running;
      const diskOn = (st.sources || ['disk', 'unlim']).includes('disk');
      box.classList.toggle('off', !diskOn);
      App.text(box.querySelector('[data-field="folders-off"]'),
        diskOn ? '' : 'Обычный Диск выключен в «Источниках» — его папки сейчас не скачиваются.');
      byId('set-save').disabled = running;
      byId('set-all').disabled = running;
      App.text(byId('set-what-note'), running ? 'Изменить можно после остановки скачивания.'
        : 'По умолчанию программа качает всё.');
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
    // Дерево ещё не узнало сохранённый выбор — папки не трогаем, чтобы не затереть его
    if (picker.synced && picker.mode === 'some') {
      if (!picker.selected.size) return { ok: false, error: 'Отметьте хотя бы одну папку или выберите «Весь Диск»' };
      body.disk_folders = [...picker.selected];
    } else if (picker.synced) {
      body.all_folders = true;
    }
    const res = await App.api.post('/api/settings', body);
    if (res.ok) {
      formDirty = false;
      picker.dirty = false;
      picker.synced = false;  // сервер мог сжать выбор — перечитаем его
      return { ok: true, data: { msg: 'Сохранено' } };
    }
    return res;
  }));

  App.action('folder-expand', (btn) => {
    const path = btn.dataset.path;
    if (picker.expanded.has(path)) picker.expanded.delete(path);
    else {
      picker.expanded.add(path);
      loadFolder(path);
    }
    paintFolders();
  });

  App.action('folders-reload', (btn) => {
    const path = btn.dataset.path || ROOT;
    picker.errors.delete(path);
    picker.loaded.delete(path);
    loadFolder(path);
    paintFolders();
  });

  App.action('folder-unpick', (btn) => {
    picker.selected.delete(btn.dataset.path);
    picker.dirty = true;
    paintFolders();
  });

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
