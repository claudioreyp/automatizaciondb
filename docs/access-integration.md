# Acceso al POS e integracion API

Guia operativa generica. Las decisiones transversales estan en `../../AGENTS.md`.

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

Los permisos predeterminados son `menu:read`, `inventory:read`, `orders:read`,
`orders:write`, `payments:write`, `reservations:write` y `events:read`.
No incluyen ajustes de stock ni cambios de disponibilidad. Tokens anteriores no
se rotan, revocan ni reducen automaticamente durante esta actualizacion.

## Consulta y recuperacion

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
