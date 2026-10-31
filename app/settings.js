'use strict';
// Settings window renderer: draws every 'settings:state' in place, sends 'settings:set' on change.
(() => {
  const $ = (id) => document.getElementById(id);
  const radios = document.querySelectorAll('input[name="close"]');
  // switch button id -> settings:set key
  const SWITCHES = { login: 'ui.openAtLogin', hidden: 'ui.startHidden', overlay: 'overlay', crops: 'engine.save_event_crops' };

  const setSwitch = (id, on) => KF.attr($(id), 'aria-checked', String(Boolean(on)));
  let updateStatus = 'disabled';

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
    const update = s.update || { status: 'disabled', message: '설치된 Windows 앱에서 사용할 수 있습니다.' };
    updateStatus = update.status;
    KF.text($('update-status'), update.message);
    const labels = { idle: '업데이트 확인', current: '업데이트 확인', checking: '확인 중…',
      available: '새 버전 다운로드', downloading: '다운로드 중…', ready: '업데이트하고 재시작',
      applying: '업데이트 준비 중…', error: '업데이트 다시 확인', disabled: '설치판에서 사용' };
    KF.text($('update-action'), labels[update.status] || labels.disabled);
    $('update-action').disabled = !['idle', 'current', 'available', 'ready', 'error'].includes(update.status);
    $('update-progress').hidden = update.status !== 'downloading';
    $('update-progress').value = Math.max(0, Math.min(100, Number(update.percent) || 0));
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
  $('open-updates').addEventListener('click', () => kfl.send('app:open', 'updates'));
  $('update-action').addEventListener('click', () => {
    if ($('update-action').disabled) return;
    kfl.send(updateStatus === 'ready' ? 'update:install' : updateStatus === 'available' ? 'update:download' : 'update:check');
  });

  kfl.on('settings:state', render);
  kfl.send('settings:ready');
})();
