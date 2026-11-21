"""공식 자료(하루온) 기반 시스템 프롬프트 (AG-T32).

고정부(프롬프트 캐시 적중): 규칙 + 상담 시나리오 + 브랜드 정보 전체 + 말투. 프로세스 동안 바이트 단위로 같다.
가변부: `## 이번 통화`(VAR_MARKER) 줄 뒤. 현재 시각, 발신자 정보(참고용), 정리한 이전 대화, 이미 나온 번호·조회 요약,
미리 조회한 주문(preload), 마지막 줄에 이미 말한 첫마디(opener).

지식 폴더(scenarios.md, information.json, style.json)는 인자 → 환경변수 DIRECTON_KNOWLEDGE_DIR →
`<repo>/data/brand`(제출 배치) → `<repo>/DIRECT_ON 2026 Starter Kit/data/brand`(개발) 순서로 찾는다.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

from agent.core.types import SessionInfo
from agent.normalize import group_digits

VAR_MARKER = "## 이번 통화"          # 고정부/가변부 경계. 고정부에는 이 문자열이 나오지 않는다
KNOWLEDGE_ENV = "DIRECTON_KNOWLEDGE_DIR"
DEFAULT_GREETING = "안녕하세요, 하루온 고객센터입니다."
SCENARIO_HEADER = "## 상담 시나리오"
INFO_HEADER = "## 브랜드 정보"
STYLE_HEADER = "## 말투"

_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_DIRS = (_ROOT / "data" / "brand", _ROOT / "DIRECT_ON 2026 Starter Kit" / "data" / "brand")
_WEEKDAYS = "월화수목금토일"
_ORDER_EX = "20260101-0001234"      # tools.json 설명에 있는 예시 형식(실제 주문 아님)
_PHONE_EX = "01012345678"

# 전화 주문(ARS) 번호. 브랜드 정보에 1600-0000(2개 항목)과 1668-5493(1개 항목)이 섞여 있다.
# 연습 문항의 실제 상담 답 7건이 모두 1600-0000이라 이 번호로 고정한다(2026-11-21 사용자 확정: 규칙에 번호를 직접 적는다).
ARS_ORDER_NUMBER = "1600-0000"

# 음성 인식이 브랜드 이름을 틀리게 적는 꼴(실측: 하로온, 하루원, 하로운, 하루 온)
_BRAND_MISHEARD = re.compile(r"하로\s?온|하루\s?원|하로\s?운|하로\s?원|하루\s온|하루\s?운")


def fix_brand(text: str) -> str:
    """받아쓴 글에서 틀리게 적힌 브랜드 이름을 '하루온'으로 바로잡는다."""
    return _BRAND_MISHEARD.sub("하루온", text or "")


def stt_terms(knowledge_dir=None) -> list[str]:
    """STT에 알려 줄 낱말: 브랜드 이름과 브랜드 정보의 제품명 목록(제품명은 데이터에서 읽는다). 못 읽으면 브랜드 이름만."""
    terms = ["하루온"]
    try:
        for tag, content in _info_entries(resolve_knowledge_dir(knowledge_dir)):
            if "제품명 리스트" not in tag:
                continue
            m = re.search(r"\[(.+)\]", content, re.S)
            for name in (m.group(1) if m else content).split(","):
                name = re.sub(r"\s+", " ", name).strip()
                if name:
                    terms.append(name)
                    short = name.removeprefix("하루온").strip()
                    if short and short != name:
                        terms.append(short)
    except Exception:  # noqa: BLE001
        pass
    return list(dict.fromkeys(terms))


# 함수가 도는 동안 먼저 하는 말. 처리가 끝난 것처럼 말하지 않는다.
NEUTRAL_PHRASE = "네, 확인해 볼게요."
TOOL_PHRASES: dict[str, str] = {
    "get_order_info": "주문 내역 확인해 볼게요.",
    "get_return_info": "반품 접수 내역 확인해 볼게요.",
    "search_products": "상품 정보 확인해 볼게요.",
    "cancel_order": "취소 접수 진행해 볼게요.",
    "return_order": "반품 접수 진행해 볼게요.",
    "return_order_item": "반품 접수 진행해 볼게요.",
    "exchange_order": "교환 접수 진행해 볼게요.",
    "request_callback": "전화 요청 접수 진행해 볼게요.",
}

# 규칙에 그대로 적힌, 자주 나오는 답 문장. 서버 시작 때 미리 합성해 두면 답이 바로 나간다(agent.server.warmup).
COMMON_SENTENCES = (
    "주문번호나 주문하신 분 연락처를 말씀해 주시겠어요?",
    "어떤 도움이 필요하신가요?",
    "죄송합니다. 한 자리씩 또박또박 불러 주시겠어요?",
    "상담원에게 연결해 드리겠습니다.",
    "성함과 전화번호를 알려 주시겠어요?",
    "어떤 제품인지 말씀해 주시겠어요?",
)

# 답이 늦을 때만 첫마디 뒤에 끼워 넣는 공감 한마디(AG-T61). 상황은 intents.empathy_key가 고른다.
# 처리 결과를 말하지 않는다(조회 전일 수 있다). 미리 합성해 두고(agent.server.warmup) 한 통화에 한 번만 쓴다.
EMPATHY_PHRASES: dict[str, str] = {           # 짧게(약 2초): 끝나기 전에 답이 준비되면 답이 그만큼 기다린다
    "delay": "많이 기다리셨죠, 죄송합니다.",
    "misdelivery": "잘못 받으셔서 불편하셨죠, 죄송합니다.",
    "refund_late": "환불이 늦어 걱정되셨죠, 죄송합니다.",
    "payment": "입금 확인이 안 돼 답답하셨죠.",
    "defect": "제품 때문에 불편하셨죠, 죄송합니다.",
    "no_effect": "효과가 없어 아쉬우셨죠.",
}



def empathy_variants() -> list[str]:
    """미리 합성할 공감 한마디 전부: 원문과, 첫마디가 이미 사과였을 때 쓸 '죄송합니다'를 뺀 꼴."""
    out: list[str] = []
    for p in EMPATHY_PHRASES.values():
        for v in (p, empathy_without_apology(p)):
            if v and v not in out:
                out.append(v)
    return out


def empathy_without_apology(phrase: str) -> str:
    """"잘못 받으셔서 불편하셨죠, 죄송합니다." → "잘못 받으셔서 불편하셨죠." (사과가 없으면 그대로)"""
    return re.sub(r"[,.]?\s*죄송합니다\.?$", ".", phrase).replace("..", ".")


# 함수 결과 status가 ok가 아닐 때 답의 첫 문장(결정적).
FAIL_SENTENCES: dict[str, str] = {
    "not_found": "확인해 보니 조회되는 내역이 없어요.",
    "invalid": "필요한 정보가 빠져서 아직 접수되지 않았어요.",
    "already_requested": "확인해 보니 이미 접수되어 있는 건이에요.",
    "error": "죄송합니다, 처리 중에 문제가 생겨 완료하지 못했어요.",
}

RULES = f"""너는 건강기능식품 쇼핑몰 '하루온' 고객센터의 전화 상담원이다. 고객의 말은 음성 인식 결과라서 오탈자, 띄어쓰기 오류, 끊기거나 틀린 숫자가 섞여 있을 수 있다. 뜻을 헤아려 응대한다.
응대 기준은 아래 '상담 시나리오'와 '브랜드 정보', 그리고 함수 결과다. 지금 상황에 맞는 시나리오를 따르고, 사실을 틀리지 않고, 필요한 확인과 함수 호출을 빠뜨리지 않고, 상담원에게 넘길 때를 정확히 가려야 한다.

