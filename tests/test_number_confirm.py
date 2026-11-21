"""AG-T56 번호 되묻기·응답 지연: 들린 번호로 조회 채우기, 들은 번호 되읽기, 결과 문구 뒤 채움말 생략, 답 문장 캐시."""
import asyncio

from agent.core.types import LLMEvent, Message
from agent.guards import GuardContext, check, heard_number
from agent.responder import Responder
from agent.tts_cache import TTSCache

from test_responder import FakeLLM, FakeTTS, Sink, settings, text_events, tool_events

OTOOLS = [{"type": "function", "function": {
    "name": "get_order_info", "description": "주문 조회",
    "parameters": {"type": "object", "properties": {"order_id": {"type": "string"},
                                                    "buyer_cellphone": {"type": "string"}}}}}]


def test_heard_number_reads_dotted_stt_phone():
    assert heard_number(GuardContext(tools=[], user_text="010. 2215. 7723이요.")) == ("phones", "01022157723")
    assert heard_number(GuardContext(tools=[], user_text="배송 언제 와요?")) is None


def test_lookup_without_number_uses_heard_phone():
    v = check("get_order_info", {}, GuardContext(tools=[], user_text="010. 2215. 7723이요."))
    assert v.blocked is None
    assert v.args == {"buyer_cellphone": "01022157723"}


async def test_not_found_on_heard_number_reads_it_back_and_ends_turn():
    guard = GuardContext(tools=[], user_text="공일공 이이일오 칠칠이삼이요")
    llm = FakeLLM(tool_events("get_order_info", {"buyer_cellphone": "01022157723"}), text_events("다른 말입니다."))

    async def tool(name, args):
        return {"status": "not_found"}

    r = Responder(llm, FakeTTS(), tool, tools=OTOOLS, settings=settings(), guard=guard)
    spoken = await r.respond("S", [Message("user", guard.user_text)], Sink())
    assert len(llm.calls) == 1                         # LLM 회차를 더 돌지 않는다
    assert spoken[-2].startswith("제가 들은 번호는 ") and spoken[-2].endswith("입니다.")
    assert spoken[-1] == "맞으실까요?"


async def test_not_found_on_caller_number_goes_to_llm():
    guard = GuardContext(tools=[], user_text="주문 확인해 주세요", caller={"name": "", "phone": "01055551234"})
    llm = FakeLLM(tool_events("get_order_info", {"buyer_cellphone": "01055551234"}),
                  text_events("그 번호로는 주문이 없어요."))

    async def tool(name, args):
        return {"status": "not_found"}

    r = Responder(llm, FakeTTS(), tool, tools=OTOOLS, settings=settings(), guard=guard)
    spoken = await r.respond("S", [Message("user", guard.user_text)], Sink())
    assert len(llm.calls) == 2
    assert not any("들은 번호" in x for x in spoken)


async def test_no_second_readback_after_confirm_question():
    hist = [{"role": "agent", "text": "제가 들은 번호는 공일공, 이이일오, 칠칠이삼입니다. 맞으실까요?"}]
    guard = GuardContext(tools=[], user_text="네 공일공 이이일오 칠칠이삼 맞아요", history=hist)
    llm = FakeLLM(tool_events("get_order_info", {"buyer_cellphone": "01022157723"}),
                  text_events("다른 연락처나 주문번호를 말씀해 주시겠어요?"))

    async def tool(name, args):
        return {"status": "not_found"}

    r = Responder(llm, FakeTTS(), tool, tools=OTOOLS, settings=settings(), guard=guard)
    spoken = await r.respond("S", [Message("user", guard.user_text)], Sink())
    assert not any(x.startswith("제가 들은 번호") for x in spoken)


async def test_confirm_switch_off():
    guard = GuardContext(tools=[], user_text="공일공 이이일오 칠칠이삼이요")
    llm = FakeLLM(tool_events("get_order_info", {"buyer_cellphone": "01022157723"}), text_events("없어요."))

    async def tool(name, args):
        return {"status": "not_found"}

    r = Responder(llm, FakeTTS(), tool, tools=OTOOLS, settings=settings(confirm_heard_number=False), guard=guard)
    await r.respond("S", [Message("user", guard.user_text)], Sink())
    assert len(llm.calls) == 2


