"""Entry point for local development: `python3 run.py` starts the app on
http://localhost:8000 (or $PORT if set), serving both the API under
/api/* and the frontend.

This runs Flask's built-in development server (single-threaded, debug mode
on) — fine for your own machine, not meant to be reachable by anyone else.
For a real deployment, use gunicorn instead; see the root Procfile and the
README's "Deploying to Railway" section.

use_reloader=False is deliberate: server.py starts the background market
scheduler (and touches the database) at import time, not gated behind a
"only the real worker, not the reloader's watcher process" check. Flask's
default auto-reloader launches a second copy of this whole process to
watch for file changes, and that second copy would import server.py too —
meaning two live processes, each with their own scheduler thread, hammering
the same SQLite file at once. That's a direct path to constant "database is
locked" errors (as opposed to an occasional one under real concurrent
load), so it's not worth the code-reload convenience here. If you want
auto-reload back, gate the scheduler start on
`os.environ.get("WERKZEUG_RUN_MAIN") == "true"` in server.py first.
"""
import os
from app.server import app

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=True, use_reloader=False)
