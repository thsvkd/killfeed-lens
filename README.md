# Killfeed Lens

PUBG 킬피드에 표시된 픽셀을 로컬 OCR로 읽고, 관측한 이벤트를 SQLite에 저장합니다.
녹화영상과 Windows 화면 캡처가 같은 인식·파싱·추적 코드를 사용합니다.
공식 PUBG Telemetry는 경기 종료 후 검증에만 사용합니다.

**PUBG/KRAFTON의 공식 승인 제품이 아닙니다.** 개발·검증은 녹화영상, 리플레이,
커스텀 매치를 우선합니다. `RANKED_LIVE_USAGE_APPROVED=false` 상태이며,
공개 live 배포에는 서면 정책 확인이 필요합니다.

## 설치

Python 3.11 이상이 필요합니다. 이 저장소의 검증 환경은 Python 3.12입니다.

```bash
uv sync --locked --extra dev --extra ocr
uv run kfocr --help
```

Windows 화면 캡처도 사용할 경우:

```powershell
uv sync --locked --extra dev --extra ocr --extra windows
```

OCR는 RapidOCR와 패키지에 포함된 ONNX 모델을 CPU에서 로컬 실행합니다.
설치 이후 OCR 모델을 자동 다운로드하거나 화면·영상·닉네임을 업로드하지 않습니다.
macOS/Linux에서는 녹화영상 처리를 사용합니다. live 캡처는 Windows의 DXcam
Desktop Duplication 어댑터로 구현했습니다.

## ROI 보정과 영상 처리

문서의 ROI 좌표는 예시값이므로, 보정 프로필 또는 명시적인 ROI가 필요합니다.
스크린샷에서 드래그로 선택하거나 normalized 좌표를 숫자로 입력할 수 있습니다.

```bash
uv run kfocr calibrate --image screenshot.png --output data/roi.yaml --ui-scale 100
uv run kfocr calibrate --image screenshot.png --rect 0.64 0.03 0.35 0.30 --output data/roi.yaml
uv run kfocr replay match.mp4 --roi-profile data/roi.yaml --max-speed --jsonl data/events.jsonl
```

숫자 예시는 각 영상에 맞게 조정하세요. GUI 선택이 어려운 환경에는 `--rect X Y W H`를
사용합니다. 프로필에 해상도·UI scale을 기록하고, ROI는 프레임의 크기에 맞춰 변환합니다.
비율이나 게임 UI가 바뀌면 다시 보정해야 합니다.

원본 녹화 시각을 알면 `--started-at`을 설정합니다. 영상 자체에는 공식 경기의 UTC 시각이
없어, 이 값 없이 오래된 녹화영상을 처리하면 최신 경기와의 자동 매칭은 신뢰할 수 없습니다.

```bash
uv run kfocr replay match.mp4 --roi-profile data/roi.yaml --started-at 2026-10-01T12:00:00Z
uv run kfocr replay match.mp4 --roi 0.64 0.03 0.35 0.30 --realtime
```

`--max-speed`에서도 이벤트 시각은 원본 영상 타임라인을 사용합니다. 종료 시 출력되는
`session_id`로 조회·검증·내보내기를 수행합니다.

## 이벤트 종류와 아이콘 템플릿

현재 저장소에는 실제 PUBG 영상에서 검증한 kill/knock 템플릿이 없습니다.
템플릿이 없거나 분류가 불확실하면 `unknown`으로 처리합니다. 이름과 시간은 별도로 추적하지만,
불확실한 이벤트를 강제로 kill로 바꾸지 않습니다.

로컬 템플릿을 사용할 때 config의 `vision.icon_template_dir`를 설정하고 해당 폴더에
이미지와 `manifest.json`을 둡니다. 종류가 같은 위치에서 충돌하면 `unknown`입니다.

```json
{
  "icons": [
    {"label": "kill_marker", "file": "kill.png", "threshold": 0.92},
    {"label": "knock_marker", "file": "knock.png", "threshold": 0.92}
  ]
}
```

이미지는 실제 보정 환경의 아이콘 crop이어야 합니다. 템플릿/threshold는 tuning 데이터에서
선정하고 holdout 데이터로 평가하세요. 테스트의 합성 아이콘은 제품 모델이 아닙니다.

## Windows 캡처

```powershell
uv run kfocr calibrate --monitor 0 --output data/roi.yaml
uv run kfocr run --source screen --monitor 0 --roi-profile data/roi.yaml --console
```

별도 터미널에 관측된 이벤트 이력만 출력합니다. 게임 overlay, 프로세스 메모리 접근,
패킷 수집, 입력 자동화는 구현하지 않습니다. 기본 12 FPS 캡처/최대 4 FPS 추론을 사용하며,
screen 프레임 큐는 두 개로 제한합니다. 영상 replay는 프레임을 순서대로 처리합니다.
Ctrl+C로 종료하면 세션은 `aborted`로 기록하고 이미 저장된 이벤트는 유지합니다.

## 검증·조회·내보내기

API key는 `PUBG_API_KEY` 환경 변수로 전달합니다. config나 명령 인자에 저장하지 않습니다.
플레이어 조회에만 인증을 붙이고 match/Telemetry 요청에는 붙이지 않습니다.

```bash
uv run kfocr validate SESSION_ID --shard steam --player PLAYER_NAME
uv run kfocr validate SESSION_ID --shard steam --match-id MATCH_ID
uv run kfocr validate SESSION_ID --telemetry telemetry.json
uv run kfocr report SESSION_ID --format text
uv run kfocr report SESSION_ID --format json
uv run kfocr export SESSION_ID --format jsonl --output data/events.jsonl
uv run kfocr export SESSION_ID --format csv --output data/events.csv
uv run kfocr sessions
```

