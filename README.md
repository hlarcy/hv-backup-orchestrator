# HV Backup Orchestrator

Automates Veeam Backup & Replication 13.1 job creation and scheduling for a multi-tenant Hyper-V 2025 estate managed by SCVMM. It runs in a Docker container with a web UI and drives SCVMM and the Windows VBR server remotely over PowerShell remoting (PSRP/WinRM).

## What it does

- Reads every SCVMM cloud and the VMs inside it.
- Puts every cloud VM into its cloud's 30-day job (`HVBO <cloud> Standard 30d`). There are no exclusions by default.
- When a VM is marked LargeData (SCVMM custom property `BackupTier=LargeData` or tag `LargeData`), moves it to the cloud's 90-day job (`HVBO <cloud> LargeData 90d`), creating that job if needed, and moves the VM's existing restore points with it.
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

Hand-built Hyper-V jobs are treated the same way: VMs in them count as backed up (unless `coverage_mode: managed_only`), but the jobs are never changed. A VM in a hand-built job that is marked LargeData also goes into its cloud's 90-day job; the hand-built job is left as it is.

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

## How a VM is tiered

- Default: 30 days. Size, role and name do not matter.
- 90 days: SCVMM custom property `BackupTier` set to `LargeData`, or SCVMM tag `LargeData`. Create the property once with `New-SCCustomProperty -Name BackupTier -AddMember @("VM")`.
- Exclusions are off. `exclude_values` and `exclude_tags` exist in the config if ever needed; empty means every cloud VM is backed up.

### Tier moves and restore points

When LargeData appears on a VM in its cloud's 30-day job, the next sync (automatic by default, `retier.auto_upgrade`):

1. creates the 90-day job and its copy job if the cloud does not have them yet;
2. moves the VM's restore points from the 30-day job's backup into the 90-day job with `Move-VBRBackup -Backup <30d backup> -Name <vm> -Job <90d job>`, so they follow 90-day retention from then on;
3. makes sure the VM is in the 90-day job;
4. removes the VM from the 30-day job, only once no restore points are left behind there.

If both jobs use the same repository the move is quick and finishes in the same run. If they use different repositories the data has to be copied, so the move runs in the background and the VM stays in both jobs until it finishes; the next sync completes the hand-over. If a move fails (for example because a job is running), the VM stays in both jobs and the move is retried next sync, so it is never left without a backup.

Per-VM moves need per-machine backup files, the default for new repositories and SOBRs since VBR 12. Check the tenant repositories use them.

Removing LargeData would shorten retention, so the move back to 30 days waits for approval on the Plan page (`retier.auto_downgrade: false`). VMs that move to a different SCVMM cloud also wait for approval.

### Backup copy jobs

With `backup_copy.enabled: true`, each managed backup job, new or existing, gets a copy job:

- `Add-VBRBackupCopyJob -Mode Immediate -BackupJob <job> -TargetRepository <copy repo> -DirectOperation -RetentionType RestoreDays -RetentionNumber 7`;
- target is `backup_copy.target_repository`, or a per-cloud entry in `backup_copy.cloud_repositories`;
- refused if no target is configured, or if the target is the source job's own repository;
- the copy job follows its backup job, so VMs added or moved are picked up automatically.

Copy jobs are only created for the tool's own jobs, never for VMware, Nutanix or hand-built jobs. When a VM moves to the 90-day job, its copies in the 30-day copy chain age out after 7 days and new copies come from the 90-day job.

## How "already backed up" is decided

A VM counts as protected if any Hyper-V backup job in VBR contains it:

- explicitly, matched on the Hyper-V VM GUID (so identically named VMs in different tenants never collide);
- by name, only when that name is unique across the whole SCVMM estate;
- through a host or cluster container object in a job, unless the VM is excluded from that job.

Managed jobs carry a marker in their description, `[hvbo cloud=<SCVMM cloud ID> tier=<tier>]`. That marker is how the app recognises its own jobs, so renaming a job in the console is safe; removing the marker is not.

## Safety

- Ships in plan-only mode (`auto_apply: false`). Each sync builds a plan you can review on the Plan page and apply by hand. Turn auto-apply on in the UI once you trust it.
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

Mount your internal CA certificate into `/config` and set `ca_bundle`, or set `cert_validation: false` for a first test only.

### Veeam module and PowerShell 7

