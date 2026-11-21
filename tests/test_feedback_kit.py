"""AG-T53 이식용 피드백 키트(feedback_kit/): 서버 API, 독립성(우리 코드 import 없음), 다른 폴더로 복사해 실행.

네트워크 금지: 127.0.0.1 임의 포트만 쓴다.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from feedback_kit import server as kit_server
from test_demo_session_store import _bundle

KIT = Path(__file__).resolve().parents[1] / "feedback_kit"


@pytest.fixture
def kit(tmp_path):
    srv = kit_server.create_server(port=0, agent_ws="ws://127.0.0.1:9001", out_dir=tmp_path / "out",
                                   tools_url="http://127.0.0.1:8100", agent_label="팀원A v3",
                                   now="2026-11-25 (수) 10:00")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", tmp_path / "out", srv
    srv.shutdown()
    srv.server_close()


def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), e.read()


def _post(url, body: bytes):
    req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_binds_loopback(kit):
    assert kit[2].server_address[0] == "127.0.0.1"


def test_config(kit):
    base, out, _ = kit
    code, ctype, body = _get(base + "/api/config")
    cfg = json.loads(body)
    assert code == 200 and "json" in ctype
    assert cfg == {"agent_ws": "ws://127.0.0.1:9001", "tools_url": "http://127.0.0.1:8100",
                   "agent_label": "팀원A v3", "now": "2026-11-25 (수) 10:00", "out_dir": str(out),
                   "schema": "directon.demo_session/v1"}


def test_static_files(kit):
    base, _, _ = kit
    code, ctype, body = _get(base + "/")
    assert code == 200 and ctype.startswith("text/html")
    html = body.decode("utf-8")
    for src in ["session-record.js", "app.js", "style.css"]:
        assert src in html
    for name in ["session-record.js", "mic-worklet.js", "app.js", "style.css"]:
        code, ctype, _ = _get(base + "/" + name)
        assert code == 200, name
    assert _get(base + "/app.js")[1].startswith("text/javascript")


@pytest.mark.parametrize("path", ["/../store.py", "/..%2Fstore.py", "/%2e%2e/server.py", "/nope.js"])
def test_static_rejects_outside_and_missing(kit, path):
    base, _, _ = kit
    assert _get(base + path)[0] == 404


def test_post_sessions_saves(kit):
    base, out, _ = kit
    code, res = _post(base + "/api/sessions", json.dumps(_bundle(sid="KIT-1")).encode())
    assert code == 200 and res["ok"] is True
    assert (out / "KIT-1" / "mix.wav").is_file()
    assert (out / "KIT-1" / "cases.jsonl").is_file()
    assert "turns/01_customer.wav" in res["files"]


def test_post_sessions_rejects(kit):
    base, out, _ = kit
    b = _bundle()
    b["schema"] = "x"
    assert _post(base + "/api/sessions", json.dumps(b).encode())[0] == 400
    assert _post(base + "/api/sessions", b"[]")[0] == 400
    assert _post(base + "/api/other", b"{}")[0] == 404
    assert not out.exists() or list(out.iterdir()) == []


def test_kit_does_not_import_project_code():
    bad = re.compile(r"^\s*(from|import)\s+(agent|demo|mock_api|eval|bench)\b", re.M)
    for p in KIT.rglob("*.py"):
        assert not bad.search(p.read_text(encoding="utf-8")), p
    for p in (KIT / "static").glob("*"):
        txt = p.read_text(encoding="utf-8")
        assert "/api/members" not in txt and "/api/calls" not in txt, p     # 데모 전용 API에 기대지 않음


def test_kit_runs_from_copy(tmp_path):
    """다른 팀원 레포에 폴더만 복사한 상황: 그 폴더에서 서버 --help, 내려받은 JSON 풀기."""
    repo = tmp_path / "other_repo"
    shutil.copytree(KIT, repo / "feedback_kit", ignore=shutil.ignore_patterns("__pycache__"))
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
    env["PYTHONIOENCODING"] = "utf-8"
    r = subprocess.run([sys.executable, "-s", "-m", "feedback_kit", "--help"], cwd=repo, env=env,
                       capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert r.returncode == 0, r.stderr
    assert "--agent-ws" in r.stdout
    src = repo / "KIT-2.json"
    src.write_text(json.dumps(_bundle(sid="KIT-2"), ensure_ascii=False), encoding="utf-8")
    r = subprocess.run([sys.executable, "-s", "-c",
                        "import feedback_kit.store as s, sys; print(s.__file__); sys.exit(s.main(['KIT-2.json']))"],
                       cwd=repo, env=env, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert r.returncode == 0, r.stderr
    assert str(repo / "feedback_kit") in r.stdout                       # 복사본을 썼다
    assert (repo / "feedback_sessions" / "KIT-2" / "session.json").is_file()


def test_readme_exists():
    txt = (KIT / "README.md").read_text(encoding="utf-8")
    for must in ["python -m feedback_kit", "--agent-ws", "turn.end", "debug.stt", "feedback_sessions"]:
        assert must in txt


def test_session_start_sends_debug_true_by_default_and_extra_overrides():
    """AG-T56: 인식 텍스트·함수 호출을 보려면 debug가 켜져 있어야 한다. 추가 필드가 기본값보다 뒤에 합쳐진다."""
    js = (KIT / "static" / "app.js").read_text(encoding="utf-8")
    start = js.index("const msg = Object.assign({")
    block = js[start:js.index("msg.type = \"session.start\"", start)]
    assert "debug: true," in block
    assert block.rstrip().endswith("}, extra);")          # extra가 마지막 인수 → {"debug": false}로 끌 수 있다
    assert '{"debug": false}' in (KIT / "README.md").read_text(encoding="utf-8")


def test_turn_end_stops_dropping_audio_so_next_opener_plays():
    """AG-T61: 끼어든 뒤 버리기(dropAudio)는 그 턴의 turn.end에서 끝난다. 서버는 다음 턴 첫마디를
    debug.turn decided보다 먼저 보내므로, decided에서만 풀면 끼어든 다음 턴의 첫마디가 버려진다."""
    for path in [KIT / "static" / "app.js", KIT.parent / "demo" / "static" / "app.js"]:
        js = path.read_text(encoding="utf-8")
        block = js.split('case "turn.end": {', 1)[1].split("break;", 1)[0]
        assert "c.dropAudio = false;" in block, path

