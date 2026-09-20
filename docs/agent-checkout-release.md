# Checkout ampliado del agente

## Contratos compatibles

Todo acceso del agente conserva Bearer limitado al negocio/sucursal. No se
incluyen credenciales en enlaces. Las nuevas escrituras requieren version esperada,
remitente e Idempotency-Key; una respuesta perdida reutiliza la misma operacion.

| Ruta (bajo /api/v1) | Metodo | Finalidad |
| --- | --- | --- |
| /integrations/context/catalog | GET | Catalogo apto para consumidor, sin recetas/costos internos |
| /integrations/orders/preview | POST | Validar seleccion y promociones sin crear pedido |
| /integrations/orders/{id}/customer-state?sender=... | GET | Pedido propio, saldo, solicitudes y acciones permitidas |
| /integrations/orders/{id}/item-batches | POST | Adicion independiente; Yape espera revision humana |
| /integrations/orders/{id}/item-revisions | POST | Productos no pagados ni preparados |
| /orders/{id}/delivery-fee | PATCH | Encargado confirma importe y forma de cobro opcional |
| /orders/{id}/delivery-payment | PATCH | Encargado elige forma de cobro pendiente |
| /integrations/orders/{id}/delivery-payment | PATCH | Consumidor elige efectivo/Yape de envio |

Comprobantes usan la ruta previa payment-evidence, con payment_request_id cuando
corresponden a adicion/envio. El comprobante inicial no cambia de identidad. Los
estados finales no se repiten; los resultados idempotentes no crean otro pago.
CLIENTES muestra todo el historial y revisiones independientes. Se aprueba primero
el inicial. Un extra rechazado admite otro comprobante sin cancelar el original.

Solo WhatsApp y modalidad quote permiten envio pendiente. El preview devuelve
final_total=null y delivery_fee=null; total/known_total conservan el importe conocido.
Los comprobantes impresos dicen envio pendiente, sin reimpresion automatica. No
despachar hasta costo/metodo definidos y, para Yape de envio, comprobante aprobado.
Tarifas variables requieren cotizacion real y nunca se convierten en cero.

El agente descarga todas las cartas privadas en orden; primera imagen con texto
adjunto. Consulta QR, titular y numero vigentes. No publica Storage ni cambia
metodos habilitados. Conserva memoria, pero revalida estado/precio/disponibilidad.
Solo payment.approved con cocina y order.ready generan avisos; estado de entrega
se responde a consulta, no mediante aviso automatico.

## Migracion y evidencia

- Respaldo privado: checkout-release-20260920T173043Z, 52 tablas/947 filas,
  configuracion y archivos locales. No se publica en Git.
- Alembic 20260920_0024: nueva solicitud durable y relacion nullable con evidencia.
  Ensayo aislado de migracion y carrera de confirmacion de tarifa: 2 pruebas PG.
  Suites anteriores de concurrencia/folios/Auth: 16 pruebas con PostgreSQL.
- Aplicada en el proyecto EscalarAI existente; 51 tablas de datos anteriores
  comparadas sin diferencias. RLS activo, anon/authenticated sin SELECT directo.
  Rol temporal de ensayo eliminado. Sin reescribir pagos historicos.
- API: 754 pruebas Pytest aprobadas y 5 omitidas en la corrida general. Las
  comprobaciones PostgreSQL se ejecutaron por separado: 18 aprobadas en total.
- CLIENTES: lint/build, 763 pruebas Vitest, 66 Playwright de Pedidos entre
  escritorio/tablet/movil. Revision visual agrupada y detector Impeccable sin
  hallazgos. Advertencia de bundle preexistente permanece.
- Gateway y parche del workflow: 35 pruebas simuladas aprobadas. Ninguna envio
  mensajes a consumidores, cobros bancarios ni trabajos reales de impresion.
- Supabase advisory mantiene aviso previo de proteccion contra contrasenas
  filtradas deshabilitada. No se altero Auth en esta tarea.

## Activacion

