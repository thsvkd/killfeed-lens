# 구현 상태와 검증 범위

PRD/SPEC의 자료 없이 구현 가능한 코드 경로를 구현했습니다. 실제 PUBG 데이터와 Windows
장비가 필요한 acceptance는 미검증 상태입니다. 구현과 제품 정확도 승인 여부를 구분합니다.

## 구현한 경로

- 녹화영상/Windows Desktop Duplication → 공통 ROI·전처리·변화 감지·OCR·아이콘·행 조립
- 공간 기반 parser, 명시적 confidence, 반복 증거와 일대일 행 추적, 스크롤·burst 중복 제거
- SQLite WAL, 즉시 event commit, heartbeat 기반 crash recovery, 동시 writer 잠금
- 독립 console/JSONL sink, JSONL/CSV export, opt-in crop/full-frame와 retention
- PUBG API client, 보수적 session matching, 의미가 불분명한 finisher 분리
- 시간 offset·capture interval·일대일 Telemetry 정렬, 필드·event·중복 지표와 평가 coverage
- calibration/replay/run/validate/report/export/sessions CLI
- fixture manifest 생성, 실제 입력 SHA256 검증, benchmark matrix/sample/holdout acceptance 검사
- 금지 기능 정적 검사 및 Linux/Windows CI 설정

## 검증 증거

- 전체 단위·통합·회귀 테스트: 102개 통과
- Ruff 및 mypy 통과
- 실제 OpenCV 영상 디코딩과 로컬 RapidOCR/ONNX inference를 사용하는 합성 회귀 테스트 통과
- ONNX Runtime의 선택적 telemetry를 import/session 생성 전에 비활성화
- 합성 CLI smoke: replay → SQLite/JSONL → offline validate → JSON report → CSV export 통과
- 합성 benchmark: 20 frames, 1 confirmed event, 중복 0, 오류 0
- CLI smoke의 `AAAA → BBBB` 한 건은 합성 정답과 일대일 정렬됨
- benchmark acceptance는 `not_established`로 표시됨: 실제 holdout·시간·표본·matrix 미충족
- wheel과 sdist 빌드 및 wheel 직접 import/CLI smoke 통과
- 금지 기능 audit 및 독립 코드 리뷰 수행; 확인된 High/Medium 이슈를 회귀 테스트와 함께 수정

합성 smoke 결과는 로컬 [data/synthetic-smoke/](../data/synthetic-smoke/)에 있으며 git에는
포함하지 않습니다. 영상·아이콘·Telemetry 모두 테스트용 합성 데이터입니다.
이는 실제 PUBG F1/정확도 증거가 아닙니다.

## 주요 리뷰 수정

- 캡처 실패를 성공으로 종료하지 않음; 동일한 정상 픽셀과 실제 capture 장애를 구분
- `NaN`/무한 confidence 거부; nickname punctuation을 보존해 다른 행의 증거가 합쳐지지 않도록 수정
- OCR unknown type도 강한 이름 쌍 여러 개로 시간 offset 추정 가능
- partial capture 범위를 벗어난 Telemetry는 FN에서 제외하고 제외 수·범위를 표시
- 잘못된 Telemetry 시각은 조용히 누락하지 않고 검증 실패로 표시
- match asset/CDN 가용성 지연도 bounded polling; 이전 정상 report 보존
- 재시작 시간 대신 마지막 heartbeat로 세션 종료 추정; historical replay 중단은 영상 시각 사용
- 추론 scheduling과 tracker evidence 설정을 같은 cadence 계산으로 검증
- 임시 출력 경로와 독립적인 config hash, threshold까지 포함하는 model hash, 실제 video/Telemetry hash
- holdout 이외 데이터로 RC acceptance를 통과시킬 수 없음

## 남은 acceptance

| 항목 | 상태 | 필요한 입력/환경 |
|---|---|---|
| 실제 kill/knock icon assets와 ROI/threshold 선정 | 미검증 | PUBG crop/영상 tuning 자료 |
| 30분 이상 안정성·100개 이상 이벤트 | 미검증 | 라벨/Telemetry가 있는 녹화영상 |
| holdout F1 ≥ 0.95, pair exact ≥ 0.97 | 미검증 | 별도 holdout dataset |
| 실제 duplicate < 0.5%, false event ≤ 1/30분 | 미검증 | 위 dataset |
| 1080p/1440p, 두 UI scale, bright/dark·slow/burst | 미검증 | stratified footage |
| Windows 실제 화면 캡처·permission·재연결 | 실장비 미실행 | Windows PC |
| 게임 동시 CPU/GPU/RAM·FPS 영향 | 미측정 | 대상 Windows PC/PUBG 실행 |
| PUBG API 인증·실제 경기 자동 매칭 | HTTP fixture 검증 | API key와 실제 종료 경기 |
| live 배포 정책 승인 | 승인 없음 | KRAFTON/PUBG 서면 확인 |

실제 템플릿이 없으면 분류는 `unknown`입니다. 현재 자료만으로 위 수치나 정책 승인을
주장하지 않습니다. P1/P2의 headshot/weapon 세분화, Overwolf, desktop viewer 등은 별도 범위입니다.
