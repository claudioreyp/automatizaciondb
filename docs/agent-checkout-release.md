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

## Indicador de escritura (2026-09-20)

Cambio local en Impulsa/src/typing.ts y src/gateway.ts, sin modificar el workflow,
API, CLIENTES, Admins ni credenciales. Antes se usaba startTyping con duracion de
15 segundos; su promesa espera ese plazo. El finally esperaba esa promesa despues
del envio y podia mantener la presencia aunque la respuesta ya estuviera entregada.

Ahora whileTyping limita la presencia a la preparacion de la respuesta. El inicio
no impone una duracion; el stop explicito espera solo el inicio pendiente, cancela
refrescos y es idempotente. La parada termina antes del bucle de envios, no despues
del ACK. Los errores y las respuestas suprimidas tambien limpian la presencia.
No se agregaron demoras ni se alteraron la cola de conversacion o los envios.

Verificacion: 56 pruebas gateway/workflow aprobadas, npm run check y npm run build
correctos. Los casos adicionales cubren ACK lento, multiples acciones, refrescos
encolados, stop repetido, errores, respuesta vacia y procesamiento posterior.

Activacion local completada a las 13:57 America/Lima. Tras bloqueo del primer
comando, el usuario autorizo expresamente reiniciar solo el gateway. Se verifico
su proceso y ausencia de trafico reciente, se detuvo mediante PowerShell y se
inicio oculto el launcher persistente con el nuevo build. PID 31052, un solo
proceso y relay; escucha exclusiva 127.0.0.1:3008. Estado connected, sessionReady y
messageBusReady, sin QR nuevo ni mensajes pendientes de agrupar. No se borro la
sesion. Fecha de activacion (2026-09-20T01:46:39.933Z) y huella SHA256 del registro
durable de entregas identicas antes y despues. El build cargado contiene
whileTyping y startTyping sin duracion. No se enviaron mensajes de prueba a
consumidores. La comprobacion visual del indicador en un telefono sigue pendiente;
no confundir las pruebas de ciclo de vida con evidencia fisica de WhatsApp.

## Conversacion y eleccion de pago (2026-09-20)

Version activa final `557b6118-cda3-4b64-bbc4-b27fb640c9b3`, mismo workflow
`HstQEpRLMqONt6v4`. Version previa respaldada `66c45f97-e09e-4891-9a3d-077c7c2d41e9`;
revisiones intermedias a1af5122-6f67-4a9e-8d23-3683df4db691 y
cefa7f78-2a2b-45c8-b7f8-673599305260 reemplazadas. Lectura posterior confirma version
publicada, nodos y conexiones iguales al artefacto preparado. Regenerar desde el
transformador completo reproduce todos los parametros y conexiones, sin diferencias
de credenciales. No se envio staticData en la actualizacion ni se borraron recuerdos.

Causas comprobadas con codigo publicado y casos de las capturas:

- El detector de pagos se ejecutaba antes de incorporar modalidad/productos. Si el
  carrito no estaba completo, Yape iba a una frase fija y no guardaba la eleccion.
  El cliente quedaba atrapado entre confirmar y volver a elegir pago.
- El envio de carta dependia ademas de intent=consult_menu. Un turno mixto como
  Familiar / mandame la carta podia clasificarse prepare_order y perder la imagen.
- El resumen estructurado omitia variante y opciones; responder modalidad/pago
  podia reemplazar una seleccion valida por otra incompleta generada por el modelo.
- El filtro final quitaba los emojis originales y agregaba uno a TODOS los textos,
  incluso ante frustracion. Las frases fijas de confirmacion agravaban la repeticion.

Correccion:

- Conservar requestedPaymentMethod dentro de la sesion de compra, con deteccion
  de elecciones explicitas, negaciones, preguntas y metodos ambiguos. Reconocer
  recojo en el local, para llevar, consumo en local y Yapeeee. No es autorizacion de
  pago ni prueba bancaria. La nueva compra descarta decisiones de la anterior.
- El carrito nuevo pasa primero por contexto/modelo/resolucion y despues por
  Resolve Checkout Payment. Reutilizar la cotizacion y validacion existentes; las
  imagenes de comprobante y los pagos de pedidos ya registrados conservan su ruta
  anterior. No hay doble paso de creacion, nuevos endpoints ni cambios de cobros.
