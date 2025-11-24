

⭐ Project Overview — Raft-Backed URL Shortener System
This project implements a complete distributed URL Shortener system powered by a 5-node Raft consensus cluster that manages global rate-limiting.
Every request entering the system flows through the API Gateway, which communicates with the Raft cluster to make consistent rate-limit decisions across all nodes.
The system includes:
•	API Gateway (entry point)
•	Redirect Service (URL creation, TTL, click limits)
•	Analytics Service (leaderboard, click tracking)
•	Redis (persistent key-value store)
•	Raft Rate-Limit Cluster (5 independent nodes achieving consensus)
🟢 How It Fits Into the URL Shortener
Before ANY URL is shortened or resolved:
1.	Gateway receives request
2.	Gateway forwards the client IP to Raft Leader → /check
3.	Leader replicates log entries to followers
4.	If allowed → gateway calls redirect service
5.	If blocked → gateway returns 429 Too Many Requests
This ensures rate limiting works correctly, consistently, and safely, even during failures, elections, or network delays.


#####RUN GUIDE 

## frist need to be in the path below then you can run the clean up and bulid script using run-raft.ps1 file 

D:\UTA\URL\CSE-5306-DS-PA2-URLShortener\deploy\compose>

## command to run from the above dierctory from same directory above

.\run-raft5.ps1


## to check raft status use the command from same directory above

.\show-raft-status.ps1

####test guide 

1) Quick Check — Who Is the Raft Leader
Run this through the API Gateway:

Invoke-RestMethod -Uri "http://localhost:8080/raft/leader" 
# Returns leader node info

2) Full Raft Cluster Status (Through Gateway)
Invoke-RestMethod -Uri "http://localhost:8080/raft/status" 
# Shows each node and which one is LEADER



3) Test Rate-Limiting via /shorten (Through Gateway)
simulate a client IP using X-Forwarded-For.
$body = @{
    long_url   = "https://www.youtube.com"
    ttl_sec    = 600
    max_clicks = 2
} | ConvertTo-Json

$headers = @{ "X-Forwarded-For" = "10.10.10.1" }

$response = Invoke-RestMethod `
    -Uri "http://localhost:8080/shorten" `
    -Method POST `
    -Headers $headers `
    -ContentType "application/json" `
    -Body $body
# Should succeed — proves gateway → Raft → redirect → Redis pipeline works

4) Stress Test — Multiple Requests from Same IP
Last 2 of 122 requests should be rate-limited (429):
$headers = @{ "X-Forwarded-For" = "20.20.20.20" }

1..122 | ForEach-Object {
    Write-Host "Request #$_" -ForegroundColor Yellow
    try {
        $resp = Invoke-RestMethod `
            -Uri "http://localhost:8080/shorten" `
            -Method POST `
            -Headers $headers `
            -ContentType "application/json" `
            -Body $body

        "  -> OK (code = $($resp.code))"
    }
    catch {
        if ($_.Exception.Response.StatusCode.value__ -eq 429) {
            Write-Host "  -> RATE LIMITED (429)" -ForegroundColor Red
        }
        else {
            Write-Host "  -> ERROR: $($_.Exception.Message)" -ForegroundColor Red
        }
    }

# Final ~2 requests must return 429 RATE LIMITED

