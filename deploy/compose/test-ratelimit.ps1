#!/usr/bin/env pwsh
# Test Rate Limit Service with Raft Cluster

$GATEWAY_URL = "http://localhost:8080"
$RATE_LIMIT = 120
$TEST_ITERATIONS = 130  # Test beyond the limit

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "Rate Limit Service Test" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "Gateway URL: $GATEWAY_URL" -ForegroundColor Yellow
Write-Host "Rate Limit: $RATE_LIMIT requests per minute" -ForegroundColor Yellow
Write-Host ""

# Function to check cluster status
function Get-ClusterStatus {
    Write-Host "`n--- Cluster Status ---" -ForegroundColor Green
    try {
        $response = Invoke-RestMethod -Uri "$GATEWAY_URL/raft/status" -Method Get -ErrorAction Stop
        Write-Host "Leader: $($response.leader)" -ForegroundColor Yellow
        Write-Host "Cached Leader: $($response.cached_leader)" -ForegroundColor Yellow
        Write-Host "Total Nodes: $($response.total_nodes)" -ForegroundColor Yellow

        Write-Host "`nNode States:" -ForegroundColor Cyan
        foreach ($node in $response.cluster.PSObject.Properties) {
            $state = $node.Value.state
            $term = $node.Value.term
            $color = if ($state -eq "LEADER") { "Green" } else { "White" }
            Write-Host "  $($node.Name): $state (term: $term)" -ForegroundColor $color
        }
        return $response.leader
    }
    catch {
        Write-Host "Error getting cluster status: $_" -ForegroundColor Red
        return $null
    }
}

# Function to check leader info
function Get-LeaderInfo {
    Write-Host "`n--- Leader Information ---" -ForegroundColor Green
    try {
        $response = Invoke-RestMethod -Uri "$GATEWAY_URL/raft/leader" -Method Get -ErrorAction Stop
        Write-Host "Leader URL: $($response.leader_url)" -ForegroundColor Yellow
        Write-Host "Leader Node ID: $($response.leader_info.node_id)" -ForegroundColor Yellow
        Write-Host "Is Leader: $($response.leader_info.is_leader)" -ForegroundColor Yellow
        Write-Host "Term: $($response.leader_info.term)" -ForegroundColor Yellow
        Write-Host "Cached: $($response.cached)" -ForegroundColor $(if ($response.cached) { "Yellow" } else { "Cyan" })
        return $response.leader_url
    }
    catch {
        Write-Host "Error getting leader info: $_" -ForegroundColor Red
        return $null
    }
}