- QR solo con importe actual validado, metodo habilitado y datos reales del POS.
  El efectivo sigue entrando por Prepare Deferred Checkout Submission y validacion
  existente. Una consulta de disponibilidad no autoriza una escritura por recordar
  CASH. Un fallo de precio no puede emitir un QR ni anunciar registro.
- Requerimiento de carta independiente del intent, tambien en la salida de pago.
  Gateway expande pos_menu a las imagenes actuales en orden con el texto de la
  primera. El filtro conserva privacidad, autorizacion y folios comprobados.
- Preguntas concretas para tamano/opciones faltantes, segun errores conocidos de
  preview. Errores desconocidos o indisponibilidad no se convierten en preguntas
  inventadas ni filtran detalles internos. Sin emojis forzados en textos normales.

Verificacion:

- 72 pruebas gateway/workflow, npm run check y npm run build aprobados. Incluyen
  reproduccion de la version anterior, mensajes combinados/separados, persistencia,
  omision de variantes por el modelo, negaciones, metodo deshabilitado, error de
  precio, delivery incompleto, efectivo, nueva compra y conservacion de credenciales.
- Once turnos reales de webhook/modelo repartidos entre cinco identidades sinteticas
  distintas: secuencia de las capturas; compra + recojo/Yape/carta; Yape antes de
  indicar tamano; compra completa en un solo mensaje; carta seguida de pregunta de
  hamburguesas. Las cuatro compras devolvieron QR actual por S/45 sin pedir otra
  confirmacion. La compra incompleta pregunto exactamente el tamano y con Familiar
  paso al QR sin preguntar de nuevo el pago. Las consultas de producto devolvieron
  texto sin reenviar carta; el mensaje mixto incluyo carta y QR.
- La primera prueba combinada anterior compartia accidentalmente el identificador
  normalizado con otra prueba. No se utilizo como evidencia de aislamiento: se
  repitieron los escenarios con sufijos numericos distintos y se verificaron sus
  estados independientes. No se tocaron conversaciones reales para simularlos.
- Cuatro carritos sinteticos quedaron awaiting_yape_evidence, TAKEAWAY, YAPE y sin
  submittedSaleId; el de consultas quedo collecting y sin venta. No se enviaron
  imagenes de comprobante ni solicitudes de efectivo en pruebas reales.
- Un turno adicional en el chat de la prueba tecnica anterior pidio compra nueva
  Familiar/Yape. Recibio QR por S/45, no el importe previo de S/30. La respuesta de
  estado del pedido tecnico #27 (ID28) permanecio identica antes/despues. La nueva
  compra sigue siendo un carrito, no otra venta registrada.
- Comparacion SHA256 de todos los QR retornados con el endpoint autenticado actual.
  Gateway descargo tambien la galeria real: una imagen WEBP, 194972 bytes, con
  Aqui esta nuestra carta y el emoji solicitado en el caption. No se enviaron estas
  pruebas a WhatsApp ni se consumieron eventos/impresiones para comprobarlas.
- Gateway connected/sessionReady/messageBusReady, sin lotes pendientes; no hubo
  reinicio de WhatsApp en esta correccion. El monitor duplicado sigue deshabilitado.

Artefactos locales en Impulsa/scripts: pizza-house-conversation-code.mjs,
pizza-house-conversation-patch.mjs, release-pizza-conversation.mjs,
check-pizza-conversation-live.mjs y check-pizza-new-purchase-live.mjs. El generador
principal incorpora el parche; las pruebas estan en tests/pizza-house-conversation.test.ts.
Respaldos, payloads y recibos privados en Apis/backups, fuera de Git. No se publica
el token ni la memoria en codigo. El gateway no tiene un repositorio Git utilizable;
no afirmar que estos archivos locales se subieron a GitHub. El workflow si esta
publicado y comprobado. API runtime, CLIENTES, Admins y esquema no cambiaron.

Limites: evidencia de modelo/webhook/API/bytes y pruebas automatizadas, no entrega
visual en un telefono durante esta verificacion. No afirmar que un modelo nunca
se equivocara; los precios, permisos, comprobantes y escrituras siguen protegidos
por contratos deterministas. No hubo pagos bancarios ni ventas nuevas en estas pruebas.
