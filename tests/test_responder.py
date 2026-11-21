"""AG-T07 responder: LLM 툴 루프, 문장 분할, TTS 파이프라인, 필러, 정규화 훅
+ tts_cache / prompts / turn_log 단위 테스트."""
import asyncio
import json

import httpx
import pytest

from agent.config import Settings
from agent.core.types import LLMEvent, Message, SessionInfo, ToolCall
from agent.responder import Responder, SentenceSplitter
from agent.tools_client import ToolsClient
from agent.tts_cache import TTSCache, select_opener
from agent.turn_log import TurnLog, TurnLogWriter

TOOLS = [{"type": "function", "function": {
    "name": "get_order_info", "description": "주문 조회",
    "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}},
                   "required": ["order_id"]}}}]


def pcm_for(text: str) -> bytes:
    return b"\x10\x27" * 4 + text.encode("utf-8")


# ── fakes ────────────────────────────────────────────
class FakeLLM:
    """scripts: 호출마다 이벤트 목록(또는 예외/'hang'). 호출 기록을 남긴다."""

    def __init__(self, *scripts):
        self.scripts = list(scripts)
        self.calls: list[dict] = []

    async def stream(self, system, messages, tools):
        self.calls.append({"system": system, "messages": [Message(**vars(m)) for m in messages],
                           "tools": tools})
        script = self.scripts[min(len(self.calls) - 1, len(self.scripts) - 1)]
        if isinstance(script, Exception):
            raise script
        if script == "hang":
            await asyncio.Event().wait()
        for ev in script:
            await asyncio.sleep(0)
            yield ev


def text_events(*chunks):
    return [LLMEvent("text", text=c) for c in chunks] + [LLMEvent("done")]


def tool_events(name, args, cid="c1", pre_text=""):
    evs = [LLMEvent("text", text=pre_text)] if pre_text else []
    return evs + [LLMEvent("tool_call", tool_call=ToolCall(cid, name, args)), LLMEvent("done")]


class FakeTTS:
    def __init__(self, fail=False):
        self.requests: list[str] = []
        self.fail = fail

    async def synthesize(self, text):
        self.requests.append(text)
        if self.fail:
            raise RuntimeError("tts down")
        data = pcm_for(text)
        await asyncio.sleep(0)
        yield data[:4]
        await asyncio.sleep(0)
        yield data[4:]


class Sink:
    def __init__(self):
        self.chunks: list[bytes] = []

    async def __call__(self, pcm: bytes):
        self.chunks.append(pcm)

    @property
    def data(self):
        return b"".join(self.chunks)


def settings(**kw):
    base = dict(filler_delay_ms=700, llm_timeout_s=2.0, tts_timeout_s=2.0, min_sentence_chars=6)
    base.update(kw)
    return Settings(**base)


async def no_tool(name, args):  # pragma: no cover - 호출되면 실패
    raise AssertionError("tool should not be called")


# ── 문장 분할 ─────────────────────────────────────────
def test_splitter_basic_boundaries_and_merge_short():
    sp = SentenceSplitter(min_chars=6)
    out = []
    for ch in ["네. 주문하신 상품은 ", "내일 도착합니다. 다른 문의", " 있으신가요? 감사합", "니다!"]:
        out += sp.feed(ch)
    out += sp.flush()
    assert out == ["네. 주문하신 상품은 내일 도착합니다.", "다른 문의 있으신가요?", "감사합니다!"]


def test_splitter_does_not_split_decimal_and_flushes_tail():
    sp = SentenceSplitter(min_chars=6)
    out = sp.feed("금액은 1.5만 원이에요. 배송비는 무료")
    assert out == ["금액은 1.5만 원이에요."]
    assert sp.flush() == ["배송비는 무료"]


def test_splitter_strips_markdown():
    sp = SentenceSplitter(min_chars=1)
    out = sp.feed("**배송** 중입니다. ") + sp.flush()
    assert out == ["배송 중입니다."]


# ── 응답기 ───────────────────────────────────────────
async def test_respond_text_sentences_go_to_tts_in_order():
    llm = FakeLLM(text_events("주문하신 상품은 내일 ", "도착합니다. 다른 문의 있으신가요?"))
    tts = FakeTTS()
    r = Responder(llm, tts, no_tool, tools=TOOLS, settings=settings())
    sink = Sink()
    spoken = await r.respond("SYS", [Message("user", "언제 와요?")], sink)
    assert spoken == ["주문하신 상품은 내일 도착합니다.", "다른 문의 있으신가요?"]
    assert tts.requests == spoken
    assert sink.data == pcm_for(spoken[0]) + pcm_for(spoken[1])
    assert llm.calls[0]["tools"] == TOOLS and llm.calls[0]["system"] == "SYS"


