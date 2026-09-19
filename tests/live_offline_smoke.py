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


def fleet_argv(registry: Path, port: int) -> list[str]:
    """A Fleet server on a private registry: this checkout's module under
    `--tree`, else the installed engine's module run by its own interpreter
    (so engine_root resolves to the installation's share directory)."""
    script = ("import sys; %simport handsoff_fleet as fleet; from pathlib import Path; "
              "fleet.FleetServer(('127.0.0.1', %d), Path(%r)).serve_forever()")
    if USE_TREE:
        return [sys.executable, "-c", script % ("sys.path.insert(0, %r); " % str(ROOT / "bin"), port, str(registry))]
    venv_python = Path(HANDSOFF).resolve().parent / "python"
    return [str(venv_python), "-c", script % ("", port, str(registry))]


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

# Every process this smoke starts is registered here and stopped in the
# finally block, whichever one a local name happens to point at.
started: list[subprocess.Popen] = []


def start(argv: list[str]) -> subprocess.Popen:
    process = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    started.append(process)
    return process


def stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


with tempfile.TemporaryDirectory(prefix="handsoff-live-offline-") as tmp:
    project = Path(tmp) / "project"
    cli("init", str(project), cwd=ROOT)
    cli("supervisor", "init", "Offline smoke", "--item", "#151", cwd=project)
    try:
        port = free_port()
        server = start(dashboard_argv(project, port, "--owned-by-run"))
        base = f"http://127.0.0.1:{port}"
        for _ in range(40):
            try:
                urllib.request.urlopen(base + "/api/dashboard", timeout=1)
                break
            except Exception:
                time.sleep(.25)
        debug_port = free_port()
        chrome = start([CHROME, "--headless=new", f"--remote-debugging-port={debug_port}", f"--user-data-dir={Path(tmp) / 'chrome'}", "about:blank"])
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
        closed_server = start(dashboard_argv(project, port))
        time.sleep(1)
        with websocket_client.connect(target["webSocketDebuggerUrl"]) as ws:
            evaluate(ws, f"location.href='http://127.0.0.1:{port}/'")
            time.sleep(1)
            assert evaluate(ws, "document.body.className").find("is-closed") >= 0
            assert evaluate(ws, "document.querySelectorAll('.phase-node.active').length") == 0
            assert evaluate(ws, "document.querySelectorAll('.phase-node.closed').length") == 1
            assert "smoke complete" in evaluate(ws, "document.querySelector('#supervisor-headline').textContent")
        # Fleet's own page: serve a Fleet with a private registry, watch it,
        # stop it, and expect the same offline rendering within 10 seconds.
        registry = Path(tmp) / "fleet.json"
        registry.write_text(json.dumps({"schema": 1, "projects": [{"root": str(project), "registered_at": "2026-09-19T00:00:00+00:00"}]}))
        port = free_port()
        stop(closed_server)
        fleet_server = start(fleet_argv(registry, port))
        base = f"http://127.0.0.1:{port}"
        for _ in range(40):
            try:
                urllib.request.urlopen(base + "/api/fleet", timeout=1)
                break
            except Exception:
                time.sleep(.25)
        with websocket_client.connect(target["webSocketDebuggerUrl"]) as ws:
            evaluate(ws, f"location.href='{base}/'")
            time.sleep(4)
            assert "is-offline" not in (evaluate(ws, "document.body.className") or ""), "fleet read offline while served"
            cards = evaluate(ws, "document.querySelectorAll('.project').length")
            assert cards >= 1, "fleet rendered no card: " + str(evaluate(ws, "JSON.stringify({href: location.href, title: document.title, body: document.body.className, text: document.body.innerText.slice(0, 200)})"))
            fleet_server.send_signal(signal.SIGTERM)
            for _ in range(10):
                time.sleep(1)
                if "is-offline" in (evaluate(ws, "document.body.className") or ""):
                    break
            assert "is-offline" in evaluate(ws, "document.body.className"), "fleet never noticed its dead server"
            assert "DASHBOARD OFFLINE since" in evaluate(ws, "document.querySelector('#offline-banner').textContent")
            remaining = evaluate(ws, "JSON.stringify(document.getAnimations().map(a => (a.effect && a.effect.target ? a.effect.target.tagName + '.' + a.effect.target.className : '?') + ':' + (a.animationName || a.transitionProperty || a.constructor.name)))")
            assert evaluate(ws, "document.getAnimations().length") == 0, f"fleet animations still running: {remaining}"
        print("LIVE_OFFLINE_OK")
    finally:
        for process in reversed(started):
            stop(process)
