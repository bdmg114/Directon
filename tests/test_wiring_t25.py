"""AG-T25 통합 연결: Jev 인계 판정(DIRECTON_JEV_HANDOFF), Jev 말 끝 판단 맥락(set_context),
바지인·첫 마디 설정(DIRECTON_*), 서버 예열의 첫 마디 후보 합성, 끊긴 상담원 말 뒤 응답 규칙.

외부 호출 없음: Jev 인계 판정기는 fake(judge만 흉내)를 SessionDeps.handoff에 넣는다.
"""
from __future__ import annotations

import asyncio

import pytest

from agent import handoff_jev as hj
from agent.config import Settings
from agent.knowledge import VAR_MARKER
from agent.session import SESSION_DEFAULTS, INTERRUPTED_NOTE, Session
from agent.tts_cache import opener_phrases
from test_session import FakeDetector, FakeLLM, FakeSTT, FakeWS, make_deps, run, start_msg, text_events, utterance

WEEKDAY = "2026-11-20 11:00"       # 금요일 영업시간 → transfer
WEEKEND = "2026-11-21 11:00"       # 토요일 → callback


class FakeHandoff:
    """JevHandoff.judge 흉내. 호출 기록을 남기고 지정한 판정(또는 지연)을 돌려준다."""

    def __init__(self, judgment=None, delay=0.0, fail=False):
        self.judgment = judgment
        self.delay = delay
        self.fail = fail
        self.calls: list[dict] = []

    async def judge(self, utterance, history=None, failures=0, caller_known=False):
        self.calls.append({"utterance": utterance, "history": list(history or []), "failures": failures,
                           "caller_known": caller_known})
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("jev down")
        return self.judgment


def handoff_yes(*reasons):
    reasons = list(reasons or ["human_request"])
    probs = {k: (0.95 if k in reasons else 0.02) for k in hj.CRITERIA}
    return hj.HandoffJudgment(probs, True, reasons, 150.0)


def handoff_no():
    return hj.HandoffJudgment({k: 0.02 for k in hj.CRITERIA}, False, [], 150.0)


@pytest.fixture
def jev_on(monkeypatch):
    monkeypatch.setenv(hj.ENV_SWITCH, "1")


@pytest.fixture
def jev_off(monkeypatch):
    monkeypatch.delenv(hj.ENV_SWITCH, raising=False)


def _deps(tmp_path, ws, llm, handoff, **kw):
    deps, det = make_deps(tmp_path, ws=ws, llm=llm, **kw)
    deps.handoff = handoff
    return deps, det


# ══════════════════════════════════════════════════════
# 1. Jev 인계 판정 연결
# ══════════════════════════════════════════════════════
async def test_handoff_hint_added_and_hintless_precall_discarded(tmp_path, jev_on):
    ws = FakeWS(start_msg(now=WEEKDAY), utterance(50))
    llm = FakeLLM(text_events("선호출 답."), text_events("상담원에게 연결해 드릴게요."), ws=ws)
    stt = FakeSTT(partial="상담원 바꿔 주세요.")
    fh = FakeHandoff(handoff_yes("human_request"))
    deps, _ = _deps(tmp_path, ws, llm, fh, stt=stt)
    await run(ws, deps)

    assert fh.calls and fh.calls[0]["utterance"] == "상담원 바꿔 주세요."
    assert len(fh.calls) == 1                                # 최종 텍스트가 같으면 다시 부르지 않는다
    assert len(llm.calls) == 2                               # 힌트 없는 선호출은 버리고 다시 부른다
    assert "[인계 판정]" not in llm.calls[0]["system"]
    sys_ = llm.calls[1]["system"]
    assert "[인계 판정]" in sys_ and "transfer_to_agent 함수를 호출" in sys_
    assert sys_.index("[인계 판정]") > sys_.index(VAR_MARKER)   # 가변부(캐시 밖)
    assert sys_.startswith(llm.calls[0]["system"])           # 고정부·가변부 그대로 + 힌트만 뒤에
    assert ws.texts()[-2]["text"] == "네, 고객님. 상담원에게 연결해 드릴게요."


