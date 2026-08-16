"""A small in-memory rate limiter, stdlib only.

Correctness note: this state lives in one process's memory, so it only
works correctly with a single worker process — which is exactly the
constraint the Railway Procfile already pins (`gunicorn --workers 1
--threads 8`), for an unrelated reason (the background scheduler thread).
If that constraint is ever relaxed to multiple worker processes, this
limiter would need to move to something shared (e.g. Redis, or a DB
table) — each worker would otherwise enforce its own independent limit,
letting a client get N requests per worker instead of N total.

Not a general-purpose library: fixed-window counting (not sliding-window
or token-bucket), no persistence across restarts, no cleanup of old
identities (fine for a demo's traffic volume; would need eviction for a
long-running production service with many distinct callers).
"""
import time
from collections import defaultdict, deque
from functools import wraps

from flask import g, jsonify, request

_windows = defaultdict(deque)


def _client_identity() -> str:
    """Rate-limit by authenticated identity when we have one (a user or an
    API key), falling back to IP for unauthenticated requests. Keyed this
    way so one bot's traffic doesn't get lumped in with unrelated visitors
    behind the same NAT/proxy IP, and so switching IPs doesn't reset a
    bot's limit."""
    identity = getattr(g, "rate_limit_identity", None)
    if identity:
        return identity
    return f"ip:{request.remote_addr or 'unknown'}"


def rate_limit(max_requests: int, per_seconds: int):
    """Decorator: at most `max_requests` calls per `per_seconds` window,
    per identity (see _client_identity). Returns HTTP 429 with a Retry-
    After-style hint in the body once exceeded."""

    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            identity = _client_identity()
            key = f"{f.__name__}:{identity}"
            now = time.monotonic()
            window = _windows[key]
            while window and now - window[0] > per_seconds:
                window.popleft()
            if len(window) >= max_requests:
                retry_after = max(0, per_seconds - (now - window[0]))
                return jsonify({
                    "detail": f"Rate limit exceeded: max {max_requests} requests "
                              f"per {per_seconds}s for this endpoint.",
                    "retry_after_seconds": round(retry_after, 1),
                }), 429
            window.append(now)
            return f(*args, **kwargs)
        return wrapper
    return decorator
