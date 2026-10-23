# HV Backup Orchestrator

Automates Veeam Backup & Replication 13.1 job creation and scheduling for a multi-tenant Hyper-V 2025 estate managed by SCVMM. It runs in a Docker container with a web UI and drives SCVMM and the Windows VBR server remotely over PowerShell remoting (PSRP/WinRM).

## What it does

- Reads every SCVMM cloud and the VMs inside it.
- Reads each VM's retention from its SCVMM custom property `BackupTier` (a number of days; 30 if blank or not a number) and keeps one job per retention per cloud, e.g. `HVBO <cloud> 30d` and `HVBO <cloud> 180d`. When the number changes, the VM and its existing restore points move to the matching job.
- Gives every managed backup job a backup copy job (`<job> Copy`) to a separate repository or SOBR, keeping 7 days. Configurable.
- On each sync (default hourly), picks up new VMs and tag changes.
- Gives every managed job its own daily start time between 18:00 and 00:00, 3 minutes apart (120 slots). Slots are sticky, and minutes already used by any other job on the server (VMware, Nutanix, hand-built) are skipped.
- Never changes any job it did not create. See "Isolation" below.
- Records every run, action and VBR read-back in a run history.

## Isolation from VMware, Nutanix and existing jobs

The VBR server also protects live VMware and Nutanix estates. The tool is built so it cannot change those jobs:

1. Ownership marker. Jobs the tool creates carry `[hvbo cloud=<id> tier=<tier>]` (backup) or `[hvbo-copy cloud=<id> tier=<tier>]` (copy) in their description. Nothing else is ever owned.
2. Python guard (`app/safety.py`). Before anything is sent, every operation in every phase is checked against the owned job IDs captured at plan time. New jobs must use a name no existing job on the server has. One failure refuses the whole batch and nothing is sent.
3. PowerShell guard (`Assert-HvboOwned` in `app/ps/vbr_common.ps1`). On the VBR server, every change re-reads the live job and refuses it unless it carries the marker and is a Hyper-V backup job (`JobType = Backup`, platform Hyper-V, not VMware, Nutanix, agent, replica or copy).
4. A small, tested set of change cmdlets: `Add-VBRHvBackupJob`, `Add-VBRBackupCopyJob`, `Add-VBRHvJobObject`, `Remove-VBRJobObject`, `Set-VBRJobOptions`, `Set-VBRJobVssOptions`, `Set-VBRJobSchedule`, `Enable-VBRJobSchedule` and `Move-VBRBackup`. `tests/test_safety.py` fails if any other change cmdlet appears, if a read-only script contains one, or if an operation changes anything before calling `Assert-HvboOwned`.
5. Nothing destructive. The tool never deletes, disables, starts or stops a job, and never changes a global or server setting. The only removal is taking a VM out of one of its own jobs after a tier move.
6. Other jobs are read for two things only: their names (so new names never collide) and their daily start times (so managed jobs never start in the same 3 minutes). The Isolation page lists them all as read only.

Hand-built Hyper-V jobs are treated the same way: VMs in them count as backed up (unless `coverage_mode: managed_only`), but the jobs are never changed. A VM in a hand-built job whose BackupTier asks for a non-default retention also goes into its cloud's job for that retention; the hand-built job is left as it is.

Shared repositories are the one indirect effect: new Hyper-V and copy jobs add load to whatever repositories and SOBRs you point them at. Point `default_repository` and `backup_copy.target_repository` at Hyper-V tenant storage, not at repositories sized for the VMware or Nutanix estates.

## Architecture

```
 Browser ──HTTPS──> [ Docker: FastAPI + scheduler + SQLite ]
                          │  PSRP (WinRM 5986)        │  PSRP (WinRM 5986)
                          ▼                           ▼
                   SCVMM server                VBR 13.1 server (Windows)
                   VirtualMachineManager       Veeam.Backup.PowerShell
                   (read only)                 (creates and edits jobs)
```

The container never needs Windows. PowerShell scripts live in `app/ps/`, are sent over PSRP, and return JSON. Planning is pure Python (`app/planner.py`) and is unit-tested.

