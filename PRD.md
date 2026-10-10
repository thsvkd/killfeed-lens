# PRD.md — PUBG Killfeed OCR/CV

> Status: Final after adversarial review  
> Date: 2026-10-06 (KST)  
> Working name: `pubg-killfeed-ocr`  
> Primary platform: Windows PC / PUBG: BATTLEGROUNDS  
> Document language: Korean, identifiers/interfaces in English

---

## 1. 한 줄 정의

PUBG 플레이 화면에 **이미 표시된 killfeed(킬피드) 픽셀만 OS 수준에서 캡처**하여, `killer → victim` 관계와 이벤트 종류를 실시간으로 구조화·기록하고, 경기 종료 후 PUBG 공식 Telemetry를 정답 데이터(ground truth)로 사용해 정확도를 자동 검증하는 로컬 프로그램을 만든다.

---

## 2. 문제 정의

PUBG에서 경기 후에는 공식 Telemetry의 `LogPlayerKillV2`, `LogPlayerMakeGroggy` 등으로 정확한 killer/victim 관계를 얻을 수 있다. 그러나 공개된 공식 실시간 API만으로는 경기 전체의 killfeed와 동일한 `killer → victim` 관계를 실시간으로 얻을 수 없다.

Overwolf GEP(Game Events Provider)는 PUBG의 일부 실시간 정보를 제공하지만:

- `kill` 이벤트는 local player의 kill 발생 사실만 제공하고 victim 이름을 제공하지 않는다.
- `killer` 이벤트는 local player가 사망했을 때 killer nickname을 제공한다.
- `roster`는 전체 플레이어와 `out` 상태를 제공하지만, 각 플레이어의 `kills`는 그 플레이어가 match를 떠날 때 갱신된다.
- 따라서 전체 경기의 `A → B`, `C → D` 형태 killfeed 관계를 실시간 복원할 수 없다.

반면 실제 PUBG 화면의 killfeed에는 해당 정보가 시각적으로 노출된다.

따라서 본 프로젝트의 제품적·기술적 가치는 다음 공백을 메우는 데 있다.

| 데이터 소스 | 실시간 | 전체 killer→victim 관계 | 목적 |
|---|---:|---:|---|
| PUBG 공식 Telemetry | 아니오 | 예 | 사후 정답/검증 |
| Overwolf PUBG GEP | 예 | 아니오 | 선택적 보조 정보 |
| 화면 killfeed | 예 | 예(화면에 보인 범위) | 본 프로젝트의 핵심 입력 |

---

## 3. 제품 목표

### P0 — MVP 목표

1. PUBG 화면 우상단 killfeed에서 새 이벤트를 감지한다.
2. 최소한 다음 필드를 구조화한다.
   - `observed_at`
   - `killer_name`
   - `victim_name`
   - `event_type`: `kill | knock | unknown`
   - `confidence`
3. 같은 killfeed row가 여러 프레임에 남아 있어도 이벤트를 한 번만 기록한다.
4. 외부 sidecar UI/CLI에서 시간순 event history를 보여준다.
5. 경기 종료 후 공식 PUBG Telemetry와 자동 대조하여 end-to-end 정확도를 측정한다.
6. 게임 프로세스, 메모리, 패킷, 입력 시스템, anti-cheat에 접근하지 않는다.

### P1 — 안정화 목표

- team number 추출
- headshot/weapon/icon 분류
- `finish`, `bleedout`, `suicide`, `zone`, `vehicle` 등 이벤트 세분화
- 다양한 해상도/UI scale 지원
- roster-aware nickname correction
- session 자동 매칭 및 validation report
- 녹화영상 replay mode

### P2 — 선택적 확장

- Overwolf GEP를 **승인된 보조 데이터 소스**로 연결
- 로컬 웹/데스크톱 viewer
- 자동 calibration 개선
- 모델/템플릿 업데이트 체계

---

## 4. 비목표 / 금지 범위

이 섹션은 구현 편의보다 우선한다.

### 명시적으로 구현하지 않는다

