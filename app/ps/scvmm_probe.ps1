param([string]$InputJson)
$ErrorActionPreference = 'Stop'
$in = $InputJson | ConvertFrom-Json
Import-Module VirtualMachineManager | Out-Null
$vmm = Get-SCVMMServer -ComputerName $in.VmmServer
[pscustomobject]@{
    ps_version  = "$($PSVersionTable.PSVersion)"
    computer    = $env:COMPUTERNAME
    vmm_version = "$($vmm.ProductVersion)"
    clouds      = @(Get-SCCloud -VMMServer $vmm | ForEach-Object { $_.Name })
    custom_property_found = [bool](Get-SCCustomProperty -VMMServer $vmm -Name $in.CustomPropertyName -ErrorAction SilentlyContinue)
} | ConvertTo-Json -Depth 4 -Compress
