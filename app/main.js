'use strict';
// killfeed-lens UI, main process. Spawns the board engine (`kfocr engine`, see CONTRACT.md), fans
// its snapshots out to the board window and the overlay, and owns the overlay's placement,
// visibility, click-through and capture exclusion, plus the ROI editor window.
const { app, BrowserWindow, globalShortcut, ipcMain, nativeTheme, screen } = require('electron');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const readline = require('node:readline');

const argv = process.argv.slice(1);
const flag = (name) => argv.includes(name);
const option = (name) => { const i = argv.indexOf(name); return i >= 0 ? argv[i + 1] : undefined; };

// Software compositing: the game owns the GPU. The board changes a few times a second at most, so
// small CPU rasters are enough; a D3D device of our own would compete with the game for the GPU
// queue and VRAM (measured: no GPU driver loaded at all). `--gpu` turns it back on for A/B checks.
if (!flag('--gpu')) app.disableHardwareAcceleration();
// Chromium would otherwise grab the media keys from the player's music app.
app.commandLine.appendSwitch('disable-features', 'HardwareMediaKeyHandling,MediaSessionService');

const DEMO = flag('--demo'); // engine plays the scripted match instead of capturing
const SHOTS = option('--shots'); // directory: save screenshots of every window, then quit (smoke)
const REPO = path.join(__dirname, '..');
const HOTKEY = 'Control+Alt+O';
const OVERLAY_W = 272;
const GAP = 8;
// The engine's LiveFeed.stop (src/kfocr/ui/engine.py) waits up to 15 s for capture to close its
// session; give it a margin so that wait ends before the UI kills the process tree.
const ENGINE_STOP_MS = 20000;
// gui.ROI_DEFAULT and LiveCapture's alive-count default
const DEFAULT_ROIS = { killfeed: [0.68, 0, 0.31, 0.2], alive: [0.92, 0.01, 0.04, 0.04] };

let board = null;
let overlay = null;
let editor = null;
let engine = null;
let last = null; // latest snapshot
let frozen = false; // --shots: ignore new snapshots while capturing
let guided = false; // the first-run ROI guide opens once per launch
const ov = { wanted: true, editing: false, hotkey: false, pos: null, w: OVERLAY_W, h: 200 };

const webPreferences = {
  preload: path.join(__dirname, 'preload.js'),
  sandbox: true,
  contextIsolation: true,
  spellcheck: false,
};

// --- engine ----------------------------------------------------------------------------------
/** Installed: the console kfocr.exe of the PyInstaller onedir in resources/. From the repo:
 *  `uv run kfocr engine`, or KFL_ENGINE (a whole shell command; never read when packaged). */
function engineCommand() {
  const args = ['engine', ...(DEMO ? ['--demo'] : [])];
  if (app.isPackaged) {
    const engine = path.join(process.resourcesPath, 'killfeed-lens');
    return { cmd: path.join(engine, 'kfocr.exe'), args, shell: false, cwd: engine };
  }
  const custom = process.env.KFL_ENGINE;
  if (custom) return { cmd: custom, args: [], shell: true, cwd: REPO };
  return { cmd: 'uv', args: ['run', 'kfocr', ...args], shell: false, cwd: REPO };
}

function startEngine() {
  const { cmd, args, shell, cwd } = engineCommand();
  const child = spawn(cmd, args, { shell, cwd, windowsHide: true, stdio: ['pipe', 'pipe', 'inherit'] });
  engine = child;
  child.stdin.on('error', () => {}); // engine gone: its 'exit' reports it
  child.on('error', (err) => {
    engine = null;
    sendBoard('engine', { alive: false, reason: `엔진을 시작하지 못했습니다: ${err.message}` });
  });
  child.on('exit', (code) => {
    if (engine === child) engine = null;
    sendBoard('engine', { alive: false, reason: `엔진 종료됨 (코드 ${code})` });
  });
  readline.createInterface({ input: child.stdout }).on('line', onLine);
  sendBoard('engine', { alive: true });
}

