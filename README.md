Get-Content body.json
# CSE-5306-DS-PA2-URLShortener

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Docker](https://img.shields.io/badge/docker-ready-brightgreen.svg)](https://www.docker.com/)

A high-performance, distributed URL shortening service implementing both **Microservices** and **Layered** architectures for comparative analysis.

---

## Important: Testing Guide

**All test commands are organized in dedicated txt files. All of them are Git Bash or works on mac terminal** Follow these for step-by-step testing:

- [`microservices_http_runs.txt`](microservices_http_runs.txt) - This file has all the run commands for microservice architecture with all the functional requirements executed in order.
- [`layered_grpc_runs.txt`](layered_grpc_runs.txt) - This file has all the run commands for layered architecture with all the functional requirements executed in order.
- [`loadtest_microservice_runs.txt`](loadtest_microservice_runs.txt) - This file has all the run commands for microservice architecture for loadtesting.
- [`loadtest_layered_runs.txt`](loadtest_layered_runs.txt) - This file has all the run commands for layered architecture for loadtesting.

These files contain **all commands in the correct order** of execution. Use them as your primary reference.

---

## Table of Contents
- [Important: Testing Guide](#important-testing-guide)
- [What is URL Shortening?](#what-is-url-shortening)
- [Key Features](#key-features)
- [Project Structure](#project-structure)
- [Functional Requirements](#functional-requirements)
- [System Architectures](#system-architectures)
- [Contributing](#contributing)
- [License](#license)
- [Quick Links](#quick-links)

---

## What is URL Shortening?

URL shortening transforms long, unwieldy URLs into short, manageable links while maintaining full functionality.

**Example:**
- Long URL: `https://www.example.com/very/long/path?id=12345`
- Short URL: `http://short.ly/abc123`
- User visits short URL and is automatically redirected to long URL

---

## Key Features

- **Create Short Links** - Convert any URL to a 7-character code
- **Instant Redirection** - HTTP 301 redirects for optimal browser caching
- **Time-based Expiration** - Set TTL (time-to-live) for links
- **Click-based Expiration** - Limit number of uses per link
- **Rate Limiting** - Protect against abuse (120 req/min default)
- **Analytics** - Track click counts and view top links
- **HEAD Requests** - Check URLs without consuming click counts
- **High Availability** - Redis-backed persistence with optional replication

---

## Project Structure

```
CSE-5306-DS-PA2-URLShortener/
│
├── Test Files (Your Main Reference)
│   ├── microservices_http_runs.txt      # All REST API test commands
│   ├── layered_grpc_runs.txt            # All gRPC test commands
│   ├── loadtest_microservice_runs.txt   # HTTP performance tests
│   └── loadtest_layered_runs.txt        # gRPC performance tests
│
├── Microservices Architecture
│   ├── microservices_http/
│   │   ├── api_gateway/                 # HTTP Gateway (Port 8080)
│   │   ├── redirect_service/            # URL resolution
│   │   ├── analytics_service/           # Click tracking
│   │   └── ratelimit_service/           # Rate limiting
│   └── deploy/compose/
│       └── docker-compose.micro.yaml    # Microservices deployment
│
├── Layered Architecture
│   └── layered_simple/
│       ├── src/
│       │   ├── proto/                   # gRPC definitions
│       │   ├── gateway/                 # API Layer (Port 8081)
│       │   ├── business/                # Logic Layer (Port 8082)
│       │   └── data/                    # Data Layer (Port 8083)
│       └── docker-compose.yaml          # Layered deployment
│
├── Common Components
│   ├── common/                          # Shared libraries
│   ├── persistence/                     # Redis repositories
│   └── deploy/                          # Deployment configs
│
└── Configuration
    ├── payload.json                     # Sample request payload
    └── README.md                        # This file
```

---

## Functional Requirements

### Requirement 1: Create Short Links

**What:** Convert long URLs into short codes

**How:** 
- Accept long URL as input
- Generate unique 7-character code (base62 encoding)
- Store mapping in Redis
- Return short URL to user

**Example:**
```
Input:  "https://www.google.com"
Output: "http://localhost:8080/Vy6UJ4i"
```

### Requirement 2: Resolve & Redirect (HTTP 301)

**What:** Redirect users from short URL to original URL

**How:**
- Lookup short code in Redis
- Return HTTP 301 redirect
- Increment analytics counter (for GET requests)
- Support HEAD requests (check without counting)

**Flow:**
```
User visits: http://localhost:8080/Vy6UJ4i
System finds: https://www.google.com
Browser redirects user to original URL
```

### Requirement 3: Rate Limiting

**What:** Prevent abuse and overload

**How:**
- Track requests per IP address
- Default: 120 requests per minute per IP
- Sliding window algorithm
- Return HTTP 429 when limit exceeded

**Example:**
```
IP makes request #1-120:   Allowed
IP makes request #121:     Blocked (429 Too Many Requests)
After 60 seconds:          Counter resets
```

### Requirement 4: Top Links Analytics

**What:** Show most popular URLs

**How:**
- Track click count per short code
- Maintain sorted set in Redis (O(log N) updates)
- Provide API to query top N links
- Real-time updates

**Example Output:**
```json
[
  {"code": "abc123", "clicks": 1523, "long_url": "https://example.com"},
  {"code": "xyz789", "clicks": 892, "long_url": "https://another.com"},
  {"code": "def456", "clicks": 654, "long_url": "https://third.com"}
]
```

### Requirement 5: Link Expiration

**What:** Automatic link expiration based on time or clicks

**How:**

**Time-based (TTL):**
- Set expiration in seconds (e.g., 3600 = 1 hour)
- Redis handles automatic deletion
- Returns 404 after expiration

**Click-based (max_clicks):**
- Set maximum click count
- Atomic decrement on each access
- Returns 410 when limit reached

**Combined:**
- Both limits can be set
- Link expires when either limit hits

**Example:**
```json
{
  "long_url": "https://example.com",
  "ttl_sec": 3600,
  "max_clicks": 100
}
```

---

## System Architectures

### Architecture 1: Microservices (HTTP/REST)

**Concept:** Independent, loosely-coupled services communicating via HTTP

**Structure:**
```
API Gateway (Port 8080)
   - Routes requests to services
   - Aggregates responses
   - Single entry point

Redirect Service (Port 8001)
   - Creates short URLs
   - Resolves codes to long URLs
   - Manages expiration

Analytics Service (Port 8002)
   - Tracks click counts
   - Provides top links
   - Real-time statistics

Rate Limit Service (Port 8003)
   - Checks request limits
   - Enforces rate policies
   - Sliding window counters

Redis (Port 6379)
   - Shared data store
   - Persistence layer
```

**Communication:** HTTP/REST with JSON  
**Deployment:** Docker Compose (5 containers)

### Architecture 2: Layered (gRPC)

**Concept:** Vertical stack with strict layer separation and gRPC communication

**Structure:**
```
API Gateway Layer (Port 8081)
   - HTTP to gRPC translation
   - Client-facing interface
   - Request validation

Business Logic Layer (Port 8082)
   - URL validation
   - Code generation
   - Rate limit checks
   - Expiration logic

Data Service Layer (Port 8083)
   - Database operations
   - Redis communication
   - Data persistence

Redis Master (Port 6379)
   - Primary storage

Redis Replica (Port 6380)
   - Backup storage (optional)
```

**Communication:** gRPC with Protocol Buffers  
**Deployment:** Docker Compose (5 containers)

---


## License

MIT License - See [LICENSE](LICENSE) file for details

---


**GitHub**: https://github.com/AbhijitChallapalli/CSE-5306-DS-PA2-URLShortener

---

