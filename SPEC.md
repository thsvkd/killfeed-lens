# SPEC.md — PUBG Killfeed OCR/CV Technical Specification

> Status: Final after adversarial review  
> Date: 2026-10-06 (KST)  
> Repository working name: `pubg-killfeed-ocr`  
> Scope: MVP through validation-capable live prototype

---

## 0. 구현 에이전트에게

이 문서에서 **MUST / MUST NOT**은 구현 계약이다.

특히 아래 금지선은 다른 요구보다 우선한다.

### MUST NOT

- PUBG process memory 읽기
- PUBG process injection/hooking
- packet sniffing/interception/modification
- kernel/DMA/anti-cheat bypass
- automated input
- recoil/aim assistance
- game UI modification
- hidden game state 추출
- tactical recommendation

### MUST

- OS-level rendered pixels만 capture
- live output은 observed killfeed 정보로 제한
- replay path와 live path가 동일 vision/parser/tracker를 공유
- official Telemetry를 post-match ground truth로 사용
- low-confidence event를 억지로 확정하지 않음
- 모든 핵심 단계에 test seam/interface를 둠

---

# 1. 권장 구현 스택

## 1.1 MVP

- Python 3.11+
- Windows 11 우선
- Capture abstraction
  - production Windows adapter: Windows Graphics Capture 또는 Desktop Duplication 기반 library
  - replay adapter: OpenCV/FFmpeg
- OpenCV
- OCR engine abstraction
  - default implementation은 local inference 가능한 엔진
  - 특정 OCR vendor에 domain layer가 결합되면 안 됨
- SQLite
- pytest
- pydantic/dataclasses 중 하나로 domain schema
- HTTP client: `httpx` 권장
- CLI: `typer` 또는 `argparse`

### OCR 엔진 선택 원칙

엔진보다 interface가 우선이다.

필수 capability:

- local inference
- Latin letters/digits/symbol 처리
- bounding box 반환
- per-token confidence
- GPU가 없어도 동작

P0에서 두 OCR 엔진을 동시에 통합할 필요는 없다.

---

# 2. 디렉터리 구조

```text
pubg-killfeed-ocr/
├─ pyproject.toml
├─ README.md
├─ PRD.md
├─ SPEC.md
├─ src/
│  └─ kfocr/
│     ├─ cli.py
│     ├─ config.py
│     ├─ logging.py
│     ├─ capture/
│     │  ├─ base.py
│     │  ├─ screen.py
│     │  └─ replay.py
│     ├─ vision/
│     │  ├─ roi.py
│     │  ├─ change.py
│     │  ├─ text.py
│     │  ├─ icons.py
│     │  ├─ rows.py
│     │  └─ preprocess.py
│     ├─ domain/
│     │  ├─ models.py
│     │  ├─ parser.py
│     │  ├─ tracker.py
│     │  └─ confidence.py
│     ├─ storage/
│     │  ├─ db.py
│     │  └─ export.py
│     ├─ telemetry/
│     │  ├─ client.py
│     │  ├─ match_finder.py
│     │  ├─ mapping.py
│     │  └─ validator.py
│     ├─ ui/
│     │  └─ sidecar.py
│     └─ metrics/
│        └─ report.py
├─ tests/
│  ├─ unit/
│  ├─ integration/
│  ├─ regression/
│  └─ fixtures/
├─ data/
│  └─ .gitkeep
└─ scripts/
   ├─ benchmark.py
   └─ build_fixture_manifest.py
```

---

# 3. 핵심 아키텍처

```text
FrameSource
   │
   ▼
KillfeedROI
   │
   ▼
ChangeDetector ── no meaningful change ──> skip
   │
   ▼
Preprocessor
   │
   ├──────────────┐
   ▼              ▼
TextRecognizer   IconRecognizer
   │              │
   └──────┬───────┘
          ▼
      RowAssembler
          ▼
      EventParser
          ▼
    CandidateEvent
          ▼
    TemporalTracker
          │
     ┌────┴─────┐
     │          │
 confirmed    rejected/pending
     │
     ▼
 EventStore ──────> Sidecar/CLI
     │
     └── post-match ──> TelemetryValidator ──> Report
```

