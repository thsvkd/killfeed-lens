# 공개 저장소 전환 및 검사 기록

점검일: 2026-11-01. 기준 커밋: `2e4c605` (`v0.4.1`).
저장소 `thsvkd/killfeed-lens`는 사용자 승인에 따라 **2026-11-01 공개 전환을 완료**했다.
인증 없는 요청으로 저장소 API의 `private=false`·HTTP 200, 최신 `v0.4.1` 릴리스·HTTP 200,
설치 파일의 HEAD·HTTP 200과 SHA-256 digest 존재를 확인했다.
공개 전환 점검 단계에서는 새 push·태그·릴리스 게시나 Git 이력 재작성을 수행하지 않았다.

## 판단

원격 Git 이력의 패턴 검사에서 실제 인증정보를 발견하지 못했다. 다만 이것이 모든 비밀정보의 부재를
증명하지는 않는다. 공개 전환과 별도로 **코드의 이용 허용 범위, 게임 유래 자산의 재배포 근거와
배포 고지**는 정리 대상이다. 저장소가 공개됐다는 사실을 오픈소스 이용 허락이나 게임 내 live 사용
승인으로 간주하지 않는다. 기존 릴리스 12개 자산이 접근 가능하며, 내부를 검사하지 않은 10개의
한계는 아래에 남겼다.

## 완료한 준비

- 현재 추적 파일 129개와 로컬 전체 refs에서 도달 가능한 42개 커밋을 조사했다.
  고유 blob 497개 중 텍스트 459개를 패턴 검사했고, 바이너리 38개는 내용 검사의 대상에서 제외했다.
- 원격 heads/tags를 `git ls-remote`로 대조했다. origin 브랜치 이력은 35개 커밋이며,
  추가 7개는 로컬 백업 refs의 커밋이다. 백업 자료를 원격 공개 자료와 구분했다.
- API 키·GitHub 토큰·AWS 키·개인키·JWT·문자열 secret 할당과 사용자 경로·이메일·LAN/Tailnet 주소를
  검사했다. 결과에는 비밀값을 남기지 않았다. 이 초기 패턴 검사는 아래 추가 Gitleaks 검사와 구분한다.
- 42개 커밋의 메시지·작성자 메타데이터를 GitHub 토큰·AWS 키·개인키·JWT 패턴으로 검사했다.
  현재 조회되는 Actions 실행 17개의 로그 ZIP도 같은 네 종류의 패턴으로 검사했다.
  두 검사에서 해당 패턴은 검출되지 않았다. 모든 종류의 비밀이나 개인정보를 배제하는 검사는 아니다.
- `.gitignore`의 `/.omc/`를 `.omc/`로 넓혀 `app/.omc/` 등 하위 디렉터리의 에이전트 상태와
  검증 자료도 제외했다. 이미 추적한 과거 자료를 지우는 변경은 아니다.
- 현재 `.env`, `.env.*`, `data/*`, SQLite, `build/`, `dist/` 제외 규칙을 확인했다.

## Git 이력과 개인정보

| 구분 | 확인 결과 | 공개 전 조치 |
| --- | --- | --- |
| 원격 이력의 인증정보 후보 | `tests/unit/test_telemetry.py:109`의 테스트용 가짜 값. 관련 blob 버전 6개 | 실제 운영 키 검출로 보지 않음 |
| 원격 이력의 사용자 경로 후보 | `tests/unit/test_release.py:20`의 가상 사용자 경로. 관련 blob 버전 3개 | 실제 개인 경로 검출로 보지 않음 |
| 로컬 백업 refs | `1eaa83f`, `63c9bec`에 `.omc/` 세션 상태, 임시 DB, 화면 이미지, 사용자 경로 존재 | origin에는 도달하지 않음. 백업 refs를 원격에 올리지 않음 |
| 커밋 작성자 정보 | GitHub noreply가 아닌 작성자 이메일 존재 | 소유자가 공개 여부 확인. 값은 이 문서에 복사하지 않음 |

로컬 백업 refs의 자료는 현재 GitHub 공개 전환으로 새로 업로드되는 것이 아니다. `git push --mirror`나
백업 ref를 명시하는 push를 피한다. 브랜치를 올릴 때도 대상 ref를 확인한다. 사용자 확인 없이 과거 커밋을
재작성하거나 백업 자료를 삭제하지 않는다.

