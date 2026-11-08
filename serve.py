"""Serve the health dashboard on this computer with the Python standard library."""
import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}


def regular_file(path):
    if path.is_symlink() or path.parent.is_symlink() or not path.is_file():
        raise FileNotFoundError
    return path.read_bytes()


class Handler(BaseHTTPRequestHandler):
    def send_content(self, status, body, content_type, *, head=False, source=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; script-src 'self'; style-src 'self'; "
                         "connect-src 'self'; img-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        if source:
            self.send_header("X-Overview-Source", source)
        self.end_headers()
        if not head:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def serve(self, head=False):
        port = self.server.server_port
        hosts = {"localhost", "127.0.0.1", f"localhost:{port}", f"127.0.0.1:{port}"}
        received_hosts = self.headers.get_all("Host", [])
        if len(received_hosts) != 1 or received_hosts[0] not in hosts:
            return self.send_content(403, b"Forbidden", "text/plain", head=head)
        try:
            route = urlsplit(self.path)
        except ValueError:
            return self.send_content(400, b"Bad request", "text/plain", head=head)
        if route.scheme or route.netloc:
            return self.send_content(400, b"Bad request", "text/plain", head=head)
        try:
            if route.path in STATIC:
                name, mime = STATIC[route.path]
                return self.send_content(200, regular_file(ROOT / "site" / name), mime, head=head)
            if route.path == "/api/overview":
                file = ROOT / "data" / "overview.json"
                source = "local"
                if not file.exists() and not file.is_symlink():
                    file = ROOT / "examples" / "overview.json"
                    source = "demo"
                body = regular_file(file)
                overview = json.loads(body)
                if not (isinstance(overview, dict)
                        and isinstance(overview.get("days"), list)
                        and isinstance(overview.get("activities"), list)
                        and isinstance(overview.get("updated_at"), str)):
                    raise ValueError
                return self.send_content(200, body, "application/json; charset=utf-8",
                                         head=head, source=source)
        except (ValueError, OSError):
            return self.send_content(500, b"Dashboard input unavailable", "text/plain", head=head)
        self.send_content(404, b"Not found", "text/plain", head=head)

    def do_GET(self):
        self.serve()

    def do_HEAD(self):
        self.serve(head=True)

    def log_message(self, format, *args):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    try:
        with ThreadingHTTPServer(("127.0.0.1", args.port), Handler) as server:
            print(f"打开 http://127.0.0.1:{args.port}/ 查看概览，按 Ctrl+C 停止服务。", flush=True)
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    except OSError as error:
        print(f"无法启动本地看板。{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