각 단계는 독립 interface로 구현하여 recorded fixture만으로 unit/integration test가 가능해야 한다.

---

# 4. Domain model

## 4.1 `Frame`

```python
@dataclass(frozen=True)
class Frame:
    source_id: str
    seq: int
    captured_at_ns: int
    image_bgr: np.ndarray
    width: int
    height: int
```

`captured_at_ns`는 monotonic timestamp를 우선한다.

wall clock timestamp는 session metadata에서 별도 유지한다.

---

## 4.2 `NormalizedRect`

```python
@dataclass(frozen=True)
class NormalizedRect:
    x: float
    y: float
    w: float
    h: float
```

모든 값은 `[0, 1]`.

pixel rect는 runtime에서 frame size로 변환한다.

---

## 4.3 `OCRToken`

```python
@dataclass(frozen=True)
class OCRToken:
    text: str
    confidence: float
    bbox: tuple[int, int, int, int]
```

---

## 4.4 `VisualElement`

```python
class VisualKind(str, Enum):
    TEXT = "text"
    ICON = "icon"

@dataclass(frozen=True)
class VisualElement:
    kind: VisualKind
    label: str
    confidence: float
    bbox: tuple[int, int, int, int]
```

---

## 4.5 `KillfeedRowCandidate`

```python
@dataclass
class KillfeedRowCandidate:
    frame_seq: int
    observed_at_ns: int
    bbox: tuple[int, int, int, int]
    elements: list[VisualElement]
    row_confidence: float
    crop_phash: str | None
```

---

## 4.6 `EventType`

```python
class EventType(str, Enum):
    KILL = "kill"
    KNOCK = "knock"
    UNKNOWN = "unknown"

    # P1+
    FINISH = "finish"
    BLEEDOUT = "bleedout"
    SUICIDE = "suicide"
    ENVIRONMENT = "environment"
```

---

## 4.7 `CandidateEvent`

```python
@dataclass
class CandidateEvent:
    observed_at_ns: int
    frame_seq: int
    killer_name_raw: str | None
    victim_name_raw: str | None
    killer_name: str | None
    victim_name: str | None
    killer_team: int | None
    victim_team: int | None
    event_type: EventType
    weapon_label: str | None
    headshot: bool | None

    confidence_row: float
    confidence_killer: float
    confidence_victim: float
    confidence_event_type: float
    confidence_total: float

    source_bbox: tuple[int, int, int, int]
    source_crop_ref: str | None = None
```

---

## 4.8 `ConfirmedEvent`

`CandidateEvent`에 다음을 추가한다.

```python
event_id: UUID
session_id: UUID
confirmed_at_ns: int
dedup_key: str
tracker_evidence_count: int
```

---

# 5. Session model

```python
@dataclass
class Session:
    session_id: UUID
    source_type: Literal["screen", "video"]
    started_at_utc: datetime
    ended_at_utc: datetime | None
    source_resolution: tuple[int, int]
    roi_profile_id: str
    app_version: str
    git_commit: str | None
    config_hash: str
    model_manifest_hash: str
    status: Literal["running", "completed", "crashed", "aborted"]
```

session 시작 직후 DB에 row를 만든다.

정상 종료 시 `completed`.

프로세스 재시작 때 `running`으로 남아 있는 session은 `crashed`로 마킹한다.

---

# 6. Capture layer

## 6.1 Interface

```python
class FrameSource(Protocol):
    def start(self) -> None: ...
    def frames(self) -> Iterator[Frame]: ...
    def stop(self) -> None: ...
```

## 6.2 Screen source

조건:

- screen/window의 rendered pixels만 사용
- PUBG process handle이 필요한 방식 사용 금지
- injection/hook 금지

구현은 capture backend를 별도 module로 감싼다.

### Failure detection

다음 경우 `CaptureHealth`를 degraded/error로 변경한다.

- 연속 N frame이 완전 black
- frame timestamp가 멈춤
- 해상도가 비정상적으로 0 또는 급변
- backend exception

black frame을 정상 empty killfeed로 취급하면 안 된다.

---

## 6.3 Replay source

