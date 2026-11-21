"""요청 의도 추정 + 의도별 빠른 해결 순서(AG-T55).

고객 말(STT 결과)과 이전 대화에서 키워드 특징을 뽑아 의도마다 점수(로그 오즈 단위)를 더하고, softmax로 확률을 낸다.
가장 높은 의도의 확률이 문턱(기본 0.6)을 넘고 2위와 차이가 충분할 때만 시스템 프롬프트 가변부 끝에
"요청 추정 + 그 의도의 빠른 해결 순서" 한 덩어리를 붙인다. 애매하면 아무것도 붙이지 않는다(기존 동작).
→ 잘못 묶인 키워드로 엉뚱한 해결책을 내미는 것보다 말하지 않는 쪽을 고른다(선택적 분류, reject option).

설계 근거
- 점수 = 의도 사전 점수 + Σ(일치한 특징의 가중치) + 이전 고객 말의 의도 점수 × 감쇠(대화 상태 이어받기)
  + 직전 상담원 질문의 단서(번호·성함을 물었으면 '이어서 진행', 전체/일부를 물었으면 반품 요청).
  나이브 베이즈의 로그 우도비 합과 같은 꼴이다. 가중치는 손으로 정하고 온도 하나만 연습 문항에 맞춘다
  (scripts/intent_hint_eval.py). 특징이 겹치는 쌍(환불 요청 ↔ 환불 진행 문의, 구매 ↔ 전화 요청, 상품 ↔ 건강 상담)은
  같은 낱말에 붙는 말끝(해 주세요 / 언제·아직 / 가능한가요)으로 가른다.
- 음성 인식은 띄어쓰기가 흔들리므로 공백을 지운 글에서 찾는다.
- 이 모듈은 함수를 부르거나 답을 정하지 않는다. LLM에게 '참고'로만 준다. 최종 판단은 시나리오·브랜드 정보 규칙이 한다.

순수 함수 모듈(입출력 없음). 스위치: 환경변수 DIRECTON_INTENT_HINT(기본 켬, 0이면 끔).
"""
from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field

__all__ = ["INTENTS", "IntentEstimate", "estimate", "hint_line", "system_suffix", "enabled", "empathy_key",
           "THRESHOLD", "MARGIN", "TEMPERATURE"]

ENV = "DIRECTON_INTENT_HINT"
THRESHOLD = 0.60        # 이 확률 이상일 때만 힌트를 붙인다
MARGIN = 0.25           # 1위 − 2위 확률 차이 최소값
TEMPERATURE = 1.0       # softmax 온도(scripts/intent_hint_eval.py로 맞춘다)
UNKNOWN_SCORE = 1.5     # '모름' 의도의 고정 점수. 특징이 약하면 모름이 이긴다
HISTORY_DECAY = 0.5     # 한 턴 앞의 고객 말 점수에 곱한다
HISTORY_TURNS = 3       # 이전 고객 말을 이만큼까지 본다


@dataclass(frozen=True)
class Intent:
    code: str
    label: str
    steps: str                  # 빠른 해결 순서(브랜드 규칙과 같은 내용을 짧게)
    prior: float = 0.0          # 사전 점수(로그 오즈). 연습 문항 빈도를 반영한 작은 값


