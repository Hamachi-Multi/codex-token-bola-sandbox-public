# Codex Token Bola Architecture

이 문서는 Codex Token Bola의 현재 구조를 나중에 빠르게 다시 파악하기 위한 아키텍처 개요다.

## Purpose

이 프로젝트는 로컬 Codex 사용 로그를 수집하고, 턴 단위/모델 호출 단위/도구 호출 단위로 정규화한 뒤, SQLite 분석 DB와 웹 대시보드로 보여주는 로컬 관측 서비스다.

핵심 흐름은 다음과 같다.

```text
Codex hook
  -> raw segment logs
  -> normalize
  -> normalized JSONL
  -> build analytics
  -> SQLite DB
  -> dashboard API / UI
```

## Main Data Flow

1. Codex hook이 turn start/stop 이벤트를 받아 raw usage row를 기록한다.
2. raw row는 현재 active raw segment에 append된다.
3. Analyze 실행 시 current segment를 닫고 새 current segment로 넘긴다.
4. `normalize.py`가 raw source를 읽어 normalized JSONL을 만든다.
5. `build_analytics.py`가 normalized JSONL, Codex state DB, transcript를 조합해 SQLite analytics DB를 만든다.
6. `serve_dashboard.py`가 SQLite DB를 읽어 dashboard API와 UI를 제공한다.

## Capture Layer

Primary files:

- `hooks/token-usage.py`
- `scripts/reconcile.py`

`hooks/token-usage.py`는 Codex hook entrypoint다.

주요 역할:

- turn start 상태 저장
- turn stop 시점의 token usage delta 계산
- user prompt preview 저장
- instruction excerpt 저장
- model call summary 추출
- raw prompt usage row append

기본 저장 위치:

```text
~/.codex/codex-token-bola/raw/current/
~/.codex/codex-token-bola/state/
```

`state/*.json` 파일은 start는 기록됐지만 stop이 아직 처리되지 않은 pending turn 상태를 저장한다.

`scripts/reconcile.py`는 pending state를 나중에 복구하는 경로다. Codex가 중간에 종료되거나 stop hook이 누락되면 transcript를 다시 읽어서 recover 가능한 turn usage row를 만든다.

## Raw Segment Layer

Primary file:

- `scripts/raw_segments.py`

raw log는 단일 flat JSONL 파일에 계속 쓰는 구조가 아니라 segment 기반 구조를 쓴다.

```text
raw/
  current/
    prompt-usage.raw.jsonl.current....
  archive/
    prompt-usage.raw.jsonl....jsonl.gz

state/
  current-raw-segments.json
  raw-segments-manifest.json
  raw-segment-rotation-pending.json
  raw-segment-apply-pending.json
  raw-segment.lock
  raw-segment-manifest.lock
```

중요한 파일:

- `current-raw-segments.json`: hook이 현재 append할 segment pointer
- `raw-segments-manifest.json`: 닫힌 segment 목록과 metadata
- `raw-segment-rotation-pending.json`: rotation 중간 실패 복구 marker
- `raw-segment-apply-pending.json`: retention apply 중간 실패 복구 marker
- `raw-segment.lock`: append/rotation/cleanup 충돌 방지
- `raw-segment-manifest.lock`: manifest 변경 충돌 방지

Analyze는 current segment를 닫고 새 current segment를 만드는 pointer handoff를 수행한다. 이 설계는 분석 중 새 hook write가 들어와도 새 current segment로 분리되게 하기 위한 것이다.

## CLI Orchestration Layer

Primary files:

- `scripts/codex_token_usage.py`
- `scripts/service_lock.py`
- `scripts/cancel_control.py`
- `scripts/progress_control.py`

`scripts/codex_token_usage.py`는 서비스의 CLI entrypoint다.

주요 command:

```text
doctor
pipeline
normalize
build
compact
retention-prune
serve
```

`pipeline --incremental`의 기본 흐름:

```text
service lock acquire
  -> optional reconcile
  -> compact_raw.py --rotate-current
  -> normalize.py
  -> build_analytics.py
  -> analytics DB update
  -> progress update
```

`service_lock.py`는 pipeline, normalize, compact, build, retention-prune 같은 큰 작업의 상호 배제를 담당한다.

`progress_control.py`는 Analyze 진행률을 state 파일로 기록한다.

`cancel_control.py`는 Analyze 취소 요청을 cooperative cancellation 방식으로 처리한다. 즉 프로세스를 즉시 kill하는 방식이 아니라 normalize/build 내부 checkpoint에서 취소를 확인한다.

## Normalize Layer

Primary file:

- `scripts/normalize.py`

`normalize.py`는 raw JSONL을 build layer가 쓰기 쉬운 normalized JSONL로 변환한다.

입력:

```text
raw/current/*
raw/archive/*
```

출력:

```text
normalized/prompt-usage.normalized.jsonl
normalized/normalize-state.json
bad/
```

주요 책임:

- raw row schema 정규화
- token usage numeric field 정규화
- incomplete/missing start 상태 보정
- embedded model call row flatten
- 중복 `(session_id, turn_id)` row 중 더 높은 rank row 선택
- incremental normalize를 위한 source offset 저장
- malformed JSON row를 `bad/`로 격리

goal/auto task처럼 user prompt submit hook이 없는 흐름은 transcript lifecycle을 다시 읽어 `missing_start_state` row를 보정한다.

## Analytics Build Layer

Primary file:

- `scripts/build_analytics.py`

`build_analytics.py`는 normalized JSONL과 Codex 내부 metadata를 조합해 dashboard용 SQLite DB를 만든다.

입력:

