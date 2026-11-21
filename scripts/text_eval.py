"""음성 없이 도는 평가(대회 게이트웨이 키를 쓰지 않는다). 프롬프트·검사 규칙을 빠르게 반복할 때 쓴다.

실제 서버와 같은 프롬프트(agent.knowledge), 같은 응답기·검사(agent.responder, agent.guards), 같은 모의 함수 규칙
(mock_api.server.Store)을 쓰고, STT와 TTS만 건너뛴다. 응대 채점은 공식 채점기(score.py)의 판정 함수를 그대로 부른다.

  # 1) 받아쓰기 결과를 한 번만 저장(Soniox 사용 = 게이트웨이 키 사용. 문항당 연결 1개)
  uv run python scripts/text_eval.py stt
  # 2) 그 뒤로는 OpenAI 키만으로 반복
  uv run python scripts/text_eval.py run --out runs/text/t1 [--only P0010-11,P0027-01] [--limit 20] [--source stt|original]

- --source stt(기본): 저장해 둔 받아쓰기 결과를 고객 말로 쓴다(번호가 뭉개진 실제 조건).
  받아쓰기는 실제 서버처럼 제품명 용어를 넘겨 저장한다. 실제 음성 경로는 쉼마다 끝맺음을 요청해 번호가
  마침표로 끊기는 등 글이 조금 다르므로(80문항 중 25문항), 이 평가는 음성 경로의 근사치다.
  --source original: 문항 원문을 그대로 쓴다(받아쓰기가 완벽할 때의 상한).
- 속도는 재지 않는다. 음성 전체 측정은 공식 측정기(run_eval.py)로 한다.
- 결과(runs/ 아래)는 연습 문항 내용을 담으므로 커밋하지 않는다.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import re
import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
KIT = ROOT / "DIRECT_ON 2026 Starter Kit"
CASES = KIT / "data" / "practice_cases.jsonl"
AUDIO = KIT / "data" / "audio" / "practice"
STT_CACHE = ROOT / "runs" / "text" / "stt_cache.json"


def load_cases() -> dict[str, dict]:
    return {json.loads(l)["case_id"]: json.loads(l) for l in open(CASES, encoding="utf-8")}


def pick(cases: dict[str, dict], only: str, limit: int) -> list[str]:
    ids = [c.strip() for c in only.split(",") if c.strip()] if only else list(cases)
    return ids[:limit] if limit else ids


# ── 받아쓰기 저장 ─────────────────────────────────────
async def cmd_stt(args) -> None:
    from agent.providers.factory import make_stt

    cases = load_cases()
    cache = json.loads(STT_CACHE.read_text(encoding="utf-8")) if STT_CACHE.is_file() else {}
    ids = [c for c in pick(cases, args.only, args.limit) if args.force or c not in cache]
    from agent import knowledge

    stt = make_stt()
    terms = ", ".join(knowledge.stt_terms())                   # agent.server.build_deps와 같은 값
    sem = asyncio.Semaphore(args.concurrency)

    async def one(cid: str) -> None:
        async with sem:
            w = wave.open(str(AUDIO / f"{cid}.wav"))
            pcm = w.readframes(w.getnframes())
            w.close()
            st = await stt.open_stream(terms)                  # 실제 서버와 같은 용어(브랜드·제품 이름)를 넘긴다
            step = 640 * 5
            for i in range(0, len(pcm), step):                 # 실제 속도로 보낸다
                await st.send_audio(pcm[i:i + step])
                await asyncio.sleep(0.1)
            await st.send_audio(bytes(640 * 25))
            await asyncio.sleep(0.3)
            cache[cid] = (await st.finalize(3.0)).strip()
            await st.close()

    await asyncio.gather(*(one(c) for c in ids))
    STT_CACHE.parent.mkdir(parents=True, exist_ok=True)
    STT_CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"받아쓰기 {len(ids)}개 저장 → {STT_CACHE} (전체 {len(cache)}개)")


# ── 텍스트 실행 ───────────────────────────────────────
class SilentTTS:
    """합성하지 않는다. 문장마다 짧은 소리 조각을 돌려준다(응답기의 송신 경로만 통과)."""

    async def synthesize(self, text: str):
        yield (b"\x10\x27" * 160)


async def run_case(case: dict, text: str, llm, store, tools: list[dict]) -> dict:
    from agent import knowledge
    from agent.config import Settings
    from agent.core.types import Message, SessionInfo
    from agent.guards import GuardContext, valid_phone
    from agent.responder import Responder
    from agent.tts_cache import select_opener_for_text

    s = Settings.from_env()
    info = SessionInfo(session_id=case["case_id"], case_id=case["case_id"], now=case["now"],
                       history=case.get("history") or [], caller=case.get("customer_profile"), tools_url="")
    from agent.knowledge import history_numbers
    _n = history_numbers(info.history)
    can_lookup = bool(re.sub(r"\D", "", str((info.caller or {}).get("phone") or "")) or _n["orders"] or _n["phones"])
    opener = select_opener_for_text(text, s, default=s.openers[0], can_lookup=can_lookup)   # 서버(Session._opener_for)와 같게
    calls: list[dict] = []
    preload_text, preload_results = "", []
    phone = re.sub(r"\D", "", str((info.caller or {}).get("phone") or ""))
    if s.preload_orders and valid_phone(phone) and not any(
            isinstance(h, dict) and h.get("role") == "tool_result" and h.get("name") == "get_order_info"
            for h in info.history):                     # 서버의 Session._start_preload와 같은 조건
        a = {"buyer_cellphone": phone}
        res, _ = store.lookup("get_order_info", a)
        calls.append({"name": "get_order_info", "arguments": a, "matched": res.get("status") == "ok"})
        preload_results = [{"name": "get_order_info", "arguments": a, "result": res}]
        preload_text = json.dumps(res, ensure_ascii=False)[:3000]
    system = knowledge.build_system(info, opener, preload=preload_text)
    from agent import intents
    system += intents.system_suffix(text, info.history)     # AG-T55: 서버(Session)와 같은 위치

    async def call_tool(name: str, a: dict) -> dict:
        res, matched = store.lookup(name, a)
        calls.append({"name": name, "arguments": a, "matched": res.get("status") == "ok"})
        return res

    async def sink(pcm: bytes) -> None:
        return None

    guard = GuardContext(tools=tools, user_text=text, history=list(info.history), caller=info.caller,
                         results=list(preload_results))
    r = Responder(llm, SilentTTS(), call_tool, tools=tools, settings=s, cache=None, now=info.now, guard=guard)
    out = {"case_id": case["case_id"], "user_text": text, "opener": opener}
    try:
        spoken = await asyncio.wait_for(r.respond(system, [Message("user", text)], sink), 60)
        out["agent_text"] = " ".join([opener, *spoken]).strip()
    except Exception as e:  # noqa: BLE001
        out["agent_text"] = opener
        out["error"] = repr(e)[:200]
    out["tool_calls"] = calls
    out["blocked"] = [{"name": e["name"], "arguments": e["arguments"]} for e in r.tool_log if e.get("blocked")]
    return out


import httpx as _httpx  # noqa: E402


class _BazeJudgeTransport(_httpx.AsyncBaseTransport):
    """공식 채점기의 api.openai.com/v1/... 요청을 BAZE 게이트웨이(OpenAI 호환)로 옮긴다."""

    def __init__(self, base: str, key: str):
        import httpx
        self.base = base.rstrip("/")
        self.key = key
        self.inner = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request):
        import httpx
        if request.url.host == "api.openai.com":
            path = request.url.path.removeprefix("/v1")
            headers = {k: v for k, v in request.headers.items() if k.lower() not in ("authorization", "host", "content-length")}
            headers["Authorization"] = "Bearer " + self.key
            request = httpx.Request(request.method, self.base + path, headers=headers, content=request.content)
        return await self.inner.handle_async_request(request)

    async def aclose(self):
        await self.inner.aclose()


def load_official_score():
    spec = importlib.util.spec_from_file_location("official_score", KIT / "eval" / "score.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def cmd_run(args) -> None:
    import httpx
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    from agent.providers.factory import ProviderConfig, make_llm
    from mock_api.server import load_store

    # 키: OPENAI_API_KEY(직접)가 있으면 그것만 쓴다. 없고 BAZE_API_KEY(대회 게이트웨이)가 있으면 게이트웨이로
    # 상담원 LLM과 (--no-judge가 아니면) 응대 채점을 보낸다. 채점은 공식 score.judge를 그대로 부르고,
    # api.openai.com 요청만 게이트웨이 주소로 옮긴다(공식 키트는 고치지 않는다).
    direct = bool(os.environ.get("OPENAI_API_KEY"))
    baze = "" if direct else os.environ.get("BAZE_API_KEY", "").strip()
    if not direct and not baze:
        raise SystemExit("키가 없습니다: .env에 OPENAI_API_KEY 또는 BAZE_API_KEY를 넣으세요.")
    print(f"키: {'OPENAI_API_KEY(직접)' if direct else 'BAZE_API_KEY(게이트웨이)'}", flush=True)
    cases = load_cases()
    ids = pick(cases, args.only, args.limit)
    if args.source == "stt":
        if not STT_CACHE.is_file():
            raise SystemExit(f"{STT_CACHE} 없음 — 먼저 `text_eval.py stt`를 돌리세요")
        from agent.knowledge import fix_brand
        texts = {c: fix_brand(t) for c, t in json.loads(STT_CACHE.read_text(encoding="utf-8")).items()}
        ids = [c for c in ids if c in texts]
    else:
        texts = {c: " ".join(cases[c]["customer_utterance"].split()) for c in ids}
    llm = make_llm(ProviderConfig.from_env(baze_api_key="") if direct else ProviderConfig.from_env())
    store = load_store()
    tools = [{"type": "function", "function": t} for t in store.tools]
    sem = asyncio.Semaphore(args.concurrency)

    async def one(cid: str) -> dict:
        async with sem:
            return await run_case(cases[cid], texts[cid], llm, store, tools)

    S = load_official_score()
    scen = (KIT / "data/brand/scenarios.md").read_text(encoding="utf-8")
    info = json.loads((KIT / "data/brand/information.json").read_text(encoding="utf-8"))
    ctx = {"scenarios": scen, "info": "\n".join(f"{i['tag']}\n{i['content']}" for i in info), "rubric": {}}
    jsem = asyncio.Semaphore(6)

    async def score(c, r: dict) -> dict:
        case = cases[r["case_id"]]
        ok, why = S.tool_match(case["reference"]["tool_calls"], r["tool_calls"], case.get("history", []))
        if args.no_judge:
            return {**r, "tool_ok": ok, "tool_why": why, "verdict": None, "reason": ""}
        async with jsem:
            v = await S.judge(c, case, r["agent_text"], r["tool_calls"], ctx)
        return {**r, "said": r["agent_text"], "tool_ok": ok, "tool_why": why, **v}

    # 같은 코드로 돌려도 응대가 6점까지 벌어진다(2026-11-21 실측). 변경의 효과는 --repeat 3 이상의 평균으로 판단한다.
    lines, resps, tools_ = [], [], []
    transport = None
    if baze and not args.no_judge:
        from agent.providers.factory import baze_url
        os.environ.setdefault("OPENAI_API_KEY", "via-baze")        # score.KEY()가 읽는 자리(헤더는 아래에서 바꾼다)
        transport = _BazeJudgeTransport(baze_url(ProviderConfig.from_env()), baze)
    async with httpx.AsyncClient(timeout=180, transport=transport) as c:
        for i in range(max(1, args.repeat)):
            out = Path(args.out) if args.repeat <= 1 else Path(args.out) / f"r{i + 1}"
            out.mkdir(parents=True, exist_ok=True)
            results = await asyncio.gather(*(one(cid) for cid in ids))
            scored = await asyncio.gather(*(score(c, r) for r in results))
            with open(out / "scored.jsonl", "w", encoding="utf-8") as f:
                for s_ in scored:
                    f.write(json.dumps(s_, ensure_ascii=False) + "\n")
            n = len(scored)
            tool = sum(s_["tool_ok"] for s_ in scored) / n * 100
            tools_.append(tool)
            line = f"문항 {n}개 ({args.source}) | 함수 {tool:.1f}"
            if not args.no_judge:
                resp = sum(S.PTS[s_["verdict"]] for s_ in scored) / n * 100
                resps.append(resp)
                verdicts = {k: sum(s_["verdict"] == k for s_ in scored) for k in S.PTS}
                line += f" | 응대 {resp:.1f} {verdicts}"
            errs = sum("error" in s_ for s_ in scored)
            line += f" | 오류 {errs}" if errs else ""
            lines.append(line)
            print(line, flush=True)
    if len(lines) > 1:
        avg = f"평균({len(lines)}회) | 함수 {sum(tools_) / len(tools_):.1f}"
        if resps:
            avg += f" | 응대 {sum(resps) / len(resps):.1f} (최소 {min(resps):.1f}, 최대 {max(resps):.1f})"
        lines.append(avg)
        print(avg, flush=True)
    Path(args.out).mkdir(parents=True, exist_ok=True)
    (Path(args.out) / "summary.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("stt", help="받아쓰기 결과 저장(게이트웨이 키 사용)")
    a.add_argument("--only", default="")
    a.add_argument("--limit", type=int, default=0)
    a.add_argument("--concurrency", type=int, default=3)
    a.add_argument("--force", action="store_true")
    b = sub.add_parser("run", help="텍스트 평가(OpenAI 키만)")
    b.add_argument("--out", required=True)
    b.add_argument("--only", default="")
    b.add_argument("--limit", type=int, default=0)
    b.add_argument("--source", choices=["stt", "original"], default="stt")
    b.add_argument("--concurrency", type=int, default=8)
    b.add_argument("--no-judge", action="store_true")
    b.add_argument("--repeat", type=int, default=1, help="같은 설정으로 N번 돌려 평균을 낸다(변경 효과 판단은 3 이상)")
    args = ap.parse_args()
    if args.cmd == "stt":
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
        asyncio.run(cmd_stt(args))
    else:
        asyncio.run(cmd_run(args))


if __name__ == "__main__":
    main()