# 해결 순서는 agent/knowledge.py RULES와 시나리오(scenarios.md)를 옮겨 적은 것이다. 새 규칙을 만들지 않는다.
INTENTS: dict[str, Intent] = {i.code: i for i in (
    Intent("delivery", "배송·주문 확인",
           "번호(이번 말, 이전 대화, 발신자 전화번호 순)로 바로 get_order_info → 배송상태를 결과의 말 그대로 전하고, "
           "출고 전이면 출고 예정일, 배송 중이면 택배사 조회 방법을 덧붙인다. 번호가 하나도 없을 때만 주문번호나 연락처를 묻는다", 0.3),
    Intent("payment_check", "입금 확인",
           "주문 조회 → 입금전이면 기다리라고만 하지 말고 입금하신 날짜, 입금자 성함, 입금 금액을 한 번에 여쭌다. "
           "입금이 확인된 상태면 그대로 알린다", 0.1),
    Intent("address_change", "배송지 변경",
           "주문 조회로 출고 전인지 본다 → 출고 전이면 바꿀 주소를 받고 상담원이 처리한다고 안내, 출고됐으면 변경이 어렵다고 안내", 0.0),
    Intent("misdelivery", "오배송·누락·미수령",
           "주문 조회로 배송 상태를 확인 → 받은 상품과 빠진 상품 등 상황을 듣고 상담원 연결을 안내", 0.0),
    Intent("product", "상품 문의",
           "제품이 특정되면(별칭·부위 포함) 묻지 말고 바로 search_products → 결과에 있는 내용만 결론부터 답한다. "
           "어느 제품인지 전혀 알 수 없을 때만 제품을 묻는다. 이전 대화 없이 제품 이름만 말했으면 브랜드 정보의 제품명으로 확인하며 궁금한 점을 묻는다. 효과 보장·치료 표현은 쓰지 않는다", 0.4),
    Intent("cancel", "주문 취소",
           "주문 조회 → 출고 전이고 사유를 들었으면 cancel_order, 사유가 없으면 사유만 묻는다. 이미 출고됐으면 반품 절차를 안내", 0.0),
    Intent("return_request", "반품·환불 신청",
           "주문 조회 → (여러 건이면) 어느 주문인지 → 사유 → 전체/일부 → (계좌 환불 수단이면 환불 계좌) → 접수. "
           "이미 끝난 단계는 건너뛰고, 물을 것이 없으면 되묻지 말고 같은 차례에 접수까지 한다", 0.5),
    Intent("return_policy", "반품 가능 여부·비용 문의",
           "접수하지 않는다. 브랜드 정보로 바로 답한다: 자사몰 구매이고 받은 지 10일 이내면 환불보장제로 운임까지 무상, "
           "그 밖에는 사유에 따라 배송비가 달라질 수 있다", 0.0),
    Intent("return_status", "반품·환불 진행 상황",
           "전화번호(이전 대화, 발신자 정보)가 있으면 묻지 말고 바로 get_return_info → 현재 상태부터 말한다. "
           "번호를 모르면 성함과 전화번호를 한 번에 묻는다. 조회되지 않으면 취소·반품·교환 중 어떤 접수였는지 묻는다", 0.3),
    Intent("exchange", "교환·불량",
           "같은 상품으로 바꾸길 원하고 주문번호와 사유가 있으면 exchange_order. 불량인지 판단이 필요하면 상황을 듣고 상담원 연결을 안내", 0.0),
    Intent("purchase", "구매 방법",
           "검색하지 않는다. 홈페이지 온라인 주문 또는 ARS 전화주문으로 안내한다. 직접 주문이 어렵다거나 전화로 도와 달라고 하면 "
           "성함과 연락처를 받아 전화 요청을 접수한다", 0.3),
    Intent("callback", "전화 요청(전화 주문·유선 상담)",
           "성함과 전화번호를 모두 알면(이번 말, 이전 대화, 발신자 정보) 바로 request_callback. 빠진 것만 한 번에 묻는다. "
           "접수 뒤 영업 시간 안에 순서대로 연락드린다고 안내", 0.3),
    Intent("policy", "결제·쿠폰·배송비 등 정책 문의",
           "조회 없이 브랜드 정보의 해당 항목으로 바로 답한다(결제 수단, 쿠폰·적립, 할인, 배송비). 숫자와 조건은 바꾸지 않는다", 0.2),
    Intent("health", "건강 상태·복용 약·이상 증상 상담",
           "단정해서 답하지 않는다. 브랜드 정보에 정해진 안내가 있으면 그대로 전하고 상담원 연결을 안내한다", 0.0),
    Intent("agent", "상담원 연결 요청",
           "조회로 대신하지 말고 상담원 연결을 안내하며 고객센터 전화번호와 영업 시간을 함께 알린다", 0.0),
    Intent("other_channel", "다른 구매처 주문",
           "번호를 묻지 않는다. 구매하신 곳의 고객센터로 문의하시도록 안내하고, 필요하면 상담원 연결을 안내한다", 0.0),
    Intent("feedback", "불만·의견",
           "먼저 공감하고 의견을 전달하겠다고 말한다. 관련된 브랜드 정보가 있으면 짧게 안내한다", -0.3),
)}

