"""Downloadable, public-only instructions for explicit per-computer QZ activation."""

from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from fastapi import HTTPException

from .qz_signing import qz_certificate


def activation_bundle() -> bytes:
    pem = qz_certificate()
    certificate = x509.load_pem_x509_certificate(pem.encode())
    public_pem = certificate.public_bytes(serialization.Encoding.PEM).decode()
    name = certificate.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
    if (not name or name[0].value != "Escalar AI POS" or certificate.issuer != certificate.subject
            or pem.strip() != public_pem.strip()):
        raise HTTPException(409, "Este paquete es para el certificado propio de Escalar AI POS. Consulta la activacion del certificado comercial con tu administrador.")
    fingerprint = certificate.fingerprint(hashes.SHA256()).hex()
    resources = Path(__file__).parent / "resources" / "qz"
    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as bundle:
        bundle.writestr("digital-certificate.txt", public_pem)
        bundle.writestr("activar-windows.ps1", (resources / "activate-windows.ps1").read_bytes())
        bundle.writestr("activar-macos-linux.sh", (resources / "activate-unix.sh").read_text(encoding="utf-8"))
        bundle.writestr("ACTIVAR-WINDOWS.cmd", (
            "@echo off\r\n"
            "echo Activacion unica de Escalar AI POS para este usuario de Windows.\r\n"
            "echo Solo se confiara en el POS, no en sitios anonimos. QZ se reiniciara.\r\n"
            "echo No continuar si hay impresiones en curso. Cerrar esta ventana para cancelar.\r\n"
            "pause\r\n"
            "powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"%~dp0activar-windows.ps1\" "
            f"-CertificatePath \"%~dp0digital-certificate.txt\" -ExpectedSha256 {fingerprint} -RestartQz\r\n"
            "if errorlevel 1 echo La activacion no termino. Revisa el mensaje anterior.\r\n"
            "pause\r\n"
        ))
        bundle.writestr("LEEME.txt", (
            "ESCALAR AI POS - ACTIVACION UNICA DE QZ TRAY\n\n"
            "Extrae TODOS los archivos de este ZIP antes de ejecutar la activacion.\n"
            "Descarga este paquete solo desde tu POS de confianza. No contiene claves privadas.\n"
            f"Huella SHA256 del certificado: {fingerprint}\n"
            f"Valido hasta (UTC): {certificate.not_valid_after_utc.isoformat()}\n\n"
            "WINDOWS: ejecuta ACTIVAR-WINDOWS.cmd y lee la confirmacion antes de continuar.\n"
            "Configura QZ_OPTS solo para tu usuario y conserva respaldo local del valor anterior.\n"
            "El script usa una excepcion de ejecucion solo para ese proceso; no cambia la politica global.\n"
            "MACOS/LINUX: requiere openssl y QZ Tray. Desde esta carpeta ejecuta:\n"
            f"sh activar-macos-linux.sh {fingerprint}\n"
            "Se solicita permiso de administrador para registrar el certificado publico en QZ.\n"
            "Si ya existe otro certificado propio, no se reemplaza; pide activacion supervisada.\n\n"
            "DESPUES: abre o actualiza el POS, entra a Configuracion > Impresion y actualiza impresoras.\n"
            "Comprueba dos consultas y una prueba corta antes de operar. Una descarga no confirma activacion.\n"
            "Repite una sola vez en cada equipo/usuario que imprime. No se requiere por pedido.\n"
            "Instalar la PWA no puede modificar por si sola la confianza del sistema o de QZ.\n"
            "QZ Tray funciona en equipos Windows, macOS y Linux, no se instala en iPhone o Android.\n"
            "Necesitas la misma API del POS accesible por HTTPS en otros equipos; no copies credenciales\n"
            "de desarrollo ni la clave privada del servidor. El permiso de red local del navegador es independiente.\n"
            "La renovacion de la identidad requiere una nueva activacion supervisada.\n"
        ))
    return output.getvalue()
