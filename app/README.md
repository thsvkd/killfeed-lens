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
npm run smoke        :: 데모 엔진으로 --shots 실행, 세 창의 PNG와 렌더러 오류 없음 확인
```

`npm install` 뒤 `node_modules\electron\dist\electron.exe`가 없으면 `node node_modules\electron\install.js`를
실행합니다. 종료는 팀 현황 창을 닫으면 됩니다. 엔진은 stdin이 닫히면 캡처 세션을 정리하고 끝나며(캡처
정리는 최대 15초), 20초 안에 끝나지 않으면 프로세스 트리째 종료됩니다.

| 설정 | 뜻 |
|---|---|
| `KFL_ENGINE` | 엔진 명령 전체(셸 명령). 예: `set KFL_ENGINE=uv run kfocr engine --roi-profile data\roi.yaml` |
| `--gpu` | 하드웨어 가속을 켭니다(비교 측정용). 기본은 소프트웨어 합성입니다 |
| `--shots <dir>` | 보드·오버레이·ROI 편집기 스크린샷을 저장하고 종료합니다(`npm run smoke`가 사용) |

## 패키징

레포 루트의 `.\script\package.ps1`이 전부 만듭니다. 순서는 PyInstaller 엔진 onedir(`dist\killfeed-lens`),
`npm ci`, `node app\package.mjs <version>`, 스모크, Inno Setup입니다. 마지막 단계는 `@electron/packager`
20.3.0으로 `dist\KillfeedLens-win32-x64`를 만듭니다. 결과물 구성은 다음과 같습니다.

- `KillfeedLens.exe`
- `resources\app.asar`: 페이지와 Pretendard woff2·OFL 라이선스만 들어 있습니다.
- `resources\killfeed-lens\`: 엔진 onedir

이어서 `@electron/fuses` 2.1.3이 RunAsNode·NODE_OPTIONS·NodeCliInspect를 끄고 OnlyLoadAppFromAsar·asar integrity를 켭니다.
설치판은 엔진을 `resources\killfeed-lens\kfocr.exe engine`으로 실행하며, `KFL_ENGINE`은 읽지 않습니다.
코드 서명은 하지 않습니다.

## 사용

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
- 변경 강조에는 애니메이션을 쓰지 않음: 144 Hz 화면에서 소프트웨어 합성은 애니메이션 프레임마다 CPU를
  씁니다. 강조는 즉시 켜고 타이머로 두 단계 어둡게 한 뒤 끕니다. 남은 transition은 스위치·접기 표시처럼
  사용자가 누를 때만 짧게 돕니다.
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
| `main.js` | 창 생성, 엔진 실행·종료, 오버레이 배치·표시, ROI 편집기, 단축키 |
| `preload.js` | 렌더러에 열어 주는 IPC 채널 허용 목록 |
| `common.js`, `base.css` | 상태 기호(모양이 의미를 담고 색은 반복), 시계, 변경 강조, 색 토큰 |
| `index.html`, `board.js`, `board.css` | 팀 현황 창 |
| `overlay.html`, `overlay.js`, `overlay.css` | 오버레이 |
| `roi.html`, `roi.js`, `roi.css` | ROI 편집기 |
| `package.mjs` | 패키징과 fuses(`script\package.ps1`이 호출) |
| `smoke.mjs`, `measure.ps1` | 스모크 확인, 자원 측정 |
