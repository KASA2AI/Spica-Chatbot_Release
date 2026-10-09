#Requires -RunAsAdministrator
param(
    [Parameter(Mandatory)][string]$OpenRgbArchive,
    [Parameter(Mandatory)][string]$ProfileDirectory,
    [Parameter(Mandatory)][ValidatePattern('^S-1-5-21-[0-9-]+$')][string]$OwnerSid,
    [Parameter(Mandatory)][ValidatePattern('^[a-f0-9]{16}$')][string]$Identity,
    [Parameter(Mandatory)][string]$InstallationRoot,
    [Parameter(Mandatory)][ValidatePattern('^[a-fA-F0-9]{64}$')][string]$ExpectedProfileSha256,
    [Parameter(Mandatory)][ValidatePattern('^[a-fA-F0-9]{64}$')][string]$ExpectedHelperSha256,
    [Parameter(Mandatory)][ValidatePattern('^[a-fA-F0-9]{64}$')][string]$ExpectedNativeSha256
)
$ErrorActionPreference='Stop'
$archive=(Resolve-Path -LiteralPath $OpenRgbArchive).Path
$profile=(Resolve-Path -LiteralPath $ProfileDirectory).Path
$root=(Resolve-Path -LiteralPath $InstallationRoot).Path
$expectedBinary='86a88b99f60a086e13e6f6ecfb8260a94dbcd598ddeabffda7070d78c97d7e95'
function Assert-Hash([string]$Path,[string]$Expected) {
    if((Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash -ine $Expected){throw ('Unexpected file hash: '+$Path)}
}
Assert-Hash (Join-Path $profile 'OpenRGB.json') $ExpectedProfileSha256
Assert-Hash (Join-Path $PSScriptRoot 'run_ram_task.ps1') $ExpectedHelperSha256
Assert-Hash (Join-Path $PSScriptRoot 'WindowsRgbNative.cs') $ExpectedNativeSha256
if([Security.Principal.WindowsIdentity]::GetCurrent().User.Value -ne $OwnerSid){throw 'Install from the intended user elevated token'}
$actualIdentity=[BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash(
    [Text.Encoding]::UTF8.GetBytes($root.ToLowerInvariant()+"`n"+$OwnerSid))).Replace('-','').ToLowerInvariant().Substring(0,16)
if($actualIdentity -ne $Identity){throw 'Installation identity differs'}

$base=Join-Path ([Environment]::GetFolderPath('CommonApplicationData')) 'SpicaWindowsRgb'
$state=Join-Path $base $Identity
function Protect-Path([string]$Path,[bool]$Directory) {
    $item=Get-Item -LiteralPath $Path -Force
    if($item.Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'Protected deployment cannot contain reparse points'}
    $acl=if($Directory){[Security.AccessControl.DirectorySecurity]::new()}else{[Security.AccessControl.FileSecurity]::new()}
    $acl.SetOwner([Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))
    $acl.SetAccessRuleProtection($true,$false)
    $inherit=if($Directory){[Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit'}else{[Security.AccessControl.InheritanceFlags]::None}
    foreach($sid in @('S-1-5-18','S-1-5-32-544',$OwnerSid)){
        $rights=if($sid -eq $OwnerSid){[Security.AccessControl.FileSystemRights]::ReadAndExecute}else{[Security.AccessControl.FileSystemRights]::FullControl}
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new($sid),$rights,$inherit,
            [Security.AccessControl.PropagationFlags]::None,[Security.AccessControl.AccessControlType]::Allow))
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
}
if(Test-Path -LiteralPath $base){
    $item=Get-Item -LiteralPath $base -Force
    $owner=(Get-Acl -LiteralPath $base).GetOwner([Security.Principal.SecurityIdentifier]).Value
    if(($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -or $owner -notin @('S-1-5-18','S-1-5-32-544')){throw 'Existing RGB base has a foreign owner'}
}else{New-Item -ItemType Directory -Path $base | Out-Null}
Protect-Path $base $true
if(Test-Path -LiteralPath $state){throw 'Protected RAM installation already exists; drain and explicitly review replacement instead of overwriting it'}
New-Item -ItemType Directory -Path $state | Out-Null
Protect-Path $state $true
foreach($name in @('bin','profile','results')){New-Item -ItemType Directory -Path (Join-Path $state $name) | Out-Null; Protect-Path (Join-Path $state $name) $true}
# Verify and extract the official archive through the same handle. This avoids
# trusting user-writable DLLs beside the staging copy of OpenRGB.exe.
Add-Type -AssemblyName System.IO.Compression
$stream=[IO.File]::Open($archive,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
$zip=$null
try {
    $zipHash=[BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash($stream)).Replace('-','').ToLowerInvariant()
    if($zipHash -ne '182a52a3c97c4c4ae52c80286b4260c9666c51c3447dbc416ee8945f66192e90'){throw 'Official OpenRGB archive fingerprint differs'}
    $stream.Position=0
    $zip=[IO.Compression.ZipArchive]::new($stream,[IO.Compression.ZipArchiveMode]::Read,$true)
    $bin=Join-Path $state 'bin'
    foreach($entry in $zip.Entries){
        if(-not $entry.FullName.StartsWith('OpenRGB Windows 64-bit/')){throw 'Unexpected OpenRGB archive root'}
        $relative=$entry.FullName.Substring('OpenRGB Windows 64-bit/'.Length)
        if(-not $relative -or $relative.EndsWith('/')){continue}
        $target=[IO.Path]::GetFullPath((Join-Path $bin $relative))
        if(-not $target.StartsWith($bin+'\',[StringComparison]::OrdinalIgnoreCase)){throw 'Archive path escapes protected bin'}
        [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($target)) | Out-Null
        $archiveInput=$entry.Open()
        $archiveOutput=[IO.File]::Open($target,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
        try{$archiveInput.CopyTo($archiveOutput)}finally{$archiveOutput.Dispose();$archiveInput.Dispose()}
    }
}finally{if($zip){$zip.Dispose()};$stream.Dispose()}
Copy-Item -LiteralPath (Join-Path $profile 'OpenRGB.json') -Destination (Join-Path $state 'profile\OpenRGB.json')
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'run_ram_task.ps1'),(Join-Path $PSScriptRoot 'WindowsRgbNative.cs') -Destination $state
Assert-Hash (Join-Path $state 'bin\OpenRGB.exe') $expectedBinary
Assert-Hash (Join-Path $state 'profile\OpenRGB.json') $ExpectedProfileSha256
Assert-Hash (Join-Path $state 'run_ram_task.ps1') $ExpectedHelperSha256
Assert-Hash (Join-Path $state 'WindowsRgbNative.cs') $ExpectedNativeSha256
Get-ChildItem -LiteralPath $state -Force -Recurse | ForEach-Object {Protect-Path $_.FullName $_.PSIsContainer}
$files=@{}
Get-ChildItem -LiteralPath $state -Recurse -File | ForEach-Object {
    $relative=$_.FullName.Substring($state.Length+1).Replace('\','/')
    $files[$relative]=(Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
}
$tasks=@{off='Spica-RAM-Off-'+$Identity;on='Spica-RAM-On-'+$Identity}
$lighting=Get-CimInstance Win32_Service -Filter "Name='LightingService'"
$lightingBinding=$null
if($lighting){
    $lightingExe=Join-Path ([Environment]::GetFolderPath('ProgramFilesX86')) 'LightingService\LightingService.exe'
    $allowed=@($lightingExe,('"'+$lightingExe+'"'))
    if($lighting.PathName -notin $allowed){throw 'LightingService is not the expected ASUS lighting executable'}
    $lightingBinding=@{name='LightingService';path_name=$lighting.PathName;executable=$lightingExe;
        executable_sha256=(Get-FileHash -LiteralPath $lightingExe -Algorithm SHA256).Hash.ToLowerInvariant();
        original_start_mode=$lighting.StartMode}
}
$manifest=@{identity=$Identity;owner_sid=$OwnerSid;installation_root=$root;files=$files;tasks=$tasks;lighting_service=$lightingBinding}
[IO.File]::WriteAllText((Join-Path $state 'deployment.json'),($manifest | ConvertTo-Json -Depth 5),[Text.UTF8Encoding]::new($false))
Protect-Path (Join-Path $state 'deployment.json') $false
$scheduler=New-Object -ComObject Schedule.Service
$scheduler.Connect()
$folder=$scheduler.GetFolder('\')
$program=Join-Path ([Environment]::GetFolderPath('System')) 'WindowsPowerShell\v1.0\powershell.exe'
$taskSecurity='O:BAG:BAD:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;GRGX;;;'+$OwnerSid+')'
foreach($mode in @('off','on')){
    $definition=$scheduler.NewTask(0)
    $definition.RegistrationInfo.Description="Spica protected RAM v1`n$($root.ToLowerInvariant())`n$OwnerSid`n$mode"
    $definition.Principal.UserId='S-1-5-18'
    $definition.Principal.LogonType=5
    $definition.Principal.RunLevel=1
    $definition.Settings.Enabled=$true
    $definition.Settings.ExecutionTimeLimit='PT15S'
    $definition.Settings.MultipleInstances=2
    $definition.Settings.DisallowStartIfOnBatteries=$false
    $definition.Settings.StopIfGoingOnBatteries=$false
    $definition.Settings.AllowHardTerminate=$true
    $action=$definition.Actions.Create(0)
    $action.Path=$program
    $action.Arguments='-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "'+(Join-Path $state 'run_ram_task.ps1')+'" -Mode '+$mode
    $action.WorkingDirectory=$state
    [void]$folder.RegisterTaskDefinition($tasks[$mode],$definition,2,'SYSTEM',$null,5,$taskSecurity)
}
@{state=$state;tasks=$tasks} | ConvertTo-Json
