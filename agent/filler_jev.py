"""AG-T66 Jev(TypeSafe System One) 채움말 판정. 근거 데이터: data/jev/filler_scenarios(사용자가 만든 가상 발화 50개).

Jev는 학습하는 모델이 아니라 질문마다 설명·예시를 받아 확률을 돌려주는 분류기다. 그래서 데이터의 발화를
의도·대화 상태별 예시로 질문 안에 넣는다(평가 때는 맞힐 발화를 예시에서 뺀다, scripts/jev_filler_eval.py).

한 요청에 세 가지를 묻는다:
- intent (choice, 11개): 배송·주문·취소·반품환불·결제·정기배송·상품정보·섭취 후 이상·품질·상담원·기타
- dialogue_state (choice, 5개): 일반·정정·정보 부족·복수 의도·불명확
- trouble (noul): 고객이 우리 쪽 문제로 불편을 겪었다고 말하는가(공감 한마디가 맞는가)

정책은 코드가 가진다(decide_empathy). Jev 결과는 로컬 규칙(intents.empathy_key)을 보강·억제만 한다:
- 섭취 후 이상(PRODUCT_SAFETY)·정정·불명확·복수 의도 → 공감 한마디 없음(안전 안내·정확한 확인이 먼저)
- trouble이 높고(≥TROUBLE_ON) 로컬이 못 골랐으면 의도로 상황을 정한다
- trouble이 낮으면(≤TROUBLE_OFF) 로컬이 골랐어도 쓰지 않는다
- Jev 실패·시간 초과·아직 안 끝남 → 로컬 규칙 그대로(지연을 더하지 않는다)

LLM 선호출과 같은 순간에 쏘고(Session._fire_filler_jev), 공감 한마디를 고를 때 이미 끝난 결과만 쓴다.
스위치: DIRECTON_JEV_FILLER=1 이고 TYPESAFE_API_KEY가 있을 때만 켜진다(기본 끔).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

MODEL = "jev-1.14.0"
ENV_SWITCH = "DIRECTON_JEV_FILLER"
DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "jev" / "filler_scenarios"
MAX_EXAMPLES = 4                # 라벨마다 질문에 넣는 예시 수(요청 크기·지연을 위해)
TROUBLE_ON = 0.7                # 이 이상이면 불편 상황으로 본다
TROUBLE_OFF = 0.3               # 이 이하면 로컬 규칙이 골랐어도 공감 한마디를 쓰지 않는다
NO_EMPATHY_STATES = frozenset({"CORRECTION", "UNCLEAR", "MULTIPLE_INTENTS"})
NO_EMPATHY_INTENTS = frozenset({"PRODUCT_SAFETY", "HUMAN_HANDOFF"})
# Jev 의도 → knowledge.EMPATHY_PHRASES 키(로컬 규칙이 못 골랐을 때만)
# 결제(PAYMENT)는 넣지 않는다: 이중 결제·결제 실패·입금 미확인이 섞여 있어 "입금 확인이 안 돼…"가 틀리게 나간다(CS015).
INTENT_TO_EMPATHY = {"DELIVERY": "delay", "QUALITY_ISSUE": "defect", "RETURN_REFUND": "refund_late"}

SITUATION = ("Korean phone customer-service call for an online health-supplement shop, handled by an AI agent. "
             "`customer_utterance` is a speech-to-text transcript (it may contain recognition errors) of what the "
             "customer just said; `recent_conversation` is the earlier part of the call. While the AI prepares the "
             "answer it may say one short filler line, so we need to know what the customer wants and whether "
             "they are reporting an inconvenience they suffered.")


# ── 데이터 ───────────────────────────────────────────
def load_dataset(data_dir: Path | str = DATA_DIR) -> tuple[list[dict], dict]:
    """(발화 목록, 라벨 정의). 없으면 ([], {})."""
    d = Path(data_dir)
    try:
        rows = [json.loads(l) for l in (d / "scenarios.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
        labels = json.loads((d / "labels_and_policy.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log.warning("Jev 채움말 데이터를 읽지 못했다(%s): %r", d, e)
        return [], {}
    return rows, labels


def build_questions(rows: list[dict], labels: dict, exclude: set[str] | frozenset[str] = frozenset(),
                    max_examples: int = MAX_EXAMPLES) -> dict:
    """세 질문. 예시는 exclude에 없는 발화에서 라벨마다 max_examples개까지."""
    def examples(field_: str, label: str) -> list[str]:
        return [r["utterance"] for r in rows if r.get(field_) == label and r.get("id") not in exclude][:max_examples]

    intents = labels.get("intent_labels") or {}
    states = labels.get("dialogue_states") or {}
    return {
        "intent": {
            "type": "choice",
            "instructions": "Which task does the customer's utterance mainly ask for? Use recent_conversation when the "
                            "utterance only answers the agent's question (a number or a name).",
            "criteria": {k: {"covers": v, "examples": examples("intent", k)} for k, v in intents.items()},
        },
        "dialogue_state": {
            "type": "choice",
            "instructions": "What is the conversational state of the customer's utterance?",
            "criteria": {k: {"covers": v, "examples": examples("dialogue_state", k)} for k, v in states.items()},
        },
        "trouble": {
            "type": "noul",
            "instructions": ("Is the customer reporting an inconvenience they have already suffered because of the "
                             "shop or its delivery (late or missing delivery, wrong or damaged item, refund or payment "
                             "not processed, product not working as promised), so that a brief apology fits?"),
            "criteria": {
                "true": "A complaint about something that already went wrong for the customer.",
                "false": ("A plain question or request (how to buy, prices, ingredients, when it will arrive with no "
                          "sign of delay, cancel or return requests without a complaint), a correction, or an unclear "
                          "or unfinished utterance."),
            },
        },
    }


def build_state(utterance: str, history: list[dict] | None = None) -> dict:
    return {
        "situation": SITUATION,
        "recent_conversation": [{"role": m.get("role"), "text": m.get("text", m.get("content"))}
                                for m in (history or []) if isinstance(m, dict) and m.get("role") in
                                ("customer", "agent", "user", "assistant")][-6:],
        "customer_utterance": (utterance or "").strip(),
    }


# ── 결과 ─────────────────────────────────────────────
@dataclass
class FillerJudgement:
    intent: str
    intent_probs: dict[str, float] = field(default_factory=dict)
    state: str = ""
    trouble: float = 0.0
    ms: float = 0.0


def _get(obj: Any, key: str) -> Any:
    return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)


def parse_answers(resp: Any, ms: float = 0.0) -> FillerJudgement | None:
    """HTTP 응답 dict({"answers": {...}})나 SDK 응답 객체. 필요한 답이 없으면 None."""
    ans = _get(resp, "answers") or {}
    it, st, tr = _get(ans, "intent"), _get(ans, "dialogue_state"), _get(ans, "trouble")
    choice = _get(it, "choice") if it is not None else None
    noul = _get(tr, "noul") if tr is not None else None
    if not choice or noul is None:
        return None
    probs = _get(it, "probabilities") or {}
    return FillerJudgement(intent=str(choice), intent_probs={str(k): float(v) for k, v in dict(probs).items()},
                           state=str(_get(st, "choice") or "") if st is not None else "", trouble=float(noul), ms=ms)


def decide_empathy(j: FillerJudgement | None, local_key: str | None) -> tuple[str | None, str]:
    """(공감 한마디 키 또는 None, 근거). Jev 결과가 없으면 로컬 규칙 그대로."""
    if j is None:
        return local_key, "local"
    if j.intent in NO_EMPATHY_INTENTS:
        return None, f"jev_intent_{j.intent}"
    if j.state in NO_EMPATHY_STATES:
        return None, f"jev_state_{j.state}"
    if j.trouble <= TROUBLE_OFF:
        return None, "jev_no_trouble"
    if local_key:
        return local_key, "local+jev"
    if j.trouble >= TROUBLE_ON and j.intent in INTENT_TO_EMPATHY:
        return INTENT_TO_EMPATHY[j.intent], "jev"
    return None, "jev_unsure"


# ── 판정기 ───────────────────────────────────────────
def filler_enabled() -> bool:
    return os.environ.get(ENV_SWITCH, "").strip().lower() in ("1", "true", "yes", "on")


class JevFiller:
    """client: system_one(state=, questions=, model=)을 가진 비동기 클라이언트. None이면 첫 요청 때 만든다.
    실패·시간 초과는 None(호출자는 로컬 규칙 그대로)."""

    def __init__(self, client: Any = None, *, model: str = MODEL, timeout_s: float = 0.6,
                 data_dir: Path | str = DATA_DIR):
        self._client = client
        self.model = model
        self.timeout_s = timeout_s
        rows, labels = load_dataset(data_dir)
        self.questions = build_questions(rows, labels) if rows else {}

    def _client_(self) -> Any:
        if self._client is None:
            from agent.turn.jev_turn import make_client
            self._client = make_client(self.timeout_s)
        return self._client

    async def judge(self, text: str, history: list[dict] | None = None) -> FillerJudgement | None:
        text = (text or "").strip()
        if not text or not self.questions:
            return None
        t0 = time.perf_counter()
        try:
            resp = await asyncio.wait_for(
                self._client_().system_one(state=build_state(text, history), questions=self.questions,
                                           model=self.model),
                self.timeout_s)
        except (Exception, asyncio.TimeoutError) as e:  # noqa: BLE001
            log.info("jev filler failed: %r", e)
            return None
        return parse_answers(resp, (time.perf_counter() - t0) * 1000)


def make_filler_judge() -> JevFiller | None:
    """스위치가 켜져 있고 키가 있을 때만 판정기."""
    if not filler_enabled():
        return None
    from agent.turn.jev_turn import api_key
    if not api_key():
        log.warning("%s=1이지만 TYPESAFE_API_KEY가 없어 Jev 채움말 판정을 끈다", ENV_SWITCH)
        return None
    return JevFiller()
