"""체험 데모 서버(AG-T14). 실행: uv run python -m demo.server --port 8080

브라우저 페이지(demo/static)를 내주고, 체험에 필요한 작은 API를 제공한다. 표준 라이브러리만 쓴다.
- GET  /                       → static/index.html (그 밖의 /<파일>은 static/ 안의 파일만)
- GET  /api/members            → mock 데이터 회원 목록 + 주문 요약(주문번호 한글 읽기 포함)
- GET  /api/calls?session_id=  → mock API /calls 프록시(같은 출처라 CORS 불필요)
- GET  /api/config             → 상담원 WS 주소, mock 주소, 기본 now
- GET  /api/history_presets    → 이전 대화(history) 프리셋(demo/presets/history_presets.json, AG-T14b)
                                  caller 전화번호가 있으면 caller_name(회원 이름)을 붙인다
- POST /api/feedback           → runs/demo_feedback/feedback-YYYYMMDD.jsonl에 한 줄 추가(예전 형식, 오디오 없음)
- POST /api/sessions           → 통화 묶음(설정·대화·피드백·오디오, demo/feedback_store.py 형식)을
                                  runs/demo_feedback/sessions/<session_id>/로 풀어 저장(AG-T52)

127.0.0.1에만 바인딩한다(마이크는 http://127.0.0.1·localhost에서 허용된다).
상담원 서버와의 음성 연결은 브라우저가 직접 WebSocket으로 한다(이 서버는 중계하지 않는다).
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from agent.normalize import digits_for_speech
from demo import feedback_store

ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = Path(__file__).resolve().parent / "static"
# static/에 없는 파일(session-record.js, mic-worklet.js)은 이식용 키트에서 내준다(AG-T53, 원본 한 벌)
KIT_STATIC_DIR = ROOT / "feedback_kit" / "static"
DATA_PATH = ROOT / "legacy" / "shop_mock" / "mock_data.json"
FEEDBACK_DIR = ROOT / "runs" / "demo_feedback"
SESSIONS_DIR = feedback_store.DEFAULT_ROOT
MAX_SESSION_BODY = 300_000_000   # 통화 묶음(오디오 base64 포함). 10분 통화 ≈ 55MB
PRESETS_PATH = Path(__file__).resolve().parent / "presets" / "history_presets.json"
DEFAULT_AGENT_WS = "ws://127.0.0.1:9000"
DEFAULT_MOCK_URL = "http://127.0.0.1:8100"
DEFAULT_NOW = "2026-11-20 14:00"
HOST = "127.0.0.1"
MAX_BODY = 1_000_000

CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8",
                 ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon"}


def _item_text(item: dict) -> str:
    name = item.get("product_name", "")
    opt = item.get("option")
    qty = item.get("quantity", 1)
    return f"{name}({opt})×{qty}" if opt else f"{name}×{qty}"


KIT_DATA = ROOT / "DIRECT_ON 2026 Starter Kit" / "data"


def build_members_official(kit_data: Path | str = KIT_DATA) -> list[dict]:
    """공식 키트(mock_db.json의 get_order_info)에서 전화번호별 주문을 모은 회원 목록.
    이름은 연습 문항의 발신자 정보(customer_profile)에서 같은 전화번호로 찾는다(없으면 '고객 뒷자리')."""
    kit = Path(kit_data)
    db = json.loads((kit / "mock_db.json").read_text(encoding="utf-8"))
    names: dict[str, str] = {}
    cases = kit / "practice_cases.jsonl"
    if cases.is_file():
        for line in cases.read_text(encoding="utf-8").splitlines():
            prof = (json.loads(line).get("customer_profile") or {}) if line.strip() else {}
            ph = "".join(ch for ch in str(prof.get("phone") or "") if ch.isdigit())
            if ph and prof.get("name"):
                names.setdefault(ph, str(prof["name"]))
    orders_by_phone: dict[str, dict[str, dict]] = {}
    for e in db.get("get_order_info", []):
        phone = "".join(ch for ch in str((e.get("arguments") or {}).get("buyer_cellphone") or "") if ch.isdigit())
        res = e.get("result") or {}
        if not phone or res.get("status") != "ok":
            continue
        for o in (res.get("data") or {}).get("orders", []):
            oid = o.get("주문번호")
            if not oid:
                continue
            cur = orders_by_phone.setdefault(phone, {}).setdefault(oid, {
                "order_id": oid, "order_speech": digits_for_speech(oid.replace("-", ""), "order"),
                "products": [], "status": o.get("배송상태", ""), "ordered_at": o.get("주문일자", ""),
                "total_amount": None})
            if o.get("제품명") and o["제품명"] not in cur["products"]:
                cur["products"].append(o["제품명"])
    out = []
    for phone, orders in orders_by_phone.items():
        rows = sorted(orders.values(), key=lambda o: o["ordered_at"], reverse=True)
        out.append({"name": names.get(phone) or f"고객 {phone[-4:]}", "phone": phone,
                    "phone_speech": digits_for_speech(phone, "phone"), "grade": "",
                    "orders": [{**{k: v for k, v in o.items() if k != "products"},
                                "product": ", ".join(o["products"])} for o in rows]})
    return sorted(out, key=lambda m: m["name"])


def build_members(data_path: Path | str = DATA_PATH) -> list[dict]:
    """회원마다 이름·전화(읽기)·등급·주문 요약(주문번호·읽기·상품·상태).
    옛 가정 자료(data_path)가 없으면 공식 키트 자료로 만든다."""
    if not Path(data_path).is_file():
        return build_members_official() if (KIT_DATA / "mock_db.json").is_file() else []
    data = json.loads(Path(data_path).read_text(encoding="utf-8"))
    orders_by_phone: dict[str, list[dict]] = {}
    for o in data.get("orders", []):
        orders_by_phone.setdefault(o.get("phone_number", ""), []).append(o)
    out = []
    for m in data.get("members", []):
        phone = m.get("phone_number", "")
        orders = sorted(orders_by_phone.get(phone, []), key=lambda o: o.get("ordered_at", ""), reverse=True)
        out.append({
            "name": m.get("name", ""),
            "phone": phone,
            "phone_speech": digits_for_speech(phone, "phone"),
            "grade": m.get("grade", ""),
            "orders": [{"order_id": o["order_id"],
                        "order_speech": digits_for_speech(o["order_id"], "order"),
                        "product": ", ".join(_item_text(i) for i in o.get("items", [])),
                        "status": o.get("status", ""),
                        "ordered_at": o.get("ordered_at", ""),
                        "total_amount": o.get("total_amount")} for o in orders],
        })
    return out


def build_history_presets(presets_path: Path | str = PRESETS_PATH,
                          data_path: Path | str = DATA_PATH) -> list[dict]:
    """프리셋 목록. 각 항목에 caller_name(회원 이름, 비회원·없는 번호면 None)을 붙인다."""
    raw = json.loads(Path(presets_path).read_text(encoding="utf-8"))
    presets = raw.get("presets", []) if isinstance(raw, dict) else raw
    if Path(data_path).is_file():
        data = json.loads(Path(data_path).read_text(encoding="utf-8"))
        names = {m.get("phone_number"): m.get("name") for m in data.get("members", [])}
    else:
        names = {m["phone"]: m["name"] for m in build_members(data_path)}
    out = []
    for p in presets:
        item = dict(p)
        item["caller_name"] = names.get(p.get("caller")) if p.get("caller") else None
        out.append(item)
    return out


class DemoServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, *, mock_url: str, agent_ws: str, data_path: Path, static_dir: Path,
                 feedback_dir: Path, date_fn: Callable[[], str], presets_path: Path = PRESETS_PATH,
                 sessions_dir: Path = SESSIONS_DIR):
        super().__init__(addr, Handler)
        self.presets_path = Path(presets_path)
        self.sessions_dir = Path(sessions_dir)
        self.mock_url = mock_url.rstrip("/")
        self.agent_ws = agent_ws
        self.data_path = Path(data_path)
        self.static_dir = Path(static_dir).resolve()
        self.feedback_dir = Path(feedback_dir)
        self.date_fn = date_fn
        self.lock = threading.Lock()
        self._members: list[dict] | None = None

    def members(self) -> list[dict]:
        if self._members is None:
            self._members = build_members(self.data_path)
        return self._members

    def history_presets(self) -> list[dict]:
        # 파일을 고치며 시험할 수 있게 매번 읽는다(작다)
        return build_history_presets(self.presets_path, self.data_path)


class Handler(BaseHTTPRequestHandler):
    server: DemoServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 조용히
        pass

    # ── 응답 도우미 ──────────────────────────────
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), CONTENT_TYPES[".json"])

    # ── GET ──────────────────────────────────────
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/api/members":
            return self._json(self.server.members())
        if u.path == "/api/config":
            return self._json({"agent_ws": self.server.agent_ws, "mock_url": self.server.mock_url,
                               "default_now": DEFAULT_NOW, "sessions_dir": str(self.server.sessions_dir)})
        if u.path == "/api/calls":
            return self._proxy_calls(u.query)
        if u.path == "/api/history_presets":
            try:
                return self._json(self.server.history_presets())
            except (OSError, ValueError) as e:
                return self._json({"error": f"프리셋을 읽지 못했습니다: {e}"}, 500)
        return self._static(u.path)

    def _proxy_calls(self, query: str) -> None:
        sid = urllib.parse.parse_qs(query).get("session_id", [""])[0]
        url = f"{self.server.mock_url}/calls?" + urllib.parse.urlencode({"session_id": sid})
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                body = r.read()
            json.loads(body)
        except (urllib.error.URLError, OSError, ValueError) as e:
            return self._json({"error": f"mock API에 연결할 수 없습니다: {e}"}, 502)
        self._send(200, body, CONTENT_TYPES[".json"])

    def _static(self, path: str) -> None:
        rel = urllib.parse.unquote(path).lstrip("/") or "index.html"
        target = None
        for root in (self.server.static_dir, KIT_STATIC_DIR.resolve()):
            try:
                cand = (root / rel).resolve()
                cand.relative_to(root)
            except (ValueError, OSError):
                return self._json({"error": "not found"}, 404)
            if cand.is_file():
                target = cand
                break
        if target is None:
            return self._json({"error": "not found"}, 404)
        ctype = CONTENT_TYPES.get(target.suffix.lower()) or mimetypes.guess_type(target.name)[0] \
            or "application/octet-stream"
        self._send(200, target.read_bytes(), ctype)

    # ── POST ─────────────────────────────────────
    def _read_json(self, limit: int):
        """본문 JSON 객체. 실패하면 400을 보내고 None."""
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > limit:
            self.close_connection = True       # 읽지 않은 본문이 남으므로 연결을 닫는다
            self._json({"error": "본문 크기가 올바르지 않습니다"}, 400)
            return None
        raw = self.rfile.read(n)
        try:
            rec = json.loads(raw)
        except ValueError:
            self._json({"error": "JSON이 아닙니다"}, 400)
            return None
        if not isinstance(rec, dict):
            self._json({"error": "JSON 객체여야 합니다"}, 400)
            return None
        return rec

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/api/sessions":
            return self._save_session()
        if u.path != "/api/feedback":
            return self._json({"error": "not found"}, 404)
        rec = self._read_json(MAX_BODY)
        if rec is None:
            return
        rec["received_at"] = datetime.now().isoformat(timespec="seconds")
        path = self.server.feedback_dir / f"feedback-{self.server.date_fn()}.jsonl"
        with self.server.lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self._json({"ok": True, "file": str(path)})

    def _save_session(self) -> None:
        bundle = self._read_json(MAX_SESSION_BODY)
        if bundle is None:
            return
        try:
            res = feedback_store.write_session(bundle, self.server.sessions_dir)
        except feedback_store.BundleError as e:
            return self._json({"error": str(e)}, 400)
        except OSError as e:
            return self._json({"error": f"저장 실패: {e}"}, 500)
        self._json({"ok": True, **res})


def create_server(port: int = 8080, *, mock_url: str = DEFAULT_MOCK_URL, agent_ws: str = DEFAULT_AGENT_WS,
                  data_path: Path | str = DATA_PATH, static_dir: Path | str = STATIC_DIR,
                  feedback_dir: Path | str = FEEDBACK_DIR, presets_path: Path | str = PRESETS_PATH,
                  sessions_dir: Path | str = SESSIONS_DIR,
                  date_fn: Callable[[], str] = lambda: datetime.now().strftime("%Y%m%d")) -> DemoServer:
    """127.0.0.1에 바인딩한 서버를 만든다(serve_forever는 호출자가). port=0이면 임의 포트."""
    return DemoServer((HOST, port), mock_url=mock_url, agent_ws=agent_ws, data_path=Path(data_path),
                      static_dir=Path(static_dir), feedback_dir=Path(feedback_dir), date_fn=date_fn,
                      presets_path=Path(presets_path), sessions_dir=Path(sessions_dir))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Directon 체험 데모 서버")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--agent-ws", default=DEFAULT_AGENT_WS)
    ap.add_argument("--mock-url", default=DEFAULT_MOCK_URL)
    a = ap.parse_args(argv)
    srv = create_server(a.port, mock_url=a.mock_url, agent_ws=a.agent_ws)
    print(f"Directon demo on http://{HOST}:{srv.server_address[1]} "
          f"(agent {a.agent_ws}, mock {a.mock_url})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
