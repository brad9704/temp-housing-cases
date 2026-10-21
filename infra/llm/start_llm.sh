#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# start_llm.sh  —  launch Gemma-4-31B (dense) as an OpenAI-compatible
#                 llama.cpp server on port 8005, optionally under a watchdog.
#
# Usage:
#   ./start_llm.sh                        # start (background, no watchdog)
#   ./start_llm.sh start --auto-revive    # start + watchdog (checks every 60s)
#   ./start_llm.sh start --auto-revive --revive-on-hang
#   ./start_llm.sh stop                   # stop watchdog, then server
#   ./start_llm.sh status                 # health + pid + watchdog state
#   ./start_llm.sh check                  # thinking 이 실제로 꺼졌는지 판정
#
# CHANGED (multi-instance revision, 2026-10-20):
#   - 로그/PID/워치독 파일 기본 경로가 포트 번호를 따른다 (이전: 8005 고정).
#     같은 스크립트로 두 번째 인스턴스를 띄워도 서로의 PID 를 덮어쓰지 않는다.
#   - LLM_MODEL_PATH 로 모델 경로 지정 가능 (이전: CANDIDATES 배열 고정).
#   - LLM_PARALLEL 로 -np(동시 슬롯) 지정 가능. 비우면 빌드 기본값 (이전 동작과 동일).
#   - LLM_EMBEDDING=1 이면 임베딩 서버로 기동 (--embedding, --pooling).
#     이때 --reasoning 은 넘기지 않는다.
#   - status 가 /props 의 total_slots 와 슬롯당 n_ctx 를 보여준다.
#   - 컨텍스트 기본값은 코드 기준 65536 이다. 아래 이전 주석의 32768 은
#     과거 리비전 기록이며 현재 기본값이 아니다.
#
# 두 번째 인스턴스 예시:
#   LLM_PORT=8006 LLM_EMBEDDING=1 LLM_ALIAS=qwen3-embedding-8b \
#   LLM_MODEL_PATH=<MODELS_DIR>/<embedding-model>.gguf LLM_CTX=8192 \
#     ./start_llm.sh start --auto-revive
#   LLM_PORT=8006 ./start_llm.sh stop
#
# CHANGED (previous revision):
#   LLM_CTX  16384 -> 32768
#       프롬프트 ~10.5k + max_tokens 16384 = 26,857 이 16384 를 넘어서
#       생성이 max_tokens 가 아니라 컨텍스트 고갈로 잘리고 있었음.
#   --reasoning off
#       thinking 비활성. 시작 로그의 "chat template, thinking = 1" 은
#       실제로 꺼져 있어도 그대로 뜨므로 판정 근거가 아니다. 판정은 요청마다
#       찍히는 "reasoning-budget: activated" 가 사라지는지로 한다.
#       -> ./start_llm.sh check
#
# Env overrides:
#   LLM_CTX             (32768)  컨텍스트 크기
#   LLM_REASONING       (off)    on|off|auto. 빈 문자열이면 플래그를 안 넘김
#   LLM_CTX_CHECKPOINTS ()       비우면 안 넘김. 0=끔, N=슬롯당 최대 N개
#                                (SWA 체크포인트 churn 을 실험에서 뺄 때 0)
#   LLM_EXTRA_ARGS      ()       추가 플래그 그대로 전달
#   LLM_MODEL_PATH      ()       모델 gguf 경로. 비우면 CANDIDATES 에서 탐색
#   LLM_PARALLEL        ()       -np 동시 슬롯 수. 비우면 안 넘김(빌드 기본값)
#   LLM_EMBEDDING       (0)      1 이면 임베딩 서버 모드
#   LLM_POOLING         (last)   임베딩 pooling (Qwen3-Embedding 은 last)
#   LLM_REVIVE_INTERVAL (60)     watchdog 체크 주기(초)
#   LLM_MAX_REVIVES     (5)      윈도우당 허용 revive 횟수
#   LLM_REVIVE_WINDOW   (1800)   revive 카운터 윈도우(초)
#   LLM_LOAD_GRACE      (900)    이 시간 안의 /health 503 은 "로딩 중"으로 간주
#   LLM_HANG_STRIKES    (3)      hang 판정에 필요한 연속 health 실패 횟수
# ---------------------------------------------------------------------------
set -euo pipefail

