"""AG-T19 바지인(중간 개입): 상담원이 말하는 중 고객 음성 300ms 이상 → 남은 TTS 중단 → 실제로 말한 만큼
transcript + turn.end → 이어 들은 말로 재응답(끊겼다는 맥락 포함). 측정기 경로(끼어들지 않음)는 그대로.

시간은 프레임 수 기준(20ms/프레임). 세션의 재생 시계도 받은 프레임 수를 따른다(테스트는 실시간보다 빠르게 돈다).
"""
import asyncio
import json

from agent.core.types import LLMEvent
from agent.session import INTERRUPTED_NOTE, heard_text
from test_session import (OPENER, OPENER_PCM, SILENCE, SPEECH, FakeDetector, FakeSTT, make_deps, run,
                          start_msg, utterance)

S1 = "첫 문장입니다."
S2 = "둘째 문장은 조금 깁니다."
MS = 32                                    # 16kHz s16le: 1ms = 32바이트


def voiced(ms: int, tag: int = 1) -> bytes:
    return bytes([0x10, 0x20 + tag]) * (16 * ms)


class LongTTS:
    """문장마다 정해진 길이의 유성 PCM(기본 2000ms)을 100ms 덩어리로 준다."""

    def __init__(self, lengths=None, default_ms=2000):
        self.lengths = lengths or {}
        self.default_ms = default_ms
        self.requests: list[str] = []

    async def synthesize(self, text):
        self.requests.append(text)
        b = voiced(self.lengths.get(text, self.default_ms))
        for i in range(0, len(b), 3200):
            await asyncio.sleep(0)
            yield b[i:i + 3200]


class ScriptLLM:
    """scripts[i] = i번째 호출의 이벤트 목록. "HANG"을 만나면 거기서 멈춘다(취소 기록)."""

    def __init__(self, *scripts):
        self.scripts = list(scripts)
        self.calls = []
        self.cancelled = []

    async def stream(self, system, messages, tools):
        n = len(self.calls)
        self.calls.append({"system": system, "messages": list(messages)})
        script = self.scripts[min(n, len(self.scripts) - 1)]
        try:
            for ev in script:
                await asyncio.sleep(0)
                if ev == "HANG":
                    await asyncio.Event().wait()
                yield ev
        except (asyncio.CancelledError, GeneratorExit):
            self.cancelled.append(n)
            raise


def text_events(*chunks, hang=False):
    evs = [LLMEvent("text", text=c) for c in chunks]
    return evs + (["HANG"] if hang else [LLMEvent("done")])


class BargeWS:
    """측정기 흉내 + 끼어들기. 첫 발화 뒤 무음을 보내다가, anchor(첫 오디오 수신 또는 첫 turn.end) 뒤
    after 프레임째에 interrupt 프레임들을 끼워 넣는다."""

    def __init__(self, start, first, *, interrupt=(), after=60, anchor="audio", turns=2,
                 after_end_frames=5, max_frames=20000):
        self._start = json.dumps(start, ensure_ascii=False)
        self.first = list(first)
        self.interrupt = list(interrupt)
        self.after = after
        self.anchor = anchor
        self.turns = turns
        self.after_end_frames = after_end_frames
        self.max_frames = max_frames
        self.started = False
        self.idx = 0
        self.sent: list[tuple[int, object]] = []
        self.turn_ends = 0
        self.end_idx: list[int] = []
        self.anchor_idx: int | None = None
        self.inject_pos = 0
        self.inject_start: int | None = None
        self.frames_after_end = 0

    async def recv(self):
        return await self.__anext__()

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(0)
        if not self.started:
            self.started = True
            return self._start
        if self.turn_ends >= self.turns:
            if self.frames_after_end >= self.after_end_frames:
                raise StopAsyncIteration
            self.frames_after_end += 1
        if self.idx > self.max_frames:
            raise StopAsyncIteration
        frame = SILENCE
        if self.idx < len(self.first):
            frame = self.first[self.idx]
        elif (self.anchor_idx is not None and self.idx >= self.anchor_idx + self.after
              and self.inject_pos < len(self.interrupt)):
            if self.inject_start is None:
                self.inject_start = self.idx
            frame = self.interrupt[self.inject_pos]
            self.inject_pos += 1
        self.idx += 1
        return frame

    async def send(self, msg):
        self.sent.append((self.idx, msg))
        if isinstance(msg, bytes):
            if self.anchor == "audio" and self.anchor_idx is None:
                self.anchor_idx = self.idx
            return
        if json.loads(msg).get("type") == "turn.end":
            self.turn_ends += 1
            self.end_idx.append(self.idx)
            if self.anchor == "turn_end" and self.anchor_idx is None:
                self.anchor_idx = self.idx

    def binaries(self):
        return [(i, m) for i, m in self.sent if isinstance(m, bytes)]

    def texts(self):
        return [json.loads(m) for _, m in self.sent if isinstance(m, str)]

    def proto(self):
        return [t for t in self.texts() if not t["type"].startswith("debug.")]


