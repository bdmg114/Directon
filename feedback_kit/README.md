# 상담 피드백 키트 (feedback_kit)

내 음성 상담원과 브라우저로 직접 통화해 보고, 턴마다 평가(👍/👎, 문제 유형, 실제로 한 말, 메모)를 남겨
**대화·평가·녹음을 JSON 한 개**로 저장하는 도구입니다. 팀원끼리 같은 형식으로 피드백을 모아 상담원을 비교하려고 만들었습니다.

- 이 폴더만 복사하면 됩니다. 표준 라이브러리만 쓰고(설치할 패키지 없음) 상담원 코드를 import하지 않습니다.
- 상담원은 대회 프로토콜(`session.start` → PCM ↔ PCM + `turn.end`)만 지키면 됩니다. 코드를 고칠 필요가 없습니다.

## 준비물

- Python 3.9 이상
- 크롬 또는 엣지 최신 버전, 마이크(이어폰 권장: 스피커 소리가 마이크로 다시 들어가지 않게)
- 내 상담원 서버가 떠 있을 것(예: `ws://127.0.0.1:9000`). Mock API를 쓰는 상담원이면 Mock도 띄워 둡니다.

## 붙이기

1. `feedback_kit/` 폴더를 내 레포 맨 위에 복사합니다.
2. 결과 폴더는 git에서 빼 둡니다. `.gitignore`에 한 줄 추가:
   ```
   feedback_sessions/
   ```

## 실행

레포 맨 위(=`feedback_kit/`이 있는 폴더)에서:

```
python -m feedback_kit --agent-ws ws://127.0.0.1:9000 --agent-label "팀원A v3"
```

브라우저에서 `http://127.0.0.1:8090` 을 엽니다. `127.0.0.1`이나 `localhost` 주소로 열어야 마이크를 쓸 수 있습니다.

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `--agent-ws` | `ws://127.0.0.1:9000` | 상담원 WebSocket 주소. 페이지에서도 바꿀 수 있음 |
| `--agent-label` | 없음 | 상담원 이름·버전. 모든 기록에 남아 비교할 때 씀 (예: `"팀원A v3 1a2b3c"`) |
| `--tools-url` | 없음 | `session.start`의 `tools_url` (Mock API 주소, 예: `http://127.0.0.1:8100`) |
| `--now` | 지금 시각 | `session.start`의 `now` 기본값 (예: `"2026-11-25 (수) 10:00"`) |
| `--port` | `8090` | 페이지 포트 |
| `--out` | `./feedback_sessions` | 저장 폴더 |

## 쓰는 법

1. **통화 설정**: 상담원 주소·이름, 고객(발신자) 이름·전화(비우면 `caller: null`), 현재 시각, tools_url.
   고급 칸에서 이전 대화(`history`)와 `session.start`에 더할 필드를 넣을 수 있습니다.
2. **통화 시작** → 평소처럼 말합니다. 기본은 반이중(상담원이 말하는 동안 내 마이크를 막음)입니다.
3. **통화 종료** → 턴마다 "내 말 / 상담원" 녹음이 붙습니다. 다시 들으며:
   - 👍/👎
   - 문제 유형(여러 개): 잘못 알아들음 `stt`, 응답 느림 `slow`, 틀린·지어낸 정보 `wrong_info`, 조회 잘못·누락 `wrong_tool`,
     같은 걸 또 물음 `repeat_ask`, 말투 어색 `unnatural`, 말 끊김·끼어들기 `turn_taking`, 소리 문제 `audio`
   - **내가 실제로 한 말**: 상담원이 인식 텍스트를 보내지 않으면 "나" 칸이 비어 있습니다. 여기 적은 문장이 테스트 문항의 정답 발화가 됩니다.
   - 메모
4. 통화 전체 점수(1~5)·의견·평가자 → **서버에 저장** 또는 **JSON 내려받기**.
5. 받은 JSON은 **저장한 JSON 불러오기**로 다시 열어 듣고 평가를 더할 수 있습니다.

## 결과물

**서버에 저장**하면:

```
feedback_sessions/
  index.jsonl                저장할 때마다 요약 한 줄(점수, 턴 수, 👍/👎 수, 길이)
  FB-20261125-100000-ab12/
    session.json             설정(상담원 이름, 실제로 보낸 session.start)·턴(텍스트, 시각, 평가)·파일 경로
    customer.wav             상담원에게 실제로 보낸 내 목소리(반이중으로 막힌 구간은 무음), 16kHz mono
    agent.wav                내가 실제로 들은 상담원 목소리
    mix.wav                  스테레오(왼쪽 나, 오른쪽 상담원)
    turns/01_customer.wav …  턴별 구간
    cases.jsonl              턴마다 테스트셋 모양 문항 한 줄(now, history, customer_utterance,
                             customer_profile, recording{file, speech_end, duration}, agent_reply, feedback)
```

