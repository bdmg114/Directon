"""상담원 WebSocket 서버. 실행: uv run python -m agent.server --port 9000

실제 Provider·턴 감지·정규화는 build_deps()에서 지연 import한다(다른 Task의 모듈).
테스트는 fake deps로 handle(ws, deps)만 검증한다.

시작 순서(serve): build_deps → warmup(모델 첫 추론, TTS 캐시 합성, LLM 체인 예열, STT 연결 1회) → 대기.
AG-T25: 예열은 첫 마디 후보 전체(tts_cache.opener_phrases)도 합성한다(기본 문구 다음 순서, 지표 opener_synthesized).
DIRECTON_JEV_HANDOFF=1이고 TYPESAFE_API_KEY가 있으면 build_deps가 Jev 인계 판정기(SessionDeps.handoff)를 붙인다.

세션 격리(AG-T18): 측정 중에는 코드 수정·재시작이 불가하므로 handle()은 예외를 밖으로 내지 않는다.
- LLM·TTS·함수·STT 실패는 Session이 캐시 사과 + turn.end로 처리한다(session.py _respond_turn).
- 그 밖의 예외로 Session.run이 죽으면(턴 결정기·VAD·송신 오류 등) 비상 모드로 넘어간다:
  응답 중(turn.end 미송신)이었으면 즉시 캐시 사과 + transcript + turn.end, 그 뒤로는 연결이 끊길 때까지
  에너지 기준(측정기와 같은 RMS 300)으로 고객 발화 끝을 잡아 매 턴 사과 + turn.end를 보낸다.
  고객 발화가 끝나기 전에는 소리를 보내지 않는다(속도 0점 조건).
- deps는 세션 사이에 공유된다. 세션 상태(VAD·결정기·STT 스트림)는 세션마다 새로 만든다.
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import importlib
import inspect
import logging
import json
import os
import time
from pathlib import Path
from typing import Any, Callable

import numpy as np

from agent.config import Settings
from agent.session import Session, SessionDeps
from agent.tts_cache import opener_phrases

log = logging.getLogger("agent.server")

# build_deps가 찾는 이름(앞에서부터 처음 있는 것을 쓴다)
EXPECTED = {
    "agent.providers.factory": {"stt": ["make_stt"], "tts": ["make_tts"], "llm": ["make_llm"]},
    "agent.turn.vad": {"vad": ["make_vad", "SileroVAD"]},
    "agent.turn.detector": {"detector": ["make_detector", "RuleTurnDetector", "KoreanTurnDetector",
                                         "TurnDetector"]},
    "agent.normalize": {"normalize_args": ["normalize_args"]},
}


ROOT = Path(__file__).resolve().parent.parent
WARMUP_CAP_S = 25.0
# 서버로 띄울 때의 기본값. 시험 음성·발화를 저장하지 않는다(규정).
OFFICIAL_DEFAULTS = {"DIRECTON_NO_AUDIO_LOG": "1"}


class DepsImportError(RuntimeError):
    pass


# ── 세션 격리 ─────────────────────────────────────────
DEGRADED_SILENCE_RMS = 300.0      # 측정기 무음 기준과 같다
DEGRADED_MIN_SPEECH_MS = 200      # 이만큼 소리가 있어야 발화로 본다
DEGRADED_END_SILENCE_MS = 1000    # 발화 뒤 이만큼 무음이면 끝으로 본다(비상 모드라 보수적으로)
DEGRADED_WINDOW_FRAMES = 500      # 정상 모드에서 보관하는 최근 프레임(약 10초). 에너지 계산은 비상 모드에서만


def _frame_rms(b: bytes) -> float:
    a = np.frombuffer(b[: len(b) // 2 * 2], dtype=np.int16).astype(np.float32)
    return float(np.sqrt(np.mean(a * a))) if a.size else 0.0


class GuardedWS:
    """측정기 연결 감싸개. Session에는 원래 ws처럼 보이고, 서버는 송수신을 관찰한다.
    - 수신: session_id, 마지막 turn.end 이후 최근 프레임(정상 모드는 deque에 넣기만 한다 — Session이
      벽시계로 송신 속도를 맞추므로 프레임마다 계산을 얹지 않는다). 비상 모드(track())부터는
      고객 소리 누적(speech_ms)·마지막 소리 뒤 무음(silence_ms)을 프레임마다 갱신한다.
    - 송신: 마지막 JSON 종류(last_type), turn.end 횟수. turn.end를 보내면 발화 누적을 0으로.
    """

    def __init__(self, ws: Any):
        self.ws = ws
        self._it: Any = None
        self.session_id = ""
        self.last_type = ""
        self.turn_ends = 0
        self.speech_ms = 0
        self.silence_ms = 0
        self.closed = False
        self._tracking = False
        self._recent: collections.deque[bytes] = collections.deque(maxlen=DEGRADED_WINDOW_FRAMES)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.ws, name)

    def _update(self, frame: bytes) -> None:
        ms = len(frame) * 1000 // (2 * 16000)
        if _frame_rms(frame) >= DEGRADED_SILENCE_RMS:
            self.speech_ms += ms
            self.silence_ms = 0
        elif self.speech_ms:
            self.silence_ms += ms

    def track(self) -> None:
        """비상 모드 시작: 보관한 프레임으로 상태를 계산하고, 이후 프레임마다 갱신한다."""
        if self._tracking:
            return
        self._tracking = True
        self.speech_ms = self.silence_ms = 0
        for f in self._recent:
            self._update(f)
        self._recent.clear()

    def _observe(self, msg: Any) -> None:
        if isinstance(msg, (bytes, bytearray)):
            if self._tracking:
                self._update(bytes(msg))
            else:
                self._recent.append(msg)
        elif isinstance(msg, str) and not self.session_id:
            try:
                obj = json.loads(msg)
            except ValueError:
                return
            if isinstance(obj, dict) and obj.get("type") == "session.start":
                self.session_id = str(obj.get("session_id", ""))

    async def recv(self) -> Any:
        msg = await self.ws.recv()
        self._observe(msg)
        return msg

    def __aiter__(self) -> "GuardedWS":
        return self

    async def __anext__(self) -> Any:
        if self._it is None:
            self._it = self.ws.__aiter__()
        msg = await self._it.__anext__()
        self._observe(msg)
        return msg

    async def send(self, msg: Any) -> None:
        await self.ws.send(msg)
        if isinstance(msg, str):
            try:
                self.last_type = str(json.loads(msg).get("type", ""))
            except (ValueError, AttributeError):
                self.last_type = ""
            if self.last_type == "turn.end":
                self.turn_ends += 1
                self.speech_ms = self.silence_ms = 0
                self._recent.clear()

    def customer_done(self) -> bool:
        return self.speech_ms >= DEGRADED_MIN_SPEECH_MS and self.silence_ms >= DEGRADED_END_SILENCE_MS


async def _apology_turn(g: GuardedWS, deps: SessionDeps) -> None:
    """캐시 사과(없으면 소리 없이) + agent.transcript + turn.end. 송신 실패는 연결 종료로 본다."""
    s = deps.settings
    try:
        pcm = deps.cache.get(s.apology) or b""
    except Exception:  # noqa: BLE001
        pcm = b""
    n = max(2, int(getattr(s, "out_chunk_bytes", 3200) or 3200))
    try:
        for i in range(0, len(pcm), n):
            await g.send(pcm[i:i + n])
        await g.send(json.dumps({"type": "agent.transcript", "text": s.apology}, ensure_ascii=False))
        await g.send(json.dumps({"type": "turn.end"}))
    except Exception as e:  # noqa: BLE001
        log.warning("비상 사과 송신 실패(%s): %r", g.session_id, e)
        g.closed = True


async def _degraded(g: GuardedWS, deps: SessionDeps, session: Session | None) -> None:
    """Session이 죽은 뒤 연결을 유지하며 턴마다 사과 + turn.end(프로토콜만 지킨다)."""
    g.track()
    owed = session is not None and bool(getattr(session, "responding", False)) and g.last_type != "turn.end"
    if owed or g.customer_done():
        await _apology_turn(g, deps)
    try:
        async for msg in g:
            if g.closed:
                return
            if isinstance(msg, (bytes, bytearray)) and g.customer_done():
                await _apology_turn(g, deps)
    except Exception:  # noqa: BLE001 — 연결 종료(ConnectionClosed 등)
        return


async def handle(ws: Any, deps: SessionDeps) -> None:
    """세션 하나. 어떤 예외도 밖으로 내지 않는다(다른 세션·서버 보호). 취소만 그대로 올린다."""
    g = GuardedWS(ws)
    session: Session | None = None
    try:
        session = Session(g, deps)
        await session.run()
        return
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        log.exception("세션 예외(%s) → 비상 모드", g.session_id or "?")
    try:
        await _degraded(g, deps, session)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        log.exception("비상 모드 예외(%s)", g.session_id or "?")


def _pick(module: str, names: list[str]) -> Any:
    try:
        mod = importlib.import_module(module)
    except ImportError as e:
        raise DepsImportError(f"{module} 를 import할 수 없습니다({e}). "
                              f"해당 Task 구현이 필요합니다: {', '.join(names)}") from e
    for n in names:
        if hasattr(mod, n):
            return getattr(mod, n)
    raise DepsImportError(f"{module} 에 {' / '.join(names)} 중 아무것도 없습니다.")


def _adapt_normalize(fn: Callable) -> Callable:
    """normalize_args(tool, args, schema[, now]) 둘 다 받는다."""
    try:
        n = len(inspect.signature(fn).parameters)
    except (TypeError, ValueError):
        n = 4
    if n >= 4:
        return fn
    return lambda tool, args, schema, now: fn(tool, args, schema)


def _cache_voice_key(s: Settings, tts: Any) -> str:
    """캐시 네임스페이스: 설정 키 + TTS 종류·모델·음성 + 레벨(모델·음성·레벨을 바꾸면 캐시도 바뀐다)."""
    inner = getattr(tts, "inner", tts)
    voice = getattr(inner, "voice", "") or ""
    model = getattr(inner, "model", "") or ""
    return f"{s.tts_voice_key}|{type(inner).__name__}:{model}:{voice}|L{int(s.tts_level_rms)}"


def build_deps(settings: Settings | None = None) -> SessionDeps:
    from agent.tools_client import ToolsClient
    from agent.tts_cache import LevelTTS, TTSCache
    from agent.turn_log import TurnLogWriter

    s = settings or Settings.from_env()
    if not s.stt_context:                              # STT에 브랜드·제품 이름을 알려 준다(제품명은 브랜드 정보에서 읽는다)
        try:
            import dataclasses

            from agent import knowledge
            s = dataclasses.replace(s, stt_context=", ".join(knowledge.stt_terms()))
        except Exception as e:  # noqa: BLE001
            log.warning("STT 용어를 만들지 못했다: %r", e)
    f = EXPECTED
    make_stt = _pick("agent.providers.factory", f["agent.providers.factory"]["stt"])
    make_tts = _pick("agent.providers.factory", f["agent.providers.factory"]["tts"])
    make_llm = _pick("agent.providers.factory", f["agent.providers.factory"]["llm"])
    vad_factory = _pick("agent.turn.vad", f["agent.turn.vad"]["vad"])
    detector_factory = _pick("agent.turn.detector", f["agent.turn.detector"]["detector"])
    normalize = _adapt_normalize(_pick("agent.normalize", f["agent.normalize"]["normalize_args"]))
    tts = make_tts()
    if s.tts_level_rms and s.tts_level_rms > 0:
        tts = LevelTTS(tts, target_rms=float(s.tts_level_rms))
    cache = TTSCache(s.tts_cache_dir, voice=_cache_voice_key(s, tts), chunk_bytes=s.out_chunk_bytes,
                     target_rms=float(s.tts_level_rms or 0))
    return SessionDeps(stt=make_stt(), tts=tts, llm=make_llm(), vad_factory=vad_factory,
                       smart_turn=None, detector_factory=detector_factory,
                       tools=ToolsClient(local_tools=_local_tools()), cache=cache,
                       settings=s, normalize_args=normalize, log_writer=TurnLogWriter(s.log_dir),
                       handoff=_make_handoff(), filler=_make_filler())


def _local_tools() -> list[dict] | None:
    """함수 정의를 지식 폴더의 tools.json에서 읽는다(없으면 None → 세션마다 GET {tools_url}/tools).
    측정기 중계가 느리거나 실패해도 함수 없이 답하는 일이 없고, 프롬프트 앞부분이 글자 단위로 고정된다."""
    import json

    from agent import knowledge

    try:
        path = knowledge.resolve_knowledge_dir() / "tools.json"
        tools = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as e:  # noqa: BLE001
        log.warning("함수 정의 파일을 읽지 못했다(GET /tools로 받는다): %r", e)
        return None
    if not isinstance(tools, list) or not tools:
        return None
    return [t if isinstance(t, dict) and "function" in t else {"type": "function", "function": t} for t in tools]


def _make_filler() -> Any:
    """AG-T66 Jev 채움말 판정기: DIRECTON_JEV_FILLER=1과 TYPESAFE_API_KEY가 다 있을 때만. 아니면 None(로컬 규칙만)."""
    from agent import filler_jev
    j = filler_jev.make_filler_judge()
    if j is not None:
        log.info("Jev 채움말 판정 켬(%s, 질문 %d개)", filler_jev.MODEL, len(j.questions))
    return j


def _make_handoff() -> Any:
    """AG-T25 Jev 인계 판정기: 스위치(DIRECTON_JEV_HANDOFF=1)와 TYPESAFE_API_KEY가 다 있을 때만. 아니면 None(기존 동작)."""
    from agent import handoff_jev
    from agent.turn import jev_turn

    if not handoff_jev.handoff_enabled():
        return None
    if not jev_turn.api_key():
        log.warning("DIRECTON_JEV_HANDOFF=1이지만 TYPESAFE_API_KEY가 없어 인계 판정을 끈다")
        return None
    log.info("Jev 인계 판정 켬(%s)", handoff_jev.MODEL)
    return handoff_jev.JevHandoff()


# ── 시작 예열 ─────────────────────────────────────────
async def _warm_models(deps: SessionDeps) -> float:
    t = time.perf_counter()
    vad = deps.vad_factory()
    for _ in range(10):
        vad.process(bytes(640))
    if deps.smart_turn is not None:
        await asyncio.to_thread(deps.smart_turn.predict, bytes(32000))
    return time.perf_counter() - t


def _is_not_found(e: BaseException) -> bool:
    code = getattr(e, "code", None) or getattr(e, "status_code", None)
    return code == 404 or "404" in str(e)[:40] or "NOT_FOUND" in str(e)


async def _warm_one_llm(llm: Any, timeout: float, system: str = "한 단어로만 답하세요.",
                        tools: list[dict] | None = None) -> bool | BaseException:
    """성공이면 True, 실패면 예외 객체. 실제 고정부와 함수 정의를 주면 프롬프트 캐시도 데워진다."""
    from agent.core.types import Message

    try:
        async with asyncio.timeout(timeout):
            agen = llm.stream(system, [Message("user", "안녕하세요")], tools or [])
            try:
                async for _ev in agen:
                    pass                              # 끝까지 받아야 캐시가 만들어진다
            finally:
                aclose = getattr(agen, "aclose", None)
                if aclose is not None:
                    await aclose()
        return True
    except (Exception, TimeoutError) as e:  # noqa: BLE001
        log.warning("llm warm failed (%s): %r", getattr(llm, "model", type(llm).__name__), e)
        return e


async def _warm_llm(deps: SessionDeps) -> tuple[bool, list[str]]:
    """LLM에 짧은 요청 1회(연결·모델 확인). 반환: (성공 여부, 뺀 모델 이름 — 지금은 항상 빈 목록)."""
    if not deps.settings.warmup_llm:
        return False, []
    system, tools = _warm_prompt(deps)
    res = await _warm_one_llm(deps.llm, deps.settings.warmup_timeout_s, system, tools)
    if res is True and tools:                         # 두 번째 요청은 캐시를 탄다(로그의 cached 값으로 확인)
        await _warm_one_llm(deps.llm, deps.settings.warmup_timeout_s, system, tools)
    return res is True, []


def _warm_prompt(deps: SessionDeps) -> tuple[str, list[dict]]:
    """예열용 프롬프트: 실제 통화와 같은 고정부 + 함수 정의. 준비가 안 되면 짧은 문장."""
    from agent import knowledge
    from agent.core.types import SessionInfo

    tools = getattr(deps.tools, "local_tools", None) or []
    try:
        info = SessionInfo(session_id="warmup", case_id="", now="", history=[], caller=None, tools_url="")
        return knowledge.build_system(info, prefix=deps.manual), tools
    except Exception as e:  # noqa: BLE001
        log.warning("예열용 고정부를 만들지 못했다: %r", e)
        return "한 단어로만 답하세요.", []


async def _warm_stt(deps: SessionDeps) -> bool:
    try:
        async with asyncio.timeout(deps.settings.warmup_timeout_s):
            stream = await deps.stt.open_stream(deps.settings.stt_context)
            await stream.close()
        return True
    except (Exception, TimeoutError) as e:  # noqa: BLE001
        log.warning("stt warm failed: %r", e)
        return False


async def warmup(deps: SessionDeps) -> dict:
    """첫 문항 지연 제거: 모델 첫 추론 → (캐시 합성 ‖ LLM 예열 ‖ STT 연결). 실패는 경고만."""
    t0 = time.perf_counter()
    info: dict[str, Any] = {"models_s": round(await _warm_models(deps), 3)}

    from agent import knowledge

    base = deps.settings.cached_phrases()
    extra = [p for p in opener_phrases(deps.settings) if p not in base]   # AG-T20 첫 마디 후보(기본 문구 뒤)
    extra += [p for p in (*knowledge.FAIL_SENTENCES.values(), *knowledge.TOOL_PHRASES.values(),
                          knowledge.NEUTRAL_PHRASE, *knowledge.COMMON_SENTENCES,
                          *knowledge.empathy_variants()) if p not in base and p not in extra]
    n_extra = 0

    async def cache() -> int:
        nonlocal n_extra
        try:
            n = await deps.cache.warm(base, deps.tts)
        except Exception as e:  # noqa: BLE001
            log.warning("tts cache warm failed: %r", e)
            n = 0
        try:
            n_extra = await deps.cache.warm(extra, deps.tts)
        except Exception as e:  # noqa: BLE001
            log.warning("opener cache warm failed: %r", e)
        return n

    n, (llm_ok, dropped), stt_ok = await asyncio.gather(cache(), _warm_llm(deps), _warm_stt(deps))
    missing = [p for p in base if deps.cache.get(p) is None]
    info.update(cache_synthesized=n, cache_missing=missing, opener_synthesized=n_extra,
                opener_missing=[p for p in extra if deps.cache.get(p) is None],
                llm_ok=llm_ok, llm_dropped=dropped,
                llm_chain=[str(getattr(m, "model", m)) for m in (getattr(deps.llm, "llms", None) or [deps.llm])],
                stt_ok=stt_ok,
                total_s=round(time.perf_counter() - t0, 3))
    return info


async def serve(port: int, host: str = "0.0.0.0", deps: SessionDeps | None = None) -> None:
    import websockets

    deps = deps or build_deps()
    try:                                              # 예열이 끝나야 포트를 연다 → 상한을 둔다
        info = await asyncio.wait_for(warmup(deps), WARMUP_CAP_S)
    except (Exception, TimeoutError) as e:  # noqa: BLE001
        log.warning("warmup 중단(%r) — 예열 없이 시작한다", e)
        info = {}
    log.info("warmup: %s", info)
    if info.get("cache_missing"):
        log.warning("캐시 문구 합성 실패(첫 마디가 늦어진다): %s", info["cache_missing"])
    # ping_timeout을 넉넉히: 4세션 동시 처리 중 이벤트 루프가 잠깐 바빠도(터널 경유 포함) 연결을 끊지 않게
    async with websockets.serve(lambda ws: handle(ws, deps), host, port, max_size=None,
                                ping_interval=20, ping_timeout=60):
        log.info("agent server on :%d", port)
        await asyncio.Future()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--host", default="0.0.0.0")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except Exception as e:  # noqa: BLE001
        log.warning(".env를 읽지 못했다: %r", e)
    for k, v in OFFICIAL_DEFAULTS.items():            # 측정 기본값(환경변수가 이미 있으면 그대로 둔다)
        os.environ.setdefault(k, v)
    try:
        asyncio.run(serve(a.port, a.host))
    except DepsImportError as e:
        raise SystemExit(f"의존 모듈 준비 안 됨: {e}")


if __name__ == "__main__":
    main()