function stopEngine() {
  return new Promise((resolve) => {
    if (!engine) return resolve();
    const child = engine;
    // stdin EOF is the polite stop; a stuck engine gets its whole tree killed
    const timer = setTimeout(() => {
      spawn('taskkill', ['/pid', String(child.pid), '/T', '/F'], { windowsHide: true }).on('exit', resolve);
    }, ENGINE_STOP_MS);
    child.once('exit', () => { clearTimeout(timer); resolve(); });
    child.stdin.end();
  });
}

function sendEngine(cmd) {
  if (engine) engine.stdin.write(`${JSON.stringify(cmd)}\n`);
}

function onLine(line) {
  if (frozen) return;
  let snap;
  try { snap = JSON.parse(line); } catch { return; } // not a snapshot (engine log noise)
  if (!snap || snap.v !== 1) return;
  const roisChanged = JSON.stringify(snap.rois) !== JSON.stringify(last?.rois);
  last = snap;
  sendBoard('snapshot', snap);
  if (overlay?.isVisible()) overlay.webContents.send('snapshot', snap);
  if (roisChanged && overlay) place(); // re-calibrated: move the overlay off the new ROIs
  syncOverlay();
  if (!guided && snap.mode === 'live' && !snap.error && !snap.rois.killfeed && !SHOTS) {
    guided = true; // first run: no killfeed ROI yet, like LiveCapture.begin
    openEditor(true);
  }
}

function sendBoard(channel, data) {
  if (board && !board.isDestroyed()) board.webContents.send(channel, data);
}

// --- overlay placement -----------------------------------------------------------------------
const apart = ([x, y, w, h], [rx, ry, rw, rh]) => x >= rx + rw || x + w <= rx || y >= ry + rh || y + h <= ry;

/** Port of gui.clear_of_rois: nearest spot that covers no ROI and stays on screen. */
function clearOfRois(box, rois, [sw, sh]) {
  const [x, y, w, h] = box;
  if (rois.every((r) => apart(box, r))) return [x, y];
  const xs = new Set([x]);
  const ys = new Set([y]);
  for (const [rx, ry, rw, rh] of rois) {
    xs.add(rx - w - GAP); xs.add(rx + rw + GAP);
    ys.add(ry - h - GAP); ys.add(ry + rh + GAP);
  }
  let best = [x, y];
  let cost = Infinity;
  for (const a of xs) for (const b of ys) {
    if (a < 0 || b < 0 || a > sw - w || b > sh - h) continue;
    if (!rois.every((r) => apart([a, b, w, h], r))) continue;
    const c = Math.abs(a - x) + Math.abs(b - y);
    if (c < cost) { cost = c; best = [a, b]; }
  }
  return best; // ponytail: no fitting spot keeps the overlap (capture is excluded anyway)
}

function screenRois() {
  // ROIs are normalized to the captured monitor; like the Tk app, assume it is the primary one.
  const { width, height } = screen.getPrimaryDisplay().bounds;
  const rois = Object.values(last?.rois ?? {}).filter(Boolean);
  return rois.map(([x, y, w, h]) => [x * width, y * height, w * width, h * height]);
}

function place() {
  const { width, height } = screen.getPrimaryDisplay().bounds;
  ov.pos ??= [width - OVERLAY_W - 16, 80];
  if (!ov.editing) ov.pos = clearOfRois([...ov.pos, ov.w, ov.h], screenRois(), [width, height]);
  overlay.setBounds({ x: Math.round(ov.pos[0]), y: Math.round(ov.pos[1]), width: ov.w,
                      height: Math.round(ov.h) });
  sendState();
}

/** Same rule as gui.overlay_has_content: before the first event or HUD reading it would only say
 *  "waiting", and any visible topmost window over a borderless game makes DWM compose the game's
 *  frames instead of letting them flip straight to the screen. */
function hasContent() {
  return Boolean(last && (last.events.length || last.teams.length || last.hud));
}

function syncOverlay() {
  if (!overlay) return;
  const show = ov.editing || (ov.wanted && hasContent() && !editor);
  if (show && !overlay.isVisible()) {
    if (last) overlay.webContents.send('snapshot', last);
    overlay.showInactive(); // never take focus from the game
    overlay.setAlwaysOnTop(true, 'screen-saver');
    overlay.setContentProtection(true); // affinity needs the shown window, like gui.hide_from_capture
  } else if (!show && overlay.isVisible()) {
    overlay.hide(); // hidden: Chromium stops painting it and throttles its timers
  }
  sendState();
}

