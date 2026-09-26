# Acceso al POS e integracion API

Guia operativa generica. Las decisiones transversales estan en `../../AGENTS.md`.

## Contexto configurado desde el POS

CLIENTES > Configuracion > Perfil del agente administra el nombre, la carta,
el numero de Yape, su titular y el QR completo. Datos de sucursal administra
direccion, telefono, enlace de Maps y coordenadas; Costos de envio administra
tarifas y condiciones. Admins solo consulta este contexto por sucursal, sin
duplicar formularios, y sigue administrando credenciales y rutas copiables.

`GET /api/v1/integrations/context` requiere Bearer con `menu:read`. El token
impone negocio/sucursal. Agrega estos bloques sin retirar `business` ni `branch`:

- `agent`: nombre y version del perfil.
- `location`: direccion, telefono, maps_url, latitude y longitude del local.
  El origen personalizado de reparto permanece separado en delivery.policy.
- `payments`: metodos por modalidad, datos heredados de Plin y `yape` con number,
  recipient_name, qr_configured, qr_url, qr_authentication y qr_scope.
- `delivery`: enabled, mode, fee, fee_status, requires_quote, requires_destination,
  configuration_version, tarifas, minimum_order_amount, free_delivery_threshold,
  policy y bands. Lee BranchSettings; source=legacy_branch solo cuando todavia no
  existe configuracion POS. La consulta no crea ni modifica configuracion.

`delivery.fee` es la tarifa configurada de fixed/free, sujeta a cobertura, minimo
y condiciones, no una cotizacion de un pedido. En quote es null/pending_quote;
distance/bands/neighborhoods devuelven null/destination_required con sus reglas.
Deshabilitado devuelve null/disabled. Nunca interpretar null como cero. No usar
`branch.delivery_fee` para el contrato nuevo: se conserva solo por compatibilidad.
Esta entrega no cambia el calculo ni el registro de pedidos de integracion.

Descarga del QR (respuesta binaria, no JSON):

```sh
curl --fail --show-error \
  -H "Authorization: Bearer $POS_INTEGRATION_TOKEN" \
  https://api.escalarai.tech/api/v1/integrations/context/yape-qr \
  --output yape-qr.png
```

Usar el Content-Type devuelto (PNG/JPEG/WEBP) al enviar la imagen. Resolver qr_url
contra el origen de la API, no contra pos.escalarai.tech ni admin.escalarai.tech.
La futura integracion descargara el archivo con su token; no debe enviar al
consumidor el token ni una URL con credenciales. No se modifico n8n para hacerlo.
404 indica QR ausente/no encontrado; 503 indica almacenamiento indisponible y
requiere reintentar la lectura, nunca afirmar que se envio la imagen.

El archivo permanece en Storage privado `impulsa-private`; PostgreSQL guarda su
referencia. Las claves de servidor nunca salen al navegador. La descarga usa
`Cache-Control: private, no-store` y valida el ambito en cada solicitud. Las rutas
publicas de la carta siguen disponibles por compatibilidad.

`GET /api/v1/admin/branches/{id}/agent-context` exige superadmin, no devuelve
tokens ni rutas privadas de Storage y permite consultar negocios suspendidos.
`GET/PATCH /api/v1/settings/branches/{id}/agent` agrega `yape_number` (40) y
`payment_recipient_name` (180), opcionales; PATCH exige version e idempotencia.
Omitir conserva; null/vacio elimina solo el texto, no el QR ni los metodos.
No requiere migracion ni restablece datos existentes.

## Alta desde Admins

1. Ingresar con el superadministrador y abrir Negocios > Nuevo restaurante.
2. Completar negocio, propietario y sucursal principal. No se asigna plan: el
   restaurante recibe POS completo, sujeto a sus roles y ajustes operativos.
3. Pulsar Crear restaurante y APIs una vez. La API reserva el slug antes de
   crear el usuario externo y confirma el alta local antes de anunciar exito.
4. Entregar al propietario solamente URL de CLIENTES, usuario y contrasena inicial.
5. Guardar el token privado en el gestor de secretos de la integracion. No
   entregarlo al consumidor ni pegarlo en chats, capturas o documentos compartidos.

La contrasena no vuelve desde la API: Admins conserva su borrador en memoria hasta
cerrar el resultado. El token se devuelve una vez y la base conserva su hash.
El propietario administra empleados, roles e invitaciones desde CLIENTES; instalar
la PWA o vincular un dispositivo no concede privilegios adicionales.

## Renovar la contrasena de un propietario

