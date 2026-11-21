"""AG-T55 의도 추정. 연습 문항이 아닌 지어낸 문장만 쓴다."""
from agent import intents


def top(text, history=None):
    e = intents.estimate(text, history)
    return e.top, e.confident


def test_refund_request_vs_status():
    assert top("환불해 주세요") == ("return_request", True)
    assert top("전부 반품 부탁드려요")[0] == "return_request"
    t, conf = top("반품 보냈는데 환불 아직 안 들어왔어요")
    assert t == "return_status" and conf
    assert top("환불 신청했는데 언제 되나요")[0] == "return_status"


def test_refund_never_gets_delivery_playbook():
    for s in ("환불하고 싶어요", "반품 신청합니다", "환불 언제 돼요"):
        assert top(s)[0] != "delivery"


def test_policy_question_vs_request():
    assert top("뜯어서 먹어 봤는데 환불 가능한가요? 비용 드나요?")[0] == "return_policy"
    # 가능하냐고 묻다가 해 달라고 하면 신청
    assert top("반품 가능한가요? 가능하면 환불해 주세요")[0] == "return_request"


def test_traps_no_wrong_hint():
    # 다른 쇼핑몰과 가격 비교는 다른 구매처 주문이 아니다
    t, conf = top("쿠팡보다 비싸네요")
    assert t != "other_channel"
    # 눈 영양제를 사고 싶다는 말은 건강 상담이 아니다
    assert top("눈약 사고 싶은데요")[0] != "health"
    # 결제 수단 문의는 구매 안내가 아니라 정책 안내
    assert top("카드로 결제는 안 되나요? 카드로 사고 싶어요")[0] == "policy"
    # 취소한 주문의 환불은 반품 현황 안내로 보내지 않는다
    t, conf = top("취소했는데 환불이 안 됐어요")
    assert not (t == "return_status" and conf)


def test_other_channel_and_agent():
    assert top("쿠팡에서 산 거 반품이 안 돼요")[0] == "other_channel"
    assert top("상담원 연결해 주세요") == ("agent", True)


def test_greeting_and_number_only_give_no_hint():
    assert not intents.estimate("안녕하세요").confident
    assert not intents.estimate("010 1111 2222").confident
    assert intents.hint_line(intents.estimate("안녕하세요")) == ""


def test_slot_answer_carries_previous_intent():
    hist = [{"role": "customer", "text": "환불하고 싶어요"},
            {"role": "agent", "text": "주문번호나 주문하신 분 연락처를 말씀해 주시겠어요?"}]
    e = intents.estimate("010 1111 2222요", hist)
    assert e.top == "return_request" and e.confident and e.slot_answer
    assert "이어지는 답" in intents.hint_line(e)


def test_status_cue_from_agent_question():
    hist = [{"role": "customer", "text": "환불 건 문의요"},
            {"role": "agent", "text": "환불 진행 현황 확인을 위해 성함과 전화번호를 알려 주시겠어요?"}]
    assert top("홍길동 010 1111 2222", hist)[0] == "return_status"
    hist[0]["text"] = "환불이 아직 안 됐어요"
    assert top("홍길동 010 1111 2222", hist) == ("return_status", True)


def test_contact_after_purchase_is_callback():
    hist = [{"role": "customer", "text": "구매하고 싶어요"},
            {"role": "agent", "text": "홈페이지나 ARS로 주문하실 수 있어요."}]
    assert top("홍길동 010 1111 2222", hist)[0] == "callback"


def test_probabilities_sum_to_one():
    e = intents.estimate("배송 언제 와요?")
    assert abs(sum(e.probs.values()) - 1.0) < 1e-9
    assert e.top == "delivery" and e.confident


def test_switch_off(monkeypatch):
    monkeypatch.setenv(intents.ENV, "0")
    assert intents.system_suffix("환불해 주세요") == ""
    monkeypatch.setenv(intents.ENV, "1")
    s = intents.system_suffix("환불해 주세요")
    assert s.startswith("\n- 요청 추정") and "반품·환불 신청" in s


def test_hint_has_no_var_marker():
    from agent.knowledge import VAR_MARKER
    assert VAR_MARKER not in intents.system_suffix("배송 언제 와요?")