**피드백을 모으는 사람에게 보낼 때**는 **JSON 내려받기** 파일(`<session_id>.json`, 오디오 포함, 10분 통화 약 55MB)만 보내면 됩니다.
받은 쪽에서 폴더로 풀기:

```
python -m feedback_kit.store 받은파일.json [다른파일.json ...] [--out 폴더]
```

## 턴은 어떻게 나누나

- **기본(대회 프로토콜만 쓰는 상담원)**: 브라우저가 내 마이크 음량으로 말 구간을 잽니다. 상담원 소리(또는 `agent.transcript`)가 처음 온 순간을
  "말 끝 판정"으로 보고, 그 전까지 잰 말 구간을 그 턴의 고객 말로 붙입니다. `turn.end`로 턴을 닫습니다.
  상담원이 먼저 말한 턴(인사 등)은 고객 말이 없는 턴으로 남습니다.
- **선택 확장**: 상담원이 아래 텍스트 이벤트를 보내면 그것을 씁니다. 하나도 안 보내도 됩니다.

| 이벤트 | 필드 | 쓰임 |
|---|---|---|
| `agent.transcript` | `text` | 상담원이 한 말(대화 기록·`cases.jsonl`의 agent_reply) |
| `debug.turn` | `event`: `"speech_start"` \| `"decided"`, `silence_ms`, `speech_ms`, `reason`, `text` | 상담원이 직접 판정한 말 시작·말 끝 |
| `debug.stt` | `text` | 최종 인식 텍스트("나" 칸) |
| `debug.tool` | `name`, `arguments`, `status` | 함수 호출 표시 |
| `debug.barge_in` | `late`, `heard_ms`, `voiced_ms` | 끼어들기: 페이지가 재생을 즉시 멈춤 |
| `debug.timing` | 아무 키 | 그대로 `session.json`에 보관 |

  `debug.*`를 채점 때는 끄고 싶다면, `session.start`에 플래그가 있을 때만 보내게 만드세요.
  이 페이지는 `session.start`에 `"debug": true`를 **기본으로** 붙여 보냅니다. 그래야 턴마다 "내 말이 어떻게 인식됐는지",
  "어떤 번호로 무엇을 조회했는지"가 보여서 받아쓰기 문제와 응답 문제를 가를 수 있습니다.
  끄려면 "session.start에 더할 필드"에 `{"debug": false}`를 적습니다. 이 칸에 적은 값이 기본값보다 우선합니다.
  (상담원은 모르는 필드를 무시해야 합니다.)
- **끼어들기**: 상담원이 말하는 중에 끊고 말하려면 "상담원이 말하는 동안 내 마이크 막기(반이중)"를 풀어야 합니다.
  풀면 스피커 소리가 마이크로 들어가 상담원이 제 목소리에 끊길 수 있으니 이어폰을 씁니다.

## 알아 둘 것

- 시각(ms)은 모두 연결 순간부터 잽니다. 판정 시각은 보낸 오디오 양 기준, 상담원 소리 위치는 재생 시계 기준이라 수십 ms 어긋날 수 있습니다.
- 브라우저 추정 말 끝은 "상담원이 반응한 순간" 기준이라, 화면의 "말 끝 → 상담원 반응" 값은 상담원의 판정 대기 + 처리 시간을 합친 값입니다.
  "첫 소리"는 내 목소리가 마지막으로 컸던 시점부터 상담원 첫 유성음까지입니다. 대회 채점과는 수십~수백 ms 차이가 날 수 있습니다.
- 반이중을 끄면 스피커 소리가 내 말로 잡힐 수 있습니다. 끼어들기를 시험할 때만 끄고 이어폰을 쓰세요.
- 녹음에는 목소리가 들어 있습니다. 결과 폴더·JSON은 git에 넣지 말고 공유 범위에 주의하세요.

## 형식 (`directon.demo_session/v1`)

```
{ "schema": "directon.demo_session/v1", "session_id", "started_at", "ended_at",
  "setup":    { "caller", "now", "history", "agent_ws", "agent_label", "session_start", "turn_source", ... },
  "feedback": { "rating", "comment", "reviewer", "updated_at" },
  "turns": [ { "index", "user_stt", "user_corrected", "agent", "tools", "decided",
               "ts": { "speech_start_ms", "decided_ms", "first_sound_ms", "turn_end_ms", "agent_audio_end_ms" },
               "audio": { "customer": {"start_ms","end_ms","speech_end_ms"}, "agent": {"start_ms","end_ms"} },
               "feedback": { "vote", "tags", "note" } } ],
  "audio": { "encoding": "pcm_s16le", "sample_rate": 16000, "channels": 1,
             "tracks": { "customer": {"wav_base64"}, "agent": {"wav_base64"} } } }
```

Directon 레포의 체험 데모(`demo/`)도 같은 형식을 쓰므로, 두 쪽 파일을 섞어 모아도 됩니다.