# Function to test rate limiting
function Test-RateLimit {
    param (
        [int]$Iterations = 130
    )

    Write-Host "`n--- Testing Rate Limit ($Iterations requests) ---" -ForegroundColor Green

    $allowed = 0
    $denied = 0
    $errors = 0
    $firstDenied = -1

    for ($i = 1; $i -le $Iterations; $i++) {
        try {
            # Create a test URL shortening request
            $body = @{
                long_url = "https://example.com/test-$i"
            } | ConvertTo-Json

            $response = Invoke-WebRequest -Uri "$GATEWAY_URL/shorten" -Method Post `
                -Body $body -ContentType "application/json" -ErrorAction Stop

            $allowed++

            # Progress indicator
            if ($i % 10 -eq 0) {
                Write-Host "." -NoNewline -ForegroundColor Green
            }
        }
        catch {
            $statusCode = $_.Exception.Response.StatusCode.value__

            if ($statusCode -eq 429) {
                $denied++
                if ($firstDenied -eq -1) {
                    $firstDenied = $i
                    Write-Host "`n  ✗ First rate limit hit at request #$i" -ForegroundColor Yellow
                }

                # Show progress for rate limited requests
                if ($denied % 5 -eq 0) {
                    Write-Host "x" -NoNewline -ForegroundColor Red
                }
            }
            else {
                $errors++
                Write-Host "`n  ✗ Error at request #$i : Status $statusCode" -ForegroundColor Red
            }
        }

        # Small delay to avoid overwhelming the system
        Start-Sleep -Milliseconds 50
    }

    Write-Host ""
    Write-Host "`n--- Rate Limit Test Results ---" -ForegroundColor Cyan
    Write-Host "  ✓ Allowed: $allowed" -ForegroundColor Green
    Write-Host "  ✗ Denied (429): $denied" -ForegroundColor $(if ($denied -gt 0) { "Yellow" } else { "Red" })
    Write-Host "  ✗ Errors: $errors" -ForegroundColor $(if ($errors -gt 0) { "Red" } else { "Gray" })
    Write-Host "  First denied at request: $firstDenied" -ForegroundColor Yellow

    # Verify rate limit is working correctly
    if ($allowed -le $RATE_LIMIT -and $denied -gt 0) {
        Write-Host "`n✓ PASS: Rate limiting is working correctly!" -ForegroundColor Green
    }
    elseif ($denied -eq 0) {
        Write-Host "`n✗ FAIL: No requests were rate limited!" -ForegroundColor Red
    }
    else {
        Write-Host "`n⚠ WARNING: Rate limit behavior unexpected" -ForegroundColor Yellow
        Write-Host "  Expected ~$RATE_LIMIT allowed, got $allowed" -ForegroundColor Yellow
    }

    return @{
        Allowed = $allowed
        Denied = $denied
        Errors = $errors
        FirstDenied = $firstDenied
    }
}

# Function to test direct rate limit check endpoint
function Test-DirectRateLimit {
    Write-Host "`n--- Testing Direct Rate Limit Check ---" -ForegroundColor Green

    $testIP = "192.168.1.100"
    $success = 0
    $limited = 0

    for ($i = 1; $i -le 125; $i++) {
        try {
            $leader = Get-LeaderInfo
            if (-not $leader) {
                Write-Host "No leader found, waiting..." -ForegroundColor Yellow
                Start-Sleep -Seconds 2
                continue
            }

            # Call leader directly
            $leaderURL = $leader -replace "http://ratelimit-(\d+):8003", "http://localhost:800$([int]'$1' + 2)"

            $body = @{
                ip = $testIP
            } | ConvertTo-Json

            $response = Invoke-RestMethod -Uri "$leaderURL/check" -Method Post `
                -Body $body -ContentType "application/json" -ErrorAction Stop

            if ($response.allowed) {
                $success++
                if ($i % 10 -eq 0) {
                    Write-Host "." -NoNewline -ForegroundColor Green
                }
            }
            else {
                $limited++
                if ($limited -eq 1) {
                    Write-Host "`n  ✗ Rate limited at request #$i" -ForegroundColor Yellow
                }
            }
        }
        catch {
            Write-Host "x" -NoNewline -ForegroundColor Red
        }

        Start-Sleep -Milliseconds 50
    }

    Write-Host ""
    Write-Host "`n--- Direct Rate Limit Results ---" -ForegroundColor Cyan
    Write-Host "  ✓ Allowed: $success" -ForegroundColor Green
    Write-Host "  ✗ Limited: $limited" -ForegroundColor Yellow
}

# Main test execution
Write-Host "`n=== Step 1: Check Cluster Health ===" -ForegroundColor Magenta
Get-ClusterStatus

Write-Host "`n=== Step 2: Verify Leader ===" -ForegroundColor Magenta
Get-LeaderInfo

Write-Host "`n=== Step 3: Test Rate Limiting via Gateway ===" -ForegroundColor Magenta
$results = Test-RateLimit -Iterations $TEST_ITERATIONS

# Optional: Test direct endpoint
$testDirect = Read-Host "`nDo you want to test direct rate limit endpoint? (y/n)"
if ($testDirect -eq "y") {
    Test-DirectRateLimit
}

Write-Host "`n============================================================" -ForegroundColor Cyan
Write-Host "Rate Limit Test Complete" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""