```text
normalized/*.jsonl
~/.codex/state_5.sqlite
~/.codex/session_index.jsonl
Codex rollout/transcript JSONL
state/retention-pruned-turns.json
```

출력:

```text
analytics/token-usage.sqlite
```

주요 table 개념:

- `turns`: 사용자 prompt/turn 단위 사용량
- `model_call_summaries`: turn 내부 model call 요약
- `tool_call_summaries`: tool call 출력량/시간/비용 요약
- `tool_call_samples`: tool call 샘플
- `task_rollups`: parent turn -> subagent 사용량 attribution
- `run_metadata`: 마지막 build offset, row count, elapsed time 등

subagent attribution은 Codex `thread_spawn_edges`, parent transcript의 `spawn_agent` call, child thread start time을 조합해서 계산한다.

대표 confidence 값:

```text
spawn_call_turn_context
child_task_time_overlap
spawn_edge_nearest_parent_turn
parent_pruned_by_retention
orphan
```

## Dashboard Server Layer

Primary files:

- `scripts/serve_dashboard.py`
- `scripts/dashboard_queries.py`
- `assets/dashboard.html`
- `assets/dashboard.css`
- `assets/dashboard.js`

`serve_dashboard.py`는 Python built-in HTTP server 기반 dashboard server다.

역할:

- dashboard HTML serving
- static asset serving
- dashboard API routing
- Analyze 실행
- Analyze progress polling endpoint
- Analyze cancel endpoint
- cleanup/retention API 제공

주요 endpoint 개념:

```text
/api/dashboard
/api/rebuild
/api/rebuild/progress
/api/rebuild/cancel
/api/log-cleanup
/api/log-cleanup/detail
/api/log-cleanup/retention
/api/log-cleanup/all
```

`dashboard_queries.py`는 SQLite DB를 읽어 API payload를 만드는 read/query layer다.
대시보드 route별 안정 필드는 `docs/dashboard-api-contract.md`를 기준으로 한다.

대시보드 주요 화면:

- Overview
- Turns
- Tools
- Subagents
- Cleanup

Frontend는 framework 없이 vanilla JS로 동작한다. `assets/dashboard.js`가 client-side state, API call, pagination, selected detail rendering, Analyze button progress UI를 담당한다.

## Cleanup and Retention Layer

Primary file:

- `scripts/dashboard_cleanup.py`

cleanup layer는 service-owned log/data 삭제와 retention pruning을 담당한다.

주요 기능:

- cleanup payload 생성
- retention preview
- retention index cache
- cutoff 이전 raw rows/segments 삭제 계획 수립
- segment manifest 기반 delete/rewrite
- derived output reset
- tmp/bad 삭제
- 전체 로그 삭제
- pruned turn state 저장

retention-prune은 raw source를 삭제한 뒤 normalized/analytics 같은 파생 output을 reset하고 다시 build해야 한다.

## Generated Data Boundaries

아래 경로는 생성 데이터다. 명시적인 데이터 작업 요청 없이 수정/삭제/커밋하지 않는다.

```text
analytics/
bad/
normalized/
raw/
state/
tmp/
prompt-usage*.jsonl
hook-probe-events.jsonl
```

코드와 문서의 주요 경로:

```text
hooks/
scripts/
assets/
tests/
docs/
Makefile
pyproject.toml
```

## Concurrency Model

이 프로젝트는 여러 종류의 lock과 marker를 조합해 data corruption을 막는다.

주요 동시성 장치:

- service lock: pipeline/build/normalize/compact/retention-prune 같은 큰 작업 상호 배제
- raw segment lock: hook append, current segment handoff, raw cleanup 충돌 방지
- manifest lock: raw segment manifest 변경 충돌 방지
- pending rotation marker: rotation 실패 복구
- pending apply marker: retention apply 실패 복구
- normalize state offsets: incremental normalize 진행 지점 저장
- build run metadata offsets: incremental build 진행 지점 저장

중요한 설계 원칙:

- hook path는 가볍게 유지한다.
- 무거운 transcript scan과 analytics build는 offline pipeline으로 미룬다.
- raw source는 되도록 append/segment 단위로 다룬다.
- derived output은 raw source에서 다시 만들 수 있어야 한다.
- cleanup은 raw source mutation과 derived output rebuild를 함께 고려해야 한다.

## Operational Commands

개발 서버:

```bash
python3 scripts/serve_dashboard.py --host 127.0.0.1 --port 8766
```

기본 분석:

```bash
python3 scripts/codex_token_usage.py pipeline --incremental
```

pending state 복구 포함 분석:

```bash
python3 scripts/codex_token_usage.py pipeline --incremental --recover
```

retention prune:

```bash
python3 scripts/codex_token_usage.py retention-prune --cutoff 2026-05-20T00:00:00+00:00 --preview-signature <signature-from-log-cleanup-preview>
```

검증:

```bash
make compile && make test
make ui-check
```

## Mental Model

이 서비스를 볼 때는 다음 계층으로 나눠서 보면 된다.

```text
Capture
  hook/reconcile

Storage
  raw segments/manifest/current pointer/state

Transform
  normalize

Analytics
  build_analytics/SQLite

Presentation
  serve_dashboard/dashboard_queries/assets

Maintenance
  cleanup/retention/compact
```

현재 복잡도가 높은 영역은 다음이다.

- current raw segment와 normalize offset의 일관성
- retention cleanup과 manifest apply marker의 원자성
- service lock과 dashboard API cleanup의 상호 배제
- Codex transcript 기반 goal/auto task lifecycle 보정
- subagent attribution을 위한 transcript scan 비용
- Analyze progress/cancel의 cooperative checkpoint 설계
