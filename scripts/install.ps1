<#
.SYNOPSIS
  Alphred installer for Windows (PowerShell).

.DESCRIPTION
  - Verifies Python 3.11+.
  - Installs Alphred (editable) into the user site.
  - Adds the user Scripts dir to PATH so the `alphred` command works (fixes the common
    "alphred is not recognized" issue).

  Hermes Agent must already be installed (Alphred is a wrapper over it).

.PARAMETER NoPath
  Do not modify PATH.

.PARAMETER Python
  Python executable to use (e.g. "py", "python3.11"). Auto-detected if omitted.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\install.ps1
#>
[CmdletBinding()]
param(
  [switch]$NoPath,
  [string]$Python
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot   # scripts\ -> repo root

function Info($m)  { Write-Host $m -ForegroundColor Cyan }
function Ok($m)    { Write-Host "  $m" -ForegroundColor Green }
function Warn($m)  { Write-Host "  $m" -ForegroundColor Yellow }

Info "Alphred installer (Windows)"
Write-Host "  repo: $repo"

# ---- 1) Resolve a Python 3.11+ interpreter ----
# NOTE: the invoker is NOT named "Py" — PowerShell command resolution is case-insensitive,
# so a function named "Py" would shadow the external "py" launcher and recurse forever.
$pyExe = $null; $pyArgs = @()
$candidates = @()
if ($Python) { $candidates += ,@{ Exe = $Python; Rest = @() } }
$candidates += ,@{ Exe = "python"; Rest = @() }
$candidates += ,@{ Exe = "py";     Rest = @("-3") }
foreach ($cand in $candidates) {
  if (-not (Get-Command $cand.Exe -ErrorAction SilentlyContinue)) { continue }
  $rest = @($cand.Rest)
  $ver = $null
  try { $ver = & $cand.Exe @rest -c "import sys;print('{}.{}'.format(*sys.version_info[:2]))" 2>$null }
  catch { continue }
  if ($ver -is [array]) { $ver = $ver[-1] }
  $ver = "$ver".Trim()
  if ($ver -match '^\d+\.\d+$' -and ([version]$ver -ge [version]"3.11")) {
    $pyExe = $cand.Exe; $pyArgs = $rest; break
  }
}
if (-not $pyExe) { throw "Python 3.11+ not found. Install it from https://python.org and retry." }
function Invoke-Py { & $pyExe @pyArgs @args }
Ok "Python: $pyExe $($pyArgs -join ' ') ($(Invoke-Py -c 'import sys;print(sys.version.split()[0])'))"

# ---- 2) Install Alphred (editable) ----
Info "Installing Alphred (pip install -e .) ..."
Push-Location $repo
try { Invoke-Py -m pip install -e . } finally { Pop-Location }
Ok "installed"

# ---- 3) Ensure the console-script dir is on PATH ----
$findDir = @'
import sysconfig, os
names = ['alphred.exe', 'alphred']
dirs = []
for args in ((), ('nt_user',)):
    try: dirs.append(sysconfig.get_path('scripts', *args))
    except Exception: pass
for d in dirs:
    if any(os.path.exists(os.path.join(d, n)) for n in names):
        print(d); break
else:
    print(dirs[-1] if dirs else '')
'@
$scriptsDir = (Invoke-Py -c $findDir).Trim()
if ($scriptsDir -and (Test-Path $scriptsDir)) {
  $onPath = ($env:Path -split ';') -contains $scriptsDir
  if ($onPath) {
    Ok "PATH already contains $scriptsDir"
  } elseif ($NoPath) {
    Warn "PATH not modified (--NoPath). Use 'python -m alphred.cli' or add: $scriptsDir"
  } else {
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if (($userPath -split ';') -notcontains $scriptsDir) {
      [Environment]::SetEnvironmentVariable("Path", "$userPath;$scriptsDir", "User")
    }
    $env:Path += ";$scriptsDir"   # current session
    Ok "added to PATH: $scriptsDir (open a NEW terminal for it to persist)"
  }
} else {
  Warn "could not locate the alphred script dir; use 'python -m alphred.cli'"
}

# ---- 4) Hermes presence check (informational) ----
$hermesBin = ""
try { $hermesBin = (Invoke-Py -c "from alphred.config import resolve_hermes_bin; print(resolve_hermes_bin() or '')").Trim() } catch {}
if (-not $hermesBin) {
  Warn "Hermes not found — install Hermes Agent, then run 'alphred setup'. (Alphred wraps Hermes.)"
}

Write-Host ""
Info "Done. Next steps:"
Write-Host "  alphred setup     # configure LLM provider (Hermes onboarding), if not done yet"
Write-Host "  alphred           # start the queue-aware TUI"
Write-Host "  alphred serve     # or run the gateway + web dashboard (http://localhost:8643/)"
Write-Host "  (if 'alphred' is still not found, open a new terminal, or use 'python -m alphred.cli')"
