"""AG-T62 Jev 채움말 판정 평가. 데이터: data/jev/filler_scenarios(가상 발화 50개).

  uv run python scripts/jev_filler_eval.py --dry     # API 없이: 질문 크기와 로컬 규칙 기준선
  uv run python scripts/jev_filler_eval.py           # Jev 호출(TYPESAFE_API_KEY 필요, 50회): 2겹 교차 평가

2겹 교차 평가: 짝수 번째 발화를 예시로 넣고 홀수 번째를 맞히고, 그다음 반대로 한다(맞힐 발화는 예시에 없다).
지표: 의도 정확도, 대화 상태 정확도, 불편(trouble) 판정이 공감형 채움말 라벨과 맞는 비율, 공감 한마디 결정, 지연 p50/p95.
불편 라벨은 데이터의 filler_text에 공감 표현(기다리셨·불편·죄송·아쉬…)이 있는지로 근사한다(데이터에 직접 라벨이 없다).
결과는 runs/jev_filler/<시각>/report.json(가상 발화만 담는다).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent import filler_jev, intents  # noqa: E402

_EMPATHIC = re.compile(r"기다리셨|불편|죄송|아쉬|답답|걱정|겪으셨")


def trouble_label(r: dict) -> bool:
    return bool(_EMPATHIC.search(r.get("filler_text") or ""))


def pct(xs: list[float], p: float) -> float | None:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * len(xs)))] if xs else None


def local_baseline(rows: list[dict]) -> dict:
    picked = {r["id"]: intents.empathy_key(r["utterance"]) for r in rows}
    agree = sum(bool(picked[r["id"]]) == trouble_label(r) for r in rows)
    return {"empathy_picked": {k: v for k, v in picked.items() if v}, "trouble_agree": agree / len(rows)}


async def run_live(rows: list[dict], labels: dict, concurrency: int) -> dict:
    from agent.turn.jev_turn import api_key, make_client
    if not api_key():
        raise SystemExit("TYPESAFE_API_KEY가 없습니다(.env에 TYPESAFE_API_KEY=… 를 넣으세요).")
    client = make_client(5.0)
    sem = asyncio.Semaphore(concurrency)
    folds = [[r for i, r in enumerate(rows) if i % 2 == k] for k in (0, 1)]   # fold k = 예시
    out: list[dict] = []

    async def one(r: dict, questions: dict) -> None:
        async with sem:
            t0 = time.perf_counter()
            try:
                resp = await client.system_one(state=filler_jev.build_state(r["utterance"]), questions=questions,
                                               model=filler_jev.MODEL)
                j = filler_jev.parse_answers(resp, (time.perf_counter() - t0) * 1000)
                err = "" if j is not None else "parse"
            except Exception as e:  # noqa: BLE001
                j, err = None, repr(e)[:160]
            local = intents.empathy_key(r["utterance"])
            key, why = filler_jev.decide_empathy(j, local)
            out.append({"id": r["id"], "utterance": r["utterance"], "intent": r["intent"],
                        "state": r["dialogue_state"], "trouble_label": trouble_label(r),
                        "jev_intent": j.intent if j else None, "jev_state": j.state if j else None,
                        "jev_trouble": round(j.trouble, 3) if j else None, "ms": round(j.ms) if j else None,
                        "local_empathy": local, "empathy": key, "why": why, "error": err})

    tasks = []
    for k in (0, 1):
        questions = filler_jev.build_questions(rows, labels, exclude={r["id"] for r in folds[k]})
        tasks += [one(r, questions) for r in folds[k]]
    await asyncio.gather(*tasks)
    ok = [x for x in out if not x["error"]]
    n = len(ok) or 1
    ms = [x["ms"] for x in ok if x["ms"] is not None]
    return {
        "n": len(out), "errors": len(out) - len(ok),
        "intent_acc": sum(x["jev_intent"] == x["intent"] for x in ok) / n,
        "state_acc": sum(x["jev_state"] == x["state"] for x in ok) / n,
        "trouble_agree": sum(((x["jev_trouble"] or 0) >= 0.5) == x["trouble_label"] for x in ok) / n,
        "ms_p50": pct(ms, 0.5), "ms_p95": pct(ms, 0.95),
        "rows": sorted(out, key=lambda x: x["id"]),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--concurrency", type=int, default=4)
    a = ap.parse_args()
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    rows, labels = filler_jev.load_dataset()
    if not rows:
        raise SystemExit(f"데이터 없음: {filler_jev.DATA_DIR}")
    q = filler_jev.build_questions(rows, labels)
    size = len(json.dumps(q, ensure_ascii=False))
    base = local_baseline(rows)
    print(f"발화 {len(rows)}개 | 질문 {len(q)}개, {size}자 | 로컬 규칙: 공감 한마디 {len(base['empathy_picked'])}개, "
          f"불편 라벨과 일치 {base['trouble_agree']:.0%}")
    if a.dry:
        return
    rep = asyncio.run(run_live(rows, labels, a.concurrency))
    print(f"Jev 2겹 교차 | 의도 {rep['intent_acc']:.0%} | 대화 상태 {rep['state_acc']:.0%} | "
          f"불편 판정 일치 {rep['trouble_agree']:.0%} | 지연 p50 {rep['ms_p50']}ms p95 {rep['ms_p95']}ms | "
          f"오류 {rep['errors']}")
    wrong = [x for x in rep["rows"] if not x["error"] and x["jev_intent"] != x["intent"]]
    for x in wrong:
        print(f"  의도 틀림 {x['id']}: {x['intent']} → {x['jev_intent']} | {x['utterance']}")
    for x in rep["rows"]:
        if x["empathy"] != x["local_empathy"]:
            print(f"  공감 한마디 바뀜 {x['id']}: 로컬 {x['local_empathy']} → {x['empathy']} ({x['why']}) | {x['utterance']}")
    out = ROOT / "runs" / "jev_filler" / time.strftime("%m%d_%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"결과: {out / 'report.json'}")


if __name__ == "__main__":
    main()
