import os, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


import pytest


@pytest.fixture(autouse=True)
def _mini_knowledge(monkeypatch):
    """테스트는 공식 키트 대신 지어낸 작은 지식 폴더를 쓴다(키트가 없는 환경에서도 같은 결과)."""
    from agent import knowledge

    monkeypatch.setenv(knowledge.KNOWLEDGE_ENV, str(ROOT / "tests" / "fixtures" / "official_mini" / "brand"))
    knowledge.reset()
    yield
    knowledge.reset()


@pytest.fixture(autouse=True)
def _legacy_order_digits(monkeypatch, request):
    """턴 감지 테스트의 예문은 10자리 주문번호로 쓰여 있다(로직은 같다). 그 모듈에서만 자릿수 기준을 10으로 둔다.
    실제 기본값은 공식 형식의 15자리(agent.turn.rules.ORDER_DIGITS)."""
    if request.module.__name__ in ("test_turn", "test_turn_r2", "test_jev_turn"):
        from agent.turn import detector, rules
        monkeypatch.setattr(rules, "ORDER_DIGITS", 10)
        monkeypatch.setattr(detector, "ORDER_DIGITS", 10)


@pytest.fixture(autouse=True)
def _no_sentence_gap(monkeypatch, request):
    """문장 사이 쉼은 송신 바이트를 바꾸므로 기존 테스트는 끈다. 쉼 테스트는 Settings(sentence_gap_ms=…)로 직접 켠다."""
    from agent import responder
    monkeypatch.setattr(responder, "DEFAULT_SENTENCE_GAP_MS", 0)
