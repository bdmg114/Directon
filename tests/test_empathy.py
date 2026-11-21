"""AG-T61 답이 늦을 때만 넣는 공감 한마디."""
import asyncio
import time

from agent import intents, knowledge
from agent.config import Settings
from agent.core.types import Message
from agent.responder import Responder
from agent.tts_cache import TTSCache
from tests.test_responder import TOOLS3, FakeLLM, Sink, text_events, tool_events

EMP = knowledge.EMPATHY_PHRASES["delay"]
EMP_PCM = b"\x44\x27" * 8


def S(**kw):
    base = dict(filler_delay_ms=30, llm_timeout_s=2.0, tts_timeout_s=2.0, min_sentence_chars=6)
    base.update(kw)
    return Settings(**base)


class SlowTTS:
    """첫 요청만 delay초 늦게 답한다(게이트웨이 TTS 대기 흉내)."""

    def __init__(self, delay: float):
        self.delay = delay
        self.requests: list[str] = []

    async def synthesize(self, text):
        self.requests.append(text)
        if len(self.requests) == 1:
            await asyncio.sleep(self.delay)
        yield b"\x10\x27" * 4 + text.encode()


class Order:
    """보낸 PCM 순서를 문구로 기록한다."""

    def __init__(self):
        self.seq: list[bytes] = []

    async def __call__(self, pcm: bytes):
        self.seq.append(pcm)


# ── 상황 고르기 ─────────────────────────────────────
def test_empathy_only_for_clear_trouble():
    assert intents.empathy_key("배송이 아직 안 왔어요") == "delay"
    assert intents.empathy_key("다른 제품이 왔어요") == "misdelivery"
    assert intents.empathy_key("환불이 아직 안 들어왔어요") == "refund_late"
    assert intents.empathy_key("입금했는데 확인이 안 돼요") == "payment"
    assert intents.empathy_key("포가 터져서 왔어요") == "defect"
    assert intents.empathy_key("효과가 없어서 환불하고 싶어요") == "no_effect"


def test_no_empathy_for_plain_questions_or_other_store():
    for t in ("주문한 거 언제 와요?", "카드 결제 되나요?", "환불하고 싶어요", "배송비 얼마예요?",
              "간 영양제 효과 있나요?", "쿠팡에서 산 게 아직 안 왔어요", "안녕하세요", ""):
        assert intents.empathy_key(t) is None, t


def test_every_situation_has_a_phrase_without_numbers():
    import re
    keys = {k for k, _ in intents._EMPATHY_RULES}
    assert keys == set(knowledge.EMPATHY_PHRASES)
    assert not any(re.search(r"\d", p) for p in knowledge.EMPATHY_PHRASES.values())


# ── 늦을 때만 ───────────────────────────────────────
async def test_slow_answer_gets_empathy_first():
    sink = Order()
    r = Responder(FakeLLM(text_events("주문번호를 말씀해 주시겠어요?")), SlowTTS(0.4), None, tools=[], settings=S())
    spoken = await r.respond("S", [Message("user", "x")], sink, empathy=(EMP, EMP_PCM),
                             empathy_at=time.monotonic() + 0.05)
    assert spoken == [EMP, "주문번호를 말씀해 주시겠어요?"]
    assert sink.seq[0] == EMP_PCM and r.empathy_used == EMP


async def test_fast_answer_gets_no_empathy():
    sink = Order()
    r = Responder(FakeLLM(text_events("주문번호를 말씀해 주시겠어요?")), SlowTTS(0.0), None, tools=[], settings=S())
    spoken = await r.respond("S", [Message("user", "x")], sink, empathy=(EMP, EMP_PCM),
                             empathy_at=time.monotonic() + 0.3)
    assert spoken == ["주문번호를 말씀해 주시겠어요?"] and r.empathy_used == ""


async def test_answer_apology_is_dropped_after_empathy():
    r = Responder(FakeLLM(text_events("죄송합니다. 주문번호를 말씀해 주시겠어요?")), SlowTTS(0.3), None,
                  tools=[], settings=S(min_sentence_chars=2))

    async def late_llm():                               # 공감 한마디가 먼저 나가고 그 뒤에 LLM 글이 온다
        await asyncio.sleep(0.15)
        for ev in text_events("죄송합니다. ", "주문번호를 말씀해 주시겠어요?"):
            yield ev

    r.llm.stream = lambda *a: late_llm()
    spoken = await r.respond("S", [Message("user", "x")], Sink(), empathy=(EMP, EMP_PCM),
                             empathy_at=time.monotonic() + 0.05)
    assert spoken == [EMP, "주문번호를 말씀해 주시겠어요?"]


