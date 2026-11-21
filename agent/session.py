"""측정기 세션 하나. 프레임 수신 → 턴 감지 → 첫 마디 → 본 응답 → transcript → turn.end.

타이밍 규칙(프레임 수 기준, 20ms/프레임):
- 확정 전에는 아무 오디오도 보내지 않는다.
- 침묵 precall_silence_ms(200)에 STT finalize, Smart Turn, LLM 선호출을 시작한다.
- finalize가 확정 전에 끝나면 그 텍스트로 선호출을 다시 건다(이전 선호출과 정규화 텍스트가 다를 때, AG-T10c).
  결정기에도 그때부터 finalize 텍스트를 준다(짧은 발화는 부분 결과가 비어 있는 일이 잦다).
- 선호출 LLM의 첫 문장은 확정 전에 미리 합성해 버퍼에만 둔다(송신하지 않는다). 재사용되면 그 버퍼를 이어 보낸다.
- 확정 전에 음성이 다시 들리면 선호출·finalize·선합성을 취소하고 처음 상태로 돌아간다.
- 확정되면 같은 프레임 처리 안에서 캐시된 첫 마디 PCM을 즉시 보낸다. 첫 마디는 확정 시점 텍스트로 고른다
  (선호출이 있으면 그 선호출을 만들 때 고른 첫 마디 — 시스템 프롬프트의 "이미 '…'라고 말했다"와 맞춘다).
- finalize 텍스트가 선호출 텍스트와 정규화 비교(match_key: 공백·문장부호 제거, 한글 숫자→아라비아 숫자)로 같으면
  선호출 결과를 그대로 쓰고, 다르면 LLM을 다시 부른다.
- 확정부터 safety_timeout_s 안에 turn.end를 보낸다. 실패·시간 초과면 캐시 사과 문구.
- turn.end 뒤에도 연결이 살아 있으면 프레임을 계속 읽는다(다음 발화를 다시 받는다).

디버그(AG-T14, 데모 전용): session.start에 "debug": true가 있을 때만 debug.* 텍스트 이벤트를 더 보낸다
(debug.turn, debug.stt, debug.tool, debug.timing, debug.barge_in). 없으면 송신 메시지는 기존과 같다.

바지인(AG-T19, 기본 켬, session.start "barge_in": false로 끔):
- 응답 중에도 들어오는 프레임을 VAD에 넣는다. 상담원 첫 소리가 나간 뒤 barge_in_arm_ms(1000) 이후에 시작된
  연속 음성(사이 끊김 ≤ barge_in_gap_ms 60)이 barge_in_ms(300) 쌓이면 끼어들기로 본다. 300ms 미만 맞장구("네")는 무시.
  첫 소리 1초 안에 시작된 말은 잘못 끊은 발화의 연속일 수 있어 기존처럼 흘린다(측정기의 말 중간 쉼 문항 보호).
- 끼어들면 남은 TTS 송신을 즉시 멈추고 그 턴의 LLM·TTS·선호출을 취소한 뒤, 실제로 내보낸 만큼의 말로
  agent.transcript + turn.end를 보낸다(프로토콜 유지). 끼어든 말(시작 200ms 전부터)은 새 턴으로 이어 듣고,
  대화 맥락에는 직전 상담원 말이 끊겼다고 남긴다(INTERRUPTED_NOTE).
- 끊을 수 있게 응답 오디오는 재생 시계보다 pace_lead_ms(1500) 넘게 앞서 보내지 않는다(확정 뒤 pace_cap_s(10초)까지만;
  측정기 turn.end 25초 한도 보호). 첫 소리는 그대로 즉시 나간다. 재생 시계 = max(받은 프레임 수 × 20ms, 벽시계).
- turn.end 뒤에도 재생이 남은 동안 고객이 300ms 넘게 말하면 직전 상담원 말을 끊긴 것으로 고친다(늦은 끼어들기).
- 디버그 세션(데모)에는 debug.barge_in을 보내 클라이언트가 재생을 멈추게 한다. 이때 transcript는 끼어든 시점까지.

첫 마디 재설계(AG-T20, session.start "opener_adaptive": true 또는 settings.opener_adaptive로 켬, 기본은 기존 동작):
- 확정 때 선호출 첫 문장이 합성돼 있고 최종 텍스트와 일치하면 첫 마디 없이 바로 본 응답.
- 아니면(LLM 미도착, 함수 호출 예상) 상황별 첫 마디를 직전과 겹치지 않게 고른다(tts_cache.choose_opener).
  선합성이 진행 중이면 opener_grace_ms(200)까지 기다려 본다. 툴 필러도 직전·첫 마디와 겹치지 않게 고른다.
- 첫 마디 사용률: 턴 로그 opener_used/opener_reason, debug.timing opener_rate.

Jev 인계 판정(AG-T25, DIRECTON_JEV_HANDOFF=1이고 SessionDeps.handoff가 있을 때만. 근거 docs/decisions/jev_handoff.md 6절):
- LLM 선호출과 같은 순간(침묵 200ms, finalize 뒤 재선호출 때 포함)에 판정을 쏜다. 텍스트가 같으면 다시 쏘지 않는다.
- 응답 시작 때 최종 텍스트로 한 번 더 확인(다르면 다시 쏜다)하고 jev_handoff_wait_s(0.3초)까지만 기다린다.
- 인계면 시스템 프롬프트 끝(가변부, `## 이번 통화` 뒤)에 handoff_jev.system_hint를 붙이고, 힌트 없이 시작한 선호출은 버린다.
  영업시간 밖이면 힌트가 request_callback으로 안내한다(handoff_jev.route). 시간 초과·오류·인계 아님 → 기존과 같다.
- 턴 로그 jev_handoff: route·reasons·probs·ms.

Jev 말 끝 판단 맥락(AG-T25): 결정기에 set_context가 있으면(JevTurnDetector) 세션 시작·매 턴 새 결정기에 대화 맥락을 넣는다.
"""
from __future__ import annotations

import array
import asyncio
import collections
import dataclasses
import json
import logging
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable

from agent.config import Settings
from agent.handoff_jev import handoff_enabled, route, system_hint
from agent.core.types import (FRAME_BYTES, FRAME_MS, Message, SessionInfo, SmartTurn, STTProvider, ToolCall,
                              STTStream, TTSProvider, LLMProvider, TurnDetector, TurnSignals, VAD)
from agent.normalize import spoken_digits
from agent.guards import GuardContext, valid_phone
from agent.knowledge import build_system, fix_brand
from agent.responder import NormalizeArgs, Prefetch, Responder, SentenceSplitter
from agent.tools_client import ToolsClient
from agent.tts_cache import (OPENER_KINDS, TTSCache, choose_filler, choose_opener, opener_candidates,
                             opener_kind, opener_phrases, select_opener, select_opener_for_text)
from agent.turn_log import TurnLog, TurnLogWriter

try:  # 실제 websockets 연결이 끊긴 경우
    from websockets.exceptions import ConnectionClosed
except Exception:  # pragma: no cover
    class ConnectionClosed(Exception):  # type: ignore[no-redef]
        pass

log = logging.getLogger(__name__)