입력:

- mp4/mkv 등 local file

옵션:

```bash
kfocr replay match.mp4 --realtime
kfocr replay match.mp4 --max-speed
```

`--max-speed`에서도 frame timestamp는 원본 video timeline 기준으로 유지한다.

---

# 7. ROI

## 7.1 P0 strategy

기본 killfeed region을 normalized coordinate로 둔다.

실제 기본값은 fixture 관찰 후 calibration tool로 확정하고 코드에 magic pixel 좌표를 박지 않는다.

예시 config:

```yaml
roi:
  profile: pubg-default-v1
  rect:
    x: 0.64
    y: 0.03
    w: 0.35
    h: 0.30
```

위 값은 예시이며 golden fixture로 검증 후 결정한다.

## 7.2 Calibration

```bash
kfocr calibrate
```

동작:

1. 현재 screen frame 표시
2. 사용자가 killfeed 영역 drag/select
3. normalized rect 저장
4. profile에 resolution/UI scale metadata 기록

사용자 drag가 어려운 환경을 위해 keyboard nudge 또는 numeric edit도 제공한다.

## 7.3 P1 auto-location

- stable text/icon geometry
- top-right prior
- template/feature matching

자동 검출 실패 시 silent fallback하지 말고 calibrated profile을 요구한다.

---

# 8. Change detection

목적은 OCR 비용 절감이다.

## 8.1 입력

연속 ROI image.

## 8.2 P0

다음 조합 중 단순하고 testable한 방법을 선택한다.

- grayscale absolute difference
- edge map difference
- text-likelihood masked difference

게임 scene background가 계속 변하므로 전체 pixel MAE만으로 판단하지 않는다.

## 8.3 Contract

```python
class ChangeDetector(Protocol):
    def score(self, previous: np.ndarray, current: np.ndarray) -> float: ...
    def should_infer(self, score: float) -> bool: ...
```

### Safety valve

최대 500ms 이상 OCR inference가 한 번도 실행되지 않았다면 periodic refresh를 허용한다.

killfeed가 매우 미세하게 변해 threshold를 놓치는 경우를 방지한다.

---

# 9. Preprocessing

파이프라인은 fixture benchmark로 선택한다.

후보:

- local contrast normalization
- grayscale
- adaptive threshold는 optional
- scale-up
- mild sharpening

주의:

killfeed text color/team color/icon color가 정보일 수 있으므로 원본 color crop도 함께 유지한다.

preprocessing이 원본을 파괴하지 않도록 pure function으로 구현한다.

---

# 10. Text recognition

## 10.1 Interface

```python
class TextRecognizer(Protocol):
    def recognize(self, image: np.ndarray) -> list[OCRToken]: ...
```

## 10.2 Nickname policy

nickname은 dictionary word로 가정하지 않는다.

post-correction pipeline:

```text
raw OCR
  ↓
safe normalization
  ↓
candidate roster가 있으면 fuzzy matching
  ↓
confidence-adjusted canonical nickname
```

### Safe normalization

허용:

- 명백한 surrounding whitespace 제거
- OCR engine이 별도 token으로 준 UI separator 제거

금지:

- 임의 spell correction
- 일반 영어 사전 기반 교정

### Fuzzy correction

roster가 존재할 때만 적용 가능.

다음 조건 모두 만족해야 canonicalize:

- best distance가 threshold 이하
- second-best와 충분한 margin
- OCR confidence와 결합 confidence가 threshold 이상

그 외에는 raw OCR을 유지한다.

---

# 11. Icon recognition

## 11.1 MVP contract

```python
class IconRecognizer(Protocol):
    def detect(self, image: np.ndarray) -> list[VisualElement]: ...
```

MVP class:

- `kill_marker`
- `knock_marker`
- `unknown_icon`

프로젝트 초기에 weapon 전체 분류를 하지 않는다.

## 11.2 P1

- headshot
- weapon family
- environment
- finish semantics

새 PUBG patch 후 confidence가 급락하면 unknown으로 degrade해야 한다.

---

# 12. Row assembly

OCR token과 icon detection 결과를 spatially clustering해 row를 만든다.

