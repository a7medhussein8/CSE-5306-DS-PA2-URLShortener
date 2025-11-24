import os
import time
import httpx
import asyncio
from typing import Optional
import logging
from fastapi import FastAPI, Request, HTTPException, Response
from fastapi.responses import RedirectResponse

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("Client")

app = FastAPI(title="api_gateway")

# Service URLs
REDIRECT_URL   = os.getenv("REDIRECT_URL",   "http://redirect:8001")
ANALYTICS_URL  = os.getenv("ANALYTICS_URL",  "http://analytics:8002")

# Raft cluster configuration
RATELIMIT_URLS = os.getenv(
    "RATELIMIT_URLS", 
    "http://ratelimit-1:8003,http://ratelimit-2:8003,http://ratelimit-3:8003,http://ratelimit-4:8003,http://ratelimit-5:8003"
).split(",")

# HTTP client with timeout
client = httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=2.0))

# Raft leader cache
raft_leader_cache = {
    "leader_node_id": None,
    "leader_url": None,
    "last_check": 0,
    "ttl": 5  # Cache for 5 seconds
}


def client_ip(req: Request) -> str:
    """Extract client IP from request"""
    fwd = req.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return req.client.host if req.client else "unknown"


async def find_raft_leader() -> Optional[str]:
    """
    Find the current Raft leader by querying all nodes.
    Returns the leader URL or None if no leader is found.
    """
    current_time = time.time()
    
    # Use cached leader if still valid
    if (raft_leader_cache["leader_url"] and 
        current_time - raft_leader_cache["last_check"] < raft_leader_cache["ttl"]):
        logger.debug(f"Using cached leader: {raft_leader_cache['leader_url']}")
        return raft_leader_cache["leader_url"]
    
    logger.info("🔍 Searching for Raft leader...")
    
    # Query all nodes to find the leader
    for url in RATELIMIT_URLS:
        try:
            response = await client.get(f"{url}/raft/leader", timeout=2.0)
            if response.status_code == 200:
                data = response.json()
                if data.get("is_leader"):
                    raft_leader_cache["leader_node_id"] = data.get("node_id")
                    raft_leader_cache["leader_url"] = url
                    raft_leader_cache["last_check"] = current_time
                    logger.info(f"✓ Found leader: {data.get('node_id')} at {url}")
                    return url
        except Exception as e:
            logger.debug(f"Failed to query {url}: {e}")
            continue
    
    # No leader found
    logger.warning("⚠️  No Raft leader found - election may be in progress")
    raft_leader_cache["leader_url"] = None
    raft_leader_cache["last_check"] = current_time
    return None


async def call_rate_limit_leader(ip: str, max_retries: int = 3) -> dict:
    """
    Call rate limit check on the Raft leader with automatic retry and leader discovery.
    
    Returns the rate limit response as a dict.
    Raises HTTPException if service is unavailable.
    """
    for attempt in range(max_retries):
        try:
            # Find the leader
            leader_url = await find_raft_leader()
            
            if not leader_url:
                # No leader found - wait and retry
                if attempt < max_retries - 1:
                    logger.warning(f"No leader found, retrying... (attempt {attempt + 1}/{max_retries})")
                    await asyncio.sleep(0.5)  # Wait for election
                    continue
                else:
                    raise HTTPException(503, "No Raft leader available - cluster may be electing")
            
            # Call the leader
            response = await client.post(
                f"{leader_url}/check",
                json={"ip": ip},
                timeout=2.0
            )
            
            # If we get 503 (not the leader), invalidate cache and retry
            if response.status_code == 503:
                logger.warning(f"Node at {leader_url} is no longer leader, invalidating cache")
                raft_leader_cache["leader_url"] = None
                if attempt < max_retries - 1:
                    continue
                else:
                    raise HTTPException(503, "Rate limit leader changed, please retry")
            
            # Success - return the response
            if response.status_code == 200:
                return response.json()
            elif response.status_code == 429:
                # Rate limit exceeded
                return response.json()
            else:
                raise HTTPException(response.status_code, response.text)
            
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error calling rate limit leader: {e}")
            raft_leader_cache["leader_url"] = None
            if attempt == max_retries - 1:
                raise HTTPException(503, f"Rate limit service unavailable: {str(e)}")
    
    raise HTTPException(503, "Rate limit service unavailable after retries")


@app.get("/healthz")
async def healthz():
    """
    Health check endpoint - checks all downstream services including Raft cluster.
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
                    if node_data.get("raft_state", {}).get("state") == "LEADER":
                        leader_found = True
                    raft_status[url] = "healthy"
            except Exception as e:
                raft_status[url] = f"unhealthy: {str(e)}"
        
        return {
            "gateway": "ok",
            "redirect": r.json(),
            "analytics": a.json(),
            "ratelimit_cluster": {
                "cluster_size": len(RATELIMIT_URLS),
                "healthy_nodes": healthy_nodes,
                "leader_found": leader_found,
                "cached_leader": raft_leader_cache.get("leader_node_id"),
                "nodes": raft_status
            }
        }
    except Exception as e:
        return {"gateway": "degraded", "error": str(e)}


@app.post("/shorten")
async def shorten(req: Request, payload: dict):
    """
    Create a shortened URL.
    Checks rate limit via Raft cluster before creating.
    """
    ip = client_ip(req)
    
    # Check rate limit with Raft leader
    rl_response = await call_rate_limit_leader(ip)
    
    if not rl_response.get("allowed", False):
        raise HTTPException(429, "Too Many Requests")
    
    # Create short URL
    r = await client.post(f"{REDIRECT_URL}/shorten", json=payload)
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.text)
    return r.json()


@app.api_route("/{code}", methods=["GET", "HEAD"])
async def redirect_or_head(req: Request, code: str):
    """
    Redirect to the original URL (GET) or return headers only (HEAD).
    Rate limits both request types.
    """
    ip = client_ip(req)

    # Rate limit both GET and HEAD
    rl_response = await call_rate_limit_leader(ip)
    
    if not rl_response.get("allowed", False):
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
        except Exception as e:
            logger.error(f"Failed to increment analytics: {e}")

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
    return r.json()


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
                if node_status.get("state") == "LEADER":
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
    """
    leader_url = await find_raft_leader()
    
    if not leader_url:
        return {
            "leader_url": None,
            "message": "No leader found - election may be in progress"
        }
    
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


@app.on_event("startup")
async def startup_event():
    """Log configuration on startup"""
    logger.info("")
    logger.info("="*60)
    logger.info("API Gateway Starting")
    logger.info("="*60)
    logger.info(f"Redirect service: {REDIRECT_URL}")
    logger.info(f"Analytics service: {ANALYTICS_URL}")
    logger.info(f"Rate limit cluster: {len(RATELIMIT_URLS)} nodes")
    for i, url in enumerate(RATELIMIT_URLS, 1):
        logger.info(f"  Node {i}: {url}")
    logger.info(f"Leader cache TTL: {raft_leader_cache['ttl']} seconds")
    logger.info("="*60)
    logger.info("")