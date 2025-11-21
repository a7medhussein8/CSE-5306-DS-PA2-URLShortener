# File: microservices_http/api_gateway/app.py
import os
import time
import httpx
from fastapi import FastAPI, Request, HTTPException, Response
from fastapi.responses import RedirectResponse
from common.lib.rate_limit import ShortenRequest, ShortenResponse

app = FastAPI(title="api_gateway")

# Service URLs
REDIRECT_URL   = os.getenv("REDIRECT_URL",   "http://redirect:8001")
ANALYTICS_URL  = os.getenv("ANALYTICS_URL",  "http://analytics:8002")

# Raft cluster configuration
RATELIMIT_URLS = os.getenv("RATELIMIT_URLS", "http://ratelimit-1:8003,http://ratelimit-2:8004,http://ratelimit-3:8005,http://ratelimit-4:8006,http://ratelimit-5:8007").split(",")
RATELIMIT_URL  = os.getenv("RATELIMIT_URL",  RATELIMIT_URLS[0])  # Backward compatibility

# HTTP client with reasonable timeout
client = httpx.AsyncClient(timeout=5.0)

# Raft leader cache
raft_leader_cache = {
    "leader_url": None,
    "last_check": 0,
    "ttl": 5  # Cache leader for 5 seconds
}

def client_ip(req: Request) -> str:
    """Extract client IP from request"""
    fwd = req.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return req.client.host if req.client else "unknown"


async def get_raft_leader() -> str:
    """
    Find the current Raft leader with caching.
    Returns the leader URL or falls back to first available node.
    """
    current_time = time.time()
    
    # Use cached leader if still valid
    if (raft_leader_cache["leader_url"] and 
        current_time - raft_leader_cache["last_check"] < raft_leader_cache["ttl"]):
        return raft_leader_cache["leader_url"]
    
    # Query all nodes to find the leader
    for url in RATELIMIT_URLS:
        try:
            response = await client.get(f"{url}/raft/leader", timeout=2.0)
            if response.status_code == 200:
                data = response.json()
                if data.get("is_leader"):
                    raft_leader_cache["leader_url"] = url
                    raft_leader_cache["last_check"] = current_time
                    return url
        except Exception:
            continue
    
    # Fallback: use first available node or backward-compatible RATELIMIT_URL
    raft_leader_cache["leader_url"] = RATELIMIT_URL
    raft_leader_cache["last_check"] = current_time
    return RATELIMIT_URL


async def check_rate_limit_with_retry(ip: str, max_retries: int = 3) -> httpx.Response:
    """
    Check rate limit with automatic Raft leader discovery and retry.
    Handles leader changes gracefully.
    """
    for attempt in range(max_retries):
        try:
            leader_url = await get_raft_leader()
            response = await client.get(f"{leader_url}/check", params={"ip": ip}, timeout=2.0)
            
            # If we get 503 (not the leader), invalidate cache and retry
            if response.status_code == 503:
                raft_leader_cache["leader_url"] = None
                if attempt < max_retries - 1:
                    continue
            
            return response
            
        except Exception as e:
            # On error, invalidate cache and retry
            raft_leader_cache["leader_url"] = None
            if attempt == max_retries - 1:
                raise HTTPException(503, f"Rate limit service unavailable: {str(e)}")
    
    # Should not reach here, but just in case
    raise HTTPException(503, "Rate limit service unavailable after retries")


@app.get("/healthz")
async def healthz():
    """
    Health check endpoint - checks all downstream services.
    Includes Raft cluster status.
    """
    try:
        # Check core services
        r = await client.get(f"{REDIRECT_URL}/healthz", timeout=2.0)
        a = await client.get(f"{ANALYTICS_URL}/healthz", timeout=2.0)
        
        # Check Raft cluster
        raft_status = {}
        leader_found = False
        healthy_nodes = 0
        
        for url in RATELIMIT_URLS:
            try:
                rl = await client.get(f"{url}/healthz", timeout=2.0)
                if rl.status_code == 200:
                    healthy_nodes += 1
                    node_data = rl.json()
                    if node_data.get("raft_state", {}).get("state") == "leader":
                        leader_found = True
                    raft_status[url] = "healthy"
            except Exception as e:
                raft_status[url] = f"unhealthy: {str(e)}"
        
        return {
            "gateway": "ok",
            "redirect": r.json(),
            "analytics": a.json(),
            "ratelimit": {
                "cluster_size": len(RATELIMIT_URLS),
                "healthy_nodes": healthy_nodes,
                "leader_found": leader_found,
                "nodes": raft_status
            }
        }
    except Exception as e:
        return {"gateway": "degraded", "error": str(e)}