## 12.1 기본 알고리즘

1. element의 vertical center 계산
2. y-overlap/center-distance로 row cluster
3. row 내 element를 x-coordinate로 sort
4. geometry pattern으로 killer/victim 후보 결정
5. row confidence 계산

## 12.2 Parser contract

parser는 “OCR string 전체를 regex 하나로 처리”하면 안 된다.

```python
class EventParser(Protocol):
    def parse(self, row: KillfeedRowCandidate) -> CandidateEvent | None: ...
```

필수 조건:

- killer와 victim 둘 중 하나를 읽지 못하면 confidence 감소
- 양쪽 이름이 모두 없는 row는 confirmed event 금지
- event type 확신이 없으면 `UNKNOWN`
- unknown을 kill로 강제 변환 금지

---

# 13. Temporal tracker / dedup

이 모듈은 P0 핵심이다.

## 13.1 문제

killfeed row는 수 초간 화면에 남고 위로 이동한다.

frame마다 OCR하면 동일 kill이 여러 event로 기록된다.

## 13.2 Matching feature

기존 pending/confirmed row와 신규 row의 similarity:

- killer string similarity
- victim string similarity
- event type
- relative geometry
- crop perceptual hash
- temporal distance

position exact-match는 사용하지 않는다. row scroll 때문이다.

## 13.3 Confirmation

기본 policy 예:

```text
window: 최근 750ms
min_evidence_frames: 2
max_gap: 500ms
```

동일 후보가 충분히 반복 관측되면 confirmed.

고 confidence single-shot 이벤트를 허용할지는 benchmark 후 결정하며 기본은 OFF.

## 13.4 Dedup key

canonical string 자체만 hash하지 않는다.

동일 플레이어가 짧은 시간 안에 같은 victim-like OCR 오인식을 만들 수 있기 때문이다.

권장:

```text
normalized killer
normalized victim
event type
time bucket
visual fingerprint
```

---

# 14. Confidence model

P0에서는 learned meta-model 대신 명시적 weighted score를 사용해 디버깅 가능성을 유지한다.

예:

```python
total = (
    0.20 * row_conf +
    0.30 * killer_conf +
    0.30 * victim_conf +
    0.10 * type_conf +
    0.10 * temporal_conf
)
```

실제 weight/threshold는 validation fixture로 tune한다.

threshold 종류:

```yaml
confidence:
  confirmed_min: 0.88
  candidate_min: 0.65
```

중요:

- 수치는 초기 예시
- holdout를 보고 고정
- test set에 맞춰 반복 조정 금지

---

# 15. Storage

SQLite file 예:

```text
data/kfocr.sqlite3
```

## 15.1 Tables

### `sessions`

```sql
CREATE TABLE sessions (
  session_id TEXT PRIMARY KEY,
  source_type TEXT NOT NULL,
  started_at_utc TEXT NOT NULL,
  ended_at_utc TEXT,
  width INTEGER NOT NULL,
  height INTEGER NOT NULL,
  roi_profile_id TEXT NOT NULL,
  app_version TEXT NOT NULL,
  git_commit TEXT,
  config_hash TEXT NOT NULL,
  model_manifest_hash TEXT NOT NULL,
  status TEXT NOT NULL
);
```

### `events`

```sql
CREATE TABLE events (
  event_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  observed_at_ns INTEGER NOT NULL,
  wall_time_utc TEXT,
  killer_name_raw TEXT,
  victim_name_raw TEXT,
  killer_name TEXT,
  victim_name TEXT,
  killer_team INTEGER,
  victim_team INTEGER,
  event_type TEXT NOT NULL,
  weapon_label TEXT,
  headshot INTEGER,
  confidence_total REAL NOT NULL,
  confidence_killer REAL,
  confidence_victim REAL,
  confidence_event_type REAL,
  source_bbox_json TEXT NOT NULL,
  source_crop_ref TEXT,
  tracker_evidence_count INTEGER NOT NULL,
  FOREIGN KEY(session_id) REFERENCES sessions(session_id)
);
```

### `validation_matches`

