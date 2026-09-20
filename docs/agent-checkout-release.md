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

Estado pendiente de completar en esta entrega: commits/despliegues y comprobacion
real de WhatsApp. No confundir pruebas simuladas con entrega fisica de imagen o
avisos. Las pruebas finales requieren mensajes nuevos identificados y coordinacion
para no despachar impresion automatica; no se haran pagos bancarios.

Rollback: volver a codigo compatible, sin revertir la migracion aditiva ni
restaurar una base antigua sobre nuevas escrituras. Detener gateway ante resultado
incierto, sin borrar sesion ni registro de entregas.