async def test_tool_loop_calls_mock_with_session_header_and_feeds_result_back():
    seen = []

    def handler(req: httpx.Request):
        seen.append((req.url.path, req.headers.get("X-Session-Id"), json.loads(req.content)))
        return httpx.Response(200, json={"status": "ok", "delivery": "배송중"})

    client = ToolsClient(http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    async def call_tool(name, args):
        return await client.call("http://mock:8100", "S-42", name, args)

    llm = FakeLLM(tool_events("get_order_info", {"order_id": "2611150231"}),
                  text_events("현재 배송 중입니다. 내일 도착 예정이에요."))
    tts = FakeTTS()
    r = Responder(llm, tts, call_tool, tools=TOOLS, settings=settings())
    sink = Sink()
    spoken = await r.respond("SYS", [Message("user", "배송 어디쯤이에요?")], sink)

    assert seen == [("/tools/get_order_info", "S-42", {"order_id": "2611150231"})]
    assert len(llm.calls) == 2
    msgs = llm.calls[1]["messages"]
    assert msgs[0].role == "user"
    assert msgs[1].role == "assistant" and msgs[1].tool_calls[0].name == "get_order_info"
    assert msgs[2].role == "tool" and msgs[2].tool_call_id == "c1" and msgs[2].name == "get_order_info"
    assert json.loads(msgs[2].content) == {"status": "ok", "delivery": "배송중"}
    assert spoken == ["현재 배송 중입니다.", "내일 도착 예정이에요."]




async def test_default_normalize_is_identity():
    calls = []

    async def call_tool(name, args):
        calls.append(args)
        return {"status": "ok"}

    llm = FakeLLM(tool_events("get_order_info", {"order_id": "공일"}), text_events("네 확인했습니다."))
    await Responder(llm, FakeTTS(), call_tool, tools=TOOLS, settings=settings()).respond(
        "S", [Message("user", "x")], Sink())
    assert calls == [{"order_id": "공일"}]


async def test_max_three_rounds_last_round_without_tools():
    async def call_tool(name, args):
        return {"status": "ok"}

    loop = tool_events("get_order_info", {"order_id": "1"})
    llm = FakeLLM(loop, loop, text_events("확인 결과를 안내드릴게요."))
    r = Responder(llm, FakeTTS(), call_tool, tools=TOOLS, settings=settings(max_tool_rounds=3))
    spoken = await r.respond("S", [Message("user", "x")], Sink())
    assert len(llm.calls) == 3
    assert llm.calls[2]["tools"] == []
    assert spoken == ["확인 결과를 안내드릴게요."]


async def test_filler_played_when_tool_slow(tmp_path):
    cache = TTSCache(tmp_path)
    filler = "확인해 드릴게요, 잠시만요."
    cache.put(filler, b"\xaa\x55" * 8)

    async def slow_tool(name, args):
        await asyncio.sleep(0.3)
        return {"status": "ok"}

    llm = FakeLLM(tool_events("get_order_info", {"order_id": "1"}), text_events("배송 중입니다. 감사합니다."))
    r = Responder(llm, FakeTTS(), slow_tool, tools=TOOLS, cache=cache,
                  settings=settings(filler_delay_ms=50, fillers=(filler,)))
    sink = Sink()
    spoken = await r.respond("S", [Message("user", "x")], sink)
    assert spoken[0] == filler
    assert sink.chunks[0] == b"\xaa\x55" * 8


async def test_no_filler_when_tool_fast(tmp_path):
    cache = TTSCache(tmp_path)
    filler = "확인해 드릴게요, 잠시만요."
    cache.put(filler, b"\xaa\x55" * 8)

    async def fast_tool(name, args):
        return {"status": "ok"}

    llm = FakeLLM(tool_events("get_order_info", {"order_id": "1"}), text_events("배송 중입니다."))
    r = Responder(llm, FakeTTS(), fast_tool, tools=TOOLS, cache=cache,
                  settings=settings(filler_delay_ms=500, fillers=(filler,)))
    spoken = await r.respond("S", [Message("user", "x")], Sink())
    assert filler not in spoken


async def test_tts_pipeline_starts_next_sentence_before_previous_finishes():
    started = {}

    class GatedTTS:
        async def synthesize(self, text):
            started[text] = asyncio.get_running_loop().time()
            if text.startswith("첫"):
                # 다음 문장 합성이 미리 시작되지 않으면 여기서 타임아웃
                for _ in range(200):
                    if any(t.startswith("둘") for t in started):
                        break
                    await asyncio.sleep(0.005)
                else:
                    raise AssertionError("next sentence synthesis not started early")
            yield pcm_for(text)

    llm = FakeLLM(text_events("첫 번째 문장입니다. ", "둘째 문장도 있어요."))
    r = Responder(llm, GatedTTS(), no_tool, tools=TOOLS, settings=settings())
    sink = Sink()
    spoken = await r.respond("S", [Message("user", "x")], sink)
    assert spoken == ["첫 번째 문장입니다.", "둘째 문장도 있어요."]
    assert sink.data == pcm_for(spoken[0]) + pcm_for(spoken[1])


async def test_llm_error_propagates():
    r = Responder(FakeLLM(RuntimeError("llm down")), FakeTTS(), no_tool, tools=TOOLS, settings=settings())
    with pytest.raises(Exception):
        await r.respond("S", [Message("user", "x")], Sink())


async def test_llm_hang_times_out():
    r = Responder(FakeLLM("hang"), FakeTTS(), no_tool, tools=TOOLS, settings=settings(llm_timeout_s=0.05))
    with pytest.raises((asyncio.TimeoutError, TimeoutError)):
        await asyncio.wait_for(r.respond("S", [Message("user", "x")], Sink()), 2.0)


async def test_tts_error_propagates_and_spoken_keeps_partial():
    r = Responder(FakeLLM(text_events("안내 드리겠습니다.")), FakeTTS(fail=True), no_tool,
                  tools=TOOLS, settings=settings())
    spoken: list[str] = []
    with pytest.raises(Exception):
        await r.respond("S", [Message("user", "x")], Sink(), spoken=spoken)
    assert spoken == []


async def test_prefetch_is_reused_without_second_llm_call():
    llm = FakeLLM(text_events("네 바로 확인해 드리겠습니다."))
    r = Responder(llm, FakeTTS(), no_tool, tools=TOOLS, settings=settings())
    msgs = [Message("user", "배송 언제 와요?")]
    pf = r.prefetch("S", msgs)
    await asyncio.sleep(0.01)          # 선호출이 버퍼에 쌓인다
    spoken = await r.respond("S", msgs, Sink(), prefetch=pf)
    assert len(llm.calls) == 1
    assert spoken == ["네 바로 확인해 드리겠습니다."]


async def test_prefetch_cancel_stops_stream():
    cancelled = asyncio.Event()

    class HangLLM:
        async def stream(self, system, messages, tools):
            try:
                await asyncio.Event().wait()
                yield LLMEvent("done")
            finally:
                cancelled.set()

    r = Responder(HangLLM(), FakeTTS(), no_tool, tools=TOOLS, settings=settings())
    pf = r.prefetch("S", [Message("user", "x")])
    await asyncio.sleep(0.01)
    pf.cancel()
    await asyncio.wait_for(cancelled.wait(), 1.0)


async def test_turn_log_records_llm_and_tts_events():
    async def call_tool(name, args):
        return {"status": "ok"}

    t = iter(range(1000))
    log = TurnLog("S", clock=lambda: next(t) / 1000)
    llm = FakeLLM(tool_events("get_order_info", {"order_id": "1"}), text_events("배송 중입니다."))
    await Responder(llm, FakeTTS(), call_tool, tools=TOOLS, settings=settings()).respond(
        "S", [Message("user", "x")], Sink(), log=log)
    d = log.to_dict()
    names = [e["name"] for e in d["events"]]
    assert "llm_first_token" in d["marks"]
    assert "tool_call" in names and "tool_result" in names
    assert "tts_request" in names and "tts_first_byte" in names


# ── TTS 캐시 ──────────────────────────────────────────
async def test_tts_cache_warm_synthesizes_only_missing_and_persists(tmp_path):
    c = TTSCache(tmp_path)
    c.put("네, 고객님.", b"\x01\x02" * 10)
    tts = FakeTTS()
    n = await c.warm(["네, 고객님.", "잠시만요."], tts)
    assert n == 1 and tts.requests == ["잠시만요."]
    assert len(list(tmp_path.glob("*.pcm"))) == 2
    c2 = TTSCache(tmp_path)                       # 새 인스턴스가 디스크에서 읽는다
    assert c2.get("네, 고객님.") == b"\x01\x02" * 10
    assert c2.get("잠시만요.") is not None
    assert c2.get("없는 문구") is None


async def test_tts_cache_trims_leading_silence(tmp_path):
    c = TTSCache(tmp_path)
    loud = (b"\x00\x10" * 320)          # 4096 진폭
    c.put("x", b"\x00\x00" * 320 * 5 + loud)
    assert c.get("x") == loud


def test_select_opener_default_without_name():
    s = Settings()
    info = SessionInfo("S", caller={"name": "김민수", "phone": "01012345678"})
    assert select_opener(info, s) == s.openers[0]
    assert "김민수" not in select_opener(info, s)
    s2 = Settings(opener_use_name=True)
    assert select_opener(info, s2) == "네, 김민수 고객님."


# ── 프롬프트 ─────────────────────────────────────────




def test_turn_log_writer_appends_jsonl(tmp_path):
    w = TurnLogWriter(tmp_path, date_fn=lambda: "20261121")
    w.write({"session_id": "S", "a": 1})
    w.write({"session_id": "S", "a": 2})
    lines = (tmp_path / "turns-20261121.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(l)["a"] for l in lines] == [1, 2]


# ══════════════════════════════════════════════════════
# 제출일 보강(공식 측정 v5): 글로 쓴 함수 인수, 두 번째 조회 뒤 연결 문구, 실제로 쓴 번호 알림
# ══════════════════════════════════════════════════════
from agent.responder import CALLED_WITH_NOTE, FINAL_ROUND_NOTE, speech_safe  # noqa: E402

TOOLS3 = TOOLS + [
    {"type": "function", "function": {
        "name": "return_order", "description": "반품 접수",
        "parameters": {"type": "object", "properties": {"order_number": {"type": "string"},
                                                        "reason": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "request_callback", "description": "전화 요청 접수",
        "parameters": {"type": "object", "properties": {"customer_name": {"type": "string"},
                                                        "phone_number": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "search_products", "description": "상품 검색",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}}}},
]
OK_PHRASE = "네, 확인했어요."
JSON_ARGS = ('{"ordernumber":"20260929-0022095","reason":"포장해 놨어요. 반품해 주시고 환불해 주세요."}')


def fake_guard(monkeypatch, rule):
    """agent.guards.check를 바꿔 끼운다. rule(name, args) → (실행할 인수, 막았으면 결과 | None)."""
    from agent import guards

    def check(name, args, ctx):
        used, blocked = rule(name, dict(args))
        return guards.Verdict(args=used, blocked=blocked, reason="test" if blocked else "")

    monkeypatch.setattr(guards, "check", check)
    return guards.GuardContext(tools=TOOLS3, user_text="")


def tool_bodies(call) -> list[dict]:
    return [json.loads(m.content) for m in call["messages"] if m.role == "tool"]


# ── 1(a) 조각난 JSON은 읽지 않는다 ───────────────────
@pytest.mark.parametrize("piece", [
    '{"ordernumber":"20260929-0022095","reason":"포장해 놨어요.',     # 여는 중괄호만
    '반품해 주시고 환불해 주세요."}',                                  # 닫는 중괄호만
    '"reason":"포장해 놨어요.',                                        # "키": 꼴
    '환불해 주세요.", "pickup_address": null',
    '반품해 주세요."} েছjson {"ordernumber":"20260929-0022095","reason":"포장해 놨어요.',
    '{"order_id": null}',                                              # 온전한 인수(기존 동작)
    'return_order({"order_number": "20260929-0022095"})',
    'to=functions.return_order',
    'েছjson',
    'เติมเงินไทยฟรี json',
])
def test_speech_safe_drops_call_text_and_json_pieces(piece):
    assert speech_safe(piece, ["return_order"]) == ""


@pytest.mark.parametrize("sentence", [
    '고객님께서 "내일 받고 싶다"고 하셨죠?',
    '배송 상태는 "배송 완료", "배송 중" 두 가지예요.',
    "주문번호 20260929-0022095, 1박스 30,000원이에요.",
    "평일 10:00부터 17:00까지, 점심시간은 12시부터 13시예요.",
    "010, 4833, 4402번으로 접수해 드렸습니다.",
    "하루온 간 가드 맥스2는 1일 1정, 물과 함께 드시면 돼요.",
    "반품 사유를 말씀해 주시겠어요?",
])
def test_speech_safe_keeps_normal_sentences(sentence):
    assert speech_safe(sentence, ["return_order", "get_order_info"]) == sentence


def test_speech_safe_strips_other_scripts_but_keeps_korean():
    assert speech_safe("네, เติมเงิน 확인했어요.") == "네, 확인했어요."
    assert speech_safe('주문번호를 {"order_id": null} 알려 주시겠어요?') == "주문번호를 알려 주시겠어요?"


async def test_json_args_split_by_sentence_are_not_spoken_and_apology_is_said():
    """P0074-01: 인수 값 안의 마침표에서 문장이 잘려도 한 조각도 읽지 않는다. 아무 말도 못 했으면 안전 문장."""
    s = settings()
    llm = FakeLLM(text_events(JSON_ARGS + " েছjson ", JSON_ARGS + " json " + JSON_ARGS))
    tts = FakeTTS()
    log = TurnLog("S")
    spoken = await Responder(llm, tts, no_tool, tools=TOOLS3, settings=s).respond(
        "S", [Message("user", "반품해 주세요")], Sink(), log=log)
    assert spoken == [s.apology]
    assert tts.requests == [s.apology]
    assert "json_text_dropped" in [e["name"] for e in log.to_dict()["events"]]


async def test_sentences_before_json_piece_are_kept_and_rest_of_round_dropped():
    llm = FakeLLM(text_events('주문 확인했어요. {"reason":"포장해 놨어요. 반품해 주세요. 환불도 해 주세요."} 접수해 드렸습니다.'))
    tts = FakeTTS()
    spoken = await Responder(llm, tts, no_tool, tools=TOOLS3, settings=settings()).respond(
        "S", [Message("user", "x")], Sink())
    assert spoken == ["주문 확인했어요."]              # 글로 쓴 호출 뒤의 "접수해 드렸습니다"는 실행 없는 말이라 버린다


async def test_json_mute_is_per_round(monkeypatch):
    """앞 회차에서 JSON 조각이 나와도 다음 회차의 답은 그대로 말한다."""
    async def call_tool(name, args):
        return {"status": "ok"}

    llm = FakeLLM(tool_events("get_order_info", {"order_id": "1"}, pre_text='{"reason":"포장해 놨어요. 반품'),
                  text_events("주문을 확인했어요. 반품 사유를 말씀해 주시겠어요?"))
    spoken = await Responder(llm, FakeTTS(), call_tool, tools=TOOLS3, settings=settings()).respond(
        "S", [Message("user", "x")], Sink())
    assert spoken == ["주문을 확인했어요.", "반품 사유를 말씀해 주시겠어요?"]


# ── 1(b) 마지막 회차는 말로 답하게 한다 ──────────────
async def test_blocked_call_then_last_round_is_told_to_answer_in_words(monkeypatch):
    ran = []

    async def call_tool(name, args):
        ran.append(name)
        return {"status": "ok", "orders": [{"주문번호": "20260929-0022095"}]}

    guard = fake_guard(monkeypatch, lambda name, args: (
        args, {"status": "blocked", "message": "사유를 먼저 여쭤보세요."} if name == "return_order" else None))
    llm = FakeLLM(tool_events("get_order_info", {"buyer_cellphone": "01062096654"}),
                  tool_events("return_order", {"order_number": "20260929-0022095", "reason": "포장"}, cid="c2"),
                  text_events("주문을 확인했어요. 반품 사유를 말씀해 주시겠어요?"))
    r = Responder(llm, FakeTTS(), call_tool, tools=TOOLS3, settings=settings(max_tool_rounds=3), guard=guard)
    spoken = await r.respond("SYS", [Message("user", "반품해 주세요")], Sink())

    assert ran == ["get_order_info"]                   # 막힌 접수는 실행하지 않았다
    assert len(llm.calls) == 3                          # 상한 그대로(회차가 늘지 않는다)
    assert llm.calls[0]["system"] == "SYS" and llm.calls[1]["system"] == "SYS"
    assert llm.calls[1]["tools"] == TOOLS3
    assert llm.calls[2]["tools"] == []
    assert llm.calls[2]["system"] == "SYS" + FINAL_ROUND_NOTE
    assert "함수를 부르지 않고 말로만 답한다" in FINAL_ROUND_NOTE and "blocked" in FINAL_ROUND_NOTE
    assert tool_bodies(llm.calls[2])[-1]["status"] == "blocked"     # 막힌 이유는 그대로 전달
    assert spoken == ["주문을 확인했어요.", "반품 사유를 말씀해 주시겠어요?"]
    assert [e["blocked"] for e in r.tool_log] == [False, True]


async def test_last_round_without_blocked_call_gets_no_note():
    """조회만 두 번 한 턴(P0158-01·P0166-01)은 지금처럼 부른다: 실측에서 이 경우엔 호출을 글로 쓰지 않았고(21회 중 0회),
    안내 글을 붙이면 답이 두루뭉술해졌다(제품명을 되묻는 답 21회 중 5회)."""
    async def call_tool(name, args):
        return {"status": "ok"}

    loop = tool_events("search_products", {"query": "간"})
    llm = FakeLLM(loop, loop, text_events("확인된 정보가 없어 상담원에게 연결해 드리겠습니다."))
    await Responder(llm, FakeTTS(), call_tool, tools=TOOLS3, settings=settings()).respond(
        "SYS", [Message("user", "x")], Sink())
    assert llm.calls[2]["tools"] == [] and llm.calls[2]["system"] == "SYS"


async def test_single_round_setting_has_no_final_note():
    llm = FakeLLM(text_events("안내해 드릴게요."))
    await Responder(llm, FakeTTS(), no_tool, tools=TOOLS3, settings=settings(max_tool_rounds=1)).respond(
        "SYS", [Message("user", "x")], Sink())
    assert llm.calls[0]["system"] == "SYS"


# ── 1(c) 아무 말도 못 했으면 안전 문장 ───────────────
async def test_all_calls_blocked_and_no_words_says_apology(monkeypatch):
    s = settings()
    guard = fake_guard(monkeypatch, lambda name, args: (args, {"status": "blocked", "message": "막힘"}))
    blocked = tool_events("return_order", {"order_number": "1", "reason": "x"})
    llm = FakeLLM(blocked, blocked, text_events(JSON_ARGS))
    log = TurnLog("S")
    r = Responder(llm, FakeTTS(), no_tool, tools=TOOLS3, settings=s, guard=guard)
    spoken = await r.respond("S", [Message("user", "x")], Sink(), log=log)
    assert spoken == [s.apology]
    assert "empty_response" in [e["name"] for e in log.to_dict()["events"]]


async def test_executed_then_blocked_then_json_says_cached_apology(tmp_path, monkeypatch):
    """P0074-01 그대로: 조회 실행 → 접수 막힘 → 마지막 회차가 인수를 글로 씀. 캐시에 있는 사과 문구를 바로 낸다."""
    s = settings()
    cache = TTSCache(tmp_path)
    cache.put(OK_PHRASE, b"\xaa\x55" * 8)
    cache.put(s.apology, b"\x33\x44" * 8)

    async def call_tool(name, args):
        return {"status": "ok"}

    guard = fake_guard(monkeypatch, lambda name, args: (
        args, {"status": "blocked", "message": "막힘"} if name == "return_order" else None))
    llm = FakeLLM(tool_events("get_order_info", {"order_id": "1"}),
                  tool_events("return_order", {"order_number": "1", "reason": "x"}, cid="c2"),
                  text_events(JSON_ARGS + " json ", JSON_ARGS, " json " + JSON_ARGS))
    tts = FakeTTS()
    sink = Sink()
    spoken = await Responder(llm, tts, call_tool, tools=TOOLS3, settings=s, cache=cache, guard=guard).respond(
        "S", [Message("user", "x")], sink)
    assert spoken == [OK_PHRASE, s.apology]
    assert tts.requests == []                           # 인수 글은 합성 요청조차 하지 않는다
    assert sink.chunks[-1] == b"\x33\x44" * 8


async def test_empty_llm_answer_says_apology_instead_of_silence():
    s = settings()
    spoken = await Responder(FakeLLM([LLMEvent("done")]), FakeTTS(), no_tool, tools=TOOLS3, settings=s).respond(
        "S", [Message("user", "x")], Sink())
    assert spoken == [s.apology]


# ── 2 두 번째 함수 결과 뒤 연결 문구 ─────────────────
def bridge_cache(tmp_path, s, *extra):
    cache = TTSCache(tmp_path)
    for i, p in enumerate((s.post_tool_ok, s.post_tool_more, *extra)):
        cache.put(p, bytes([0x10 + i, 0x27]) * 8)
    return cache


def test_second_bridge_phrase_is_a_setting_and_is_warmed():
    s = Settings()
    assert s.post_tool_more and s.post_tool_more != s.post_tool_ok
    assert s.post_tool_ok in s.cached_phrases() and s.post_tool_more in s.cached_phrases()
    assert Settings.from_env({"DIRECTON_POST_TOOL_MORE": "조금만 더 확인해 볼게요."}).post_tool_more == \
        "조금만 더 확인해 볼게요."


async def test_bridge_after_each_tool_round_with_different_phrases(tmp_path):
    """P0158-01·P0166-01: 조회를 두 회차에 걸쳐 두 번 하면 두 번째 결과 뒤에도 (다른) 캐시 문구가 나간다."""
    s = settings()
    cache = bridge_cache(tmp_path, s)

    async def call_tool(name, args):
        return {"status": "ok", "data": args["query"]}

    llm = FakeLLM(tool_events("search_products", {"query": "간영양제"}),
                  tool_events("search_products", {"query": "간 가드 맥스2"}, cid="c2"),
                  text_events("간 가드 맥스2는 1일 1정이에요."))
    log = TurnLog("S")
    spoken = await Responder(llm, FakeTTS(), call_tool, tools=TOOLS3, settings=s, cache=cache).respond(
        "S", [Message("user", "x")], Sink(), log=log)

    assert spoken == [s.post_tool_ok, s.post_tool_more, "간 가드 맥스2는 1일 1정이에요."]
    assert [e["text"] for e in log.to_dict()["events"] if e["name"] == "bridge"] == [s.post_tool_ok, s.post_tool_more]
    first = tool_bodies(llm.calls[1])
    assert [b.get("_already_said") for b in first] == [s.post_tool_ok]
    both = tool_bodies(llm.calls[2])
    assert [b.get("_already_said") for b in both] == [s.post_tool_ok, s.post_tool_more]
    assert all(b["_note"] for b in both)
    assert both[1]["data"] == "간 가드 맥스2"           # 결과 내용은 그대로


async def test_bridge_once_when_calls_are_parallel_in_one_round(tmp_path):
    s = settings()
    cache = bridge_cache(tmp_path, s)

    async def call_tool(name, args):
        return {"status": "ok"}

    two = [LLMEvent("tool_call", tool_call=ToolCall("c1", "search_products", {"query": "간"})),
           LLMEvent("tool_call", tool_call=ToolCall("c2", "search_products", {"query": "눈"})),
           LLMEvent("done")]
    llm = FakeLLM(two, text_events("두 제품 모두 1일 1정이에요."))
    spoken = await Responder(llm, FakeTTS(), call_tool, tools=TOOLS3, settings=s, cache=cache).respond(
        "S", [Message("user", "x")], Sink())
    assert spoken == [s.post_tool_ok, "두 제품 모두 1일 1정이에요."]
    assert [b.get("_already_said") for b in tool_bodies(llm.calls[1])] == [s.post_tool_ok, None]


async def test_bridge_failure_sentence_unchanged_on_second_round(tmp_path):
    from agent import knowledge

    s = settings()
    fail = knowledge.FAIL_SENTENCES["not_found"]
    cache = bridge_cache(tmp_path, s, fail)
    results = iter([{"status": "ok"}, {"status": "not_found"}])

    async def call_tool(name, args):
        return next(results)

    llm = FakeLLM(tool_events("search_products", {"query": "간"}),
                  tool_events("get_order_info", {"order_id": "1"}, cid="c2"),
                  text_events("주문번호를 다시 알려 주시겠어요?"))
    spoken = await Responder(llm, FakeTTS(), call_tool, tools=TOOLS3, settings=s, cache=cache).respond(
        "S", [Message("user", "x")], Sink())
    assert spoken == [s.post_tool_ok, fail, "주문번호를 다시 알려 주시겠어요?"]
    assert tool_bodies(llm.calls[2])[1]["_already_said"] == fail


async def test_bridge_ok_after_failure_uses_first_ok_phrase(tmp_path):
    from agent import knowledge

    s = settings()
    fail = knowledge.FAIL_SENTENCES["not_found"]
    cache = bridge_cache(tmp_path, s, fail)
    results = iter([{"status": "not_found"}, {"status": "ok"}])

    async def call_tool(name, args):
        return next(results)

    llm = FakeLLM(tool_events("get_order_info", {"order_id": "1"}),
                  tool_events("get_order_info", {"order_id": "2"}, cid="c2"),
                  text_events("배송 중입니다."))
    spoken = await Responder(llm, FakeTTS(), call_tool, tools=TOOLS3, settings=s, cache=cache).respond(
        "S", [Message("user", "x")], Sink())
    assert spoken == [fail, s.post_tool_ok, "배송 중입니다."]


# ── 3 실제로 쓴 번호를 결과에 알려 준다 ──────────────
async def test_called_with_is_added_when_guard_changed_phone_number(tmp_path, monkeypatch):
    """P0133-03: 받아쓴 글은 4002, 검사가 이전 대화의 4402로 맞춰 접수 → 결과에 실제로 쓴 번호를 넣는다."""
    s = settings()
    cache = bridge_cache(tmp_path, s)
    seen = []

    async def call_tool(name, args):
        seen.append(args)
        return {"status": "ok", "message": "고객 정보 접수 성공"}

    guard = fake_guard(monkeypatch, lambda name, args: ({**args, "phone_number": "01048334402"}, None))
    llm = FakeLLM(tool_events("request_callback", {"customer_name": "도원희", "phone_number": "010-4833-4002"}),
                  text_events("도원희님, 010, 4833, 4402번으로 접수해 드렸습니다."))
    r = Responder(llm, FakeTTS(), call_tool, tools=TOOLS3, settings=s, cache=cache, guard=guard)
    await r.respond("S", [Message("user", "도원희. 010-4833-4002. 제품 구매하고 싶어요.")], Sink())

    assert seen == [{"customer_name": "도원희", "phone_number": "01048334402"}]
    body = tool_bodies(llm.calls[1])[0]
    assert body["_called_with"] == {"phone_number": "010-4833-4402"}       # 읽기 쉬운 꼴
    assert body["_already_said"] == s.post_tool_ok                          # 연결 문구 메모와 같이 들어간다
    assert CALLED_WITH_NOTE in body["_note"] and "_already_said" in body["_note"]
    assert body["status"] == "ok" and body["message"] == "고객 정보 접수 성공"
    assert r.tool_log[0]["arguments"]["phone_number"] == "01048334402"
    assert "_called_with" not in r.tool_log[0]["result"]                    # 통화 기록에는 원래 결과만


async def test_called_with_without_bridge_and_for_order_number(monkeypatch):
    async def call_tool(name, args):
        return {"status": "ok"}

    guard = fake_guard(monkeypatch, lambda name, args: ({**args, "order_id": "20260929-0022095"}, None))
    llm = FakeLLM(tool_events("get_order_info", {"order_id": "20260929-0022096"}), text_events("배송 완료입니다."))
    await Responder(llm, FakeTTS(), call_tool, tools=TOOLS3, settings=settings(), guard=guard).respond(
        "S", [Message("user", "x")], Sink())
    body = tool_bodies(llm.calls[1])[0]
    assert body["_called_with"] == {"order_id": "20260929-0022095"}
    assert body["_note"] == CALLED_WITH_NOTE and "_already_said" not in body


async def test_called_with_absent_when_only_format_differs_or_guard_filled_it(monkeypatch):
    async def call_tool(name, args):
        return {"status": "ok"}

    def rule(name, args):
        if name == "request_callback":
            return {**args, "phone_number": "01048334402"}, None            # 하이픈만 뺐다
        return {**args, "buyer_cellphone": "01062096654"}, None             # LLM이 안 준 번호를 검사가 채웠다

    guard = fake_guard(monkeypatch, rule)
    two = [LLMEvent("tool_call", tool_call=ToolCall("c1", "request_callback",
                                                    {"customer_name": "도원희", "phone_number": "010-4833-4402"})),
           LLMEvent("tool_call", tool_call=ToolCall("c2", "get_order_info", {})),
           LLMEvent("done")]
    llm = FakeLLM(two, text_events("접수해 드렸습니다."))
    await Responder(llm, FakeTTS(), call_tool, tools=TOOLS3, settings=settings(), guard=guard).respond(
        "S", [Message("user", "x")], Sink())
    for body in tool_bodies(llm.calls[1]):
        assert "_called_with" not in body and "_note" not in body


# ── AG-T57 들은 번호로 조회했는데 내역이 없으면 그 번호를 코드가 되읽는다 ─────────
# 공식 정의처럼 주문번호·전화번호 중 하나만 있으면 되는 조회 함수
LOOKUP_TOOLS = [{"name": "get_order_info", "description": "주문 조회",
                 "parameters": {"type": "object", "required": [],
                                "properties": {"order_id": {"type": ["string", "null"]},
                                               "buyer_cellphone": {"type": ["string", "null"]}}}}]


def _real_guard(user_text="", history=None, caller=None, spoken=None):
    from agent import guards
    return guards.GuardContext(tools=LOOKUP_TOOLS, user_text=user_text, history=history or [], caller=caller,
                               spoken=spoken or [])


async def _lookup_once(guard, args, result, answer="LLM이 만든 답입니다."):
    seen = []

    async def call_tool(name, a):
        seen.append(a)
        return result

    llm = FakeLLM(tool_events("get_order_info", args), text_events(answer))
    r = Responder(llm, FakeTTS(), call_tool, tools=LOOKUP_TOOLS, settings=settings(), guard=guard)
    spoken = await r.respond("S", [Message("user", guard.user_text or "x")], Sink())
    return r, tool_bodies(llm.calls[1])[0], spoken, seen


async def test_notfound_readback_phone_llm_writes_token_and_server_speaks_the_number():
    """휴대폰 번호는 LLM에 표식만 주고(게이트웨이가 어차피 가린다), 답 속의 [PHONE]을 서버가 실제 번호로 바꿔 읽는다."""
    from agent.responder import NOTFOUND_READBACK_NOTE
    guard = _real_guard(user_text="어, 아마 010. 1234-5678.")
    r, body, spoken, seen = await _lookup_once(guard, {"buyer_cellphone": "010-1234-5678"}, {"status": "not_found"},
                                               answer="[PHONE]번으로 조회했는데 내역이 없네요. 번호가 맞으실까요?")
    assert seen == [{"buyer_cellphone": "01012345678"}]
    assert body["_looked_up_with"] == "[PHONE]" and NOTFOUND_READBACK_NOTE in body["_note"]
    assert "01012345678" not in json.dumps(body) and "1234" not in json.dumps(body)   # 번호는 LLM에 가지 않는다
    assert spoken == ["010, 1234, 5678번으로 조회했는데 내역이 없네요.", "번호가 맞으실까요?"]
    assert "_looked_up_with" not in r.tool_log[0]["result"]             # 통화 기록에는 원래 결과만


async def test_masked_phone_argument_is_restored_and_read_back():
    """AG-T58: 게이트웨이가 번호를 가려 LLM이 "[PHONE]"을 넣어도 받아쓴 글의 번호로 조회하고 그 번호를 되읽는다."""
    guard = _real_guard(user_text="어, 아마 010. 1234-5678.")
    _, body, spoken, seen = await _lookup_once(guard, {"order_id": None, "buyer_cellphone": "[PHONE]"},
                                               {"status": "not_found"}, answer="[PHONE]으로 조회했는데 맞으실까요?")
    assert seen == [{"buyer_cellphone": "01012345678"}]
    assert spoken == ["010, 1234, 5678으로 조회했는데 맞으실까요?"]


async def test_notfound_readback_order_number_is_given_to_the_llm():
    """주문번호는 가려지지 않으므로 값을 주고 LLM이 읽는다. ok인데 목록이 빈 결과도 내역 없음으로 본다."""
    guard = _real_guard(user_text="주문번호 20260929-0033428 이에요")
    _, body, _, _ = await _lookup_once(guard, {"order_id": "202609290033428"}, {"status": "ok", "data": {"orders": []}})
    assert body["_looked_up_with"] == "20260929-0033428"


async def test_no_readback_when_lookup_succeeds():
    guard = _real_guard(user_text="010-1234-5678")
    _, body, _, _ = await _lookup_once(guard, {"buyer_cellphone": "01012345678"},
                                       {"status": "ok", "data": {"orders": [{"제품명": "x"}]}})
    assert "_looked_up_with" not in body


async def test_no_readback_for_caller_or_history_number():
    guard = _real_guard(user_text="배송 언제 와요", caller={"name": "김", "phone": "01012345678", "email": None})
    _, body, _, _ = await _lookup_once(guard, {"buyer_cellphone": "01012345678"}, {"status": "not_found"})
    assert "_looked_up_with" not in body
    hist = [{"role": "customer", "text": "전화번호는 010-1234-5678입니다"}]
    _, body, _, _ = await _lookup_once(_real_guard(user_text="네 그 번호요", history=hist),
                                       {"buyer_cellphone": "01012345678"}, {"status": "not_found"})
    assert "_looked_up_with" not in body


async def test_readback_for_number_spoken_in_an_earlier_turn_of_this_call():
    """이번 통화의 앞선 턴에서 받아 적은 번호도 잘못 들었을 수 있다(history에 있어도 되읽는다)."""
    hist = [{"role": "customer", "text": "전화번호는 010-1234-5678입니다"}]
    guard = _real_guard(user_text="네 맞아요 조회해 주세요", history=hist, spoken=["전화번호는 010-1234-5678입니다"])
    _, body, spoken, _ = await _lookup_once(guard, {"buyer_cellphone": "01012345678"}, {"status": "not_found"},
                                            answer="[PHONE]번이 맞으실까요?")
    assert body["_looked_up_with"] == "[PHONE]" and spoken == ["010, 1234, 5678번이 맞으실까요?"]


async def test_notfound_readback_switch_off(monkeypatch):
    monkeypatch.setenv("DIRECTON_NOTFOUND_READBACK", "0")
    guard = _real_guard(user_text="010-1234-5678")
    _, body, _, _ = await _lookup_once(guard, {"buyer_cellphone": "01012345678"}, {"status": "not_found"})
    assert "_looked_up_with" not in body


def test_speech_swaps_the_mask_token_for_the_number_or_a_phrase():
    from agent.responder import speech_safe
    assert speech_safe("[PHONE]번으로 접수해 드렸습니다.", (), "010, 1234, 5678") == "010, 1234, 5678번으로 접수해 드렸습니다."
    assert speech_safe("[PHONE]번으로 접수해 드렸습니다.") == "말씀하신 번호으로 접수해 드렸습니다."
    assert "[PHONE]" not in speech_safe("연락처는 [PHONE] 맞으실까요?")


def test_masked_phone_with_prefix_is_not_read_twice():
    # 데모(DEMO-20261121-180355): LLM이 "010, [PHONE]"으로 써 "010, 010, 1234, 5678"로 읽혔다
    from agent.responder import speech_safe
    m = "010, 1234, 5678"
    assert speech_safe("접수되었습니다. 010, [PHONE]번으로 전화드리겠습니다.", (), m) == \
        "접수되었습니다. 010, 1234, 5678번으로 전화드리겠습니다."
    assert speech_safe("[PHONE]번으로 접수했어요.", (), m) == "010, 1234, 5678번으로 접수했어요."