```sql
CREATE TABLE validation_matches (
  event_id TEXT NOT NULL,
  telemetry_event_id TEXT,
  telemetry_type TEXT,
  match_score REAL,
  result TEXT NOT NULL,
  detail_json TEXT,
  PRIMARY KEY(event_id, telemetry_event_id)
);
```

## 15.2 DB settings

- WAL
- foreign keys ON
- short transactions
- event confirm 시 즉시 commit 또는 bounded batch
- shutdown hook에서 session finalize

---

# 16. Debug artifact storage

기본:

```yaml
debug:
  save_full_frames: false
  save_event_crops: false
```

enable된 경우:

```text
data/sessions/<session-id>/crops/<event-id>.png
```

retention policy를 설정 가능하게 한다.

전체 화면은 민감 정보가 포함될 수 있으므로 기본 저장 금지.

---

# 17. Sidecar output

MVP는 overlay가 아니라 독립 process UI다.

최소 인터페이스:

```python
class EventSink(Protocol):
    def publish(self, event: ConfirmedEvent) -> None: ...
```

implementations:

- `ConsoleSink`
- `JsonlSink`
- `SidecarWindowSink` (P1 가능)

UI는 다음만 표시:

- timestamp
- killer
- victim
- type
- optional team
- confidence/debug mode

전술적 요약은 금지.

---

# 18. PUBG API client

## 18.1 Secret handling

API key는 repository/config yaml에 저장하지 않는다.

지원 순서:

1. environment variable `PUBG_API_KEY`
2. P1: Windows Credential Manager

`.env`는 local development에서 사용할 수 있지만 `.gitignore`에 포함.

로그에 Authorization header 출력 금지.

---

## 18.2 API client contract

```python
class PubgApiClient:
    def get_player(self, shard: str, player_name: str) -> dict: ...
    def get_match(self, shard: str, match_id: str) -> dict: ...
    def get_telemetry(self, telemetry_url: str) -> list[dict]: ...
```

HTTP:

- timeout
- retry only for retryable errors
- 429 존중
- exponential backoff + jitter
- max retry budget
- user-agent 명시

무한 polling 금지.

---

# 19. Session-to-match matching

이 부분은 자동 validator의 핵심이다.

## 19.1 Inputs

- local session start/end UTC
- configured player name/account if resolvable
- candidate match IDs
- match start time/duration
- participant names

## 19.2 Candidate score

예:

```text
+ timestamp overlap
+ configured player included
+ duration plausibility
+ session started near match
```

## 19.3 Decision

```yaml
match_finder:
  auto_accept_min: 0.90
  ambiguous_min: 0.70
```

- `>= auto_accept_min`: 자동 선택
- ambiguous: 사용자 선택 또는 미확정
- 낮음: validation 중단

잘못된 match를 정답으로 사용하느니 validation을 실패시키는 것이 낫다.

---

# 20. Telemetry mapping

## 20.1 Source events

### Kill/death

`LogPlayerKillV2`

중요 fields:

- `victim`
- `dBNOMaker`
- `finisher`
- `killer`
- `killerDamageInfo`
- `finishDamageInfo`
- `isSuicide`
- `_D`

### Knock

`LogPlayerMakeGroggy`

중요 fields:

- `attacker`
- `victim`
- damage fields
- `dBNOId`
- `_D`

---

## 20.2 Semantic rule

**절대로** `killer == screen left name`이라고 하드코딩하지 않는다.

PUBG의 UI killfeed semantics는 knock/finish/teamkill/suicide 상황에서 telemetry fields와 다르게 표현될 수 있다.

따라서 `TelemetryCanonicalEvent`로 변환한다.

```python
@dataclass
class TelemetryCanonicalEvent:
    occurred_at_utc: datetime
    event_type: EventType
    actor_candidates: list[str]
    victim: str | None
    dBNO_maker: str | None
    finisher: str | None
    killer: str | None
    raw_type: str
    raw_index: int
```

UI와 field mapping이 fixture로 확인된 case만 strict-match한다.

ambiguous case는 validator에서 `UNRESOLVED_SEMANTICS`.

---

# 21. Ground-truth alignment

## 21.1 First pass

candidate pair 생성:

