"""Vercel entrypoint: every /api/* request is rewritten here (see vercel.json)."""

from http.server import BaseHTTPRequestHandler

from mumble.api import handle, respond

MAX_BODY = 2 * 1024 * 1024  # core.add_memo enforces the real audio limit


class handler(BaseHTTPRequestHandler):
    def _serve(self):
        size = int(self.headers.get("Content-Length") or 0)
        if size > MAX_BODY:
            res = respond(413, {"error": "memo is too big"})
        else:
            body = self.rfile.read(size).decode("utf-8", "replace") if size else ""
            res = handle(self.command, self.path, dict(self.headers), body)
        payload = res["body"].encode()
        self.send_response(res["statusCode"])
        for k, v in res["headers"].items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = do_PATCH = do_OPTIONS = _serve
