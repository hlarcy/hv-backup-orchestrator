param([string]$InputJson)
# Runs on the VBR server. Applies a list of operations and reports per-operation results.
# Operations: ensure_job, set_schedule, retier_vm, ensure_copy
#
# ISOLATION: this server also protects VMware and Nutanix. Every operation that changes an
# existing job calls Assert-HvboOwned first, which refuses any job without the orchestrator
# marker or that is not a Hyper-V backup job. New jobs are only created under names no other
# job uses. Nothing here starts, stops, deletes or edits a job it did not create.
Connect-HvboVbr
$in = $InputJson | ConvertFrom-Json

function Get-HvboJob($op) {
    if ($op.job_id) {
        $j = Get-VBRJob -WarningAction SilentlyContinue | Where-Object { "$($_.Id)" -eq "$($op.job_id)" }
        if ($j) { return $j }
    }
    if ($op.job_name) { return (Get-VBRJob -WarningAction SilentlyContinue | Where-Object { $_.Name -eq $op.job_name } | Select-Object -First 1) }
    return $null
}

function Get-HvboRepo($name) {
    $r = Get-VBRBackupRepository -Name $name -ErrorAction SilentlyContinue
    if (-not $r) { $r = Get-VBRBackupRepository -ScaleOut -Name $name -ErrorAction SilentlyContinue }
    if (-not $r) { throw "Backup repository '$name' not found." }
    return $r
}

function Get-HvboRepoId($job) {
    try { return "$($job.GetBackupTargetRepository().Id)" } catch { return $null }
}

function Set-HvboRetention($job, [int]$days) {
    Assert-HvboOwned $job
    $opts = Get-VBRJobOptions -Job $job
    try { $opts.BackupStorageOptions.RetentionType = [Veeam.Backup.Model.ERetentionType]::Days }
    catch { try { $opts.BackupStorageOptions.RetentionType = 'Days' } catch { } }
    $opts.BackupStorageOptions.RetainDaysToKeep = $days
    Set-VBRJobOptions -Job $job -Options $opts | Out-Null
}

function Set-HvboSchedule($job, [string]$at) {
    $job = Get-HvboJob ([pscustomobject]@{ job_id = "$($job.Id)" })
    Assert-HvboOwned $job
    Set-VBRJobSchedule -Job $job -Daily -At $at -DailyKind Everyday | Out-Null
    $job = Get-HvboJob ([pscustomobject]@{ job_id = "$($job.Id)" })
    Enable-VBRJobSchedule -Job $job | Out-Null
}

# Weekly GFS on an orchestrator backup job, with a weekly synthetic full on the same day so the
# GFS point always has a full to use. Only ever called on a job that passed Assert-HvboOwned.
# VBR 12+ made the synthetic-full properties on the options object read-only, so those are set
# with Set-VBRJobAdvancedBackupOptions (parameter spelling differs between builds: Synthetic/Syntethic).
function Set-HvboBackupGfs($job, [string]$day, [int]$weeks) {
    Assert-HvboOwned $job
    $opts = Get-VBRJobOptions -Job $job
    $opts.GfsPolicy.IsEnabled = $true
    $opts.GfsPolicy.Weekly.IsEnabled = $true
    $opts.GfsPolicy.Weekly.KeepBackupsForNumberOfWeeks = $weeks
    try { $opts.GfsPolicy.Weekly.DesiredTime = $day } catch { }
    Set-VBRJobOptions -Job $job -Options $opts | Out-Null

    $job = Get-HvboJob ([pscustomobject]@{ job_id = "$($job.Id)" })
    Assert-HvboOwned $job
    $cmd = Get-Command Set-VBRJobAdvancedBackupOptions -ErrorAction SilentlyContinue
    if (-not $cmd) { throw "Set-VBRJobAdvancedBackupOptions is not available, so the weekly synthetic full could not be set." }
    $sp = if ($cmd.Parameters.ContainsKey('TransformFullToSynthetic')) { 'Synthetic' } else { 'Syntethic' }
    $splat = @{ Job = $job; Algorithm = 'Incremental'; "TransformFullTo$sp" = $true
                "TransformTo${sp}Days" = [System.DayOfWeek[]]@([System.DayOfWeek]$day) }
    if ($cmd.Parameters.ContainsKey("TransformTo${sp}ScheduleKind")) { $splat["TransformTo${sp}ScheduleKind"] = 'Daily' }
    Set-VBRJobAdvancedBackupOptions @splat | Out-Null
}