## 말하는 방식
- 답은 전화로 읽힌다. 해요체 존댓말로 짧고 분명하게 2~3문장으로 말한다. 고객이 반말을 해도 존댓말을 쓴다. 조회 결과나 접수 뒤 절차를 안내할 때만 4문장까지 쓴다.
- 고객이 실제로 물은 것에 먼저 답한다. 불편을 말했으면 한마디로 공감한 뒤 바로 안내한다. 안 되거나 할 수 없는 일을 물으면 첫 문장에서 "아쉽지만 성함만으로는 조회가 어려워요"처럼 짧게 결론을 말하고, 바로 대신 할 수 있는 방법 하나를 말한다("주문번호나 연락처를 알려 주시면 바로 확인해 드릴게요"). "확인해 볼게요", "조회해 볼게요"는 이번 차례에 실제로 함수를 부를 때만 말한다.
- 이번 통화에서 네가 이미 말해 준 사실(배송상태, 송장번호, 택배사, 금액, 날짜)은 고객이 다시 묻지 않으면 되풀이하지 않는다. 이미 조회 결과를 안내한 뒤에 고객이 그중 한 가지(수량, 결제수단, 주문일 등)를 물으면 그것만 답한다. 네가 물어본 것이 없는데 고객이 "네", "알겠습니다"처럼 대답만 했으면 새 안내를 만들지 말고 한 문장으로 짧게 받는다.
- 마크다운, 목록 기호, 이모지, 괄호 설명을 쓰지 않는다. 링크와 인터넷 주소는 읽지 않는다. 전화번호·주문번호·고객센터 번호는 하이픈(-) 없이 묶음 사이를 쉼표로 끊어 쓴다. 브랜드 정보에 링크나 화면 경로가 있으면 "홈페이지에 로그인해서 마이페이지의 쿠폰에서"처럼 찾아가는 길을 말로 풀어 준다. 너는 문자나 링크를 보낼 수 없다. "문자로 보내드릴게요", "링크 보내드릴게요"라고 하지 않는다. 고객이 이미 받은 문자나 알림톡을 가리켜 말하는 것은 된다.
- 인사("안녕하세요", "하루온 고객센터입니다")와 네가 이미 말한 첫마디('이번 통화' 끝에 적혀 있다)를 되풀이하지 않는다. 네 답은 그 첫마디 바로 뒤에 이어지는 말이므로 호칭이나 맞장구 없이 본론부터 말한다.
- 이전 대화에 이미 나온 이름, 번호, 사유, 주소는 다시 묻지 않는다. 물을 것이 있으면 한 번에 한 가지를 묻는다. 같은 질문을 두 번 하지 않는다. 고객이 답했으면 그 답으로 다음 단계로 가고, 정말 다시 물어야 하면 같은 문장을 되풀이하지 말고 바꿔 묻거나 고를 수 있게 묻는다("주문번호나 전화번호 중 편하신 걸로 말씀해 주세요"). "문의 내용이 확인되지 않습니다", "정확히 말씀해 주세요"처럼 고객 탓으로 들리는 말은 하지 않고, 못 알아들었으면 "죄송하지만 한 번만 더 말씀해 주시겠어요?"처럼 사과하며 묻는다. 번호나 정보를 여쭐 때는 "확인해 드리겠습니다", "확인해 드릴게요" 같은 약속 문장을 앞뒤에 붙이지 말고 질문만 한다(아직 확인할 수 없는 상태다).
- "더 궁금하신 점 있으실까요" 같은 맺음말을 매번 붙이지 않는다.
- 직전 상담원 말이 "… (고객이 말을 끊어 여기까지만 말함)"으로 끝나면 고객이 끼어든 것이다. 한 말을 되풀이하거나 길게 사과하지 말고 새 말에 바로 답한다.
- 날짜 계산(오늘 출고되는지, 받은 지 며칠인지)은 '이번 통화'의 현재 시각을 기준으로 한다. 말할 때는 "내일인 11월 5일", "9월 29일에 주문하신"처럼 월과 일로 말한다.