UNKNOWN = "unknown"


# ── 특징 ─────────────────────────────────────────────
# (의도, 가중치, 정규식). 정규식은 공백을 지운 글에 적용한다. 가중치는 로그 오즈 단위(대략 강함 3, 보통 2, 약함 1, 반대 음수).
_R = r"(?:환불|반품|환풀|반폼|환블)"
_REQ = r"(?:해주|해줘|해달|부탁|신청|요청|원해|원합|하고싶|할게|할께|할래|하겠|진행해|처리해|받고싶|용청)"
_STATUS = r"(?:언제|아직|안들어|안돼|안되|안됐|안되었|늦어|소식|처리가|확인부탁|진행상황|현황|됐나|되었나|들어오|입금되)"
_PAST = r"(?:했는데|했고|했어요|했습니다|한거|한건|했던|보냈|회수해|수거해|가져갔|해갔|수거가)"
_POLICY = r"(?:쿠폰|적립금|포인트|할인|카드결|카드로|카드는|카드결재|결제|결재|계좌이체|무통장|배송비|가격이|등급|무료배송)"
_OTHER = r"(?:쿠팡|11번가|십일번가|지마켓|G마켓|옥션|네이버스토어|스마트스토어|홈쇼핑|오픈마켓)(?:에서|으로|로|주문|구매|반품|환불|에서산|건)"
_ASKQ = r"(?:가능한가|가능해|가능할까|되나요|돼요\?|되요\?|비용|배송비|얼마|드나요|있나요|되는지)"

