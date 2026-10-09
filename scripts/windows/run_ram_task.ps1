param([Parameter(Mandatory)][ValidateSet('off','on')][string]$Mode)
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
$state=$PSScriptRoot
$manifest=Get-Content -LiteralPath (Join-Path $state 'deployment.json') -Raw | ConvertFrom-Json
$result=@{ mode=$Mode; started_at=[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()/1000.0; status='failed'; instance_guid=''; reason='' }
$destination=Join-Path $state ('results\'+$Mode+'.json')
$mutex=$null
$acquired=$false
$exitCode=1
function Save-Result {
    $result.finished_at=[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()/1000.0
    $temporary=$destination+'.'+[Guid]::NewGuid().ToString('N')+'.tmp'
    [IO.File]::WriteAllText($temporary,($result | ConvertTo-Json -Compress),[Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temporary -Destination $destination -Force
}
function Clean-Output([string]$value) {
    $value=[regex]::Replace($value,'\x1b\][^\x07]*(?:\x07|\x1b\\)','')
    return [regex]::Replace($value,'\x1b\[[0-?]*[ -/]*[@-~]','').Replace("`r`n","`n").Replace("`r","`n")
}
function Read-Controllers {
    $listed=[WindowsRgbNative]::Run($executable,[string[]]@('--noautoconnect','--config',$profile,'--list-detailed'),4000)
    $output=Clean-Output $listed.Output
    if($listed.ExitCode -ne 0){throw 'OpenRGB RAM identity query failed'}
    $blocks=@([regex]::Split($output,'(?m)(?=^\d+: )') | Where-Object {$_ -match '^\d+: '})
    if($blocks.Count -ne 2){throw 'Exactly two ENE DRAM controllers were not confirmed'}
    $controllers=@(foreach($block in $blocks){
        $header=[regex]::Match($block,'^(\d+): ([^\r\n]+)')
        $version=[regex]::Match($block,'(?m)^\s*Version:\s*([^\r\n]+)').Groups[1].Value.Trim()
        $location=[regex]::Match($block,'(?m)^\s*Location:\s*([^\r\n]+)').Groups[1].Value.Trim()
        $active=[regex]::Match($block,'(?m)^\s*Modes:[^\r\n]*\[([^\]]+)\]').Groups[1].Value
        if($header.Groups[2].Value.Trim() -cne 'ENE DRAM' -or $version -cne 'AUDA0-E6K5-0101'){
            throw 'RAM controller name or firmware identity differs'
        }
        @{index=[int]$header.Groups[1].Value;location=$location;mode=$active}
    })
    if($controllers[0].index -ne 0 -or $controllers[1].index -ne 1 -or
        (($controllers.location | Sort-Object) -join '|') -cne 'I2C: PawnIO SMBus i801 0, address 0x71|I2C: PawnIO SMBus i801 0, address 0x73'){
        throw 'RAM controller indexes or bound SMBus addresses differ'
    }
    return $controllers
}
try {
    if ([Security.Principal.WindowsIdentity]::GetCurrent().User.Value -ne 'S-1-5-18') { throw 'RAM task requires its SYSTEM principal' }
    $scheduler=New-Object -ComObject Schedule.Service
    $scheduler.Connect()
    $task=$scheduler.GetFolder('\').GetTask($manifest.tasks.$Mode)
    $instances=@(foreach($instance in $task.GetInstances(0)){ $instance.InstanceGuid })
    if($instances.Count -ne 1){throw 'RAM task instance is not unique'}
    $result.instance_guid=[string]$instances[0]

    $security=[Security.AccessControl.MutexSecurity]::new()
    foreach($sid in @('S-1-5-18','S-1-5-32-544')) {
        $security.AddAccessRule([Security.AccessControl.MutexAccessRule]::new(
            [Security.Principal.SecurityIdentifier]::new($sid),[Security.AccessControl.MutexRights]::FullControl,
            [Security.AccessControl.AccessControlType]::Allow))
    }
    $created=$false
    $mutex=[Threading.Mutex]::new($false,('Global\Spica-RAM-Worker-'+$manifest.identity),[ref]$created,$security)
    if($mutex.GetAccessControl().GetOwner([Security.Principal.SecurityIdentifier]).Value -notin @('S-1-5-18','S-1-5-32-544')) {
        throw 'RAM worker mutex has a foreign owner'
    }
    try {$acquired=$mutex.WaitOne(0)} catch [Threading.AbandonedMutexException] {$acquired=$true}
    if(-not $acquired){throw 'Another RAM action is still running'}

    # This administrator-owned manifest pins every executable dependency and
    # the entire JSON profile. PowerShell 5 must not parse detector maps whose
    # legitimate names can differ only in case.
    foreach($entry in $manifest.files.PSObject.Properties) {
        $path=Join-Path $state $entry.Name
        if((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -ne $entry.Value){
            throw ('Protected RAM deployment changed: '+$entry.Name)
        }
    }
    Add-Type -Path (Join-Path $state 'WindowsRgbNative.cs')
    $lighting=Get-CimInstance Win32_Service -Filter "Name='LightingService'"
    if($lighting){
        $binding=$manifest.lighting_service
        if(-not $binding -or $lighting.PathName -cne $binding.path_name -or
            (Get-FileHash -LiteralPath $binding.executable -Algorithm SHA256).Hash -ine $binding.executable_sha256){
            throw 'Competing LightingService identity changed; it was not stopped'
        }
        $service=Get-Service -Name LightingService
        if($service.Status -ne 'Stopped'){
            if($service.Status -ne 'Running'){throw 'LightingService is already transitioning'}
            # Native SCM stop acts on this one service only. No force, recursive
            # dependency stop, startup-type change, or Armoury service action.
            [WindowsRgbNative]::StopLightingService()
            $service.WaitForStatus([ServiceProcess.ServiceControllerStatus]::Stopped,[TimeSpan]::FromSeconds(5))
            $result.lighting_service='stopped'
        }else{$result.lighting_service='already_stopped'}
    }else{
        if($manifest.lighting_service){throw 'The bound LightingService is missing'}
        $result.lighting_service='not_installed'
    }
    $executable=Join-Path $state 'bin\OpenRGB.exe'
    $profile=Join-Path $state 'profile'
    $effect=if($Mode -eq 'off'){'Off'}else{'Rainbow'}
    foreach($index in @(0,1)){
        [void](Read-Controllers)
        # Separate processes prevent concurrent DeviceCallThreads from racing
        # the two DIMMs' shared SMBus command/read sequences.
        $changed=[WindowsRgbNative]::Run($executable,[string[]]@('--noautoconnect','--config',$profile,'--device',[string]$index,'--mode',$effect),4000)
        $output=Clean-Output $changed.Output
        if($changed.ExitCode -ne 0 -or $output -match 'Error:|Wrong number of colors specified') {
            throw 'OpenRGB did not accept the RAM effect command'
        }
    }
    $observed=@((Read-Controllers) | ForEach-Object {$_.mode})
    $result.observed_modes=$observed
    if($observed.Count -ne 2 -or @($observed | Where-Object {$_ -cne $effect}).Count){throw 'Both RAM modes were not confirmed by readback'}
    $result.status='requested'
    $exitCode=0
} catch {
    $result.reason=$_.Exception.Message
} finally {
    try {Save-Result} finally {
        if($acquired){$mutex.ReleaseMutex()}
        if($mutex){$mutex.Dispose()}
    }
}
exit $exitCode
