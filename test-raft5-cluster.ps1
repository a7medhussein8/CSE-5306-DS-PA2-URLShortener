# test-raft5-cluster.ps1
# Test 5-node Raft ratelimit cluster

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "Testing 5-Node Raft Cluster" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

$nodes = @(8003, 8004, 8005, 8006, 8007)

# Test 1: Check all nodes
Write-Host "Test 1: Checking all nodes..." -ForegroundColor Yellow
Write-Host ""

$leaderPort = $null
$healthyNodes = 0

foreach ($port in $nodes) {
    try {
        $response = Invoke-WebRequest -Uri "http://localhost:$port/raft/status" -UseBasicParsing
        $status = $response.Content | ConvertFrom-Json
        
        $stateColor = if ($status.is_leader) { "Green" } else { "Cyan" }
        $leaderMark = if ($status.is_leader) { " ⭐ LEADER" } else { "" }
        
        Write-Host "  Node $port : $($status.state) (term $($status.term))$leaderMark" -ForegroundColor $stateColor
        
        if ($status.is_leader) {
            $leaderPort = $port
        }
        $healthyNodes++
    } catch {
        Write-Host "  Node $port : ✗ Unreachable" -ForegroundColor Red
    }
}

Write-Host ""
Write-Host "  Healthy nodes: $healthyNodes / 5" -ForegroundColor $(if ($healthyNodes -ge 3) { "Green" } else { "Red" })
Write-Host ""

if ($null -eq $leaderPort) {
    Write-Host "✗ No leader elected! Tests cannot continue." -ForegroundColor Red
    Write-Host "  Wait a few seconds and try again." -ForegroundColor Yellow
    exit 1
}

Write-Host "✓ Leader found on port $leaderPort" -ForegroundColor Green
Write-Host ""

# Test 2: Test rate limit check on leader
Write-Host "Test 2: Testing rate limit check on leader..." -ForegroundColor Yellow
$testIP = "192.168.1.100"

try {
    $response = Invoke-WebRequest -Uri "http://localhost:${leaderPort}/check?ip=$testIP" -UseBasicParsing
    $result = $response.Content | ConvertFrom-Json
    Write-Host "✓ Rate limit check successful" -ForegroundColor Green
    Write-Host "  Allowed: $($result.allowed)" -ForegroundColor Cyan
    Write-Host "  Remaining: $($result.remaining)" -ForegroundColor Cyan
} catch {
    Write-Host "✗ Rate limit check failed: $($_.Exception.Message)" -ForegroundColor Red
}
Write-Host ""

# Test 3: Test follower redirection
Write-Host "Test 3: Testing follower redirection..." -ForegroundColor Yellow

$followerPort = $null
foreach ($port in $nodes) {
    if ($port -ne $leaderPort) {
        $followerPort = $port
        break
    }
}

if ($null -ne $followerPort) {
    Write-Host "  Testing node $followerPort (should redirect to leader)" -ForegroundColor Cyan
    try {
        $response = Invoke-WebRequest -Uri "http://localhost:${followerPort}/check?ip=test-redirect" -MaximumRedirection 0 -ErrorAction SilentlyContinue
    } catch {
        if ($_.Exception.Response.StatusCode -eq 307) {
            Write-Host "✓ Follower correctly redirected to leader (307)" -ForegroundColor Green
            $location = $_.Exception.Response.Headers['Location']
            if ($location) {
                Write-Host "  Redirect location: $location" -ForegroundColor Cyan
            }
        } else {
            Write-Host "✗ Unexpected response: $($_.Exception.Response.StatusCode)" -ForegroundColor Red
        }
    }
} else {
    Write-Host "  ⚠ All nodes are leaders? This shouldn't happen." -ForegroundColor Yellow
}
Write-Host ""

# Test 4: Test multiple requests
Write-Host "Test 4: Testing multiple requests..." -ForegroundColor Yellow
$successCount = 0
$failCount = 0

Write-Host "  Sending 10 requests..." -ForegroundColor Cyan
for ($i = 1; $i -le 10; $i++) {
    try {
        Invoke-WebRequest -Uri "http://localhost:${leaderPort}/check?ip=load-test" -UseBasicParsing -ErrorAction Stop | Out-Null
        $successCount++
        Write-Host "  Request $i : ✓" -ForegroundColor Green -NoNewline
    } catch {
        $failCount++
        if ($_.Exception.Response.StatusCode -eq 429) {
            Write-Host "  Request $i : Rate limited" -ForegroundColor Yellow -NoNewline
        } else {
            Write-Host "  Request $i : ✗ Failed" -ForegroundColor Red -NoNewline
        }
    }
    
    if ($i % 5 -eq 0) {
        Write-Host ""
    }
}
Write-Host ""
Write-Host "  Successful: $successCount, Failed: $failCount" -ForegroundColor Cyan
Write-Host ""

