// 다이렉트몰 상담 체험 — 브라우저 마이크 ↔ 상담원 서버(측정기 프로토콜) 직접 연결.
// 빌드 도구·외부 라이브러리 없음.
"use strict";

(() => {
  const VOICE_RMS = 0.02;          // 마이크 프레임 RMS가 이보다 크면 "말하는 중"(float, -34dBFS 정도)
  const AGENT_VOICE_RMS = 300;     // 상담원 첫 유성음 판정(대회 채점과 같은 int16 RMS 기준)
  const JITTER_S = 0.06;           // 재생 지터 버퍼
  const ECHO_TAIL_S = 0.25;        // 상담원 소리가 끝난 뒤에도 잠깐 마이크를 막는다(잔향)
  const TARGET_MS = 1500;          // 대회 속도 만점 기준
  const SCHEMA = "directon.demo_session/v1";   // 통화 묶음 JSON(AG-T52, demo/feedback_store.py와 같은 형식)
  const R = window.DemoRecord;
  // 턴별 문제 유형 태그(값은 분석용 고정 키)
  const TAGS = [
    ["stt", "잘못 알아들음"], ["slow", "응답 느림"], ["wrong_info", "틀린·지어낸 정보"],
    ["wrong_tool", "조회 잘못·누락"], ["repeat_ask", "같은 걸 또 물음"], ["unnatural", "말투 어색"],
    ["turn_taking", "말 끊김·끼어들기"], ["audio", "소리 문제"],
  ];

  const $ = (id) => document.getElementById(id);
  const el = {
    conn: $("conn"), phase: $("phase"), caller: $("caller"), now: $("now"), half: $("halfduplex"),
    adaptive: $("adaptive-opener"),
    start: $("start"), stop: $("stop"), meter: $("meter-bar"), notice: $("notice"),
    orders: $("orders"), orderList: $("order-list"), memberPhone: $("member-phone"),
    log: $("log"), sessionId: $("session-id"), latValue: $("lat-value"), latServer: $("lat-server"),
    latHistory: $("lat-history"), fbForm: $("fb-form"), fbSend: $("fb-send"), fbStatus: $("fb-status"),
    fbComment: $("fb-comment"), tpl: $("turn-tpl"),
    fbDownload: $("fb-download"), fbImport: $("fb-import"), fbReviewer: $("fb-reviewer"),
    recBox: $("rec-box"), recInfo: $("rec-info"), recMix: $("rec-mix"),
    hPanel: $("history-panel"), hPreset: $("history-preset"), hDesc: $("history-desc"), hTurns: $("history-turns"),
    hJson: $("history-json"), hAdd: $("history-add"), hJsonToggle: $("history-json-toggle"),
    hClear: $("history-clear"), hWarn: $("history-warn"),
  };
  const ROLES = { agent: "상담원", customer: "고객" };

  let config = { agent_ws: "ws://127.0.0.1:9000", mock_url: "http://127.0.0.1:8100", default_now: "2026-11-20 14:00" };
  let members = [];
  let presets = [];
  // 이전 대화(history) 편집 상태: [{role:"agent"|"customer", text}] / JSON 보기 여부
  let historyTurns = [];
  let jsonMode = false;

  // 통화 상태
  let call = null;  // {ws, ctx, stream, node, source, sessionId, ...}
  // 평가·저장 대상(끝난 통화 또는 불러온 JSON): {sessionId, setup, turns, tracks, durationMs, saved, urls, ...}
  let review = null;

  // ── 초기화 ────────────────────────────────────────
  async function init() {
    try {
      const r = await fetch("/api/config");
      if (r.ok) config = Object.assign(config, await r.json());
    } catch (e) { /* 기본값 */ }
    if (config.default_now) el.now.value = config.default_now.replace(" ", "T");
    try {
      const r = await fetch("/api/members");
      members = r.ok ? await r.json() : [];
    } catch (e) {
      members = [];
      setNotice("회원 목록을 불러오지 못했습니다(데모 서버 확인).");
    }
    for (const m of members) {
      const o = document.createElement("option");
      o.value = m.phone;
      o.textContent = `${m.name} · ${m.phone} · ${m.grade} (주문 ${m.orders.length}건)`;
      el.caller.appendChild(o);
    }
    el.caller.addEventListener("change", renderOrders);
    await initHistory();
    el.start.addEventListener("click", startCall);
    el.stop.addEventListener("click", () => endCall("사용자가 통화를 종료했습니다."));
    initFeedback();
    if (!window.isSecureContext) {
      setNotice("마이크는 http://127.0.0.1 또는 http://localhost 주소에서만 쓸 수 있습니다.");
    }
    setInterval(updatePhase, 100);
    renderOrders();
  }

  // ── 이전 대화(history) 주입 (AG-T14b) ─────────────
  async function initHistory() {
    try {
      const r = await fetch("/api/history_presets");
      presets = r.ok ? await r.json() : [];
    } catch (e) { presets = []; }
    if (!Array.isArray(presets)) presets = [];
    for (const p of presets) {
      const o = document.createElement("option");
      o.value = p.id;
      o.textContent = p.name + (p.caller_name ? ` · ${p.caller_name}` : " · 비회원");
      el.hPreset.appendChild(o);
    }
    el.hPreset.addEventListener("change", () => applyPreset(el.hPreset.value));
    el.hAdd.addEventListener("click", () => {
      if (!leaveJsonMode()) return;
      const last = historyTurns[historyTurns.length - 1];
      historyTurns.push({ role: last && last.role === "customer" ? "agent" : "customer", text: "" });
      renderHistoryTurns();
      const boxes = el.hTurns.querySelectorAll("textarea");
      if (boxes.length) boxes[boxes.length - 1].focus();
    });
    el.hClear.addEventListener("click", () => {
      historyTurns = [];
      el.hPreset.value = "";
      if (jsonMode) el.hJson.value = "[]";
      renderPresetDesc(null);
      renderHistoryTurns();
    });
    el.hJsonToggle.addEventListener("click", () => {
      if (jsonMode) { leaveJsonMode(); return; }
      jsonMode = true;
      el.hJson.value = JSON.stringify(historyTurns, null, 2);
      el.hJson.hidden = false;
      el.hTurns.hidden = true;
      el.hAdd.disabled = true;
      el.hJsonToggle.textContent = "목록으로 보기";
      el.hJsonToggle.setAttribute("aria-pressed", "true");
      updateHistoryWarn();
    });
    el.hJson.addEventListener("input", updateHistoryWarn);
    renderHistoryTurns();
  }

  // JSON 보기 → 목록. 파싱에 실패하면 JSON 보기에 머문다.
  function leaveJsonMode() {
    if (!jsonMode) return true;
    const res = parseHistory(el.hJson.value);
    if (res.error) { el.hWarn.textContent = res.error; return false; }
    historyTurns = res.turns;
    jsonMode = false;
    el.hJson.hidden = true;
    el.hTurns.hidden = false;
    el.hAdd.disabled = false;
    el.hJsonToggle.textContent = "JSON으로 보기";
    el.hJsonToggle.setAttribute("aria-pressed", "false");
    renderHistoryTurns();
    return true;
  }

  function applyPreset(id) {
    const p = presets.find((x) => x.id === id) || null;
    renderPresetDesc(p);
    if (!p) return;
    historyTurns = p.history.map((h) => ({ role: h.role, text: h.text }));
    el.caller.value = p.caller || "";
    if (el.caller.value !== (p.caller || "")) el.caller.value = "";   // 목록에 없는 번호
    if (p.now) el.now.value = p.now.replace(" ", "T");
    renderOrders();
    if (jsonMode) { el.hJson.value = JSON.stringify(historyTurns, null, 2); updateHistoryWarn(); }
    else renderHistoryTurns();
  }

  function renderPresetDesc(p) {
    el.hDesc.textContent = "";
    if (!p) { el.hDesc.hidden = true; return; }
    el.hDesc.hidden = false;
    const add = (cls, text) => {
      const d = document.createElement("div");
      if (cls) d.className = cls;
      d.textContent = text;
      el.hDesc.appendChild(d);
    };
    add("", p.description);
    add("muted", `고객: ${p.caller ? `${p.caller_name || "?"} (${p.caller})` : "비회원"} · 현재 시각: ${p.now}`);
    for (const s of p.try_saying || []) add("say", `이렇게 말해 보세요: "${s}"`);
    const exp = p.expected || {};
    const calls = (exp.tools || []).map((c) => `${c.name}(${JSON.stringify(c.arguments)})`).join(", ");
    if (calls) add("exp", "기대 함수: " + calls);
    if (exp.not_tools && exp.not_tools.length) add("exp", "부르면 안 됨: " + exp.not_tools.join(", "));
    if (exp.note) add("muted", exp.note);
  }

  function renderHistoryTurns() {
    el.hTurns.textContent = "";
    historyTurns.forEach((t, i) => {
      const li = document.createElement("li");
      li.className = "history-turn";
      li.dataset.role = t.role;
      const sel = document.createElement("select");
      sel.setAttribute("aria-label", `${i + 1}번째 줄 화자`);
      for (const [v, label] of Object.entries(ROLES)) {
        const o = document.createElement("option");
        o.value = v;
        o.textContent = label;
        sel.appendChild(o);
      }
      sel.value = t.role;
      sel.addEventListener("change", () => { t.role = sel.value; li.dataset.role = t.role; updateHistoryWarn(); });
      const ta = document.createElement("textarea");
      ta.rows = 2;
      ta.value = t.text;
      ta.placeholder = t.role === "agent" ? "상담원이 한 말" : "고객이 한 말";
      ta.setAttribute("aria-label", `${i + 1}번째 줄 내용`);
      ta.addEventListener("input", () => { t.text = ta.value; updateHistoryWarn(); });
      const btns = document.createElement("div");
      btns.className = "row-btns";
      const mk = (label, aria, fn, disabled) => {
        const b = document.createElement("button");
        b.type = "button";
        b.textContent = label;
        b.setAttribute("aria-label", aria);
        b.disabled = disabled;
        b.addEventListener("click", fn);
        btns.appendChild(b);
      };
      mk("▲", "위로", () => moveTurn(i, -1), i === 0);
      mk("▼", "아래로", () => moveTurn(i, 1), i === historyTurns.length - 1);
      mk("✕", "삭제", () => { historyTurns.splice(i, 1); renderHistoryTurns(); }, false);
      li.append(sel, ta, btns);
      el.hTurns.appendChild(li);
    });
    updateHistoryWarn();
  }

  function moveTurn(i, d) {
    const j = i + d;
    if (j < 0 || j >= historyTurns.length) return;
    [historyTurns[i], historyTurns[j]] = [historyTurns[j], historyTurns[i]];
    renderHistoryTurns();
  }

  // JSON 텍스트 → {turns} 또는 {error}. 역할은 "agent"|"customer"만, text는 문자열.
  function parseHistory(raw) {
    const src = String(raw || "").trim();
    if (!src) return { turns: [] };
    let v;
    try { v = JSON.parse(src); } catch (e) { return { error: "JSON 형식이 올바르지 않습니다: " + e.message }; }
    if (!Array.isArray(v)) return { error: "JSON은 [{\"role\": ..., \"text\": ...}, ...] 배열이어야 합니다." };
    const turns = [];
    for (let i = 0; i < v.length; i++) {
      const h = v[i];
      if (!h || typeof h !== "object" || Array.isArray(h)) return { error: `${i + 1}번째 항목이 객체가 아닙니다.` };
      if (!Object.prototype.hasOwnProperty.call(ROLES, h.role)) {
        return { error: `${i + 1}번째 항목의 role은 "agent" 또는 "customer"여야 합니다(지금: ${JSON.stringify(h.role)}).` };
      }
      if (h.text != null && typeof h.text !== "string") return { error: `${i + 1}번째 항목의 text가 문자열이 아닙니다.` };
      turns.push({ role: h.role, text: h.text || "" });
    }
    return { turns };
  }

  // 보낼 history: 빈 줄 제거, 역할 검사. 마지막이 상담원 말이 아니면 경고만(막지 않음).
  function collectHistory() {
    let turns = historyTurns;
    if (jsonMode) {
      const res = parseHistory(el.hJson.value);
      if (res.error) return { error: res.error, warnings: [] };
      turns = res.turns;
    }
    const history = [];
    let dropped = 0;
    for (const t of turns) {
      const text = String(t.text || "").trim();
      if (!text) { dropped++; continue; }
      if (!Object.prototype.hasOwnProperty.call(ROLES, t.role)) {
        return { error: `역할 값이 올바르지 않습니다: ${t.role}`, warnings: [] };
      }
      history.push({ role: t.role, text });
    }
    const warnings = [];
    if (dropped) warnings.push(`빈 줄 ${dropped}개는 보내지 않습니다.`);
    if (history.length && history[history.length - 1].role !== "agent") {
      warnings.push("마지막 줄이 고객 말입니다. 실제 통화는 상담원 말 다음에 고객이 말하므로 마지막은 상담원 말로 두는 것이 자연스럽습니다(그대로 보낼 수는 있음).");
    }
    return { history, warnings };
  }

  function updateHistoryWarn() {
    const res = collectHistory();
    el.hWarn.textContent = res.error || res.warnings.join(" ");
  }

  function renderInjectedHistory(history) {
    if (!history.length) return;
    const li = document.createElement("li");
    li.className = "injected";
    const title = document.createElement("div");
    title.className = "injected-title";
    title.textContent = `주입한 이전 대화 (${history.length}줄, 실제 통화 아님)`;
    li.appendChild(title);
    for (const h of history) {
      const b = document.createElement("div");
      b.className = "bubble " + (h.role === "agent" ? "agent" : "user");
      const who = document.createElement("span");
      who.className = "who";
      who.textContent = h.role === "agent" ? "상담원 (이전)" : "나 (이전)";
      const p = document.createElement("p");
      p.className = "text";
      p.textContent = h.text;
      b.append(who, p);
      li.appendChild(b);
    }
    const end = document.createElement("div");
    end.className = "injected-end";
    end.textContent = "— 여기서부터 실제 통화 —";
    li.appendChild(end);
    el.log.appendChild(li);
  }

  function setNotice(text) { el.notice.textContent = text || ""; }

  function selectedMember() {
    return members.find((m) => m.phone === el.caller.value) || null;
  }

  function renderOrders() {
    const m = selectedMember();
    el.orderList.textContent = "";
    if (!m) { el.orders.hidden = true; return; }
    el.orders.hidden = false;
    el.memberPhone.textContent = `${m.name} · ${m.phone_speech}`;
    for (const o of m.orders) {
      const li = document.createElement("li");
      const top = document.createElement("div");
      const id = document.createElement("span");
      id.className = "order-id";
      id.textContent = o.order_id;
      const st = document.createElement("span");
      st.className = "order-status";
      st.textContent = o.status;
      top.append(id, st);
      const speech = document.createElement("div");
      speech.className = "order-speech";
      speech.textContent = `"${o.order_speech}"`;
      const prod = document.createElement("div");
      prod.className = "muted";
      prod.textContent = `${o.product} · 주문 ${o.ordered_at}`;
      li.append(top, speech, prod);
      el.orderList.appendChild(li);
    }
  }

  function setConn(state, text) {
    el.conn.dataset.state = state;
    el.conn.textContent = text;
  }

  // ── 통화 시작/종료 ─────────────────────────────────
  function makeSessionId() {
    const d = new Date();
    const p = (n) => String(n).padStart(2, "0");
    const ts = `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`;
    return `DEMO-${ts}-${Math.random().toString(36).slice(2, 6)}`;
  }

  async function startCall() {
    if (call && !call.ended) return;
    if (!confirmDiscard()) return;
    setNotice("");
    const hist = collectHistory();
    if (hist.error) { setNotice("이전 대화를 보낼 수 없습니다: " + hist.error); return; }
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
    // 16kHz 컨텍스트면 브라우저가 고품질로 리샘플한다(워클릿은 비율 1로 프레임만 자름).
    // 지원하지 않는 브라우저는 기본 샘플레이트 + 워클릿 다운샘플로 대체.
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
    sink.gain.value = 0;                       // 워클릿을 돌리기 위한 연결(소리는 내보내지 않음)
    source.connect(node);
    node.connect(sink);
    sink.connect(ctx.destination);

    const sessionId = makeSessionId();
    const member = selectedMember();
    call = {
      ws: null, ctx, stream, node, source, sink, sessionId, member,
      now: el.now.value.replace("T", " "),
      history: hist.history, presetId: el.hPreset.value || null,
      halfDuplex: el.half.checked, adaptiveOpener: el.adaptive.checked,
      open: false, ended: false, responding: false,
      nextTime: 0, carry: null,
      sources: [],        // 예약된 재생 조각(끼어들기 때 한꺼번에 멈춘다, AG-T19)
      dropAudio: false,   // 끼어든 뒤 이미 날아오던 상담원 오디오는 버린다(다음 판정까지)
      bargeIns: 0,
      lastVoiceAt: 0, lastVoiceSeen: 0,
      awaitingFirstSound: true, pendingLatency: null,
      turn: null, lastTurn: null, turns: [], callsSeen: 0,
      startedAt: new Date().toISOString(), endedAt: null,
      // 녹음(AG-T52): t0 = 연결 순간의 오디오 시계. 고객 트랙은 보낸 프레임, 상담원 트랙은 재생 위치 기준
      rec: new R.Recorder(), t0: null, speechStartMs: null,
    };
    setReview(null);
    resetFeedbackForm();
    el.sessionId.textContent = sessionId;
    el.log.textContent = "";
    renderInjectedHistory(call.history);
    el.latHistory.textContent = "";
    el.latServer.textContent = "";
    setLatency(null);
    el.caller.disabled = el.now.disabled = el.half.disabled = el.adaptive.disabled = true;
    el.hPanel.disabled = true;               // 통화 중에는 이전 대화 편집 불가

    node.port.onmessage = (ev) => onMicFrame(ev.data);

    setConn("connecting", "연결 중");
    let ws;
    try {
      ws = new WebSocket(config.agent_ws);
    } catch (e) {
      endCall("상담원 서버 주소가 올바르지 않습니다: " + config.agent_ws);
      return;
    }
    ws.binaryType = "arraybuffer";
    call.ws = ws;
    const thisCall = call;
    ws.onopen = () => {
      if (call !== thisCall) return;
      call.open = true;
      call.t0 = call.ctx.currentTime;
      setConn("open", "연결됨");
      el.stop.disabled = false;
      const caller = member ? { name: member.name, phone: member.phone, email: null } : null;
      // 이전 대화는 연결당 한 번 보내는 이 session.start에만 넣는다(이후 턴은 서버가 자체 누적)
      ws.send(JSON.stringify({
        type: "session.start", session_id: sessionId, case_id: "DEMO", now: call.now,
        history: call.history, caller, tools_url: config.mock_url,
        audio: { encoding: "pcm_s16le", sample_rate: 16000, channels: 1 },
        debug: true,
        barge_in: true,                        // 상담원 말 중 300ms 넘게 말하면 끊고 이어 듣기(AG-T19)
        opener_adaptive: call.adaptiveOpener,  // AG-T20 첫 마디 정책
      }));
      showEmptyHint();
    };
    ws.onmessage = (ev) => {
      if (call !== thisCall) return;
      if (typeof ev.data === "string") onText(ev.data);
      else onAudio(ev.data);
    };
    ws.onerror = () => {
      if (call !== thisCall) return;
      if (!call.open) {
        setConn("error", "연결 실패");
        endCall(`상담원 서버(${config.agent_ws})에 연결할 수 없습니다. 실행 스크립트로 서버를 띄웠는지, 예열이 끝났는지 확인하세요.`, true);
      }
    };
    ws.onclose = () => {
      if (call !== thisCall) return;
      endCall(call.open ? "상담원 서버와 연결이 끊겼습니다." : "", !call.open);
    };
  }

  function showEmptyHint() {
    const li = document.createElement("li");
    li.className = "empty";
    li.textContent = "연결됐습니다. 이제 말해 보세요. 말을 마치면 상담원이 답합니다.";
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
    // 남은 상담원 음성은 끝까지 들려주고 닫는다
    const remain = Math.max(0, c.nextTime - c.ctx.currentTime);
    setTimeout(() => { try { c.ctx.close(); } catch (e) { /* 무시 */ } }, remain * 1000 + 300);
    if (!keepConnState) setConn("idle", "연결 안 됨");
    el.start.disabled = false;
    el.stop.disabled = true;
    el.caller.disabled = el.now.disabled = el.half.disabled = el.adaptive.disabled = false;
    el.hPanel.disabled = false;
    el.meter.style.width = "0";
    if (message) setNotice(message);
    if (call === c) setReview(reviewFromCall(c));
    if (c.turns.length) setFbStatus("통화가 끝났습니다. 턴별 녹음을 다시 들으며 👍/👎·문제 유형·메모를 남기고 저장해 주세요.");
  }

  // ── 마이크 → 서버 ──────────────────────────────────
  function agentAudible(c) {
    return c.ctx.currentTime < c.nextTime + ECHO_TAIL_S;
  }

  function micMuted(c) {
    return c.halfDuplex && (c.responding || agentAudible(c));
  }

  function onMicFrame(data) {
    const c = call;
    if (!c || c.ended) return;
    const muted = micMuted(c);
    const db = data.rms > 0 ? 20 * Math.log10(data.rms) : -90;
    const pct = Math.max(0, Math.min(100, ((db + 60) / 60) * 100));
    el.meter.style.width = pct + "%";
    el.meter.classList.toggle("muted-mic", muted);
    const now = performance.now();
    if (!muted && data.rms >= VOICE_RMS) c.lastVoiceAt = now;
    if (!c.open || !c.ws || c.ws.readyState !== 1) return;
    const out = muted ? new ArrayBuffer(640) : data.pcm;
    c.ws.send(out);
    c.rec.addMic(new Int16Array(out));          // 상담원이 실제로 받은 소리 그대로 녹음
  }

  // 녹음 시간축(ms): 서버 판정 이벤트는 받은 오디오 양 기준이므로 고객 트랙 길이로 잰다
  function micMs(c) { return c.rec.micMs(); }
  function ctxMs(c, t) { return Math.round((t - c.t0) * 1000); }

  // ── 서버 → 재생 ────────────────────────────────────
  function onAudio(buf) {
    const c = call;
    if (c.dropAudio) return;                    // 끼어든 뒤 남은 상담원 오디오(AG-T19)
    let bytes = new Uint8Array(buf);
    if (c.carry) {                              // 홀수 바이트가 남았던 경우
      const merged = new Uint8Array(c.carry.length + bytes.length);
      merged.set(c.carry, 0);
      merged.set(bytes, c.carry.length);
      bytes = merged;
      c.carry = null;
    }
    if (bytes.length % 2) {
      c.carry = bytes.slice(bytes.length - 1);
      bytes = bytes.slice(0, bytes.length - 1);
    }
    const n = bytes.length / 2;
    if (!n) return;
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.length);
    const f32 = new Float32Array(n);
    const i16 = new Int16Array(n);
    let sq = 0;
    for (let i = 0; i < n; i++) {
      const s = view.getInt16(i * 2, true);
      sq += s * s;
      i16[i] = s;
      f32[i] = s / 32768;
    }
    const rms = Math.sqrt(sq / n);
    const startAt = Math.max(c.nextTime, c.ctx.currentTime + JITTER_S);
    if (c.awaitingFirstSound && rms >= AGENT_VOICE_RMS) {
      c.awaitingFirstSound = false;
      const lat = c.lastVoiceAt ? Math.round(performance.now() - c.lastVoiceAt) : null;
      const t = ensureTurn();
      t.clientLatencyMs = lat;
      if (t.ts.first_sound_ms == null) t.ts.first_sound_ms = ctxMs(c, startAt);
      renderTurn(t);
      setLatency(lat);
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

  // 끼어들기(AG-T19): 서버가 debug.barge_in을 보내면 예약된 상담원 음성을 즉시 멈춘다
  function stopPlayback(c) {
    for (const s of c.sources.splice(0)) {
      try { s.onended = null; s.stop(); } catch (e) { /* 이미 끝남 */ }
    }
    if (c.t0 != null) c.rec.cut((c.ctx.currentTime - c.t0) * R.RATE);   // 들리지 않은 뒷부분은 녹음에서도 지운다
    c.nextTime = c.ctx.currentTime;
  }

  // ── 서버 텍스트 이벤트 ──────────────────────────────
  function onText(raw) {
    let msg;
    try { msg = JSON.parse(raw); } catch (e) { return; }
    const c = call;
    switch (msg.type) {
      case "debug.turn":
        if (msg.event === "decided") {
          c.responding = true;
          c.dropAudio = false;
          const t = ensureTurn();
          t.decided = { reason: msg.reason, silence_ms: msg.silence_ms, speech_ms: msg.speech_ms,
                        text: msg.text, opener: msg.opener };
          t.ts.decided_ms = micMs(c);
          if (t.ts.speech_start_ms == null) t.ts.speech_start_ms = c.speechStartMs;
          c.speechStartMs = null;
          renderTurn(t);
        } else if (msg.event === "speech_start") {
          c.awaitingFirstSound = true;
          if (c.speechStartMs == null) c.speechStartMs = micMs(c);
        }
        break;
      case "debug.stt": {
        const t = ensureTurn();
        t.user = msg.text || "";
        t.userFinal = true;
        renderTurn(t);
        break;
      }
      case "debug.tool": {
        const t = ensureTurn();
        t.tools.push({ name: msg.name, arguments: msg.arguments, status: msg.status, matched: undefined });
        renderTurn(t);
        break;
      }
      case "agent.transcript": {
        const t = ensureTurn();
        t.agent = msg.text || "";
        renderTurn(t);
        break;
      }
      case "turn.end": {
        // AG-T61: 끼어든 턴은 여기서 끝났다. 이 뒤의 오디오는 다음 턴 것이라 버리지 않는다(다음 턴 첫마디는 decided보다 먼저 온다).
        c.dropAudio = false;
        const t = ensureTurn();
        t.ended = true;
        t.ts.turn_end_ms = micMs(c);
        if (c.t0 != null) t.ts.agent_audio_end_ms = ctxMs(c, c.nextTime);   // 이 턴 소리는 모두 예약됨(같은 WS 순서)
        c.responding = false;
        c.awaitingFirstSound = true;
        c.lastTurn = t;
        c.turn = null;
        renderTurn(t);
        fetchCalls(t);
        break;
      }
      case "debug.barge_in": {
        stopPlayback(c);
        c.bargeIns += 1;
        // 응답 도중이면 이 턴의 남은 오디오를 버린다. turn.end 뒤(late)면 이미 끝난 턴에 표시만 한다.
        if (!msg.late) c.dropAudio = true;
        const t = msg.late ? c.lastTurn : (c.turn || c.lastTurn);
        if (t) {
          t.interrupted = { late: !!msg.late, heard_ms: msg.heard_ms, voiced_ms: msg.voiced_ms };
          renderTurn(t);
        }
        break;
      }
      case "debug.timing": {
        const t = c.turn || c.lastTurn;
        if (t) { t.timing = msg; renderTurn(t); renderServerTiming(msg); addHistory(t); }
        break;
      }
      default:
        break;
    }
  }

  function ensureTurn() {
    const c = call;
    if (c.turn) return c.turn;
    const empty = el.log.querySelector(".empty");
    if (empty) empty.remove();
    const t = newTurn(c.turns.length + 1);
    mountTurn(t);
    c.turns.push(t);
    c.turn = t;
    return t;
  }

  function newTurn(index) {
    return { index, user: "", userFinal: false, agent: "", tools: [], decided: null, timing: null,
             clientLatencyMs: null, ended: false, interrupted: null,
             ts: { speech_start_ms: null, decided_ms: null, first_sound_ms: null, turn_end_ms: null, agent_audio_end_ms: null },
             audio: null,
             vote: null, tags: [], note: "", corrected: "",   // 휴먼 피드백
             node: null };
  }

  // 턴 노드를 만들어 대화 기록에 붙이고 평가 입력을 t에 묶는다(실시간 통화·불러온 JSON 공통)
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

  async function fetchCalls(turn) {
    const c = call;
    try {
      const r = await fetch("/api/calls?session_id=" + encodeURIComponent(c.sessionId));
      if (!r.ok) return;
      const rows = await r.json();
      const fresh = rows.slice(c.callsSeen);
      c.callsSeen = rows.length;
      for (const row of fresh) {
        const tool = turn.tools.find((x) => x.name === row.name && x.matched === undefined);
        if (tool) tool.matched = row.matched;
        else turn.tools.push({ name: row.name, arguments: row.arguments, status: null, matched: row.matched });
      }
      renderTurn(turn);
    } catch (e) { /* mock 미실행: matched 표시 생략 */ }
  }

  // ── 렌더 ──────────────────────────────────────────
  // 결정기 이유 문자열(예: "end table:final/p0s1(wait=600 ...) silence=620")을 짧은 한국어로
  const GROUPS = {
    strong: "문장이 분명히 끝남", final: "종결 어미", soft: "약한 종결", weak: "약한 종결",
    neutral: "판단 보류(기본 대기)", particle: "조사로 끝남(이어질 수 있음)", hold: "이어지는 말투(연결 어미)",
  };

  function reasonLabel(reason) {
    const r = String(reason || "");
    if (r.startsWith("hard_max")) return "최대 침묵 도달";
    const g = /table:(\w+)/.exec(r);
    const w = /wait=(\d+)/.exec(r);
    if (g) return (GROUPS[g[1]] || g[1]) + (w ? ` → ${w[1]}ms 기다림` : "");
    return r || "?";
  }

  function renderTurn(t) {
    const n = t.node;
    const userP = n.querySelector(".bubble.user .text");
    if (t.userFinal) userP.textContent = t.user || "(알아들은 말 없음)";
    else userP.textContent = (t.decided && t.decided.text) ? t.decided.text + " …" : "…";
    const meta = [];
    if (t.decided) {
      const reason = reasonLabel(t.decided.reason);
      n.querySelector(".meta").title = t.decided.reason || "";
      const parts = [`말 끝 판정: ${reason}`];
      if (t.decided.silence_ms != null) parts.push(`침묵 ${t.decided.silence_ms}ms`);
      if (t.decided.speech_ms != null) parts.push(`발화 ${(t.decided.speech_ms / 1000).toFixed(1)}초`);
      meta.push(parts.join(" · "));
      if (t.userFinal && t.decided.text && t.decided.text !== t.user) meta.push(`판정 시점 텍스트: "${t.decided.text}"`);
    }
    if (t.clientLatencyMs != null) meta.push(`첫 소리 ${t.clientLatencyMs}ms`);
    if (t.timing && t.timing.opener_reason) {
      const why = { answer_ready: "없음(답이 준비돼 바로 답함)", not_ready: "씀(답 준비 중)", tool_expected: "씀(조회 필요)",
                    fallback_simple: "씀(대체 문구)", no_fresh_opener: "없음(반복 방지)" }[t.timing.opener_reason] || t.timing.opener_reason;
      meta.push(`첫 마디 ${why}${t.timing.opener ? ` "${t.timing.opener}"` : ""}`);
    }
    if (t.interrupted) {
      const at = t.interrupted.heard_ms != null ? ` (상담원 말 ${(t.interrupted.heard_ms / 1000).toFixed(1)}초 시점)` : "";
      meta.push(t.interrupted.late ? `응답 재생 중 끼어들기${at}` : `끼어들기로 중단${at}`);
    }
    n.querySelector(".meta").textContent = meta.join("  |  ");
    const ul = n.querySelector(".tools");
    ul.textContent = "";
    for (const tool of t.tools) {
      const li = document.createElement("li");
      const args = tool.arguments == null ? "" : JSON.stringify(tool.arguments);
      li.append(document.createTextNode(`함수 ${tool.name}(${args})`));
      if (tool.status) li.append(document.createTextNode(` → ${tool.status}`));
      if (tool.matched !== undefined && tool.matched !== null) {
        const m = document.createElement("span");
        m.className = tool.matched ? "ok" : "ng";
        m.textContent = tool.matched ? "  matched" : "  not matched";
        li.append(m);
      }
      ul.appendChild(li);
    }
    n.querySelector(".bubble.agent .text").textContent = t.agent || (t.ended ? "(응답 없음)" : "…");
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

  const TIMING_LABELS = [
    ["silence_to_commit_ms", "침묵 시작 → 말 끝 판정"],
    ["commit_to_opener_ms", "판정 → 첫 마디 송신"],
    ["commit_to_finalize_ms", "판정 → 최종 인식"],
    ["commit_to_llm_first_ms", "판정 → LLM 첫 응답"],
    ["commit_to_response_audio_ms", "판정 → 본 응답 첫 소리"],
    ["commit_to_turn_end_ms", "판정 → 응답 끝"],
    ["gap_ms", "첫 마디 뒤 공백"],
  ];

  function renderServerTiming(tm) {
    el.latServer.textContent = "";
    for (const [k, label] of TIMING_LABELS) {
      if (tm[k] == null) continue;
      const dt = document.createElement("dt");
      dt.textContent = label;
      const dd = document.createElement("dd");
      dd.textContent = Math.round(tm[k]) + "ms";
      el.latServer.append(dt, dd);
    }
    const dt = document.createElement("dt");
    dt.textContent = "선호출 재사용";
    const dd = document.createElement("dd");
    dd.textContent = tm.precall_reused ? "예" : "아니요";
    el.latServer.append(dt, dd);
    if (tm.opener_rate != null) {                // AG-T20: 이 통화에서 첫 마디를 쓴 턴 비율
      const dt2 = document.createElement("dt");
      dt2.textContent = "첫 마디 사용률(통화 누적)";
      const dd2 = document.createElement("dd");
      dd2.textContent = Math.round(tm.opener_rate * 100) + "%";
      el.latServer.append(dt2, dd2);
    }
  }

  function addHistory(t) {
    const li = document.createElement("li");
    const ms = t.clientLatencyMs;
    li.className = grade(ms);
    const end = t.timing && t.timing.commit_to_turn_end_ms != null ? ` · 응답 끝 ${(t.timing.commit_to_turn_end_ms / 1000).toFixed(1)}초` : "";
    li.textContent = (ms == null ? "첫 소리 측정 안 됨" : `첫 소리 ${(ms / 1000).toFixed(2)}초`) + end;
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

  // ── 휴먼 피드백: 평가·저장·내려받기·불러오기 (AG-T52) ─────────
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
    try { el.fbReviewer.value = localStorage.getItem("demo.reviewer") || ""; } catch (e) { /* 저장소 없음 */ }
    el.fbReviewer.addEventListener("input", () => {
      try { localStorage.setItem("demo.reviewer", el.fbReviewer.value.trim()); } catch (e) { /* 무시 */ }
      markDirty();
    });
    window.addEventListener("beforeunload", (e) => {
      if (hasUnsaved()) { e.preventDefault(); e.returnValue = ""; }
    });
    updateSaveButtons();
  }

  function hasUnsaved() { return !!(review && !review.saved && review.turns.length); }
  function markDirty() { if (review) review.saved = false; }
  function confirmDiscard() {
    return !hasUnsaved() || window.confirm("저장하지 않은 평가가 있습니다. 버리고 계속할까요?");
  }

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
    const hasAudio = c.rec && (c.rec.micSamples > 0 || c.rec.agent.length > 0);
    const tracks = hasAudio ? { customer: c.rec.micTrack(), agent: c.rec.agentTrack() } : null;
    const durationMs = hasAudio ? c.rec.durationMs() : 0;
    const clips = R.turnClips(c.turns, durationMs);
    c.turns.forEach((t, i) => { t.audio = tracks ? clips[i] : null; });
    return {
      sessionId: c.sessionId, startedAt: c.startedAt, endedAt: c.endedAt,
      setup: {
        caller: c.member ? { name: c.member.name, phone: c.member.phone, email: null } : null,
        now: c.now, history_preset: c.presetId, history: c.history,
        half_duplex: c.halfDuplex, adaptive_opener: c.adaptiveOpener, barge_ins: c.bargeIns,
        agent_ws: config.agent_ws, user_agent: navigator.userAgent,
      },
      turns: c.turns, tracks, durationMs, saved: false, audioMissing: false, urls: [],
    };
  }

  // 평가 대상 교체: 이전 대상의 오디오 URL을 정리하고 턴별·전체 재생기를 붙인다
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

  // 평가 대상 → 통화 묶음 JSON(서버 저장·내려받기 공통, demo/feedback_store.py 형식)
  function buildBundle(r) {
    const ratingEl = el.fbForm.querySelector("input[name=rating]:checked");
    const b = {
      schema: SCHEMA,
      session_id: r.sessionId,
      started_at: r.startedAt,
      ended_at: r.endedAt,
      setup: r.setup,
      feedback: {
        rating: ratingEl ? Number(ratingEl.value) : null,
        comment: el.fbComment.value.trim(),
        reviewer: el.fbReviewer.value.trim() || null,
        updated_at: new Date().toISOString(),
      },
      turns: r.turns.map((t) => ({
        index: t.index,
        user_stt: t.user,
        user_corrected: t.corrected.trim(),
        agent: t.agent,
        decided: t.decided,
        tools: t.tools.map((x) => ({ name: x.name, arguments: x.arguments, status: x.status,
                                     matched: x.matched === undefined ? null : x.matched })),
        client_latency_ms: t.clientLatencyMs,
        timing: t.timing,
        interrupted: t.interrupted || null,
        ts: t.ts,
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
    setFbStatus(`내려받음: ${a.download} (${(blob.size / 1e6).toFixed(1)}MB). 폴더로 풀기: uv run python -m demo.feedback_store ${a.download}`, "ok");
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
    el.latServer.textContent = "";
    setLatency(null);
    const banner = document.createElement("li");
    banner.className = "review-banner";
    banner.textContent = `불러온 통화 ${file.name} · 시작 ${b.started_at || "?"} · 현재 시각 ${(b.setup && b.setup.now) || "?"}`
      + (b.feedback && b.feedback.reviewer ? ` · 이전 평가자 ${b.feedback.reviewer}` : "");
    el.log.appendChild(banner);
    renderInjectedHistory((b.setup && b.setup.history) || []);
    const turns = b.turns.map((x, i) => {
      const fb = x.feedback || {};
      const t = newTurn(x.index || i + 1);
      Object.assign(t, {
        user: x.user_stt || "", userFinal: true, agent: x.agent || "", tools: Array.isArray(x.tools) ? x.tools : [],
        decided: x.decided || null, timing: x.timing || null,
        clientLatencyMs: x.client_latency_ms == null ? null : x.client_latency_ms,
        interrupted: x.interrupted || null, ended: true,
        ts: Object.assign(t.ts, x.ts || {}), audio: x.audio || null,
        vote: fb.vote === "up" || fb.vote === "down" ? fb.vote : null,
        tags: Array.isArray(fb.tags) ? fb.tags.filter((k) => typeof k === "string") : [],
        note: fb.note || "", corrected: x.user_corrected || "",
      });
      mountTurn(t);
      renderTurn(t);
      if (t.timing) addHistory(t);
      return t;
    });
    el.sessionId.textContent = `${b.session_id} (불러옴)`;
    resetFeedbackForm();
    const fb = b.feedback || {};
    const star = fb.rating ? el.fbForm.querySelector(`input[name=rating][value="${Number(fb.rating)}"]`) : null;
    if (star) star.checked = true;
    el.fbComment.value = fb.comment || "";
    setReview({
      sessionId: b.session_id, startedAt: b.started_at || null, endedAt: b.ended_at || null,
      setup: b.setup || {}, turns, tracks, durationMs, saved: true, audioMissing: !tracks, urls: [],
    });
    setFbStatus(tracks ? "불러왔습니다. 평가를 고친 뒤 저장하면 같은 session_id 폴더를 덮어씁니다."
                       : "오디오가 없는 JSON입니다. 서버 저장은 막고(기존 녹음 보호) 내려받기만 됩니다.");
  }

  init();
})();
