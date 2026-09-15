@echo off
rem powerdesigner-mcp stdio server wrapper.
rem Portable fallback for MCP clients that cannot pass environment variables:
rem point the client's "command" at this file and leave "args" empty.
setlocal
set "ROOT=%~dp0.."
set "PYTHONPATH=%ROOT%\src"
"%ROOT%\.venv\Scripts\python.exe" -m pd_mcp serve
