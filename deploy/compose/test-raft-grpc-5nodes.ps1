# test-raft-grpc-5nodes.ps1
param(
    [string[]]$Containers = @("ratelimit-1", "ratelimit-2", "ratelimit-3", "ratelimit-4", "ratelimit-5")
)

Write-Host "`n========================================" -ForegroundColor Cyan
Write-Host "  5-Node Raft gRPC Communication Test" -ForegroundColor Cyan
Write-Host "========================================`n" -ForegroundColor Cyan

$totalTests = 0
$passedTests = 0

foreach ($sourceContainer in $Containers) {
    Write-Host "From: $sourceContainer" -ForegroundColor White
    
    foreach ($targetContainer in $Containers) {
        if ($sourceContainer -eq $targetContainer) {
            continue
        }
        
        $totalTests++
        
        $grpcTest = @"
import grpc
import raft_pb2
import raft_pb2_grpc
try:
    channel = grpc.insecure_channel('$targetContainer:50051', options=[('grpc.keepalive_time_ms', 30000)])
    stub = raft_pb2_grpc.RaftServiceStub(channel)
    request = raft_pb2.RequestVoteRequest(term=999, candidate_id='test', last_log_index=0, last_log_term=0)
    response = stub.RequestVote(request, timeout=2)
    print('OK')
except Exception as e:
    print(f'ERROR: {e}')
"@
        
        try {
            $result = docker exec $sourceContainer python -c $grpcTest 2>&1
            
            if ($result -match "OK") {
                Write-Host "  → $targetContainer : " -NoNewline
                Write-Host "✅ gRPC Working" -ForegroundColor Green
                $passedTests++
            } else {
                Write-Host "  → $targetContainer : " -NoNewline
                Write-Host "❌ Failed - $result" -ForegroundColor Red
            }
        } catch {
            Write-Host "  → $targetContainer : " -NoNewline
            Write-Host "❌ Error" -ForegroundColor Red
        }
    }
    Write-Host ""
}

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "Summary:" -ForegroundColor Cyan
Write-Host "  Total gRPC Tests: $totalTests (20 connections)" -ForegroundColor White
Write-Host "  Working:          $passedTests" -ForegroundColor Green
Write-Host "  Failed:           $($totalTests - $passedTests)" -ForegroundColor Red

if ($totalTests -eq $passedTests) {
    Write-Host "`n✅ All 20 gRPC connections working!" -ForegroundColor Green
} else {
    Write-Host "`n⚠️  Some gRPC connections failed!" -ForegroundColor Yellow
}

Write-Host "========================================`n" -ForegroundColor Cyan