/* Помощь: что происходит с Яндексом, частые вопросы, инструкция, отчёт для поддержки, журналы. */
(function () {
  'use strict';
  const App = window.App;
  const { esc, fmt, byId } = App;

  const STATE = { ok: ['ok', 'работает'], degraded: ['wait', 'с перебоями'], down: ['err', 'не отвечает'], unknown: ['', 'ещё не проверяли'] };
  const FAQ = [
    ['Часть файлов «ждёт Яндекса» — что это?',
      'Каждый файл хранится на конкретном сервере Яндекса. После атак на дата-центры часть серверов молчит. ' +
      'Программа замечает это за 10 секунд, откладывает файлы такого сервера и качает остальные. Каждые 30 минут она пробует ' +
      'отложенные снова — как только сервер оживёт, файлы скачаются сами. Попытки при этом не тратятся.'],
    ['Что за папка «_превью»?',
      'Пока оригинал фото недоступен, программа сохраняет его уменьшенную копию (до 1280 пикселей) — на случай, если оригинал ' +
      'так и не вернётся. Когда оригинал скачается, он ляжет на своё место, а превью можно удалить. Выключить превью можно в «Настройках».'],
    ['Программа пишет, что ключ или доступ устарел',
      'Ключ к Диску живёт около года, доступ к Безлимиту (данные входа из браузера) — от нескольких дней до недель. ' +
      'Нажмите кнопку в сообщении на Главной и введите новый — скачивание продолжится с того же места.'],
    ['Можно закрыть браузер или выключить компьютер?',
      'Браузер — можно: программа работает в чёрном окне. Его закрывать нельзя. Пока идёт скачивание, программа не даёт ' +
      'компьютеру уснуть, но закрытая крышка ноутбука его всё равно усыпит. Если выключили — просто запустите снова: ' +
      'уже скачанное второй раз качаться не будет.'],
    ['Где мои файлы и что будет при повторном запуске?',
      'В папке, которая указана на Главной в блоке «Где мои файлы». Обычный Диск — с сохранением ваших папок, фото из ' +
      'Безлимита — в подпапке «_unlim». При повторном запуске программа сверяет файлы и докачивает только недостающее.'],
    ['Это безопасно?',
      'Ключ и данные входа хранятся только на вашем компьютере и уходят только на серверы Яндекса. Отчёт для поддержки их не содержит. ' +
      'Программа ничего не удаляет ни на Диске, ни у вас — только скачивает.'],
  ];

  // Подписи проверок без жаргона
  const PROBE_LABEL = { rest_api: 'Официальный доступ к Диску (обычные файлы)', web: 'Веб-версия Диска (Безлимит)' };

  let logsLoaded = false;

  function healthHtml(h, s) {
    if (!h) return '<p class="muted">Монитор запускается вместе с программой — данные появятся через минуту.</p>';
    const [cls, label] = STATE[h.overall] || STATE.unknown;
    const probes = (h.probes || []).map((p) => {
      const [pc, pl] = STATE[p.state] || STATE.unknown;
      const tl = (p.timeline || []).slice(-96).map((b) =>
        `<span class="${b.ok_ratio == null ? '' : b.ok_ratio >= 0.99 ? 'ok' : b.ok_ratio > 0.5 ? 'slow' : 'fail'}"></span>`).join('');
      const avail = p.availability && p.availability['1h'] != null ? ` · за час ${p.availability['1h']}%` : '';
      return `<div class="probe"><div style="flex:1"><b>${esc(PROBE_LABEL[p.key] || p.label)}</b>
          <div class="tiny">${p.latency_ms_p50 ? `ответ ≈ ${p.latency_ms_p50} мс` : ''}${avail}</div>
          <div class="timeline" title="последние сутки">${tl}</div></div>
        <span class="badge ${pc}">${pl}</span></div>`;
    }).join('');
    const src = (s.rescue || {}).sources || {};
    const silent = ((src.disk || {}).storage_hosts_down || 0) + ((src.unlim || {}).storage_hosts_down || 0);
    return `<div class="row" style="margin-bottom:8px"><span class="badge ${cls}">${label}</span><span class="muted">${esc(h.summary || '')}</span></div>
      ${probes}
      ${silent ? `<p class="small" style="margin-top:12px">Серверов хранения, которые сейчас не отвечают на ваши файлы: <b>${fmt.num(silent)}</b>.</p>` : ''}`;
  }

  App.screens.help = {
    mount(el) {
      el.innerHTML = `<div class="stack">
        <div class="card"><h2>Что происходит с Яндексом</h2><div id="help-health"></div>
          <div class="section-title" style="margin-top:16px">Новости</div><ul class="small" id="help-news" style="padding-left:18px"></ul></div>
        <div class="card faq"><h2>Частые вопросы</h2>
          ${FAQ.map(([q, a]) => `<details><summary>${esc(q)}</summary><div class="body"><p>${esc(a)}</p></div></details>`).join('')}</div>
        <div class="card"><h2>Нужна помощь</h2>
          <p class="lead">Подробная инструкция для новичков — по шагам, с картинками. Если что-то не работает — скачайте отчёт
          и пришлите его в Telegram: в нём нет ключа и паролей.</p>
          <div class="row">
            <a class="btn" href="/guide" target="_blank" rel="noopener">Открыть инструкцию</a>
            <a class="btn primary" href="/api/logs/bundle">Скачать отчёт для поддержки</a>
            <a class="btn" id="help-telegram" href="https://t.me/davronsv" target="_blank" rel="noopener">Написать в Telegram</a>
          </div></div>
        <div class="card"><details id="help-logs-box"><summary>Журналы программы</summary><div class="body">
          <div class="row" style="margin-bottom:10px"><select id="help-log-select" style="max-width:360px"></select>
            <button data-action="help-log-show">Показать</button></div>
          <div class="console" id="help-log"></div></div></details></div>
      </div>`;
      byId('help-logs-box').addEventListener('toggle', (e) => { if (e.target.open && !logsLoaded) loadLogs(); });
    },
    update(s) {
      App.html(byId('help-health'), healthHtml(s.yandex_health, s));
      App.html(byId('help-news'), (s.news || []).map((n) =>
        `<li><a href="${esc(n.url)}" target="_blank" rel="noopener">${esc(n.title)}</a></li>`).join(''));
      if (s.telegram_url) byId('help-telegram').href = s.telegram_url;
    },
  };

  async function loadLogs() {
    const res = await App.api.get('/api/logs');
    if (!res.ok) return;
    logsLoaded = true;
    App.html(byId('help-log-select'), (res.data.logs || []).map((l) =>
      `<option value="${esc(l.name)}">${esc(l.name)} · ${fmt.bytes(l.size)}</option>`).join(''));
  }

  App.action('help-log-show', async (btn) => {
    const name = byId('help-log-select').value;
    if (!name) return;
    btn.disabled = true;
    const res = await App.api.get(`/api/logs/tail?name=${encodeURIComponent(name)}&lines=300`);
    btn.disabled = false;
    App.text(byId('help-log'), res.ok ? (res.data.lines || []).join('\n') : res.error);
  });
})();
