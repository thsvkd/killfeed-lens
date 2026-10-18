// Smoke check: start the app on the demo engine with --shots, then require a PNG from every window
// and no warning or error from any renderer.
// Usage: npm run smoke (the dev app, needs uv), or node smoke.mjs <packaged KillfeedLens.exe>
// (scripts/smoke_packaged.py). The app must not be running already: its single-instance lock
// would end this run at once.
import { spawnSync } from 'node:child_process';
import { existsSync, mkdtempSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const packaged = process.argv[2];
const [exe, args] = packaged ? [packaged, []] : [(await import('electron')).default, ['.']];
const dir = mkdtempSync(join(tmpdir(), 'kfl-smoke-'));
const run = spawnSync(exe, [...args, '--demo', '--shots', dir],
                      { cwd: import.meta.dirname, encoding: 'utf8', timeout: 300_000 });
const logged = `${run.stdout ?? ''}${run.stderr ?? ''}`.split(/\r?\n/).filter((l) => /^\[(board|overlay|roi)\]/.test(l));
const missing = ['board.png', 'overlay.png', 'roi.png'].filter((f) => {
  const p = join(dir, f);
  return !existsSync(p) || statSync(p).size < 2000;
});
const failed = run.status !== 0 || missing.length || logged.length;
console.log(failed ? 'smoke FAILED' : 'smoke ok', { exit: run.status, missing, logged, shots: dir });
process.exit(failed ? 1 : 0);
