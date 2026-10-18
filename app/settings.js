'use strict';
// Settings window renderer: draws every 'settings:state' in place, sends 'settings:set' on change.
(() => {
  const $ = (id) => document.getElementById(id);
  const radios = document.querySelectorAll('input[name="close"]');
  // switch button id -> settings:set key
  const SWITCHES = { login: 'ui.openAtLogin', hidden: 'ui.startHidden', overlay: 'overlay', crops: 'engine.save_event_crops' };

  const setSwitch = (id, on) => KF.attr($(id), 'aria-checked', String(Boolean(on)));

  function render(s) {
    radios.forEach((r) => { r.checked = (r.value === 'tray') === Boolean(s.ui.closeToTray); });
    setSwitch('login', s.ui.openAtLogin);
    setSwitch('hidden', s.ui.startHidden);
    setSwitch('overlay', s.overlay.wanted);

    $('login').disabled = !s.packaged;
    KF.text($('d-login'), s.packaged ? '로그인하면 앱이 자동으로 시작됩니다.' : '설치된 앱에서만 쓸 수 있습니다 (개발 실행에서는 꺼짐).');

    const hk = $('hotkey-state');
    hk.classList.toggle('bad', !s.overlay.hotkey);
    KF.text(hk, s.overlay.hotkey ? '등록됨 · 게임 중에도 켜고 끔' : '단축키를 다른 앱이 쓰고 있음');

    const known = Boolean(s.engine);
    $('crops').disabled = !known;
    setSwitch('crops', known && s.engine.save_event_crops);
    KF.text($('d-crops'), known ? '인식한 킬피드 이벤트의 이미지를 데이터 폴더에 남깁니다.' : '엔진 연결 후 설정 가능');

    KF.text($('version'), s.version);
  }

  radios.forEach((r) => r.addEventListener('change', () => {
    if (r.checked) kfl.send('settings:set', { key: 'ui.closeToTray', value: r.value === 'tray' });
  }));
  for (const [id, key] of Object.entries(SWITCHES)) {
    $(id).addEventListener('click', () => {
      const value = $(id).getAttribute('aria-checked') !== 'true';
      setSwitch(id, value); // optimistic; the next settings:state confirms or reverts it
      kfl.send('settings:set', { key, value });
    });
  }
  $('open-data').addEventListener('click', () => kfl.send('app:open', 'data'));
  $('open-logs').addEventListener('click', () => kfl.send('app:open', 'logs'));

  kfl.on('settings:state', render);
  kfl.send('settings:ready');
})();