- victim exact/fuzzy match
- actor candidate match
- event type compatibility
- time distance

## 21.2 Assignment

한 telemetry event를 여러 OCR event에 중복 매칭하지 않는다.

최소-cost bipartite matching 또는 greedy-with-guard를 사용할 수 있다.

P0에서 greedy를 쓰더라도 test로 collision case를 포함한다.

## 21.3 Time tolerance

capture wall clock과 telemetry `_D`의 clock offset이 있을 수 있다.

session 단위 offset estimation:

1. 여러 고-confidence event pair 후보 수집
2. median timestamp delta 추정
3. outlier 제거
4. corrected timeline으로 재정렬

단일 event로 clock offset을 확정하지 않는다.

---

# 22. Validation metrics

## 22.1 Event-level

- TP / FP / FN
- precision
- recall
- F1

## 22.2 Field-level

- killer exact accuracy
- victim exact accuracy
- pair exact accuracy
- event type accuracy
- team number accuracy(P1)
- icon accuracy(P1)

## 22.3 Tracking

- duplicate rate
- merge error rate
- split error rate

## 22.4 Confidence calibration

confidence bucket별 실제 accuracy를 report한다.

예:

```text
0.90–1.00: 98.8%
0.80–0.90: 91.2%
0.70–0.80: 74.1%
```

confidence가 의미 있는 수치인지 검증한다.

---

# 23. Benchmark protocol

## 23.1 Dataset manifest

각 fixture는 manifest를 가진다.

```yaml
id: match-001
video: match-001.mp4
resolution: [2560, 1440]
ui_scale: 100
locale: en
duration_sec: 1800
split: holdout
notes: burst kills, dark scene
```

## 23.2 Split

- dev/tuning
- regression
- holdout

holdout는 threshold/model 선택 후 반복적으로 tuning에 사용하지 않는다.

## 23.3 Required matrix

RC 전에 최소:

- 1080p + 1440p
- UI scale 2종
- 밝은/어두운 배경
- slow feed + burst feed
- 최소 100 confirmed kill/knock events

표본이 부족하면 metric에 confidence interval 또는 sample count를 명시한다.

---

# 24. CLI specification

## 24.1 `kfocr replay`

```bash
kfocr replay VIDEO \
  [--roi-profile PROFILE] \
  [--max-speed] \
  [--save-crops]
```

## 24.2 `kfocr run`

```bash
kfocr run \
  [--monitor N] \
  [--roi-profile PROFILE] \
  [--console]
```

`run`은 OS screen capture만 사용한다.

## 24.3 `kfocr calibrate`

```bash
kfocr calibrate [--monitor N]
```

## 24.4 `kfocr validate`

```bash
kfocr validate SESSION_ID \
  --shard steam \
  --player PLAYER_NAME
```

API key는 command argument로 받지 않는다. shell history 노출 방지.

## 24.5 `kfocr report`

```bash
kfocr report SESSION_ID --format text
kfocr report SESSION_ID --format json
```

---

# 25. Config

```yaml
capture:
  sample_fps: 12
  inference_fps_cap: 4
  monitor: 0

roi:
  profile: pubg-default-v1

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

telemetry:
  enabled: true
  api_key_env: PUBG_API_KEY
  poll_interval_sec: 30
  max_wait_sec: 900
```

수치는 initial defaults이며 benchmark 후 config version과 함께 고정한다.

---

# 26. Logging / observability

structured log 권장.

event examples:

```text
capture.started
capture.degraded
roi.changed
ocr.inference
tracker.confirmed
tracker.rejected
storage.error
telemetry.match_candidate
telemetry.matched
validation.completed
```

로그에 포함 금지:

- PUBG API Authorization
- full screenshots
- unnecessary player data dump

performance counter:

- capture FPS
- inference FPS
- OCR p50/p95 latency
- queue depth
- confirmed events
- candidate reject count
- DB write latency

---

# 27. Concurrency model

추천:

```text
Capture Thread
   ↓ bounded latest-frame queue
Vision Worker
   ↓ candidate queue
Tracker/Domain Worker
   ↓
Store + Sink
```

원칙:

