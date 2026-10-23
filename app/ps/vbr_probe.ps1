param([string]$InputJson)
# Diagnostics: confirms the module loads and shows the property names Veeam exposes
# in this version, so the GUID matching can be checked against the real estate.
Connect-HvboVbr
$out = [ordered]@{
    ps_version   = "$($PSVersionTable.PSVersion)"
    ps_edition   = "$($PSVersionTable.PSEdition)"
    computer     = $env:COMPUTERNAME
    user         = "$([Security.Principal.WindowsIdentity]::GetCurrent().Name)"
    vbr_module   = "$((Get-Module Veeam.Backup.PowerShell).Version)"
    hv_servers   = @(Get-VBRServer | Where-Object { "$($_.Type)" -match 'Hv|Scvmm|HyperV' } | ForEach-Object { "$($_.Name) [$($_.Type)]" })
    sample_vm    = $null
    sample_job   = $null
}
$vm = Find-VBRHvEntity | Where-Object { "$($_.Type)" -match '^Vm' } | Select-Object -First 1
if ($vm) {
    $out.sample_vm = [ordered]@{
        name = $vm.Name; type = "$($vm.Type)"; path = "$($vm.Path)"; refs = Get-HvboRefs $vm
        properties = @($vm.PSObject.Properties | ForEach-Object { "$($_.Name)=$($_.Value)" } | Select-Object -First 30)
    }
}
$job = Get-VBRJob -WarningAction SilentlyContinue | Where-Object { Test-HvboIsHyperVJob $_ } | Select-Object -First 1
if ($job) {
    $o = Get-VBRJobObject -Job $job | Select-Object -First 1
    $out.sample_job = [ordered]@{
        name = $job.Name; type = "$($job.TypeToString)"; start_time = Get-HvboJobTime $job
        retention = Get-HvboRetention $job
        object = if ($o) { [ordered]@{ name = $o.Name; type = "$($o.Type)"; obj_type = "$($o.Object.Type)"; refs = Get-HvboRefs $o } } else { $null }
    }
}
[pscustomobject]$out | ConvertTo-Json -Depth 6 -Compress