1. El superadministrador abre el negocio y busca Propietarios y empleados.
2. Pulsa Renovar contrasena en un propietario elegible. No se ofrece para empleados,
   PIN, cuentas ficticias ni identidades que tambien sean superadministradoras.
3. Introduce y confirma la clave, o usa Generar segura. La politica exige 12 a 128
   caracteres con mayuscula, minuscula, numero y simbolo; permite mostrar/ocultar.
4. Confirma sabiendo que cerrara las sesiones web/PWA de toda esa identidad,
   incluso si pertenece a varios restaurantes. No cambia roles, suspendidos,
   tokens de integracion ni dispositivos/PIN.
5. Solo despues del exito confirmado, copia correo, contrasena y enlace CLIENTES.
   La clave vive en memoria y desaparece al cerrar o abandonar el formulario.
   El portapapeles, si se usa, queda bajo control del usuario; no es un respaldo.

Si falla de forma confirmada, el borrador permanece para corregirlo. Si no hay
confirmacion, usar Comprobar resultado; no crear otra operacion ni cambiar la clave
a ciegas. Cerrar y volver permite consultar el estado, pero no recuperar la clave
del servidor. Si se perdio el borrador, resolver primero la operacion pendiente y
solo despues realizar una nueva renovacion intencional.

### Contrato administrativo

Todas estas rutas tienen prefijo `/api/v1`, requieren superadmin y responden con
`Cache-Control: no-store`, tambien ante errores:

- `POST /admin/businesses/{business_id}/memberships/{membership_id}/password-reset`:
  cuerpo `password` y `expected_version`, encabezado `Idempotency-Key` de 8 a 200
  caracteres. El servidor comprueba rol, negocio, propietario e identidad Auth real.
- `GET /admin/businesses/{business_id}/memberships/{membership_id}/password-reset/lookup`:
  consulta por la misma `Idempotency-Key` si se perdio la respuesta del POST.
- `GET /admin/businesses/{business_id}/memberships/{membership_id}/password-reset/{operation_id}`:
  consulta/reconcilia una operacion conocida sin volver a enviar su contrasena.

La respuesta incluye `operation_id`, `status`, `security_version`, `error_code`
y enlace CLIENTES, nunca la contrasena. `pending` no es exito; `unconfirmed` en lookup
no prueba que un POST anterior haya terminado. `succeeded` confirma el marcador
del proveedor. `failed` representa rechazo confirmado. El alta durable y la version
serializan renovaciones de una misma identidad, incluso entre dos restaurantes.
Reutilizar una clave con otro cuerpo produce 409. No repetir automaticamente POST.

Auth se actualiza solo desde el servidor. El registro durable conserva HMAC, no
contrasenas ni cuerpos sensibles. HTTP/socket comprueban la version de seguridad
y la sesion del proveedor; el cache positivo dura como maximo 60 segundos por
version. Un cambio de version invalida comprobaciones previas. Una caida de Auth
se informa como indisponibilidad, no como contrasena incorrecta.

La recuperacion del proyecto eliminado y el estado de sus propietarios se detallan
en `supabase-migration.md`; no se recuperaron sus claves anteriores.

## Datos copiables

El bloque `integration` de la respuesta de alta contiene:

- `api_base_url`, terminada en `/api/v1`.
- `business_id` y `branch_id` de esta credencial.
- Autenticacion Bearer y nombre del encabezado `Authorization`.
- `write_idempotency_header`: `Idempotency-Key`.
- `scopes` y `endpoints`, cada uno con metodo HTTP, URL y permiso requerido.

Admins ofrece Copiar acceso, Copiar token, Copiar paquete de APIs y copia individual.
El paquete copiado contiene un encabezado Bearer utilizable durante el alta. La
consulta posterior usa `<TOKEN_PRIVADO>`: sustituirlo por el secreto ya guardado.
El bloque antiguo de compatibilidad no forma parte de la interfaz ni del paquete.

Ejemplo de lectura, usando valores ficticios:

```http
GET https://api.example.test/api/v1/integrations/context
Authorization: Bearer <TOKEN_PRIVADO>
```

Toda escritura requiere `Idempotency-Key`, con un valor nuevo por operacion
intencional. Un reintento tecnico conserva exactamente clave y cuerpo. Un segundo
pedido realmente solicitado usa otra clave. Consultar el esquema de cada cuerpo en
`/docs` de la API correspondiente; no inventar nombres de productos o importes.

