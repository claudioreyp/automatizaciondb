# R07: preparación y corte único de la API sintética

Estas fuentes no se han ejecutado. Adaptan el despliegue R06 revisado; no cambian n8n, modelos, presupuestos ni credenciales. El runtime R07 conserva los 70 blobs API70, los quince contratos originales y la imagen base local exacta `93dc8439…`.

El materializador separado publica sólo los 106 archivos del manifiesto R07, su manifiesto y los resultados locales, más estas fuentes de operación. No publica bases, sesiones, snapshots, diarios, bindings, claves o payloads de credenciales. El archivo `archive-inputs.json` contiene las cuatro rutas absolutas del workspace y sus hashes/bytes. Los archivos completos ya archivados permanecen allí; al VPS sólo se transfiere el recibo de metadatos generado por ROOT tras comprobarlos. No se duplican payloads en el VPS ni en el paquete público.

Antes del corte se conserva el caso R12 completo: snapshot posterior a la recuperación de su ACK, diario completo, recibo de recuperación y fila financiera. El recibo original FAIL se conserva. El bloque de modelos puede seguir OPEN: no se exige cerrar ese bloque ni ejecutar un CAS. ROOT mantiene los catorce flujos apagados y prepara el gate privado con las referencias comprobadas. No se registra aceptación comercial por este archivo de metadatos.

Orden futuro en el VPS, después de revisar estas fuentes y publicar sus bytes exactos:

1. Extraer el paquete nuevo bajo `/opt/escalar-qr/original-runtime-r07-release-r01`, con directorios raíz propiedad de root y fuentes 0644, sin sobreescribir una entrega existente.
2. ROOT verifica los cuatro archivos locales completos y el estado reciente catorceOFF/head48. Guardar sólo `gate_receipt` de `archive-inputs.json` en `/opt/escalar-qr/private/original-r07-r12-case-archive-receipt.json` (root0600/padre0700), con `archive_location=user_workspace`. Ningún payload del caso se transmite al VPS.
3. Ejecutar `python deploy/prepare.py --closure-gate RUTA_PRIVADA --closure-sha256 SHA`. Sólo inspecciona y archiva en el VPS su configuración/binding anteriores y el recibo de referencias locales; devuelve el SHA del plan privado.
4. Ejecutar una sola vez `python deploy/apply.py --plan /opt/escalar-qr/private/original-r07-deploy-r01/plan.json --plan-sha256 SHA --execute-reviewed-r07`. Construye sin Internet (`--pull=false --network=none`) contra la base local exacta y comprueba API70 en la imagen. Detiene y conserva R05, arranca backend serial R07 en la red interna y verifica binding UID/GID10001, archivo0600 y padre0700 antes de arrancar el proxy. Mantiene límites, redes, montajes y seguridad anteriores.
5. Preparar la renovación sintética y el controlador según el README del stager. Este despliegue no los activa.

La reversión conserva configuración y contenedores; **no recupera memoria SQLite ni reinicia el caso R12**. `reverse.py` exige archivar el caso sintético R07 antes de apartarlo y sólo restaura la configuración de R05 detenida. En un fallo de aplicación se intenta esa misma reversión una vez, sin repetir construcción ni corte. Los IDs exactos creados y las etiquetas R07 delimitan las operaciones sobre contenedores propios. No se borran imágenes ni evidencias.

El instalador reutiliza el lock exclusivo del despliegue R06 para impedir ejecuciones paralelas de las dos propuestas. No instala paquetes ni accede a credenciales productivas. El visor de localhost permanece en el paquete local separado; no entra en Docker.
