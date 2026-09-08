# Run the existing Python desktop app from any working directory (PowerShell 5.1+).
#
# Usage (from anywhere; the script cd's to the repo root):
#   powershell -ExecutionPolicy Bypass -File scripts\windows\run_spica.ps1
#   .\scripts\windows\run_spica.ps1 -CondaEnv spica
#   .\scripts\windows\run_spica.ps1 -PythonExe "C:\path with spaces\python.exe"
#
# The conda python is PARAMETERIZED (E1): pass -PythonExe to bypass conda
# entirely, or -CondaEnv to pick a different env for `conda run`. No ibus, no
# ALSA -- webui_qt.py already guards its Linux-only preflights by platform.
param(
    [string]$PythonExe = "",
    [string]$CondaEnv = "spica"
)

$ErrorActionPreference = "Stop"
$ExitCode = 1
$LocationPushed = $false

try {
    # Resolve a relative interpreter path before moving to the project root.
    if ($PythonExe) {
        $PythonExe = (Get-Command -Name $PythonExe -CommandType Application -ErrorAction Stop).Source
    } elseif (-not (Get-Command conda -ErrorAction SilentlyContinue)) {
        throw "Conda was not found. Use Anaconda Prompt, or pass -PythonExe with your Python 3.11 interpreter path."
    }
    $RepoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")).Path
    Push-Location -LiteralPath $RepoRoot
    $LocationPushed = $true
    if ($PythonExe) {
        & $PythonExe -X utf8 webui_qt.py @args
    } else {
        conda run -n $CondaEnv --no-capture-output python -X utf8 webui_qt.py @args
    }
    $ExitCode = $LASTEXITCODE
} catch {
    [Console]::Error.WriteLine("Spica Chatbot could not start: " + $_.Exception.Message)
} finally {
    if ($LocationPushed) { Pop-Location }
}
exit $ExitCode
