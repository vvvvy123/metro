"""TEMPORARY diagnostic function — delete once Phase C is wired up.

It exists to answer, with facts instead of guesses, the four things about this
deployment layout that the Vercel docs do not settle:

  1. Do `api/*.py` functions coexist with `"outputDirectory": "web"`, or does
     setting an output directory suppress them?
  2. When a request to /api/health is rewritten to /api/index, what does the
     function actually see in `self.path` — the ORIGINAL path or the rewritten
     one? The whole router depends on this.
  3. Does the filesystem win over `rewrites`, i.e. is /api/ping still reachable
     as its own function despite the /api/(.*) rewrite?
  4. Is DATABASE_URL visible to the function, and can it import server.py / db.py
     from the repo root?

It never prints a secret — only whether a variable is set, and its length.
"""
import json
import os
import platform
import sys
from http.server import BaseHTTPRequestHandler

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        info = {
            "self_path": self.path,
            "command": self.command,
            "python": platform.python_version(),
            "cwd": os.getcwd(),
            "file_dir": os.path.dirname(os.path.abspath(__file__)),
            "root_listdir": sorted(os.listdir(ROOT))[:40],
            "cwd_listdir": sorted(os.listdir("."))[:40],
            # values withheld on purpose; presence + length is enough to diagnose
            "env_present": {k: len(os.environ.get(k, ""))
                            for k in ("DATABASE_URL", "DB_PATH", "VERCEL",
                                      "VERCEL_ENV", "VERCEL_REGION", "AWS_REGION")},
            "headers": {k.lower(): v for k, v in self.headers.items()},
        }
        for mod in ("server", "db"):
            try:
                m = __import__(mod)
                info[f"import_{mod}"] = f"ok from {getattr(m, '__file__', '?')}"
            except Exception as e:
                info[f"import_{mod}"] = f"FAILED {type(e).__name__}: {e}"
        try:
            import db as dbx
            info["db_is_pg"] = dbx.IS_PG
            if dbx.IS_PG:
                conn = dbx.connect()
                info["db_cities"] = conn.execute("SELECT COUNT(*) c FROM city").fetchone()["c"]
                dbx.finish(conn)
        except Exception as e:
            info["db_probe"] = f"FAILED {type(e).__name__}: {str(e)[:200]}"

        body = json.dumps(info, ensure_ascii=False, indent=1).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
