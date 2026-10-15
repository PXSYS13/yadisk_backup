/* Поддержать проект и «Нас поддерживают» — одна страница вместо ссылок на каждом экране.
   Список поддержавших — supporters.json на GitHub (/api/supporters): донаты DonationAlerts
   попадают туда сами. */
(function () {
  'use strict';
  const App = window.App;
  const { esc, fmt, byId } = App;

  const PAGE = 9;
  let visible = PAGE;

  function card(x, i) {
    return `<li class="supporter"><div class="supporter-top">${App.avatar(x.name, i)}
        <div class="grow"><div class="supporter-name" title="${esc(x.name)}">${esc(x.name)}</div>
          <div class="supporter-when">${esc(App.dayAgo(x.date))}</div></div>
        ${x.amount != null ? `<div class="supporter-sum">${esc(App.money(x.amount, x.currency))}</div>` : ''}</div>
      ${x.message ? `<blockquote>«${esc(x.message)}»</blockquote>` : ''}</li>`;
  }

  function paintList() {
    const sp = App.supporters;
    const box = byId('support-list');
    if (!box) return;
    if (!sp) {
      App.html(box, `<div class="empty">${App.icon('hourglass')}Загружаю список…</div>`);
      App.text(byId('support-count'), '');
      App.text(byId('support-updated'), '');
      return;
    }
    const list = sp.supporters || [];
    const people = sp.people || 0;
    App.text(byId('support-count'), people
      ? `Спасибо ${fmt.num(people)} ${fmt.plural(people, 'человеку', 'людям', 'людям')}, которые помогли проекту` : '');
    App.text(byId('support-updated'), sp.updated ? `Последнее обновление списка: ${App.dayAgo(sp.updated)}` : '');
    if (!list.length) {
      App.html(box, `<div class="empty-box"><span class="icon-box">${App.icon('heart')}</span>
        <p>Здесь появятся имена тех, кто поддержал проект</p></div>`);
      return;
    }
    App.html(box, `<ul class="supporters">${list.slice(0, visible).map(card).join('')}</ul>
      ${visible < list.length ? `<div class="more-row"><button data-action="supporters-more">Показать ещё</button></div>` : ''}`);
  }

  App.screens.support = {
    mount(el) {
      const I = App.icon;
      el.innerHTML = `<section class="card support-hero">
          <span class="badge-art">${I('heart')}</span>
          <h1>Поддержать проект</h1>
          <p class="sub">Программа бесплатная и открытая. Её делают в свободное время, чтобы люди не потеряли свои фото и файлы.
            Если она помогла — можно поддержать автора или просто рассказать о ней знакомым.</p>
          <p class="support-note">Имя и сообщение из доната появятся ниже, в «Нас поддерживают», примерно через полчаса.
            Не хотите, чтобы имя было видно, — подпишитесь «Аноним».</p>
          <div class="row actions">
            <a class="btn primary big" id="support-donate" href="https://dalink.to/davronsvv" target="_blank" rel="noopener">${I('heart')}Поддержать на DonationAlerts</a>
            <a class="btn big" id="support-telegram" href="https://t.me/davronsv" target="_blank" rel="noopener">${I('send')}Канал в Telegram</a>
            <a class="btn big" href="https://github.com/PXSYS13/yadisk_backup" target="_blank" rel="noopener">${I('code')}Проект на GitHub</a>
          </div>
        </section>
        <section aria-labelledby="support-title">
          <div class="supporters-head"><div><h2 id="support-title">Нас поддерживают</h2>
            <p id="support-count"></p></div><span class="tiny" id="support-updated"></span></div>
          <div id="support-list"></div>
          <p class="tiny" style="margin-top:24px">Спасибо и участникам сообщества, которые помогли с кодом и идеями.</p>
        </section>`;
    },
    show() {
      visible = PAGE;
      App.loadSupporters();
      paintList();
    },
    update(s) {
      if (s.donation_url) byId('support-donate').href = s.donation_url;
      if (s.telegram_url) byId('support-telegram').href = s.telegram_url;
      App.text(byId('footer-text'), `Бэкап Яндекс.Диска ${esc(s.app_version || '')}`);
      paintList();
    },
  };

  App.action('supporters-more', () => {
    visible += PAGE;
    paintList();
  });
})();
