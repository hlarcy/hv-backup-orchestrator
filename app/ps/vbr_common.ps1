# Shared helpers, prepended to every VBR script by the orchestrator.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$WarningPreference = 'SilentlyContinue'

# $HvboVbrServer and $HvboAcceptVbrTls are injected by the orchestrator from config (vbr.server_name,
# vbr.accept_local_tls_certificate). VBR 13 talks TLS even to itself, and a non-interactive session
# cannot answer the "trust this certificate?" prompt, so the certificate is accepted explicitly.
function Connect-HvboVbr {
    if (-not (Get-Module -Name Veeam.Backup.PowerShell)) {
        Import-Module Veeam.Backup.PowerShell -DisableNameChecking -WarningAction SilentlyContinue | Out-Null
    }
    $session = $null
    try { $session = Get-VBRServerSession -ErrorAction SilentlyContinue } catch { }
    if ($session) { return }

    $server = if ($HvboVbrServer) { $HvboVbrServer } else { $env:COMPUTERNAME }
    $cmd = Get-Command Connect-VBRServer
    $canAccept = $cmd.Parameters.ContainsKey('ForceAcceptTlsCertificate')
    try {
        if ($HvboAcceptVbrTls -and $canAccept) {
            Connect-VBRServer -Server $server -ForceAcceptTlsCertificate -ErrorAction Stop | Out-Null
        } else {
            Connect-VBRServer -Server $server -ErrorAction Stop | Out-Null
        }
    } catch {
        $hint = if (-not $HvboAcceptVbrTls) { ' Set vbr.accept_local_tls_certificate: true in config.' }
                elseif (-not $canAccept) { ' This Connect-VBRServer has no -ForceAcceptTlsCertificate; trust the VBR certificate for this account first.' }
                else { '' }
        throw "Connect-VBRServer to '$server' failed: $($_.Exception.Message)$hint"
    }
}

# Collect GUID-looking identifiers from the properties Veeam uses across versions.
function Get-HvboRefs($o) {
    $refs = New-Object System.Collections.Generic.List[string]
    $paths = @('Reference', 'ObjectRef', 'Ref', 'Uuid', 'VmId', 'Object.ObjectRef', 'Object.Reference',
               'Object.Info.Ref', 'Object.Info.ObjectRef', 'Object.Uuid', 'Info.Ref', 'Info.ObjectRef')
    foreach ($p in $paths) {
        $v = $o
        foreach ($part in $p.Split('.')) {
            if ($null -eq $v) { break }
            try { $v = $v.$part } catch { $v = $null }
        }
        if ($v) { $refs.Add("$v") }
    }
    return ,$refs.ToArray()
}

# True only for Hyper-V backup jobs. VMware, Nutanix AHV, agent, replica and copy jobs are all false.
function Test-HvboIsHyperVJob($j) {
    if (-not $j) { return $false }
    $t = "$($j.TypeToString) $($j.BackupPlatform) $($j.JobType)"
    if ("$($j.JobType)" -ne 'Backup') { return $false }
    if ($t -match 'VMware|vSphere|vCloud|Nutanix|AHV|Agent|Replica|Copy|Plugin') { return $false }
    return ($t -match 'Hyper-?V')
}

# Hard guard: every change goes through this. Anything without our marker is refused.
function Assert-HvboOwned($job, [string]$Kind = 'backup') {
    if (-not $job) { throw "SAFETY: target job not found; refusing to continue." }
    $d = "$($job.Description)"
    if ($Kind -eq 'copy') {
        if ($d -notmatch '\[hvbo-copy\s+cloud=') { throw "SAFETY: '$($job.Name)' is not an orchestrator backup copy job. It was not changed." }
        return
    }
    if ($d -notmatch '\[hvbo\s+cloud=') { throw "SAFETY: '$($job.Name)' is not managed by the orchestrator. It was not changed." }
    if (-not (Test-HvboIsHyperVJob $job)) { throw "SAFETY: '$($job.Name)' carries the marker but is not a Hyper-V backup job. It was not changed." }
}

# Any existing job, of any kind, with this name (read only).
function Get-HvboAnyJobByName([string]$Name) {
    $hit = @(Get-VBRJob -WarningAction SilentlyContinue | Where-Object { $_.Name -eq $Name })
    if ($hit.Count) { return $hit[0] }
    try { $hit = @(Get-VBRBackupCopyJob -WarningAction SilentlyContinue | Where-Object { $_.Name -eq $Name }) } catch { $hit = @() }
    if ($hit.Count) { return $hit[0] }
    try { $hit = @(Get-VBRComputerBackupJob -WarningAction SilentlyContinue | Where-Object { $_.Name -eq $Name }) } catch { $hit = @() }
    if ($hit.Count) { return $hit[0] }
    return $null
}

function Test-HvboJobHasVm($job, $vm) {
    $g = "$($vm.vm_guid)".ToLower()
    return (@(Get-VBRJobObject -Job $job | Where-Object {
        -not ("$($_.Type)" -match 'Exclude') -and (
            ($g -and ((Get-HvboRefs $_) -join ' ').ToLower().Contains($g)) -or ($_.Name -eq $vm.name))
    }).Count -gt 0)
}

function Get-HvboJobTime($j) {
    try {
        $o = $j.ScheduleOptions
        if ($o.OptionsDaily -and $o.OptionsDaily.Enabled) { return ([datetime]$o.OptionsDaily.TimeLocal).ToString('HH:mm') }
        if ($o.OptionsDaily) { return ([datetime]$o.OptionsDaily.TimeLocal).ToString('HH:mm') }
    } catch { }
    return $null
}

# Value at a dotted property path, or $null. Read only.
function Get-HvboProp($o, [string]$Path) {
    $v = $o
    foreach ($part in $Path.Split('.')) {
        if ($null -eq $v) { return $null }
        try { $v = $v.$part } catch { return $null }
    }
    return $v
}