async def test_no_empathy_when_waiting_sentence_is_already_an_apology():
    r = Responder(FakeLLM(text_events("불편을 드려 죄송합니다. 확인해 볼게요.")), SlowTTS(0.4), None, tools=[],
                  settings=S())
    spoken = await r.respond("S", [Message("user", "x")], Sink(), empathy=(EMP, EMP_PCM),
                             empathy_at=time.monotonic() + 0.1)
    assert EMP not in spoken


async def test_empathy_replaces_generic_filler_during_lookup(tmp_path):
    s = S()
    cache = TTSCache(tmp_path)
    cache.put(s.fillers[0], b"\x21\x27" * 8)

    async def call_tool(name, args):
        await asyncio.sleep(0.1)                        # 측정기 중계 지연 흉내
        return {"status": "ok", "data": {"orders": []}}

    llm = FakeLLM(tool_events("get_order_info", {"buyer_cellphone": "01011112222"}),
                  text_events("배송 중이에요."))
    r = Responder(llm, SlowTTS(0.0), call_tool, tools=TOOLS3, settings=s, cache=cache)
    spoken = await r.respond("S", [Message("user", "x")], Sink(), empathy=(EMP, EMP_PCM),
                             empathy_at=time.monotonic() + 5)
    assert spoken[0] == EMP and s.fillers[0] not in spoken


def test_apology_free_variant_for_apology_opener():
    assert knowledge.empathy_without_apology("잘못 받으셔서 불편하셨죠, 죄송합니다.") == "잘못 받으셔서 불편하셨죠."
    assert knowledge.empathy_without_apology("많이 기다리셨죠, 죄송합니다.") == "많이 기다리셨죠."
    assert knowledge.empathy_without_apology("효과가 없어 아쉬우셨죠.") == "효과가 없어 아쉬우셨죠."
    assert all(v in knowledge.empathy_variants() for v in knowledge.EMPATHY_PHRASES.values())


def test_refund_deposit_is_refund_late_not_payment():
    """data/jev/filler_scenarios CS012: 환불 입금이 안 된 것은 결제 입금 문제가 아니다."""
    assert intents.empathy_key("환불한다고 했는데 입금이 아직 안 됐어요.") == "refund_late"
    assert intents.empathy_key("입금했는데 확인이 안 돼요") == "payment"


def test_jev_filler_scenarios_empathy_and_hints():
    """사용자가 만든 가상 발화 50개(data/jev/filler_scenarios): 공감 문장은 불편 발화에만, 힌트는 대부분 맞게."""
    import json
    from pathlib import Path
    rows = [json.loads(l) for l in (Path(__file__).resolve().parents[1] / "data/jev/filler_scenarios/scenarios.jsonl")
            .read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(rows) == 50 and all(r["synthetic"] for r in rows)
    picked = {r["id"]: intents.empathy_key(r["utterance"]) for r in rows}
    assert picked["CS003"] == "delay" and picked["CS025"] == "defect" and picked["CS012"] == "refund_late"
    assert sum(1 for v in picked.values() if v) <= 8          # 대부분은 공감 문장 없이(단순 문의)


def test_empathy_once_per_call_survives_turn_reset():
    """한 통화에 한 번: 턴이 바뀌어도(_reset_turn) 이미 썼다는 표시는 남는다."""
    import inspect
    from agent.session import Session
    assert "_empathy_done" not in inspect.getsource(Session._reset_turn)
    assert "_empathy_done = False" in inspect.getsource(Session.__init__)


async def test_urgent_phrase_arriving_with_first_chunk_is_still_sent():
    """첫 조각이 도착한 것과 같은 순간에 들어온 공감 한마디도 버리지 않고 그 앞에 보낸다."""
    import asyncio
    from agent.config import Settings
    from agent.responder import _Speaker

    sent: list[bytes] = []

    async def sink(pcm: bytes) -> None:
        sent.append(pcm)

    sp = _Speaker(None, sink, [], Settings(), None)
    try:
        payload: asyncio.Queue = asyncio.Queue()
        payload.put_nowait(b"\x01\x00" * 160)
        sp.waiting_text = "배송 중입니다."
        task = asyncio.ensure_future(sp._first_chunk(payload, "배송 중입니다."))
        await asyncio.sleep(0)                       # 조각은 이미 와 있고, 아직 돌려주기 전
        assert sp.play_urgent("많이 기다리셨죠.", b"\x02\x00" * 160) is True
        chunk = await task
        assert sent == [b"\x02\x00" * 160] and chunk == b"\x01\x00" * 160
        assert sp.spoken == ["많이 기다리셨죠."]
    finally:
        sp.task.cancel()