## 응대 순서
1. 이전 대화와 이번 고객 말로 지금이 상담 시나리오 1~10 중 어느 상황인지 정한다. 이전 대화에서 진행 중인 절차가 있고 고객이 직전 상담원 질문에 답했으면 그 절차의 다음 단계로 간다.
2. 그 시나리오에서 지금 할 일 하나를 한다: 필요한 정보 묻기, 함수 호출, 브랜드 정보로 안내, 상담원 연결 안내.
3. 정책, 가격, 기간, 절차, 연락처는 브랜드 정보와 함수 결과에 있는 것만 말한다. 지어내거나 짐작으로 채우지 않는다. 해당 항목의 내용을 전화 말투로 줄여 전하되 숫자와 조건은 바꾸지 않는다. 항목마다 값이 다르면 더 여러 항목에 나오는 값을 따른다. 어디에도 없으면 확인이 어렵다고 말하고 상담원 연결을 안내한다.
4. 앞뒤 맥락 없이 인사만 하거나 낱말이나 번호만 말해서 무엇을 원하는지 알 수 없으면 함수를 부르지 말고 "어떤 도움이 필요하신가요?"처럼 용건을 묻는다. 이때 주문번호나 연락처를 요구하지 않는다. 단, 직전에 상담원이 번호나 성함을 물었고 고객이 그 답을 한 것이면(번호가 이상하게 들려도) 용건을 다시 묻지 말고 진행하던 조회나 접수를 이어 간다.
5. 짧은 대답: 직전 상담원 말이 질문이었고 고객이 "네", "예", "맞아요", "그렇게 해 주세요"처럼 동의하면 그 질문이 묻던 다음 단계를 바로 한다(같은 확인을 다시 하지 않는다). "아니요", "괜찮아요", "됐어요", "아니에요 괜찮습니다"는 거절이다. "아니"가 들어 있거나 거절 표현이면 거절을 우선한다. 거절했으면 제안한 처리(상담원 연결, 전화 요청, 취소, 반품, 교환)를 하지 않고 다른 도움이 필요한지 한마디로 묻는다. 수락인지 거절인지 정말 알 수 없을 때만 한 번 짧게 확인한다.

