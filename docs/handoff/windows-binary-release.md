# 핸드오프: Windows 바이너리 GitHub Releases 배포 (1차)

`thsvkd/ftp-browser`의 Windows 릴리스 구조(태그 트리거, 태그↔버전 단일 규칙, per-user 설치기,
portable exe, 로컬 `script/package.ps1`)를 이 저장소의 Python 앱에 옮긴 결정 기록이다.
이 문서에 없는 케이스는 완료 범위 밖이다.

작업 유형: **기능(feature)**.

---

## 1. 문제 정의

이 앱은 Python 3.12 + `uv` 프로젝트이고, 실행하려면 저장소를 클론해 `uv sync` 후
`uv run kfocr ...` 명령을 직접 조합해야 한다.

- 설치 파일·배포 채널·릴리스 워크플로가 없다.
- `kfocr gui`(팀 현황 창)와 `kfocr run`(live 캡처)이 별도 프로세스라 더블클릭 한 번으로 쓸 수 없다.
- 기본 데이터 경로가 cwd 기준 `data/...`라서, 설치 폴더나 바로가기의 작업 폴더에 DB가 생긴다.
- 콘솔 없는(windowed) 앱에서는 캡처 오류가 어디에도 보이지 않는다.
- 기존 `.github/workflows/ci.yml`은 Windows에서 `mypy`(fcntl 속성, 기존 타입 오류)와
  `tests/unit/test_benchmark.py`의 경로 구분자(`scripts\bad.py`) 때문에 실패한다.

사용자는 소스 클론 없이 Windows에서 앱을 설치하고, 더블클릭으로 팀 현황 창 + live 캡처를 시작할 수 있어야 한다.

---

## 2. 핵심 결정