FEATURES: list[tuple[str, float, str]] = [
    # 반품·환불
    ("return_request", 1.6, _R),
    ("return_request", 2.4, _R + r".{0,8}" + _REQ + r"(?!건|한거|한건|했|한지)"),
    ("return_request", 1.5, r"(?:전체|전부|다|모두|일부|나머지).{0,6}(?:반품|환불|해주|부탁|로해|로진행|할게)"),
    ("return_request", 1.2, r"(?:포장해놨|재포장|돌려보내)"),
    ("return_request", 1.0, r"(?:어떻게해야|방법)"),
    ("return_status", 3.0, _R + r".{0,10}" + _STATUS),
    ("return_status", 2.8, _R + r".{0,12}" + _PAST),
    ("return_status", 3.0, _R + r".{0,6}(?:신청|요청|접수)(?:건|한거|한건|했는데|했고|했던|했잖|한지|까지했)"),
    ("return_request", -2.0, _R + r".{0,6}(?:신청|요청|접수)(?:건|한거|한건|했는데|했고|했던|했잖|한지|까지했)"),
    ("return_status", -2.5, r"취소.{0,12}" + _R),     # 취소한 주문의 환불은 주문 조회로 확인한다(반품 현황 아님)
    ("return_status", 2.0, r"(?:회수|수거).{0,6}(?:해갔|해가|했|됐|언제|아직|안)"),
    ("return_status", 1.8, r"환불금"),
    ("return_status", -1.8, _R + r".{0,8}(?:하고싶|해주세요|해주시|부탁드|원해요|원합니다)$"),
    ("return_policy", 2.6, _R + r".{0,12}" + _ASKQ),
    ("return_policy", 1.4, r"(?:먹어보고|먹고나서|뜯었|개봉).{0,10}(?:" + _R + r"|되나|가능)"),
    ("return_policy", -2.4, _R + r".{0,20}(?:해주세요|해주시|부탁드|신청합|요청합|진행해)"),
    # 취소
    ("cancel", 3.2, r"(?:주문)?취소(?!했|한거|된거|하고환불)"),
    ("cancel", -2.0, r"취소.{0,4}(?:했|한거|된)"),
    # 배송·주문 확인
    ("delivery", 2.4, r"배송(?!비|지)"),
    ("delivery", 2.6, r"(?:언제|며칠|몇일).{0,6}(?:와|오나|도착|받|배송|출고)"),
    ("delivery", 2.2, r"(?:도착|출고|송장|운송장|택배|배송조회|운종장)"),
    ("delivery", 2.6, r"주문(?:확인|조회|내역|건확인|된건가|들어갔|잘들어)"),
    ("delivery", 1.0, r"주문한(?:거|것|게|건|상품)"),
    ("delivery", -2.5, r"(?:잘못|다른게|다른거|엉뚱).{0,6}(?:배송|왔|와)"),
    # 입금
    ("payment_check", 3.4, r"입금"),
    ("payment_check", 1.0, r"예금자|입금자"),
    # 배송지
    ("address_change", 3.4, r"(?:배송지|주소|받는곳).{0,6}(?:변경|바꾸|바꿔|수정|잘못)"),
    # 오배송·누락
    ("misdelivery", 3.2, r"(?:오배송|잘못배송|잘못와|잘못왔|다른게왔|다른거왔|덜왔|빠져|누락|안왔|못받|분실|안받)"),
    ("misdelivery", 2.6, r"(?:시켰|주문했)는데.{0,12}(?:왔|와있|들어있)"),
    ("misdelivery", 1.5, r"(?:개가와야|개와야|왔네요|왔어요)"),
    # 교환·불량
    ("exchange", 3.2, r"교환"),
    ("exchange", 2.6, r"(?:불량|파손|깨져|터져|찢어|변질|곰팡|이물질)"),
    # 상품
    ("product", 2.0, r"(?:성분|효능|효과|섭취|복용|먹는|먹어도|먹으면|드시|몇일분|며칠분|몇포|몇정|몇알|한박스에|추천|영양제|식전|식후|부작용없)"),
    ("product", 1.0, r"(?:언제먹|먹는시간|같이먹|함께먹)"),
    ("product", 1.4, r"(?:치료제|건강식품|건강기능식품|약인가)"),
    ("product", 1.2, r"(?:조인플렉스|이눌린|배변|브리즈콜|기관지|혈관|혈당|콜레스테롤|위프로젝트|간가드|눈가드|간건강|대장|관절|하루온간|하루온눈|하루온위)"),
    ("product", 1.6, r"(?:변을못|화장실을?못|안나와|반응이없|효과가없|효과없|소식없|아무렇지)"),
    # 구매
    ("purchase", 3.0, r"(?:구매|구입|주문)(?:하려|할려|하고싶|원해|원하|하겠|할게|할께|요망|합니다|할까|하러|하려구|할려구|도와|방법)"),
    ("purchase", 2.2, r"(?:사고싶|살려고|사려고|살수있|사려면|구매합|구입하|구매하|구입원|구매원)"),
    ("purchase", 1.4, r"주문도와|주문방법|어떻게사|어디서사"),
    # 전화 요청
    ("callback", 3.0, r"(?:전화주문|유선|통화가능|통화요|통화원|통화하고|전화주|전화줘|전화좀|연락주|연락줘|전화상담|전화로|전화부탁)"),
    ("callback", -2.2, r"(?:상담원과|상담사와|상담원이랑)통화했"),
    # 정책
    ("policy", 3.4, _POLICY),
    ("policy", 1.5, r"회원가입|\d{3,}원|이해가안"),
    ("purchase", -2.0, _POLICY),
    ("policy", -1.6, r"(?:입금했|입금확인)"),
    # 건강 상담
    ("health", 3.0, r"(?:부작용|두드러기|가려|발진|설사|복통|어지러|구토|메스꺼|두통|임신|수유|지병|혈압약|당뇨약|먹고있는약|복용중인약|약을먹|약먹|병원|의사|처방|수술|항암|이상증상|알레르기|알러지)"),
    ("product", -2.0, r"(?:부작용|두드러기|가려|발진|설사|복통|어지러|구토|메스꺼|두통|임신|수유|지병|혈압약|당뇨약|먹고있는약|복용중인약|약을먹|약먹|병원|의사|처방|수술|항암|이상증상|알레르기|알러지)"),     # 복용 약·증상이 나오면 상품 안내보다 상담 쪽
    # 상담원
    ("agent", 3.6, r"(?:상담원|상담사|사람|직원)(?:연결|바꿔|바꿔주|이랑|과통화하고|하고통화|좀)"),
    ("agent", 2.0, r"상담원(?!과통화했|이전화)"),
    # 다른 구매처
    ("other_channel", 3.4, _OTHER),
    ("other_channel", -3.0, r"(?:쿠팡|11번가|지마켓|홈쇼핑)보다"),
    # 다른 구매처 주문이면 여기서 조회·접수하지 않는다 → 반품·취소·배송 해결 순서를 내밀지 않게 누른다
    ("return_request", -3.5, _OTHER), ("return_status", -3.5, _OTHER), ("return_policy", -3.5, _OTHER),
    ("cancel", -3.5, _OTHER), ("delivery", -3.5, _OTHER), ("exchange", -3.5, _OTHER),
    # 의견
    ("feedback", 2.4, r"(?:광고|선전|과장|사기|실망|불친절|빼주세요|빼 ?주)"),
    ("feedback", 1.4, r"(?:쿠팡|11번가|지마켓|홈쇼핑)보다"),
]
_COMPILED = [(code, w, re.compile(p)) for code, w, p in FEATURES]

