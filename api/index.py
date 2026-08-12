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


def _original_path(h):
    """The path server.py should route on.

    A rewrite sends every /api/* request to /api/index, and it is not documented
    whether the function then sees the original path or the rewritten one — so
    take the original from whichever source actually has it, and fall back to
    self.path. (api/ping.py was deployed first purely to find out which of these
    is populated; keep the belt and braces, it costs two dict lookups.)
    """
    for k in ("x-vercel-original-path", "x-original-uri", "x-forwarded-uri"):
        v = h.headers.get(k)
        if v:
            return v
    return h.path


class handler(Handler):
    # Vercel terminates TLS and applies its own limits; the per-connection socket
    # timeout in Handler is for the standalone server and is harmless here.

    def _route(self, verb):
        u = urlparse(_original_path(self))
        p = u.path.rstrip("/")
        # TEMPORARY, deleted together with api/ping.py. /api/ping is served by its
        # own file, so it cannot answer the one question the router depends on:
        # what does a REWRITTEN request see? This route is reached only via the
        # /api/(.*) rewrite, touches no database, and echoes the raw path back.
        if p == "/api/__echo":
            return self._json({"self_path": self.path, "routed_path": p,
                               "headers": {k.lower(): v for k, v in self.headers.items()}})
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
