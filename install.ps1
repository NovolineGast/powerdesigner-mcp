# =====================================================================
# powerdesigner-mcp installer for Windows
#
#   .\install.ps1                      - install + register + verify
#   .\install.ps1 -Client workbuddy    - register with one client only
#   .\install.ps1 -NoRegister          - install only, touch no client config
#   .\install.ps1 -SkipProbe           - skip the PowerDesigner COM probe
#   .\install.ps1 -SkipSmoke           - skip the MCP stdio smoke test
#   .\install.ps1 -Index ""            - use the default PyPI instead of the mirror
#
# Performs:
#   1. Environment checks (Python 3.10+, PowerDesigner ProgID)
#   2. Package install into an isolated tool env (uv) or a .venv fallback
#   3. Self-registration with the detected MCP clients - the installer writes
#      the client config for you, so no absolute paths are ever hand-edited
#   4. PowerDesigner COM probe + MCP stdio smoke test on the *configured*
#      launch command
# =====================================================================
param(
    [string]$Client = "",
    [switch]$NoRegister,
    [switch]$SkipProbe,
    [switch]$SkipSmoke,
    [string]$Index = "https://pypi.tuna.tsinghua.edu.cn/simple"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

function Write-Step($msg)  { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }
function Write-Ok($msg)    { Write-Host "  [OK] $msg" -ForegroundColor Green }
function Write-Warn2($msg) { Write-Host "  [!!] $msg" -ForegroundColor Yellow }

Write-Host "powerdesigner-mcp installer" -ForegroundColor Magenta
Write-Host "Project: $ProjectRoot"

# ---------------------------------------------------------------------
# 1. Python
# ---------------------------------------------------------------------
Write-Step "Checking Python"
$py = $null
foreach ($candidate in @("py", "python", "python3")) {
    try {
        $ver = & $candidate -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and $ver) {
            $major, $minor = $ver.Split(".")
            if ([int]$major -ge 3 -and [int]$minor -ge 10) { $py = $candidate; break }
        }
    } catch { }
}
if (-not $py) {
    Write-Warn2 "Python 3.10+ not found. Install from https://www.python.org/downloads/ (check 'Add to PATH')."
    exit 1
}
$ver = & $py -c "import sys; print('%d.%d.%d' % sys.version_info[:3])"
Write-Ok "Python $ver ($py)"

# ---------------------------------------------------------------------
# 2. PowerDesigner (informational; the COM probe is the real test)
# ---------------------------------------------------------------------
Write-Step "Checking PowerDesigner installation"
$pdProgId = $false
try {
    $k = [Microsoft.Win32.Registry]::ClassesRoot.OpenSubKey("PowerDesigner.Application")
    $pdProgId = ($null -ne $k)
} catch { }
if ($pdProgId) {
    Write-Ok "COM ProgID 'PowerDesigner.Application' registered"
} else {
    Write-Warn2 "ProgID 'PowerDesigner.Application' NOT registered."
    Write-Host "       If PowerDesigner is installed, run once as admin:" -ForegroundColor Gray
    Write-Host "         <PD home>\pdlegacyshell16.exe /RegServer" -ForegroundColor Gray
    Write-Host "       The server can also launch PD itself (attach_mode=auto)." -ForegroundColor Gray
}

# ---------------------------------------------------------------------
# 3. Install the package itself (not just its dependencies)
#
#    uv tool install puts the console script on PATH in an isolated env, which
#    is what makes the client config path-free (the "npx experience").  Without
#    uv we fall back to a project venv with an editable install.
# ---------------------------------------------------------------------
Write-Step "Installing the package"
$uv = Get-Command uv -ErrorAction SilentlyContinue
$launcher = $null
if ($uv) {
    $arr = @("tool", "install", "--editable", ".")
    if ($Index) { $arr += @("--index-url", $Index) }
    Write-Host "  uv $($arr -join ' ')" -ForegroundColor Gray
    & uv @arr
    if ($LASTEXITCODE -eq 0) {
        $launcher = (Get-Command powerdesigner-mcp -ErrorAction SilentlyContinue).Source
        if ($launcher) { Write-Ok "installed as a uv tool: $launcher" }
        else { Write-Warn2 "uv reported success but 'powerdesigner-mcp' is not on PATH" }
    } else {
        Write-Warn2 "uv tool install failed; falling back to a project venv"
    }
} else {
    Write-Warn2 "uv not found - using a project venv instead (configs then carry a python path)"
}

