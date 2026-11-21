"""AG-T62 Jev 채움말 판정(가짜 클라이언트, 네트워크 없음)."""
import asyncio

from agent import filler_jev
from agent.filler_jev import FillerJudgement, decide_empathy


def answers(intent="DELIVERY", state="NORMAL", trouble=0.9):
    return {"answers": {"intent": {"type": "choice", "choice": intent, "probabilities": {intent: 0.8}},
                        "dialogue_state": {"type": "choice", "choice": state},
                        "trouble": {"type": "noul", "noul": trouble}}}


class FakeClient:
    def __init__(self, resp=None, delay=0.0, fail=False):
        self.resp, self.delay, self.fail = resp or answers(), delay, fail
        self.calls = []

    async def system_one(self, state, questions, model):
        self.calls.append({"state": state, "questions": questions, "model": model})
        await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("down")
        return self.resp


def test_questions_use_dataset_examples_and_can_hold_out():
    rows, labels = filler_jev.load_dataset()
    assert len(rows) == 50 and set(labels["intent_labels"]) >= {"DELIVERY", "PRODUCT_SAFETY"}
    q = filler_jev.build_questions(rows, labels)
    assert set(q) == {"intent", "dialogue_state", "trouble"}
    assert q["intent"]["type"] == "choice" and q["trouble"]["type"] == "noul"
    assert "유산균 주문했는데 언제 와요?" in q["intent"]["criteria"]["DELIVERY"]["examples"]
    held = filler_jev.build_questions(rows, labels, exclude={"CS001"})
    assert "유산균 주문했는데 언제 와요?" not in held["intent"]["criteria"]["DELIVERY"]["examples"]
    assert all(len(v["examples"]) <= filler_jev.MAX_EXAMPLES for v in q["intent"]["criteria"].values())


def test_parse_answers():
    j = filler_jev.parse_answers(answers("QUALITY_ISSUE", "NORMAL", 0.95), ms=12)
    assert j.intent == "QUALITY_ISSUE" and j.state == "NORMAL" and j.trouble == 0.95 and j.ms == 12
    assert filler_jev.parse_answers({"answers": {}}) is None


def test_decide_policy():
    J = FillerJudgement
    assert decide_empathy(None, "delay") == ("delay", "local")                      # Jev 없음 → 로컬 그대로
    assert decide_empathy(J("PRODUCT_SAFETY", trouble=0.95), "defect")[0] is None    # 섭취 후 이상 → 안전 안내 먼저
    assert decide_empathy(J("DELIVERY", state="CORRECTION", trouble=0.9), "delay")[0] is None
    assert decide_empathy(J("DELIVERY", trouble=0.1), "delay")[0] is None            # Jev가 불편 아님 → 억제
    assert decide_empathy(J("DELIVERY", trouble=0.9), None)[0] == "delay"            # 로컬이 놓친 지연
    assert decide_empathy(J("QUALITY_ISSUE", trouble=0.9), None)[0] == "defect"
    assert decide_empathy(J("DELIVERY", trouble=0.5), None)[0] is None               # 애매하면 안 씀
    assert decide_empathy(J("RETURN_REFUND", trouble=0.9), "no_effect")[0] == "no_effect"   # 로컬 상황 우선


async def test_judge_ok_fail_timeout():
    ok = filler_jev.JevFiller(FakeClient(answers("PAYMENT", trouble=0.8)), timeout_s=0.5)
    j = await ok.judge("입금했는데 확인이 안 돼요", [{"role": "agent", "text": "무엇을 도와드릴까요?"}])
    assert j.intent == "PAYMENT" and ok._client.calls[0]["state"]["customer_utterance"] == "입금했는데 확인이 안 돼요"
    assert ok._client.calls[0]["model"] == filler_jev.MODEL
    assert await filler_jev.JevFiller(FakeClient(fail=True)).judge("x") is None
    assert await filler_jev.JevFiller(FakeClient(delay=0.3), timeout_s=0.05).judge("x") is None
    assert await ok.judge("  ") is None


def test_switch_off_by_default(monkeypatch):
    monkeypatch.delenv(filler_jev.ENV_SWITCH, raising=False)
    assert filler_jev.make_filler_judge() is None
    monkeypatch.setenv(filler_jev.ENV_SWITCH, "1")
    monkeypatch.setattr("agent.turn.jev_turn.api_key", lambda: None)
    assert filler_jev.make_filler_judge() is None                                    # 키 없으면 끔
