// 상담 피드백 키트 — 브라우저 마이크 ↔ 음성 상담원(대회 프로토콜) 직접 연결 + 턴별 평가 + 통화 묶음 JSON.
// 프로토콜: session.start(JSON) → 16kHz s16le mono PCM ↔ 상담원 PCM + turn.end (+ 선택: agent.transcript, debug.*)
// 빌드 도구·외부 라이브러리 없음.
"use strict";

(() => {
  const VOICE_RMS = 0.02;          // 마이크 프레임 RMS가 이보다 크면 말소리(float, 약 -34dBFS)
  const AGENT_VOICE_RMS = 300;     // 상담원 첫 유성음(int16 RMS, 대회 채점과 같은 기준)
  const JITTER_S = 0.06;           // 재생 지터 버퍼
  const ECHO_TAIL_S = 0.25;        // 상담원 소리가 끝난 뒤에도 잠깐 마이크를 막는다
  const TARGET_MS = 1500;          // 대회 속도 만점 기준
  const SCHEMA = "directon.demo_session/v1";
  const CLIENT = "feedback_kit/1.0";
  const R = window.DemoRecord;
  const TAGS = [
    ["stt", "잘못 알아들음"], ["slow", "응답 느림"], ["wrong_info", "틀린·지어낸 정보"],
    ["wrong_tool", "조회 잘못·누락"], ["repeat_ask", "같은 걸 또 물음"], ["unnatural", "말투 어색"],
    ["turn_taking", "말 끊김·끼어들기"], ["audio", "소리 문제"],
  ];

  const $ = (id) => document.getElementById(id);
  const el = {
    conn: $("conn"), phase: $("phase"), agentWs: $("agent-ws"), agentLabel: $("agent-label"),
    callerName: $("caller-name"), callerPhone: $("caller-phone"), now: $("now"), toolsUrl: $("tools-url"),
    history: $("history"), extra: $("extra"), half: $("halfduplex"),
    start: $("start"), stop: $("stop"), meter: $("meter-bar"), notice: $("notice"),
    log: $("log"), sessionId: $("session-id"), latValue: $("lat-value"), latHistory: $("lat-history"),
    fbForm: $("fb-form"), fbSend: $("fb-send"), fbDownload: $("fb-download"), fbStatus: $("fb-status"),
    fbComment: $("fb-comment"), fbReviewer: $("fb-reviewer"), fbImport: $("fb-import"),
    recBox: $("rec-box"), recInfo: $("rec-info"), recMix: $("rec-mix"), tpl: $("turn-tpl"),
  };
  const SETUP_FIELDS = ["agentWs", "agentLabel", "callerName", "callerPhone", "now", "toolsUrl", "history", "extra"];

  let config = { agent_ws: "ws://127.0.0.1:9000", tools_url: null, agent_label: null, now: null };
  let call = null;     // 진행 중(또는 방금 끝난) 통화
  let review = null;   // 평가·저장 대상: 끝난 통화 또는 불러온 JSON

  // ── 초기화 ────────────────────────────────────────
  async function init() {
    try {
      const r = await fetch("/api/config");
      if (r.ok) config = Object.assign(config, await r.json());
    } catch (e) { /* 기본값 */ }
    // 서버 인자 → 기본값, 이 브라우저에서 마지막으로 쓴 값이 있으면 그것
    el.agentWs.value = config.agent_ws || "";
    el.agentLabel.value = config.agent_label || "";
    el.toolsUrl.value = config.tools_url || "";
    el.now.value = config.now || nowText();
    const FROM_SERVER = { agentWs: "agent_ws", agentLabel: "agent_label", toolsUrl: "tools_url", now: "now" };
    for (const k of SETUP_FIELDS) {
      const v = load("kit." + k);
      const serverGave = FROM_SERVER[k] && config[FROM_SERVER[k]];   // 실행 인자로 준 값이 우선
      if (v != null && v !== "" && !serverGave) el[k].value = v;
      el[k].addEventListener("change", () => save("kit." + k, el[k].value));
    }
    el.start.addEventListener("click", startCall);
    el.stop.addEventListener("click", () => endCall("통화를 종료했습니다."));
    initFeedback();
    if (!window.isSecureContext) setNotice("마이크는 http://127.0.0.1 또는 http://localhost 주소에서만 쓸 수 있습니다.");
    setInterval(updatePhase, 100);
  }

  function load(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function save(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* 저장소 없음 */ } }
  function setNotice(text) { el.notice.textContent = text || ""; }
  function setConn(state, text) { el.conn.dataset.state = state; el.conn.textContent = text; }

  function nowText() {
    const d = new Date();
    const p = (n) => String(n).padStart(2, "0");
    const wd = "일월화수목금토"[d.getDay()];
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} (${wd}) ${p(d.getHours())}:${p(d.getMinutes())}`;
  }

  function makeSessionId() {
    const d = new Date();
    const p = (n) => String(n).padStart(2, "0");
    return `FB-${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`
      + `-${Math.random().toString(36).slice(2, 6)}`;
  }

  // 설정 → session.start. 오류면 {error}.
  function buildStart(sessionId) {
    let history = [];
    const hs = el.history.value.trim();
    if (hs) {
      try { history = JSON.parse(hs); } catch (e) { return { error: "이전 대화 JSON 형식 오류: " + e.message }; }
      if (!Array.isArray(history)) return { error: "이전 대화는 JSON 배열이어야 합니다." };
    }
    let extra = {};
    const xs = el.extra.value.trim();
    if (xs) {
      try { extra = JSON.parse(xs); } catch (e) { return { error: "추가 필드 JSON 형식 오류: " + e.message }; }
      if (!extra || typeof extra !== "object" || Array.isArray(extra)) return { error: "추가 필드는 JSON 객체여야 합니다." };
    }
    const name = el.callerName.value.trim(), phone = el.callerPhone.value.replace(/\D/g, "");
    const caller = name || phone ? { name: name || null, phone: phone || null, email: null } : null;
    // debug: true가 기본(AG-T56). 상담원이 debug.*를 지원하면 인식 텍스트·함수 호출·끼어들기가 화면에 나온다.
    // 지원하지 않는 상담원은 모르는 필드라 무시한다. 끄려면 추가 필드에 {"debug": false}.
    const msg = Object.assign({
      type: "session.start", session_id: sessionId, case_id: "FEEDBACK", now: el.now.value.trim(),
      history, caller, tools_url: el.toolsUrl.value.trim() || null,
      audio: { encoding: "pcm_s16le", sample_rate: 16000, channels: 1 },
      debug: true,
    }, extra);
    msg.type = "session.start";
    msg.session_id = sessionId;
    return { msg };
  }

  // ── 통화 시작/종료 ─────────────────────────────────
  async function startCall() {
    if (call && !call.ended) return;
    if (!confirmDiscard()) return;
    setNotice("");
    const sessionId = makeSessionId();
    const st = buildStart(sessionId);
    if (st.error) { setNotice(st.error); return; }
    const agentWs = el.agentWs.value.trim();
    if (!/^wss?:\/\//.test(agentWs)) { setNotice("상담원 주소는 ws:// 또는 wss://로 시작해야 합니다."); return; }
    el.start.disabled = true;
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
      });
    } catch (e) {
      el.start.disabled = false;
      setNotice("마이크를 쓸 수 없습니다: " + (e && e.message ? e.message : e) + " — 주소창 왼쪽 자물쇠에서 마이크를 허용하세요.");
      return;
    }
    let ctx;
    try { ctx = new AudioContext({ sampleRate: 16000 }); } catch (e) { ctx = new AudioContext(); }
    try {
      await ctx.audioWorklet.addModule("mic-worklet.js");
    } catch (e) {
      stream.getTracks().forEach((t) => t.stop());
      ctx.close();
      el.start.disabled = false;
      setNotice("오디오 처리 모듈을 불러오지 못했습니다(크롬·엣지 최신 버전을 쓰세요).");
      return;
    }
    const source = ctx.createMediaStreamSource(stream);
    const node = new AudioWorkletNode(ctx, "mic-frame-processor");
    const sink = ctx.createGain();
    sink.gain.value = 0;
    source.connect(node);
    node.connect(sink);
    sink.connect(ctx.destination);

    call = {
      ws: null, ctx, stream, node, source, sink, sessionId, start: st.msg, agentWs,
      agentLabel: el.agentLabel.value.trim() || null, halfDuplex: el.half.checked,
      open: false, ended: false, responding: false, nextTime: 0, carry: null, sources: [], dropAudio: false,
      lastVoiceAt: 0, awaitingFirstSound: true, turn: null, turns: [], bargeIns: 0, events: {},
      rec: new R.Recorder(), speech: new R.SpeechTracker(), t0: null, serverSpeechStartMs: null,
      startedAt: new Date().toISOString(), endedAt: null,
    };
    setReview(null);
    resetFeedbackForm();
    el.sessionId.textContent = sessionId;
    el.log.textContent = "";
    renderHistory(st.msg.history);
    el.latHistory.textContent = "";
    setLatency(null);
    for (const k of SETUP_FIELDS) el[k].disabled = true;
    el.half.disabled = true;
    updateSaveButtons();
    node.port.onmessage = (ev) => onMicFrame(ev.data);

    setConn("connecting", "연결 중");
    let ws;
    try { ws = new WebSocket(agentWs); } catch (e) { endCall("상담원 주소가 올바르지 않습니다: " + agentWs, true); return; }
    ws.binaryType = "arraybuffer";
    call.ws = ws;
    const thisCall = call;
    ws.onopen = () => {
      if (call !== thisCall) return;
      call.open = true;
      call.t0 = call.ctx.currentTime;
      setConn("open", "연결됨");
      el.stop.disabled = false;
      ws.send(JSON.stringify(call.start));
      const li = document.createElement("li");
      li.className = "empty";
      li.textContent = "연결됐습니다. 이제 말해 보세요.";
      el.log.appendChild(li);
    };
    ws.onmessage = (ev) => {
      if (call !== thisCall) return;
      if (typeof ev.data === "string") onText(ev.data); else onAudio(ev.data);
    };
    ws.onerror = () => {
      if (call !== thisCall || call.open) return;
      setConn("error", "연결 실패");
      endCall(`상담원(${agentWs})에 연결할 수 없습니다. 상담원 서버가 떠 있는지 확인하세요.`, true);
    };
    ws.onclose = () => {
      if (call !== thisCall) return;
      endCall(call.open ? "상담원과 연결이 끊겼습니다." : "", !call.open);
    };
  }

  function renderHistory(history) {
    if (!history || !history.length) return;
    const li = document.createElement("li");
    li.className = "review-banner";
    li.textContent = `이전 대화 ${history.length}줄을 주입했습니다(실제 통화 아님): `
      + history.map((h) => `${h.role === "agent" ? "상담원" : "고객"}: ${h.text || ""}`).join(" / ");
    el.log.appendChild(li);
  }

  function endCall(message, keepConnState) {
    const c = call;
    if (!c || c.ended) return;
    c.ended = true;
    c.endedAt = new Date().toISOString();
    try { if (c.ws && c.ws.readyState <= 1) c.ws.close(); } catch (e) { /* 무시 */ }
    try { c.node.port.onmessage = null; c.source.disconnect(); c.node.disconnect(); } catch (e) { /* 무시 */ }
    c.stream.getTracks().forEach((t) => t.stop());
    const remain = Math.max(0, c.nextTime - c.ctx.currentTime);
    setTimeout(() => { try { c.ctx.close(); } catch (e) { /* 무시 */ } }, remain * 1000 + 300);
    if (!keepConnState) setConn("idle", "연결 안 됨");
    el.start.disabled = false;
    el.stop.disabled = true;
    for (const k of SETUP_FIELDS) el[k].disabled = false;
    el.half.disabled = false;
    el.meter.style.width = "0";
    if (message) setNotice(message);
    setReview(reviewFromCall(c));
    if (c.turns.length) setFbStatus("통화가 끝났습니다. 턴별 녹음을 다시 들으며 평가하고 저장해 주세요.");
  }

  // ── 마이크 → 상담원 ────────────────────────────────
  function agentAudible(c) { return c.ctx.currentTime < c.nextTime + ECHO_TAIL_S; }
  function micMuted(c) { return c.halfDuplex && (c.responding || agentAudible(c)); }
  function micMs(c) { return c.rec.micMs(); }
  function ctxMs(c, t) { return Math.round((t - c.t0) * 1000); }

  function onMicFrame(data) {
    const c = call;
    if (!c || c.ended) return;
    const muted = micMuted(c);
    const db = data.rms > 0 ? 20 * Math.log10(data.rms) : -90;
    el.meter.style.width = Math.max(0, Math.min(100, ((db + 60) / 60) * 100)) + "%";
    el.meter.classList.toggle("muted-mic", muted);
    const voiced = !muted && data.rms >= VOICE_RMS;
    if (voiced) c.lastVoiceAt = performance.now();
    if (!c.open || !c.ws || c.ws.readyState !== 1) return;
    const out = muted ? new ArrayBuffer(640) : data.pcm;
    c.ws.send(out);
    c.speech.frame(c.rec.micSamples / (R.RATE / 1000), voiced);   // 이 프레임의 시작 위치(ms)
    c.rec.addMic(new Int16Array(out));
  }

  // ── 상담원 → 재생 ──────────────────────────────────
  function onAudio(buf) {
    const c = call;
    if (c.dropAudio) return;
    let bytes = new Uint8Array(buf);
    if (c.carry) {
      const merged = new Uint8Array(c.carry.length + bytes.length);
      merged.set(c.carry, 0);
      merged.set(bytes, c.carry.length);
      bytes = merged;
      c.carry = null;
    }
    if (bytes.length % 2) { c.carry = bytes.slice(bytes.length - 1); bytes = bytes.slice(0, bytes.length - 1); }
    const n = bytes.length / 2;
    if (!n) return;
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.length);
    const f32 = new Float32Array(n), i16 = new Int16Array(n);
    let sq = 0;
    for (let i = 0; i < n; i++) {
      const s = view.getInt16(i * 2, true);
      sq += s * s;
      i16[i] = s;
      f32[i] = s / 32768;
    }
    const t = currentTurn();
    const startAt = Math.max(c.nextTime, c.ctx.currentTime + JITTER_S);
    if (c.awaitingFirstSound && Math.sqrt(sq / n) >= AGENT_VOICE_RMS) {
      c.awaitingFirstSound = false;
      t.clientLatencyMs = c.lastVoiceAt ? Math.round(performance.now() - c.lastVoiceAt) : null;
      if (t.ts.first_sound_ms == null) t.ts.first_sound_ms = ctxMs(c, startAt);
      renderTurn(t);
      setLatency(t.clientLatencyMs);
    }
    if (c.t0 != null) c.rec.addAgent((startAt - c.t0) * R.RATE, i16);
    const ab = c.ctx.createBuffer(1, n, 16000);
    ab.copyToChannel(f32, 0);
    const src = c.ctx.createBufferSource();
    src.buffer = ab;
    src.connect(c.ctx.destination);
    src.start(startAt);
    c.nextTime = startAt + ab.duration;
    c.sources.push(src);
    src.onended = () => { const i = c.sources.indexOf(src); if (i >= 0) c.sources.splice(i, 1); };
  }

  function stopPlayback(c) {
    for (const s of c.sources.splice(0)) { try { s.onended = null; s.stop(); } catch (e) { /* 끝남 */ } }
    if (c.t0 != null) c.rec.cut((c.ctx.currentTime - c.t0) * R.RATE);
    c.nextTime = c.ctx.currentTime;
  }

  // 지금 응답 중인 턴. 없으면 새로 연다: 상담원이 반응한 순간을 말 끝 판정으로 보고,
  // 그 전까지 브라우저가 잰 말 구간을 고객 말로 붙인다(서버 debug.turn이 오면 덮어씀).
  function currentTurn() {
    const c = call;
    if (c.turn) return c.turn;
    const empty = el.log.querySelector(".empty");
    if (empty) empty.remove();
    const t = newTurn(c.turns.length + 1);
    const sp = c.speech.take();
    if (sp) {
      const now = micMs(c);
      t.ts.speech_start_ms = sp.speech_start_ms;
      t.ts.decided_ms = now;
      t.decided = { source: "client", silence_ms: Math.max(0, now - sp.speech_end_ms),
                    speech_ms: sp.speech_end_ms - sp.speech_start_ms };
    }
    c.responding = true;
    mountTurn(t);
    c.turns.push(t);
    c.turn = t;
    renderTurn(t);
    return t;
  }

  // ── 상담원 텍스트 이벤트 ────────────────────────────
  function onText(raw) {
    let msg;
    try { msg = JSON.parse(raw); } catch (e) { return; }
    const c = call;
    c.events[msg.type] = (c.events[msg.type] || 0) + 1;
    switch (msg.type) {
      case "debug.turn":
        if (msg.event === "speech_start") {
          c.awaitingFirstSound = true;
          if (c.serverSpeechStartMs == null) c.serverSpeechStartMs = micMs(c);
        } else if (msg.event === "decided") {
          c.dropAudio = false;
          const t = currentTurn();
          t.decided = { source: "server", reason: msg.reason, silence_ms: msg.silence_ms, speech_ms: msg.speech_ms,
                        text: msg.text };
          t.ts.decided_ms = micMs(c);
          if (c.serverSpeechStartMs != null) t.ts.speech_start_ms = c.serverSpeechStartMs;
          c.serverSpeechStartMs = null;
          renderTurn(t);
        }
        break;
      case "debug.stt": {
        const t = currentTurn();
        t.user = msg.text || "";
        t.userFinal = true;
        renderTurn(t);
        break;
      }
      case "debug.tool": {
        const t = currentTurn();
        t.tools.push({ name: msg.name, arguments: msg.arguments, status: msg.status, matched: null });
        renderTurn(t);
        break;
      }
      case "agent.transcript": {
        const t = currentTurn();
        t.agent = msg.text || "";
        renderTurn(t);
        break;
      }
      case "debug.barge_in": {
        stopPlayback(c);
        c.bargeIns += 1;
        if (!msg.late) c.dropAudio = true;
        const t = c.turn || c.turns[c.turns.length - 1];
        if (t) { t.interrupted = { late: !!msg.late, heard_ms: msg.heard_ms, voiced_ms: msg.voiced_ms }; renderTurn(t); }
        break;
      }
      case "debug.timing": {
        const t = c.turn || c.turns[c.turns.length - 1];
        if (t) { t.timing = msg; renderTurn(t); }
        break;
      }
      case "turn.end": {
        // AG-T61: 끼어든 턴은 여기서 끝났다. 이 뒤의 오디오는 다음 턴 것이라 버리지 않는다(다음 턴 첫마디는 decided보다 먼저 온다).
        c.dropAudio = false;
        const t = currentTurn();
        t.ended = true;
        t.ts.turn_end_ms = micMs(c);
        if (c.t0 != null) t.ts.agent_audio_end_ms = ctxMs(c, c.nextTime);
        c.responding = false;
        c.awaitingFirstSound = true;
        c.turn = null;
        c.speech.take();              // 응답 중 들어온 소리(에코 등)는 다음 턴 말로 치지 않는다
        renderTurn(t);
        addLatencyRow(t);
        break;
      }
      default:
        break;
    }
  }

  // ── 턴 렌더·평가 입력 ───────────────────────────────
  function newTurn(index) {
    return { index, user: "", userFinal: false, agent: "", tools: [], decided: null, timing: null,
             clientLatencyMs: null, ended: false, interrupted: null,
             ts: { speech_start_ms: null, decided_ms: null, first_sound_ms: null, turn_end_ms: null, agent_audio_end_ms: null },
             audio: null, vote: null, tags: [], note: "", corrected: "", node: null };
  }

  function mountTurn(t) {
    const node = el.tpl.content.firstElementChild.cloneNode(true);
    t.node = node;
    const votes = node.querySelectorAll(".vote");
    const syncVotes = () => votes.forEach((x) => x.setAttribute("aria-pressed", String(x.dataset.vote === t.vote)));
    votes.forEach((b) => b.addEventListener("click", () => {
      t.vote = t.vote === b.dataset.vote ? null : b.dataset.vote;
      syncVotes();
      markDirty();
    }));
    syncVotes();
    const tagBox = node.querySelector(".tags");
    for (const [key, label] of TAGS) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "tag";
      b.textContent = label;
      b.setAttribute("aria-pressed", String(t.tags.includes(key)));
      b.addEventListener("click", () => {
        const i = t.tags.indexOf(key);
        if (i >= 0) t.tags.splice(i, 1); else t.tags.push(key);
        b.setAttribute("aria-pressed", String(i < 0));
        markDirty();
      });
      tagBox.appendChild(b);
    }
    const corrected = node.querySelector(".corrected");
    corrected.value = t.corrected;
    corrected.classList.toggle("filled", !!t.corrected.trim());
    corrected.addEventListener("input", () => {
      t.corrected = corrected.value;
      corrected.classList.toggle("filled", !!t.corrected.trim());
      markDirty();
    });
    const note = node.querySelector(".note");
    note.value = t.note;
    note.addEventListener("input", () => { t.note = note.value; markDirty(); });
    el.log.appendChild(node);
  }

  function setText(p, text, placeholder) {
    p.textContent = text || placeholder;
    p.classList.toggle("placeholder", !text);
  }

  function renderTurn(t) {
    const n = t.node;
    const userP = n.querySelector(".bubble.user .text");
    if (t.userFinal) setText(userP, t.user, "(인식 결과 없음)");
    else if (t.decided && t.decided.text) setText(userP, t.decided.text + " …", "");
    else setText(userP, "", t.ts.speech_start_ms == null ? "(내 말 없이 상담원이 먼저 말함)"
                                                            : "(상담원이 인식 텍스트를 보내지 않음 — 녹음을 듣고 아래에 실제로 한 말을 적어 주세요)");
    const meta = [];
    if (t.decided) {
      const src = t.decided.source === "server" ? "말 끝(상담원 판정)" : "말 끝(브라우저 추정)";
      const parts = [src];
      if (t.decided.silence_ms != null) {
        parts.push(t.decided.source === "server" ? `침묵 ${t.decided.silence_ms}ms` : `말 끝 → 상담원 반응 ${t.decided.silence_ms}ms`);
      }
      if (t.decided.speech_ms != null) parts.push(`발화 ${(t.decided.speech_ms / 1000).toFixed(1)}초`);
      meta.push(parts.join(" · "));
    }
    if (t.clientLatencyMs != null) meta.push(`첫 소리 ${t.clientLatencyMs}ms`);
    if (t.interrupted) meta.push(t.interrupted.late ? "응답 재생 중 끼어들기" : "끼어들기로 중단");
    n.querySelector(".meta").textContent = meta.join("  |  ");
    const ul = n.querySelector(".tools");
    ul.textContent = "";
    for (const tool of t.tools) {
      const li = document.createElement("li");
      li.textContent = `함수 ${tool.name}(${tool.arguments == null ? "" : JSON.stringify(tool.arguments)})`
        + (tool.status ? ` → ${tool.status}` : "");
      ul.appendChild(li);
    }
    setText(n.querySelector(".bubble.agent .text"), t.agent,
            t.ended ? "(상담원 텍스트 없음 — 녹음으로 확인)" : "…");
  }

  function grade(ms) {
    if (ms == null) return "none";
    if (ms <= TARGET_MS) return "good";
    if (ms <= 2500) return "warn";
    return "bad";
  }

  function setLatency(ms) {
    el.latValue.textContent = ms == null ? "—" : (ms / 1000).toFixed(2) + "초";
    el.latValue.dataset.grade = grade(ms);
  }

  function addLatencyRow(t) {
    const li = document.createElement("li");
    li.className = grade(t.clientLatencyMs);
    li.textContent = t.clientLatencyMs == null ? "첫 소리 측정 안 됨" : `첫 소리 ${(t.clientLatencyMs / 1000).toFixed(2)}초`;
    el.latHistory.appendChild(li);
  }

  function updatePhase() {
    const c = call;
    let phase = "idle", label = "대기";
    if (c && !c.ended && c.open) {
      label = "듣는 중";
      if (agentAudible(c) && c.nextTime > c.ctx.currentTime) { phase = "agent"; label = "상담원이 말하는 중"; }
      else if (c.responding) { phase = "thinking"; label = "상담원이 답을 준비하는 중"; }
      else if (performance.now() - c.lastVoiceAt < 300) { phase = "user"; label = "지금 말하는 중"; }
    }
    el.phase.dataset.phase = phase;
    el.phase.textContent = label;
  }

  // ── 평가·저장·내려받기·불러오기 ─────────────────────
  function initFeedback() {
    el.fbForm.addEventListener("submit", saveToServer);
    el.fbDownload.addEventListener("click", downloadBundle);
    el.fbImport.addEventListener("change", () => {
      const f = el.fbImport.files && el.fbImport.files[0];
      el.fbImport.value = "";
      if (f) importBundle(f);
    });
    el.fbForm.querySelectorAll("input[name=rating]").forEach((x) => x.addEventListener("change", markDirty));
    el.fbComment.addEventListener("input", markDirty);
    el.fbReviewer.value = load("kit.reviewer") || "";
    el.fbReviewer.addEventListener("input", () => { save("kit.reviewer", el.fbReviewer.value.trim()); markDirty(); });
    window.addEventListener("beforeunload", (e) => { if (hasUnsaved()) { e.preventDefault(); e.returnValue = ""; } });
    updateSaveButtons();
  }

  function hasUnsaved() { return !!(review && !review.saved && review.turns.length); }
  function markDirty() { if (review) review.saved = false; }
  function confirmDiscard() { return !hasUnsaved() || window.confirm("저장하지 않은 평가가 있습니다. 버리고 계속할까요?"); }

  function setFbStatus(text, kind) {
    el.fbStatus.textContent = text || "";
    if (kind) el.fbStatus.dataset.kind = kind; else delete el.fbStatus.dataset.kind;
  }

  function resetFeedbackForm() {
    el.fbForm.querySelectorAll("input[name=rating]").forEach((x) => { x.checked = false; });
    el.fbComment.value = "";
    setFbStatus("");
  }

  function updateSaveButtons() {
    const live = !!(call && !call.ended);
    const can = !!(review && review.turns.length) && !live;
    el.fbSend.disabled = !can || !!(review && review.audioMissing);
    el.fbDownload.disabled = !can;
    el.fbImport.disabled = live;
  }

  function reviewFromCall(c) {
    const hasAudio = c.rec.micSamples > 0 || c.rec.agent.length > 0;
    const tracks = hasAudio ? { customer: c.rec.micTrack(), agent: c.rec.agentTrack() } : null;
    const durationMs = hasAudio ? c.rec.durationMs() : 0;
    const clips = R.turnClips(c.turns, durationMs);
    c.turns.forEach((t, i) => { t.audio = tracks ? clips[i] : null; });
    const debugEvents = Object.keys(c.events).filter((k) => k.startsWith("debug."));
    return {
      sessionId: c.sessionId, startedAt: c.startedAt, endedAt: c.endedAt,
      setup: {
        caller: c.start.caller, now: c.start.now, history: c.start.history, history_preset: null,
        agent_ws: c.agentWs, agent_label: c.agentLabel, half_duplex: c.halfDuplex, barge_ins: c.bargeIns,
        session_start: c.start, received_events: c.events,
        turn_source: debugEvents.includes("debug.turn") ? "server" : "client",
        client: CLIENT, user_agent: navigator.userAgent,
      },
      turns: c.turns, tracks, durationMs, saved: false, audioMissing: false, urls: [],
    };
  }

  function setReview(r) {
    if (review) review.urls.forEach((u) => URL.revokeObjectURL(u));
    review = r;
    el.recMix.removeAttribute("src");
    el.recBox.hidden = true;
    if (r && r.tracks) {
      const url = (bytes) => {
        const u = URL.createObjectURL(new Blob([bytes], { type: "audio/wav" }));
        r.urls.push(u);
        return u;
      };
      for (const t of r.turns) {
        const box = t.node.querySelector(".turn-audio");
        box.textContent = "";
        const add = (label, track, spec) => {
          if (!spec) return;
          const s = document.createElement("span");
          s.className = "who-label";
          s.textContent = label;
          const a = document.createElement("audio");
          a.controls = true;
          a.preload = "none";
          a.src = url(R.wavBytes(R.sliceMs(track, spec.start_ms, spec.end_ms), 1));
          a.setAttribute("aria-label", `${t.index}번째 턴 ${label} 녹음`);
          box.append(s, a);
        };
        if (t.audio) {
          add("내 말", r.tracks.customer, t.audio.customer);
          add("상담원", r.tracks.agent, t.audio.agent);
        }
        box.hidden = box.childElementCount === 0;
      }
      el.recMix.src = url(R.wavBytes(R.interleave(r.tracks.customer, r.tracks.agent), 2));
      el.recInfo.textContent = `${fmtDuration(r.durationMs)} · 턴 ${r.turns.length}개`;
      el.recBox.hidden = false;
    }
    updateSaveButtons();
  }

  function fmtDuration(ms) {
    const s = Math.round(ms / 1000);
    return s >= 60 ? `${Math.floor(s / 60)}분 ${s % 60}초` : `${s}초`;
  }

  function stripFile(spec) {
    if (!spec) return null;
    const o = Object.assign({}, spec);
    delete o.file;
    return o;
  }

  function buildBundle(r) {
    const ratingEl = el.fbForm.querySelector("input[name=rating]:checked");
    const b = {
      schema: SCHEMA, session_id: r.sessionId, started_at: r.startedAt, ended_at: r.endedAt, setup: r.setup,
      feedback: {
        rating: ratingEl ? Number(ratingEl.value) : null, comment: el.fbComment.value.trim(),
        reviewer: el.fbReviewer.value.trim() || null, updated_at: new Date().toISOString(),
      },
      turns: r.turns.map((t) => ({
        index: t.index, user_stt: t.user, user_corrected: t.corrected.trim(), agent: t.agent, decided: t.decided,
        tools: t.tools.map((x) => ({ name: x.name, arguments: x.arguments, status: x.status,
                                     matched: x.matched === undefined ? null : x.matched })),
        client_latency_ms: t.clientLatencyMs, timing: t.timing, interrupted: t.interrupted || null, ts: t.ts,
        audio: t.audio ? { customer: stripFile(t.audio.customer), agent: stripFile(t.audio.agent) }
                       : { customer: null, agent: null },
        feedback: { vote: t.vote, tags: t.tags.slice(), note: t.note.trim() },
      })),
    };
    if (r.tracks) {
      b.audio = {
        encoding: "pcm_s16le", sample_rate: R.RATE, channels: 1,
        tracks: {
          customer: { wav_base64: R.toBase64(R.wavBytes(r.tracks.customer, 1)) },
          agent: { wav_base64: R.toBase64(R.wavBytes(r.tracks.agent, 1)) },
        },
      };
    }
    return b;
  }

  async function saveToServer(ev) {
    ev.preventDefault();
    const r = review;
    if (!r || !r.turns.length) return;
    el.fbSend.disabled = el.fbDownload.disabled = true;
    setFbStatus("저장 중…");
    try {
      const res = await fetch("/api/sessions", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(buildBundle(r)),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      if (review === r) r.saved = true;
      setFbStatus(`저장됨: ${data.dir} (session.json·오디오·cases.jsonl 등 ${data.files.length}개)`, "ok");
    } catch (e) {
      setFbStatus("저장 실패: " + (e && e.message ? e.message : e) + " — JSON 내려받기로 보관할 수 있습니다.", "err");
    } finally {
      updateSaveButtons();
    }
  }

  function downloadBundle() {
    const r = review;
    if (!r) return;
    const blob = new Blob([JSON.stringify(buildBundle(r))], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${r.sessionId}.json`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 30000);
    r.saved = true;
    setFbStatus(`내려받음: ${a.download} (${(blob.size / 1e6).toFixed(1)}MB). 폴더로 풀기: python -m feedback_kit.store ${a.download}`, "ok");
  }

  async function importBundle(file) {
    if (call && !call.ended) return;
    if (!confirmDiscard()) return;
    let b;
    try {
      b = JSON.parse(await file.text());
      if (!b || b.schema !== SCHEMA) throw new Error(`통화 묶음 JSON이 아닙니다(schema ${SCHEMA} 필요)`);
      if (!Array.isArray(b.turns)) throw new Error("turns가 없습니다");
    } catch (e) {
      setFbStatus("불러오기 실패: " + (e && e.message ? e.message : e), "err");
      return;
    }
    let tracks = null, durationMs = 0;
    const tr = b.audio && b.audio.tracks;
    try {
      if (tr && tr.customer && tr.customer.wav_base64 && tr.agent && tr.agent.wav_base64) {
        tracks = { customer: R.wavToInt16(R.fromBase64(tr.customer.wav_base64)),
                   agent: R.wavToInt16(R.fromBase64(tr.agent.wav_base64)) };
        durationMs = Math.ceil(Math.max(tracks.customer.length, tracks.agent.length) / (R.RATE / 1000));
      }
    } catch (e) {
      setFbStatus("오디오를 읽지 못했습니다: " + e.message, "err");
      return;
    }
    setReview(null);
    el.log.textContent = "";
    el.latHistory.textContent = "";
    setLatency(null);
    const setup = b.setup || {};
    const banner = document.createElement("li");
    banner.className = "review-banner";
    banner.textContent = `불러온 통화 ${file.name} · 상담원 ${setup.agent_label || "?"} · 시작 ${b.started_at || "?"}`
      + (b.feedback && b.feedback.reviewer ? ` · 이전 평가자 ${b.feedback.reviewer}` : "");
    el.log.appendChild(banner);
    renderHistory(setup.history);
    const turns = b.turns.map((x, i) => {
      const fb = x.feedback || {};
      const t = newTurn(x.index || i + 1);
      Object.assign(t, {
        user: x.user_stt || "", userFinal: !!x.user_stt, agent: x.agent || "",
        tools: Array.isArray(x.tools) ? x.tools : [], decided: x.decided || null, timing: x.timing || null,
        clientLatencyMs: x.client_latency_ms == null ? null : x.client_latency_ms,
        interrupted: x.interrupted || null, ended: true, ts: Object.assign(t.ts, x.ts || {}), audio: x.audio || null,
        vote: fb.vote === "up" || fb.vote === "down" ? fb.vote : null,
        tags: Array.isArray(fb.tags) ? fb.tags.filter((k) => typeof k === "string") : [],
        note: fb.note || "", corrected: x.user_corrected || "",
      });
      mountTurn(t);
      renderTurn(t);
      addLatencyRow(t);
      return t;
    });
    el.sessionId.textContent = `${b.session_id} (불러옴)`;
    resetFeedbackForm();
    const fb = b.feedback || {};
    const star = fb.rating ? el.fbForm.querySelector(`input[name=rating][value="${Number(fb.rating)}"]`) : null;
    if (star) star.checked = true;
    el.fbComment.value = fb.comment || "";
    setReview({ sessionId: b.session_id, startedAt: b.started_at || null, endedAt: b.ended_at || null, setup,
                turns, tracks, durationMs, saved: true, audioMissing: !tracks, urls: [] });
    setFbStatus(tracks ? "불러왔습니다. 평가를 고친 뒤 저장하면 같은 session_id 폴더를 덮어씁니다."
                       : "오디오가 없는 JSON입니다. 서버 저장은 막고(기존 녹음 보호) 내려받기만 됩니다.");
  }

  init();
})();
