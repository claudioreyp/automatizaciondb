# Pizza House: conexion de n8n y gateway QR

## Estado al 2026-09-19 (America/Lima)

Se actualizo el workflow existente **Agente Pizza House**, ID `HstQEpRLMqONt6v4`,
para el negocio 2 y sucursal 2. Se conservaron memoria, conversacion, credenciales
de OpenAI y credenciales previas del POS. No se modificaron otros workflows,
CLIENTES, DNS, suscripciones, pagos ni impresion.

| Componente | Version comprobada |
| --- | --- |
| API | `27128c983434f6b37d544409a451b5025c65b738` |
| Render | `dep-danjcebm8hqs73bhb9r0`, servicio existente `escalar-ai-pos-api` |
| Admins | `9b38677919494f8974aad6095f7be5e786a6e497` |
| Vercel Admins | `9kjBCCbR7V2FbsbFbSyg5kDWuDGF`, Ready |
| Workflow publicado | `9049c998-ad18-4ab0-bcf7-4c69d535d935`, activo |
| Workflow previo respaldado | `c9c53ba4-fba6-416f-b61e-8fa03ed1a18e` |

API y Admins se publicaron en ese orden, en sus repositorios/ramas existentes de
`claudioreyp`. Los commits de documentacion posteriores no requieren redesplegar.
No hubo migracion ni cambios de esquema en Supabase.

El usuario escaneo el QR. Se comprobo `connected`, `sessionReady=true` y
`messageBusReady=true` en el gateway, con salud `healthy`. No se afirma que esta
conexion por si sola pruebe todas las conversaciones, pedidos o entregas.

## Credenciales y aislamiento

- Credencial POS adicional ID 4: **Agente Pizza House - pruebas QR**. Scopes:
  `menu:read`, `inventory:read`, `orders:read`, `orders:write`, `payments:write`,
  `reservations:write`, `events:read`. No permite cambiar stock/disponibilidad.
- El secreto es reutilizable; se mostro una sola vez. Admins no lo recupera y no
  reemplaza otras credenciales. [Contrato de emision](additional-credentials.md).
- n8n guarda el token en una credencial privada **HTTP Header Auth**, asignada a
  las herramientas del POS de este workflow. No esta en codigo, prompts, variables
  globales, enlaces ni exportaciones. Base fija: `https://api.escalarai.tech/api/v1`.
- Un secreto de transporte distinto protege el webhook gateway -> n8n. Una llamada
  sin ese secreto recibio 403; la llamada autenticada respondio 200.
- La clave administrativa de n8n solo se usa para configurar. El agente no la
  necesita para responder y no debe recibirla. No se crea una por restaurante.
- Los archivos privados locales estan fuera de Git y con ACL limitada. El relay
  lee su token de configuracion privada. Nunca imprimir `.env`, secretos, payloads
  de ejecucion ni respaldos completos durante diagnosticos.
- Incidencia: un fragmento sensible del token adicional aparecio en una salida
  de diagnostico anterior, no en GitHub. Se informo al usuario y se solicito
  autorizacion para renovar solo la credencial adicional y actualizar ambos
  consumidores privados. No se roto automaticamente ni se toco la anterior.

## Datos vigentes de Yape

Cada mensaje nuevo admitido consulta `/integrations/context`. Numero, titular y
QR se toman del Perfil del agente de la sucursal; ubicacion de Datos de sucursal
y tarifas de Costos de envio. No se usan numeros, direcciones ni imagenes antiguas
como sustituto cuando faltan datos.

Guardar los datos en CLIENTES se detecta en la siguiente interaccion, sin volver
a configurar n8n. No dispara mensajes espontaneos, no cambia automaticamente el
metodo de pago ni salta las condiciones del checkout. Yape debe estar habilitado
para la modalidad y deben estar completos los datos necesarios para pagar.

El QR se obtiene mediante `GET /integrations/context/yape-qr` con Bearer. n8n
descarga bytes, valida PNG/JPEG/WEBP y limite de 8 MB, y responde al gateway con
una accion binaria. Storage permanece privado. QR ausente (404) y fallo temporal
tienen mensajes distintos; ninguno permite enviar una imagen vieja.

En la comprobacion publica inicial, el contexto y carta devolvieron 200 para 2/2
y habia dos productos. Numero, titular y QR aun no estaban configurados; la descarga
de QR devolvio 404. La consulta de prueba sobre Yape respondio honestamente que
faltaban datos. No se subio un QR ficticio al negocio real.

