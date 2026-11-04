param([string]$NativeProfileFile, [switch]$ActivateNative, [string]$SessionDirectory, [string]$NodeExecutable)
$ErrorActionPreference = 'Stop'
$qrRoot = $PSScriptRoot
$qrNode = if ($NodeExecutable) { $NodeExecutable } else { (Get-Command node -ErrorAction Stop).Source }
# Existing installations retain their pairing; deployments can supply a new private directory explicitly.
$qrDirectory = if ($SessionDirectory) { $SessionDirectory } else { Join-Path $env:USERPROFILE '.codex\secrets\impulsa-qr-direct-c443' }
if (-not [IO.Path]::IsPathRooted($qrDirectory)) { throw 'The session directory must be absolute' }
if ($ActivateNative -and -not $NativeProfileFile) { throw 'Native activation requires an explicit private profile file' }
$qrMode = if ($ActivateNative) { 'native_agent' } else { 'direct_pairing_only' }
$qrProfileStatus = if ($ActivateNative) { 'ENABLED' } else { 'STAGED_OFF' }
$qrEntry = Join-Path $qrRoot 'src\qr-direct.ts'
if ($NativeProfileFile) {
  if (-not [IO.Path]::IsPathRooted($NativeProfileFile) -or $NativeProfileFile -notmatch '^(?:[A-Za-z]:[\\/]|\\\\)' -or -not (Test-Path -LiteralPath $NativeProfileFile -PathType Leaf)) { throw 'A staged native profile must be an existing absolute private file' }
  $qrProfileHash = (Get-FileHash -LiteralPath $NativeProfileFile -Algorithm SHA256).Hash.ToLowerInvariant()
}
foreach ($qrPath in @($qrNode, $qrEntry, (Join-Path $qrRoot 'qr-direct-runtime\node_modules\@builderbot\provider-baileys\dist\index.mjs'))) {
  if (-not (Test-Path -LiteralPath $qrPath -PathType Leaf)) { throw 'Required direct QR component is missing' }
}
$qrHealth = $null
try { $qrHealth = Invoke-RestMethod -Uri 'http://127.0.0.1:3008/api/health' -TimeoutSec 2 } catch {}
if ($qrHealth) {
  $qrOwner = Get-CimInstance Win32_Process -Filter ("ProcessId=" + [int]$qrHealth.pid)
  if ($qrOwner.ExecutablePath -eq $qrNode -and $qrOwner.CommandLine.Contains($qrEntry) -and $qrOwner.CommandLine.Contains($qrDirectory)) {
    if ($qrHealth.mode -ne $qrMode -or ($NativeProfileFile -and ($qrHealth.nativeProfile.status -ne $qrProfileStatus -or $qrHealth.nativeProfile.fingerprint -ne $qrProfileHash))) { throw 'The current Impulsa QR process has different activation/profile settings; an explicit restart is required' }
    [ordered]@{ pid=$qrOwner.ProcessId; mode=$qrMode; alreadyRunning=$true; url='http://127.0.0.1:3008/api/bots/impulsa/qr?format=view' } | ConvertTo-Json -Compress
    exit 0
  }
  throw 'Port 3008 belongs to another process; inspect it before starting Impulsa'
}
# Windows may retain a PID 0 LISTENING row after the previous process exits.
# An exclusive bind checks the actual address without trusting that stale row.
$qrPortProbe = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 3008)
$qrPortProbe.Server.ExclusiveAddressUse = $true
try { $qrPortProbe.Start() }
catch { throw 'Port 3008 is occupied and is not a responsive Impulsa QR server' }
finally { $qrPortProbe.Stop() }
New-Item -ItemType Directory -Path $qrDirectory -Force | Out-Null
$qrRun = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ')
$qrAttemptPath = Join-Path $qrDirectory ("launch-attempt-" + $qrRun + '.json')
if (Test-Path -LiteralPath $qrAttemptPath) { throw 'This launch attempt already exists; inspect the recorded process instead of relaunching' }
$qrAttempt = [ordered]@{ revision=2; mode=$qrMode; browserProcess=$false; startedUtc=[DateTime]::UtcNow.ToString('o'); entry='src/qr-direct.ts' }
$qrAttemptJson = $qrAttempt | ConvertTo-Json -Compress
$qrStream = [System.IO.File]::Open($qrAttemptPath, [System.IO.FileMode]::CreateNew)
try { $qrBytes=[System.Text.Encoding]::UTF8.GetBytes($qrAttemptJson); $qrStream.Write($qrBytes,0,$qrBytes.Length) } finally { $qrStream.Dispose() }
$qrArguments = @('--import','tsx', ('"' + $qrEntry + '"'), ('"' + $qrDirectory + '"'))
if ($NativeProfileFile) { $qrArguments += ('"' + $NativeProfileFile + '"') }
if ($ActivateNative) { $qrArguments += '--activate-native' }
$qrProcess = Start-Process -FilePath $qrNode -ArgumentList $qrArguments -WorkingDirectory $qrRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $qrDirectory ("stdout-" + $qrRun + '.log')) -RedirectStandardError (Join-Path $qrDirectory ("stderr-" + $qrRun + '.log')) -PassThru
[ordered]@{ pid=$qrProcess.Id; revision=2; mode=$qrMode; browserProcess=$false; startedUtc=$qrAttempt.startedUtc; url='http://127.0.0.1:3008/api/bots/impulsa/qr?format=view' } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $qrDirectory ("launch-" + $qrRun + '.json')) -Encoding utf8
[ordered]@{ pid=$qrProcess.Id; mode=$qrMode; browserProcess=$false; url='http://127.0.0.1:3008/api/bots/impulsa/qr?format=view' } | ConvertTo-Json -Compress
