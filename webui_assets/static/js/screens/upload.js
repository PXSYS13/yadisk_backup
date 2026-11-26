/* Загрузка фото и видео с компьютера в безлимитное хранилище Яндекс.Диска (/photounlim).
   Использует общие куки cookies.json из настроек программы; исходная папка на диске только читается. */
(function () {
  'use strict';
  const App = window.App;
  const { esc, fmt, byId } = App;

  let upStatus = null;
  let filesFilter = 'all';
  let filesSearch = '';
  let filesOffset = 0;
  let lastFetchAt = 0;
  let fetchInFlight = false;
  let pendingForce = false;
  let wasRunning = false;
  let filesLoading = false;
  let prefillPath = '';
  const dirtyFields = new Set();

  const STATUS_LABELS = {
    pending: 'В очереди',
    uploading: 'Загружается',
    uploaded: 'Загружен',
    skipped_remote: 'Уже в облаке',
    skipped_dup_local: 'Дубликат на диске',
    skipped_limit: 'Превышен лимит',
    skipped_unsupported: 'Пропущен формат',
    failed: 'Ошибка',
  };

  function statusPill(st) {
    const label = STATUS_LABELS[st] || st || '—';
    let cls = 'pill';
    if (st === 'uploaded') cls += ' ok';
    else if (st === 'failed') cls += ' err';
    else if (st === 'uploading') cls += ' info';
    else if (String(st || '').startsWith('skipped')) cls += ' warn';
    return `<span class="${cls}">${esc(label)}</span>`;
  }

  async function refreshUploadStatus(force = false) {
    if (fetchInFlight) {
      if (force) pendingForce = true;
      return;
    }
    const now = Date.now();
    if (!force && now - lastFetchAt < 1200) return;
    fetchInFlight = true;
    try {
      do {
        pendingForce = false;
        const res = await App.api.get('/api/upload/status');
        if (pendingForce) continue;
        if (res.ok && res.data) {
          const prevRunning = wasRunning;
          upStatus = res.data;
          lastFetchAt = Date.now();
          wasRunning = !!upStatus.running;
          paintUpload();
          if (prevRunning && !wasRunning) {
            const phase = (upStatus.live && upStatus.live.phase) || upStatus.mode || 'done';
            if (phase === 'error' || (upStatus.exit_code && upStatus.exit_code !== 0 && upStatus.exit_code !== 130)) {
              App.toast(upStatus.message || 'Операция завершилась с ошибкой', 'err');
            } else if (phase === 'stopped' || upStatus.exit_code === 130) {
              App.toast(upStatus.message || 'Операция остановлена', 'warn');
            } else {
              App.toast(upStatus.message || 'Готово', 'ok');
            }
            loadUploadFiles();
          }
        } else if (!res.ok) {
          const phaseEl = byId('up-phase-text');
          if (phaseEl) App.text(phaseEl, res.error || 'Не удалось получить статус загрузки');
        }
      } while (pendingForce);
    } finally {
      fetchInFlight = false;
    }
  }

  async function loadUploadFiles() {
    if (filesLoading) return;
    filesLoading = true;
    try {
      const params = new URLSearchParams({
        status: filesFilter,
        search: filesSearch,
        offset: String(filesOffset),
        limit: '50',
      });
      const res = await App.api.get(`/api/upload/files?${params.toString()}`);
      const tbody = byId('up-files-tbody');
      const countEl = byId('up-files-count');
      if (!tbody || !countEl) return;
      if (!res.ok || !res.data) {
        App.text(countEl, res.error || 'Не удалось загрузить список файлов');
        return;
      }
      const items = res.data.items || [];
      const total = res.data.total || 0;
      App.text(countEl, `Показано ${items.length} из ${fmt.num(total)}`);
      if (!items.length) {
        App.html(tbody, '<tr><td colspan="5" class="muted">Файлов по выбранному фильтру нет.</td></tr>');
        return;
      }
      App.html(tbody, items.map((row) => `<tr>
        <td class="name">${esc(row.rel_path || '')}</td>
        <td>${row.media_kind === 'video' ? 'Видео' : 'Фото'}</td>
        <td>${esc(fmt.bytes(row.size || 0))}</td>
        <td>${statusPill(row.status)}</td>
        <td class="tiny">${esc(row.error || row.remote_id || '')}</td>
      </tr>`).join(''));
    } finally {
      filesLoading = false;
    }
  }

  function paintUpload() {
    const st = upStatus;
    if (!st || !byId('up-src')) return;
    const cfg = st.config || {};
    const stats = st.stats || {};
    const live = st.live || {};
    const ck = st.cookies || {};
    const acc = st.account || null;
    const running = !!st.running;

    const srcInput = byId('up-src');
    if (prefillPath) {
      srcInput.value = prefillPath;
      prefillPath = '';
      dirtyFields.add('source_dir');
    } else if (!dirtyFields.has('source_dir') && document.activeElement !== srcInput && cfg.source_dir) {
      srcInput.value = cfg.source_dir;
    }

    const wInput = byId('up-workers');
    if (!dirtyFields.has('max_workers') && document.activeElement !== wInput) {
      wInput.value = String(cfg.max_workers || 2);
    }
    const cInput = byId('up-chunk');
    if (!dirtyFields.has('chunk_mb') && document.activeElement !== cInput) {
      cInput.value = String(cfg.chunk_mb || 16);
    }
    const guardCb = byId('up-strict-guard');
    if (!dirtyFields.has('strict_quota_guard') && document.activeElement !== guardCb) {
      guardCb.checked = cfg.strict_quota_guard !== false;
    }
    const watchCb = byId('up-watch');
    if (!dirtyFields.has('watch_mode') && document.activeElement !== watchCb) {
      watchCb.checked = !!cfg.watch_mode;
    }

    // Статус кук и безлимита
    const ckBanner = byId('up-auth-banner');
    if (!ck.present || !ck.ok) {
      ckBanner.className = 'banner warn';
      App.html(ckBanner, `<span class="b-icon">${App.icon('alert')}</span><div class="grow">
        <h3>Нужен вход в Яндекс.Диск (cookies.json)</h3>
        <p>${esc(ck.error || 'Добавьте куки в разделе «Настройки» — они общие для скачивания и загрузки в Безлимит.')}</p>
        <div class="row" style="margin-top:8px">
          <button type="button" class="primary" data-action="up-open-settings">${App.icon('sliders')}Открыть Настройки</button>
          <button type="button" data-action="up-check-cookies">${App.icon('refresh')}Проверить вход</button>
        </div></div>`);
    } else if (acc) {
      const okUnlim = !!acc.unlimited_free;
      ckBanner.className = okUnlim ? 'banner ok' : 'banner warn';
      const usedTxt = acc.used != null && acc.limit != null
        ? `Обычный Диск: ${fmt.bytes(acc.used)} из ${fmt.bytes(acc.limit)} · Безлимит фото: ${fmt.bytes(acc.actual_photounlim_size || acc.photounlim || 0)}`
        : 'Подключение активно';
      App.html(ckBanner, `<span class="b-icon">${App.icon(okUnlim ? 'shield-check' : 'alert')}</span><div class="grow">
        <h3>${okUnlim ? 'Безлимит для фото и видео активен' : 'Проверьте статус безлимита'}</h3>
        <p>${esc(acc.warning || usedTxt)}</p>
      </div><button type="button" data-action="up-check-cookies">${App.icon('refresh')}Обновить</button>`);
    } else {
      ckBanner.className = 'banner info';
      App.html(ckBanner, `<span class="b-icon">${App.icon('key')}</span><div class="grow">
        <h3>Куки подключены (общие с бэкапом)</h3>
        <p>Можно сразу сканировать папку или проверить статус безлимитной подписки.</p>
      </div><button type="button" data-action="up-check-cookies">${App.icon('shield-check')}Проверить безлимит</button>`);
    }

    // Кнопки управления
    byId('up-pick-btn').disabled = running;
    byId('up-scan-btn').disabled = running;
    byId('up-start-btn').disabled = running || !ck.ok;
    byId('up-stop-btn').classList.toggle('hidden', !running);
    byId('up-retry-btn').disabled = running || !(stats.failed > 0);
    byId('up-reset-btn').disabled = running;

    // Прогресс и счётчики
    const phaseMap = {
      idle: 'Ожидание',
      scanning: 'Сканирование папки',
      indexing: 'Сверка с облаком (/photounlim)',
      uploading: 'Загрузка в Безлимит',
      watching: 'Ожидание новых файлов (Watch)',
      done: 'Завершено',
      stopped: 'Остановлено',
      error: 'Ошибка',
    };
    const phaseLabel = phaseMap[live.phase || st.mode || 'idle'] || (live.phase || 'Ожидание');
    App.text(byId('up-phase-text'), st.message || phaseLabel);
    const mediaTotal = Number(stats.media_files != null ? stats.media_files : (stats.total_files || 0));
    const totalBytes = Number(stats.media_bytes || 0);
    const skippedMedia = Number(stats.skipped_remote || 0) + Number(stats.skipped_dup_local || 0);
    const skippedAll = skippedMedia + Number(stats.skipped_unsupported || 0);
    const doneCount = Number(stats.uploaded || 0) + skippedMedia;
    const pct = mediaTotal > 0 ? Math.min(100, Math.round((doneCount / mediaTotal) * 100)) : 0;
    const bar = byId('up-progress');
    bar.max = 100;
    bar.value = pct;
    App.text(byId('up-progress-pct'), `${pct}%`);

    App.html(byId('up-kpis'), `
      <div class="stat"><span class="stat-label">Фото и видео</span><b>${fmt.num(mediaTotal)}</b><span class="tiny">${esc(fmt.bytes(totalBytes))}</span></div>
      <div class="stat"><span class="stat-label">Загружено</span><b class="ok-text">${fmt.num(stats.uploaded || 0)}</b><span class="tiny">${esc(fmt.bytes(stats.uploaded_bytes || 0))}</span></div>
      <div class="stat"><span class="stat-label">В очереди</span><b>${fmt.num(stats.pending || 0)}</b><span class="tiny">${esc(fmt.bytes(stats.pending_bytes || 0))}</span></div>
      <div class="stat"><span class="stat-label">Уже в облаке / пропуск</span><b>${fmt.num(skippedAll)}</b><span class="tiny">без дубликатов</span></div>
      <div class="stat"><span class="stat-label">Ошибок</span><b class="${stats.failed ? 'err-text' : ''}">${fmt.num(stats.failed || 0)}</b><span class="tiny">можно повторить</span></div>
    `);

    const events = st.events || [];
    App.html(byId('up-events'), events.length
      ? events.slice(0, 20).map((ev) => `<div class="feed-line ${esc(ev.kind || 'info')}">
          <span class="tiny">${esc(ev.at || '')}</span> <span>${esc(ev.text || '')}</span>
        </div>`).join('')
      : '<p class="tiny">События появятся после сканирования или запуска загрузки.</p>');
  }

  App.uploadPrefillDir = (dirPath) => {
    prefillPath = String(dirPath || '').trim();
    const inp = byId('up-src');
    if (inp && prefillPath) {
      inp.value = prefillPath;
      prefillPath = '';
    }
  };

  App.screens.upload = {
    mount(el) {
      el.innerHTML = `<div class="page-head">
          <h1>Загрузить в Безлимит</h1>
          <p>Загружает фото и видео из папки на компьютере в безлимитный раздел Яндекс.Диска (<code>/photounlim</code>).
          Оригиналы на компьютере остаются на месте — папка только читается, дубликаты пропускаются по хешу.</p>
        </div>
        <div class="stack">
          <div id="up-auth-banner" class="banner info"></div>

          <div class="card">
            <div class="card-head">
              <span class="icon-box">${App.icon('upload')}</span>
              <div class="grow"><h2>Папка и параметры загрузки</h2></div>
            </div>
            <label class="field"><span>Папка с фото и видео на компьютере</span>
              <div class="row">
                <input type="text" id="up-src" class="grow" placeholder="Например: D:\\Фото\\Семья" spellcheck="false">
                <button type="button" data-action="up-pick" id="up-pick-btn">${App.icon('folder')}Выбрать папку…</button>
              </div>
            </label>
            <div class="grid-2" style="margin-top:12px">
              <label class="field"><span>Потоков загрузки (1–3)</span>
                <input type="number" id="up-workers" min="1" max="3" value="2">
              </label>
              <label class="field"><span>Размер чанка (МБ, 4–64)</span>
                <input type="number" id="up-chunk" min="4" max="64" value="16">
              </label>
            </div>
            <div class="row" style="margin-top:12px">
              <label class="check-inline"><input type="checkbox" id="up-strict-guard" checked> <span>Останавливать, если начнёт расходоваться обычный Диск</span></label>
              <label class="check-inline"><input type="checkbox" id="up-watch"> <span>Режим наблюдения (автодогрузка новых файлов)</span></label>
            </div>
            <div class="row" style="margin-top:16px">
              <button type="button" data-action="up-scan" id="up-scan-btn">${App.icon('search')}Проверить папку</button>
              <button type="button" class="primary" data-action="up-start" id="up-start-btn">${App.icon('upload')}Загрузить в Безлимит</button>
              <button type="button" class="danger hidden" data-action="up-stop" id="up-stop-btn">${App.icon('stop')}Остановить</button>
              <button type="button" data-action="up-save-cfg" id="up-save-btn">${App.icon('check')}Сохранить настройки</button>
              <span class="spacer"></span>
              <button type="button" data-action="up-retry" id="up-retry-btn">${App.icon('refresh')}Повторить ошибки</button>
              <button type="button" data-action="up-reset" id="up-reset-btn">${App.icon('trash')}Очистить очередь</button>
            </div>
          </div>

          <div class="card">
            <div class="card-head">
              <span class="icon-box">${App.icon('activity')}</span>
              <div class="grow"><h2>Прогресс загрузки</h2><p class="tiny" id="up-phase-text">Ожидание</p></div>
              <b id="up-progress-pct">0%</b>
            </div>
            <progress id="up-progress" class="bar-native" value="0" max="100"></progress>
            <div class="stats-grid" id="up-kpis" style="margin-top:14px"></div>
            <div class="feed-box" id="up-events" style="margin-top:14px"></div>
          </div>

          <div class="card">
            <div class="card-head">
              <span class="icon-box">${App.icon('list')}</span>
              <div class="grow"><h2>Очередь файлов</h2><p class="tiny" id="up-files-count">Нажмите «Обновить список»</p></div>
              <a class="btn-link" href="/api/upload/export.csv" target="_blank" rel="noopener">Скачать CSV</a>
            </div>
            <div class="row" style="margin-bottom:10px">
              <select id="up-filter-status">
                <option value="all">Все статусы</option>
                <option value="pending">В очереди</option>
                <option value="uploaded">Загружены</option>
                <option value="skipped">Пропущены / уже в облаке</option>
                <option value="failed">С ошибкой</option>
              </select>
              <input type="text" id="up-filter-search" class="grow" placeholder="Поиск по имени файла…">
              <button type="button" data-action="up-load-files" id="up-load-files-btn">${App.icon('refresh')}Обновить список</button>
            </div>
            <div class="table-wrap">
              <table class="list">
                <thead><tr><th>Файл</th><th>Тип</th><th>Размер</th><th>Статус</th><th>Комментарий</th></tr></thead>
                <tbody id="up-files-tbody"><tr><td colspan="5" class="muted">Список пока не загружен.</td></tr></tbody>
              </table>
            </div>
          </div>
        </div>`;

      byId('up-src').addEventListener('input', () => dirtyFields.add('source_dir'));
      byId('up-workers').addEventListener('input', () => dirtyFields.add('max_workers'));
      byId('up-chunk').addEventListener('input', () => dirtyFields.add('chunk_mb'));
      byId('up-strict-guard').addEventListener('change', () => dirtyFields.add('strict_quota_guard'));
      byId('up-watch').addEventListener('change', () => dirtyFields.add('watch_mode'));

      byId('up-filter-status').addEventListener('change', (ev) => {
        filesFilter = ev.target.value;
        filesOffset = 0;
        loadUploadFiles();
      });
      byId('up-filter-search').addEventListener('keydown', (ev) => {
        if (ev.key === 'Enter') {
          filesSearch = ev.target.value.trim();
          filesOffset = 0;
          loadUploadFiles();
        }
      });
    },
    show() {
      refreshUploadStatus(true);
      loadUploadFiles();
    },
    update() {
      refreshUploadStatus(false);
    },
  };

  function collectSettings() {
    const rawW = Number(byId('up-workers').value);
    const rawC = Number(byId('up-chunk').value);
    return {
      source_dir: byId('up-src').value.trim(),
      max_workers: Number.isFinite(rawW) && rawW >= 1 ? Math.trunc(rawW) : 2,
      chunk_mb: Number.isFinite(rawC) && rawC >= 4 ? Math.trunc(rawC) : 16,
      strict_quota_guard: !!byId('up-strict-guard').checked,
      watch_mode: !!byId('up-watch').checked,
    };
  }

  App.action('up-open-settings', () => App.go('#/settings'));

  App.action('up-check-cookies', (btn) => App.run(btn, async () => {
    const res = await App.api.post('/api/upload/cookies/check', {});
    await refreshUploadStatus(true);
    if (res.ok) App.toast('Куки проверены, связь с Яндекс.Диском активна', 'ok');
    return res;
  }, { okToast: false }));

  App.action('up-pick', (btn) => App.run(btn, async () => {
    const res = await App.api.post('/api/upload/folder/pick', {});
    if (res.ok && res.data && res.data.picked && res.data.path) {
      byId('up-src').value = res.data.path;
      dirtyFields.delete('source_dir');
    }
    return res;
  }, { okToast: false }));

  App.action('up-save-cfg', (btn) => App.run(btn, async () => {
    const res = await App.api.post('/api/upload/settings', collectSettings());
    if (res.ok) {
      dirtyFields.clear();
      App.toast('Настройки загрузки сохранены', 'ok');
    }
    await refreshUploadStatus(true);
    return res;
  }, { okToast: false }));

  App.action('up-scan', (btn) => App.run(btn, async () => {
    const cfg = collectSettings();
    if (!cfg.source_dir) return { ok: false, error: 'Укажите папку с фото и видео' };
    const cfgRes = await App.api.post('/api/upload/settings', cfg);
    if (!cfgRes.ok) return cfgRes;
    dirtyFields.clear();
    const res = await App.api.post('/api/upload/scan', { source_dir: cfg.source_dir });
    if (res.ok) wasRunning = true;
    await refreshUploadStatus(true);
    return res;
  }, { okToast: false }));

  App.action('up-start', (btn) => App.run(btn, async () => {
    const cfg = collectSettings();
    if (!cfg.source_dir) return { ok: false, error: 'Укажите папку с фото и видео' };
    const cfgRes = await App.api.post('/api/upload/settings', cfg);
    if (!cfgRes.ok) return cfgRes;
    dirtyFields.clear();
    const res = await App.api.post('/api/upload/start', {
      source_dir: cfg.source_dir,
      watch_mode: cfg.watch_mode,
    });
    if (res.ok) wasRunning = true;
    await refreshUploadStatus(true);
    return res;
  }, { okToast: false }));

  App.action('up-stop', (btn) => App.run(btn, async () => {
    const res = await App.api.post('/api/upload/stop', {});
    await refreshUploadStatus(true);
    return res;
  }, { okToast: false }));

  App.action('up-retry', (btn) => App.run(btn, async () => {
    const res = await App.api.post('/api/upload/retry-failed', {});
    await refreshUploadStatus(true);
    await loadUploadFiles();
    if (res.ok) App.toast(`Возвращено в очередь: ${(res.data && res.data.requeued) || 0}`, 'ok');
    return res;
  }, { okToast: false }));

  App.action('up-reset', async (btn) => {
    const yes = await App.confirm({
      title: 'Очистить локальную очередь загрузки?',
      text: 'Сбросится только список очереди в базе. Оригиналы на компьютере и уже загруженные фото в облаке останутся на месте.',
      ok: 'Очистить',
      danger: true,
    });
    if (!yes) return;
    await App.run(btn, async () => {
      const res = await App.api.post('/api/upload/reset', {});
      await refreshUploadStatus(true);
      await loadUploadFiles();
      return res;
    }, { okToast: false });
  });

  App.action('up-load-files', () => {
    filesSearch = (byId('up-filter-search').value || '').trim();
    filesOffset = 0;
    loadUploadFiles();
  });
})();