| #   | 결정 | 근거 |
| --- | --- | --- |
| D1  | 채널은 **GitHub Releases**(private 저장소 `thsvkd/killfeed-lens`) | 추가 인프라 없음 |
| D2  | 산출물은 **Windows x64 `killfeed-lens-<version>-setup.exe`** 하나뿐. portable exe는 Electron UI로 바꾸면서 당분간 만들지 않는다 | ftp-browser 1차 범위(설치판 + portable)에서 portable만 보류 |
| D3  | 설치기는 **Inno Setup, per-user, 관리자 승격 없음**: `PrivilegesRequired=lowest`, `{localappdata}\Programs\killfeed-lens`, 시작 메뉴 바로가기, 선택형 바탕화면 바로가기, 제거 프로그램 | UAC 없이 설치. 버전은 `iscc /DAppVersion=`로 주입 |
| D4  | 릴리스 트리거는 **`push.tags: v*`뿐**. `workflow_dispatch` 없음 | 태그와 버전의 단일 입구 |
| D5  | 태그 규칙: `GITHUB_REF_NAME == 'v' + pyproject version` (완전 일치) | `0.1.0`(접두사 없음), `v0.1.0` vs `0.1.1`, `v0.1.0-beta.1` vs `0.1.0` 전부 거부 |
| D6  | 태그 규칙은 순수 함수 `release_tag_matches_version(tag, version)` 하나(`scripts/release_tag.py`). CI는 같은 파일의 `main()`을 실행 | 규칙을 두 곳에 적지 않는다. 단위 테스트와 CI가 같은 함수를 쓴다 |
| D7  | 릴리스 잡은 `windows-latest`. **`ci.yml`(테스트)을 `uses:`로 먼저 통과**한 뒤 패키징. 기존 CI 검사 범위는 줄이지 않는다(ubuntu·windows 매트릭스, mypy는 Linux 잡에서만: `pipeline.py`의 fcntl 분기가 POSIX 전용) | 테스트 전 패키징 금지 |
| D8  | Release는 **즉시 공개**(draft 아님). `permissions: contents: write`는 publish 잡에만 | 최소 권한 |
| D9  | 업로드는 `softprops/action-gh-release`, 첨부는 `*-setup.exe`만 | 첨부 glob을 고정·검증 가능 |
| D10 | 패키징은 두 단계다. **PyInstaller** spec 하나(`packaging/killfeed-lens.spec`)가 엔진 onedir(`kfocr.exe`, console)만 만들고, `@electron/packager`(`app/package.mjs`)가 Electron 앱(`KillfeedLens.exe`)을 만들면서 그 onedir를 `resources\killfeed-lens`에 넣는다. tk 창 exe와 portable onefile은 만들지 않는다 | 설치판 하나에 UI와 콘솔 CLI를 함께 둔다. tk GUI는 `kfocr.exe gui --live`로 충분하다 |
| D11 | PyInstaller는 새 optional extra `package = ["pyinstaller>=6.10,<7"]`로 추가하고 `uv.lock` 갱신 | 런타임 의존성에 섞지 않는다 |
| D12 | 더블클릭(`KillfeedLens.exe`) = Electron UI. UI가 `resources\killfeed-lens\kfocr.exe engine`을 자식 프로세스로 띄우고, 엔진이 live 캡처와 팀 상태를 맡아 보드 스냅샷을 stdout JSON lines로 보낸다(`app/CONTRACT.md`). 이전의 Tk GUI(`kfocr gui --live`, Tk 메인 스레드 + Pipeline 백그라운드 스레드)는 바로가기 없이 CLI로만 남는다 | G004 결정: 같은 방법으로 잰 UI 부담이 Electron 3.4%, tkinter 30.9%(코어 1개 기준) |
| D13 | 창을 닫으면 캡처 소스의 프레임 루프만 끝내고(`_LiveScreenSource.request_stop`), 카메라 해제·세션 마감은 파이프라인 스레드가 스스로 한다 | dxcam을 다른 스레드에서 grab 중에 해제하지 않는다. `pipeline.py` 무수정 |
| D14 | 데이터 폴더: frozen 앱은 `%LOCALAPPDATA%\killfeed-lens`(config, SQLite, sessions, logs, roi.yaml), 소스 실행은 기존 `data/` | 설치 폴더·cwd에 쓰지 않는다. 기존 CLI 기본값은 그대로 |
| D15 | live 상태(준비 중/캡처 중/불안정/오류)는 툴바에 표시하고 `logs\killfeed-lens.log`에도 남긴다. windowed 앱의 stdout/stderr도 같은 로그로 보낸다 | 콘솔 없는 앱에서 오류가 보여야 한다 |
| D16 | ROI 프로필이 없으면 첫 실행에서 보정을 안내하고, 기존 `kfocr calibrate`의 드래그 선택 로직(`cli._calibrate`)을 재사용해 데이터 폴더의 `roi.yaml`에 저장한 뒤 캡처를 시작한다. 툴바에 **ROI 다시 보정** | 보정 코드를 두 벌 두지 않는다 |
| D17 | 패키징 스모크는 `scripts/smoke_packaged.py` 하나를 CI와 `script/package.ps1`이 같이 쓴다: `--help`, `gui --demo --exit-after 3`, 합성 영상 `replay`로 번들 ONNX 모델 로드·OCR 실행 확인 | ftp-browser `smoke:packaged`와 같은 역할 |
| D18 | 코드 사이닝 **범위 밖**. README에 SmartScreen "추가 정보 → 실행" 안내 | 인증서 없음 |
| D19 | 0.5.0부터 공개 GitHub Releases + 기존 Inno Setup으로 OTA 제공 | 고정 주소·크기·SHA-256 검증 후 사용자가 다운로드·재시작을 확인한다. 코드 서명은 적용하지 않으며 GitHub/HTTPS를 신뢰한다. 0.4.1 이하는 새 설치 파일을 한 번 직접 설치한다 |
| D20 | 태그 푸시·커밋은 **인간이 한다**. 스크립트·CI가 태그를 만들지 않는다. pyproject version도 이 작업에서 올리지 않는다 | 승인 전 push 금지 |
| D21 | `RANKED_LIVE_USAGE_APPROVED=false` 정책 문구는 README에 그대로 둔다 | 공개 live 배포에는 서면 정책 확인이 필요 |

---

## 3. 기각한 대안

