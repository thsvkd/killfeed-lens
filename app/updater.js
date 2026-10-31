'use strict';
// Public GitHub Releases + the existing per-user Inno installer. No Electron dependency here.
const fs = require('node:fs');
const fsp = require('node:fs/promises');
const path = require('node:path');
const { createHash } = require('node:crypto');
const childProcess = require('node:child_process');

const REPO = 'thsvkd/killfeed-lens';
const API = `https://api.github.com/repos/${REPO}/releases/latest`;
const VERSION = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/;
const DOWNLOAD_HOSTS = new Set(['github.com', 'release-assets.githubusercontent.com',
                              'objects.githubusercontent.com', 'github-releases.githubusercontent.com']);
const MAX_SIZE = 1024 ** 3;

function versionParts(version) {
  if (!VERSION.test(version)) throw new Error('올바른 정식 버전이 아닙니다.');
  const parts = version.split('.').map(Number);
  if (!parts.every(Number.isSafeInteger)) throw new Error('버전 번호가 너무 큽니다.');
  return parts;
}

function newer(next, current) {
  const a = versionParts(next), b = versionParts(current);
  for (let i = 0; i < 3; i++) if (a[i] !== b[i]) return a[i] > b[i];
  return false;
}

async function fileDigest(file) {
  const hash = createHash('sha256');
  for await (const chunk of fs.createReadStream(file)) hash.update(chunk);
  return hash.digest('hex');
}

class Updater {
  constructor({ version, directory, launcherPath, onChange = () => {}, fetchImpl = globalThis.fetch }) {
    versionParts(version);
    this.currentVersion = version;
    this.directory = path.resolve(directory);
    this.launcherPath = launcherPath;
    this.fetch = fetchImpl;
    this.onChange = onChange;
    this.state = { status: 'idle', version: null, percent: 0, message: '업데이트를 확인할 수 있습니다.' };
    this.release = null;
    this.file = null;
  }

  _set(status, message, extra = {}) {
    this.state = { ...this.state, status, message, ...extra };
    this.onChange(this.state);
  }

  async _response(url, binary = false) {
    const signal = AbortSignal.timeout(binary ? 10 * 60_000 : 15_000);
    for (let redirects = 0; redirects <= 5; redirects++) {
      const parsed = new URL(url);
      if (parsed.protocol !== 'https:' || parsed.username || parsed.password || parsed.port
          || !(binary ? DOWNLOAD_HOSTS.has(parsed.hostname) : parsed.hostname === 'api.github.com')) {
        throw new Error('허용되지 않은 업데이트 다운로드 주소입니다.');
      }
      const response = await this.fetch(url, {
        redirect: 'manual', signal,
        headers: { 'User-Agent': `killfeed-lens/${this.currentVersion}`,
                   Accept: binary ? 'application/octet-stream' : 'application/vnd.github+json' },
      });
      if ([301, 302, 303, 307, 308].includes(response.status)) {
        const location = response.headers.get('location');
        await response.body?.cancel();
        if (!location) throw new Error('업데이트 다운로드 주소가 비어 있습니다.');
        url = new URL(location, url).href;
        continue;
      }
      if (!response.ok) {
        await response.body?.cancel();
        throw new Error(`업데이트 서버에 연결하지 못했습니다 (HTTP ${response.status}).`);
      }
      return response;
    }
    throw new Error('업데이트 주소가 너무 많이 변경되었습니다.');
  }

  async check() {
    if (['checking', 'downloading', 'ready', 'applying'].includes(this.state.status)) return;
    this.release = null;
    this._set('checking', '새 버전을 확인하고 있습니다.', { percent: 0 });
    try {
      const response = await this._response(API);
      const chunks = [];
      let size = 0;
      for await (const chunk of response.body) {
        size += chunk.length;
        if (size > 1024 ** 2) throw new Error('업데이트 정보가 너무 큽니다.');
        chunks.push(chunk);
      }
      const release = JSON.parse(Buffer.concat(chunks).toString('utf8'));
      if (release.draft !== false || release.prerelease !== false || typeof release.tag_name !== 'string'
          || !release.tag_name.startsWith('v')) throw new Error('올바른 정식 릴리스가 아닙니다.');
      const version = release.tag_name.slice(1);
      if (!newer(version, this.currentVersion)) {
        this._set('current', '최신 버전을 사용하고 있습니다.', { version: null });
        return;
      }
      const name = `killfeed-lens-${version}-setup.exe`;
      const assets = release.assets?.filter((a) => a.name === name);
      if (!assets || assets.length !== 1) throw new Error('이 버전의 Windows 설치 파일을 찾지 못했습니다.');
      const asset = assets[0];
      const url = `https://github.com/${REPO}/releases/download/v${version}/${name}`;
      if (asset.state !== 'uploaded' || asset.browser_download_url !== url
          || !Number.isSafeInteger(asset.size) || asset.size <= 0 || asset.size > MAX_SIZE
          || !/^sha256:[a-f0-9]{64}$/.test(asset.digest ?? '')) {
        throw new Error('설치 파일의 주소·크기·검증 정보를 확인하지 못했습니다.');
      }
      this.release = { version, name, url, size: asset.size, digest: asset.digest.slice(7) };
      this._set('available', `${version} 버전을 다운로드할 수 있습니다.`, { version });
    } catch (error) {
      this._set('error', `업데이트 확인 실패: ${error.message}`);
    }
  }

