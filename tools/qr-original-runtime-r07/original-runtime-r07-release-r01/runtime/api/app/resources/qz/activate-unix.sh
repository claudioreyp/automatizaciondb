#!/bin/sh
# Explicit, one-time trust registration. Only public material is installed.
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
expected="${1:-}"
case "$expected" in ''|*[!a-f0-9]*) echo 'Se requiere la huella SHA256 indicada en LEEME.'; exit 1 ;; esac
[ "${#expected}" -eq 64 ] || { echo 'Huella SHA256 incompleta.'; exit 1; }
cert="$(pwd)/digital-certificate.txt"
[ "$(grep -c '^-----BEGIN CERTIFICATE-----' "$cert")" -eq 1 ] || { echo 'Se requiere un certificado publico unico.'; exit 1; }
if grep -q 'PRIVATE KEY' "$cert"; then echo 'Nunca copiar claves privadas a este equipo.'; exit 1; fi
actual=$(openssl x509 -in "$cert" -outform DER | openssl dgst -sha256 | awk '{print $NF}')
[ "$actual" = "$expected" ] || { echo 'La huella no coincide. No se modifico QZ.'; exit 1; }
openssl x509 -in "$cert" -checkend 0 -noout >/dev/null || { echo 'Certificado vencido.'; exit 1; }
openssl verify -CAfile "$cert" "$cert" >/dev/null || { echo 'Certificado no valido.'; exit 1; }
case "$(uname -s)" in
  Darwin) directory='/Applications/QZ Tray.app/Contents/Resources'; qz='/Applications/QZ Tray.app/Contents/MacOS/QZ Tray' ;;
  Linux) directory='/opt/qz-tray'; qz='/opt/qz-tray/qz-tray' ;;
  *) echo 'Este script requiere macOS o Linux con QZ Tray instalado.'; exit 1 ;;
esac
[ -x "$qz" ] || { echo 'Instala primero QZ Tray desde https://qz.io/download/'; exit 1; }
case "${QZ_OPTS:-}" in *trustedRootCert*|*authcert.override*) echo 'Hay raices QZ personalizadas. Se requiere activacion supervisada para conservarlas.'; exit 1 ;; esac
if [ -f "$directory/qz-tray.properties" ] && grep -Ev '^[[:space:]]*[#!]' "$directory/qz-tray.properties" | sed 's/\\//g' | grep -Eiq 'authcert|trustedRoot|u[0-9a-f]{4}'; then
  echo 'QZ tiene otras raices configuradas. Se requiere activacion supervisada.'; exit 1
fi
if [ -f "$directory/qz-tray.properties" ] && grep -Ev '^[[:space:]]*[#!]' "$directory/qz-tray.properties" | grep -Eq '\\$'; then
  echo 'QZ contiene propiedades continuadas. Se requiere activacion supervisada.'; exit 1
fi
if [ "$(uname -s)" = Darwin ] && defaults read io.qz.qz-tray QZ_OPTS 2>/dev/null | grep -Eq 'trustedRootCert|authcert\.override'; then
  echo 'QZ tiene otras raices configuradas. Se requiere activacion supervisada.'; exit 1
fi
target="$directory/override.crt"
if [ -e "$target" ]; then
  saved=$(openssl x509 -in "$target" -outform DER | openssl dgst -sha256 | awk '{print $NF}')
  [ "$saved" = "$expected" ] || { echo 'QZ ya tiene otro certificado propio. El administrador debe agregar la nueva identidad sin reemplazar la anterior.'; exit 1; }
else
  echo 'Se registrara solo el certificado publico de Escalar AI POS en QZ Tray.'
  echo 'No se autorizaran sitios anonimos. Se solicitara la clave de administrador.'
  sudo install -m 644 "$cert" "$target"
fi
"$qz" --allow "$cert"
echo 'Cierra y abre QZ Tray cuando no haya impresiones en curso. Despues actualiza impresoras en el POS.'
echo 'La activacion no es una comprobacion de salida en papel.'
