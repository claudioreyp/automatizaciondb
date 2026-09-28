# Recuperacion del boton de desconexion del gateway QR

Fecha: 2026-09-27. Alcance: gateway local Impulsa, puerto 3008, Pizza House 2/2.

## Causa y cambio

La pagina incluia `actionInFlight` en el estado ocupado y despues exigia que
dejara de estar ocupado para retirarlo. La condicion era imposible. La API ya
habia terminado la operacion, pero el boton permanecia deshabilitado.

`src/api.ts` libera ese estado en `finally`, conserva el bloqueo durante una
operacion real del servidor y limita las solicitudes POST a 12 segundos y las
lecturas GET a 8 segundos, incluido el cuerpo JSON. Un resultado incierto muestra
un aviso y reconcilia mediante GET; nunca repite automaticamente el POST.

Las lecturas no se superponen. Se descartan respuestas anteriores al clic y
generaciones antiguas; una identidad publica por instancia permite reconocer un
reinicio del gateway aunque su contador vuelva a cero.

La compilacion modifica solamente `dist/api.js` entre los 13 archivos existentes.
No cambia los modulos de mensajes, provider, filtros de backlog, n8n ni relay.

## Verificacion

- 28/28 pruebas: 11 de la pagina QR, 6 de recuperacion de sesion y 11 del relay.
- TypeScript (`npm run check`) y compilacion (`npm run build`) correctos.
- Regresiones: respuesta exitosa, QR y vinculacion, doble clic, timeout sin
  repetir POST, HTTP 400/503, GET y cuerpo JSON bloqueados, respuesta tardia y
  reinicio con una instancia nueva. El relay conserva ACK y entregas inciertas.
- Respaldo anterior del fuente, compilado y manifest de 13 archivos en el
  directorio privado y excluido de Git `Apis/backups`.

Huella SHA256 del fuente:
`340bdb459d899992ec872b25ba4bf0e88b416a2b7d0210512d07f3805ea30c5c`.
Huella del compilado:
`f773ef2a7a129c95b6e5700078cf939ad3bcadde684beb2d7c91e6817ca6b0f2`.

## Activacion

Correccion activa el 27 de septiembre a las 20:13, America/Lima. El reinicio se
hizo tras comprobar 25 minutos sin actividad, el ultimo turno terminado y cero
ejecuciones activas o en espera del workflow de Pizza House. El contador de cola
anterior era una metrica desactualizada: se registra al encolar y no al vaciar;
no se presento ese contador como evidencia de cola vacia.

El navegador del provider termino junto con el proceso anterior. Se comprobo
que ambos habian salido y que el puerto y el perfil estaban libres antes de
iniciar una sola instancia nueva. Se conservaron y respaldaron los archivos de
sesion; WhatsApp solicito nueva vinculacion y el usuario la confirmo. No se
borro la sesion ni se restauro un ledger antiguo.

Lectura posterior: API saludable, un tenant, WhatsApp conectado, alcance POS
2/2, HTML corregido y lock del relay perteneciente al proceso actual. El ledger
conserva exactamente la misma huella; `.env` y los otros 12 compilados no cambian.
La pestaña existente muestra `connected` y el boton de desconexion habilitado.
La evidencia privada de activacion conserva estos resultados y el respaldo del
perfil, sin exponer credenciales en este documento.

La logica de POST se verifica mediante las 11 pruebas aisladas de la pagina. No
se inicia otra desvinculacion real despues de enlazar el telefono para probarla.
