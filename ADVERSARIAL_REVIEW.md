# ADVERSARIAL_REVIEW.md

> Review target: final `PRD.md` + `SPEC.md`
> Date: 2026-10-06
> Review method: three isolated adversarial review passes with different roles.
>
> Note: this chat runtime did not expose an independent sub-agent invocation after the Work handoff was declined, so the review passes were role-separated within the current runtime rather than claiming an independently executed external model.

## Severity definition

- HIGH: 구현 방향/안전/정확도를 근본적으로 깨뜨림. 종료 전에 반드시 해결.
- MEDIUM: 실제 사용/검증에서 높은 확률로 실패 또는 큰 재작업 유발. 종료 전에 해결.
- LOW: MVP blocker는 아니나 후속 품질 개선 대상.

---

## Pass 1 — Product / Policy Adversary

### HIGH — live helper 범위가 무제한으로 확장될 위험
**Finding:** killfeed history에서 생존 인원, 팀 전력, push 추천으로 자연스럽게 scope creep 가능.

**Resolution:** Visual Parity 원칙, explicit prohibited features, no tactical inference, release gate 추가.

**Status:** RESOLVED

### HIGH — kill/knock/finisher semantic mismatch
**Finding:** 화면 왼쪽 이름을 무조건 Telemetry `killer`로 대응하면 잘못된 ground truth를 만들 수 있음.

**Resolution:** `TelemetryCanonicalEvent`, dBNOMaker/finisher/killer 분리, ambiguous semantic은 unresolved 처리.

**Status:** RESOLVED

### MEDIUM — injected overlay로 오해/정책 risk 증가
**Resolution:** MVP in-game overlay 금지. 독립 sidecar/console만 허용.

**Status:** RESOLVED

### MEDIUM — session을 어떤 PUBG match와 연결할지 정의 없음
**Resolution:** timestamp + participant + duration score 기반 Session-to-match matcher 추가.

**Status:** RESOLVED

### MEDIUM — screenshots 저장이 privacy/disk risk
**Resolution:** full frame/crop storage 기본 OFF, opt-in.

**Status:** RESOLVED

---

## Pass 2 — CV / ML Adversary

### HIGH — fixed pixel ROI는 첫 해상도 변경에서 깨짐
**Resolution:** normalized rect + calibration + P1 auto-location.

**Status:** RESOLVED

### HIGH — accuracy target이 easy fixture에 overfit될 수 있음
**Resolution:** dev/regression/holdout 분리, 1080p/1440p, UI scale, dark/light, burst stratification.

**Status:** RESOLVED

### MEDIUM — killfeed row가 여러 초 남아 duplicate 폭발
**Resolution:** temporal tracker/dedup을 P0 핵심 요구로 승격.

**Status:** RESOLVED

### MEDIUM — unknown icon/text를 forced classification
**Resolution:** `UNKNOWN`, candidate path, confidence gating 추가.

**Status:** RESOLVED

### MEDIUM — game capture black frame이 empty feed로 오인될 수 있음
**Resolution:** CaptureHealth와 black-frame degraded state 추가.

**Status:** RESOLVED

---

## Pass 3 — Systems / Operations Adversary

### MEDIUM — crash 시 event/session corruption
**Resolution:** SQLite WAL, append-oriented writes, incomplete session recovery.

**Status:** RESOLVED

### MEDIUM — PUBG API key 노출 가능
**Resolution:** env/credential store, CLI arg 금지, Authorization logging 금지.

**Status:** RESOLVED

### MEDIUM — API lag/rate limit로 validator 무한 polling
**Resolution:** bounded polling, 429 respect, exponential backoff, max wait.

**Status:** RESOLVED

### MEDIUM — 모델/설정이 바뀌면 benchmark 재현 불가
**Resolution:** git commit/config hash/model manifest hash/report metadata.

**Status:** RESOLVED

---

# Final open issues

## LOW-001 — ultrawide / 4K / all UI scales
완전 자동 calibration coverage는 추가 fixture가 필요.

## LOW-002 — rare icons
희귀 무기/환경 이벤트는 `unknown`으로 degrade할 수 있음.

## LOW-003 — spectator/custom/replay variants
모드별 killfeed layout 차이가 존재할 수 있음.

## LOW-004 — localization
한국어/영어 이외 locale의 UI 요소는 추가 검증 필요.

**Final gate result: PASS — High: 0, Medium: 0, Low: 4**