The VBR REST API was not used because job creation for Hyper-V is still more complete in the Veeam PowerShell module. SCVMM has no REST API, so PSRP is needed for it anyway.

## How retention is set

One SCVMM custom property on the VM, `BackupTier`, holds its retention in days. Create it once with `New-SCCustomProperty -Name BackupTier -AddMember @("VM")`.

| `BackupTier` value | Retention |
|---|---|
| A whole number above 0, e.g. `7`, `45`, `180`, `400` | Exactly that many days, shorter or longer than the default |
| Blank, missing, or anything else (`0`, `12.5`, `180 days`, text) | `default_retention_days` (30) |

Each distinct retention gets its own job per cloud (for example `HVBO Murrays Hall 180d`), with its own start slot and copy job. Size, role and name do not matter, and no VM can opt out.

### Retention changes and restore points

When a VM's retention changes (for example its BackupTier goes from blank to 180), the next sync (automatic by default, `retier.auto_upgrade`):

1. creates the longer job and its copy job if the cloud does not have them yet;
2. moves the VM's restore points into the new job with `Move-VBRBackup -Backup <old backup> -Name <vm> -Job <new job>`, so they follow the new retention from then on;
3. makes sure the VM is in the new job;
4. removes the VM from the old job, only once no restore points are left behind there.

If both jobs use the same repository the move is quick and finishes in the same run. If they use different repositories the data has to be copied, so the move runs in the background and the VM stays in both jobs until it finishes; the next sync completes the hand-over. If a move fails (for example because a job is running), the VM stays in both jobs and the move is retried next sync, so it is never left without a backup.

Per-VM moves need per-machine backup files, the default for new repositories and SOBRs since VBR 12. Check the tenant repositories use them.

Lowering retention (180 to 90, or clearing the property) shortens how long existing backups are kept, so by default it waits for approval on the Plan page. Set `retier.auto_downgrade: true` to apply it automatically. VMs that move to a different SCVMM cloud also wait for approval.

### Backup copy jobs

With `backup_copy.enabled: true`, each managed backup job, new or existing, gets a copy job:

- `Add-VBRBackupCopyJob -Mode Immediate -BackupJob <job> -TargetRepository <copy repo> -DirectOperation -RetentionType RestoreDays -RetentionNumber 7`;
- target is `backup_copy.target_repository`, or a per-cloud entry in `backup_copy.cloud_repositories`;
- refused if no target is configured, or if the target is the source job's own repository;
- the copy job follows its backup job, so VMs added or moved are picked up automatically.

Copy jobs are only created for the tool's own jobs, never for VMware, Nutanix or hand-built jobs. When a VM moves job, its copies in the old copy chain age out after 7 days and new copies come from the new job.

## Encryption

Every backup job and copy job the app manages encrypts its backup files with the key in VBR's key vault (Password Manager) whose description is `encryption.key_description` (default `ITGlue`).

- The key is looked up by exact description, and must match exactly one key. If it is missing or ambiguous, every create is blocked (and an attention email goes out), so a job is never created unencrypted or with the wrong key. VMs added to existing jobs are unaffected.
- On a new backup job the key is applied before the schedule is enabled, after any `template_job` options are copied, so a template cannot switch it off. Copy jobs get it at creation (`New-VBRBackupCopyJobStorageOptions -EnableEncryption -EncryptionKey`, which also sets `backup_copy.compression` and `storage_optimization`).
- Every sync checks the live setting. An unencrypted managed job, or one using a different key, is fixed automatically (`Set-VBRJobAdvancedStorageOptions -EnableEncryption $true -EncryptionKey`). Veeam takes an active full the next time such a job runs.
- Hand-built, VMware and Nutanix jobs are never touched.

## Weekly GFS and synthetic fulls

Every backup job and copy job the app manages has weekly GFS turned on ("Keep certain full backups longer for archival purposes", weekly, `gfs.weekly_keep_weeks`, default 1). Backup jobs also get a weekly synthetic full on the same day, so there is always a full for the weekly GFS point to use.