function sendState() {
  const { width, height } = screen.getPrimaryDisplay().bounds;
  const b = overlay?.getBounds();
  sendBoard('overlay:state', {
    wanted: ov.wanted, visible: Boolean(overlay?.isVisible()), editing: ov.editing,
    hotkey: ov.hotkey, roiEditing: Boolean(editor),
    rect: b ? [b.x / width, b.y / height, b.width / width, b.height / height] : null,
    screen: [width, height],
  });
}

function setEditing(on) {
  if (!overlay) return; // the board closed and took the overlay with it
  ov.editing = on;
  overlay.setIgnoreMouseEvents(!on);
  overlay.webContents.send('overlay:editing', on);
  if (!on) place();
  syncOverlay();
}

// --- ROI editor: a transparent window over the whole game monitor --------------------------------
let editorDone = false;

function openEditor(guide = false) {
  if (editor) return editor.focus();
  const display = screen.getPrimaryDisplay();
  editorDone = false;
  editor = new BrowserWindow({
    ...display.bounds, title: 'killfeed-lens ROI', show: false, frame: false, transparent: true,
    resizable: false, movable: false, minimizable: false, maximizable: false, fullscreenable: false,
    skipTaskbar: true, hasShadow: false, alwaysOnTop: true, webPreferences,
  });
  editor.setAlwaysOnTop(true, 'screen-saver');
  logConsole(editor, 'roi');
  editor.loadFile('roi.html');
  editor.webContents.once('did-finish-load', () => {
    const rois = last?.rois ?? {};
    editor.webContents.send('roi:init', {
      killfeed: rois.killfeed ?? DEFAULT_ROIS.killfeed, alive: rois.alive ?? DEFAULT_ROIS.alive,
      // like LiveCapture: the alive box joins on first setup or when it already exists
      useAlive: Boolean(rois.alive) || !rois.killfeed,
      guide, demo: last?.mode === 'demo', monitor: last?.monitor ?? 0, scale: display.scaleFactor,
      size: [display.bounds.width, display.bounds.height],
    });
  });
  editor.once('ready-to-show', () => {
    editor.setBounds(display.bounds); // over the taskbar too: the ROI may sit anywhere on the monitor
    if (!SHOTS) { editor.show(); editor.focus(); }
  });
  editor.on('closed', () => {
    if (!editorDone) sendEngine({ cmd: 'cancel' }); // closed without Enter/Esc
    editor = null;
    syncOverlay();
  });
  sendEngine({ cmd: 'calibrate' }); // the engine pauses capture while the boxes sit on the killfeed
  syncOverlay();
}

/** Tell the engine how editing ended (`roi` or `cancel`), then close the editor. */
function closeEditor(cmd) {
  if (!editor) return;
  editorDone = true;
  sendEngine(cmd);
  editor.close();
}

// --- windows ---------------------------------------------------------------------------------
function logConsole(win, tag) {
  win.webContents.on('console-message', (e, level, message) => {
    const lvl = e.level ?? level;
    if (lvl === 'warning' || lvl === 'error' || lvl >= 2) console.error(`[${tag}] ${e.message ?? message}`);
  });
}

function createOverlay() {
  overlay = new BrowserWindow({
    title: 'killfeed-lens overlay',
    width: OVERLAY_W, height: ov.h, show: false,
    transparent: true, frame: false, resizable: false, maximizable: false, minimizable: false,
    fullscreenable: false, hasShadow: false, skipTaskbar: true,
    focusable: false, // WS_EX_NOACTIVATE: clicks and the hotkey never steal the game's focus
    alwaysOnTop: true,
    webPreferences,
  });
  overlay.setAlwaysOnTop(true, 'screen-saver');
  // Click-through without `forward`: forwarding installs a system-wide low-level mouse hook, and
  // every mouse event of the game would then pass through this process.
  overlay.setIgnoreMouseEvents(true);
  // WDA_EXCLUDEFROMCAPTURE: the killfeed OCR (DXGI duplication) never sees the overlay's text
  overlay.setContentProtection(true);
  logConsole(overlay, 'overlay');
  overlay.loadFile('overlay.html');
  overlay.on('moved', () => { const b = overlay.getBounds(); ov.pos = [b.x, b.y]; sendState(); });
  place();
}

