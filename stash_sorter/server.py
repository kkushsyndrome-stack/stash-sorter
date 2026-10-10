"""Local web GUI. Binds to 127.0.0.1 only; every API request must carry the per-session token."""

import json
import re
import secrets
import threading
import time
import traceback
import webbrowser
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

from . import __version__
from .apply import ApplyError
from .clock import ClockError
from .launcher import LaunchError
from .rules import RulesError
from .artmod import ArtModError

WEB_DIR = Path(__file__).parent / "web"
STATIC = {"app.js": "text/javascript; charset=utf-8", "app.css": "text/css; charset=utf-8", "tz-art.png": "image/png"}
_ART_KEY = re.compile(r"^[a-z0-9_/\-]+$")


def serve(session, port=0, open_browser=True):
    token = secrets.token_urlsafe(24)
    lock = threading.Lock()  # one operation at a time: the world model is not thread-safe

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _send(self, code, body, ctype="application/json", cache=False):
            data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "max-age=86400" if cache else "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def _host_ok(self):
            host = (self.headers.get("Host") or "").split(":")[0]
            return host in ("127.0.0.1", "localhost")

        def _authorized(self):
            return self._host_ok() and secrets.compare_digest(self.headers.get("X-Token", ""), token)

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, {"error": "forbidden"})
            url = urlparse(self.path)
            path = url.path
            if path in ("/", "/index.html"):
                html = (WEB_DIR / "index.html").read_text(encoding="utf-8").replace("__TOKEN__", token).replace("__VERSION__", __version__)
                return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            if path.startswith("/static/") and path[8:] in STATIC:
                return self._send(200, (WEB_DIR / path[8:]).read_bytes(), STATIC[path[8:]])
            if path.startswith("/art/") and path.endswith(".png"):
                # images can't send headers; they only expose read-only game artwork, so no token is needed
                key = unquote(path[5:-4])
                hd = parse_qs(url.query).get("hd") == ["1"]
                if not _ART_KEY.match(key):
                    return self._send(404, {"error": "not found"})
                try:
                    png = session.art.png(key, low=not hd)
                except (OSError, ValueError):
                    png = None
                return self._send(200, png, "image/png", cache=True) if png else self._send(404, {"error": "no art"})
            if not path.startswith("/api/"):
                return self._send(404, {"error": "not found"})
            if not self._authorized():
                return self._send(403, {"error": "bad token"})
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            crafter = int(q["crafter"]) if q.get("crafter", "").isdigit() else None
            try:
                with lock:
                    if crafter:
                        session.crafter_level = crafter
                    routes = {
                        "/api/state": lambda: session.state(),
                        "/api/stash": lambda: session.stash_json(q.get("file")),
                        "/api/character": lambda: session.character_json(q["name"]),
                        "/api/assess": lambda: session.assess_rows(crafter, q.get("scope", "stash")),
                        "/api/items": lambda: session.all_items(),
                        "/api/collection": lambda: session.collection(),
                        "/api/rules": lambda: session.rules_json(),
                        "/api/duplicates": lambda: session.duplicates(),
                        "/api/empty_mules": lambda: session.empty_mules(),
                        "/api/session": lambda: session.session_state(),
                        "/api/tz": lambda: session.tz_state(),
                        "/api/launch": lambda: session.launch_state(),
                        "/api/seeds": lambda: session.seeds_state(),
                        "/api/seedrun": lambda: session.seedrun_state(),
                        "/api/session/load": lambda: session.session_load(q.get("id", "")),
                        "/api/backups": lambda: session.backups(),
                        "/api/artmod": lambda: session.artmod_state(),
                        "/api/mule_defaults": lambda: session.mule_defaults(int(q.get("level") or 1),
                                                                            q.get("hint", "1") == "1"),
                    }
                    if path in routes:
                        return self._send(200, routes[path]())
            except Exception as e:
                traceback.print_exc()
                return self._send(500, {"error": str(e)})
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._authorized():
                return self._send(403, {"error": "bad token"})
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            path = urlparse(self.path).path
            try:
                with lock:
                    def apply():
                        logs = []
                        res = session.apply(int(body.get("plan_id", -1)), log=logs.append)
                        res["log_lines"] = logs
                        for k in ("moves", "stacked", "files_after", "written"):
                            res.pop(k, None)
                        return res

                    def reload():
                        session.reload()
                        return {"ok": True}

                    routes = {
                        "/api/reload": reload,
                        "/api/plan": lambda: session.make_plan(body),
                        "/api/seeds/save": lambda: session.seeds_save(body),
                        "/api/seeds/delete": lambda: session.seeds_delete(body.get("id", "")),
                        "/api/seedrun/start": lambda: session.seedrun_start(body),
                        "/api/seedrun/again": lambda: session.seedrun_again(),
                        "/api/seedrun/cancel": lambda: session.seedrun_cancel(),
                        "/api/seedrun/dismiss": lambda: session.seedrun_dismiss(),
                        "/api/launch/preview": lambda: session.launch_preview(body),
                        "/api/launch/save": lambda: session.launch_save(body),
                        "/api/launch/restore": lambda: session.launch_restore(body.get("backup", "")),
                        "/api/launch/start": lambda: session.launch_game(),
                        "/api/tz/update": lambda: session.tz_update(body),
                        "/api/tz/set": lambda: session.tz_set(body.get("zone", "")),
                        "/api/tz/revert": lambda: session.tz_revert(),
                        "/api/tz/refresh": lambda: session.tz_refresh(),
                        "/api/backup_now": lambda: session.backup_now(),
                        "/api/session/start": lambda: session.session_start(),
                        "/api/session/end": lambda: session.session_end(),
                        "/api/session/delete": lambda: session.session_delete(body.get("id", "")),
                        "/api/plan_delete_items": lambda: session.plan_delete_items(body.get("keys") or []),
                        "/api/plan_move_item": lambda: session.plan_move_item(
                            body.get("key", ""), body.get("destination", ""), body.get("page", 5)),
                        "/api/plan_delete_mules": lambda: session.plan_delete_mules(body.get("names") or []),
                        "/api/plan_restyle": lambda: session.plan_restyle(body.get("changes") or []),
                        "/api/plan_rename_char": lambda: session.plan_rename_char(body.get("name", ""),
                                                                                  body.get("new", "")),
                        "/api/apply": apply,
                        "/api/restore": lambda: session.restore(body.get("file", "")),
                        "/api/undo": lambda: session.undo(body.get("log", "")),
                        "/api/recover": lambda: session.recover(),
                        "/api/rules": lambda: session.save_rules(body.get("rules")),
                        "/api/rules/reset": lambda: session.reset_rules(),
                        "/api/artmod/set_art": lambda: session.artmod_set_art(body),
                        "/api/artmod/reset_art": lambda: session.artmod_reset_art(body),
                        "/api/artmod/set_look": lambda: session.artmod_set_look(body),
                        "/api/artmod/reset_look": lambda: session.artmod_reset_look(body),
                        "/api/artmod/restore": lambda: session.artmod_restore(body),
                        "/api/artmod/forget": lambda: session.artmod_forget(),
                    }
                    if path in routes:
                        return self._send(200, routes[path]())
            except (ApplyError, RulesError, ValueError, ClockError, LaunchError, ArtModError) as e:
                return self._send(409, {"error": str(e)})
            except Exception as e:
                traceback.print_exc()
                return self._send(500, {"error": str(e)})
            return self._send(404, {"error": "not found"})

    def watch():  # session tracker (read-only) and an unfinished seed run, every couple of seconds
        while True:
            time.sleep(2)
            try:
                with lock:
                    session.tracker.poll()
                    if session.seed_run.active:  # keeps going with the browser closed
                        session.seed_run.tick()
            except Exception:
                traceback.print_exc()

    threading.Thread(target=watch, daemon=True).start()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print(f"Horadric Toolkit {__version__} is running at {url}  (close this window or press Ctrl+C to stop)")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    _on_console_close(lambda: session.revert_clock_on_exit())
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()
        if session.clock.shifted and session.clock.settings.get("auto_revert", True):
            print("Restoring the real time (Windows will ask for permission)...")
            ok = session.revert_clock_on_exit()
            print("Done." if ok else "The clock couldn't be restored - fix it in Windows settings.")


_handler_ref = []


def _on_console_close(callback):
    """Run `callback` when the console window is closed (best effort: Windows allows a few seconds)."""
    try:
        import ctypes
        handler_type = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)

        def handler(event):
            if event in (2, 5, 6):  # close, logoff, shutdown
                try:
                    callback()
                except Exception:
                    pass
                return 1
            return 0
        _handler_ref.append(handler_type(handler))  # keep it alive
        ctypes.windll.kernel32.SetConsoleCtrlHandler(_handler_ref[-1], 1)
    except (AttributeError, OSError):
        pass
