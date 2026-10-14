/* Запуск интерфейса: маршрут из адреса и опрос статуса. */
(function () {
  'use strict';
  const App = window.App;
  App.route();
  App.startPolling();
  document.addEventListener('visibilitychange', () => { if (!document.hidden) App.refresh(); });
})();