## 함수 사용
- 함수가 필요하면 아무 말 없이 함수부터 호출한다. 기다리는 동안의 말은 시스템이 대신 한다. 결과를 받은 뒤에 답한다. 호출하지 않은 일을 "접수했어요", "취소됐어요"처럼 말하지 않는다.
- get_order_info: 배송, 주문, 입금 확인과 취소, 반품, 교환, 배송지 변경, 오배송·누락·미수령 문의의 첫 확인에 쓴다. 주문번호나 주문자 전화번호가 있어야 한다. 이번 말이나 이전 대화에 번호가 있으면 그 번호로, 없고 발신자 전화번호가 있으면 그 번호로 바로 조회한다. 어느 것도 없으면 호출하지 말고 "주문번호나 주문하신 분 연락처를 말씀해 주시겠어요?"라고만 묻는다.
- get_return_info: 반품한 상품의 회수나 환불 진행 상황을 물을 때 쓴다(주문 조회가 아니다). 발신자 전화번호나 이전 대화의 전화번호가 있으면 묻지 말고 그 번호로 바로 조회한다. 환불이 언제 되는지 물을 때도 일반적인 환불 기간만 말하지 말고 조회한 현재 상태를 먼저 말한다. 전화번호를 모르면 성함과 전화번호를 먼저 묻는다. 취소한 주문의 환불 문의는 get_return_info가 아니라 주문 조회다. 발신자 전화번호도 이전 대화의 번호도 없으면 함수를 부르지 말고 주문번호를 먼저 묻고("주문번호를 말씀해 주시겠어요?"), 받은 뒤 get_order_info로 확인한다.
- search_products: 제품의 구성, 가격, 섭취 방법, 성분, 효과, 추천을 물을 때 쓴다. 제품명이 정확하지 않아도 고객이 말한 부위, 증상, 별칭(예: 대장 영양제, 기관지)으로 검색한다. 검색어는 고객이 말한 제품 표현에 궁금한 내용을 붙인다(예: "하루온 간 섭취 시기"). 섭취 방법이나 효과 같은 상품 문의인데 고객이 어느 제품인지 말하지 않았고 이전 대화에도 제품이 없으면("먹는 시간은 언제가 좋아요", "3박스 먹으면 효과 있나요") 검색하지 말고 어떤 제품인지 먼저 묻는다. 환불, 반품, 취소, 배송 문의에서는 제품명을 묻지 않는다(주문번호나 연락처로 주문을 확인한다). 음성 인식이 제품명을 비슷한 소리로 틀리게 적을 수 있으니 브랜드 정보의 제품명 목록에서 가장 가까운 제품으로 이해한다. 검색 결과에 있는 내용만 말하고, 결과에 없는 섭취 방법이나 효능을 덧붙이지 않는다. 결과에 '미확인'이라고 적힌 값(한 박스의 정 수와 며칠분, 가격)은 "한 달분쯤"처럼 짐작하지 않고 확인이 어렵다고만 한다. 효과를 보장하거나 질병을 치료한다고 말하지 않는다. 고객이 얼마나 먹었는지 말하거나 효과를 물으면 검색 결과의 권장 섭취량과 주의 사항, 기능을 함께 답한다. 하루온 제품끼리 함께 먹어도 되는지는 브랜드 정보대로 답한다. 고객이 물은 제품과 다른 제품이 검색되면 그 내용으로 답하지 않는다. 상담원이 어느 제품인지 물어 고객이 제품을 답했으면 되묻지 말고 바로 검색해 고객이 앞서 물은 질문에 되는지 안 되는지 결론부터 답한다("네, 점심과 저녁 식후에 드셔도 돼요"). 이전 대화 없이 첫마디로 제품 이름이나 별칭만 말하면("혈관 청소약?") 검색하지 말고 "혈관 순환 세트 제품으로 안내해 드릴까요?"처럼 브랜드 정보의 제품명으로 확인하며 궁금한 점을 묻는다.
- 구매하겠다, 주문하겠다, 주문하려고 한다, 사고 싶다는 말에는 제품명이 함께 나와도 검색하지 않는다. 구매 방법을 안내한다: 홈페이지에서 온라인으로 주문하거나 ARS 전화주문 {ARS_ORDER_NUMBER}번으로 주문할 수 있다. 전화 주문 번호는 이 ARS 번호로만 안내하고, 고객센터 번호(전화 문의용)를 주문 번호로 안내하지 않는다. 직접 주문하기 어렵다고 하면 "성함과 연락처를 남겨 주시면 담당 부서에서 전화로 주문을 도와드려요"처럼 전화로 도와준다는 것을 함께 안내하고, 성함과 연락처를 받으면 전화 요청을 접수한다.
- 같은 조회 결과가 이전 대화나 '이번 통화'의 미리 조회한 주문에 이미 있으면 다시 조회하지 않고 그 결과로 답한다.
- 접수 함수(cancel_order, return_order, return_order_item, exchange_order, request_callback)는 고객이 그 처리를 분명히 요청했고 필요한 정보를 모두 받았을 때만 호출한다. 가능한지 묻기만 했거나, 고민 중이거나, 정보가 하나라도 빠졌으면 호출하지 않는다. 혹시 몰라서 부르지 않는다. 잘못된 접수는 되돌릴 수 없다. 사유에는 고객이 말한 내용을 그대로 간추려 넣고 지어내지 않는다. 확인 질문은 최소로 한다: 조회는 묻지 않고 바로 하고(발신자 번호로 조회하면 "등록된 번호로 확인해 보니"처럼 말하고 번호는 읽지 않는다), 접수는 고객이 분명히 요청했고 정보가 다 있으면 "접수해 드릴까요"로 되묻지 않고 바로 하되 접수를 알릴 때 무엇을 접수했는지(주문일·상품, 전체·일부) 한 번 말해 고객이 바로잡을 수 있게 한다. 접수 전에 묻는 것은 대상 주문이 분명하지 않거나 고객이 가능한지만 물었을 때뿐이다.
- 입금했는데 확인이 안 된다고 하면 주문을 조회한다. 결과가 입금전이면 기다리라고만 하지 말고 입금하신 날짜, 입금자 성함, 입금 금액을 여쭤본다.
- 취소: 먼저 주문을 조회해 배송상태를 본다. 브랜드 정보 기준으로 취소할 수 있는 출고 전 상태이고 취소 사유를 들었으면 cancel_order를 호출한다. 사유를 모르면 사유만 묻는다. 이미 출고됐으면 호출하지 말고 받은 뒤 반품하는 절차를 안내한다. 입금 전 주문은 브랜드 정보의 직접 취소 방법을 안내한다.
- 배송지 변경: 주문을 조회해 상태를 본다. 주문이 여러 건이면 아직 받지 않은 가장 최근 주문이 대상이다(이미 받은 주문과 섞지 않는다). 출고 전(입금전, 상품준비중, 배송준비중)이면 바꿀 주소를 받고 상담원이 변경을 처리한다고 안내한다. 이미 출고됐으면(배송중, 배송완료) 출고된 상품은 배송지를 바꿀 수 없다고 안내한 뒤 상담원 연결을 안내한다. 배송지 변경 함수는 없으니 접수 함수를 부르지 않는다.
- 반품이 가능한지, 비용이 드는지만 물을 때는 접수하지 말고 브랜드 정보로 답한다: 자사몰 구매이고 받은 지 10일 이내면 환불보장제로 운임까지 무상 반품이라고 먼저 말하고, 그 밖의 경우에 사유에 따라 배송비가 달라질 수 있다고 덧붙인다. "환불 방법 문의"처럼 환불·반품 방법만 막연히 물으면 주문번호나 주문하신 분 연락처부터 묻는다.
- 반품·환불 요청은 다음 순서로 한다: 주문 조회 → 주문이 여러 건이면(취소된 주문 포함) 어느 주문인지 확인 → 반품 사유 듣기 → 전체 반품인지 일부 반품인지 확인 → 접수. 이미 끝난 단계는 건너뛰고, 고객에게 물어야 하는 단계에서 멈춰 그것 하나만 묻는다. 더 물을 것이 없으면 같은 차례에 접수까지 한다. 브랜드 정보의 반품 가능 기간을 조회 결과의 배송완료일과 현재 시각으로 따져서 지났으면 접수하지 않고 안내한다. 결제수단이 무통장입금이나 가상계좌처럼 브랜드 정보상 계좌로 환불되는 수단이면 접수 전에 환불 계좌(은행, 계좌번호, 예금주)를 받는다. 회수지는 따로 묻지 않고 고객이 다른 주소를 말했을 때만 넣는다. 주문한 상품을 전부 돌려보내면(고객이 전체, 전부, 다, 모두라고 했거나 주문한 상품이 한 가지뿐이면) 반드시 return_order에 주문번호를 넣는다. return_order_item은 여러 상품 중 일부만 돌려보낼 때만 쓰고 조회 결과의 상세주문번호와 수량을 넣는다. 단, 상품이 한 가지뿐인데 고객이 "일부 반품"이라고 직접 말했으면 일부 반품이 어렵다고 하지 말고 return_order_item에 그 상세주문번호와 수량 1을 넣어 접수하고, 접수를 알릴 때 '일부'라는 말은 쓰지 않는다. 전체 반품은 주문번호만 있으면 되므로 고객에게 상세주문번호를 묻지 않는다. 주문번호는 이전 대화(사진 분석 글 포함)나 조회 결과에서 찾고, 없으면 전화번호로 조회해 얻는다. 고객이 "반품해 주세요", "환불해 주세요"처럼 분명히 요청했고 조건이 맞으면 "접수해 드릴까요"라고 되묻지 말고 바로 접수한다. 반품 사유가 이전 대화에 이미 나왔으면 다시 묻지 않고 그 사유로 접수한다. 주문한 상품이 여러 가지여도 전부 반품이면 return_order를 한 번만 부른다. 접수 뒤에는 다음 네 가지를 이 순서로 빠뜨리지 않는다: 영업일 기준 3~4일 안에 회수 기사가 방문하니 제품을 다시 포장해 전달해 달라는 것, 자사몰 구매이고 받은 지 10일 이내면 환불보장제로 운임까지 무상이라는 것, 회수된 상품이 물류센터에 입고된 뒤 환불이 진행되고 최대 2주 정도 걸릴 수 있다는 것, 그 주문의 결제수단에 해당하는 환불 기간(다른 결제수단 내용은 섞지 않는다).
- 교환: 같은 상품으로 바꾸길 원하고 주문번호와 사유를 알 때만 exchange_order를 호출한다. 다른 제품이나 옵션으로 바꾸려는 것은 브랜드 정보대로 안내한다.
- 전화 요청: 고객이 전화 주문이나 상담원의 전화를 원하고 이름과 전화번호를 모두 받았을 때만 request_callback을 호출한다. 빠진 것이 있으면 그것만 묻는다. 이전 대화에서 받은 이름과 번호는 그대로 쓴다. 접수 뒤에는 브랜드 정보의 고객센터 영업 시간 안에 순서대로 연락드린다고 안내한다.

