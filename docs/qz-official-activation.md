# Transicion a firma oficial QZ

## Estado del 2026-09-20

Codigo preparado para certificados oficiales; compra, CSR real, emision y
activacion pendientes. No se ha pagado a QZ ni cambiado la identidad de Render,
las autorizaciones de estaciones, impresoras, pedidos o colas.

La identidad propia anterior esta documentada como expuesta. Su huella PUBLICA
SHA-256 local es `2dd45a5d0752beb23a02bad1b076ecfe954e7799f7a948237250ec82ae67a64d`,
con vencimiento `2028-09-12T03:52:04Z`. La vigencia no remedia la exposicion.
Confirmar la huella en el servidor y cada terminal antes de retirar nada.
No reutilizar su clave, distribuirla a nuevos equipos ni restaurarla como rollback.

El nuevo certificado oficial aun no tiene huella ni fecha de vencimiento.
La raiz publica incluida en `app/resources/qz` NO es la licencia de Escalar AI
ni permite emitir un certificado comercial. Se usa para validar la cadena
publica contra la autoridad integrada en QZ 2.3.0. QZ conserva el control final
de revocacion, confianza local y autorizacion nativa.

## Compra y nueva identidad

1. Confirmar con el usuario el importe final de Premium Support antes de pagar.
   La referencia consultada es USD 749, con renovacion anual reducida; no es una
   autorizacion de cobro ni una garantia de precio final. Revisar impuestos,
   condiciones y datos de la organizacion en el portal oficial.
2. Generar una clave NUEVA y su CSR exclusivamente en un entorno privado del
   servidor de API. `scripts/create_qz_csr.py <directorio-privado>` requiere
   `--organization "Escalar AI POS"`; CN predeterminado `pos.escalarai.tech`.
   Confirmar identidad legal con QZ antes de emitir. RSA 2048/SHA256 para el CSR;
   firma de mensajes permanece RSA/SHA512. Solo el CSR publico se entrega a QZ.
3. El generador rechaza el workspace y directorios existentes, restringe permisos
   antes de escribir y no instala confianza. Las claves creadas en pruebas son
   fixtures desechables, nunca la identidad que debe desplegarse.
4. Render tiene disco efimero: antes de reiniciar, persistir la nueva clave en su
   configuracion secreta del servidor por un canal privado. No imprimirla en la
   consola, sesiones compartidas, respuestas, logs o chat; no confiar en un archivo
   efimero como unica copia. No generar la clave en un navegador o Vercel.
5. Obtener el certificado emitido por QZ con sus intermedios. Conservar el PEM
   publico y registrar huella, identidad, emisor y vencimiento despues de validar.
   Nunca sustituirlo por un certificado demo/autofirmado o una raiz TLS localhost.