def _deps(tmp_path, ws, llm, stt, tts=None, detector=None, verify=False):
    # 기존 테스트는 소리만으로 바로 끊는 경로(barge_in_verify=False)를 본다. 확인 경로는 아래 AG-U05 테스트.
    deps, det = make_deps(tmp_path, ws=ws, llm=llm, stt=stt, tts=tts or LongTTS(),
                          settings_kw={"barge_in_verify": verify},
                          detector=detector or FakeDetector(ws))
    return deps, det


def _audio_ms(ws, upto_idx=None):
    return sum(len(m) for i, m in ws.binaries() if upto_idx is None or i <= upto_idx) / MS


# ── 순수 함수: 실제로 말한 만큼 ──────────────────────────
def test_heard_text_cuts_at_stop_point_on_word_boundary():
    spoken = ["네, 고객님.", "첫 문장입니다.", "둘째 문장은 조금 깁니다."]
    segs = {0: (0, 100), 1: (100, 2100), 2: (2100, 4100)}
    assert heard_text(spoken, segs, 5000) == "네, 고객님. 첫 문장입니다. 둘째 문장은 조금 깁니다."
    assert heard_text(spoken, segs, 2100) == "네, 고객님. 첫 문장입니다."
    cut = heard_text(spoken, segs, 3100)                       # 셋째 문장 절반
    assert cut.startswith("네, 고객님. 첫 문장입니다. 둘째")
    assert "깁니다" not in cut and not cut.endswith(" ")
    assert heard_text(spoken, segs, 20) == ""                   # 첫 마디 도중(어절 하나 못 채움)
    assert heard_text(spoken, segs, 50) == "네"
    assert heard_text(spoken, {0: (0, 100)}, 5000) == "네, 고객님."   # 오디오 없는 문장은 안 들림


# ── 끼어들기 → 중단 → 재응답 ─────────────────────────────
async def test_barge_in_stops_tts_ends_turn_and_answers_new_utterance(tmp_path):
    ws = BargeWS(start_msg(), utterance(50), interrupt=[SPEECH] * 25, after=60)
    llm = ScriptLLM(text_events(S1 + " ", S2, hang=True), text_events("네, 주문을 취소해 드릴게요."))
    stt = FakeSTT(texts=[("배송 언제 와요?", "배송 언제 와요?"), ("아니 그냥 취소해 주세요", "아니 그냥 취소해 주세요")])
    tts = LongTTS()
    deps, det = _deps(tmp_path, ws, llm, stt, tts)
    await run(ws, deps)

    proto = ws.proto()
    assert [t["type"] for t in proto] == ["agent.transcript", "turn.end", "agent.transcript", "turn.end"]
    first_end = ws.end_idx[0]
    onset = ws.inject_start
    # 300ms(15프레임) 음성 뒤 곧바로 중단(처리 여유 몇 프레임)
    assert onset + 15 <= first_end <= onset + 20
    # 남은 TTS는 보내지 않았다: 둘째 문장 전체(2000ms)를 다 보내지 않음
    assert _audio_ms(ws, first_end) < 100 + 2000 + 2000
    # 중단 뒤 다음 확정 전까지 오디오 없음(고객 발화 중 유성음 금지)
    second_commit = det.decided_at[-1]
    assert all(not (first_end < i < second_commit) for i, _ in ws.binaries())
    # 첫 턴 transcript = 실제로 보낸 만큼(첫 마디 + 첫 문장 + 둘째 문장 일부)
    t1 = proto[0]["text"]
    assert t1.startswith(f"{OPENER} {S1}")
    assert S2 not in t1
    # LLM·선합성 작업 취소
    assert 0 in llm.cancelled
    # 재응답: 끼어든 말이 사용자 발화, 직전 상담원 말은 끊겼다는 표시와 함께
    last = llm.calls[-1]["messages"]
    assert last[-1].role == "user" and last[-1].content == "아니 그냥 취소해 주세요"
    assert last[-2].role == "assistant" and INTERRUPTED_NOTE in last[-2].content
    assert last[-2].content.startswith(t1)
    assert last[-3].content == "배송 언제 와요?"
    assert proto[2]["text"].endswith("네, 주문을 취소해 드릴게요.")
    # 끼어든 말의 시작(프리롤 포함)이 새 STT 스트림으로 들어갔다
    assert deps.stt.streams[1].audio >= 25


