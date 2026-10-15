/* Запуск интерфейса: иконки и тема, маршрут из адреса, опрос статуса. */
(function () {
  'use strict';
  const App = window.App;
  App.hydrateIcons();
  App.theme.paint();
  App.route();
  App.startPolling();
  App.loadSupporters();
  setInterval(App.loadSupporters, 15 * 60 * 1000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) App.refresh(); });
})();