## 번호
- 주문번호(예: {_ORDER_EX}, 숫자 15~16자리)와 전화번호는 들은 그대로 인수에 넣는다. "공일공 일이삼사"처럼 한글로 읽은 번호도 직접 숫자로 바꾸지 말고 그대로 넣는다. 시스템이 숫자로 바꾼다. 자릿수가 이상해 보여도 스스로 고치거나 지어내지 않는다.
- 이전 대화의 함수 결과에 있는 주문번호와 상세주문번호는 글자 그대로 쓴다.
- 고객 말이나 이전 대화에 "[PHONE]"이라고 적힌 곳은 시스템이 가린 전화번호다. 고객은 번호를 말한 것이므로 다시 불러 달라고 하지 않는다. 그 번호가 필요한 함수는 인수에 "[PHONE]"을 그대로 넣어 부르면 시스템이 실제 번호로 바꿔 실행한다. 답에서 그 번호를 말해야 할 때(번호 확인, 전화 요청 접수)는 [PHONE]이라고 그대로 쓴다. 시스템이 실제 번호로 바꿔 읽는다.
- 발신자 정보의 전화번호는 참고용이다. 고객이 다른 번호를 말하면 고객이 말한 번호를 쓴다.
- 이번 고객 말에서 들은 전화번호로 전화 요청을 접수했으면, 접수했다고 말할 때 성함과 그 번호를 함께 알려 주고 다르면 말해 달라고 한다("홍길동 님, [PHONE]번으로 접수했어요. 성함이나 번호가 다르면 말씀해 주세요"). 번호 자리에는 [PHONE]만 쓰고 앞에 010 같은 숫자를 덧붙이지 않는다(시스템이 실제 번호 전체로 바꿔 읽는다). 잘못 들은 번호로 접수되면 연락이 가지 않으므로 고객이 바로 알아챌 수 있게 하려는 것이다. 조회가 성공했을 때와 발신자 번호나 이전 대화의 번호를 썼을 때는 번호를 읽지 않는다.
- 번호 확인 흐름: 들은 번호로 조회했는데 not_found이거나 시스템이 번호 형식이 맞지 않는다고 알려 주면, 들은 번호를 끊어 불러 주며 "…로 들었는데 맞으실까요?"라고 확인한다(시스템이 먼저 "제가 들은 번호는 …입니다. 맞으실까요?"라고 물었을 수 있다). 직전 상담원 말이 이런 번호 확인 질문이었고 고객이 "네", "맞아요"라고 하면 그 번호로 진행하되, 이미 조회해 없던 번호면 주문하실 때 쓴 다른 연락처나 주문번호를 묻는다. 고객이 "아니요"라고 하면 "죄송합니다. 번호를 천천히 한 번 더 불러 주시겠어요?"라고 한다. 두 번 다시 받아도 확인되지 않으면 더 묻지 말고 상담원 연결을 안내한다.
- 이전 대화에서 조회해 주문이 없던 번호가 있으면 고객이 같은 번호라고 하거나 번호를 말하다 말아도("전화번호는 010") 번호를 다시 불러 달라고 하지 않는다. 그 번호로는 주문이 확인되지 않는다고 말하고 다른 연락처나 주문번호를 요청한다.
- 그 밖에 번호를 되읽는 것은 꼭 필요할 때만 한다. 되읽을 때는 "{group_digits(_ORDER_EX, "order")}", "{group_digits(_PHONE_EX, "phone")}"처럼 쉼표로 끊는다.

## 함수 결과
- ok: 결과에 있는 사실을 결과의 말 그대로 정확히 전한다. 배송상태가 입금전이면 "아직 입금 전", 배송중이면 "배송 중"이라고 하고 "준비 중"처럼 뭉뚱그리지 않는다. 조회 결과를 처음 전하는 답에서만 그 상태에 맞는 브랜드 정보(입금 확인에 걸리는 시간, 출고와 배송 일정, 택배사 조회 방법, 환불 기간)를 한두 가지 덧붙인다(예: 입금전이면 아직 입금이 확인되지 않아 출고 전이라는 것과 무통장입금은 확인까지 20분 정도 걸릴 수 있다는 것). 출고 전 주문이면 주문 시각과 브랜드 정보의 출고 기준으로 출고 예정일과 받는 데 걸리는 기간을 구체적으로 말한다. 조회 결과가 고객이 말한 사실(수거한 날짜, 송장번호 등)과 맞지 않으면 번호를 다시 묻지 말고 상담원 확인이 필요하다고 안내한다. 조회한 바로 다음 답은 고객이 처음 원한 일에 맞춘다. 배송 문의면 상태를 안내하고, 반품 요청이면 다음 단계를 묻는다. 그 뒤의 물음에는 물은 것만 답하고 배송상태나 송장번호를 다시 붙이지 않는다.
- 주문이 여러 건이면 문의에 맞는 건(아직 받지 않은 주문, 고객이 말한 상품이나 날짜)을 골라 답한다. 고를 수 없으면 상품명과 주문일을 짧게 말하고 어느 주문인지 묻는다. 반품 현황은 조회된 건이 고객이 말한 주문이 맞는지 확인한다.
- not_found: 그 번호로는 확인되는 내역이 없다고 그대로 말한다. 이번에 음성으로 들은 번호였으면 조회한 번호를 끊어 읽어 주며 맞는지 확인한다(위의 번호 확인 흐름). 발신자 번호나 이전 대화의 번호였으면 주문할 때 쓴 다른 연락처나 주문번호를 묻는다. 반품 현황이 없으면 취소, 반품, 교환 중 어떤 접수였는지 묻는다.
- invalid: 필요한 값이 빠졌거나 번호 형식이 맞지 않는다는 뜻이다. 접수됐다고 말하지 않는다. 빠진 정보를 묻거나 번호를 천천히 다시 말씀해 달라고 한 번만 요청한다.
- already_requested: 이미 접수되어 있다고 알리고 다시 접수하지 않는다. 이후 진행을 브랜드 정보대로 안내한다.
- error: 처리하지 못했다고 사과하고 상담원 연결을 안내한다. 같은 접수 함수를 다시 부르지 않는다.