- Each job gets one day, spread across the week (least-used day first, from `gfs.days`), so the synthetic fulls do not all land on the same night. A copy job uses its source job's day.
- Days are sticky, like start times: adding a cloud never moves another job's day. A job that already has weekly GFS on an allowed day keeps it.
- Every sync checks the live settings. If someone turns GFS off, changes the weeks, the day or the synthetic full, the app puts it back automatically (only on its own jobs). The Schedule page shows how many jobs land on each day.
- Backup jobs use the job options (`GfsPolicy.Weekly`, `BackupTargetOptions.TransformFullToSyntethic` / `TransformToSyntethicDays`); copy jobs use `New-VBRComputerGFSOptions` on `Add-VBRBackupCopyJob` / `Set-VBRBackupCopyJob`. Both are read back after setting. If a Veeam build does not expose these settings in readable form, the app relies on what it last applied instead of re-applying every run.

## Deleted, renamed and moved things

| What changed | What the app does |
|---|---|
| VM deleted in SCVMM | Its VM ID is no longer anywhere in SCVMM, so it is removed from the app's own job (after approval by default, `cleanup.deleted_vms`). Only by VM ID, never by name. Restore points are kept and age out under Veeam's retention. Skipped if SCVMM reports no VMs, or any VM without an ID, and blocked if more than `cleanup.max_per_run` disappear at once. A VM that only left its cloud is not treated as deleted. |
| VM renamed | Nothing to do: VMs are matched by VM ID. When its retention later changes, its restore points are found by VM ID too, so history moves with it even though Veeam still has it under the old name. |
| `BackupTier` changed | Moved to the job for the new number with its restore points (see above). Lowering waits for approval unless `retier.auto_downgrade: true`. |
| Cloud renamed | Jobs are tied to the cloud's ID, so nothing breaks. The app's jobs and copy jobs whose names contain the old cloud name are renamed (after approval by default, `rename_jobs_on_cloud_rename`); jobs you renamed by hand are left alone. Per-cloud config keyed by ID keeps working; config keyed by name needs updating. |

Renaming a Hyper-V backup job uses Veeam's documented workaround (`$job.Info.CommonInfo.Name` then `Update()`), because there is no supported rename cmdlet for that job type. The result is read back and reported. Copy jobs are renamed with `Set-VBRBackupCopyJob -Name`.

## Email alerts

Set `email.enabled: true` with `host`, `sender` and `to` (and `SMTP_PASSWORD` in `.env` if needed), then use Send test email on the Diagnostics page.

- Change: every run that created or changed something in VBR, with what was done, the values read back from VBR, and any failures.
- Attention: actions waiting for approval, blocked actions, and, while auto-apply is off, changes waiting to be applied. Sent when the list changes, and again every `reminder_hours` while unchanged.
- Failure: a run that could not finish (for example SCVMM or VBR unreachable). Sent when the error changes, then as a reminder; cleared by the next good run.

## How "already backed up" is decided

A VM counts as protected if any Hyper-V backup job in VBR contains it:

- explicitly, matched on the Hyper-V VM GUID (so identically named VMs in different tenants never collide);
- by name, only when that name is unique across the whole SCVMM estate;
- through a host or cluster container object in a job, unless the VM is excluded from that job.

Managed jobs carry a marker in their description, `[hvbo cloud=<SCVMM cloud ID> tier=<tier>]`. That marker is how the app recognises its own jobs, so renaming a job in the console is safe; removing the marker is not.

## Safety

- Ships in plan-only mode (`auto_apply: false`). Each sync builds a plan you can review on the Plan page and apply by hand. Turn auto-apply on in the UI once you trust it.
- You can apply the whole plan (automatic actions only, or including approvals) or one action at a time: each row on the Plan page and a cloud's page has its own **Apply** button. Applying one action counts as approving it, goes through the same safety checks, and refreshes the plan afterwards. A copy job whose source job does not exist yet, or a move into a job another action creates, cannot be applied alone; apply that other action first.
- Plans older than 30 minutes cannot be applied; discovery must run again.
- One run at a time. The scheduler and the buttons share a lock.
- Every create, schedule change and copy job is read back from VBR and shown on the run page.