# Backup file encryption with the vault key, on one of our own backup jobs.
function Set-HvboBackupEncryption($job, $key) {
    Assert-HvboOwned $job
    Set-VBRJobAdvancedStorageOptions -Job $job -EnableEncryption $true -EncryptionKey $key | Out-Null
}

function New-HvboCopyStorage($key, [string]$compression, [string]$optimization) {
    # In-memory storage settings for a backup copy job (encryption with the vault key); changes nothing on its own.
    return (New-VBRBackupCopyJobStorageOptions -CompressionLevel $compression -StorageOptimizationType $optimization `
            -EnableEncryption -EncryptionKey $key)
}

function New-HvboCopyGfs([string]$day, [int]$weeks) {
    # In-memory settings object for a backup copy job; changes nothing on its own.
    $weekly = New-VBRComputerGFSWeeklyOptions -RetentionPeriod $weeks -SelectedDay $day
    return (New-VBRComputerGFSOptions -GFSWeeklyOptions $weekly)
}

function Get-HvboVerify($job) {
    $job = Get-HvboJob ([pscustomobject]@{ job_id = "$($job.Id)" })
    $ret = Get-HvboRetention $job
    return [pscustomobject]@{
        start_time       = Get-HvboJobTime $job
        schedule_enabled = [bool]$job.IsScheduleEnabled
        retention_type   = $ret.type
        retention_days   = $ret.days
        object_count     = @(Get-VBRJobObject -Job $job).Count
        gfs              = Get-HvboGfs $job
        encryption       = Get-HvboEncryption $job
    }
}

function Get-HvboVmBackup($job, $vm) {
    # Find the backup of $job holding $vm's restore points. Read only.
    # Matched by Hyper-V VM ID first, so a VM renamed since its last backup is still found (Veeam
    # keeps the name it had when backed up). Falls back to the current name only when the backup
    # exposes no IDs at all. Returns @{ Backup; Name (the name inside the backup); By }.
    $g = "$($vm.vm_guid)".ToLower()
    foreach ($b in @(Get-VBRBackup | Where-Object { "$($_.JobId)" -eq "$($job.Id)" })) {
        $items = @()
        try { $items = @($b.GetObjects()) } catch { }
        if (-not $items.Count) { try { $items = @(Get-VBRRestorePoint -Backup $b) } catch { } }
        $sawIds = $false
        foreach ($o in $items) {
            $refs = ((Get-HvboRefs $o) -join ' ').ToLower()
            if ($refs) { $sawIds = $true }
            if ($g -and $refs.Contains($g)) { return @{ Backup = $b; Name = $o.Name; By = 'VM ID' } }
        }
        if (-not $sawIds) {
            $hit = @($items | Where-Object { $_.Name -eq $vm.name } | Select-Object -First 1)
            if ($hit.Count) { return @{ Backup = $b; Name = $hit[0].Name; By = 'name' } }
        }
    }
    return $null
}

$results = New-Object System.Collections.Generic.List[object]
foreach ($op in @($in.operations)) {
    $res = [ordered]@{ id = $op.id; op = $op.op; ok = $false; job_id = $op.job_id; job_name = $op.job_name; messages = @() }
    try {
        switch ($op.op) {
            'ensure_job' {
                $job = $null
                if ($op.job_id) {
                    $job = Get-HvboJob $op
                    Assert-HvboOwned $job
                } else {
                    $clash = Get-HvboAnyJobByName $op.job_name
                    if ($clash) { Assert-HvboOwned $clash; $job = $clash }   # only reuse a job that is ours
                }
                $ents = New-Object System.Collections.Generic.List[object]
                $missing = @()
                $stepFailed = $false
                foreach ($vm in @($op.vms)) {
                    if ($job -and (Test-HvboJobHasVm $job $vm)) { continue }
                    try { $ents.Add((Find-HvboVm $vm)) } catch { $missing += $_.Exception.Message }
                }
                $res.messages += $missing
                if ($job) {
                    if ($ents.Count -gt 0) {
                        Add-VBRHvJobObject -Job $job -Entities $ents.ToArray() | Out-Null
                        $res.messages += "Added $($ents.Count) VM(s) to '$($job.Name)'."
                    } else { $res.messages += "Nothing to add to '$($job.Name)'." }
                } else {
                    if ($ents.Count -eq 0) { throw "No VMs could be resolved in Veeam; job not created." }
                    # Resolve the encryption key first: no key, no job (never create an unencrypted one).
                    $encKey = if ($op.encryption_key) { Get-HvboEncryptionKey $op.encryption_key } else { $null }
                    $repo = Get-HvboRepo $op.repository
                    $job = Add-VBRHvBackupJob -Name $op.job_name -Entity $ents.ToArray() -BackupRepository $repo -Description $op.description
                    $res.messages += "Created job '$($op.job_name)' with $($ents.Count) VM(s) on '$($op.repository)'."
                    if ($op.template_job) {
                        $tpl = Get-VBRJob -WarningAction SilentlyContinue | Where-Object { $_.Name -eq $op.template_job } | Select-Object -First 1
                        if ($tpl -and (Test-HvboIsHyperVJob $tpl)) {
                            # Template is only read. Its options are copied onto our new job.
                            Set-VBRJobOptions -Job $job -Options $tpl.GetOptions() | Out-Null
                            try { Set-VBRJobVssOptions -Job $job -Options $tpl.GetVssOptions() | Out-Null } catch { $res.messages += "Template VSS options not copied: $($_.Exception.Message)" }
                            $res.messages += "Copied options from template job '$($op.template_job)'."
                        } else { $res.messages += "Template job '$($op.template_job)' not found or not a Hyper-V job; using defaults." }
                    }
                    # Each setting is applied on its own, so one failure never leaves the rest undone.
                    # Encryption and the schedule go first; a failure here marks the result failed and the
                    # next plan picks up whatever is still missing (set_encryption / set_schedule / set_gfs).
                    $steps = New-Object System.Collections.Generic.List[object]
                    $steps.Add(@('retention', { Set-HvboRetention $job $op.retention_days }, "Retention $($op.retention_days) days."))
                    if ($encKey) { $steps.Add(@('encryption', { Set-HvboBackupEncryption $job $encKey }, "Backup files encrypted with key '$($op.encryption_key)'.")) }
                    if ($op.start_time) { $steps.Add(@('schedule', { Set-HvboSchedule $job $op.start_time }, "Daily schedule set to $($op.start_time).")) }
                    if ($op.gfs) { $steps.Add(@('weekly GFS', { Set-HvboBackupGfs $job $op.gfs.day ([int]$op.gfs.weeks) }, "Weekly GFS on, keeping $($op.gfs.weeks) week(s), with a synthetic full every $($op.gfs.day).")) }
                    foreach ($st in $steps) {
                        try {
                            $job = Get-HvboJob ([pscustomobject]@{ job_id = "$($job.Id)" })
                            & $st[1]
                            $res.messages += $st[2]
                        } catch { $stepFailed = $true; $res.messages += "Setting $($st[0]) failed: $($_.Exception.Message)" }
                    }
                }
                $res.job_id = "$($job.Id)"; $res.job_name = $job.Name
                $res.verify = Get-HvboVerify $job
                $res.ok = ($missing.Count -eq 0) -and -not $stepFailed
            }
            'set_schedule' {
                $job = Get-HvboJob $op
                Assert-HvboOwned $job
                Set-HvboSchedule $job $op.start_time
                $res.messages += "Daily schedule set to $($op.start_time)."
                $res.verify = Get-HvboVerify $job
                $res.ok = ($res.verify.start_time -eq $op.start_time)
                if (-not $res.ok) { $res.messages += "Read-back start time $($res.verify.start_time) differs from $($op.start_time)." }
            }
            'retier_vm' {
                # Move VMs from one of our jobs to another of our jobs, taking their restore points along.
                $src = Get-HvboJob ([pscustomobject]@{ job_id = $op.from_job_id })
                Assert-HvboOwned $src
                $dst = Get-HvboJob $op
                Assert-HvboOwned $dst
                $res.job_id = "$($dst.Id)"; $res.job_name = $dst.Name
                $sameRepo = ((Get-HvboRepoId $src) -and (Get-HvboRepoId $src) -eq (Get-HvboRepoId $dst))
                $allDone = $true
                foreach ($vm in @($op.vms)) {
                    $state = 'no restore points to move'
                    if ($op.remap) {
                        $found = Get-HvboVmBackup $src $vm
                        if ($found) {
                            $bk = $found.Backup
                            try {
                                if ($sameRepo) {
                                    Move-VBRBackup -Backup $bk -Name @($found.Name) -Job $dst -Force -Confirm:$false | Out-Null
                                    $state = "restore points moved (found by $($found.By)$(if ($found.Name -ne $vm.name) { ", backed up as '$($found.Name)'" }))"
                                } else {
                                    Move-VBRBackup -Backup $bk -Name @($found.Name) -Job $dst -Force -Confirm:$false -RunAsync | Out-Null
                                    $state = 'restore point move started (different repository, runs in background)'
                                }
                            } catch { $state = "restore points not moved: $($_.Exception.Message)" }
                        }
                    }
                    $dst = Get-HvboJob ([pscustomobject]@{ job_id = "$($dst.Id)" })
                    Assert-HvboOwned $dst
                    if (-not (Test-HvboJobHasVm $dst $vm)) {
                        Add-VBRHvJobObject -Job $dst -Entities @(Find-HvboVm $vm) | Out-Null
                    }
                    # Only take the VM out of the old job once no restore points are left behind there.
                    $left = if ($op.remap) { Get-HvboVmBackup $src $vm } else { $null }
                    $src = Get-HvboJob ([pscustomobject]@{ job_id = "$($src.Id)" })
                    Assert-HvboOwned $src
                    if ($left) {
                        $allDone = $false
                        $res.messages += "'$($vm.name)': $state. Kept in '$($src.Name)' as well until its restore points have moved; the next sync finishes it."
                    } else {
                        $g = "$($vm.vm_guid)".ToLower()
                        $objs = @(Get-VBRJobObject -Job $src | Where-Object {
                            ($g -and ((Get-HvboRefs $_) -join ' ').ToLower().Contains($g)) -or ($_.Name -eq $vm.name) })
                        if ($objs.Count) { Remove-VBRJobObject -Objects $objs | Out-Null }
                        $res.messages += "'$($vm.name)': $state; now in '$($dst.Name)', removed from '$($src.Name)'."
                    }
                }
                $res.ok = $allDone
                $res.partial = (-not $allDone)
            }
            'ensure_copy' {
                $existing = Get-HvboAnyJobByName $op.copy_name
                if ($existing) {
                    Assert-HvboOwned $existing 'copy'
                    $res.messages += "Copy job '$($op.copy_name)' already exists."
                    $res.job_id = "$($existing.Id)"; $res.job_name = $existing.Name; $res.ok = $true
                    break
                }
                $src = Get-HvboJob ([pscustomobject]@{ job_id = $op.source_job_id; job_name = $op.source_job_name })
                Assert-HvboOwned $src
                $repo = Get-HvboRepo $op.repository
                if ((Get-HvboRepoId $src) -eq "$($repo.Id)") { throw "Copy target '$($op.repository)' is the source job's own repository." }
                $copyArgs = @{ Name = $op.copy_name; Description = $op.description; Mode = $op.mode; BackupJob = @($src)
                               TargetRepository = $repo; DirectOperation = $true
                               RetentionType = 'RestoreDays'; RetentionNumber = [int]$op.retention_days }
                if ($op.gfs) { $copyArgs.GFSOptions = New-HvboCopyGfs $op.gfs.day ([int]$op.gfs.weeks) }
                if ($op.encryption_key) {
                    $copyArgs.StorageOptions = New-HvboCopyStorage (Get-HvboEncryptionKey $op.encryption_key) $op.copy_compression $op.copy_optimization
                }
                $cj = Add-VBRBackupCopyJob @copyArgs
                if ($op.gfs) { $res.messages += "Weekly GFS on, keeping $($op.gfs.weeks) week(s), weekly full on $($op.gfs.day)." }
                if ($op.encryption_key) { $res.messages += "Backup files encrypted with key '$($op.encryption_key)'." }
                $res.job_id = "$($cj.Id)"; $res.job_name = $cj.Name
                $res.messages += "Created $($op.mode) backup copy job '$($op.copy_name)' from '$($src.Name)' to '$($op.repository)', keeping $($op.retention_days) days."
                $res.verify = [pscustomobject]@{ retention_type = "$($cj.RetentionType)"; retention_days = $cj.RetentionNumber; mode = "$($cj.Mode)"; gfs = Get-HvboGfs $cj 'copy'; encryption = Get-HvboEncryption $cj 'copy' }
                $res.ok = $true
            }
            'set_encryption' {
                # Turn on backup file encryption with the vault key on one of our own jobs.
                $key = Get-HvboEncryptionKey $op.encryption_key
                if ($op.job_kind -eq 'copy') {
                    $cj = @(Get-VBRBackupCopyJob -WarningAction SilentlyContinue | Where-Object { "$($_.Id)" -eq "$($op.job_id)" })[0]
                    Assert-HvboOwned $cj 'copy'
                    Set-VBRBackupCopyJob -Job $cj -StorageOptions (New-HvboCopyStorage $key $op.copy_compression $op.copy_optimization) | Out-Null
                    $cj = @(Get-VBRBackupCopyJob -WarningAction SilentlyContinue | Where-Object { "$($_.Id)" -eq "$($op.job_id)" })[0]
                    $e = Get-HvboEncryption $cj 'copy'
                    $res.job_id = "$($cj.Id)"; $res.job_name = $cj.Name
                } else {
                    $job = Get-HvboJob $op
                    Assert-HvboOwned $job
                    Set-HvboBackupEncryption $job $key
                    $job = Get-HvboJob $op
                    $e = Get-HvboEncryption $job
                    $res.job_id = "$($job.Id)"; $res.job_name = $job.Name
                }
                $res.verify = [pscustomobject]@{ encryption = $e }
                $res.ok = (-not $e) -or ($e.enabled -and ((-not $e.key_id) -or $e.key_id -eq "$($key.Id)"))
                $res.messages += $(if ($res.ok) { "Backup files encrypted with key '$($op.encryption_key)'. Veeam runs an active full next time for existing backups." } else { "Encryption read back as $($e | ConvertTo-Json -Compress)." })
            }
            'set_gfs' {
                # Turn on / correct weekly GFS (and the weekly synthetic full) on one of our own jobs.
                if ($op.job_kind -eq 'copy') {
                    $cj = @(Get-VBRBackupCopyJob -WarningAction SilentlyContinue | Where-Object { "$($_.Id)" -eq "$($op.job_id)" })[0]
                    Assert-HvboOwned $cj 'copy'
                    Set-VBRBackupCopyJob -Job $cj -GFSOptions (New-HvboCopyGfs $op.day ([int]$op.weeks)) | Out-Null
                    $cj = @(Get-VBRBackupCopyJob -WarningAction SilentlyContinue | Where-Object { "$($_.Id)" -eq "$($op.job_id)" })[0]
                    $res.job_id = "$($cj.Id)"; $res.job_name = $cj.Name
                    $res.verify = [pscustomobject]@{ gfs = Get-HvboGfs $cj 'copy' }
                    $res.messages += "Copy job weekly GFS: keep $($op.weeks) week(s), weekly full on $($op.day)."
                    $res.ok = $true
                } else {
                    $job = Get-HvboJob $op
                    Assert-HvboOwned $job
                    Set-HvboBackupGfs $job $op.day ([int]$op.weeks)
                    $v = Get-HvboVerify $job
                    $res.job_id = "$($job.Id)"; $res.job_name = $job.Name; $res.verify = $v
                    $g = $v.gfs
                    $res.ok = (-not $g) -or ($g.enabled -and $g.weekly -and $g.synthetic -and $g.day -eq $op.day)
                    $res.messages += $(if ($res.ok) { "Weekly GFS: keep $($op.weeks) week(s), synthetic full every $($op.day)." } else { "GFS read back as $($g | ConvertTo-Json -Compress), expected weekly on $($op.day)." })
                }
            }
            'rename_job' {
                # Only our own jobs (the marker is checked on the live job), and only to a name nobody uses.
                if ($op.job_kind -eq 'copy') {
                    $job = @(Get-VBRBackupCopyJob -WarningAction SilentlyContinue | Where-Object { "$($_.Id)" -eq "$($op.job_id)" })[0]
                    Assert-HvboOwned $job 'copy'
                } else {
                    $job = Get-HvboJob $op
                    Assert-HvboOwned $job
                }
                $clash = Get-HvboAnyJobByName $op.new_name
                if ($clash -and "$($clash.Id)" -ne "$($job.Id)") { throw "Another job is already called '$($op.new_name)'." }
                if ($op.job_kind -eq 'copy') {
                    Set-VBRBackupCopyJob -Job $job -Name $op.new_name | Out-Null
                    $after = @(Get-VBRBackupCopyJob -WarningAction SilentlyContinue | Where-Object { "$($_.Id)" -eq "$($op.job_id)" })[0].Name
                } else {
                    # Veeam has no supported rename cmdlet for Hyper-V backup jobs; this is the documented workaround.
                    $job.Info.CommonInfo.Name = $op.new_name
                    $job.Update() | Out-Null
                    $after = (Get-HvboJob ([pscustomobject]@{ job_id = $op.job_id })).Name
                }
                $res.ok = ($after -eq $op.new_name)
                $res.job_id = "$($job.Id)"; $res.job_name = $after
                $res.messages += $(if ($res.ok) { "Renamed '$($op.job_name)' to '$($op.new_name)'." } else { "Rename did not take effect; the job is still called '$after'." })
            }
            'remove_deleted_vms' {
                # Removes objects for VMs that no longer exist in SCVMM. Matched by VM ID only, never by name.
                # The restore points are not touched; Veeam's retention handles them.
                $job = Get-HvboJob $op
                Assert-HvboOwned $job
                $removed = @()
                foreach ($vm in @($op.vms)) {
                    $g = "$($vm.vm_guid)".ToLower()
                    if (-not $g) { continue }
                    $objs = @(Get-VBRJobObject -Job $job | Where-Object { ((Get-HvboRefs $_) -join ' ').ToLower().Contains($g) })
                    if ($objs.Count) { Remove-VBRJobObject -Objects $objs | Out-Null; $removed += $vm.name }
                }
                $res.messages += "Removed $($removed.Count) deleted VM(s) from '$($job.Name)': $($removed -join ', '). Their restore points are kept."
                $res.job_id = "$($job.Id)"; $res.job_name = $job.Name
                $res.ok = $true
            }
            default { throw "Unknown operation '$($op.op)'." }
        }
    } catch {
        $res.messages += $_.Exception.Message
        $res.ok = $false
    }
    $results.Add([pscustomobject]$res)
}

[pscustomobject]@{ results = $results.ToArray() } | ConvertTo-Json -Depth 6 -Compress
