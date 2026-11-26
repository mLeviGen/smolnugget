# smolnugget installer for Windows.
#
#   irm https://raw.githubusercontent.com/mLeviGen/smolnugget/main/install.ps1 | iex
#
# Installs into %LOCALAPPDATA%\smolnugget (its own venv) and puts snt.cmd into
# %LOCALAPPDATA%\smolnugget\bin, which is added to your user PATH.
# Re-run to upgrade. To uninstall:  & ([scriptblock]::Create((irm <url>))) -Uninstall
param(
    [switch]$Uninstall,
    [string]$Ref = $(if ($env:SNT_REF) { $env:SNT_REF } else { "main" }),
    [string]$Source = $env:SNT_SOURCE
)
$ErrorActionPreference = "Stop"

$Prefix = Join-Path $env:LOCALAPPDATA "smolnugget"
$Bin = Join-Path $Prefix "bin"
if (-not $Source) { $Source = "https://github.com/mLeviGen/smolnugget/archive/$Ref.tar.gz" }

function Say($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }

# Read/write the user PATH via the registry so %VARIABLES% in it stay unexpanded.
function Get-UserPath {
    (Get-Item HKCU:\Environment).GetValue("Path", "", "DoNotExpandEnvironmentNames")
}
function Set-UserPath([string]$Value) {
    Set-ItemProperty -Path HKCU:\Environment -Name Path -Value $Value -Type ExpandString
    # setting any user variable broadcasts WM_SETTINGCHANGE, so new terminals see the new PATH
    [Environment]::SetEnvironmentVariable("SNT_INSTALLER", "1", "User")
    [Environment]::SetEnvironmentVariable("SNT_INSTALLER", $null, "User")
}

if ($Uninstall) {
    if (Test-Path $Prefix) { Remove-Item -Recurse -Force $Prefix }
    $parts = (Get-UserPath) -split ";" | Where-Object { $_ -and ($_ -ne $Bin) }
    Set-UserPath ($parts -join ";")
    Say "smolnugget removed. Your apps and data in ~\.smolnugget were kept."
    return
}

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "git is required: https://git-scm.com/download/win"
}

$Py = $null
foreach ($cand in @(@("py", "-3"), @("python"), @("python3"))) {
    if (Get-Command $cand[0] -ErrorAction SilentlyContinue) {
        $checkArgs = @($cand | Select-Object -Skip 1) + @("-c", "import sys; sys.exit(sys.version_info < (3, 11))")
        & $cand[0] @checkArgs 2>$null
        if ($LASTEXITCODE -eq 0) { $Py = $cand; break }
    }
}
if (-not $Py) { throw "Python 3.11+ is required: https://www.python.org/downloads/" }

$VenvPy = Join-Path $Prefix "venv\Scripts\python.exe"
if (-not (Test-Path $VenvPy)) {
    Say "creating $Prefix\venv"
    $pyArgs = @($Py | Select-Object -Skip 1) + @("-m", "venv", (Join-Path $Prefix "venv"))
    & $Py[0] @pyArgs
    if ($LASTEXITCODE -ne 0) { throw "could not create the virtualenv" }
}

Say "installing smolnugget ($Ref)"
& $VenvPy -m pip install --quiet --disable-pip-version-check --upgrade pip
$Spec = if (Test-Path $Source -PathType Container) { "$Source[top]" } else { "smolnugget[top] @ $Source" }
& $VenvPy -m pip install --quiet --disable-pip-version-check --no-cache-dir --upgrade $Spec
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
# re-running the installer must always bring the latest code, even with an unchanged version
& $VenvPy -m pip install --quiet --disable-pip-version-check --no-cache-dir --force-reinstall --no-deps $Spec
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

New-Item -ItemType Directory -Force $Bin | Out-Null
$Exe = Join-Path $Prefix "venv\Scripts\snt.exe"
Set-Content -Path (Join-Path $Bin "snt.cmd") -Encoding ascii -Value "@echo off`r`n`"$Exe`" %*"

$userPath = Get-UserPath
if (-not (($userPath -split ";") -contains $Bin)) {
    Set-UserPath "$Bin;$userPath"
    $env:Path = "$Bin;$env:Path"
    Say "added $Bin to your user PATH (open a new terminal to use it everywhere)"
}

Say ("installed " + (& $Exe --version))
Write-Host @"

Next steps:
  snt add https://github.com/you/your-bot     # clone + detect how to run it
  snt run                                     # run everything (keep this window open)
  snt top                                     # watch it live (in another window)
"@
