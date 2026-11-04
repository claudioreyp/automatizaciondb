# API 1.1.2: reemplazo de avisos nunca transportados

Cambio compatible y aditivo sobre API 1.1.1. No requiere migración, nuevas variables de entorno ni cambios de credenciales. Conserva las rutas y contratos de conversación, claim, freshness y ACK existentes.

El transporte interno con perfil `generic` y scope `events:deliver` dispone de `POST /api/v1/integrations/events/{event_id}/outbox/{delivery_id}/replace-never-transported`. Requiere la clave estable `Idempotency-Key: notice-replacement:<delivery_id>`, el token privado de la reserva y el descriptor completo de retiro. El perfil `restaurant_agent` no recibe esta herramienta.

Antes de invocar el endpoint, el adaptador fijo demuestra con los protocolos y guardas originales que la cola nunca fue reclamada/autorizada/transportada, que su ventana original venció y que su retiro quedó durable mediante CAS y lectura completa. El descriptor no es una autorización de envío. El POS valida de nuevo reserva, negocio/sucursal, evento/pedido/destinatario, fingerprints y alias; una transacción suprime la reserva anterior, crea un hijo con el texto preparado por el servidor y avanza las fuentes auditadas. El evento sólo se confirma por el ACK ordinario del nuevo transporte. Una respuesta incierta no habilita repetir un envío.

Los alias tardíos siguen al hijo vigente, también después de su ACK, sin crear otra entrega. Las auditorías son persistentes y no contienen el token original en texto plano. Los errores de validación omiten entradas privadas.

La reversión a 1.1.1 sólo es apropiada antes de cualquier reemplazo en la base productiva. Si ya existen reemplazos, un forward-fix debe conservar la resolución de hijos y la auditoría, aunque cierre temporalmente nuevos reemplazos. No se restaura la base ni se reactiva una reserva suprimida.

Los resultados funcionales publicados junto al release deberán citar la evidencia aislada exacta y los tres GET posteriores del commit real de publicación. Esta nota no afirma un despliegue, un reemplazo productivo ni aceptación integral del agente.