class SlowSecondLLM(FakeLLM):
    async def stream(self, system, messages, tools):
        if self.calls:
            await asyncio.sleep(0.25)
        async for ev in super().stream(system, messages, tools):
            yield ev


async def test_filler_not_played_after_result_phrase(tmp_path):
    cache = TTSCache(tmp_path)
    filler = "확인해 드릴게요, 잠시만요."
    s = settings(filler_delay_ms=50, fillers=(filler,))
    cache.put(filler, b"\xaa\x55" * 8)
    cache.put(s.post_tool_ok, b"\xbb\x66" * 8)

    async def tool(name, args):
        return {"status": "ok"}

    llm = SlowSecondLLM(tool_events("get_order_info", {"order_id": "1"}), text_events("배송 중입니다."))
    r = Responder(llm, FakeTTS(), tool, tools=OTOOLS, cache=cache, settings=s)
    spoken = await r.respond("S", [Message("user", "x")], Sink())
    assert s.post_tool_ok in spoken
    assert filler not in spoken                        # 결과 문구를 냈으니 채움말은 생략


async def test_cached_answer_sentence_skips_tts(tmp_path):
    cache = TTSCache(tmp_path)
    ask = "주문번호나 주문하신 분 연락처를 말씀해 주시겠어요?"
    pcm = cache.put(ask, b"\xcc\x77" * 800)
    tts = FakeTTS()
    llm = FakeLLM(text_events(ask))
    r = Responder(llm, tts, None, tools=[], cache=cache, settings=settings())
    sink = Sink()
    spoken = await r.respond("S", [Message("user", "배송 언제 와요")], sink)
    assert spoken == [ask]
    assert ask not in tts.requests
    assert pcm and pcm in sink.data


# ── AG-T57 KT 통화 피드백·예시 point 반영 ──
def test_tts_text_turns_digit_hyphen_into_pause():
    from agent.responder import tts_text
    assert tts_text("고객센터 02-000-0000번") == "고객센터 02, 000, 0000번"
    assert tts_text("주문번호 20260929 - 0022095") == "주문번호 20260929, 0022095"
    assert tts_text("1+1 행사, 하루-이틀") == "1+1 행사, 하루-이틀"


def test_rules_reflect_call_feedback_and_example_points():
    from agent import knowledge as K
    for phrase in ("같은 질문을 두 번 하지 않는다", "고객 탓으로 들리는 말", "아니에요 괜찮습니다", "거절을 우선한다",
                   "확인 질문은 최소로 한다", "아직 받지 않은 가장 최근 주문", "배송지를 바꿀 수 없다고 안내한 뒤 상담원 연결",
                   "주문번호를 먼저 묻고", "하이픈(-) 없이", "출고 전이라는 것"):
        assert phrase in K.RULES, phrase


# ── 010을 빼고 말한 휴대폰 번호(데모 피드백) ──
def test_phone_without_010_prefix_is_completed():
    v = check("get_order_info", {"buyer_cellphone": "4777-5671"},
              GuardContext(tools=[], user_text="전화번호 4777-5671로 주문했는데 제가 뭘 시켰죠?"))
    assert v.blocked is None and v.args == {"buyer_cellphone": "01047775671"}


def test_heard_number_completes_eight_digit_phone_only_with_phone_words():
    assert heard_number(GuardContext(tools=[], user_text="전화번호 4777-5671이요")) == ("phones", "01047775671")
    assert heard_number(GuardContext(tools=[], user_text="4777-5671이요")) is None


def test_masked_phone_from_gateway_is_restored():
    v = check("get_order_info", {"buyer_cellphone": "[PHONE]"},
              GuardContext(tools=[], user_text="010-4777-5671이요."))
    assert v.blocked is None and v.args == {"buyer_cellphone": "01047775671"}
