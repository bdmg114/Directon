# AGENT_PLAN — 한국어 음성 CS 에이전트 구현

> 작성일 2026-11-19 · 기준 `main`(초기 커밋) · 브랜치 `main` · Task 접두사 `AG-` · 결정 접두사 `AD` · 사용자 작업 `AG-U*` · 커밋 scope `agent|turn|providers|mock|eval|docs`
> 상태: **진행 중.** AD1~AD5 확정(2026-11-19).

- 근거: 사용자 요청(2026-11-19).
  1. "한번 구현해봐. 가능하면 TestCase용 음성도 생성해서 최적화해. 통화 환경과 발화 내용 등 여러 변수를 고려해서 테스트를 제작해. 서브에이전트 적극 사용해서 빠르게 병렬작업해. /fast-dev-workflow 참고해서 진행해"
- 관련 문서: `docs/01_구현계획.md`(설계), `docs/00_대회규격_분석.md`(규격), `docs/research/01~03`(조사)
- 진행 규칙은 CLAUDE.md와 같다.

---

## 1. 현재 흐름 (2026-11-19)

- 코드 없음. Starter Kit만 있다: `example_server/server.py`(RMS 800ms 침묵 → 삐 소리), `mock_tools/mock_api_template.py`(`lookup()` 미구현), `eval/run_eval.py`(측정기), `eval/score_demo.py`(속도 표).
- 사용 가능한 키: `GEMINI_API_KEY`만 있다. Soniox·OpenAI·TypeSafe·ElevenLabs 키는 없다.
- 이 키로 쓸 수 있는 모델(2026-11-19 `models.list` 확인): `gemini-3.5-transcribe-live`(bidi STT), `gemini-3.5-transcribe`, `gemini-3.8-flash`, `gemini-3.5-flash-lite`, `gemini-3.1-flash-lite`, `gemini-3.8-flash-tts`, `gemini-3.8-flash-lite-tts`, `gemini-2.5-flash-preview-tts`, `gemini-embedding-2`.
- 하드웨어: CPU 16스레드, GPU 없음.

## 2. 문제 진단

| # | 문제 | 근거 |
|---|---|---|
| 1 | 계획서의 STT(Soniox)·LLM(OpenAI) 키가 없다 | 환경변수 확인 |
| 2 | 한국어 테스트 음성이 없다(데모는 영어 3개) | `Starter Kit/data/audio/demo` |
| 3 | 공식 14개 함수·매뉴얼이 미공개 | `Starter Kit/docs/MOCK_API_TASK.md` |

## 3. 설계

`docs/01_구현계획.md` 3~4절을 따른다. 차이점만 적는다.

### 3.1 Provider 교체 구조 (AD1, AD2)
- 모든 외부 서비스는 `agent/core/types.py`의 Protocol 뒤에 둔다. `agent/providers/factory.py`가 환경변수와 설정으로 고른다.
- STT 기본값: `SONIOX_API_KEY`가 있으면 Soniox, 없으면 Gemini(`gemini-3.5-transcribe-live` 스트리밍, 실패하면 `gemini-3.5-transcribe` 일괄).
- LLM 기본값: `OPENAI_API_KEY`가 있으면 `gpt-5.4-mini`, 없으면 Gemini Flash 계열 중 0단계 실측에서 가장 빠른 모델.
- TTS: `gemini-3.8-flash-tts`(대안 flash-lite-tts). 첫 마디·필러·고정 문구는 캐시.
- BAZE 게이트웨이(AG-T16, AD9): 직접 키(`SONIOX_API_KEY`·`OPENAI_API_KEY`·`GEMINI_API_KEY`)가 없고 `BAZE_API_KEY`가 있으면 그 서비스를 `BAZE_BASE_URL`(기본 `https://api.baze.co/v1/gateway`)로 보낸다. Soniox `wss://…/soniox/transcribe-websocket`, OpenAI Responses `…/`, Gemini `…/gemini`(v1beta). 게이트웨이에 없는 모델: `gemini-3.5-transcribe(-live)`, `gemini-2.5-flash-lite` → BAZE 모드 STT는 Soniox만(폴백 없음), LLM 기본 폴백은 `gemini-3.1-flash-lite`만. TTS는 `DIRECTON_TTS_PROVIDER=baze`일 때 `POST …/audio/speech/`(24k PCM 한 번에 응답 → 16k 변환), 기본은 edge 유지.

### 3.2 테스트셋 형식 (AG-T05/T06/T09 사이의 계약)
- `data/testset/cases.jsonl`: 한 줄에 한 문항.
  ```json
  {"case_id":"T0001","now":"2026-11-21 11:00","history":[],"customer_profile":null,
   "customer_utterance":"주문한 거 언제 와요?",
   "segments":[{"text":"주문번호가요","pause_after_ms":700},{"text":"이육일일일오공이삼일이요. 언제 와요?","pause_after_ms":0}],
   "voice":"gemini:Kore",
   "env":{"channel":"phone_ulaw","noise":"babble","snr_db":15,"gain_db":-6,"speed":1.0,"lead_ms":400,"packet_loss":0.0},
   "category":"tool_lookup","tags":["mid_pause","digits"],
   "expected_tools":[{"name":"get_order_info","arguments":{"order_id":"2611150231"}}],
   "expected_points":["배송 상태(배송중)를 안내한다"],"must_not":["주문 취소를 실행한다"]}
  ```
  - `history` 원소: `{"role":"agent"|"customer","text":"..."}` (공식 형식이 나오면 맞춘다)
  - Starter Kit `run_eval.py`가 읽는 필드(`case_id, now, history, customer_profile`)를 그대로 포함한다.
- `data/testset/audio/<case_id>.wav`(16k mono), `data/testset/audio/meta.json` = `{case_id: {"speech_end": 초}}`. 오디오는 git에 넣지 않는다.

### 3.3 통화 환경 변수 (AG-T06)
| 축 | 값 |
|---|---|
| channel | `wideband`(원음) / `phone`(8kHz 다운샘플 + 300~3400Hz 대역) / `phone_ulaw`(phone + G.711 μ-law) |
| noise | `none` / `white` / `pink` / `babble`(다른 발화를 겹쳐 만든 웅성거림) / `hum`(60Hz 험) |
| snr_db | 30 / 20 / 10 / 5 |
| gain_db | 0 / −12(작은 목소리) / +6(클리핑 근접) |
| packet_loss | 0 / 0.02(20ms 프레임 무작위 무음화) |
| speed | 0.85 / 1.0 / 1.2 |
| lead_ms | 말 시작 전 무음 200~1500 |

### 3.4 발화 내용 변수 (AG-T05)
- 범주: `faq`(정책·영업시간), `tool_lookup`(조회), `tool_action`(취소·반품·교환·주소 변경·콜백), `need_info`(정보 부족 → 되묻기), `out_of_scope`, `complaint_handoff`(불만·상담원 요청), `followup`(history 있는 후속 턴, 숫자만 말하기 등)
- 태그: `mid_pause`(발화 중 300~1200ms 쉼), `filler_words`(어, 음), `connective_end`(연결어미에서 쉼), `digits`(숫자 읽기: 공/영, 끊어 읽기), `self_correction`, `negation`, `banmal`, `long`, `short`, `caller_known`

### 3.5 조립 규약 (AG-T07에서 확정)
- `agent/server.py:build_deps()`가 지연 import: `agent.providers.factory.make_stt/make_tts/make_llm()`(인자 없음), `agent.turn.vad.make_vad|SileroVAD`(세션마다), `agent.turn.smart_turn.make_smart_turn|SmartTurnONNX|SmartTurnV3`(공유), `agent.turn.detector.make_detector|KoreanTurnDetector|TurnDetector`(세션마다), `agent.normalize.normalize_args(tool, args, schema, now)`.
- 첫 마디·필러·사과·재질문 문구는 `agent/config.py:Settings`(환경변수 `DIRECTON_<필드>`), 캐시는 `assets/tts_cache/`.
- transcript는 응답 끝에 한 번(첫 마디·필러 포함), 그다음 turn.end.

## 4. 결정 필요 사항

| ID | 주제 | 선택지 | 권장 | 결정 |
|---|---|---|---|---|
| AD1 | 개발용 STT | A: Gemini transcribe-live(키 있음) / B: Soniox 키 발급 후 진행 | **A.** 지금 키가 있고, Soniox 어댑터도 같이 만들어 키가 생기면 바로 교체 | **A** (2026-11-19 사용자 확정) |
| AD2 | 개발용 LLM | A: Gemini Flash 계열 / B: OpenAI 키 대기 | **A.** 같은 이유. 실측으로 모델 선택 | **A** (2026-11-19 사용자 확정) |
| AD3 | 테스트 음성 합성 | A: Gemini TTS 여러 음성 + edge-tts(무료, 키 불필요) / B: Gemini만 | **A.** 상담원 음성(Gemini)과 다른 엔진이 섞여야 과적합을 피한다. 가상 문장만 보낸다 | **A** (2026-11-19 사용자 확정) |
| AD4 | 가상 쇼핑몰 함수 | A: 공식 미공개이므로 쇼핑몰 함수 14개를 가정해 구현 / B: 데모 2개만 | **A.** 공식 공개 시 데이터만 교체 | **A** (2026-11-19 사용자 확정) |
| AD6 | 상담원 TTS(Gemini TTS 무료 하루 10회) | A: Gemini 결제 연결 / B: 개발 중 edge-tts 대체 / C: 다른 TTS 키 | **A.** 비용 약 $5 | **B** (2026-11-19 사용자 확정) |
| AD7 | 테스트 음성 화자 | A: edge 3종+Heami에 피치·속도 변형 / B: 결제 연결 후 Gemini 음성 사용 | **A** | **B** (2026-11-19 사용자 확정) — T05b는 AG-U04 이후. T12 보정셋은 edge+피치로 먼저 만들고 Gemini 음성은 결제 후 추가 |
| AD8 | 잡음 VAD 래퍼 기본값 | A: 끔 / B: 켬 / C: 결정기 재보정 후 | **A.** 잘못 끊기(0점) 손해가 말 끝 놓침(80~90점)보다 큼 | **A** (2026-11-19 사용자 확정) — `DIRECTON_VAD_NOISE_GATE=1`로 켬 |
| AD9 | BAZE 게이트웨이 범위 (2026-11-19 사용자 요청) | A: Claude Code만 / B: 프로젝트 provider만 / C: 둘 다. TTS: 기본 edge 유지+baze 선택 / baze 기본 / 안 함. STT 폴백: Soniox만 / gemini-3.5-flash 일괄 | — | **B**, TTS는 edge 기본+`baze` 선택, STT 폴백 없음 (2026-11-19 사용자 확정) |
| AD10 | BAZE 크레딧 소진 → 서비스 선택 | — | — | **Gemini 직접 키로 복귀** (2026-11-19 사용자 지시). `.env`의 `BAZE_API_KEY` 주석 처리(값 보존). 다시 켜려면 주석 해제 |
| AD5 | 저장소 | A: 이 폴더를 git 저장소로 / B: git 없이 | **A.** Task별 커밋이 워크플로 전제 | **A** (2026-11-19 사용자 확정) |

## 5. 사용자가 직접 확인할 것

