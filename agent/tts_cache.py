"""고정 문구 → PCM 캐시(assets/tts_cache/<hash>.pcm). 첫 마디·필러·사과 문구용.
첫 마디 정책(AG-T20)은 이 파일 끝의 choose_opener / choose_filler(순수 함수).

레벨(AG-T09): 측정기는 바이너리 메시지 단위로 RMS ≥ 300을 '소리'로 본다. 그래서
- 합성해 캐시에 넣는 PCM은 음성 구간 RMS를 TARGET_RMS(3000, 약 -21dBFS)로 맞추고(피크 한도로 클리핑 방지),
  앞 무음을 자른 뒤 첫 송신 청크(100ms)의 RMS가 FIRST_CHUNK_MIN_RMS 이상이 되도록 느린 시작을 더 자른다.
- 실시간 문장 TTS는 LevelTTS로 감싸 같은 레벨로 맞춘다(문장마다 잰 레벨의 이동 평균으로 고정 이득).
put()은 받은 바이트를 그대로 저장한다(앞 무음 자르기만). 정규화는 합성 경로(warm·get_or_synth)에서 한다.
"""
from __future__ import annotations

import asyncio

import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Mapping, Sequence

import numpy as np

from agent.config import Settings
from agent.core.types import FRAME_BYTES, SessionInfo, TTSProvider

log = logging.getLogger(__name__)

TRIM_RMS = 200.0     # 앞쪽 무음 자르기 기준(20ms 프레임 RMS)
TARGET_RMS = 3000.0  # 음성 구간 목표 RMS(테스트 음성과 같은 기준, eval/build_testset.py)
PEAK_LIMIT = 30000.0
MEASURE_RMS = 300.0  # 측정기 무음 기준
FIRST_CHUNK_MIN_RMS = 2 * MEASURE_RMS   # 여유를 두고 첫 청크가 이 이상이게
MAX_ONSET_TRIM_MS = 300                 # 느린 시작을 더 자르는 최대 길이


def _rms(b: bytes) -> float:
    a = np.frombuffer(b[: len(b) - len(b) % 2], dtype=np.int16).astype(np.float32)
    return float(np.sqrt(np.mean(a * a))) if len(a) else 0.0


def trim_leading_silence(pcm: bytes, threshold: float = TRIM_RMS) -> bytes:
    """첫 프레임부터 유성음이 되게 앞 무음 프레임을 자른다. 전부 무음이면 원본."""
    for off in range(0, len(pcm), FRAME_BYTES):
        if _rms(pcm[off:off + FRAME_BYTES]) >= threshold:
            return pcm[off:]
    return pcm


def _i16(pcm: bytes) -> tuple[np.ndarray, bytes]:
    n = len(pcm) // 2 * 2
    return np.frombuffer(pcm[:n], dtype=np.int16), pcm[n:]


