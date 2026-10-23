param([string]$InputJson, [string]$ScriptB64, [string]$Pwsh)
# Runs inside the Windows PowerShell 5.1 endpoint (Microsoft.PowerShell) and executes the real
# script in a PowerShell 7 child process. Used for VBR 13.x, whose module needs PowerShell 7.6,
# when the PowerShell.7 remoting endpoint cannot be used. Input and output go through UTF-8
# files in a private temp folder, so there are no command-line length or encoding limits.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

if (-not (Test-Path -LiteralPath $Pwsh)) {
    $cmd = Get-Command pwsh.exe -ErrorAction SilentlyContinue
    if (-not $cmd) { throw "PowerShell 7 not found at '$Pwsh' or on PATH. Install PowerShell 7.6+ or set vbr.pwsh_path." }
    $Pwsh = $cmd.Source
}

$dir = Join-Path $env:TEMP ("hvbo-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $dir | Out-Null
try {
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    $scriptFile = Join-Path $dir 'run.ps1'
    $inFile = Join-Path $dir 'in.json'
    $outFile = Join-Path $dir 'out.json'
    $errFile = Join-Path $dir 'err.txt'
    [IO.File]::WriteAllBytes($scriptFile, [Convert]::FromBase64String($ScriptB64))
    [IO.File]::WriteAllText($inFile, $InputJson, $utf8)

    $q = { param($p) $p -replace "'", "''" }
    $inner = "`$ErrorActionPreference = 'Stop'; " +
             "`$r = & '$(& $q $scriptFile)' -InputJson ([IO.File]::ReadAllText('$(& $q $inFile)')); " +
             "[IO.File]::WriteAllText('$(& $q $outFile)', (`$r | Out-String), (New-Object System.Text.UTF8Encoding(`$false)))"
    # Windows PowerShell 5.1 turns native stderr into errors; don't let that abort the run.
    $ErrorActionPreference = 'Continue'
    $null = & $Pwsh -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command $inner 2> $errFile
    $code = $LASTEXITCODE
    $ErrorActionPreference = 'Stop'

    if (Test-Path -LiteralPath $outFile) {
        $text = [IO.File]::ReadAllText($outFile, $utf8).Trim()
        if ($text) { return $text }
    }
    $err = if (Test-Path -LiteralPath $errFile) { (Get-Content -LiteralPath $errFile -Raw) } else { '' }
    throw "PowerShell 7 ($Pwsh) exited with code $code and no output. $err"
} finally {
    Remove-Item -LiteralPath $dir -Recurse -Force -ErrorAction SilentlyContinue
}