El paquete copiable incluye las rutas vigentes del agente: catalogo apto para el
consumidor, galeria privada de la carta, preview sin escritura, estado del pedido
del remitente, adiciones, revisiones y eleccion del pago del envio. `customer-state`
requiere `sender` y `orders:read`; las modificaciones del pedido requieren
`orders:write`. La galeria y el catalogo requieren `menu:read`.

Para el comprobante **inicial** de un pedido con origen agente/WhatsApp,
`POST /integrations/orders/{order_id}/payment-evidence` requiere imagen, remitente
vinculado (`sender`), `whatsapp_message_id`, `provider=yape`, metodo Yape vigente y
`looks_like_payment_receipt=true`, ademas de `payments:write` e
`Idempotency-Key`. Una imagen no clasificada o rechazada por el analisis devuelve
error sin cambiar el pedido ni guardar comprobante. El numero de operacion, monto,
fecha, destinatario y codigo de seguridad de tres digitos solo se conservan cuando
son legibles; la aprobacion sigue siendo exclusivamente humana. Las solicitudes
`payment_request_id` de adiciones/envio mantienen su contrato anterior.

Los permisos predeterminados son `menu:read`, `inventory:read`, `orders:read`,
`orders:write`, `payments:write`, `reservations:write` y `events:read`.
No incluyen ajustes de stock ni cambios de disponibilidad. Tokens anteriores no
se rotan, revocan ni reducen automaticamente durante esta actualizacion.

## Consulta y recuperacion

La emision adicional y su recuperacion ante respuestas perdidas se documentan en
`additional-credentials.md`. El token queda vinculado al ID exacto de credencial,
no a la ultima credencial que aparezca en una lista.

El caso conectado posteriormente de Pizza House, con credencial privada y gateway
local, tiene su evidencia y limites en [esta guia](pizza-house-qr-integration.md).

- En el restaurante de Admins, seleccionar sucursal y Ver APIs de una credencial.
  `GET /admin/branches/{id}/integration?credential_id=...` devuelve configuracion
  filtrada por sus permisos, sin el secreto. Solo el superadministrador accede.
- Rotar invalida el token anterior inmediatamente y muestra uno nuevo una vez.
  Revocar retira el acceso. Ambas acciones requieren decision explicita.
- Una respuesta perdida del alta no significa fracaso. Consultar estado del alta
  usando el slug original; el sistema no manda otra creacion automaticamente.
- `created` confirma negocio, propietario y credencial locales; `requires_review`
  exige revisar el negocio existente. `not_found` no prueba que una solicitud
  anterior haya terminado: comprobar tambien Auth y su auditoria antes de repetir.
- Si Auth acepto la creacion pero se perdio su respuesta, puede existir un usuario
  externo sin membresia local. No tendra acceso al POS. Reconciliarlo de forma
  supervisada; no crear otro restaurante con otro slug para evadir el error.

Las operaciones autorizadas siempre quedan limitadas al negocio y sucursal de la
credencial. No basta cambiar un ID en la URL para acceder a otro restaurante.
La aprobacion de Yape sigue siendo humana. Eventos durables conservan su contrato
de lectura y ACK: exponer una ruta no implica que un consumidor externo ya la use.

## Requisitos antes de publicar

- Configurar Supabase Auth y el primer superadministrador de forma supervisada;
  deshabilitar altas publicas innecesarias. La API exige membresia independiente.
- `ENVIRONMENT=production`, `AUTO_CREATE_SCHEMA=false`, sin `DEV_AUTH_TOKEN` ni
  `VITE_DEV_AUTH_TOKEN`. Las claves privadas y de servicio solo viven en el servidor.
- Configurar `PUBLIC_API_BASE_URL`, `POS_PUBLIC_BASE_URL`, `VITE_API_BASE_URL` y
  `VITE_CLIENT_POS_URL` con direcciones HTTPS reales. No entregar localhost a otros
  equipos. Configurar CORS exacto, redirects de Auth y cookies seguras del mismo sitio.
- Validar PostgreSQL, RLS, backups y migraciones en una base de pruebas aislada
  antes de autorizar cambios de produccion. La renovacion requiere la migracion
  aditiva `20260919_0023` y `AUTH_ADMIN_SECRET` privado, estable y de al menos 32
  caracteres. Se activo en el proyecto Pro autorizado, no en otras bases.
- Probar alta y login reales contra el Auth del entorno de destino, luego conectar
  el consumidor externo con su token de minimo privilegio. No se modifico ni probo
  un workflow externo real durante esta tarea.
- La PWA requiere POS y API disponibles. QZ mantiene su certificado propio,
  clave privada de servidor y activacion de confianza por equipo de impresion.