async def test_short_backchannel_does_not_interrupt(tmp_path):
    ws = BargeWS(start_msg(), utterance(50), interrupt=[SPEECH] * 10, after=60, turns=1)   # "네" 200ms
    llm = ScriptLLM(text_events(S1 + " ", S2))
    stt = FakeSTT(texts=[("배송 언제 와요?", "배송 언제 와요?")])
    deps, _ = _deps(tmp_path, ws, llm, stt)
    await run(ws, deps)
    proto = ws.proto()
    assert [t["type"] for t in proto] == ["agent.transcript", "turn.end"]
    assert proto[0]["text"] == f"{OPENER} {S1} {S2}"
    assert _audio_ms(ws) == 100 + 2000 + 2000
    assert len(llm.calls) == 1


async def test_speech_right_after_commit_is_not_barge_in(tmp_path):
    """확정 직후(첫 소리 1초 이내) 시작된 말은 끼어들기로 보지 않는다(잘못 끊은 발화의 연속 — 기존 동작 유지)."""
    ws = BargeWS(start_msg(), utterance(50), interrupt=[SPEECH] * 40, after=5, turns=1)
    llm = ScriptLLM(text_events(S1 + " ", S2))
    stt = FakeSTT(texts=[("배송 언제 와요?", "배송 언제 와요?")])
    deps, _ = _deps(tmp_path, ws, llm, stt)
    await run(ws, deps)
    proto = ws.proto()
    assert [t["type"] for t in proto] == ["agent.transcript", "turn.end"]
    assert proto[0]["text"] == f"{OPENER} {S1} {S2}"


async def test_evaluator_path_full_answer_and_paced_send(tmp_path):
    """끼어들지 않으면 전부 말하고 turn.end. 송신은 재생보다 1.5초 넘게 앞서지 않는다(중단할 수 있게)."""
    ws = BargeWS(start_msg(), utterance(50), turns=1)
    llm = ScriptLLM(text_events(S1 + " ", S2))
    stt = FakeSTT(texts=[("배송 언제 와요?", "배송 언제 와요?")])
    deps, det = _deps(tmp_path, ws, llm, stt)
    await run(ws, deps)
    commit = det.decided_at[0]
    proto = ws.proto()
    assert [t["type"] for t in proto] == ["agent.transcript", "turn.end"]
    assert proto[0]["text"] == f"{OPENER} {S1} {S2}"
    bins = ws.binaries()
    assert bins[0] == (commit, OPENER_PCM)
    total = 0.0
    for i, m in bins:
        total += len(m) / MS
        ahead = total - (i - commit) * 20
        assert ahead <= 1500 + 100 + 20, (i, total)
    assert total == 100 + 4000


async def test_barge_in_disabled_by_session_start(tmp_path):
    ws = BargeWS(start_msg(barge_in=False), utterance(50), interrupt=[SPEECH] * 25, after=60, turns=1)
    llm = ScriptLLM(text_events(S1 + " ", S2))
    stt = FakeSTT(texts=[("배송 언제 와요?", "배송 언제 와요?")])
    deps, _ = _deps(tmp_path, ws, llm, stt)
    await run(ws, deps)
    proto = ws.proto()
    assert proto[0]["text"] == f"{OPENER} {S1} {S2}"