Publicar API compatible primero, CLIENTES despues y finalmente workflow/gateway.
El script local de preparacion respalda el workflow publicado y rechaza version
distinta o segundo intento incierto. No reemplazar staticData, memoria ni claves.
Conservar el estado durable del relay y su fecha de activacion al reiniciar.

Publicacion comprobada el 2026-09-20:

| Componente | Version |
| --- | --- |
| API / GitHub | `e0574daa261a56994275dfc77180d5605002bd5c` |
| Render, servicio existente | `dep-dao1lquk1f9s73abgk5g`, Live |
| CLIENTES / GitHub | `211ac25be8c84855f84861066f21ecca41f0eb46` |
| Vercel CLIENTES | `2xt2LcXa7tMx4XCfvfBqBusNua2o`, Ready en pos.escalarai.tech |
| Agente Pizza House | `7ea5eabe-637d-409e-b517-d707e1656a0f`, publicado y activo |
| Supabase / Alembic | `20260920_0024` |

Salud, catalogo y preview autenticos respondieron 200, con alcance 2/2; acceso
anonimo al catalogo fue rechazado. Pedidos y Descargar aplicacion responden en el
dominio del POS. El webhook sin secreto rechazo 403; consultas tecnicas de carta,
metodos y estado respondieron 200. La carta produjo la accion pos_menu y la
descarga privada vigente produjo una imagen con el texto adjunto esperado.
Numero, titular y QR reales estaban configurados; QR privado JPEG disponible.
No se crearon pedidos ni se enviaron WhatsApp durante esas consultas sinteticas.

Gateway levantado solo en 127.0.0.1:3008. Tras escaneo del usuario se verificaron
connected, sessionReady y messageBusReady. Se conservaron credenciales y estado
durable del relay, incluida activacion 2026-09-20T01:46:39.933Z. Antes del arranque
no habia eventos elegibles pendientes; no se reconocieron eventos historicos.
Payment Approval Monitor permanece deshabilitado en este unico workflow.

Pendiente de validacion fisica: carta y presencia de escritura desde otro telefono,
checkout completo con efectivo/Yape, extras y avisos tras revision/cocina. No
confundir pruebas simuladas con entrega fisica. Estas pruebas requieren mensajes
nuevos identificados y coordinar antes las impresiones automaticas. No realizar
pagos bancarios. Mantener este equipo y el gateway encendidos.

Rollback: volver a codigo compatible, sin revertir la migracion aditiva ni
restaurar una base antigua sobre nuevas escrituras. Detener gateway ante resultado
incierto, sin borrar sesion ni registro de entregas.

## Correccion de consulta de productos (2026-09-20)

Version posterior publicada del mismo workflow:
`35707f89-c22c-41f6-bf8d-7ab951599126`. El envio de carta ya no depende solamente
del intent consult_menu: exige solicitud actual o aceptacion de una oferta
reciente del mismo chat. Las consultas de productos conservan customer_reply,
incluido producto ausente/agotado y alternativas reales. Una peticion mixta puede
recibir imagen y respuesta, sin descartar la consulta.

Solo cambiaron Build POS Agent Context, Restaurant Agent y Build Standard Actions.
Se conservaron conexiones, memoria, credenciales, escrituras y monitor desactivado;
no fue necesario modificar ni redesplegar API, CLIENTES o el gateway.
Transformador local: scripts/pizza-house-menu-intent-patch.mjs en Impulsa.
Respaldo privado del workflow anterior y recibo de version conservados en backups.

41 pruebas locales y chequeo TypeScript aprobados. La prueba autenticada sobre el
webhook publicado uso el mismo chat tecnico para carta -> Dame una hamburguesa ->
Tienen hamburguesas? -> carta otra vez: imagen, texto, texto, imagen. Las respuestas
indicaron ausencia de hamburguesas y alternativas reales del catalogo. No creo
pedidos ni envio mensajes a consumidores. Gateway conectado al terminar.

## Correccion de recepcion Yape (2026-09-20)

Version final publicada y comparada con el artefacto preparado:
`66c45f97-e09e-4891-9a3d-077c7c2d41e9`, mismo workflow y credenciales.
Respaldos privados anteriores y recibos de actualizacion en `backups`, excluidos
de Git. No se reemplazo staticData ni se borraron memoria, sesiones o entregas.

