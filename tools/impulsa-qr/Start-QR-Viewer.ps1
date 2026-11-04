param([Parameter(Mandatory=$true)][string]$ProfileFile, [string]$NodeExecutable)
$ErrorActionPreference = 'Stop'
$qrRoot = $PSScriptRoot
$qrNode = if ($NodeExecutable) { $NodeExecutable } else { (Get-Command node -ErrorAction Stop).Source }
$qrViewerEntry = Join-Path $qrRoot 'dist\qr-viewer.mjs'
if (-not [IO.Path]::IsPathRooted($ProfileFile) -or $ProfileFile.Contains('"') -or -not (Test-Path -LiteralPath $ProfileFile -PathType Leaf)) { throw 'An existing absolute private viewer profile is required' }
if (-not (Test-Path -LiteralPath $qrViewerEntry -PathType Leaf)) { throw 'Build the QR viewer before starting it' }
$qrHealth = $null
try { $qrHealth = Invoke-RestMethod -Uri 'http://127.0.0.1:3008/api/health' -TimeoutSec 2 } catch {}
if ($qrHealth) {
  if ($qrHealth.viewerMode -eq 'remote_qr_viewer' -and $qrHealth.localProvider -eq $false) {
    [ordered]@{ mode='remote_qr_viewer'; alreadyRunning=$true; whatsappProcess=$false; url='http://127.0.0.1:3008/api/bots/impulsa/qr?format=view' } | ConvertTo-Json -Compress
    exit 0
  }
  throw 'Port 3008 belongs to a different service; inspect it before starting the viewer'
}
$qrProbe = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 3008)
$qrProbe.Server.ExclusiveAddressUse = $true
try { $qrProbe.Start() } finally { $qrProbe.Stop() }
$qrArguments = @(('"' + $qrViewerEntry + '"'), ('"' + $ProfileFile + '"'))
$qrProcess = Start-Process -FilePath $qrNode -ArgumentList $qrArguments -WorkingDirectory $qrRoot -WindowStyle Hidden -PassThru
[ordered]@{ pid=$qrProcess.Id; mode='remote_qr_viewer'; whatsappProcess=$false; url='http://127.0.0.1:3008/api/bots/impulsa/qr?format=view' } | ConvertTo-Json -Compress