def _frame_rms(a: np.ndarray, n: int = FRAME_BYTES // 2) -> np.ndarray:
    m = len(a) // n
    if m == 0:
        return np.zeros(0)
    f = a[: m * n].astype(np.float64).reshape(m, n)
    return np.sqrt(np.mean(f * f, axis=1))


def speech_rms(pcm: bytes) -> float:
    """음성 구간(20ms 프레임 RMS가 상위 레벨의 10% 이상이고 30 이상)의 RMS. 음성이 없으면 0."""
    a, _ = _i16(pcm)
    fr = _frame_rms(a)
    if fr.size == 0:
        return _rms(pcm)
    ref = float(np.percentile(fr, 95))
    voiced = fr >= max(30.0, 0.1 * ref)
    if not voiced.any():
        return 0.0
    return float(np.sqrt(np.mean(fr[voiced] ** 2)))


def apply_gain(pcm: bytes, gain: float, peak_limit: float = PEAK_LIMIT) -> bytes:
    a, rest = _i16(pcm)
    if gain == 1.0 or a.size == 0:
        return pcm
    y = np.clip(np.round(a.astype(np.float64) * gain), -peak_limit, peak_limit).astype(np.int16)
    return y.tobytes() + rest


def normalize_pcm(pcm: bytes, target_rms: float = TARGET_RMS, peak_limit: float = PEAK_LIMIT) -> bytes:
    """음성 구간 RMS를 target_rms로. 피크가 peak_limit를 넘으면 그만큼 이득을 낮춘다. 무음이면 그대로."""
    r = speech_rms(pcm)
    a, _ = _i16(pcm)
    if r <= 0 or a.size == 0:
        return pcm
    g = target_rms / r
    peak = float(np.abs(a.astype(np.float64)).max())
    if peak * g > peak_limit:
        g = peak_limit / peak
    return apply_gain(pcm, g, peak_limit)


def ensure_first_chunk(pcm: bytes, chunk_bytes: int = 3200, min_rms: float = FIRST_CHUNK_MIN_RMS,
                       max_trim_ms: int = MAX_ONSET_TRIM_MS) -> bytes:
    """앞 무음을 자르고, 첫 송신 청크 RMS가 min_rms 미만이면 느린 시작을 프레임 단위로 더 자른다."""
    pcm = trim_leading_silence(pcm)
    max_cut = max_trim_ms * 32                     # 16kHz × 2바이트
    cut = 0
    while cut < max_cut and len(pcm) - cut > chunk_bytes and _rms(pcm[cut:cut + chunk_bytes]) < min_rms:
        cut += FRAME_BYTES
    return pcm[cut:]


def prepare_cached_pcm(pcm: bytes, chunk_bytes: int = 3200, target_rms: float = TARGET_RMS) -> bytes:
    """합성 결과 → 캐시용 PCM(레벨 정규화 + 첫 청크 유성음 보장)."""
    if target_rms and target_rms > 0:
        pcm = normalize_pcm(pcm, target_rms)
    return ensure_first_chunk(pcm, chunk_bytes)


class LevelTTS:
    """TTSProvider 래퍼: 실시간 문장 합성 출력의 음성 레벨을 target_rms로 맞춘다.

    아직 레벨을 모르면 처음 probe_bytes(300ms)를 모아 이득을 정하고, 문장이 끝나면 그 문장 전체 레벨을
    이동 평균(est)에 반영한다. 그 뒤 문장은 est로 이득을 정해 덩어리를 바로 흘려보낸다(TTFB 유지).
    서버 시작 때 캐시 warm이 이 래퍼를 거치므로 첫 실시간 문장부터 버퍼링이 없다.
    """

    def __init__(self, inner: Any, target_rms: float = TARGET_RMS, peak_limit: float = PEAK_LIMIT,
                 probe_bytes: int = 9600, alpha: float = 0.3, min_gain: float = 1 / 16, max_gain: float = 16.0):
        self.inner = inner
        self.target_rms = target_rms
        self.peak_limit = peak_limit
        self.probe_bytes = probe_bytes
        self.alpha = alpha
        self.min_gain, self.max_gain = min_gain, max_gain
        self.est: float | None = None

    def __getattr__(self, name: str) -> Any:
        if name == "inner":
            raise AttributeError(name)
        return getattr(self.inner, name)

    def _gain(self, r: float) -> float:
        if r <= 0:
            return 1.0
        return min(self.max_gain, max(self.min_gain, self.target_rms / r))

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        raw: list[bytes] = []
        gain = None if self.est is None else self._gain(self.est)
        pending = b""
        carry = b""

        def out(b: bytes) -> bytes:
            nonlocal carry
            b = carry + b
            n = len(b) // 2 * 2
            carry = b[n:]
            return apply_gain(b[:n], gain or 1.0, self.peak_limit)

        async for chunk in self.inner.synthesize(text):
            raw.append(chunk)
            if gain is None:
                pending += chunk
                if len(pending) < self.probe_bytes:
                    continue
                r = speech_rms(pending)
                if r <= 0 and len(pending) < 4 * self.probe_bytes:
                    continue                          # 아직 무음뿐이면 조금 더 모은다
                gain = self._gain(r)
                chunk, pending = pending, b""
            data = out(chunk)
            if data:
                yield data
        if pending:
            if gain is None:
                gain = self._gain(speech_rms(pending))
            data = out(pending)
            if data:
                yield data
        if carry:
            yield carry
        r_all = speech_rms(b"".join(raw))
        if r_all > 0:
            self.est = r_all if self.est is None else (1 - self.alpha) * self.est + self.alpha * r_all


async def synthesize_all(tts: TTSProvider, text: str) -> bytes:
    chunks = []
    async for c in tts.synthesize(text):
        chunks.append(c)
    return b"".join(chunks)


WARM_CONCURRENCY = 4


class TTSCache:
    def __init__(self, cache_dir: str | Path = "assets/tts_cache", voice: str = "default",
                 chunk_bytes: int = 3200, target_rms: float = TARGET_RMS):
        self.dir = Path(cache_dir)
        self.voice = voice
        self.chunk_bytes = chunk_bytes
        self.target_rms = target_rms
        self._mem: dict[str, bytes] = {}

    def key(self, text: str) -> str:
        return hashlib.sha1(f"{self.voice}\n{text}".encode("utf-8")).hexdigest()[:16]

    def path(self, text: str) -> Path:
        return self.dir / f"{self.key(text)}.pcm"

    def get(self, text: str) -> bytes | None:
        if text in self._mem:
            return self._mem[text]
        p = self.path(text)
        if p.exists():
            data = p.read_bytes()
            self._mem[text] = data
            return data
        return None

    def put(self, text: str, pcm: bytes) -> bytes:
        pcm = trim_leading_silence(pcm)
        self._mem[text] = pcm
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path(text).write_bytes(pcm)
        return pcm

    async def get_or_synth(self, text: str, tts: TTSProvider) -> bytes:
        cached = self.get(text)
        if cached is not None:
            return cached
        return self.put(text, self._prepare(await synthesize_all(tts, text)))

    def _prepare(self, pcm: bytes) -> bytes:
        return prepare_cached_pcm(pcm, self.chunk_bytes, self.target_rms)

    async def warm(self, phrases: list[str] | tuple[str, ...], tts: TTSProvider) -> int:
        """없는 문구만 합성해 저장한다. 합성한 개수를 반환(실패는 경고만)."""
        todo = [t for t in dict.fromkeys(phrases) if t and self.get(t) is None]
        sem = asyncio.Semaphore(WARM_CONCURRENCY)       # 게이트웨이 TTS는 문장당 2~3초 → 동시에 합성한다

        async def one(text: str) -> int:
            async with sem:
                try:
                    self.put(text, self._prepare(await synthesize_all(tts, text)))
                    return 1
                except Exception as e:  # noqa: BLE001
                    log.warning("tts cache warm failed for %r: %r", text, e)
                    return 0

        return sum(await asyncio.gather(*(one(t) for t in todo)))


def select_opener(info: SessionInfo, settings: Settings) -> str:
    """첫 마디 선택. 기본은 이름 없는 캐시 문구."""
    name = ((info.caller or {}).get("name") or "").strip()
    if settings.opener_use_name and name:
        return f"네, {name} 고객님."
    if info.history and len(settings.openers) > 1:
        return settings.openers[1]
    return settings.openers[0]


# ── 텍스트 기반 첫 마디(AG-T10c) ───────────────────────
# 확정 시점 STT 텍스트의 키워드로 고른다. 사과가 먼저(불만이면서 조회 요청인 말은 사과가 낫다).
APOLOGY_KEYWORDS = (
    "불편", "화나", "화가", "짜증", "실망", "최악", "엉망", "어이없", "황당", "너무하", "답답",
    "파손", "깨져", "깨졌", "깨진", "찢어", "찢겨", "고장", "불량", "하자", "망가", "부서",
    "다쳤", "다쳐", "데었", "데여", "상처", "오배송", "잘못 왔", "잘못 배송", "잘못 보내", "누락", "빠져 있",
    "아직도", "안 왔", "안왔", "안 와요", "안와요", "안 오", "못 받", "늦어", "늦게", "몇 번", "항의", "책임",
)
LOOKUP_KEYWORDS = (
    "조회", "확인해", "확인 좀", "확인하고", "알아봐", "언제 와", "언제 도착", "언제 오", "어디쯤", "배송 상태",
    "배송 조회", "주문 내역", "주문한", "주문했", "취소", "반품", "교환", "환불", "재고", "쿠폰", "주소",
    "배송지", "포인트", "적립", "콜백", "전화 주",
)
_LONG_NUMBER = re.compile(r"\d{4,}|[공영일이삼사오육칠팔구]{4,}")


def select_opener_for_text(text: str, settings: Settings, default: str | None = None) -> str:
    """확정 시점 텍스트 → 첫 마디. 불만·사과 필요 → opener_apology, 조회·처리 요청 → opener_lookup, 그 밖 → default."""
    base = default if default is not None else settings.openers[0]
    t = (text or "").strip()
    if not settings.opener_by_text or not t:
        return base
    if settings.opener_apology and any(k in t for k in APOLOGY_KEYWORDS):
        return settings.opener_apology
    if settings.opener_lookup and (any(k in t for k in LOOKUP_KEYWORDS) or _LONG_NUMBER.search(t.replace(" ", ""))):
        return settings.opener_lookup
    return base


# ── 첫 마디 재설계(AG-T20) ───────────────────────────
# 공식 사이트: "모든 응답을 채움말로 시작"은 시스템 점수만 높이는 설계, "모든 답변에 같은 채움말 반복"은 UX 감점.
# 그래서 (1) 선호출 첫 문장이 이미 합성돼 있으면 첫 마디 없이 바로 답하고, (2) 답이 곧 나오지 않을 때만
# 상황별 짧은 첫 마디를 쓰되, (3) 직전에 쓴 첫 마디는 다시 쓰지 않는다(가능하면 최근 두 개도 피한다).
OPENER_KINDS = ("apology", "lookup", "simple")
OPENER_VARIANTS: dict[str, tuple[str, ...]] = {
    "apology": ("아, 정말 죄송합니다.", "많이 불편하셨겠어요, 죄송합니다."),
    "lookup": ("네, 바로 찾아볼게요.", "알겠습니다, 조회해 볼게요."),
    # 중립 첫 마디는 무엇을 하겠다는 약속이 없는 짧은 말만(AG-U05: "괜찮아요"에 "네, 안내해 드릴게요."가 나가 어긋났다)
    "simple": ("네.", "아, 네."),
}


@dataclass(frozen=True)
class OpenerChoice:
    text: str | None        # None이면 첫 마디 없음
    kind: str               # apology | lookup | simple
    reason: str             # answer_ready | not_ready | tool_expected | fallback_simple | no_fresh_opener


# AG-U05 번호를 물어야만 진행되는 주문·배송 요청(정책 질문은 빼고 LLM에 맡긴다)
ASK_NUMBER_KEYWORDS = (
    "어디쯤", "언제 와", "언제 오", "언제 도착", "배송 조회", "배송 상태", "주문 조회", "주문 확인", "주문 내역",
    "취소하고", "취소해", "취소할", "반품하고", "반품해", "반품할", "환불하고", "환불해", "교환하고", "교환해",
    "입금 확인", "입금했",
)
POLICY_MARKERS = ("며칠", "몇 일", "가능", "비용", "얼마", "방법", "어떻게", "기간", "규정", "배송비", "되나요", "돼요", "되죠")


def needs_number_only(text: str) -> bool:
    """번호만 있으면 진행되는 주문·배송·취소·반품 요청인가(정책·가능 여부 질문, 번호가 들어 있는 말은 아니다)."""
    t = (text or "").strip()
    if not t or has_long_number(t) or any(k in t for k in APOLOGY_KEYWORDS):
        return False
    return any(k in t for k in ASK_NUMBER_KEYWORDS) and not any(m in t for m in POLICY_MARKERS)


def has_long_number(text: str) -> bool:
    """주문번호·전화번호로 쓸 만한 숫자(4자리 이상, 한글 읽기 포함)가 있는지."""
    return bool(_LONG_NUMBER.search((text or "").replace(" ", "")))


def opener_kind(text: str, settings: Settings, has_identifier: bool = True) -> str:
    """확정 시점 텍스트 → 상황(select_opener_for_text와 같은 키워드). 불만 → apology, 조회·처리 → lookup, 그 밖 → simple.
    has_identifier=False(발신자 번호·이전 대화 번호·이번 말의 번호가 모두 없음)면 조회 요청이어도 simple:
    곧바로 번호를 되물을 턴이라 "확인해 드릴게요"로 시작하면 말이 어긋난다(데모 피드백 DEMO-20261121-162448)."""
    t = (text or "").strip()
    if not t:
        return "simple"
    if any(k in t for k in APOLOGY_KEYWORDS):
        return "apology"
    if any(k in t for k in LOOKUP_KEYWORDS) or has_long_number(t):
        return "lookup" if has_identifier or has_long_number(t) else "simple"
    return "simple"


def _dedup(items) -> tuple[str, ...]:
    out: list[str] = []
    for p in items:
        if p and p not in out:
            out.append(p)
    return tuple(out)


def opener_candidates(kind: str, settings: Settings) -> tuple[str, ...]:
    """상황별 첫 마디 후보. 설정 문구(opener_apology / opener_lookup / openers)가 먼저, 그다음 변형."""
    head: tuple[str, ...] = {"apology": (settings.opener_apology,),
                             "lookup": (settings.opener_lookup,)}.get(kind, tuple(settings.openers))
    return _dedup((*head, *OPENER_VARIANTS.get(kind, ())))


def opener_phrases(settings: Settings) -> list[str]:
    """첫 마디 후보 전체(캐시 예열용). 서버 시작 예열(cached_phrases)에 없는 것은 세션이 뒤에서 채운다."""
    return list(_dedup(p for k in OPENER_KINDS for p in opener_candidates(k, settings)))


def choose_opener(kind: str, *, answer_ready: bool, tool_expected: bool = False, recent: Sequence[str] = (),
                  candidates: Mapping[str, Sequence[str]], is_cached: Callable[[str], bool] = lambda _t: True,
                  preferred: str | None = None) -> OpenerChoice:
    """첫 마디 정책(순수 함수).

    - answer_ready(선호출 첫 문장이 합성돼 있고 최종 텍스트와 일치) → 첫 마디 없음. 그 문장이 곧 첫 소리다.
    - 아니면 첫 소리 1.5초를 지키려고 첫 마디를 쓴다. 함수 호출이 예상되면 단순 문의도 조회 첫 마디.
    - 캐시에 있는 후보만(확정 즉시 보내야 한다). 직전 첫 마디(recent[-1])는 쓰지 않고, 최근 두 개는 가능하면 피한다.
    - preferred(선호출 프롬프트에 넣은 첫 마디)가 허용되면 그것(프롬프트의 "이미 '…'라고 말했다"와 맞춘다).
    - 그 상황 후보가 없으면 simple 후보, 그것도 없으면 첫 마디 없음(같은 말 반복보다 낫다).
    """
    if answer_ready:
        return OpenerChoice(None, kind, "answer_ready")
    if tool_expected and kind == "simple":
        kind = "lookup"
    last = recent[-1] if recent else None
    reason = "tool_expected" if tool_expected else "not_ready"

    def pool(k: str) -> list[str]:
        return [c for c in candidates.get(k, ()) if c and c != last and is_cached(c)]

    p = pool(kind)
    if not p and kind != "simple":
        p, reason = pool("simple"), "fallback_simple"
    if not p:
        return OpenerChoice(None, kind, "no_fresh_opener")
    if preferred in p:
        return OpenerChoice(preferred, kind, reason)
    fresh = [c for c in p if c not in recent[-2:]]
    return OpenerChoice((fresh or p)[0], kind, reason)


_WORD = re.compile(r"[가-힣A-Za-z0-9]+")


def _stems(text: str) -> set[str]:
    return {w[:2] for w in _WORD.findall(text or "") if len(w) >= 2}


def choose_filler(candidates: Sequence[str], *, opener: str, last: str | None,
                  is_cached: Callable[[str], bool] = lambda _t: True) -> str | None:
    """툴 대기 필러 선택(순수 함수). 직전 필러와 다르고 이번 첫 마디와 낱말이 겹치지 않는 것이 먼저
    ("네, 확인해 드릴게요." 뒤 "확인해 드릴게요, 잠시만요." 방지). 후보가 하나뿐이면 반복이 침묵보다 낫다."""
    pool = [c for c in _dedup(candidates) if is_cached(c)]
    if not pool:
        return None
    op = _stems(opener)
    for ok in (lambda c: c != last and not (_stems(c) & op), lambda c: c != last, lambda c: not (_stems(c) & op)):
        for c in pool:
            if ok(c):
                return c
    return pool[0]
