# Credenciales adicionales y entrega de eventos

La emision adicional no reemplaza ni revoca credenciales existentes. Solo un
superadministrador puede crearla y revisar su resultado. El token es reutilizable;
su visualizacion, no su uso, ocurre una sola vez.

`POST /api/v1/admin/integration-credentials` acepta `Idempotency-Key`. Admins la
envia siempre. La primera respuesta 201 contiene el token y el identificador de
operacion; repetir clave y cuerpo devuelve 200 sin el token y sin otra emision.
Cambiar el cuerpo conservando la clave devuelve 409. Clientes previos sin clave
conservan el contrato anterior.

`GET /api/v1/admin/integration-credentials/operations/{operation_id}?branch_id=...`
devuelve `created`, `not_found` o `requires_review`, nunca secretos. Las respuestas
son `no-store`. El recibo durable guarda huella, sucursal e ID de credencial, no un
token recuperable. Una respuesta perdida requiere consulta antes de otro intento;
no rotar, revocar ni emitir otra credencial automaticamente.

`GET /api/v1/integrations/events` conserva `created_after` y acepta parametros
`event_types` repetidos (hasta 16). El filtro se aplica antes del limite. Un relay
debe conservar su fecha de activacion y nunca enviar ni reconocer eventos previos.
Tras un envio confirmado solo se reintenta el ACK; un envio incierto requiere
revision, no repeticion automatica.

La implementacion reutiliza el registro de idempotencia existente y no requiere
migraciones. La concurrencia se serializa por negocio en PostgreSQL.
