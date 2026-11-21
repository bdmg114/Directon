"""AG-T59 통화 체감: 채움말·첫 마디가 실제 상황과 맞는지, 자주 하는 문장은 캐시에서 바로 나가는지."""
import asyncio

from agent import knowledge
from agent.config import Settings
from agent.core.types import Message
from agent.responder import Responder
from agent.tts_cache import TTSCache, opener_kind, select_opener_for_text
from agent.turn_log import TurnLog
from tests.test_responder import TOOLS3, FakeLLM, FakeTTS, Sink, text_events, tool_events


def S(**kw):
    base = dict(filler_delay_ms=30, llm_timeout_s=2.0, tts_timeout_s=2.0, min_sentence_chars=6)
    base.update(kw)
    return Settings(**base)


# ── 첫 마디: "확인해 드릴게요"는 조회가 실제로 돌 때만 ─────────
def test_lookup_opener_needs_a_number_to_look_up():
    s = Settings()
    base = s.openers[0]
    # 번호가 없고 쓸 번호도 없다 → 번호를 물어야 하므로 "확인해 드릴게요"를 쓰지 않는다
    assert select_opener_for_text("주문 취소하고 싶어요", s, default=base, can_lookup=False) == base
    assert opener_kind("주문 취소하고 싶어요", s, can_lookup=False) == "simple"
    # 이번 말에 번호가 있으면 조회한다
    assert select_opener_for_text("주문 조회요 공일공 일이삼사 오육칠팔", s, default=base,
                                  can_lookup=False) == s.opener_lookup
    # 발신자 번호를 알면 번호 없이도 조회한다
    assert select_opener_for_text("배송 언제 와요 주문한 거", s, default=base, can_lookup=True) == s.opener_lookup


def test_policy_question_never_gets_lookup_opener():
    s = Settings()
    base = s.openers[0]
    # 반품 가능 여부는 조회 없이 브랜드 정보로 답한다(번호를 알아도)
    assert select_opener_for_text("뜯어서 먹어 봤는데 환불 가능한가요? 비용 드나요?", s, default=base,
                                  can_lookup=True) == base


def test_old_callers_keep_old_behavior():
    s = Settings()
    assert select_opener_for_text("주문 조회해 주세요", s, default=s.openers[0]) == s.opener_lookup


# ── 결과 문장 뒤에 "확인해 드릴게요, 잠시만요"를 내지 않는다 ─────
async def test_no_filler_after_result_sentence(tmp_path):
    s = S()
    cache = TTSCache(tmp_path)
    fail = knowledge.FAIL_SENTENCES["not_found"]
    for i, p in enumerate((s.fillers[0], fail, s.post_tool_ok)):
        cache.put(p, bytes([0x20 + i, 0x27]) * 8)

    async def call_tool(name, args):                      # 데모 Mock처럼 즉시 답한다
        return {"status": "not_found", "data": None, "message": "없음"}

    class SlowSecond(FakeLLM):
        async def stream(self, system, messages, tools):
            if len(self.calls) == 1:                      # 결과 뒤 답이 늦다 → 예전엔 이 사이에 채움말이 나갔다
                await asyncio.sleep(0.15)
            async for ev in super().stream(system, messages, tools):
                yield ev

    llm = SlowSecond(tool_events("get_order_info", {"buyer_cellphone": "01011112222"}),
                     text_events("다른 연락처를 말씀해 주시겠어요?"))
    log = TurnLog("S")
    spoken = await Responder(llm, FakeTTS(), call_tool, tools=TOOLS3, settings=s, cache=cache).respond(
        "S", [Message("user", "x")], Sink(), log=log)
    assert s.fillers[0] not in spoken
    assert spoken[0] == fail and spoken[-1] == "다른 연락처를 말씀해 주시겠어요?"