## 상담원 연결
- 상담원 연결은 함수가 없다. 말로 한다. "상담원에게 연결해 드리겠습니다"라고 말하고, 브랜드 정보의 전화 문의 항목(예: '[전화 문의 방법]')에 있는 고객센터 전화번호와 영업 시간을 함께 알려 준다. 현재 시각이 영업 시간 밖이면 지금은 상담원 연결이 어렵다는 것도 말한다. 연결을 안내한다는 이유로 request_callback을 부르지 않는다.
- 넘기는 경우: 고객이 "상담원 연결", "상담원 바꿔 주세요"처럼 상담원을 분명히 찾을 때(문의 내용을 함께 말했어도 조회로 대신하지 말고 연결을 안내한다). 건강 상태, 지병, 복용 중인 약, 부작용, 섭취 후 이상 증상처럼 전문 판단이 필요한 문의(브랜드 정보에 정해진 안내가 있으면 그 안내를 단정하지 않고 전한다). 오배송, 누락, 미수령(주문을 조회해 배송 상태를 확인하고 상황을 들은 뒤). 불량인지 판단이 필요한 문의. 배송지 변경(출고 전이면 바꿀 주소를 받고 상담원이 처리한다고 안내, 출고 뒤면 변경이 어렵다고 안내). 시나리오와 브랜드 정보로 해결할 수 없는 문의.
- 쿠팡 같은 다른 구매처에서 산 주문은 여기서 조회하거나 접수할 수 없다. 주문번호나 연락처를 묻지 말고, 구매하신 곳의 고객센터로 문의하시도록 안내하고 필요하면 상담원 연결을 안내한다.
- 넘기지 않는 경우: 조회, 접수, 브랜드 정보 안내로 끝낼 수 있는 문의. 번호만 받으면 진행할 수 있으면 번호부터 묻는다. 브랜드 정보가 상담원 연결을 안내하지 말라고 한 상황. 필요 없는 연결도 틀린 응대다."""

# ── 지식 폴더 ────────────────────────────────────────
_prefix_cache: dict[str, str] = {}
_style_cache: dict[str, dict] = {}


def reset() -> None:
    """캐시를 비운다(테스트용)."""
    _prefix_cache.clear()
    _style_cache.clear()


def resolve_knowledge_dir(knowledge_dir: str | Path | None = None) -> Path:
    """지식 폴더 경로. 인자 → DIRECTON_KNOWLEDGE_DIR → <repo>/data/brand → 공식 키트 순. 없으면 FileNotFoundError."""
    given = knowledge_dir or os.environ.get(KNOWLEDGE_ENV, "").strip()
    if given:
        p = Path(given).expanduser()
        if not p.is_dir() and not p.is_absolute() and (_ROOT / p).is_dir():
            p = _ROOT / p
        if not p.is_dir():
            raise FileNotFoundError(
                f"지식 폴더를 찾을 수 없습니다: {p} (scenarios.md, information.json, style.json이 있는 폴더여야 합니다)")
        return p.resolve()
    for p in _DEFAULT_DIRS:
        if (p / "information.json").is_file():
            return p.resolve()
    raise FileNotFoundError(
        "지식 폴더를 찾을 수 없습니다. " + " 또는 ".join(str(p) for p in _DEFAULT_DIRS)
        + f" 에 브랜드 자료를 두거나 환경변수 {KNOWLEDGE_ENV}로 폴더를 지정하세요.")


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig").strip() if path.is_file() else ""
    except OSError:
        return ""


def _load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON 형식이 잘못되었습니다: {path} ({e})") from e


def _load_style(d: Path) -> dict:
    key = str(d)
    if key not in _style_cache:
        path = d / "style.json"
        data = _load_json(path) if path.is_file() else {}
        _style_cache[key] = data if isinstance(data, dict) else {}
    return _style_cache[key]


def _info_entries(d: Path) -> list[tuple[str, str]]:
    path = d / "information.json"
    if not path.is_file():
        raise FileNotFoundError(f"브랜드 정보 파일이 없습니다: {path}")
    data = _load_json(path)
    out = []
    for e in data if isinstance(data, list) else []:
        if isinstance(e, dict) and e.get("tag"):
            out.append((str(e["tag"]).strip(), str(e.get("content") or "").strip()))
    return out


def _build_prefix(d: Path) -> str:
    parts = [RULES]
    scen = _read_text(d / "scenarios.md")
    if scen:
        scen = re.sub(r"(?m)^(#{1,5})(?=\s)", r"#\1", scen)       # 제목을 한 단계 낮춰 이 절 아래로 넣는다
        parts.append(f"{SCENARIO_HEADER}\n{scen}")
    entries = _info_entries(d)
    note = ("아래는 태그별 브랜드 정보다. 채팅 상담용 글이라 링크와 이모지가 섞여 있다. "
            "사실만 골라 전화 말투로 전하고 링크는 읽지 않는다.")
    parts.append(f"{INFO_HEADER}\n{note}\n\n" + "\n\n".join(f"### {tag}\n{content}" for tag, content in entries))
    style = str(_load_style(d).get("style") or "").strip()
    if style:
        parts.append(f"{STYLE_HEADER}\n{style}")
    # 자료 안에 경계 마커와 같은 글자가 있으면 고정부/가변부 경계가 흔들리므로 바꾼다
    return "\n\n".join(parts).replace(VAR_MARKER, "[이번 통화]")


def fixed_prefix(knowledge_dir: str | Path | None = None) -> str:
    """고정부(규칙 + 시나리오 + 브랜드 정보 전체 + 말투). 같은 폴더면 프로세스 동안 같은 문자열."""
    d = resolve_knowledge_dir(knowledge_dir)
    key = str(d)
    if key not in _prefix_cache:
        _prefix_cache[key] = _build_prefix(d)
    return _prefix_cache[key]


def greeting(knowledge_dir: str | Path | None = None) -> str:
    """style.json의 첫 인사. 없으면 기본 인사."""
    g = _load_style(resolve_knowledge_dir(knowledge_dir)).get("greeting")
    return g.strip() if isinstance(g, str) and g.strip() else DEFAULT_GREETING


# ── 시각 ─────────────────────────────────────────────
_NOW = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})(?:\s*\([^)]*\))?(?:[T\s]+(\d{1,2}):(\d{2}))?")


def parse_now(s: str) -> datetime | None:
    """"2026-11-04 (수) 12:39", "2026-11-21 11:00", "2026-11-21T11:00" → datetime. 실패하면 None."""
    m = _NOW.search(s) if isinstance(s, str) else None
    if not m:
        return None
    y, mo, d, h, mi = (int(x) if x else 0 for x in m.groups())
    try:
        return datetime(y, mo, d, h, mi)
    except ValueError:
        return None


def _day(t: datetime) -> str:
    return f"{t.month}월 {t.day}일 {_WEEKDAYS[t.weekday()]}요일"


# ── 이전 대화 정리 ───────────────────────────────────
_LINK_TAG = re.compile(r"<link>.*?</link>", re.S | re.I)
_URL = re.compile(r"(?:https?://|www\.)[^\s<>()\[\]\"']+", re.I)
_HTML_TAG = re.compile(r"</?[a-zA-Z][a-zA-Z0-9]{0,15}>")
_EMOJI = re.compile("[\U0001F000-\U0001FAFF←-⇿①-⓿■-◿☀-➿"
                    "⬀-⯿※™®️‍⃣]")
_SMILEY = re.compile(r"(?:(?<=\s)|^)[:;]-?\)|\^\^+")
_MD_MARK = re.compile(r"\*\*|__|`")
_MD_HEADING = re.compile(r"(?m)^[ \t]*#{1,6}[ \t]+")
_BULLET = re.compile(r"(?m)^[ \t]*[-*•·▪◦]+[ \t]*(?=[^\d\s])")
_CUSTOMER_ROLES = {"customer", "user", "human", "고객", "손님"}
_AGENT_ROLES = {"agent", "assistant", "bot", "상담원"}
_LOOKUP_FUNCS = ("get_order_info", "get_return_info")
_ACTION_FUNCS = ("cancel_order", "return_order", "return_order_item", "exchange_order", "request_callback")


def _squash(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def clean_agent_text(s: str) -> str:
    """채팅 상담 기록의 상담원 말을 전화 맥락용으로: 링크·이모지·목록 기호를 빼고 줄바꿈·공백을 한 칸으로."""
    if not isinstance(s, str):
        s = "" if s is None else str(s)
    s = _LINK_TAG.sub(" ", s)
    s = _URL.sub(" ", s)
    s = _HTML_TAG.sub(" ", s)
    s = _EMOJI.sub(" ", s)
    s = _SMILEY.sub(" ", s)
    s = _MD_MARK.sub("", s)
    s = _MD_HEADING.sub("", s)
    s = _BULLET.sub("", s)
    s = _squash(s)
    s = re.sub(r"\(\s+", "(", s)
    s = re.sub(r"\s+\)", ")", s)
    s = re.sub(r"\(\)|\[\]", "", s)
    return _squash(s)


def _text_of(h: dict) -> str:
    v = h.get("text")
    if v is None:
        v = h.get("content")
    return "" if v is None else (v if isinstance(v, str) else str(v))


def _dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


def _clip(s: str, n: int) -> str:
    return s if n <= 0 or len(s) <= n else s[:n] + "…(생략)"


def _maybe_json(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


def render_history(history: list[dict], max_result_chars: int = 1500) -> list[str]:
    """이전 대화를 줄 목록으로. 고객·상담원 말, 함수 호출·결과(압축 JSON, 길면 자름). 모르는 모양은 넘어가거나 그대로 적는다."""
    out: list[str] = []
    for h in history or []:
        if not isinstance(h, dict):
            t = _squash(str(h)) if h is not None else ""
            if t:
                out.append(f"- {t}")
            continue
        role = str(h.get("role") or "").strip()
        name = str(h.get("name") or "?")
        if role == "tool_call":
            args = _maybe_json(h.get("arguments"))
            if isinstance(args, dict):
                args = {k: v for k, v in args.items() if v is not None}
            out.append(f"- (함수 호출 {name}, 인수 {_clip(_dumps(args if args is not None else {}), max_result_chars)})")
        elif role == "tool_result":
            res = h.get("result")
            if res is None:
                res = _text_of(h)
            res = _maybe_json(res)
            shown = _clip(_squash(_EMOJI.sub("", _dumps(res))), max_result_chars) if res not in ("", None) else "결과 없음"
            out.append(f"- (함수 결과 {name}: {shown})")
        elif role in _AGENT_ROLES:
            t = clean_agent_text(_text_of(h))
            if t:
                out.append(f"- 상담원: {t}")
        elif role in _CUSTOMER_ROLES:
            t = _squash(_EMOJI.sub(" ", _text_of(h)))
            if t:
                out.append(f"- 고객: {t}")
        else:
            t = _squash(_text_of(h))
            if t:
                out.append(f"- ({role or '기타'}: {t})")
    return out


# 주문번호: 20260924-0035429(8자리-7자리, 공백·하이픈 허용) 또는 붙여 쓴 15~16자리. 전화번호: 01X + 3~4자리 + 4자리.
_ORDER_DASH = re.compile(r"(?<!\d)(20\d{6})\s?-?\s?(\d{7})(?!\d)")
_ORDER_RUN = re.compile(r"(?<!\d)\d{15,16}(?!\d)")
_PHONE = re.compile(r"(?<!\d)(01[016789])[\s\-.,]{0,3}(\d{3,4})[\s\-.,]{0,3}(\d{4})(?!\d)")
# 주문번호·전화번호로 헷갈릴 수 있는 값(상세주문번호는 앞부분이 주문번호와 다른 기록이 있다)
_SKIP_KEYS = {"account_no", "계좌번호", "송장번호", "상세주문번호", "order_item_code"}


def _strings(obj):
    """중첩된 값에서 문자열을 나온 순서대로."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, bool) or obj is None:
        return
    elif isinstance(obj, (int, float)):
        yield str(obj)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if k not in _SKIP_KEYS:
                yield from _strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _strings(v)


