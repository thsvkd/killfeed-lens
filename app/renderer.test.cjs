'use strict';
// Run the real board/overlay and preload in hidden Electron windows; no capture engine or user data.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

if (!process.versions.electron) {
  const env = { ...process.env };
  delete env.ELECTRON_RUN_AS_NODE;
  const r = require('node:child_process').spawnSync(require('electron'), [__filename], { env, stdio: 'inherit', windowsHide: true });
  process.exit(r.status ?? 1);
}

const { app, BrowserWindow, ipcMain } = require('electron');
app.disableHardwareAcceleration(); // same software compositing as the shipped app
app.commandLine.appendSwitch('disable-renderer-backgrounding');
app.commandLine.appendSwitch('disable-backgrounding-occluded-windows');
app.setPath('userData', fs.mkdtempSync(path.join(os.tmpdir(), 'kfl-renderer-test-')));
const failures = [];
function check(label, fn) {
  try { fn(); console.log(`PASS ${label}`); }
  catch (e) { failures.push(label); console.error(`FAIL ${label}: ${e.message}`); }
}
async function open(file, readyChannel = 'ui:ready') {
  const win = new BrowserWindow({ show: false, width: 1200, height: 900,
    webPreferences: { preload: path.join(__dirname, 'preload.js'), contextIsolation: true, sandbox: true, backgroundThrottling: false } });
  const ready = new Promise((resolve) => {
    const listener = (event) => {
      if (event.sender === win.webContents) { ipcMain.removeListener(readyChannel, listener); resolve(); }
    };
    ipcMain.on(readyChannel, listener);
  });
  await win.loadFile(path.join(__dirname, file));
  await ready;
  return win;
}
async function send(win, snap) {
  win.webContents.send('snapshot', snap);
  const players = fixtureCount(snap);
  await win.webContents.executeJavaScript(`new Promise((resolve,reject) => {
    const end = performance.now() + 3000;
    const poll = () => document.querySelector('.card,.row') && document.getElementById('players').textContent === ${JSON.stringify(players)}
      ? resolve() : performance.now() > end ? reject(new Error('snapshot was not rendered')) : setTimeout(poll, 10);
    poll();
  })`);
}
function fixtureCount(snap) { return snap.hud || snap.start_teams ? String(snap.players_left) : '—'; }
const player = (name, st = 'alive', extra = {}) => ({ name, st, at: 0, back: 0, sure: true, ...extra });
const team = (id, left, players, extra = {}) => ({ id, left, players, slots: 4, kills: 0, knocks: 0,
  out: false, maybe_out: false, at: 0, atk: 0, hit: 0, assumed: 0, ...extra });
const fixture = { v: 1, now: 10000, mode: 'demo', tracking: false, status: '검증', error: false,
  players_left: 7, teams_left: 2, teams_left_min: 2, start_teams: 25, squad: 4, hud: null,
  basis: '검증', kills: 0, knocked: 1, last_at: 0, events: [], teams: [
    team(2, 3, [player('Bravo', 'dead')], { maybe_out: true }),
    team(3, 2, [player('Charlie', 'knocked', { downs: 2, knock_seconds: 30 }), player('Delta', 'dead'), player('Echo', 'alive', { sure: false })]),
    team(1, 4, [player('Alpha', 'alive', { sure: false, downs: 1 })]),
    team(4, 0, [], { out: true }), team(null, 7, [player('Unassigned')], { slots: 1 }),
  ] };

