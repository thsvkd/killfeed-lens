'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { createHash } = require('node:crypto');
const { EventEmitter } = require('node:events');
const childProcess = require('node:child_process');
const { Updater } = require('./updater');

const content = Buffer.from('verified test installer bytes');
const digest = createHash('sha256').update(content).digest('hex');
const downloadURL = 'https://github.com/thsvkd/killfeed-lens/releases/download/v1.2.3/killfeed-lens-1.2.3-setup.exe';
function release() {
  return { tag_name: 'v1.2.3', draft: false, prerelease: false,
    assets: [{ name: 'killfeed-lens-1.2.3-setup.exe', state: 'uploaded', size: content.length,
      digest: `sha256:${digest}`, browser_download_url: downloadURL }] };
}
async function fixture(t, fetchImpl, version = '1.2.2') {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'kfl-updater-test-'));
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  return new Updater({ version, directory, fetchImpl });
}

test('a newer stable release becomes available without downloading it', async (t) => {
  let requests = 0;
  const updater = await fixture(t, async (url) => {
    requests++;
    assert.equal(url, 'https://api.github.com/repos/thsvkd/killfeed-lens/releases/latest');
    return Response.json(release());
  });
  await updater.check();
  assert.equal(updater.state.status, 'available');
  assert.equal(updater.state.version, '1.2.3');
  assert.equal(requests, 1);
});

for (const version of ['1.2.3', '1.2.4', '2.0.0']) {
  test(`release 1.2.3 is not offered to ${version}`, async (t) => {
    const updater = await fixture(t, async () => Response.json(release()), version);
    await updater.check();
    assert.equal(updater.state.status, 'current');
  });
}
test('version comparison treats 1.2.10 as newer than 1.2.3', async (t) => {
  const r = release();
  r.tag_name = 'v1.2.10';
  r.assets[0].name = 'killfeed-lens-1.2.10-setup.exe';
  r.assets[0].browser_download_url = downloadURL.replaceAll('1.2.3', '1.2.10');
  const updater = await fixture(t, async () => Response.json(r), '1.2.3');
  await updater.check();
  assert.equal(updater.state.version, '1.2.10');
  assert.equal(updater.state.status, 'available');
});

for (const [label, mutate] of [
  ['draft', (r) => { r.draft = true; }],
  ['prerelease', (r) => { r.prerelease = true; }],
  ['invalid version', (r) => { r.tag_name = 'v1.2.3-beta.1'; }],
  ['missing checksum', (r) => { r.assets[0].digest = null; }],
  ['wrong filename', (r) => { r.assets[0].name = '../setup.exe'; }],
  ['foreign download', (r) => { r.assets[0].browser_download_url = 'https://example.com/setup.exe'; }],
  ['wrong version URL', (r) => { r.assets[0].browser_download_url = downloadURL.replace('/v1.2.3/', '/v9.0.0/'); }],
  ['empty size', (r) => { r.assets[0].size = 0; }],
  ['excessive size', (r) => { r.assets[0].size = 2 ** 40; }],
]) {
  test(`${label} cannot become an installable update`, async (t) => {
    const r = release(); mutate(r);
    const updater = await fixture(t, async () => Response.json(r));
    await updater.check();
    assert.equal(updater.state.status, 'error');
    await updater.download();
    assert.notEqual(updater.state.status, 'ready');
  });
}

test('a verified streamed download becomes ready and is written intact', async (t) => {
  const updater = await fixture(t, async (url) => url.includes('/releases/latest')
    ? Response.json(release()) : new Response(content));
  await updater.check();
  await updater.download();
  assert.equal(updater.state.status, 'ready');
  assert.equal(updater.state.percent, 100);
  const [job] = await fs.readdir(updater.directory);
  assert.deepEqual(await fs.readFile(path.join(updater.directory, job, 'killfeed-lens-1.2.3-setup.exe')), content);
});

for (const [label, data] of [
  ['truncated', content.subarray(0, 6)],
  ['oversized', Buffer.concat([content, Buffer.from('extra')])],
  ['wrong checksum', Buffer.alloc(content.length, 65)],
]) {
  test(`${label} download is discarded and never becomes ready`, async (t) => {
    const updater = await fixture(t, async (url) => url.includes('/releases/latest')
      ? Response.json(release()) : new Response(data));
    await updater.check(); await updater.download();
    assert.equal(updater.state.status, 'error');
    assert.deepEqual(await fs.readdir(updater.directory), []);
  });
}