@app.post("/shorten", response_model=ShortenResponse)
async def shorten(req: Request, payload: ShortenRequest):
    """
    Create a shortened URL.
    Checks rate limit via Raft cluster before creating.
    """
    ip = client_ip(req)
    
    # Check rate limit with Raft leader discovery
    rl_response = await check_rate_limit_with_retry(ip)
    if rl_response.status_code == 429:
        raise HTTPException(429, "Too Many Requests")
    
    # Create short URL
    data = payload.model_dump(mode="json")
    r = await client.post(f"{REDIRECT_URL}/shorten", json=data)
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.text)
    return ShortenResponse(**r.json())


@app.api_route("/{code}", methods=["GET", "HEAD"])
async def redirect_or_head(req: Request, code: str):
    """
    Redirect to the original URL (GET) or return headers only (HEAD).
    Rate limits both request types.
    """
    ip = client_ip(req)

    # Rate limit both GET and HEAD
    rl = await check_rate_limit_with_retry(ip)
    if rl.status_code == 429:
        raise HTTPException(429, "Too Many Requests")

    # HEAD should NOT consume click -> count=false
    count = "false" if req.method == "HEAD" else "true"
    r = await client.get(f"{REDIRECT_URL}/resolve/{code}", params={"count": count})

    if r.status_code == 404:
        raise HTTPException(404, "Link not found")
    if r.status_code == 410:
        raise HTTPException(410, "Link expired")

    long_url = r.json()["long_url"]

    # Only GET increments analytics
    if req.method == "GET":
        try:
            await client.post(f"{ANALYTICS_URL}/increment/{code}")
        except:
            pass

    # Return 301; for HEAD, empty body with Location header
    if req.method == "HEAD":
        return Response(status_code=301, headers={"Location": long_url})
    return RedirectResponse(url=long_url, status_code=301)


@app.get("/analytics/top")
async def top(limit: int = 10):
    """Get top N most accessed short codes"""
    r = await client.get(f"{ANALYTICS_URL}/top", params={"limit": limit})
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.text)
    return r.json()


@app.get("/stats/{code}")
async def stats(code: str):
    """Get statistics for a specific short code"""
    r = await client.get(f"{REDIRECT_URL}/stats/{code}")
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.text)
    return r.json()


@app.get("/api/resolve")
async def api_resolve(code: str, count: bool = True):
    """
    API endpoint to resolve a short code without redirect.
    Returns the long URL as JSON.
    """
    r = await client.get(
        f"{REDIRECT_URL}/resolve/{code}", 
        params={"count": "true" if count else "false"}
    )
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.text)
    return r.json()  # {"long_url": "..."}


@app.get("/raft/status")
async def raft_cluster_status():
    """
    Get detailed status of all Raft nodes in the cluster.
    Useful for monitoring and debugging.
    """
    status = {}
    leader_url = None
    
    for url in RATELIMIT_URLS:
        try:
            response = await client.get(f"{url}/raft/status", timeout=2.0)
            if response.status_code == 200:
                node_status = response.json()
                status[url] = node_status
                if node_status.get("state") == "leader":
                    leader_url = url
        except Exception as e:
            status[url] = {"error": str(e)}
    
    return {
        "cluster": status,
        "leader": leader_url,
        "total_nodes": len(RATELIMIT_URLS),
        "cached_leader": raft_leader_cache["leader_url"],
        "cache_age": time.time() - raft_leader_cache["last_check"]
    }


@app.get("/raft/leader")
async def raft_leader_info():
    """
    Get information about the current Raft leader.
    Faster than full cluster status.
    """
    leader_url = await get_raft_leader()
    
    try:
        response = await client.get(f"{leader_url}/raft/leader", timeout=2.0)
        if response.status_code == 200:
            return {
                "leader_url": leader_url,
                "leader_info": response.json(),
                "cached": time.time() - raft_leader_cache["last_check"] < 1
            }
    except Exception as e:
        return {
            "leader_url": leader_url,
            "error": str(e)
        }
    
    return {"leader_url": leader_url}


# Backward compatibility: support single RATELIMIT_URL environment variable
@app.on_event("startup")
async def startup_event():
    """Log configuration on startup"""
    print(f"API Gateway starting...")
    print(f"Redirect service: {REDIRECT_URL}")
    print(f"Analytics service: {ANALYTICS_URL}")
    print(f"Rate limit cluster: {len(RATELIMIT_URLS)} nodes")
    for i, url in enumerate(RATELIMIT_URLS, 1):
        print(f"  Node {i}: {url}")
    print(f"Leader cache TTL: {raft_leader_cache['ttl']} seconds")