- queue는 bounded
- backlog 시 오래된 frame을 버리고 최신 frame 우선
- killfeed analysis가 game capture 지연을 누적시키지 않음
- DB/UI failure가 capture thread를 block하지 않음

MVP는 process 1개 + threads/tasks로 충분하다.

---

# 28. Error behavior

## Capture failure

- viewer에 degraded 표시
- event 생성 중지
- 재연결 시도
- black frame을 no-event로 기록하지 않음

## OCR failure

- 해당 inference candidate drop
- pipeline 계속
- error counter 증가

## DB failure

- confirmed event를 메모리 bounded buffer에 잠시 유지
- persistent failure 시 loud failure
- 무한 메모리 축적 금지

## Telemetry failure

- live capture 결과에는 영향 없음
- validation status만 `pending/error`
- retry budget 초과 시 종료

---

# 29. Test plan

## 29.1 Unit

- normalized ROI conversion
- change detector thresholds
- text normalization
- fuzzy roster matching margin
- row clustering
- parser
- confidence
- dedup
- timestamp offset estimation
- telemetry semantic mapping
- session matcher scoring

## 29.2 Integration

- video → confirmed events
- crash/restart → DB recovery
- mock PUBG API → telemetry validator
- repeated row scrolling → single event
- burst 3 rows → 3 distinct events

## 29.3 Regression fixtures

버그가 발견될 때마다 최소 crop/video fixture 추가.

원칙:

**fix without fixture is incomplete.**

## 29.4 Prohibited-capability audit

CI/static review checklist:

- `ReadProcessMemory`
- packet capture libraries
- injection/hooking libs
- input automation libs
- kernel/device access added 여부

의도치 않은 dependency 추가를 막는다.

---

# 30. Acceptance tests

### AT-001 Basic kill

Given recorded fixture에 `AAAA → BBBB` kill row가 나타남  
When pipeline 실행  
Then confirmed event 정확히 1개  
And killer=`AAAA`, victim=`BBBB`

### AT-002 Persistent row

Given 동일 row가 4초 동안 여러 frame에 존재  
Then confirmed event는 1개만 생성

### AT-003 Scroll

Given 새로운 kill로 기존 row가 위로 이동  
Then 기존 event가 새 event로 중복 생성되지 않음

### AT-004 Burst

Given 500ms 이내 서로 다른 3개 row 등장  
Then 3개 event가 각각 생성

### AT-005 Low confidence

Given victim nickname이 읽히지 않음  
Then confirmed event threshold 미달이면 live confirmed log에 넣지 않음

### AT-006 Capture black frame

Given capture backend가 black frames 반환  
Then system은 degraded 상태  
And no false events generated

### AT-007 Telemetry alignment

Given matching `LogPlayerKillV2` fixture  
Then OCR event가 ground truth에 1:1 match

### AT-008 Ambiguous finisher

Given dBNOMaker와 finisher가 다름  
Then validator는 명시된 semantic rule 없이는 actor를 강제 결정하지 않음

### AT-009 Secret safety

Given debug logging enabled  
Then API key가 log/output에 나타나지 않음

### AT-010 No forbidden integration

Code audit에서 process memory/packet/hook/input automation path가 없음

---

# 31. Performance test

측정 대상 PC를 기록한다.

최소 report:

```text
CPU:
GPU:
Resolution:
Capture backend:
OCR engine:

capture_fps:
inference_fps:
OCR latency p50/p95:
end-to-end confirmation p50/p95:
CPU avg/p95:
RAM avg/peak:
GPU avg/peak:
```

게임과 동시에 실행한 성능은 recorded benchmark와 별도로 기록한다.

---

# 32. Model/template versioning

`models/manifest.json` 예:

```json
{
  "manifest_version": 1,
  "ocr": {
    "engine": "ENGINE_NAME",
    "version": "X.Y.Z",
    "checksum": "sha256:..."
  },
  "icons": {
    "model": "pubg-killfeed-icons-v1",
    "checksum": "sha256:..."
  }
}
```

report에 manifest hash 기록.

PUBG patch 후 model/template이 바뀌면 version 증가.

---

# 33. Optional Overwolf adapter

MVP blocker가 아니다.