  async download() {
    if (this.state.status !== 'available' || !this.release) return;
    const release = this.release;
    this._set('downloading', '새 버전을 다운로드하고 있습니다.', { percent: 0 });
    let job, handle;
    try {
      await fsp.mkdir(this.directory, { recursive: true });
      job = await fsp.mkdtemp(path.join(this.directory, 'download-'));
      const partial = path.join(job, `${release.name}.part`);
      handle = await fsp.open(partial, 'wx');
      const response = await this._response(release.url, true);
      const hash = createHash('sha256');
      let size = 0;
      for await (const chunk of response.body) {
        size += chunk.length;
        if (size > release.size) throw new Error('다운로드 크기가 릴리스 정보와 다릅니다.');
        hash.update(chunk);
        await handle.writeFile(chunk);
        const percent = Math.floor(size * 100 / release.size);
        if (percent !== this.state.percent) this._set('downloading', '새 버전을 다운로드하고 있습니다.', { percent });
      }
      await handle.close(); handle = null;
      if (size !== release.size || hash.digest('hex') !== release.digest) {
        throw new Error('설치 파일 검증에 실패했습니다. 다시 다운로드해 주세요.');
      }
      this.file = path.join(job, release.name);
      await fsp.rename(partial, this.file);
      this._set('ready', '다운로드와 검증을 마쳤습니다. 적용하면 앱이 다시 시작됩니다.', { percent: 100 });
    } catch (error) {
      await handle?.close().catch(() => {});
      if (job) await fsp.rm(job, { recursive: true, force: true }).catch(() => {});
      this.file = null;
      this._set('error', `업데이트 다운로드 실패: ${error.message}`, { percent: 0 });
    }
  }

  async readResult() {
    const file = path.join(this.directory, 'result.json');
    try {
      const result = JSON.parse((await fsp.readFile(file, 'utf8')).replace(/^\uFEFF/, ''));
      await fsp.unlink(file);
      if (result.status === 'installed' && result.version === this.currentVersion) {
        this._set('current', '업데이트를 마쳤습니다.');
      } else if (result.status === 'error' || result.status === 'installed') {
        const messages = {
          WAIT_TIMEOUT: '이전 앱이나 엔진이 종료되지 않아 설치를 중단했습니다.',
          HASH_MISMATCH: '다운로드한 설치 파일이 변경되어 설치를 중단했습니다.',
          RESTART_REQUIRED: '업데이트를 마치려면 Windows를 직접 다시 시작해 주세요.',
          APP_LAUNCH_FAILED: '설치 후 앱을 다시 실행하지 못했습니다.',
        };
        this._set('error', `업데이트 설치 실패: ${messages[result.code] || '업데이트 로그를 확인해 주세요.'}`);
      }
    } catch (error) {
      if (error.code !== 'ENOENT') console.error(`update result: ${error.message}`);
    }
  }

  async install({ appExe, appPid, enginePid = 0 }) {
    if (this.state.status !== 'ready' || !this.file || !this.release) return false;
    this._set('applying', '업데이트를 준비하고 있습니다.');
    let child, job;
    try {
      const stat = await fsp.stat(this.file);
      if (!stat.isFile() || stat.size !== this.release.size || await fileDigest(this.file) !== this.release.digest) {
        throw new Error('설치 파일이 변경되었습니다. 다시 다운로드해 주세요.');
      }
      if (process.platform !== 'win32' || !path.isAbsolute(appExe) || path.basename(appExe) !== 'KillfeedLens.exe'
          || !Number.isSafeInteger(appPid) || appPid <= 0 || !Number.isSafeInteger(enginePid) || enginePid < 0) {
        throw new Error('설치된 Windows 앱에서만 업데이트를 적용할 수 있습니다.');
      }
      job = path.dirname(this.file);
      const script = path.join(job, 'install-update.ps1');
      const config = path.join(job, 'job.json');
      const executable = path.join(job, 'UpdateLauncher.exe');
      if (!this.launcherPath || !path.isAbsolute(this.launcherPath)) throw new Error('업데이트 실행 파일을 찾지 못했습니다.');
      await fsp.copyFile(this.launcherPath, executable);
      await fsp.copyFile(path.join(__dirname, 'install-update.ps1'), script);
      await fsp.writeFile(config, JSON.stringify({ version: this.release.version, sha256: this.release.digest,
        appExe, appPid, enginePid }), { flag: 'wx' });
      const diagnostic = await fsp.open(path.join(job, 'helper.log'), 'a');
      let failure;
      try {
        child = childProcess.spawn(executable, [], { windowsHide: true, detached: true,
          stdio: ['ignore', diagnostic.fd, diagnostic.fd] });
        child.once('error', (error) => { failure = error; });
        child.once('exit', (code) => { failure ??= new Error(`설치 보조 프로세스가 종료되었습니다 (코드 ${code}).`); });
      } finally { await diagnostic.close(); }
      const deadline = Date.now() + 30_000;
      while (Date.now() < deadline) {
        try {
          const ready = JSON.parse(await fsp.readFile(path.join(job, 'ready.json'), 'utf8'));
          if (ready.ready === true) { child.unref(); return true; }
        } catch (error) { if (error.code !== 'ENOENT' && !(error instanceof SyntaxError)) throw error; }
        if (failure) throw failure;
        await new Promise((resolve) => setTimeout(resolve, 50));
      }
      throw new Error('설치 보조 프로세스를 준비하지 못했습니다.');
    } catch (error) {
      if (job) await fsp.writeFile(path.join(job, 'abort'), '').catch(() => {});
      child?.kill(); // only our own waiting helper; the app has not begun quitting
      this._set('error', `업데이트 적용 실패: ${error.message}`);
      return false;
    }
  }
}

module.exports = { Updater, fileDigest };
