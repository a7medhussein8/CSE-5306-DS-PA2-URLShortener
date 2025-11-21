# File: microservices_http/ratelimit_service/app.py
import os
from fastapi import FastAPI, HTTPException, Request
from persistence.redis_client import get_redis
from common.lib.rate_limit import check_and_consume

app = FastAPI(title="ratelimit_service")
redis = get_redis()

DEFAULT_LIMIT = int(os.getenv("RL_LIMIT_PER_MIN", "120"))
DEFAULT_WINDOW = int(os.getenv("RL_WINDOW_SEC", "60"))

def client_ip_from_request(req: Request) -> str:
    # Prefer X-Forwarded-For if present
    fwd = req.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return req.client.host if req.client else "unknown"

@app.get("/healthz")
async def healthz():
    pong = await redis.ping()
    return {"status": "ok", "redis": pong}

@app.get("/check")
async def check(request: Request, ip: str | None = None, limit: int | None = None, window: int | None = None):
    ip_addr = ip or client_ip_from_request(request)
    allowed, remaining = await check_and_consume(
        redis,
        ip=ip_addr,
        limit=limit or DEFAULT_LIMIT,
        window_sec=window or DEFAULT_WINDOW
    )
    if not allowed:
        raise HTTPException(status_code=429, detail={"retry": window or DEFAULT_WINDOW, "remaining": 0})
    return {"allowed": True, "remaining": remaining}
