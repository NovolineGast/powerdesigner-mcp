# =====================================================================
# powerdesigner-mcp installer for Windows
#
#   .\install.ps1              - full install + verification
#   .\install.ps1 -SkipProbe   - skip the PowerDesigner COM probe
#   .\install.ps1 -SkipSmoke   - skip the MCP stdio smoke test
#
# Performs:
#   1. Environment checks (Python 3.10+, pip, optional .NET / PowerDesigner)
#   2. Virtual environment (.venv) + dependencies (mcp, pywin32, pytest)
#   3. PowerDesigner COM capability probe (writes logs/probe_report.json)
#   4. MCP stdio smoke test (initialize / tools-list / tool-call)
#   5. Client config examples with absolute paths (mcp-configs/)
# =====================================================================
param(
    [switch]$SkipProbe,
    [switch]$SkipSmoke
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
# 2. PowerDesigner (informational; COM probe is the real test)
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
    Write-Host "       The MCP server can also launch PD itself (attach_mode=auto)." -ForegroundColor Gray
}

# ---------------------------------------------------------------------
# 3. Virtual environment + dependencies
# ---------------------------------------------------------------------
Write-Step "Creating virtual environment (.venv)"
if (-not (Test-Path ".venv")) {
    & $py -m venv .venv
    Write-Ok ".venv created"
} else {
    Write-Ok ".venv already exists"
}
$vexe = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

Write-Step "Installing dependencies (mcp, pywin32)"
& $vexe -m pip install --quiet --disable-pip-version-check `
    -i https://pypi.tuna.tsinghua.edu.cn/simple `
    "mcp>=1.2.0,<2.0" "pywin32>=306" "pytest>=8.0"
if ($LASTEXITCODE -ne 0) {
    Write-Warn2 "pip install failed (network?). Retry manually:"
    Write-Host "       .venv\Scripts\python.exe -m pip install -r requirements.txt" -ForegroundColor Gray
    exit 1
}
Write-Ok "dependencies installed"

# ---------------------------------------------------------------------
# 4. COM capability probe (real PowerDesigner round-trip)
# ---------------------------------------------------------------------
if (-not $SkipProbe) {
    Write-Step "Running PowerDesigner COM probe (this may launch PowerDesigner)"
    $env:PYTHONPATH = Join-Path $ProjectRoot "src"
    & $vexe -m pd_mcp probe
    if ($LASTEXITCODE -eq 0) {
        Write-Ok "probe passed - see logs\probe_report.json"
    } else {
        Write-Warn2 "probe failed - the server will fall back to the in-memory mock."
        Write-Host "       Check logs\pdmcp.log and logs\probe_report.json" -ForegroundColor Gray
        Write-Host "       Set PDMCP_ATTACH_MODE=launch to let the server start PD itself." -ForegroundColor Gray
    }
} else {
    Write-Host "Skipping probe (-SkipProbe)"
}

# ---------------------------------------------------------------------
# 5. MCP stdio smoke test
# ---------------------------------------------------------------------
if (-not $SkipSmoke) {
    Write-Step "Running MCP stdio smoke test (mock backend)"
    $env:PYTHONPATH = Join-Path $ProjectRoot "src"
    $env:PDMCP_ADAPTER = "mock"
    & $vexe (Join-Path $ProjectRoot "scripts\smoke_test.py")
    if ($LASTEXITCODE -eq 0) { Write-Ok "smoke test passed" }
    else { Write-Warn2 "smoke test failed - see output above" }
    Remove-Item Env:PDMCP_ADAPTER -ErrorAction SilentlyContinue
}

# ---------------------------------------------------------------------
# 6. Client configuration examples
# ---------------------------------------------------------------------
Write-Step "Generating client config examples (mcp-configs\)"
New-Item -ItemType Directory -Force -Path "mcp-configs" | Out-Null
$pyExe = $vexe
$workDir = $ProjectRoot

$claudeDesktop = @{
    mcpServers = @{
        powerdesigner = @{
            command = $pyExe
            args    = @("-m", "pd_mcp", "serve")
            env     = @{ PYTHONPATH = (Join-Path $ProjectRoot "src") }
        }
    }
}
$claudeDesktop | ConvertTo-Json -Depth 6 |
    Out-File "mcp-configs\claude_desktop.json" -Encoding utf8

$cursor = @{
    mcpServers = @{
        powerdesigner = @{
            command = $pyExe
            args    = @("-m", "pd_mcp", "serve")
            env     = @{ PYTHONPATH = (Join-Path $ProjectRoot "src") }
        }
    }
}
$cursor | ConvertTo-Json -Depth 6 | Out-File "mcp-configs\cursor.json" -Encoding utf8

@"
# Claude Code: run one of the following
claude mcp add powerdesigner -- "$pyExe" -m pd_mcp serve
# with env (PowerShell):
#   $env:PYTHONPATH = "$ProjectRoot\src"   (or use the -e option of claude mcp add)
"@ | Out-File "mcp-configs\claude_code.txt" -Encoding utf8

Write-Ok "wrote mcp-configs\claude_desktop.json / cursor.json / claude_code.txt"

# ---------------------------------------------------------------------
Write-Step "Done"
Write-Host @"
Next steps:
 1. Claude Desktop : merge mcp-configs\claude_desktop.json into
                     %APPDATA%\Claude\claude_desktop_config.json
 2. Cursor         : merge mcp-configs\cursor.json into
                     %USERPROFILE%\.cursor\mcp.json
 3. Claude Code    : claude mcp add powerdesigner -- "$pyExe" -m pd_mcp serve
 4. Run tests      : .venv\Scripts\python.exe -m pytest tests -m "not live"
 5. Live probe     : .venv\Scripts\python.exe -m pd_mcp probe
"@ -ForegroundColor Gray