test('a foreign redirect is rejected without requesting it', async (t) => {
  const updater = await fixture(t, async (url) => {
    if (url.includes('/releases/latest')) return Response.json(release());
    assert.equal(url, downloadURL);
    return new Response(null, { status: 302, headers: { location: 'https://example.com/payload.exe' } });
  });
  await updater.check(); await updater.download();
  assert.equal(updater.state.status, 'error');
});

test('an interrupted download can be checked and downloaded again', async (t) => {
  let interrupted = true;
  const updater = await fixture(t, async (url) => {
    if (url.includes('/releases/latest')) return Response.json(release());
    if (interrupted) throw new Error('network disconnected');
    return new Response(content);
  });
  await updater.check(); await updater.download();
  assert.equal(updater.state.status, 'error');
  interrupted = false;
  await updater.check(); await updater.download();
  assert.equal(updater.state.status, 'ready');
});

test('duplicate check and download actions do not start concurrent work', async (t) => {
  let finishCheck, finishDownload;
  let requests = 0;
  const updater = await fixture(t, async (url) => {
    requests++;
    if (url.includes('/releases/latest')) return new Promise((resolve) => { finishCheck = resolve; });
    return new Promise((resolve) => { finishDownload = resolve; });
  });
  const checking = updater.check(); await updater.check();
  finishCheck(Response.json(release())); await checking;
  const downloading = updater.download();
  while (!finishDownload) await new Promise((resolve) => setTimeout(resolve, 1));
  await updater.download(); await updater.check();
  finishDownload(new Response(content)); await downloading;
  assert.equal(requests, 2);
  assert.equal(updater.state.status, 'ready');
});

test('HTTP failure and malformed metadata are retryable errors', async (t) => {
  let response = new Response('', { status: 429 });
  const updater = await fixture(t, async () => response);
  await updater.check(); assert.equal(updater.state.status, 'error');
  response = new Response('not json');
  await updater.check(); assert.equal(updater.state.status, 'error');
  response = Response.json(release());
  await updater.check(); assert.equal(updater.state.status, 'available');
});

test('oversized release metadata is not accepted even if it is valid JSON', async (t) => {
  const r = release(); r.body = 'x'.repeat(1024 ** 2);
  const updater = await fixture(t, async () => Response.json(r));
  await updater.check();
  assert.equal(updater.state.status, 'error');
});

test('redirect loops are bounded', async (t) => {
  let requests = 0;
  const updater = await fixture(t, async (url) => {
    if (url.includes('/releases/latest')) return Response.json(release());
    requests++;
    return new Response(null, { status: 302, headers: { location: downloadURL } });
  });
  await updater.check(); await updater.download();
  assert.equal(updater.state.status, 'error');
  assert.ok(requests <= 6);
});

test('changed cached installer is refused before starting any helper', async (t) => {
  const updater = await fixture(t, async (url) => url.includes('/releases/latest')
    ? Response.json(release()) : new Response(content));
  await updater.check(); await updater.download();
  const [job] = await fs.readdir(updater.directory);
  await fs.writeFile(path.join(updater.directory, job, 'killfeed-lens-1.2.3-setup.exe'), Buffer.alloc(content.length));
  assert.equal(await updater.install({ appExe: 'C:\\unrelated\\KillfeedLens.exe', appPid: process.pid, enginePid: 0 }), false);
  assert.equal(updater.state.status, 'error');
});

test('previous installer failure is surfaced once on the next launch', async (t) => {
  const updater = await fixture(t, async () => Response.json(release()));
  await fs.writeFile(path.join(updater.directory, 'result.json'), JSON.stringify({ status: 'error', message: '설치 실패', version: '1.2.3' }));
  await updater.readResult();
  assert.equal(updater.state.status, 'error');
  assert.match(updater.state.message, /설치 실패/);
  await assert.rejects(fs.stat(path.join(updater.directory, 'result.json')), { code: 'ENOENT' });
});

test('an asynchronous launcher startup error is reported without crashing the app', { skip: process.platform !== 'win32' }, async (t) => {
  const updater = await fixture(t, async (url) => url.includes('/releases/latest')
    ? Response.json(release()) : new Response(content));
  await updater.check(); await updater.download();
  updater.launcherPath = path.join(updater.directory, 'fixture-launcher.exe');
  await fs.writeFile(updater.launcherPath, content);
  t.mock.method(childProcess, 'spawn', () => {
    const child = new EventEmitter(); child.kill = () => {}; child.unref = () => {};
    process.nextTick(() => child.emit('error', new Error('launcher startup failed')));
    return child;
  });
  assert.equal(await updater.install({ appExe: path.join(updater.directory, 'KillfeedLens.exe'), appPid: process.pid, enginePid: 0 }), false);
  assert.equal(updater.state.status, 'error');
  assert.match(updater.state.message, /launcher startup failed/);
});