function createBoard() {
  board = new BrowserWindow({
    title: 'killfeed-lens · 팀 현황',
    width: 1080, height: 800, minWidth: 720, minHeight: 520, x: 80, y: 40,
    backgroundColor: '#0A0D12', show: false, autoHideMenuBar: true,
    webPreferences,
  });
  board.setMenu(null);
  logConsole(board, 'board');
  board.loadFile('index.html');
  board.once('ready-to-show', () => { if (!SHOTS) board.show(); });
  board.on('closed', () => {
    board = null;
    editor?.destroy();
    overlay?.destroy();
    overlay = null;
  });
}

// --- IPC -------------------------------------------------------------------------------------
ipcMain.on('ui:ready', (e) => {
  if (last) e.sender.send('snapshot', last);
  sendState();
  sendBoard('engine', { alive: Boolean(engine), reason: engine ? '' : '엔진 연결 안 됨' });
});
ipcMain.on('overlay:toggle', () => { ov.wanted = !ov.wanted; syncOverlay(); });
ipcMain.on('overlay:edit', (_e, on) => setEditing(Boolean(on)));
ipcMain.on('overlay:size', (_e, { w, h }) => {
  if (!overlay) return; // a late message after the board closed
  if (Math.round(h) === Math.round(ov.h) && w === ov.w) return;
  ov.h = h; ov.w = w;
  place();
});
ipcMain.on('engine:cmd', (_e, cmd) => {
  if (cmd?.cmd === 'tracking') sendEngine({ cmd: 'tracking', on: Boolean(cmd.on) });
});
ipcMain.on('roi:open', () => openEditor(false));
ipcMain.on('roi:done', (_e, { killfeed, alive }) => {
  const { bounds, scaleFactor } = screen.getPrimaryDisplay();
  // the engine validates the rects; resolution is the monitor in physical pixels, as dxcam sees it
  closeEditor({ cmd: 'roi', killfeed, alive, screen: [Math.round(bounds.width * scaleFactor),
                                                       Math.round(bounds.height * scaleFactor)] });
});
ipcMain.on('roi:cancel', () => closeEditor({ cmd: 'cancel' }));

// --- app -------------------------------------------------------------------------------------
if (!app.requestSingleInstanceLock()) app.exit(0); // a second engine would fight over the camera
app.on('second-instance', () => { board?.show(); board?.focus(); });

app.whenReady().then(() => {
  nativeTheme.themeSource = 'dark'; // dark native title bar
  createBoard();
  createOverlay();
  ov.hotkey = globalShortcut.register(HOTKEY, () => { ov.wanted = !ov.wanted; syncOverlay(); });
  // Games and other topmost windows can push the overlay down; re-assert while it is shown.
  setInterval(() => { if (overlay?.isVisible()) overlay.moveTop(); }, 2000);
  startEngine();
  if (SHOTS) shoot(SHOTS);
});

app.on('will-quit', () => globalShortcut.unregisterAll());
app.on('window-all-closed', async () => { await stopEngine(); app.quit(); });

// --- --shots <dir>: board, overlay and ROI editor as PNGs, then quit (npm run smoke) ------------
async function shoot(dir) {
  fs.mkdirSync(dir, { recursive: true });
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const save = async (win, name) => fs.writeFileSync(path.join(dir, name), (await win.webContents.capturePage()).toPNG());
  await wait(Number(process.env.KFL_SHOT_DELAY ?? 8000));
  frozen = true; // hold the board still while capturing
  board.showInactive();
  await wait(1200);
  await save(board, 'board.png');
  if (overlay.isVisible()) await save(overlay, 'overlay.png');
  openEditor(true);
  await wait(1500);
  editor.showInactive();
  await wait(600);
  await save(editor, 'roi.png');
  closeEditor({ cmd: 'cancel' });
  board.close();
}