def _scan_numbers(text: str, orders: list[str], phones: list[str]) -> None:
    found = [(m.start(), "o", m.group(1) + m.group(2)) for m in _ORDER_DASH.finditer(text)]
    found += [(m.start(), "o", m.group(0)) for m in _ORDER_RUN.finditer(text)]
    found += [(m.start(), "p", "".join(m.groups())) for m in _PHONE.finditer(text)]
    for _, kind, digits in sorted(found):
        bucket = orders if kind == "o" else phones
        if digits not in bucket:
            bucket.append(digits)


def history_numbers(history: list[dict]) -> dict:
    """이전 대화(말, 함수 인수, 함수 결과)에 나온 주문번호·전화번호를 숫자만, 나온 순서대로, 중복 없이."""
    orders: list[str] = []
    phones: list[str] = []
    for h in history or []:
        if isinstance(h, dict):
            role = h.get("role")
            if role == "tool_call":
                src = _maybe_json(h.get("arguments"))
            elif role == "tool_result":
                src = _maybe_json(h.get("result")) if h.get("result") is not None else _text_of(h)
            else:
                src = _text_of(h)
        else:
            src = h
        for s in _strings(src):
            _scan_numbers(s, orders, phones)
    return {"orders": orders, "phones": phones}


def _fmt_order(d: str) -> str:
    return f"{d[:8]}-{d[8:]}" if len(d) == 15 else d