Git 이력 검사는 로컬에서 도달 가능한 refs와 원격 heads/tags를 대조한 범위다. GitHub의 숨겨진 PR refs,
이슈·댓글·위키·Discussion·첨부, 삭제 후 서버에 남은 객체, 다른 clone은 전수 검사하지 않았다.
초기 텍스트 검사는 바이너리 내용을 보증하지 않는다. 아래 대표 릴리스 검사도 전체 12개 자산의
모든 픽셀·압축 내부·내장 문자열에 인증정보가 없음을 보증하는 검사는 아니다.
현재 `.npz`는 이미지 배열·단일 문자 배열·너비 배열로 구성됨을 별도 확인했다.

### 추가 비밀정보 검사

공식 Gitleaks `v8.30.1` Windows x64 ZIP을 upstream checksum 파일의 SHA-256과 대조한 뒤,
사용자 정의 규칙이나 allowlist를 추가하지 않고 기본 규칙으로 검사했다. `--redact=100`을 적용한
JSON·로그는 Git에서 제외되는 `.omc/public-readiness-20261101/`에만 저장했다.

- `gitleaks git --log-opts=--all`: 20건 검출. 모두 원격에서 도달하지 않는 백업 커밋
  `1eaa83f`·`63c9bec`의 `.omc/state/session-end-jobs/`에 있는 `scopeKey`, `intentKey`,
  `idempotencyKey` 작업 식별자가 `generic-api-key` 규칙에 걸린 경우다. 서비스 인증키로 사용되는
  위치는 아니며 공개 대상 원격 refs의 실제 키 검출로 분류하지 않는다.
- 현재 작업 파일 131개 스냅샷의 `gitleaks dir`: 무탐지, 종료 코드 0. Git에서 제외된 `.omc/`,
  `.venv/`, `node_modules/`, `build/`, `dist/`는 작업 파일 검사에서 제외했다.