if (-not $launcher) {
    if (-not (Test-Path ".venv")) {
        & $py -m venv .venv
        Write-Ok ".venv created"
    } else {
        Write-Ok ".venv already exists"
    }
    $venvPy = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    $pipArgs = @("-m", "pip", "install", "--quiet", "--disable-pip-version-check", "-e", ".")
    if ($Index) { $pipArgs += @("-i", $Index) }
    & $venvPy @pipArgs
    if ($LASTEXITCODE -ne 0) {
        Write-Warn2 "editable install failed (network?). Retry manually:"
        Write-Host "       .venv\Scripts\python.exe -m pip install -e ." -ForegroundColor Gray
        exit 1
    }
    Write-Ok "installed into .venv (editable)"
    $launcher = $venvPy
}

# ---------------------------------------------------------------------
# 4. Self-registration: the installer writes the client config
# ---------------------------------------------------------------------
if (-not $NoRegister) {
    Write-Step "Registering with MCP clients"
    $cmdArgs = @("install")
    if ($Client) { $cmdArgs += @("--client", $Client) }
    if ($launcher -like "*python.exe") {
        $env:PYTHONPATH = Join-Path $ProjectRoot "src"
        & $launcher -m pd_mcp @cmdArgs
        Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
    } else {
        & $launcher @cmdArgs
    }
    if ($LASTEXITCODE -ne 0) {
        Write-Warn2 "registration failed - add the entry manually (see README)"
    } else {
        Write-Ok "client configs updated (a backup of any replaced file was kept)"
    }
} else {
    Write-Host "Skipping registration (-NoRegister)"
}

# ---------------------------------------------------------------------
# 5. PowerDesigner COM probe
# ---------------------------------------------------------------------
if (-not $SkipProbe) {
    Write-Step "Running PowerDesigner COM probe (may launch PowerDesigner)"
    $env:PYTHONPATH = Join-Path $ProjectRoot "src"
    if ($launcher -like "*python.exe") { & $launcher -m pd_mcp probe }
    else { & $launcher probe }
    if ($LASTEXITCODE -eq 0) {
        Write-Ok "probe passed - see logs\probe_report.json"
    } else {
        Write-Warn2 "probe failed - the server will fall back to the in-memory mock."
        Write-Host "       Check logs\pdmcp.log and logs\probe_report.json" -ForegroundColor Gray
        Write-Host "       Set PDMCP_ATTACH_MODE=launch to let the server start PD itself." -ForegroundColor Gray
    }
    Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
} else {
    Write-Host "Skipping probe (-SkipProbe)"
}

# ---------------------------------------------------------------------
# 6. MCP stdio smoke test on the command that was just registered
# ---------------------------------------------------------------------
if (-not $SkipSmoke) {
    Write-Step "Smoke-testing the configured launch command"
    $json = if ($launcher -like "*python.exe") {
        @($launcher, "-m", "pd_mcp", "serve") | ConvertTo-Json -Compress
    } else {
        @($launcher, "serve") | ConvertTo-Json -Compress
    }
    $prevCmd = $env:PDMCP_SMOKE_CMD
    $prevPy = $env:PYTHONPATH
    $env:PDMCP_SMOKE_CMD = $json
    $env:PYTHONPATH = Join-Path $ProjectRoot "src"
    # the smoke test itself only needs a Python 3.10+; the launch command under
    # test comes from PDMCP_SMOKE_CMD, i.e. exactly what was registered
    $smokePy = if ($launcher -like "*python.exe") { $launcher }
               elseif (Test-Path ".venv\Scripts\python.exe") { Join-Path $ProjectRoot ".venv\Scripts\python.exe" }
               else { $py }
    & $smokePy (Join-Path $ProjectRoot "scripts\smoke_test.py")
    if ($LASTEXITCODE -eq 0) { Write-Ok "smoke test passed" }
    else { Write-Warn2 "smoke test failed - see output above" }
    if ($prevCmd) { $env:PDMCP_SMOKE_CMD = $prevCmd } else { Remove-Item Env:PDMCP_SMOKE_CMD -ErrorAction SilentlyContinue }
    if ($prevPy) { $env:PYTHONPATH = $prevPy } else { Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue }
}

Write-Step "Done"
Write-Host @"
Common commands:
  powerdesigner-mcp serve                 # run the stdio server
  powerdesigner-mcp probe                 # verify PowerDesigner COM
  powerdesigner-mcp install --dry-run     # preview client config changes
  .venv\Scripts\python.exe -m pytest tests -m "not live"
"@ -ForegroundColor Gray
