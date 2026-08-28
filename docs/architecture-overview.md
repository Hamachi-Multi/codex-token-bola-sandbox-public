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

- `scripts/hook.py`
- `scripts/reconcile.py`

`codex_token_bola.hook`가 설치형 Codex hook entrypoint이고 `scripts/hook.py`가 구현을 제공한다.

주요 역할:

- turn start 상태 저장
- turn stop 시점의 token usage delta 계산
- user prompt preview 저장
- instruction excerpt 저장
- model call summary 추출
- raw prompt usage row append

기본 저장 위치:

```text
<output-dir>/raw/current/
<output-dir>/state/
```

`codex_dir`은 Codex의 입력 데이터 위치이고 output directory는 이 앱이 생성하는 파일의 위치다. `--output-dir`를 설정하지 않으면 OS 사용자 데이터 디렉터리의 `bola` 경로를 사용한다.

`install-hook`과 `paths set --codex-dir`은 기존에 초기화된 `codex_dir`만 허용한다. `install-hook`은 추가로 PATH의 `codex --version`과 설치된 훅 모듈 import를 확인하며 검증 실패 전에는 설정이나 훅 파일을 변경하지 않는다.

`state/*.json` 파일은 start는 기록됐지만 stop이 아직 처리되지 않은 pending turn 상태를 저장한다.

`scripts/reconcile.py`는 pending state를 나중에 복구하는 경로다. Codex가 중간에 종료되거나 stop hook이 누락되면 transcript를 다시 읽어서 recover 가능한 turn usage row를 만든다.

`turn_lifecycle.py`는 Hook, Reconcile, Normalize, Dashboard가 공유하는 terminal event 분류와 Turn accumulator를 제공한다. 각 호출자는 bounded scan, transcript snapshot, cache 같은 I/O 정책만 소유한다. `turn_capture.py`는 usage 계산, metadata 생성, raw segment append를 제공하며 import 시 runtime path를 해석하거나 파일을 만들지 않는다. 따라서 Reconcile은 Hook 실행 진입점을 import하지 않는다.

## Raw Segment Layer

Primary files:

- `scripts/raw_segments.py`: 외부 모듈이 사용하는 명시적 compatibility facade
- `scripts/raw_segments_state.py`: pointer, manifest, apply marker 상태 관리
- `scripts/raw_segments_rotation.py`: current segment 생성과 rotation
- `scripts/raw_segments_retention.py`: segment retention 계획, 검증, 적용

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

- `codex_token_bola/`
- `scripts/bola.py`
- `scripts/runtime_command_runner.py`
- `scripts/runtime_command_service.py`
- `scripts/doctor_service.py`
- `scripts/pipeline_service.py`
- `scripts/retention_service.py`
- `scripts/paths_service.py`
- `scripts/hook_service.py`
- `scripts/service_lock.py`
- `scripts/cancel_control.py`
- `scripts/progress_control.py`

`codex_token_bola.cli`는 설치형 `bola` 명령과 `python -m codex_token_bola`의 entrypoint다.
`scripts/bola.py`는 argparse, JSON 출력, 공통 오류 변환만 담당하는 CLI adapter다.
각 `*_service.py`는 argparse나 stdout에 의존하지 않는 typed option/result 기반 application 흐름을 소유한다.
`runtime_command_runner.py`는 bundled runtime script를 실행하고 종료 코드, stdout, stderr, JSON payload를 구조화하는 process boundary다.

command dispatch 흐름:

```text
argparse subcommand handler
  -> typed options
  -> application service
  -> runtime command runner / filesystem modules
  -> typed result
  -> CLI JSON renderer
```

dashboard의 Analyze와 cleanup API는 격리, 취소, 진행률 계약을 유지하기 위해 계속 별도 `bola` subprocess를 호출한다.

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

`doctor`는 `health.status`를 `healthy`, `degraded`, `failed`로 분류한다.
종료 코드는 각각 `0`, `1`, `2`이며 최근 hook 오류, 복구가 필요한 state,
오래 남은 analytics 임시 파일, normalize publish 복구 상태를 반영한다.

`quarantine_health.py`는 normalize/reconcile이 제외한 손상 입력을 안정된
이벤트 ID로 집계한다. 원문 증거는 `bad/`에 유지하고 상태 파일에는 해시와
발생 metadata만 저장한다. 미확인 이벤트는 `doctor`와 pipeline을 degraded로
만들며 `quarantine acknowledge`는 증거를 삭제하지 않고 확인 상태만 기록한다.

pipeline은 하위 명령의 종료 코드 1과 `status: degraded`가 함께 있을 때만
유효 데이터 build를 계속한다. build가 끝난 뒤 pipeline도 degraded로 종료하며,
구조화된 degraded 결과가 아닌 nonzero 종료는 기존처럼 즉시 실패 처리한다.

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

