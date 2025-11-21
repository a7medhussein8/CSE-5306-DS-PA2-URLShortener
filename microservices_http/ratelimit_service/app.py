import os
import asyncio
import time
import logging
from contextlib import asynccontextmanager
from typing import Optional
import redis.asyncio as redis

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
import grpc

from raft_simple import RaftNode, RaftServicer
import raft_pb2_grpc

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration
NODE_ID = os.getenv("NODE_ID", "ratelimit-1")
RAFT_PORT = int(os.getenv("RAFT_PORT", "50051"))
RAFT_PEERS = os.getenv("RAFT_PEERS", "").split(",") if os.getenv("RAFT_PEERS") else []
STARTUP_DELAY = int(os.getenv("STARTUP_DELAY", "0"))
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# Rate limit configuration
RATE_LIMIT_PER_MIN = int(os.getenv("RL_LIMIT_PER_MIN", "120"))
RATE_LIMIT_WINDOW = int(os.getenv("RL_WINDOW_SEC", "60"))

# Global instances
raft_node: Optional[RaftNode] = None
grpc_server: Optional[grpc.aio.Server] = None
redis_client: Optional[redis.Redis] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle manager for FastAPI app"""
    global raft_node, grpc_server, redis_client
    
    # Startup: Initialize Redis, Raft node and gRPC server
    logger.info(f"Starting node {NODE_ID} with peers: {RAFT_PEERS}")
    
    # Initialize Redis client
    try:
        redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        await redis_client.ping()
        logger.info(f"Connected to Redis at {REDIS_URL}")
    except Exception as e:
        logger.error(f"Failed to connect to Redis: {e}")
        raise
    
    # Add startup delay to avoid synchronized starts
    if STARTUP_DELAY > 0:
        logger.info(f"Waiting {STARTUP_DELAY} seconds before starting...")
        await asyncio.sleep(STARTUP_DELAY)
    
    # Initialize Raft node
    raft_node = RaftNode(node_id=NODE_ID, peers=RAFT_PEERS)
    await raft_node.start()
    
    # Start gRPC server for Raft communication
    grpc_server = grpc.aio.server()
    raft_pb2_grpc.add_RaftServiceServicer_to_server(
        RaftServicer(raft_node),
        grpc_server
    )
    grpc_server.add_insecure_port(f"0.0.0.0:{RAFT_PORT}")
    await grpc_server.start()
    
    logger.info(f"Raft gRPC server started on port {RAFT_PORT}")
    
    yield
    
    # Shutdown: Stop Raft node, gRPC server, and Redis
    logger.info(f"Shutting down node {NODE_ID}")
    if raft_node:
        await raft_node.stop()
    if grpc_server:
        await grpc_server.stop(grace=2)
    if redis_client:
        await redis_client.close()


# Create FastAPI app
app = FastAPI(
    title="Rate Limiting Service with Raft Consensus",
    description="Distributed rate limiting using Raft consensus and Redis",
    version="1.0.0",
    lifespan=lifespan
)


@app.get("/healthz")
async def health_check():
    """Health check endpoint"""
    if raft_node is None:
        raise HTTPException(status_code=503, detail="Raft node not initialized")
    
    # Check Redis connection
    redis_healthy = False
    try:
        await redis_client.ping()
        redis_healthy = True
    except Exception as e:
        logger.error(f"Redis health check failed: {e}")
    
    state = raft_node.get_state()
    return {
        "status": "healthy" if redis_healthy else "degraded",
        "node_id": NODE_ID,
        "redis": "connected" if redis_healthy else "disconnected",
        "raft_state": state
    }


@app.get("/raft/status")
async def raft_status():
    """Get Raft cluster status"""
    if raft_node is None:
        raise HTTPException(status_code=503, detail="Raft node not initialized")
    
    return raft_node.get_state()


@app.get("/raft/leader")
async def get_leader():
    """Get current leader information"""
    if raft_node is None:
        raise HTTPException(status_code=503, detail="Raft node not initialized")
    
    leader = raft_node.get_leader()
    is_leader = raft_node.is_leader()
    
    return {
        "leader": leader,
        "is_leader": is_leader,
        "node_id": NODE_ID
    }


@app.post("/check")
async def check_rate_limit(request: Request):
    """Check rate limit for an IP address"""
    try:
        data = await request.json()
        ip = data.get("ip")
        
        if not ip:
            raise HTTPException(status_code=400, detail="IP address required")
        
        # Check if we're the leader
        if not raft_node or not raft_node.is_leader():
            leader = raft_node.get_leader() if raft_node else None
            return JSONResponse(
                status_code=503,
                content={
                    "error": "Not the leader",
                    "leader": leader,
                    "redirect_to": f"http://{leader}:8003" if leader else None
                }
            )
        
        current_time = time.time()
        
        # Redis key for rate limiting
        redis_key = f"ratelimit:{ip}"
        
        # Get current count and window start from Redis
        pipe = redis_client.pipeline()
        pipe.hget(redis_key, "count")
        pipe.hget(redis_key, "window_start")
        results = await pipe.execute()
        
        count = int(results[0]) if results[0] else 0
        window_start = float(results[1]) if results[1] else current_time
        
        # Reset window if expired
        if current_time - window_start > RATE_LIMIT_WINDOW:
            count = 0
            window_start = current_time
        
        # Check limit
        if count >= RATE_LIMIT_PER_MIN:
            # Log to Raft (rate limit exceeded)
            await raft_node.append_entry(ip, current_time)
            
            return {
                "allowed": False,
                "ip": ip,
                "limit": RATE_LIMIT_PER_MIN,
                "window_seconds": RATE_LIMIT_WINDOW,
                "current_count": count,
                "retry_after": int(RATE_LIMIT_WINDOW - (current_time - window_start))
            }
        
        # Increment count in Redis
        count += 1
        pipe = redis_client.pipeline()
        pipe.hset(redis_key, "count", count)
        pipe.hset(redis_key, "window_start", window_start)
        pipe.expire(redis_key, RATE_LIMIT_WINDOW + 10)  # TTL slightly longer than window
        await pipe.execute()
        
        # Log to Raft (rate limit check)
        await raft_node.append_entry(ip, current_time)
        
        return {
            "allowed": True,
            "ip": ip,
            "limit": RATE_LIMIT_PER_MIN,
            "window_seconds": RATE_LIMIT_WINDOW,
            "current_count": count,
            "remaining": RATE_LIMIT_PER_MIN - count
        }
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error checking rate limit: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/reset")
async def reset_rate_limit(request: Request):
    """Reset rate limit for an IP address (admin operation)"""
    try:
        data = await request.json()
        ip = data.get("ip")
        
        if not ip:
            raise HTTPException(status_code=400, detail="IP address required")
        
        # Check if we're the leader
        if not raft_node or not raft_node.is_leader():
            leader = raft_node.get_leader() if raft_node else None
            return JSONResponse(
                status_code=503,
                content={
                    "error": "Not the leader",
                    "leader": leader,
                    "redirect_to": f"http://{leader}:8003" if leader else None
                }
            )
        
        # Reset in Redis
        redis_key = f"ratelimit:{ip}"
        await redis_client.delete(redis_key)
        
        # Log to Raft
        await raft_node.append_entry(ip, time.time())
        
        return {
            "status": "reset",
            "ip": ip
        }
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error resetting rate limit: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/stats")
async def get_stats():
    """Get rate limiting statistics"""
    if raft_node is None:
        raise HTTPException(status_code=503, detail="Raft node not initialized")
    
    # Get count of tracked IPs from Redis
    tracked_ips = 0
    try:
        keys = await redis_client.keys("ratelimit:*")
        tracked_ips = len(keys)
    except Exception as e:
        logger.error(f"Error getting stats from Redis: {e}")
    
    return {
        "node_id": NODE_ID,
        "is_leader": raft_node.is_leader(),
        "raft_state": raft_node.get_state(),
        "tracked_ips": tracked_ips,
        "rate_limit": {
            "limit_per_window": RATE_LIMIT_PER_MIN,
            "window_seconds": RATE_LIMIT_WINDOW
        }
    }


@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "service": "Rate Limiting with Raft Consensus",
        "node_id": NODE_ID,
        "version": "1.0.0"
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8003)