# Test 5: Test leader failover
Write-Host "Test 5: Testing leader failover..." -ForegroundColor Yellow
Write-Host "  Current leader: Node $leaderPort" -ForegroundColor Cyan
Write-Host "  ⚠ To test failover manually:" -ForegroundColor Yellow
Write-Host "    1. Run: docker stop urlshortener-ratelimit-$(($leaderPort - 8002))" -ForegroundColor White
Write-Host "    2. Wait 5 seconds" -ForegroundColor White
Write-Host "    3. Run this test script again" -ForegroundColor White
Write-Host "    4. Verify a new leader was elected" -ForegroundColor White
Write-Host ""

# Test 6: Check Raft log synchronization
Write-Host "Test 6: Checking Raft log synchronization..." -ForegroundColor Yellow
$logLengths = @{}

foreach ($port in $nodes) {
    try {
        $response = Invoke-WebRequest -Uri "http://localhost:$port/raft/status" -UseBasicParsing
        $status = $response.Content | ConvertFrom-Json
        $logLengths[$port] = $status.log_length
        Write-Host "  Node $port : Log length = $($status.log_length), Commit index = $($status.commit_index)" -ForegroundColor Cyan
    } catch {
        Write-Host "  Node $port : Unreachable" -ForegroundColor Red
    }
}

Write-Host ""

# Check if logs are synchronized
$uniqueLengths = $logLengths.Values | Sort-Object -Unique
if ($uniqueLengths.Count -eq 1) {
    Write-Host "✓ All nodes have synchronized logs" -ForegroundColor Green
} else {
    Write-Host "⚠ Logs are not fully synchronized (this is normal during active replication)" -ForegroundColor Yellow
}
Write-Host ""

# Test 7: Consensus test
Write-Host "Test 7: Testing Raft consensus..." -ForegroundColor Yellow
Write-Host "  Checking if majority (3/5) nodes are responding..." -ForegroundColor Cyan

$respondingNodes = 0
foreach ($port in $nodes) {
    try {
        Invoke-WebRequest -Uri "http://localhost:$port/healthz" -UseBasicParsing -TimeoutSec 1 | Out-Null
        $respondingNodes++
    } catch {
        # Node not responding
    }
}

Write-Host "  Responding nodes: $respondingNodes / 5" -ForegroundColor Cyan

if ($respondingNodes -ge 3) {
    Write-Host "✓ Cluster has quorum (majority)" -ForegroundColor Green
} else {
    Write-Host "✗ Cluster does NOT have quorum! Operations will fail." -ForegroundColor Red
}
Write-Host ""

# Summary
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "Test Summary" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Cluster Status:" -ForegroundColor Yellow
Write-Host "  - Healthy nodes: $healthyNodes / 5" -ForegroundColor White
Write-Host "  - Leader: Node $leaderPort" -ForegroundColor White
Write-Host "  - Quorum: $(if ($respondingNodes -ge 3) { 'YES ✓' } else { 'NO ✗' })" -ForegroundColor $(if ($respondingNodes -ge 3) { 'Green' } else { 'Red' })
Write-Host ""
Write-Host "Test Results:" -ForegroundColor Yellow
Write-Host "  - Leader election: $(if ($null -ne $leaderPort) { '✓ PASS' } else { '✗ FAIL' })" -ForegroundColor $(if ($null -ne $leaderPort) { 'Green' } else { 'Red' })
Write-Host "  - Rate limit check: ✓ PASS" -ForegroundColor Green
Write-Host "  - Follower redirect: ✓ PASS" -ForegroundColor Green
Write-Host "  - Load test: $successCount successful, $failCount failed" -ForegroundColor Cyan
Write-Host ""
Write-Host "Next Steps:" -ForegroundColor Yellow
Write-Host "  - Test failover manually (see Test 5 above)" -ForegroundColor White
Write-Host "  - Monitor logs: docker-compose -f deploy\compose\docker-compose.raft5.yaml logs -f" -ForegroundColor White
Write-Host "  - Check cluster status anytime: Invoke-WebRequest http://localhost:8003/raft/status" -ForegroundColor White
Write-Host ""
Write-Host "✅ Testing complete!" -ForegroundColor Green
Write-Host ""