async def test_handoff_outside_hours_routes_to_callback(tmp_path, jev_on):
    ws = FakeWS(start_msg(now=WEEKEND), utterance(50))
    llm = FakeLLM(text_events("선호출 답."), text_events("콜백 접수해 드릴게요."), ws=ws)
    fh = FakeHandoff(handoff_yes("human_request"))
    deps, _ = _deps(tmp_path, ws, llm, fh, stt=FakeSTT(partial="상담원 연결해 주세요."))
    await run(ws, deps)
    sys_ = llm.calls[-1]["system"]
    assert "request_callback" in sys_.split("[인계 판정]", 1)[1]
    assert "transfer_to_agent 함수를 호출" not in sys_


async def test_handoff_switch_off_never_judges(tmp_path, jev_off):
    ws = FakeWS(start_msg(now=WEEKDAY), utterance(50))
    llm = FakeLLM(text_events("안내해 드릴게요."), ws=ws)
    fh = FakeHandoff(handoff_yes())
    deps, _ = _deps(tmp_path, ws, llm, fh)
    await run(ws, deps)
    assert fh.calls == []
    assert len(llm.calls) == 1 and "[인계 판정]" not in llm.calls[0]["system"]


async def test_handoff_none_dep_is_todays_behaviour(tmp_path, jev_on):
    ws = FakeWS(start_msg(now=WEEKDAY), utterance(50))
    llm = FakeLLM(text_events("안내해 드릴게요."), ws=ws)
    deps, _ = _deps(tmp_path, ws, llm, None)
    await run(ws, deps)
    assert len(llm.calls) == 1


async def test_no_handoff_keeps_precall(tmp_path, jev_on):
    ws = FakeWS(start_msg(now=WEEKDAY), utterance(50))
    llm = FakeLLM(text_events("배송은 내일 와요."), ws=ws)
    fh = FakeHandoff(handoff_no())
    deps, _ = _deps(tmp_path, ws, llm, fh)
    await run(ws, deps)
    assert len(fh.calls) == 1
    assert len(llm.calls) == 1 and "[인계 판정]" not in llm.calls[0]["system"]


@pytest.mark.parametrize("fh", [FakeHandoff(handoff_yes(), delay=2.0), FakeHandoff(fail=True)],
                         ids=["timeout", "error"])
async def test_handoff_timeout_or_error_behaves_as_today(tmp_path, jev_on, fh):
    ws = FakeWS(start_msg(now=WEEKDAY), utterance(50))
    llm = FakeLLM(text_events("안내해 드릴게요."), ws=ws)
    deps, _ = _deps(tmp_path, ws, llm, fh)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await run(ws, deps)
    assert len(llm.calls) == 1 and "[인계 판정]" not in llm.calls[0]["system"]
    assert ws.texts()[-2]["text"] == "네, 고객님. 안내해 드릴게요."
    assert loop.time() - t0 < 1.9                            # 판정을 기다리느라 응답이 막히지 않는다


async def test_handoff_refired_when_final_text_differs(tmp_path, jev_on):
    ws = FakeWS(start_msg(now=WEEKDAY), utterance(50))
    llm = FakeLLM(text_events("앞부분 답."), text_events("다시 답."), text_events("연결해 드릴게요."), ws=ws)
    fh = FakeHandoff(handoff_yes("health"))
    deps, _ = _deps(tmp_path, ws, llm, fh, stt=FakeSTT(partial="먹고 나서", final="먹고 나서 두드러기가 났어요."))
    await run(ws, deps)
    utts = [c["utterance"] for c in fh.calls]
    assert utts[0] == "먹고 나서" and utts[-1] == "먹고 나서 두드러기가 났어요."
    sys_ = llm.calls[-1]["system"]
    assert "[인계 판정]" in sys_ and "병원" in sys_
    assert llm.calls[-1]["messages"][-1].content == "먹고 나서 두드러기가 났어요."


