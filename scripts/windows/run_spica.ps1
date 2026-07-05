# Spica Windows entry (W3, WINDOWS_COMPAT_PLAN §5-W3 内容 3 / A6).
#
# Usage (from anywhere; the script cd's to the repo root):
#   powershell -ExecutionPolicy Bypass -File scripts\windows\run_spica.ps1
#   .\scripts\windows\run_spica.ps1 -CondaEnv spica-win
#   .\scripts\windows\run_spica.ps1 -PythonExe C:\path\to\envs\spica-win\python.exe
#
# The conda python is PARAMETERIZED (E1): pass -PythonExe to bypass conda
# entirely, or -CondaEnv to pick a different env for `conda run`. No ibus, no
# ALSA -- webui_qt.py already guards its Linux-only preflights by platform.
param(
    [string]$PythonExe = "",
    [string]$CondaEnv = "spica-win"
)

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $RepoRoot

if ($PythonExe) {
    & $PythonExe webui_qt.py @args
} else {
    conda run -n $CondaEnv --no-capture-output python webui_qt.py @args
}
exit $LASTEXITCODE
