import argparse
import json
import logging
import os
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import config
import inbox
import procs

BOT_DIR = Path(__file__).resolve().parent
PAGE = BOT_DIR / "dashboard.html"
LOG_TAIL = 300
POLL_SECONDS = 1.0
HEARTBEAT_SECONDS = 15
STALE_ACTIVITY_GRACE = timedelta(minutes=5)
MAX_BODY = 4096
TOKEN_PLACEHOLDER = b"__PR_TOKEN__"
DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
BREAKAWAY = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
SCHTASKS = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "schtasks.exe"


def parse_iso(value):
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def activity_alive(entry, run, cfg):
    activity = entry.get("activity")
    if not activity:
        return False
    started = parse_iso(activity.get("started_at"))
    finished = parse_iso((run or {}).get("finished_at"))
    if started and finished and finished >= started:
        return False
    if run and not run.get("finished_at") and run.get("alive") is False:
        return False
    limit = timedelta(minutes=cfg.get("claude_timeout_minutes", 30)) + STALE_ACTIVITY_GRACE
    return bool(started) and datetime.now(timezone.utc) - started < limit


def phase(entry, run=None, cfg=None):
    if activity_alive(entry, run, cfg or {}):
        return "reviewing"
    status = entry["status"]
    if status == "ERROR":
        return "error"
    if status == "SKIPPED":
        return "skipped"
    head = entry.get("head_sha")
    if status == "APPROVED":
        return "approved" if entry.get("approved_sha") == head else "queued"
    if status == "NEW" or head != entry.get("reviewed_sha"):
        return "queued"
    gate = entry.get("gate") or {}
    if gate.get("sha") == head and gate.get("open_threads"):
        return "waiting_author"
    return "waiting_ci"


def review_view(pr_url, review):
    rid = review.get("id")
    return {
        "kind": review.get("kind"), "sha": (review.get("sha") or "")[:7], "at": review.get("at"),
        "counts": review.get("counts"), "cost_usd": review.get("cost_usd") or 0,
        "url": f"{pr_url}#pullrequestreview-{rid}" if rid and pr_url else None,
    }


def cost_today(entries, today):
    total = 0.0
    for entry in entries:
        for review in entry.get("reviews", []):
            at = parse_iso(review.get("at"))
            if at and at.astimezone().date() == today:
                total += review.get("cost_usd") or 0
    return round(total, 2)


def state_path(cfg):
    return BOT_DIR / ("state.dry-run.json" if cfg.get("mode") == "dry-run" else "state.json")


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def snapshot(data, cfg, config_error=None, today=None, pending=None, run_alive=None):
    data = data or {"prs": {}}
    run = data.get("run")
    if run is not None:
        run = {**run, "alive": run_alive if not run.get("finished_at") else None}
    prs = []
    for key, e in data.get("prs", {}).items():
        url = e.get("url")
        alive = activity_alive(e, run, cfg)
        prs.append({
            "key": key, "url": url, "title": e.get("title"), "author": e.get("author"), "status": e["status"],
            "phase": phase(e, run, cfg), "note": e.get("note"), "note_at": e.get("note_at"),
            "head": (e.get("head_sha") or "")[:7], "reviewed": (e.get("reviewed_sha") or "")[:7],
            "activity": e.get("activity") if alive else None, "gate": e.get("gate"), "labels": e.get("labels"),
            "reviews": [review_view(url, r) for r in e.get("reviews", [])],
            "discovered_at": e.get("discovered_at"), "approved_at": e.get("approved_at"),
            "skip_reason": e.get("skip_reason"),
            "last_error": e.get("last_error") if e.get("failures") or e["status"] == "ERROR" else None,
        })
    done = [
        {
            "key": d.get("key"), "url": d.get("url"), "title": d.get("title"), "author": d.get("author"),
            "reason": d.get("reason"), "status": d.get("status"), "left_at": d.get("left_at"),
            "reviews": len(d.get("reviews", [])),
        }
        for d in data.get("done", [])
    ]
    today = today or datetime.now().astimezone().date()
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": cfg.get("mode"), "login": cfg.get("login"), "org": cfg.get("org"),
        "interval_minutes": cfg.get("schedule_interval_minutes"), "config_error": config_error,
        "run": run, "prs": prs, "done": done, "inbox": pending or [], "claude_down": data.get("claude_down"),
        "cost_today": cost_today(list(data.get("prs", {}).values()) + data.get("done", []), today),
    }