def _frame_db(frame: bytes) -> float:
    """프레임 에너지(dBFS). numpy 없이(프레임 320샘플)."""
    a = array.array("h")
    a.frombytes(frame[: len(frame) // 2 * 2])
    if not a:
        return -120.0
    return 10.0 * math.log10(sum(x * x for x in a) / len(a) / (32768.0 ** 2) + 1e-12)


PRELOAD_MAX_CHARS = 3000        # 미리 조회한 주문 결과를 프롬프트에 넣을 때의 상한
NEXT_TURN_STT_FRAMES = 40       # turn.end 뒤 이만큼(0.8초 분량) 프레임이 더 오면 여러 턴 통화로 보고 STT를 다시 연다


@dataclass
class SessionDeps:
    stt: STTProvider
    tts: TTSProvider
    llm: LLMProvider
    vad_factory: Callable[[], VAD]                  # 세션마다 새 VAD(상태 있음)
    smart_turn: SmartTurn | None
    detector_factory: Callable[[], TurnDetector]    # 세션마다 새 결정기
    tools: ToolsClient
    cache: TTSCache
    settings: Settings = field(default_factory=Settings)
    normalize_args: NormalizeArgs | None = None
    log_writer: TurnLogWriter | None = None
    clock: Callable[[], float] = time.perf_counter
    manual: str | None = None                       # 고정부를 직접 줄 때(테스트). None이면 지식 폴더에서 읽는다
    handoff: Any = None                             # AG-T25 Jev 인계 판정기(handoff_jev.JevHandoff). None이면 끔


# ── 바지인·첫 마디 기본값(AG-T19·T20) ─────────────────
# Settings에 같은 이름 필드가 있으면 그 값을 쓴다. AG-T25부터 전부 agent/config.py 필드(DIRECTON_<이름> 환경변수)이고,
# 여기는 Settings 대용 객체를 넘길 때의 기본값이다(두 곳 값이 같아야 한다: tests/test_wiring_t25.py).
SESSION_DEFAULTS: dict[str, Any] = {
    "barge_in": True,
    "barge_in_ms": 300,             # 이만큼 연속 음성이면 끼어들기
    "barge_in_gap_ms": 60,          # 연속 음성 사이 이 이하 끊김은 이어진 것으로
    "barge_in_arm_ms": 1000,        # 첫 소리 뒤 이 시간 이후에 시작된 말만 끼어들기로 본다
    "barge_in_preroll_ms": 200,     # 끼어든 말 시작 전 이만큼을 새 턴 STT에 같이 넣는다
    "pace_lead_ms": 1500,           # 응답 오디오를 재생 시계보다 이 이상 앞서 보내지 않는다(0이면 끔)
    "pace_cap_s": 10.0,             # 확정 뒤 이 시간이 지나면 속도 조절 없이 보낸다(turn.end 25초 한도)
    "opener_adaptive": False,       # AG-T20 첫 마디 정책
    "opener_grace_ms": 200,         # 선합성이 진행 중이면 이만큼 기다려 본다
}
_HARD_CAP_S = 23.0                  # 속도 조절로 안전 상한을 늘려도 확정 뒤 이 시간은 넘기지 않는다

INTERRUPTED_NOTE = "(고객이 말을 끊어 여기까지만 말함)"


def _opt(settings: Settings, name: str) -> Any:
    return getattr(settings, name, SESSION_DEFAULTS[name])


def _cut_words(text: str, frac: float) -> str:
    """text 앞에서 frac 비율만큼, 어절 경계에서 자른다(어절 하나도 못 채우면 "")."""
    n = int(len(text) * max(0.0, min(1.0, frac)))
    if n >= len(text):
        return text
    head = text[:n]
    if not text[n].isspace():
        sp = head.rfind(" ")
        head = head[:sp] if sp >= 0 else ""
    return head.rstrip(" ,")


def heard_text(spoken: list[str], segs: dict[int, tuple[float, float]], stop_ms: float) -> str:
    """실제로 재생된 말: spoken[i]의 재생 구간 segs[i]=(시작, 끝 ms) 중 stop_ms 전까지. 걸친 문장은 비율로 자른다."""
    out: list[str] = []
    for i, text in enumerate(spoken):
        seg = segs.get(i)
        if seg is None or not text:
            continue
        start, end = seg
        if start >= stop_ms:
            break
        if end <= stop_ms:
            out.append(text)
            continue
        cut = _cut_words(text, (stop_ms - start) / max(1e-6, end - start))
        if cut:
            out.append(cut)
        break
    return " ".join(out)


def interrupted_context(heard: str) -> str:
    heard = (heard or "").strip()
    return f"{heard}… {INTERRUPTED_NOTE}" if heard else f"… {INTERRUPTED_NOTE}"


def _expect_short_from_history(history: list[dict]) -> bool:
    if not history:
        return False
    last = history[-1]
    if not isinstance(last, dict) or last.get("role") not in ("agent", "assistant", "bot"):
        return False
    text = (last.get("text") or last.get("content") or "").strip()
    return text.endswith("?") or text.endswith("？")


# ── 선호출 재사용 비교(AG-T10c) ───────────────────────
_TOKEN_SPLIT = re.compile(r"[\s,.!?~…·]+")
_NON_WORD = re.compile(r"[^\w]+")


def match_key(text: str) -> str:
    """선호출 재사용 비교용 정규화: 어절마다 한글 숫자 낭독을 아라비아 숫자로 바꾸고(agent.normalize.spoken_digits),
    공백·문장부호를 지우고 소문자로. 부분 결과("이육일일 일오공")와 최종 결과("2611150")의 표기 차이를 없앤다."""
    out = []
    for tok in _TOKEN_SPLIT.split(text or ""):
        if not tok:
            continue
        try:
            d = spoken_digits(tok)
        except Exception:  # noqa: BLE001
            d = None
        out.append(d if d else tok)
    return _NON_WORD.sub("", "".join(out)).lower()


def _nchars(s: str) -> int:
    return len(re.sub(r"\s", "", s))


class _PreSynth:
    """선호출 첫 문장의 TTS를 미리 받아 버퍼에 쌓는다(송신하지 않음). stream()은 버퍼 + 이어지는 덩어리."""

    def __init__(self, text: str, tts: TTSProvider):
        self.text = text
        self.chunks: list[bytes] = []
        self.done = False
        self.error: BaseException | None = None
        self.claimed = False
        self._ev = asyncio.Event()
        self.task = asyncio.create_task(self._run(tts))

    async def _run(self, tts: TTSProvider) -> None:
        try:
            async for c in tts.synthesize(self.text):
                self.chunks.append(c)
                self._ev.set()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            self.error = e
        finally:
            self.done = True
            self._ev.set()

    async def stream(self) -> AsyncIterator[bytes]:
        i = 0
        while True:
            if i < len(self.chunks):
                yield self.chunks[i]
                i += 1
                continue
            if self.done:
                if self.error is not None:
                    raise self.error
                if self.task.cancelled():
                    raise RuntimeError("presynth cancelled")
                return
            self._ev.clear()
            await self._ev.wait()

    def cancel(self) -> None:
        self.task.cancel()


@dataclass
class _Precall:
    text: str
    key: str
    opener: str
    prefetch: Prefetch
    presynth: _PreSynth | None = None
    watcher: asyncio.Task | None = None
    closed: bool = False            # True면 더는 선합성을 시작하지 않는다
    preload: bool = False           # 미리 조회한 주문이 프롬프트에 들어간 상태로 시작했는가

    def cancel(self) -> None:
        self.closed = True
        self.prefetch.cancel()
        if self.watcher is not None and not self.watcher.done():
            self.watcher.cancel()
        if self.presynth is not None and not self.presynth.claimed:
            self.presynth.cancel()


class _PresynthTTS:
    """TTSProvider 래퍼: 재사용된 선호출의 선합성 문장이면 그 버퍼를 이어 주고, 아니면 원래 TTS."""

    def __init__(self, inner: TTSProvider, precall: _Precall | None):
        self.inner = inner
        self.precall = precall
        self.used = False

    def __getattr__(self, name: str) -> Any:
        if name == "inner":
            raise AttributeError(name)
        return getattr(self.inner, name)

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        pc = self.precall
        pre = pc.presynth if pc is not None else None
        if pc is not None and pre is None:
            pc.closed = True                                # 이제 시작해 봐야 늦다
        if pre is not None and not pre.claimed and pre.text == text and not pre.task.cancelled():
            pre.claimed = True
            self.used = True
            sent = False
            try:
                async for c in pre.stream():
                    sent = True
                    yield c
                return
            except Exception:  # noqa: BLE001
                if sent:
                    raise
                log.warning("presynth failed, synthesizing again: %r", text[:40])
        async for c in self.inner.synthesize(text):
            yield c


class _DebugTurnLog(TurnLog):
    """event()마다 listener를 부른다(디버그 세션에서 응답기의 툴 이벤트를 받기 위해)."""

    listener: Callable[[str, dict], None] | None = None

    def event(self, name: str, **extra: Any) -> None:
        super().event(name, **extra)
        if self.listener is not None:
            try:
                self.listener(name, extra)
            except Exception:  # noqa: BLE001
                pass


def _ms_between(marks: dict[str, float], a: str, b: str) -> float | None:
    if a in marks and b in marks:
        return round(marks[b] - marks[a], 1)
    return None


class Session:
    def __init__(self, ws: Any, deps: SessionDeps):
        self.ws = ws
        self.d = deps
        self.s = deps.settings
        self.closed = False
        self.debug = False              # session.start의 "debug": true(데모 전용)
        self._dbg_pending: list[dict] = []
        self._dbg_tool_args: dict[str, list] = {}
        self.frame_idx = 0
        self.info: SessionInfo | None = None
        self.vad = deps.vad_factory()
        self.detector = deps.detector_factory()
        self.stt: STTStream | None = None
        self._stt_open: asyncio.Task | None = None
        self._stt_lazy_at: int | None = None        # 남은 프레임 수. 0이 되면 다음 턴 STT를 연다
        self._stt_backfill = False
        self._tools_task: asyncio.Task | None = None
        self.opener = ""                # 세션 기본 첫 마디(select_opener)
        self.system = ""
        self._preload_text = ""         # 통화 시작 때 미리 조회한 주문(AG-T34)
        self._preload_results: list[dict] = []
        self._preload_task: asyncio.Task | None = None
        self._systems: dict[str, str] = {}
        self.conversation: list[Message] = []
        self.live_history: list[dict] = []          # 이번 연결에서 쌓인 대화(공식 history 형식: 고객·함수 호출·결과·상담원)
        self._live_calls = 0
        self.expect_short = False
        self.responding = False
        self._response_task: asyncio.Task | None = None
        self._ring: collections.deque[bytes] = collections.deque(
            maxlen=max(1, int(self.s.smart_turn_window_s * 1000 / FRAME_MS)))
        # AG-T19 바지인 / 재생 시계
        self.barge_in = bool(_opt(self.s, "barge_in"))
        self._frame_ev = asyncio.Event()
        self._wall0: float | None = None
        self._play_end_ms = 0.0                         # 지금까지 보낸 오디오가 재생을 마치는 시각(재생 시계)
        self._prev_play: dict | None = None             # 직전 턴 재생 정보(늦은 끼어들기용)
        self._replay: list[bytes] | None = None         # 끼어든 말: 다음 턴 시작 때 다시 처리할 프레임
        self._work_task: asyncio.Task | None = None
        self._safety: Any = None                        # 응답 안전 상한(asyncio.timeout)
        self._safety_cap: float | None = None
        self._turn_end_sent = False
        # AG-T20 첫 마디
        self.opener_adaptive = bool(_opt(self.s, "opener_adaptive"))
        self._recent_openers: list[str] = []
        self._last_filler: str | None = None
        self._warm_task: asyncio.Task | None = None
        self.turn_count = 0
        self.opener_turns = 0
        # AG-T25 Jev 인계 판정
        self._handoff_task: asyncio.Task | None = None
        self._handoff_key = ""
        self.failures = 0                               # 재질문(빈 텍스트)·응답 실패 횟수(판정 상태 agent_failed_…)
        # AG-T55 손님 목소리 크기(통화 내내 유지): 확실한 음성 프레임 에너지(dBFS) 최근 3초
        self._voice_db: collections.deque[float] = collections.deque(maxlen=150)
        self.gated_frames = 0                           # 목소리 크기 기준으로 침묵으로 바꾼 프레임 수
        self._reset_turn()

    # ── 상태 ─────────────────────────────────────────
    def _reset_turn(self) -> None:
        self._turn_spoken: list[str] = []
        self._seg_play: dict[int, list[float]] = {}
        self._first_audio_ms: float | None = None
        self._commit_ms = 0.0
        self._commit_text = ""
        self._barged = False
        self._bi_buf: collections.deque[bytes] = collections.deque(maxlen=150)
        self._bi_run_start: int | None = None
        self._bi_onset_ms = 0.0
        self._bi_voiced = 0
        self._bi_gap = 0
        self._bi_ignored = False
        self._bi_frames: list[bytes] = []
        self._bi_info: dict | None = None
        self._late_barge_done = False
        self._speech_onset_ms = 0.0
        self._turn_user_text = ""
        self._turn_filler: str | None = None
        self.speech_ms = 0
        self.silence_ms = 0
        self.prepared = False
        # AG-T55 소음 속 말 끝
        self._turn_ms = 0                               # 이번 턴에 처리한 오디오 길이(프레임 시계)
        self._resume_ms = 0                             # 침묵 중 이어진 음성 길이(resume_min_ms 전까지 보류)
        self._text_last = ""
        self._text_changed_ms = 0                       # 받아쓰기 글이 마지막으로 바뀐 프레임 시계
        self._stall_reason = ""                         # 글 멈춤 확정 대기 중이면 그 이유
        self._stall_commit_ms = 0
        self.smart_p: float | None = None
        self._finalize_task: asyncio.Task | None = None
        self._smart_task: asyncio.Task | None = None
        self._precall: _Precall | None = None
        self._final_seen = False
        self._early_final: str | None = None            # 확정 전에 끝난 finalize 텍스트
        self.turn_opener = ""
        self._opener_ms: int | None = None
        self.tlog = self._new_tlog(self.info.session_id if self.info else "")
        self.precall_cancels = 0
        self.precall_count = 0

    def _new_tlog(self, session_id: str, t0: float | None = None) -> TurnLog:
        if not self.debug:
            return TurnLog(session_id, clock=self.d.clock, t0=t0)
        tl = _DebugTurnLog(session_id, clock=self.d.clock, t0=t0)
        tl.listener = self._on_tlog_event
        return tl

    # ── 디버그(데모 전용) ───────────────────────────
    def _on_tlog_event(self, name: str, extra: dict) -> None:
        """응답기의 툴 이벤트 → debug.tool(다음 송신 직전에 같이 보낸다)."""
        if name == "tool_call":
            self._dbg_tool_args.setdefault(extra.get("tool") or "", []).append(extra.get("args"))
        elif name in ("tool_result", "tool_blocked"):
            tool = extra.get("tool") or ""
            if name == "tool_result":
                q = self._dbg_tool_args.get(tool) or []
                args = q.pop(0) if q else None
                status = extra.get("status")
            else:
                args, status = extra.get("args"), "blocked"
            self._dbg_pending.append({"type": "debug.tool", "name": tool, "arguments": args,
                                      "status": status})

    async def _debug(self, obj: dict) -> None:
        if self.debug:
            await self._send_json(obj)

    def _timing(self) -> dict:
        m = self.tlog.marks
        f = self.tlog.fields
        return {"type": "debug.timing",
                "decision_reason": f.get("decision_reason"),
                "speech_ms": f.get("speech_ms"),
                "silence_to_commit_ms": _ms_between(m, "silence_start", "commit"),
                "commit_to_opener_ms": _ms_between(m, "commit", "opener_start"),
                "commit_to_finalize_ms": _ms_between(m, "commit", "finalize_done"),
                "commit_to_llm_first_ms": _ms_between(m, "commit", "llm_first_token"),
                "commit_to_response_audio_ms": _ms_between(m, "commit", "response_first_audio"),
                "commit_to_turn_end_ms": _ms_between(m, "commit", "turn_end"),
                "gap_ms": f.get("gap_ms"),
                "opener": f.get("opener"),
                "opener_used": f.get("opener_used"),
                "opener_reason": f.get("opener_reason"),
                "opener_rate": round(self.opener_turns / self.turn_count, 3) if self.turn_count else None,
                "barge_in": bool(f.get("barge_in")),
                "barge_in_reaction_ms": f.get("barge_in_reaction_ms"),
                "precall_reused": f.get("precall_reused"),
                "presynth_reused": f.get("presynth_reused"),
                "failed": f.get("failed")}

    def _cancel_prepared(self) -> None:
        for t in (self._finalize_task, self._smart_task):
            if t is not None and not t.done():
                t.cancel()
        if self._precall is not None:
            self._precall.cancel()
        self._finalize_task = self._smart_task = None
        self._precall = None
        self._final_seen = False
        self._early_final = None
        self.smart_p = None
        self.prepared = False

    # ── 송신 ─────────────────────────────────────────
    async def _send(self, msg: bytes | str) -> None:
        if self.closed:
            return
        try:
            while self._dbg_pending:                    # 디버그 세션에서만 쌓인다
                await self.ws.send(json.dumps(self._dbg_pending.pop(0), ensure_ascii=False))
            await self.ws.send(msg)
        except ConnectionClosed:
            self.closed = True

    async def _send_json_safe(self, obj: dict) -> None:
        """마무리용: 송신 오류(ConnectionClosed 아님)도 삼킨다. turn.end를 반드시 시도하기 위해."""
        try:
            await self._send_json(obj)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("send failed (%s): %r", self.info.session_id if self.info else "", e)

    async def send_audio(self, pcm: bytes, pace: bool = False) -> None:
        """100ms 덩어리로 보낸다. 끼어들기가 났으면 더 보내지 않는다. pace=True(응답 본문)면 재생 시계보다
        pace_lead_ms 넘게 앞서지 않게 기다린다(끊을 수 있게)."""
        n = self.s.out_chunk_bytes
        for i in range(0, len(pcm), n):
            if self._barged:
                return
            if pace and self.barge_in and self._play_end_ms - self._now_ms() > _opt(self.s, "pace_lead_ms"):
                await self._pace()
                if self._barged:
                    return
            chunk = pcm[i:i + n]
            self._note_play(len(chunk))
            await self._send(chunk)

    # ── 재생 시계(AG-T19) ────────────────────────────
    def _now_ms(self) -> float:
        """고객 쪽 재생 시계(ms). 받은 프레임 수 × 20ms와 첫 프레임 이후 벽시계 중 큰 값."""
        frames = self.frame_idx * FRAME_MS
        if self._wall0 is None:
            return float(frames)
        return max(float(frames), (time.perf_counter() - self._wall0) * 1000.0)

    def _note_play(self, nbytes: int) -> None:
        now = self._now_ms()
        start = max(self._play_end_ms, now)
        self._play_end_ms = start + nbytes / 32.0       # 16kHz s16le: 1ms = 32바이트
        if not self.responding:
            return
        if self._first_audio_ms is None:
            self._first_audio_ms = start
        idx = len(self._turn_spoken) - 1
        if idx >= 0:
            seg = self._seg_play.get(idx)
            if seg is None:
                self._seg_play[idx] = [start, self._play_end_ms]
            else:
                seg[1] = self._play_end_ms

    async def _pace(self) -> None:
        lead = _opt(self.s, "pace_lead_ms")
        cap_ms = float(_opt(self.s, "pace_cap_s")) * 1000.0
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        while not self.closed and not self._barged:
            now = self._now_ms()
            if self._play_end_ms - now <= lead or now - self._commit_ms >= cap_ms:
                break
            self._frame_ev.clear()
            try:
                await asyncio.wait_for(self._frame_ev.wait(), 0.05)
            except TimeoutError:
                pass
        waited = loop.time() - t0
        cm = self._safety
        if waited > 0.001 and cm is not None and self._safety_cap is not None:
            try:
                when = cm.when()
                if when is not None and not cm.expired():
                    cm.reschedule(min(when + waited, self._safety_cap))   # 기다린 만큼 안전 상한을 미룬다
            except RuntimeError:
                pass

    async def _send_json(self, obj: dict) -> None:
        await self._send(json.dumps(obj, ensure_ascii=False))

    # ── 메인 루프 ────────────────────────────────────
    async def run(self) -> None:
        try:
            start = await self._recv_start()
            if start is None:
                return
            self.info = SessionInfo.from_start(start)
            if start.get("debug") is True:
                self.debug = True
                self.tlog = self._new_tlog(self.info.session_id, t0=self.tlog.t0)
            if self.info.caller is None and start.get("customer_profile"):
                self.info.caller = start["customer_profile"]
            self.tlog.session_id = self.info.session_id
            for key in ("barge_in", "opener_adaptive"):     # 데모·실험용 세션별 덮어쓰기
                if isinstance(start.get(key), bool):
                    setattr(self, key, start[key])
            if self.opener_adaptive:
                self._warm_task = asyncio.create_task(self._warm_openers())
            self.expect_short = _expect_short_from_history(self.info.history)
            self._set_detector_context()
            self.opener = select_opener(self.info, self.s)
            self.system = self._system_for(self.opener)
            self._tools_task = asyncio.create_task(self.d.tools.list_tools(self.info.tools_url))
            self._start_preload()
            self._stt_open = asyncio.create_task(self.d.stt.open_stream(self.s.stt_context))
            try:
                async for msg in self.ws:
                    if isinstance(msg, (bytes, bytearray)):
                        await self._on_frame(bytes(msg))
            except ConnectionClosed:
                pass
            self.closed = True
            if self._response_task is not None:
                await self._response_task
        finally:
            await self._cleanup()

    async def _recv_start(self) -> dict | None:
        while True:
            try:
                raw = await self.ws.recv()
            except (ConnectionClosed, StopAsyncIteration):
                return None
            if isinstance(raw, (bytes, bytearray)):
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if msg.get("type") == "session.start":
                return msg

    async def _cleanup(self) -> None:
        self._cancel_prepared()
        for t in (self._response_task, self._work_task, self._tools_task, self._warm_task, self._handoff_task,
                  self._preload_task):
            if t is not None and not t.done():
                t.cancel()
        for t in (self._response_task, self._work_task):    # 취소가 끝날 때까지 기다린 뒤 턴 상태 정리
            if t is not None and not t.done():
                try:
                    await asyncio.wait({t}, timeout=1.0)
                except Exception:  # noqa: BLE001
                    pass
        self._replay = None
        self._barged = False
        self._bi_frames = []
        self.responding = False
        stt = self.stt
        if stt is None and self._stt_open is not None:
            if self._stt_open.done() and not self._stt_open.cancelled() and self._stt_open.exception() is None:
                stt = self._stt_open.result()
            else:
                self._stt_open.cancel()
        if stt is not None:
            try:
                await stt.close()
            except Exception:  # noqa: BLE001
                pass

    async def _ensure_stt(self) -> STTStream | None:
        if self.stt is None and self._stt_open is None and self._stt_lazy_at is not None:
            self._stt_lazy_at -= 1
            if self._stt_lazy_at > 0:
                return None                             # turn.end 직후: 연결이 이어지는지 본다
            self._stt_lazy_at = None
            self._stt_open = asyncio.create_task(self.d.stt.open_stream(self.s.stt_context))
            self._stt_backfill = True
        if self.stt is None and self._stt_open is not None:
            try:
                self.stt = await self._stt_open
            except Exception as e:  # noqa: BLE001
                log.error("stt open failed: %r", e)
                self._stt_open = None
            if self.stt is not None and self._stt_backfill:
                self._stt_backfill = False
                pcm = b"".join(self._ring)              # 여는 동안 들어온 소리를 먼저 보낸다
                if pcm:
                    try:
                        await self.stt.send_audio(pcm)
                    except Exception as e:  # noqa: BLE001
                        log.warning("stt backfill failed: %r", e)
        return self.stt

    # ── 통화 시작 때 주문 미리 조회(AG-T34) ─────────────
    def _start_preload(self) -> None:
        """발신자 전화번호가 있고 이전 대화에 주문 조회 결과가 없으면, 고객이 말하는 동안 그 번호로 주문을 조회해 둔다.
        조회만 한다(접수 함수는 미리 부르지 않는다). 측정기 중계는 응답을 약 1.2초 늦추지만 고객 말이 끝나기 전에 끝난다."""
        info = self.info
        if info is None or not getattr(self.s, "preload_orders", False):
            return
        phone = re.sub(r"\D", "", str((info.caller or {}).get("phone") or ""))
        if not valid_phone(phone):
            return
        if any(isinstance(h, dict) and h.get("role") == "tool_result" and h.get("name") == "get_order_info"
               for h in info.history or []):
            return
        self._preload_task = asyncio.create_task(self._preload(phone))

    async def _preload(self, phone: str) -> None:
        info = self.info
        assert info is not None
        args = {"buyer_cellphone": phone}
        try:
            res = await self.d.tools.call(info.tools_url, info.session_id, "get_order_info", args)
        except Exception as e:  # noqa: BLE001
            log.warning("preload failed: %r", e)
            return
        if not isinstance(res, dict) or res.get("status") == "error":
            return
        self._preload_results = [{"name": "get_order_info", "arguments": args, "result": res}]
        self._preload_text = json.dumps(res, ensure_ascii=False)[:PRELOAD_MAX_CHARS]
        self._systems.clear()                           # 프롬프트 가변부에 조회 결과를 넣어 다시 만든다
        self.tlog.event("preload", status=res.get("status"))
        pc = self._precall
        if pc is not None and not pc.preload and not self.responding:
            text = pc.text                              # 결과 없이 시작한 선호출은 버리고 다시 건다
            pc.cancel()
            self._precall = None
            self._start_precall(text, "preload")

    async def _await_preload(self) -> None:
        t = self._preload_task
        if t is None or t.done():
            return
        try:
            await asyncio.wait_for(asyncio.shield(t), float(getattr(self.s, "preload_wait_s", 1.5) or 0))
        except (Exception, TimeoutError):  # noqa: BLE001
            pass

    def _system_for(self, opener: str) -> str:
        """첫 마디마다 시스템 프롬프트("이미 '{opener}'라고 말했다")를 만든다(세션 안에서 캐시)."""
        sys_ = self._systems.get(opener)
        if sys_ is None:
            assert self.info is not None
            sys_ = build_system(self.info, opener, preload=self._preload_text, prefix=self.d.manual)
            self._systems[opener] = sys_
        return sys_

    # ── 대화 맥락(AG-T25: Jev 인계 판정·말 끝 판단) ───────
    def _context_history(self) -> list[dict]:
        """이전 대화(session.start history) + 이번 통화의 고객·상담원 말({"role","text"})."""
        return list((self.info.history if self.info else None) or []) + list(self.live_history)

    def _set_detector_context(self) -> None:
        """JevTurnDetector처럼 set_context가 있는 결정기에만 맥락을 넣는다(표 결정기는 그대로)."""
        fn = getattr(self.detector, "set_context", None)
        if fn is None or self.info is None:
            return
        try:
            fn(self._context_history(), caller_known=bool(self.info.caller))
        except Exception as e:  # noqa: BLE001
            log.warning("detector set_context failed: %r", e)

    def _fire_handoff(self, text: str) -> None:
        """Jev 인계 판정을 뒤에서 시작한다(스위치·판정기·텍스트가 있을 때만, 같은 텍스트면 다시 쏘지 않는다)."""
        text = (text or "").strip()
        if self.d.handoff is None or self.info is None or not text or not handoff_enabled():
            return
        k = self._key(text)
        if k == self._handoff_key and self._handoff_task is not None:
            return
        if self._handoff_task is not None and not self._handoff_task.done():
            self._handoff_task.cancel()
        self._handoff_key = k
        t = asyncio.create_task(self.d.handoff.judge(text, self._context_history(), self.failures,
                                                     bool(self.info.caller)))
        t.add_done_callback(lambda f: f.cancelled() or f.exception())   # 실패는 조용히(기존 동작)
        self._handoff_task = t

    def _known_handoff(self) -> Any:
        """이미 끝난 판정 결과(없거나 진행 중·실패면 None)."""
        t = self._handoff_task
        if t is None or not t.done() or t.cancelled() or t.exception() is not None:
            return None
        return t.result()

    async def _await_handoff(self, text: str) -> Any:
        """최종 텍스트로 판정 결과를 받는다(다르면 다시 쏜다). jev_handoff_wait_s 넘으면 None = 기존 동작."""
        self._fire_handoff(text)
        t = self._handoff_task
        if t is None:
            return None
        try:
            return await asyncio.wait_for(asyncio.shield(t), float(getattr(self.s, "jev_handoff_wait_s", 0.3)))
        except asyncio.CancelledError:
            cur = asyncio.current_task()
            if cur is not None and cur.cancelling():
                raise                                   # 바깥 취소(바지인·세션 종료)
            return None                                 # 판정 작업만 취소됨
        except (Exception, TimeoutError):  # noqa: BLE001
            return None

    async def _warm_openers(self) -> None:
        """AG-T20 첫 마디 후보 중 캐시에 없는 것을 뒤에서 합성해 둔다(서버 예열 목록에 없을 수 있다)."""
        missing = [p for p in opener_phrases(self.s) if self.d.cache.get(p) is None]
        if not missing:
            return
        try:
            await self.d.cache.warm(missing, self.d.tts)
        except Exception as e:  # noqa: BLE001
            log.warning("opener warm failed: %r", e)

    def _opener_candidates(self) -> dict[str, tuple[str, ...]]:
        return {k: opener_candidates(k, self.s) for k in OPENER_KINDS}

    def _cached(self, text: str) -> bool:
        return bool(text) and self.d.cache.get(text) is not None

    def _plan_opener(self, text: str) -> str:
        """선호출 프롬프트에 넣을 첫 마디(답이 준비 안 됐을 때 쓸 것). 없으면 ""."""
        c = choose_opener(opener_kind(text, self.s), answer_ready=False, recent=self._recent_openers,
                          candidates=self._opener_candidates(), is_cached=self._cached)
        return c.text or ""

    def _opener_fixed(self) -> bool:
        name = ((self.info.caller or {}).get("name") or "").strip() if self.info else ""
        return bool(self.s.opener_use_name and name)

    def _opener_for(self, text: str) -> str:
        """텍스트 기반 첫 마디. 캐시에 없는 문구면 세션 기본 첫 마디(확정 즉시 내보낼 수 있어야 한다)."""
        base = self.opener
        if not self.s.opener_by_text or self._opener_fixed():
            return base
        cand = select_opener_for_text(text, self.s, default=base)
        if cand != base and self.d.cache.get(cand) is None:
            return base
        return cand

    def _tools(self) -> list[dict] | None:
        t = self._tools_task
        if t is not None and t.done() and not t.cancelled() and t.exception() is None:
            return t.result()
        return None

    # ── 프레임 처리 ──────────────────────────────────
    async def _on_frame(self, frame: bytes) -> None:
        self.frame_idx += 1
        if self._wall0 is None:
            self._wall0 = time.perf_counter() - FRAME_MS / 1000.0
        self._frame_ev.set()
        if self._replay is not None and not self.responding:
            frames, self._replay = self._replay, None   # 끼어든 말(AG-T19)을 새 턴으로 먼저 처리
            for f in frames:
                if self.responding:
                    break
                await self._process_frame(f)
        if self.responding:
            if self.barge_in:
                await self._watch_barge_in(frame)
            return                                      # 응답 중 프레임은 STT에 넣지 않는다(drain)
        await self._process_frame(frame)

    # ── 바지인(AG-T19) ───────────────────────────────
    async def _watch_barge_in(self, frame: bytes) -> None:
        """응답 중 프레임: 끼어들기 감지. 이미 끼어들었으면 다음 턴에 다시 처리할 프레임으로 모은다."""
        if self._barged:
            self._bi_frames.append(frame)
            return
        self._bi_buf.append(frame)
        p = self.vad.process(frame)
        if p >= self.s.vad_threshold:
            if self._bi_run_start is None:
                self._bi_run_start = self.frame_idx
                self._bi_onset_ms = self._now_ms() - FRAME_MS
                self._bi_voiced = 0
                fa = self._first_audio_ms
                # 첫 소리 전이나 직후(arm 전)에 시작된 말은 잘못 끊은 발화의 연속으로 보고 넘긴다
                self._bi_ignored = fa is None or self._bi_onset_ms - fa < _opt(self.s, "barge_in_arm_ms")
            self._bi_voiced += FRAME_MS
            self._bi_gap = 0
            if not self._bi_ignored and self._bi_voiced >= _opt(self.s, "barge_in_ms"):
                await self._trigger_barge_in()
        elif self._bi_run_start is not None:
            self._bi_gap += FRAME_MS
            if self._bi_gap > _opt(self.s, "barge_in_gap_ms"):
                self._bi_run_start = None

    async def _trigger_barge_in(self) -> None:
        self._barged = True
        now = self._now_ms()
        assert self._bi_run_start is not None
        pre = int(_opt(self.s, "barge_in_preroll_ms")) // FRAME_MS
        n = self.frame_idx - self._bi_run_start + 1 + pre
        self._bi_frames = list(self._bi_buf)[-n:]
        # 데모(디버그)는 신호를 받으면 재생을 멈춘다 → 끼어든 시점까지. 그 밖의 클라이언트는 이미 보낸 만큼 재생된다.
        stop_ms = now if self.debug else self._play_end_ms
        fa = self._first_audio_ms or now
        self._bi_info = {"onset_ms": self._bi_onset_ms, "trigger_ms": now, "stop_ms": stop_ms}
        self.tlog.mark("barge_in")
        self.tlog.set("barge_in", True)
        self.tlog.set("barge_in_heard_ms", round(self._bi_onset_ms - fa))
        self.tlog.set("barge_in_reaction_ms", round(now - self._bi_onset_ms))
        await self._debug({"type": "debug.barge_in", "late": False, "voiced_ms": self._bi_voiced,
                           "heard_ms": round(self._bi_onset_ms - fa)})
        if self._work_task is not None and not self._work_task.done():
            self._work_task.cancel()                    # LLM·TTS·필러·선합성 취소(응답기가 정리)
        if self._precall is not None:
            self._precall.cancel()

    async def _check_late_barge(self) -> None:
        """turn.end 뒤에도 직전 응답이 재생되는 동안 고객이 말하면: 직전 상담원 말을 끊긴 것으로 고친다."""
        self._late_barge_done = True
        prev = self._prev_play
        if prev is None or prev.get("first") is None:
            return
        now = self._now_ms()
        if now >= prev["end"] or self._speech_onset_ms - prev["first"] < _opt(self.s, "barge_in_arm_ms"):
            return
        self._prev_play = None
        stop_ms = now if self.debug else prev["end"]
        heard = heard_text(prev["spoken"], prev["segs"], stop_ms)
        if self.conversation and self.conversation[-1].role == "assistant":
            self.conversation[-1] = Message("assistant", interrupted_context(heard))
        self.expect_short = False
        self.tlog.set("late_barge_in", True)
        self.tlog.event("late_barge_in", heard=heard)
        await self._debug({"type": "debug.barge_in", "late": True, "voiced_ms": self.speech_ms,
                           "heard_ms": round(self._speech_onset_ms - prev["first"])})

    async def _process_frame(self, frame: bytes) -> None:
        stt = await self._ensure_stt()
        if stt is not None:
            try:
                await stt.send_audio(frame)
            except Exception as e:  # noqa: BLE001
                log.warning("stt send failed: %r", e)
        self._ring.append(frame)
        p = self.vad.process(frame)
        ms = len(frame) * 1000 // (2 * 16000) or FRAME_MS
        self._turn_ms += ms
        self._track_text(stt)
        voiced = p >= self.s.vad_threshold
        gate = float(getattr(self.s, "speaker_gate_db", 0.0) or 0.0)
        if voiced and gate > 0:
            db = _frame_db(frame)
            level = self._speaker_level()
            if level is not None and db < level - gate:
                voiced = False                          # 손님 목소리보다 한참 작은 소리(주변 말소리·TV)는 침묵으로
                self.gated_frames += 1
            elif p >= 0.8:
                self._voice_db.append(db)
        resume_min = int(getattr(self.s, "resume_min_ms", 0) or 0)
        if voiced:
            if self.silence_ms > 0 and resume_min > 0 and self.speech_ms >= self.s.min_speech_ms:
                self._resume_ms += ms
                if self._resume_ms < resume_min:
                    return                              # 아직 짧은 소리: 침묵을 깨지도 확정하지도 않는다
                ms, self._resume_ms = self._resume_ms, 0
            if self.speech_ms == 0:
                self._speech_onset_ms = self._now_ms() - FRAME_MS
                self.tlog.mark("first_speech")
                self.tlog.mark("first_speech_frame", self.frame_idx)
                await self._debug({"type": "debug.turn", "event": "speech_start", "silence_ms": 0})
            elif self.silence_ms > 0 and self.debug:
                await self._debug({"type": "debug.turn", "event": "resume", "silence_ms": self.silence_ms,
                                   "prepared": self.prepared})
            if self.silence_ms > 0 and self.prepared and not self._stall_reason:
                self._cancel_prepared()                 # 확정 전 음성 재개 → 선호출 취소
                self.precall_cancels += 1
                self.tlog.event("precall_cancelled")
            self.speech_ms += ms
            self.silence_ms = 0
            if (self.barge_in and not self._late_barge_done and self._prev_play is not None
                    and self.speech_ms >= _opt(self.s, "barge_in_ms")):
                await self._check_late_barge()
            await self._check_text_stall(stt)
            return
        if self._resume_ms:                             # 짧은 소리(잡음)는 침묵으로 센다
            self.silence_ms += self._resume_ms
            self._resume_ms = 0
        if self.speech_ms < self.s.min_speech_ms:
            if self.speech_ms and self.silence_ms + ms >= self.s.precall_silence_ms:
                self.speech_ms = 0                      # 짧은 잡음은 잊는다
                self.silence_ms = 0
            elif self.speech_ms:
                self.silence_ms += ms
            return
        if self.silence_ms == 0:
            self.tlog.mark("silence_start", overwrite=True)
            await self._debug({"type": "debug.turn", "event": "silence", "silence_ms": 0,
                               "speech_ms": self.speech_ms})
        self.silence_ms += ms
        if self._stall_reason:                          # 글 멈춤 확정을 기다리는 중이면 그쪽을 먼저 본다
            await self._check_text_stall(stt)
            if self.responding:
                return
        if not self.prepared and self.silence_ms >= self.s.precall_silence_ms:
            self._prepare(stt)
        self._check_finalize()
        self._collect_smart()
        # 텍스트가 비어도(짧은 발화는 부분 결과가 늦다) 결정기 규칙 그대로. finalize가 끝났으면 그 텍스트.
        early = self._early_final if self.s.precall_refresh else None
        text = early or (stt.latest_text() if stt is not None else "")
        sig = TurnSignals(silence_ms=self.silence_ms, speech_ms=self.speech_ms, text=text,
                          smart_turn_p=self.smart_p, expect_short=self.expect_short)
        dec = self.detector.decide(sig)
        if dec.end:
            await self._commit(dec.reason, text)

    # ── AG-T55 소음 속 말 끝 ─────────────────────────
    def _speaker_level(self) -> float | None:
        """손님 목소리 크기(dBFS): 확실한 음성 프레임 에너지의 75분위. 음성이 speaker_gate_min_ms보다 적으면 None."""
        need = max(1, int(getattr(self.s, "speaker_gate_min_ms", 600) or 0) // FRAME_MS)
        if len(self._voice_db) < need:
            return None
        v = sorted(self._voice_db)
        return v[min(len(v) - 1, int(len(v) * 0.75))]

    def _track_text(self, stt: STTStream | None) -> None:
        if stt is None:
            return
        text = stt.latest_text()
        if text != self._text_last:
            self._text_last = text
            self._text_changed_ms = self._turn_ms

    async def _check_text_stall(self, stt: STTStream | None) -> None:
        """VAD가 음성이라고 하는 동안 받아쓰기 글이 멈춰 있으면(소음) 말 끝으로 본다.
        text_stall_end_ms부터는 멈춘 시간을 침묵처럼 결정기에 넘기고, text_stall_cap_ms가 되면 규칙과 상관없이 확정한다.
        확정 전에 받아쓰기 끝맺음(finalize)을 요청하고 최대 300ms 기다린다. 글이 없으면(아직 말하지 않음) 하지 않는다."""
        s = self.s
        end_ms = int(getattr(s, "text_stall_end_ms", 0) or 0)
        cap_ms = int(getattr(s, "text_stall_cap_ms", 0) or 0)
        if stt is None or self.responding or not (end_ms or cap_ms):
            return
        if self._stall_reason:
            fin = self._finalize_task
            if self._turn_ms >= self._stall_commit_ms or (fin is not None and fin.done()):
                reason, self._stall_reason = self._stall_reason, ""
                text = (self._early_final if s.precall_refresh else None) or stt.latest_text()
                self.tlog.event("text_stall_commit", reason=reason, speech_ms=self.speech_ms)
                await self._commit(reason, text)
            return
        text = self._text_last
        if not text or self.speech_ms < s.min_speech_ms:
            return
        stalled = self._turn_ms - self._text_changed_ms
        reason = ""
        if cap_ms and stalled >= cap_ms:
            reason = f"text_stall_cap({stalled}ms)"
        elif end_ms and stalled >= end_ms:
            sig = TurnSignals(silence_ms=stalled - end_ms + 200, speech_ms=self.speech_ms, text=text,
                              smart_turn_p=None, expect_short=self.expect_short)
            dec = self.detector.decide(sig)
            if dec.end:
                reason = f"text_stall({stalled}ms,{dec.reason})"
        if not reason:
            return
        self._stall_reason = reason
        self._stall_commit_ms = self._turn_ms + 300
        await self._debug({"type": "debug.turn", "event": "text_stall", "stalled_ms": stalled, "reason": reason})
        if not self.prepared:
            self._prepare(stt)

    def _prepare(self, stt: STTStream | None) -> None:
        self.prepared = True
        if stt is not None:
            self.tlog.mark("finalize_request", overwrite=True)
            self._finalize_task = asyncio.create_task(stt.finalize(self.s.finalize_timeout_s))
        if self.d.smart_turn is not None:
            pcm = b"".join(self._ring)
            self._smart_task = asyncio.create_task(asyncio.to_thread(self.d.smart_turn.predict, pcm))
        self._start_precall(stt.latest_text() if stt is not None else "", "partial")

    def _key(self, text: str) -> str:
        return match_key(text) if self.s.precall_refresh else (text or "").strip()

    def _check_finalize(self) -> None:
        """확정 전에 finalize가 끝났으면 그 텍스트를 기억하고, 선호출 텍스트와 다르면 선호출을 다시 건다."""
        t = self._finalize_task
        if t is None or self._final_seen or not t.done():
            return
        self._final_seen = True
        if t.cancelled() or t.exception() is not None:
            return
        self._early_final = fix_brand((t.result() or "").strip())
        self.tlog.event("finalize_early", text=self._early_final)
        if self._early_final and self.s.precall_refresh:
            self._start_precall(self._early_final, "finalize")

    def _start_precall(self, text: str, source: str) -> None:
        text = fix_brand((text or "").strip())
        tools = self._tools()
        if not text or tools is None:
            return
        key = self._key(text)
        if self._precall is not None:
            if self._precall.key == key:
                return
            self._precall.cancel()
            self.tlog.event("precall_replaced", old=self._precall.text)
        opener = self._plan_opener(text) if self.opener_adaptive else self._opener_for(text)
        self._fire_handoff(text)                        # AG-T25: 인계 판정도 같은 순간에
        r = self._responder(tools)
        pf = r.prefetch(self._system_for(opener), self.conversation + [Message("user", text)])
        pc = _Precall(text=text, key=key, opener=opener, prefetch=pf, preload=bool(self._preload_results))
        if self.s.precall_refresh:
            pc.watcher = asyncio.create_task(self._presynth_first(pc))
        self._precall = pc
        self.precall_count += 1
        self.tlog.event("precall", text=text, source=source, opener=opener)

    async def _presynth_first(self, pc: _Precall) -> None:
        """선호출 스트림에서 첫 문장을 응답기와 같은 규칙(SentenceSplitter)으로 찾아 미리 합성한다(송신 안 함)."""
        sp = SentenceSplitter(self.s.min_sentence_chars)
        first = None
        try:
            async for ev in pc.prefetch.events():
                if ev.type == "text" and ev.text:
                    out = sp.feed(ev.text)
                    if out:
                        first = out[0]
                        break
                elif ev.type == "done":
                    break
            if first is None:
                rest = sp.flush()
                first = rest[0] if rest else None
        except Exception:  # noqa: BLE001
            return
        if not first or pc.closed or (self.s.max_response_chars and _nchars(first) > self.s.max_response_chars):
            return
        pc.presynth = _PreSynth(first, self.d.tts)
        self.tlog.event("presynth", text=first)

    def _collect_smart(self) -> None:
        t = self._smart_task
        if t is not None and self.smart_p is None and t.done():
            if not t.cancelled() and t.exception() is None:
                self.smart_p = float(t.result())
                self.tlog.mark("smart_turn", overwrite=True)
                self.tlog.set("smart_turn_p", self.smart_p)

    def _responder(self, tools: list[dict], tts: Any = None, settings: Settings | None = None,
                   user_text: str | None = None) -> Responder:
        info = self.info
        assert info is not None

        async def call_tool(name: str, args: dict) -> dict:
            return await self.d.tools.call(info.tools_url, info.session_id, name, args)

        guard = None
        if user_text is not None:                       # 실제 응답용: 함수 실행 전 검사 문맥
            guard = GuardContext(tools=tools, user_text=user_text, history=self._context_history(),
                                 caller=info.caller, results=list(self._preload_results),
                                 spoken=[h.get("text") or "" for h in self.live_history if h.get("role") == "customer"])
        return Responder(self.d.llm, tts or self.d.tts, call_tool, tools=tools, settings=settings or self.s,
                         cache=self.d.cache, now=info.now, guard=guard)

    # ── 확정 → 응답 ──────────────────────────────────
    async def _commit(self, reason: str, text: str) -> None:
        self.responding = True
        self.tlog.mark("commit")
        self.tlog.mark("commit_frame", self.frame_idx)
        self.tlog.set("decision_reason", reason)
        self.tlog.set("speech_ms", self.speech_ms)
        self.tlog.set("silence_ms", self.silence_ms)
        self.tlog.set("precall_cancels", self.precall_cancels)
        self._check_finalize()
        self._commit_ms = self._now_ms()
        self._commit_text = (self._early_final if self.s.precall_refresh else None) or text
        self._prev_play = None
        spoken: list[str] = []
        self._turn_spoken = spoken
        if self.opener_adaptive:                        # AG-T20: 첫 마디는 응답 작업에서 답 준비 상태를 보고 정한다
            self.turn_opener = self._precall.opener if self._precall is not None else ""
            await self._debug({"type": "debug.turn", "event": "decided", "silence_ms": self.silence_ms,
                               "speech_ms": self.speech_ms, "reason": reason, "text": text,
                               "opener": self.turn_opener})
            self._response_task = asyncio.create_task(self._respond_turn(spoken, opener_sent=True))
            return
        if self._precall is not None:                   # 선호출 프롬프트와 같은 첫 마디
            self.turn_opener = self._precall.opener
        else:
            self.turn_opener = self._opener_for(self._commit_text)
        self.tlog.set("opener", self.turn_opener)
        opener_pcm = self.d.cache.get(self.turn_opener)
        if opener_pcm is not None:
            self.tlog.mark("opener_start")
            spoken.append(self.turn_opener)
            await self.send_audio(opener_pcm)           # 확정 즉시(같은 프레임 처리 안)
            self.tlog.mark("opener_sent")
            self._opener_ms = len(opener_pcm) // 32
        await self._debug({"type": "debug.turn", "event": "decided", "silence_ms": self.silence_ms,
                           "speech_ms": self.speech_ms, "reason": reason, "text": text,
                           "opener": self.turn_opener})
        self._response_task = asyncio.create_task(
            self._respond_turn(spoken, opener_sent=opener_pcm is not None))

    async def _final_text(self) -> str:
        stt = self.stt
        if self._finalize_task is None and stt is not None:
            self.tlog.mark("finalize_request", overwrite=True)
            self._finalize_task = asyncio.create_task(stt.finalize(self.s.finalize_timeout_s))
        text = ""
        if self._finalize_task is not None:
            try:
                text = await asyncio.wait_for(self._finalize_task, self.s.finalize_timeout_s + 0.5)
            except Exception as e:  # noqa: BLE001
                log.warning("finalize failed: %r", e)
                text = stt.latest_text() if stt is not None else ""
        elif stt is not None:
            text = stt.latest_text()
        if not (text or "").strip() and stt is not None:
            text = await self._late_text(stt)
        self.tlog.mark("finalize_done")
        return fix_brand((text or "").strip())

    async def _late_text(self, stt: STTStream) -> str:
        """받아쓰기가 빈 채 끝났는데 말소리는 충분했을 때: 끝맺음을 한 번 더 요청하고 empty_stt_wait_s 동안 글을 기다린다.

        2026-11-21 공식 측정 P0029-01·P0120-01: 말소리가 있었는데 부분 결과가 한 번도 안 오고 끝맺음이 시간 초과로 끝나
        곧바로 다시 물었다. 첫 마디는 확정 때 이미 나갔으므로 여기서 기다려도 응답 시작 시각은 그대로다.
        글이 끝내 없으면 ""(지금처럼 다시 묻는다). 더하는 기다림은 최대 empty_stt_wait_s + 0.25초."""
        wait = float(getattr(self.s, "empty_stt_wait_s", 0.0) or 0.0)
        if wait <= 0 or self.speech_ms < getattr(self.s, "empty_stt_min_speech_ms", 0):
            return ""
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        text = ""
        try:
            text = (await asyncio.wait_for(stt.finalize(wait), wait + 0.25) or "").strip()
        except Exception as e:  # noqa: BLE001
            log.warning("finalize retry failed: %r", e)
        seen, seen_t = "", t0
        while not text and loop.time() - t0 < wait:     # 끝맺음이 빈 채 일찍 끝났으면 부분 결과라도 기다려 본다
            await asyncio.sleep(0.05)
            cur = (stt.latest_text() or "").strip()
            if cur != seen:
                seen, seen_t = cur, loop.time()
            elif cur and loop.time() - seen_t >= 0.2:   # 0.2초 동안 더 늘지 않았다
                text = cur
        text = text or seen
        waited_ms = round((loop.time() - t0) * 1000)
        self.tlog.event("stt_empty_retry", waited_ms=waited_ms, chars=len(text), speech_ms=self.speech_ms)
        log.warning("stt text empty after finalize (%s): speech=%dms, waited %dms more, got %d chars",
                    self.info.session_id if self.info else "", self.speech_ms, waited_ms, len(text))
        return text

    async def _play_cached(self, text: str, spoken: list[str], pace: bool = False) -> bytes | None:
        pcm = self.d.cache.get(text)
        if pcm is None:
            try:
                pcm = await asyncio.wait_for(self.d.cache.get_or_synth(text, self.d.tts), 3.0)
            except Exception:  # noqa: BLE001
                pcm = None
        spoken.append(text)
        if pcm:
            await self.send_audio(pcm, pace=pace)
        return pcm

    # ── AG-T20 첫 마디(opener_adaptive) ──────────────
    def _final_matches(self, pc: _Precall) -> bool:
        t = self._finalize_task
        if t is None or not t.done() or t.cancelled() or t.exception() is not None:
            return False
        return self._key((t.result() or "").strip()) == pc.key

    @staticmethod
    def _tool_expected(pc: _Precall) -> bool:
        for ev in pc.prefetch.buf:
            if ev.type == "text" and (ev.text or "").strip():
                return False
            if ev.type == "tool_call":
                return True
        return False

    def _answer_ready(self, pc: _Precall) -> bool:
        pre = pc.presynth
        return (pre is not None and bool(pre.chunks) and pre.error is None and not pre.task.cancelled()
                and self._final_matches(pc))

    async def _answer_state(self, pc: _Precall | None) -> tuple[bool, bool]:
        """(답 준비됨, 함수 호출 예상). 선합성이 진행 중이고 최종 텍스트가 맞으면 opener_grace_ms까지 기다려 본다."""
        if pc is None:
            return False, False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _opt(self.s, "opener_grace_ms") / 1000.0
        while True:
            if self._answer_ready(pc):
                return True, False
            if self._tool_expected(pc):
                return False, True
            pre = pc.presynth
            pending = pre is not None and not pre.done and self._final_matches(pc)
            if not pending or loop.time() >= deadline:
                return False, False
            await asyncio.sleep(0.01)

    async def _adaptive_opener(self, spoken: list[str]) -> None:
        pc = self._precall
        kind = opener_kind(self._commit_text or (pc.text if pc is not None else ""), self.s)
        ready, tool = await self._answer_state(pc)
        if ready and route(self._known_handoff(), self.info.now if self.info else "") is not None:
            ready = False                               # 인계 턴: 힌트 없는 선합성 답은 버려지므로 첫 마디를 쓴다
        choice = choose_opener(kind, answer_ready=ready, tool_expected=tool, recent=self._recent_openers,
                               candidates=self._opener_candidates(), is_cached=self._cached,
                               preferred=pc.opener if pc is not None else None)
        self.turn_opener = choice.text or ""
        self.tlog.set("opener", self.turn_opener)
        self.tlog.set("opener_kind", choice.kind)
        self.tlog.set("opener_reason", choice.reason)
        if not choice.text:
            return
        pcm = self.d.cache.get(choice.text)
        if not pcm:
            self.turn_opener = ""
            return
        self.tlog.mark("opener_start")
        spoken.append(choice.text)
        await self.send_audio(pcm)
        self.tlog.mark("opener_sent")
        self._opener_ms = len(pcm) // 32
        self._recent_openers = (self._recent_openers + [choice.text])[-4:]

    def _adaptive_settings(self) -> Settings:
        """툴 필러를 직전 필러·이번 첫 마디와 겹치지 않게 고른다."""
        s = self.s
        filler = choose_filler((*s.fillers, s.opener_lookup_filler), opener=self.turn_opener,
                               last=self._last_filler, is_cached=self._cached)
        self._turn_filler = filler
        return dataclasses.replace(s, fillers=(filler,) if filler else ())

    # ── 응답 ─────────────────────────────────────────
    async def _respond_turn(self, spoken: list[str], opener_sent: bool) -> None:
        st: dict[str, Any] = {"user_text": "", "failed": False}
        self._work_task = asyncio.create_task(self._respond_work(spoken, opener_sent, st))
        try:
            await self._work_task
        except asyncio.CancelledError:
            cur = asyncio.current_task()
            if not self._barged or (cur is not None and cur.cancelling()):
                raise                                   # 세션 종료 등 바깥 취소
        except Exception as e:  # noqa: BLE001  # 사과 송신까지 실패한 경우 등
            log.warning("response work failed (%s): %r", self.info.session_id if self.info else "", e)
            st["failed"] = True
        self._turn_end_sent = False
        try:
            await self._finish_turn(spoken, st)
        except Exception as e:  # noqa: BLE001  # 마무리 중 오류여도 turn.end와 다음 턴 준비는 보장
            log.warning("finish turn failed (%s): %r", self.info.session_id if self.info else "", e)
            if not self._turn_end_sent:
                await self._send_json_safe({"type": "turn.end"})
            try:
                await self._next_turn()
            except Exception as e2:  # noqa: BLE001
                log.warning("next turn failed: %r", e2)
                self._reset_turn()
                self.responding = False

    async def _finish_turn(self, spoken: list[str], st: dict[str, Any]) -> None:
        user_text = st["user_text"]
        failed = st["failed"]
        if self._barged:                                # AG-T19: 실제로 내보낸 만큼만
            if self._precall is not None:
                self._precall.cancel()
            if not user_text:
                user_text = self._fallback_user_text()
            agent_text = heard_text(spoken, self._seg_tuples(), self._bi_info["stop_ms"] if self._bi_info else 0)
        else:
            agent_text = " ".join(t for t in spoken if t)
        self._log_gap()
        await self._send_json({"type": "agent.transcript", "text": agent_text})
        await self._send_json({"type": "turn.end"})
        self._turn_end_sent = True
        self.tlog.mark("turn_end")
        self.tlog.set("agent_text", agent_text)
        self.tlog.set("failed", failed)
        opener_used = bool(self.turn_opener) and self.turn_opener in spoken
        self.tlog.set("opener_used", opener_used)
        self.turn_count += 1
        self.opener_turns += int(opener_used)
        if self._turn_filler and self._turn_filler in spoken:
            self._last_filler = self._turn_filler
        await self._debug(self._timing())
        if self._smart_task is not None and not self._smart_task.done():
            await asyncio.wait({self._smart_task}, timeout=0.2)   # 로그용(응답과 무관)
        self._collect_smart()
        self._write_log()
        if user_text:
            self.conversation.append(Message("user", user_text))
            self.live_history.append({"role": "customer", "text": user_text})
        self._remember_tools(st.get("tool_log") or [])
        if self._barged:
            if self._bi_info is None:                   # transcript를 보낸 뒤에 끼어든 경우
                self._bi_info = {"stop_ms": self._now_ms() if self.debug else self._play_end_ms}
            heard = heard_text(spoken, self._seg_tuples(), self._bi_info["stop_ms"])
            self.conversation.append(Message("assistant", interrupted_context(heard)))
            self.live_history.append({"role": "agent", "text": interrupted_context(heard)})
            self.expect_short = False
            self._prev_play = None
        else:
            self.conversation.append(Message("assistant", agent_text))
            self.live_history.append({"role": "agent", "text": agent_text})
            self.expect_short = agent_text.rstrip().endswith("?")
            self._prev_play = {"first": self._first_audio_ms, "end": self._play_end_ms,
                               "spoken": list(spoken), "segs": self._seg_tuples()}
        await self._next_turn()

    def _remember_tools(self, tool_log: list[dict]) -> None:
        """이번 턴에 실행한 함수 호출과 결과를 통화 기록에 남긴다. 다음 턴의 LLM과 검사가 다시 묻거나 조회하지 않게."""
        for e in tool_log:
            if e.get("blocked"):
                continue
            self._live_calls += 1
            tc = ToolCall(f"live{self._live_calls}", e["name"], e.get("arguments") or {})
            self.conversation.append(Message("assistant", "", tool_calls=[tc]))
            self.conversation.append(Message("tool", json.dumps(e.get("result"), ensure_ascii=False),
                                             tool_call_id=tc.id, name=tc.name))
            self.live_history.append({"role": "tool_call", "name": e["name"], "arguments": e.get("arguments") or {}})
            self.live_history.append({"role": "tool_result", "name": e["name"], "result": e.get("result")})

    def _seg_tuples(self) -> dict[int, tuple[float, float]]:
        return {i: (a, b) for i, (a, b) in self._seg_play.items()}

    def _fallback_user_text(self) -> str:
        t = self._finalize_task
        if t is not None and t.done() and not t.cancelled() and t.exception() is None:
            return (t.result() or "").strip()
        if self._early_final:
            return self._early_final
        return (self.stt.latest_text() if self.stt is not None else "").strip()

    async def _respond_work(self, spoken: list[str], opener_sent: bool, st: dict[str, Any]) -> None:
        first_audio = False

        async def send_response(pcm: bytes) -> None:
            nonlocal first_audio
            if not first_audio:
                first_audio = True
                self.tlog.mark("response_first_audio")
            await self.send_audio(pcm, pace=True)

        loop = asyncio.get_running_loop()
        try:
            async with asyncio.timeout(self.s.safety_timeout_s) as cm:
                self._safety = cm
                self._safety_cap = loop.time() + max(self.s.safety_timeout_s, _HARD_CAP_S)
                if self.opener_adaptive:
                    await self._adaptive_opener(spoken)
                elif not opener_sent:                   # 캐시에 없으면 실시간 합성
                    self.tlog.mark("opener_start")
                    pcm = await self._play_cached(self.turn_opener, spoken)
                    self.tlog.mark("opener_sent")
                    if pcm:
                        self._opener_ms = len(pcm) // 32
                user_text = await self._final_text()
                st["user_text"] = user_text
                self.tlog.set("user_text", user_text)
                await self._debug({"type": "debug.stt", "text": user_text})
                if not user_text:
                    if self._precall is not None:
                        self._precall.cancel()
                    self.failures += 1
                    await self._play_cached(self.s.reprompt, spoken, pace=True)
                else:
                    tools = self._tools()
                    if tools is None and self._tools_task is not None:
                        try:
                            tools = await asyncio.wait_for(asyncio.shield(self._tools_task),
                                                           self.s.tools_timeout_s)
                        except Exception:  # noqa: BLE001
                            tools = []
                    tools = tools or []
                    await self._await_preload()
                    pc = self._precall
                    if pc is not None and pc.preload != bool(self._preload_results):
                        pc.cancel()                     # 미리 조회 결과가 뒤늦게 온 경우
                        pc = None
                        self.tlog.event("precall_discarded_preload")
                    if pc is not None and pc.key != self._key(user_text):
                        pc.cancel()
                        pc = None
                        self.tlog.event("precall_discarded")
                    system = self._system_for(self.turn_opener)
                    j = await self._await_handoff(user_text)   # 끈 상태·시간 초과·오류면 None(기존 동작)
                    hint = system_hint(j, self.info.now) if j is not None else ""
                    if j is not None:
                        self.tlog.set("jev_handoff", {"route": route(j, self.info.now), "reasons": list(j.reasons),
                                                      "probs": dict(j.probs), "ms": round(j.ms)})
                    if hint:
                        system += hint                  # 가변부 끝(`## 이번 통화` 뒤) → 고정부 캐시 유지
                        if pc is not None:              # 힌트 없이 시작한 선호출은 버린다(인계 턴에만)
                            pc.cancel()
                            pc = None
                            self.tlog.event("precall_discarded_handoff")
                    self.tlog.set("precall_reused", pc is not None)
                    self.tlog.set("precall_count", self.precall_count)
                    settings = self.s
                    if self.opener_adaptive:
                        settings = self._adaptive_settings()
                    elif self.turn_opener == self.s.opener_lookup and self.s.opener_lookup_filler:
                        settings = dataclasses.replace(self.s, fillers=(self.s.opener_lookup_filler,))
                    tts = _PresynthTTS(self.d.tts, pc)
                    messages = self.conversation + [Message("user", user_text)]
                    responder = self._responder(tools, tts=tts, settings=settings, user_text=user_text)
                    try:
                        await responder.respond(
                            system, messages, send_response, spoken=spoken,
                            prefetch=pc.prefetch if pc is not None else None, log=self.tlog)
                    finally:
                        st["tool_log"] = list(responder.tool_log)
                        self.tlog.set("presynth_reused", tts.used)
                        if pc is not None:
                            pc.cancel()                 # 남은 선합성 정리(이미 쓴 것은 claimed라 그대로)
        except (Exception, TimeoutError) as e:  # noqa: BLE001
            st["failed"] = True
            self.failures += 1
            log.warning("response failed (%s): %r", self.info.session_id if self.info else "", e)
            self.tlog.set("error", repr(e)[:300])
            if self._precall is not None:
                self._precall.cancel()
            await self._play_cached(self.s.apology, spoken, pace=True)
        finally:
            self._safety = None
            self._safety_cap = None

    def _log_gap(self) -> None:
        """공백 = 본 응답 첫 오디오 송신 - (첫 마디 송신 시작 + 첫 마디 길이), ms.
        음수면 첫 마디가 끝나기 전에 본 응답이 도착(들리는 공백 없음)."""
        m = self.tlog.marks
        if self._opener_ms is not None:
            self.tlog.set("opener_ms", self._opener_ms)
        if "opener_start" in m and "response_first_audio" in m and self._opener_ms is not None:
            self.tlog.set("gap_ms", round(m["response_first_audio"] - (m["opener_start"] + self._opener_ms), 1))

    def _write_log(self) -> None:
        if self.d.log_writer is None:
            return
        try:
            self.d.log_writer.write(self.tlog.to_dict())
        except Exception as e:  # noqa: BLE001
            log.warning("turn log write failed: %r", e)

    async def _next_turn(self) -> None:
        """turn.end 뒤 연결이 유지되면 다음 발화를 받을 준비(새 STT 스트림, VAD 초기화)."""
        if self.closed:
            return
        old = self.stt
        self.stt = None
        if self._barged:                                # 고객이 지금 말하는 중: 바로 연다
            self._stt_open = asyncio.create_task(self.d.stt.open_stream(self.s.stt_context))
        else:
            self._stt_open = None
            self._stt_lazy_at = NEXT_TURN_STT_FRAMES
        if old is not None:
            try:
                await old.close()
            except Exception:  # noqa: BLE001
                pass
        self.vad.reset()
        self._ring.clear()
        self.detector = self.d.detector_factory()
        self._set_detector_context()                    # AG-T25: 이번 통화 대화까지 넣는다
        if self._handoff_task is not None and not self._handoff_task.done():
            self._handoff_task.cancel()
        self._handoff_task, self._handoff_key = None, ""
        self._dbg_tool_args = {}
        replay = self._bi_frames if self._barged else None   # 끼어든 말(AG-T19): 다음 프레임 때 새 턴으로 처리
        self._reset_turn()
        self._replay = replay or None
        self.responding = False