- **Nuitka / cx_Freeze / Briefcase** — 빌드 시간·설정이 늘고 rapidocr/onnxruntime/dxcam hook 경험이 PyInstaller보다 적다. 기각(D10).
- **설치판도 onefile** — 매 실행마다 임시 폴더에 수백 MB를 풀어 시작이 느리다. 설치판은 onedir(D10).
- **GUI와 캡처를 두 프로세스로 띄우고 JSONL로 연결** — 프로세스 수명·오류 전달이 복잡해진다. 한 프로세스 + 스레드(D12). 이후 G004에서 Electron UI로 바꾸며 이 방식을 채택했다(지금의 D12, `kfocr engine`).
- **`source.stop()`을 Tk 스레드에서 직접 호출** — grab 도중 COM 포인터를 해제할 수 있다. 프레임 루프 플래그만 내린다(D13).
- **CLI 기본 경로를 전부 `%LOCALAPPDATA%`로 변경** — 기존 CLI·테스트 동작이 바뀐다. GUI live 진입점만 데이터 폴더를 쓴다(D14).
- **NSIS / MSI(WiX)** — per-user 설치·버전 주입은 Inno Setup이 가장 짧다. 기각(D3).
- **workflow_dispatch로 재빌드** — 태그와 버전의 단일 입구가 깨진다. 기각(D4).
- **`config.py` 전역 기본값을 번들 템플릿으로 변경** — CLI·benchmark의 "템플릿 없음 ⇒ unknown" 의미가 바뀐다. GUI live 기본값만 번들 템플릿을 쓴다(6장 5번).
- **새 YAML 파서 의존성** — 계약 테스트는 이미 의존성인 PyYAML(`yaml.safe_load`)과 평문 단언만 쓴다.

---

## 4. 관련 코드 포인터

| 파일 | 역할 |
| --- | --- |
| `pyproject.toml`, `uv.lock` | `package` extra 추가. version은 그대로 |
| (신규) `scripts/release_tag.py` | `release_tag_matches_version` + CI 진입점 `main()` |
| (신규) `scripts/smoke_packaged.py` | 패키징 산출물 스모크(D17). 앱 폴더를 받아 `kfocr.exe engine --demo` 스냅샷과 패키지 앱 `--shots`(`app/smoke.mjs`)도 확인 |
| `scripts/audit_capabilities.py` | finding 경로를 posix로 출력(Windows CI 실패 수정) |
| (신규) `src/kfocr/ui/live.py` | 데이터 폴더 선택, 파일 로그, live config, `LiveCapture`(스레드·중지·ROI 보정) |
| `src/kfocr/ui/gui.py` | 툴바 상태 라벨·**ROI 다시 보정** 버튼, `run(live_dir=...)` |
| `src/kfocr/cli.py` | `kfocr gui --live` |
| (신규) `packaging/entry.py` | frozen 진입점(`kfocr.exe`, console = `kfocr` CLI). 콘솔 없이 실행되면 없는 stdio만 로그로 |
| (신규) `packaging/killfeed-lens.spec` | 엔진 onedir(`kfocr.exe`). tk 창 exe·portable onefile 없음 |
| `app/`, `app/package.mjs` | Electron UI와 패키징(`@electron/packager` + `@electron/fuses`, 엔진 onedir를 `resources\killfeed-lens`로) |
| `src/kfocr/ui/engine.py` | `kfocr engine`: live 캡처·팀 상태 → 보드 스냅샷(stdout), 명령(stdin) |
| (신규) `packaging/killfeed-lens.iss` | Inno Setup per-user 설치기. Electron 앱 폴더(`dist\KillfeedLens-win32-x64`, 엔진 포함)를 설치 |
| (신규) `script/_common.ps1`, `script/package.ps1` | 로컬 패키징(엔진 빌드 → `npm ci` → Electron 패키징·fuses → 스모크 → 설치기) |
| `.github/workflows/ci.yml` | `workflow_call` 추가. 기존 범위 유지: ubuntu·windows 매트릭스에서 ruff(`src tests scripts`)·pytest·audit, mypy는 Linux 잡에서만. Windows 전용 패키징 스모크 잡 추가 |
| (신규) `.github/workflows/release.yml` | 태그 트리거 릴리스 |
| `README.md` | 설치 섹션(어떤 파일을 받는지, SmartScreen) |

---

## 5. 테스트 케이스 리스트

실제 exe를 만들어 실행하는 검증은 단위 테스트 범위 밖이다. CI의 패키징 스모크 잡과 `script/package.ps1`이 수행한다.

### A. `release_tag_matches_version(tag, version)` — `tests/unit/test_release.py`

