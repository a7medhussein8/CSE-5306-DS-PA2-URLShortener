# Raft Cluster Diagnostic Script for Windows
# Run this to check your cluster health and connectivity

$ErrorActionPreference = "Continue"

function Write-ColorOutput {
    param(
        [string]$Message,
        [ConsoleColor]$Color = [ConsoleColor]::White
    )
    $previousColor = $host.UI.RawUI.ForegroundColor
    $host.UI.RawUI.ForegroundColor = $Color
    Write-Host $Message
    $host.UI.RawUI.ForegroundColor = $previousColor
}

function Write-Success {
    param([string]$Message)
    Write-ColorOutput "✓ $Message" ([ConsoleColor]::Green)
}

function Write-Failed {
    param([string]$Message)
    Write-ColorOutput "✗ $Message" ([ConsoleColor]::Red)
}

function Write-Warn {
    param([string]$Message)
    Write-ColorOutput "⚠ $Message" ([ConsoleColor]::Yellow)
}

function Write-InfoMsg {
    param([string]$Message)
    Write-ColorOutput "ℹ $Message" ([ConsoleColor]::Cyan)
}

function Write-Header {
    param([string]$Message)
    Write-Host ""
    Write-ColorOutput "═══════════════════════════════════════════" ([ConsoleColor]::Blue)
    Write-ColorOutput " $Message" ([ConsoleColor]::Blue)
    Write-ColorOutput "═══════════════════════════════════════════" ([ConsoleColor]::Blue)
    Write-Host ""
}

Write-Header "Raft Cluster Diagnostics"

# Configuration
$nodes = @(
    @{name="ratelimit-1"; httpPort=8003; grpcPort=50051},
    @{name="ratelimit-2"; httpPort=8004; grpcPort=50052},
    @{name="ratelimit-3"; httpPort=8005; grpcPort=50053},
    @{name="ratelimit-4"; httpPort=8006; grpcPort=50054},
    @{name="ratelimit-5"; httpPort=8007; grpcPort=50055}
)

# Test 1: Check Docker is running
Write-Header "Test 1: Docker Service Status"
try {
    $dockerVersion = docker --version 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Success "Docker is installed: $dockerVersion"
    } else {
        Write-Failed "Docker command failed"
        exit 1
    }
} catch {
    Write-Failed "Docker is not installed or not in PATH"
    exit 1
}

# Test 2: Check Docker Compose
Write-Header "Test 2: Docker Compose Status"
try {
    $composeVersion = docker-compose --version 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Success "Docker Compose is installed: $composeVersion"
    } else {
        Write-Warn "Docker Compose not found (may be integrated into Docker)"
    }
} catch {
    Write-Warn "Docker Compose not available as standalone command"
}

# Test 3: Check containers
Write-Header "Test 3: Container Status"
$runningContainers = @()
$stoppedContainers = @()

foreach ($node in $nodes) {
    try {
        $status = docker inspect -f '{{.State.Status}}' $node.name 2>&1
        if ($LASTEXITCODE -eq 0 -and $status -eq "running") {
            Write-Success "$($node.name) is RUNNING"
            $runningContainers += $node.name
        } elseif ($status -eq "exited" -or $status -eq "stopped") {
            Write-Failed "$($node.name) is STOPPED"
            $stoppedContainers += $node.name
        } else {
            Write-Failed "$($node.name) not found"
        }
    } catch {
        Write-Failed "$($node.name) not found or error checking status"
    }
}

Write-Host ""
Write-InfoMsg "Running: $($runningContainers.Count)/5 containers"
if ($stoppedContainers.Count -gt 0) {
    Write-Warn "Stopped containers: $($stoppedContainers -join ', ')"
}

# Test 4: Check network
Write-Header "Test 4: Docker Network"
try {
    $networkCheck = docker network inspect raft-network 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Success "raft-network exists"
        
        # Count containers on network
        $networkJson = docker network inspect raft-network | ConvertFrom-Json
        $containersOnNetwork = ($networkJson[0].Containers | Measure-Object).Count
        Write-InfoMsg "Containers on network: $containersOnNetwork"
    } else {
        Write-Failed "raft-network not found"
    }
} catch {
    Write-Failed "Error inspecting network: $($_.Exception.Message)"
}

# Test 5: Check HTTP endpoints
Write-Header "Test 5: HTTP API Endpoints"
$httpResults = @{}

foreach ($node in $nodes) {
    try {
        $url = "http://localhost:$($node.httpPort)/healthz"
        $response = Invoke-WebRequest -Uri $url -TimeoutSec 3 -ErrorAction Stop
        
        if ($response.StatusCode -eq 200) {
            Write-Success "$($node.name) HTTP API responding on port $($node.httpPort)"
            $httpResults[$node.name] = $true
        }
    } catch {
        Write-Failed "$($node.name) HTTP API not responding on port $($node.httpPort)"
        $httpResults[$node.name] = $false
    }
}

# Test 6: Check Raft status
Write-Header "Test 6: Raft Cluster Status"
$raftResults = @{}
$leaderFound = $false
$leaderNode = $null

