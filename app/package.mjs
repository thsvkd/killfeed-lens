// Packages the Windows app around the PyInstaller onedir (build that first, script/package.ps1):
//   dist/KillfeedLens-win32-x64/KillfeedLens.exe          Electron UI
//   dist/KillfeedLens-win32-x64/resources/app.asar        this folder, only what the app loads
//   dist/KillfeedLens-win32-x64/resources/killfeed-lens/  the engine: kfocr.exe engine
// then flips the Electron fuses. Usage: npm run package -- <version>
import { join, resolve } from 'node:path';
import { FuseV1Options, FuseVersion, flipFuses } from '@electron/fuses';
import { packager } from '@electron/packager';

const version = process.argv[2];
if (!/^\d+\.\d+\.\d+$/.test(version ?? '')) throw new Error('usage: npm run package -- <x.y.z version>');
const root = resolve(import.meta.dirname, '..');
const engine = join(root, 'dist', 'killfeed-lens');

// From node_modules only the font file the pages load and its licence: Pretendard is the one
// runtime dependency, and its npm package also carries ~95 MB of other formats and subsets.
const FONT = ['/node_modules/pretendard/package.json', '/node_modules/pretendard/dist/LICENSE.txt',
              '/node_modules/pretendard/dist/web/variable/pretendardvariable.css',
              '/node_modules/pretendard/dist/web/variable/woff2/PretendardVariable.woff2'];
const DEV = /^\/(package-lock\.json|\.gitignore|README\.md|CONTRACT\.md|renderer\.test\.cjs|smoke\.mjs|package\.mjs|measure\.ps1|\.omc(\/.*)?)$/;

function ignore(path) {
  if (DEV.test(path)) return true;
  if (!path.startsWith('/node_modules/')) return false;
  return !FONT.some((keep) => keep === path || keep.startsWith(`${path}/`)); // keep their parent dirs
}

const [out] = await packager({
  dir: import.meta.dirname,
  out: join(root, 'dist'),
  name: 'KillfeedLens',
  executableName: 'KillfeedLens',
  platform: 'win32',
  arch: 'x64',
  appVersion: version,
  buildVersion: version,
  appCopyright: 'thsvkd',
  icon: join(root, 'packaging', 'killfeed-lens.ico'),
  asar: true,
  prune: true,
  overwrite: true,
  ignore,
  extraResource: [engine],
  win32metadata: { CompanyName: 'thsvkd', FileDescription: 'killfeed-lens',
                   ProductName: 'killfeed-lens', OriginalFilename: 'KillfeedLens.exe' },
});

await flipFuses(join(out, 'KillfeedLens.exe'), {
  version: FuseVersion.V1,
  [FuseV1Options.RunAsNode]: false, // ELECTRON_RUN_AS_NODE cannot turn the exe into plain Node
  [FuseV1Options.EnableNodeCliInspectArguments]: false, // nor --inspect attach a debugger
  [FuseV1Options.EnableNodeOptionsEnvironmentVariable]: false, // nor NODE_OPTIONS preload code
  [FuseV1Options.OnlyLoadAppFromAsar]: true,
  [FuseV1Options.EnableEmbeddedAsarIntegrityValidation]: true, // packager embedded the digest
});
console.log(out);
