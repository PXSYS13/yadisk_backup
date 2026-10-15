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
    return n ? `${fmt.files(n)} · ${fmt.bytes(b)}` : '';
  }

  // «6 015 файлов · 23,4 ГБ» под названием источника
  function sourceSummary(src) {
    const c = counts[src];
    if (!c || !c.total) return src === 'disk' ? 'ваши папки и файлы' : 'фото и видео, загруженные с телефона';
    const bytes = Object.values(c.categories || {}).reduce((sum, x) => sum + (x.bytes || 0), 0);
    return `${fmt.files(c.total)} · ${fmt.bytes(bytes)}`;
  }

  function paintWhat(s, force) {
    if (formDirty && !force) return;
    const st = s.settings || {};
    const labels = st.category_labels || {};
    const running = !!(s.pipeline && s.pipeline.running);
    const sources = st.sources || ['disk', 'unlim'];
    const cats = st.categories;  // null — всё
    const allChecked = !cats;
    const srcBox = (k, ic, title) => `<label class="check src"><input type="checkbox" data-field="src-${k}" ${
      sources.includes(k) ? 'checked' : ''} ${running ? 'disabled' : ''}><span class="icon-box">${App.icon(ic)}</span>
      <span><b>${title}</b><span class="tiny">${esc(sourceSummary(k))}</span></span></label>`;
    const catBox = (k) => `<label class="check"><input type="checkbox" data-field="cat" value="${esc(k)}" ${
      allChecked || cats.includes(k) ? 'checked' : ''} ${running ? 'disabled' : ''}><span><b>${esc(labels[k] || k)}</b>
      <span class="tiny">${esc(countFor(k))}</span></span></label>`;
    App.html(byId('set-sources'), `<div class="checks two">${srcBox('disk', 'hdd', 'Обычный Диск')}${
      srcBox('unlim', 'phone', 'Безлимит (фото с телефона)')}</div>`);
    App.html(byId('set-types'), `<div class="checks types">${Object.keys(labels).map(catBox).join('')}</div>`);
  }

  // Полоса «Есть несохранённые изменения» — только когда что-то поменяли
  function paintSavebar() {
    const bar = byId('set-savebar');
    if (bar) bar.classList.toggle('hidden', !(formDirty || picker.dirty));
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
    if (!sel.length) return '<span>Пока ничего не выбрано — отметьте папки ниже.</span>';
    return `<span>Выбрано: <b>${fmt.num(sel.length)}</b> ${fmt.plural(sel.length, 'папка', 'папки', 'папок')}</span>` +
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
      paintSavebar();
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
    paintSavebar();
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

  const SECTIONS = [['set-sec-access', 'Доступ'], ['set-sec-what', 'Что скачивать'],
    ['set-sec-extra', 'Дополнительно'], ['set-sec-danger', 'Если что-то не так']];
  let observer = null;

  // Подсветка раздела в оглавлении слева — того, что сейчас на экране
  function watchSections() {
    if (observer || typeof IntersectionObserver === 'undefined') return;
    observer = new IntersectionObserver((entries) => {
      for (const e of entries) {
        if (!e.isIntersecting) continue;
        for (const btn of document.querySelectorAll('#set-nav button')) btn.classList.toggle('active', btn.dataset.target === e.target.id);
      }
    }, { rootMargin: '-15% 0px -70% 0px' });
    for (const [id] of SECTIONS) observer.observe(byId(id));
  }

  App.screens.settings = {
    mount(el) {
      const head = (title, sub) => `<div class="sec-head"><h2>${title}</h2>${sub ? `<p>${sub}</p>` : ''}</div>`;
      el.innerHTML = `<div class="page-head"><h1>Настройки</h1>
          <p>Доступ к Диску, папка на компьютере и что именно скачивать. Менять можно в любой момент.</p></div>
        <div class="settings-layout" id="set-root">
          <nav class="settings-nav" id="set-nav" aria-label="Разделы настроек">${SECTIONS.map(([id, label], i) =>
            `<button type="button" class="${i ? '' : 'active'}" data-action="settings-jump" data-target="${id}">${label}</button>`).join('')}</nav>
          <div class="settings-main">
            <section id="set-sec-access">${head('Доступ', 'Чем программа заходит на Диск и куда кладёт файлы. Всё хранится только на этом компьютере.')}
              <div class="card" id="set-access"></div></section>
            <section id="set-sec-what">${head('Что скачивать', 'Откуда и какие файлы сохранять. По умолчанию программа качает всё.')}
              <div class="card">
                <p class="tiny" id="set-what-note"></p>
                <div class="panel-sec" id="set-what"><div class="section-title">Источники</div><div id="set-sources"></div></div>
                <div class="panel-sec folders" id="set-folders">
                  <div class="section-title">Папки Диска</div>
                  <fieldset class="bare" data-field="folders-set">
                    <legend class="sr-only">Какие папки Диска скачивать</legend>
                    <div class="checks two">
                      <label class="check"><input type="radio" name="folders-mode" value="all" data-field="folders-mode">
                        <span><b>Весь Диск</b><span class="tiny">все папки и файлы, и общие тоже</span></span></label>
                      <label class="check"><input type="radio" name="folders-mode" value="some" data-field="folders-mode">
                        <span><b>Только выбранные папки</b><span class="tiny">отметьте нужные ниже</span></span></label>
                    </div>
                    <div class="hidden" data-field="folders-picker">
                      <div class="folder-box">
                        <div class="folders-summary" data-field="folders-summary"></div>
                        <div class="tree" data-field="folders-tree"></div>
                      </div>
                      <p class="tiny" style="margin-top:10px">Значок «общая» — папка из общего доступа. На компьютере папки
                        будут разложены так же, как на Диске.</p>
                    </div>
                  </fieldset>
                  <p class="tiny" data-field="folders-off"></p>
                </div>
                <div class="panel-sec" id="set-what-types">
                  <div class="row" style="justify-content:space-between;margin-bottom:12px">
                    <div class="section-title" style="margin:0">Типы файлов</div>
                    <button class="link" data-action="settings-all" id="set-all">Отметить всё</button></div>
                  <div id="set-types"></div>
                  <p class="tiny" style="margin-top:12px">Это только фильтр скачивания: файлы лягут в те же папки, что на Диске.
                    В общие папки «Фото», «Документы» и другие их собирает только «Сортировка» — если вы сами её запустите.</p>
                </div>
              </div>
              <div class="savebar hidden" id="set-savebar" role="region" aria-label="Несохранённые изменения" style="margin-top:16px">
                <p><span class="dot wait"></span>Есть несохранённые изменения</p>
                <div class="row"><button data-action="settings-reset">Отменить</button>
                  <button class="primary" data-action="settings-save-what" id="set-save">Сохранить</button></div>
              </div>
            </section>
            <section id="set-sec-extra">${head('Дополнительно', 'Обычно менять ничего не нужно. Переключатели сохраняются сразу.')}
              <div class="card">
                <div class="switch"><div><b>Бережный режим</b>
                    <p>Вдвое меньше одновременных скачиваний. Включите, если Яндекс часто отвечает
                    «слишком много запросов» или интернет медленный.</p></div>
                  <input type="checkbox" data-field="opt-gentle" data-action="settings-toggle" data-option="gentle" aria-label="Бережный режим"></div>
                <div class="switch"><div><b>Сохранять уменьшенные копии фото, пока оригинал недоступен</b>
                    <p>Копия до 1280 пикселей ляжет в папку «_превью». Оригинал докачается сам, когда Яндекс его отдаст, —
                    тогда копия удалится.</p></div>
                  <input type="checkbox" data-field="opt-previews" data-action="settings-toggle" data-option="previews" aria-label="Страховочные превью фото"></div>
              </div></section>
            <section id="set-sec-danger">${head('Если что-то пошло не так', 'Нужно редко. Скачанные файлы в папке не удаляются.')}
              <div class="card"><details><summary>Забыть список файлов</summary><div class="body">
                <p class="small">Программа забудет список файлов и при следующем запуске соберёт его заново.
                Скачанные файлы в папке <b>не удаляются</b> и второй раз качаться не будут.</p>
                <div class="row" style="margin-top:12px">
                  <button class="danger" data-danger="1" data-action="forget-list" data-source="disk">Забыть список файлов Диска</button>
                  <button class="danger" data-danger="1" data-action="forget-list" data-source="unlim">Забыть список файлов Безлимита</button>
                </div><p class="tiny" id="set-danger-note"></p></div></details></div></section>
          </div>
        </div>`;
      App.Access.mount(byId('set-access'));
      byId('set-what').addEventListener('change', () => { formDirty = true; paintSavebar(); });
      byId('set-what-types').addEventListener('change', () => { formDirty = true; paintSavebar(); });
      byId('set-folders').addEventListener('change', onFoldersChange);
    },
    show() {
      formDirty = false;
      // Несохранённый выбор папок забываем, как и галочки типов; загруженное дерево оставляем
      picker.dirty = false;
      picker.synced = false;
      picker.errors.clear();
      paintSavebar();
      loadCounts();
      setTimeout(watchSections, 0);
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
      App.text(byId('set-what-note'), running ? 'Изменить можно после остановки скачивания.' : '');
      byId('set-what-note').classList.toggle('hidden', !running);
    },
  };

  App.action('settings-jump', (btn) => {
    const target = byId(btn.dataset.target);
    if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
  });

  App.action('settings-reset', () => {
    formDirty = false;
    picker.dirty = false;
    picker.synced = false;
    paintSavebar();
    if (App.state.status) App.screens.settings.update(App.state.status);
  });

  App.action('settings-all', () => {
    for (const box of byId('set-types').querySelectorAll('input[type=checkbox]')) box.checked = true;
    formDirty = true;
    paintSavebar();
  });

  App.action('settings-save-what', (btn) => App.run(btn, async () => {
    const root = byId('set-root');
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
      paintSavebar();
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
    paintSavebar();
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