# --- local paths (not committed) -------------------------------------------
# 서버별 경로·모델 파일은 infra/llm/local.env 에 둔다 (local.env.example 참고).
# local.env 는 `: "${VAR:=value}"` 형식으로 써서 호출 시 넘긴 환경변수가 우선하게 한다.
LOCAL_ENV="${LLM_LOCAL_ENV:-$(dirname "$(readlink -f "$0")")/local.env}"
# shellcheck disable=SC1090
[[ -f "$LOCAL_ENV" ]] && source "$LOCAL_ENV"

# --- config (override via env) ---------------------------------------------
MODELS_DIR="${MODELS_DIR:-}"
PORT="${LLM_PORT:-8005}"
CTX="${LLM_CTX:-65536}"
NGL="${LLM_NGL:-99}"
HOST="${LLM_HOST:-0.0.0.0}"
LLAMA_SERVER="${LLAMA_SERVER:-llama-server}"
LOG="${LLM_LOG:-$HOME/llm_server_${PORT}.log}"
PIDFILE="${LLM_PIDFILE:-$HOME/llm_server_${PORT}.pid}"

REASONING="${LLM_REASONING:-off}"
CTX_CHECKPOINTS="${LLM_CTX_CHECKPOINTS:-}"
EXTRA_ARGS="${LLM_EXTRA_ARGS:-}"
PARALLEL="${LLM_PARALLEL:-}"
EMBEDDING="${LLM_EMBEDDING:-0}"
POOLING="${LLM_POOLING:-last}"

WATCH_PIDFILE="${LLM_WATCH_PIDFILE:-$HOME/llm_watchdog_${PORT}.pid}"
WATCH_LOG="${LLM_WATCH_LOG:-$HOME/llm_watchdog_${PORT}.log}"
REVIVE_INTERVAL="${LLM_REVIVE_INTERVAL:-60}"
MAX_REVIVES="${LLM_MAX_REVIVES:-5}"
REVIVE_WINDOW="${LLM_REVIVE_WINDOW:-1800}"
LOAD_GRACE="${LLM_LOAD_GRACE:-900}"
HANG_STRIKES="${LLM_HANG_STRIKES:-3}"
REVIVE_ON_HANG="${LLM_REVIVE_ON_HANG:-0}"

if [[ -n "${LLM_MODEL_PATH:-}" ]]; then
  CANDIDATES=("$LLM_MODEL_PATH")
else
  CANDIDATES=(
    "${MODELS_DIR:+$MODELS_DIR/}${LLM_DEFAULT_MODEL_FILE:-}"
  )
fi
ALIAS="${LLM_ALIAS:-gemma-4-31b}"

SELF="$(readlink -f "$0")"

# --- arg parsing ------------------------------------------------------------
cmd="start"
if [[ $# -gt 0 && "$1" != -* ]]; then cmd="$1"; shift; fi

AUTO_REVIVE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --auto-revive)    AUTO_REVIVE=1 ;;
    --revive-on-hang) REVIVE_ON_HANG=1 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

# --- helpers ----------------------------------------------------------------
wlog() { printf '%s  %s\n' "$(date -Is)" "$*" >> "$WATCH_LOG"; }

find_model() {
  local f
  for f in "${CANDIDATES[@]}"; do
    if [[ -f "$f" ]]; then echo "$f"; return 0; fi
  done
  echo "ERROR: LLM gguf not found. Looked in:" >&2
  printf '  %s\n' "${CANDIDATES[@]}" >&2
  return 1
}

# llama.cpp 는 플래그 이름이 릴리스마다 바뀐다(--swa-checkpoints ->
# --ctx-checkpoints 등). 기억이 아니라 --help 로 확인한 뒤에만 넘긴다.
supports_flag() {
  "$LLAMA_SERVER" --help 2>&1 | grep -q -- "$1"
}

