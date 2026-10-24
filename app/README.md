# killfeed-lens 데스크톱 UI (Electron)

팀 현황 창(팀 카드·최근 킬피드·추적/오버레이 스위치·ROI 지도), 게임 위에 뜨는 오버레이,
ROI 편집기로 이루어진 UI입니다. 모든 경기 상태와 캡처는 `kfocr engine`(Python)이 맡고, 이 앱은 엔진이
stdout으로 보내는 보드 스냅샷을 그리기만 합니다([CONTRACT.md](CONTRACT.md)).

## 개발 실행

레포 루트에서 `uv sync`를 마친 상태여야 합니다.

```bat
cd app
npm install          :: electron 44.5.1, pretendard 1.3.9 (exact pin, package-lock.json)
npm start            :: uv run kfocr engine (레포 루트 기준 data/로 실시간 캡처)
npm run demo         :: uv run kfocr engine --demo (가짜 경기, ROI는 메모리에만)
npm test             :: 별도 임시 설정의 숨김 Electron 창으로 필터·정렬·상태·페이드 검증
npm run smoke        :: 데모 엔진으로 --shots 실행, 네 창의 PNG·트레이로 닫기·렌더러 오류 없음 확인
```

선수 상태는 보드와 오버레이 모두 기호로 표시하며, 생존 추정은 반만 채운 원입니다. 생존 탭에는 전멸 가능 팀과 팀 미확인 항목을
넣지 않으며, 남은 인원순에서는 전멸 가능 팀의 `0~N`을 0명으로 취급합니다.

