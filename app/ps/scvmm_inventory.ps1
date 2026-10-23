param([string]$InputJson)
# Runs on the SCVMM server (or any host with the VMM console) via PowerShell remoting.
# Returns every cloud and every VM that belongs to a cloud, as compressed JSON.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$in = $InputJson | ConvertFrom-Json

Import-Module VirtualMachineManager -ErrorAction Stop | Out-Null
$vmm = Get-SCVMMServer -ComputerName $in.VmmServer

$cp = $null
if ($in.CustomPropertyName) {
    $cp = Get-SCCustomProperty -VMMServer $vmm -Name $in.CustomPropertyName -ErrorAction SilentlyContinue
}

$clouds = @(Get-SCCloud -VMMServer $vmm | ForEach-Object {
    [pscustomobject]@{ id = "$($_.ID)"; name = $_.Name; description = $_.Description }
})

$vms = New-Object System.Collections.Generic.List[object]
foreach ($vm in @(Get-SCVirtualMachine -VMMServer $vmm -All)) {
    if (-not $vm.Cloud) { continue }

    $maxBytes = [double]0; $usedBytes = [double]0
    foreach ($d in @($vm.VirtualDiskDrives)) {
        if ($d.VirtualHardDisk) {
            $maxBytes  += [double]$d.VirtualHardDisk.MaximumSize
            $usedBytes += [double]$d.VirtualHardDisk.Size
        }
    }

    $prop = $null
    if ($in.CustomPropertyName) {
        try { $prop = $vm.CustomProperty[$in.CustomPropertyName] } catch { }
        if (-not $prop -and $cp) {
            try { $prop = (Get-SCCustomPropertyValue -InputObject $vm -CustomProperty $cp -ErrorAction SilentlyContinue).Value } catch { }
        }
    }

    $tags = @()
    if ($vm.Tag) { $tags = @($vm.Tag | ForEach-Object { "$_" } | Where-Object { $_ -and $_ -ne '(none)' }) }

    $cluster = $null
    try { if ($vm.VMHost -and $vm.VMHost.HostCluster) { $cluster = $vm.VMHost.HostCluster.Name } } catch { }

    $vms.Add([pscustomobject]@{
        id              = "$($vm.ID)"
        vm_guid         = "$($vm.VMId)"
        name            = $vm.Name
        computer_name   = $vm.ComputerName
        cloud_id        = "$($vm.Cloud.ID)"
        cloud_name      = $vm.Cloud.Name
        host            = "$($vm.HostName)"
        cluster         = $cluster
        status          = "$($vm.Status)"
        highly_available= [bool]$vm.IsHighlyAvailable
        in_library      = (-not $vm.VMHost)
        max_gb          = [math]::Round($maxBytes / 1GB, 1)
        used_gb         = [math]::Round($usedBytes / 1GB, 1)
        custom_property = $prop
        tags            = $tags
        owner           = "$($vm.Owner)"
    })
}

[pscustomobject]@{ clouds = $clouds; vms = $vms.ToArray() } | ConvertTo-Json -Depth 5 -Compress