# The one encryption key in VBR's key vault (Password Manager) whose description matches exactly.
# Read only. Throws unless exactly one matches, so a job is never created unencrypted or with the wrong key.
function Get-HvboEncryptionKey([string]$Description) {
    $keys = @(Get-VBREncryptionKey | Where-Object { "$($_.Description)" -eq $Description })
    if ($keys.Count -ne 1) {
        throw "Encryption key '$Description' must exist exactly once in VBR's key vault; found $($keys.Count)."
    }
    return $keys[0]
}

# Encryption state of a backup job or a backup copy job: @{ enabled; key_id } or $null if unreadable. Read only.
function Get-HvboEncryption($j, [string]$Kind = 'backup') {
    try {
        if ($Kind -eq 'copy') {
            $s = Get-HvboProp $j 'StorageOptions'
            if (-not $s) { return $null }
            $en = $null
            foreach ($p in 'EnableEncryption', 'EncryptionEnabled', 'StorageEncryptionEnabled') {
                if ($s.PSObject.Properties[$p]) { $en = [bool]$s.$p; break }
            }
            if ($null -eq $en) { return $null }
            $kid = $null
            foreach ($p in 'EncryptionKey.Id', 'EncryptionKeyId', 'KeyId') { if (-not $kid) { $kid = Get-HvboProp $s $p } }
            return [ordered]@{ enabled = $en; key_id = $(if ($kid) { "$kid" } else { $null }) }
        }
        $o = Get-VBRJobOptions -Job $j
        $en = Get-HvboProp $o 'BackupStorageOptions.StorageEncryptionEnabled'
        if ($null -eq $en) { return $null }
        $kid = $null
        foreach ($p in 'UserCryptoKey.Id', 'Info.PwdKeyId', 'Info.CryptoKeyId') { if (-not $kid) { $kid = Get-HvboProp $j $p } }
        if ("$kid" -eq '00000000-0000-0000-0000-000000000000') { $kid = $null }
        return [ordered]@{ enabled = [bool]$en; key_id = $(if ($kid) { "$kid" } else { $null }) }
    } catch { return $null }
}

# GFS weekly + synthetic full state of a backup job, or of a backup copy job. Read only.
# Returns $null when this Veeam build does not expose the settings in a readable form.
function Get-HvboGfs($j, [string]$Kind = 'backup') {
    try {
        if ($Kind -eq 'copy') {
            $g = $null
            foreach ($p in 'GFSOptions', 'GfsOptions', 'GFSRetentionOptions') { if (-not $g) { try { $g = $j.$p } catch { } } }
            if (-not $g) { return $null }
            $w = $g.GFSWeeklyOptions; if (-not $w) { $w = $g.WeeklyOptions }
            if (-not $w) { return $null }
            $en = $null
            foreach ($p in 'IsEnabled', 'Enabled', 'EnableRetention') {
                if ($w.PSObject.Properties[$p]) { $en = [bool]$w.$p; break }
            }
            if ($null -eq $en) { return $null }   # cannot tell: the planner falls back to what it last applied
            # Copy jobs build their weekly GFS full themselves on the selected day; no separate synthetic setting.
            return [ordered]@{ enabled = $en; weekly = $en; weeks = $w.RetentionPeriod; day = "$($w.SelectedDay)"
                               synthetic = $true; synthetic_days = @("$($w.SelectedDay)") }
        }
        $o = Get-VBRJobOptions -Job $j
        $gp = $o.GfsPolicy; $bt = $o.BackupTargetOptions
        if (-not $gp -or -not $bt) { return $null }
        return [ordered]@{
            enabled = [bool]$gp.IsEnabled; weekly = [bool]$gp.Weekly.IsEnabled
            weeks = $gp.Weekly.KeepBackupsForNumberOfWeeks; day = "$($gp.Weekly.DesiredTime)"
            synthetic = [bool]$bt.TransformFullToSyntethic; synthetic_days = @($bt.TransformToSyntethicDays | ForEach-Object { "$_" })
        }
    } catch { return $null }
}

function Get-HvboRetention($j) {
    $r = [ordered]@{ type = $null; days = $null; points = $null }
    try {
        $s = (Get-VBRJobOptions -Job $j).BackupStorageOptions
        $r.type = "$($s.RetentionType)"
        $r.days = $s.RetainDaysToKeep
        $r.points = $s.RetainCycles
    } catch { }
    return $r
}

# Resolve an SCVMM VM to a Veeam Hyper-V entity. Matches by VM GUID first, then
# by name + host/cluster, so identically named VMs in different tenants never collide.
function Find-HvboVm($vm) {
    $cands = @(Find-VBRHvEntity -Name $vm.name | Where-Object { "$($_.Type)" -match '^Vm' })
    if ($vm.vm_guid) {
        $g = "$($vm.vm_guid)".ToLower()
        $hit = @($cands | Where-Object { (Get-HvboRefs $_) -join ' ' -match [regex]::Escape($g) })
        if ($hit.Count -ge 1) { return $hit[0] }
    }
    $hostShort = if ($vm.host) { "$($vm.host)".Split('.')[0] } else { $null }
    if ($hostShort) {
        $hit = @($cands | Where-Object { "$($_.Path)" -match [regex]::Escape($hostShort) })
        if ($hit.Count -eq 1) { return $hit[0] }
    }
    if ($cands.Count -eq 1 -and -not $vm.vm_guid) { return $cands[0] }
    throw "VM '$($vm.name)' (GUID $($vm.vm_guid)) not found unambiguously in Veeam inventory ($($cands.Count) candidates). Rescan the Hyper-V/SCVMM server in VBR."
}
