param([string]$InputJson)
# Runs on the VBR server. READ ONLY: nothing in this script changes anything.
# Returns:
#   jobs          Hyper-V backup jobs with objects, schedule, retention (managed and hand-built)
#   copy_jobs     backup copy jobs carrying the orchestrator marker (others are not returned)
#   foreign_jobs  name, type and start time of every other job (VMware, Nutanix, agents...)
#   all_job_names every job name on the server, so new names never collide
Connect-HvboVbr

$jobs = New-Object System.Collections.Generic.List[object]
$foreign = New-Object System.Collections.Generic.List[object]
$names = New-Object System.Collections.Generic.List[string]

foreach ($j in @(Get-VBRJob -WarningAction SilentlyContinue)) {
    $names.Add($j.Name)
    if (-not (Test-HvboIsHyperVJob $j)) {
        if ("$($j.Description)" -match '\[hvbo') { continue }   # our own copy jobs listed by Get-VBRJob
        $foreign.Add([pscustomobject]@{
            name = $j.Name; type = "$($j.TypeToString)"; platform = "$($j.BackupPlatform)"
            start_time = Get-HvboJobTime $j; schedule_enabled = [bool]$j.IsScheduleEnabled
        })
        continue
    }
    $objects = @(Get-VBRJobObject -Job $j | ForEach-Object {
        $kindRaw = "$($_.Object.Type) $($_.TypeDisplayName)"
        [pscustomobject]@{
            name    = $_.Name
            kind    = $(if ($kindRaw -match '\bVm\b|Virtual Machine') { 'vm' } else { 'container' })
            kind_raw= $kindRaw.Trim()
            include = -not ("$($_.Type)" -match 'Exclude')
            refs    = Get-HvboRefs $_
            path    = "$($_.Location)"
        }
    })
    $repo = $null
    try { $repo = $j.GetBackupTargetRepository().Name } catch { }
    $ret = Get-HvboRetention $j
    $jobs.Add([pscustomobject]@{
        id               = "$($j.Id)"
        name             = $j.Name
        description      = $j.Description
        type             = "$($j.TypeToString)"
        enabled          = [bool]$j.IsScheduleEnabled
        schedule_enabled = [bool]$j.IsScheduleEnabled
        start_time       = Get-HvboJobTime $j
        retention_type   = $ret.type
        retention_days   = $ret.days
        retention_points = $ret.points
        repository       = $repo
        objects          = $objects
    })
}

$copies = New-Object System.Collections.Generic.List[object]
try {
    foreach ($c in @(Get-VBRBackupCopyJob -WarningAction SilentlyContinue)) {
        if (-not $names.Contains($c.Name)) { $names.Add($c.Name) }
        if ("$($c.Description)" -notmatch '\[hvbo-copy\s') {
            $foreign.Add([pscustomobject]@{ name = $c.Name; type = 'Backup Copy'; platform = ''; start_time = $null; schedule_enabled = $false })
            continue
        }
        $target = $null; $days = $null
        try { $target = $c.TargetRepository.Name } catch { }
        try { $days = $c.RetentionNumber } catch { }
        $copies.Add([pscustomobject]@{
            id = "$($c.Id)"; name = $c.Name; description = $c.Description; mode = "$($c.Mode)"
            repository = $target; retention_days = $days
        })
    }
} catch { }

foreach ($getter in 'Get-VBRComputerBackupJob', 'Get-VBRNASBackupJob', 'Get-VBRUnstructuredBackupJob') {
    if (-not (Get-Command $getter -ErrorAction SilentlyContinue)) { continue }
    try {
        foreach ($x in @(& $getter -WarningAction SilentlyContinue)) {
            if ($names.Contains($x.Name)) { continue }
            $names.Add($x.Name)
            $foreign.Add([pscustomobject]@{ name = $x.Name; type = $getter.Replace('Get-VBR', ''); platform = ''; start_time = $null; schedule_enabled = $false })
        }
    } catch { }
}

$repos = @(Get-VBRBackupRepository | ForEach-Object { $_.Name }) + @(Get-VBRBackupRepository -ScaleOut | ForEach-Object { $_.Name })

[pscustomobject]@{
    jobs = $jobs.ToArray(); copy_jobs = $copies.ToArray(); foreign_jobs = $foreign.ToArray()
    all_job_names = $names.ToArray(); repositories = $repos
} | ConvertTo-Json -Depth 6 -Compress
