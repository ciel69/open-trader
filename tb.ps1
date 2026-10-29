# Windows (PowerShell) entry point: .\tb.ps1 <group> <command> [args]   (list: .\tb.ps1 help)
# Thin wrapper over src\tb\launcher.py: the Bybit key file and proxy removal are done there, same as ./tb on macOS.
# If script execution is blocked: powershell -ExecutionPolicy Bypass -File .\tb.ps1 ...   (or use tb.cmd)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root
$env:PYTHONPATH = if ($env:PYTHONPATH) { "$Root\src;$($env:PYTHONPATH)" } else { "$Root\src" }
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
if ($env:TB_PYTHON) { $py = @($env:TB_PYTHON) }
elseif (Get-Command py -ErrorAction SilentlyContinue) { $py = @("py", "-3") }
else { $py = @("python") }
$rest = @()
if ($py.Count -gt 1) { $rest = $py[1..($py.Count - 1)] }
& $py[0] @rest -m tb.launcher @args
exit $LASTEXITCODE