# ── 자주 하는 문장은 캐시에서 바로, 새 일반 문장은 남긴다 ─────────
async def test_cached_sentence_skips_tts_and_new_plain_sentence_is_learned(tmp_path):
    s = S()
    cache = TTSCache(tmp_path)
    common = knowledge.COMMON_SENTENCES[0]
    cache.put(common, b"\x30\x27" * 8)
    tts = FakeTTS()
    llm = FakeLLM(text_events(common))
    spoken = await Responder(llm, tts, None, tools=[], settings=s, cache=cache).respond(
        "S", [Message("user", "x")], Sink())
    assert spoken == [common] and tts.requests == []      # TTS를 부르지 않았다

    llm2 = FakeLLM(text_events("아쉽지만 성함만으로는 조회가 어려워요."))
    await Responder(llm2, tts, None, tools=[], settings=s, cache=cache).respond("S", [Message("user", "x")], Sink())
    assert cache.get("아쉽지만 성함만으로는 조회가 어려워요.") is not None


async def test_sentences_with_numbers_or_caller_name_are_not_learned(tmp_path):
    from agent.guards import GuardContext
    s = S()
    cache = TTSCache(tmp_path)
    guard = GuardContext(tools=[], caller={"name": "홍길동", "phone": "01011112222"})
    for line in ("010, 1111, 2222번으로 접수했어요.", "홍길동 고객님 주문이 확인됐어요.", "공일공 일일일일 번호로 볼게요."):
        await Responder(FakeLLM(text_events(line)), FakeTTS(), None, tools=[], settings=s, cache=cache,
                        guard=guard).respond("S", [Message("user", "x")], Sink())
        assert cache.get(line) is None, line


def test_common_sentences_are_in_rules_verbatim():
    """미리 합성한 문장이 규칙에 그대로 있어야 LLM이 같은 글자로 말하고 캐시가 맞는다(첫 문장만)."""
    assert knowledge.COMMON_SENTENCES[0] in knowledge.RULES


# ── 2026-11-21 데모 녹음에서 나온 번호 문제 ─────────────
def test_masked_or_missing_phone_is_restored_from_what_customer_said():
    from agent.guards import GuardContext, check
    for args in ({"buyer_cellphone": "[PHONE]"}, {}):
        v = check("get_order_info", args, GuardContext(tools=[], user_text="010-9002-5517이요."))
        assert v.blocked is None and v.args == {"buyer_cellphone": "01090025517"}


def test_short_year_order_number_becomes_db_format():
    from agent.guards import GuardContext, check
    from agent.responder import utterance_numbers
    assert utterance_numbers("260929. 0004550. 이요.", 7) == ["2609290004550"]     # 떨어진 '이요'는 숫자 2가 아니다
    v = check("get_order_info", {"order_id": "260929-0004550"}, GuardContext(tools=[], user_text="260929. 0004550. 이요."))
    assert v.args == {"order_id": "20260929-0004550"}


def test_empty_action_number_is_not_filled_from_speech():
    from agent.guards import GuardContext, check
    v = check("request_callback", {"customer_name": "", "phone_number": ""},
              GuardContext(tools=[], user_text="010-9002-5517이요"))
    assert v.blocked is not None


def test_number_read_with_rising_tone_is_not_a_finished_question():
    from agent.turn.detector import digits_partial
    from agent.turn.rules import analyze
    assert analyze("2026-09-29?").kind != "question" and digits_partial("2026-09-29?")
    assert analyze("배송 언제 와요?").kind == "question"
    assert analyze("260929 0004550").kind == "digits_complete"


async def test_no_double_yes_after_opener(tmp_path):
    s = S()
    spoken = ["네, 고객님."]
    out = await Responder(FakeLLM(text_events("네, 천천히 말씀해 주세요.")), FakeTTS(), None, tools=[], settings=s,
                          cache=None).respond("S", [Message("user", "x")], Sink(), spoken=spoken)
    assert out == ["네, 고객님.", "천천히 말씀해 주세요."]
