"""AG-T20 첫 마디 정책(순수 함수): 상황별 후보, 연속 반복 금지, 답이 준비되면 첫 마디 생략, 필러 교대."""
from agent.config import Settings
from agent.tts_cache import (OPENER_KINDS, OpenerChoice, choose_filler, choose_opener, opener_candidates,
                             opener_kind, opener_phrases)

S = Settings()
CANDS = {k: opener_candidates(k, S) for k in OPENER_KINDS}


def test_opener_kind_from_text():
    assert opener_kind("물건이 깨져서 왔어요. 너무 화나네요", S) == "apology"
    assert opener_kind("주문 조회 좀 해 주세요", S) == "lookup"
    assert opener_kind("주문번호 이육일일 일오공 이삼일이요", S) == "lookup"
    assert opener_kind("영업시간이 어떻게 돼요?", S) == "simple"
    assert opener_kind("", S) == "simple"


def test_lookup_without_identifier_is_simple():
    # 번호가 아직 없으면 곧 번호를 되물을 턴이라 조회 첫 마디("확인해 드릴게요")를 쓰지 않는다
    assert opener_kind("혹시 제 주문이 어디쯤 왔을까요?", S, has_identifier=False) == "simple"
    assert opener_kind("혹시 제 주문이 어디쯤 왔을까요?", S, has_identifier=True) == "lookup"
    assert opener_kind("주문번호 이육일일 일오공 이삼일이요", S, has_identifier=False) == "lookup"   # 이번 말에 번호가 있음
    assert opener_kind("물건이 깨져서 왔어요", S, has_identifier=False) == "apology"


def test_candidates_start_with_settings_phrase_and_have_variety():
    assert CANDS["apology"][0] == S.opener_apology
    assert CANDS["lookup"][0] == S.opener_lookup
    assert CANDS["simple"][0] == S.openers[0]
    for k in OPENER_KINDS:
        assert len(CANDS[k]) >= 3, k
        assert len(set(CANDS[k])) == len(CANDS[k]), k


def test_opener_phrases_cover_all_candidates():
    phrases = opener_phrases(S)
    for k in OPENER_KINDS:
        for c in CANDS[k]:
            assert c in phrases
    assert len(set(phrases)) == len(phrases)


def test_answer_ready_skips_opener():
    c = choose_opener("simple", answer_ready=True, candidates=CANDS)
    assert c == OpenerChoice(None, "simple", "answer_ready")
    # 조회 문장이라도 선합성 첫 문장이 이미 있으면 생략(그 문장이 곧 첫 소리)
    assert choose_opener("lookup", answer_ready=True, candidates=CANDS).text is None


def test_not_ready_uses_kind_specific_opener():
    assert choose_opener("lookup", answer_ready=False, candidates=CANDS).text == S.opener_lookup
    assert choose_opener("apology", answer_ready=False, candidates=CANDS).text == S.opener_apology
    c = choose_opener("simple", answer_ready=False, candidates=CANDS)
    assert c.text == S.openers[0] and c.reason == "not_ready"


def test_tool_expected_turns_simple_into_lookup():
    c = choose_opener("simple", answer_ready=False, tool_expected=True, candidates=CANDS)
    assert c.kind == "lookup" and c.text in CANDS["lookup"] and c.reason == "tool_expected"


def test_never_repeats_last_opener_and_rotates():
    recent: list[str] = []
    used = []
    for _ in range(6):
        c = choose_opener("lookup", answer_ready=False, recent=recent, candidates=CANDS)
        assert c.text is not None
        if recent:
            assert c.text != recent[-1]
        recent.append(c.text)
        used.append(c.text)
    assert len(set(used)) >= 3                      # 세 가지 이상 돌아가며 쓴다


def test_prefers_not_used_in_last_two():
    a, b, _ = CANDS["simple"][:3]
    c = choose_opener("simple", answer_ready=False, recent=[a, b], candidates=CANDS)
    assert c.text not in (a, b)


def test_only_cached_candidates_and_no_repeat_means_no_opener():
    only = S.openers[0]
    cached = lambda t: t == only  # noqa: E731
    c1 = choose_opener("simple", answer_ready=False, recent=[], candidates=CANDS, is_cached=cached)
    assert c1.text == only
    c2 = choose_opener("simple", answer_ready=False, recent=[only], candidates=CANDS, is_cached=cached)
    assert c2.text is None and c2.reason == "no_fresh_opener"


def test_falls_back_to_simple_when_kind_has_no_cached_phrase():
    cached = lambda t: t in CANDS["simple"]  # noqa: E731
    c = choose_opener("apology", answer_ready=False, candidates=CANDS, is_cached=cached)
    assert c.text in CANDS["simple"] and c.reason == "fallback_simple"


def test_preferred_used_when_allowed():
    pref = CANDS["lookup"][1]
    c = choose_opener("lookup", answer_ready=False, candidates=CANDS, preferred=pref)
    assert c.text == pref
    c2 = choose_opener("lookup", answer_ready=False, recent=[pref], candidates=CANDS, preferred=pref)
    assert c2.text != pref                           # 직전과 같으면 선호도 무시


def test_choose_filler_alternates_and_avoids_overlap_with_opener():
    cands = (*S.fillers, S.opener_lookup_filler)
    f1 = choose_filler(cands, opener="", last=None)
    assert f1 == cands[0]
    f2 = choose_filler(cands, opener="", last=f1)
    assert f2 != f1
    # "네, 확인해 드릴게요." 뒤에 "확인해 드릴게요, 잠시만요." 겹침을 피한다
    f3 = choose_filler(cands, opener=S.opener_lookup, last=None)
    assert f3 == S.opener_lookup_filler
    # 후보가 하나뿐이면 반복이 침묵보다 낫다
    assert choose_filler(("잠시만요.",), opener="", last="잠시만요.") == "잠시만요."
    assert choose_filler((), opener="", last=None) is None
    assert choose_filler(cands, opener="", last=None, is_cached=lambda t: False) is None


def test_needs_number_only_for_order_requests_not_policy_questions():
    from agent.tts_cache import needs_number_only
    assert needs_number_only("주문한 게 어디쯤 왔는지 궁금해서요.")
    assert needs_number_only("어제 주문한 거 취소하고 싶은데요.")
    assert not needs_number_only("반품하려면 며칠 안에 해야 돼요?")          # 정책 질문
    assert not needs_number_only("취소 가능한가요?")
    assert not needs_number_only("주문번호 이공이육 일공일공 공공일구 팔칠오요. 어디쯤이에요?")   # 번호가 있다
    assert not needs_number_only("배송이 안 와서 너무 화나요")              # 사과가 먼저
    assert not needs_number_only("영업시간이 어떻게 돼요?")
