/* Запуск интерфейса: иконки и тема, маршрут из адреса, опрос статуса. */
(function () {
  'use strict';
  const App = window.App;
  App.hydrateIcons();
  App.theme.paint();
  App.route();
  App.startPolling();
  document.addEventListener('visibilitychange', () => { if (!document.hidden) App.refresh(); });
})();
