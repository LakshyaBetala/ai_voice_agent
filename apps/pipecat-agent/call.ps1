# Place a Priya call. Usage:
#   .\call.ps1 9876543210
#   .\call.ps1 9876543210 Suresh
#   .\call.ps1 +919876543210 Suresh ta-IN
#
# Args: <number> [name] [lang]   lang = hi-IN (default) | ta-IN | en-IN
param(
    [Parameter(Mandatory = $true, Position = 0)][string]$To,
    [Parameter(Position = 1)][string]$Name = "Sir",
    [Parameter(Position = 2)][ValidateSet("hi-IN", "ta-IN", "en-IN")][string]$Lang = "hi-IN"
)

# Normalise: bare 10-digit Indian number -> +91XXXXXXXXXX. Keep +<...> as-is.
if ($To -notmatch '^\+') {
    $digits = $To -replace '\D', ''
    if ($digits.Length -eq 10) { $To = "+91$digits" } else { $To = "+$digits" }
}

$body = @{ to = $To; lead_first_name = $Name; lang_hint = $Lang } | ConvertTo-Json -Compress
Write-Host "Calling $To  (name=$Name, lang=$Lang) ..." -ForegroundColor Cyan
try {
    $resp = Invoke-RestMethod -Method Post -Uri "http://localhost:8080/exotel/calls" `
        -ContentType "application/json" -Body $body -TimeoutSec 20
    Write-Host "OK  call_sid=$($resp.call_sid)  status=$($resp.status)" -ForegroundColor Green
}
catch {
    Write-Host "FAILED: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "Is the agent running? Start it with .\start-stack.ps1" -ForegroundColor Yellow
}
