"""AG-T10c 세션: finalize 후 선호출 재시작, 정규화 비교 재사용, 선합성 TTS 이어 쓰기, 텍스트 기반 첫 마디,
빈 텍스트 짧은 발화, 턴 로그(재사용·공백).

시간은 프레임 수 기준(20ms/프레임). FakeWS.idx = 지금까지 서버에 전달한 PCM 프레임 수.
"""
import asyncio
import json

import httpx

from agent.config import Settings
from agent.core.types import LLMEvent, ToolCall, TurnDecision, TurnSignals
from agent.server import handle
from agent.session import SessionDeps, match_key
from agent.tools_client import ToolsClient
from agent.tts_cache import TTSCache, select_opener_for_text
from agent.turn_log import TurnLogWriter

SPEECH = b"\x10\x27" * 320
SILENCE = b"\x00\x00" * 320
OPENER = "네, 고객님."
OPENER_PCM = b"\x11\x22" * 1600          # 100ms
APOLOGY_PCM = b"\x33\x44" * 1600
FILLER_PCM = b"\x55\x66" * 1600
REPROMPT_PCM = b"\x77\x66" * 1600
LOOKUP_PCM = b"\x12\x34" * 3200          # 200ms
SORRY_PCM = b"\x21\x43" * 3200
TOOLS = [{"type": "function", "function": {
    "name": "get_order_info", "description": "주문 조회",
    "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}}}}}]


def pcm_for(text: str) -> bytes:
    return b"\x10\x27" * 4 + text.encode("utf-8")


# ── fakes ────────────────────────────────────────────
class FakeWS:
    def __init__(self, start: dict, frames, after_end_frames=5, fast_frames=4000):
        self._start = json.dumps(start, ensure_ascii=False)
        self.frames = list(frames)
        self.pos = 0
        self.started = False
        self.idx = 0
        self.sent: list[tuple[int, object]] = []
        self.turn_ends = 0
        self.after_end_frames = after_end_frames
        self.frames_after_end = 0
        self.fast_frames = fast_frames

    async def recv(self):
        return await self.__anext__()

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(0)
        if not self.started:
            self.started = True
            return self._start
        if self.turn_ends >= 1:
            if self.frames_after_end >= self.after_end_frames:
                raise StopAsyncIteration
            self.frames_after_end += 1
        if self.idx > self.fast_frames:
            await asyncio.sleep(0.001)
        if self.idx > 100000:
            raise StopAsyncIteration
        frame = SILENCE
        if self.pos < len(self.frames):
            frame = self.frames[self.pos]
            self.pos += 1
        self.idx += 1
        return frame

    async def send(self, msg):
        self.sent.append((self.idx, msg))
        if isinstance(msg, str) and json.loads(msg).get("type") == "turn.end":
            self.turn_ends += 1

    def binaries(self):
        return [(i, m) for i, m in self.sent if isinstance(m, bytes)]

    def texts(self):
        return [json.loads(m) for _, m in self.sent if isinstance(m, str)]


class FakeVAD:
    def process(self, frame):
        return 1.0 if any(frame) else 0.0

    def reset(self):
        pass


class FakeSmartTurn:
    def predict(self, pcm):
        return 0.9


class FakeDetector:
    def __init__(self, ws, end_ms=600):
        self.ws = ws
        self.end_ms = end_ms
        self.signals: list[TurnSignals] = []
        self.decided_at: list[int] = []

    def decide(self, s):
        self.signals.append(s)
        if s.silence_ms >= self.end_ms:
            self.decided_at.append(self.ws.idx)
            return TurnDecision(True, "silence")
        return TurnDecision(False, "wait")


class FakeStream:
    """partial은 오디오 6프레임 뒤부터 보인다. finalize는 ws.idx가 finalize_at에 이를 때(없으면 바로) final을 준다."""

    def __init__(self, ws, partial, final, finalize_at=None):
        self.ws = ws
        self.partial, self.final = partial, final
        self.finalize_at = finalize_at
        self.audio = 0
        self.finalize_calls = 0

    async def send_audio(self, pcm):
        self.audio += 1

    def latest_text(self):
        return self.partial if self.audio > 5 else ""

    async def finalize(self, timeout=1.0):
        self.finalize_calls += 1
        await asyncio.sleep(0)
        if self.finalize_at is not None:
            while self.ws.idx < self.finalize_at:
                await asyncio.sleep(0.0005)
        return self.final

    async def close(self):
        pass


class FakeSTT:
    def __init__(self, ws, partial, final, finalize_at=None):
        self.ws = ws
        self.args = (partial, final, finalize_at)
        self.streams: list[FakeStream] = []

    async def open_stream(self, context=""):
        s = FakeStream(self.ws, *self.args)
        self.streams.append(s)
        return s


class FakeLLM:
    def __init__(self, *scripts, ws=None):
        self.scripts = list(scripts)
        self.calls = []
        self.cancelled = []
        self.ws = ws

    async def stream(self, system, messages, tools):
        n = len(self.calls)
        self.calls.append({"system": system, "messages": list(messages), "tools": tools,
                           "idx": self.ws.idx if self.ws else None})
        script = self.scripts[min(n, len(self.scripts) - 1)]
        try:
            if script == "hang":
                await asyncio.Event().wait()
            for ev in script:
                await asyncio.sleep(0)
                yield ev
        except (asyncio.CancelledError, GeneratorExit):
            self.cancelled.append(n)
            raise


def text_events(*chunks):
    return [LLMEvent("text", text=c) for c in chunks] + [LLMEvent("done")]


class FakeTTS:
    def __init__(self, ws=None):
        self.ws = ws
        self.requests: list[tuple[str, int | None]] = []

    async def synthesize(self, text):
        self.requests.append((text, self.ws.idx if self.ws else None))
        await asyncio.sleep(0)
        yield pcm_for(text)


def start_msg(**kw):
    m = {"type": "session.start", "session_id": "S-1", "case_id": "C1", "now": "2026-11-21 11:00",
         "history": [], "caller": None, "tools_url": "http://mock:8100",
         "audio": {"encoding": "pcm_s16le", "sample_rate": 16000, "channels": 1}}
    m.update(kw)
    return m


def utterance(speech=50, silence=0):
    return [SPEECH] * speech + [SILENCE] * silence


def make_deps(tmp_path, *, ws, llm, stt, tts=None, detector=None, settings_kw=None, extra_cache=None,
              log_writer=None):
    s = Settings(**{"filler_delay_ms": 700, "safety_timeout_s": 5.0, "llm_timeout_s": 5.0,
                    "tts_timeout_s": 5.0, **(settings_kw or {})})
    cache = TTSCache(tmp_path / "cache")
    cache.put(OPENER, OPENER_PCM)
    cache.put(s.apology, APOLOGY_PCM)
    cache.put(s.reprompt, REPROMPT_PCM)
    for f in s.fillers:
        cache.put(f, FILLER_PCM)
    for text, pcm in (extra_cache or {}).items():
        cache.put(text, pcm)

    def handler(req):
        if req.method == "GET":
            return httpx.Response(200, json=TOOLS)
        return httpx.Response(200, json={"status": "ok"})

    tools = ToolsClient(http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    det = detector or FakeDetector(ws)
    return SessionDeps(stt=stt, tts=tts or FakeTTS(ws), llm=llm, vad_factory=FakeVAD,
                       smart_turn=FakeSmartTurn(), detector_factory=lambda: det, tools=tools,
                       cache=cache, settings=s, log_writer=log_writer), det


async def run(ws, deps):
    await asyncio.wait_for(handle(ws, deps), 10.0)


def read_log(tmp_path):
    lines = (tmp_path / "logs" / "turns-20261121.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(x) for x in lines]


def writer(tmp_path):
    return TurnLogWriter(tmp_path / "logs", date_fn=lambda: "20261121")


# ── 정규화 비교 ───────────────────────────────────────
def test_match_key_ignores_spaces_punct_and_digit_forms():
    assert match_key("배송 언제 와요?") == match_key("배송언제 와요")
    assert match_key("주문번호 이육일일 일오공 이삼일이요.") == match_key("주문번호 2611150231이요")
    assert match_key("XL 사이즈요") == match_key("xl 사이즈요.")
    assert match_key("배송 언제") != match_key("배송 언제 와요?")
    assert match_key("") == ""


# ── finalize 후 선호출 재시작 ─────────────────────────
async def test_precall_restarted_with_finalize_text_before_commit(tmp_path):
    ws = FakeWS(start_msg(), utterance(50))
    llm = FakeLLM("hang", text_events("배송은 내일 도착합니다."), ws=ws)
    stt = FakeSTT(ws, partial="배송 언제", final="배송 언제 와요?", finalize_at=65)
    deps, det = make_deps(tmp_path, ws=ws, llm=llm, stt=stt, log_writer=writer(tmp_path))
    await run(ws, deps)
    commit = det.decided_at[0]
    assert commit == 80
    assert len(llm.calls) == 2                               # 부분 결과 선호출 + finalize 선호출, 확정 뒤 추가 호출 없음
    assert llm.calls[0]["messages"][-1].content == "배송 언제"
    assert llm.calls[1]["messages"][-1].content == "배송 언제 와요?"
    assert 65 <= llm.calls[1]["idx"] < commit                 # finalize 직후, 확정 전
    assert 0 in llm.cancelled                                 # 이전 선호출은 취소
    assert ws.texts()[-2]["text"] == "네, 고객님. 배송은 내일 도착합니다."
    rec = read_log(tmp_path)[0]
    assert rec["precall_reused"] is True
    assert rec["precall_count"] == 2


async def test_precall_started_after_finalize_when_partial_was_empty(tmp_path):
    ws = FakeWS(start_msg(), utterance(50))
    llm = FakeLLM(text_events("네 알겠습니다."), ws=ws)
    stt = FakeSTT(ws, partial="", final="네 맞아요.", finalize_at=66)
    deps, det = make_deps(tmp_path, ws=ws, llm=llm, stt=stt)
    await run(ws, deps)
    assert len(llm.calls) == 1
    assert 66 <= llm.calls[0]["idx"] < det.decided_at[0]
    assert llm.calls[0]["messages"][-1].content == "네 맞아요."


async def test_normalized_match_reuses_precall_without_new_call(tmp_path):
    ws = FakeWS(start_msg(), utterance(50))
    llm = FakeLLM(text_events("확인해 보니 배송 중입니다."), text_events("다시 부르면 안 됨."), ws=ws)
    stt = FakeSTT(ws, partial="주문번호 이육일일 일오공 이삼일", final="주문번호 2611150231.", finalize_at=95)
    deps, det = make_deps(tmp_path, ws=ws, llm=llm, stt=stt, log_writer=writer(tmp_path))
    await run(ws, deps)
    assert det.decided_at[0] == 80 and len(llm.calls) == 1   # finalize는 확정 뒤에 끝났지만 정규화 일치 → 재사용
    assert ws.texts()[-2]["text"].endswith("확인해 보니 배송 중입니다.")
    rec = read_log(tmp_path)[0]
    assert rec["precall_reused"] is True
    assert rec["user_text"] == "주문번호 2611150231."


async def test_final_text_differs_after_commit_recalls(tmp_path):
    ws = FakeWS(start_msg(), utterance(50))
    llm = FakeLLM(text_events("앞부분 답."), text_events("배송은 내일 도착합니다."), ws=ws)
    stt = FakeSTT(ws, partial="배송 언제", final="배송 언제 와요?", finalize_at=95)
    deps, det = make_deps(tmp_path, ws=ws, llm=llm, stt=stt, log_writer=writer(tmp_path))
    await run(ws, deps)
    assert len(llm.calls) == 2
    assert llm.calls[1]["idx"] >= det.decided_at[0]
    assert ws.texts()[-2]["text"] == "네, 고객님. 배송은 내일 도착합니다."
    assert read_log(tmp_path)[0]["precall_reused"] is False


async def test_speech_resume_cancels_finalize_precall(tmp_path):
    frames = utterance(30, 20) + utterance(20)               # 400ms 쉬고 다시 말함
    ws = FakeWS(start_msg(), frames)
    llm = FakeLLM("hang", "hang", text_events("네 확인해 드리겠습니다."), ws=ws)
    stt = FakeSTT(ws, partial="배송", final="배송 조회", finalize_at=None)
    deps, det = make_deps(tmp_path, ws=ws, llm=llm, stt=stt)
    await run(ws, deps)
    assert {0, 1} <= set(llm.cancelled)                       # 부분·finalize 선호출 모두 취소
    assert all(i >= det.decided_at[0] for i, _ in ws.binaries())
    assert ws.texts()[-1] == {"type": "turn.end"}


# ── 확정 전 오디오 0 + 선합성 TTS 이어 쓰기 ───────────
async def test_presynth_tts_reused_and_no_audio_before_commit(tmp_path):
    ws = FakeWS(start_msg(), utterance(50))
    first = "내일 도착 예정입니다."
    llm = FakeLLM(text_events(first + " ", "감사합니다 고객님."), ws=ws)
    tts = FakeTTS(ws)
    stt = FakeSTT(ws, partial="배송 언제 와요?", final="배송 언제 와요?")
    deps, det = make_deps(tmp_path, ws=ws, llm=llm, stt=stt, tts=tts, log_writer=writer(tmp_path))
    await run(ws, deps)
    commit = det.decided_at[0]
    bins = ws.binaries()
    assert all(i >= commit for i, _ in bins)                  # 불변 규칙: 확정 전 송신 0
    assert bins[0] == (commit, OPENER_PCM)
    first_reqs = [i for t, i in tts.requests if t == first]
    assert len(first_reqs) == 1 and first_reqs[0] < commit    # 첫 문장은 확정 전에 미리 합성(송신은 안 함)
    audio = b"".join(m for _, m in bins)
    assert audio == OPENER_PCM + pcm_for(first) + pcm_for("감사합니다 고객님.")
    rec = read_log(tmp_path)[0]
    assert rec["precall_reused"] is True and rec["presynth_reused"] is True


async def test_presynth_discarded_when_precall_discarded(tmp_path):
    ws = FakeWS(start_msg(), utterance(50))
    llm = FakeLLM(text_events("틀린 답입니다."), text_events("맞는 답입니다."), ws=ws)
    tts = FakeTTS(ws)
    stt = FakeSTT(ws, partial="배송 언제", final="배송 언제 와요?", finalize_at=95)
    deps, _ = make_deps(tmp_path, ws=ws, llm=llm, stt=stt, tts=tts)
    await run(ws, deps)
    audio = b"".join(m for _, m in ws.binaries())
    assert pcm_for("틀린 답입니다.") not in audio
    assert audio == OPENER_PCM + pcm_for("맞는 답입니다.")


# ── 턴 로그: 공백 ──────────────────────────────────────
async def test_turn_log_records_gap_after_opener(tmp_path):
    ws = FakeWS(start_msg(), utterance(50))
    llm = FakeLLM(text_events("배송 중입니다."), ws=ws)
    stt = FakeSTT(ws, partial="배송 언제 와요?", final="배송 언제 와요?")
    deps, _ = make_deps(tmp_path, ws=ws, llm=llm, stt=stt, log_writer=writer(tmp_path))
    await run(ws, deps)
    rec = read_log(tmp_path)[0]
    for k in ("opener_start", "opener_sent", "response_first_audio"):
        assert k in rec["marks"], k
    assert rec["opener"] == OPENER
    assert rec["opener_ms"] == 100
    assert isinstance(rec["gap_ms"], (int, float))
    expected = rec["marks"]["response_first_audio"] - (rec["marks"]["opener_start"] + 100)
    assert abs(rec["gap_ms"] - round(expected, 1)) < 0.2


# ── 텍스트 기반 첫 마디 ───────────────────────────────
def test_select_opener_for_text_rules():
    s = Settings()
    assert select_opener_for_text("물건이 깨져서 왔어요. 너무 화나네요", s) == s.opener_apology
    assert select_opener_for_text("배송이 아직도 안 왔어요", s) == s.opener_apology
    assert select_opener_for_text("주문 조회 좀 해 주세요", s) == s.opener_lookup
    assert select_opener_for_text("주문번호 2611150231 배송 언제 와요?", s) == s.opener_lookup
    assert select_opener_for_text("주문 취소하고 싶어요", s) == s.opener_lookup
    assert select_opener_for_text("영업시간이 어떻게 돼요?", s) == s.openers[0]
    assert select_opener_for_text("", s) == s.openers[0]
    assert select_opener_for_text("주문 조회해 주세요", Settings(opener_by_text=False)) == s.openers[0]


def test_text_openers_are_warmed():
    s = Settings()
    phrases = s.cached_phrases()
    for p in (s.opener_apology, s.opener_lookup, s.opener_lookup_filler):
        assert p in phrases


async def test_commit_uses_text_based_opener_and_rebuilds_system(tmp_path):
    s = Settings()
    ws = FakeWS(start_msg(), utterance(50))
    llm = FakeLLM(text_events("주문 내역을 보니 배송 중입니다."), ws=ws)
    stt = FakeSTT(ws, partial="주문 조회 좀 해 주세요 공일공 일이삼사 오육칠팔", final="주문 조회 좀 해 주세요 공일공 일이삼사 오육칠팔")
    deps, det = make_deps(tmp_path, ws=ws, llm=llm, stt=stt, log_writer=writer(tmp_path),
                          extra_cache={s.opener_lookup: LOOKUP_PCM, s.opener_apology: SORRY_PCM})
    await run(ws, deps)
    bins = ws.binaries()
    assert bins[0][0] == det.decided_at[0]
    assert b"".join(m for _, m in bins).startswith(LOOKUP_PCM)
    assert f"'{s.opener_lookup}'라고 말했다" in llm.calls[-1]["system"]
    assert f"'{OPENER}'라고 말했다" not in llm.calls[-1]["system"]
    assert ws.texts()[-2]["text"].startswith(s.opener_lookup)
    rec = read_log(tmp_path)[0]
    assert rec["opener"] == s.opener_lookup and rec["opener_ms"] == 200


async def test_lookup_opener_turn_uses_alternate_filler(tmp_path):
    s = Settings()
    alt_pcm = b"\x01\x7f" * 1600
    ws = FakeWS(start_msg(), utterance(50))

    class SlowSecondLLM:                                       # 2회차(툴 결과 뒤)는 늦게 와서 필러가 나간다
        def __init__(self):
            self.calls = []

        async def stream(self, system, messages, tools):
            n = len(self.calls)
            self.calls.append({"system": system, "messages": list(messages), "tools": tools})
            if n == 0:
                yield LLMEvent("tool_call", tool_call=ToolCall("c1", "get_order_info", {"order_id": "20260101-0000001"}))
                yield LLMEvent("done")
                return
            await asyncio.sleep(0.05)
            yield LLMEvent("text", text="배송 중입니다.")
            yield LLMEvent("done")

    llm = SlowSecondLLM()
    stt = FakeSTT(ws, partial="주문 조회해 주세요 공일공 일이삼사 오육칠팔", final="주문 조회해 주세요 공일공 일이삼사 오육칠팔")
    deps, _ = make_deps(tmp_path, ws=ws, llm=llm, stt=stt, settings_kw={"filler_delay_ms": 10},
                        extra_cache={s.opener_lookup: LOOKUP_PCM, s.opener_lookup_filler: alt_pcm})
    await run(ws, deps)
    audio = [m for _, m in ws.binaries()]
    assert alt_pcm in audio and FILLER_PCM not in audio      # "확인해 드릴게요" 반복 대신 다른 필러
    assert s.opener_lookup_filler in ws.texts()[-2]["text"]


async def test_text_opener_not_cached_falls_back_to_default(tmp_path):
    ws = FakeWS(start_msg(), utterance(50))
    llm = FakeLLM(text_events("배송 중입니다."), ws=ws)
    stt = FakeSTT(ws, partial="주문 조회 좀 해 주세요", final="주문 조회 좀 해 주세요")
    deps, _ = make_deps(tmp_path, ws=ws, llm=llm, stt=stt)
    await run(ws, deps)
    assert ws.binaries()[0][1] == OPENER_PCM
    assert ws.texts()[-2]["text"].startswith(OPENER)


async def test_precall_system_matches_opener_used_at_commit(tmp_path):
    s = Settings()
    ws = FakeWS(start_msg(), utterance(50))
    llm = FakeLLM(text_events("불편을 드려 정말 죄송합니다. 바로 교환해 드릴게요."), ws=ws)
    stt = FakeSTT(ws, partial="물건이 깨져서 왔어요", final="물건이 깨져서 왔어요.")
    deps, _ = make_deps(tmp_path, ws=ws, llm=llm, stt=stt, log_writer=writer(tmp_path),
                        extra_cache={s.opener_lookup: LOOKUP_PCM, s.opener_apology: SORRY_PCM})
    await run(ws, deps)
    assert len(llm.calls) == 1                                # 선호출 그대로 재사용
    assert f"'{s.opener_apology}'라고 말했다" in llm.calls[0]["system"]
    assert b"".join(m for _, m in ws.binaries()).startswith(SORRY_PCM)
    assert read_log(tmp_path)[0]["precall_reused"] is True


# ── 빈 텍스트 짧은 발화 ───────────────────────────────
async def test_short_utterance_with_empty_text_goes_through_detector(tmp_path):
    ws = FakeWS(start_msg(), utterance(40))                   # 800ms 짧은 발화
    llm = FakeLLM(text_events("네 알겠습니다."), ws=ws)
    stt = FakeSTT(ws, partial="", final="", finalize_at=None)
    deps, det = make_deps(tmp_path, ws=ws, llm=llm, stt=stt)
    await run(ws, deps)
    assert det.decided_at                                     # 결정기 규칙대로 확정
    assert any(s.text == "" and 0 < s.speech_ms < 1300 and s.silence_ms > 0 for s in det.signals)
    assert llm.calls == []
    assert [m for _, m in ws.binaries()] == [OPENER_PCM, REPROMPT_PCM]
    assert ws.texts()[-1] == {"type": "turn.end"}


async def test_detector_gets_finalize_text_once_done(tmp_path):
    ws = FakeWS(start_msg(), utterance(40))
    llm = FakeLLM(text_events("네."), ws=ws)
    stt = FakeSTT(ws, partial="", final="네 맞아요.", finalize_at=None)
    deps, det = make_deps(tmp_path, ws=ws, llm=llm, stt=stt)
    await run(ws, deps)
    texts = [s.text for s in det.signals if s.silence_ms > 0]
    assert texts[0] == "" and texts[-1] == "네 맞아요."      # finalize가 끝나면 그 텍스트로 판단


def test_real_detector_handles_empty_text_short_speech():
    from agent.turn.detector import make_detector

    d = make_detector()
    sig = lambda ms: TurnSignals(silence_ms=ms, speech_ms=800, text="", smart_turn_p=0.9)  # noqa: E731
    assert not d.decide(sig(100)).end
    assert not d.decide(sig(300)).reason.startswith("noise")  # 잡음이 아니라 정상 경로
    assert any(d.decide(sig(ms)).end for ms in range(200, 2001, 20))
