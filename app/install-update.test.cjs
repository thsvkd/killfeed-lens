'use strict';
// The real PowerShell helper runs only fake executables in an isolated temporary profile.
const { test, before, after } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const { execFile, spawn } = require('node:child_process');
const { promisify } = require('node:util');
const { createHash } = require('node:crypto');
const { Updater } = require('./updater');
const run = promisify(execFile);
const windows = process.platform === 'win32';
let root, env, stub, launcher, powershell, serial = 0;

before(async () => {
  if (!windows) return;
  // Legacy CodeDOM compilation can narrow TEMP paths on English Windows. Compile in ASCII;
  // every runtime job below still uses Unicode and spaces, as real installed profiles do.
  root = await fs.mkdtemp(path.join(os.tmpdir(), 'kfl-helper-test-'));
  env = { ...process.env, HOME: path.join(root, 'profile'), USERPROFILE: path.join(root, 'profile'),
    APPDATA: path.join(root, 'roaming'), LOCALAPPDATA: path.join(root, 'local'),
    TEMP: path.join(root, 'tmp'), TMP: path.join(root, 'tmp'),
    PSModuleAnalysisCachePath: path.join(root, 'tmp', 'module-analysis-cache') };
  for (const key of ['HOME', 'APPDATA', 'LOCALAPPDATA', 'TEMP']) await fs.mkdir(env[key], { recursive: true });
  powershell = path.join(process.env.SystemRoot, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe');
  stub = path.join(root, 'fixture.exe');
  launcher = path.join(root, 'launcher.exe');
  const compile = path.join(root, 'compile.ps1');
  await fs.writeFile(compile, `param([string]$StubPath, [string]$LauncherSource, [string]$LauncherOutput)
$ErrorActionPreference = 'Stop'
Add-Type -OutputAssembly $StubPath -OutputType WindowsApplication -TypeDefinition @'
using System;
using System.IO;
public class Fixture {
  public static int Main(string[] args) {
    string dir = AppDomain.CurrentDomain.BaseDirectory;
    if (Path.GetFileName(Environment.GetCommandLineArgs()[0]).StartsWith("killfeed-lens-")) {
      File.WriteAllLines(Path.Combine(dir, "installer-args.txt"), args);
      string exitFile = Path.Combine(dir, "exit-code.txt");
      return File.Exists(exitFile) ? int.Parse(File.ReadAllText(exitFile)) : 0;
    }
    File.AppendAllText(Path.Combine(dir, "app-started.txt"), "started\\n");
    return 0;
  }
}
'@
Add-Type -OutputAssembly $LauncherOutput -OutputType WindowsApplication -TypeDefinition ([IO.File]::ReadAllText($LauncherSource))
`);
  await run(powershell, ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', compile, '-StubPath', stub,
    '-LauncherSource', path.join(__dirname, '..', 'packaging', 'update-launcher.cs'), '-LauncherOutput', launcher],
    { env, windowsHide: true, timeout: 30_000 });
});
after(async () => { if (root) await fs.rm(root, { recursive: true, force: true }); });

async function job(t) {
  const dir = path.join(root, `case-한글 space-${serial++}`);
  const cache = path.join(dir, 'updates', 'download-fixture');
  const appExe = path.join(dir, 'Programs 앱', 'KillfeedLens.exe');
  await fs.mkdir(cache, { recursive: true }); await fs.mkdir(path.dirname(appExe), { recursive: true });
  const installer = path.join(cache, 'killfeed-lens-1.2.3-setup.exe');
  await fs.copyFile(stub, installer); await fs.copyFile(stub, appExe);
  const main = spawn(process.execPath, ['-e', 'setTimeout(()=>{},100)'], { env, windowsHide: true, stdio: 'ignore' });
  t.after(() => main.kill());
  const config = { version: '1.2.3', sha256: createHash('sha256').update(await fs.readFile(installer)).digest('hex'),
    appExe, appPid: main.pid, enginePid: 0 };
  const configFile = path.join(cache, 'job.json');
  const start = async () => {
    await fs.writeFile(configFile, JSON.stringify(config));
    let exit = 0;
    try {
      await run(powershell, ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File',
        path.join(__dirname, 'install-update.ps1'), '-ConfigFile', configFile],
      { env, windowsHide: true, timeout: 45_000 });
    } catch (error) { exit = error.code; }
    const result = JSON.parse((await fs.readFile(path.join(dir, 'updates', 'result.json'), 'utf8')).replace(/^\uFEFF/, ''));
    return { exit, result };
  };
  return { cache, appExe, config, start };
}

