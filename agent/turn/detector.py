"""말 끝 결정기. 계약: agent.core.types.TurnDetector

목표: 잘못 끊기 최소 + 무음 1.1초 상한(말 끝 후 1.5초 안에 첫 소리).
순수 함수 decide(signals, config). 상태 없음. 세션이 20ms마다 불러도 된다.

판정 순서
 1. 잡음성 발화(speech_ms < min_speech_ms 이고 STT 텍스트 없음) → 끝 아님(상한도 적용 안 함)
 2. silence < min_silence_ms(200) → 끝 아님
 3. silence ≥ hard_max_ms(1100) → 끝
 4. 대기 시간 wait를 고르고 silence ≥ wait면 끝
    - 텍스트가 연결어미/간투사/숫자 미완                         → hold_ms 1100
    - p 있음: p ≥ 0.7 and end_score ≥ 0                       → fast_ms 400
              단답(네/아니요…) and p ≥ 0.3                     → fast_ms 400
              p < 0.3 or end_score < 0                        → slow_ms 900
              그 밖(애매)                                      → mid_ms 600
    - p 없음(텍스트만): 단답·완결 숫자열 → 400, end_score < 0 → 900, 그 밖 → 600
    - expect_short면 wait −100ms(최소 300ms)
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from agent.core.types import TurnDecision, TurnSignals
import re

from agent.turn.rules import ORDER_DIGITS, SHORT_ANSWERS, STRONG_HOLD, RuleResult, analyze, short_order_id

TEXT_ONLY_FAST_KINDS = frozenset({"short_answer", "digits_complete"})


@dataclass(frozen=True)
class TurnConfig:
    min_silence_ms: int = 200
    hard_max_ms: int = 1100
    fast_ms: int = 400
    mid_ms: int = 600
    slow_ms: int = 900
    hold_ms: int = 1100
    short_step_ms: int = 100       # expect_short일 때 줄이는 양
    short_floor_ms: int = 300      # expect_short 최솟값
    min_speech_ms: int = 250       # 이보다 짧고 텍스트가 없으면 잡음으로 본다
    p_high: float = 0.7
    p_low: float = 0.3


DEFAULT_CONFIG = TurnConfig()


def _fmt_p(p: float | None) -> str:
    return "none" if p is None else f"{p:.2f}"


def wait_ms(s: TurnSignals, c: TurnConfig = DEFAULT_CONFIG) -> tuple[int, str]:
    """이 신호에서 말 끝으로 보려면 필요한 무음 길이(ms)와 근거."""
    r = analyze(s.text)
    p = s.smart_turn_p
    if r.kind in STRONG_HOLD:
        w, why = c.hold_ms, "hold"
    elif p is None:
        if r.kind in TEXT_ONLY_FAST_KINDS:
            w, why = c.fast_ms, "text_fast"
        elif r.score < 0:
            w, why = c.slow_ms, "text_slow"
        else:
            w, why = c.mid_ms, "text_mid"
    elif p >= c.p_high and r.score >= 0:
        w, why = c.fast_ms, "fast"
    elif r.kind == "short_answer" and p >= c.p_low:
        w, why = c.fast_ms, "fast_short_answer"
    elif p < c.p_low or r.score < 0:
        w, why = c.slow_ms, "slow"
    else:
        w, why = c.mid_ms, "mid"
    if s.expect_short:
        w = max(c.short_floor_ms, w - c.short_step_ms)
        why += "+short"
    return w, f"{why}(wait={w} p={_fmt_p(p)} text={r.kind}:{r.score:+.1f})"


def decide(s: TurnSignals, c: TurnConfig = DEFAULT_CONFIG) -> TurnDecision:
    if s.speech_ms < c.min_speech_ms and not (s.text or "").strip():
        return TurnDecision(False, f"noise_short_speech(speech={s.speech_ms}ms)")
    if s.silence_ms < c.min_silence_ms:
        return TurnDecision(False, f"silence<{c.min_silence_ms}")
    if s.silence_ms >= c.hard_max_ms:
        return TurnDecision(True, f"hard_max({c.hard_max_ms}) silence={s.silence_ms}")
    w, why = wait_ms(s, c)
    if s.silence_ms >= w:
        return TurnDecision(True, f"end {why} silence={s.silence_ms}")
    return TurnDecision(False, f"wait {why} silence={s.silence_ms}")


class RuleTurnDetector:
    """TurnDetector 구현. 설정만 들고 있는 얇은 래퍼."""

    def __init__(self, config: TurnConfig = DEFAULT_CONFIG):
        self.config = config

    def decide(self, s: TurnSignals) -> TurnDecision:
        return decide(s, self.config)

    def wait_ms(self, s: TurnSignals) -> tuple[int, str]:
        return wait_ms(s, self.config)


# ── AG-T12 보정 결정기(표 방식) ─────────────────────────
# 텍스트 유형 묶음 × Smart Turn 확률 구간별 대기 시간 표. 판정 순서는 decide()와 같다:
# 잡음성 짧은 발화 → 끝 아님, 침묵 < min_silence → 끝 아님, 침묵 ≥ hard_max → 끝, 침묵 ≥ wait → 끝.
# 값은 eval/turn_calibrate.py가 data/turnset으로 고른다(docs/decisions/turn_calibration.md).
KIND_GROUPS_DEFAULT: tuple[tuple[str, str], ...] = (
    ("short_answer", "strong"), ("question", "strong"), ("digits_complete", "strong"),
    ("final", "final"), ("soft_final", "soft"), ("weak_final", "weak"), ("neutral", "neutral"),
    ("particle", "particle"), ("connective", "hold"), ("filler", "hold"), ("digits_incomplete", "hold"),
    ("digits_partial", "hold"),
)

# ── 숫자 끊어 읽기 보정 ──
# rules.analyze는 ≤4자리 숫자를 weak_final(+0.3)로 본다. 그래서 "이육일일, [쉼] 일오공…"이나 STT "2611"처럼
# 숫자 그룹 사이 쉼에서 빨리 끊었다(2026-11-19 실서버 베이스라인: 말 끝 전 확정 55건 중 digits 21건).
# 표 방식에서는 끝맺음 꼬리(-요/-이요/-입니다/번…) 없이 숫자열로 끝나고 자릿수가 완결(010… 11자리, 0… 9자리,
# 그 밖 ORDER_DIGITS자리 — 기본 15) 미만이면 "digits_partial"(보류)로 바꾼다.
_DIGIT_SYL = "공영일이삼사오육륙칠팔구"
_SYL2DIGIT = {c: str(i) for i, c in enumerate("영일이삼사오육칠팔구")} | {"공": "0", "륙": "6"}
_ARABIC_TOK = re.compile(r"^[0-9][0-9\-.]*$")
_KOR_TOK = re.compile(f"^[{_DIGIT_SYL}]+$")


def digits_partial(text: str) -> bool:
    t = re.sub(r"\s+", " ", text or "").strip()
    if not t or (("?" in t[-3:] or "？" in t[-3:]) and not re.search(r"[0-9][0-9\-.\s]*[?？]\s*$", t)):
        return False                      # 진짜 질문. 숫자 뒤 물음표("2026-09-29?")는 번호를 부르는 중으로 본다
    t = re.sub(r"[\s.,!~…·\"'”’)\]?？]+$", "", t)
    toks = [re.sub(r"[,.]", "", w) for w in t.split(" ")]
    toks = [w for w in toks if w]
    run: list[str] = []
    for w in reversed(toks):
        if _ARABIC_TOK.match(w) or _KOR_TOK.match(w):
            run.insert(0, w)
        else:
            break
    if not run:
        return False
    raw = "".join(run)
    if not any(_ARABIC_TOK.match(w) for w in run) and len(raw) < 3:
        return False                      # "이", "오" 같은 한 글자 단어는 숫자로 보지 않는다
    d = "".join(_SYL2DIGIT.get(c, c) for c in raw if c.isdigit() or c in _SYL2DIGIT)
    if len(d) < 2:
        return False                      # 한 자리 답("3")은 그대로
    if d.startswith("01"):
        return len(d) < 11
    if d.startswith("0"):
        return len(d) < 9
    return len(d) < ORDER_DIGITS and not short_order_id(d)


_END_TAILS = ("이에요", "예요", "입니다", "이요", "요", "번이요", "번이에요", "번")
_JOIN_TAILS = ("에", "이고", "하고", "이랑", "랑")


def digits_complete_with_tail(text: str) -> bool:
    """rules는 "공일공에 1234 5678이요"처럼 '에'로 이어 부른 번호를 마지막 덩어리만 세어 미완으로 본다.
    끝맺음 꼬리(-요/-이요/-입니다…)로 끝나고 앞의 '에'·'이고' 연결까지 합친 자릿수가 완결이면 True."""
    t = re.sub(r"[\s.,!~…·\"'”’)\]]+$", "", re.sub(r"\s+", " ", text or "").strip())
    toks = [re.sub(r"[,.]", "", w) for w in t.split(" ") if w]
    if not toks:
        return False
    last = toks[-1]
    for tail in sorted(_END_TAILS, key=len, reverse=True):
        if last.endswith(tail) and len(last) > len(tail):
            core = last[: -len(tail)]
            if _ARABIC_TOK.match(core) or _KOR_TOK.match(core):
                toks[-1] = core
                break
    else:
        return False
    run: list[str] = []
    for i, w in enumerate(reversed(toks)):
        core = w
        if i > 0:
            for tail in _JOIN_TAILS:
                if w.endswith(tail) and len(w) > len(tail):
                    core = w[: -len(tail)]
                    break
        if _ARABIC_TOK.match(core) or _KOR_TOK.match(core):
            run.insert(0, core)
        else:
            break
    d = "".join(_SYL2DIGIT.get(ch, ch) for ch in "".join(run) if ch.isdigit() or ch in _SYL2DIGIT)
    if d.startswith("01"):
        return len(d) >= 11
    if d.startswith("0"):
        return len(d) >= 9
    return len(d) >= ORDER_DIGITS


# ── AG-T15b: '에'로 이어 부른 번호(끝맺음 꼬리 없어도) ──
# rules는 "공일공에 1234 5678"을 마지막 덩어리만 세어 미완(digits_incomplete)으로, "…일이삼사에 오육칠팔요"를
# ≤4자리+요(weak_final)로 본다. 이어 부른 덩어리를 모두 합쳐 자릿수가 완결이면 완결 번호로 본다.
def digits_join_complete(text: str) -> bool:
    t = re.sub(r"[\s.,!~…·\"'”’)\]]+$", "", re.sub(r"\s+", " ", text or "").strip())
    if not t or "?" in t[-3:]:
        return False
    toks = [re.sub(r"[,.]", "", w) for w in t.split(" ") if w]
    toks = [w for w in toks if w]
    if not toks:
        return False
    last = toks[-1]
    for tail in sorted(_END_TAILS, key=len, reverse=True):
        if last.endswith(tail) and len(last) > len(tail):
            core = last[: -len(tail)]
            if _ARABIC_TOK.match(core) or _KOR_TOK.match(core):
                toks[-1] = core
                break
    if not (_ARABIC_TOK.match(toks[-1]) or _KOR_TOK.match(toks[-1])):
        return False                      # 마지막이 숫자가 아니거나 '에'로 끝남(이어 부르는 중)
    run: list[str] = []
    for i, w in enumerate(reversed(toks)):
        core = w
        if i > 0:
            for tail in sorted(_JOIN_TAILS, key=len, reverse=True):
                if w.endswith(tail) and len(w) > len(tail):
                    core = w[: -len(tail)]
                    break
        if _ARABIC_TOK.match(core) or _KOR_TOK.match(core):
            run.insert(0, core)
        else:
            break
    raw = "".join(run)
    if not any(_ARABIC_TOK.match(w) for w in run) and len(raw) < 3:
        return False
    d = "".join(_SYL2DIGIT.get(ch, ch) for ch in raw if ch.isdigit() or ch in _SYL2DIGIT)
    if d.startswith("01"):
        return len(d) >= 11
    if d.startswith("0"):
        return len(d) >= 9
    return len(d) >= ORDER_DIGITS


# ── AG-T15b: 자기 정정·잠깐 멈춤 ──
# round1(172문항 실서버)에서 "…이일공팔, 아 아니 이일공구요", "취소요. 아 아니다, …"처럼 정정·"잠깐만요" 뒤 쉼에서
# 끊긴 사례. 정정 표현은 "-요/-다"로 끝나 rules가 완결(final)로 본다.
#  - 어디서든 끝에 오면 보류: 잠깐만요·찾아볼게요·다시 말할게요·잘못 말했어요·그게 아니라 …
#  - 절 단위일 때만 보류(앞이 문장부호·숫자이거나 "아" 같은 감탄사 뒤): 아니·아니다·아니야·아니고요·아니에요·다시 할게요
#    ("그건 제 주문이 아니에요.", "결제 다시 할게요."는 완결 문장이므로 제외). 단답("아니요", "아니에요")은 그대로.
_SC_ANYWHERE = ("잠깐만요", "잠시만요", "잠깐만", "잠시만", "찾아볼게요", "찾아 볼게요", "찾아볼게", "확인해 볼게요",
                "확인해볼게요", "다시 말할게요", "다시 말씀드릴게요", "다시 불러 드릴게요", "잘못 말했어요", "잘못 말했다",
                "잘못 말했네요", "잘못 불렀어요", "틀렸다", "틀렸네", "그게 아니라", "그게 아니고", "정정할게요")
_SC_CLAUSE = ("아니고요", "아니에요", "아니야", "아니다", "아니고", "아니", "다시 할게요")
_INTERJ = re.compile(r"(^|[\s,.!?])(아|어|아이|에이|아니)[,.]?$")
_SC_TRAIL = re.compile(r"[\s.,!~…·\"'”’)\]]+$")


def _is_digit_tok(w: str) -> bool:
    """숫자 덩어리("2611", "이육일일", "3일", "270번")."""
    w = w.strip(",.")
    return bool(w) and (w[0].isdigit() or (bool(_KOR_TOK.match(w)) and len(w) >= 2))


def self_correction_end(text: str) -> bool:
    t = re.sub(r"\s+", " ", text or "").strip()
    if not t or "?" in t[-3:] or "？" in t[-3:]:
        return False
    t = _SC_TRAIL.sub("", t)
    # STT 띄어쓰기 흔들림: "아니고 요" → "아니고요", "잘 못 말했다" → "잘못 말했다"
    t = re.sub(r"\s+요$", "요", t).replace("잘 못 ", "잘못 ")
    if not t or t in SHORT_ANSWERS:
        return False
    nospace = t.replace(" ", "")
    for m in _SC_ANYWHERE:
        if nospace.endswith(m.replace(" ", "")):
            return True
    for m in _SC_CLAUSE:
        if not t.endswith(m):
            continue
        pre = t[: -len(m)]
        if pre and not pre[-1] in " ,.":
            return False                  # 낱말 중간("…거 아니야"는 공백 뒤라 아래에서 다시 본다)
        pre = pre.rstrip()
        stripped = _INTERJ.sub(r"\1", pre).rstrip()
        had_interj = stripped != pre
        if not stripped:
            return had_interj             # "아 아니"는 정정, "아니야"·"아니다" 단독은 판단 보류(규칙 그대로)
        if stripped[-1] in ",.!" or had_interj:
            return True
        return _is_digit_tok(stripped.split(" ")[-1])
    return False


def comma_end(text: str) -> bool:
    return (text or "").rstrip().endswith((",", "，", "、"))


# 쉼표로 끝난 완결·애매 텍스트는 이어 말할 가능성이 크다(STT가 쉼표를 붙였다는 것 자체가 문장 중간이라는 신호)
_COMMA_KINDS = frozenset({"final", "soft_final", "weak_final", "neutral", "digits_complete", "short_answer"})


def table_kind(text: str, c: "TableTurnConfig") -> RuleResult:
    r = analyze(text)
    if c.self_corr_hold and self_correction_end(text):
        return RuleResult(-0.8, "self_correction")
    if c.digit_partial_hold and r.kind == "digits_incomplete" and digits_complete_with_tail(text):
        r = RuleResult(1.0, "digits_complete")
    elif c.digit_join_fix and r.kind in ("digits_incomplete", "weak_final", "neutral") and digits_join_complete(text):
        r = RuleResult(1.0, "digits_complete")
    elif c.digit_partial_hold and r.kind in ("weak_final", "neutral", "digits_incomplete") and digits_partial(text):
        return RuleResult(-0.8, "digits_partial")
    if c.comma_hold and r.kind in _COMMA_KINDS and comma_end(text):
        return RuleResult(-0.3, "comma")
    return r


@dataclass(frozen=True)
class TableTurnConfig:
    min_silence_ms: int = 200
    hard_max_ms: int = 1100
    min_speech_ms: int = 250
    short_step_ms: int = 100
    short_floor_ms: int = 300
    # 상한 예외: 이 묶음(예: 연결어미·간투사·숫자 미완 텍스트)은 hard_max 대신 exempt_max_ms까지 기다린다.
    # 진짜 말 끝에서 이런 텍스트가 나오는 일은 드물고(STT 오인식 정도), 1.1초를 넘는 쉼에서 끊지 않게 해 준다.
    exempt_groups: tuple[str, ...] = ()
    exempt_max_ms: int = 2000
    digit_partial_hold: bool = True                       # 숫자 그룹 사이 쉼 보정(digits_partial)
    # AG-T15b(기본 끔, 보정 설정에서 켬): 자기 정정·잠깐 멈춤 → self_correction, 쉼표 끝 → comma,
    # '에'로 이어 부른 완결 번호(꼬리 없어도) → digits_complete
    self_corr_hold: bool = False
    comma_hold: bool = False
    digit_join_fix: bool = False
    p_edges: tuple[float, ...] = (0.3, 0.7)               # p 구간: [0,e0), [e0,e1), ..., [e_last,1]
    speech_edges: tuple[int, ...] = ()                    # 누적 음성(ms) 구간: [0,s0), [s0,s1), ...
    kind_groups: tuple[tuple[str, str], ...] = KIND_GROUPS_DEFAULT
    # 묶음 → 대기 ms 평탄 튜플. 인덱스 = p_bin × (len(speech_edges)+1) + speech_bin
    # (p_bin 0 = p 없음, 1.. = p 구간). speech_edges가 비면 (p 없음, 구간별...)과 같다.
    waits: tuple[tuple[str, tuple[int, ...]], ...] = (
        ("strong", (400, 900, 400, 400)), ("final", (600, 900, 600, 400)), ("soft", (600, 900, 600, 400)),
        ("weak", (600, 900, 600, 400)), ("neutral", (600, 900, 600, 400)), ("particle", (900, 900, 900, 900)),
        ("hold", (1100, 1100, 1100, 1100)),
    )

    def group(self, kind: str) -> str:
        return dict(self.kind_groups).get(kind, "neutral")

    def p_bin(self, p: float | None) -> int:
        """0 = p 없음, 1.. = 구간 번호+1."""
        if p is None:
            return 0
        return 1 + sum(p >= e for e in self.p_edges)

    def s_bin(self, speech_ms: int) -> int:
        return sum(speech_ms >= e for e in self.speech_edges)

    def table_wait(self, kind: str, p: float | None, speech_ms: int = 0) -> int:
        row = dict(self.waits)[self.group(kind)]
        return int(row[self.p_bin(p) * (len(self.speech_edges) + 1) + self.s_bin(speech_ms)])

    def cap_for(self, kind: str) -> int:
        return self.exempt_max_ms if self.group(kind) in self.exempt_groups else self.hard_max_ms


def table_wait_ms(s: TurnSignals, c: TableTurnConfig) -> tuple[int, str]:
    r = table_kind(s.text, c)
    g = c.group(r.kind)
    w = c.table_wait(r.kind, s.smart_turn_p, s.speech_ms)
    why = f"table:{g}/p{c.p_bin(s.smart_turn_p)}s{c.s_bin(s.speech_ms)}"
    if s.expect_short:
        w = max(c.short_floor_ms, w - c.short_step_ms)
        why += "+short"
    return w, f"{why}(wait={w} p={_fmt_p(s.smart_turn_p)} text={r.kind}:{r.score:+.1f})"


def table_decide(s: TurnSignals, c: TableTurnConfig) -> TurnDecision:
    if s.speech_ms < c.min_speech_ms and not (s.text or "").strip():
        return TurnDecision(False, f"noise_short_speech(speech={s.speech_ms}ms)")
    if s.silence_ms < c.min_silence_ms:
        return TurnDecision(False, f"silence<{c.min_silence_ms}")
    cap = c.cap_for(table_kind(s.text, c).kind)
    if s.silence_ms >= cap:
        return TurnDecision(True, f"hard_max({cap}) silence={s.silence_ms}")
    w, why = table_wait_ms(s, c)
    if s.silence_ms >= w:
        return TurnDecision(True, f"end {why} silence={s.silence_ms}")
    return TurnDecision(False, f"wait {why} silence={s.silence_ms}")


class TableTurnDetector:
    """TurnDetector 구현(표 방식). 상태 없음."""

    def __init__(self, config: TableTurnConfig | None = None):
        self.config = config if config is not None else CALIBRATED_CONFIG

    def decide(self, s: TurnSignals) -> TurnDecision:
        return table_decide(s, self.config)

    def wait_ms(self, s: TurnSignals) -> tuple[int, str]:
        return table_wait_ms(s, self.config)

    def cap_ms(self, s: TurnSignals) -> int:
        """이 신호에서 무조건 끝으로 보는 침묵 길이(보통 hard_max_ms)."""
        return self.config.cap_for(table_kind(s.text, self.config).kind)


KIND_GROUPS_CALIBRATED: tuple[tuple[str, str], ...] = (
    ("short_answer", "strong"), ("question", "strong"), ("digits_complete", "strong"),
    ("final", "final"), ("soft_final", "soft"), ("weak_final", "weak"), ("neutral", "neutral"),
    ("particle", "particle"), ("connective", "conn"), ("filler", "filler"),
    ("digits_incomplete", "dinc"), ("digits_partial", "dinc"),
)

# AG-T12 보정 결과(2026-11-19, eval/turn_calibrate.py --text stt, data/turnset train 618발화에서 선택).
# 근거·지표: docs/decisions/turn_calibration.md. Smart Turn은 한국어 통화 음성에서 변별력이 없어(AUC 0.57)
# p 축을 쓰지 않는다(p 없음/있음 열이 같다). 열 = 누적 음성 길이 구간 [0,800) [800,1600) [1600,2400) [2400,∞) ms.
# 연결어미·간투사·조사로 끝난 텍스트(conn/filler/particle)는 상한 1.1초 대신 2초까지 기다린다(긴 쉼 보호).
# 숫자 미완(dinc)은 STT 숫자 오류 때문에 상한 안(≤1.1초)에 둔다.
CALIBRATED_CONFIG_T12 = TableTurnConfig(
    min_silence_ms=200, hard_max_ms=1100, min_speech_ms=250, short_step_ms=100, short_floor_ms=300,
    exempt_groups=("conn", "filler", "particle"), exempt_max_ms=2000, digit_partial_hold=True,
    p_edges=(), speech_edges=(800, 1600, 2400), kind_groups=KIND_GROUPS_CALIBRATED,
    waits=(
        #            p 없음: s0    s1    s2    s3  | p 있음: s0    s1    s2    s3
        ("strong", (750, 950, 300, 300, 750, 950, 300, 300)),
        ("final", (920, 920, 800, 700, 920, 920, 800, 700)),
        ("soft", (1040, 760, 1040, 700, 1040, 760, 1040, 700)),
        ("weak", (940, 950, 680, 600, 940, 950, 680, 600)),
        ("neutral", (960, 920, 950, 900, 960, 920, 950, 900)),
        ("particle", (1800, 1900, 1400, 1600, 1800, 1900, 1400, 1600)),
        ("conn", (1300, 1600, 1700, 1400, 1300, 1600, 1700, 1400)),
        ("filler", (2000, 1700, 2000, 2000, 2000, 1700, 2000, 2000)),
        ("dinc", (1060, 980, 1100, 900, 1060, 980, 1100, 900)),
    ),
)


# ── AG-T15b 2차 보정 ──
# 묶음을 더 쪼갠다: 단답(short)·물음표(question)·완결 숫자(dcomp)를 따로(172문항에서 "번호, [쉼] 요청"과
# "질문? [쉼] 추가 정보"가 말 끝 전 확정의 큰 몫), 자기 정정·잠깐 멈춤(corr), 쉼표 끝(comma) 추가.
KIND_GROUPS_R2: tuple[tuple[str, str], ...] = (
    ("short_answer", "short"), ("question", "question"), ("digits_complete", "dcomp"),
    ("final", "final"), ("soft_final", "soft"), ("weak_final", "weak"), ("neutral", "neutral"),
    ("particle", "particle"), ("connective", "conn"), ("filler", "filler"),
    ("digits_incomplete", "dinc"), ("digits_partial", "dinc"), ("self_correction", "corr"), ("comma", "comma"),
)


def _r2_waits(rows: dict[str, tuple[int, int, int, int]]) -> tuple[tuple[str, tuple[int, ...]], ...]:
    """p 축을 쓰지 않으므로 p 없음/있음 열을 같게 둔다."""
    return tuple((g, tuple(w) + tuple(w)) for g, w in rows.items())


# AG-T15b 채택(2026-11-19): eval/turn_calibrate.py r2 --mode mix --p95-max 1300 --miss-slack 0.01, 상한 1300 변형
# (data/turnset/calibration_r2_mix.json). train = turnset train 732 + 172문항 시뮬레이션, 목적 = false cut 최소
# s.t. 완료 지연 p95 ≤ 1300ms·miss ≤ T12+1%p. 근거·지표: docs/decisions/turn_calibration_r2.md
# 비예외 묶음 상한 1.3초, 연결어미·간투사·조사·정정(corr)은 2초까지. corr는 데이터가 적어 사전값 1800 그대로, comma는 보정 후 손으로 1500(상한 예외)에 고정 —
# 쉼표 끝은 마침표 끝보다 짧게 기다리면 안 되고 STT 텍스트에서 드물다(보정셋 지점 0.2%).
CALIBRATED_CONFIG = TableTurnConfig(
    min_silence_ms=200, hard_max_ms=1300, min_speech_ms=250, short_step_ms=100, short_floor_ms=300,
    exempt_groups=("conn", "filler", "particle", "corr", "comma"), exempt_max_ms=2000, digit_partial_hold=True,
    self_corr_hold=True, comma_hold=True, digit_join_fix=True,
    p_edges=(), speech_edges=(800, 1600, 2400), kind_groups=KIND_GROUPS_R2,
    waits=_r2_waits({
        #             s0    s1    s2    s3
        "short": (850, 1050, 300, 300),
        "question": (600, 950, 1100, 600),
        "dcomp": (750, 950, 850, 900),
        "final": (1200, 1250, 1000, 700),
        "soft": (1040, 850, 1300, 750),
        "weak": (940, 1300, 1300, 600),
        "neutral": (960, 1100, 1150, 950),
        "particle": (1800, 1900, 1700, 1600),
        "conn": (1300, 1600, 1700, 1400),
        "filler": (2000, 1700, 2000, 2000),
        "dinc": (1060, 1150, 1250, 1050),
        "corr": (1800, 1800, 1800, 1800),
        "comma": (1500, 1500, 1500, 1500),   # 쉼표 끝: 마침표 끝(최대 1300)보다 길게, 상한 예외
    }),
)


def make_detector() -> TableTurnDetector:
    """서버 build_deps가 세션마다 부르는 팩토리. 보정 설정(CALIBRATED_CONFIG)을 쓴다.

    AG-T24: DIRECTON_JEV_TURN=1이고 TYPESAFE_API_KEY가 있으면 Jev 턴 판정을 얹은 래퍼(JevTurnDetector)를 돌려준다
    (기본 꺼짐, 근거 docs/decisions/jev_turn.md). 꺼져 있으면 이전과 똑같다."""
    base = TableTurnDetector(CALIBRATED_CONFIG)
    if os.environ.get("DIRECTON_JEV_TURN", "").strip() == "1":
        from agent.turn import jev_turn
        judge = jev_turn.shared_judge()
        if judge is not None:
            return jev_turn.JevTurnDetector(base, judge)  # type: ignore[return-value]
    return base
