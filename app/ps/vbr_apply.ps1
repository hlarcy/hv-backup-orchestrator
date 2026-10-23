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

function Get-HvboVerify($job) {
    $job = Get-HvboJob ([pscustomobject]@{ job_id = "$($job.Id)" })
    $ret = Get-HvboRetention $job
    return [pscustomobject]@{
        start_time       = Get-HvboJobTime $job
        schedule_enabled = [bool]$job.IsScheduleEnabled
        retention_type   = $ret.type
        retention_days   = $ret.days
        object_count     = @(Get-VBRJobObject -Job $job).Count
    }
}

function Get-HvboVmBackup($job, $vm) {
    # The backup (restore point chain) of $job that holds $vm, if any. Read only.
    foreach ($b in @(Get-VBRBackup | Where-Object { "$($_.JobId)" -eq "$($job.Id)" })) {
        try { if (@($b.GetObjects() | Where-Object { $_.Name -eq $vm.name }).Count -gt 0) { return $b } } catch { }
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
                    Set-HvboRetention $job $op.retention_days
                    if ($op.start_time) { Set-HvboSchedule $job $op.start_time }
                }
                $res.job_id = "$($job.Id)"; $res.job_name = $job.Name
                $res.verify = Get-HvboVerify $job
                $res.ok = ($missing.Count -eq 0)
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
                        $bk = Get-HvboVmBackup $src $vm
                        if ($bk) {
                            try {
                                if ($sameRepo) {
                                    Move-VBRBackup -Backup $bk -Name @($vm.name) -Job $dst -Force -Confirm:$false | Out-Null
                                    $state = 'restore points moved'
                                } else {
                                    Move-VBRBackup -Backup $bk -Name @($vm.name) -Job $dst -Force -Confirm:$false -RunAsync | Out-Null
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
                $cj = Add-VBRBackupCopyJob -Name $op.copy_name -Description $op.description -Mode $op.mode `
                        -BackupJob @($src) -TargetRepository $repo -DirectOperation `
                        -RetentionType RestoreDays -RetentionNumber ([int]$op.retention_days)
                $res.job_id = "$($cj.Id)"; $res.job_name = $cj.Name
                $res.messages += "Created $($op.mode) backup copy job '$($op.copy_name)' from '$($src.Name)' to '$($op.repository)', keeping $($op.retention_days) days."
                $res.verify = [pscustomobject]@{ retention_type = "$($cj.RetentionType)"; retention_days = $cj.RetentionNumber; mode = "$($cj.Mode)" }
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