- PUBG 프로세스 handle 획득
- `ReadProcessMemory` 또는 이에 준하는 메모리 읽기
- DLL injection / code injection / hooks into PUBG
- kernel driver / DMA / anti-cheat 우회
- 게임-서버 패킷 sniffing, interception, modification
- 입력 자동화, recoil 제어, aim assist, macro
- 적 위치 추론 또는 표시
- 적 장비/상태 등 화면에 없던 비공개 정보 표시
- 자동 전술 추천
- push/hold/rotate 추천
- 팀별 전력 점수 또는 위협도 계산
- 생존 인원 기반 실시간 전략 제안
- 게임 내부 UI 변경
- PUBG 위에 직접 주입되는 in-game overlay

### MVP 데이터 원칙: Visual Parity

MVP가 실시간으로 보여주는 정보는 **현재 또는 과거에 PUBG killfeed 화면에 실제 표시되어 프로그램이 픽셀로 관측한 정보**로 한정한다.

사후 Telemetry는 정확도 평가와 교정에만 사용하며, 경기 중 화면에 나오지 않은 정보를 live viewer에 역주입하지 않는다.

---

## 5. 정책/공정성 가드레일

2026-06-25 PUBG Anti-Cheat Dev Letter는 PUBG가 공식 제공하거나 허용하지 않은 third-party program의 사용이 제재될 수 있으며, 특히 게임이 제공하지 않는 정보 표시, 비인가 UI 변경, client/process의 비정상적 데이터 접근을 문제로 명시한다.

따라서:

1. 본 프로젝트는 기술적으로 OS-level screen pixels만 입력으로 사용한다.
2. 하지만 이것이 곧 PUBG의 명시적 사용 승인을 뜻하지는 않는다.
3. **개발/검증 단계는 녹화영상, replay, custom match를 우선한다.**
4. 일반전/랭크 실시간 사용을 배포 기본값으로 만들지 않는다.
5. live usage를 제품화/배포하기 전 KRAFTON/PUBG의 서면 정책 확인을 별도 gate로 둔다.
6. README와 UI에 “PUBG/KRAFTON의 공식 승인 제품이 아님”을 명시한다.

### Release gate

`RANKED_LIVE_USAGE_APPROVED=false`를 기본 정책 상태로 본다.

정책 승인 근거가 확보되기 전에는 문서/릴리스 노트에서 “safe”, “allowed”, “approved”라고 단정하지 않는다.

---

## 6. 사용자

### Primary user

PUBG PC 플레이어 중:

- 빠르게 사라지는 killfeed 기록을 놓치지 않고 싶다.
- 경기 중 표시된 정보만 다시 확인하고 싶다.
- 자신의 OCR/CV 시스템을 개발·평가하고 싶다.

### Secondary user

- PUBG 영상 분석 도구 개발자
- OCR/CV 실험자
- 경기 분석 연구자

---

## 7. 핵심 사용자 시나리오

### Scenario A — 녹화영상 개발 모드

1. 사용자가 PUBG 녹화영상을 입력한다.
2. 시스템이 killfeed ROI를 읽는다.
3. 이벤트를 구조화한다.
4. JSONL/SQLite에 저장한다.
5. 사람이 결과를 검토하거나 공식 Telemetry와 비교한다.

이 모드는 최초 개발/회귀 테스트의 기본 모드다.

### Scenario B — 실시간 capture

1. 사용자가 프로그램을 실행한다.
2. 프로그램이 OS screen capture를 시작한다.
3. 우상단 killfeed 변화만 추적한다.
4. 새로운 row가 안정적으로 검출되면 event를 append한다.
5. 외부 viewer 또는 terminal에 event history를 표시한다.

### Scenario C — 경기 종료 후 자동 검증

1. 사용자가 PUBG API key, platform, player identity를 설정한다.
2. capture session이 종료된다.
3. 시스템이 최신 match를 찾아 session과 매칭한다.
4. Telemetry의 kill/knock 이벤트를 가져온다.
5. OCR events와 시간/이름/유형을 정렬한다.
6. precision/recall/F1, name accuracy, latency proxy, mismatch 사례를 report한다.

---

## 8. MVP UX

### 실행

```bash
kfocr run --source screen
```

표시 예:

```text
03:14.231  [KILL]   [12] AAAA -> [08] BBBB   conf=0.98
03:18.602  [KNOCK]  [04] CCCC -> [15] DDDD   conf=0.93
03:23.117  [KILL]   [12] AAAA -> [03] EEEE   conf=0.97
```

### 녹화영상 테스트

```bash
kfocr replay ./fixtures/match-001.mp4
```

### 검증

```bash
kfocr validate <session-id>
```

### 리포트

```bash
kfocr report <session-id>
```

---

## 9. 기능 요구사항

### FR-001 Screen capture

- OS-level screen capture만 사용한다.
- PUBG process memory 또는 packet에 접근하지 않는다.
- ROI crop은 capture 후 별도 단계에서 수행한다.
- capture가 black frame/permission failure인 경우 명확히 실패 상태를 표시한다.

### FR-002 ROI calibration

고정 pixel 좌표만 사용하지 않는다.

MVP는 다음을 지원한다.

1. normalized default ROI
2. user-assisted calibration
3. config로 ROI override

P1에서 자동 anchor detection을 추가한다.

### FR-003 Change detection

- 매 frame OCR하지 않는다.
- killfeed ROI의 변화가 threshold를 넘는 경우에만 OCR pipeline을 활성화한다.
- 순간적인 scene background 변화에 과도하게 반응하지 않도록 text/edge 중심 특징 또는 masked difference를 사용한다.

### FR-004 Text recognition

MVP 필수:

- killer nickname
- victim nickname

P1:

- team number
- 기타 텍스트 요소

OCR engine은 interface 뒤에 숨겨 교체 가능하게 만든다.

### FR-005 Visual/icon classification

MVP:

- `kill`
- `knock`
- `unknown`

P1:

- headshot
- weapon/icon family
- finish/bleedout/suicide/environment semantics

모델이 확신하지 못하면 추측하지 않고 `unknown`을 반환한다.

### FR-006 Row assembly

텍스트 box와 icon box의 화면 좌표를 이용해 한 killfeed row를 구성한다.

parser가 단순 OCR 문자열 순서에 의존하지 않도록 한다.

### FR-007 Temporal tracking / deduplication

- 같은 row가 여러 frame에 노출되어도 event는 한 번만 emit한다.
- row가 scroll/reflow되어 위치가 변해도 동일 event로 추적해야 한다.
- 2-of-3 또는 동등한 temporal stability rule을 적용한다.
- 서로 다른 두 kill이 매우 짧은 시간 간격으로 연속 발생해도 합치지 않는다.

### FR-008 Confidence gating

event confidence는 최소한 다음 하위 confidence를 고려한다.

- row detection
- killer OCR
- victim OCR
- event/icon classification
- temporal stability

threshold 미만은:

- live confirmed log에 넣지 않음
- optional `candidate` store에 저장 가능

### FR-009 Local event store

- SQLite 사용
- append-oriented
- WAL mode
- crash 후 session recovery 가능
- raw frame 전체 저장은 기본 비활성
- event crop 저장도 opt-in

### FR-010 Sidecar viewer

MVP는 PUBG client에 injection되는 overlay를 만들지 않는다.

허용 UI:

- terminal/TUI
- 독립 desktop window
- local-only viewer

실시간 viewer가 표시하는 내용은 observed killfeed history와 confidence에 한정한다.

### FR-011 Replay mode

file/video source를 screen source와 동일한 downstream pipeline에 연결한다.

동일 test fixture로 regression test가 가능해야 한다.

### FR-012 PUBG Telemetry validator

다음 event를 사용한다.

- `LogPlayerKillV2`
- `LogPlayerMakeGroggy`

필요 시:

- `LogMatchStart`
- `LogMatchEnd`
- participant data

검증기는 game UI 의미와 telemetry field 의미가 완전히 같다고 가정하지 않는다.

특히 `killer`, `finisher`, `dBNOMaker`, victim death의 관계를 별도 mapping rule로 정의한다.

### FR-013 Session-to-match matcher

자동 매칭 입력:

- configured player
- platform/shard
- session start/end timestamp
- candidate match timestamps
- participant membership

매칭 confidence가 낮으면 자동 확정하지 않는다.

### FR-014 Validation report

최소 지표:

- event precision
- event recall
- event F1
- killer exact-match accuracy
- victim exact-match accuracy
- event type accuracy
- duplicate rate
- false-event rate
- unmatched OCR events
- unmatched telemetry events

### FR-015 Data export

지원:

- JSONL
- CSV
- SQLite query

---

## 10. 비기능 요구사항

### NFR-001 성능

MVP 목표:

- capture sampling: 8–15 FPS
- expensive OCR inference: adaptive, 최대 4 FPS 수준
- event detection latency p95: 1.0초 이하
- target: 0.75초 이하
- game FPS에 체감 가능한 영향을 주지 않는 것을 목표로 한다.

정확한 CPU/GPU budget은 benchmark를 통해 baseline을 확정한다.

### NFR-002 정확도

최초 release candidate의 목표:

- end-to-end event F1 ≥ 0.95
- recognized event의 killer/victim pair exact-match ≥ 0.97
- false confirmed event ≤ 1 / 30 match-minutes
- duplicate confirmed event rate < 0.5%

단, 수치는 반드시 holdout dataset에서 측정한다.

### NFR-003 복원력

- capture 일시 실패 시 process restart 없이 복구 시도
- OCR engine failure 격리
- SQLite transaction 안정성
- session metadata flush
- crash 후 incomplete session 표시

### NFR-004 Privacy

- 처리 기본값: local-only
- cloud OCR 금지
- screenshot/video 자동 업로드 금지
- full-frame 저장 기본 OFF
- crop 저장 기본 OFF
- debug artifact에 player names가 포함될 수 있음을 명시

### NFR-005 Reproducibility

report에는 다음을 기록한다.

- app version/git commit
- model identifier/checksum
- config hash
- screen resolution
- UI scale/calibration profile
- OCR engine/version
- platform
- benchmark dataset version

---

## 11. 정확도 평가 데이터셋

단일 조건에서만 95%를 달성하는 것은 성공으로 간주하지 않는다.

최소 stratification:

- 1920×1080
- 2560×1440
- 밝은/어두운 scene background
- 연속 killfeed burst
- 영어/숫자/underscore 등 nickname 변형
- kill
- knock
- suicide/environmental edge case가 포함된 fixture
- UI scale 최소 2종

P1:

- ultrawide
- 4K
- colorblind/UI 설정 변형
- spectator/replay/custom modes

Train/tuning fixture와 final holdout fixture를 분리한다.

---

## 12. 성공 기준

MVP 완료 조건은 “화면에 뭔가 찍힌다”가 아니다.

다음을 모두 만족해야 한다.

1. recorded fixture 30분 이상에서 pipeline이 안정 동작
2. end-to-end F1 ≥ 0.95
3. killer/victim pair exact-match ≥ 0.97
4. duplicate rate < 0.5%
5. live screen capture path와 replay path가 동일 parser/tracker를 사용
6. official Telemetry validator가 자동 report 생성
7. prohibited integration(memory/packet/hook/input automation)이 코드베이스에 존재하지 않음
8. crash 후 event DB가 손상되지 않음
9. secrets가 repository에 저장되지 않음
10. High/Medium 수준의 알려진 설계 이슈가 없음

---

## 13. 릴리스 단계

### Milestone 0 — Fixture pipeline

- video input
- manual ROI
- OCR
- row parser
- JSONL output

### Milestone 1 — Robust event extraction

- change detector
- temporal tracker
- SQLite
- confidence gating
- benchmark suite

### Milestone 2 — Telemetry ground truth

- PUBG API client
- session matcher
- `LogPlayerKillV2` / `LogPlayerMakeGroggy` mapping
- validation report

### Milestone 3 — Live capture

- Windows screen capture
- calibration
- sidecar viewer
- performance profiling

### Milestone 4 — Policy-gated distribution

- policy review
- packaging
- public/private distribution decision
- optional Overwolf investigation

---

## 14. 리스크

### R1 — PUBG 정책 리스크

**Severity: High externally, controlled by product scope**

화면 OCR만 사용하더라도 KRAFTON이 비인가 third-party helper로 판단할 가능성을 배제할 수 없다.

Mitigation:

- non-invasive architecture
- no overlay/injection
- no derived tactical advice
- recorded/replay-first development
- distribution/live-ranked usage gate
- written policy inquiry before public release

### R2 — UI 변경

PUBG patch가 killfeed 디자인을 바꾸면 parser/icon classifier가 깨질 수 있다.

Mitigation:

- template/model versioning
- confidence drop monitoring
- fixture regression
- parser와 classifier 분리

### R3 — OCR ambiguity

nickname은 사전 단어가 아니므로 일반 OCR에 불리하다.

Mitigation:

- character-level OCR
- participant roster 후보군 기반 fuzzy correction(사용 가능한 경우)
- post-match participant list를 validator에 사용
- low confidence reject

### R4 — Background interference

killfeed는 게임 장면 위에 렌더되므로 배경 변화가 심하다.

Mitigation:

- geometry + text detector
- local contrast preprocessing
- temporal consensus
- background-independent row assembly

### R5 — Telemetry semantics mismatch

UI killfeed와 API event semantics가 1:1이 아닐 수 있다.

Mitigation:

- explicit semantic mapping
- raw telemetry 보존
- ambiguous cases를 `unresolved`로 분류
- validator가 억지 match를 만들지 않음

---

## 15. 적대적 리뷰 결과

본 문서는 세 번의 분리된 adversarial review pass를 반영했다.

### Review 1 — Product / policy / scope

수정 완료:

- **HIGH**: 공정성/정책 경계 불명확 → Visual Parity, 금지 기능, release gate 추가
- **HIGH**: kill vs knock vs finisher 의미 혼용 → event semantic mapping 요구 추가
- **MEDIUM**: live overlay가 정책 경계를 악화 → MVP overlay 제외
- **MEDIUM**: session-to-match linkage 미정 → matcher 명시
- **MEDIUM**: raw frame/privacy/disk risk → local-only + opt-in artifacts

### Review 2 — CV / robustness

수정 완료:

- **HIGH**: fixed ROI는 해상도/UI scale에 취약 → normalized + calibration contract
- **HIGH**: benchmark가 단일 환경에 과적합될 수 있음 → stratified holdout 요구
- **MEDIUM**: repeated frames로 duplicate 발생 → temporal tracker를 P0로 승격
- **MEDIUM**: confidence 없는 forced classification → `unknown`/candidate path 추가
- **MEDIUM**: black frame/capture failure 처리 부재 → failure state 추가

### Review 3 — Implementation / operations

수정 완료:

- **MEDIUM**: crash-safe event store 부재 → SQLite WAL/session recovery
- **MEDIUM**: API key 취급 미정 → secret management 요구
- **MEDIUM**: validator의 API lag/rate behavior 미정 → SPEC에 backoff/timeout 명시
- **MEDIUM**: 재현 가능한 benchmark metadata 부족 → model/config hashes 요구

### 남은 이슈

Low만 남긴다.

- **LOW**: 모든 ultrawide/4K/UI-scale 조합의 자동 calibration 완전 지원
- **LOW**: 희귀 weapon/event icon의 100% 분류
- **LOW**: spectator/custom/replay mode별 UI 차이의 장기 호환성
- **LOW**: 모든 언어/localization 지원

이들은 MVP blocker가 아니다.

---

## 16. 근거 자료

- PUBG API — Telemetry Events / `LogPlayerKillV2`
  - https://documentation.pubg.com/en/telemetry-events.html
- PUBG API — Telemetry retrieval
  - https://documentation.pubg.com/en/telemetry.html
- Overwolf — PUBG Game Events
  - https://dev.overwolf.com/ow-native/live-game-data-gep/supported-games/playerunknowns-battlegrounds/
- Overwolf — Real-time GEP overview
  - https://dev.overwolf.com/ow-native/live-game-data-gep/live-game-data-gep-intro/
- Overwolf — Deprecated PUBG `knockout` etc.
  - https://dev.overwolf.com/ow-native/live-game-data-gep/supported-games/deprecated/overview/
- PUBG Rules of Conduct
  - https://www.pubg.com/en-asia/clause/rules_of_conduct
- PUBG Anti-Cheat Dev Letter — 2026-06-25
  - https://pubg.com/en/news/10338?category=dev_notes