def today_log():
    return BOT_DIR / "logs" / f"{datetime.now():%Y-%m-%d}.log"


def read_new_lines(path, offset):
    try:
        size = path.stat().st_size
    except OSError:
        return [], 0, False
    reset = size < offset
    if reset:
        offset = 0
    if size == offset:
        return [], offset, reset
    with path.open("rb") as f:
        f.seek(offset)
        chunk = f.read()
    cut = chunk.rfind(b"\n") + 1
    if cut == 0:
        return [], offset, reset
    lines = chunk[:cut].decode("utf-8", errors="replace").splitlines()
    return lines, offset + cut, reset


def tail_lines(path, n=LOG_TAIL):
    lines, offset, _ = read_new_lines(path, 0)
    return lines[-n:], offset


def allowed_host(host, port):
    return host in (f"127.0.0.1:{port}", f"localhost:{port}")


def allowed_origin(origin, port):
    return origin in (f"http://127.0.0.1:{port}", f"http://localhost:{port}")


def spawn_bot(task_name=None):
    if sys.platform == "win32" and task_name:
        try:
            result = subprocess.run(
                [str(SCHTASKS), "/Run", "/TN", task_name], capture_output=True, creationflags=NO_WINDOW, timeout=15,
            )
            if result.returncode == 0:
                return "task"
        except (OSError, subprocess.TimeoutExpired):
            pass
    args = dict(
        cwd=BOT_DIR, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
    )
    command = [sys.executable, str(BOT_DIR / "pr_bot.py")]
    try:
        subprocess.Popen(command, creationflags=DETACHED | BREAKAWAY, **args)
    except OSError:
        subprocess.Popen(command, creationflags=DETACHED, **args)
    return "process"


def request_review(source, url, full=False):
    cfg, error = source.load()
    if error:
        return 409, {"error": f"config inválida: {error}"}
    if cfg.get("mode") == "off":
        return 409, {"error": "o bot está pausado (BOT_MODE=off)"}
    parsed = inbox.parse_pr_url(url)
    if not parsed:
        return 400, {"error": "use a URL do PR, no formato https://github.com/<org>/<repo>/pull/<número>"}
    owner, _, _ = parsed
    if owner.lower() != str(cfg.get("org", "")).lower():
        return 400, {"error": f"só PRs da org {cfg.get('org')}"}
    item = inbox.enqueue(BOT_DIR, url, full=full)
    spawn_bot(cfg.get("schedule_task_name"))
    what = "review completa forçada de" if full else "review de"
    return 202, {"key": item["key"], "full": item["full"], "message": f"{what} {item['key']} pedida e o bot foi disparado"}


def sse(event, payload):
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {body}\n\n".encode("utf-8")


class Source:
    def __init__(self, env_file=None):
        self.env_file = env_file

    def load(self):
        try:
            cfg, error = config.load_config(self.env_file), None
        except config.ConfigError as e:
            cfg, error = {"mode": "off"}, str(e)
        return cfg, error

    def signature(self):
        paths = [BOT_DIR / "state.json", BOT_DIR / "state.dry-run.json", BOT_DIR / ".env", BOT_DIR / "state.lock"]
        out = []
        for p in paths:
            try:
                out.append(p.stat().st_mtime_ns)
            except OSError:
                out.append(None)
        return tuple(out) + inbox.signature(BOT_DIR)

    def snapshot(self):
        cfg, error = self.load()
        owner = procs.lock_owner(BOT_DIR / "state.lock")
        return snapshot(
            read_json(state_path(cfg)), cfg, error, pending=inbox.pending(BOT_DIR),
            run_alive=owner is not None and procs.pid_alive(owner),
        )


CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; "
    "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


