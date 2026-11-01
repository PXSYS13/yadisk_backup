/* Флажок «От автора» и другие программы автора. Когда скачивание закончилось целиком и удачно,
   на главной карточке появляется флажок; в первый раз под карточкой сама раскрывается карточка
   «Будем на связи» — канал, где автор пишет о сбоях, и донат. Поверх экрана ничего не всплывает:
   после «Не сейчас», перехода по ссылке или трёх заходов на Главную остаётся только флажок.
   Ссылки обычные, их открывает сам человек — программа по ним ничего не запрашивает. Что флажок
   уже показан, помнит только этот браузер: время и отметки, но не то, на что нажали. */
(function () {
  'use strict';
  const App = window.App;
  const { esc, icon } = App;

  const TELEGRAM_URL = 'https://t.me/davronsv';
  const DONATE_URL = 'https://dalink.to/davronsvv';
  // Карточку программы прячет кнопка «Уже есть»
  const APPS = {
    vk: { icon: 'users', name: 'VK Backup', ask: 'А ВКонтакте?', url: 'https://github.com/PXSYS13/vk_backup',
      text: 'Бэкап ВКонтакте: переписки, фото, стена и друзья из официального архива ВК, без пароля.' },
    icloud: { icon: 'image', name: 'iCloud Backup', url: 'https://github.com/PXSYS13/icloud_backup',
      text: 'Скачивает всю медиатеку iCloud Фото на компьютер, если вы больше не платите за iCloud+. Бета-версия.' },
  };
  const KEY = 'ydb.flag';
  const VISITS = 3;         // столько заходов на Главную карточка открыта сама, дальше — только флажок
  const AGAIN_DAYS = 30;    // после нового скачивания раскроем снова не раньше, чем через месяц
  const AGAIN_CLICKED = 90; // …а если человек уже переходил по ссылке — через три
  const DAY_MS = 864e5;

  // ── Что помнит браузер: время показа и отметки, никаких «на что нажали» ──
  // Без хранилища (приватный режим, запрещены данные сайтов) помним до перезагрузки страницы.
  // Демо — только в памяти: придуманное скачивание не должно откладывать флажок настоящего
  let memory = {};
  function read() {
    if (App.demo) return { ...memory };
    try {
      const raw = localStorage.getItem(KEY);
      const v = raw === null ? memory : JSON.parse(raw);
      return v && typeof v === 'object' && !Array.isArray(v) ? { ...v } : {};
    } catch (e) {
      return { ...memory };
    }
  }
  function save(patch) {
    const next = { ...read(), ...patch };
    memory = next;
    if (!App.demo) {
      try { localStorage.setItem(KEY, JSON.stringify(next)); } catch (e) { /* хватит памяти страницы */ }
    }
    return next;
  }

  // Номер удачного скачивания — время, когда оно закончилось. null — просить не о чем: идёт скачивание,
  // нужно действие человека, пауза, часть файлов не скачалась или программа не отвечает
  function finished(s, offline) {
    const r = (s && s.rescue) || {};
    if (offline || r.running || r.state !== 'done' || r.action || !(r.summary || {}).complete) return null;
    if (!r.finished_at) return null;
    return App.demo ? 1 : r.finished_at;  // демо на каждом опросе «заканчивает» скачивание заново
  }

  // Закончилось новое удачное скачивание: пора ли снова раскрыть карточку. Пауза считается от прошлого
  // показа до конца этого скачивания — просто зайти через месяц, без нового скачивания, мало
  function noteBackup(id) {
    const f = read();
    if (f.lastBackupId === id) return;
    const pause = (f.clickedAt ? AGAIN_CLICKED : AGAIN_DAYS) * DAY_MS;
    if (f.lastFullAt && id * 1000 < f.lastFullAt + pause) return;
    save({ lastFullAt: Date.now(), lastBackupId: id, visits: 0, expanded: null, dismissed: null, clickedAt: null });
  }

  let visitPending = false;  // зашли на Главную: заход засчитаем, когда станет известно скачивание
  function noteVisit(id) {
    const f = read();
    if (f.lastBackupId === id) save({ visits: (f.visits || 0) + 1 });
  }

  function isOpen(f, id) {
    if (f.expanded === id) return true;
    if (f.lastBackupId !== id || f.dismissed === id) return false;
    return (f.visits || 0) <= VISITS;
  }

  // ── Разметка ──
  // Обычная ссылка: открывает её сам человек. data-ask — ссылки карточки, переход по ним её сворачивает
  const link = (href, label, cls, ico, ask = false) =>
    `<a class="${cls}" href="${esc(href)}" target="_blank" rel="noopener noreferrer"${ask ? ' data-ask' : ''}>${icon(ico)}${esc(label)}</a>`;

  function appCard(key, withAsk = false) {
    const p = APPS[key];
    if ((read().owned || {})[key]) return '';
    return `<div class="app-promo"><span class="icon-box accent">${icon(p.icon)}</span>
      <div class="app-text"><b>${withAsk ? `${esc(p.ask)} ` : ''}${esc(p.name)}</b><span class="tiny">${esc(p.text)}</span></div>
      <div class="row promo-actions">${link(p.url, 'Скачать', 'btn small', 'download')}
        <button type="button" class="small ghost" data-action="app-owned" data-app="${esc(key)}">Уже есть</button></div></div>`;
  }

  const pennant = (open, glow) => `<button type="button" class="pennant${glow ? ' glow' : ''}" data-action="flag-toggle"
    aria-expanded="${open}"${open ? ' aria-controls="flag-card"' : ''}>${icon('flag')}От автора</button>`;

  function card(s) {
    return `<section class="card flag-card" id="flag-card" aria-labelledby="flag-card-title">
      <div class="card-head"><span class="icon-box accent">${icon('send')}</span>
        <div class="grow"><h3 id="flag-card-title">Будем на связи</h3><span class="tiny">От автора программы</span></div>
        <button type="button" class="small ghost" data-action="flag-later">Не сейчас</button></div>
      <p class="muted">О сбоях Яндекса и ВК пишу в канале. Программы бесплатные, взнос любой суммы — по желанию.</p>
      <div class="row actions">${link(s.telegram_url || TELEGRAM_URL, 'Подписаться', 'btn primary', 'send', true)}${
        link(s.donation_url || DONATE_URL, 'Поддержать', 'btn', 'heart', true)}</div>
      ${appCard('vk', true)}</section>`;
  }

  App.flag = {
    // Человек открыл Главную (App.route → home.show)
    visit() { visitPending = true; },
    // Флажок на главной карточке и карточка под ней. finished — скачивание закончилось целиком
    // и удачно: тогда Главная не показывает рядом вторую просьбу о поддержке
    home(s, offline) {
      const id = finished(s, offline);
      if (!id || read().hidden) return { finished: !!id, pennant: '', card: '' };
      noteBackup(id);
      if (visitPending) { noteVisit(id); visitPending = false; }
      const f = read();
      const open = isOpen(f, id);
      // Переливается только при первом показе; анимация в app.css — два раза, и никакой,
      // если в системе просят меньше движения
      const glow = open && (f.visits || 0) <= 1 && !f.expanded;
      return { finished: true, pennant: pennant(open, glow), card: open ? card(s) : '' };
    },
    // «Поддержать»: карточки других программ (пусто, если все уже есть) и переключатель флажка
    apps() {
      const cards = Object.keys(APPS).map((k) => appCard(k)).join('');
      return cards ? `<h2 id="support-apps-title">Другие программы автора</h2><div class="promos">${cards}</div>` : '';
    },
    note() {
      const hidden = !!read().hidden;
      return `<p>Программа никуда не сообщает, нажали ли вы на ссылку. Флажок «От автора» появляется на Главной
        после удачного скачивания; что его уже показали, помнит только этот браузер.</p>
        <button type="button" class="small" data-action="flag-hide" aria-pressed="${hidden}">${icon(hidden ? 'check' : 'flag')}Скрыть флажок</button>`;
    },
  };

  // ── Действия ──
  // Блок перерисован — фокус возвращается на кнопку в нём, а не пропадает в начало страницы
  function repaint(boxId) {
    App.render();
    const box = boxId && App.byId(boxId);
    const btn = box && box.querySelector('button');
    if (btn) btn.focus();
  }
  const currentId = () => finished(App.state.status, App.state.offline);

  App.action('flag-toggle', () => {
    const id = currentId();
    if (!id) return;
    save(isOpen(read(), id) ? { expanded: null, dismissed: id } : { expanded: id });
    repaint('home-flag');
  });
  App.action('flag-later', () => {
    const id = currentId();
    if (id) save({ expanded: null, dismissed: id });
    repaint('home-flag');
  });
  App.action('flag-hide', () => {
    save({ hidden: !read().hidden });
    repaint('support-flag');
  });
  App.action('app-owned', (el) => {
    const key = el.dataset.app;
    if (!Object.prototype.hasOwnProperty.call(APPS, key)) return;
    save({ owned: { ...(read().owned || {}), [key]: true } });
    repaint(el.closest('#flag-card') ? 'home-flag' : 'support-apps');
  });

  // Перешли в канал или к донату из карточки — значит, увидели: карточка сворачивается, а раскроется
  // снова не раньше чем через три месяца. Запоминаем только время, не то, куда нажали. Ссылку
  // открывает сам браузер: обработчик её не останавливает и никуда ничего не отправляет
  document.addEventListener('click', (ev) => {
    if (!ev.target.closest('#flag-card a[data-ask]')) return;
    const id = currentId();
    if (!id) return;
    save({ clickedAt: Date.now(), expanded: null, dismissed: id });
    setTimeout(App.render, 0);  // сначала браузер откроет ссылку, потом карточка свернётся
  });
})();