foreach ($node in $nodes) {
    if ($httpResults[$node.name]) {
        try {
            $url = "http://localhost:$($node.httpPort)/raft/status"
            $raftStatus = Invoke-RestMethod -Uri $url -TimeoutSec 3 -ErrorAction Stop
            
            $state = $raftStatus.state
            $term = $raftStatus.term
            $leader = $raftStatus.leader
            
            # Color code by state
            $color = switch ($state.ToUpper()) {
                "LEADER" { [ConsoleColor]::Green }
                "FOLLOWER" { [ConsoleColor]::Yellow }
                "CANDIDATE" { [ConsoleColor]::Magenta }
                default { [ConsoleColor]::Red }
            }
            
            Write-Host "  $($node.name): " -NoNewline
            Write-ColorOutput "$state" $color
            Write-Host "    Term: $term"
            Write-Host "    Leader: $leader"
            Write-Host "    Log Size: $($raftStatus.log_size)"
            Write-Host "    Commit Index: $($raftStatus.commit_index)"
            Write-Host ""
            
            $raftResults[$node.name] = @{
                state = $state
                term = $term
                leader = $leader
            }
            
            if ($state.ToUpper() -eq "LEADER") {
                $leaderFound = $true
                $leaderNode = $node.name
            }
        } catch {
            Write-Failed "$($node.name) failed to get Raft status: $($_.Exception.Message)"
        }
    }
}

# Check leader status
if ($leaderFound) {
    Write-Success "Cluster has a leader: $leaderNode"
} else {
    Write-Failed "No leader elected! Cluster is in election or split-brain"
}

# Test 7: Check gRPC ports
Write-Header "Test 7: gRPC Port Accessibility"
foreach ($node in $nodes) {
    try {
        $tcpClient = New-Object System.Net.Sockets.TcpClient
        $connect = $tcpClient.BeginConnect("localhost", $node.grpcPort, $null, $null)
        $wait = $connect.AsyncWaitHandle.WaitOne(1000, $false)
        
        if ($wait -and $tcpClient.Connected) {
            Write-Success "$($node.name) gRPC port $($node.grpcPort) is OPEN"
            $tcpClient.Close()
        } else {
            Write-Failed "$($node.name) gRPC port $($node.grpcPort) is CLOSED"
            $tcpClient.Close()
        }
    } catch {
        Write-Failed "$($node.name) gRPC port $($node.grpcPort) is CLOSED or unreachable"
    }
}

# Test 8: Check logs for errors
Write-Header "Test 8: Recent Error Logs"
Write-InfoMsg "Checking last 50 lines of logs for errors..."

foreach ($node in $nodes) {
    if ($runningContainers -contains $node.name) {
        try {
            $logs = docker logs --tail 50 $node.name 2>&1 | Select-String -Pattern "error|ERROR|failed|FAILED|exception|Exception" | Select-Object -First 5
            
            if ($logs) {
                Write-Warn "$($node.name) has recent errors:"
                foreach ($line in $logs) {
                    Write-Host "    $line" -ForegroundColor Yellow
                }
                Write-Host ""
            } else {
                Write-Success "$($node.name) no recent errors found"
            }
        } catch {
            Write-Warn "Could not check logs for $($node.name)"
        }
    }
}

# Test 9: Check container environment variables
Write-Header "Test 9: Container Configuration"
foreach ($node in $nodes) {
    if ($runningContainers -contains $node.name) {
        try {
            Write-InfoMsg "$($node.name) environment:"
            $env = docker exec $node.name env 2>&1 | Select-String -Pattern "RAFT|NODE_ID|REDIS"
            foreach ($line in $env) {
                Write-Host "    $line"
            }
            Write-Host ""
        } catch {
            Write-Warn "Could not check environment for $($node.name)"
        }
    }
}

# Summary
Write-Header "Summary"

$passedTests = 0
$totalTests = 9

# Container status
if ($runningContainers.Count -eq 5) {
    Write-Success "All containers running"
    $passedTests++
} else {
    Write-Failed "Not all containers running ($($runningContainers.Count)/5)"
}

# HTTP APIs
$httpSuccess = ($httpResults.Values | Where-Object { $_ -eq $true }).Count
if ($httpSuccess -eq 5) {
    Write-Success "All HTTP APIs responding"
    $passedTests++
} else {
    Write-Failed "Some HTTP APIs not responding ($httpSuccess/5)"
}

# Leader election
if ($leaderFound) {
    Write-Success "Leader elected"
    $passedTests++
} else {
    Write-Failed "No leader elected"
}

Write-Host ""
$summaryColor = if ($passedTests -eq $totalTests) { [ConsoleColor]::Green } else { [ConsoleColor]::Yellow }
Write-ColorOutput "Test Results: $passedTests/$totalTests core tests passed" $summaryColor

# Recommendations
if ($passedTests -lt $totalTests) {
    Write-Header "Recommendations"
    
    if ($runningContainers.Count -lt 5) {
        Write-InfoMsg "1. Start missing containers:"
        Write-Host "     docker-compose up -d"
    }
    
    if (-not $leaderFound) {
        Write-InfoMsg "2. Check for gRPC connectivity issues:"
        Write-Host "     docker-compose logs | Select-String 'grpc|GRPC|error|ERROR'"
    }
    
    Write-InfoMsg "3. Rebuild containers if issues persist:"
    Write-Host "     docker-compose down"
    Write-Host "     docker-compose build --no-cache"
    Write-Host "     docker-compose up -d"
    
    Write-InfoMsg "4. Run the Python debug script inside a container:"
    Write-Host "     docker cp debug_grpc.py ratelimit-1:/app/"
    Write-Host "     docker exec -it ratelimit-1 python /app/debug_grpc.py"
}

Write-Host ""
Write-ColorOutput "Diagnostic complete!" ([ConsoleColor]::Cyan)
Write-Host ""