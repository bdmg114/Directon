"""상담원 서버 설정. 턴 감지 파라미터는 detector가 가진다. 여기는 응답·세션 동작값만 둔다.

환경변수(접두사 DIRECTON_)로 덮어쓸 수 있다. 문구 목록은 '|'로 구분한다.
AG-T25: 바지인·속도 조절·첫 마디 정책 값도 여기 둔다(DIRECTON_BARGE_IN, DIRECTON_BARGE_IN_MS, DIRECTON_PACE_LEAD_MS,
DIRECTON_OPENER_ADAPTIVE 등).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, fields
from typing import Mapping

ENV_PREFIX = "DIRECTON_"
# 당일 체크리스트에서 쓰는 짧은 이름 → 필드
ENV_ALIASES: dict[str, str] = {}


@dataclass
class Settings:
    # 캐시 문구
    openers: tuple[str, ...] = ("네, 고객님.",)
    fillers: tuple[str, ...] = ("확인해 드릴게요, 잠시만요.",)
    apology: str = "죄송합니다, 잠시 확인이 어렵습니다. 잠시 후 다시 말씀해 주시겠어요?"
    reprompt: str = "죄송합니다, 잘 듣지 못했어요. 한 번만 더 말씀해 주시겠어요?"
    opener_use_name: bool = False       # True면 "네, ○○ 고객님."(실시간 합성 필요)
    # 텍스트 기반 첫 마디(AG-T10c): 확정 시점 텍스트의 키워드로 고른다(tts_cache.select_opener_for_text).
    # 캐시에 없으면 기본 첫 마디를 쓴다. 조회 첫 마디를 쓴 턴의 툴 필러는 opener_lookup_filler로 바꾼다("확인해" 반복 방지).
    opener_by_text: bool = True
    opener_apology: str = "네, 불편을 드려 죄송합니다."
    opener_lookup: str = "네, 확인해 드릴게요."
    opener_lookup_filler: str = "잠시만 기다려 주세요."

    # 타이밍
    precall_silence_ms: int = 200       # 이 침묵에서 finalize·Smart Turn·LLM 선호출 시작
    precall_refresh: bool = True        # AG-T10c: finalize 후 선호출 재시작·정규화 비교·첫 문장 선합성·결정기에 finalize 텍스트(False면 이전 동작)
    min_speech_ms: int = 60             # 이보다 짧은 음성은 발화로 보지 않는다(잡음 클릭)
    vad_threshold: float = 0.5
    filler_delay_ms: int = 150          # 툴 회차 시작부터 이 안에 다음 LLM 텍스트가 없으면 필러 재생(AG-T10a)
    filler_skip_tools: tuple[str, ...] = ("transfer_to_agent",)   # 이 함수를 부르는 회차엔 필러 없음
    safety_timeout_s: float = 20.0      # 확정 → turn.end 전체 상한
    llm_timeout_s: float = 8.0          # LLM 이벤트 사이 최대 대기
    tts_timeout_s: float = 12.0         # 문장 TTS 첫 바이트 최대 대기(게이트웨이 TTS는 문장당 2~4초, 동시 4세션이면 더 걸린다)
    finalize_timeout_s: float = 1.0
    empty_stt_wait_s: float = 1.5       # 말소리는 있었는데 받아쓰기가 빈 채 끝나면 끝맺음을 한 번 더 요청하고 이만큼 글을 기다린다(0이면 끔)
    empty_stt_min_speech_ms: int = 400  # 위 대기를 거는 최소 말소리 길이. 이보다 짧으면 잡음으로 보고 바로 다시 묻는다
    tools_timeout_s: float = 5.0        # GET /tools 대기

    # 소음 속 말 끝(AG-T55). 주변 말소리·TV처럼 VAD가 음성으로 보는 소음이 있으면 침묵이 쌓이지 않아 확정이 안전 상한까지 밀렸다.
    resume_min_ms: int = 100            # 침묵이 시작된 뒤 음성이 이만큼 이어져야 말이 다시 시작된 것으로 본다. 더 짧은 소리는 침묵으로 센다(0이면 끔)
    speaker_gate_db: float = 0.0        # 손님 목소리 크기(확실한 음성 프레임 에너지의 75분위)보다 이만큼 이상 작은 음성 프레임은 침묵으로 본다(0이면 끔, 제안 12)
    speaker_gate_min_ms: int = 600      # 손님 목소리 크기를 믿기 시작하는 최소 누적 음성 길이
    text_stall_end_ms: int = 0          # VAD는 음성인데 받아쓰기 글이 이만큼 그대로면 그때부터를 침묵으로 보고 결정기 규칙으로 확정(0이면 끔, 제안 1500)
    text_stall_cap_ms: int = 3000       # 같은 상황(글 있음)이 이만큼 이어지면 규칙과 상관없이 확정한다(0이면 끔). 깨끗한 음성은 말하는 동안 글이 계속 바뀐다

    # 응답
    max_tool_rounds: int = 3            # LLM 호출 최대 횟수(마지막 회차는 툴 없이)
    sentence_gap_ms: int = -1           # 말하는 소리 조각(첫 마디·채움말·결과 문구·답 문장) 사이 쉼. 음수면 responder.DEFAULT_SENTENCE_GAP_MS(300), 0이면 끔
    tts_combine: bool = True            # 회차의 첫 문장 뒤 문장들은 묶어서 한 번에 합성(게이트웨이 TTS는 스트리밍이 없고 분당 요청 한도가 있다)
    preload_orders: bool = False        # 연결 직후 발신자 전화번호로 주문을 미리 조회해 맥락에 넣는다(AG-T34). 기본 끔:
                                        # 2026-11-21 텍스트 평가 80문항에서 켜면 응대 65.9 → 57.2(주문과 무관한 문의에 조회 기록이 붙어 감점)
    preload_wait_s: float = 1.5         # 응답을 시작할 때 미리 조회가 아직 안 끝났으면 이만큼 기다린다
    post_tool_bridge: bool = True       # 함수 결과를 받은 즉시 캐시 문구를 먼저 말한다(TTS가 문장당 2~3초라 답 전 침묵을 줄인다)
    post_tool_ok: str = "네, 확인했어요."  # 결과가 ok일 때의 문구. 실패 결과는 agent.knowledge.FAIL_SENTENCES
    post_tool_more: str = "조금만 더 확인해 볼게요."   # 같은 턴의 두 번째 함수 회차 결과(ok) 뒤 문구(같은 말 반복 방지)
    confirm_heard_number: bool = True   # AG-T56: 들은 번호를 못 쓰거나(검사에서 막힘) 그 번호로 주문이 없으면 LLM 대신 "제가 들은 번호는 …입니다. 맞으실까요?"로 되물어 턴을 끝낸다
    answer_cache: bool = True           # AG-T56: 답 문장이 캐시에 있는 문구와 같으면 TTS 없이 캐시 음성을 바로 낸다
    answer_phrases: tuple[str, ...] = ("주문번호나 주문하신 분 연락처를 말씀해 주시겠어요?", "어떤 도움이 필요하신가요?",
                                       "확인해 드리겠습니다.", "맞으실까요?")   # 서버 시작 때 미리 합성할 자주 쓰는 답 문장
    ground_number_args: bool = True     # LLM 번호 인자를 고객 발화의 번호에 맞춘다(AG-T10a)
    validate_number_length: bool = True # 스키마 설명의 'N자리'와 다른 번호는 툴에 보내지 않는다(AG-T10a)
    ensure_transfer_call: bool = True   # 상담원 연결을 말했는데 transfer_to_agent가 없으면 부른다(AG-T10a)
    max_response_chars: int = 300       # 한 응답 상한(공백 제외 글자). 넘으면 LLM 스트림을 끊는다(폭주 생성 방지, AG-T10a)
    min_sentence_chars: int = 6         # 이보다 짧은 문장 조각은 다음과 합친다
    out_chunk_bytes: int = 3200         # 송신 청크(100ms)
    tts_level_rms: float = 3000.0       # 캐시·실시간 TTS 음성 구간 목표 RMS(0이면 정규화 끔)
    smart_turn_window_s: float = 8.0
    stt_context: str = ""

    # 바지인·첫 마디(AG-T19·T20, AG-T25에서 설정으로 옮김). session.start의 "barge_in"/"opener_adaptive"가 있으면 그쪽이 우선.
    # 기본값은 agent.session.SESSION_DEFAULTS와 같다(tests/test_wiring_t25.py).
    barge_in: bool = True               # DIRECTON_BARGE_IN=0으로 끔
    barge_in_ms: int = 300              # 이만큼 연속 음성이면 끼어들기
    barge_in_gap_ms: int = 60           # 연속 음성 사이 이 이하 끊김은 이어진 것으로
    barge_in_arm_ms: int = 1000         # 첫 소리 뒤 이 시간 이후에 시작된 말만 끼어들기로 본다
    barge_in_preroll_ms: int = 200      # 끼어든 말 시작 전 이만큼을 새 턴 STT에 같이 넣는다
    pace_lead_ms: int = 1500            # 응답 오디오를 재생 시계보다 이 이상 앞서 보내지 않는다(0이면 끔)
    pace_cap_s: float = 10.0            # 확정 뒤 이 시간이 지나면 속도 조절 없이 보낸다(turn.end 25초 한도)
    opener_adaptive: bool = False       # AG-T20 첫 마디 정책. 측정 전까지 끔(사용자 결정, DIRECTON_OPENER_ADAPTIVE=1로 켬)
    opener_grace_ms: int = 200          # 선합성이 진행 중이면 이만큼 기다려 본다

    # Jev 인계 판정(AG-T25, 스위치는 DIRECTON_JEV_HANDOFF=1 — agent.handoff_jev)
    jev_handoff_wait_s: float = 0.3     # 응답 시작 때 판정 결과를 기다리는 최대 시간(보통 이미 끝나 있다)

    # 경로
    log_dir: str = "runs/logs"
    tts_cache_dir: str = "assets/tts_cache"
    tts_voice_key: str = "default"      # 캐시 키 네임스페이스(서버는 여기에 TTS 음성·레벨을 덧붙인다)

    # 서버 시작 예열
    warmup_llm: bool = True             # LLM 체인 모델마다 짧은 요청 1회
    warmup_timeout_s: float = 20.0

    def cached_phrases(self) -> list[str]:
        """서버 시작 때 미리 합성해 둘 문구."""
        out: list[str] = []
        for p in (*self.openers, *self.fillers, *self.answer_phrases, self.apology, self.reprompt,
                  self.opener_apology, self.opener_lookup, self.opener_lookup_filler, self.post_tool_ok,
                  self.post_tool_more):
            if p and p not in out:
                out.append(p)
        return out

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        kw = {}
        for f in fields(cls):
            raw = env.get(ENV_ALIASES[f.name]) if f.name in ENV_ALIASES else None
            if raw is None:
                raw = env.get(ENV_PREFIX + f.name.upper())
            if raw is None:
                continue
            default = getattr(cls, f.name)
            if isinstance(default, tuple):
                kw[f.name] = tuple(p.strip() for p in raw.split("|") if p.strip())
            elif isinstance(default, bool):
                kw[f.name] = raw.strip().lower() in ("1", "true", "yes", "on")
            elif isinstance(default, int):
                kw[f.name] = int(raw)
            elif isinstance(default, float):
                kw[f.name] = float(raw)
            else:
                kw[f.name] = raw
        return cls(**kw)
