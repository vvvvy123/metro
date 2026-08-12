"""Vercel Function entrypoint for the whole /api/* surface.

Vercel loads a top-level name `handler` (lowercase) that subclasses
BaseHTTPRequestHandler — our class in server.py is `Handler`, so this file is the
adapter, not a second implementation. All routing, validation and SQL stay in
server.py, which still runs standalone (`python server.py`) against SQLite.

`vercel.json` rewrites /api/(.*) here, because one function serving a regex router
is the shape server.py already has; the alternative would be one .py file per
endpoint, which cannot express /api/cities/<id>/stations.
"""
import os
import sys
from urllib.parse import urlparse, parse_qs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from server import Handler  # noqa: E402  (importing server also loads db + .env)


class handler(Handler):
    # Vercel terminates TLS and applies its own limits; the per-connection socket
    # timeout in Handler is for the standalone server and is harmless here.
    #
    # `self.path` is the ORIGINAL request path, not the rewrite destination —
    # measured on a deployed preview, not assumed: a request to /api/__echo?x=1
    # (reachable only through the /api/(.*) -> /api/index rewrite) reported
    # self_path == "/api/__echo?x=1", and none of x-vercel-original-path /
    # x-original-uri / x-matched-path existed. So routing on self.path is correct
    # and the header fallbacks that used to be here were dead code.

    def _route(self, verb):
        u = urlparse(self.path)
        p = u.path.rstrip("/")
        try:
            if verb == "GET":
                self.route_get(p, parse_qs(u.query))
            elif verb == "POST":
                self.route_post(p, self._body())
            else:
                self.route_delete(p)
        except Exception as e:
            self._fail(e)
        finally:
            # On Postgres this rolls back and KEEPS the connection, so a warm
            # instance reuses the socket instead of paying TCP+TLS per request.
            self._close_db()

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_DELETE(self):
        self._route("DELETE")