async def test_handoff_in_adaptive_opener_mode_uses_opener(tmp_path, jev_on):
    """첫 마디 정책을 켜도 인계 턴은 힌트 없는 선합성 답을 버리므로 첫 마디를 쓴다."""
    from test_opener_session import cache_all_openers

    ws = FakeWS(start_msg(now=WEEKDAY, opener_adaptive=True), utterance(50))
    llm = FakeLLM(text_events("선호출 답이에요."), text_events("상담원에게 연결해 드릴게요."), ws=ws)
    fh = FakeHandoff(handoff_yes("abuse_anger"))
    deps, _ = _deps(tmp_path, ws, llm, fh, stt=FakeSTT(partial="이게 말이 돼요?"))
    cache_all_openers(deps)
    await run(ws, deps)
    assert "[인계 판정]" in llm.calls[-1]["system"]
    said = ws.texts()[-2]["text"]
    assert "선호출 답이에요." not in said and said.endswith("상담원에게 연결해 드릴게요.")
    assert said != "상담원에게 연결해 드릴게요."               # 첫 마디가 앞에 있다


async def test_handoff_history_includes_conversation(tmp_path, jev_on):
    hist = [{"role": "customer", "text": "환불 언제 돼요?"}, {"role": "agent", "text": "확인해 볼게요."}]
    ws = FakeWS(start_msg(now=WEEKDAY, history=hist, caller={"name": "김", "phone": "01012345678"}),
                utterance(30), utterance(30), turns=2)
    llm = FakeLLM(text_events("첫 답이에요."), ws=ws)
    stt = FakeSTT(texts=[("첫 질문이요", "첫 질문이요"), ("두 번째 질문이요", "두 번째 질문이요")])
    fh = FakeHandoff(handoff_no())
    deps, _ = _deps(tmp_path, ws, llm, fh, stt=stt)
    await run(ws, deps)
    last = fh.calls[-1]
    assert last["utterance"] == "두 번째 질문이요"
    assert last["caller_known"] is True
    assert last["history"][:2] == hist
    assert {"role": "customer", "text": "첫 질문이요"} in last["history"]
    assert any(m["role"] == "agent" and "첫 답이에요." in m["text"] for m in last["history"][2:])




# ══════════════════════════════════════════════════════
# 2. Jev 말 끝 판단 맥락
# ══════════════════════════════════════════════════════
class ContextDetector(FakeDetector):
    def __init__(self, ws=None, end_ms=600):
        super().__init__(ws, end_ms)
        self.contexts: list[tuple[list, bool]] = []

    def set_context(self, history, caller_known=False):
        self.contexts.append((list(history or []), caller_known))


async def test_detector_context_on_start_and_after_turn(tmp_path):
    hist = [{"role": "agent", "text": "주문번호를 말씀해 주시겠어요?"}]
    ws = FakeWS(start_msg(history=hist, caller={"phone": "01012345678"}), utterance(30), utterance(30), turns=2)
    det = ContextDetector(ws)
    llm = FakeLLM(text_events("확인해 드릴게요."), ws=ws)
    stt = FakeSTT(texts=[("이육일일", "이육일일"), ("감사합니다", "감사합니다")])
    deps, _ = make_deps(tmp_path, ws=ws, llm=llm, stt=stt, detector=det)
    await run(ws, deps)
    assert det.contexts[0] == (hist, True)                   # 세션 시작
    after = det.contexts[1][0]                               # 첫 턴 뒤: 이전 대화 + 이번 대화
    assert after[0] == hist[0]
    assert {"role": "customer", "text": "이육일일"} in after
    assert any(m["role"] == "agent" and "확인해 드릴게요." in m["text"] for m in after)


# ══════════════════════════════════════════════════════
# 3. 설정(환경변수)
# ══════════════════════════════════════════════════════
def test_settings_defaults_match_session_defaults():
    s = Settings()
    for k, v in SESSION_DEFAULTS.items():
        assert getattr(s, k) == v, k
    assert s.opener_adaptive is False                        # 측정 전에는 끔(사용자 결정)
    assert s.barge_in is True


