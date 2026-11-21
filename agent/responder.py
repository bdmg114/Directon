"""응답기: LLM 스트리밍 + 툴 루프 → 문장 분할 → 문장별 TTS(다음 문장 미리 합성) → 송신 콜백."""
from __future__ import annotations

import array
import asyncio
import json
import logging
import re
import time
import unicodedata
from typing import Any, AsyncIterator, Awaitable, Callable

from agent.config import Settings
from agent.core.types import LLMEvent, LLMProvider, Message, ToolCall, TTSProvider
from agent.normalize import spoken_digits
from agent.turn_log import TurnLog

log = logging.getLogger(__name__)

AudioSink = Callable[[bytes], Awaitable[None]]
CallTool = Callable[[str, dict], Awaitable[dict]]
NormalizeArgs = Callable[[str, dict, dict, str], dict]


def identity_normalize(tool_name: str, args: dict, schema: dict, now: str) -> dict:
    return args


class ResponderError(RuntimeError):
    pass


# ── 문장 분할 ─────────────────────────────────────────
_PUNCT = set(".?!…~。？！")
_MARKDOWN = re.compile(r"[*#`_]+")
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️]")


def clean_for_speech(text: str) -> str:
    return _EMOJI.sub("", _MARKDOWN.sub("", text))


def _nchars(s: str) -> int:
    return sum(1 for c in s if not c.isspace())


class SentenceSplitter:
    """증분 문장 분할. 문장부호(. ? ! … ~) 뒤에 공백/줄바꿈이 와야 경계로 본다(1.5 같은 소수 보호).
    min_chars보다 짧은 조각은 다음 문장과 합친다."""

    def __init__(self, min_chars: int = 6):
        self.min_chars = min_chars
        self.buf = ""
        self.pending = ""

    def _emit(self, sentence: str, out: list[str]) -> None:
        s = f"{self.pending} {sentence}".strip() if self.pending else sentence.strip()
        if not s:
            return
        if _nchars(s) < self.min_chars:
            self.pending = s
        else:
            out.append(s)
            self.pending = ""

    def feed(self, text: str) -> list[str]:
        self.buf += clean_for_speech(text)
        out: list[str] = []
        i = 0
        while i < len(self.buf):
            c = self.buf[i]
            if c == "\n":
                self._emit(self.buf[:i], out)
                self.buf = self.buf[i + 1:].lstrip()
                i = 0
                continue
            if c in _PUNCT:
                k = i
                while k < len(self.buf) and self.buf[k] in _PUNCT:
                    k += 1
                if k >= len(self.buf):
                    break                       # 다음 글자를 봐야 안다
                if self.buf[k].isspace():
                    self._emit(self.buf[:k], out)
                    self.buf = self.buf[k:].lstrip()
                    i = 0
                    continue
                i = k
                continue
            i += 1
        return out

    def flush(self) -> list[str]:
        rest = self.buf.strip()
        s = f"{self.pending} {rest}".strip() if self.pending else rest
        self.buf, self.pending = "", ""
        return [s] if s else []


# ── LLM 선호출 ────────────────────────────────────────
class Prefetch:
    """LLM 스트림을 미리 읽어 버퍼에 쌓는다. 툴 실행 같은 부작용은 없다."""

    def __init__(self, agen: AsyncIterator[LLMEvent]):
        self.buf: list[LLMEvent] = []
        self.done = False
        self.error: BaseException | None = None
        self._ev = asyncio.Event()
        self.task = asyncio.create_task(self._run(agen))

    async def _run(self, agen: AsyncIterator[LLMEvent]) -> None:
        try:
            async for ev in agen:
                self.buf.append(ev)
                self._ev.set()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            self.error = e
        finally:
            self.done = True
            self._ev.set()
            aclose = getattr(agen, "aclose", None)
            if aclose is not None:
                try:
                    await aclose()
                except BaseException:  # noqa: BLE001
                    pass

    @property
    def cancelled(self) -> bool:
        return self.task.cancelled()

    def cancel(self) -> None:
        self.task.cancel()

    async def events(self) -> AsyncIterator[LLMEvent]:
        i = 0
        while True:
            if i < len(self.buf):
                yield self.buf[i]
                i += 1
                continue
            if self.done:
                if self.error is not None:
                    raise self.error
                if self.task.cancelled():
                    raise ResponderError("prefetch cancelled")
                return
            self._ev.clear()
            await self._ev.wait()


# ── 송신 파이프라인 ───────────────────────────────────
# 문장 사이 쉼(사용자 요청 2026-11-21: "각 단계 사이에는 무조건 0.8초의 무음"): 상담원이 말하는 소리 조각(첫 마디·채움말·
# 결과 문구·답 문장·번호 되읽기) 사이에 최소 이만큼의 무음을 보장한다. 조각 끝의 무음과, 앞 소리 재생이 다 끝난 뒤 실제로
# 흐른 무음(세션 재생 시계, play_idle_ms)은 이미 들린 쉼이라 빼고 모자란 만큼만 채운다. 그 턴의 첫 소리 앞에는 넣지 않는다.
# Settings.sentence_gap_ms가 음수면 이 기본값(테스트는 conftest에서 0으로 둔다).
DEFAULT_SENTENCE_GAP_MS = 800
_GAP_SILENT_RMS = 300.0         # 이보다 작은 20ms 프레임은 무음으로 센다(조각 끝 무음 길이)
_FRAME = 640                    # 20ms, 16kHz s16le


def sentence_gap_ms(settings: Any) -> int:
    v = getattr(settings, "sentence_gap_ms", -1)
    return DEFAULT_SENTENCE_GAP_MS if v is None or v < 0 else int(v)


