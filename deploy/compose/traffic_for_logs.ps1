$ips = "1.2.3.4"
$URL = "http://localhost:8003/check"

Write-Host "Sending 10 POST requests to $URL..."

for ($i=1; $i -le 10; $i++) {
    try {
        $resp = Invoke-RestMethod -Uri $URL -Method POST -Body ("{""ip"":""$ips""}") -ContentType "application/json"
        Write-Host "[$i] allowed=$($resp.allowed)  count=$($resp.current_count)"
    }
    catch {
        Write-Host "[$i] error: $($_.Exception.Message)" -ForegroundColor Red
    }
}

Write-Host "Done."