Causas reproducidas con los cuerpos reales de los nodos anteriores:

- Prepare Payment Pricing devolvia solo JSON, eliminando el binario antes de
  Payment Evidence Has Image. Vision nunca recibia la captura.
- Build Order Validation Actions devolvia el pedido a awaiting_confirmation.
  La siguiente captura ya no encontraba la compra esperando evidencia Yape.
- El ejemplo JSON del agente omitia variant_name aunque una instruccion posterior
  lo exigia. La prueba real detecto Personal en la respuesta, pero no en el carrito;
  la API rechazo correctamente cotizar una seleccion incompleta.

Correccion: conservar bytes, clasificar por separado imagen ajena/fallo de vision,
aceptar formatos estructurados o textuales, retener el pedido y metodo confirmado,
y reparar solo el estado heredado del fallo. El contrato ahora incluye variante en
items y adiciones. Los importes, titular y fecha extraidos no aprueban un pago ni
descalifican por si solos el aspecto de comprobante. El encargado mantiene la
decision. Se retiran frases como captura real y se conservan solo folios que la
API scoped confirmo, no IDs ni numeros propuestos por el modelo.

Verificacion:

- 52 pruebas gateway/workflow, incluyendo reproduccion del fallo anterior,
  persistencia entre mensajes, imagen ajena -> captura, fallo de vision -> captura,
  formatos OpenAI, datos de pago distintos, texto sin archivo, reparacion limitada,
  efectivo y solicitudes independientes. TypeScript sin errores.
- 29 Pytest aislados de checkout/integraciones. Regresion adicional: monto/titular
  distintos se conservan en revision humana; repetir la misma peticion devuelve el
  mismo resultado. Un pedido, un comprobante, cero pagos/comandas/impresiones/eventos.
- Prueba real autorizada mediante el webhook autenticado publicado, sin usar un
  telefono de consumidor: compra -> confirmacion -> Yape -> imagen de tienda ->
  solicitud de captura -> declaracion sin imagen -> comprobante de simulacion.
  Vision reconocio la imagen ajena y luego el comprobante. El mismo carrito quedo
  registrado como folio #27, PRUEBA TECNICA - NO PREPARAR, S/30, evidence_received,
  comprobante under_review con codigo 085, paid_amount=0, tickets=[], sin envio a
  cocina. No hubo transferencias bancarias ni aprobacion humana. Se conservo esa
  prueba identificada; no aprobarla ni prepararla como pedido real.
- Consulta posterior del mismo chat devolvio pedido #27, Pizza Peperoni Personal,
  recojo y comprobante en revision. No creo otra compra. La primera seleccion
  incompleta de variante fallo antes de escribir y fue corregida en la misma compra.
- Gateway connected/sessionReady/messageBusReady; unico relay y monitor duplicado
  deshabilitado. No se consumieron eventos historicos ni se reinicio WhatsApp.

Transformadores locales en Impulsa/scripts: pizza-house-yape-evidence-code.mjs,
pizza-house-yape-evidence-patch.mjs y pizza-house-yape-contract-patch.mjs. El
generador principal de checkout incorpora estas correcciones. El publicador usa
version esperada, respaldo y registro de intento para reconciliar respuestas
perdidas; nunca publica secretos ni reescribe memoria. Las revisiones intermedias
d4dd7591-f503-4453-8702-2fa4f6fbde90 y 99702fd8-741f-4792-92d0-a30d7f1983f1
quedan sustituidas por la version final.

Limites: prueba real de webhook/API y modelo, no entrega fisica desde otro telefono.
Autenticidad bancaria sigue siendo una comprobacion humana. No se cambiaron
runtime API, esquema, CLIENTES, Admins, otros workflows ni credenciales. No hizo
falta desplegar Render/Vercel. Los fallos de vision o proveedor todavia pueden
pedir reenviar, pero no borran la eleccion Yape ni confirman un registro inexistente.
