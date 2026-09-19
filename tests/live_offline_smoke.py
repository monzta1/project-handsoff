import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import sys
import urllib.request
from pathlib import Path

try:
    import websockets
    from websockets.sync import client as websocket_client
except ImportError:
    websocket_client = None

ROOT = Path(__file__).resolve().parent.parent
HANDSOFF = str(Path.home() / ".local" / "bin" / "handsoff")
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
# The deployed engine is the subject, like every other live smoke. `--tree`
# serves the dashboard from this checkout instead, for a check before the
# release that carries the change is installed.
USE_TREE = "--tree" in sys.argv


def dashboard_argv(project: Path, port: int, *extra: str) -> list[str]:
    if USE_TREE:
        return [sys.executable, str(ROOT / "bin" / "handsoff_dashboard.py"), "--root", str(project), "--port", str(port), "--no-open", *extra]
    return [HANDSOFF, "dashboard", "--root", str(project), "--port", str(port), "--no-open", *extra]

def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]

def cli(*args, cwd):
    return subprocess.run([HANDSOFF, *args], cwd=cwd, capture_output=True, text=True, check=True)

def evaluate(ws, expression):
    evaluate.counter += 1
    ws.send(json.dumps({"id": evaluate.counter, "method": "Runtime.evaluate", "params": {"expression": expression, "returnByValue": True}}))
    while True:
        message = json.loads(ws.recv())
        if message.get("id") == evaluate.counter:
            return message["result"]["result"].get("value")
evaluate.counter = 0

if not os.path.exists(CHROME) or websocket_client is None:
    print("LIVE_OFFLINE_BLOCKED")
    raise SystemExit(1)

server = chrome = None
with tempfile.TemporaryDirectory(prefix="handsoff-live-offline-") as tmp:
    project = Path(tmp) / "project"
    cli("init", str(project), cwd=ROOT)
    cli("supervisor", "init", "Offline smoke", "--item", "#151", cwd=project)
    try:
        port = free_port()
        server = subprocess.Popen(dashboard_argv(project, port, "--owned-by-run"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        base = f"http://127.0.0.1:{port}"
        for _ in range(40):
            try:
                urllib.request.urlopen(base + "/api/dashboard", timeout=1)
                break
            except Exception:
                time.sleep(.25)
        debug_port = free_port()
        chrome = subprocess.Popen([CHROME, "--headless=new", f"--remote-debugging-port={debug_port}", f"--user-data-dir={Path(tmp) / 'chrome'}", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(40):
            try:
                targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{debug_port}/json"))
                target = next(item for item in targets if item.get("type") == "page")
                break
            except Exception:
                time.sleep(.25)
        with websocket_client.connect(target["webSocketDebuggerUrl"]) as ws:
            evaluate(ws, f"location.href='{base}/'")
            evaluate(ws, "document.body.getAttribute('class')")
            time.sleep(4)
            assert "is-offline" not in (evaluate(ws, "document.body.className") or "")
            assert evaluate(ws, "document.getAnimations().length") > 0
            server.send_signal(signal.SIGTERM)
            for _ in range(10):
                time.sleep(1)
                if "is-offline" in (evaluate(ws, "document.body.className") or ""):
                    break
            assert "is-offline" in evaluate(ws, "document.body.className")
            assert "DASHBOARD OFFLINE since" in evaluate(ws, "document.querySelector('#offline-banner').textContent")
            assert evaluate(ws, "document.getAnimations().length") == 0
            assert evaluate(ws, "document.querySelectorAll('.phase-node.active').length") == 0
        status = json.loads((project / "handsoff-status.json").read_text())
        cli("supervisor", "run-close", "--by", "pilot", "--reason", "smoke complete", "--expected-updated-at", status["updated_at"], cwd=project)
        port = free_port()
        server = subprocess.Popen(dashboard_argv(project, port), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1)
        with websocket_client.connect(target["webSocketDebuggerUrl"]) as ws:
            evaluate(ws, f"location.href='http://127.0.0.1:{port}/'")
            time.sleep(1)
            assert evaluate(ws, "document.body.className").find("is-closed") >= 0
            assert evaluate(ws, "document.querySelectorAll('.phase-node.active').length") == 0
            assert evaluate(ws, "document.querySelectorAll('.phase-node.closed').length") == 1
            assert "smoke complete" in evaluate(ws, "document.querySelector('#supervisor-headline').textContent")
        print("LIVE_OFFLINE_OK")
    finally:
        for process in (server, chrome):
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
