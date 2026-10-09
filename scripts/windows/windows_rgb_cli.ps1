param(
    [Parameter(Mandatory)][string]$Executable,
    [Parameter(Mandatory)][string]$ArgumentsBase64,
    [ValidateRange(1,30000)][int]$TimeoutMilliseconds = 5000
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
Add-Type -Path (Join-Path $PSScriptRoot 'WindowsRgbNative.cs')
$arguments = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($ArgumentsBase64)) | ConvertFrom-Json
if ($arguments -isnot [array] -or @($arguments | Where-Object { $_ -isnot [string] }).Count) {
    throw 'Invalid OpenRGB argument array'
}
$result = [WindowsRgbNative]::Run((Resolve-Path -LiteralPath $Executable).Path, [string[]]$arguments, $TimeoutMilliseconds)
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
[Console]::Write($result.Output)
exit $result.ExitCode
