"""Sonorus dentro Passaporta: la stessa interfaccia servita su http://127.0.0.1:8795, solo in locale.

L'interfaccia chiama `api.metodo(...)` come nella finestra pywebview; qui un piccolo shim JavaScript
trasforma ogni chiamata in POST /api/<metodo> con gli argomenti in JSON. È lo stesso processo della
finestra: un solo motore, una sola GPU, la diretta continua anche se chiudi la pagina.

Sicurezza: ascolta solo su 127.0.0.1, accetta solo Host locali (niente DNS rebinding) e vuole
l'intestazione X-Sonorus: un'altra pagina web non può mandarla senza un preflight CORS, che qui non passa.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = 8795
HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
# Solo pagine locali (Passaporta su qualsiasi porta) possono incorporarla
FRAME = "frame-ancestors 'self' http://127.0.0.1:* http://localhost:*"

SHIM = """<script>
window.pywebview = { api: new Proxy({}, { get: (_, name) => (...args) =>
  fetch('/api/' + name, { method: 'POST', headers: { 'X-Sonorus': '1', 'Content-Type': 'application/json' }, body: JSON.stringify(args) })
    .then(r => r.ok ? r.json() : r.json().then(e => { throw new Error(e.error || ('Errore ' + r.status)); })) }) };
window.SONORUS_WEB = true;
document.addEventListener('DOMContentLoaded', () => window.dispatchEvent(new Event('pywebviewready')));
</script>
"""


def serve(api, static_dir: Path, port: int = PORT):
    page = (static_dir / "index.html").read_text(encoding="utf-8").replace("<script>", SHIM + "<script>", 1).encode()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", FRAME)
            self.end_headers()
            self.wfile.write(body)

        def _host_ok(self):
            return self.headers.get("Host", "") in HOSTS

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, b'{"error":"host"}')
            if self.path.split("?")[0] in ("/", "/index.html"):
                return self._send(200, page, "text/html; charset=utf-8")
            if self.path == "/salute":
                return self._send(200, b'{"ok":true,"app":"sonorus"}')
            self._send(404, b'{"error":"non trovato"}')

        def do_POST(self):
            if not self._host_ok() or self.headers.get("X-Sonorus") != "1":
                return self._send(403, b'{"error":"non permesso"}')
            name = self.path.removeprefix("/api/")
            fn = getattr(api, name, None) if name.isidentifier() and not name.startswith("_") else None
            if not callable(fn):
                return self._send(404, b'{"error":"metodo sconosciuto"}')
            try:
                n = int(self.headers.get("Content-Length") or 0)
                args = json.loads(self.rfile.read(n) or b"[]") if n else []
                res = fn(*args) if isinstance(args, list) else fn(**args)
                self._send(200, json.dumps(res, ensure_ascii=False, default=str).encode())
            except Exception as e:  # noqa: BLE001
                self._send(500, json.dumps({"error": f"{type(e).__name__}: {e}"}).encode())

    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    except OSError:
        return None  # porta occupata (un altro Sonorus già aperto): si va avanti con la sola finestra
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True, name="sonorus-web").start()
    return srv
