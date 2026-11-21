"""한국어 STT 텍스트로 발화가 끝났는지 추정하는 규칙.

end_score(text) ∈ [-1, 1]: +1 끝남 확실 … 0 모름 … −1 보류(이어 말할 것).
형태소 분석기 없이 어말 문자열만 본다. 오판이 나도 결정기의 하드 상한(1.1초)이 있으므로
"보류"는 늦어질 뿐이고, 위험한 것은 "끝남" 오판이다. 그래서 애매하면 보류/약한 끝남 쪽으로 둔다.

kind 목록과 점수
  short_answer      +1.0  네/아니요/예/맞아요 …
  question          +1.0  물음표
  digits_complete   +1.0  숫자열만, 자릿수 완결(010… 11자리, 주문번호 ORDER_DIGITS자리 — 기본 15)
  final             +0.8  -요/-다/-까/-죠/-니다/-세요
  soft_final        +0.5  -는데요/-거든요
  weak_final        +0.3  조사·연결어미 + 요("주문번호가요", "저는요", "해서요"), 짧은 숫자(≤4자리)
  neutral            0.0  판단 근거 없음(빈 문자열, 명사구)
  particle          −0.5  조사로 끝남(-가/-를/-은/-는/-에서 …)
  connective        −0.8  연결어미(-고/-서/-면/-는데/-니까/-지만 …), 접속사(그리고/그래서 …)
  filler            −0.8  간투사(어/음/그/저기 …)
  digits_incomplete −0.8  숫자를 부르다 만 경우(자릿수 미달, "010에")
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

SCORES = {
    "short_answer": 1.0, "question": 1.0, "digits_complete": 1.0,
    "final": 0.8, "soft_final": 0.5, "weak_final": 0.3, "neutral": 0.0,
    "particle": -0.5, "connective": -0.8, "filler": -0.8, "digits_incomplete": -0.8,
}
ORDER_DIGITS = int(os.environ.get("DIRECTON_ORDER_DIGITS", "15"))   # 이만큼 불러야 주문번호가 끝난 것으로 본다(공식 YYYYMMDD-NNNNNNN)
STRONG_HOLD = frozenset({"connective", "filler", "digits_incomplete"})


@dataclass(frozen=True)
class RuleResult:
    score: float
    kind: str


SHORT_ANSWERS = frozenset({
    "네", "예", "넵", "네네", "예예", "응", "어어", "아니요", "아니오", "아뇨", "아니", "아니에요",
    "맞아요", "맞습니다", "맞아", "그래요", "좋아요", "괜찮아요", "됐어요", "알겠습니다", "알겠어요",
    "네 맞아요", "네 맞습니다", "예 맞아요", "네 그래요", "아니요 괜찮아요", "네 알겠습니다",
})
FILLERS = frozenset({"어", "음", "그", "저", "저기", "저기요", "아", "에", "흠", "엄", "으", "그게", "뭐지",
                     "어어어", "음음", "그러니까요", "있잖아요", "있잖아", "혹시", "뭐냐면", "뭐랄까"})
CONJUNCTIONS = frozenset({"그리고", "그래서", "그런데", "근데", "그러면", "그럼", "그러니까", "아니면",
                          "그리고요", "그래서요", "또", "및", "그러다가", "그다음에", "다음에", "그러고"})

# 연결어미(요 없음): 접미 문자열 → connective
CONNECTIVE_SUFFIXES = (
    "는데", "은데", "인데", "한데", "던데", "ㄴ데",
    "니까", "으니까", "지만", "다가", "거나", "든지", "려고", "는지", "은지", "인지", "을지", "도록",
    "해서", "어서", "아서", "여서", "라서", "워서", "와서", "져서", "가서", "봐서", "돼서", "줘서",
    "나서", "써서", "해가지고", "가지고", "갖고",
    "하고", "있고", "없고", "했고", "되고", "싶고", "이고", "고",
    "하면", "으면", "다면", "라면", "이면", "되면", "있으면", "없으면", "면",
)
NOUN_EXCEPTIONS = frozenset({
    "재고", "창고", "광고", "최고", "사고", "신고", "참고", "입고", "출고", "반고",
    "라면", "화면", "측면", "앞면", "뒷면", "양면", "표면", "냉면", "정면", "이면",
})
# 조사(요 없음): 긴 것부터
PARTICLES = ("에서는", "에서", "에게", "한테", "으로", "부터", "까지", "이랑", "랑", "처럼", "보다",
             "께서", "가", "를", "을", "은", "는", "에", "로", "도", "만", "와", "과", "의")
FINAL_SUFFIXES = ("니다", "세요", "셔요", "죠", "지요", "까", "요", "다")
SOFT_FINAL_SUFFIXES = ("는데요", "은데요", "인데요", "던데요", "거든요")

_DIGIT_SYL = "공영일이삼사오육륙칠팔구"
_SYL2DIGIT = {c: str(i) for i, c in enumerate("영일이삼사오육칠팔구")} | {"공": "0", "륙": "6"}
_ARABIC_TOKEN = re.compile(r"^[0-9][0-9\-.]*$")
_KOR_DIGIT_TOKEN = re.compile(f"^[{_DIGIT_SYL}]+$")
# 숫자 뒤 꼬리: 이어짐 / 끝남
_DIGIT_CONT_TAILS = ("에", "이고", "하고", "이랑", "랑", "다음에")
_DIGIT_END_TAILS = ("이에요", "예요", "입니다", "이요", "요", "번", "번이요", "번이에요")
_PUNCT_TRAIL = re.compile(r"[\s.,!~…·\"'”’)\]]+$")


STRONG_YO = ("에요", "예요", "어요", "아요", "해요", "워요", "져요", "돼요", "세요", "셔요",
             "네요", "래요", "대요", "나요", "까요", "게요", "죠요")


def _jong(ch: str) -> int:
    """한글 음절의 종성 인덱스(0=받침 없음, 4=ㄴ, 17=ㅂ). 한글이 아니면 -1."""
    o = ord(ch) - 0xAC00
    return o % 28 if 0 <= o < 11172 else -1


def _is_n_ga_yo(word: str) -> bool:
    """-ㄴ가요/-는가요/-던가요(가능한가요, 되는 건가요): 의문 종결."""
    return word.endswith("가요") and len(word) >= 3 and _jong(word[-3]) == 4


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _strip_tail(token: str, tails: tuple[str, ...]) -> tuple[str, str]:
    for t in sorted(tails, key=len, reverse=True):
        if token.endswith(t) and len(token) > len(t):
            return token[: -len(t)], t
    return token, ""


# 번호를 부르며 끝을 올려 읽으면 STT가 "2026-09-29?"처럼 물음표를 붙인다 → 질문이 아니라 번호를 부르는 중으로 본다
_DIGITS_THEN_Q = re.compile(r"(?:^|\s)[0-9][0-9\-.\s]*[?？]\s*$")


def short_order_id(d: str) -> bool:
    """연도 앞 두 자리를 뺀 주문번호(YYMMDD + 7자리 = 13자리, 예: 260929 0004550)인가."""
    if len(d) != 13 or not d.isdigit() or d[0] != "2":
        return False
    mm, dd = int(d[2:4]), int(d[4:6])
    return 1 <= mm <= 12 and 1 <= dd <= 31


def _digits_result(tokens: list[str]) -> RuleResult | None:
    """끝부분이 숫자열이면 자릿수로 판정. 숫자로 끝나지 않으면 None."""
    if not tokens:
        return None
    last = tokens[-1]
    tail_kind = ""
    core, tail = _strip_tail(last, _DIGIT_CONT_TAILS)
    if tail and (_ARABIC_TOKEN.match(core) or _KOR_DIGIT_TOKEN.match(core)):
        tail_kind = "cont"
    else:
        core, tail = _strip_tail(last, _DIGIT_END_TAILS)
        if tail and (_ARABIC_TOKEN.match(core) or _KOR_DIGIT_TOKEN.match(core)):
            tail_kind = "end"
        else:
            core = last
    run = [core]
    for tok in reversed(tokens[:-1]):
        run.insert(0, tok)
        if not (_ARABIC_TOKEN.match(tok) or _KOR_DIGIT_TOKEN.match(tok)):
            run.pop(0)
            break
    if not all(_ARABIC_TOKEN.match(t) or _KOR_DIGIT_TOKEN.match(t) for t in run):
        return None
    has_arabic = any(_ARABIC_TOKEN.match(t) for t in run)
    raw = "".join(run)
    if not has_arabic and len(raw) < 3:          # "이", "오" 같은 한 글자는 숫자로 보지 않는다
        return None
    digits = "".join(_SYL2DIGIT.get(c, c) for c in raw if c.isdigit() or c in _SYL2DIGIT)
    n = len(digits)
    if tail_kind == "cont":
        return RuleResult(SCORES["digits_incomplete"], "digits_incomplete")
    if digits.startswith("01"):
        complete = n >= 11
    elif digits.startswith("0"):
        complete = n >= 9                         # 지역번호
    elif n <= 4:
        return RuleResult(SCORES["weak_final"], "weak_final")   # "3", "1234"(뒷자리) 등
    else:
        complete = n >= ORDER_DIGITS or short_order_id(digits)   # 주문번호 자릿수(공식: 8자리-7자리, 또는 YYMMDD+7)
    if complete:
        return RuleResult(SCORES["digits_complete"], "digits_complete")
    return RuleResult(SCORES["digits_incomplete"], "digits_incomplete")


def _ends_with_particle(word: str) -> bool:
    for p in PARTICLES:
        if word.endswith(p) and len(word) > len(p):
            return True
    return False


def _ends_with_connective(word: str) -> bool:
    if word in NOUN_EXCEPTIONS:
        return False
    for s in CONNECTIVE_SUFFIXES:
        if word.endswith(s) and (len(word) > len(s) or len(s) >= 2):
            return True
    return False


def analyze(text: str) -> RuleResult:
    t = _norm(text)
    if not t:
        return RuleResult(0.0, "neutral")
    if ("?" in t[-3:] or "？" in t[-3:]) and not _DIGITS_THEN_Q.search(t):
        return RuleResult(SCORES["question"], "question")
    t = _PUNCT_TRAIL.sub("", t)
    if not t:
        return RuleResult(0.0, "neutral")
    if t in SHORT_ANSWERS:
        return RuleResult(SCORES["short_answer"], "short_answer")
    tokens = t.split(" ")
    last = tokens[-1]

    if last in FILLERS:
        return RuleResult(SCORES["filler"], "filler")
    if last in CONJUNCTIONS:
        return RuleResult(SCORES["connective"], "connective")

    d = _digits_result(tokens)
    if d is not None:
        return d

    if any(last.endswith(s) for s in SOFT_FINAL_SUFFIXES):
        return RuleResult(SCORES["soft_final"], "soft_final")
    if last.endswith("요"):
        if any(last.endswith(s) for s in STRONG_YO) or _is_n_ga_yo(last):
            return RuleResult(SCORES["final"], "final")
        stem = last[:-1]
        if len(last) >= 3 and (
            _ends_with_particle(stem) or _ends_with_connective(stem)
            or (stem.endswith("이") and len(stem) >= 3)        # "번호이요"
        ):
            return RuleResult(SCORES["weak_final"], "weak_final")
        return RuleResult(SCORES["final"], "final")
    if last.endswith("니까"):
        # -ㅂ니까(합니까/됩니까)는 의문 종결, 그 밖(-니까/-으니까)은 연결어미
        if len(last) >= 3 and _jong(last[-3]) == 17:
            return RuleResult(SCORES["final"], "final")
        return RuleResult(SCORES["connective"], "connective")
    if any(last.endswith(s) for s in FINAL_SUFFIXES):
        return RuleResult(SCORES["final"], "final")
    if _ends_with_connective(last):
        return RuleResult(SCORES["connective"], "connective")
    if _ends_with_particle(last):
        return RuleResult(SCORES["particle"], "particle")
    return RuleResult(0.0, "neutral")


def end_score(text: str) -> float:
    """−1(보류) ~ +1(끝남)."""
    return analyze(text).score


def is_strong_hold(text: str) -> bool:
    """연결어미·간투사·숫자 미완처럼 이어 말할 것이 거의 확실한 경우."""
    return analyze(text).kind in STRONG_HOLD
