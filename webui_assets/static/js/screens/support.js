/* Поддержать проект — одна страница вместо ссылок на каждом экране. */
(function () {
  'use strict';
  const App = window.App;
  const { esc, byId } = App;

  App.screens.support = {
    mount(el) {
      el.innerHTML = `<div class="card hero"><h1>Поддержать проект</h1>
        <p>Программа бесплатная и открытая. Её делают в свободное время, чтобы люди не потеряли свои фото и файлы.
        Если она помогла — можно поддержать автора или просто рассказать о ней знакомым.</p>
        <div class="row actions">
          <a class="btn primary" id="support-donate" href="https://www.donationalerts.com/r/davronsvv" target="_blank" rel="noopener">Поддержать на DonationAlerts</a>
          <a class="btn" id="support-telegram" href="https://t.me/davronsv" target="_blank" rel="noopener">Канал в Telegram</a>
          <a class="btn" href="https://github.com/PXSYS13/yadisk_backup" target="_blank" rel="noopener">Проект на GitHub</a>
        </div>
        <p class="tiny" style="margin-top:16px">Спасибо участникам сообщества, которые помогли с кодом и идеями.</p></div>`;
    },
    update(s) {
      if (s.donation_url) byId('support-donate').href = s.donation_url;
      if (s.telegram_url) byId('support-telegram').href = s.telegram_url;
      App.text(byId('footer-text'), `Бэкап Яндекс.Диска ${esc(s.app_version || '')}`);
    },
  };
})();
