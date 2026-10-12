"""Request-level protection for the web admin: real client IPs behind the
Cloudflare Tunnel, per-IP rate limits, body size caps and security headers."""
from __future__ import annotations

import ipaddress
import time
from collections import defaultdict, deque

from starlette.requests import Request
from starlette.responses import PlainTextResponse

MAX_BODY = 2_000_000  # bytes; settings import is the largest legitimate upload

# (path prefix, requests, per seconds). First match wins; "" is the default.
LIMITS = [
    ("/login", 10, 60),
    ("/auth/", 20, 60),
    ("/static/", 600, 60),
    ("", 240, 60),
]


def _private(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback


def via_cloudflare(request: Request) -> bool:
    """True when the request came in through cloudflared. Cloudflare always
    sets CF-Connecting-IP; it is only trusted from a private peer (the
    cloudflared container), so nobody can fake it from outside."""
    peer = request.client.host if request.client else ""
    return bool(request.headers.get("cf-connecting-ip")) and _private(peer)


def client_ip(request: Request) -> str:
    if via_cloudflare(request):
        return request.headers["cf-connecting-ip"].strip()
    return request.client.host if request.client else "?"

# Per signed-in person (owners exempt), on top of the per-IP limits above, so
# one compromised admin or member account can't wear Nyerk down from many IPs.
# (path prefix, method or "" for any, requests, per seconds). All matches apply.
USER_LIMITS = [
    ("", "", 120, 60),
    ("/search", "GET", 30, 60),
    # Live suggestions fire as you type, so they get their own, larger
    # allowance instead of counting toward the two above (see LIVE_PATHS).
    ("/search/suggest", "GET", 180, 60),
    ("/stats", "GET", 20, 60),
    ("/settings/export", "GET", 10, 600),
    ("/me", "", 30, 60),          # /me reads and changes roles through Discord
]
LIVE_PATHS = ("/search/suggest",)
# Actions that post or delete many Discord messages, create roles or fetch
# shugo.gg: at most once per this many seconds, shared by everyone but the
# owner, so nobody can use them to run Nyerk into Discord's rate limits.
COOLDOWNS = {
    "/dashboard/repost": 60,
    "/categories/repost-roles": 120,
    "/party-panels": 60,
    "/party/content/create-roles": 60,
    "/defaults/check": 300,
}


def under(path: str, prefix: str) -> bool:
    return not prefix or path == prefix or path.startswith(prefix.rstrip("/") + "/")


class RateLimiter:
    def __init__(self) -> None:
        self.hits: dict[tuple[str, str], deque[float]] = defaultdict(deque)

    def allow(self, key: str, bucket: str, limit: int, window: float, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        q = self.hits[(bucket, key)]
        while q and q[0] <= now - window:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        if len(self.hits) > 50_000:  # keep memory bounded under a flood
            self.hits.clear()
        return True


def install(app, cfg) -> RateLimiter:
    limiter = RateLimiter()

    @app.middleware("http")
    async def _protect(request: Request, call_next):
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > MAX_BODY:
            return PlainTextResponse("Request too large.", status_code=413)
        ip = client_ip(request)
        prefix, limit, window = next(x for x in LIMITS if request.url.path.startswith(x[0]))
        if not limiter.allow(ip, prefix or "all", limit, window):
            return PlainTextResponse("Too many requests. Slow down and try again in a minute.", status_code=429,
                                     headers={"Retry-After": str(window)})
        request.state.client_ip = ip
        request.state.via_cloudflare = via_cloudflare(request)
        resp = await call_next(request)
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data: https://cdn.discordapp.com; style-src 'self'; "
            "script-src 'self'; form-action 'self' https://discord.com; frame-ancestors 'none'; base-uri 'none'")
        if cfg.is_public:
            resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        if request.url.path.startswith(("/login", "/auth/", "/me", "/members", "/settings")):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    return limiter