오프라인 `--telemetry`에는 API key가 필요 없습니다. 자동 경기 매칭이 불확실하면 중단합니다.
명시적 `--match-id`는 사용자가 선택한 정답 경기로 기록합니다. `--wait`를 추가하면 설정된
최대 대기 시간 동안 경기 가용성을 확인합니다. 재시도·대기는 제한되며 live history를 변경하지 않습니다.

이름은 원문(`*_raw`)을 보존하고, 비교용 이름에서는 팀 번호·`[클랜]` 태그와 닉네임에 쓸 수 없는
문자를 제거합니다(PUBG 닉네임은 영문·숫자·`_`·`-`). OCR이 `_`를 `.`로 읽는 경우는 `_`로 되돌립니다.
이름 옆의 팀 번호 배지는 `killer_team`/`victim_team`으로 기록합니다. 배지는 1px 순수 검정 테두리로
둘러싸인 단색 상자이므로, 행 OCR과 별도로 상자를 찾아 숫자만 따로 읽습니다(인식 단계만 사용, 2배 확대).
읽은 배지는 행 OCR 전에 배경색으로 칠해서 숫자가 이름에 붙어 읽히지(`33Name`) 않게 합니다.
배지 상자를 찾지 못한 행에서는 이전처럼 행 OCR의 숫자 토큰으로 팀 번호를 추정합니다.
팀 현황판은 같은 팀 안에서 일부만 읽힌 이름이나 OCR 혼동 문자(o/0, i·l/1)만 다른 이름을 한 선수로
합칩니다. 숫자만 다른 팀원(`kim1`/`kim2`)은 합치지 않습니다.
`LogPlayerKillV2`의 killer/finisher/dBNOMaker가 다르면 의미가 불명확한 사례로 남깁니다.
리포트는 TP/FP/FN, F1, 이름·종류 exact accuracy, 중복, 불일치, confidence bucket과
실제로 평가한 표본 비율을 표시합니다. 한 Telemetry 이벤트를 여러 OCR 이벤트에 매칭하지 않습니다.
단일 이벤트로 시계 offset을 확정하지 않습니다. merge/split 비율은 라벨 데이터 없이 측정하지 않습니다.

## 설정·데이터

`--config config.yaml`와 `--db data/custom.sqlite3`는 각 명령에서 지원합니다.
다음은 초기 설정이며, 아이콘 디렉터리는 실제 템플릿이 있을 때만 추가합니다.

```yaml
capture:
  sample_fps: 12
  inference_fps_cap: 4
  monitor: 0
tracker:
  evidence_frames: 2
  evidence_window_ms: 750
  max_gap_ms: 500
confidence:
  confirmed_min: 0.88
  candidate_min: 0.65
storage:
  sqlite_path: data/kfocr.sqlite3
debug:
  save_full_frames: false
  save_event_crops: false
  retention_days: 7
telemetry:
  enabled: true
  api_key_env: PUBG_API_KEY
  poll_interval_sec: 30
  max_wait_sec: 900
```

추론 간격, tracker gap, evidence window가 모순되는 설정은 거부합니다. SQLite WAL로
이벤트마다 commit합니다. 동일 DB에는 하나의 캡처 파이프라인만 실행합니다. 재시작 시 남아 있는
`running` 세션을 `crashed`로 복구하며, active writer가 있는 세션은 조회 명령이 종료하지 않습니다.
config hash에는 출력 파일 경로를 제외해 임시 benchmark 경로로 인한 변화를 방지합니다.

전체 프레임·이벤트 crop은 기본 저장하지 않습니다. `--save-crops` 또는 debug 설정으로
켜면 `data/sessions/<session-id>/`에 저장합니다. 닉네임 등 민감 정보가 포함될 수 있습니다.
설정한 retention 기간이 지난 세션의 debug artifacts를 정리합니다. `.env`와 data는 git에서 제외됩니다.

## 테스트·벤치마크

```bash
uv run pytest -q
uv run ruff check src tests scripts
uv run mypy src scripts
uv run python scripts/audit_capabilities.py
uv run python scripts/build_fixture_manifest.py match.mp4 --split regression --ui-scale 100 --roi-profile data/roi.yaml --output data/fixtures/match.yaml
uv run python scripts/benchmark.py data/fixtures/match.yaml --output data/benchmark.json
```

manifest의 `telemetry`와 `started_at_utc`를 설정하면 검증 리포트도 생성합니다. holdout 평가에는
`--holdout`이 필요합니다. 리포트에 config/model/dataset hashes, 처리 FPS,
OCR·확정·DB 지연 p50/p95와 표본 수를 기록합니다. CPU/GPU 사용률과 RAM,
게임과 동시 실행했을 때의 성능은 현재 미측정입니다.

실제 로컬 OCR·OpenCV video decode·템플릿 탐지·파싱·추적·DB 저장을 연결한 합성 회귀 테스트와
API MockTransport 경계 테스트를 포함합니다. 합성 결과는 실제 PUBG 정확도를 증명하지 않습니다.
금지 기능 정적 검사는 회귀 방지용이며 코드/의존성 리뷰를 함께 수행해야 합니다.

현재 실제 PUBG 30분 이상 영상, holdout F1 ≥ 0.95 / pair exact ≥ 0.97,
Windows 장비 캡처·게임 동시 성능은 미검증입니다. 이 항목을 통과하기 전에는
PRD/SPEC의 MVP acceptance 완료로 간주하지 않습니다.

구현 범위와 acceptance 상태는 [docs/IMPLEMENTATION_STATUS.md](docs/IMPLEMENTATION_STATUS.md)에 정리했습니다.
