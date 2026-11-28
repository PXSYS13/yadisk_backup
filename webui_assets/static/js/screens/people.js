/* Сортировка фото и видео по людям: 4 шага — поиск лиц, разметка и взросление детей,
   правила и предпросмотр (Dry-Run), сортировка и безопасный откат. */
(function () {
  'use strict';

  const App = window.App;
  const { esc, fmt, byId } = App;

  const PHASE_LABELS = {
    idle: 'Ожидание',
    downloading_model: 'Загрузка моделей распознавания',
    scanning: 'Сканирование папки',
    detecting: 'Поиск лиц на фото и видео',
    clustering: 'Кластеризация лиц по людям',
    sorting: 'Сортировка файлов',
    rollback: 'Безопасный откат',
    cancelled: 'Остановлено',
    done: 'Готово',
    error: 'Ошибка',
  };

  const MODE_LABELS = {
    hardlink: 'Жёсткие ссылки',
    copy: 'Копирование',
    move: 'Перемещение',
  };

  const CATEGORY_LABELS = {
    person: 'По людям',
    shared: 'Совместные',
    by_date: 'По датам',
    unnamed: 'Нераспознанные',
    no_faces: 'Без лиц',
  };

  const RUN_STATUS_LABELS = {
    running: 'Выполняется',
    completed: 'Завершён',
    partial: 'Частично',
    cancelled: 'Остановлен',
    rolled_back: 'Откачен',
    failed: 'Ошибка',
  };

  let activeStep = 1;
  let studioSubtab = 'all'; // 'all' | 'bridges' | 'ignored'
  let bridgeIndex = 0;

  let statusData = null;
  let persons = [];
  let ageBridges = [];
  let runs = [];

  let studioSearch = '';
  let studioFilter = 'all'; // 'all' | 'named' | 'unnamed'
  const selectedPersonIds = new Set();

  let lastPlan = null;
  let planDirty = false;

  let wasRunning = false;
  let lastRunningTask = null;

  let statusFetchInFlight = false;
  let statusPendingForce = false;
  let lastStatusFetchAt = 0;

  let studioLoading = false;
  let pendingReload = false;

  let runsLoading = false;
  let pendingRunsReload = false;
  let lastRunsPollAt = 0;

  let inspectorPersonId = null;
  let inspectorReqId = 0;
  let inspectorFaces = [];
  const inspectorSelectedFaceIds = new Set();

  let configHydrated = false;
  const dirtyFields = new Set();
  let keyListenerBound = false;

  // ── Вспомогательные функции ──

  function isCannotLinkError(res) {
    const raw = (res && (res.error || (res.data && (res.data.detail || res.data.msg)))) || '';
    const msg = String(raw).toLowerCase();
    return (
      msg.includes('cannot_link') ||
      msg.includes('на одном фото') ||
      msg.includes('разделены пользователем') ||
      msg.includes('409')
    );
  }

  function formatYearSpan(minY, maxY) {
    if (minY && maxY) return minY === maxY ? String(minY) : `${minY}–${maxY}`;
    if (minY) return String(minY);
    if (maxY) return String(maxY);
    return '';
  }

  function formatTimestamp(ts) {
    if (!ts) return '—';
    const d = new Date(Number(ts) * 1000);
    if (Number.isNaN(d.getTime())) return '—';
    return d.toLocaleString('ru-RU', {
      day: '2-digit',
      month: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
    });
  }

  function personDisplayName(p) {
    if (!p) return 'Без имени';
    const nm = String(p.name || '').trim();
    return nm || `Человек #${p.id}`;
  }

  function phasePillClass(phase, running) {
    if (running) return 'pill live';
    if (phase === 'done') return 'pill ok';
    if (phase === 'error') return 'pill err';
    if (phase === 'cancelled') return 'pill wait';
    return 'pill';
  }

  function runStatusPill(status) {
    const label = RUN_STATUS_LABELS[status] || status || '—';
    let cls = 'pill';
    if (status === 'completed') cls = 'pill ok';
    else if (status === 'running') cls = 'pill live';
    else if (status === 'partial' || status === 'cancelled') cls = 'pill wait';
    else if (status === 'failed') cls = 'pill err';
    return { cls, label };
  }

  function parseBirthYear(raw) {
    const s = String(raw == null ? '' : raw).trim();
    if (!s) return { empty: true, value: null };
    const n = Number(s);
    if (!Number.isInteger(n) || n < 1850 || n > 2100) {
      return { empty: false, invalid: true, value: null };
    }
    return { empty: false, invalid: false, value: n };
  }

  function invalidatePlan() {
    lastPlan = null;
    planDirty = true;
    renderPlanPreview();
    paintStepBadges();
    paintStep4Summary();
  }

  function collectSortOptions() {
    const srcEl = byId('ppl-src');
    const dstEl = byId('ppl-dst');
    return {
      source_dir: srcEl ? srcEl.value.trim() : '',
      target_dir: dstEl ? dstEl.value.trim() : '',
      mode: (byId('ppl-opt-mode') && byId('ppl-opt-mode').value) || 'hardlink',
      tree_layout: (byId('ppl-opt-layout') && byId('ppl-opt-layout').value) || 'people_first',
      include_child_age: !!(byId('ppl-opt-child-age') && byId('ppl-opt-child-age').checked),
      group_policy: (byId('ppl-opt-group') && byId('ppl-opt-group').value) || 'each_person',
      no_faces_policy: (byId('ppl-opt-nofaces') && byId('ppl-opt-nofaces').value) || 'folder',
      unnamed_policy: (byId('ppl-opt-unnamed') && byId('ppl-opt-unnamed').value) || 'folder',
      skip_ignored: !!(byId('ppl-opt-skip-ignored') && byId('ppl-opt-skip-ignored').checked),
      preserve_dates: !!(byId('ppl-opt-preserve-dates') && byId('ppl-opt-preserve-dates').checked),
    };
  }

  function collectStep1Settings() {
    return {
      source_dir: (byId('ppl-src') && byId('ppl-src').value.trim()) || '',
      model_pack: (byId('ppl-model-pack') && byId('ppl-model-pack').value) || 'antelopev2',
      prefer_gpu: !!(byId('ppl-prefer-gpu') && byId('ppl-prefer-gpu').checked),
    };
  }

  function setActiveStep(step) {
    const n = Math.max(1, Math.min(4, Number(step) || 1));
    activeStep = n;
    for (let i = 1; i <= 4; i++) {
      const panel = byId(`ppl-panel-${i}`);
      const tab = byId(`ppl-tab-${i}`);
      if (panel) panel.classList.toggle('hidden', i !== n);
      if (tab) {
        tab.classList.toggle('active', i === n);
        tab.setAttribute('aria-selected', i === n ? 'true' : 'false');
      }
    }
    if (n === 2) {
      refreshStudio();
    } else if (n === 4) {
      paintStep4Summary();
      loadRuns();
    }
  }

  function setStudioSubtab(tab) {
    if (tab !== 'all' && tab !== 'bridges' && tab !== 'ignored') return;
    studioSubtab = tab;
    selectedPersonIds.clear();
    paintStudioSubtabs();
    renderStudioContent();
  }

  function paintStudioSubtabs() {
    const tabs = ['all', 'bridges', 'ignored'];
    for (const t of tabs) {
      const btn = byId(`ppl-subtab-${t}`);
      if (btn) btn.classList.toggle('active', studioSubtab === t);
    }
    const wallWrap = byId('ppl-wall-wrap');
    const bridgesWrap = byId('ppl-bridges-wrap');
    if (wallWrap) wallWrap.classList.toggle('hidden', studioSubtab === 'bridges');
    if (bridgesWrap) bridgesWrap.classList.toggle('hidden', studioSubtab !== 'bridges');

    const filterEl = byId('ppl-filter');
    if (filterEl) filterEl.classList.toggle('hidden', studioSubtab === 'ignored');
  }

  // ── Загрузка данных с /api/people/* ──

  async function refreshPeopleStatus(force = false) {
    const now = Date.now();
    const minGap = statusData && statusData.running ? 750 : 1200;
    if (statusFetchInFlight) {
      if (force) statusPendingForce = true;
      return;
    }
    if (!force && now - lastStatusFetchAt < minGap) return;
    statusFetchInFlight = true;
    try {
      do {
        statusPendingForce = false;
        const res = await App.api.get('/api/people/status');
        if (statusPendingForce) continue;
        if (res.ok && res.data) {
          const prevRunning = wasRunning;
          const prevTask = lastRunningTask;
          statusData = res.data;
          lastStatusFetchAt = Date.now();
          const nowRunning = !!statusData.running;
          if (nowRunning) {
            lastRunningTask = statusData.task || lastRunningTask;
          }
          wasRunning = nowRunning;

          paintStatus();

          if (nowRunning && (statusData.task === 'sort' || statusData.task === 'rollback')) {
            if (Date.now() - lastRunsPollAt >= 1800) {
              lastRunsPollAt = Date.now();
              loadRuns();
            }
          }

          if (prevRunning && !nowRunning) {
            handleTaskCompleted(statusData, prevTask);
          }
        } else if (!res.ok && force) {
          App.toast(res.error || 'Не удалось получить статус сортировки по людям', 'err');
        }
      } while (statusPendingForce);
    } finally {
      statusFetchInFlight = false;
    }
  }

  function handleTaskCompleted(st, finishedTask) {
    const phase = st.phase || 'done';
    if (phase === 'done') {
      if (st.error) {
        App.toast(st.message || st.error || 'Завершено с предупреждениями', 'warn');
      } else {
        App.toast(st.message || 'Операция завершена', 'ok');
      }
    } else if (phase === 'cancelled') {
      App.toast(st.message || 'Операция остановлена', 'warn');
    } else if (phase === 'error') {
      App.toast(st.error || st.message || 'Ошибка выполнения операции', 'err');
    }
    if (finishedTask === 'scan' || finishedTask === 'cluster') {
      invalidatePlan();
    }
    refreshStudio();
    loadRuns();
  }

  async function refreshStudio() {
    if (studioLoading) {
      pendingReload = true;
      return;
    }
    studioLoading = true;
    try {
      do {
        pendingReload = false;
        const [pRes, bRes] = await Promise.all([
          App.api.get('/api/people/persons?include_ignored=true'),
          App.api.get('/api/people/age-bridges?top_k=25'),
        ]);
        if (!pRes.ok) {
          App.toast(pRes.error || 'Не удалось загрузить список людей', 'err');
        } else if (pRes.data) {
          persons = Array.isArray(pRes.data.items) ? pRes.data.items : [];
          const validIds = new Set(persons.map((p) => Number(p.id)));
          for (const sid of Array.from(selectedPersonIds)) {
            if (!validIds.has(sid)) selectedPersonIds.delete(sid);
          }
        }
        if (!bRes.ok) {
          App.toast(bRes.error || 'Не удалось загрузить очередь возрастных совпадений', 'err');
        } else if (bRes.data) {
          ageBridges = Array.isArray(bRes.data.items) ? bRes.data.items : [];
          if (bridgeIndex >= ageBridges.length) bridgeIndex = 0;
        }
        paintStudioCounts();
        renderStudioContent();
        paintStepBadges();
      } while (pendingReload);
    } finally {
      studioLoading = false;
    }
  }

  async function loadRuns() {
    if (runsLoading) {
      pendingRunsReload = true;
      return;
    }
    runsLoading = true;
    try {
      do {
        pendingRunsReload = false;
        const res = await App.api.get('/api/people/runs');
        if (!res.ok) {
          App.toast(res.error || 'Не удалось загрузить журнал запусков', 'err');
        } else if (res.data) {
          runs = Array.isArray(res.data.items) ? res.data.items : [];
          renderRunsTable();
          paintSortProgress();
          paintStepBadges();
        }
      } while (pendingRunsReload);
    } finally {
      runsLoading = false;
    }
  }

  // ── Отрисовка состояния, настроек, бейджей ──

  function hydrateConfigFields(cfg) {
    if (!cfg) return;
    const setVal = (id, key, val) => {
      const el = byId(id);
      if (!el || document.activeElement === el) return;
      if (configHydrated && dirtyFields.has(key)) return;
      if (el.type === 'checkbox') {
        el.checked = !!val;
      } else if (val != null) {
        el.value = String(val);
      }
    };

    const srcInput = byId('ppl-src');
    if (srcInput && document.activeElement !== srcInput && !dirtyFields.has('source_dir')) {
      const fallbackDl =
        App.state.status && App.state.status.config && App.state.status.config.download_dir;
      const desired = cfg.source_dir || (!configHydrated && fallbackDl) || '';
      if (desired && !srcInput.value) srcInput.value = desired;
      else if (cfg.source_dir && !configHydrated) srcInput.value = cfg.source_dir;
    }

    setVal('ppl-model-pack', 'model_pack', cfg.model_pack || 'antelopev2');
    setVal('ppl-prefer-gpu', 'prefer_gpu', cfg.prefer_gpu !== false);
    setVal('ppl-dst', 'target_dir', cfg.target_dir || '');
    setVal('ppl-opt-mode', 'mode', cfg.mode || 'hardlink');
    setVal('ppl-opt-layout', 'tree_layout', cfg.tree_layout || 'people_first');
    setVal('ppl-opt-group', 'group_policy', cfg.group_policy || 'each_person');
    setVal('ppl-opt-nofaces', 'no_faces_policy', cfg.no_faces_policy || 'folder');
    setVal('ppl-opt-unnamed', 'unnamed_policy', cfg.unnamed_policy || 'folder');
    setVal('ppl-opt-child-age', 'include_child_age', cfg.include_child_age !== false);
    setVal('ppl-opt-skip-ignored', 'skip_ignored', cfg.skip_ignored !== false);
    setVal('ppl-opt-preserve-dates', 'preserve_dates', cfg.preserve_dates !== false);

    configHydrated = true;
  }

  function paintStepBadges() {
    const stats = (statusData && statusData.stats) || {};
    const running = !!(statusData && statusData.running);
    const task = statusData && statusData.task;

    const b1 = byId('ppl-badge-1');
    if (b1) {
      if (running && (task === 'scan' || task === 'cluster')) {
        App.text(b1, `${Math.round(((statusData.progress || {}).percent) || 0)}%`);
      } else if (stats.media_total > 0) {
        App.text(b1, fmt.num(stats.media_total));
      } else {
        App.text(b1, '');
      }
    }

    const bridgeCount = Math.max(
      Number((statusData && statusData.age_bridge_count) || 0),
      ageBridges.length
    );
    const b2 = byId('ppl-badge-2');
    if (b2) {
      if (bridgeCount > 0) {
        b2.className = 'ppl-step-badge warn';
        App.text(b2, `${fmt.num(stats.persons_total || persons.length)} · +${fmt.num(bridgeCount)}`);
      } else if ((stats.persons_total || persons.length) > 0) {
        b2.className = 'ppl-step-badge';
        App.text(b2, fmt.num(stats.persons_total || persons.length));
      } else {
        b2.className = 'ppl-step-badge';
        App.text(b2, '');
      }
    }

    const b3 = byId('ppl-badge-3');
    if (b3) {
      App.text(b3, lastPlan && !planDirty ? fmt.num(lastPlan.total_ops || 0) : '');
    }

    const b4 = byId('ppl-badge-4');
    if (b4) {
      const rc = runs.length || Number(stats.sort_runs || 0);
      if (running && (task === 'sort' || task === 'rollback')) {
        App.text(b4, `${Math.round(((statusData.progress || {}).percent) || 0)}%`);
      } else {
        App.text(b4, rc > 0 ? fmt.num(rc) : '');
      }
    }
  }

  function paintHardwarePill(model) {
    const pill = byId('ppl-hw-pill');
    if (!pill) return;
    const m = model || {};
    const providers = Array.isArray(m.providers) ? m.providers : [];
    const activeGpu = providers.find((p) => p && p !== 'CPUExecutionProvider');
    const gpuName = activeGpu
      ? activeGpu
          .replace('ExecutionProvider', '')
          .replace('Dml', 'DirectML')
          .replace('CUDA', 'CUDA')
          .replace('CoreML', 'CoreML')
      : '';
    const hwLabel = activeGpu
      ? `GPU: ${gpuName}`
      : m.gpu_available
        ? 'CPU (GPU доступна)'
        : 'CPU';
    const readyLabel = m.ready
      ? `Модель ${m.pack || 'antelopev2'} готова`
      : `Модель ${m.pack || 'antelopev2'} загрузится при старте`;
    const cls = activeGpu && m.ready ? 'ppl-hw-pill ok' : m.ready ? 'ppl-hw-pill info' : 'ppl-hw-pill warn';
    pill.className = cls;
    App.text(pill, `${hwLabel} · ${readyLabel}`);
  }

  function paintStatus() {
    const st = statusData;
    if (!st || !byId('ppl-src')) return;
    const cfg = st.config || {};
    const stats = st.stats || {};
    const prog = st.progress || {};
    const running = !!st.running;
    const phase = st.phase || 'idle';

    const unavailable = st.available === false;
    hydrateConfigFields(cfg);
    if (unavailable) {
      const pill = byId('ppl-hw-pill');
      if (pill) {
        pill.className = 'ppl-hw-pill warn';
        App.text(pill, 'Модуль недоступен на этой ОС');
      }
    } else {
      paintHardwarePill(st.model);
    }

    const unavailBanner = byId('ppl-unavail-banner');
    if (unavailBanner) {
      if (unavailable) {
        unavailBanner.classList.remove('hidden');
        App.text(
          byId('ppl-unavail-text'),
          st.unavailable_reason ||
            'Дополнительные библиотеки распознавания лиц недоступны на этой системе. Бэкап Яндекс.Диска, Безлимит, ссылки и загрузка работают полностью.'
        );
      } else {
        unavailBanner.classList.add('hidden');
      }
    }

    // Заметный баннер ошибки при наличии st.error
    const errBanner = byId('ppl-error-banner');
    if (errBanner) {
      if (st.error) {
        errBanner.classList.remove('hidden');
        App.text(byId('ppl-error-text'), st.error);
      } else {
        errBanner.classList.add('hidden');
      }
    }

    // Кнопки шага 1
    const locked = running || unavailable;
    byId('ppl-pick-src-btn').disabled = locked;
    byId('ppl-use-dl-btn').disabled = locked;
    byId('ppl-model-pack').disabled = locked;
    byId('ppl-prefer-gpu').disabled = locked;
    byId('ppl-scan-btn').disabled = locked;
    byId('ppl-cluster-btn').disabled = locked || !(stats.faces_active > 0);
    byId('ppl-reset-btn').disabled = locked;
    byId('ppl-stop-btn').classList.toggle('hidden', !running);

    // Прогресс шага 1
    const phaseLabel = PHASE_LABELS[phase] || phase;
    const phasePill = byId('ppl-phase-pill');
    phasePill.className = phasePillClass(phase, running);
    App.html(phasePill, `<span class="pdot"></span>${esc(phaseLabel)}`);
    App.text(byId('ppl-phase-msg'), st.message || phaseLabel);

    const pct = Math.max(0, Math.min(100, Number(prog.percent || 0)));
    const bar = byId('ppl-progress-bar');
    bar.max = 100;
    bar.value = pct;
    App.text(byId('ppl-progress-pct'), `${Math.round(pct)}%`);
    App.text(byId('ppl-current-file'), prog.current_file ? `Файл: ${prog.current_file}` : '');

    // KPI плитки: Фото, Видео, Лиц, Людей, Ошибок
    App.html(
      byId('ppl-kpis'),
      `<div class="ppl-kpi"><span class="ppl-kpi-label">Фото</span><b class="ppl-kpi-val num">${fmt.num(stats.photos || 0)}</b><span class="ppl-kpi-sub">обработано ${fmt.num(stats.scanned || 0)}</span></div>` +
        `<div class="ppl-kpi"><span class="ppl-kpi-label">Видео</span><b class="ppl-kpi-val num">${fmt.num(stats.videos || 0)}</b><span class="ppl-kpi-sub">${esc(fmt.bytes(stats.total_bytes || 0))}</span></div>` +
        `<div class="ppl-kpi"><span class="ppl-kpi-label">Лиц</span><b class="ppl-kpi-val num">${fmt.num(stats.faces_active || 0)}</b><span class="ppl-kpi-sub">назначено ${fmt.num(stats.faces_assigned || 0)}</span></div>` +
        `<div class="ppl-kpi ok"><span class="ppl-kpi-label">Людей</span><b class="ppl-kpi-val num">${fmt.num(stats.persons_total || 0)}</b><span class="ppl-kpi-sub">названо ${fmt.num(stats.persons_named || 0)}</span></div>` +
        `<div class="ppl-kpi${stats.errors > 0 ? ' err' : ''}"><span class="ppl-kpi-label">Ошибок</span><b class="ppl-kpi-val num">${fmt.num(stats.errors || 0)}</b><span class="ppl-kpi-sub">чтения медиа</span></div>`
    );

    const goto2 = byId('ppl-goto-step2-btn');
    if (goto2) goto2.disabled = !(stats.persons_total > 0 || persons.length > 0);

    // Лента событий
    const events = Array.isArray(st.events) ? st.events : [];
    const feedEl = byId('ppl-events');
    if (feedEl) {
      if (!events.length) {
        App.html(feedEl, '<p class="tiny">События появятся после запуска сканирования.</p>');
      } else {
        App.html(
          feedEl,
          events
            .slice(0, 30)
            .map((ev) => {
              const kind = ['ok', 'warn', 'err', 'run', 'info'].includes(ev.kind) ? ev.kind : 'info';
              return `<div class="ppl-feed-row ${kind}"><span class="tiny num">${esc(ev.at || '')}</span><span>${esc(ev.text || '')}</span></div>`;
            })
            .join('')
        );
      }
    }

    // Undo кнопка на шаге 2
    const undoBtn = byId('ppl-undo-btn');
    const undoHint = byId('ppl-undo-hint');
    const lastUndo = st.last_undo || null;
    if (undoBtn) {
      undoBtn.disabled = !lastUndo || running;
      undoBtn.title = lastUndo && lastUndo.description ? `Отменить: ${lastUndo.description}` : 'Нет действий для отмены';
    }
    if (undoHint) {
      App.text(undoHint, lastUndo && lastUndo.description ? lastUndo.description : '');
    }

    // Бейджи вкладок Студии
    paintStudioCounts();
    paintStepBadges();
    paintStep4Summary();
    paintSortProgress();
    // Блокируем кнопки Продолжить / Откатить в таблице запусков пока running === true
    updateRunsActionDisabled(running);
  }

  function paintStudioCounts() {
    const activePersons = persons.filter((p) => !p.is_ignored);
    const ignoredPersons = persons.filter((p) => !!p.is_ignored);
    const bridgeCount = Math.max(
      Number((statusData && statusData.age_bridge_count) || 0),
      ageBridges.length
    );
    const cAll = byId('ppl-subtab-all-cnt');
    const cBr = byId('ppl-subtab-bridges-cnt');
    const cIgn = byId('ppl-subtab-ignored-cnt');
    if (cAll) App.text(cAll, fmt.num(activePersons.length));
    if (cBr) App.text(cBr, fmt.num(bridgeCount));
    if (cIgn) App.text(cIgn, fmt.num(ignoredPersons.length));
  }

  // ── Шаг 2: Стена лиц (Face Wall) ──

  function updateMergeBar() {
    const bar = byId('ppl-merge-bar');
    const cnt = byId('ppl-merge-count');
    if (!bar || !cnt) return;
    const n = selectedPersonIds.size;
    bar.classList.toggle('hidden', n < 2);
    App.text(cnt, `Выбрано профилей: ${fmt.num(n)}`);
  }

  function filteredPersons() {
    const q = studioSearch.trim().toLowerCase();
    return persons.filter((p) => {
      if (studioSubtab === 'ignored') {
        if (!p.is_ignored) return false;
      } else {
        if (p.is_ignored) return false;
        if (studioFilter === 'named' && !p.is_named) return false;
        if (studioFilter === 'unnamed' && p.is_named) return false;
      }
      if (q) {
        const hay = `${p.name || ''} #${p.id} ${p.birth_year || ''}`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });
  }

  function makeCropImage(faceId, altText, cls) {
    if (!faceId) {
      const fb = document.createElement('div');
      fb.className = 'ppl-crop-fallback';
      fb.innerHTML = App.icon('users');
      return fb;
    }
    const img = document.createElement('img');
    img.className = cls || 'ppl-card-crop';
    img.loading = 'lazy';
    img.alt = altText || '';
    img.src = `/api/people/faces/${encodeURIComponent(String(faceId))}/crop`;
    img.addEventListener('error', () => {
      const fb = document.createElement('div');
      fb.className = 'ppl-crop-fallback';
      fb.innerHTML = App.icon('users');
      img.replaceWith(fb);
    });
    return img;
  }

  function renderFaceWall() {
    const grid = byId('ppl-wall-grid');
    const emptyEl = byId('ppl-wall-empty');
    if (!grid || !emptyEl) return;

    const list = filteredPersons();
    grid.replaceChildren();
    updateMergeBar();

    if (!list.length) {
      emptyEl.classList.remove('hidden');
      const msg =
        studioSubtab === 'ignored'
          ? 'Скрытых профилей (прохожих) пока нет.'
          : persons.length === 0
            ? 'Профилей пока нет — запустите поиск лиц на шаге 1.'
            : 'По текущему фильтру никого не найдено.';
      App.text(byId('ppl-wall-empty-text'), msg);
      return;
    }
    emptyEl.classList.add('hidden');

    const frag = document.createDocumentFragment();
    for (const p of list) {
      const pid = Number(p.id);
      const isSelected = selectedPersonIds.has(pid);

      const card = document.createElement('article');
      card.className = `ppl-card${isSelected ? ' selected' : ''}${p.is_ignored ? ' ignored' : ''}`;
      card.dataset.personId = String(pid);

      // Квадратный кроп + чекбокс + диапазон годов
      const media = document.createElement('div');
      media.className = 'ppl-card-media';
      media.appendChild(makeCropImage(p.cover_face_id, personDisplayName(p), 'ppl-card-crop'));

      const check = document.createElement('input');
      check.type = 'checkbox';
      check.className = 'cbx ppl-card-check';
      check.checked = isSelected;
      check.dataset.action = 'ppl-toggle-select';
      check.dataset.personId = String(pid);
      check.title = 'Выбрать для объединения';
      check.setAttribute('aria-label', `Выбрать ${personDisplayName(p)}`);
      media.appendChild(check);

      const spanTxt = formatYearSpan(p.min_year, p.max_year);
      if (spanTxt) {
        const yr = document.createElement('span');
        yr.className = 'ppl-year-badge num';
        yr.textContent = spanTxt;
        media.appendChild(yr);
      }
      card.appendChild(media);

      // Тело карточки
      const body = document.createElement('div');
      body.className = 'ppl-card-body';

      const meta = document.createElement('div');
      meta.className = 'ppl-card-meta';
      const mc = Number(p.media_count || 0);
      const fc = Number(p.face_count || 0);
      const metaText = document.createElement('span');
      metaText.className = 'tiny num';
      metaText.textContent = `${fmt.num(mc)} ${fmt.plural(mc, 'фото', 'фото', 'фото')} · ${fmt.num(fc)} ${fmt.plural(fc, 'лицо', 'лица', 'лиц')}`;
      meta.appendChild(metaText);

      if (p.is_ignored) {
        const badge = document.createElement('span');
        badge.className = 'badge plain';
        badge.textContent = 'Скрыт';
        meta.appendChild(badge);
      } else if (p.is_named) {
        const badge = document.createElement('span');
        badge.className = 'badge ok plain';
        badge.textContent = 'Назван';
        meta.appendChild(badge);
      }
      body.appendChild(meta);

      // Inline имя + год рождения + Сохранить
      const form = document.createElement('div');
      form.className = 'ppl-card-form';

      const nameInput = document.createElement('input');
      nameInput.type = 'text';
      nameInput.className = 'ppl-name-input';
      nameInput.placeholder = 'Имя, напр. Маша';
      nameInput.value = p.name || '';
      nameInput.setAttribute('aria-label', 'Имя человека');

      const birthInput = document.createElement('input');
      birthInput.type = 'number';
      birthInput.className = 'ppl-birth-input';
      birthInput.placeholder = 'Год р.';
      birthInput.min = '1850';
      birthInput.max = '2100';
      birthInput.value = p.birth_year != null ? String(p.birth_year) : '';
      birthInput.title = 'Год рождения (для папок с возрастом ребёнка)';
      birthInput.setAttribute('aria-label', 'Год рождения');

      const saveBtn = document.createElement('button');
      saveBtn.type = 'button';
      saveBtn.className = 'small primary';
      saveBtn.dataset.action = 'ppl-save-person';
      saveBtn.dataset.personId = String(pid);
      saveBtn.title = 'Сохранить имя и год рождения';
      saveBtn.innerHTML = App.icon('check');

      const onEnterSave = (ev) => {
        if (ev.key === 'Enter') {
          ev.preventDefault();
          savePersonCard(pid, saveBtn);
        }
      };
      nameInput.addEventListener('keydown', onEnterSave);
      birthInput.addEventListener('keydown', onEnterSave);

      form.append(nameInput, birthInput, saveBtn);
      body.appendChild(form);

      // Кнопки: Все лица + Скрыть / Вернуть
      const actions = document.createElement('div');
      actions.className = 'ppl-card-actions';

      const inspBtn = document.createElement('button');
      inspBtn.type = 'button';
      inspBtn.className = 'small';
      inspBtn.dataset.action = 'ppl-open-inspector';
      inspBtn.dataset.personId = String(pid);
      inspBtn.innerHTML = `${App.icon('search')}Все лица`;

      const ignBtn = document.createElement('button');
      ignBtn.type = 'button';
      ignBtn.className = 'small ghost';
      ignBtn.dataset.action = 'ppl-toggle-ignore';
      ignBtn.dataset.personId = String(pid);
      ignBtn.dataset.ignored = p.is_ignored ? '1' : '0';
      ignBtn.textContent = p.is_ignored ? 'Вернуть' : 'Скрыть';

      actions.append(inspBtn, ignBtn);
      body.appendChild(actions);

      card.appendChild(body);
      frag.appendChild(card);
    }

    grid.appendChild(frag);
  }

  // ── Шаг 2: Очередь взросления детей (Age-Bridge) ──

  function renderAgeBridges() {
    const emptyEl = byId('ppl-bridge-empty');
    const cardEl = byId('ppl-bridge-card');
    const counterEl = byId('ppl-bridge-counter');
    if (!emptyEl || !cardEl || !counterEl) return;

    if (!ageBridges.length) {
      emptyEl.classList.remove('hidden');
      cardEl.classList.add('hidden');
      App.text(counterEl, 'Кандидатов нет');
      return;
    }

    if (bridgeIndex >= ageBridges.length || bridgeIndex < 0) bridgeIndex = 0;
    const cand = ageBridges[bridgeIndex];
    emptyEl.classList.add('hidden');
    cardEl.classList.remove('hidden');
    App.text(counterEl, `Пара ${bridgeIndex + 1} из ${ageBridges.length}`);

    const personMap = new Map(persons.map((p) => [Number(p.id), p]));
    const pA = personMap.get(Number(cand.person_a_id));
    const pB = personMap.get(Number(cand.person_b_id));

    const fillSide = (prefix, pid, rawName, yearSpan, sampleFaces) => {
      const titleEl = byId(`ppl-bridge-${prefix}-title`);
      const yearsEl = byId(`ppl-bridge-${prefix}-years`);
      const inspBtn = byId(`ppl-bridge-${prefix}-insp`);
      const cropsEl = byId(`ppl-bridge-${prefix}-crops`);
      const cleanName = String(rawName || '').trim() || `Профиль #${pid}`;
      if (titleEl) App.text(titleEl, cleanName);
      const yTxt = Array.isArray(yearSpan) ? formatYearSpan(yearSpan[0], yearSpan[1]) : '';
      if (yearsEl) App.text(yearsEl, yTxt || 'Годы не указаны');
      if (inspBtn) inspBtn.dataset.personId = String(pid);

      if (cropsEl) {
        cropsEl.replaceChildren();
        const faces = Array.isArray(sampleFaces) ? sampleFaces.slice(0, 4) : [];
        if (!faces.length && (prefix === 'a' ? pA : pB) && (prefix === 'a' ? pA : pB).cover_face_id) {
          faces.push((prefix === 'a' ? pA : pB).cover_face_id);
        }
        for (const fid of faces) {
          const btn = document.createElement('button');
          btn.type = 'button';
          btn.className = 'ppl-bridge-crop-btn';
          btn.dataset.action = 'ppl-open-inspector';
          btn.dataset.personId = String(pid);
          btn.title = `Открыть все лица: ${cleanName}`;
          btn.appendChild(makeCropImage(fid, cleanName, 'ppl-card-crop'));
          cropsEl.appendChild(btn);
        }
        if (!faces.length) {
          const emptyCrop = document.createElement('div');
          emptyCrop.className = 'ppl-crop-fallback';
          emptyCrop.innerHTML = App.icon('users');
          cropsEl.appendChild(emptyCrop);
        }
      }
    };

    fillSide('a', cand.person_a_id, cand.name_a, cand.year_span_a, cand.sample_faces_a);
    fillSide('b', cand.person_b_id, cand.name_b, cand.year_span_b, cand.sample_faces_b);

    const scorePct = Math.round(Number(cand.score || 0) * 100);
    const bndPct = Math.round(Number(cand.boundary_sim || 0) * 100);
    App.text(byId('ppl-bridge-score'), `Сходство ${scorePct}% (погранично ${bndPct}%)`);
    App.text(byId('ppl-bridge-reason'), cand.reason_ru || 'Похожие черты лица в соседние годы.');

    const famBox = byId('ppl-bridge-family');
    famBox.replaceChildren();
    const sharedNames = Array.isArray(cand.shared_family_names) ? cand.shared_family_names : [];
    if (sharedNames.length) {
      famBox.classList.remove('hidden');
      const lbl = document.createElement('span');
      lbl.className = 'tiny';
      lbl.textContent = 'На общих фото с:';
      famBox.appendChild(lbl);
      for (const fn of sharedNames) {
        const pill = document.createElement('span');
        pill.className = 'badge plain violet';
        pill.textContent = String(fn);
        famBox.appendChild(pill);
      }
    } else {
      famBox.classList.add('hidden');
    }

    const suggestedName =
      String(cand.name_a || '').trim() ||
      String(cand.name_b || '').trim() ||
      (pA && pA.name) ||
      (pB && pB.name) ||
      '';
    const suggestedBirth =
      (pA && pA.birth_year != null ? pA.birth_year : null) ??
      (pB && pB.birth_year != null ? pB.birth_year : null);

    const nameInput = byId('ppl-bridge-name');
    const birthInput = byId('ppl-bridge-birth');
    if (nameInput) nameInput.value = suggestedName;
    if (birthInput) birthInput.value = suggestedBirth != null ? String(suggestedBirth) : '';

    const skipBtn = byId('ppl-bridge-skip-btn');
    if (skipBtn) skipBtn.classList.toggle('hidden', ageBridges.length <= 1);
  }

  function renderStudioContent() {
    if (studioSubtab === 'bridges') {
      renderAgeBridges();
    } else {
      renderFaceWall();
    }
  }

  // ── Модальное окно Инспектора кластера (Outlier-First) и Lightbox ──

  async function openInspector(personId) {
    const pid = Number(personId);
    if (!pid) return;
    inspectorPersonId = pid;
    inspectorSelectedFaceIds.clear();
    const reqId = ++inspectorReqId;

    const modal = byId('ppl-inspector-modal');
    modal.classList.remove('hidden');
    App.text(byId('ppl-insp-title'), `Профиль #${pid}`);
    App.text(byId('ppl-insp-sub'), 'Загрузка лиц…');
    byId('ppl-insp-protos').replaceChildren();
    byId('ppl-insp-grid').replaceChildren();
    const splitNameInput = byId('ppl-insp-split-name');
    if (splitNameInput) splitNameInput.value = '';
    updateInspectorSelectionUI();

    const res = await App.api.get(`/api/people/persons/${encodeURIComponent(String(pid))}/faces`);
    if (reqId !== inspectorReqId) return; // устаревший ответ

    if (!res.ok || !res.data) {
      App.toast((res && res.error) || 'Не удалось загрузить лица профиля', 'err');
      closeInspector();
      return;
    }

    const person = res.data.person || { id: pid };
    inspectorFaces = Array.isArray(res.data.items) ? res.data.items : [];

    const titleText = personDisplayName(person);
    App.text(byId('ppl-insp-title'), titleText);
    const subParts = [];
    if (person.birth_year) subParts.push(`год рождения ${person.birth_year}`);
    subParts.push(
      `${fmt.num(inspectorFaces.length)} ${fmt.plural(inspectorFaces.length, 'лицо', 'лица', 'лиц')}`
    );
    const ySpan = formatYearSpan(person.min_year, person.max_year);
    if (ySpan) subParts.push(ySpan);
    App.text(byId('ppl-insp-sub'), subParts.join(' · '));

    // Возрастные прототипы в заголовке модалки
    const protosBox = byId('ppl-insp-protos');
    protosBox.replaceChildren();
    const protos = Array.isArray(person.prototypes) ? person.prototypes : [];
    if (protos.length) {
      for (const pr of protos) {
        const pill = document.createElement('span');
        pill.className = 'ppl-proto-pill num';
        const yb = Number(pr.year_bucket || 0);
        const fc = Number(pr.face_count || 0);
        pill.textContent = yb > 0 ? `${yb} (${fc})` : `Без даты (${fc})`;
        if (pr.mean_est_age != null && Number.isFinite(Number(pr.mean_est_age))) {
          pill.title = `Оценочный возраст: ~${Math.round(Number(pr.mean_est_age))} лет`;
        }
        protosBox.appendChild(pill);
      }
    }

    renderInspectorGrid();
  }

  function renderInspectorGrid() {
    const grid = byId('ppl-insp-grid');
    if (!grid) return;
    grid.replaceChildren();
    updateInspectorSelectionUI();

    if (!inspectorFaces.length) {
      const p = document.createElement('p');
      p.className = 'muted';
      p.textContent = 'В этом профиле нет активных лиц.';
      grid.appendChild(p);
      return;
    }

    const frag = document.createDocumentFragment();
    for (const f of inspectorFaces) {
      const fid = Number(f.id);
      const mid = Number(f.media_id);
      const isSel = inspectorSelectedFaceIds.has(fid);

      const tile = document.createElement('div');
      tile.className = `ppl-face-tile${isSel ? ' selected' : ''}`;
      tile.dataset.faceId = String(fid);

      const media = document.createElement('div');
      media.className = 'ppl-face-media';

      const thumbBtn = document.createElement('button');
      thumbBtn.type = 'button';
      thumbBtn.className = 'ppl-face-thumb-btn';
      thumbBtn.dataset.action = 'ppl-open-lightbox';
      thumbBtn.dataset.mediaId = String(mid);
      const caption = `${f.rel_path || `Медиа #${mid}`}${f.year ? ` (${f.year})` : ''}`;
      thumbBtn.dataset.caption = caption;
      thumbBtn.title = 'Открыть фото целиком';
      thumbBtn.appendChild(makeCropImage(fid, caption, 'ppl-card-crop'));
      media.appendChild(thumbBtn);

      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.className = 'cbx ppl-card-check';
      cb.checked = isSel;
      cb.dataset.action = 'ppl-insp-toggle-face';
      cb.dataset.faceId = String(fid);
      cb.title = 'Выбрать лицо для выноса';
      media.appendChild(cb);

      const sim = Number(f.proto_similarity);
      if (Number.isFinite(sim)) {
        const simBadge = document.createElement('span');
        const simCls = sim < 0.35 ? 'err' : sim < 0.5 ? 'warn' : 'ok';
        simBadge.className = `ppl-sim-badge ${simCls} num`;
        simBadge.textContent = `${Math.round(sim * 100)}%`;
        simBadge.title = 'Сходство с возрастным прототипом профиля';
        media.appendChild(simBadge);
      }

      tile.appendChild(media);

      const foot = document.createElement('div');
      foot.className = 'ppl-face-foot';

      const info = document.createElement('span');
      info.className = 'tiny num';
      const kindLbl = f.media_kind === 'video' ? 'Видео' : 'Фото';
      info.textContent = `${f.year || '—'} · ${kindLbl}`;

      const rejBtn = document.createElement('button');
      rejBtn.type = 'button';
      rejBtn.className = 'ppl-reject-btn';
      rejBtn.dataset.action = 'ppl-reject-face';
      rejBtn.dataset.faceId = String(fid);
      rejBtn.textContent = 'Не лицо';
      rejBtn.title = 'Пометить как ложное срабатывание';

      foot.append(info, rejBtn);
      tile.appendChild(foot);
      frag.appendChild(tile);
    }

    grid.appendChild(frag);
  }

  function updateInspectorSelectionUI() {
    const n = inspectorSelectedFaceIds.size;
    const cnt = byId('ppl-insp-sel-count');
    const splitBtn = byId('ppl-insp-split-btn');
    const clearBtn = byId('ppl-insp-clear-btn');
    if (cnt) App.text(cnt, n > 0 ? `Выбрано: ${fmt.num(n)}` : 'Выберите лица для выноса');
    if (splitBtn) splitBtn.disabled = n === 0;
    if (clearBtn) clearBtn.disabled = n === 0;
  }

  function closeInspector() {
    inspectorReqId += 1;
    inspectorPersonId = null;
    inspectorFaces = [];
    inspectorSelectedFaceIds.clear();
    const modal = byId('ppl-inspector-modal');
    if (modal) modal.classList.add('hidden');
  }

  function openLightbox(mediaId, caption) {
    const mid = Number(mediaId);
    if (!mid) return;
    const lb = byId('ppl-lightbox');
    const img = byId('ppl-lightbox-img');
    const cap = byId('ppl-lightbox-caption');
    if (!lb || !img) return;
    img.src = `/api/people/media/${encodeURIComponent(String(mid))}/thumb`;
    img.alt = caption || '';
    if (cap) App.text(cap, caption || `Медиа #${mid}`);
    lb.classList.remove('hidden');
  }

  function closeLightbox() {
    const lb = byId('ppl-lightbox');
    const img = byId('ppl-lightbox-img');
    if (lb) lb.classList.add('hidden');
    if (img) img.removeAttribute('src');
  }

  // ── Шаг 3: Предпросмотр плана (Dry-Run) ──

  function buildTreeNodeEl(node, depth) {
    const children = Array.isArray(node.children) ? node.children : [];
    const countTxt = `${fmt.num(node.count || 0)} ${fmt.plural(node.count || 0, 'файл', 'файла', 'файлов')}`;
    const bytesTxt = fmt.bytes(node.bytes || 0);

    if (!children.length) {
      const row = document.createElement('div');
      row.className = 'ppl-tree-row';
      row.style.setProperty('--depth', String(depth));
      const ico = document.createElement('span');
      ico.className = 'ico';
      ico.innerHTML = App.icon('folder');
      const nameSpan = document.createElement('span');
      nameSpan.className = 'grow';
      nameSpan.textContent = node.name || '';
      const metaSpan = document.createElement('span');
      metaSpan.className = 'tiny num';
      metaSpan.textContent = `${countTxt} · ${bytesTxt}`;
      row.append(ico, nameSpan, metaSpan);
      return row;
    }

    const det = document.createElement('details');
    det.className = 'ppl-tree-node';
    if (depth < 2) det.open = true;

    const sum = document.createElement('summary');
    sum.className = 'ppl-tree-row';
    const ico = document.createElement('span');
    ico.className = 'ico';
    ico.innerHTML = App.icon('folder');
    const nameSpan = document.createElement('span');
    nameSpan.className = 'grow';
    nameSpan.textContent = node.name || '';
    const metaSpan = document.createElement('span');
    metaSpan.className = 'tiny num';
    metaSpan.textContent = `${countTxt} · ${bytesTxt}`;
    sum.append(ico, nameSpan, metaSpan);
    det.appendChild(sum);

    const body = document.createElement('div');
    body.className = 'ppl-tree-children';
    for (const ch of children) {
      body.appendChild(buildTreeNodeEl(ch, depth + 1));
    }
    det.appendChild(body);
    return det;
  }

  function renderPlanPreview() {
    const card = byId('ppl-preview-card');
    if (!card) return;
    if (!lastPlan) {
      card.classList.add('hidden');
      return;
    }
    card.classList.remove('hidden');

    const p = lastPlan;
    const totalOps = Number(p.total_ops || 0);
    const uniqueMedia = Number(p.unique_media || 0);
    const effMode = p.effective_mode || p.requested_mode || 'hardlink';
    const effLabel = MODE_LABELS[effMode] || effMode;

    const modePill = byId('ppl-plan-mode-pill');
    modePill.className = effMode === 'move' ? 'pill wait' : 'pill ok';
    App.html(modePill, `<span class="pdot"></span>Режим: ${esc(effLabel)}`);

    App.text(
      byId('ppl-plan-summary'),
      `${fmt.num(totalOps)} ${fmt.plural(totalOps, 'операция', 'операции', 'операций')} для ${fmt.num(uniqueMedia)} ${fmt.plural(uniqueMedia, 'файла', 'файлов', 'файлов')}`
    );

    // Жёлтый баннер fallback при недоступности hardlink
    const fb = byId('ppl-plan-fallback');
    if (p.mode_fallback_reason) {
      fb.classList.remove('hidden');
      App.text(byId('ppl-plan-fallback-text'), p.mode_fallback_reason);
    } else {
      fb.classList.add('hidden');
    }

    // Предупреждения
    const warnBox = byId('ppl-plan-warnings');
    const extraWarnings = (Array.isArray(p.warnings) ? p.warnings : []).filter(
      (w) => w && w !== p.mode_fallback_reason
    );
    warnBox.replaceChildren();
    if (extraWarnings.length) {
      warnBox.classList.remove('hidden');
      for (const w of extraWarnings) {
        const div = document.createElement('div');
        div.className = 'tiny';
        div.textContent = `• ${w}`;
        warnBox.appendChild(div);
      }
    } else {
      warnBox.classList.add('hidden');
    }

    // Индикатор свободного места
    const reqBytes = Number(p.required_bytes || 0);
    const freeBytes = p.free_bytes != null ? Number(p.free_bytes) : null;
    const enough = p.enough_space !== false;
    App.text(
      byId('ppl-disk-text'),
      `Требуется: ${fmt.bytes(reqBytes)} · Свободно на диске: ${freeBytes != null ? fmt.bytes(freeBytes) : 'не определено'}`
    );
    const diskBadge = byId('ppl-disk-badge');
    diskBadge.className = enough ? 'badge ok plain' : 'badge err plain';
    App.text(diskBadge, enough ? 'Места достаточно' : 'Недостаточно места');

    const fillEl = byId('ppl-disk-fill');
    let usedPct = 0;
    if (reqBytes === 0) {
      usedPct = 4;
    } else if (freeBytes && freeBytes > 0) {
      usedPct = Math.min(100, Math.max(4, Math.round((reqBytes / freeBytes) * 100)));
    } else {
      usedPct = 100;
    }
    fillEl.style.width = `${usedPct}%`;
    fillEl.className = `ppl-disk-fill ${enough ? 'ok' : 'err'}`;

    // Категории
    const catsEl = byId('ppl-plan-cats');
    catsEl.replaceChildren();
    const byCat = p.summary_by_category || {};
    for (const [key, label] of Object.entries(CATEGORY_LABELS)) {
      const val = Number(byCat[key] || 0);
      const pill = document.createElement('span');
      pill.className = `pill${val > 0 ? ' ok' : ''}`;
      pill.textContent = `${label}: ${fmt.num(val)}`;
      catsEl.appendChild(pill);
    }

    // Виртуальное дерево папок
    const treeEl = byId('ppl-plan-tree');
    treeEl.replaceChildren();
    if (p.tree_preview && p.tree_preview.name) {
      treeEl.appendChild(buildTreeNodeEl(p.tree_preview, 0));
    } else {
      const emptyTree = document.createElement('p');
      emptyTree.className = 'tiny';
      emptyTree.textContent = 'Папок для создания нет.';
      treeEl.appendChild(emptyTree);
    }

    // Таблица примеров (до 80 операций)
    const sampleOps = Array.isArray(p.sample_ops) ? p.sample_ops.slice(0, 80) : [];
    App.text(
      byId('ppl-plan-sample-count'),
      `Примеры операций (показано ${fmt.num(sampleOps.length)} из ${fmt.num(totalOps)})`
    );
    const tbody = byId('ppl-plan-sample-tbody');
    tbody.replaceChildren();
    if (!sampleOps.length) {
      const tr = document.createElement('tr');
      const td = document.createElement('td');
      td.colSpan = 4;
      td.className = 'muted';
      td.textContent = 'Нет запланированных операций.';
      tr.appendChild(td);
      tbody.appendChild(tr);
    } else {
      const frag = document.createDocumentFragment();
      for (const op of sampleOps) {
        const tr = document.createElement('tr');

        const tdSrc = document.createElement('td');
        tdSrc.className = 'name';
        tdSrc.title = op.src_path || '';
        tdSrc.textContent = fmt.shortPath(op.src_path || '', 42);

        const tdDst = document.createElement('td');
        tdDst.className = 'name';
        tdDst.title = op.dst_path || op.rel_dst || '';
        tdDst.textContent = op.rel_dst || '';

        const tdOp = document.createElement('td');
        const opBadge = document.createElement('span');
        opBadge.className = `badge plain${op.op_type === 'move' ? ' wait' : ''}`;
        opBadge.textContent = MODE_LABELS[op.op_type] || op.op_type || '';
        tdOp.appendChild(opBadge);

        const tdSize = document.createElement('td');
        tdSize.className = 'num';
        tdSize.textContent = fmt.bytes(op.src_size || 0);

        tr.append(tdSrc, tdDst, tdOp, tdSize);
        frag.appendChild(tr);
      }
      tbody.appendChild(frag);
    }
  }

  // ── Шаг 4: Выполнение сортировки, живые счётчики и история запусков ──

  function paintStep4Summary() {
    const sumEl = byId('ppl-sort-summary');
    const moveGuard = byId('ppl-move-guard');
    const allowCb = byId('ppl-allow-move');
    const startBtn = byId('ppl-sort-start-btn');
    const stopBtn = byId('ppl-sort-stop-btn');
    const handoffBtn = byId('ppl-handoff-upload-btn');
    if (!sumEl || !moveGuard || !startBtn) return;

    const opts = collectSortOptions();
    const running = !!(statusData && statusData.running);
    const effMode = (lastPlan && lastPlan.effective_mode) || opts.mode || 'hardlink';
    const isMove = opts.mode === 'move' || effMode === 'move';

    moveGuard.classList.toggle('hidden', !isMove);
    if (!isMove && allowCb) allowCb.checked = false;

    const lines = [];
    lines.push(`Источник: ${opts.source_dir || 'не указан'}`);
    lines.push(`Назначение: ${opts.target_dir || 'не указано'}`);
    lines.push(`Режим: ${MODE_LABELS[effMode] || effMode}`);
    if (lastPlan && !planDirty) {
      const n = Number(lastPlan.total_ops || 0);
      lines.push(
        `План: ${fmt.num(n)} ${fmt.plural(n, 'операция', 'операции', 'операций')}, требуется ${fmt.bytes(lastPlan.required_bytes || 0)}`
      );
    } else {
      lines.push('План не построен или настройки изменены — рекомендуется проверить на шаге 3.');
    }

    sumEl.replaceChildren();
    for (const ln of lines) {
      const p = document.createElement('div');
      p.textContent = ln;
      sumEl.appendChild(p);
    }

    const blockedBySpace = !!(lastPlan && !planDirty && lastPlan.enough_space === false);
    const blockedByMove = isMove && !(allowCb && allowCb.checked);
    startBtn.disabled = running || !opts.source_dir || !opts.target_dir || blockedByMove || blockedBySpace;
    if (stopBtn) stopBtn.classList.toggle('hidden', !running);

    const effectiveTarget =
      opts.target_dir ||
      (lastPlan && lastPlan.target_dir) ||
      (runs[0] && runs[0].target_dir) ||
      ((statusData && statusData.config && statusData.config.target_dir) || '');
    if (handoffBtn) handoffBtn.disabled = !effectiveTarget;
  }

  function paintSortProgress() {
    const phasePill = byId('ppl-sort-phase-pill');
    const msgEl = byId('ppl-sort-msg');
    const pctEl = byId('ppl-sort-pct');
    const bar = byId('ppl-sort-bar');
    const fileEl = byId('ppl-sort-file');
    const countersEl = byId('ppl-sort-counters');
    if (!phasePill || !bar || !countersEl) return;

    const st = statusData || {};
    const prog = st.progress || {};
    const running = !!st.running;
    const isSortOrRollback = st.task === 'sort' || st.task === 'rollback';
    const phase = isSortOrRollback ? (st.phase || 'idle') : 'idle';
    const label = PHASE_LABELS[phase] || phase;

    phasePill.className = phasePillClass(phase, running && isSortOrRollback);
    App.html(phasePill, `<span class="pdot"></span>${esc(label)}`);

    const latestRun = runs[0] || null;
    const activeRun = st.active_run || latestRun;

    const doneOps =
      prog.done_ops != null
        ? Number(prog.done_ops)
        : activeRun && activeRun.done_ops != null
          ? Number(activeRun.done_ops)
          : isSortOrRollback
            ? Number(prog.done || 0)
            : 0;
    const skippedOps =
      prog.skipped_ops != null
        ? Number(prog.skipped_ops)
        : activeRun && activeRun.skipped_ops != null
          ? Number(activeRun.skipped_ops)
          : 0;
    const failedOps =
      prog.failed_ops != null
        ? Number(prog.failed_ops)
        : activeRun && activeRun.failed_ops != null
          ? Number(activeRun.failed_ops)
          : 0;
    const totalOps =
      prog.total_ops != null
        ? Number(prog.total_ops)
        : isSortOrRollback && Number(prog.total || 0) > 0
          ? Number(prog.total)
          : activeRun && activeRun.total_ops != null
            ? Number(activeRun.total_ops)
            : 0;

    const pct = isSortOrRollback
      ? Math.max(0, Math.min(100, Number(prog.percent || 0)))
      : totalOps > 0
        ? Math.min(100, Math.round(((doneOps + skippedOps + failedOps) / totalOps) * 100))
        : 0;

    bar.max = 100;
    bar.value = pct;
    App.text(pctEl, `${Math.round(pct)}%`);
    App.text(
      msgEl,
      isSortOrRollback && st.message
        ? st.message
        : latestRun
          ? `Последний запуск #${latestRun.id}: ${RUN_STATUS_LABELS[latestRun.status] || latestRun.status}`
          : 'Сортировка ещё не запускалась'
    );
    App.text(
      fileEl,
      isSortOrRollback && prog.current_file ? `Текущий файл: ${prog.current_file}` : ''
    );

    App.html(
      countersEl,
      `<div class="ppl-kpi ok"><span class="ppl-kpi-label">Выполнено</span><b class="ppl-kpi-val num">${fmt.num(doneOps)}</b></div>` +
        `<div class="ppl-kpi"><span class="ppl-kpi-label">Пропущено</span><b class="ppl-kpi-val num">${fmt.num(skippedOps)}</b></div>` +
        `<div class="ppl-kpi${failedOps > 0 ? ' err' : ''}"><span class="ppl-kpi-label">Ошибок</span><b class="ppl-kpi-val num">${fmt.num(failedOps)}</b></div>` +
        `<div class="ppl-kpi"><span class="ppl-kpi-label">Всего операций</span><b class="ppl-kpi-val num">${fmt.num(totalOps)}</b></div>`
    );
  }

  function updateRunsActionDisabled(running) {
    const tbody = byId('ppl-runs-tbody');
    if (!tbody) return;
    for (const btn of tbody.querySelectorAll('button[data-action="ppl-run-resume"], button[data-action="ppl-run-rollback"]')) {
      if (btn.dataset.action === 'ppl-run-resume') {
        btn.disabled = !!running || btn.dataset.canResume !== '1';
      } else {
        btn.disabled = !!running;
      }
    }
  }

  function renderRunsTable() {
    const tbody = byId('ppl-runs-tbody');
    if (!tbody) return;
    tbody.replaceChildren();
    const running = !!(statusData && statusData.running);

    if (!runs.length) {
      const tr = document.createElement('tr');
      const td = document.createElement('td');
      td.colSpan = 7;
      td.className = 'muted';
      td.textContent = 'Запусков сортировки ещё не было.';
      tr.appendChild(td);
      tbody.appendChild(tr);
      return;
    }

    const frag = document.createDocumentFragment();
    for (const r of runs) {
      const rid = Number(r.id);
      const tr = document.createElement('tr');

      const tdId = document.createElement('td');
      tdId.className = 'num';
      tdId.textContent = `#${rid}`;

      const tdDate = document.createElement('td');
      tdDate.className = 'tiny num';
      tdDate.textContent = formatTimestamp(r.created_at);

      const effMode = String(r.effective_mode || r.mode || '');
      const tdMode = document.createElement('td');
      tdMode.textContent = MODE_LABELS[effMode] || effMode || '—';

      const tdTarget = document.createElement('td');
      const pathSpan = document.createElement('span');
      pathSpan.className = 'ppl-path-cell';
      pathSpan.title = r.target_dir || '';
      pathSpan.textContent = r.target_dir || '—';
      tdTarget.appendChild(pathSpan);

      const tdOps = document.createElement('td');
      tdOps.className = 'tiny num';
      const done = Number(r.done_ops || 0);
      const skip = Number(r.skipped_ops || 0);
      const fail = Number(r.failed_ops || 0);
      const tot = Number(r.total_ops || 0);
      const pendingOps = r.pending_ops != null ? Number(r.pending_ops) : Math.max(0, tot - done - skip - fail);
      const rolledBackOps = Number(r.rolled_back_ops || 0);
      tdOps.textContent =
        `${fmt.num(done)} / ${fmt.num(tot)}` +
        (skip ? ` (проп. ${fmt.num(skip)})` : '') +
        (fail ? ` (ош. ${fmt.num(fail)})` : '') +
        (rolledBackOps ? ` (отк. ${fmt.num(rolledBackOps)})` : '');

      const tdStatus = document.createElement('td');
      const sp = runStatusPill(r.status);
      const pill = document.createElement('span');
      pill.className = sp.cls;
      pill.textContent = sp.label;
      tdStatus.appendChild(pill);

      const tdAct = document.createElement('td');
      const actWrap = document.createElement('div');
      actWrap.className = 'ppl-runs-actions';

      const canResume =
        r.status !== 'completed' &&
        r.status !== 'rolled_back' &&
        r.status !== 'running' &&
        pendingOps > 0 &&
        !(rolledBackOps > 0);
      const canRollback =
        r.status !== 'rolled_back' &&
        r.status !== 'running' &&
        (done > 0 || r.status === 'completed' || r.status === 'partial' || r.status === 'cancelled');

      if (canResume) {
        const resBtn = document.createElement('button');
        resBtn.type = 'button';
        resBtn.className = 'small';
        resBtn.dataset.action = 'ppl-run-resume';
        resBtn.dataset.runId = String(rid);
        resBtn.dataset.canResume = '1';
        resBtn.dataset.effectiveMode = effMode;
        resBtn.disabled = running;
        resBtn.textContent = 'Продолжить';
        actWrap.appendChild(resBtn);
      }

      if (canRollback) {
        const rbBtn = document.createElement('button');
        rbBtn.type = 'button';
        rbBtn.className = 'small danger';
        rbBtn.dataset.action = 'ppl-run-rollback';
        rbBtn.dataset.runId = String(rid);
        rbBtn.disabled = running;
        rbBtn.textContent = 'Откатить';
        actWrap.appendChild(rbBtn);
      }

      if (!canResume && !canRollback) {
        const dash = document.createElement('span');
        dash.className = 'tiny';
        dash.textContent = '—';
        actWrap.appendChild(dash);
      }
      tdAct.appendChild(actWrap);

      tr.append(tdId, tdDate, tdMode, tdTarget, tdOps, tdStatus, tdAct);
      frag.appendChild(tr);
    }
    tbody.appendChild(frag);
  }

  // ── Операции с API ──

  async function savePersonCard(pid, btn) {
    const card = byId('ppl-wall-grid') && byId('ppl-wall-grid').querySelector(`.ppl-card[data-person-id="${pid}"]`);
    if (!card) return;
    const nameEl = card.querySelector('.ppl-name-input');
    const birthEl = card.querySelector('.ppl-birth-input');
    const nameVal = nameEl ? nameEl.value.trim() : '';
    const parsedBirth = parseBirthYear(birthEl ? birthEl.value : '');
    if (parsedBirth.invalid) {
      App.toast('Укажите корректный год рождения (1850–2100) или оставьте пустым', 'err');
      return;
    }

    if (btn) btn.disabled = true;
    try {
      const payload = { name: nameVal, birth_year: parsedBirth.value };
      const res = await App.api.patch(`/api/people/persons/${encodeURIComponent(String(pid))}`, payload);
      if (!res.ok) {
        App.toast(res.error || 'Не удалось сохранить профиль', 'err');
        return;
      }
      if (res.data && res.data.merged_into) {
        App.toast(`Профиль объединён с «${nameVal || `#${res.data.merged_into}`}»`, 'ok');
      } else {
        App.toast('Профиль сохранён', 'ok');
      }
      invalidatePlan();
      await Promise.all([refreshStudio(), refreshPeopleStatus(true)]);
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  async function submitBridgeDecision(decision, btn) {
    if (!ageBridges.length) return;
    if (bridgeIndex >= ageBridges.length) bridgeIndex = 0;
    const cand = ageBridges[bridgeIndex];
    if (!cand) return;

    const nameVal = (byId('ppl-bridge-name') && byId('ppl-bridge-name').value.trim()) || '';
    const parsedBirth = parseBirthYear(byId('ppl-bridge-birth') ? byId('ppl-bridge-birth').value : '');
    if (decision === 'merge' && parsedBirth.invalid) {
      App.toast('Укажите корректный год рождения (1850–2100) или оставьте пустым', 'err');
      return;
    }

    const payload = {
      person_a_id: Number(cand.person_a_id),
      person_b_id: Number(cand.person_b_id),
      decision,
    };
    if (decision === 'merge') {
      payload.merged_name = nameVal || null;
      if (!parsedBirth.empty) {
        payload.birth_year = parsedBirth.value;
      }
    }

    const yesBtn = byId('ppl-bridge-yes-btn');
    const noBtn = byId('ppl-bridge-no-btn');
    if (yesBtn) yesBtn.disabled = true;
    if (noBtn) noBtn.disabled = true;
    if (btn) btn.disabled = true;

    try {
      const res = await App.api.post('/api/people/age-bridges/decision', payload);
      if (!res.ok) {
        if (decision === 'merge' && isCannotLinkError(res)) {
          const forceYes = await App.confirm({
            title: 'Объединить профили принудительно?',
            text: 'Эти профили встречались на одном кадре или были ранее разделены вручную. Объединить их всё равно?',
            ok: 'Да, объединить',
            danger: true,
          });
          if (forceYes) {
            const mRes = await App.api.post('/api/people/persons/merge', {
              target_person_id: Number(cand.person_a_id),
              source_person_id: Number(cand.person_b_id),
              force: true,
            });
            if (!mRes.ok) {
              App.toast(mRes.error || 'Не удалось объединить профили', 'err');
              return;
            }
            if (nameVal || !parsedBirth.empty) {
              const patchBody = {};
              if (nameVal) patchBody.name = nameVal;
              if (!parsedBirth.empty) patchBody.birth_year = parsedBirth.value;
              const pRes = await App.api.patch(
                `/api/people/persons/${encodeURIComponent(String(cand.person_a_id))}`,
                patchBody
              );
              if (!pRes.ok) {
                App.toast(
                  `Профили объединены, но имя/год рождения не сохранены: ${pRes.error || 'ошибка'}`,
                  'warn'
                );
              } else {
                App.toast('Профили объединены принудительно', 'ok');
              }
            } else {
              App.toast('Профили объединены принудительно', 'ok');
            }
          } else {
            return;
          }
        } else {
          App.toast(res.error || 'Не удалось применить решение', 'err');
          return;
        }
      } else {
        App.toast(decision === 'merge' ? 'Профили объединены' : 'Помечены как разные люди', 'ok');
      }
      invalidatePlan();
    } finally {
      if (yesBtn) yesBtn.disabled = false;
      if (noBtn) noBtn.disabled = false;
      if (btn) btn.disabled = false;
      await Promise.all([refreshStudio(), refreshPeopleStatus(true)]);
    }
  }

  async function performUndo(btn) {
    if (btn) btn.disabled = true;
    try {
      const res = await App.api.post('/api/people/studio/undo', {});
      if (!res.ok) {
        App.toast(res.error || 'Не удалось отменить действие', 'err');
        return;
      }
      const undone = res.data && res.data.undone;
      if (undone) {
        App.toast(`Отменено: ${undone.description || 'последнее действие'}`, 'ok');
      } else {
        App.toast('Нет действий для отмены', 'info');
      }
      invalidatePlan();
      await Promise.all([refreshStudio(), refreshPeopleStatus(true)]);
      if (inspectorPersonId != null) {
        const stillExists = persons.some((p) => Number(p.id) === Number(inspectorPersonId));
        if (stillExists) {
          openInspector(inspectorPersonId);
        } else {
          closeInspector();
        }
      }
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  // ── Клавиатурные сочетания (Y / N на вкладке взросления, Ctrl+Z, Escape) ──

  function onGlobalKeydown(ev) {
    if (App.state.route !== 'people') return;

    const lbOpen = byId('ppl-lightbox') && !byId('ppl-lightbox').classList.contains('hidden');
    const inspOpen = byId('ppl-inspector-modal') && !byId('ppl-inspector-modal').classList.contains('hidden');
    const confirmOpen = !!document.querySelector('#dialog-root .dialog-backdrop');

    if (ev.key === 'Escape') {
      if (lbOpen) {
        ev.preventDefault();
        closeLightbox();
      } else if (inspOpen) {
        ev.preventDefault();
        closeInspector();
      }
      return;
    }

    if (confirmOpen || lbOpen) return;

    const t = ev.target;
    const inEditable =
      t &&
      (t.tagName === 'INPUT' ||
        t.tagName === 'TEXTAREA' ||
        t.tagName === 'SELECT' ||
        t.isContentEditable);
    if (inEditable) return;

    // Ctrl+Z на шаге 2
    if ((ev.ctrlKey || ev.metaKey) && !ev.shiftKey && !ev.altKey && ev.code === 'KeyZ') {
      if (activeStep === 2) {
        ev.preventDefault();
        const undoBtn = byId('ppl-undo-btn');
        if (undoBtn && !undoBtn.disabled) performUndo(undoBtn);
      }
      return;
    }

    // Y / N только на шаге 2 во вкладке «Похожи в разном возрасте» и без открытых модалок
    if (
      activeStep === 2 &&
      studioSubtab === 'bridges' &&
      !inspOpen &&
      ageBridges.length > 0 &&
      !ev.ctrlKey &&
      !ev.metaKey &&
      !ev.altKey
    ) {
      if (ev.code === 'KeyY') {
        ev.preventDefault();
        submitBridgeDecision('merge', byId('ppl-bridge-yes-btn'));
      } else if (ev.code === 'KeyN') {
        ev.preventDefault();
        submitBridgeDecision('reject', byId('ppl-bridge-no-btn'));
      }
    }
  }

  // ── Регистрация экрана App.screens.people ──

  App.screens.people = {
    mount(el) {
      el.innerHTML = `<div class="page-head">
          <h1>Сортировка по людям</h1>
          <p>Локальный поиск лиц на фото и видео, объединение детских и взрослых фотографий одного человека и безопасная раскладка по папкам. Оригиналы остаются на месте (по умолчанию используются жёсткие ссылки).</p>
        </div>

        <div class="ppl-steps" role="tablist" aria-label="Шаги сортировки по людям">
          <button type="button" class="ppl-step active" id="ppl-tab-1" role="tab" aria-selected="true" data-action="ppl-step" data-step="1">
            <span class="ppl-step-num">1</span>
            <span class="ppl-step-title">1. Папка и поиск лиц</span>
            <span class="ppl-step-badge num" id="ppl-badge-1"></span>
          </button>
          <button type="button" class="ppl-step" id="ppl-tab-2" role="tab" aria-selected="false" data-action="ppl-step" data-step="2">
            <span class="ppl-step-num">2</span>
            <span class="ppl-step-title">2. Кто на фото?</span>
            <span class="ppl-step-badge num" id="ppl-badge-2"></span>
          </button>
          <button type="button" class="ppl-step" id="ppl-tab-3" role="tab" aria-selected="false" data-action="ppl-step" data-step="3">
            <span class="ppl-step-num">3</span>
            <span class="ppl-step-title">3. Правила и предпросмотр</span>
            <span class="ppl-step-badge num" id="ppl-badge-3"></span>
          </button>
          <button type="button" class="ppl-step" id="ppl-tab-4" role="tab" aria-selected="false" data-action="ppl-step" data-step="4">
            <span class="ppl-step-num">4</span>
            <span class="ppl-step-title">4. Сортировка и откат</span>
            <span class="ppl-step-badge num" id="ppl-badge-4"></span>
          </button>
        </div>

        <div id="ppl-unavail-banner" class="banner warn hidden" role="status">
          <span class="b-icon">${App.icon('info')}</span>
          <div class="grow">
            <h3>Сортировка по людям недоступна на этой системе</h3>
            <p id="ppl-unavail-text"></p>
          </div>
        </div>

        <div id="ppl-error-banner" class="banner action hidden" role="alert">
          <span class="b-icon">${App.icon('alert')}</span>
          <div class="grow">
            <h3>Ошибка операции</h3>
            <p id="ppl-error-text"></p>
          </div>
        </div>

        <!-- ШАГ 1: Папка и поиск лиц -->
        <section id="ppl-panel-1" class="stack">
          <div class="card">
            <div class="card-head">
              <span class="icon-box accent">${App.icon('users')}</span>
              <div class="grow">
                <h2>Папка с архивом и параметры поиска лиц</h2>
                <p class="tiny">Исходная папка открывается только на чтение — оригиналы остаются на месте.</p>
              </div>
              <span class="ppl-hw-pill" id="ppl-hw-pill">Проверка оборудования…</span>
            </div>

            <label class="field">
              <span>Папка с фотографиями и видео</span>
              <div class="row">
                <input type="text" id="ppl-src" class="grow" placeholder="Например: D:\\Фото\\Архив" spellcheck="false">
                <button type="button" id="ppl-pick-src-btn" data-action="ppl-pick-src">${App.icon('folder')}Выбрать папку…</button>
                <button type="button" id="ppl-use-dl-btn" data-action="ppl-use-dl">${App.icon('download')}Взять папку скачивания</button>
              </div>
            </label>

            <div class="grid-2">
              <label class="field">
                <span>Пакет моделей распознавания</span>
                <select id="ppl-model-pack">
                  <option value="antelopev2">antelopev2 — Рекомендуется для детей (точнее)</option>
                  <option value="buffalo_l">buffalo_l — Быстрее</option>
                </select>
              </label>
              <div class="field ppl-checks-row" style="align-self:end;padding-bottom:10px">
                <label class="check-inline">
                  <input type="checkbox" class="cbx" id="ppl-prefer-gpu" checked>
                  <span>Использовать видеокарту (GPU), если доступна</span>
                </label>
              </div>
            </div>

            <div class="row" style="margin-top:16px">
              <button type="button" class="primary" id="ppl-scan-btn" data-action="ppl-scan">${App.icon('play')}Сканировать и найти лица</button>
              <button type="button" class="danger hidden" id="ppl-stop-btn" data-action="ppl-stop">${App.icon('stop')}Остановить</button>
              <button type="button" id="ppl-cluster-btn" data-action="ppl-cluster">${App.icon('refresh')}Перекластеризовать</button>
              <span class="spacer"></span>
              <button type="button" class="danger" id="ppl-reset-btn" data-action="ppl-reset">${App.icon('trash')}Сбросить индекс</button>
            </div>
          </div>

          <div class="card">
            <div class="card-head">
              <span class="icon-box">${App.icon('activity')}</span>
              <div class="grow">
                <div class="row">
                  <h2>Прогресс и статистика</h2>
                  <span class="pill" id="ppl-phase-pill"><span class="pdot"></span>Ожидание</span>
                </div>
                <p class="tiny" id="ppl-phase-msg">Ожидание</p>
              </div>
              <b class="num" id="ppl-progress-pct">0%</b>
            </div>

            <progress id="ppl-progress-bar" class="ppl-progress" value="0" max="100"></progress>
            <p class="tiny ppl-current-file" id="ppl-current-file"></p>

            <div class="ppl-kpis" id="ppl-kpis"></div>

            <div class="row" style="margin-top:16px">
              <button type="button" class="primary" id="ppl-goto-step2-btn" data-action="ppl-step" data-step="2" disabled>Перейти к разметке людей ${App.icon('arrow')}</button>
            </div>

            <div class="section-title" style="margin-top:20px">Журнал событий</div>
            <div class="ppl-feed" id="ppl-events">
              <p class="tiny">События появятся после запуска сканирования.</p>
            </div>
          </div>
        </section>

        <!-- ШАГ 2: Кто на фото? + Взросление детей -->
        <section id="ppl-panel-2" class="stack hidden">
          <div class="card">
            <div class="ppl-studio-bar">
              <div class="seg-tabs ppl-subtabs">
                <button type="button" class="seg-tab active" id="ppl-subtab-all" data-action="ppl-subtab" data-subtab="all">
                  Все люди <span class="cnt" id="ppl-subtab-all-cnt">0</span>
                </button>
                <button type="button" class="seg-tab" id="ppl-subtab-bridges" data-action="ppl-subtab" data-subtab="bridges">
                  Похожи в разном возрасте <span class="cnt" id="ppl-subtab-bridges-cnt">0</span>
                </button>
                <button type="button" class="seg-tab" id="ppl-subtab-ignored" data-action="ppl-subtab" data-subtab="ignored">
                  Прохожие / скрытые <span class="cnt" id="ppl-subtab-ignored-cnt">0</span>
                </button>
              </div>
              <div class="row">
                <span class="tiny" id="ppl-undo-hint"></span>
                <button type="button" class="small" id="ppl-undo-btn" data-action="ppl-undo" disabled>${App.icon('back')}Отменить (Ctrl+Z)</button>
                <button type="button" class="small" id="ppl-studio-refresh-btn" data-action="ppl-studio-refresh">${App.icon('refresh')}Обновить</button>
              </div>
            </div>

            <!-- Стена лиц -->
            <div id="ppl-wall-wrap">
              <div class="ppl-toolbar">
                <div class="search-wrap">
                  ${App.icon('search')}
                  <input type="text" id="ppl-search" placeholder="Поиск по имени или номеру…" spellcheck="false">
                </div>
                <select id="ppl-filter">
                  <option value="all">Все</option>
                  <option value="named">Названные</option>
                  <option value="unnamed">Без имени</option>
                </select>
                <button type="button" class="primary small" id="ppl-goto-step3-btn" data-action="ppl-step" data-step="3">Правила и предпросмотр ${App.icon('arrow')}</button>
              </div>

              <div id="ppl-merge-bar" class="ppl-merge-bar hidden">
                <b id="ppl-merge-count">Выбрано профилей: 0</b>
                <span class="spacer"></span>
                <button type="button" class="primary small" id="ppl-merge-btn" data-action="ppl-merge-selected">${App.icon('users')}Объединить выбранных</button>
                <button type="button" class="small" id="ppl-clear-sel-btn" data-action="ppl-clear-selection">Снять выбор</button>
              </div>

              <div id="ppl-wall-empty" class="empty-box hidden">
                <span class="icon-box">${App.icon('users')}</span>
                <p id="ppl-wall-empty-text">Профилей пока нет.</p>
              </div>
              <div id="ppl-wall-grid" class="ppl-wall"></div>
            </div>

            <!-- Очередь взросления детей (Age-Bridge) -->
            <div id="ppl-bridges-wrap" class="hidden">
              <div class="ppl-bridge-bar">
                <b id="ppl-bridge-counter">Кандидатов нет</b>
                <span class="tiny">Клавиши: <code>Y</code> — один человек, <code>N</code> — разные люди</span>
              </div>

              <div id="ppl-bridge-empty" class="empty-box">
                <span class="icon-box ok">${App.icon('check')}</span>
                <p>Все возрастные пары проверены! Можно переходить к правилам сортировки.</p>
              </div>

              <div id="ppl-bridge-card" class="ppl-bridge-grid hidden">
                <div class="ppl-bridge-side">
                  <div class="ppl-bridge-side-head">
                    <div class="grow">
                      <h3 id="ppl-bridge-a-title">Профиль A</h3>
                      <span class="tiny num" id="ppl-bridge-a-years"></span>
                    </div>
                    <button type="button" class="small ghost" id="ppl-bridge-a-insp" data-action="ppl-open-inspector">Все лица</button>
                  </div>
                  <div class="ppl-bridge-crops" id="ppl-bridge-a-crops"></div>
                </div>

                <div class="ppl-bridge-center">
                  <span class="ppl-bridge-score num" id="ppl-bridge-score"></span>
                  <p class="ppl-bridge-reason" id="ppl-bridge-reason"></p>
                  <div class="ppl-bridge-family hidden" id="ppl-bridge-family"></div>
                  <div class="ppl-bridge-inputs">
                    <input type="text" id="ppl-bridge-name" placeholder="Общее имя (необязательно)">
                    <input type="number" id="ppl-bridge-birth" min="1850" max="2100" placeholder="Год рождения (для возраста)">
                  </div>
                  <div class="ppl-bridge-actions">
                    <button type="button" class="primary" id="ppl-bridge-yes-btn" data-action="ppl-bridge-yes">${App.icon('check')}Да, это один человек</button>
                    <button type="button" class="danger" id="ppl-bridge-no-btn" data-action="ppl-bridge-no">${App.icon('x')}Нет, разные люди</button>
                    <button type="button" class="ghost small" id="ppl-bridge-skip-btn" data-action="ppl-bridge-skip">Пропустить ${App.icon('arrow')}</button>
                  </div>
                </div>

                <div class="ppl-bridge-side">
                  <div class="ppl-bridge-side-head">
                    <div class="grow">
                      <h3 id="ppl-bridge-b-title">Профиль B</h3>
                      <span class="tiny num" id="ppl-bridge-b-years"></span>
                    </div>
                    <button type="button" class="small ghost" id="ppl-bridge-b-insp" data-action="ppl-open-inspector">Все лица</button>
                  </div>
                  <div class="ppl-bridge-crops" id="ppl-bridge-b-crops"></div>
                </div>
              </div>
            </div>
          </div>
        </section>

        <!-- ШАГ 3: Правила и предпросмотр -->
        <section id="ppl-panel-3" class="stack hidden">
          <div class="card">
            <div class="card-head">
              <span class="icon-box">${App.icon('sliders')}</span>
              <div class="grow">
                <h2>Правила сортировки и предпросмотр (Dry-Run)</h2>
                <p class="tiny">Предпросмотр проверяет файловую систему и свободное место без изменения файлов на диске.</p>
              </div>
            </div>

            <label class="field">
              <span>Целевая папка для результата (должна быть отдельной от исходной)</span>
              <div class="row">
                <input type="text" id="ppl-dst" class="grow" placeholder="Например: D:\\Фото\\По_людям" spellcheck="false">
                <button type="button" id="ppl-pick-dst-btn" data-action="ppl-pick-dst">${App.icon('folder')}Выбрать папку…</button>
              </div>
            </label>

            <div class="ppl-rules-grid">
              <label class="field">
                <span>Способ раскладки файлов</span>
                <select id="ppl-opt-mode">
                  <option value="hardlink">Жёсткие ссылки — 0 доп. места, оригиналы остаются на месте</option>
                  <option value="copy">Копирование — оригиналы остаются на месте</option>
                  <option value="move">Перемещение — переносит оригиналы (нужно подтверждение)</option>
                </select>
              </label>

              <label class="field">
                <span>Структура папок</span>
                <select id="ppl-opt-layout">
                  <option value="people_first">По людям (Люди / Имя / Год / Месяц)</option>
                  <option value="dual">Двойная (По людям + По датам)</option>
                </select>
              </label>

              <label class="field">
                <span>Групповые фото (несколько людей в кадре)</span>
                <select id="ppl-opt-group">
                  <option value="each_person">В папку каждого человека</option>
                  <option value="shared_folder">В общую папку «Совместные»</option>
                  <option value="priority">Только главному по приоритету</option>
                </select>
              </label>

              <label class="field">
                <span>Фото и видео без лиц</span>
                <select id="ppl-opt-nofaces">
                  <option value="folder">В папку «Без_лиц»</option>
                  <option value="skip">Пропускать</option>
                </select>
              </label>

              <label class="field">
                <span>Неназванные люди</span>
                <select id="ppl-opt-unnamed">
                  <option value="folder">В папку «Нераспознанные»</option>
                  <option value="skip">Пропускать</option>
                </select>
              </label>
            </div>

            <div class="ppl-checks-row">
              <label class="check-inline">
                <input type="checkbox" class="cbx" id="ppl-opt-child-age" checked>
                <span>Подписывать возраст ребёнка в папках (при указанном годе рождения)</span>
              </label>
              <label class="check-inline">
                <input type="checkbox" class="cbx" id="ppl-opt-skip-ignored" checked>
                <span>Пропускать скрытых (прохожих)</span>
              </label>
              <label class="check-inline">
                <input type="checkbox" class="cbx" id="ppl-opt-preserve-dates" checked>
                <span>Сохранять даты съёмки файлов и папок</span>
              </label>
            </div>

            <div class="row" style="margin-top:16px">
              <button type="button" class="primary" id="ppl-preview-btn" data-action="ppl-preview">${App.icon('list')}Построить предпросмотр (Dry-Run)</button>
              <button type="button" id="ppl-save-rules-btn" data-action="ppl-save-rules">${App.icon('check')}Сохранить правила</button>
            </div>
          </div>

          <div id="ppl-preview-card" class="card hidden">
            <div class="card-head">
              <span class="icon-box ok">${App.icon('folder')}</span>
              <div class="grow">
                <h2>План раскладки</h2>
                <p class="tiny num" id="ppl-plan-summary"></p>
              </div>
              <span class="pill ok" id="ppl-plan-mode-pill"></span>
            </div>

            <div id="ppl-plan-fallback" class="banner safety hidden" style="box-shadow:none">
              <span class="b-icon">${App.icon('alert')}</span>
              <div>
                <h3>Автоматический переход на копирование</h3>
                <p id="ppl-plan-fallback-text"></p>
              </div>
            </div>

            <div id="ppl-plan-warnings" class="ppl-plan-warnings hidden"></div>

            <div class="ppl-disk-gauge" id="ppl-disk-gauge">
              <div class="row">
                <span class="small num grow" id="ppl-disk-text"></span>
                <span class="badge ok plain" id="ppl-disk-badge"></span>
              </div>
              <div class="ppl-disk-bar"><div class="ppl-disk-fill ok" id="ppl-disk-fill"></div></div>
            </div>

            <div class="ppl-cat-pills" id="ppl-plan-cats"></div>

            <div class="section-title" style="margin-top:18px">Виртуальное дерево папок</div>
            <div class="ppl-tree" id="ppl-plan-tree"></div>

            <div class="section-title" style="margin-top:18px" id="ppl-plan-sample-count">Примеры операций</div>
            <div class="table-wrap scroll">
              <table class="list">
                <thead>
                  <tr><th>Откуда</th><th>Куда</th><th>Операция</th><th>Размер</th></tr>
                </thead>
                <tbody id="ppl-plan-sample-tbody"></tbody>
              </table>
            </div>

            <div class="row" style="margin-top:18px">
              <button type="button" class="primary big" id="ppl-goto-step4-btn" data-action="ppl-step" data-step="4">Перейти к сортировке ${App.icon('arrow')}</button>
            </div>
          </div>
        </section>

        <!-- ШАГ 4: Сортировка и откат -->
        <section id="ppl-panel-4" class="stack hidden">
          <div class="card">
            <div class="card-head">
              <span class="icon-box accent">${App.icon('play')}</span>
              <div class="grow">
                <h2>Запуск сортировки</h2>
                <p class="tiny">Каждая операция записывается в журнал с SHA-256 и inode для безопасного отката и продолжения.</p>
              </div>
            </div>

            <div class="ppl-sort-summary" id="ppl-sort-summary"></div>

            <div id="ppl-move-guard" class="ppl-move-guard hidden">
              <label class="check-inline">
                <input type="checkbox" class="cbx" id="ppl-allow-move" data-action="ppl-toggle-move">
                <span><b>Подтверждаю перемещение (move):</b> исходные файлы перенесутся из исходной папки в целевую. Вернуть их можно кнопкой «Откатить» в журнале ниже.</span>
              </label>
            </div>

            <div class="row" style="margin-top:16px">
              <button type="button" class="primary big" id="ppl-sort-start-btn" data-action="ppl-sort-start">${App.icon('play')}Запустить сортировку</button>
              <button type="button" class="danger hidden" id="ppl-sort-stop-btn" data-action="ppl-stop">${App.icon('stop')}Остановить</button>
              <span class="spacer"></span>
              <button type="button" id="ppl-handoff-upload-btn" data-action="ppl-handoff-upload">${App.icon('upload')}Загрузить результат в Безлимит ${App.icon('arrow')}</button>
            </div>

            <div class="ppl-sort-progress" id="ppl-sort-progress-box">
              <div class="card-head" style="margin-bottom:10px">
                <div class="grow">
                  <div class="row">
                    <span class="pill" id="ppl-sort-phase-pill"><span class="pdot"></span>Ожидание</span>
                    <span class="small" id="ppl-sort-msg">Сортировка ещё не запускалась</span>
                  </div>
                </div>
                <b class="num" id="ppl-sort-pct">0%</b>
              </div>
              <progress id="ppl-sort-bar" class="ppl-progress" value="0" max="100"></progress>
              <p class="tiny ppl-current-file" id="ppl-sort-file"></p>
              <div class="ppl-sort-counters" id="ppl-sort-counters"></div>
            </div>
          </div>

          <div class="card">
            <div class="card-head">
              <span class="icon-box">${App.icon('clock')}</span>
              <div class="grow">
                <h2>Журнал запусков и безопасный откат</h2>
                <p class="tiny">При откате проверяется целостность файла: изменённые вручную файлы пропускаются.</p>
              </div>
              <button type="button" class="small" id="ppl-runs-refresh-btn" data-action="ppl-runs-refresh">${App.icon('refresh')}Обновить</button>
            </div>

            <div class="table-wrap ppl-runs-wrap">
              <table class="list ppl-runs-table">
                <thead>
                  <tr>
                    <th>#</th>
                    <th>Дата</th>
                    <th>Режим</th>
                    <th>Целевая папка</th>
                    <th>Операции</th>
                    <th>Статус</th>
                    <th>Действия</th>
                  </tr>
                </thead>
                <tbody id="ppl-runs-tbody">
                  <tr><td colspan="7" class="muted">Загрузка журнала…</td></tr>
                </tbody>
              </table>
            </div>
          </div>
        </section>

        <!-- Модальное окно Инспектора кластера (Outlier-First) -->
        <div id="ppl-inspector-modal" class="ppl-modal hidden" role="dialog" aria-modal="true" aria-labelledby="ppl-insp-title">
          <div class="ppl-modal-backdrop" data-action="ppl-close-inspector"></div>
          <div class="ppl-modal-box">
            <div class="ppl-modal-head">
              <div class="grow">
                <h2 id="ppl-insp-title">Профиль</h2>
                <p class="tiny" id="ppl-insp-sub"></p>
                <div class="ppl-proto-pills" id="ppl-insp-protos"></div>
              </div>
              <button type="button" class="small" data-action="ppl-close-inspector" aria-label="Закрыть">${App.icon('x')}</button>
            </div>
            <div class="ppl-insp-toolbar">
              <span class="tiny" id="ppl-insp-sel-count">Выберите лица для выноса</span>
              <span class="spacer"></span>
              <input type="text" id="ppl-insp-split-name" class="ppl-split-input" placeholder="Новое имя (опционально)">
              <button type="button" class="primary small" id="ppl-insp-split-btn" data-action="ppl-insp-split" disabled>Вынести в нового человека</button>
              <button type="button" class="small" id="ppl-insp-clear-btn" data-action="ppl-insp-clear" disabled>Снять выбор</button>
            </div>
            <div class="ppl-modal-body">
              <p class="tiny" style="margin-bottom:12px">Сначала показаны лица с наименьшим сходством — так быстрее заметить и вынести чужие лица. Нажмите на фото, чтобы открыть полный кадр.</p>
              <div id="ppl-insp-grid" class="ppl-face-grid"></div>
            </div>
          </div>
        </div>

        <!-- Lightbox полного кадра -->
        <div id="ppl-lightbox" class="ppl-lightbox hidden" role="dialog" aria-modal="true" aria-label="Просмотр фото">
          <div class="ppl-modal-backdrop" data-action="ppl-close-lightbox"></div>
          <div class="ppl-lightbox-box">
            <img id="ppl-lightbox-img" class="ppl-lightbox-img" alt="">
            <div class="row" style="justify-content:space-between;width:100%">
              <span class="tiny" id="ppl-lightbox-caption"></span>
              <button type="button" class="small" data-action="ppl-close-lightbox">${App.icon('x')}Закрыть</button>
            </div>
          </div>
        </div>`;

      // Подписки на ввод и изменение настроек (с немедленной инвалидацией кэшированного Dry-Run)
      const markStep3Dirty = (key) => {
        if (key) dirtyFields.add(key);
        invalidatePlan();
      };

      byId('ppl-src').addEventListener('input', () => markStep3Dirty('source_dir'));
      byId('ppl-model-pack').addEventListener('change', async () => {
        const res = await App.api.post('/api/people/settings', {
          model_pack: byId('ppl-model-pack').value || 'antelopev2',
        });
        dirtyFields.delete('model_pack');
        if (!res.ok) App.toast(res.error || 'Не удалось сохранить выбор модели', 'err');
        await refreshPeopleStatus(true);
      });
      byId('ppl-prefer-gpu').addEventListener('change', async () => {
        const res = await App.api.post('/api/people/settings', {
          prefer_gpu: !!byId('ppl-prefer-gpu').checked,
        });
        dirtyFields.delete('prefer_gpu');
        if (!res.ok) App.toast(res.error || 'Не удалось сохранить настройку GPU', 'err');
        await refreshPeopleStatus(true);
      });

      const step3Inputs = [
        ['ppl-dst', 'target_dir', 'input'],
        ['ppl-opt-mode', 'mode', 'change'],
        ['ppl-opt-layout', 'tree_layout', 'change'],
        ['ppl-opt-group', 'group_policy', 'change'],
        ['ppl-opt-nofaces', 'no_faces_policy', 'change'],
        ['ppl-opt-unnamed', 'unnamed_policy', 'change'],
        ['ppl-opt-child-age', 'include_child_age', 'change'],
        ['ppl-opt-skip-ignored', 'skip_ignored', 'change'],
        ['ppl-opt-preserve-dates', 'preserve_dates', 'change'],
      ];
      for (const [id, key, evName] of step3Inputs) {
        const node = byId(id);
        if (node) node.addEventListener(evName, () => markStep3Dirty(key));
      }

      byId('ppl-search').addEventListener('input', (ev) => {
        studioSearch = ev.target.value || '';
        renderFaceWall();
      });
      byId('ppl-filter').addEventListener('change', (ev) => {
        studioFilter = ev.target.value || 'all';
        renderFaceWall();
      });

      if (!keyListenerBound) {
        keyListenerBound = true;
        document.addEventListener('keydown', onGlobalKeydown);
      }
    },

    show() {
      refreshPeopleStatus(true);
      refreshStudio();
      loadRuns();
    },

    update() {
      refreshPeopleStatus(false);
    },
  };

  // ── Обработчики App.action ──

  App.action('ppl-step', (el) => {
    setActiveStep(el.dataset.step);
  });

  App.action('ppl-subtab', (el) => {
    setStudioSubtab(el.dataset.subtab);
  });

  App.action('ppl-pick-src', (btn) =>
    App.run(
      btn,
      async () => {
        const res = await App.api.post('/api/people/folder/pick', { field: 'source_dir' });
        if (res.ok && res.data && res.data.picked && res.data.path) {
          byId('ppl-src').value = res.data.path;
          dirtyFields.delete('source_dir');
          invalidatePlan();
          await refreshPeopleStatus(true);
        }
        return res;
      },
      { okToast: false }
    )
  );

  App.action('ppl-use-dl', (btn) =>
    App.run(
      btn,
      async () => {
        const dlDir =
          App.state.status && App.state.status.config && App.state.status.config.download_dir;
        if (!dlDir) {
          App.toast('Папка скачивания пока не указана в разделе «Настройки»', 'warn');
          return { ok: true };
        }
        byId('ppl-src').value = dlDir;
        dirtyFields.delete('source_dir');
        invalidatePlan();
        const res = await App.api.post('/api/people/settings', { source_dir: dlDir });
        await refreshPeopleStatus(true);
        if (res.ok) App.toast('Подставлена папка скачивания', 'ok');
        return res;
      },
      { okToast: false }
    )
  );

  App.action('ppl-scan', (btn) =>
    App.run(
      btn,
      async () => {
        const s1 = collectStep1Settings();
        if (!s1.source_dir) return { ok: false, error: 'Укажите папку с фотографиями и видео' };
        const cfgRes = await App.api.post('/api/people/settings', s1);
        if (!cfgRes.ok) return cfgRes;
        const res = await App.api.post('/api/people/scan/start', {
          source_dir: s1.source_dir,
          recluster: true,
          model_pack: s1.model_pack,
          prefer_gpu: s1.prefer_gpu,
        });
        if (res.ok) {
          dirtyFields.delete('source_dir');
          dirtyFields.delete('model_pack');
          dirtyFields.delete('prefer_gpu');
          wasRunning = true;
          lastRunningTask = 'scan';
          App.toast('Сканирование и поиск лиц запущены', 'ok');
        }
        await refreshPeopleStatus(true);
        return res;
      },
      { okToast: false }
    )
  );

  App.action('ppl-cluster', (btn) =>
    App.run(
      btn,
      async () => {
        const res = await App.api.post('/api/people/cluster/run', {});
        if (res.ok) {
          wasRunning = true;
          lastRunningTask = 'cluster';
          App.toast('Перекластеризация запущена', 'ok');
        }
        await refreshPeopleStatus(true);
        return res;
      },
      { okToast: false }
    )
  );

  App.action('ppl-stop', (btn) =>
    App.run(
      btn,
      async () => {
        const res = await App.api.post('/api/people/task/stop', {});
        if (res.ok) App.toast('Запрошена остановка текущей операции', 'info');
        await refreshPeopleStatus(true);
        return res;
      },
      { okToast: false }
    )
  );

  App.action('ppl-reset', async (btn) => {
    const yes = await App.confirm({
      title: 'Сбросить индекс распознанных лиц?',
      text: 'Очистятся локальная база лиц, имена людей и кеш миниатюр. Оригиналы фото и видео на диске остаются на месте.',
      ok: 'Сбросить индекс',
      danger: true,
    });
    if (!yes) return;
    await App.run(
      btn,
      async () => {
        const res = await App.api.post('/api/people/reset', {});
        if (res.ok) {
          selectedPersonIds.clear();
          invalidatePlan();
          App.toast('Индекс лиц очищен', 'ok');
        }
        await Promise.all([refreshPeopleStatus(true), refreshStudio()]);
        return res;
      },
      { okToast: false }
    );
  });

  App.action('ppl-undo', (btn) => performUndo(btn));

  App.action('ppl-studio-refresh', async () => {
    await Promise.all([refreshStudio(), refreshPeopleStatus(true)]);
  });

  App.action('ppl-toggle-select', (el) => {
    const pid = Number(el.dataset.personId);
    if (!pid) return;
    if (el.checked) selectedPersonIds.add(pid);
    else selectedPersonIds.delete(pid);
    const card = el.closest('.ppl-card');
    if (card) card.classList.toggle('selected', el.checked);
    updateMergeBar();
  });

  App.action('ppl-clear-selection', () => {
    selectedPersonIds.clear();
    renderFaceWall();
  });

  App.action('ppl-merge-selected', async (btn) => {
    const ids = Array.from(selectedPersonIds);
    if (ids.length < 2) {
      App.toast('Выберите хотя бы два профиля для объединения', 'warn');
      return;
    }

    const byIdMap = new Map(persons.map((p) => [Number(p.id), p]));
    const picked = ids
      .map((id) => byIdMap.get(id))
      .filter(Boolean)
      .sort((a, b) => {
        if (Boolean(b.is_named) !== Boolean(a.is_named)) return b.is_named ? 1 : -1;
        return Number(b.face_count || 0) - Number(a.face_count || 0);
      });
    if (picked.length < 2) return;

    const target = picked[0];
    const sources = picked.slice(1);

    if (btn) btn.disabled = true;
    let mergedCount = 0;
    try {
      for (const src of sources) {
        let res = await App.api.post('/api/people/persons/merge', {
          target_person_id: Number(target.id),
          source_person_id: Number(src.id),
          force: false,
        });
        if (!res.ok && isCannotLinkError(res)) {
          const forceYes = await App.confirm({
            title: 'Объединить принудительно?',
            text: `Профили «${personDisplayName(target)}» и «${personDisplayName(src)}» встречались на одном кадре или ранее разделялись вручную. Объединить их всё равно?`,
            ok: 'Да, объединить',
            danger: true,
          });
          if (!forceYes) continue;
          res = await App.api.post('/api/people/persons/merge', {
            target_person_id: Number(target.id),
            source_person_id: Number(src.id),
            force: true,
          });
        }
        if (!res.ok) {
          App.toast(res.error || 'Не удалось объединить профили', 'err');
        } else {
          mergedCount += 1;
        }
      }
      if (mergedCount > 0) {
        App.toast(
          `Объединено профилей: ${fmt.num(mergedCount + 1)} в «${personDisplayName(target)}»`,
          'ok'
        );
      }
    } finally {
      if (btn) btn.disabled = false;
      selectedPersonIds.clear();
      invalidatePlan();
      await Promise.all([refreshStudio(), refreshPeopleStatus(true)]);
    }
  });

  App.action('ppl-save-person', (btn) => {
    const pid = Number(btn.dataset.personId);
    if (pid) savePersonCard(pid, btn);
  });

  App.action('ppl-toggle-ignore', async (btn) => {
    const pid = Number(btn.dataset.personId);
    if (!pid) return;
    const currentlyIgnored = btn.dataset.ignored === '1';
    btn.disabled = true;
    try {
      const res = await App.api.patch(`/api/people/persons/${encodeURIComponent(String(pid))}`, {
        is_ignored: !currentlyIgnored,
      });
      if (!res.ok) {
        App.toast(res.error || 'Не удалось изменить статус профиля', 'err');
        return;
      }
      App.toast(currentlyIgnored ? 'Профиль возвращён в общий список' : 'Профиль скрыт (прохожий)', 'ok');
      selectedPersonIds.delete(pid);
      invalidatePlan();
      await Promise.all([refreshStudio(), refreshPeopleStatus(true)]);
    } finally {
      btn.disabled = false;
    }
  });

  App.action('ppl-open-inspector', (el) => {
    const pid = Number(el.dataset.personId);
    if (pid) openInspector(pid);
  });

  App.action('ppl-close-inspector', () => closeInspector());

  App.action('ppl-open-lightbox', (el) => {
    openLightbox(el.dataset.mediaId, el.dataset.caption || '');
  });

  App.action('ppl-close-lightbox', () => closeLightbox());

  App.action('ppl-insp-toggle-face', (el) => {
    const fid = Number(el.dataset.faceId);
    if (!fid) return;
    if (el.checked) inspectorSelectedFaceIds.add(fid);
    else inspectorSelectedFaceIds.delete(fid);
    const tile = el.closest('.ppl-face-tile');
    if (tile) tile.classList.toggle('selected', el.checked);
    updateInspectorSelectionUI();
  });

  App.action('ppl-insp-clear', () => {
    inspectorSelectedFaceIds.clear();
    renderInspectorGrid();
  });

  App.action('ppl-insp-split', async (btn) => {
    const faceIds = Array.from(inspectorSelectedFaceIds);
    if (!faceIds.length) return;
    const newName = (byId('ppl-insp-split-name') && byId('ppl-insp-split-name').value.trim()) || '';
    btn.disabled = true;
    try {
      const res = await App.api.post('/api/people/persons/split', {
        face_ids: faceIds,
        new_name: newName,
      });
      if (!res.ok) {
        App.toast(res.error || 'Не удалось вынести лица в новый профиль', 'err');
        return;
      }
      App.toast(`Вынесено лиц: ${fmt.num(faceIds.length)}`, 'ok');
      invalidatePlan();
      const pid = inspectorPersonId;
      await Promise.all([refreshStudio(), refreshPeopleStatus(true)]);
      if (pid != null) {
        const stillExists = persons.some((p) => Number(p.id) === Number(pid) && Number(p.face_count || 0) > 0);
        if (stillExists) {
          await openInspector(pid);
        } else {
          closeInspector();
        }
      }
    } finally {
      btn.disabled = false;
    }
  });

  App.action('ppl-reject-face', async (btn) => {
    const fid = Number(btn.dataset.faceId);
    if (!fid) return;
    btn.disabled = true;
    try {
      const res = await App.api.post(`/api/people/faces/${encodeURIComponent(String(fid))}/reject`, {
        rejected: true,
      });
      if (!res.ok) {
        App.toast(res.error || 'Не удалось отклонить лицо', 'err');
        return;
      }
      inspectorSelectedFaceIds.delete(fid);
      App.toast('Помечено как не лицо', 'ok');
      invalidatePlan();
      const pid = inspectorPersonId;
      await Promise.all([refreshStudio(), refreshPeopleStatus(true)]);
      if (pid != null) {
        await openInspector(pid);
      }
    } finally {
      btn.disabled = false;
    }
  });

  App.action('ppl-bridge-yes', (btn) => submitBridgeDecision('merge', btn));
  App.action('ppl-bridge-no', (btn) => submitBridgeDecision('reject', btn));
  App.action('ppl-bridge-skip', () => {
    if (ageBridges.length > 1) {
      bridgeIndex = (bridgeIndex + 1) % ageBridges.length;
      renderAgeBridges();
    }
  });

  App.action('ppl-pick-dst', (btn) =>
    App.run(
      btn,
      async () => {
        const res = await App.api.post('/api/people/folder/pick', { field: 'target_dir' });
        if (res.ok && res.data && res.data.picked && res.data.path) {
          byId('ppl-dst').value = res.data.path;
          dirtyFields.delete('target_dir');
          invalidatePlan();
          await refreshPeopleStatus(true);
        }
        return res;
      },
      { okToast: false }
    )
  );

  App.action('ppl-save-rules', (btn) =>
    App.run(
      btn,
      async () => {
        const opts = collectSortOptions();
        const res = await App.api.post('/api/people/settings', opts);
        if (res.ok) {
          dirtyFields.clear();
          App.toast('Правила сортировки сохранены', 'ok');
        }
        await refreshPeopleStatus(true);
        return res;
      },
      { okToast: false }
    )
  );

  App.action('ppl-preview', (btn) =>
    App.run(
      btn,
      async () => {
        const opts = collectSortOptions();
        if (!opts.source_dir) return { ok: false, error: 'Укажите исходную папку на шаге 1' };
        if (!opts.target_dir) return { ok: false, error: 'Укажите целевую папку для результата' };

        const cfgRes = await App.api.post('/api/people/settings', opts);
        if (!cfgRes.ok) return cfgRes;
        dirtyFields.clear();

        const res = await App.api.post('/api/people/plan/preview', opts);
        if (!res.ok || !res.data) return res;

        lastPlan = res.data;
        planDirty = false;
        renderPlanPreview();
        paintStepBadges();
        paintStep4Summary();

        const n = Number(lastPlan.total_ops || 0);
        App.toast(
          `Предпросмотр готов: ${fmt.num(n)} ${fmt.plural(n, 'операция', 'операции', 'операций')}`,
          'ok'
        );
        return res;
      },
      { okToast: false }
    )
  );

  App.action('ppl-toggle-move', () => {
    paintStep4Summary();
  });

  App.action('ppl-sort-start', (btn) =>
    App.run(
      btn,
      async () => {
        const opts = collectSortOptions();
        if (!opts.source_dir) return { ok: false, error: 'Укажите исходную папку на шаге 1' };
        if (!opts.target_dir) return { ok: false, error: 'Укажите целевую папку на шаге 3' };
        const isMove = opts.mode === 'move' || (lastPlan && lastPlan.effective_mode === 'move');
        const allowMove = isMove ? !!(byId('ppl-allow-move') && byId('ppl-allow-move').checked) : false;
        if (isMove && !allowMove) {
          return { ok: false, error: 'Подтвердите режим перемещения галочкой перед запуском' };
        }

        const res = await App.api.post('/api/people/sort/start', {
          ...opts,
          allow_move: allowMove,
        });
        if (res.ok) {
          wasRunning = true;
          lastRunningTask = 'sort';
          App.toast('Сортировка запущена', 'ok');
          await Promise.all([refreshPeopleStatus(true), loadRuns()]);
        }
        return res;
      },
      { okToast: false }
    )
  );

  App.action('ppl-handoff-upload', () => {
    const opts = collectSortOptions();
    const targetDir =
      opts.target_dir ||
      (lastPlan && lastPlan.target_dir) ||
      (runs[0] && runs[0].target_dir) ||
      ((statusData && statusData.config && statusData.config.target_dir) || '');
    if (!targetDir) {
      App.toast('Сначала укажите целевую папку сортировки', 'warn');
      return;
    }
    if (typeof App.uploadPrefillDir === 'function') {
      App.uploadPrefillDir(targetDir);
    }
    App.go('#/upload');
  });

  App.action('ppl-runs-refresh', () => loadRuns());

  App.action('ppl-run-resume', async (btn) => {
    const rid = Number(btn.dataset.runId);
    if (!rid) return;
    const run = runs.find((item) => Number(item.id) === rid);
    if (run) {
      const pendingOps = run.pending_ops != null ? Number(run.pending_ops) : 1;
      const rolledBackOps = Number(run.rolled_back_ops || 0);
      if (!(pendingOps > 0) || rolledBackOps > 0) {
        App.toast('Этот запуск нельзя продолжить: нет ожидающих операций или он уже частично откачен', 'warn');
        return;
      }
    }
    const effMode = (run && (run.effective_mode || run.mode)) || btn.dataset.effectiveMode || '';
    const isMove = effMode === 'move';
    if (isMove) {
      const yes = await App.confirm({
        title: 'Продолжить перемещение?',
        text: `Запуск #${rid} работает в режиме перемещения (move): оставшиеся исходные файлы перенесутся в целевую папку. Продолжить?`,
        ok: 'Продолжить перемещение',
        danger: true,
      });
      if (!yes) return;
    }
    await App.run(
      btn,
      async () => {
        const res = await App.api.post(
          `/api/people/runs/${encodeURIComponent(String(rid))}/resume`,
          { allow_move: isMove }
        );
        if (res.ok) {
          wasRunning = true;
          lastRunningTask = 'sort';
          App.toast(`Продолжение запуска #${rid} начато`, 'ok');
          await Promise.all([refreshPeopleStatus(true), loadRuns()]);
        }
        return res;
      },
      { okToast: false }
    );
  });

  App.action('ppl-run-rollback', async (btn) => {
    const rid = Number(btn.dataset.runId);
    if (!rid) return;
    const yes = await App.confirm({
      title: `Откатить запуск #${rid}?`,
      text: 'Программа безопасно вернёт перемещённые файлы и удалит только немодифицированные ссылки/копии с проверкой SHA-256. Изменённые вручную файлы затронуты не будут.',
      ok: 'Откатить запуск',
      danger: true,
    });
    if (!yes) return;
    await App.run(
      btn,
      async () => {
        const res = await App.api.post(`/api/people/runs/${encodeURIComponent(String(rid))}/rollback`, {});
        if (res.ok) {
          wasRunning = true;
          lastRunningTask = 'rollback';
          App.toast(`Откат запуска #${rid} начат`, 'ok');
          await Promise.all([refreshPeopleStatus(true), loadRuns()]);
        }
        return res;
      },
      { okToast: false }
    );
  });
})();