Delivery estaba **Por cotizar**, con importe `null`, no gratuito. El flujo no
registra ni pide pagar un delivery sin cotizacion confirmada. No se agrego un
calculador de tarifas variables. Para probar pedidos completos, usar recojo/local
o configurar una tarifa real autorizada desde el POS; no inventar un importe.

## Gateway y notificaciones

Abrir en este equipo: <http://127.0.0.1:3008/api/bots/impulsa/qr?format=view>.
El gateway conserva sesiones y solo escucha en loopback; no hay tunel ni acceso
publico. Tras un reinicio, desde la carpeta local `Impulsa` puede usarse `npm start`
si no existe otra instancia. No arrancar dos procesos sobre la misma sesion.
No se instalo inicio automatico. Mantener este equipo y el gateway encendidos.

Solo se procesan mensajes nuevos individuales, no grupos ni historial. El webhook
se autentica antes de procesar el mensaje y las referencias de pedidos antiguos
se comprueban contra sucursal, negocio y remitente antes de reutilizarlas.

**Payment Approval Monitor** queda deshabilitado solo en este workflow. El relay
local es el unico emisor de avisos de pago, listo, despacho, entrega y cancelacion.
Conserva fecha de activacion y entregas en el archivo de estado v2 dentro de
`sessions`; consulta `created_after` y `event_types`, sin consumir eventos previos.
No borrar el archivo de estado/lock ni restaurar una copia vieja sobre entregas
nuevas. Un lock de un proceso vivo impide abrir otro relay.

Se persiste `uncertain` antes de enviar, `sent` tras confirmacion de WhatsApp y
`acked` tras la API. Si falla el ACK se reintenta solo ese ACK; una entrega incierta
queda para revision sin reenvio automatico. Los avisos muestran folio, no ID interno.

## Verificacion y limites

- API: 742 pruebas Pytest aprobadas, 3 omitidas de PostgreSQL. Los ensayos PG no
  pudieron ejecutarse: usuario limitado sin CREATE SCHEMA y acceso de migracion
  indisponible. No se ampliaron permisos ni se uso la base real para suplirlos.
- Admins: lint, 30 pruebas Vitest y build aprobados; 2 pruebas Playwright de
  escritorio/movil, revision visual e Impeccable aprobados. Advertencia de bundle
  preexistente, sin nuevas dependencias.
- Gateway/workflow: 21 pruebas aprobadas de medios binarios, origen local, relay y
  flujos de pago; chequeo TypeScript/build y scripts de grupos, deduplicacion y
  aislamiento de pedido nuevo. La prueba de actualizacion de Yape reutiliza la
  misma conversacion: datos ausentes, agregados, cambiados y retirados.
- Contexto/carta reales, scope 2/2, webhook sin/con autenticacion y workflow activo
  verificados. La prueba sintetica de Yape no envio WhatsApp ni creo pedidos.
- Simulados: efectivo, Yape sin imagen, comprobante requerido, QR binario identico,
  metodo deshabilitado, pedido ajeno, importe pendiente, eventos historicos,
  ACK fallido y reinicio del relay. No equivalen a pruebas bancarias/fisicas.
- Pendiente: conversacion real desde otro telefono; pedido nuevo de prueba,
  comprobante real pendiente de revision, aprobacion humana y un unico aviso.
  Coordinar impresiones antes de crear pedidos: las reglas del POS siguen activas.
  No se despacharon trabajos de impresion ni eventos historicos durante los checks.
- Con el QR de Yape real aun ausente, la entrega final de su imagen por WhatsApp
  no se verifico fisicamente. El transporte binario se verifico con datos aislados.

El codigo del gateway es local (la carpeta padre no tiene un repositorio Git
utilizable); se conservaron respaldos privados antes de editar. No se creo otro
repositorio ni servicio. La API y Admins si quedaron publicados como arriba.

## Recuperacion segura

Si ocurre un fallo, detener primero el gateway sin borrar su sesion ni estado del
relay. Revisar resultado de la operacion antes de repetir una emision o escritura.
Restaurar el workflow anterior exige revision: apunta a configuracion antigua y
puede rehabilitar un segundo emisor de eventos. No hacer rollback automatico ni
reactivar el monitor mientras exista el relay. Nunca restaurar una base vieja
sobre pedidos nuevos ni revocar/rotar credenciales como intento ciego de solucion.
