# Cambios de pedidos desde WhatsApp — Pizza House

## Alcance

Correccion del 2026-09-27 para negocio 2, sucursal 2 y workflow
`HstQEpRLMqONt6v4`. No se modifica retrospectivamente el pedido #42: al revisar
su estado real, cocina ya lo habia marcado listo.

- El agente consulta el pedido del mismo remitente y su estado de cocina antes
  de proponer o aplicar un cambio.
- `PATCH /integrations/orders/{order_id}/fulfillment` exige remitente, alcance de
  credencial, version e `Idempotency-Key`. Cambia modalidad y destino solo tras
  aceptacion en la conversacion y exito confirmado de la API.
- Una comanda completada bloquea cambios del agente aunque se reabra. Mientras
  el pedido siga abierto se permiten adiciones, con su comanda nueva y cobro
  separado segun el metodo de pago.
- Yape conserva comprobante, importe y revision humana originales. El envio se
  cotiza o cobra aparte. No hay aprobaciones ni devoluciones automaticas.
- Las modificaciones de productos en pedidos pagados o con comprobante conservan
  los importes existentes; un cambio de precio requiere revision del encargado.
- El POS muestra un aviso auditado durante 48 horas mientras el pedido permanezca
  abierto. Las comandas activas muestran la modalidad actual sin eliminar su
  instantanea original. No se crea una comanda ni se reimprime por cambiar modalidad.
- Una respuesta perdida se reconcilia contra el pedido y destino actuales. Un
  rechazo definitivo permite corregir la solicitud; un resultado incierto no se
  vuelve a escribir a ciegas.

## Verificacion

Las escrituras, pagos, comandas, versiones y trabajos de impresion se comprueban
con bases y servicios aislados. No se generan pedidos reales ni se aprueban pagos
ficticios para probar esta correccion. La lectura de #42 se limita a comprobar que
la regla de cocina completa impide convertirlo ahora.

- API: `python -m pytest -q`, 810 pasan y 5 ensayos PostgreSQL opcionales se omiten
  por ausencia de `POS_TEST_POSTGRES_URL`; 15 avisos Alembic preexistentes.
- CLIENTES: 777 pruebas pasan; lint, build y comprobacion de diff correctos.
- Workflow: 19 regresiones aisladas pasan, incluyendo cambios de productos antes
  de completar cocina y adiciones despues de completarla. Todos los nodos de
  codigo del artefacto compilan.
- Lectura real posterior: #42 (ID 43), negocio/sucursal 2/2, version 6, estado
  `ready`, modalidad `takeaway`; cocina completada, cambios bloqueados y adiciones
  permitidas. No se escribio sobre ese pedido.

## Publicacion

Publicacion verificada en el orden API, CLIENTES y workflow:

| Componente | Version publicada | Evidencia |
| --- | --- | --- |
| API | `ed05cc6f48469a4accd09c8adb88c09a7aacc4aa` | Render Live `dep-dasqepm0tbcc738j59r0`; salud 200 y PATCH de fulfillment presente en OpenAPI |
| CLIENTES | `34bfe2b3035bbe925eaf6b2e8a57c920adefc045` | Vercel Production Ready `CgZcWxs6D1Br7YEdcgNHLJKqzWGc`; carga operativa de Pizza House en `pos.escalarai.tech` |
| n8n | `291ae3cf-6f28-446a-9adb-60bb6b76e71c` | 147 nodos, lectura activa/draft coincidente con el artefacto revisado |

SHA256 del artefacto n8n:
`539a15d4455729a2d7dfbcc8a0b6f024d3ba290ad668d23ade5cc235093888f5`.
La lectura posterior confirma que se conserva todo `staticData` del respaldo
inmediato, ademas de credenciales, modelo y monitor paralelo deshabilitado.

El respaldo y recibo privados de n8n se conservaron en `backups`, excluido de Git.
No se requiere migracion, cambio de credenciales ni reinicio del gateway QR.
La comprobacion tecnica posterior paso los tres turnos de saludo, carta y resumen
de pago. Conservo `turn.id`, obtuvo HTTP 200 y `outcome=none` en los tres casos:
cero pedidos solicitados, pagos, comprobantes enviados o mensajes a WhatsApp real.
El gateway mantuvo salud 200 y sesion conectada. No se hizo una prueba fisica de
impresion ni se envio una nueva comanda al restaurante.