def test_settings_env_overrides():
    s = Settings.from_env({"DIRECTON_OPENER_ADAPTIVE": "1", "DIRECTON_BARGE_IN": "0",
                           "DIRECTON_BARGE_IN_MS": "400", "DIRECTON_BARGE_IN_ARM_MS": "800",
                           "DIRECTON_BARGE_IN_GAP_MS": "80", "DIRECTON_PACE_LEAD_MS": "900",
                           "DIRECTON_PACE_CAP_S": "8.5", "DIRECTON_OPENER_GRACE_MS": "150"})
    assert s.opener_adaptive is True and s.barge_in is False
    assert (s.barge_in_ms, s.barge_in_arm_ms, s.barge_in_gap_ms, s.pace_lead_ms) == (400, 800, 80, 900)
    assert s.pace_cap_s == 8.5 and s.opener_grace_ms == 150


def test_session_reads_settings_and_start_overrides(tmp_path):
    ws = FakeWS(start_msg())
    deps, _ = make_deps(tmp_path, ws=ws, llm=FakeLLM(text_events("네.")),
                        settings_kw={"barge_in": False, "opener_adaptive": True})
    sess = Session(ws, deps)
    assert sess.barge_in is False and sess.opener_adaptive is True


async def test_start_message_overrides_settings(tmp_path):
    ws = FakeWS(start_msg(barge_in=True, opener_adaptive=False), utterance(30))
    deps, _ = make_deps(tmp_path, ws=ws, llm=FakeLLM(text_events("네 안내해 드릴게요.")),
                        settings_kw={"barge_in": False, "opener_adaptive": True})
    sess = Session(ws, deps)
    await asyncio.wait_for(sess.run(), 10.0)
    assert sess.barge_in is True and sess.opener_adaptive is False


# ══════════════════════════════════════════════════════
# 4. 서버 예열: 첫 마디 후보 전체
# ══════════════════════════════════════════════════════
async def test_warmup_synthesizes_all_opener_phrases(tmp_path):
    from agent.server import warmup
    from agent.session import SessionDeps
    from agent.tools_client import ToolsClient
    from agent.tts_cache import TTSCache
    from test_server_wiring import DONE, FakeSmart, FakeVAD, ScriptLLM, ToneTTS, text
    from test_server_wiring import FakeSTT as SFakeSTT

    s = Settings(tts_cache_dir=str(tmp_path / "c"))
    deps = SessionDeps(stt=SFakeSTT(), tts=ToneTTS(amp=3000), llm=ScriptLLM("p", [text("네."), DONE]),
                       vad_factory=FakeVAD, smart_turn=FakeSmart(), detector_factory=lambda: None,
                       tools=ToolsClient(), cache=TTSCache(s.tts_cache_dir), settings=s)
    info = await warmup(deps)
    for p in opener_phrases(s):
        assert deps.cache.get(p) is not None, p
    from agent import knowledge

    extra = [p for p in opener_phrases(s) if p not in s.cached_phrases()]
    more = [p for p in (*knowledge.FAIL_SENTENCES.values(), *knowledge.TOOL_PHRASES.values(), knowledge.NEUTRAL_PHRASE,
                          *knowledge.COMMON_SENTENCES, *knowledge.empathy_variants())
            if p not in s.cached_phrases() and p not in extra]
    assert extra and info["opener_synthesized"] == len(set(extra + more))   # 첫 마디 후보 + 함수 문구·실패 문장
    for p in more:
        assert deps.cache.get(p) is not None, p
    assert info["cache_synthesized"] == len(s.cached_phrases())   # 기존 지표 그대로


# ══════════════════════════════════════════════════════
# 5. 끊긴 상담원 말 뒤 응답 규칙(고정부)
# ══════════════════════════════════════════════════════
def test_prompt_rule_for_interrupted_previous_answer():
    from agent import knowledge

    fixed = knowledge.RULES
    assert INTERRUPTED_NOTE in fixed
    line = next(ln for ln in fixed.splitlines() if INTERRUPTED_NOTE in ln)
    assert "되풀이" in line and "사과" in line
    assert VAR_MARKER not in fixed