도구 배포 근거: [Gitleaks v8.30.1](https://github.com/gitleaks/gitleaks/releases/tag/v8.30.1).
기본 규칙의 무탐지는 모든 형식의 비밀정보가 없음을 증명하지 않으며, 이후 변경분은 다시 검사한다.

## 권리·배포 정책의 후속 정리

### 1. 코드의 이용 허용 범위

루트 `LICENSE`가 없다. 공개 열람만 허용할지, 수정·재배포·상업적 이용까지 허용할지를 먼저 정하고,
오픈소스로 배포한다면 선택한 라이선스 전문과 저작권 표기를 추가한다. 이번 작업에서 임의로 MIT 등의
라이선스를 적용하지 않았다. 공개 저장소와 오픈소스 이용 허락은 구분한다.
[GitHub 라이선스 안내](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/licensing-a-repository).

### 2. 게임 유래 자산과 live 배포

`src/kfocr/assets/icons/2026-10/*.png`와 `src/kfocr/assets/glyphs/2560x1440.npz`가 추적되고 있다.
코드 주석은 glyph 템플릿을 같은 해상도의 캡처에서 수집한 것으로 설명한다. 아이콘 manifest에는
라벨·파일명·임계값만 있고, 자산의 출처·권리자·재배포 허용 근거는 없다. 앱 아이콘 두 파일의 출처도
저장소 내 문서만으로 확정하지 못했다. 직접 제작 여부와 게임에서 추출한 부분의 권리 처리를 확인한다.

README의 `RANKED_LIVE_USAGE_APPROVED=false` 및 공개 live 배포 전 서면 정책 확인 조건은 유지한다.
기술적 화면 캡처 방식만으로 게임 정책 승인을 추정하지 않는다.
[PUBG 이용약관](https://www.pubg.com/en/clause/term_of_service),
[PUBG 콘텐츠 제작 가이드라인](https://www.pubg.com/en/clause/content_creation_guideline),
[PUBG Developer 약관](https://developer.pubg.com/tos?locale=en).

### 3. 기존 Releases와 Actions 기록의 공개 범위

현재 Releases 7개에는 setup 설치 파일 7개와 portable 실행 파일 5개, 총 12개 자산이 있다.
공개 전환에 따라 기존 게시 상태의 릴리스와 이 12개 파일 모두 익명으로 접근할 수 있다.
이번에 내부를 검사한 바이너리는 아래 최신 setup과 대표 portable 두 개다. 나머지 10개는 내부를
재검사하지 않았고, 검사한 두 개도 모든 번들·고지·자산의 재배포 권리를 확정한 것은 아니다.

GitHub는 private → public 전환 시 Actions 기록과 로그도 누구나 볼 수 있게 된다고 안내한다.
전환 직전에 남아 있는 로그·artifact와 작성자 정보를 다시 확인한다.
[GitHub 공개 범위 변경 안내](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/managing-repository-settings/setting-repository-visibility).

### 대표 릴리스 바이너리의 개인정보·키 검사

원격 Release의 아래 두 파일을 내려받아 GitHub API의 SHA-256 digest와 일치함을 확인했다.
설치 프로그램이나 앱은 실행하지 않았다. setup은 체크섬을 대조한 upstream portable
Inno Unpacker `2.71.3`으로, portable은 로컬 PyInstaller의 `CArchiveReader`로 추출했다.
Electron `app.asar`, Python `PYZ.pyz`와 `base_library.zip` 내부도 코드 실행 없이 풀었다.

| 검사 자산 | 원본 크기 | 파생 추출물을 포함한 검사 범위 |
| --- | --- | --- |
| `v0.4.1` setup | 192,620,645 bytes | 2,484개 파일, 663,630,761 bytes |
| `v0.3.0` portable | 105,412,937 bytes | 2,394개 파일, 265,070,038 bytes |

- 두 추출 폴더에 공식 기본 Gitleaks 규칙과 `--redact=100`을 적용했다. 이 추가 검사에서는
  16 MB 초과 파일을 Gitleaks에서 제외했으며, 아래 별도 문자열 패턴 검사는 큰 파일도 포함했다.
- Gitleaks는 각 자산에서 3건씩 검출했다. 모두 `cv2/gapi/__init__.py:229,234,235`의 `op()` 함수
  내 타입 이름 매핑이 `generic-api-key` 규칙에 걸린 경우다. 로컬에 설치된 OpenCV 의존성과 해당
  소스 파일의 바이트가 동일함을 확인했으며, 서비스 인증키로 분류하지 않는다.
- 전체 추출 파일에서 알려진 GitHub/AWS/Google/Slack 키, 개인키, JWT와 소유자의 Windows
  사용자 경로 패턴은 검출되지 않았다. 일반 사용자 경로 후보는 Python·NumPy·Shapely·DXcam의
  빌드 경로와 표준 라이브러리 경로 예시로 분류했다. 소유자의 개인 경로가 검출된 것은 아니다.
- 파일 목록과 파일 헤더에서 사용자 SQLite DB, 세션 로그, `.env`, `roi.yaml`, `settings.json`을
  발견하지 못했다. 포함된 `config.yaml` 후보는 두 자산 모두 RapidOCR의 기본 의존성 설정뿐이다.

검사한 두 자산에서는 공개 시 직접 노출되는 실제 인증키나 사용자 DB·설정·로그를 발견하지 못했다.
나머지 10개 자산의 내부, 영상·이미지의 개인정보, 모든 가능한 사용자 지정 키 형식, 다른 중첩
포맷까지 전수 검사한 결과는 아니다. 검사 무탐지를 안전성이나 재배포 권리의 보증으로 사용하지 않는다.

도구 근거: [Inno Unpacker upstream 릴리스](https://github.com/jrathlev/InnoUnpacker-Windows-GUI/releases/tag/ui_2_2_13),
[PyInstaller archive reader](https://github.com/pyinstaller/pyinstaller/blob/develop/PyInstaller/archive/readers.py).

## 의존성과 바이너리 고지

아래는 현재 로컬 환경과 기존 로컬 `dist/KillfeedLens-win32-x64`를 조사한 출발점이다.
모든 전이 의존성·정적 링크 라이브러리·과거 릴리스의 의무를 확정한 목록은 아니다.

| 항목 | 확인한 라이선스·고지 | 남은 준비 |
| --- | --- | --- |
| Electron / Chromium | 로컬 배포 폴더에 `LICENSE`, `LICENSES.chromium.html` 존재 | 새 설치 파일에도 두 파일이 포함되는지 확인 |
| Pretendard 1.3.9 | 패키지 `dist/LICENSE.txt`, 로컬 배포 `app.asar`에 포함 확인 | 새 설치 파일에서도 보존 확인 |
| RapidOCR 1.4.4 | 설치 메타데이터 Apache-2.0. 패키지의 라이선스 파일 목록은 비어 있음 | 코드와 번들 ONNX 모델의 출처·라이선스·필요한 고지 확보 |
| ONNX Runtime 1.30.0 | 설치 패키지에 `LICENSE`, `ThirdPartyNotices.txt` 존재 | 조사한 로컬 엔진 배포 폴더에서 두 파일을 찾지 못함. 배포 포함 필요 여부를 검토하고 준비 |
| OpenCV 4.14.0.94 | 설치 패키지에 Apache-2.0 및 서드파티 라이선스 파일 존재 | 조사한 엔진 배포 폴더에서 고지 파일을 찾지 못함. 포함 준비 |
| NumPy 2.5.3 / DXcam 0.3.0 | 로컬 엔진 배포 폴더에 라이선스 파일 존재 | 새 빌드에서도 보존 확인 |
| HTTPX / PyYAML / Python / Tcl·Tk / 기타 번들 | 일부 설치 라이선스·배포 고지 확인 | 실제 번들 목록을 기준으로 누락 고지를 수집 |

[RapidOCR 라이선스](https://github.com/RapidAI/RapidOCR/blob/v1.4.4/LICENSE),
[ONNX Runtime 라이선스](https://github.com/microsoft/onnxruntime/blob/main/LICENSE),
[Pretendard 라이선스](https://github.com/orioncactus/pretendard/blob/main/LICENSE).

프로젝트에 선택할 코드 라이선스로 제3자 게임 자산이나 번들 모델의 권리를 대신 처리하지 않는다.
고지 수집과 패키징 변경은 해당 권리·모델 목록을 확정한 다음 별도 검증한다.

## CI·릴리스 설정

- workflow 기본 권한은 `contents: read`이며 `publish-release`에만 `contents: write`가 있다.
  저장소의 기본 workflow 권한도 `read`, PR 승인 권한은 꺼져 있다.
- 저장소 Actions secrets 목록은 비어 있고, 두 workflow에 별도 PAT나 PUBG API 키 참조가 없다.
  private 저장소의 공유 토큰을 앱에 넣는 구조는 없다.
- CI는 `pull_request`로 동작하며 `pull_request_target`, self-hosted runner는 사용하지 않는다.
  외부 PR도 Windows 패키징 잡까지 실행할 수 있다. 현재 timeout·concurrency는 존재하나,
  외부 기여자의 workflow 실행 승인 정책과 실제 runner 과금 범위는 소유자 설정에서 확인한다.
- 외부 Actions는 버전 태그로 고정되어 있고 전체 commit SHA로 고정되지는 않았다.
  공개 전 강화 항목으로 남긴다. 현재의 동작을 검증 없이 바꾸지는 않았다.
- Release는 `v*` 태그 push → 기존 CI → 패키징 → 설치 파일 게시 순서이며,
  `pyproject.toml` 버전과 정확히 일치하는 태그만 허용한다. OTA는 이번 준비 범위에 없다.

권한 설정의 근거와 SHA 고정 권고는
[GitHub Actions 보안 안내](https://docs.github.com/en/actions/reference/security/secure-use)를 참고한다.

## 이후 변경·배포 확인 순서

1. 위 소유자 결정과 자산·고지 정리를 마친다. 기존 Releases를 포함해 공개할 범위를 확인한다.
2. 새 커밋·추가 refs·Actions 자료를 다시 검사한다. 전용 secret scanner와 GitHub secret scanning /
   push protection 활성화도 검토한다. 실키가 발견되면 공개 이전에 폐기·교체한다.
3. `git status`, staged diff와 push 대상 ref를 확인한다. `.omc/`·실제 경기 자료·로컬 출력은 올리지 않는다.
4. 공개 상태와 익명 접근이 유지되는지 확인한다. 새 push·태그·릴리스 게시와 권리 정책 변경은
   이번 공개 전환과 별도 작업이다.

## 후속 0.5.0 OTA 배포 준비

사용자가 별도로 OTA 설계와 검증 후 커밋·푸시·배포를 승인했다. 0.5.0에서는 기존 설치 구조를
유지하면서 공개 Releases 기반 업데이트를 추가하고, 배포 고지를 다음과 같이 보완했다.

- ONNX Runtime의 `LICENSE`·`ThirdPartyNotices.txt`는 패키지 데이터에서 명시적으로 수집한다.
- OpenCV와 의존성의 배포 메타데이터·고지는 재귀적으로 포함한다. RapidOCR·모델 관련 공식 고지와
  출처는 `packaging/licenses/SOURCES.md`에 정리했다.
- 실제 로컬 패키지에서 런처·업데이트 스크립트·고지가 포함되고 테스트 파일·`.omc`가 제외됨을 확인했다.
  이 검사는 패키지 스모크에도 추가해 향후 릴리스에서 누락을 거부한다.
- 전체 Python 269개, 업데이트·설치 34개 및 렌더러 18개 검사와 lint/type/audit를 통과했다.
  사용자 설치판을 교체하는 실기 설치는 수행하지 않았다. 원격 CI·게시 자산은 배포 단계에서 별도로 확인한다.

이 추가 작업이 기존 미검사 자산 10개나 게임 자산의 권리 확인을 대신하지는 않는다.
