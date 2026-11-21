"""말하는 소리 조각 사이 쉼(문장 사이 쉼): 이미 말한 것이 있으면 다음 조각 앞에 고정 쉼, 조각 끝 무음만큼은 덜 채운다."""
from agent.core.types import Message
from agent.responder import Responder, _Speaker, _tail_silence_ms, sentence_gap_ms

from test_responder import FakeLLM, FakeTTS, Sink, pcm_for, settings, text_events

VOICED = b"\x10\x27" * 320            # 20ms, 진폭 10000
SILENT = b"\x00\x00" * 320


def zeros_ms(ms):
    return b"\x00" * (ms * 32)


async def test_gap_before_every_sentence_after_opener():
    sink = Sink()
    llm = FakeLLM(text_events("배송 중입니다. 내일 도착해요."))
    r = Responder(llm, FakeTTS(), None, settings=settings(sentence_gap_ms=200, tts_combine=False))
    await r.respond("S", [Message("user", "x")], sink, spoken=["네, 고객님."])
    a, b = pcm_for("배송 중입니다."), pcm_for("내일 도착해요.")
    assert sink.data == zeros_ms(200) + a + zeros_ms(200) + b


async def test_no_gap_before_first_sound_of_turn():
    sink = Sink()
    llm = FakeLLM(text_events("배송 중입니다. 내일 도착해요."))
    r = Responder(llm, FakeTTS(), None, settings=settings(sentence_gap_ms=200, tts_combine=False))
    await r.respond("S", [Message("user", "x")], sink)
    a, b = pcm_for("배송 중입니다."), pcm_for("내일 도착해요.")
    assert sink.data == a + zeros_ms(200) + b          # 첫 소리는 바로, 둘째 문장 앞에만 쉼


async def test_trailing_silence_counts_toward_gap():
    sink = Sink()
    sp = _Speaker(FakeTTS(), sink, ["네, 고객님."], settings(sentence_gap_ms=300), None)
    sp.play("확인해 볼게요.", VOICED * 5 + SILENT * 5)  # 끝에 무음 100ms
    sp.say("배송 중입니다.")
    await sp.finish()
    data = sink.data
    first = zeros_ms(300) + VOICED * 5 + SILENT * 5      # 첫 조각 앞: 이미 첫 마디를 말했으니 300ms
    assert data.startswith(first)
    assert data[len(first):] == zeros_ms(200) + pcm_for("배송 중입니다.")   # 300 - 끝 무음 100


def test_gap_setting_and_tail_measure():
    assert sentence_gap_ms(settings(sentence_gap_ms=0)) == 0
    assert sentence_gap_ms(settings(sentence_gap_ms=250)) == 250
    assert _tail_silence_ms(VOICED + SILENT * 3, 0) == 60
    assert _tail_silence_ms(SILENT * 2, 40) == 80
    assert _tail_silence_ms(VOICED, 100) == 0



async def test_real_idle_playback_counts_toward_gap():
    sink = Sink()
    sp = _Speaker(FakeTTS(), sink, ["네, 고객님."], settings(sentence_gap_ms=800), None, play_idle_ms=lambda: 500.0)
    sp.say("배송 중입니다.")
    await sp.finish()
    assert sink.data == zeros_ms(300) + pcm_for("배송 중입니다.")   # 이미 0.5초 조용했으니 0.3초만


async def test_no_extra_gap_when_already_silent_long_enough():
    sink = Sink()
    sp = _Speaker(FakeTTS(), sink, ["네, 고객님."], settings(sentence_gap_ms=800), None, play_idle_ms=lambda: 2000.0)
    sp.say("배송 중입니다.")
    await sp.finish()
    assert sink.data == pcm_for("배송 중입니다.")


def test_default_gap_is_800ms(monkeypatch):
    from agent import responder
    monkeypatch.setattr(responder, "DEFAULT_SENTENCE_GAP_MS", 800)
    assert sentence_gap_ms(settings()) == 800
