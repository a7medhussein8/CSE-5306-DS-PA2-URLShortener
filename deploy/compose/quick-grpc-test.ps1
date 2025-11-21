# Test gRPC Cluster Status
# Simple PowerShell script to check if your Raft cluster is working

Write-Host "`n================================================" -ForegroundColor Cyan
Write-Host "     Raft Cluster gRPC Status Check" -ForegroundColor Cyan
Write-Host "================================================`n" -ForegroundColor Cyan

# Check if Docker is running
try {
    $null = docker ps 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: Docker is not running!" -ForegroundColor Red
        exit 1
    }
} catch {
    Write-Host "ERROR: Docker is not available!" -ForegroundColor Red
    exit 1
}

# Define ports
$ports = 8003..8007

Write-Host "Checking cluster status...`n" -ForegroundColor Yellow

# Check each node
$results = @()
$successCount = 0

foreach ($port in $ports) {
    try {
        $status = Invoke-RestMethod -Uri "http://localhost:$port/raft/status" -TimeoutSec 2 -ErrorAction Stop
        
        # Determine color based on state
        if ($status.state.ToUpper() -eq "LEADER") {
            $color = "Green"
        } elseif ($status.state.ToUpper() -eq "FOLLOWER") {
            $color = "Yellow"
        } elseif ($status.state.ToUpper() -eq "CANDIDATE") {
            $color = "Magenta"
        } else {
            $color = "Red"
        }
        
        # Display status
        Write-Host "$($status.node_id): " -NoNewline
        Write-Host "$($status.state.ToUpper())" -ForegroundColor $color -NoNewline
        Write-Host " (term: $($status.term), leader: $($status.leader))"
        
        # Store result
        $results += [PSCustomObject]@{
            node = $status.node_id
            state = $status.state
            term = $status.term
            leader = $status.leader
        }
        
        $successCount++
    }
    catch {
        Write-Host "Port $port - " -NoNewline
        Write-Host "NOT RESPONDING" -ForegroundColor Red
    }
}

Write-Host "`n================================================" -ForegroundColor Cyan

# Analyze results
if ($successCount -eq 0) {
    Write-Host "❌ CLUSTER DOWN - No nodes responding!" -ForegroundColor Red
    Write-Host "`nTroubleshooting steps:" -ForegroundColor Yellow
    Write-Host "  1. Check containers: docker-compose ps" -ForegroundColor Gray
    Write-Host "  2. View logs: docker-compose logs" -ForegroundColor Gray
    Write-Host "  3. Restart cluster: docker-compose restart" -ForegroundColor Gray
}
elseif ($successCount -lt 5) {
    Write-Host "⚠️  PARTIAL OUTAGE - Only $successCount/5 nodes responding" -ForegroundColor Yellow
    Write-Host "`nSome containers may be stopped or crashed." -ForegroundColor Yellow
}
else {
    # Count states
    $leaderCount = ($results | Where-Object { $_.state -eq "leader" }).Count
    $followerCount = ($results | Where-Object { $_.state -eq "follower" }).Count
    $candidateCount = ($results | Where-Object { $_.state -eq "candidate" }).Count
    
    if ($leaderCount -eq 1 -and $followerCount -eq 4) {
        Write-Host "✅ CLUSTER HEALTHY" -ForegroundColor Green
        $leaderNode = ($results | Where-Object { $_.state -eq "leader" })[0]
        Write-Host "   Leader: $($leaderNode.leader)" -ForegroundColor Green
        Write-Host "   Term: $($leaderNode.term)" -ForegroundColor Green
    }
    elseif ($leaderCount -eq 0) {
        Write-Host "⚠️  NO LEADER - Election in progress" -ForegroundColor Yellow
        Write-Host "   Wait 10-30 seconds and check again" -ForegroundColor Yellow
        Write-Host "   Or check logs: docker-compose logs -f" -ForegroundColor Gray
    }
    elseif ($leaderCount -gt 1) {
        Write-Host "❌ SPLIT BRAIN - Multiple leaders detected!" -ForegroundColor Red
        Write-Host "   Leaders: $leaderCount" -ForegroundColor Red
        Write-Host "   Action needed: docker-compose restart" -ForegroundColor Yellow
    }
    elseif ($candidateCount -gt 0) {
        Write-Host "⚠️  ELECTION IN PROGRESS" -ForegroundColor Yellow
        Write-Host "   Candidates: $candidateCount" -ForegroundColor Yellow
        Write-Host "   This is normal, wait a few seconds..." -ForegroundColor Gray
    }
}

Write-Host "`n================================================" -ForegroundColor Cyan

# Show detailed node information if requested
if ($args -contains "-detailed") {
    Write-Host "`nDetailed Node Information:" -ForegroundColor Cyan
    foreach ($result in $results) {
        Write-Host "`n$($result.node):" -ForegroundColor White
        Write-Host "  State: $($result.state)" -ForegroundColor Gray
        Write-Host "  Term: $($result.term)" -ForegroundColor Gray
        Write-Host "  Leader: $($result.leader)" -ForegroundColor Gray
    }
    Write-Host ""
}

# Quick commands reference
Write-Host "`nQuick commands:" -ForegroundColor Cyan
Write-Host "  Check this status:  .\test-grpc-status.ps1" -ForegroundColor Gray
Write-Host "  View live logs:     docker-compose logs -f" -ForegroundColor Gray
Write-Host "  Restart cluster:    docker-compose restart" -ForegroundColor Gray
Write-Host "  Full rebuild:       docker-compose down && docker-compose up -d" -ForegroundColor Gray
Write-Host ""