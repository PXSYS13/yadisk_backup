/* Поддержать проект — одна страница вместо ссылок на каждом экране. */
(function () {
  'use strict';
  const App = window.App;
  const { esc, byId } = App;

  App.screens.support = {
    mount(el) {
      const I = App.icon;
      el.innerHTML = `<div class="card hero state-done"><div class="hero-body">
        <div class="hero-art">${I('heart')}</div>
        <div><h1>Поддержать проект</h1>
        <p class="sub">Программа бесплатная и открытая. Её делают в свободное время, чтобы люди не потеряли свои фото и файлы.
        Если она помогла — можно поддержать автора или просто рассказать о ней знакомым.</p>
        <div class="row actions">
          <a class="btn primary big" id="support-donate" href="https://dalink.to/davronsvv" target="_blank" rel="noopener">${I('heart')}Поддержать на DonationAlerts</a>
          <a class="btn" id="support-telegram" href="https://t.me/davronsv" target="_blank" rel="noopener">${I('send')}Канал в Telegram</a>
          <a class="btn" href="https://github.com/PXSYS13/yadisk_backup" target="_blank" rel="noopener">${I('external')}Проект на GitHub</a>
        </div>
        <p class="tiny" style="margin-top:16px">Спасибо участникам сообщества, которые помогли с кодом и идеями.</p></div></div></div>`;
    },
    update(s) {
      if (s.donation_url) byId('support-donate').href = s.donation_url;
      if (s.telegram_url) byId('support-telegram').href = s.telegram_url;
      App.text(byId('footer-text'), `Бэкап Яндекс.Диска ${esc(s.app_version || '')}`);
    },
  };
})();