| ID | 할 일 | 왜 |
|---|---|---|
| AG-U01 | Soniox·OpenAI 키 발급, `.env`에 추가 | 계획서 기본 스택으로 전환 |
| AG-U02 | 운영진 질문 발송(`00_대회규격_분석.md` 6절) | 허용 API·실행 위치·채점 방식 |
| AG-U03 | 생성된 한국어 테스트 음성 샘플 청취 | 합성 품질 확인 |
| AG-U05 | 체험 데모 실제 마이크로 확인(`powershell -ExecutionPolicy Bypass -File demo
un_demo.ps1`, 가이드 `docs/03_사용자_테스트_가이드.md`) — 마이크 권한, 에코, 음질, Ctrl+C 종료, 다중 턴 | 실제 마이크·스피커 경로는 미검증 |
| AG-U04 | Gemini API 결제 연결(Tier 1) 또는 대체 TTS 키 | 무료 티어 TTS 하루 10회 — ✅ 2026-11-19 프로젝트 158777464759 결제 연결 확인 |
| AG-U06 | AI Hub [민원(콜센터) 질의-응답](https://aihub.or.kr/aihubdata/data/view.do?dataSetSn=98) 신청·다운로드(본인 인증, 내국인만) → `data/aihub_98/raw/`에 압축 해제 | AG-T27 실제 통화 음성 평가의 입력. 재배포 금지 데이터라 git에 넣지 않는다 |

## 6. Task 상태표

| ID | Task | 의존 | 상태 | 완료일 |
|---|---|---|---|---|
| AG-T01 | 골격: git, uv, 계약(`agent/core/types.py`), CLAUDE.md | — | DONE | 2026-11-19 |
| AG-T02 | Mock API 서버 + 가상 쇼핑몰 함수 14개·데이터·응대 매뉴얼 | AG-T01, AD4 | DONE | 2026-11-19 |
| AG-T03 | 오디오 유틸 + VAD + Smart Turn + 한국어 규칙 + 턴 결정기 | AG-T01 | DONE | 2026-11-19 |
| AG-T04 | Provider(Gemini STT/TTS/LLM, Soniox, OpenAI, edge TTS) + 실측 벤치 | AG-T01, AD1, AD2 | DONE | 2026-11-19 |
| AG-T06 | 테스트 음성 도구(합성·통화환경 증강) + 측정기·채점기 | AG-T01, AD3 | DONE | 2026-11-19 |
| AG-T07 | 세션·응답기·툴 클라이언트·TTS 캐시·서버 | AG-T01 | DONE | 2026-11-19 |
| AG-T08 | 인자 정규화(한국어 숫자·철자·전화번호·날짜) | AG-T01 | DONE | 2026-11-19 |
| AG-T05 | 테스트 시나리오 작성 + 음성 생성 — 분할됨 | AG-T02, AG-T06 | 분할됨 | |
| AG-T05a | 시나리오 텍스트(cases.jsonl) 작성 | AG-T02 | DONE | 2026-11-19 |
| AG-T05b | 테스트 음성 생성(build_testset) | AG-T05a, AG-T06, AG-U04 | DONE | 2026-11-19 |
| AG-T09 | 실제 구성 연결 + 첫 측정(베이스라인) — 분할됨 | AG-T03, T04, T07, T08 | 분할됨 | |
| AG-T09a | 조립·LLM 폴백·레벨 정규화·예열 + E2E 스모크 20문항 | 위와 같음 | DONE | 2026-11-19 |
| AG-T09b | 172문항 베이스라인 측정 | AG-T09a, AG-T05b | DONE | 2026-11-19 |
| AG-T12 | 턴 감지 보정 데이터셋(600발화, 쉼/말끝 자동 라벨) + 오프라인 보정 | AG-T03, AG-T06 | DONE | 2026-11-19 |
| AG-T10 | 최적화 루프 — 분할됨 | AG-T05b, AG-T09, AG-T12 | 분할됨 | |
| AG-T10a | LLM 응대: 프롬프트(인계 함수 호출, 재조회·호칭 반복 금지), 첫 마디-본 응답 공백 | AG-T09a | DONE | 2026-11-19 |
| AG-T10b | STT 숫자 인식: 문맥 힌트, 부분 결과 공백, 자릿수 불일치 시 재인식 | AG-T09a | DONE | 2026-11-19 |
| AG-T10c | 세션: 선호출 재사용(finalize 후 재시작·정규화 비교), 텍스트 기반 첫 마디, babble 잡음 VAD, 빈 텍스트 짧은 발화 처리 | AG-T09a | DONE | 2026-11-19 |
| AG-T10d | 상품명·옵션 별칭 정규화(엑스라지↔XL, 회색↔그레이, 부분 상품명) | AG-T02, AG-T08 | DONE | 2026-11-19 |
| AG-T13 | 벤치마크: 단순 STT+LLM+TTS 조합 172문항 점수 (2026-11-19 사용자 요청) | AG-T09b | DONE | 2026-11-19 |
| AG-T14 | 사용자 체험 데모: 브라우저 마이크 데모 페이지 + 데모 서버 + 사용자 테스트 가이드 (2026-11-19 사용자 요청) | AG-T10c | DONE | 2026-11-19 |
| AG-T16 | BAZE 게이트웨이 연동: BAZE_API_KEY로 Soniox·OpenAI·Gemini 라우팅 + BAZE TTS 어댑터 (2026-11-19 사용자 요청) | AG-T04, AD9 | DONE | 2026-11-19 |
| AG-T14b | 체험 데모 이전 대화(history) 주입 UI + 예시 프리셋 (2026-11-19 사용자 요청) | AG-T14 | DONE | 2026-11-19 |
| AG-T15a | 2차: 번호 되묻기 조건부 완화 + 응대 품질(judge) 개선 | AG-T13 | DONE | 2026-11-19 |
| AG-T15b | 2차: 말 끝 전 응답 감소(숫자 묶음·자기 정정·긴 쉼 규칙) | AG-T12, AG-T13 | DONE | 2026-11-20 |
| AG-T15c | 2차: LLM 동시 요청(hedging) + 컨텍스트 캐시 | AG-T13 | DONE | 2026-11-19 |
| AG-T15d | 2차 통합 172문항 측정(round2) + judge | AG-T15a~c, AG-T14, AG-T21, AG-T25 — 첫 마디 정책 켬/끔·Jev 인계 켬/끔 A/B 포함(2026-11-20 사용자: 첫 마디 정책은 측정 후 결정) | DONE | 2026-11-20 |
| AG-T17 | 당일 교체: Mock `--data/--tools`, 매뉴얼·브랜드·말투 파일 교체, 번호 자릿수 데이터 추론, 시험 음성 저장 끄기, 당일 체크리스트 (2026-11-19 사용자 요청: 공식 사이트 기반 작업 착수) | AG-T09 | DONE | 2026-11-20 |
| AG-T18 | 외부 접속·안정성: 터널 스크립트, 동시 4세션 연속 부하, edge-tts 장애 시 TTS 대체, 측정 중 예외 격리 | AG-T09 | DONE | 2026-11-20 |
| AG-T19 | 바지인(중간 개입): 상담원 발화 중 고객 음성 → 즉시 중단 → 이어 듣고 재응답 | AG-T10c | DONE | 2026-11-20 |
| AG-T20 | 첫 마디 재설계: 같은 채움말 반복 금지, 답 준비 시 첫 마디 생략, 상황별 다양한 첫 마디 | AG-T10c | DONE | 2026-11-20 |
| AG-T21 | 조회 뒤 답까지 시간 단축(after_tool_sound) + 측정 지표 | AG-T15c | DONE | 2026-11-20 |
| AG-T22 | 건강기능식품 연습판: 매뉴얼·상품·결제·몸 불편 인계를 가정한 Mock 데이터 + 시나리오 40개 + 음성 | AG-T17 계약 | DONE | 2026-11-20 |
| AG-T23 | 기획서·발표 초안 + 1장 검토 보고서(결정 모델 선준비·빠른 서빙) | — | DONE | 2026-11-20 |
| AG-T24 | Jev 결정 모델: 말 끝 판단 Noul(오프라인 평가 + 기본 끔 스위치), 사람 인계 판단 Noul(몸 불편·직접 요청·욕설·반복 실패·법적·예외, 기본 끔 스위치), 여유 있으면 의도 Choice (2026-11-20 사용자 요청: "Jev 추가해보기로 결정했어", "사람 인계 여부도 Jev로 판단하도록 해") | AG-T15b | DONE | 2026-11-20 |
| AG-T25 | 통합 배선: Jev 인계·말 끝 문맥을 세션·서버에 연결(스위치), 첫 마디·바지인 설정 필드(config), 워밍업에 첫 마디 문구, 끊긴 답 프롬프트 규칙 | AG-T19, AG-T20, AG-T24 | DONE | 2026-11-20 |
| AG-T26 | 최종 결과 보고서 HTML: 모든 측정 결과를 각 선택의 trade-off 중심으로 정리 (2026-11-20 사용자 요청: "모든 측정 끝나고 보기좋은 HTML 페이지로 결과보고서 정리해줘. 각 선택의 Trade-off를 중심으로") | AG-T15d | DONE | 2026-11-20 |
| AG-T27 | 실제 통화 평가셋: AI Hub 98번(다산콜센터 녹취) 고객 발화 → 16k 평가셋 변환 + STT·턴 감지 실음성 측정, 합성 테스트셋과 격차 비교 (2026-11-20 사용자 요청: "응 98번으로 Task 적어줘") | AG-U06, AG-T06, AG-T12 | TODO | |
| AG-T28 | 번호 확인 되묻기: 주문번호가 막히거나(자릿수, 교정 실패) not_found면 발신 고객 주문 중 가장 가까운 1~2개를 끝자리·상품명으로 확인 질문, 기본 끔 스위치 `DIRECTON_CONFIRM_ORDER_ID` (2026-11-20 사용자 요청: "선택지 중 가장 가까운 것을 하나 골라서 0000 맞으실까요?하고 되묻는 식으로", "기본 끔 스위치로 구현해줘") | AG-T15a | DONE | 2026-11-20 |
| AG-T29 | 공식 자료로 교체 — 분할됨 (2026-11-21 사용자 요청: "새로 받은 공식 메뉴얼로 다 교체해봐 지금 옛날거랑 섞여있어가지고 헷갈리잖아") | `docs/12_공식자료_분석.md` | 분할됨 | |
| AG-T29a | 옛 가정 자료를 `legacy/`로 이동(동작 변화 없음) | — | DONE | 2026-11-21 |
| AG-T29b | `DIRECT_ON 2026 Starter Kit/`(11/18 사전 공개본)을 공식 zip 원본으로 교체, 옛 데모 음성·데이터는 `tests/fixtures/starter_kit_demo/`로 (2026-11-21 사용자: "Starter Kit 폴더"를 교체, 코드 수정은 하지 않음) | — | DONE | 2026-11-21 |
| AG-T30 | 공식 Mock 서버: `--db`, 조회=번호 숫자 일치, 접수=필수값 검사, 검색=가장 가까운 기록, 미정의 함수 404. 옛 쇼핑몰 Mock 로직·`legacy/shop_mock/` 삭제 (2026-11-21 사용자 승인 계획) | AG-T29 | DONE | 2026-11-21 |
| AG-T31 | 지정 모델(BAZE 게이트웨이: Soniox stt-rt-v6, GPT-5.4 mini, gemini-3.8-flash-lite-tts) + 제출 묶음(Python 3.11, requirements.txt, start_server.sh, start_mock.sh, scripts/package.sh). Gemini STT·LLM, edge-tts, 다른 모델 대체·동시 요청, Gemini 캐시, Smart Turn 삭제 | AG-T30 | DONE | 2026-11-21 |
| AG-T32 | 지식과 이전 대화: 시나리오·브랜드 정보 96개·말투를 고정부에, 시작 때 캐시 예열, history의 tool_call/tool_result 정리, 링크·이모티콘 제거, 상담원 연결은 말로만. 옛 프롬프트 규칙·영업시간·`legacy/practice_health/`·`legacy/testset/`와 그 평가 도구 삭제 | AG-T31 | DONE | 2026-11-21 |
| AG-T33 | 함수 실행 전 검사(정의·필수값·번호 출처)와 번호 정규화, 15자리 주문번호 말 끝 기준. 상담원 연결 강제 호출·주문 목록 자동 조회·10자리 검사·번호 확인 되묻기 삭제 | AG-T32 | DONE | 2026-11-21 |
| AG-T34 | 시작할 때 주문 조회(발신자 번호, 스위치 `DIRECTON_PRELOAD_ORDERS`, **기본 끔** — 측정 결과 응대 감점) | AG-T33 | DONE | 2026-11-21 |
| AG-T35 | 함수별 사전 발화 + 실패 결과 정해진 문장 | AG-T33 | TODO | |
| AG-T57 | 조회 실패 때 들은 번호 되읽기: 이번 통화에서 받아 적은 번호(이번 말 + 앞선 턴의 고객 말)로 조회했는데 not_found면 함수 결과에 `_looked_up_with`와 안내를 붙여 "…번으로 조회했는데 확인되는 내역이 없네요. 번호가 맞으실까요?"라고 답하게 한다. 주문번호는 값을 그대로 주고, 휴대폰 번호는 표식 `[PHONE]`만 준 뒤 답 속의 `[PHONE]`을 소리로 내보내기 직전에 서버가 실제 번호로 바꾼다(`speech_safe`의 `mask_number`, AG-T58 참고). 조회 성공, 발신자 번호, session.start 이전 대화의 번호는 되읽지 않는다(AG-T33 보강 2: 성공 때도 읽으면 응대 감점). `guards.heard_lookup_numbers`·`lookup_status`·`heard_phone`, `GuardContext.spoken`(앞선 턴의 고객 말, 기본 빈 목록), 규칙의 not_found 문장을 번호 확인 흐름과 맞춤(전에는 "천천히 다시 말씀"과 "되읽어 확인"이 서로 달랐다), 스위치 `DIRECTON_NOTFOUND_READBACK`(기본 켬). 코드가 문장을 통째로 만드는 방식은 어색하다는 사용자 의견으로 버렸다. **응대 점수 영향은 미측정**(채점용 OpenAI 키 없음) (2026-11-21 사용자 직접 통화 피드백: 번호를 잘못 들은 건지 내역이 없는 건지 알 수 없다) | AG-T33 | DONE | 2026-11-21 |
| AG-T58 | 게이트웨이가 가린 휴대폰 번호 되살리기: BAZE 게이트웨이는 요청 안의 휴대폰 번호(`010-1234-5678`, `01012345678`, `010 1234 5678`)를 `[PHONE]`으로 바꿔 LLM에 넘긴다(2026-11-21 실측. 주문번호·일반 전화·이메일·한글로 읽은 번호·한 자씩 띄운 숫자는 그대로. 의도된 기능인지는 미확인). LLM은 번호를 못 보고 인수에 `"[PHONE]"`을 넣어 검사에서 missing으로 막혔다(직접 통화에서 정확히 받아 적은 번호인데 "정확히 들리지 않았다"고 답함). AG-T15a·llm_tuning_r2에서 한 번 고쳤던 문제인데 AG-T33 응답기 재작성 때 빠졌고, 그 뒤 평가는 OpenAI 직접 키로만 돌려 드러나지 않았다. 대응: (1) 전화번호 인수가 가려진 표식이면 이번 고객 말에 하나뿐인 유효한 전화번호(없으면 믿는 출처에 하나뿐인 번호)로 바꾼다(`guards._unmask_phone`), (2) 번호 없이 조회를 불렀고 이번 말에 전화번호가 하나면 그 번호로 조회, (3) 규칙에 `[PHONE]`의 뜻과 쓰는 법, (4) 답 속의 `[PHONE]`은 실제 번호로 바꿔 읽고 모르면 "말씀하신 번호"로. 발신자·이전 대화 번호 우선 규칙은 그대로. 확인(개인 BAZE 키, 게이트웨이 경유 gpt-5.4-mini, 문장 4종 × 5회): 수정 전 조회 실행 0/20 → 수정 후 20/20, 내역 없는 번호는 "010, 1234, 5678번으로 조회했는데 확인되는 내역이 없네요. 번호가 맞으실까요?"로 답함. 테스트 14개(AG-T57 포함). 별도 발견: `.env`의 대회 BAZE 키가 LLM 호출에 402를 돌려준다(2026-11-21 17시경, 14:55에는 정상) (2026-11-21 사용자 직접 통화 피드백) | AG-T57 | DONE | 2026-11-21 |
| AG-T59 | 직전 답이 아직 재생 중일 때 시작된 300ms 미만 소리는 새 턴으로 받지 않는다(`session._tail_backchannel`). 응답 중 300ms 미만 맞장구를 무시하는 기준(AG-T19)을 turn.end 뒤 재생 구간까지 넓힌 것이다. 재생이 끝난 뒤의 짧은 "네"는 지금처럼 답한다. 직접 통화에서 긴 답을 다 보낸 지 1.0초 뒤 들어온 0.2초 소리가 "네"류로 인식돼 고객 말 없이 답이 하나 더 나갔다(마이크 막기를 끈 스피커 통화). 측정기는 문항 하나가 연결 하나라 직전 턴이 없어 영향이 없다 (2026-11-21 사용자 직접 통화 피드백) | AG-T19 | DONE | 2026-11-21 |
| AG-T60 | 물은 것만 답하기(규칙): 이미 말해 준 사실(배송상태·송장번호·택배사·금액·날짜)은 다시 묻지 않으면 되풀이하지 않고, 조회 결과를 안내한 뒤의 물음에는 물은 것만 답한다. 브랜드 정보 덧붙이기는 조회 결과를 처음 전하는 답에서만. 물은 것이 없는데 "네"만 오면 한 문장으로 받는다. 직접 통화에서 송장번호를 다섯 답 연속으로 읽었다. 채점은 한 가지 덜 말하면 0.7이라 응대 점수가 조금 내려갈 수 있으나 사용자 경험을 우선한다(2026-11-21 사용자 결정). 연습 문항 전후 측정은 아직 하지 않았다 | AG-T32 | DONE | 2026-11-21 |
| AG-T61 | 끼어든 다음 턴의 첫마디가 화면에서 버려지던 것: 피드백 키트·체험 데모는 끼어들기 뒤 남은 오디오를 버리다가(`dropAudio`) `debug.turn decided`에서만 풀었는데, 서버는 다음 턴 첫마디를 decided보다 먼저 보낸다. 그래서 끼어든 다음 턴은 첫마디 없이 본 답변부터 들렸다(직접 통화: 첫 소리 7.34초). 그 턴의 `turn.end`에서 풀게 고쳤다. 서버·측정기 경로는 그대로다 (2026-11-21 사용자 직접 통화 피드백) | AG-T56 | DONE | 2026-11-21 |
| AG-T52 | 체험 데모 휴먼 피드백 묶음: 통화 중 고객 음성(보낸 PCM)·상담원 음성(실제 재생분, 끼어들기 뒤 무음)을 같은 시간축에 녹음, 턴별 이벤트 시각·오디오 구간, 턴별 태그·STT 정정·메모, 통화 후 턴별 재생. 설정·대화·피드백·오디오(WAV base64)를 한 JSON(`directon.demo_session/v1`)으로 서버 저장(`POST /api/sessions`) 또는 내려받기, `demo/feedback_store.py`가 session.json·customer/agent/mix.wav·턴 클립·테스트셋 모양 cases.jsonl로 풀어 저장(CLI로 내려받은 파일도) (2026-11-21 사용자 요청: "휴먼 피드백을 위해 가상 데모 페이지를 업데이트해줘. 특히 피드백과 대화 내역, 오디오 파일을 json 파일로 정리하는 기능에 집중해줘") | AG-T14, AG-T14b, AG-T19 | DONE | 2026-11-21 |
| AG-T56 | 피드백 키트가 `session.start`에 `"debug": true`를 기본으로 보낸다(추가 필드의 `{"debug": false}`가 우선). 직접 통화 피드백: 인식 텍스트가 안 보여 받아쓰기 문제인지 응답 문제인지 가를 수 없었고, 어떤 번호로 조회했는지도 보이지 않았다(둘 다 `debug.stt`·`debug.tool`이 꺼져 있어서). 힌트 문구·README에 끄는 법과 끼어들기(반이중 해제 + 이어폰) 안내, 테스트 1개 (2026-11-21 사용자 요청: "디버그는 다 트루로 하자") | AG-T53 | DONE | 2026-11-21 |
| AG-T53 | 이식용 피드백 키트 `feedback_kit/`: 표준 라이브러리만 쓰는 서버(`python -m feedback_kit --agent-ws ...`)·페이지·README, 우리 코드 import 없음, 대회 프로토콜만 쓰는 상담원은 브라우저 말 구간 추적으로 턴을 나누고 `debug.*`가 오면 그것을 쓴다. 저장 모듈·녹음 모듈·워클릿은 키트가 원본, 데모는 키트 것을 쓴다 (2026-11-21 사용자 요청: "이거 다른 팀원 레포에도 붙일 수 있을까?" → "키트로 만들어줘" → "jin에서 키트 이어서 만들어줘") | AG-T52 | DONE | 2026-11-21 |
| AG-T54 | 요청 종류 판정률 평가: `scripts/intent_eval.py`(text_eval.run_case로 한 턴 → 행동 분류 → 허용 행동과 비교, 종류별 정확도·혼동표), 연습 80문항 라벨 `data/eval/request_types.json`, 녹음 대본 200문항 `data/eval/haruon200/` | AG-T33 | DONE(측정 전) | 2026-11-21 |
| AG-T55 | 소음 속 말 끝: 짧은 소리로 침묵 깨지 않기(`resume_min_ms` 100, 기본 켬), 받아쓰기 글 멈춤 상한(`text_stall_cap_ms` 3000, 기본 켬), 글 멈춤 + 결정기(`text_stall_end_ms`, 기본 끔), 손님 목소리 크기 기준(`speaker_gate_db`, 기본 끔) | AG-T33 | DONE(측정 전) | 2026-11-21 |
| AG-T56 | 번호 되묻기·응답 지연(데모 피드백): 번호 없는 조회는 이번 말의 번호로 채움, 들은 번호를 되읽어 "맞으실까요?"로 확인(`confirm_heard_number`), 결과 문구 뒤 채움말 생략, 자주 쓰는 답 문장 캐시(`answer_cache`, `answer_phrases`) | AG-T33 | DONE(측정 전) | 2026-11-21 |
| AG-T57 | 시스템 프롬프트: KT 고객센터 AI 통화 관찰(보완 제안 문서) + 예시 10개 point 반영 — 같은 질문 반복·고객 탓 금지, 짧은 대답·거절 우선, 확인 최소화(접수 뒤 복창), 배송지 변경 대상·출고 후 안내, 취소 환불 문의는 주문번호 먼저, 입금전 안내 예, 숫자 사이 하이픈 없이(TTS 입력은 쉼표) | AG-T56 | DONE(측정 전) | 2026-11-21 |
| AG-T11 | 전체 회귀 + 결과 보고 | 위 전부 | TODO | |

## 7. Task 상세 (테스트 먼저)

### 공통 규칙
- 테스트: `uv run pytest -q`. 외부 API는 fake(CLAUDE.md 표). 실제 호출 테스트는 `@pytest.mark.live`.
- 실측·음성 생성 스크립트(`scripts/`, `eval/`)는 실제 API를 불러도 된다(사용자 요청: 테스트 음성 생성과 최적화). 호출 수는 필요한 만큼만.
- 모듈은 생성자 주입으로 만들고, 전역 상태를 두지 않는다.

### AG-T02 Mock API 서버 + 가상 쇼핑몰
- 테스트 먼저(`tests/test_mock_api.py`):
  - `GET /tools`가 14개 함수를 `[{"type":"function","function":{...}}]` 형식으로 준다
  - 정의된 입력이면 정의된 출력 + `/calls`에 `matched=true`, `X-Session-Id` 기록
  - 정의되지 않은 입력이면 함수별 not_found 응답 + `matched=false`
  - 모르는 함수는 404, 잘못된 JSON은 400
- 구현: `mock_api/server.py`(Starter Kit 템플릿과 같은 경로·형식), `mock_api/data/tools.json`, `mock_api/data/mock_data.json`, `cases/manual.md`(가상 쇼핑몰 응대 매뉴얼)
  - 함수 후보: get_member_info(phone_number), get_order_list(phone_number), get_order_info(order_id), track_delivery(order_id), cancel_order(order_id, reason), request_return(order_id, reason), request_exchange(order_id, reason), change_shipping_address(order_id, new_address), get_refund_status(order_id), get_product_info(product_name), check_stock(product_name, option), get_coupons(phone_number), request_callback(phone_number, preferred_time), transfer_to_agent(reason)
  - 주문번호는 숫자 10자리, 전화번호는 `010`+8자리(하이픈 없음)

### AG-T03 오디오·턴 감지
- 테스트 먼저(`tests/test_audio.py`, `tests/test_turn.py`):
  - `rms`, `trim_silence`, `resample`(24k→16k 길이 2/3), `frames()` 20ms 분할
  - Silero VAD: 무음 프레임 확률 < 0.2, 데모 음성 구간 평균 확률 > 0.5, 20ms→512샘플 재분할
  - Smart Turn: 데모 음성 전체 → [0,1] float, 8초 초과 입력은 뒤 8초만 사용
  - 한국어 규칙: "배송 언제 와요?"→끝남, "주문번호가요"→끝남 아님 수준의 판별(`end_score`), "그리고"·"어"·연결어미 → 보류
  - 결정기: 침묵 < 200ms면 항상 false, 최대 1100ms면 항상 true, 400/600/900ms 경계 규칙, `expect_short`이면 단축
- 구현: `agent/audio.py`, `agent/turn/vad.py`, `agent/turn/smart_turn.py`, `agent/turn/rules.py`, `agent/turn/detector.py`, `scripts/download_models.py`(→ `models/`)

### AG-T04 Provider
- 테스트 먼저(`tests/test_providers.py`): fake client로 각 Provider가 계약대로 동작
  - STT: 오디오를 보내고 `latest_text`가 갱신되고 `finalize`가 최종 텍스트를 반환
  - TTS: 16k PCM 덩어리를 순서대로 yield(24k면 변환)
  - LLM: 텍스트 이벤트, 툴 호출 이벤트(인자 dict), done. `Message` → 각 API 형식 변환(툴 결과 포함)
  - factory: 환경변수에 따라 Provider 선택
- 구현: `agent/providers/{gemini_stt,gemini_tts,gemini_llm,soniox_stt,openai_llm,factory}.py`, `scripts/bench_providers.py`(실측 → `docs/decisions/bench_providers.md`)

### AG-T06 테스트 음성 도구 + 측정·채점
- 테스트 먼저(`tests/test_eval_tools.py`):
  - 증강: 각 channel·noise·gain·packet_loss가 길이를 유지하고 결정적(seed)이며, SNR이 목표 ±1dB
  - speech_end 계산: 앞 무음 + 음성 + 쉼이 있는 합성 신호에서 마지막 음성 끝을 20ms 오차로
  - 세그먼트 조립: pause_after_ms 반영
  - 채점: 속도 표(Starter Kit과 동일), 함수 일치(이름+인자), 태그별 집계
- 구현: `eval/augment.py`, `eval/synth.py`(Gemini TTS·edge-tts, 해시 캐시), `eval/build_testset.py`(cases.jsonl → wav + meta), `eval/runner.py`(Starter Kit 측정기 확장: agent_text·tool_calls 기록), `eval/score.py`(속도·함수·LLM 심사 옵션·태그별 표 → `report.md`)

### AG-T07 세션·응답기
- 테스트 먼저(`tests/test_session.py`, `tests/test_responder.py`, `tests/test_tools_client.py`): fake STT/TTS/LLM/VAD/결정기 + `httpx.MockTransport`
  - 말 끝 확정 전에는 오디오를 보내지 않는다
  - 확정 직후 첫 오디오는 캐시된 첫 마디
  - LLM 툴 호출 → `X-Session-Id` 헤더로 호출 → 결과를 넣어 다시 LLM → 문장 단위 TTS
  - `agent.transcript`(전체 텍스트) 후 `turn.end`가 마지막 메시지
  - 확정 전 음성 재개 시 선호출 취소
  - LLM·TTS 실패 시 캐시된 사과 문구 + `turn.end`
- 구현: `agent/session.py`, `agent/responder.py`, `agent/tools_client.py`, `agent/prompts.py`, `agent/tts_cache.py`, `agent/config.py`, `agent/turn_log.py`, `agent/server.py`

### AG-T08 인자 정규화
- 테스트 먼저(`tests/test_normalize.py`):
  - "공일공 일이삼사 오육칠팔" / "영일영..." / "010-1234-5678" → "01012345678"
  - "이육일일 일오공 이삼일" → "2611150231"
  - 영문 철자 읽기 "디 이 엠 오" → "DEMO"
  - "내일", "모레", "다음 주 월요일" + now → 날짜
  - `normalize_args(tool_name, args, schema, now)`: 파라미터 이름(phone_number, order_id 등)으로 규칙 적용, 모르는 필드는 그대로
- 구현: `agent/normalize.py`

### AG-T05 시나리오 + 음성 생성
- 3.2·3.4절 형식으로 `data/testset/cases.jsonl` 150문항 이상(Mock 데이터와 일치하는 기대 함수·인자). `eval/build_testset.py`로 음성 생성, 3.3절 환경을 고르게 배분.
- 테스트 먼저(`tests/test_testset.py`): 모든 문항이 스키마를 지키고, expected_tools가 Mock API에서 matched가 되며, 범주·환경 분포가 기준을 넘는다.

### AG-T12 턴 감지 보정 (2026-11-19 사용자 요청: "네가 데이터셋 만들어서 보정할 수 있어?" → "ㄱㄱ")
- 데이터: 테스트셋과 별개의 발화 600개(연결어미·조사·숫자 중단·간투사·완결처럼 들리는 함정에서 끊기는 세그먼트 구조). 세그먼트 조립으로 쉼 위치=미완료, 마지막 끝=완료 라벨을 자동 생성. 음성 11종 이상 + 3.3절 통화 환경. 쉼 지점까지 오디오의 실제 STT 텍스트를 함께 저장. 학습/평가는 음성·문장 비중복 분할. 위치 `data/turnset/`(오디오 gitignore).
- 보정 1단계: Smart Turn 확률·end_score·침묵·발화 길이를 결합한 결정 규칙/로지스틱 + 대기 시간 재설정. 프레임 단위 오프라인 재생 시뮬레이터로 잘못 끊기 ≤1% 제약 하 평균 대기 최소화. 결과 `docs/decisions/turn_calibration.md`, 확정 파라미터는 `agent/turn/detector.py` 설정값으로.
- 보정 2단계(필요 시): Smart Turn 한국어 미세조정.
- 테스트 먼저: 라벨 생성(쉼/끝 위치) 정확성, 시뮬레이터가 결정기와 같은 판정을 내는지, 분할 비중복.

### AG-T09 연결 + 베이스라인
- `agent/server.py`가 실제 Provider·턴 감지·정규화를 엮어 실행. Starter Kit 측정기와 `eval/runner.py`로 테스트셋 전체를 측정해 `docs/decisions/baseline.md`.

### AG-T10 최적화
- 베이스라인 지표에서 병목을 골라 반복: 턴 대기 임계값, 첫 마디, 프롬프트, 모델 선택, 병렬화. 회차마다 `runs/`와 `docs/decisions/opt_log.md`에 기록.

### AG-T16 BAZE 게이트웨이 연동 (2026-11-19 사용자 요청: "아래 gateway를 통해 원안의 AI 모델들을 사용하고자 해")
- 테스트 먼저(`tests/test_providers.py`): BAZE 키만 있을 때 factory가 Soniox(게이트웨이 URL·BAZE 키)·OpenAI(base_url)·Gemini(base_url) 선택, 직접 키가 있으면 직접 키 우선, BAZE 모드 기본 폴백 체인에서 2.5-flash-lite 제외, `BazeTTS`가 `httpx.MockTransport`로 요청 형식(model·input·voice·instructions·Bearer)을 지키고 24k → 16k PCM을 순서대로 yield, 빈 텍스트 무출력, HTTP 오류 시 예외.
- 구현: `agent/providers/{factory,baze_tts,_gemini,openai_llm}.py`, `openai` 의존성 추가(`OpenAILLM`이 지연 import하지만 의존성에 없었다).

### AG-T17~T23 공식 사이트 기반 작업 (2026-11-19 사용자 요청: "공식사이트 기반 작업들 착수해줘", 근거 `docs/04_목표_점검.md`)
공통 계약(병렬 작업 전 확정):
- Mock: `python -m mock_api.server --data <mock_data.json> --tools <tools.json>`(둘 다 생략 시 기존 `mock_api/data/`). 환경변수 `DIRECTON_MOCK_DATA`, `DIRECTON_MOCK_TOOLS`도 같은 뜻.
- 상담원 매뉴얼·브랜드·말투: `DIRECTON_MANUAL`(기본 `cases/manual.md`), `DIRECTON_BRAND`, `DIRECTON_STYLE`(선택, 없으면 생략). 카탈로그는 기존 `DIRECTON_CATALOG_PATH`.
- 시험 음성 저장 금지: `DIRECTON_NO_AUDIO_LOG=1`이면 오디오·고객 발화 원문을 디스크에 쓰지 않는다(측정 모드 기본 켬).
- after_tool 지표: 턴 로그 `after_tool_ms` = 마지막 함수 결과 수신 → 그 뒤 첫 유성 PCM 전송.

- **AG-T17** `mock_api/server.py`, `agent/config.py`, `agent/prompts.py`, `agent/normalize.py`, `agent/catalog.py`, `agent/turn_log.py`, `docs/05_당일_체크리스트.md`. 테스트 먼저: `--data/--tools` 로드, 매뉴얼·브랜드·말투 파일이 시스템 프롬프트에 들어감, 4자리 주문번호 데이터에서 자릿수 규칙이 4자리로 바뀜(고정 10자리 제거), 저장 끄기 시 파일 없음.
- **AG-T18** `agent/server.py`, `agent/providers/factory.py`, `agent/providers/edge_tts.py`, 새 `agent/providers/fallback_tts.py`, `scripts/tunnel.*`, 새 `eval/stress.py`. 테스트 먼저: TTS 1차 실패 시 대체 TTS로 이어 말함, 세션 하나의 예외가 다른 세션·서버를 죽이지 않음, fake로 4세션 동시 처리.
- **AG-T19·T20** `agent/session.py`, `agent/tts_cache.py`, `demo/*`. 테스트 먼저: 재생 중 고객 음성 300ms 이상 → 남은 TTS 중단·`turn.end` 규칙 유지·이어 들은 말로 재응답, 측정기 경로(끼어들지 않음) 결과 불변. 연속 턴에서 같은 첫 마디 반복 없음, 선호출 답이 준비됐으면 첫 마디 없이 본 응답.
- **AG-T21** `agent/responder.py`, `eval/runner.py`, `eval/score.py`. 테스트 먼저: 함수 결과 뒤 첫 문장이 끝나는 즉시 TTS로 넘어감, `after_tool_ms` 기록·집계, responder의 10자리 가정 제거(알려진 주문번호 길이 기준).
- **AG-T22** 새 `data/practice_health/`(mock_data.json, tools.json, manual.md, cases.jsonl, README), `eval/build_testset.py`(경로 인자). 테스트 먼저: 모든 문항 기대 함수가 연습 Mock에서 matched, 업무 7종·몸 불편 인계 포함.
- **AG-T23** `docs/06_기획서_초안.md`, `docs/07_발표_초안.md`, `docs/08_검토보고서_선준비.md`. 코드 변경 없음.

### AG-T27 실제 통화 평가셋 (2026-11-20 사용자 요청: "실제 고객 통화 녹음 음성데이터를 구할 수 있는지 찾아봐" → "응 98번으로 Task 적어줘")
- 왜: 지금 테스트셋(172문항)·보정셋(960발화)은 모두 TTS 합성 + 인공 통화 환경이다. 실제 전화 음질·말버릇·쉼에서 STT CER과 턴 감지(잘못 끊기·말 끝 놓침)가 얼마나 달라지는지 모른다.
- 데이터: AI Hub dataSetSn=98, 음성 440시간 이상, 상담사 0 / 고객 1 화자 구분. 실제 녹취는 다산콜센터 부분만(금융/보험은 구축자가 직접 건 통화, 개인정보는 창작). 서울시 민원 내용이라 쇼핑 Mock·함수 일치·judge는 측정하지 않는다 → **STT·턴 감지·첫 소리 지연만**.
- 0단계(코드 전): 받은 파일의 라벨 형식·샘플레이트·발화 단위(문장/대화)를 확인해 이 절에 적는다. 8kHz면 경계에서 16kHz로 변환(불변 규칙).
- 새 `eval/build_realset.py`: `--raw data/aihub_98/raw --out data/realset --n 200 --seed 0`. 다산콜센터·고객(화자 1) 발화만, 길이·숫자 포함·문장 내 쉼(VAD 300ms 이상)으로 층화 표본. 출력은 3.2절 형식 그대로(`cases.jsonl`, `audio/<case_id>.wav` 16k mono s16le, `audio/meta.json`의 `speech_end`는 Silero VAD 마지막 음성 끝). `customer_utterance` = 원 전사, `category: "real_call"`, `env.channel: "real_phone"`, `expected_tools: []`. 턴 시뮬용으로 쉼 구간·구간별 전사를 보정셋(`data/turnset`) 라벨 형식으로도 낸다.
- 측정: `eval.stt_eval --cases data/realset/cases.jsonl --audio data/realset/audio`(현재 기본 STT + Soniox 키 있으면 비교), `eval.turn_sim`(realset 라벨), 서버 실측은 `eval.runner`로 첫 소리 p50/p95·말 끝 전 응답 수만. 결과 `docs/decisions/realset.md`에 합성 테스트셋 대비 표(CER·숫자 정확도·false cut·miss·지연).
- 테스트 먼저(`tests/test_realset.py`, 네트워크·실데이터 없음): 작은 가짜 라벨 + 8kHz/16kHz 짧은 wav 픽스처로 ① 고객 발화만 뽑힘 ② 출력 wav가 16k mono s16le ③ cases가 3.2절 필수 필드를 가짐(Starter Kit `run_eval.py` 필드 포함) ④ `speech_end` ≤ 오디오 길이 ⑤ 같은 seed면 같은 표본. 라벨 파서는 0단계에서 확인한 형식 기준.
- 지키기: `data/aihub_98/`, `data/realset/audio/`는 `.gitignore`. 문서·보고서에는 집계 수치만, 발화 원문 인용 금지. 결과가 합성셋과 크게 다르면 보정(AG-T12 방식)을 다음 Task로 따로 연다.

### AG-T28 번호 확인 되묻기 (2026-11-20 사용자 요청)
- 왜: 지금은 번호를 확실히 고칠 수 없으면 "번호를 한 자리씩 천천히 다시 말씀해 주시겠어요?"로 되묻는다. 고객 본인 주문 중 가까운 것이 있으면 "끝자리 공이삼일 무선 충전기 주문 맞으실까요?"로 물어 "네" 한마디로 끝나게 한다. round2 R2-best 기준 해당 후보는 막힌 호출 14 + not_found 7(상한, 발신 번호 모르는 문항 포함).
- 범위: 기존 조용한 교정(자릿수 틀림 + 후보 하나뿐 → 바로 호출, AG-T15a)은 그대로. 확인 질문은 **원래 되묻던 경우만** 바꾼다(그 턴에 함수 호출이 없다는 점은 기존과 같아 함수 점수 불변).
  1. 자릿수가 틀리고 교정이 안 된 경우(후보가 여럿이라 애매).
  2. 자릿수는 맞는데 함수 결과가 `not_found`인 경우.
- 후보: **발신 고객 본인의 주문(`get_order_list`)에서만**(남의 주문번호를 읽어 주지 않는다). 발신 번호를 모르거나 목록 함수가 없으면 기존 동작. 공통 부분열 기준(`_close_enough`)을 통과한 주문 중 1등과 차이가 기준(`margin`) 안인 것이 1~2개일 때만, 3개 이상이면 기존 되묻기.
- 결과: `{"status": "needs_confirmation", "original_status", "candidates": [{order_id, last_digits, product_name}], "message"}`. message는 끝자리(4자리 이하 번호면 전체)를 한글 숫자로 읽어 확인만 하라는 지시. 주문번호 전체는 읽지 않고, 조회 결과를 지어내지 않는다.
- 스위치: `DIRECTON_CONFIRM_ORDER_ID=1`(기본 끔). 끄면 이전과 같다.
- 테스트 먼저(`tests/test_order_confirm.py`): 후보 고르기(1개·2개·3개 이상·먼 번호·같은 번호 제외), 끝자리 읽기, 스위치 끔이면 기존 invalid_arguments·not_found 그대로, 켜면 애매한 자릿수 오류와 not_found가 needs_confirmation으로 바뀜, 발신 번호 없으면 그대로, 목록은 턴당 한 번.

### AG-T29 공식 자료로 교체 (2026-11-21 사용자 요청)
- 왜: 11/21 공식 자료(분석은 `docs/12_공식자료_분석.md`)가 나왔는데 코드·데이터에 우리가 가정한 쇼핑몰(함수 14개, `transfer_to_agent`, 10자리 주문번호, 지어낸 영업시간)이 섞여 있다. 그대로면 정의에 없는 함수 호출로 함수 점수를 잃고, 사람이 볼 때도 헷갈린다.
- **AG-T29a** 옛 자료 이동(코드 동작 변화 최소): `mock_api/data/` → `legacy/shop_mock/`, `cases/manual.md` → `legacy/shop_mock/manual.md`, `data/practice_health/` → `legacy/practice_health/`, `data/testset/` → `legacy/testset/`. 옛 자료를 쓰는 코드·테스트·평가 도구 경로를 `legacy/`로 바꾼다(동작 변화 없음). 런타임 기본값·코드(Mock 규칙, 프롬프트, 응답기)를 공식 기준으로 바꾸는 것은 이 Task 범위가 아니다(사용자가 파일 교체만 요청).
- **AG-T29b** 공식 zip(`directon-2026-voice-cs.zip`, 100개 파일)을 `DIRECT_ON 2026 Starter Kit/`에 그대로 둔다. 옛 Starter Kit 파일은 지우고, 테스트가 쓰던 데모 음성(DEMO1~3)·`demo_cases/mock_data/tools`만 `tests/fixtures/starter_kit_demo/`로 옮긴다. 공식 키트에는 실제 상담 데이터가 있어 폴더 전체를 `.gitignore`(이전 `official/` 자리). CLAUDE.md의 원본·프로토콜 경로(`SPEC.md`)를 고친다. `data/turnset`(턴 감지 보정)은 함수와 무관해 그대로 둔다.

### AG-T30~T35 공식 규격 전환 (2026-11-21 사용자 승인 계획)
- 왜: 공식 키트(`DIRECT_ON 2026 Starter Kit/`, 분석 `docs/12_공식자료_분석.md`)는 함수 8개, 상담원 연결 함수 없음, 주문번호 `YYYYMMDD-NNNNNNN`, 지정 모델 3개, 운영진 컨테이너(Python 3.11, pip) 제출이다. 지금 코드는 우리가 가정한 쇼핑몰 위에 있다. 제출 마감 18:30, 설치·시작 실패는 전 문항 0점.
- 방침: 속도 뼈대(말하는 동안 미리 준비)는 그대로. 함수·지식·모델을 공식 기준으로 바꾸고 **옛 가정에 묶인 코드·자료·테스트는 같은 단계에서 지운다.** 사전 분류 단계는 두지 않는다. Jev 인계는 꺼 둔 채 남긴다.
- 키: `.env`(git 제외)의 `BAZE_API_KEY`(대회 게이트웨이, 제출용)와 `OPENAI_API_KEY`(개발·채점용, 제출물에 넣지 않음).
- 단계별 끝 상태: T30 단위 테스트 / T31 v0(새 3.11 환경에서 공식 측정기 5문항) / T32 v1(20문항 채점) / T33 v2(80문항) / T34 해당 문항 켬·끔 비교 / T35 80문항. T33까지가 제출 최소선.
- 지우는 기준: 옛 함수 이름·옛 주문번호·지어낸 정책에 묶인 코드, 지정 모델이 아닌 연결 코드, 그것만 검사하는 테스트. 남기는 것: 듣기·말 끝 판단·미리 준비·끼어들기, 말 끝 보정 도구와 `data/turnset`, 체험 데모(공식 데이터로), Jev 인계(끔).
- 검증: `uv run pytest -q` + 공식 `run_eval.py`·`score.py`(원본 그대로 실행). 제출 전 zip을 새 폴더·새 3.11 환경·Docker에서 설치 → 시작 → 5문항.

### AG-T52 체험 데모 휴먼 피드백 묶음 (2026-11-21 사용자 요청)
- 파일: `demo/feedback_store.py`(새), `demo/static/session-record.js`(새), `demo/server.py`(`POST /api/sessions`, `sessions_dir`), `demo/static/{app.js,index.html,style.css}`, `docs/03_사용자_테스트_가이드.md` 8절.
- 형식 `directon.demo_session/v1`: 서버 저장과 내려받기가 같은 JSON. 오디오는 두 트랙(customer = 보낸 PCM, agent = 실제 재생분) WAV base64, 턴 구간은 ms 오프셋으로만 담고 자르기는 저장 쪽(`write_session`)과 페이지 재생 쪽이 같은 값을 쓴다.
- 테스트 먼저: 폴더 배치·session.json에 base64 없음·ms 자르기·스테레오 믹스·cases.jsonl(history 누적, 정정 우선)·덮어쓰기 시 오래된 클립 삭제·잘못된 session_id/schema/샘플레이트/base64/트랙 거부·index.jsonl·CLI·서버 400. JS(node): 트랙 조립·끼어들기 잘라내기·WAV·구간 계산·base64.
- 덤: 통화가 끝난 뒤 두 번째 통화 시작이 막혀 있던 문제(`if (call) return`) 수정.
- (jin 브랜치 기록) 이 Task는 다른 줄기(신호진, AG-T36~T51)에서 만들어 이 브랜치로 옮겼다. 위 AG-T29~T35는 이 브랜치의 번호다.

### AG-T53 이식용 피드백 키트 (2026-11-21 사용자 요청)
- 파일: `feedback_kit/`(새: `server.py`·`__main__.py`·`store.py`(← demo/feedback_store.py 이동)·`static/{index.html,app.js,style.css,session-record.js,mic-worklet.js}`·`README.md`), `demo/feedback_store.py`(키트 store를 다시 내보내는 얇은 모듈, 데모 기본 저장 위치 유지), `demo/server.py`(static/에 없는 파일은 `feedback_kit/static`에서), 데모의 `session-record.js`·`mic-worklet.js`는 키트로 옮김(원본 한 벌).
- 턴 나누기: `debug.*`가 없으면 브라우저 `SpeechTracker`(마이크 유성 프레임)로 말 구간을 재고, 상담원 첫 반응(PCM·`agent.transcript`) 순간을 말 끝 판정으로, `turn.end`로 닫는다. 판정이 늦은 턴의 고객 구간 끝은 말 끝 + 800ms까지(`turnClips` MAX_TRAIL_MS).
- 테스트 먼저(`tests/test_feedback_kit.py`): 127.0.0.1 바인딩, `/api/config`, 정적 파일·경로 이탈 404, 저장·거부, 우리 코드 import 없음, 다른 폴더에 복사해 `python -m feedback_kit --help`·`feedback_kit.store` 풀기, README 필수 항목. JS: SpeechTracker, 긴 말 뒤 공백 자르기.

### AG-T54 요청 종류 판정률 평가 (2026-11-21 사용자 요청)
- 근거: "knowledge.py로 시스템 프롬프트를 작성했을 때의 고객 요청 종류 판정률을 테스트해보고 더 높은 결과가 나오게끔 다듬어줘", "이 시나리오들(녹음 대본 200문항)에 대해서도 테스트를 진행해주고".
- 파일: `scripts/intent_eval.py`(새), `data/eval/request_types.json`(새: 연습 80문항), `data/eval/haruon200/{cases.jsonl,labels.json}`(새: 녹음 대본 PDF에서 추출, 시나리오 분포가 대본 표지와 같음), `tests/test_intent_eval.py`(새).
- 행동 코드: ASK_ID·ASK_PRODUCT·ASK_DETAIL·CLARIFY·LOOKUP_ORDER·LOOKUP_RETURN·SEARCH·ACT:<접수 함수>·ANSWER·HANDOFF. 접수 실행 > 조회·검색 > 말 분류 순으로 하나를 고른다. 말은 마지막 질문 문장으로 정하고, 실제 안내 문장(공감·사과 제외 25자 이상)이 있으면 ANSWER도 맞음으로 본다.
- 분류기 검증: 연습 80문항 실제 상담 답에 돌려 79/80이 라벨과 같다(P0029 실제 답은 "감사합니다"뿐).
- haruon200 조건: 현재 시각 2026-11-24(화) 10:30, 등록 고객은 이름 + 가상 번호(모의 DB에 주문 없음 → 조회 not_found). 이전 대화에 주문번호가 없는 접수 문항은 조회도 맞음.
- 남은 일: OpenAI 키로 기준 측정(`--repeat 3`) → 틀린 유형별 `agent/knowledge.py` RULES 수정 → 재측정.

### AG-T55 소음 속 말 끝 (2026-11-21 사용자 요청)
- 근거: "주변 소음이 있으면 엔드 포인트가 안끊기는 문제가 있네. 이 부분을 어떻게 해결할 수 있을까?", "논의한 내용을 반영해줘".
- 원인: 침묵은 VAD 확률이 임계 미만인 프레임으로만 쌓이고, 음성 프레임 하나에 0이 된다. 주변 말소리·TV는 Silero가 음성으로 보므로 침묵이 쌓이지 않아 확정이 안전 상한(20초)까지 밀린다. NoiseGate(AG-T10c)는 출렁임 판정을 통화 첫 200ms로만 하고, 엄격 판정의 유지 기준이 잡음 바닥 + 3dB라 손님보다 12dB 작은 말소리도 음성으로 남는다.
- 파일: `agent/config.py`(설정 5개), `agent/session.py`(`_process_frame`, `_speaker_level`, `_track_text`, `_check_text_stall`, `_frame_db`), `tests/test_noise_endpoint.py`(새).
- 동작: (1) 침묵 뒤 음성이 `resume_min_ms`(100) 미만이면 침묵으로 세고 그동안 판정을 보류, (2) VAD 음성 중 받아쓰기 글이 `text_stall_cap_ms`(3000) 그대로면(글이 있을 때만) 끝맺음 요청 후 최대 300ms 뒤 확정, (3) `text_stall_end_ms`(기본 0)부터는 멈춘 시간을 침묵 200ms부터로 결정기에 넘김, (4) `speaker_gate_db`(기본 0, 제안 12): 확실한 음성(p ≥ 0.8) 에너지 75분위보다 그만큼 작은 음성 프레임은 침묵. 끼어들기 감지(`_watch_barge_in`)는 바꾸지 않았다.
- 남은 일: 잡음 합성(white·babble, SNR 지정) 음성으로 오프라인 비교(말 끝 전 확정·놓침·지연 p50/p95·확정 6초 초과 비율) 뒤 (3)·(4) 기본값 결정.

### AG-T56 번호 되묻기·응답 지연 (2026-11-21 사용자 요청)
- 근거: "주문번호를 알려줬을 때 잘 인식했는데도 다시 묻는 것 같네. 멘트를 들은 숫자를 말해주고 그게 맞냐고 반문하게 수정해줘. 그리고 채움말 후로 나오는 응답이 좀 늦네."
- 데모 기록(DEMO-20261121-165416-mm6k) 4턴: 받아쓰기 "010. 2215. 7723이요."는 맞았는데 LLM이 번호 없이 `get_order_info`를 불러 검사(`missing`)에 막혔고, LLM이 "번호가 잘 안 들렸어요…"라고 지어냈다. 5턴: 함수 결과 뒤 결과 문구 → 채움말 → LLM 2.8초 → TTS 첫 바이트 3.5초. 문장 TTS(BAZE `gemini-3.8-flash-tts`) 첫 바이트가 턴마다 3.0~3.5초.
- 파일: `agent/guards.py`(`heard_number`, `heard_digits`, 조회 번호 채우기), `agent/responder.py`(`_confirm_sentence`, 캐시 답 문장, 채움말 취소), `agent/config.py`, `tests/test_number_confirm.py`(새, 9개).
- 동작: (1) 조회 함수에 번호가 없고 이번 고객 말에 형식에 맞는 번호가 하나면 그 번호로 조회(발신자·이전 대화 번호 우선 규칙은 그대로). (2) 조회가 번호 문제(missing·unclear_number)로 막혔거나, 이번 말에서 들은 번호로 `get_order_info`가 not_found면 LLM 회차 없이 "제가 들은 번호는 공일공, ….입니다." + "맞으실까요?"로 끝낸다. 직전 상담원 말이 이미 번호 확인이었으면 하지 않는다. `get_return_info` not_found는 되읽지 않는다(연습 문항 정답이 접수 유형을 묻는다). (3) 결과 문구(`post_tool_bridge`)를 냈으면 채움말 타이머를 취소. (4) 답 문장이 캐시 문구와 같으면 TTS 없이 캐시 음성(서버 시작 때 `answer_phrases` 4개 예열).
- 남은 일: 응답 지연의 대부분은 TTS 첫 바이트(3초대)다. 행사 지정 `gemini-3.8-flash-lite-tts`가 게이트웨이에서 되면(`GEMINI_TTS_MODEL`) 비교. 함수 결과 뒤 회차만 추론 수준을 낮추는 안은 응대 점수 영향 측정 뒤.

### AG-T57 KT 통화 피드백·예시 point 반영 (2026-11-21 사용자 요청)
- 근거: "KT 상담원 사용해본 것에 대한 피드백을 반영해주고 examples의 point를 잘 반영하게끔 시스템 프롬프트를 수정해줘." 자료: 보완 제안 PDF(KT 고객센터 AI 통화 관찰, 2026-11-20), 공식 `examples.json` 10개.
- 반영(`agent/knowledge.py` RULES, 57 → 59줄): 같은 질문 두 번 금지·바꿔 묻기, 고객 탓 문구 금지, 짧은 "네"는 다음 단계로·"아니에요 괜찮습니다"류는 거절 우선(제안한 처리 안 함), 확인 최소화(조회는 바로, 발신자 번호는 "등록된 번호로"만, 분명한 접수는 되묻지 않되 접수 문장에 대상 복창), 배송지 변경(여러 주문 중 아직 받지 않은 최근 주문, 출고 전 주소 받고 상담원 처리, 출고 뒤 변경 불가 + 상담원 연결, EX03), 취소 환불 문의는 번호 없으면 주문번호 먼저(EX09), 입금전 안내 예(EX01), 번호는 하이픈 없이 쉼표, 번호 확인 문구를 시스템 되읽기 문구와 맞춤. `agent/responder.py` `tts_text`: TTS 입력에서만 숫자 사이 하이픈을 쉼표로.
- 반영하지 않은 것: 제안서의 '취소·반품·교환은 접수 전 명시 확인'. 연습 문항 실제 응대(예: EX05 "전체 환불 원해요" → 바로 return_order)와 어긋나고 턴이 늘어 응대·속도 점수를 잃을 수 있어, 접수 뒤 복창(암묵 확인)으로 대신했다. 끼어들기의 짧은 군말 무시는 프롬프트가 아니라 바지인 코드 쪽 일이라 이번 범위 밖.
- 남은 일: `scripts/intent_eval.py`·공식 채점으로 전후 비교. EX01~EX10을 문항으로 넣어 재는 것도 가능.

## 8. 수동 체크리스트

| 항목 | 기대 | 결과 |
|---|---|---|
| 생성 음성 샘플 10개 청취 | 자연스러운 한국어, 통화 환경 구분 | |
| 상담원 응답 음성 10개 청취 | 응답이 질문에 맞고 끊김 없음 | |

## 9. 기존 테스트 영향
없음(신규 저장소).

## 11. 성공 지표
`docs/01_구현계획.md` 4.9절: first_sound p95 ≤ 1.3초, 잘못 끊기 ≤ 1%, 함수 일치 ≥ 95%.

## 12. 리스크
`docs/01_구현계획.md` 8절 + Gemini STT 지연이 Soniox보다 클 수 있음(실측으로 확인).

## 13. 발견 이슈 · 범위 밖
- (AG-T02) 키 인자(phone_number, order_id, product_name, option)만 정확 일치로 matched. 자유 텍스트(reason, new_address, preferred_time)는 판정 제외 → 채점기도 키 인자만 비교. X-Session-Id는 ASCII여야 한다(httpx). 데이터 기준일 2026-11-20(금).
- (AG-T07) 측정기는 바이너리 메시지 단위로 RMS를 잰다 → 캐시 PCM 앞 무음 제거 + 100ms 청크 송신. 실제 TTS 음량이 작으면 첫 청크 RMS<300 가능 → AG-T09에서 확인.
- (AG-T03) Starter Kit 데모 meta.json의 speech_end가 wav와 맞지 않는다(DEMO1 3.087s인데 음성 ~8.1s, DEMO2 5.337s인데 파일 3.65s). 운영진 질문 후보. 데모 음성 무음 구간에 RMS 20~30 잡음 있음.
- (AG-T03) Smart Turn은 여러 문장 클립에서 낮은 확률(DEMO1 0.04, DEMO3 0.03) → 한국어 테스트셋으로 보정 필요(AG-T10).
- (AG-T03→T09) 결정기 클래스명은 `RuleTurnDetector`, build_deps가 찾는 이름(make_detector 등)과 다름 → AG-T09에서 연결.
- (AG-T05a) 2단계 함수 문항(get_order_list → cancel 등) 있음 → 채점기의 순서·부분 일치 규칙 필요. "…이삼일이요"처럼 숫자 뒤 '이요' 어미 → 정규화 확인. 새 문항은 원본 끝에 추가(case_id 밀림 방지).
- **(AG-T06) Gemini TTS 무료 티어는 모델당 하루 10요청.** 상담원 TTS(문항마다 호출)와 테스트 음성 합성 모두 막힌다 → 유료 결제 연결(AG-U04) 또는 대체 TTS 필요. 테스트 음성은 edge-tts 위주로 전환.
- (AG-T06) google-genai는 TTS에 `api_version=v1beta` 필요. Gemini TTS 응답은 RIFF WAV(+메타 청크) → 청크 파싱. flash-lite-tts는 스타일 지시를 소리 내 읽음 → 스타일 요청은 제외.
- (AG-T04) 실측(p50/p95): STT live finalize→최종 0.43/0.47s(CER 0), edge TTS TTFB 0.47/0.67s, LLM gemini-3.5-flash-lite 첫 이벤트 0.78/2.96s(툴 정확도 7/7). Gemini 3.x Flash는 503·시간 초과 잦음. Gemini TTS 16k 직접 출력은 400 거부 → soxr 변환. 본 응답 p50 약 1.7~2.2s → 첫 마디 캐시가 필수.
- (AG-T09a) 스모크 20문항: first_sound p50 0.50s/p95 1.94s, **말 끝 전 발화 8/20**(문장 사이 쉼·쉼표·"어…" 뒤에서 400~600ms 확정, Smart Turn 0.66~0.99), 함수 완전 일치 7/20. STT가 끊어 읽은 숫자를 빠뜨림(통화 채널·잡음). 확정 시점 STT 부분 결과가 비는 턴 7/20. babble을 음성으로 오인. LLM이 transfer_to_agent를 말로만 함. 첫 마디와 본 응답 사이 ~0.9s 공백. `gemini-2.5-flash-lite`는 이 키에서 404.
- (AG-T09a) Starter Kit 데모 음성은 실제로 한국어. 원본 측정기는 결과를 cp949로 쓴다.
- (AG-T10b) STT 설정으로는 숫자 정확도 개선 없음(digits 74.1%, phone 63~68%, edge 음성 54%). transcribe-live는 시스템 지시 불가. batch 재인식은 1.5~3s 추가·개선 0~1건 → 기본 끔. 부분 결과 숫자 복원(지연 0)만 기본 켬. 남은 대안: 자릿수 불일치 시 LLM 되묻기(프롬프트), Soniox 비교, 사람 녹음 확인.
- (AG-T10b) T09a의 '확정 시점 텍스트 공백 7/20'은 오독(`neutral`은 규칙 중립). 실제 공백 8.4%, 모두 1.3s 이하 짧은 발화(첫 부분 결과가 음성 시작 후 ~1.1s). 선호출 재사용률이 낮은 원인은 부분 결과가 마지막 단어를 놓쳐 finalize와 불일치 → 비교 규칙 완화는 세션 쪽(T10c).
- (AG-T10a) 선호출 재사용 2/20: 침묵 200ms 부분 결과가 마지막 어절을 놓치고 `_respond_turn`이 정확 일치만 재사용 → 제안: finalize 직후 선호출 재시작, 정규화 비교, 텍스트 기반 첫 마디 선택(T10c).
- (AG-T10a) LLM 폭주 생성 1건(같은 문장 11,544회) → 300자 상한으로 차단, 원인 미상. 옵션·상품명 표기 불일치(엑스라지↔XL, 회색↔그레이, 후드티↔기모 후드티)로 not_found. history에 주문번호 없는 후속 문항 6~7개는 기대값 점검 필요.
- (AG-T10d) 프롬프트의 "상품명이 분명하지 않으면 확인" 규칙 때문에 분명한 상품명도 되묻는다(T0075) → 카탈로그 정규화가 생겼으니 규칙 완화 후보. Gemini 404가 실행마다 1~2건 간헐 발생(재시도 시 정상).
- (AG-T12) 보정셋 771발화(test 153): false cut 42.5% → 15.7%, 1.1s 이하 쉼 false cut 22.9% → 1.3%, 지연 p50 504 → 965ms·p95 1114ms, miss 7.8% → 9.2%. **false cut ≤1%는 구조적으로 불가**: 남은 오류 대부분이 1.1s 초과 쉼과 함정(완결 문장+쉼+추가 요청).
- (AG-T12) Smart Turn은 한국어 통화 음성에서 변별력 없음(AUC 0.57, 함정 쉼 평균 p 0.97 > 진짜 끝 0.88) → 채택 설정에서 미사용. 텍스트 규칙 AUC 0.83, 텍스트+음성 길이 0.894. 미세조정은 실제 평가 음성 확보 후 재검토.
- (AG-T12) rules.py 숫자 판정 버그 2건(4자리 이하를 weak_final, '공일공에 …'를 미완으로) → 결정기 층에서 보정. 잡음이 적을수록 false cut 많음(잡음이 VAD 침묵을 쪼갬). 보정셋 Gemini 음성은 429로 미포함.
- (AG-T10c) 잡음 VAD 래퍼: babble 말 끝 놓침 34% → 21%지만 보정셋 false cut 16.7% → 18.4% → 기본 끔(AD8). 측정 시간대에 flash-lite 첫 이벤트 시간 초과가 잦아 확정 → LLM 첫 이벤트 ~4s(폴백) — LLM 지연이 본 응답 공백의 주원인.
- (AG-T13) 응대(judge) round1 60.4 < 단순 조합 64.8: round1 말 끝 전 발화 16건이 응대 0점, 번호 자릿수 되묻기 16건(단순 0건), 측정 당시 폴백 모델 의존. 말 끝 전 발화를 빼면 66.0 vs 67.7. → 되묻기를 조건부(not_found 등)로 완화 검토. judge는 인사 반복·공백을 감점하지 않음. 서버 로그는 cp949. run_baseline.sh의 kill이 uv 자식 python을 남길 수 있음.
- (AG-T14) 브라우저 경로: 연결 전 마이크 오디오는 버려짐("연결됐습니다" 후 말하기 안내). 브라우저 노이즈 억제·AGC가 쉼 길이를 바꿔 조기 확정 1/4회 관찰. 16kHz AudioContext로 다운샘플 품질 문제 해결.
- (AG-T15a) BAZE 게이트웨이 모드에서는 phone_number가 "[PHONE]"으로 가려짐 → responder가 원문·caller로 복원. judge 비교 1은 게이트웨이 gpt-5.4-mini 혼합 조건(1회) — Gemini 단독 judge 재측정은 AG-T15d에서. 폴백 체인의 gemini-2.5-flash-lite 404. eval 로그에 `aclose(): asynchronous generator is already running` 경고(비치명).
- (AG-T14b) history 마지막이 '?'이면 expect_short로 대기 −100ms → 띄어 읽은 번호가 중간에 끊김(1/2회). 함수 결과와 다른 금액·요일 착오 발화 관찰(LLM).
- (AG-T15c) 컨텍스트 캐시는 prompts.py의 `## 이번 통화` 마커에 의존(바뀌면 캐시 꺼짐). 캐시 요청엔 system_instruction·tools 동시 지정 불가 → 가변부를 별도 user 턴으로. DEFAULT_LLM_FALLBACK의 gemini-2.5-flash-lite는 404 → 제거 권장.
- (AG-T08) 숫자가 아닌 말 속의 수 단어도 잡힌다("이 번호요 공일공" → "2010"). "다시" 정정은 앞부분을 대체하지 않고 이어 붙인다. 고유어 수(하나, 둘) 미지원. 콜백 시간 인자 형식은 Mock API 정의와 맞춰야 한다.

- (AG-T16) BAZE 경유 LLM 첫 이벤트 지연이 들쭉날쭉: 3회 실측 gpt-5.4-mini 4.75/1.40/1.94초, gemini-3.5-flash-lite 1.50/1.02/5.66초. `LLM_FIRST_EVENT_TIMEOUT_S`(2.5초)를 넘기면 3.1-flash-lite로 폴백된다(스모크 1회 발생). BAZE TTS 첫 오디오 3.24초(비스트리밍, 2.8초 분량). → BAZE 모드 속도 점수는 AG-T10/T11 측정에서 확인. 게이트웨이 Soniox는 stt-rt-v6·16kHz 정상.

- (AG-T15d) 당일 TTS 대체 위험: gemini-3.8-flash-tts Tier 1 일일 100회 한도 소진 상태 — edge 장애 시 대체 실패(01:35~01:47 장애 구간에서 사과문 응답 발생). 대체 모델을 2.5-flash-preview-tts로 바꿀지 검토(코드 변경, 별도 Task).
- (AG-T15d) R2-best 남은 실패: 말 끝 전 발화 12건(대부분 tool_action), 첫 소리 1.5초 초과 13건(모두 babble), caller 문항에서 get_order_list 선호출 약 12건(기대값 엄격성 점검 필요), STT 숫자 오인식 약 6, 되묻기 약 8 — round2.md 7절.
- (AG-T15d) 4자리 주문번호 STT가 앞자리 "일"을 빠뜨림(연습판 함수 실패 6건 중 5건). 템플릿 뒤 LLM 세부 누락·반복. Jev 인계 질문당 약 5.5회 호출(선호출 텍스트 바뀔 때마다) — 단가 미확인.

## 14. 작업 로그
- 2026-11-19 | AG-T01 | DONE | git init, uv(Python 3.12) 의존성, `agent/core/types.py` 계약, CLAUDE.md, pytest 설정, 테스트 0개(골격)
- 2026-11-19 | AG-T08 | DONE | agent/normalize.py, tests/test_normalize.py, 94개 RED ModuleNotFoundError → GREEN 94 passed, normalize_args에 now 인자 추가(설계 반영), 한계는 13절
- 2026-11-19 | AG-T02 | DONE | mock_api/{server.py,data/tools.json,mock_data.json,README.md}, cases/manual.md, tests/test_mock_api.py, RED ModuleNotFoundError → GREEN 54 passed, status 5종(ok/not_allowed/no_refund/not_found/invalid_arguments)
- 2026-11-19 | AG-T07 | DONE | agent/{config,tools_client,tts_cache,prompts,responder,session,turn_log,server}.py, tests 3개, RED ModuleNotFoundError → GREEN 40 passed, 전체 428 passed, VAD·결정기는 팩토리 주입(계약 주석 반영)
- 2026-11-19 | AG-T03 | DONE | agent/audio.py, agent/turn/{vad,smart_turn,rules,detector}.py, scripts/download_models.py, tests 2개, RED ImportError → GREEN 130 passed, VAD 0.1~0.2ms/청크, Smart Turn 23~31ms(4스레드), 잡음성 짧은 발화(<250ms, 텍스트 없음)는 상한에서도 끝 아님, 단답은 400ms
- 2026-11-19 | AG-T05a | DONE | data/testset/{scenarios_src.yaml,cases.jsonl,README.md}, scripts/gen_scenarios.py, tests/test_testset.py, RED 21 errors → GREEN 21 passed, 172문항(범주 7종·음성 11종·채널 3종 균형)
- 2026-11-19 | AG-T06 | DONE | eval/{augment,synth,build_testset,runner,score}.py, tests/test_eval_tools.py, RED ModuleNotFoundError → GREEN 120 passed, 전체 489 passed, 실제 스모크(edge·Gemini 합성, 증강 4종, runner+score), 레벨 정규화(RMS 3000) 추가, .gitignore에 캐시 경로 추가
- 2026-11-19 | AG-T04 | DONE | agent/providers/{_gemini,gemini_stt,gemini_tts,edge_tts,gemini_llm,soniox_stt,openai_llm,factory}.py, scripts/bench_providers.py, docs/decisions/bench_providers.md, tests/test_providers.py, RED 29 실패 → GREEN 35 passed, live 스모크 통과(edge TTS→live STT→flash-lite 툴 호출), 기본 LLM gemini-3.5-flash-lite·TTS edge
- 2026-11-19 | AG-T09a | DONE | agent/{server,tts_cache,config}.py, agent/providers/{fallback_llm,factory}.py, scripts/build_smoke_set.py, data/testset/smoke/, tests/test_server_wiring.py, docs/decisions/baseline_smoke.md, RED 24 실패 → GREEN 27 passed, 회귀 521 passed, E2E 스모크·Starter Kit 측정기 호환 확인, 개선 후보는 baseline_smoke.md
- 2026-11-19 | AG-T05b | DONE | data/testset/audio/ 172 wav + meta.json(gitignore, `eval.build_testset`로 재생성), 1차 161건 + 429 실패 11건 순차 재시도로 완료. Tier 1에서도 TTS 동시 3개는 분당 한도에 걸림 → 동시 1~2 권장
- 2026-11-19 | AG-T09b | DONE | docs/decisions/baseline.md(+report 사본), runs/run_baseline.sh, 속도 67.4·말 끝 전 발화 55/172·첫 소리 p50 0.51s·함수 56.4(완전 일치 0.52)·에러 0, 추정(응대 제외) 63.0
- 2026-11-19 | AG-T10b | DONE | eval/stt_eval.py, agent/providers/{stt_recheck,gemini_stt,factory}.py, tests 2개, docs/decisions/stt_tuning.md, RED ImportError → GREEN 34 passed, 회귀 610 passed, 숫자 정확도 74.1% 유지(설정 실험 무효), 부분 결과 숫자 복원 기본 켬·batch 재인식 기본 끔, live 세션 약 950회
- 2026-11-19 | AG-T10a | DONE | eval/llm_eval.py, agent/{prompts,responder,config}.py, tests/test_llm_eval.py·test_prompts.py, docs/decisions/llm_tuning.md, RED ImportError → GREEN 57 passed, 회귀 612 passed, 오프라인 함수 완전 일치 0.70~0.72 → 0.87~0.90, judge 69.5 → 73.0, 호칭 반복 22 → 0, 번호 인자 교정·자릿수 검사·인계 안전망·필러 150ms·응답 300자 상한, LLM 약 2,750회+judge 344회
- 2026-11-19 | AG-T10d | DONE | agent/catalog.py, agent/normalize.py(catalog 인자 추가), tests/test_catalog.py, docs/decisions/catalog_normalize.md, RED ImportError → GREEN 73 passed, 회귀 247 passed·전체 749 passed, 상품 관련 21문항 함수 완전 일치 0.71 → 0.79
- 2026-11-19 | AG-T12 | DONE | scripts/gen_turnset.py, eval/{turn_sim,turn_calibrate}.py, agent/turn/detector.py(TableTurnDetector·CALIBRATED_CONFIG·make_detector 추가), data/turnset/(라벨·보정 결과, 오디오 제외), tests 2개, docs/decisions/turn_calibration.md, RED ImportError → GREEN 194 passed, 전체 759 passed, Gemini STT 약 700회·edge 700회
- 2026-11-19 | AG-T10c | DONE | agent/{session,config,tts_cache}.py, agent/turn/vad.py(NoiseGate·NoiseRobustSileroVAD·make_vad), eval/vad_noise_eval.py, tests/test_session_precall.py·test_vad_noise.py, docs/decisions/session_tuning.md, RED ImportError → GREEN 28 passed, 선호출 재사용 2/20 → 24/30, 텍스트 기반 첫 마디, 잡음 래퍼는 AD8로 기본 끔(새 테스트의 기본값 단언을 결정에 맞게 수정)
- 2026-11-19 | — | 측정 | round1(40751d7): 속도 67.4 → 85.6, 말 끝 전 발화 55 → 23, 함수 56.4 → 74.4(완전 일치 0.52 → 0.72), 추정(응대 제외) 63.0 → 81.1, turn.end p50 3.07 → 5.99s(flash-lite 시간 초과로 폴백 다발). docs/decisions/round1.md
- 2026-11-19 | AG-T13 | DONE | bench/{naive_server.py,run_naive.sh}, tests/test_naive_server.py, docs/decisions/naive_benchmark.md, 6 passed·전체 783 passed, 단순 조합 N0 추정 총점 53.1 vs round1 70.7(속도 25.2 vs 85.6, 함수 65.7 vs 74.4, 응대 64.8 vs 60.4), N1(문장 TTS만) 효과 ~0.2s
- 2026-11-19 | AG-T16 | DONE | agent/providers/{baze_tts.py(신규),factory.py,_gemini.py,openai_llm.py}, pyproject.toml·uv.lock(openai 추가), tests/test_providers.py(BAZE 6개), RED 6 failed → GREEN 41 passed, 전체 802 passed, 실호출 스모크(라우팅·도구 호출·TTS) 정상, LLM 지연 편차는 발견 이슈로
- 2026-11-19 | AG-T14 | DONE | demo/{server.py,static/*,run_demo.ps1,run_demo.sh}, agent/session.py(debug:true일 때만 debug 이벤트, 끔이면 바이트 동일), tests/test_demo_server.py·test_session_debug.py, docs/03_사용자_테스트_가이드.md(시나리오 16개), RED ImportError → GREEN 13 passed, 전체 802 passed, WS 스모크·헤드리스 크롬(가짜 마이크) 확인
- 2026-11-19 | AG-T15a | DONE | agent/{prompts,responder}.py, eval/llm_eval.py(--stt-text), tests/test_reprompt_policy.py, docs/decisions/llm_tuning_r2.md, RED ImportError → GREEN 35 passed·전체 915 passed, 후보 C 채택: judge(STT 텍스트, 146문항) 58.9 → 65.3, 함수 유지(84.2 → 83.6, Gemini 직접), caller 주문 목록 기반 주문번호 교정, 응답 내용 보강(3~4문장), DIRECTON_PROMPT_DETAIL·DIRECTON_REPAIR_ORDER_ID로 끔
- 2026-11-19 | AG-T14b | DONE | demo/{presets/history_presets.json,server.py,static/*}, docs/03_사용자_테스트_가이드.md(6절), tests/test_demo_history.py, RED 2 실패+6 에러 → GREEN 8 passed, 회귀 21 passed, 프리셋 8개 mock matched·실서버 스모크
- 2026-11-19 | AG-T15c | DONE | agent/providers/{hedged_llm,llm_cache,gemini_llm,factory}.py, scripts/bench_llm_r2.py, tests/test_hedged_llm.py, docs/decisions/llm_latency_r2.md, RED 26 실패 → GREEN 27 passed·전체 924 passed, 기본 LLM_MODE=hedge(500ms)+컨텍스트 캐시: 첫 이벤트 p95 3.73 → 1.64s, 공백 p90 2231 → 207ms, 질문당 비용 0.248 → 0.074¢
- 2026-11-20 | AG-T15b | DONE | detector.py 13묶음 표·정정/'에' 이어 부르기/쉼표 규칙·상한 1.3초, turn_sim·turn_calibrate r2, 보정셋 771→960, 172 시뮬 말 끝 전 응답 28→13(교차 검증 18), 지연 p95 1292ms, miss +1.2%p(교차 +1.7%p), test_turn_r2 43 RED → 52 GREEN, 전체 924 passed. 이슈: 테스트셋 불만 문항 음성에 TTS 스타일 지시가 읽혀 들어감(T0035~37 재합성 필요), 실서버 재측정은 T15d
- 2026-11-20 | AG-T23 | DONE | docs/06_기획서_초안.md(공식 12개 목차), 07_발표_초안.md(9장·420초·예상 질문 8), 08_검토보고서_선준비.md(1장), 코드 변경 없음. 당일 값은 〔당일 측정〕 표시(round2, after_tool, T17~T22 결과)
- 2026-11-20 | AG-T17 | DONE | mock_api --data/--tools(+Starter Kit 형식 {함수: [{arguments,result}]}), DIRECTON_MANUAL/BRAND/STYLE(캐시 고정부 유지), 주문번호·전화 자릿수 데이터 추론(catalog.order_id_lengths), DIRECTON_NO_AUDIO_LOG 고객 텍스트 제거, docs/05_당일_체크리스트.md, .gitignore official/. test_dayof_swap 22 RED → 23 GREEN. 이슈: stt_recheck.py:139,158 INFO 로그에 고객 발화(콘솔을 파일로 돌리면 저장됨), responder MIN_NUMBER_DIGITS=7이 4자리 번호를 버림(T21에 전달)
- 2026-11-20 | AG-T22 | DONE | data/practice_health/(가상 브랜드 온결헬스, 함수 15개(결제 2개 신규), 상품 15·주문 23(4자리), Starter Kit 매핑 형식 mock_data 280항목, manual.md, cases 40(업무 7종·몸 불편 인계 3·history 3), generate.py), 음성 40개(Gemini 73회, gitignore). test_practice_health 23 RED → 24 GREEN. 이슈: 매핑 모드 transfer_to_agent는 reason 무관 matched, H0001 '천이백삼십사' 읽기 정규화 미확인
- 2026-11-20 | AG-T18 | DONE | fallback_tts.py(edge→Gemini, 첫 청크 2초·쿨다운 30초), factory.make_tts_with_fallback, server GuardedWS·세션 사망 시 사과+turn.end 열화 모드, _privacy.redact(stt_recheck·openai_llm 로그), scripts/tunnel.ps1·sh(cloudflared), eval/stress.py, docs/decisions/stability.md. test_stability 23 RED → 32 GREEN, 전체 1073 passed. 실측 4×2 동시: 실패 0, 첫 소리 p50 0.86 / p95 1.21s, RSS 159→246MB. 이슈: Windows에서 두 서버가 같은 포트 9000 공유 가능(당일 단일 프로세스 확인), responder 번호 로그·session 사과 송신 미보호(T21·T19에 전달)
- 2026-11-20 | AG-T19·T20 | DONE | session.py 바지인(응답 중 음성 300ms·첫 소리 뒤 1초 무장, 남은 TTS 중단·들린 만큼 transcript·turn.end·끼어든 말+200ms를 다음 턴 STT로, 송신 페이싱 1.5초 앞까지·10초 뒤 해제), 실패 시 turn.end 보장·_cleanup 상태 초기화, tts_cache.choose_opener(답 준비 시 생략·직전 반복 금지·상황별)·choose_filler, 데모 바지인 신호·첫 마디 체크박스, 가이드 S17. 새 테스트 29개 RED(ImportError) → GREEN, 전체 1078 passed. 첫 마디 정책은 기본 끔(session.start opener_adaptive) — 켜면 기존 단언 9개 변경 필요(결정 대기). 이슈: server 워밍업에 opener_phrases 미포함, config 필드 없음, 끊긴 답 프롬프트 규칙, 함수 실행 중 끼어들기
- 2026-11-20 | AG-T24 | DONE | agent/turn/jev_turn.py(말 끝 Noul 3개, 200ms 침묵에 자체 요청, DIRECTON_JEV_TURN=1), agent/handoff_jev.py(인계 Noul 6개·기준별 임계·영업시간 외 콜백, DIRECTON_JEV_HANDOFF=1), eval/jev_turn_eval.py·jev_handoff_eval.py, typesafe-sdk 0.7.5, docs/decisions/jev_turn.md·jev_handoff.md. 테스트 25개 RED(수집 오류) → GREEN, 전체 1079 passed. Jev 지연 p50 176 / p95 247ms. 말 끝: AUC 0.916(규칙 0.826)이나 표와 결합 시 false cut 1건 감소뿐 → 선택 사용. 인계: testset 16/16·오탐 0(LLM 10/16), 연습판 4/4 — 같은 문항으로 조정한 표본 안 수치. 세션 배선 미적용(제안 패치 문서화). Jev 호출 1,456회. 의도 Choice는 생략
- 2026-11-20 | AG-T25 | DONE | session.py SessionDeps.handoff(선택 필드, types.py 계약 변경 없음)·선호출과 동시에 Jev 인계 요청·확정 뒤 ≤0.3초 대기·인계면 힌트(동적부) 추가 후 힌트 없는 선호출 폐기·failures 카운트·detector.set_context, config 바지인·페이싱·opener_adaptive(기본 False)·jev_handoff_wait_s 필드(DIRECTON_*), server 워밍업에 첫 마디 문구·_make_handoff, prompts 끊긴 답 규칙, handoff_jev 힌트에 중복 사과 금지. test_wiring_t25 13 RED → 18 GREEN, 전체 1097 passed. 실측 5건(round1 LLM 누락분): 3건 transfer_to_agent 호출, 2건은 말 끝 전 확정으로 부분 텍스트(턴 결정기 문제)
- 2026-11-20 | AG-T21 | DONE | responder.py 조회 뒤 hedge(DIRECTON_POST_TOOL_HEDGE 기본 1)·결과 템플릿 첫 문장(DIRECTON_TOOL_TEMPLATE 기본 0, 켜면 기존 test_llm_eval 2개 단언과 충돌)·번호 자릿수 상대화(repair_thresholds)·번호 로그 redact, runner/score after_tool_ms·speed_official·total_official. test_after_tool 24개 RED → GREEN, 전체 1099 passed. 40문항 실측(B2 hedge vs C2 +템플릿): 조회 뒤 실제 답 p50 2.32→0.73s, 공식 속도 91.6→92.5(채움말 제외 83.7→90.5), judge 70.0→70.4, 함수 82.5→80.0. 이슈: 템플릿 바꿔 말한 반복 약 5/25, validate_number_length가 스키마 'N자리'에 의존, 페이싱 뒤 turn_end p50 4.4→10.8s, edge 타임아웃 시 Gemini TTS 429
- 2026-11-20 | AG-T15d | DONE | eval/synth.py 말투 지시 분리 프롬프트(STYLE_PROMPT_VERSION 3)·per_day 한도 판별, test_eval_tools 4개 추가, 오염 음성 5문항 재합성, docs/decisions/round2.md. 172문항 10회(A/A 포함): 잡음 폭 judge ±4·시스템 ±2.5. R2-best(템플릿+Jev 인계+첫 마디 정책) 시스템 79.8(÷5=16.0), 공식 속도(채움말 제외) 88.3, 조회 뒤 답 p50 0.94s, 인계 15/16 오탐 0. 템플릿 2.3→1.0s 유효, Jev 인계 재현율 13→15, Jev 말 끝 효과 없음, 첫 마디 정책 시스템 중립. 연습판 40문항 시스템 74.5. 전체 1103 passed. 이슈: TTS 대체 일일 한도, 4자리 번호 STT, 남은 실패 목록(13절)
- 2026-11-20 | AG-T26 | DONE | docs/09_결과보고서.html(선택 11개 trade-off 카드, 점수 흐름·조회 뒤 지연 차트, 당일 설정, 남은 실패, 연습판, 비용), artifact https://claude.ai/artifact/NgVkk6fBVUhNbW7sTQSxbb. 코드 변경 없음
- 2026-11-20 | AG-T28 | DONE | agent/responder.py(confirm_candidates·last_digits·digits_ko·confirm_message, _confirm: 막힌 호출·not_found → 고객 본인 주문 후보 1~2개 needs_confirmation, get_order_list 결과에서 상품명 보관, 스위치 DIRECTON_CONFIRM_ORDER_ID 기본 끔), tests/test_order_confirm.py 13개 RED ImportError → GREEN, 전체 1116 passed. 실서버 측정 안 함(당일 A/B 후보). 다음 턴에서 "네"를 받았을 때 이전 대화의 끝자리로 주문을 찾는 처리는 없음(LLM이 목록 조회로 찾아야 함)
- 2026-11-21 | AG-T29a | DONE | git mv: mock_api/data → legacy/shop_mock, cases/manual.md → legacy/shop_mock/manual.md, data/practice_health → legacy/practice_health, data/testset → legacy/testset. 코드·테스트·스크립트·.gitignore 경로 치환(docs/ 기록은 그대로). 전체 1116 passed, 1 failed(test_turn smart_turn DEMO1_half, 이동 전부터 실패) + test_session_precall 2개 간헐 실패(이동 전부터)
- 2026-11-21 | AG-T29b | DONE | 옛 Starter Kit 삭제(git rm), 데모 음성·데이터 → tests/fixtures/starter_kit_demo(테스트 경로 4개 파일 수정), 공식 zip 원본을 DIRECT_ON 2026 Starter Kit/로 이동(official/ 폐지), .gitignore·CLAUDE.md·docs/12·docs/05 경로 갱신. 코드 변경 없음. 전체 1114 passed, 실패 3개는 교체 전과 같음(test_turn DEMO1_half, test_session_precall 간헐 2개). 처음 잡았던 Mock·프롬프트 공식화 계획(T29b·T29c)은 사용자 요청 범위 밖이라 취소
- 2026-11-21 | AG-T30 | DONE | mock_api/server.py를 공식 규칙으로 새로 씀(Store.lookup: 조회=번호 숫자 일치·주문번호 우선, 접수=필수값 → ok/invalid·기록 있으면 그 결과, 검색=difflib 최근접, 미정의 404, `--db`=`--data`, 기본 host 0.0.0.0, 기본 경로 data/ → 공식 키트), start_mock.sh, tests/test_mock_official.py 37개(공식 mock_db 76개 기록이 모두 자기 status를 돌려주는 검사 포함), 픽스처 tests/fixtures/official_mini(지어낸 값). 삭제: 옛 쇼핑몰 Mock 로직, legacy/shop_mock, legacy/practice_health, eval/llm_eval.py, scripts/bench_llm_r2.py, 테스트 7개 파일(test_mock_api·llm_eval·order_confirm·reprompt_policy·testset·demo_history·practice_health)과 옛 데이터에 묶인 테스트 13개(test_dayof_swap 10, test_catalog 2, test_demo_server 1). 전체 964 passed + 기존 실패 3개. 이슈: demo/server.py가 지운 legacy 회원 데이터를 읽는다(T32에서 공식 데이터로 고침)
- 2026-11-21 | AG-T31 | DONE | factory.py를 지정 모델 전용으로 새로 씀(Soniox·OpenAI·Gemini TTS, 직접 키 → BAZE 게이트웨이, 키 없으면 오류). server.py: Smart Turn 미사용, 함수 정의 로컬 로드(ToolsClient local_tools), TTS 캐시 키에 모델, 예열 상한 25초, .env 경로 고정, DIRECTON_NO_AUDIO_LOG 기본 1. requirements.txt·start_server.sh·scripts/package.sh(제출 .env는 대회 키만), pyproject Python 3.11 허용. 삭제: Gemini STT·LLM, edge-tts, 대체·동시 요청 LLM, 대체 TTS, Gemini 캐시, 숫자 재인식, Smart Turn 모듈, eval/ 전체, bench/, legacy/testset, 터널·옛 데이터 생성 스크립트, 그 테스트. **실측(대회 키):** LLM·STT 정상. TTS는 게이트웨이에 gemini-3.8-flash-lite-tts가 없고 스트리밍도 없다(/audio/speech/ 한 번에 응답, 첫 바이트 2.2~3.0초) → gemini-3.8-flash-tts로 진행, 운영진 확인 대기. 공식 측정기 5문항(v0a, 옛 프롬프트): 첫 소리 p50 0.94s, 조회 뒤 답 p50 3.76s, 함수 80, 속도 97. 전체 732 passed + 기존 간헐 실패 2개(test_session_precall), test_prompts 1개는 T32에서 교체. 계획과 다른 점: 말 끝 보정 도구(eval/turn_sim·turn_calibrate)도 Gemini STT에 묶여 있어 함께 삭제(data/turnset 음성은 남김), Smart Turn은 모듈만 지우고 세션의 None 처리 코드는 남음
- 2026-11-21 | AG-T32·T33 | DONE | 새 모듈 agent/knowledge.py(고정부=규칙+시나리오+브랜드 정보 96개+말투, 가변부=시각·발신자·정리한 이전 대화·번호 요약·첫 마디, TOOL_PHRASES·FAIL_SENTENCES)와 agent/guards.py(미정의 함수 차단, 번호 정리·알려진 번호로 맞춤, 필수값, 접수는 근거 있는 번호만·턴당 1개). session: build_system을 knowledge로, 턴마다 GuardContext, 통화 중 함수 호출·결과를 live_history와 conversation에 쌓음. responder: 옛 번호 교정·상담원 연결 강제 호출·주문 목록 조회·결과 템플릿 삭제(1057 → 619줄), 검사 통과분만 실행, 함수 이름을 읽는 문장 걸러 냄, 함수 뒤 답이 없으면 사과 문장. openai_llm: prompt_cache_key와 캐시 토큰 로그(입력 13,202 중 13,056 캐시 확인), 예열을 실제 고정부로. tts_timeout_s 5 → 12(게이트웨이 TTS 동시 요청 시 5초 초과로 실패). 삭제: agent/prompts.py, tests/test_prompts.py. 테스트 692 passed(+ 기존 간헐 실패 2). 공식 측정 80문항(v2, 응대 채점 gpt-5.5): 응대 51.9 / 함수 75.0 / 속도 95.4 = 69.6, 첫 소리 p50 1.00s, 조회 뒤 답 p50 3.54s. 20문항 v1(가드 전)은 62.1. 발견: 시험 음성의 붙여 쓴 번호는 합성 음성이 뭉개 읽어 Soniox·gpt-4o-transcribe 모두 못 듣는다(음성에만 있는 번호 17개 중 5개 인식, 문맥 힌트 효과 없음). 두 Task를 한 커밋에 묶음(session.py에서 서로 물림)
- 2026-11-21 | AG-T33 보강·T34 | DONE | **게이트웨이 한도:** 분당 120회(연결·HTTP 요청 기준, STT 제어 메시지·음성 프레임은 세지 않음을 실험으로 확인). 다른 세션과 키를 같이 써서 v3 측정에서 STT 끊김·TTS 429 발생(v3 응대 46.1은 그 영향). 대책: 다음 턴 STT는 연결이 이어질 때만 연다(문항당 연결 1개 절약), 첫 문장 뒤 문장은 묶어서 합성(tts_combine), 게이트웨이 TTS 429는 기다렸다 재시도. **평가 도구:** scripts/text_eval.py — 저장해 둔 받아쓰기 결과로 LLM·검사·채점만 돌린다(OpenAI 키만 사용, 대회 키 불필요). **번호 규칙(사용자 확정):** 조회 번호는 발신자 → 이전 대화 → 들은 번호 순서로 코드가 채운다. 믿는 번호로 이미 조회했으면 형식이 맞는 들은 번호로 한 번만 재조회, 형식이 안 맞으면 조회하지 않고 다시 묻는다. 형식이 틀린 주문번호를 이전 대화의 주문으로 바꾸는 것은 후보가 하나뿐일 때만(여러 개 중 마지막을 집어 다른 주문을 안내하던 문제 수정). **검사 추가:** 통화 첫 마디에는 접수 함수 차단(연습 80문항에 첫 턴 접수 정답 0건), 전화 요청의 이름은 고객 말·이전 대화·발신자 정보에 있어야 함, LLM이 글로 쓴 함수 호출·JSON·한자 조각은 읽지 않음. **추론 수준:** OPENAI_REASONING_EFFORT 기본 medium(텍스트 평가 응대 none 55.2 / low 61.0 / medium 70.2, 첫 출력 p50 0.77 / 1.28 / 1.57초). **T34 미리 조회:** 구현했으나 기본 끔(켜면 응대 65.9 → 57.2, 주문과 무관한 문의에 조회 기록이 붙어 감점). 텍스트 평가 현재값(medium, 미리 조회 끔): 응대 65.9 / 함수 80.0. 테스트 705 passed(+ 기존 간헐 실패 2)
- 2026-11-21 | AG-T33 보강 2 | DONE | 사용자 확정 요청 반영: ARS 전화주문 번호 1600-0000을 규칙에 직접 적음(고객센터 번호와 혼동 방지), 브랜드 정보의 제품명 목록을 STT 용어로 전달(stt_terms)·틀리게 받아쓴 브랜드 이름 교정(fix_brand), 검사 추가(구매 의사만 있으면 상품 검색 차단, 반품·교환 사유는 고객 말에 근거, 전체·일부 반품 중복은 기존 '턴당 접수 1개'가 막음). **번호 확인 흐름:** 형식이 틀린 번호로는 조회·접수하지 않고 들은 번호를 불러 확인("…로 들었는데 맞으실까요?" → 아니면 "한 자리씩 또박또박"), 두 번 실패하면 상담원 연결. 들은 번호를 답에서 읽어 주는 것은 전화 요청 접수에만 적용(조회 성공 때도 읽으면 STT가 형식은 맞지만 틀리게 들은 번호가 채점에서 감점: t6 68.2 → t7 62.0). 말 끝 판단의 주문번호 완결 기준 10 → 15자리(DIRECTON_ORDER_DIGITS). 반품 접수 뒤 안내(회수 기사 방문·재포장·무상 반품·환불 시점)와 브랜드 정보 표기 우선 규칙 추가. 텍스트 평가(medium, 80문항): 응대 69.2 / 함수 80.0(맞음 40, 괜찮음 22, 틀림 18). 채점 AI 판정은 실행마다 ±4점쯤 흔들림. 음성 20문항(v4): 응대 57.5 / 함수 70 / 속도 100 = 72.8(같은 20문항 v1 62.1). 테스트 722 passed(+ 기존 간헐 실패 2)
- 2026-11-21 | AG-T33 보강 3 | DONE | 평가 방식: text_eval은 같은 코드로도 응대가 6점까지 벌어진다(다른 세션 실측: 같은 커밋 63.0 / 69.4). `--repeat N`을 넣고 변경 효과는 3회 평균으로 판단한다. 3회 평균 결과(80문항, medium): 기준값 응대 66.7(64.2~69.4)·함수 80.0 / 미리 조회 켬 62.5(62.1~62.9)·80.8 → 끔 유지(차이 −4.2, 처음 한 번 재고 본 −9는 과장) / 규칙 보강 69.0(68.5~69.5)·81.2 → 반영. 보강한 규칙: 이전 대화에 나온 반품 사유는 다시 묻지 않음·전부 반품이면 return_order 한 번, 환불 시점 문의는 조회한 현재 상태부터, 출고 전 주문은 출고 예정일과 수령 기간을 구체적으로, 다른 구매처(쿠팡 등) 주문은 번호를 묻지 않고 구매처 문의 안내, 조회 결과가 고객 말과 안 맞으면 상담원 확인, 상품 문의에 권장 섭취량·주의 포함. 응대 상한: 번호가 음성에서 깨지고 다른 출처도 없는 13문항(16.2점)은 풀 수 없어 약 84점(Soniox 언어 힌트 en / ko+en / 없음 모두 17개 중 5개로 동일). 작업 브랜치 main → jin(사용자 지시, 팀원별 브랜치)
- 2026-11-21 | AG-T33 보강 4 | DONE(전체 3회 측정은 보류) | 서브에이전트 3개를 파일 단위로 나눠 병렬 수정. **규칙(knowledge.py):** 상품 검색어에 궁금한 내용을 붙임(모의 검색이 가장 가까운 검색어 기록을 고르므로), '미확인' 값은 짐작하지 않음, 첫마디로 제품 별칭만 말하면 브랜드 정보의 제품명으로 확인, "일부 반품"이라고 직접 말하면 return_order_item, 반품 접수 뒤 안내 순서(회수 기사 방문 → 무상 → 입고 뒤 환불 → 그 결제수단의 환불 기간), 환불 방법만 물으면 주문 정보부터, 조회해 없던 번호는 다시 묻지 않음. "검색 결과와 브랜드 정보가 다르면 브랜드 정보 표기" 문장은 앞 문장과 충돌해 한 번도 지켜지지 않아 삭제. **검사(guards.py):** 이미 조회한 번호로 다시 조회하면 repeat_lookup으로 막고 "그 번호로는 내역이 없으니 주문번호나 다른 연락처 요청" 안내(전에는 형식이 맞는 번호에도 "형식이 맞지 않아 한 자리씩"이라는 틀린 안내), 결과 목록이 빈 ok도 not_found로 봄. **응답기·세션:** 막힌 호출이 있던 턴의 마지막 회차에 "부르지 않고 말로만 답한다" 안내(호출을 글로 쓰는 일 38회 중 16회 → 34회 중 0회), 조각난 JSON은 읽지 않음, 답 문장이 0개면 사과 문구, 두 번째 조회 결과 뒤 다른 대기 문구(post_tool_more), 실행한 번호가 LLM 인수와 다르면 tool 결과에 _called_with(접수한 번호를 올바로 읽음: P0133-03·P0119-04 3회 모두 맞음), 받아쓰기가 비었는데 말소리가 있었으면 최대 1.75초 더 기다림, Soniox 빈 결과 경고 로그. 확인된 사실: P0071-05는 연습 데이터에 그 고객의 조회 기록이 없어 정상 경로로 함수 점수를 받을 수 없다. 측정: 겨냥 19문항 3회 응대 71.2 → 90.0·함수 93.0 → 96.5, 80문항은 1회차 중 채점된 48문항만 유효(응대 68.8 → 75.8, 0.3 이상 나빠진 문항 없음). **평가 도중 OpenAI 키가 무효(401)가 되어 80문항 3회 평균은 못 냈다** — 새 키를 받은 뒤 다시 잰다. 저장소·커밋 기록에 키 문자열 없음을 확인. 테스트 787 passed(+ 기존 간헐 실패 2). 남은 일: 새 대기 문구의 TTS 캐시는 TTS를 켜고 예열해야 만들어진다. "영업일 기준 3~4일 안에 회수 기사 방문"은 브랜드 정보에 없고 연습 문항의 실제 상담 답 9건에서 온 문구다
- 2026-11-21 | AG-T52 | DONE | demo/feedback_store.py(새: bundle 검사·WAV 디코드·턴 클립·mix·cases.jsonl·index.jsonl·CLI), demo/static/session-record.js(새: Recorder·wavBytes·interleave·sliceMs·turnClips·base64·wavToInt16), demo/server.py(POST /api/sessions 300MB, /api/config sessions_dir), demo/static/app.js(녹음·이벤트 시각·턴 태그/STT 정정·턴/전체 재생·서버 저장·JSON 내려받기/불러오기·미저장 경고, 두 번째 통화 막힘 수정), index.html·style.css, docs/03 8절, tests/test_demo_session_store.py 27개·test_demo_record_js.py 6개(node). RED ImportError → GREEN, 데모 회귀 17 passed. 헤드리스 크롬 + 가짜 상담원 WS 스모크: 2턴 통화 → 저장 9개 파일, 트랙 에너지 위치·턴 클립 확인, 불러오기 → 평가 수정 → 내려받기 → CLI 풀기 오디오 바이트 동일. 전체 1373 passed, test_latency_sim::test_tts_delay 1건은 전체 실행 부하 때만 실패(단독·변경 전 모두 통과, 데모와 무관) (2026-11-21 jin 브랜치로 cherry-pick, 데모 파일은 충돌 없음)
- 2026-11-21 | AG-T53 | DONE | feedback_kit/ 새 키트(서버·페이지·README, 표준 라이브러리만), store·녹음 모듈·워클릿을 키트 원본으로 옮기고 데모는 키트 것을 씀, tests/test_feedback_kit.py 14개 + JS 2개. RED(모듈 없음) → GREEN, 키트·데모 테스트 55 passed. 헤드리스 크롬 스모크: debug 없이 PCM + turn.end(+ transcript 1회)만 보내는 가짜 상담원으로 브라우저 추정 턴 2개·녹음·클립·cases.jsonl 저장, 데모 회귀 스모크도 통과
- 2026-11-21 | AG-T54 | DONE(측정 전) | scripts/intent_eval.py, data/eval/request_types.json(80), data/eval/haruon200(200), tests/test_intent_eval.py. 분류기는 실제 상담 답 79/80 일치. 이 작업 환경에서 OpenAI 접속이 막혀 LLM 측정은 못 함
- 2026-11-21 | AG-T55 | DONE(측정 전) | agent/{config,session}.py, tests/test_noise_endpoint.py(8개). 이 작업 환경에 numpy·httpx가 없어 pytest를 돌리지 못함 — 반영 후 `uv run pytest -q` 필요
- 2026-11-21 | AG-T56 | DONE(측정 전) | agent/{guards,responder,config}.py, tests/test_number_confirm.py(9개). 이 작업 환경에서 pytest 미실행
- 2026-11-21 | AG-T57 | DONE(측정 전) | agent/knowledge.py RULES(59줄), agent/responder.py tts_text, tests/test_number_confirm.py(+2). 이 작업 환경에서 pytest 미실행
- 2026-11-21 | AG-T59 | DONE | agent/session.py(_tail_backchannel, 턴 로그 이벤트 tail_backchannel_ignored), tests/test_barge_in.py 2개(재생 중 200ms 소리는 턴 아님 RED→GREEN, 재생 끝난 뒤 "네"는 그대로 턴). 전체 846 passed, test_session_precall 2개 실패는 main에도 있던 것.
- 2026-11-21 | AG-T60 | DONE(점수 측정 전) | agent/knowledge.py 규칙 3곳(말하는 방식 2줄 추가, 조회 ok의 덧붙이기·조회 뒤 답 범위). tests/test_knowledge.py 17 passed. 연습 문항 응대 점수 전후 비교는 남아 있다.
- 2026-11-21 | AG-T61 | DONE | feedback_kit/static/app.js, demo/static/app.js(turn.end에서 dropAudio 해제), tests/test_feedback_kit.py 1개. 키트·데모·끼어들기 테스트 41 passed. 브라우저 실제 통화 확인은 아직.
- 2026-11-21 | PR #10 합치기 | DONE | main(a2b4ee7)과 충돌 2곳 해결(knowledge.py: 전화 요청 접수 뒤 성함·번호 확인은 main, 번호 확인 흐름은 jun / responder.py: tts_text는 jun, 010 중복 방지 정규식은 main). 조회 실패 되읽기가 두 벌(main AG-T57, jun confirm_heard_number)이라 jun 쪽이 가로채 main 테스트 3개가 깨졌다 → not_found는 AG-T57이 맡고(내역이 없다는 것까지 말함) jun의 서버 되읽기는 검사에서 막힌 경우와 AG-T57 스위치를 껐을 때만. 공식 키트 DB 번호가 든 docs/14 데모 시나리오 문서는 뺐고(키트 데이터는 커밋 금지) 테스트·주석의 키트 전화번호는 가짜 번호로 바꿨다. 전체 892 passed, test_session_precall 2개 실패는 main에도 있던 것.
