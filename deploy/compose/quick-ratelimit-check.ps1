#!/usr/bin/env pwsh
# Quick Rate Limit Health Check

$GATEWAY_URL = "http://localhost:8080"

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "Quick Rate Limit Health Check" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

# 1. Check Gateway Health
Write-Host "`n[1] Gateway Health:" -ForegroundColor Yellow
try {
    $health = Invoke-RestMethod -Uri "$GATEWAY_URL/healthz" -Method Get
    Write-Host "  Status: $($health.gateway)" -ForegroundColor Green
    Write-Host "  Leader Found: $($health.ratelimit_cluster.leader_found)" -ForegroundColor $(if ($health.ratelimit_cluster.leader_found) { "Green" } else { "Red" })
    Write-Host "  Healthy Nodes: $($health.ratelimit_cluster.healthy_nodes)/$($health.ratelimit_cluster.cluster_size)" -ForegroundColor Green
    Write-Host "  Cached Leader: $($health.ratelimit_cluster.cached_leader)" -ForegroundColor Cyan
}
catch {
    Write-Host "  ERROR: Cannot reach gateway!" -ForegroundColor Red
    exit 1
}

# 2. Check Current Leader
Write-Host "`n[2] Current Leader:" -ForegroundColor Yellow
try {
    $leader = Invoke-RestMethod -Uri "$GATEWAY_URL/raft/leader" -Method Get
    if ($leader.leader_url) {
        Write-Host "  Leader URL: $($leader.leader_url)" -ForegroundColor Green
        Write-Host "  Leader Node: $($leader.leader_info.node_id)" -ForegroundColor Green
        Write-Host "  Term: $($leader.leader_info.term)" -ForegroundColor Cyan
    }
    else {
        Write-Host "  WARNING: No leader elected yet!" -ForegroundColor Red
    }
}
catch {
    Write-Host "  ERROR: Cannot get leader info!" -ForegroundColor Red
}

# 3. Test Rate Limit (5 requests)
Write-Host "`n[3] Testing Rate Limit (5 sample requests):" -ForegroundColor Yellow
$success = 0
$failed = 0

for ($i = 1; $i -le 5; $i++) {
    try {
        $body = @{ long_url = "https://test.com/$i" } | ConvertTo-Json
        $response = Invoke-WebRequest -Uri "$GATEWAY_URL/shorten" -Method Post `
            -Body $body -ContentType "application/json" -ErrorAction Stop

        $success++
        Write-Host "  ✓ Request $i : Success" -ForegroundColor Green
    }
    catch {
        $failed++
        $statusCode = $_.Exception.Response.StatusCode.value__
        Write-Host "  ✗ Request $i : Failed (Status: $statusCode)" -ForegroundColor Red
    }
    Start-Sleep -Milliseconds 100
}

Write-Host "`n  Results: $success success, $failed failed" -ForegroundColor Cyan

# 4. Check Individual Node Status
Write-Host "`n[4] Individual Node Status:" -ForegroundColor Yellow
$ports = @(8003, 8004, 8005, 8006, 8007)
$nodeNames = @("ratelimit-1", "ratelimit-2", "ratelimit-3", "ratelimit-4", "ratelimit-5")

for ($i = 0; $i -lt $ports.Length; $i++) {
    $port = $ports[$i]
    $name = $nodeNames[$i]

    try {
        $nodeStatus = Invoke-RestMethod -Uri "http://localhost:$port/raft/leader" -Method Get -TimeoutSec 2
        $state = if ($nodeStatus.is_leader) { "LEADER" } else { "FOLLOWER" }
        $color = if ($nodeStatus.is_leader) { "Green" } else { "White" }
        Write-Host "  $name (port $port): $state (term: $($nodeStatus.term))" -ForegroundColor $color
    }
    catch {
        Write-Host "  $name (port $port): UNREACHABLE" -ForegroundColor Red
    }
}

Write-Host "`n========================================" -ForegroundColor Cyan
Write-Host "Health Check Complete!" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
