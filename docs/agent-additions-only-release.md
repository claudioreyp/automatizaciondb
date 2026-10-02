# Pizza House: adiciones del agente, 2026-10-01

## Regla publicada

La integracion del agente no cancela ni modifica pedidos ya registrados, aunque
cocina siga pendiente. El carrito anterior al registro sigue editable. Las
operaciones manuales autorizadas del POS conservan edicion, cancelacion y adiciones.

Para agregar productos, el workflow conserva el pedido original, recoge solo las
selecciones nuevas y valida opciones, disponibilidad e importe en la API. Presenta
el resumen adicional y pregunta Yape o efectivo, incluso si el metodo original
era diferente. La eleccion del pago adicional no reemplaza el metodo original.

- Efectivo: incorpora los productos con cobro pendiente y una comanda nueva.
  Solo comunica el exito con confirmacion de la API de productos y comandas.
- Yape: crea una solicitud de pago adicional del mismo pedido, envia el QR real
  del POS y recibe un comprobante clasificado. No incorpora productos ni envia
  su comanda antes de la aprobacion humana. El POS ofrece
  **Aprobar pago y agregar productos** o **Rechazar**. La aprobacion incorpora
  productos, pago y comanda en una transaccion; rechazo conserva el pedido previo.
- El relay avisa que los productos adicionales se agregaron al folio confirmado
  y se envio la nueva comanda. Dos adiciones aprobadas independientemente conservan
  sus propios avisos. Si cocina ya termino, el texto corresponde al estado actual.
- Lista y detalle muestran la atribucion del agente solo tras la incorporacion
  confirmada. La auditoria mantiene esa atribucion en el historial finalizado.
- Despachado, entregado o cerrado no admite adiciones de la integracion. Una compra
  posterior abre otro carrito sin heredar modalidad, pago, destino o variante.

La operacion incierta conserva cuerpo, version y clave de idempotencia. La API
mantiene el fingerprint anterior cuando una peticion antigua omite el nuevo metodo
opcional. No se reenvia un aviso de WhatsApp con entrega incierta.

## Versiones verificadas

| Componente | Version y verificacion |
| --- | --- |
| API | `c912c2c` + `3e259d35e39acb5859dbae6b203b5f891d924a74`; Render `dep-davff5uk1f9s73a4g9o0`, Deploy succeeded / Live. |
| CLIENTES | `7f5764d` + `d641c4c6ef90714aff13fbdd2907f4c724399783`; Vercel `GMX7axXRHWG1UzY3qPfvnLSEgm96`, Ready / Production / Current en `pos.escalarai.tech`. |
| n8n | `HstQEpRLMqONt6v4`, version activa `36c0e413-3143-4214-8608-b77ee0448041`, SHA256 `b901fb4a4a605221bda8247e9b9552d1788aeeb8a9f8f4f8150e25fe3aaa8227`. |
| Gateway / relay local | Manifest SHA256 `363e9eb3fcbdd0bcefd2c8f1c3d05d62f7f91142bd7bae1940390824fa610212`; arranque manual verificado, una instancia y sesion conectada/operativa. |

API health, contexto autorizado 2/2 y schema de `Addition.payment_method`
verificados mediante lecturas. No hubo migracion de esquema.

n8n conserva 147 nodos, `gpt-6-luna`, credenciales existentes y ajustes.
Se modificaron 18 nodos y solo la salida negativa de `Safe Customer Change?`.
Lectura posterior confirma el hash tanto del borrador como de la version activa.
La memoria no cambio durante la publicacion. El monitor paralelo de pagos sigue
deshabilitado; el relay local sigue siendo el unico emisor de eventos operativos.

El primer intento de publicacion se detuvo antes de invocar la API por diferencia
entre el artifact preparado y el ultimo ajuste probado. La version v2 incluyo
esa limpieza de propuesta heredada, se volvio a probar y se publico una sola vez.

## Evidencia de pruebas y limites

- API: 838 pruebas completas y 5 omitidas antes del ultimo ajuste de compatibilidad;
  111 pruebas afectadas y 1 omitida despues de ese ajuste. Incluyen rechazo sin
  mutacion, aislamiento, adiciones efectivo/Yape, revision humana, eventos,
  idempotencia y preservacion de las operaciones manuales del POS.
- Workflow: 40 pruebas de la transformacion y 4 pruebas de lifecycle sobre el
  artifact v2; 18 regresiones de compra posterior y 95 de conversacion existente.
- Relay: 13 pruebas, incluidos dos pagos adicionales, estado reciente y ACK sin
  reenvio. Gateway compilado y activado despues de validar inactividad.
- CLIENTES: 38 pruebas y 11 afectadas por el texto del boton; lint/build y
  comprobacion visual desktop/movil con fixtures aislados. No son pedidos reales.
- Webhook activo: cuatro turnos sinteticos directos, sin transporte WhatsApp:
  Hola, La carta, Hoy quiero una pizza hawaiana, Mediana. Respuestas correctas,
  carta mediante `pos_menu`, turno preservado y `outcome=none`, antes de modalidad
  o pago. Las ejecuciones exitosas no se guardan, segun configuracion existente;
  no se habilito su almacenamiento para hacer la comprobacion.
- Reinicio manual del gateway: PID 15496, Chrome propio 48976, perfil conservado,
  relay 2/2 y ledger sin restauracion ni reinicio de su fecha de activacion.
  Salud healthy, sessionReady/messageBusReady y QR isOperational confirmados.

No se crearon pedidos reales, enviaron comprobantes ni aprobaron pagos ficticios
en produccion. Las escrituras de compra/adicion y su aprobacion se probaron en
entorno aislado; una prueba visible desde WhatsApp con cocina/impresion requiere
coordinacion antes de generarla. El gateway sigue dependiendo de esta PC encendida.

Respaldos, recibos privados y smoke record permanecen en `Apis/backups`, excluidos
de Git. Las transformaciones y los scripts de activacion se mantienen en el
workspace padre de Impulsa; su manifest y artifact identifican el codigo activo.
No contienen ni publican credenciales en este documento.
