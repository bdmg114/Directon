#!/usr/bin/env bash
# 의도 힌트(AG-T55) 켬/끔 A/B — 적은 크레딧으로. 필요한 것: 저장소 맨 위 .env 의 OPENAI_API_KEY 또는 BAZE_API_KEY 하나.
# BAZE_API_KEY(대회 게이트웨이)만 있으면 상담원 LLM과 --judge 채점을 게이트웨이로 보낸다.
# 게이트웨이에 gpt-5.5가 없으면 JUDGE_MODEL=gpt-5.4-mini 처럼 채점 모델을 바꿔서 돌린다(공식 채점과 달라지므로 켬/끔 비교용으로만).
#
#   bash scripts/intent_ab.sh            # 함수 점수만(채점 AI 안 씀, 가장 쌈)
#   bash scripts/intent_ab.sh --judge    # 응대 점수까지(채점 gpt-5.5 호출 추가)
#   N=10 bash scripts/intent_ab.sh       # 힌트 문항 중 앞 10개만(더 쌈). REPEAT=3 이면 3번 반복 평균
#
# 아끼는 방법
# - 힌트가 붙는 문항만 돈다(나머지는 켬/끔 프롬프트가 같아 결과도 같다): scripts/intent_hint_eval.py --hinted
# - 받아쓰기 없이 문항 원문을 쓴다(--source original, STT 크레딧 0).
# - 비용 대략(힌트 문항 51개, 1회): 상담원 LLM 호출 51×2팔×1~3회. --judge면 채점 51×2회가 더 붙는다.
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] || { echo ".env 가 없습니다: cp .env.example .env 후 OPENAI_API_KEY 를 채우세요"; exit 1; }
grep -qE '^(OPENAI_API_KEY|BAZE_API_KEY)=.+' .env || { echo ".env 에 OPENAI_API_KEY 또는 BAZE_API_KEY 값을 넣으세요"; exit 1; }
[ -f "DIRECT_ON 2026 Starter Kit/data/practice_cases.jsonl" ] || { echo "공식 키트가 없습니다: DIRECT_ON 2026 Starter Kit/ 에 풀어 두세요"; exit 1; }

JUDGE="--no-judge"; [ "${1:-}" = "--judge" ] && JUDGE=""
REPEAT="${REPEAT:-1}"
IDS=$(uv run python scripts/intent_hint_eval.py --hinted)
if [ -n "${N:-}" ]; then IDS=$(echo "$IDS" | tr ',' '\n' | head -n "$N" | paste -sd, -); fi
echo "A/B 문항 $(echo "$IDS" | tr ',' '\n' | wc -l | tr -d ' ')개 | 반복 $REPEAT | 채점 $([ -z "$JUDGE" ] && echo 켬 || echo 끔)"
STAMP=$(date +%m%d_%H%M)
for arm in 0 1; do
  echo "── 힌트 $([ $arm = 1 ] && echo 켬 || echo 끔) ──"
  DIRECTON_INTENT_HINT=$arm uv run python scripts/text_eval.py run --source original --only "$IDS" \
    --repeat "$REPEAT" $JUDGE --concurrency 4 --out "runs/intent_ab/${STAMP}_hint$arm"
done
if grep -q "오류" "runs/intent_ab/${STAMP}_hint0/summary.txt" "runs/intent_ab/${STAMP}_hint1/summary.txt"; then
  echo "⚠ 오류 난 문항이 있습니다(키·크레딧·네트워크). 오류 문항은 0점으로 잡히므로 비교 전에 scored.jsonl 의 error 를 확인하세요."
fi
if grep -q "채점 실패" runs/intent_ab/${STAMP}_hint*/scored.jsonl 2>/dev/null; then
  echo "⚠ 응대 채점이 실패한 문항이 있습니다(키·크레딧, 또는 게이트웨이에 채점 모델이 없음 → JUDGE_MODEL=gpt-5.4-mini 로 다시)."
fi
echo
echo "결과: runs/intent_ab/${STAMP}_hint0 (끔) vs runs/intent_ab/${STAMP}_hint1 (켬) — summary.txt 비교"
echo "켬이 함수·응대에서 끔보다 낮지 않으면 기본값(켬) 유지, 낮으면 DIRECTON_INTENT_HINT=0"