read_pid() { [[ -f "$PIDFILE" ]] && tr -dc '0-9' < "$PIDFILE" || true; }
read_watch_pid() { [[ -f "$WATCH_PIDFILE" ]] && tr -dc '0-9' < "$WATCH_PIDFILE" || true; }

# alive AND actually llama-server (stale pidfile 로 인한 PID 재사용 방어)
proc_alive() {
  local pid="${1:-}"
  [[ -n "$pid" ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  grep -qa 'llama-server' "/proc/$pid/cmdline" 2>/dev/null
}

proc_uptime() { ps -p "$1" -o etimes= 2>/dev/null | tr -dc '0-9' || true; }

health_ok() { curl -sf --max-time 5 "http://localhost:$PORT/health" >/dev/null 2>&1; }

ARGS=()
build_args() {
  local model="$1"
  ARGS=(
    --model "$model"
    --host "$HOST"
    --port "$PORT"
    --ctx-size "$CTX"
    --n-gpu-layers "$NGL"
    --jinja
    --no-webui
    --alias "$ALIAS"
  )
  if [[ -n "$PARALLEL" ]]; then
    if supports_flag "--parallel"; then
      ARGS+=(--parallel "$PARALLEL")
    else
      echo "WARNING: 이 빌드에 --parallel 이 없습니다. LLM_PARALLEL 무시." >&2
    fi
  fi
  if [[ "$EMBEDDING" == "1" ]]; then
    if supports_flag "--embedding"; then
      ARGS+=(--embedding)
    else
      echo "ERROR: 이 빌드에 --embedding 이 없습니다." >&2
      return 1
    fi
    if supports_flag "--pooling"; then
      ARGS+=(--pooling "$POOLING")
    else
      echo "WARNING: 이 빌드에 --pooling 이 없습니다. 모델 기본 pooling 사용." >&2
    fi
  elif [[ -n "$REASONING" ]]; then
    if supports_flag "--reasoning"; then
      ARGS+=(--reasoning "$REASONING")
    else
      echo "WARNING: 이 빌드에 --reasoning 이 없습니다. thinking 이 계속 켜집니다." >&2
      echo "         llama.cpp 업데이트 또는 클라이언트의 reasoning_effort=none 에 의존하세요." >&2
    fi
  fi
  if [[ -n "$CTX_CHECKPOINTS" ]]; then
    if supports_flag "--ctx-checkpoints"; then
      ARGS+=(--ctx-checkpoints "$CTX_CHECKPOINTS")
    elif supports_flag "--swa-checkpoints"; then
      ARGS+=(--swa-checkpoints "$CTX_CHECKPOINTS")
    else
      echo "WARNING: --ctx-checkpoints / --swa-checkpoints 둘 다 없습니다. 무시합니다." >&2
    fi
  fi
  # shellcheck disable=SC2206
  [[ -n "$EXTRA_ARGS" ]] && ARGS+=($EXTRA_ARGS)
  return 0
}

launch() {
  local model pid libdir
  model="$(find_model)"
  build_args "$model"
  # The llama.cpp build ships its shared libs (libmtmd/libllama/libggml*.so)
  # next to the binary. After moving the build tree the binary's baked-in
  # RPATH no longer points at them (it was an absolute build path, or was never
  # $ORIGIN-relative), so the loader reports "libmtmd.so.0: cannot open shared
  # object file". Prepend the binary's own directory to LD_LIBRARY_PATH so the
  # co-located .so files resolve. Derived from the binary path so it follows any
  # future relocation. Override with LLM_LIB_DIR if libs live elsewhere.
  libdir="${LLM_LIB_DIR:-$(dirname "$LLAMA_SERVER")}"
  { echo; echo "=== $(date -Is) launching $model ==="
    echo "=== args: ${ARGS[*]} ==="
    echo "=== LD_LIBRARY_PATH prefix: $libdir ==="; } >> "$LOG"
  # NOTE: >> not > so crash evidence from previous runs survives a revive.
  LD_LIBRARY_PATH="$libdir${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
    nohup "$LLAMA_SERVER" "${ARGS[@]}" >> "$LOG" 2>&1 &
  pid=$!
  echo "$pid" > "$PIDFILE"
  printf '%s' "$pid"
}

# --- watchdog ---------------------------------------------------------------
watchdog_loop() {
  local revives=0 strikes=0 window_start now pid up
  window_start=$(date +%s)
  trap 'wlog "watchdog exiting (signal)"; rm -f "$WATCH_PIDFILE"; exit 0' TERM INT
  wlog "watchdog up: interval=${REVIVE_INTERVAL}s max=${MAX_REVIVES}/${REVIVE_WINDOW}s hang_check=${REVIVE_ON_HANG}"

  while true; do
    sleep "$REVIVE_INTERVAL"
    now=$(date +%s)
    if (( now - window_start >= REVIVE_WINDOW )); then
      revives=0
      window_start=$now
    fi

    pid="$(read_pid)"

    if proc_alive "$pid"; then
      if (( REVIVE_ON_HANG )); then
        up="$(proc_uptime "$pid")"
        if [[ -n "$up" ]] && (( up > LOAD_GRACE )) && ! health_ok; then
          strikes=$(( strikes + 1 ))
          wlog "pid $pid alive but /health failing (strike $strikes/$HANG_STRIKES, up ${up}s)"
          if (( strikes >= HANG_STRIKES )); then
            wlog "killing hung pid $pid"
            kill "$pid" 2>/dev/null || true
            sleep 10
            kill -9 "$pid" 2>/dev/null || true
            strikes=0
          fi
        else
          strikes=0
        fi
      fi
      continue
    fi

    if (( revives >= MAX_REVIVES )); then
      wlog "GIVING UP: ${revives} revives inside ${REVIVE_WINDOW}s. 재시작으로 풀리는 문제가 아닙니다. $LOG 확인."
      rm -f "$WATCH_PIDFILE"
      exit 1
    fi
    revives=$(( revives + 1 ))
    wlog "server dead (pid=${pid:-none}) -> revive ${revives}/${MAX_REVIVES}"
    wlog "tail of server log: $(tail -n 3 "$LOG" 2>/dev/null | tr '\n' '|')"
    if launch >/dev/null; then
      wlog "relaunched as pid $(read_pid)"
    else
      wlog "relaunch FAILED (model missing?) - giving up"
      rm -f "$WATCH_PIDFILE"
      exit 1
    fi
    strikes=0
  done
}

# --- subcommands ------------------------------------------------------------
case "$cmd" in
  __watchdog)
    watchdog_loop
    exit 0
    ;;

  check)
    echo "--- 시작 로그 (참고용, 판정 근거 아님) ---"
    grep -h 'chat template, thinking' "$LOG" 2>/dev/null | tail -3 || echo "(없음)"
    echo
    echo "--- reasoning-budget (판정 근거) ---"
    n=$(grep -c 'reasoning-budget: activated' "$LOG" 2>/dev/null || true)
    echo "activated 총 횟수(로그 전체): ${n:-0}"
    tail -n 400 "$LOG" 2>/dev/null | grep 'reasoning-budget' | tail -5 || true
    echo
    if tail -n 400 "$LOG" 2>/dev/null | grep -q 'reasoning-budget: activated'; then
      echo ">> thinking 이 아직 켜져 있습니다."
      echo "   이 빌드에서 --reasoning off 가 안 먹는 경우입니다."
      echo "   클라이언트에서 reasoning_effort=none 또는"
      echo "   chat_template_kwargs={\"enable_thinking\":false} 를 병행하세요."
    else
      echo ">> 최근 400줄에 activated 없음."
      echo "   (요청을 최소 한 번 보낸 뒤 다시 실행해야 유효한 판정입니다)"
    fi
    echo
    echo "--- Gemma 4 <unused49> flood 흔적 ---"
    u=$(grep -c 'unused49' "$LOG" 2>/dev/null || true)
    echo "서버 로그 내 unused49: ${u:-0}   (0 이 아니면 알려진 Gemma 4 thinking 버그)"
    exit 0
    ;;

  stop)
    wpid="$(read_watch_pid)"
    if [[ -n "$wpid" ]] && kill -0 "$wpid" 2>/dev/null; then
      kill "$wpid" 2>/dev/null || true      # watchdog 을 먼저 죽여야 revive 안 함
      sleep 1
      kill -9 "$wpid" 2>/dev/null || true
      echo "Watchdog stopped (pid $wpid)."
    fi
    rm -f "$WATCH_PIDFILE"

    pid="$(read_pid)"
    if proc_alive "$pid"; then
      kill "$pid" 2>/dev/null || true
      for _ in $(seq 1 20); do proc_alive "$pid" || break; sleep 1; done
      proc_alive "$pid" && kill -9 "$pid" 2>/dev/null || true
      echo "LLM server stopped (pid $pid)."
    else
      echo "No live llama-server found."
    fi
    rm -f "$PIDFILE"
    exit 0
    ;;

  status)
    rc=0
    pid="$(read_pid)"
    if proc_alive "$pid"; then
      echo "process : alive (pid $pid, up $(proc_uptime "$pid")s)"
    else
      echo "process : DEAD (pidfile=${pid:-none})"; rc=1
    fi
    if health_ok; then
      echo "health  : ok on :$PORT"
    else
      echo "health  : not responding on :$PORT (loading, or hung)"; rc=1
    fi
    wpid="$(read_watch_pid)"
    if [[ -n "$wpid" ]] && kill -0 "$wpid" 2>/dev/null; then
      echo "watchdog: running (pid $wpid), log $WATCH_LOG"
    else
      echo "watchdog: not running"
    fi
    echo "config  : ctx=$CTX parallel=${PARALLEL:-<build default>} embedding=$EMBEDDING reasoning=${REASONING:-<unset>} ctx-checkpoints=${CTX_CHECKPOINTS:-<unset>}"
    if health_ok; then
      props="$(curl -sf --max-time 5 "http://localhost:$PORT/props" 2>/dev/null || true)"
      slots="$(printf '%s' "$props" | grep -o '"total_slots":[0-9]*' | head -1 | cut -d: -f2)"
      slot_ctx="$(printf '%s' "$props" | grep -o '"n_ctx":[0-9]*' | head -1 | cut -d: -f2)"
      echo "slots   : total=${slots:-?}  n_ctx(per slot)=${slot_ctx:-?}   -> .env 의 LLM_MAX_CONCURRENCY 에 total 값을 넣을 것"
    fi
    exit "$rc"
    ;;

  start) ;;
  *) echo "Usage: $0 {start [--auto-revive] [--revive-on-hang]|stop|status|check}" >&2; exit 2 ;;
