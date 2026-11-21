#!/usr/bin/env bash
# 체험 데모 한 번에 실행 (Git Bash / Linux / macOS)
#   bash demo/run_demo.sh [--force] [--no-browser]
# 순서: Mock API(8100) -> 상담원 서버(9000, 예열 포함) -> 데모 페이지(8080) -> 브라우저 열기
# Ctrl+C 를 누르면 세 서버를 모두 끈다(포트 기준 정리 포함).
set -u

MOCK_PORT=${MOCK_PORT:-8100}
AGENT_PORT=${AGENT_PORT:-9000}
WEB_PORT=${WEB_PORT:-8080}
FORCE=0
OPEN_BROWSER=1
for a in "$@"; do
  case "$a" in
    --force) FORCE=1 ;;
    --no-browser) OPEN_BROWSER=0 ;;
  esac
done

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1
LOG_DIR="$ROOT/runs/logs"
mkdir -p "$LOG_DIR"
export PYTHONIOENCODING=utf-8 PYTHONUTF8=1

port_open() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }

port_pids() {
  if command -v netstat >/dev/null 2>&1 && [ -n "${WINDIR:-}" ]; then
    netstat -ano | tr -d '\r' | awk -v p=":$1" '$4 == "LISTENING" && $2 ~ p"$" {print $5}' | sort -u
  elif command -v lsof >/dev/null 2>&1; then
    lsof -ti "tcp:$1" -sTCP:LISTEN 2>/dev/null
  fi
}

kill_port() {
  for pid in $(port_pids "$1"); do
    [ -z "$pid" ] || [ "$pid" = "0" ] && continue
    if [ -n "${WINDIR:-}" ]; then taskkill //PID "$pid" //F //T >/dev/null 2>&1; else kill -9 "$pid" 2>/dev/null; fi
  done
}

PIDS=()
cleanup() {
  trap - INT TERM EXIT
  echo "서버를 끄는 중..."
  for pid in "${PIDS[@]:-}"; do
    [ -n "$pid" ] && kill "$pid" 2>/dev/null
  done
  # uv가 띄운 자식 python이 남을 수 있어 포트 기준으로 한 번 더 정리
  for p in "$MOCK_PORT" "$AGENT_PORT" "$WEB_PORT"; do kill_port "$p"; done
  echo "종료했습니다."
  exit 0
}
trap cleanup INT TERM EXIT

for p in "$MOCK_PORT" "$AGENT_PORT" "$WEB_PORT"; do
  if port_open "$p"; then
    if [ "$FORCE" = 1 ]; then
      echo "포트 $p 사용 중 -> 끕니다(--force)."
      kill_port "$p"; sleep 1
    else
      echo "포트 $p 를 이미 쓰는 프로세스가 있습니다. 이전 데모가 남았다면: bash demo/run_demo.sh --force"
      trap - INT TERM EXIT
      exit 1
    fi
  fi
done

start_server() {  # name, args...
  local name=$1; shift
  uv run python "$@" >"$LOG_DIR/demo_$name.log" 2>"$LOG_DIR/demo_$name.err.log" &
  PIDS+=("$!")
  echo "  $name 시작 (PID $!, 로그 runs/logs/demo_$name*.log)"
}

wait_port() {  # port, timeout_s, name
  local i=0 max=$(( $2 * 2 ))
  while [ $i -lt $max ]; do
    port_open "$1" && return 0
    sleep 0.5; i=$((i + 1))
  done
  echo "  $3 이(가) $2초 안에 준비되지 않았습니다. runs/logs/demo_*.err.log 를 확인하세요(예: GEMINI_API_KEY)."
  return 1
}

# 키 확인: STT(Soniox)·LLM·TTS를 모두 대회 게이트웨이로 쓰려면 BAZE_API_KEY 하나면 된다(.env 또는 환경변수)
if ! { [ -n "${BAZE_API_KEY:-}" ] || [ -n "${SONIOX_API_KEY:-}" ] || grep -qE '^(BAZE_API_KEY|SONIOX_API_KEY)=.+' .env 2>/dev/null; }; then
  echo "키가 없습니다: .env 에 BAZE_API_KEY=... 를 넣으세요(음성 인식·답변·음성 합성을 모두 게이트웨이로 씁니다)."
  trap - INT TERM EXIT
  exit 1
fi

echo "[1/3] Mock API :$MOCK_PORT"
start_server mock -m mock_api.server --port "$MOCK_PORT"
wait_port "$MOCK_PORT" 30 "Mock API" || cleanup

echo "[2/3] 상담원 서버 :$AGENT_PORT (모델 예열 때문에 20~60초 걸릴 수 있습니다)"
start_server agent -m agent.server --port "$AGENT_PORT" --host 127.0.0.1
wait_port "$AGENT_PORT" 180 "상담원 서버" || cleanup

echo "[3/3] 데모 페이지 :$WEB_PORT"
start_server web -m demo.server --port "$WEB_PORT" --agent-ws "ws://127.0.0.1:$AGENT_PORT" --mock-url "http://127.0.0.1:$MOCK_PORT"
wait_port "$WEB_PORT" 30 "데모 페이지" || cleanup

URL="http://127.0.0.1:$WEB_PORT"
echo ""
echo "준비 완료: $URL"
echo "끝내려면 Ctrl+C 를 누르세요."
if [ "$OPEN_BROWSER" = 1 ]; then
  if [ -n "${WINDIR:-}" ]; then cmd //c start "" "$URL" >/dev/null 2>&1
  elif command -v open >/dev/null 2>&1; then open "$URL"
  elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL" >/dev/null 2>&1
  fi
fi

while true; do
  for pid in "${PIDS[@]}"; do
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "서버 하나가 종료됐습니다(PID $pid). runs/logs/demo_*.log 를 확인하세요."
      cleanup
    fi
  done
  sleep 1
done
