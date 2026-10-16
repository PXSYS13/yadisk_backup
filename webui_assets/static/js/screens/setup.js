/* Настройка за 3 шага: ключ Диска → папка → Безлимит. Каждый шаг — строка «Доступа». */
(function () {
  'use strict';
  const App = window.App;
  const { byId } = App;

  const STEPS = [
    { kind: 'token', title: 'Ключ доступа к Диску',
      text: 'Ключ даёт программе право читать ваш Диск. Он хранится только на этом компьютере.',
      skip: 'У меня только Безлимит — пропустить' },
    { kind: 'folder', title: 'Папка для файлов',
      text: 'Сюда программа сложит всё, что скачает. Выберите диск, где хватит места, — подойдёт и внешний.', skip: '' },
    { kind: 'cookies', title: 'Безлимит (фото с телефона)',
      text: 'Фото, которые телефон загружал в безлимит, скачиваются по данным входа из браузера. Если безлимита нет — пропустите.',
      skip: 'Пропустить — качать только Диск' },
  ];
  let step = 0;
  let finished = false;

  function isDone(kind, s) {
    if (!s) return false;
    if (kind === 'token') return !!(s.token && (s.token.ok || s.token.unreachable));
    if (kind === 'folder') return !!(s.config && s.config.download_dir_set);
    return !!(s.cookies && s.cookies.ok);
  }

  function render() {
    const st = STEPS[step];
    byId('setup-step').innerHTML = finished
      ? `<div class="card-head"><span class="icon-box ok">${App.icon('check')}</span><div class="grow"><h2>Всё готово</h2></div></div>
         <p class="lead">Нажмите кнопку — программа начнёт сохранять ваши файлы.
          Можно свернуть браузер, но не закрывайте чёрное окно программы.</p>
         <div class="row"><button class="primary big" data-action="setup-start">${App.icon('download')}Сохранить мои файлы</button></div>`
      : `<h2>Шаг ${step + 1}. ${st.title}</h2><p class="lead">${st.text}</p><div id="setup-access"></div>
         <div class="row" style="margin-top:16px">
           ${step > 0 ? `<button data-action="setup-back">${App.icon('back')}Назад</button>` : ''}
           <span class="spacer"></span>
           ${st.skip ? `<button class="link" data-action="setup-next">${st.skip}</button>` : ''}
           <button data-action="setup-next" data-require="${st.kind}">Дальше${App.icon('arrow')}</button>
         </div>`;
    if (!finished) {
      App.Access.mount(byId('setup-access'), [st.kind], [st.kind], { buttons: false });
      if (App.state.status) App.Access.update(byId('setup-access'), App.state.status);
    }
    paintStepper(App.state.status);
  }

  function paintStepper(s) {
    const html = STEPS.map((st, i) => {
      const done = isDone(st.kind, s);
      return `<div class="s ${i === step && !finished ? 'active' : ''} ${done ? 'done' : ''}" data-action="setup-goto" data-step="${i}">
        <span class="n">${done ? App.icon('check') : i + 1}</span><b>${st.title}</b></div>`;
    }).join('');
    App.html(byId('setup-stepper'), html);
  }

  App.screens.setup = {
    mount(el) {
      el.innerHTML = `<a class="back" href="#/home">${App.icon('back')}На главную</a>
        <div class="page-head"><h1>Настройка за 3 шага</h1>
          <p>Один раз дайте программе доступ и выберите папку — это 3–5 минут. Всё хранится только на этом компьютере.</p></div>
        <div class="stepper" id="setup-stepper"></div>
        <div class="card" id="setup-step"></div>`;
      el.addEventListener('access-saved', () => {
        setTimeout(() => { if (step < STEPS.length - 1) { step++; render(); } else { finished = true; render(); } }, 400);
      });
    },
    show() {
      // Открываем первый ещё не сделанный шаг
      const s = App.state.status;
      finished = false;
      const first = STEPS.findIndex((st) => !isDone(st.kind, s));
      step = first === -1 ? 0 : first;
      finished = first === -1 && !!s;
      render();
    },
    update(s) {
      paintStepper(s);
      const box = byId('setup-access');
      if (box) App.Access.update(box, s);
    },
  };

  App.action('setup-next', (btn) => {
    const need = btn.dataset.require;
    if (need && !isDone(need, App.state.status)) {
      App.toast(need === 'folder' ? 'Сначала сохраните папку' : 'Сначала сохраните, или нажмите «пропустить»', 'err');
      return;
    }
    if (step < STEPS.length - 1) step++;
    else finished = true;
    render();
  });
  App.action('setup-back', () => { if (step > 0) { step--; finished = false; render(); } });
  App.action('setup-goto', (el) => { step = Number(el.dataset.step) || 0; finished = false; render(); });
  App.action('setup-start', async (btn) => {
    const res = await App.run(btn, () => App.api.post('/api/rescue/start'));
    if (res && res.ok) App.go('#/home');
  });
})();