esac

# --- start ------------------------------------------------------------------
existing="$(read_pid)"
if proc_alive "$existing"; then
  echo "Already running (pid $existing). Use '$0 stop' first."
  exit 0
fi

MODEL="$(find_model)"
echo "Model     : $MODEL"
echo "Port      : $PORT   Ctx: $CTX   ngl: $NGL"
echo "Parallel  : ${PARALLEL:-<build default>}   Embedding: $EMBEDDING"
echo "Reasoning : ${REASONING:-<unset>}"
[[ -n "$CTX_CHECKPOINTS" ]] && echo "CtxCkpt   : $CTX_CHECKPOINTS"
echo "Log       : $LOG"

pid="$(launch)"
echo "Started (pid $pid) in background."

if (( AUTO_REVIVE )); then
  REVIVE_ON_HANG="$REVIVE_ON_HANG" \
  nohup "$SELF" __watchdog >/dev/null 2>&1 &
  echo $! > "$WATCH_PIDFILE"
  echo "Watchdog started (pid $(cat "$WATCH_PIDFILE")), checking every ${REVIVE_INTERVAL}s."
  echo "  watchdog log : tail -f $WATCH_LOG"
fi

echo "  logs    : tail -f $LOG"
echo "  health  : $0 status"
echo "  thinking: $0 check     (요청 한 번 보낸 뒤 실행)"
exit 0