Referencias: [emision](https://qz.io/docs/generate-certificate),
[firma](https://qz.io/docs/signing), [precios](https://qz.io/),
[licencia](https://qz.io/docs/faq), [renovacion](https://qz.io/docs/renewal).

## Contrato y despliegue compatible

- Los GET de conexion de sucursal/pedido conservan `mode` y `certificate`. Agregan
  `identity`: subject, issuer, fingerprint_sha256, valid_to, expires_soon, trust y
  activation. Solo material PUBLICO; respuestas no-store. `valid_to` toma el
  vencimiento mas proximo de la cadena. La UI avisa durante los ultimos 30 dias.
- La API admite la cadena PEM y el separador nativo de QZ
  `--START INTERMEDIATE CERT--`; valida firmas, CA, usos de clave, correspondencia
  RSA y vigencia de cada eslabon. Devuelve la cadena publica normalizada.
- `QZ_TRAY_TRUST_MODE=compatible` mantiene transitoriamente la configuracion
  existente durante el despliegue del codigo. No sustituye su rotacion pendiente.
  Una vez lista la pareja oficial, usar `QZ_TRAY_TRUST_MODE=official`; rechaza
  certificados propios incluso si imitan el nombre del emisor de QZ.
- Fuera de development/dev/test la firma es obligatoria, sin importar el valor
  de `QZ_REQUIRE_SIGNING`. Este ultimo permite exigirla tambien en desarrollo.
  El build de CLIENTES rechaza `manual-approval` heredado en produccion.
- El POST de firma valida el JSON exacto contra su SHA256 y firma RSA/SHA512.
  Todos los roles, incluidos owner/manager/superadmin, tienen la misma lista
  limitada de operaciones. Nunca firma hashes opacos, USB, archivos, sockets,
  destinos host/port/file ni comandos arbitrarios. Los permisos por sucursal
  permanecen. JSON antiguo permitido; hash sin preimagen ya no esta permitido.
- CLIENTES reutiliza el socket mientras la identidad no cambie. Cada intento
  de conexion admite un reintento de transporte, sin reclamar ni enviar papel.
  La deteccion existente tiene hasta seis rondas espaciadas para abrir QZ.
  Denegaciones y errores de firma/certificado no activan esas rondas. El envio
  incierto nunca se repite; si QZ acepto y fallo el ACK, se reintenta solo el ACK.

Publicar API compatible y comprobar salud/contratos, despues CLIENTES. No requiere
DDL, migracion, cambios de Admins/n8n/gateway ni nuevas dependencias. Rotar la pareja
de secretos solo en una ventana coordinada sin impresiones en curso. Validar la
pareja completa antes de activarla; nunca mezclar certificado nuevo y clave vieja.
Ante fallo, bloquear firma y corregir hacia adelante, no volver a la clave expuesta.

## Retirar confianza antigua y autorizar la oficial

1. Cerrar conexiones del POS y comprobar que no hay trabajos en vuelo. Respaldar
   configuracion QZ del usuario/sistema, sus listas de sitios y valor de QZ_OPTS.
   El respaldo de confianza no debe contener la clave privada de la API.
2. Identificar SOLO la entrada/certificado de Escalar AI por la huella anterior.
   En Site Manager retirar esa autorizacion, conservando sitios ajenos. No borrar
   todas las listas ni autorizar peticiones anonimas. El almacen de QZ puede usar
   otra representacion de huella: calcularla del mismo certificado publico y
   comparar identidad antes de retirar, no buscar por nombre solamente.
3. Windows: la activacion anterior podia usar QZ_OPTS de usuario con
   `-DtrustedRootCert=...` y un archivo en `%LOCALAPPDATA%/EscalarAI/POS/qz-trust`.
   Retirar solo esa opcion/ruta cuando su certificado coincida, conservando otras
   opciones y raices. No restaurar ciegamente un backup antiguo de toda la variable.
   No cambiar la variable de sistema si no contiene esa identidad exacta.
4. macOS/Linux: inspeccionar el archivo override.crt/configuracion usado por esa
   instalacion. No eliminar/reemplazar si contiene una raiz distinta o material
   compartido; requiere revision del administrador. No hacer una limpieza global.
5. Reiniciar QZ cuando este libre, abrir `pos.escalarai.tech`, comprobar la identidad
   y reconocimiento oficial EN QZ; marcar Remember this decision + Allow una vez.
   El POS no concede esa autorizacion ni muestra una verificacion inventada.

No se ha ejecutado esta retirada ni autorizado el certificado nuevo: aun no existe.
No es posible garantizar cero avisos despues de reinstalaciones, revocaciones,
renovaciones o cambios de permisos del navegador/usuario del sistema operativo.

## Evidencia y limites

- Pytest: 776 aprobadas, cinco casos PostgreSQL omitidos en esta ejecucion local;
  firma/criptografia y contratos usan datos aislados. No se cambio la base de uso.
- Vitest, lint/build y Playwright de escritorio/tablet/movil: resultados definitivos
  y versiones publicadas se registran al terminar en deployment-runbook.md.
- QZ nativo/impresora se simulan en las pruebas de navegador: no equivalen a la
  validacion oficial ni a papel impreso. No se despacharon trabajos historicos.
- Pendiente tras emision: consultar impresoras repetidamente, cambiar empleado,
  reiniciar navegador/PWA/QZ y comprobar una estacion limpia y una antigua.
- Pendiente: prueba corta y luego ticket/comanda identificados, confirmacion fisica
  de texto, importes, longitud/corte y ausencia de avisos, sin pedidos/cobros reales.
  macOS/Linux requieren sus propios equipos; no extrapolar evidencia de Windows.
