# start.ps1 — Launch the full City Sensor Dashboard stack
# ============================================================
# Starts all services in the correct order:
#   1. Redis (must already be running or started separately)
#   2. Server B  (city zones 151-300)
#   3. Server A  (city zones 1-150, also relays B via Redis Pub/Sub)
#   4. Vite dev  (React dashboard at http://localhost:5173)
#
# Usage:
#   .\start.ps1
#
# Prerequisites:
#   - Redis must be installed and reachable on 127.0.0.1:6379
#     (Run `redis-server` in a separate terminal if needed)
#   - Python packages: fastapi uvicorn redis noise Pillow scipy numpy
#     Install: pip install fastapi "uvicorn[standard]" redis noise Pillow scipy numpy
#   - Node packages already installed (run `npm install` in city-dashboard/ first)

$Root      = Split-Path -Parent $MyInvocation.MyCommand.Path
$Dashboard = Join-Path $Root "city-dashboard"

Write-Host "`n=== City Sensor Dashboard Startup ===" -ForegroundColor Cyan

# ── 1. Check Redis connectivity ───────────────────────────────────────────────
Write-Host "`n[1/4] Checking Redis connectivity..." -ForegroundColor Yellow
$redisOk = $false
try {
    $ping = & redis-cli PING 2>$null
    if ($ping -eq "PONG") { $redisOk = $true }
} catch { }

if (-not $redisOk) {
    Write-Host "      Redis is not responding. Please start redis-server first." -ForegroundColor Red
    Write-Host "      Run: redis-server" -ForegroundColor Gray
    exit 1
}
Write-Host "      Redis OK." -ForegroundColor Green

# ── 2. Start Server B ─────────────────────────────────────────────────────────
Write-Host "`n[2/4] Starting Server B (zones 151-300) on port 8002..." -ForegroundColor Yellow
$procB = Start-Process -FilePath "python" `
    -ArgumentList "server_b.py" `
    -WorkingDirectory $Root `
    -WindowStyle Normal `
    -PassThru
Write-Host "      Server B PID: $($procB.Id)" -ForegroundColor Green

Start-Sleep -Seconds 1

# ── 3. Start Server A ─────────────────────────────────────────────────────────
Write-Host "`n[3/4] Starting Server A (zones 1-150 + SSE relay) on port 8001..." -ForegroundColor Yellow
$procA = Start-Process -FilePath "python" `
    -ArgumentList "server_a.py" `
    -WorkingDirectory $Root `
    -WindowStyle Normal `
    -PassThru
Write-Host "      Server A PID: $($procA.Id)" -ForegroundColor Green

Start-Sleep -Seconds 2

# ── 4. Start Vite dev server ──────────────────────────────────────────────────
Write-Host "`n[4/4] Starting Vite dashboard at http://localhost:5173..." -ForegroundColor Yellow
$procVite = Start-Process -FilePath "npm" `
    -ArgumentList "run", "dev" `
    -WorkingDirectory $Dashboard `
    -WindowStyle Normal `
    -PassThru
Write-Host "      Vite PID: $($procVite.Id)" -ForegroundColor Green

Write-Host "`n=== All services started! ===" -ForegroundColor Cyan
Write-Host "Dashboard: http://localhost:5173" -ForegroundColor White
Write-Host "API:       http://localhost:8001" -ForegroundColor White
Write-Host "`nPress Ctrl+C here or close the individual windows to stop." -ForegroundColor Gray

# Wait and offer a clean shutdown
try {
    Wait-Process -Id $procVite.Id -ErrorAction SilentlyContinue
} finally {
    Write-Host "`nShutting down..." -ForegroundColor Yellow
    foreach ($proc in @($procA, $procB, $procVite)) {
        if ($proc -and -not $proc.HasExited) {
            Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
            Write-Host "  Stopped PID $($proc.Id)" -ForegroundColor Gray
        }
    }
    Write-Host "Done." -ForegroundColor Green
}