async def test_debug_session_signals_client_and_cuts_at_trigger(tmp_path):
    ws = BargeWS(start_msg(debug=True), utterance(50), interrupt=[SPEECH] * 25, after=60)
    llm = ScriptLLM(text_events(S1 + " ", S2, hang=True), text_events("네, 알겠습니다."))
    stt = FakeSTT(texts=[("배송 언제 와요?", "배송 언제 와요?"), ("잠깐만요", "잠깐만요")])
    deps, _ = _deps(tmp_path, ws, llm, stt)
    await run(ws, deps)
    texts = ws.texts()
    types = [t["type"] for t in texts]
    assert "debug.barge_in" in types
    bi = texts[types.index("debug.barge_in")]
    assert bi["late"] is False and bi["voiced_ms"] >= 300
    assert types.index("debug.barge_in") < types.index("turn.end")
    # 데모는 신호를 받으면 재생을 멈추므로, transcript는 끼어든 시점까지만(첫 문장 도중)
    t1 = next(t for t in texts if t["type"] == "agent.transcript")["text"]
    assert t1.startswith(OPENER) and S1 not in t1
    timing = next(t for t in texts if t["type"] == "debug.timing")
    assert timing["barge_in"] is True


async def test_late_barge_in_after_turn_end_marks_context(tmp_path):
    """turn.end 뒤에도 재생이 남은 동안 고객이 말하면: 데모에 재생 중단 신호, 직전 상담원 말은 끊긴 것으로 기록."""
    ws = BargeWS(start_msg(debug=True), utterance(50), interrupt=[SPEECH] * 25, after=5, anchor="turn_end")
    llm = ScriptLLM(text_events(S1 + " ", S2), text_events("네, 말씀하세요."))
    stt = FakeSTT(texts=[("배송 언제 와요?", "배송 언제 와요?"), ("잠깐만요", "잠깐만요")])
    deps, _ = _deps(tmp_path, ws, llm, stt)
    await run(ws, deps)
    texts = ws.texts()
    bis = [t for t in texts if t["type"] == "debug.barge_in"]
    assert len(bis) == 1 and bis[0]["late"] is True
    proto = [t for t in texts if not t["type"].startswith("debug.")]
    assert proto[0]["text"] == f"{OPENER} {S1} {S2}"          # 이미 보낸 transcript는 그대로
    last = llm.calls[-1]["messages"]
    assert last[-1].content == "잠깐만요"
    assert last[-2].role == "assistant" and INTERRUPTED_NOTE in last[-2].content
    assert S2 not in last[-2].content


async def test_short_sound_while_previous_answer_still_plays_is_not_a_new_turn(tmp_path):
    """AG-T59: 직전 답이 아직 재생 중일 때 들어온 300ms 미만 소리(맞장구·스피커 되울림)는 새 턴이 아니다."""
    ws = BargeWS(start_msg(), utterance(50), interrupt=[SPEECH] * 10, after=5, anchor="turn_end",
                 turns=1, after_end_frames=150)
    llm = ScriptLLM(text_events(S1 + " ", S2), text_events("네, 감사합니다."))
    stt = FakeSTT(texts=[("배송 언제 와요?", "배송 언제 와요?"), ("네", "네")])
    deps, _ = _deps(tmp_path, ws, llm, stt)
    await run(ws, deps)
    assert ws.inject_pos == 10                                  # 소리는 실제로 들어갔다
    assert ws.turn_ends == 1 and len(llm.calls) == 1


async def test_short_answer_after_previous_answer_finished_is_still_a_turn(tmp_path):
    """AG-T59: 재생이 끝난 뒤의 짧은 "네"는 지금처럼 답한다(씹지 않는다)."""
    ws = BargeWS(start_msg(), utterance(50), interrupt=[SPEECH] * 10, after=300, anchor="turn_end", turns=2)
    llm = ScriptLLM(text_events(S1 + " ", S2), text_events("네, 감사합니다."))
    stt = FakeSTT(texts=[("배송 언제 와요?", "배송 언제 와요?"), ("네", "네")])
    deps, _ = _deps(tmp_path, ws, llm, stt)
    await run(ws, deps)
    assert ws.turn_ends == 2 and len(llm.calls) == 2
    assert llm.calls[-1]["messages"][-1].content == "네"


def test_demo_client_requests_barge_in_and_stops_playback():
    from pathlib import Path
    js = (Path(__file__).resolve().parents[1] / "demo" / "static" / "app.js").read_text(encoding="utf-8")
    assert "barge_in: true" in js and "opener_adaptive:" in js       # session.start에서 켠다
    assert 'case "debug.barge_in"' in js and "stopPlayback(c)" in js  # 신호를 받으면 재생 중단
    assert "src.stop" in js or "s.stop()" in js