test('helper waits, installs in the current Unicode path, never reboots, and restarts once', { skip: !windows }, async (t) => {
  const j = await job(t);
  const { exit, result } = await j.start();
  assert.equal(exit, 0, JSON.stringify(result)); assert.equal(result.status, 'installed');
  const args = (await fs.readFile(path.join(j.cache, 'installer-args.txt'), 'utf8')).trim().split(/\r?\n/);
  for (const flag of ['/VERYSILENT', '/NORESTART', '/NOCLOSEAPPLICATIONS', '/NOFORCECLOSEAPPLICATIONS', '/NORESTARTAPPLICATIONS']) {
    assert.ok(args.includes(flag), `missing ${flag}`);
  }
  assert.ok(args.includes(`/DIR=${path.dirname(j.appExe)}`));
  assert.ok(args.includes(`/LOG=${path.join(j.cache, 'install.log')}`));
  const started = path.join(path.dirname(j.appExe), 'app-started.txt');
  for (let i = 0; i < 40; i++) {
    try { assert.equal(await fs.readFile(started, 'utf8'), 'started\n'); return; }
    catch { await new Promise((r) => setTimeout(r, 50)); }
  }
  assert.fail('updated app was not started exactly once');
});

test('helper rejects changed bytes before executing the installer', { skip: !windows }, async (t) => {
  const j = await job(t); j.config.sha256 = '0'.repeat(64);
  const { exit, result } = await j.start();
  assert.notEqual(exit, 0); assert.equal(result.status, 'error');
  assert.equal(result.code, 'HASH_MISMATCH', JSON.stringify(result));
  await assert.rejects(fs.stat(path.join(j.cache, 'installer-args.txt')), { code: 'ENOENT' });
});

test('failed installer records an error and does not restart the app', { skip: !windows }, async (t) => {
  const j = await job(t); await fs.writeFile(path.join(j.cache, 'exit-code.txt'), '7');
  const { exit, result } = await j.start();
  assert.notEqual(exit, 0); assert.equal(result.status, 'error');
  assert.equal(result.code, 'INSTALL_FAILED', JSON.stringify(result));
  await assert.rejects(fs.stat(path.join(path.dirname(j.appExe), 'app-started.txt')), { code: 'ENOENT' });
});

test('a still-running engine prevents installation and produces a timeout result', { skip: !windows, timeout: 45_000 }, async (t) => {
  const j = await job(t);
  const engine = spawn(process.execPath, ['-e', 'setTimeout(()=>{},60000)'], { env, windowsHide: true, stdio: 'ignore' });
  t.after(() => engine.kill()); j.config.enginePid = engine.pid;
  const began = Date.now();
  const { exit, result } = await j.start();
  assert.notEqual(exit, 0); assert.equal(result.status, 'error');
  assert.equal(result.code, 'WAIT_TIMEOUT', JSON.stringify(result));
  assert.ok(Date.now() - began >= 29_000, 'helper did not wait for the engine');
  await assert.rejects(fs.stat(path.join(j.cache, 'installer-args.txt')), { code: 'ENOENT' });
});

async function controllerJob(t) {
  const j = await job(t);
  const bytes = await fs.readFile(stub);
  const release = { tag_name: 'v1.2.3', draft: false, prerelease: false,
    assets: [{ name: 'killfeed-lens-1.2.3-setup.exe', state: 'uploaded', size: bytes.length,
      digest: `sha256:${j.config.sha256}`,
      browser_download_url: 'https://github.com/thsvkd/killfeed-lens/releases/download/v1.2.3/killfeed-lens-1.2.3-setup.exe' }] };
  const updater = new Updater({ version: '1.2.2', directory: path.dirname(j.cache), launcherPath: launcher,
    fetchImpl: async (url) => url.includes('/releases/latest') ? Response.json(release) : new Response(bytes) });
  await updater.check(); await updater.download();
  const parent = spawn(process.execPath, ['-e', 'setTimeout(()=>{},60000)'], { env, windowsHide: true, stdio: 'ignore' });
  t.after(() => parent.kill());
  return { ...j, updater, parent };
}