`cancel_control.py`는 Analyze 취소 요청을 cooperative cancellation 방식으로 처리한다. 즉 프로세스를 즉시 kill하는 방식이 아니라 normalize/build 내부 checkpoint에서 취소를 확인한다. Dashboard operation manager는 브라우저가 발급한 `operation_id`와 취소 요청을 함께 보관하므로 process attach 전 취소도 유실하지 않는다.

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
analytics/bola.sqlite
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
- `scripts/dashboard_operation_state.py`
- `scripts/dashboard_server_runtime.py`
- `scripts/dashboard_managed_process.py`
- `scripts/dashboard_process_supervisor.py`
- `scripts/dashboard_rebuild_api.py`
- `scripts/dashboard_cleanup_api.py`
- `scripts/dashboard_queries.py`
- `scripts/assets/dashboard.html`
- `scripts/assets/dashboard.css`
- `scripts/assets/dashboard.js`

`serve_dashboard.py`는 Python built-in HTTP server 기반 dashboard server다.
서버는 `localhost` 또는 IPv4 loopback 주소에만 bind하며, 모든 요청의 `Host`와 POST 요청의
`Origin`, `Sec-Fetch-Site`, JSON content type을 route 실행 전에 검증한다.
원격 접근이 필요하면 서버를 공개 bind하지 않고 SSH local port forwarding을
사용한다.

서버 모듈 책임은 다음처럼 나뉜다.

- `serve_dashboard.py`: HTTP 경계, route dispatch, read-only query API, asset serving
- `dashboard_operation_state.py`: operation ID 소유권, Analyze/cleanup 배타 상태, process/progress 상태
- `dashboard_server_runtime.py`: output directory별 Dashboard server lifetime lock과 runtime path handoff
- `dashboard_process_supervisor.py`: Linux parent-death 감지와 Dashboard-owned process group 종료
- `dashboard_rebuild_api.py`: Analyze 실행, 취소, progress endpoint
- `dashboard_cleanup_api.py`: compact, retention, 전체 삭제, cleanup progress endpoint
- `dashboard_queries.py`: SQLite read/query payload 생성

operation 모듈은 `serve_dashboard.py`를 역참조하지 않는다. handler가 제공하는
runtime path accessor와 공통 response/parser 메서드를 mixin 계약으로 사용한다.

각 output directory는 `state/dashboard-server.lock`을 통해 Dashboard server
하나만 허용한다. 동적 output path 변경은 활성 operation이 없을 때 새 경로
lock을 먼저 획득한 뒤 이전 lock을 해제한다. 목적지가 다른 server에 점유된
경우 이전 경로의 데이터를 대신 제공하지 않고 요청을 fail closed한다.

Dashboard가 실행하는 pipeline, compact, retention-prune 하위 명령은 Linux
supervisor와 독립 process group 안에서 실행된다. 정상 server 종료와 parent
death 모두 supervisor가 group을 정리한다. Analyze는 cooperative cancel 후
TERM/KILL 순서로, Cleanup은 SIGINT 복구 10초 후 TERM/KILL 순서로 종료한다.

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
대시보드의 반응형 레이아웃과 컴포넌트 크기 선택 기준은
`docs/dashboard-responsive-layout.md`를 따른다.

대시보드 주요 화면:

- Overview
- Turns
- Tools
- Subagents
- Cleanup

Frontend는 framework 없이 vanilla JS로 동작한다. `scripts/assets/dashboard.js`가 client-side state, API call, pagination, selected detail rendering, Analyze button progress UI를 담당한다.

## Cleanup and Retention Layer

Primary files:

- `scripts/dashboard_cleanup.py`: 외부 모듈이 사용하는 명시적 compatibility facade
- `scripts/dashboard_retention_index.py`: current source 스캔과 retention index 관리
- `scripts/dashboard_retention_preview.py`: preview signature, cache, snapshot handoff, 결과 조합
- `scripts/dashboard_cleanup_retention.py`: segment prune 사전 검사, 계획, 검증, 적용
- `scripts/dashboard_cleanup_payload.py`: cleanup 화면 payload와 전체 로그 삭제
- `scripts/dashboard_cleanup_recovery.py`: 중단된 retention job 복구

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

`RetentionJob`은 허용된 phase 전이와 phase별 저장 불변조건을 중앙에서 검증한다. 기존 schema v1 marker는 구조적으로 읽을 수 있지만 신규 저장은 operation ID, cutoff, recovery flag와 물리 삭제·파생 rebuild 플래그의 일관성을 만족해야 한다. 과거 `complete` phase는 복구 입력으로만 허용하고 신규 작업에서는 생성하지 않는다.

기간 삭제의 물리 변경 대상은 manifest가 추적하는 segment뿐이다. 계획 payload의 `untracked` 필드는 이전 형식과의 호환을 위해 빈 배열로 유지하며, 비어 있지 않으면 검증 단계에서 거부한다. current source의 일반 JSONL 스캔은 preview와 index 갱신에만 사용하고 직접 rewrite/delete하지 않는다.

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
bola pipeline --incremental
```

pending state 복구 포함 분석:

```bash
bola pipeline --incremental --recover
```

retention prune:

```bash
bola retention-prune --cutoff 2026-05-20T00:00:00+00:00 --preview-signature <signature-from-log-cleanup-preview>
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