사용 목적은 **공식적으로 제공되는 데이터만 보조적으로 활용**하는 것.

가능한 활용:

- roster nickname candidate set
- local team names
- match phase

사용 금지 해석:

Overwolf 데이터를 통해 화면에 보이지 않은 tactical 정보까지 viewer에 추가하지 않는다.

interface:

```python
class RosterProvider(Protocol):
    def current_names(self) -> list[str]: ...
```

core OCR pipeline은 Overwolf 없이 완전히 동작해야 한다.

---

# 34. Packaging

MVP 개발:

```bash
python -m kfocr ...
```

P1:

- Windows executable packaging 검토
- model assets checksum
- config/data directory 명확히 분리

installer가 PUBG directory에 파일을 설치하지 않도록 한다.

---

# 35. Security / privacy

- local-only by default
- no telemetry upload to project server
- no analytics by default
- no auto-update executable download in MVP
- API key secret
- debug crops opt-in
- filename/path에 API key 포함 금지
- untrusted video file parsing은 library security updates를 따른다

---

# 36. 구현 순서

## Step 1 — skeleton

- package
- config
- domain schema
- SQLite
- CLI
- tests

## Step 2 — replay MVP

- video frame source
- manual ROI
- OCR adapter
- row assembly
- parser
- console events

## Step 3 — tracker

- temporal evidence
- scrolling dedup
- confidence gating

## Step 4 — benchmark

- fixture manifest
- dev/regression/holdout
- metrics

## Step 5 — telemetry validator

- PUBG client
- session matcher
- canonical event mapper
- alignment
- report

## Step 6 — Windows live source

- screen capture adapter
- health detection
- calibration
- resource profiling

## Step 7 — sidecar

- minimal external viewer
- no injected overlay

## Step 8 — hardening

- crash recovery
- logs
- packaging
- policy warning

---

# 37. Definition of Done

다음이 모두 충족되어야 MVP를 완료로 본다.

- [ ] replay pipeline 구현
- [ ] live OS screen capture 구현
- [ ] calibrated ROI
- [ ] killer/victim extraction
- [ ] kill/knock/unknown
- [ ] temporal dedup
- [ ] SQLite persistent sessions/events
- [ ] official Telemetry validator
- [ ] report generation
- [ ] holdout F1 >= 0.95
- [ ] pair exact >= 0.97
- [ ] duplicate rate < 0.5%
- [ ] black-frame false event 0
- [ ] secrets safe
- [ ] no forbidden integration
- [ ] regression fixtures
- [ ] README policy disclaimer

---

# 38. 남은 Low 이슈

현재 adversarial review 이후 High/Medium open issue는 없다.

### LOW-001

ultrawide/4K와 모든 PUBG UI scale에 대한 fully automatic ROI detection은 P1 이후 계속 보강 필요.

### LOW-002

희귀 weapon/icon 및 patch 직후 신규 icon은 `unknown`으로 떨어질 수 있음.

### LOW-003

spectator/replay/custom mode는 live normal/ranked UI와 killfeed layout이 다를 수 있음.

### LOW-004

한국어/영어 이외 locale-specific UI 요소는 별도 fixture가 필요할 수 있음.

이 이슈들은 core `killer → victim` MVP 구현을 막지 않는다.

---

# 39. 공식 근거 링크

PUBG Telemetry Events:

https://documentation.pubg.com/en/telemetry-events.html

PUBG Telemetry retrieval:

https://documentation.pubg.com/en/telemetry.html

Overwolf PUBG GEP:

https://dev.overwolf.com/ow-native/live-game-data-gep/supported-games/playerunknowns-battlegrounds/

Overwolf GEP overview:

https://dev.overwolf.com/ow-native/live-game-data-gep/live-game-data-gep-intro/

Overwolf deprecated PUBG events:

https://dev.overwolf.com/ow-native/live-game-data-gep/supported-games/deprecated/overview/

PUBG Rules of Conduct:

https://www.pubg.com/en-asia/clause/rules_of_conduct

PUBG Anti-Cheat Dev Letter, 2026-06-25:

https://pubg.com/en/news/10338?category=dev_notes