app.whenReady().then(async () => {
  const board = await open('index.html');
  await send(board, fixture);
  const alive = await board.webContents.executeJavaScript(`document.querySelector('[data-v="alive"]').click(); [...document.querySelectorAll('.card .badge')].map(e=>e.textContent)`);
  check('생존 탭은 전멸 가능과 팀 미확인을 제외', () => assert.deepEqual(alive, ['01', '03']));
  const sorted = await board.webContents.executeJavaScript(`document.querySelector('[data-v="all"]').click(); const sort=document.getElementById('sort'); sort.value='left'; sort.dispatchEvent(new Event('change')); [...document.querySelectorAll('.card .badge')].map(e=>e.textContent)`);
  check('남은 인원은 확정 표시 인원순, 미확인 팀은 마지막', () => assert.deepEqual(sorted, ['01', '03', '02', '04', '??']));
  const statuses = await board.webContents.executeJavaScript(`({bare:[...document.querySelectorAll('.slots .slot:not(.more-slots)')].filter(li=>!li.querySelector('.g')).length,estimated:document.querySelectorAll('.slots .g-estimated').length,text:document.querySelector('#cards').textContent})`);
  check('선수 상태는 기호로 표시하고 기절 차수와 생존 추정을 구분', () => {
    assert.equal(statuses.bare, 0); assert.ok(statuses.estimated > 0); assert.match(statuses.text, /기절 2차/);
  });
  const overlay = await open('overlay.html');
  await send(overlay, fixture);
  const compact = await overlay.webContents.executeJavaScript(`({knocked:document.querySelectorAll('.pips .g-knocked').length,estimated:document.querySelectorAll('.pips .g-estimated').length,words:document.querySelectorAll('.pips .state').length})`);
  check('오버레이 상태는 선수마다 기호 하나', () => {
    assert.equal(compact.knocked, 1); assert.ok(compact.estimated > 0); assert.equal(compact.words, 0);
  });
  const fits = await overlay.webContents.executeJavaScript(`[...document.querySelectorAll('.row')].every(row => [...row.querySelectorAll('.pip,.maybe-tag')].every(s=>s.getBoundingClientRect().bottom<=row.getBoundingClientRect().bottom))`);
  check('상태 기호가 다음 팀과 겹치지 않음', () => assert.ok(fits));
  const fade = await board.webContents.executeJavaScript(`(async () => {
    KF.sync({now:performance.now()});
    const el=document.createElement('div'); el.className='hot-able'; document.body.append(el);
    await new Promise(r=>requestAnimationFrame(r));
    const at=KF.now()-(KF.HOLD-900); KF.heat(el,at);
    const values=[]; let rebuilt=0;
    for(let i=0;i<25;i++) {
      await new Promise(r=>setTimeout(r,40));
      values.push(Number(getComputedStyle(el,'::before').opacity));
      if(i===5) KF.heat(el,at);
      if(i===10) {const copy=document.createElement('div');copy.className='hot-able';document.body.append(copy);KF.heat(copy,at);rebuilt=Math.abs(Number(getComputedStyle(copy,'::before').opacity)-Number(getComputedStyle(el,'::before').opacity));}
    }
    return {values,rebuilt,active:document.getAnimations().filter(a=>a.playState==='running' && a.effect.target.classList.contains('hot-able')).map(a=>({target:a.effect.target.className,current:a.currentTime,start:a.startTime,timing:a.effect.getTiming()}))};
  })()`);
  check('fade는 연속적으로 감소하고 같은 이벤트로 재시작하지 않음', () => {
    assert.ok(new Set(fade.values.map(v=>v.toFixed(3))).size >= 12, JSON.stringify(fade.values));
    assert.ok(fade.values.every((v,i)=>!i || v <= fade.values[i-1]+0.03));
    assert.ok(fade.rebuilt < 0.08); assert.equal(fade.values.at(-1), 0); assert.equal(fade.active.length, 0);
  });
  console.log(JSON.stringify({ fade }));
  const settings = await open('settings.html', 'settings:ready');
  settings.setSize(560, 800);
  const settingsBase = { ui: { closeToTray: true, openAtLogin: false, startHidden: false },
    overlay: { wanted: true, hotkey: true }, engine: null, version: '0.5.0', packaged: true };
  for (const [status, label, disabled] of [
    ['idle', '업데이트 확인', false], ['checking', '확인 중…', true],
    ['available', '새 버전 다운로드', false], ['downloading', '다운로드 중…', true],
    ['ready', '업데이트하고 재시작', false], ['applying', '업데이트 준비 중…', true],
    ['error', '업데이트 다시 확인', false], ['disabled', '설치판에서 사용', true],
  ]) {
    settings.webContents.send('settings:state', { ...settingsBase,
      update: { status, version: '0.6.0', percent: 37, message: `<${status}>` } });
    const controls = await settings.webContents.executeJavaScript(`new Promise((resolve,reject)=>{
      const end=performance.now()+2000;
      const poll=()=>{
        const b=document.getElementById('update-action'), s=document.getElementById('update-status');
        if(s?.textContent===${JSON.stringify(`<${status}>`)}) resolve({label:b?.textContent,disabled:b?.disabled,
          progress:document.getElementById('update-progress')?.value, images:s.querySelectorAll('img').length});
        else if(performance.now()>end) reject(new Error('update state was not rendered')); else setTimeout(poll,10);
      }; poll();
    })`);
    check(`설정 업데이트 상태 ${status}`, () => {
      assert.equal(controls.label, label); assert.equal(controls.disabled, disabled);
      assert.equal(controls.images, 0);
      if (status === 'downloading') assert.equal(controls.progress, 37);
    });
  }
  for (const [status, channel] of [['idle', 'update:check'], ['available', 'update:download'], ['ready', 'update:install']]) {
    settings.webContents.send('settings:state', { ...settingsBase,
      update: { status, version: '0.6.0', percent: 100, message: channel } });
    await settings.webContents.executeJavaScript(`new Promise(r=>{
      const poll=()=>document.getElementById('update-status').textContent===${JSON.stringify(channel)}?r():setTimeout(poll,10);poll();
    })`);
    const received = new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error(`${channel} IPC was not sent`)), 2000);
      ipcMain.once(channel, (event, payload) => { clearTimeout(timer); resolve({ own: event.sender === settings.webContents, payload }); });
    });
    await settings.webContents.executeJavaScript("document.getElementById('update-action').click()");
    const result = await received;
    check(`설정 ${channel} 허용 목록 IPC`, () => { assert.equal(result.own, true); assert.equal(result.payload, undefined); });
  }
  if (process.env.KFL_TEST_SHOTS) {
    settings.webContents.send('settings:state', { ...settingsBase,
      update: { status: 'ready', version: '0.6.0', percent: 100, message: '다운로드와 검증을 마쳤습니다. 적용하면 앱이 다시 시작됩니다.' } });
    await settings.webContents.executeJavaScript("document.getElementById('h-about').scrollIntoView({block:'start'})");
    if (process.env.KFL_TEST_SNAPSHOT) {
      const recorded = JSON.parse(fs.readFileSync(process.env.KFL_TEST_SNAPSHOT, 'utf8'));
      await send(board, recorded); await send(overlay, recorded);
      await board.webContents.executeJavaScript('new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))');
    }
    fs.mkdirSync(process.env.KFL_TEST_SHOTS, { recursive: true });
    for (const [name, win] of [['board', board], ['overlay', overlay], ['settings', settings]]) {
      const rect = name === 'overlay' ? await win.webContents.executeJavaScript('({x:0,y:0,width:272,height:Math.ceil(document.body.getBoundingClientRect().height)})') : undefined;
      const image = await win.webContents.capturePage(rect);
      fs.writeFileSync(path.join(process.env.KFL_TEST_SHOTS, `${name}.png`), image.toPNG());
    }
    fs.writeFileSync(path.join(process.env.KFL_TEST_SHOTS, 'renderer-results.json'), JSON.stringify({ failures, fade }, null, 2));
  }
  board.destroy(); overlay.destroy(); settings.destroy(); app.exit(failures.length ? 1 : 0);
}).catch(e => { console.error(e); app.exit(1); });
