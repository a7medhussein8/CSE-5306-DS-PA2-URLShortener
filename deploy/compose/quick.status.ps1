# Simple Raft Cluster Status Check
# Quick health check for your 5-node Raft cluster

Write-Host "`n================================================" -ForegroundColor Cyan
Write-Host "     Raft Cluster Quick Status Check" -ForegroundColor Cyan
Write-Host "================================================`n" -ForegroundColor Cyan

# Check if Docker is running
try {
    docker ps > $null 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: Docker is not running!" -ForegroundColor Red
        exit 1
    }
} catch {
    Write-Host "ERROR: Docker is not available!" -ForegroundColor Red
    exit 1
}

# Define nodes
$ports = 8003..8007

Write-Host "Checking cluster status...`n" -ForegroundColor Yellow

# Check each node
$results = @()
foreach ($port in $ports) {
    try {
        $status = Invoke-RestMethod -Uri "http://localhost:$port/raft/status" -TimeoutSec 2 -ErrorAction Stop
        
        $color = switch ($status.state.ToUpper()) {
            "LEADER" { "Green" }
            "FOLLOWER" { "Yellow" }
            "CANDIDATE" { "Magenta" }
            default { "Red" }
        }
        
        Write-Host "$($status.node_id):" -NoNewline
        Write-Host " $($status.state.ToUpper())" -ForegroundColor $color -NoNewline
        Write-Host " (term: $($status.term), leader: $($status.leader))"
        
        $results += @{
            node = $status.node_id
            state = $status.state
            term = $status.term
            leader = $status.leader
        }
    }
    catch {
        Write-Host "Port $port - " -NoNewline
        Write-Host "NOT RESPONDING" -ForegroundColor Red
    }
}

Write-Host "`n================================================" -ForegroundColor Cyan

# Analyze results
if ($results.Count -eq 0) {
    Write-Host "❌ CLUSTER DOWN - No nodes responding!" -ForegroundColor Red
    Write-Host "`nTry: docker-compose ps" -ForegroundColor Yellow
}
elseif ($results.Count -lt 5) {
    Write-Host "⚠️  PARTIAL OUTAGE - Only $($results.Count)/5 nodes responding" -ForegroundColor Yellow
}
else {
    $leaders = $results | Where-Object { $_.state -eq "leader" }
    $followers = $results | Where-Object { $_.state -eq "follower" }
    $candidates = $results | Where-Object { $_.state -eq "candidate" }
    
    if ($leaders.Count -eq 1 -and $followers.Count -eq 4) {
        Write-Host "✅ CLUSTER HEALTHY" -ForegroundColor Green
        Write-Host "   Leader: $($leaders[0].leader)" -ForegroundColor Green
        Write-Host "   Term: $($leaders[0].term)" -ForegroundColor Green
    }
    elseif ($leaders.Count -eq 0) {
        Write-Host "⚠️  NO LEADER - Election in progress" -ForegroundColor Yellow
        Write-Host "   Wait 10-30 seconds and check again" -ForegroundColor Yellow
    }
    elseif ($leaders.Count -gt 1) {
        Write-Host "❌ SPLIT BRAIN - Multiple leaders detected!" -ForegroundColor Red
        Write-Host "   Leaders: $($leaders.Count)" -ForegroundColor Red
    }
    elseif ($candidates.Count -gt 0) {
        Write-Host "⚠️  ELECTION IN PROGRESS" -ForegroundColor Yellow
        Write-Host "   Candidates: $($candidates.Count)" -ForegroundColor Yellow
    }
}

Write-Host "`n================================================`n" -ForegroundColor Cyan

# Quick commands
Write-Host "Quick commands:" -ForegroundColor Cyan
Write-Host "  View logs:   docker-compose logs -f" -ForegroundColor Gray
Write-Host "  Restart:     docker-compose restart" -ForegroundColor Gray
Write-Host "  Full reset:  docker-compose down && docker-compose up -d" -ForegroundColor Gray
Write-Host ""