def _history_summary(history: list[dict]) -> list[str]:
    items = [h for h in history or [] if isinstance(h, dict)]
    if not items:
        return []
    out: list[str] = []
    nums = history_numbers(items)
    known = [f"주문번호 {_fmt_order(o)}" for o in nums["orders"]] + [f"전화번호 {p}" for p in nums["phones"]]
    if known:
        out.append(f"- 이미 나온 번호: {', '.join(known)}. 다시 묻지 말고 그대로 쓴다.")
    done = [str(h.get("name")) for h in items if h.get("role") == "tool_result" and h.get("name")]
    looked = [n for n in _LOOKUP_FUNCS if n in done]
    if looked:
        out.append(f"- 이미 조회한 결과({', '.join(looked)})가 위 대화에 있다. 같은 번호로 다시 조회하지 말고 그 결과로 답한다.")
    acted = [n for n in _ACTION_FUNCS if n in done]
    if acted:
        out.append(f"- 이미 접수한 처리({', '.join(acted)})가 위 대화에 있다. 같은 접수를 다시 하지 않는다.")
    last = items[-1]
    if last.get("role") in _AGENT_ROLES:
        t = clean_agent_text(_text_of(last))
        if t:
            out.append(f"- 직전 상담원 말: \"{_clip(t, 300)}\" 이번 고객 말은 이 말에 대한 답일 수 있다.")
    return out


# ── 시스템 프롬프트 ──────────────────────────────────
def _caller_line(caller) -> str:
    c = caller if isinstance(caller, dict) else {}
    name = str(c.get("name") or "").strip()
    phone = str(c.get("phone") or "").strip()
    if not name and not phone:
        return "- 발신자 정보: 없음(걸려 온 번호로 찾은 회원이 없다). 번호는 조회나 접수에 실제로 필요해졌을 때만 묻는다."
    desc = ", ".join(x for x in (f"이름 {name}" if name else "", f"전화번호 {phone}" if phone else "") if x)
    line = (f"- 발신자 정보(참고용): {desc}. 발신 번호로 찾은 회원 정보다. "
            "다른 번호로 주문했을 수 있으니 고객이 다른 번호를 말하면 그 번호를 쓴다.")
    if phone:
        line += " 고객이 번호를 말하지 않았으면 조회에는 이 전화번호를 쓰고 번호를 다시 묻지 않는다."
    else:
        line += " 전화번호는 없으므로 조회가 필요하면 번호를 묻는다."
    return line


def build_system(info: SessionInfo, opener: str = "", preload: str = "",
                 knowledge_dir: str | Path | None = None, prefix: str | None = None) -> str:
    """고정부 + `## 이번 통화`(시각, 발신자, 이전 대화, 번호·조회 요약, 미리 조회한 주문, 마지막 줄에 첫마디).
    prefix를 주면 지식 파일을 읽지 않고 그 문자열을 고정부로 쓴다(테스트·주입용)."""
    var: list[str] = []
    now = (getattr(info, "now", "") or "").strip()
    if now:
        t = parse_now(now)
        line = f"- 현재 시각: {now}."
        if t:
            line += f" 오늘은 {_day(t)}, 내일은 {_day(t + timedelta(days=1))}이다."
        var.append(line)
        if t:
            ago = ", ".join(f"{n}일 전은 {_day(t - timedelta(days=n))}" for n in (7, 10, 14, 30))
            var.append(f"- 기간 계산: 오늘로부터 {ago}이다. 받은 날짜가 그 날짜와 같거나 그보다 뒤면 그 기간 이내다. "
                       "기간을 따질 때는 직접 셈하지 말고 이 날짜와 비교한다.")
    caller = getattr(info, "caller", None)
    var.append(_caller_line(caller))
    history = getattr(info, "history", None) or []
    hist = render_history(history)
    if hist:
        var.append("- 이전 대화(오래된 것부터):")
        var.extend("  " + line for line in hist)
        var.extend(_history_summary(history))
    preload = (preload or "").strip()
    if preload:
        phone = str((caller or {}).get("phone") or "").strip() if isinstance(caller, dict) else ""
        var.append(f"- 발신 번호로 미리 조회한 주문(통화 시작 때 발신자 전화번호{' ' + phone if phone else ''}로 "
                   "get_order_info를 호출한 결과):")
        var.extend("  " + line for line in preload.splitlines() if line.strip())
        var.append("  이 결과는 발신 번호 기준이다. 고객이 이 번호의 주문을 물으면 다시 조회하지 말고 이 결과로 답한다. "
                   "고객이 다른 전화번호나 여기에 없는 주문번호를 말하면 그 번호로 다시 조회한다.")
    opener = _squash(opener or "")
    if opener:
        var.append(f"- 너는 이미 '{opener}'라고 말했다. 같은 말을 반복하지 말고 그 뒤에 이어서 본론부터 말한다. "
                   "호칭이나 인사를 다시 넣지 않는다.")
    body = "\n".join(var).replace(VAR_MARKER, "이번 통화")
    head = fixed_prefix(knowledge_dir) if prefix is None else prefix.replace(VAR_MARKER, "이번 통화")
    return f"{head}\n\n{VAR_MARKER}\n{body}"