class Handler(BaseHTTPRequestHandler):
    server_version = "pr-autoapprove"

    def log_message(self, fmt, *args):
        pass

    def guard(self):
        if not allowed_host(self.headers.get("Host", ""), self.server.server_address[1]):
            self.send_error(403)
            return False
        return True

    def send_bytes(self, status, content_type, body):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", CSP)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self.guard():
            return
        path = self.path.split("?", 1)[0]
        if path == "/":
            page = PAGE.read_bytes().replace(TOKEN_PLACEHOLDER, self.server.token.encode("ascii"))
            return self.send_bytes(200, "text/html; charset=utf-8", page)
        if path == "/api/state":
            body = json.dumps(self.server.source.snapshot(), ensure_ascii=False).encode("utf-8")
            return self.send_bytes(200, "application/json; charset=utf-8", body)
        if path == "/api/events":
            return self.stream()
        self.send_error(404)

    def send_json(self, status, payload):
        self.send_bytes(status, "application/json; charset=utf-8", json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def do_POST(self):
        if not self.guard():
            return
        port = self.server.server_address[1]
        if not allowed_origin(self.headers.get("Origin", ""), port):
            return self.send_json(403, {"error": "origem não permitida"})
        if not secrets.compare_digest(self.headers.get("X-PR-Token", ""), self.server.token):
            return self.send_json(403, {"error": "token inválido; recarregue o painel"})
        if self.path.split("?", 1)[0] != "/api/review":
            return self.send_json(404, {"error": "não encontrado"})
        if "application/json" not in self.headers.get("Content-Type", ""):
            return self.send_json(415, {"error": "envie JSON"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            return self.send_json(413, {"error": "corpo grande demais"})
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self.send_json(400, {"error": "JSON inválido"})
        url = body.get("url") if isinstance(body, dict) else None
        full = body.get("full") is True if isinstance(body, dict) else False
        status, payload = request_review(self.server.source, url if isinstance(url, str) else "", full=full)
        self.server.log.info("pedido manual %s -> %s %s", url, status, payload)
        return self.send_json(status, payload)

    def stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        source = self.server.source
        log_path = today_log()
        lines, offset = tail_lines(log_path)
        signature = source.signature()
        last_beat = time.monotonic()
        try:
            self.wfile.write(sse("state", source.snapshot()))
            self.wfile.write(sse("log-reset", lines))
            self.wfile.flush()
            while not self.server.stopping:
                time.sleep(POLL_SECONDS)
                current = source.signature()
                if current != signature:
                    signature = current
                    self.wfile.write(sse("state", source.snapshot()))
                if today_log() != log_path:
                    log_path, offset = today_log(), 0
                    self.wfile.write(sse("log-reset", []))
                new, offset, reset = read_new_lines(log_path, offset)
                if reset:
                    self.wfile.write(sse("log-reset", []))
                if new:
                    self.wfile.write(sse("log", new))
                if time.monotonic() - last_beat >= HEARTBEAT_SECONDS:
                    self.wfile.write(b": ping\n\n")
                    last_beat = time.monotonic()
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return


class Server(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        self.log.exception("erro atendendo %s", client_address)


def make_server(port, source=None, log=None):
    server = Server(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.source = source or Source()
    server.stopping = False
    server.token = secrets.token_urlsafe(32)
    server.log = log or logging.getLogger("pr-dashboard")
    return server


def setup_logging():
    (BOT_DIR / "logs").mkdir(exist_ok=True)
    log = logging.getLogger("pr-dashboard")
    log.setLevel(logging.INFO)
    handler = logging.FileHandler(BOT_DIR / "logs" / "dashboard.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    return log


def main():
    parser = argparse.ArgumentParser(description="Painel ao vivo do pr-autoapprove")
    parser.add_argument("--port", type=int, help="porta local (padrão: DASHBOARD_PORT do .env)")
    parser.add_argument("--open", action="store_true", help="abre o painel no navegador")
    args = parser.parse_args()
    log = setup_logging()
    sys.excepthook = lambda kind, value, tb: log.critical("erro não tratado", exc_info=(kind, value, tb))
    threading.excepthook = lambda hook: log.critical("erro não tratado na thread %s", hook.thread, exc_info=(hook.exc_type, hook.exc_value, hook.exc_traceback))
    cfg, _ = Source().load()
    port = args.port or cfg.get("dashboard_port") or 8765
    try:
        server = make_server(port, log=log)
    except OSError as e:
        log.error("não consegui abrir 127.0.0.1:%s: %s", port, e)
        return 1
    url = f"http://127.0.0.1:{port}/"
    log.info("painel em %s", url)
    if sys.stdout:
        print(f"Painel em {url}")
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    except Exception:
        log.exception("o servidor do painel caiu")
        raise
    finally:
        server.stopping = True
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