- `'v0.1.0'` + `'0.1.0'` → `True`
- `'v0.1.0'` + `'0.1.1'` → `False`
- `'0.1.0'` + `'0.1.0'` → `False` (`v` 접두사 필수)
- `'v0.1.0-beta.1'` + `'0.1.0'` → `False`

### B. 데이터 폴더 선택 — `tests/unit/test_release.py`

- frozen이면 `%LOCALAPPDATA%\killfeed-lens`
- frozen이 아니면 `data`
- frozen 앱 config의 기본 DB·sessions 경로는 데이터 폴더 안이고, 데이터 폴더의 `roi.yaml`이 ROI로 적용된다

### C. 워크플로·설치기 계약 — `tests/unit/test_release.py` (`yaml.safe_load` + 평문 단언)

- release 트리거는 `push.tags: [v*]`뿐이고 `workflow_dispatch`가 없다
- release는 `ci.yml`을 먼저 통과하고, `contents: write`는 publish 잡에만 있다
- ci 테스트 잡은 ubuntu·windows 매트릭스이고, ruff는 `scripts`까지 보며, mypy는 Linux 잡에서만 돈다
- 첨부 glob은 `*-setup.exe` 하나뿐이고 draft가 아니다
- `.iss`는 `PrivilegesRequired=lowest`, `{localappdata}\Programs\killfeed-lens`

---

## 6. 구현 시 함정

1. `dxcam`은 `importlib.metadata.version("dxcam")`을 읽는다(`screen.py`의 패치 가드). frozen 앱에 dist-info가 없으면 live 캡처가 즉시 실패하므로 spec에서 `copy_metadata("dxcam")`이 필요하다.
2. `rapidocr_onnxruntime`의 `config.yaml`과 `models/*.onnx`는 데이터 파일이다. `collect_data_files`를 빼면 OCR 생성자가 frozen 앱에서만 실패한다. 스모크의 `replay`가 이를 잡는다.
3. 콘솔 없이 실행된 PyInstaller 앱은 `sys.stdout`/`sys.stderr`가 `None`일 수 있다. `packaging/entry.py`는 `None`인 스트림만 로그 파일로 돌리고, 엔진이 스냅샷을 쓰는 stdout 파이프는 그대로 둔다.
4. `Pipeline`은 `subprocess`로 `git rev-parse`를 부른다. windowed 앱에서는 세션 시작 때 콘솔 창이 한 번 깜빡일 수 있다(`pipeline.py`는 이번 작업에서 건드리지 않는다).
5. PUBG UI 패치 2026-10용 아이콘 템플릿을 `src/kfocr/assets/icons/2026-10`(패키지 데이터; wheel·PyInstaller `collect_data_files("kfocr")` 모두 포함)에 번들한다. 데이터 폴더의 `config.yaml`에 `vision.icon_template_dir`가 없으면 GUI live 모드(`live_config`)만 이 디렉터리를 쓰고, 명시한 설정이 항상 우선한다. `config.py` 전역 기본값은 바꾸지 않는다: CLI·benchmark·테스트의 "검증된 템플릿 없음 ⇒ unknown" 의미를 유지하기 위해서다. UI 패치가 바뀌면 템플릿을 다시 수집해 이 디렉터리를 교체한다.
6. 버전은 `pyproject.toml`과 `src/kfocr/__init__.py`(`__version__`) 두 곳에 있다. 태그 규칙은 pyproject만 본다. 버전을 올릴 때 둘을 함께 바꾼다.
7. Inno Setup `Korean.isl`은 Inno Setup 6의 공식 언어 파일이다. 러너에 Inno Setup이 없으면 CI가 choco로 설치한다.
8. `softprops/action-gh-release`는 같은 태그를 다시 푸시하면 에셋을 갱신한다. 태그는 immutable로 취급하고, 고치려면 새 버전을 올린다.
9. 로컬 pytest는 이 PC에서 `--basetemp=.omc/tmp-pytest`가 필요하다(임시 폴더 권한). CI에는 넣지 않는다.
10. 기존 미커밋 파일은 이 작업이 되돌리거나 재포맷하지 않는다. `pipeline.py`, `domain/parser.py`, `vision/**`는 다른 세션이 편집 중이므로 수정하지 않는다.
