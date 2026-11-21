"""AG-T20 세션 첫 마디(opener_adaptive): 선호출 첫 문장이 준비됐으면 첫 마디 없이 본 응답, 준비 안 됐으면
상황별 첫 마디를 연속 반복 없이, 사용률 지표. session.start의 "opener_adaptive": true로 켠다(기본은 기존 동작).
"""
import asyncio
import json

from agent.core.types import LLMEvent, ToolCall
from agent.tts_cache import OPENER_KINDS, opener_candidates, opener_phrases
from agent.turn_log import TurnLogWriter
from test_session import (OPENER_PCM, FakeDetector, FakeLLM, FakeSTT, FakeWS, make_deps, pcm_for, run,
                          start_msg, text_events, utterance)


def writer(tmp_path):
    return TurnLogWriter(tmp_path / "logs", date_fn=lambda: "20261121")


def read_log(tmp_path):
    lines = (tmp_path / "logs" / "turns-20261121.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(x) for x in lines]


def phrase_pcm(i: int) -> bytes:
    return bytes([0x30 + i % 64, 0x21]) * 1600            # 100ms, 문구마다 다른 값


def cache_all_openers(deps) -> dict[bytes, str]:
    out = {}
    for i, p in enumerate(opener_phrases(deps.settings)):
        pcm = phrase_pcm(i)
        deps.cache.put(p, pcm)
        out[pcm] = p
    return out


class DelayLLM:
    """i번째 호출은 시작 뒤 delays[i] 프레임이 지나야 첫 이벤트를 낸다."""

    def __init__(self, ws, *scripts, delays=()):
        self.ws = ws
        self.scripts = list(scripts)
        self.delays = list(delays)
        self.calls = []

    async def stream(self, system, messages, tools):
        n = len(self.calls)
        start = self.ws.idx
        self.calls.append({"system": system, "messages": list(messages), "idx": start})
        d = self.delays[n] if n < len(self.delays) else 0
        while self.ws.idx < start + d:
            await asyncio.sleep(0.0005)
        for ev in self.scripts[min(n, len(self.scripts) - 1)]:
            await asyncio.sleep(0)
            yield ev


async def test_answer_ready_sends_presynth_first_sentence_without_opener(tmp_path):
    first = "영업시간은 오전 9시부터입니다."
    ws = FakeWS(start_msg(opener_adaptive=True), utterance(50))
    llm = FakeLLM(text_events(first), ws=ws)
    stt = FakeSTT(partial="영업시간이 어떻게 돼요?")
    deps, det = make_deps(tmp_path, ws=ws, llm=llm, stt=stt, log_writer=writer(tmp_path))
    await run(ws, deps)
    commit = det.decided_at[0]
    bins = ws.binaries()
    assert OPENER_PCM not in [m for _, m in bins]
    assert bins[0][1] == pcm_for(first)
    # 확정 즉시(이벤트 루프 몇 바퀴 = 테스트에선 몇 프레임, 실제로는 수 ms)
    assert commit <= bins[0][0] <= commit + 5
    assert ws.texts()[-2]["text"] == first
    rec = read_log(tmp_path)[0]
    assert rec["opener_used"] is False and rec["opener_reason"] == "answer_ready"
    assert rec["presynth_reused"] is True


async def test_not_ready_uses_opener_and_never_repeats_across_turns(tmp_path):
    # 발신자 번호가 있어야 조회 첫 마디를 쓴다(번호가 없으면 곧 번호를 되묻는 턴이라 중립 첫 마디)
    caller = {"name": "홍길동", "phone": "01012345678"}
    ws = FakeWS(start_msg(opener_adaptive=True, caller=caller), utterance(30), utterance(30), utterance(30), turns=3)
    llm = DelayLLM(ws, text_events("조회해 보니 배송 중입니다."), delays=[25, 25, 25])
    stt = FakeSTT(texts=[("주문 조회해 주세요 공일공 일이삼사 오육칠팔", "주문 조회해 주세요 공일공 일이삼사 오육칠팔"),
                         ("다른 주문도 조회해 주세요 공일공 일이삼사 오육칠팔", "다른 주문도 조회해 주세요 공일공 일이삼사 오육칠팔"),
                         ("그럼 반품 조회도 해 주세요 공일공 일이삼사 오육칠팔", "그럼 반품 조회도 해 주세요 공일공 일이삼사 오육칠팔")])
    deps, _ = make_deps(tmp_path, ws=ws, llm=llm, stt=stt, detector=FakeDetector(None),
                        log_writer=writer(tmp_path))
    by_pcm = cache_all_openers(deps)
    await run(ws, deps)
    recs = read_log(tmp_path)
    openers = [r["opener"] for r in recs]
    assert len(openers) == 3 and all(openers)
    assert all(o in opener_candidates("lookup", deps.settings) for o in openers)
    assert openers[0] != openers[1] and openers[1] != openers[2]
    assert all(r["opener_used"] is True and r["opener_reason"] == "not_ready" for r in recs)
    # 각 턴 첫 오디오 = 그 턴의 첫 마디, 선호출 프롬프트도 같은 첫 마디로 만들었다
    texts = [t for t in ws.texts() if t["type"] == "agent.transcript"]
    for o, t in zip(openers, texts):
        assert t["text"].startswith(o)
    first_bins = [m for _, m in ws.binaries() if m in by_pcm]
    assert [by_pcm[m] for m in first_bins] == openers
    assert f"'{openers[0]}'라고 말했다" in llm.calls[0]["system"]


async def test_tool_expected_uses_lookup_opener_and_different_filler(tmp_path):
    ws = FakeWS(start_msg(opener_adaptive=True), utterance(50))

    class ToolThenSlow:
        def __init__(self):
            self.calls = []

        async def stream(self, system, messages, tools):
            n = len(self.calls)
            self.calls.append(system)
            if n == 0:
                yield LLMEvent("tool_call", tool_call=ToolCall("c1", "get_order_info", {"order_id": "1"}))
                yield LLMEvent("done")
                return
            await asyncio.sleep(0.05)
            yield LLMEvent("text", text="배송 중입니다.")
            yield LLMEvent("done")

    stt = FakeSTT(partial="그거 어떻게 됐어요?")
    deps, _ = make_deps(tmp_path, ws=ws, llm=ToolThenSlow(), stt=stt, settings_kw={"filler_delay_ms": 10},
                        log_writer=writer(tmp_path))
    by_pcm = cache_all_openers(deps)
    s = deps.settings
    for i, f in enumerate((*s.fillers, s.opener_lookup_filler)):
        deps.cache.put(f, bytes([0x70 + i, 0x11]) * 1600)
    await run(ws, deps)
    rec = read_log(tmp_path)[0]
    assert rec["opener_reason"] == "tool_expected"
    assert rec["opener"] in opener_candidates("lookup", s)
    assert by_pcm[ws.binaries()[0][1]] == rec["opener"]
    text = ws.texts()[-2]["text"]
    if "확인" in rec["opener"]:
        assert s.fillers[0] not in text                      # "확인해 드릴게요" 겹침 회피


async def test_debug_timing_reports_opener_rate(tmp_path):
    ws = FakeWS(start_msg(opener_adaptive=True, debug=True), utterance(30), utterance(30), turns=2)
    llm = DelayLLM(ws, text_events("영업시간은 9시부터입니다."), text_events("주말에는 쉽니다."), delays=[0, 25])
    stt = FakeSTT(texts=[("영업시간 알려 주세요", "영업시간 알려 주세요"), ("주말에는요?", "주말에는요?")])
    deps, _ = make_deps(tmp_path, ws=ws, llm=llm, stt=stt, detector=FakeDetector(None))
    cache_all_openers(deps)
    await run(ws, deps)
    timings = [t for t in ws.texts() if t["type"] == "debug.timing"]
    assert [t["opener_used"] for t in timings] == [False, True]
    assert timings[0]["opener_rate"] == 0.0 and timings[1]["opener_rate"] == 0.5
    assert timings[1]["opener_reason"] == "not_ready"


async def test_missing_opener_phrases_are_warmed_in_background(tmp_path):
    ws = FakeWS(start_msg(opener_adaptive=True), utterance(50))
    llm = FakeLLM(text_events("네 알겠습니다."), ws=ws)
    deps, _ = make_deps(tmp_path, ws=ws, llm=llm, stt=FakeSTT(partial="고마워요"))
    await run(ws, deps)
    for k in OPENER_KINDS:
        for p in opener_candidates(k, deps.settings):
            assert deps.cache.get(p) is not None, p


async def test_ask_number_fast_path_skips_opener_and_llm(tmp_path):
    """AG-U05 번호가 하나도 없는 주문 조회 요청: 첫 마디·LLM 없이 캐시된 질문을 바로 낸다."""
    ws = FakeWS(start_msg(opener_adaptive=True), utterance(30))
    llm = FakeLLM(text_events("LLM이 만든 답"), ws=ws)
    stt = FakeSTT(partial="주문한 게 어디쯤 왔는지 궁금해서요", final="주문한 게 어디쯤 왔는지 궁금해서요")
    deps, _ = make_deps(tmp_path, ws=ws, llm=llm, stt=stt, detector=FakeDetector(None), log_writer=writer(tmp_path))
    cache_all_openers(deps)
    ask_pcm = phrase_pcm(99)
    deps.cache.put(deps.settings.ask_number_text, ask_pcm)
    await run(ws, deps)
    first = ws.binaries()[0][1]
    assert first == ask_pcm                                        # 첫 소리가 곧 질문
    texts = [t for t in ws.texts() if t["type"] == "agent.transcript"]
    assert texts[0]["text"] == deps.settings.ask_number_text
    rec = read_log(tmp_path)[0]
    assert rec.get("ask_number_fast") is True


async def test_ask_number_fast_path_not_used_with_caller_phone(tmp_path):
    caller = {"name": "홍길동", "phone": "01012345678"}
    ws = FakeWS(start_msg(opener_adaptive=True, caller=caller), utterance(30))
    llm = FakeLLM(text_events("조회해 볼게요."), ws=ws)
    stt = FakeSTT(partial="주문한 게 어디쯤 왔는지 궁금해서요", final="주문한 게 어디쯤 왔는지 궁금해서요")
    deps, _ = make_deps(tmp_path, ws=ws, llm=llm, stt=stt, detector=FakeDetector(None), log_writer=writer(tmp_path))
    cache_all_openers(deps)
    deps.cache.put(deps.settings.ask_number_text, phrase_pcm(99))
    await run(ws, deps)
    assert llm.calls                                               # 발신자 번호로 조회할 수 있으니 LLM 경로
    assert not read_log(tmp_path)[0].get("ask_number_fast")
