[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$CertificatePath,
    [Parameter(Mandatory=$true)][ValidatePattern('^[a-fA-F0-9]{64}$')][string]$ExpectedSha256,
    [switch]$RestartQz,
    [switch]$CheckOnly
)

function Test-CustomRoots([string[]]$Lines) {
    foreach ($line in $Lines) {
        if ($line -match '^\s*[#!]') { continue }
        # Escaped keys and continued properties are valid Java syntax. Unknown
        # encodings are escalated instead of silently replacing another root.
        if ($line.Replace('\', '') -match '(?i)authcert|trustedRoot' -or $line -match '(?i)\\u[0-9a-f]{4}|\\$') { return $true }
    }
    return $false
}

$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw 'Esta activacion requiere Windows.' }
$certificatePath = (Resolve-Path -LiteralPath $CertificatePath).Path
$pem = [IO.File]::ReadAllText($certificatePath)
$match = [regex]::Matches($pem, '(?s)-----BEGIN CERTIFICATE-----\s*(.*?)\s*-----END CERTIFICATE-----')
if ($match.Count -ne 1 -or $pem.Contains('PRIVATE KEY') -or $pem.Length -gt 131072) {
    throw 'Se requiere un certificado publico. Nunca copiar la clave privada a este equipo.'
}
$der = [Convert]::FromBase64String($match[0].Groups[1].Value)
$certificate = [Security.Cryptography.X509Certificates.X509Certificate2]::new($der)
$sha = [Security.Cryptography.SHA256]::Create()
try { $fingerprint = ([BitConverter]::ToString($sha.ComputeHash($der))).Replace('-', '').ToLowerInvariant() }
finally { $sha.Dispose() }
if ($fingerprint -ne $ExpectedSha256.ToLowerInvariant()) { throw 'La huella del certificado no coincide con la del POS.' }
if ($certificate.NotBefore.ToUniversalTime() -gt [DateTime]::UtcNow -or $certificate.NotAfter.ToUniversalTime() -le [DateTime]::UtcNow) {
    throw 'El certificado no esta vigente. Descargar una activacion nueva desde el POS.'
}
if ($certificate.GetNameInfo([Security.Cryptography.X509Certificates.X509NameType]::SimpleName, $false) -ne 'Escalar AI POS') {
    throw 'El certificado no identifica a Escalar AI POS.'
}
$publicKey = [Security.Cryptography.X509Certificates.RSACertificateExtensions]::GetRSAPublicKey($certificate)
if (-not $publicKey -or $publicKey.KeySize -lt 2048) { throw 'Se requiere una identidad RSA de al menos 2048 bits.' }
$publicKey.Dispose()
$qzDirectory = Join-Path $env:ProgramFiles 'QZ Tray'
$qzExe = Join-Path $qzDirectory 'qz-tray.exe'
$qzConsole = Join-Path $qzDirectory 'qz-tray-console.exe'
if (-not (Test-Path -LiteralPath $qzExe) -or -not (Test-Path -LiteralPath $qzConsole)) {
    throw 'Instalar QZ Tray 2.2 o superior desde https://qz.io/download/ antes de activar.'
}
$directory = Join-Path $env:LOCALAPPDATA 'EscalarAI/POS/qz-trust'
$target = Join-Path $directory "$fingerprint.crt"
$targetOption = '"-DtrustedRootCert=' + $target.Replace('\', '/') + '"'
$previousUser = [Environment]::GetEnvironmentVariable('QZ_OPTS', 'User')
$previousMachine = [Environment]::GetEnvironmentVariable('QZ_OPTS', 'Machine')
$previous = if ($null -ne $previousUser) { $previousUser } else { $previousMachine }
if ($previous -match '-D(?:authcert\.override|trustedRootCert)=') {
    if (-not $previous.Contains($targetOption)) {
        throw 'QZ ya tiene otra identidad configurada. No se reemplazo. El administrador debe agregar o renovar el certificado conservando las otras identidades.'
    }
    $next = $previous
} else {
    # Preserve any application-level roots instead of shadowing them with an environment override.
    $properties = Join-Path $qzDirectory 'qz-tray.properties'
    if (Test-Path -LiteralPath $properties) {
        $configured = Test-CustomRoots ([IO.File]::ReadAllLines($properties))
        if ($configured) { throw 'QZ ya tiene raices personalizadas en su configuracion. Se requiere activacion supervisada para conservarlas.' }
    }
    $next = (($previous + ' ' + $targetOption).Trim())
}
if ($RestartQz -and -not $CheckOnly) {
    $printers = @(Get-Printer -ErrorAction Stop)
    if ($printers | Where-Object { $_.JobCount -gt 0 }) { throw 'Hay impresiones en cola. Esperar a que terminen antes de activar.' }
}
if ($CheckOnly) {
    Write-Output 'Certificado, huella y configuracion verificados. No se modifico el equipo.'
    exit 0
}
New-Item -ItemType Directory -Path $directory -Force | Out-Null
if (-not (Test-Path -LiteralPath $target)) { [IO.File]::WriteAllText($target, $pem, [Text.Encoding]::ASCII) }
elseif ([IO.File]::ReadAllText($target) -ne $pem) { throw 'El archivo de confianza existente no coincide. No se reemplazo.' }
if ($previousUser -ne $next) {
    $backup = Join-Path $directory ('environment-before-' + [DateTime]::UtcNow.ToString('yyyyMMddHHmmssfff') + '.json')
    @{ QZ_OPTS = $previousUser } | ConvertTo-Json | Set-Content -LiteralPath $backup -Encoding UTF8
    [Environment]::SetEnvironmentVariable('QZ_OPTS', $next, 'User')
}
$env:QZ_OPTS = $next
# Notify Explorer so later starts use the updated, QZ-only environment setting.
Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class EscalarEnvironmentNotification {
    [DllImport("user32.dll", CharSet = CharSet.Auto, SetLastError = true)]
    public static extern IntPtr SendMessageTimeout(IntPtr window, uint message, UIntPtr wparam,
        string lparam, uint flags, uint timeout, out UIntPtr result);
}
'@
$result = [UIntPtr]::Zero
[EscalarEnvironmentNotification]::SendMessageTimeout([IntPtr]0xffff, 0x1a, [UIntPtr]::Zero, 'Environment', 2, 5000, [ref]$result) | Out-Null
$log = Join-Path $directory 'activation.log'
$authorization = Start-Process -FilePath $qzConsole -ArgumentList @('--allow', ('"' + $target + '"')) -PassThru -WindowStyle Hidden `
    -RedirectStandardOutput $log -RedirectStandardError (Join-Path $directory 'activation-error.log')
if (-not $authorization.WaitForExit(30000)) { throw "La autorizacion QZ sigue pendiente. Revisar $log antes de reintentar." }
$authorization.Refresh()
if ($null -ne $authorization.ExitCode -and $authorization.ExitCode -ne 0) { throw "QZ no confirmo la autorizacion. Revisar $log" }
# The native launcher may exit before its Java child; confirm the actual allow-list entry.
$sha1 = [Security.Cryptography.SHA1]::Create()
try { $qzFingerprint = ([BitConverter]::ToString($sha1.ComputeHash($der))).Replace('-', '').ToLowerInvariant() }
finally { $sha1.Dispose() }
$allowed = $false
$deadline = [DateTime]::UtcNow.AddSeconds(30)
do {
    foreach ($file in @((Join-Path $env:APPDATA 'qz/allowed.dat'), (Join-Path $env:ProgramData 'qz/allowed.dat'))) {
        if ((Test-Path -LiteralPath $file) -and ([IO.File]::ReadAllLines($file) | Where-Object { $_.StartsWith($qzFingerprint + "`t") -and $_.EndsWith("`ttrue") })) { $allowed = $true }
    }
    if (-not $allowed) { Start-Sleep -Milliseconds 250 }
} while (-not $allowed -and [DateTime]::UtcNow -lt $deadline)
if (-not $allowed) { throw "QZ no registro el certificado. Revisar $log" }
if ($RestartQz) { Start-Process -FilePath $qzExe -ArgumentList '--steal' -WindowStyle Hidden | Out-Null }
Write-Output 'Identidad de Escalar AI POS registrada para este usuario de Windows. No se autorizaron sitios anonimos.'
Write-Output 'Volver al POS y actualizar impresoras. Esto no es una comprobacion de salida en papel.'
if (-not $RestartQz) { Write-Output 'Cerrar y abrir QZ Tray antes de comprobar la conexion.' }