## Prerequisites

### Service accounts

| Target | Rights |
|---|---|
| SCVMM | VMM user role with read access to all tenant clouds (Read-Only Administrator is enough), plus WinRM access on the VMM server. |
| VBR | Member of local Administrators (or Remote Management Users) on the VBR server, and the Veeam Backup Administrator role in VBR. |

A single gMSA cannot be used from Linux, so use a normal domain service account with a long password and restrict its logon rights.

### WinRM on both Windows servers

HTTPS listener (recommended):

```powershell
$cert = Get-ChildItem Cert:\LocalMachine\My | Where-Object { $_.Subject -match $env:COMPUTERNAME } | Select-Object -First 1
New-Item -Path WSMan:\localhost\Listener -Transport HTTPS -Address * -CertificateThumbPrint $cert.Thumbprint -Force
New-NetFirewallRule -DisplayName "WinRM HTTPS" -Direction Inbound -Protocol TCP -LocalPort 5986 -Action Allow
Set-Item WSMan:\localhost\Service\Auth\Basic $false
```

Then tell the app how to trust the certificate, per host:

- CA-issued certificate: mount your internal CA root (PEM) into `/config` and set `ca_bundle`.
- Self-signed certificate: set `cert_thumbprint` to the listener certificate's thumbprint. Only that exact certificate is accepted, so this is as strict as CA validation for a single host, and connecting by IP address works. Read it on the server with `Get-ChildItem WSMan:\localhost\Listener\*\CertificateThumbprint` (or `winrm enumerate winrm/config/listener`). Pasting with spaces, colons or the hidden character certmgr adds is fine.
- If a TLS connection fails, the Diagnostics page shows the thumbprint the server actually presented, so you can compare it with the one on the server.

### Veeam module and PowerShell 7

VBR 13.x's `Veeam.Backup.PowerShell` module requires PowerShell 7.6 and will not load in Windows PowerShell 5.1. Install PowerShell 7.6 or later on the VBR server, then pick one:

- Bridge (default, recommended): `vbr.pwsh_bridge: true` with `configuration_name: Microsoft.PowerShell`. The app connects to the built-in 5.1 endpoint and runs each script in a `pwsh.exe` child process, passing input and output through UTF-8 files in a private temp folder that is deleted afterwards. No extra remoting endpoint is needed.
- Native endpoint: `pwsh_bridge: false`, `configuration_name: PowerShell.7`, after running `Enable-PSRemoting -Force` from an elevated `pwsh`. Some builds refuse this endpoint for local accounts over remote logons; use the bridge if so.

SCVMM's `VirtualMachineManager` module needs Windows PowerShell 5.1, so leave SCVMM on `Microsoft.PowerShell` without the bridge.

### Veeam must see the VMs

Add the SCVMM server (or the Hyper-V failover cluster) to VBR's managed servers and rescan. VMs are resolved with `Find-VBRHvEntity`; a VM VBR cannot see is reported on the run page and skipped.

## Access

The app has no login of its own. It is meant to sit behind a reverse proxy that authenticates users, so:

- make sure only the proxy can reach port 8080 (firewall it, put it on an internal Docker network, or bind it to `127.0.0.1` when the proxy runs on the same host);
- anyone who gets past the proxy can apply changes to VBR, so restrict the proxy to the people who should;
- if the proxy passes the signed-in user in `X-Forwarded-User`, `Remote-User`, `X-Auth-Request-User` or `X-Forwarded-Email`, it is recorded against each manual run;
- form posts are refused unless they come from the app's own pages (matching `Host` or `X-Forwarded-Host`), so another website cannot use a signed-in browser to trigger an apply.

## Install

```bash
cd hv-backup-orchestrator
cp config/config.example.yaml config/config.yaml   # set hosts, repository, rules
cp .env.example .env                                # set the service account passwords
docker compose up -d --build
```

Open the app through your reverse proxy, then:

1. Diagnostics: run the connection test. Check that `sample_vm.refs` contains a GUID that matches the VM's `VMId` in SCVMM. That confirms tenant-safe matching on your build.
2. Overview: Discover and plan. Nothing is changed in VBR.
3. Plan: review the jobs, VMs, repositories and start times. Apply.
4. Check one created job in the VBR console: retention, schedule, objects.
5. Overview: turn auto-apply on.

Try the UI without any infrastructure: set `HVBO_MOCK=true` in `.env`. The mock estate adds a new VM on some discoveries to show auto-detection.

## Configuration notes

`config/config.example.yaml` lists every setting with its default. All sections (`encryption`, `gfs`, `backup_copy`, `email` and so on) are top-level and apply globally to every cloud and every job the app manages; only the `*_repositories` maps and `include_clouds` / `exclude_clouds` are per cloud. A test fails if a setting is added to the app without being added to the example.


- `template_job`: name of an existing Hyper-V job whose advanced, storage and guest processing (VSS) options are copied onto each new job, then retention is set. Use this for compression, encryption, health checks, application-aware processing and guest credentials, so every tenant job matches your standard.
- Repository selection: `cloud_tier_repositories` beats `cloud_repositories`, which beats a tier's `repository`, which beats `default_repository`. A plan that targets a repository VBR does not have is blocked.
- `include_clouds` / `exclude_clouds` limit scope, for example to keep an infrastructure cloud out.
- Start times are written in the VBR server's local time. With 2 jobs per cloud the window holds 60 tenants; beyond that, widen the window or shorten the stagger and the app will report a full window rather than double-book a slot.
- `schedule.order: large_first` and the Rebalance button repack the schedule with large-data jobs first. A rebalance is a plan like any other and needs applying.

## Things to validate on your build

These scripts were written against the documented VBR 12/13 PowerShell object model and could not be run against a live 13.1 server while building. Before enabling auto-apply, use the Diagnostics page and one manual apply to confirm:

- `Move-VBRBackup` on a test VM: set its BackupTier to a new number and confirm its restore points appear under the new job and the VM is then removed from the old one.
- `Add-VBRBackupCopyJob` creates the copy job with 7 days on the copy SOBR (the run page shows the read-back).
- Open one new backup job and one copy job in the console: Storage, Advanced, Storage should show encryption with the ITGlue key, and Storage, Configure GFS should show weekly, 1 week, on the day the Schedule page lists, and the backup job's Advanced settings should show a weekly synthetic full on that day.
- The Isolation page lists your VMware and Nutanix jobs. If one is missing it is still never changed (only marked jobs are), but its start time will not be avoided.
- `Get-VBRJobObject` exposes the VM GUID in one of the probed properties (`app/ps/vbr_common.ps1`, `Get-HvboRefs`). If it does not, matching falls back to unique names and the run log will say so.
- Retention is written as days. The app sets `BackupStorageOptions.RetentionType = Days` and `RetainDaysToKeep`. The run page shows the read-back value; if it shows restore points instead, adjust `Set-HvboRetention` in `app/ps/vbr_apply.ps1`.
- `job.TypeToString` contains "Hyper-V" for your backup jobs (used to filter out VMware, agent and copy jobs).

## Development

```bash
pip install -r requirements-dev.txt
pytest -q
HVBO_MOCK=true HVBO_CONFIG=config/config.example.yaml HVBO_DATA_DIR=./data \
  uvicorn app.main:app --reload --port 8080
```

| Path | Purpose |
|---|---|
| `app/planner.py` | Classification, coverage, slot allocation, action list |
| `app/slots.py` | 18:00 to 00:00 window and 3-minute slots |
| `app/engine.py` | Discover, plan, apply in three phases (jobs, tier moves, copy jobs); run lock and history |
| `app/safety.py` | Ownership guard checked before anything is sent to VBR |
| `app/notify.py` | SMTP email alerts |
| `app/backend.py` | Live PSRP backend and mock estate |
| `app/ps/*.ps1` | PowerShell run on SCVMM and VBR |
| `app/main.py` | Web UI and JSON API (`/api/status`, `/api/plan`, `/api/docs`) |
