"""의도 추정(agent/intents.py) 오프라인 평가 (AG-T55). API를 부르지 않는다.

  uv run python scripts/intent_hint_eval.py              # 정확도·힌트 정밀도/적용률·ECE
  uv run python scripts/intent_hint_eval.py --fit        # 온도 T를 NLL 최소로 맞춘 값도 출력
  uv run python scripts/intent_hint_eval.py --show       # 문항별 예측(연습 문항 내용이 나오므로 화면에서만 본다)

라벨: data/intent/practice_labels.json (case_id → 허용 의도 목록, 'unknown'이 있으면 힌트 없음도 정답).
지표
- top1 정확도: 1위 의도가 허용 목록에 있는가(unknown 예측은 허용 목록에 unknown이 있을 때만 맞음)
- 힌트 정밀도: 힌트를 붙인 문항 중 의도가 맞은 비율. 틀린 힌트는 엉뚱한 해결책을 내밀므로 이 값을 가장 중요하게 본다(목표 ≥ 95%)
- 힌트 적용률: 힌트를 붙인 문항 비율
- ECE(10구간): 1위 확률과 실제 정답률의 차이(보정 오차, Guo et al. 2017)

API 없이 보는 추가 점검
  --consistency  힌트를 붙인 연습 문항에서, 힌트가 권하는 함수가 실제 상담 호출(reference.tool_calls)과 어긋나는지(함수 점수 대리 지표)
  --robust       받아쓰기 흔들림(띄어쓰기, 채움말, 낱말 오인식, 끝 잘림)을 넣어 힌트가 '다른 틀린 의도'로 바뀌는지
  --synthetic    data/intent/synthetic.jsonl(직접 지은 문장, 연습 문항과 무관)로 정확도·힌트 정밀도
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
CASES = ROOT / "DIRECT_ON 2026 Starter Kit" / "data" / "practice_cases.jsonl"
LABELS = ROOT / "data" / "intent" / "practice_labels.json"
SYNTH = ROOT / "data" / "intent" / "synthetic.jsonl"
EXAMPLES = ROOT / "DIRECT_ON 2026 Starter Kit" / "data" / "examples.json"

# 의도별로 실제 상담에서 나와도 되는 함수(이 밖의 함수가 정답에 있으면 힌트가 엉뚱한 쪽을 권한 것)
ALLOWED_TOOLS = {
    "delivery": {"get_order_info"}, "payment_check": {"get_order_info"}, "address_change": {"get_order_info"},
    "misdelivery": {"get_order_info"}, "product": {"search_products"},
    "cancel": {"get_order_info", "cancel_order"},
    "return_request": {"get_order_info", "return_order", "return_order_item"},
    "return_policy": set(), "return_status": {"get_return_info", "get_order_info"},
    "exchange": {"get_order_info", "exchange_order"}, "purchase": set(), "callback": {"request_callback"},
    "policy": set(), "health": {"search_products"}, "agent": set(), "other_channel": set(), "feedback": set(),
}

# 받아쓰기에서 실제로 보던 꼴의 흔들림(결정적)
_CONFUSE = [("환불", "한불"), ("반품", "반풍"), ("배송", "배성"), ("입금", "임금"), ("취소", "치소"),
            ("교환", "교한"), ("구매", "구메"), ("상담원", "상담원님"), ("주세요", "주세여"), ("언제", "언재")]


def perturb(text: str) -> list[tuple[str, str]]:
    out = [("spaces", " ".join(text.replace(" ", ""))), ("filler", "어 음 " + text), ("nospace", text.replace(" ", ""))]
    for a, b in _CONFUSE:
        if a in text:
            out.append((f"{a}->{b}", text.replace(a, b, 1)))
    words = text.split()
    if len(words) > 2:
        out.append(("cut_last", " ".join(words[:-1])))
    return out

from agent import intents  # noqa: E402


def load():
    cases = {json.loads(l)["case_id"]: json.loads(l) for l in open(CASES, encoding="utf-8")}
    labels = json.loads(LABELS.read_text(encoding="utf-8"))["labels"]
    return [(cases[c], set(v)) for c, v in labels.items() if c in cases]


def evaluate(data, T: float) -> dict:
    n = len(data)
    correct = shown = shown_ok = 0
    nll = 0.0
    bins = [[0, 0.0, 0] for _ in range(10)]        # count, conf sum, correct
    rows = []
    for case, allowed in data:
        est = intents.estimate(case["customer_utterance"], case.get("history"), temperature=T)
        ok = est.top in allowed
        correct += ok
        p_true = max(est.probs.get(a, 0.0) for a in allowed)
        nll -= math.log(max(p_true, 1e-9))
        b = min(9, int(est.p * 10))
        bins[b][0] += 1
        bins[b][1] += est.p
        bins[b][2] += ok
        conf = est.top != intents.UNKNOWN and est.p >= intents.THRESHOLD and est.margin >= intents.MARGIN
        if conf:
            shown += 1
            shown_ok += ok
        rows.append((case["case_id"], sorted(allowed), est, conf, ok))
    ece = sum(abs(c / k - s / k) * k / n for k, s, c in bins if k)
    return {"n": n, "acc": correct / n, "shown": shown, "precision": shown_ok / shown if shown else 1.0,
            "coverage": shown / n, "nll": nll / n, "ece": ece, "rows": rows}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fit", action="store_true")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--consistency", action="store_true")
    ap.add_argument("--robust", action="store_true")
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--all", action="store_true", help="--consistency --robust --synthetic")
    ap.add_argument("--hinted", action="store_true",
                    help="힌트가 붙는 연습 문항 id만 쉼표로 출력(scripts/intent_ab.sh가 이 문항만 A/B한다)")
    a = ap.parse_args()
    if a.hinted:
        print(",".join(c["case_id"] for c, _ in load()
                       if intents.estimate(c["customer_utterance"], c.get("history")).confident))
        return
    if a.all:
        a.consistency = a.robust = a.synthetic = True
    data = load()
    r = evaluate(data, intents.TEMPERATURE)
    print(f"문항 {r['n']} | top1 {r['acc']:.1%} | 힌트 {r['shown']}개(적용률 {r['coverage']:.1%}) "
          f"정밀도 {r['precision']:.1%} | NLL {r['nll']:.3f} | ECE {r['ece']:.3f} (T={intents.TEMPERATURE})")
    wrong = [x for x in r["rows"] if x[3] and not x[4]]
    if wrong:
        print("틀린 힌트:", ", ".join(f"{cid}({est.top}≠{'|'.join(al)})" for cid, al, est, _, _ in wrong))
    if a.fit:
        best = min((evaluate(data, t / 10)["nll"], t / 10) for t in range(3, 31))
        rb = evaluate(data, best[1])
        print(f"NLL 최소 T={best[1]} → NLL {best[0]:.3f} | ECE {rb['ece']:.3f} | 힌트 정밀도 {rb['precision']:.1%} "
              f"적용률 {rb['coverage']:.1%}")
    if a.consistency:
        bad = []
        n = 0
        for case, _ in data:
            est = intents.estimate(case["customer_utterance"], case.get("history"))
            if not est.confident:
                continue
            n += 1
            ref = {t["name"] for t in case["reference"]["tool_calls"]}
            extra = ref - ALLOWED_TOOLS.get(est.top, set())
            if extra:
                bad.append(f"{case['case_id']}({est.top}: 정답 {sorted(extra)})")
        print(f"[함수 일관성] 힌트 {n}개 중 실제 호출과 어긋남 {len(bad)}개" + (": " + ", ".join(bad) if bad else ""))
    if a.robust:
        tot = flip_wrong = dropped = 0
        bad = []
        for case, allowed in data:
            base = intents.estimate(case["customer_utterance"], case.get("history"))
            if not base.confident:
                continue
            for kind, t in perturb(case["customer_utterance"]):
                e = intents.estimate(t, case.get("history"))
                tot += 1
                if e.confident and e.top not in allowed:
                    flip_wrong += 1
                    bad.append(f"{case['case_id']}/{kind}→{e.top}")
                elif not e.confident:
                    dropped += 1
        print(f"[잡음 강건성] 변형 {tot}개 | 틀린 힌트로 바뀜 {flip_wrong} ({flip_wrong / max(tot, 1):.1%}) | "
              f"힌트 사라짐(안전) {dropped} ({dropped / max(tot, 1):.1%})" + (" | " + ", ".join(bad[:12]) if bad else ""))
    if a.synthetic:
        rows = [json.loads(l) for l in open(SYNTH, encoding="utf-8") if l.strip()]
        ok = shown = shown_ok = 0
        bad = []
        for r_ in rows:
            e = intents.estimate(r_["text"])
            good = e.top in r_["allowed"]
            ok += good
            if e.confident:
                shown += 1
                shown_ok += good
                if not good:
                    bad.append(f"'{r_['text']}'→{e.top}")
            elif a.show:
                print(f"   (힌트 없음) '{r_['text']}' {' '.join(f'{c}:{p:.2f}' for c, p in e.ranked(2))}")
        print(f"[지은 문장] {len(rows)}개 | top1 {ok / len(rows):.1%} | 힌트 {shown}개(적용률 {shown / len(rows):.1%}) "
              f"정밀도 {shown_ok / max(shown, 1):.1%}" + (" | 틀린 힌트: " + ", ".join(bad) if bad else ""))
    if a.show:
        for cid, al, est, conf, ok in r["rows"]:
            top3 = " ".join(f"{c}:{p:.2f}" for c, p in est.ranked(3))
            print(f"{'✓' if ok else '✗'}{'H' if conf else ' '} {cid} [{'|'.join(al)}] {top3} kw={est.keywords[:3]}")


if __name__ == "__main__":
    main()
