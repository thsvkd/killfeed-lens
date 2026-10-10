# 구현 적대적 리뷰 — 2차

검토일: 2026-10-06 (Asia/Seoul)

**최종 판정: REQUEST CHANGES.** 코드 리뷰는 `REQUEST CHANGES`, 설계 리뷰는 실제 MVP/출시 승인에 대해 `BLOCK`, 녹화영상 프로토타입에 대해 `WATCH`다. 정상 경로 테스트는 통과하지만 중복 확정, 구조 근거 없는 확정, 평가 분모 누락이 남아 있다.

이번 작업은 읽기 전용 검토와 임시 재현 실행이다. 제품 코드와 기존 테스트는 수정하지 않았다. 기존 [PRD/SPEC 적대적 리뷰](../ADVERSARIAL_REVIEW.md)는 보존했다.

## 독립 리뷰와 검증 범위

두 에이전트가 새 컨텍스트에서 같은 구현을 병렬 검토했다.

| 독립 검토 | 담당 | 결론 | 지적 수 |
|---|---|---|---|
| `/root/adversarial_code_v2` | 코드·SPEC 준수·보안·실패 경로 | REQUEST CHANGES | HIGH 5, MEDIUM 3 |
| `/root/adversarial_arch_v2` | 설계 반론·평가 계약·실시간 경계 | MVP/출시 BLOCK, replay WATCH | HIGH 4, MEDIUM 1 |

평가 coverage 지적 하나가 겹친다. 이를 합치면 **CRITICAL 0, HIGH 8, MEDIUM 4**다. 아래에서 실제 동작 재현과 정적 구조 검토를 구분한다. 외부 PUBG 영상의 정확도나 Windows 실제 동작을 재현했다는 의미는 아니다.

대상은 `src/`, `scripts/`, 테스트, PRD/SPEC, README와 구현 상태 문서다. 저장소에 아직 커밋이 없고 구현 파일들이 untracked여서 `git diff` 대신 현재 전체 트리를 검토했다.

검토 스냅샷 SHA256:

```text
6a4003eb446bf402325621711c996047f7b139b3b6d7af86e81aaf47c2943dee
```

해시 대상은 정렬된 `src/**/*.py`, `scripts/**/*.py`, `tests/**/*.py`, `pyproject.toml`, `uv.lock`, `.github/workflows/ci.yml`, `PRD.md`, `SPEC.md`의 59개 파일이다. 각 경로, NUL, 파일 바이트, NUL 순서로 SHA256에 넣었다.

새 기준 검증 결과:

| 명령 | 결과 |
|---|---|
| `.venv/bin/pytest -q` | 102 passed |
| `.venv/bin/ruff check src tests scripts` | 통과 |
| `.venv/bin/mypy src scripts` | 38개 소스 파일 통과 |
| `.venv/bin/python scripts/audit_capabilities.py` | pass, findings 없음 |

## HIGH — 출시 승인 전 해결할 사항

### H1. OCR 실패 한 번으로 같은 잔존 행을 다시 확정한다