async function installIsolated(j) {
  assert.ok(j.appExe.startsWith(root + path.sep));
  assert.ok(j.updater.file.startsWith(root + path.sep));
  assert.notEqual(j.parent.pid, process.pid);
  const keys = ['HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'TEMP', 'TMP', 'PSModuleAnalysisCachePath'];
  const saved = keys.map((key) => process.env[key]);
  for (const key of keys) process.env[key] = env[key];
  try { return await j.updater.install({ appExe: j.appExe, appPid: j.parent.pid, enginePid: 0 }); }
  finally { keys.forEach((key, i) => { if (saved[i] === undefined) delete process.env[key]; else process.env[key] = saved[i]; }); }
}

test('controller hands a verified job to the real helper and installs only after its parent exits', { skip: !windows }, async (t) => {
  const j = await controllerJob(t);
  const began = await installIsolated(j);
  assert.equal(began, true, j.updater.state.message + '\n' + await fs.readFile(path.join(path.dirname(j.updater.file), 'helper.log'), 'utf8')
    + '\n' + await fs.readFile(path.join(path.dirname(j.updater.file), 'helper-events.log'), 'utf8').catch(() => 'no events'));
  assert.equal(j.updater.state.status, 'applying');
  await assert.rejects(fs.stat(path.join(path.dirname(j.updater.file), 'installer-args.txt')), { code: 'ENOENT' });
  j.parent.kill();
  const resultFile = path.join(j.updater.directory, 'result.json');
  let result;
  for (let i = 0; i < 100; i++) {
    try { result = JSON.parse(await fs.readFile(resultFile, 'utf8')); break; } catch {}
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  assert.equal(result?.status, 'installed', JSON.stringify(result) || 'helper produced no result');
  const relaunched = new Updater({ version: '1.2.3', directory: j.updater.directory });
  await relaunched.readResult();
  assert.equal(relaunched.state.status, 'current');
  // The detached helper can be finishing its final Start-Process after writing the result.
  const started = path.join(path.dirname(j.appExe), 'app-started.txt');
  for (let i = 0; i < 100; i++) {
    try { assert.equal(await fs.readFile(started, 'utf8'), 'started\n'); return; } catch {}
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  assert.fail('the installed fixture did not restart');
});

test('helper preparation failure leaves its parent running', { skip: !windows }, async (t) => {
  const j = await controllerJob(t);
  await fs.unlink(j.appExe);
  assert.equal(await installIsolated(j), false);
  assert.equal(j.updater.state.status, 'error');
  assert.equal(j.parent.exitCode, null);
  const result = JSON.parse(await fs.readFile(path.join(j.updater.directory, 'result.json'), 'utf8'));
  assert.equal(result.code, 'INVALID_JOB');
});

test('the installation helper survives the actual spawning application exit', { skip: !windows }, async (t) => {
  const j = await job(t);
  const runner = path.join(path.dirname(j.cache), 'spawn-app.cjs');
  await fs.writeFile(runner, `
const fs = require('node:fs/promises');
const { createHash } = require('node:crypto');
const { Updater } = require(${JSON.stringify(path.join(__dirname, 'updater.js'))});
(async()=>{
  const bytes = await fs.readFile(${JSON.stringify(stub)});
  const release = {tag_name:'v1.2.3',draft:false,prerelease:false,assets:[{
    name:'killfeed-lens-1.2.3-setup.exe',state:'uploaded',size:bytes.length,
    digest:'sha256:'+createHash('sha256').update(bytes).digest('hex'),
    browser_download_url:'https://github.com/thsvkd/killfeed-lens/releases/download/v1.2.3/killfeed-lens-1.2.3-setup.exe'}]};
  const u = new Updater({version:'1.2.2',directory:${JSON.stringify(path.dirname(j.cache))},launcherPath:${JSON.stringify(launcher)},
    fetchImpl:async url=>url.includes('/releases/latest')?Response.json(release):new Response(bytes)});
  await u.check(); await u.download();
  if(!await u.install({appExe:${JSON.stringify(j.appExe)},appPid:process.pid,enginePid:0})) throw Error(u.state.message);
  process.stdout.write('prepared'); process.exit(0);
})().catch(e=>{console.error(e);process.exit(1)});
`);
  const child = await run(process.execPath, [runner], { env, windowsHide: true, timeout: 45_000 });
  assert.equal(child.stdout, 'prepared');
  let result;
  for (let i = 0; i < 100; i++) {
    try { result = JSON.parse(await fs.readFile(path.join(path.dirname(j.cache), 'result.json'), 'utf8')); break; } catch {}
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  assert.equal(result?.status, 'installed', JSON.stringify(result) || 'helper produced no result');
  const started = path.join(path.dirname(j.appExe), 'app-started.txt');
  for (let i = 0; i < 100; i++) {
    try { assert.equal(await fs.readFile(started, 'utf8'), 'started\n'); return; } catch {}
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  assert.fail('the new app did not start after the spawning app exited');
});