VBR 13 ships a PowerShell module. If `Import-Module Veeam.Backup.PowerShell` fails under Windows PowerShell 5.1 on the VBR server, install PowerShell 7 there, run `Enable-PSRemoting -Force` from `pwsh`, and set `vbr.configuration_name: PowerShell.7`. The Diagnostics page shows which edition answered.

SCVMM's `VirtualMachineManager` module needs Windows PowerShell 5.1, so leave SCVMM on `Microsoft.PowerShell`.

### Veeam must see the VMs

Add the SCVMM server (or the Hyper-V failover cluster) to VBR's managed servers and rescan. VMs are resolved with `Find-VBRHvEntity`; a VM VBR cannot see is reported on the run page and skipped.

## Install

```bash
cd hv-backup-orchestrator
cp config/config.example.yaml config/config.yaml   # set hosts, repository, rules
cp .env.example .env                                # set UI login and service account passwords
docker compose up -d --build
```

Open `http://<docker-host>:8080`, sign in, then:

1. Diagnostics: run the connection test. Check that `sample_vm.refs` contains a GUID that matches the VM's `VMId` in SCVMM. That confirms tenant-safe matching on your build.
2. Overview: Discover and plan. Nothing is changed in VBR.
3. Plan: review the jobs, VMs, repositories and start times. Apply.
4. Check one created job in the VBR console: retention, schedule, objects.
5. Overview: turn auto-apply on.

Try the UI without any infrastructure: set `HVBO_MOCK=true` in `.env`. The mock estate adds a new VM on some discoveries to show auto-detection.

## Configuration notes

- `template_job`: name of an existing Hyper-V job whose advanced, storage and guest processing (VSS) options are copied onto each new job, then retention is set. Use this for compression, encryption, health checks, application-aware processing and guest credentials, so every tenant job matches your standard.
- Repository selection: `cloud_tier_repositories` beats `cloud_repositories`, which beats a tier's `repository`, which beats `default_repository`. A plan that targets a repository VBR does not have is blocked.
- `include_clouds` / `exclude_clouds` limit scope, for example to keep an infrastructure cloud out.
- Start times are written in the VBR server's local time. With 2 jobs per cloud the window holds 60 tenants; beyond that, widen the window or shorten the stagger and the app will report a full window rather than double-book a slot.
- `schedule.order: large_first` and the Rebalance button repack the schedule with large-data jobs first. A rebalance is a plan like any other and needs applying.

## Things to validate on your build

These scripts were written against the documented VBR 12/13 PowerShell object model and could not be run against a live 13.1 server while building. Before enabling auto-apply, use the Diagnostics page and one manual apply to confirm:

- `Move-VBRBackup -Name <vm> -Job <90d job>` on a test VM: confirm the restore points appear under the 90-day job and the VM is then removed from the 30-day job.
- `Add-VBRBackupCopyJob` creates the copy job with 7 days on the copy SOBR (the run page shows the read-back).
- The Isolation page lists your VMware and Nutanix jobs. If one is missing it is still never changed (only marked jobs are), but its start time will not be avoided.
- `Get-VBRJobObject` exposes the VM GUID in one of the probed properties (`app/ps/vbr_common.ps1`, `Get-HvboRefs`). If it does not, matching falls back to unique names and the run log will say so.
- Retention is written as days. The app sets `BackupStorageOptions.RetentionType = Days` and `RetainDaysToKeep`. The run page shows the read-back value; if it shows restore points instead, adjust `Set-HvboRetention` in `app/ps/vbr_apply.ps1`.
- `job.TypeToString` contains "Hyper-V" for your backup jobs (used to filter out VMware, agent and copy jobs).

## Development

```bash
pip install -r requirements-dev.txt
pytest -q
HVBO_MOCK=true HVBO_CONFIG=config/config.example.yaml HVBO_DATA_DIR=./data \
  HVBO_UI_USER=admin HVBO_UI_PASSWORD=admin uvicorn app.main:app --reload --port 8080
```

| Path | Purpose |
|---|---|
| `app/planner.py` | Classification, coverage, slot allocation, action list |
| `app/slots.py` | 18:00 to 00:00 window and 3-minute slots |
| `app/engine.py` | Discover, plan, apply in three phases (jobs, tier moves, copy jobs); run lock and history |
| `app/safety.py` | Ownership guard checked before anything is sent to VBR |
| `app/backend.py` | Live PSRP backend and mock estate |
| `app/ps/*.ps1` | PowerShell run on SCVMM and VBR |
| `app/main.py` | Web UI and JSON API (`/api/status`, `/api/plan`, `/api/docs`) |