근거: [pipeline.py](../src/kfocr/pipeline.py#L265), [tracker.py](../src/kfocr/domain/tracker.py#L69). 담당: 코드 리뷰. **실행 재현 확인.**

OCR 예외는 빈 candidate 업데이트로 바뀐다. 마지막 성공 관측 이후 `max_gap_ms=500`을 넘으면 이미 emit한 track도 제거한다. 화면에 그대로 남아 있는 행을 다음 성공 관측에서 새 이벤트로 확정한다.

- Tracker 재현: 100ms/200ms에 동일 행을 확정 → 800ms 빈 업데이트 → 900ms/1000ms 동일 행. 총 emit 수는 **2**다.
- 독립 리뷰의 전체 Pipeline 재현: 100ms 간격 프레임에서 OCR 실패 한 번 후, 같은 `Alpha → Bravo`가 관측/확정 시각 `(0, 400ms)`와 `(1200, 1600ms)`로 기록됐다.
- 기존 persistent-row 테스트는 성공 관측이 계속 들어오는 경우만 보장한다. gap 테스트는 실제 사라짐과 OCR 실패를 구별하지 않는다.

영향: PRD FR-007, SPEC AT-002의 한 행 한 번 emit 계약. 실시간 프레임 드롭이나 느린 inference에서도 같은 경로가 발생할 수 있다.

수정 방향: 실패한 inference와 정상 프레임에서 확인한 행 부재를 구분한다. emitted 행의 수명을 시각적 사라짐/교체 근거와 함께 관리한다. 이름만으로 긴 TTL을 걸면 이후의 실제 동일 이름 이벤트를 누락할 수 있다. 지속 행 + 단일 OCR 실패의 Pipeline 회귀 사례가 필요하다.

### H2. temporal evidence가 이름의 반복 일치를 보장하지 않는다

근거: [tracker.py](../src/kfocr/domain/tracker.py#L146). 담당: 코드 리뷰. **실행 재현 확인.**

동일 fingerprint의 유사 문자열은 같은 track의 evidence가 된다. 대표값은 전체 confidence가 가장 높은 단일 관측이고, field별 합의는 계산하지 않는다.

재현: `A1pha` 후보 confidence `0.90` 다음에 `Alpha` 후보 confidence `0.855`를 같은 fingerprint로 제공한다. 두 번째 값은 killer 하위 confidence `0.85`로 다시 계산했다. 출력은 **`A1pha`, evidence_count=2**다. 출력 철자는 한 번만 관측됐는데 반복 근거를 받는다.

이 재현은 실제 영상에서 어느 철자가 정답인지 증명하는 사례가 아니다. 서로 다른 field 값 두 개가 단일 철자의 안정성으로 취급된다는 사례다. 같은 행의 추적과 field 값의 안정성을 분리해야 한다.

수정 방향: killer/victim/type별 관측과 합의 또는 명시적 margin을 관리한다. 후보 동일성 판단에는 fuzzy matching을 유지하되 확정 문자열의 근거를 별도로 요구한다. 오인식이 먼저/나중에 오는 경우와 confidence 순서를 바꾼 경우를 검증한다.

### H3. 아이콘 없는 일반 텍스트도 확정 이벤트가 된다

근거: [parser.py](../src/kfocr/domain/parser.py#L63), [rows.py](../src/kfocr/vision/rows.py#L29), [tracker.py](../src/kfocr/domain/tracker.py#L117). 담당: 코드 리뷰. **RowAssembler → Parser → Tracker 실행 재현 확인.**

아이콘이 없으면 좌우로 떨어진 첫/마지막 OCR text를 이름 쌍으로 삼는다. row confidence도 OCR confidence의 평균이다. 따라서 텍스트를 잘 읽었다는 근거만으로 killfeed 구조 confidence까지 높아진다.

ROI 안에 같은 높이의 `Alive 42`, `Ping 18` 두 text box를 confidence `0.99`로 두 프레임 제공했다. candidate confidence는 **0.792**, temporal 추가 후 **0.892**가 되어 기본 확정 임계치 `0.88`을 넘고 `unknown` 이벤트를 emit했다. 기본 아이콘 템플릿이 없는 현재 경로에서 특히 중요하다.

수정 방향: 아이콘 미검출 경로에는 검증된 separator/gap, 행 모양 등 독립적인 killfeed 구조 근거를 요구한다. 그 근거가 없으면 확정 임계치 아래로 제한한다. 일반 HUD/메뉴/반복 텍스트 negative fixture가 필요하다. `unknown`은 정당한 분류 보류 값이며, 그것 자체를 제거할 필요는 없다.

### H4. 세션 내 정답의 96%를 제외해도 benchmark가 pass한다

근거: [validator.py](../src/kfocr/telemetry/validator.py#L34), [validator.py](../src/kfocr/telemetry/validator.py#L123), [benchmark.py](../scripts/benchmark.py#L115). 담당: 양쪽 리뷰. **실제 validator와 acceptance 함수 실행 재현 확인.**

모호한 Telemetry actor를 unresolved로 남기는 정책은 타당하다. 그러나 benchmark는 validator의 평가 coverage와 제외 표본을 승인 조건으로 사용하지 않는다.

재현 입력:

- fixture마다 450초, 일치한 resolved kill 25개와 actor field가 서로 다른 unresolved kill 600개.
- **625개 모두 해당 세션 안에 있다.** 전체 경기의 세션 밖 이벤트 때문에 coverage가 낮아진 사례가 아니다.
- validator: `TP=25, FP=0, FN=0, F1=1.0`; 세션 내 resolvable coverage `25/625=0.04`.
- 1080p/1440p, UI scale 2종, bright/dark, slow/burst 조건을 포함한 4개 fixture, 총 1800초/관측 100개 → **acceptance=pass**.

수정 방향: 전체 경기의 `telemetry_ratio`를 그대로 gate하지 말고 **세션 내 resolvable/temporal coverage**와 실제 평가 표본 수를 사용한다. 필수 의미 범주별 coverage도 공개한다. 최소 coverage를 충족하지 않으면 `not_established`로 두고, 현재 F1을 resolvable subset의 조건부 지표로 명명한다. 임의 숫자를 새 출시 기준으로 넣기 전에 평가 계약을 정해야 한다.

### H5. kill/knock 분류를 전혀 하지 않아도 승인할 수 있다

근거: [validator.py](../src/kfocr/telemetry/validator.py#L263), [benchmark.py](../scripts/benchmark.py#L145), [SPEC.md](../SPEC.md#L1060). 담당: 설계 리뷰. **validator 실행 재현 + 승인 조건 정적 확인.**

`UNKNOWN`은 모든 Telemetry type과 매칭 가능하다. type accuracy는 계산하지만 승인 조건에서 사용하지 않고, 최소 100개 표본도 typed kill/knock 대신 전체 observed 수를 센다.

재현: `UNKNOWN` 관측 2개와 이름·시간이 일치하는 `KILL` 정답 2개 → **F1=1.0, pair exact=1.0, type accuracy=0.0**. 이를 충분한 표본/기간/조건으로 확대해도 현 승인 조건은 type 실패를 막지 않는다.

수정 방향: row/pair detection 지표와 typed-event 지표를 분리한다. `unknown`은 type 평가에서 abstention으로 표현하고 typed coverage, kill/knock별 평가 표본을 gate한다. SPEC의 최소 confirmed kill/knock 표본 요건을 지킨다. 타입 정확도의 별도 수치 목표는 문서에서 합의해야 하며, 이번 리뷰에서 임의로 정하지 않았다.

### H6. 캡처 재시도 한도가 개별 장애가 아닌 세션 전체에 누적된다

근거: [screen.py](../src/kfocr/capture/screen.py#L210), [screen.py](../src/kfocr/capture/screen.py#L232). 담당: 코드 리뷰. **가짜 backend 실행 재현 확인.**

복구 프레임을 충족해 `HEALTHY`가 되어도 `retry_count`를 유지한다. 기본 `max_retries=3`이면 충분히 정상 작동한 구간이 사이에 있어도 네 번째 독립 장애에서 복구를 시도하지 않는다.

재현: backend drop → 정상 프레임 2개를 반복했다. 생성한 backend는 4개이며, 준비된 정상 교체 backend 1개를 시도하지 않고 `after 3 reconnect attempts`로 종료했다.

수정 방향: 성공 복구 후 해당 장애의 연속 재시도 예산을 초기화한다. 평생 reconnect 횟수는 별도 지표로 남긴다. 장시간 세션의 분리된 장애와 복구 불가 연속 장애를 따로 검증한다. 실제 Windows에서의 안정성은 여전히 미검증이다.

### H7. 실시간 blind interval이 세션·viewer·검증으로 전달되지 않는다

근거: [screen.py](../src/kfocr/capture/screen.py#L119), [pipeline.py](../src/kfocr/pipeline.py#L210), [pipeline.py](../src/kfocr/pipeline.py#L319), [sidecar.py](../src/kfocr/ui/sidecar.py#L11), [SPEC.md](../SPEC.md#L1221). 담당: 설계 리뷰. **구조 검토.**

ScreenSource는 black/missing/invalid/recovery 프레임을 걸러내고 메모리의 health를 갱신한다. 복구하면 이전 degraded 구간이 사라진다. pipeline은 health 이력/재연결 이유를 저장하지 않고 sink 인터페이스는 event만 publish한다.

결과: 복구한 세션이 completed로 끝나도 viewer와 리포트에 관측 중단 구간이 남지 않는다. Telemetry 검증은 이 구간까지 일반 관측 시간으로 취급하며, 해당 ground truth를 FN으로 센다. 녹화영상 benchmark는 실시간 dropping queue와 backpressure를 검증하지 않는다.

수정 방향: health 변화를 별도 채널로 전달하고 monotonic/UTC 기준의 degraded 구간을 저장·표시한다. gap과 겹친 정답은 명시적으로 flag하거나 관측 가능 구간 지표에서 제외한다. live soak/backpressure 검증은 녹화 정확도 승인과 별도로 둔다.

현재 문서는 실제 Windows/live 성능 미검증을 정확히 공개한다. 이 지적은 **실시간 MVP 승인 blocker**이며, 녹화영상 경로가 검증됐다는 주장 전체를 부정하지 않는다.

### H8. holdout 승인에 입력 독립성·의미 표본 통제가 부족하다

근거: [benchmark.py](../scripts/benchmark.py#L36), [benchmark.py](../scripts/benchmark.py#L100), [benchmark.py](../scripts/benchmark.py#L194), [PRD.md](../PRD.md#L433). 담당: 설계 리뷰. **구조 검토.**

실제 파일 hash와 해상도/기간 검증은 구현돼 있다. 하지만 중복 manifest ID/video hash를 거절하지 않고 duration과 observed 수를 모두 더한다. 같은 영상을 반복 입력하면 독립 표본과 시간이 늘어난 것처럼 집계할 수 있다. 선언된 Telemetry가 해당 영상/경기의 정답인지에 대한 provenance와 nickname/kill/knock/edge-case 표본 통제도 없다. `--holdout`은 freeze 이후 평가하라는 절차적 표식이다.

수정 방향: 고유 fixture/source hash, match provenance, split, 확인된 의미 범주를 기록하는 dataset registry를 만든다. 중복 source는 승인 표본 합계에서 거절한다. 실제 평가 표본으로 필수 범주 충족 여부를 확인한다. PRD에 없는 모든 조건의 직교 조합을 이번 리뷰가 새 요구로 추가하지는 않는다.

관련 재현성 우려: [pipeline.py](../src/kfocr/pipeline.py#L69)는 HEAD만 기록하고 dirty/source tree 상태는 기록하지 않는다. 현재 커밋 없는 저장소에서는 source revision이 없으므로 출시 결과에 clean revision 또는 source-tree hash를 추가해야 한다. 이 보고서의 스냅샷은 검토 대상을 고정할 뿐 제품 benchmark의 누락을 자동으로 해결하지 않는다.

## MEDIUM — 추가 정확성·운영 문제

### M1. partial-name 매칭과 greedy assignment의 평가 계약이 불명확하다

근거: [validator.py](../src/kfocr/telemetry/validator.py#L226). 담당: 코드 리뷰. **독립 리뷰 실행 재현.**

actor 또는 victim 하나만 일치해도 event TP 후보 edge가 된다. 관측 `A→V1`, `X→V1`과 정답 `A→V1`, `A→V2`에서 현재 greedy 선택은 TP 1개지만, 현재 허용 edge 정책으로는 2개 assignment도 가능하다.

이 대안이 두 개의 정확한 이름 쌍이라는 의미는 아니다. 부분 일치를 event TP로 세는 최소 동일성 계약을 먼저 정해야 한다. 그 뒤 최대 cardinality/최소 cost 등의 전역 one-to-one assignment와 충돌 회귀 사례를 적용한다. 무조건 TP 수를 최대화하면 잘못된 강제 매칭을 늘릴 수 있다.

### M2. Telemetry max_wait가 전체 작업 deadline이 아니다

근거: [cli.py](../src/kfocr/cli.py#L187), [cli.py](../src/kfocr/cli.py#L202), [client.py](../src/kfocr/telemetry/client.py#L115). 담당: 코드 리뷰. **독립 리뷰 fake clock/client 재현.**

deadline을 player lookup과 최대 20개 match의 순차 요청 뒤에 확인한다. 각 요청에는 독립 timeout/retry가 있다. 0.1초 wait budget과 요청마다 가짜 1초 경과를 설정했을 때, 총 20개 match 요청/20초 경과 후에야 wait exhaustion을 보고했다.

수정 방향: 각 요청 전 잔여 deadline을 확인하고 요청 timeout/재시도/대기를 잔여 예산 안에 제한한다. no-wait의 한 번 조회 정책과 전체 wait deadline 계약을 구분해서 명시한다.

### M3. 한 OCR token에 붙은 team prefix가 canonical nickname에 남는다

근거: [parser.py](../src/kfocr/domain/parser.py#L22). 담당: 코드 리뷰. **독립 리뷰 실행 재현.**

team token이 숫자 전체일 때만 제거한다. `[12] Alpha`, `[08] Bravo`가 합쳐진 token으로 들어오면 이 문자열 전체를 canonical 이름으로 emit한다. team 추출이 P1이어도 이름 쌍의 정확도와 Telemetry exact match에 영향을 준다.

수정 방향: raw 문자열은 보존하고 실제 UI에서 확인한 leading team marker만 canonical 이름에서 분리한다. 합쳐진 token 사례와 정당한 닉네임 구두점을 함께 검증한다. 일반 숫자 prefix를 추정으로 무조건 제거하면 안 된다.

### M4. 저장을 끈 뒤에는 과거 debug artifact의 retention이 실행되지 않는다

근거: [pipeline.py](../src/kfocr/pipeline.py#L357), [README.md](../README.md#L150). 담당: 설계 리뷰. **구조 검토.**

현재 두 save flag가 false이면 prune 자체를 건너뛴다. 이전 opt-in 실행에서 만든 이미지가 retention을 지나도 이후 저장 비활성 실행으로는 정리되지 않는다.

수정 방향: 현재 저장 옵션과 무관하게 retention maintenance를 수행한다. lazy retention 시점을 명시하고 로컬 purge 경로를 제공한다. 삭제 구현에서는 현재 세션·비대상 디렉터리·symlink 보호를 유지한다.

## 종합 우선순위와 판정의 한계

1. H1/H3의 중복·잘못된 확정을 먼저 막고 H2의 field 안정성 근거를 보강한다.
2. H4/H5/H8의 승인 계약을 고친다. 높은 조건부 점수와 출시 가능성을 구별해야 한다.
3. H6/H7을 해결한 뒤 실시간 soak 검증을 한다.
4. M1–M4를 각 경계의 회귀 사례와 함께 수정한다.

hardcoded secret이나 금지된 process memory/packet/hook/input 연동은 발견하지 않았다. LSP 전용 도구 대신 새 mypy/Ruff 결과를 정적 검증 근거로 사용했다.

실제 PUBG fixture, 실제 icon asset, Windows 하드웨어 실행, holdout 정확도는 기존 문서에서 공개된 미검증 사항이다. 이것들을 숨겨진 결함 수에 더하지 않았다. 현재 `not_established` 상태는 적절하다. 이번 차단 사유는 향후 입력을 넣었을 때 잘못된 `pass`가 가능하다는 점과 재현된 확정/복구 경로 문제다.

코드 리뷰 `REQUEST CHANGES`와 설계 리뷰 `BLOCK`에 따라 최종 판정은 **REQUEST CHANGES**다. 수정 후 해당 실패 사례를 검증하고 독립 재검토를 받아야 출시 승인 판단을 갱신할 수 있다.
