/* Главная: одна главная кнопка, понятное состояние, прогресс по Диску и Безлимиту. */
(function () {
  'use strict';
  const App = window.App;
  const { esc, fmt, byId } = App;

  function heroActions(r, s) {
    const failed = (r.summary || {}).failed_files || 0;
    const running = !!r.running;
    switch (r.state) {
      case 'idle':
        return r.action && r.action.id === 'setup'
          ? '<button class="primary big" data-action="go-setup">Начать настройку</button>'
          : '<button class="primary big" data-action="rescue-start">Сохранить мои файлы</button>';
      case 'running':
        return '<button data-action="rescue-stop">Остановить</button>';
      case 'waiting':
        return '<button class="primary big" data-action="retry-now">Попробовать сейчас</button>' +
          '<button data-action="rescue-stop">Остановить</button>';
      case 'needs_action':
        return running ? '<button data-action="rescue-stop">Остановить</button>' : App.actionButtons(r.action, s);
      case 'done':
        return '<button class="primary big" data-action="open-folder" data-which="download">Открыть папку</button>' +
          (failed ? `<button data-action="retry-failed" data-source="all">Повторить для ${fmt.files(failed)}</button>` : '') +
          '<button data-action="rescue-start">Проверить новые файлы</button>';
      case 'stopped':
        return '<button class="primary big" data-action="rescue-start">Продолжить</button>';
      default:
        return '';
    }
  }

  function whatLine(s) {
    const st = s.settings || {};
    const labels = st.category_labels || {};
    const sources = (st.sources || ['disk', 'unlim']).map((x) => (x === 'disk' ? 'Диск' : 'Безлимит')).join(' и ');
    const cats = st.categories ? st.categories.map((c) => labels[c] || c).join(', ') : 'все файлы';
    return `Что скачиваем: ${esc(sources)} — ${esc(cats)}${st.gentle ? ' · бережный режим' : ''} · <a href="#/settings">изменить</a>`;
  }

  App.screens.home = {
    mount(el) {
      el.innerHTML = `<div id="home-banner"></div>
        <div class="card hero" id="home-hero">
          <h1 id="home-headline">Загружаю…</h1>
          <p id="home-subline"></p>
          <div class="row actions" id="home-actions"></div>
          <p class="tiny" id="home-what" style="margin-top:14px"></p>
        </div>
        <div id="home-notice" style="margin-top:16px"></div>
        <div class="grid-2" id="home-sources" style="margin-top:16px"></div>
        <div id="home-where" style="margin-top:16px"></div>
        <div id="home-safety" style="margin-top:16px"></div>`;
    },
    update(s) {
      const r = s.rescue || {};
      App.html(byId('home-banner'), App.actionBanner(r.action, s));
      byId('home-hero').className = `card hero state-${r.state || 'idle'}`;
      App.text(byId('home-headline'), r.headline || '');
      App.text(byId('home-subline'), r.subline || '');
      App.html(byId('home-actions'), heroActions(r, s));
      App.html(byId('home-what'), r.action && r.action.id === 'setup' ? '' : whatLine(s));
      App.html(byId('home-notice'), App.noticeBanner(r.notice));
      const src = r.sources || {};
      App.html(byId('home-sources'), App.sourceBlock('disk', src.disk) + App.sourceBlock('unlim', src.unlim));
      App.html(byId('home-where'), r.action && r.action.id === 'setup' ? '' : App.whereFiles(s));
      const showSafety = r.state !== 'idle' && !((r.summary || {}).complete);
      App.html(byId('home-safety'), showSafety ? App.safetyBanner() : '');
    },
  };
})();