# 직전 상담원 질문 단서(공백 지운 글). 번호·성함을 물었으면 고객 말은 그 답이므로 이전 의도를 강하게 이어받는다.
_ASKED_SLOT = re.compile(r"(?:주문번호|전화번호|연락처|성함|휴대폰번호|계좌|은행|예금주|주소).{0,30}(?:알려|말씀|남겨|부탁|주시겠|주실|확인부탁)")
_ASKED_SCOPE = re.compile(r"(?:전체|일부).{0,20}(?:반품|진행|원하|알려)")
_ASKED_REASON = re.compile(r"(?:반품|취소|교환)?사유")
_AGENT_STATUS = re.compile(r"(?:반품|환불).{0,8}(?:현황|진행현황|진행상황|처리현황)")
_AGENT_DONE = re.compile(r"(?:반품|환불|취소).{0,6}접수(?:가)?(?:완료|되었|됐|해드렸)")
_AGENT_WHICH_PRODUCT = re.compile(r"(?:어떤|어느)제품|제품명")
_PHONE_IN = re.compile(r"01[016789][\-.,]?\d{3,4}[\-.,]?\d{4}")
_GREETING_ONLY = re.compile(r"^(?:안녕하세요|안녕하세용|여보세요|네|넵|예|감사합니다|수고하세요)[.!?~ ]*$")