def _tail_silence_ms(pcm: bytes, prev_ms: float) -> float:
    """pcm을 이어 보낸 뒤 끝에 이어진 무음 길이(ms). pcm 전체가 무음이면 앞의 무음에 더한다."""
    pcm = pcm[: len(pcm) // 2 * 2]
    tail = 0.0
    end = len(pcm)
    while end > 0:
        start = max(0, end - _FRAME)
        a = array.array("h")
        a.frombytes(pcm[start:end])
        if a and (sum(x * x for x in a) / len(a)) ** 0.5 >= _GAP_SILENT_RMS:
            return tail
        tail += (end - start) / 32.0
        end = start
    return prev_ms + tail


class _Speaker:
    """문장이 들어오는 즉시 합성을 시작하고(파이프라인), 순서대로 송신한다."""

    def __init__(self, tts: TTSProvider, send_audio: AudioSink, spoken: list[str],
                 settings: Settings, tlog: TurnLog | None,
                 on_audio: Callable[[str, bytes], None] | None = None,
                 play_idle_ms: Callable[[], float] | None = None):
        self.tts = tts
        self.play_idle_ms = play_idle_ms    # 보낸 소리가 다 재생된 뒤 흐른 시간(ms, 재생 중이면 0). 없으면 빼지 않는다
        self.send_audio = send_audio
        self.on_audio = on_audio            # (kind: "pcm"=캐시 필러 | "tts"=본 응답, pcm) — 송신 직전 호출(AG-T21 측정)
        self.spoken = spoken
        self.settings = settings
        self.tlog = tlog
        self.q: asyncio.Queue = asyncio.Queue()
        self.synths: list[asyncio.Task] = []
        self._tail_ms = 0.0                 # 지금까지 보낸 소리 끝의 무음 길이
        self.task = asyncio.create_task(self._run())

    def say(self, text: str) -> None:
        out: asyncio.Queue = asyncio.Queue()
        self.synths.append(asyncio.create_task(self._synth(text, out)))
        self.q.put_nowait(("tts", text, out))

    def play(self, text: str, pcm: bytes) -> None:
        self.q.put_nowait(("pcm", text, pcm))

    async def _synth(self, text: str, out: asyncio.Queue) -> None:
        if self.tlog:
            self.tlog.event("tts_request", text=text)
        try:
            first = True
            async for chunk in self.tts.synthesize(tts_text(text)):
                if first and self.tlog:
                    self.tlog.event("tts_first_byte", text=text)
                first = False
                out.put_nowait(chunk)
            out.put_nowait(None)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            out.put_nowait(e)

    async def _send(self, pcm: bytes) -> None:
        await self.send_audio(pcm)
        self._tail_ms = _tail_silence_ms(pcm, self._tail_ms)

    async def _gap(self) -> None:
        """다음 조각 앞에 쉼을 둔다. 이번 턴에 이미 말한 것이 있을 때만(첫 소리를 늦추지 않는다)."""
        gap = sentence_gap_ms(self.settings)
        if gap <= 0 or not self.spoken:
            return
        need = gap - self._tail_ms
        if self.play_idle_ms is not None:           # 앞 소리가 다 재생된 뒤 이미 흐른 무음도 쉼이다(세션 재생 시계 기준)
            try:
                need -= max(0.0, float(self.play_idle_ms()))
            except Exception:  # noqa: BLE001 — 시계를 못 읽으면 쉼을 다 채운다
                pass
        if need < 20:
            return
        n = int(need * 16) * 2                      # 16 샘플/ms × 2바이트
        await self._send(b"\x00" * n)
        if self.tlog:
            self.tlog.event("sentence_gap", ms=round(need))

    async def _run(self) -> None:
        while True:
            item = await self.q.get()
            if item is None:
                return
            kind, text, payload = item
            if kind == "pcm":
                await self._gap()
                self.spoken.append(text)
                self._notify(kind, payload)
                await self._send(payload)
                continue
            first = True
            while True:
                chunk = await asyncio.wait_for(payload.get(), self.settings.tts_timeout_s)
                if chunk is None:
                    break
                if isinstance(chunk, BaseException):
                    raise chunk
                if first:
                    await self._gap()
                    self.spoken.append(text)
                    first = False
                self._notify(kind, chunk)
                await self._send(chunk)

    def _notify(self, kind: str, pcm: bytes) -> None:
        if self.on_audio is not None:
            try:
                self.on_audio(kind, pcm)
            except Exception as e:  # noqa: BLE001 — 측정 훅이 응답을 막으면 안 된다
                log.debug("on_audio failed: %r", e)

    def raise_if_failed(self) -> None:
        if self.task.done():
            exc = self.task.exception()
            if exc is not None:
                raise exc

    async def finish(self) -> None:
        self.q.put_nowait(None)
        await self.task

    def cancel(self) -> None:
        self.task.cancel()
        for t in self.synths:
            t.cancel()


# ── 발화 속 번호(검사 모듈 agent.guards가 쓴다) ──────
# LLM(flash-lite)이 고객이 한글로 읽은 번호("이육일일 일사공 사구팔")를 숫자로 옮기다 자릿수를 섞는 일이 잦다.
# 고객 발화에서 번호를 직접 뽑아 두고, LLM 인자가 그중 하나와 거의 같지만(편집 거리 ≤ 2) 정확히 같지 않으면
# 발화의 번호로 바꾼다(같은 길이면 편집 거리 ≤ 3). 발화에 번호가 없거나 많이 다르면(다른 번호를 일부러 쓴 경우) 그대로 둔다.
_NUM_SPLIT = re.compile(r"[\s,.\-+/·~()\[\]:;!?\"']+")


_CORRECTION = re.compile(r"^(아니|아니다|아니요|아니고|아니고요|아니에요|아니라|정정|다시)$")


MIN_NUMBER_DIGITS = 7           # 기본 데이터(주문 10자리·전화 11자리) 기준. 데이터 번호가 더 짧으면 그 길이로 낮춘다(AG-T21)


_DIGIT_CHARS = set("0123456789공영빵일이삼사오육륙칠팔구")


# (조사·어미, 앞 글자 받침 필요 여부). 긴 것부터.
_NUM_SUFFIXES = [("이거든요", True), ("이야", True), ("거든요", False), ("인데요", False), ("이고요", True), ("이에요", True), ("입니다", True), ("이요", True), ("이고", True),
                 ("으로", True), ("이랑", True), ("고요", False), ("예요", False), ("에요", False),
                 ("하고", False), ("랑", False), ("요", False), ("로", False), ("에", False), ("번", False)]


def _has_final(ch: str) -> bool:
    if ch.isdigit():
        return True
    code = ord(ch) - 0xAC00
    return 0 <= code <= 11171 and code % 28 != 0


def _token_readings(tok: str) -> list[str]:
    """숫자 토큰의 읽기 후보(첫째가 기본). "삼이요"처럼 '이'가 숫자 2일 수도 있으면 두 읽기를 모두 낸다."""
    out: list[str] = []
    cores = []
    core = tok
    while True:
        for suf, need in _NUM_SUFFIXES:
            if core.endswith(suf) and len(core) > len(suf):
                rest = core[: -len(suf)]
                if need and not _has_final(rest[-1]):
                    continue
                cores.append(core)              # 벗기기 전 형태도 후보(이→2)
                core = rest
                break
        else:
            break
    for c in [core] + list(reversed(cores)):
        c2 = re.sub(r"(요|고요|로|에)$", "", c) if c is not core else c
        if c2 and all(ch in _DIGIT_CHARS for ch in c2):
            d = spoken_digits(c2)
            if d and d not in out:
                out.append(d)
    if any(ch.isdigit() for ch in core):        # 아라비아 숫자 뒤 '이요'는 조사뿐
        return out[:1]
    return out


def utterance_numbers(text: str, min_digits: int = MIN_NUMBER_DIGITS) -> list[str]:
    """발화 속 번호(연속된 숫자 토큰 묶음, MIN_NUMBER_DIGITS자리 이상). "아니다" 뒤 짧은 묶음은 앞 번호의 끝을
    대체한다. 읽기가 모호한 토큰("삼이요")은 다른 읽기의 번호도 함께 낸다."""
    out: list[str] = []
    run: list[list[str]] = []
    corrected = False

    def variants(r: list[list[str]]) -> list[str]:
        base = "".join(x[0] for x in r)
        vs = [base]
        for i, x in enumerate(r):
            for alt in x[1:]:
                vs.append("".join(alt if j == i else y[0] for j, y in enumerate(r)))
        return vs

    def close() -> None:
        nonlocal run
        if run:
            vs = variants(run)
            if corrected and out and len(vs[0]) < len(out[-1]):
                prev = out[-1]
                out.extend(prev[: len(prev) - len(v)] + v for v in vs if len(v) < len(prev))
            else:
                out.extend(v for v in vs if len(v) >= min_digits)
        run = []

    for tok in _NUM_SPLIT.split(text or ""):
        if not tok:
            continue
        rd = _token_readings(tok)
        if rd:
            run.append(rd)
            continue
        close()
        corrected = bool(_CORRECTION.match(tok))
    close()
    return list(dict.fromkeys(out))


def _edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


VOICED_RMS = 300.0                     # 측정기(eval.runner)와 같은 유성음 기준


def _voiced(pcm: bytes, thresh: float = VOICED_RMS) -> bool:
    if not pcm:
        return False
    import numpy as np
    n = len(pcm) - len(pcm) % 2
    a = np.frombuffer(pcm[:n], dtype=np.int16).astype(np.float32)
    for i in range(0, len(a), 320):                 # 20ms 프레임 중 하나라도 유성이면
        f = a[i:i + 320]
        if len(f) and float(np.sqrt((f * f).mean())) >= thresh:
            return True
    return False


# ── 말로 내보내면 안 되는 조각 ────────────────────────
_CALL_TEXT = re.compile(r"[A-Za-z_]{3,}\s*\(\s*\{.*?\}\s*\)", re.S)   # LLM이 글로 쓴 함수 호출: search_products({"query": ...})
_JSON_TEXT = re.compile(r"\{[^{}]*\}")                  # 인수만 글로 쓴 것: {"order_id": null}
_ROUTE_TEXT = re.compile(r"\bto=\s*\S*")               # 함수 호출 머리말이 새어 나온 것: to=functions.xxx
_KEY_TEXT = re.compile(r'"[^"\s]{1,40}"\s*:')           # 조각난 인수의 "키": 꼴 — "reason":"포장해 놨어요.
_BRACE = re.compile(r"[{}]")


def _foreign(c: str) -> bool:
    """한글·라틴·그리스 문자가 아닌 글자(한자·가나·태국·벵골 문자 등과 그 결합 기호). 한국어 상담에 나올 일이 없다."""
    o = ord(c)
    if o < 0x400 or 0xAC00 <= o <= 0xD7A3 or 0x1100 <= o <= 0x11FF or 0x3130 <= o <= 0x318F:
        return False
    return unicodedata.category(c)[0] in "LM"


def _strip_json(s: str) -> str:
    """온전한 {…}를 뺀다(겹친 것은 안쪽부터)."""
    prev = None
    while prev != s:
        prev, s = s, _JSON_TEXT.sub(" ", s)
    return s


def json_piece(sentence: str) -> bool:
    """조각난 JSON인가: 온전한 {…}와 함수(…) 글을 빼고도 중괄호나 "키": 꼴이 남는다.

    LLM이 인수를 글로 쓰면 값 안의 마침표에서 문장이 잘려 `{"reason":"포장해 놨어요.`와 `환불해 주세요."}`가 따로 온다
    (2026-11-21 공식 측정 P0074-01). 큰따옴표로 인용한 말이나 쉼표·숫자가 있는 보통 문장은 해당하지 않는다."""
    s = _strip_json(_CALL_TEXT.sub(" ", sentence or ""))
    return bool(_BRACE.search(s) or _KEY_TEXT.search(s))


_DIGIT_HYPHEN = re.compile(r"(?<=\d)\s*-\s*(?=\d)")


def tts_text(text: str) -> str:
    """TTS에 넘기는 글: 숫자 사이 하이픈은 쉼표로(번호를 끊어 읽게, 하이픈을 '다시'로 읽는 일 방지). 기록·자막 글은 그대로."""
    return _DIGIT_HYPHEN.sub(", ", text or "")


# LLM이 가려진 번호 앞에 앞자리를 덧붙여 쓰면("010, [PHONE]") 바꾼 뒤 010이 두 번 읽힌다(데모 DEMO-20261121-180355) →
# 표식 바로 앞의 010·공일공은 표식과 함께 바꾼다
_MASK_TEXT = re.compile(r"(?:(?:010|공일공|공 일 공)\s*[,.\-]?\s*)?\[\s*PHONE\s*\]", re.I)
_MASK_TEXT_BEON = re.compile(r"\[\s*PHONE\s*\](?:\s*번)?", re.I)


def speech_safe(sentence: str, tool_names: Any = (), mask_number: str = "") -> str:
    """LLM이 함수 호출이나 함수 이름을 글로 써 버린 부분을 뺀다. 남는 말이 없으면 ""(그 문장은 읽지 않는다).
    조각난 JSON이 섞인 문장은 통째로 읽지 않는다."""
    if json_piece(sentence):
        return ""
    # AG-T58: 답 속의 [PHONE]은 실제 번호로 바꿔 읽는다. 바꿀 번호를 모르면 표식을 소리 내 읽지 않게 말로 바꾼다
    s = (_MASK_TEXT.sub(mask_number, sentence or "") if mask_number
         else _MASK_TEXT_BEON.sub("말씀하신 번호", sentence or ""))
    s = _CALL_TEXT.sub(" ", s)
    s = _ROUTE_TEXT.sub(" ", _strip_json(s))
    s = "".join(" " if _foreign(c) else c for c in s)
    for n in tool_names or ():
        if n:
            s = re.sub(re.escape(n) + "|" + re.escape(n.replace("_", "")), " ", s, flags=re.I)
    s = re.sub(r"\s+", " ", s).strip()
    return s if re.search(r"[가-힣0-9]", s) else ""


BRIDGE_NOTE = ("_already_said의 말은 방금 고객에게 이미 했다. 그 말을 되풀이하지 말고 바로 이어서 내용을 말한다.")
# 검사(agent.guards)가 번호 인수를 다른 번호로 맞춰 실행했을 때 그 결과에 붙인다(P0133-03: 접수는 4402, 답은 받아쓴 4002를 읽음)
CALLED_WITH_NOTE = ("이 함수는 _called_with의 번호로 실행됐다. 그 번호를 고객에게 말할 때는 고객이 방금 말한 글의 번호가 아니라 "
                    "_called_with의 값을 쓴다.")
# AG-T57: 이번 통화에서 받아 적은 번호로 조회했는데 내역이 없으면 그 결과에 붙인다. 번호를 말해 주지 않으면 고객은
# "번호를 잘못 들은 것"과 "번호는 맞는데 내역이 없는 것"을 구분할 수 없다(2026-11-21 직접 통화 피드백).
# 조회가 성공했을 때는 붙이지 않는다(성공 때도 읽게 하면 응대 점수가 떨어졌다: AG-T33 보강 2). 스위치 DIRECTON_NOTFOUND_READBACK(기본 켬).
# 휴대폰 번호는 게이트웨이가 "[PHONE]"으로 가려 LLM이 볼 수 없다(AG-T58). 그래서 LLM은 문장만 쓰고 번호 자리에 [PHONE]을 두며,
# 소리로 내보내기 직전에 서버가 실제 번호로 바꾼다(speech_safe의 mask_number). 주문번호는 가려지지 않아 값을 그대로 준다.
NOTFOUND_READBACK_ENV = "DIRECTON_NOTFOUND_READBACK"
MASK_TOKEN = "[PHONE]"
NOTFOUND_READBACK_NOTE = ("이 조회는 고객이 방금 말한 번호(_looked_up_with)로 했고 확인되는 내역이 없었다. 받아 적은 번호라 잘못 "
                          "들었을 수 있다. 답에서 그 번호를 넣어 \"…번으로 조회했는데 확인되는 내역이 없네요. 번호가 맞으실까요?\"처럼 "
                          "자연스럽게 확인한다. 번호를 말하지 않은 채 내역이 없다고만 하지 않는다. _looked_up_with가 \"[PHONE]\"이면 "
                          "답에 [PHONE]이라고 그대로 쓴다. 시스템이 실제 번호로 바꿔 읽는다.")
# 이번 턴에 검사에서 막힌 호출이 있으면 마지막 회차(함수 목록 없이 부른다)의 시스템 글 끝에 붙인다. 없으면 모델이 막힌 호출을
# 글로 다시 쓴다(P0074-01). 2026-11-21 gpt-5.4-mini 실측(조회 실행 → 접수 막힘 뒤 마지막 회차): 안내 없이는 38회 중 16회 호출을
# 글로 썼고, 이 글을 붙이면 34회 중 0회. "함수를 더 부를 수 없다"고 쓰면 그것을 이유로 상담원 연결을 안내하는 답이 섞여(12회 중 2회)
# "부르지 않고 말로만"으로 적는다. 막힌 호출이 없는 턴(조회만 두 번)은 호출을 글로 쓰지 않았고(21회 중 0회) 이 글을 붙이면 답이
# 두루뭉술해져(제품명을 되묻는 답 21회 중 5회) 붙이지 않는다.
FINAL_ROUND_NOTE = ("\n\n## 지금 할 일\n이번 답에서는 함수를 부르지 않고 말로만 답한다. 함수 호출이나 JSON(중괄호, \"키\": 값)을 글로 쓰지 않는다. "
                    "status가 blocked인 결과는 아직 실행되지 않은 것이다. 그 message가 시키는 대로 고객에게 빠진 내용을 여쭤보거나 안내하고, "
                    "처리됐다고 말하지 않는다. 함수를 부르지 못한다는 이유로 상담원 연결을 안내하지 않는다.")


def _only_digits(v: Any) -> str:
    d = re.sub(r"\D", "", str(v))
    return d or (spoken_digits(str(v)) or "")


def _dashed_phone(d: str) -> str:
    """01048334402 → 010-4833-4402(LLM이 끊어 읽기 쉬운 꼴). 전화번호 꼴이 아니면 그대로."""
    if len(d) == 11 and d.startswith("01"):
        return f"{d[:3]}-{d[3:7]}-{d[7:]}"
    if len(d) == 10 and d.startswith("01"):
        return f"{d[:3]}-{d[3:6]}-{d[6:]}"
    return d


def guards_status(res: Any) -> str:
    from agent import guards
    return guards.lookup_status(res)


def _readback_on() -> bool:
    import os
    return os.environ.get(NOTFOUND_READBACK_ENV, "1").strip().lower() not in ("0", "false", "no", "off")


def readback_number(nums: dict[str, str]) -> str:
    """{인수 키: 숫자} → 끊어 읽을 번호 하나("010, 1234, 5678"). 주문번호가 있으면 주문번호를 읽는다."""
    from agent import guards
    from agent.normalize import group_digits
    for keys, kind in ((guards.ORDER_KEYS, "order"), (guards.PHONE_KEYS, "phone")):
        for k in keys:
            if nums.get(k):
                return group_digits(nums[k], kind)
    return ""


def _number_changes(asked: Any, used: Any) -> dict[str, str]:
    """검사가 번호 인수를 다른 번호로 바꿔 실행했으면 {키: 실제로 쓴 값}. 숫자만 비교한다.
    표기만 다른 것(하이픈)과 LLM이 주지 않은 키(검사가 채운 값)는 넣지 않는다."""
    if not isinstance(asked, dict) or not isinstance(used, dict):
        return {}
    from agent import guards
    phones = tuple(getattr(guards, "PHONE_KEYS", ("buyer_cellphone", "customer_phone_number", "phone_number")))
    orders = tuple(getattr(guards, "ORDER_KEYS", ("order_id", "order_number")))
    out: dict[str, str] = {}
    for key in (*orders, *phones):
        a, u = asked.get(key), used.get(key)
        if a in (None, "") or u in (None, ""):
            continue
        da, du = _only_digits(a), _only_digits(u)
        if da and du and da != du:
            out[key] = _dashed_phone(du) if key in phones else str(u)
    return out


# ── 응답기 ───────────────────────────────────────────
class Responder:
    """LLM 스트림을 문장으로 잘라 말하고, 함수 호출은 검사(agent.guards)를 거쳐 실행한다.

    guard: 이번 턴의 GuardContext(함수 정의, 고객 말, 이전 대화, 발신자, 이미 받은 결과). None이면 검사 없이 실행한다.
    respond() 뒤 tool_log에 이번 턴에 처리한 호출이 남는다: [{"name", "arguments", "result", "blocked"}].
    """

    def __init__(self, llm: LLMProvider, tts: TTSProvider, call_tool: CallTool, *,
                 tools: list[dict] | None = None, settings: Settings | None = None,
                 cache: Any = None, normalize_args: NormalizeArgs | None = None, now: str = "",
                 guard: Any = None):
        self.llm = llm
        self.tts = tts
        self.call_tool = call_tool
        self.tools = list(tools or [])
        self.settings = settings or Settings()
        self.cache = cache
        self.now = now
        self.guard = guard
        self.tool_log: list[dict] = []
        self._tool_names = [t.get("function", t).get("name") for t in self.tools if isinstance(t, dict)]
        self._filler_used = False
        self._bridges: list[str] = []               # 이번 턴에 이미 낸 연결 문구
        self._block_reasons: dict[str, str] = {}    # 막힌 호출 id → 검사 사유 코드(AG-T56 번호 되묻기)
        self._heard: dict[str, dict[str, str]] = {}  # AG-T57: {호출 id: 이번 고객 말에서 들은 번호로 조회한 인수}
        self._mask_number = ""                      # AG-T58: 답 속의 [PHONE]을 바꿔 읽을 실제 번호("010, 1234, 5678")
        self._tool_t: float | None = None           # 마지막 함수 결과 수신 시각(perf_counter)
        self._after_any: float | None = None        # 그 뒤 첫 유성 PCM 송신까지(ms, 채움말 포함)
        self._after_answer: float | None = None     # 그 뒤 본 응답 첫 유성 PCM까지(ms)

    def prefetch(self, system: str, messages: list[Message]) -> Prefetch:
        return Prefetch(self.llm.stream(system, list(messages), self.tools))

    # ── 함수 실행 ──
    def _check(self, tc: ToolCall, tlog: TurnLog | None) -> tuple[ToolCall, dict | None]:
        """검사 결과: (정리된 호출, 막았으면 LLM에 돌려줄 결과). 검사기가 없으면 그대로 통과."""
        if self.guard is None:
            return tc, None
        from agent import guards
        v = guards.check(tc.name, tc.arguments, self.guard)
        if v.blocked is not None:
            if tlog:
                tlog.event("tool_blocked", tool=tc.name, reason=v.reason, args=tc.arguments)   # LLM이 보낸 인수(원인 확인용)
            self._block_reasons[tc.id] = v.reason
            return tc, v.blocked
        if tc.name in guards.ACTION_FUNCS:
            self.guard.called.append(tc.name)        # 같은 회차의 두 번째 접수를 막는다
        heard = guards.heard_lookup_numbers(tc.name, v.args, self.guard)
        if heard:
            self._heard[tc.id] = heard
        return ToolCall(tc.id, tc.name, v.args), None

    async def _exec_tool(self, tc: ToolCall, tlog: TurnLog | None) -> dict:
        if tlog:
            tlog.event("tool_call", tool=tc.name, args=tc.arguments)
        try:
            res = await self.call_tool(tc.name, tc.arguments)
        except Exception as e:  # noqa: BLE001
            log.warning("tool %s raised: %r", tc.name, e)
            res = {"status": "error", "message": "tool call failed"}
        if tlog:
            tlog.event("tool_result", tool=tc.name,
                       status=res.get("status") if isinstance(res, dict) else None)
        return res

    async def _run_tools(self, calls: list[ToolCall], tlog: TurnLog | None
                         ) -> tuple[list[Any], list[ToolCall], dict[int, dict[str, str]]]:
        """호출마다 검사 → 통과한 것만 동시에 실행.
        (호출 순서대로의 결과, 실제 실행한 호출, {호출 순번: 검사가 바꿔 실행한 번호 인수})를 돌려준다."""
        results: list[Any] = [None] * len(calls)
        passed: list[tuple[int, ToolCall]] = []
        changed: dict[int, dict[str, str]] = {}
        for i, tc in enumerate(calls):
            checked, blocked = self._check(tc, tlog)
            if blocked is not None:
                results[i] = blocked
                self.tool_log.append({"name": tc.name, "arguments": tc.arguments, "result": blocked, "blocked": True})
            else:
                passed.append((i, checked))
                diff = _number_changes(tc.arguments, checked.arguments)
                if diff:
                    changed[i] = diff
                    if tlog:
                        tlog.event("number_changed", tool=tc.name, keys=sorted(diff))
        done = await asyncio.gather(*(self._exec_tool(tc, tlog) for _, tc in passed))
        for (i, tc), res in zip(passed, done):
            results[i] = res
            entry = {"name": tc.name, "arguments": tc.arguments, "result": res, "blocked": False}
            self.tool_log.append(entry)
            if self.guard is not None:
                self.guard.results.append(entry)
                if tc.name not in self.guard.called:
                    self.guard.called.append(tc.name)
        return results, [tc for _, tc in passed], changed

    def _spoken_phone(self) -> str:
        """이번 고객 말에 유효한 전화번호가 하나뿐이면 끊어 읽을 꼴("010, 1234, 5678"), 아니면 ""."""
        if self.guard is None:
            return ""
        from agent import guards
        from agent.normalize import group_digits
        d = guards.heard_phone(self.guard)
        return group_digits(d, "phone") if d else ""

    # ── 조회 뒤 답(AG-T21) ──
    def _on_audio(self, kind: str, pcm: bytes) -> None:
        if self._tool_t is None or (self._after_any is not None and self._after_answer is not None):
            return
        if not _voiced(pcm):
            return
        ms = round((time.perf_counter() - self._tool_t) * 1000, 1)
        if self._after_any is None:
            self._after_any = ms
        if kind == "tts" and self._after_answer is None:
            self._after_answer = ms

    def _mark_tool_results(self) -> None:
        self._tool_t = time.perf_counter()
        self._after_any = self._after_answer = None

    def _start_filler(self, calls: list[ToolCall], speaker: _Speaker,
                      tlog: TurnLog | None) -> asyncio.Task | None:
        """함수 실행이 시작되면 채움말 타이머를 건다. filler_delay_ms 안에 다음 회차 텍스트가 오지 않으면
        (측정기 중계는 함수 응답을 0.8~1.5초 늦춘다) 캐시 채움말을 재생한다. 턴당 한 번."""
        filler = self.settings.fillers[0] if self.settings.fillers else ""
        if not filler or self.cache is None or self._filler_used or not calls:
            return None

        async def fire() -> None:
            await asyncio.sleep(self.settings.filler_delay_ms / 1000)
            pcm = self.cache.get(filler)
            if pcm and not self._filler_used:
                speaker.play(filler, pcm)
                self._filler_used = True
                if tlog:
                    tlog.event("filler", text=filler)

        return asyncio.create_task(fire())

    def _bridge(self, calls: list[ToolCall], results: list[Any], ran: list[ToolCall], speaker: _Speaker,
                tlog: TurnLog | None) -> str:
        """함수 결과를 받은 즉시 내보내는 캐시 문구(ok면 "네, 확인했어요.", 실패면 정해진 문장). 말한 문구를 돌려준다.
        실제 답(LLM → TTS)은 그 뒤에 이어진다. 캐시에 없으면 말하지 않는다.
        함수를 여러 회차에 걸쳐 부르면 회차마다 낸다(공식 '조회 뒤 답'은 마지막 함수 응답부터 잰다). ok 문구는 같은 말을
        되풀이하지 않게 post_tool_ok → post_tool_more 순으로 아직 안 쓴 것을 고른다. 한 회차의 동시 호출은 한 번만."""
        s = self.settings
        if not getattr(s, "post_tool_bridge", False) or self.cache is None or not ran:
            return ""
        from agent import knowledge
        res = results[next(i for i, tc in enumerate(calls) if tc.id == ran[0].id)]
        status = res.get("status") if isinstance(res, dict) else None
        if status == "ok":
            phrase = next((p for p in (getattr(s, "post_tool_ok", ""), getattr(s, "post_tool_more", ""))
                           if p and p not in self._bridges), "")
        else:
            phrase = knowledge.FAIL_SENTENCES.get(status or "", "")
        pcm = self.cache.get(phrase) if phrase else None
        if not pcm:
            return ""
        speaker.play(phrase, pcm)
        self._bridges.append(phrase)
        if tlog:
            tlog.event("bridge", text=phrase, status=status)
        return phrase

    def _confirm_sentence(self, calls: list[ToolCall], results: list[Any], ran: list[ToolCall]) -> str:
        """AG-T56: 이번 고객 말에서 들은 번호를 되읽어 확인할 문장. 해당 없으면 "".
        - 조회가 번호 문제로 막혔고(missing, unclear_number) 고객 말에 숫자가 7자리 이상 있을 때
        - get_order_info를 이번에 들은 번호로 조회했는데 not_found일 때(발신자·이전 대화 번호면 되묻지 않는다).
          단, 조회 실패 되읽기(AG-T57, DIRECTON_NOTFOUND_READBACK 기본 켬)가 맡는 경우는 넘긴다. 그쪽은 내역이 없다는 것까지 말한다.
        직전 상담원 말이 이미 번호 확인 질문이었으면 되풀이하지 않는다(그 뒤는 LLM 규칙대로 다른 번호를 요청)."""
        if self.guard is None or not getattr(self.settings, "confirm_heard_number", False):
            return ""
        from agent import guards
        from agent.normalize import digits_for_speech
        hist = getattr(self.guard, "history", None) or []
        last_agent = next((h.get("text") or h.get("content") or "" for h in reversed(hist)
                           if isinstance(h, dict) and h.get("role") in ("agent", "assistant")), "")
        if isinstance(last_agent, str) and ("들은 번호" in last_agent or "들었는데" in last_agent):
            return ""
        user_text = getattr(self.guard, "user_text", "") or ""
        ran_by_id = {tc.id: tc for tc in ran}
        for i, tc in enumerate(calls):
            if tc.name not in guards.LOOKUP_FUNCS:
                continue
            res = results[i]
            st = res.get("status") if isinstance(res, dict) else None
            d = ""
            if st == "blocked" and self._block_reasons.get(tc.id) in ("missing", "unclear_number"):
                d = guards.heard_digits(user_text)
            elif st == "not_found" and tc.name == "get_order_info" and tc.id in ran_by_id:
                if _readback_on() and self._heard.get(tc.id):
                    continue                        # main AG-T57이 맡는다: LLM이 "…번으로 조회했는데 내역이 없네요. 맞으실까요?"로 답한다
                h = guards.heard_number(self.guard)
                used = {re.sub(r"\D", "", str(v)) for v in (ran_by_id[tc.id].arguments or {}).values()}
                if h is not None and h[1] in used:
                    d = h[1]
            if len(d) >= 7:
                kind = "phone" if d.startswith("01") and len(d) in (10, 11) else "order"
                return f"제가 들은 번호는 {digits_for_speech(d, kind)}입니다."
        return ""

    async def respond(self, system: str, messages: list[Message], send_audio: AudioSink, *,
                      spoken: list[str] | None = None, prefetch: Prefetch | None = None,
                      log: TurnLog | None = None,
                      play_idle_ms: Callable[[], float] | None = None) -> list[str]:
        """말한 문장 목록을 반환한다(spoken에도 차례로 채운다). LLM·TTS 오류는 그대로 올린다."""
        spoken = [] if spoken is None else spoken
        tlog = log
        msgs = list(messages)
        s = self.settings
        speaker = _Speaker(self.tts, send_audio, spoken, s, tlog, on_audio=self._on_audio, play_idle_ms=play_idle_ms)
        self.tool_log = []
        self._filler_used = False
        self._bridges = []
        self._block_reasons = {}
        self._heard = {}
        self._mask_number = self._spoken_phone()
        self._tool_t = self._after_any = self._after_answer = None
        said_chars = 0
        overflow = False
        executed = False                # 이번 턴에 함수를 실제로 실행했는가
        spoke_after_tool = True         # 마지막 함수 결과 뒤에 답 문장을 말했는가
        answered = 0                    # 이번 턴에 말한 답 문장 수(첫 마디·채움말·연결 문구 제외)
        muted = False                   # 이번 회차에 LLM이 호출을 글로 쓰기 시작했다 → 회차의 남은 글은 인수라 버린다
        filler_task: asyncio.Task | None = None
        rounds = max(1, s.max_tool_rounds)

        pending: list[str] = []         # 묶어서 합성할 문장(회차의 둘째 문장부터)
        round_said = 0

        def say(sent: str) -> bool:
            """문장 하나를 말한다. 상한을 넘으면 False(스트림을 끊는다)."""
            nonlocal said_chars, spoke_after_tool, round_said, answered, muted
            if muted:
                return True
            if json_piece(sent):
                muted = True
                if tlog:
                    tlog.event("json_text_dropped", round=rnd, chars=len(sent))
                return True
            sent = speech_safe(sent, self._tool_names, self._mask_number)
            if not sent:
                return True
            if s.max_response_chars and said_chars + _nchars(sent) > s.max_response_chars:
                return False
            said_chars += _nchars(sent)
            round_said += 1
            answered += 1
            cached = (self.cache.get(sent.strip()) if getattr(s, "answer_cache", False) and self.cache is not None
                      else None)
            if cached:                      # AG-T56: 자주 쓰는 답 문장은 캐시 음성으로 바로(TTS 3초 절약)
                flush_pending()
                speaker.play(sent, cached)
                if tlog:
                    tlog.event("answer_cached", text=sent)
            elif getattr(s, "tts_combine", False) and round_said > 1:
                pending.append(sent)
            else:
                speaker.say(sent)
            spoke_after_tool = True
            return True

        def flush_pending() -> None:
            if pending:
                speaker.say(" ".join(pending))
                pending.clear()

        try:
            for rnd in range(rounds):
                last = rnd == rounds - 1
                muted = False
                if rnd == 0 and prefetch is not None:
                    stream = prefetch.events()
                elif last and any(e.get("blocked") for e in self.tool_log):
                    # 막힌 호출이 있던 턴의 마지막 회차(함수 목록 없음): 다시 부르려 하지 말고 말로 답하라고 일러 준다
                    stream = self.llm.stream(system + FINAL_ROUND_NOTE, msgs, [])
                else:
                    stream = self.llm.stream(system, msgs, [] if last else self.tools)
                splitter = SentenceSplitter(s.min_sentence_chars)
                text_parts: list[str] = []
                calls: list[ToolCall] = []
                it = stream.__aiter__()
                first = True
                try:
                    while True:
                        try:
                            ev = await asyncio.wait_for(it.__anext__(), s.llm_timeout_s)
                        except StopAsyncIteration:
                            break
                        if first:
                            first = False
                            if tlog:
                                tlog.mark("llm_first_token")
                                tlog.event("llm_round_first_event", round=rnd)
                        if ev.type == "text" and ev.text:
                            if filler_task is not None:
                                filler_task.cancel()
                                filler_task = None
                            text_parts.append(ev.text)
                            for sent in splitter.feed(ev.text):
                                if not say(sent):
                                    overflow = True
                                    break
                            if overflow:            # 폭주 생성(같은 말 반복 등): 스트림을 끊고 남은 말은 버린다
                                logging.getLogger(__name__).warning("response exceeded %d chars, stream cut",
                                                                    s.max_response_chars)
                                if tlog:
                                    tlog.event("response_cut", chars=said_chars)
                                break
                            if muted and last:      # 마지막 회차엔 진짜 호출이 올 수 없다: 인수 글을 끝까지 읽지 않는다
                                break
                        elif ev.type == "tool_call" and ev.tool_call is not None:
                            calls.append(ev.tool_call)
                        elif ev.type == "done":
                            break
                        speaker.raise_if_failed()
                finally:
                    aclose = getattr(it, "aclose", None)
                    if aclose is not None:
                        try:
                            await aclose()
                        except BaseException:  # noqa: BLE001
                            pass
                if overflow:
                    flush_pending()
                    break
                for sent in splitter.flush():
                    if not say(sent):
                        break
                flush_pending()
                round_said = 0
                if not calls or last:
                    break
                round_text = "".join(text_parts)
                msgs.append(Message("assistant", round_text, tool_calls=calls))
                if filler_task is None and not round_text.strip():
                    filler_task = self._start_filler(calls, speaker, tlog)
                results, ran, changed = await self._run_tools(calls, tlog)
                bridge = ""
                if ran:
                    executed = True
                    spoke_after_tool = False
                    self._mark_tool_results()
                    bridge = self._bridge(calls, results, ran, speaker, tlog)
                    if bridge and filler_task is not None:   # AG-T56: 결과 문구를 냈으면 채움말은 더 내지 않는다
                        filler_task.cancel()
                        filler_task = None
                elif filler_task is not None:       # 전부 막혔으면 "확인해 볼게요"를 내보내지 않는다
                    filler_task.cancel()
                    filler_task = None
                confirm = self._confirm_sentence(calls, results, ran)
                if confirm:                         # AG-T56: 들은 번호를 되읽어 묻고 이 턴을 끝낸다(LLM 회차 생략)
                    if filler_task is not None:
                        filler_task.cancel()
                        filler_task = None
                    say(confirm)
                    say("맞으실까요?")
                    flush_pending()
                    if tlog:
                        tlog.event("number_confirm", text=confirm)
                    break
                for i, (tc, res) in enumerate(zip(calls, results)):
                    body = res
                    if isinstance(res, dict):       # LLM에 주는 결과에만 메모를 붙인다(tool_log·통화 기록은 원래 결과)
                        extra: dict[str, Any] = {}
                        notes: list[str] = []
                        if bridge and tc.id == ran[0].id:
                            extra["_already_said"] = bridge
                            notes.append(BRIDGE_NOTE)
                            bridge = ""
                        if i in changed:
                            extra["_called_with"] = changed[i]
                            notes.append(CALLED_WITH_NOTE)
                        heard = self._heard.get(tc.id)
                        if heard and _readback_on() and guards_status(res) == "not_found":
                            from agent import guards as _g
                            if any(k in _g.ORDER_KEYS for k in heard):      # 주문번호는 가려지지 않는다: 읽기 쉬운 꼴로 준다
                                extra["_looked_up_with"] = next(_g.format_order_id(d) for k, d in heard.items()
                                                                if k in _g.ORDER_KEYS)
                            else:                                           # 휴대폰 번호: LLM은 표식만, 실제 번호는 서버가 끼운다
                                extra["_looked_up_with"] = MASK_TOKEN
                                self._mask_number = readback_number(heard)
                            notes.append(NOTFOUND_READBACK_NOTE)
                            if tlog:
                                tlog.event("notfound_readback", tool=tc.name, keys=sorted(heard))
                        if extra:
                            body = {**res, **extra, "_note": " ".join(notes)}
                    msgs.append(Message("tool", json.dumps(body, ensure_ascii=False),
                                        tool_call_id=tc.id, name=tc.name))
                speaker.raise_if_failed()
            if filler_task is not None:
                filler_task.cancel()
                filler_task = None
            no_answer_after_tool = executed and not spoke_after_tool
            if s.apology and (no_answer_after_tool or answered == 0):
                # 함수 결과를 받고도 답이 없으면 통화가 끊긴 것처럼 들린다(측정에서도 조회 뒤 답 0점).
                # 호출이 전부 막히고 글도 걸러져 답 문장이 하나도 없는 턴도 같은 문구로 끝낸다(첫 마디만 나가고 끊기지 않게).
                if tlog:
                    tlog.event("empty_after_tool" if no_answer_after_tool else "empty_response")
                pcm = self.cache.get(s.apology) if self.cache is not None else None
                if pcm:
                    speaker.play(s.apology, pcm)
                else:
                    speaker.say(s.apology)
            await speaker.finish()
            self._log_after_tool(tlog)
        except BaseException:
            if filler_task is not None:
                filler_task.cancel()
            speaker.cancel()
            raise
        if not spoken:
            raise ResponderError("empty response")
        return spoken

    def _log_after_tool(self, tlog: TurnLog | None) -> None:
        """턴 로그 after_tool_ms(마지막 툴 결과 → 첫 유성 PCM, 필러 포함)와 after_tool_answer_ms(본 응답만)."""
        if tlog is None or self._tool_t is None:
            return
        tlog.set("after_tool_ms", self._after_any)
        tlog.set("after_tool_answer_ms", self._after_answer)
