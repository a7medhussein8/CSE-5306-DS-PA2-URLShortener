$nodes = @(
    @{name="ratelimit-1"; port=8003},
    @{name="ratelimit-2"; port=8004},
    @{name="ratelimit-3"; port=8005},
    @{name="ratelimit-4"; port=8006},
    @{name="ratelimit-5"; port=8007}
)

Write-Host ""
Write-Host "==========  RAFT CLUSTER STATUS  ==========" -ForegroundColor Cyan
Write-Host ""

foreach ($n in $nodes) {

    $url = "http://localhost:$($n.port)/raft/status"

    try {
        $res = Invoke-RestMethod -Uri $url -TimeoutSec 2

        # Color by role ------------------------------------------
        switch ($res.state.ToUpper()) {
            "LEADER"    { $roleColor = "Green" }
            "FOLLOWER"  { $roleColor = "Yellow" }
            "CANDIDATE" { $roleColor = "Magenta" }
            default     { $roleColor = "Red" }
        }

        Write-Host ("`nNode: {0}" -f $res.node_id) -ForegroundColor White
        Write-Host (" Role: {0}" -f $res.state.ToUpper()) -ForegroundColor $roleColor
        Write-Host (" Term: {0}" -f $res.term) -ForegroundColor Gray
        Write-Host (" Leader: {0}" -f $res.leader) -ForegroundColor Gray
        Write-Host (" Log Size: {0}" -f $res.log_size) -ForegroundColor Gray
        Write-Host (" Last Log Index: {0}" -f $res.log_size) -ForegroundColor Gray
        Write-Host (" Commit Index: {0}" -f $res.commit_index) -ForegroundColor Gray
        Write-Host (" Last Applied: {0}" -f $res.last_applied) -ForegroundColor Gray

    }
    catch {
        Write-Host ("Node {0} is DOWN or NOT RESPONDING" -f $n.name) -ForegroundColor Red
    }
}

Write-Host ""
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host ""
