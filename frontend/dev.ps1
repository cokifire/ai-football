# AI Football - Start both frontend and backend dev servers
$root = Split-Path -Parent $PSScriptRoot

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  AI Football Dev Server" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# Kill any existing processes on these ports
$ports = @(3000, 8000)
foreach ($port in $ports) {
    $conn = netstat -ano | Select-String ":$port" | Select-String "LISTENING"
    if ($conn) {
        $pid = ($conn -split '\s+')[-1]
        Stop-Process -Id $pid -Force -ErrorAction SilentlyContinue
        Write-Host "  Killed process on port $port (PID: $pid)" -ForegroundColor DarkGray
    }
}

# Start Backend in a new window
Write-Host "  Starting backend..." -ForegroundColor Blue
Start-Process -FilePath "$root\venv\Scripts\python.exe" `
    -ArgumentList "-m uvicorn main:app --host 0.0.0.0 --port 8000 --reload" `
    -WorkingDirectory "$root\backend" `
    -WindowStyle Minimized

# Start Frontend (use cmd.exe to ensure npx is found via PATH)
Write-Host "  Starting frontend..." -ForegroundColor Green
$npxPath = (Get-Command npx.cmd -ErrorAction SilentlyContinue).Source
if (-not $npxPath) { $npxPath = (Get-Command npx -ErrorAction SilentlyContinue).Source }
Start-Process -FilePath $npxPath `
    -ArgumentList "vite --host 0.0.0.0 --port 3000" `
    -WorkingDirectory "$root\frontend" `
    -WindowStyle Minimized

Start-Sleep -Seconds 5

Write-Host ""
Write-Host "  Backend:  http://localhost:8000" -ForegroundColor Blue
Write-Host "  API Docs: http://localhost:8000/docs" -ForegroundColor Blue
Write-Host "  Frontend: http://localhost:3000" -ForegroundColor Green
Write-Host ""
Write-Host "  All servers started! Close their windows to stop." -ForegroundColor Yellow

# Open browser
Start-Process "http://localhost:3000"