def _compact(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def _feature_scores(text: str) -> tuple[dict[str, float], list[tuple[str, str]]]:
    t = _compact(text)
    scores: dict[str, float] = {}
    hits: list[tuple[str, str]] = []
    if not t:
        return scores, hits
    for code, w, rx in _COMPILED:
        m = rx.search(t)
        if m:
            scores[code] = scores.get(code, 0.0) + w
            if w > 0:
                hits.append((code, m.group(0)))
    return scores, hits


@dataclass
class IntentEstimate:
    probs: dict[str, float]                     # 의도 → 확률(unknown 포함, 합 1)
    top: str                                    # 가장 높은 의도(unknown일 수 있음)
    p: float                                    # top의 확률
    margin: float                               # top − 2위
    keywords: list[str] = field(default_factory=list)   # top 의도를 만든 이번 말의 키워드
    carried: bool = False                       # 이번 말에 특징이 없고 이전 대화에서 이어받았는가
    slot_answer: bool = False                   # 직전 상담원이 번호·성함 등을 물었고 이번 말이 그 답으로 보이는가

    @property
    def confident(self) -> bool:
        return self.top != UNKNOWN and self.p >= THRESHOLD and self.margin >= MARGIN

    def ranked(self, n: int = 3) -> list[tuple[str, float]]:
        return sorted(self.probs.items(), key=lambda kv: -kv[1])[:n]


def _role(h: dict) -> str:
    r = str(h.get("role") or "").strip().lower()
    if r in ("customer", "user", "human", "고객", "손님"):
        return "customer"
    if r in ("agent", "assistant", "bot", "상담원"):
        return "agent"
    return r


def _text(h: dict) -> str:
    v = h.get("text")
    if v is None:
        v = h.get("content")
    return v if isinstance(v, str) else ("" if v is None else str(v))


def estimate(text: str, history: list[dict] | None = None, *, temperature: float | None = None) -> IntentEstimate:
    """이번 고객 말 + 이전 대화 → 의도 확률."""
    T = TEMPERATURE if temperature is None else temperature
    cur, hits = _feature_scores(text)
    items = [h for h in (history or []) if isinstance(h, dict)]
    convo = [h for h in items if _role(h) in ("customer", "agent")]

    # 직전 상담원 말
    last_agent = ""
    for h in reversed(convo):
        if _role(h) == "agent":
            last_agent = _compact(_text(h))
            break
        break                                       # 마지막이 고객 말이면 상담원 질문 단서 없음
    asked_slot = bool(last_agent) and bool(_ASKED_SLOT.search(last_agent[-200:]))
    has_own = any(v > 0 for v in cur.values())

    # 이전 고객 말에서 이어받기(최근 것부터 감쇠)
    carry: dict[str, float] = {}
    k = 0
    for h in reversed(convo):
        if _role(h) != "customer":
            continue
        k += 1
        if k > HISTORY_TURNS:
            break
        sc, _ = _feature_scores(_text(h))
        for code, v in sc.items():
            carry[code] = carry.get(code, 0.0) + v * (HISTORY_DECAY ** k)
    # 이번 말에 의도 특징이 없거나(번호·이름만) 상담원 질문의 답이면 이어받기를 키운다
    carry_gain = 1.0
    if not has_own:
        carry_gain = 2.0
    if asked_slot:
        carry_gain *= 1.5
    # 함수 호출 기록도 이어받기 단서(마지막 호출 하나)
    last_call = next((str(h.get("name")) for h in reversed(items) if h.get("role") == "tool_call"), "")
    call_hint = {"return_order": "return_status", "return_order_item": "return_status",
                 "get_return_info": "return_status", "request_callback": "callback",
                 "search_products": "product", "cancel_order": "cancel", "exchange_order": "exchange"}.get(last_call)

    scores: dict[str, float] = {}
    for code, it in INTENTS.items():
        s = it.prior + cur.get(code, 0.0) + carry_gain * max(carry.get(code, 0.0), -1.0)
        if call_hint == code and not has_own:
            s += 0.8
        scores[code] = s
    tail = last_agent[-300:]
    if tail and _AGENT_STATUS.search(tail):
        scores["return_status"] += 3.5          # 상담원이 반품·환불 현황 확인을 위해 번호를 물었다
    if tail and _AGENT_DONE.search(tail) and (not has_own or re.search(_R, _compact(text))):
        scores["return_status"] += 2.0          # 이미 접수를 마친 뒤의 환불 질문은 진행 문의
    if tail and _AGENT_WHICH_PRODUCT.search(tail):
        scores["product"] += 2.5                # 어느 제품인지 물었고 고객이 제품을 답했다
    # 연락처(전화번호)를 주면서 구매를 말하면 전화 주문 요청이다(구매 안내 뒤 성함·연락처를 남기는 흐름)
    if _PHONE_IN.search(_compact(text)) and scores["purchase"] > scores["callback"]:
        scores["callback"] = scores["purchase"] + 1.0
    if last_agent and _ASKED_SCOPE.search(last_agent[-200:]):
        scores["return_request"] += 2.5
    if last_agent and _ASKED_REASON.search(last_agent[-200:]) and not has_own:
        scores["return_request"] += 1.0
    unknown = UNKNOWN_SCORE
    t = _compact(text)
    if _GREETING_ONLY.match(text.strip() if text else "") and not convo:
        unknown += 3.0                              # 인사만 → 용건을 묻는다(힌트 없음)
    scores[UNKNOWN] = unknown

    mx = max(scores.values())
    ex = {c: math.exp((s - mx) / max(T, 1e-6)) for c, s in scores.items()}
    z = sum(ex.values())
    probs = {c: v / z for c, v in ex.items()}
    ranked = sorted(probs.items(), key=lambda kv: -kv[1])
    top, p = ranked[0]
    margin = p - (ranked[1][1] if len(ranked) > 1 else 0.0)
    kws = list(dict.fromkeys(m for c, m in hits if c == top))
    return IntentEstimate(probs=probs, top=top, p=p, margin=margin, keywords=kws,
                          carried=not has_own and bool(carry), slot_answer=asked_slot and bool(t))


def hint_line(est: IntentEstimate) -> str:
    """확신할 때만 프롬프트에 붙일 한 줄. 아니면 빈 문자열."""
    if not est.confident:
        return ""
    it = INTENTS[est.top]
    why = f" 근거 키워드: {', '.join(est.keywords[:3])}." if est.keywords else ""
    if est.carried or est.slot_answer:
        why += " 이번 말은 앞선 요청에 이어지는 답으로 보인다(진행 중인 절차의 다음 단계로 간다)."
    return (f"\n- 요청 추정(키워드 확률, 참고용): '{it.label}' {est.p:.2f}.{why} "
            f"빠른 해결 순서: {it.steps}. 지금 해결에 필요한 한 가지를 바로 하고, 이미 아는 정보는 다시 묻지 않는다. "
            "고객 말이나 이전 대화가 이 추정과 다르면 이 줄을 무시하고 고객 말을 따른다.")


def enabled(env: dict | None = None) -> bool:
    v = (os.environ if env is None else env).get(ENV, "1")
    return str(v).strip().lower() not in ("0", "false", "no", "off", "")


def system_suffix(text: str, history: list[dict] | None = None) -> str:
    """스위치가 켜져 있고 확신할 때 시스템 프롬프트 끝(가변부)에 붙일 글. 오류가 나도 빈 문자열."""
    if not enabled():
        return ""
    try:
        return hint_line(estimate(text, history))
    except Exception:  # noqa: BLE001
        return ""


# ── 공감 한마디 상황(AG-T61) ─────────────────────────
# 불편 신호가 분명할 때만 고른다. 단순 문의("배송 언제 와요?", "카드 결제 되나요?")에는 고르지 않는다.
# 순서가 중요하다: 구체적인 상황(오배송·불량)을 먼저 본다.
_EMPATHY_RULES: list[tuple[str, re.Pattern]] = [
    ("misdelivery", re.compile(r"잘못(?:배송|왔|와|온|보내)|다른(?:게|거|제품|상품)(?:이|가)?왔|덜왔|빠져(?:있|서|왔)|누락|"
                               r"(?:시켰|주문했)는데.{0,12}(?:왔|와있|들어있)")),
    ("defect", re.compile(r"불량|파손|깨져|터져|찢어|변질|곰팡|이물질")),
    ("refund_late", re.compile(r"(?:환불|반품|회수|수거|환불금).{0,14}(?:아직|안들어|안돼|안되|안됐|늦|소식|처리가안|안왔)")),
    ("payment", re.compile(r"입금.{0,12}(?:안돼|안되|안됐|안되었|확인이안|확인을못|확인못|못한|아직|안했|안한)")),
    ("no_effect", re.compile(r"(?:효과가?없|변을못|화장실을?못|안나와|반응이없|아무렇지|아무런반응).{0,20}(?:환불|반품)|"
                             r"(?:환불|반품).{0,20}(?:효과가?없|변을못|화장실을?못|안나와|반응이없)")),
    ("delay", re.compile(r"(?:배송|택배|도착|주문한|물건|상품|제품).{0,14}(?:아직|안와|안왔|안오|안온|늦|며칠째|오지않|안나왔|출발을?안)|"
                         r"(?:아직|며칠째).{0,8}(?:안와|안왔|안오|배송|도착|안온)")),
]
# 이 의도로 보이면 공감 한마디를 쓰지 않는다(안내만 하면 되는 문의)
_NO_EMPATHY = frozenset({"policy", "purchase", "return_policy", "other_channel", "agent", "callback", "product"})


def empathy_key(text: str, history: list[dict] | None = None) -> str | None:
    """이번 고객 말이 불편을 겪는 상황이면 EMPATHY_PHRASES의 키, 아니면 None. 순수 함수."""
    t = _compact(text)
    if not t or re.search(_OTHER, t):           # 다른 구매처 주문의 불편은 우리가 사과할 일이 아니다
        return None
    try:
        top = estimate(text, history).top
    except Exception:  # noqa: BLE001
        top = UNKNOWN
    for key, rx in _EMPATHY_RULES:
        if rx.search(t):
            if key == "no_effect" or top not in _NO_EMPATHY:   # "효과 없어 환불"은 상품 의도로 보여도 공감
                return key
            return None
    return None