# ── AG-U05 끼어든 말 확인: 내용이 없으면 원래 답을 계속 ─────────────
def test_barge_in_meaningful_rules():
    from agent.session import barge_in_meaningful
    agent = "네, 고객님. 첫 문장입니다. 둘째 문장은 조금 깁니다."
    assert not barge_in_meaningful("", agent)                     # 받아쓰기 없음(잡음·기침)
    assert not barge_in_meaningful("음.", agent)                  # 한 글자
    assert not barge_in_meaningful("네 네.", agent)               # 맞장구
    assert not barge_in_meaningful("첫 문장입니다.", agent)       # 스피커로 되들어온 상담원 말(에코)
    assert not barge_in_meaningful("둘째 문장은 조금", agent)     # 에코 일부
    assert barge_in_meaningful("아니요, 교환이요.", agent)
    assert barge_in_meaningful("그거 취소도 돼요?", agent)


async def test_verify_ignores_echo_and_finishes_original_answer(tmp_path):
    ws = BargeWS(start_msg(), utterance(50), interrupt=[SPEECH] * 25, after=60, turns=1)
    llm = ScriptLLM(text_events(S1 + " ", S2))
    stt = FakeSTT(texts=[("배송 언제 와요?", "배송 언제 와요?"), (S1, S1)])   # 둘째 연결 = 끼어든 말 확인(에코)
    deps, _ = _deps(tmp_path, ws, llm, stt, verify=True)
    await run(ws, deps)
    proto = ws.proto()
    assert [t["type"] for t in proto] == ["agent.transcript", "turn.end"]
    assert proto[0]["text"].endswith(S2)                          # 끊지 않고 끝까지 말했다
    assert 0 not in llm.cancelled
    assert len(stt.streams) == 2                                  # 확인용 STT 연결을 한 번 열었다


async def test_verify_confirms_real_interruption(tmp_path):
    ws = BargeWS(start_msg(), utterance(50), interrupt=[SPEECH] * 25, after=60)
    llm = ScriptLLM(text_events(S1 + " ", S2, hang=True), text_events("네, 교환으로 안내해 드릴게요."))
    stt = FakeSTT(texts=[("배송 언제 와요?", "배송 언제 와요?"), ("아니요, 교환이요", "아니요, 교환이요"),
                         ("아니요, 교환이요", "아니요, 교환이요")])
    deps, _ = _deps(tmp_path, ws, llm, stt, verify=True)
    await run(ws, deps)
    proto = ws.proto()
    assert [t["type"] for t in proto] == ["agent.transcript", "turn.end", "agent.transcript", "turn.end"]
    assert S2 not in proto[0]["text"]                             # 확인 뒤 끊었다
    assert 0 in llm.cancelled
    assert llm.calls[-1]["messages"][-1].content == "아니요, 교환이요"
    assert proto[2]["text"].endswith("네, 교환으로 안내해 드릴게요.")


async def test_verify_empty_text_but_long_speech_counts_as_interruption(tmp_path):
    """받아쓰기가 비어도 사람 소리가 0.6초 넘게 이어졌으면 인식 실패로 보고 끼어들기(데모 0글자 사례)."""
    ws = BargeWS(start_msg(), utterance(50), interrupt=[SPEECH] * 40, after=60)
    llm = ScriptLLM(text_events(S1 + " ", S2, hang=True), text_events("네, 말씀하세요."))
    stt = FakeSTT(texts=[("배송 언제 와요?", "배송 언제 와요?"), ("", ""), ("다시 말할게요", "다시 말할게요")])
    deps, _ = _deps(tmp_path, ws, llm, stt, verify=True)
    await run(ws, deps)
    assert 0 in llm.cancelled
    assert [t["type"] for t in ws.proto()][:2] == ["agent.transcript", "turn.end"]
    assert S2 not in ws.proto()[0]["text"]


async def test_verify_empty_text_and_short_sound_is_ignored(tmp_path):
    ws = BargeWS(start_msg(), utterance(50), interrupt=[SPEECH] * 20, after=60, turns=1)   # 400ms
    llm = ScriptLLM(text_events(S1 + " ", S2))
    stt = FakeSTT(texts=[("배송 언제 와요?", "배송 언제 와요?"), ("", "")])
    deps, _ = _deps(tmp_path, ws, llm, stt, verify=True)
    await run(ws, deps)
    proto = ws.proto()
    assert [t["type"] for t in proto] == ["agent.transcript", "turn.end"] and proto[0]["text"].endswith(S2)
