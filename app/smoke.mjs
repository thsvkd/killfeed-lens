// Smoke check: start the app on the demo engine with --shots, then require a PNG from every window,
// the board's X leaving the app in the tray (closed-to-tray.ok) and no warning or error from any
// renderer.
// Usage: npm run smoke (the dev app, needs uv), or node smoke.mjs <packaged KillfeedLens.exe>
// (scripts/smoke_packaged.py). The app must not be running already: its single-instance lock
// would end this run at once.
import { spawnSync } from 'node:child_process';
import { existsSync, mkdtempSync, statSync, readdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';

const packaged = process.argv[2];
if (packaged) {
  const { extractFile, listPackage } = await import('@electron/asar');
  const base = dirname(packaged), resources = join(base, 'resources');
  const archive = join(resources, 'app.asar');
  const files = listPackage(archive).map((file) => file.replaceAll('\\', '/').replace(/^\/+/, ''));
  for (const file of ['updater.js', 'install-update.ps1', 'node_modules/pretendard/dist/LICENSE.txt']) {
    if (!files.includes(file)) throw new Error(`Missing packaged runtime file: ${file}`);
  }
  if (files.some((file) => file.endsWith('.test.cjs') || file.startsWith('.omc/'))) throw new Error('Test files in app.asar');
  const internal = join(resources, 'killfeed-lens', '_internal');
  const opencv = readdirSync(internal).find((file) => file.startsWith('opencv_python-') && file.endsWith('.dist-info'));
  if (!opencv) throw new Error('OpenCV license metadata not packaged');
  for (const file of [join(resources, 'UpdateLauncher.exe'), join(base, 'LICENSE'), join(base, 'LICENSES.chromium.html'),
    join(internal, 'onnxruntime', 'LICENSE'), join(internal, 'onnxruntime', 'ThirdPartyNotices.txt'),
    join(internal, opencv, 'LICENSE.txt'), join(internal, opencv, 'LICENSE-3RD-PARTY.txt'),
    join(internal, 'third-party-licenses', 'RapidOCR-LICENSE.txt'),
    join(internal, 'third-party-licenses', 'PaddleOCR-LICENSE.txt')]) {
    if (!existsSync(file) || !statSync(file).isFile() || statSync(file).size === 0) throw new Error(`Missing packaged file: ${file}`);
  }
  const metadata = JSON.parse(extractFile(archive, 'package.json'));
  console.log('package contents ok', { version: metadata.version });
  if (process.argv.includes('--check-only')) process.exit(0);
}
const [exe, args] = packaged ? [packaged, []] : [(await import('electron')).default, ['.']];
const dir = mkdtempSync(join(tmpdir(), 'kfl-smoke-'));
const run = spawnSync(exe, [...args, `--user-data-dir=${join(dir, 'user-data')}`, '--demo', '--shots', dir],
                      { cwd: import.meta.dirname, encoding: 'utf8', timeout: 300_000 });
const logged = `${run.stdout ?? ''}${run.stderr ?? ''}`.split(/\r?\n/)
  .filter((l) => /^\[(board|overlay|roi|settings)\]/.test(l));
const missing = ['board.png', 'overlay.png', 'roi.png', 'settings.png', 'closed-to-tray.ok'].filter((f) => {
  const p = join(dir, f);
  return !existsSync(p) || statSync(p).size < (f.endsWith('.png') ? 2000 : 2);
});
const failed = run.status !== 0 || missing.length || logged.length;
console.log(failed ? 'smoke FAILED' : 'smoke ok', { exit: run.status, missing, logged, shots: dir });
process.exit(failed ? 1 : 0);