기절 횟수는 관측된 행을 선수별로 누적합니다. [공식 15.2 시간표](https://pubg.com/en/news/1725)에 따라
90·30·30·10·7·5·4·3·2초를 추정 전환 기준으로 사용하고, 이후 차수는 2초를 사용합니다. 일반 소생은 횟수를
유지하고 사망 후 부활이 관측되면 초기화합니다. 사망 행 없이 시간이 지나면 ‘생존 추정’이며,
‘복귀’로 확정하지 않습니다. 피드에 누락된 기절이나 부활은 알 수 없어 실제 차수와 다를 수 있습니다.
[23.2 패치](https://pubg.com/en/news/6292)는 정지 중 출혈 규칙을 변경했으므로 위 시간은 확정 사망
시간이 아닙니다. 킬피드만으로는 정지·이동 여부를 알 수 없습니다.

`npm install` 뒤 `node_modules\electron\dist\electron.exe`가 없으면 `node node_modules\electron\install.js`를
실행합니다. 팀 현황 창의 X는 창을 트레이로 숨길 뿐이고(설정에서 '앱 종료'로 바꿀 수 있음), 종료는 트레이
아이콘 메뉴의 '종료'입니다. 엔진은 stdin이 닫히면 캡처 세션을 정리하고 끝나며(캡처 정리는 최대 15초),
20초 안에 끝나지 않으면 프로세스 트리째 종료됩니다.

| 설정 | 뜻 |
|---|---|
| `KFL_ENGINE` | 엔진 명령 전체(셸 명령). 예: `set KFL_ENGINE=uv run kfocr engine --roi-profile data\roi.yaml` |
| `--gpu` | 하드웨어 가속을 켭니다(비교 측정용). 기본은 소프트웨어 합성입니다 |
| `--shots <dir>` | 보드·오버레이·ROI 편집기·설정 창 스크린샷을 저장하고, 보드의 X가 앱을 트레이에 남기는지 확인해 `closed-to-tray.ok`를 쓴 뒤 종료합니다(`npm run smoke`가 사용). 저장된 UI 설정은 읽지도 쓰지도 않고 기본값으로 돕니다 |

## 패키징

레포 루트의 `.\script\package.ps1`이 전부 만듭니다. 순서는 PyInstaller 엔진 onedir(`dist\killfeed-lens`),
`npm ci`, `node app\package.mjs <version>`, 스모크, Inno Setup입니다. 마지막 단계는 `@electron/packager`
20.3.0으로 `dist\KillfeedLens-win32-x64`를 만듭니다. 결과물 구성은 다음과 같습니다.

- `KillfeedLens.exe`
- `resources\app.asar`: 페이지, 트레이 아이콘(`icon.ico`), Pretendard woff2·OFL 라이선스만 들어 있습니다.
- `resources\killfeed-lens\`: 엔진 onedir

이어서 `@electron/fuses` 2.1.3이 RunAsNode·NODE_OPTIONS·NodeCliInspect를 끄고 OnlyLoadAppFromAsar·asar integrity를 켭니다.
설치판은 엔진을 `resources\killfeed-lens\kfocr.exe engine`으로 실행하며, `KFL_ENGINE`은 읽지 않습니다.
코드 서명은 하지 않습니다.

## 사용

- **트레이**: 팀 현황 창의 X를 누르면 창만 숨고 캡처·엔진·오버레이는 계속 돕니다(처음 한 번은 Windows
  알림으로 알려 줍니다). 트레이 아이콘을 클릭하면 창이 다시 열리고, 아이콘 메뉴에는 '팀 현황 열기',
  '오버레이 표시', '킬피드 추적', '설정…', '종료'가 있습니다. 아이콘에 마우스를 올리면 엔진 상태가 보입니다.
  '종료'는 창을 모두 닫고 엔진을 위와 같은 순서로 멈춘 뒤 앱을 끝냅니다. 앱을 다시 실행하면 이미 떠 있는
  앱의 팀 현황 창이 열립니다.
- **설정**: 툴바 오른쪽 끝의 '설정' 버튼이나 트레이 메뉴의 '설정…'으로 엽니다. 바꾸는 즉시 적용됩니다.
  - 일반: X 버튼 동작(트레이로 숨기기 / 앱 종료), Windows 시작 시 자동 실행(설치판에서만), 트레이로
    조용히 시작(팀 현황 창을 띄우지 않음). `%APPDATA%` 아래 앱 폴더(`app.getPath('userData')`)의
    `settings.json`에 저장됩니다.
  - 오버레이: 팀 현황 창의 오버레이 스위치와 같고, `Ctrl+Alt+O` 단축키가 등록됐는지 보여 줍니다.
  - 검증용 기록: 킬피드 이벤트 이미지 저장. 엔진이 `config.yaml`의 `debug.save_event_crops`에 저장하고
    캡처를 다시 시작합니다(엔진 연결 전에는 바꿀 수 없음).
  - 정보: 앱 버전, 데이터 폴더(`data\`, 설치판은 `%LOCALAPPDATA%\killfeed-lens`)와 그 아래 `logs` 폴더 열기.
- `Ctrl+Alt+O`: 게임 중에도 오버레이를 켜고 끕니다. 오버레이는 보여줄 내용이 생겨야 나타납니다.
  PUBG는 '전체 창 모드(테두리 없음)'에서만 오버레이가 보입니다.
- **ROI 조정**: 주 모니터 전체를 덮는 편집기가 열립니다. 킬피드 상자는 표시되는 행 전체를, 생존 인원
  상자는 숫자만 포함하도록 맞춥니다. 테두리를 끌면 크기, 안쪽을 끌면 위치가 바뀌고, `Tab`은 상자 전환,
  방향키는 1px(`Shift` 10px) 이동, `Enter`는 확정, `Esc`는 취소입니다. 편집하는 동안 엔진은 캡처를 멈추고,
  확정하면 `data/roi.yaml`(설치판은 `%LOCALAPPDATA%\killfeed-lens\roi.yaml`)에 저장한 뒤 다시 캡처합니다.
  킬피드 ROI가 없으면 첫 실행 때 이 편집기가 안내와 함께 자동으로 열립니다.
- **오버레이 위치 조정**: 오버레이를 끌어 옮긴 뒤 '위치 확정'을 누릅니다. 확정하거나 ROI가 바뀌면
  오버레이는 ROI를 가리지 않는 가장 가까운 자리로 비켜납니다(`gui.clear_of_rois`와 같은 규칙).

## 게임 부담을 줄이는 설정

- 소프트웨어 합성(`app.disableHardwareAcceleration()`): 게임 GPU에 D3D 장치를 만들지 않습니다.
  - 변경 강조는 즉시 켜고 마지막 0.9초에만 불투명도를 연속으로 낮춥니다. 같은 이벤트는 다시 시작하지
  않으며, 중간에 다시 만든 항목도 경과한 시점에서 이어집니다. 종료 시 애니메이션을 제거하고,
  동작 줄이기 설정에서는 페이드 없이 끕니다. 소프트웨어 합성이므로 페이드 중에는 CPU를 사용합니다.
- 보드는 최소화되거나 가려지면 렌더링을 건너뛰고, 다시 보일 때 한 번 그립니다.
- 오버레이: 내용이 없으면 숨김, `focusable: false`, 클릭 통과(`forward` 없이: forward는 전역 저수준
  마우스 훅을 설치), `setContentProtection(true)`로 캡처 제외(킬피드 OCR이 오버레이 글자를 읽지 않음).
- 미디어 키 가로채기와 맞춤법 검사를 끕니다. 단일 인스턴스만 실행됩니다(엔진 둘이 카메라를 다투지 않게).

측정 기준(PUBG 없이, 30초, 코어 1개 기준): 보드 최소화 + 오버레이 3.4%, 186 MB, GPU 드라이버 미로드.
같은 방법으로 tkinter `kfocr gui --demo --overlay`는 30.9%, 520 MB였습니다. `npm start` 중에
`powershell -NoProfile -File measure.ps1 -Seconds 30 [-MinimizeBoard]`로 다시 잴 수 있습니다(엔진 제외).

## 파일

| 파일 | 역할 |
|---|---|
| `main.js` | 창 생성, 엔진 실행·종료, 오버레이 배치·표시, ROI 편집기, 단축키, 트레이, UI 설정 저장 |
| `preload.js` | 렌더러에 열어 주는 IPC 채널 허용 목록 |
| `common.js`, `base.css` | 상태 기호(모양이 의미를 담고 색은 반복), 시계, 변경 강조, 색 토큰 |
| `index.html`, `board.js`, `board.css` | 팀 현황 창 |
| `overlay.html`, `overlay.js`, `overlay.css` | 오버레이 |
| `roi.html`, `roi.js`, `roi.css` | ROI 편집기 |
| `settings.html`, `settings.js`, `settings.css` | 설정 창 |
| `icon.ico` | 트레이 아이콘(`packaging\killfeed-lens.ico` 복사본) |
| `package.mjs` | 패키징과 fuses(`script\package.ps1`이 호출) |
| `smoke.mjs`, `measure.ps1` | 스모크 확인, 자원 측정 |
