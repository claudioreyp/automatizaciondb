# Recuperacion en Supabase Pro

Estado al 2026-09-19: **datos restaurados y aplicaciones locales activadas** en
el proyecto autorizado `vgxbymduddknaxczudxj` de EscalarAI Pro. No se publicaron
servicios ni se modificaron n8n o el gateway. Decisiones: `../../AGENTS.md`.

## Datos y accesos recuperados

- PostgreSQL es la fuente operativa de la API local. Admins y CLIENTES apuntan al
  mismo proyecto Auth. Autenticacion de desarrollo y altas publicas deshabilitadas.
- 51 tablas de aplicacion, 753 filas: conteos y huellas de todas las columnas
  coinciden con la copia de trabajo. Incluyen 2 negocios, 2 sucursales, 24 pedidos,
  19 pagos, 27 comandas, 33 trabajos de impresion, 23 eventos y 1 credencial API.
- Se preservaron IDs, relaciones, fechas UTC, decimales, SQL NULL frente a JSON
  null y secuencias. Los valores de las 49 tablas anteriores no cambiaron al
  aplicar la migracion aditiva `20260919_0023` en la copia; agrega dos tablas de
  renovaciones y seguridad. El original SQLite permanece en `20260912_0022`.
- Hay un trabajo de impresion pendiente. No se despacho ni se consumieron eventos
  historicos durante la recuperacion o las pruebas. No hay backfill de impresiones.
- El usuario confirmo la eliminacion del proyecto anterior. El respaldo local no
  contiene contrasenas Auth: **no se recuperaron contrasenas anteriores**.
- Se reconstruyeron dos identidades Auth reales con sus UUID originales. No se
  convirtio la cuenta ficticia de desarrollo en un usuario real. Se conservaron
  membresias y permisos. El superadministrador usa la contrasena solicitada,
  gestionada de forma privada; su acceso real a Admins fue comprobado.
- El propietario recuperado tiene una clave aleatoria no distribuida y acceso
  pendiente de renovacion. Su contrasena definitiva debe establecerse desde
  **Admins > Negocios > Pizza House > Propietarios y empleados > Renovar contrasena**.
  No se realizo una renovacion de esa cuenta real durante las comprobaciones.

## Respaldos y archivos

Todo permanece en `Apis/backups`, excluido de Git:

- SQLite original y respaldos previos intactos; la copia de trabajo congelada es
  `supabase-source-20260919T130057892036Z.db`, con manifiesto JSON de conteos y SHA256.
- El ZIP asociado conserva 590 archivos subidos. Las cuatro referencias locales
  activas encontradas (logo, portada y dos productos) tienen su archivo disponible.
  No se detectaron referencias activas a Storage eliminado en el inventario.
- `activation-config-20260919T132840Z` conserva la configuracion privada anterior
  de las tres aplicaciones. No publicar estos archivos ni el manifiesto con datos.

Los archivos siguen servidos localmente. Se creo el bucket privado
`impulsa-private`, pero no se traslado la biblioteca local a Storage. No se afirma
haber recuperado archivos del proyecto eliminado que no estuvieran en el respaldo.

## Seguridad y sesiones

- La conexion de ejecucion usa `escalar_pos_api`, sin superuser, BYPASSRLS,
  CREATEDB, CREATEROLE, CREATE en public ni lectura de tablas Auth. Tiene solamente
  DML del POS y acceso a sus secuencias. Las politicas RLS son exclusivas de ese rol.
- Las tablas tienen RLS y anon/authenticated no tienen permisos directos sobre
  tablas o secuencias. Los navegadores acceden al POS mediante FastAPI, no SQL.
- Clave administrativa Auth, conexion PostgreSQL y secreto HMAC de renovacion
  viven solo en configuracion privada del servidor. Las claves de navegador son
  publicas. Secretos de dispositivos, credenciales API y firma QZ se conservaron.
- Una renovacion registra su intencion antes de llamar a Auth. No persiste la
  contrasena; una respuesta incierta queda pendiente y se reconcilia por el marcador
  de operacion en app_metadata, sin repetir automaticamente el cambio.
- La actualizacion administrativa de contrasena revoca sesiones en Auth. Ademas,
  HTTP y WebSocket validan firma, emisor y session_id con una version local durable.
  Una version nueva invalida comprobaciones antiguas; cache positivo hasta 60 s,
  consultas deduplicadas y respuestas tardias rechazadas. Indisponibilidad de Auth
  es 503/1013, no una contrasena incorrecta ni cierre de sesion fingido.

La revocacion se comprobo contra Auth real, no solo con mocks. Referencias:
[sesiones de Supabase](https://supabase.com/docs/guides/auth/sessions),
[actualizacion administrativa](https://github.com/supabase/auth/blob/master/internal/api/admin.go)
y [revocacion al cambiar contrasena](https://github.com/supabase/auth/blob/master/internal/models/user.go).
Revalidar este comportamiento al actualizar el proveedor.

## Evidencia de verificacion

- API: **717 aprobadas, 2 omitidas** en la ejecucion general. Las dos pruebas que
  requieren PostgreSQL (folios concurrentes y renovacion concurrente de una misma
  identidad entre negocios) se ejecutaron por separado y pasaron en esquemas
  aislados del proyecto autorizado, retirados al terminar.
- CLIENTES: **753 pruebas**; Admins: **22 pruebas**. Lint y build aprobados en ambos.
  Persisten avisos existentes de bundle superior a 500 kB y deprecaciones de tests.
- Playwright Admins: **9 pruebas** en escritorio, tablet y movil. Incluye formulario,
  errores, resultado incierto y bloqueo de doble envio. Revision visual agrupada
  y ronda de confirmacion completadas sin cambiar la identidad visual existente.
- Detector Impeccable: dos avisos preexistentes sobre Fraunces; se conserva la
  tipografia del producto. No se hizo un redisenyo para eliminar esos avisos.
- Prueba real con identidad Auth temporal y base POS desechable: renovar desde
  Admins, entrar en CLIENTES con la nueva clave, rechazar la anterior, bloquear
  HTTP/WebSocket antiguos, denegar falta de permisos y comprobar idempotencia.
  La identidad temporal se elimino al terminar. No se escribieron pedidos, pagos,
  eventos ni impresiones de la base recuperada.
- Los tests cubren caida de Auth, respuestas perdidas/tardias, secreto redactado,
  reutilizacion de clave con otro cuerpo, suspension y cuentas no elegibles. La
  regresion incluye empleados/PIN, cocina, cobros, integraciones, impresion y PWA.
  Esto no equivale a repetir fisicamente impresion, instalacion o acceso movil.

### Incidente corregido durante el ensayo

Antes del traslado definitivo, una prueba antigua de folios dependia de
`search_path` en opciones de conexion. El pooler no lo aplico y genero tablas y
filas sinteticas en public, que estaba vacio. La transferencia concurrente fallo
y se revirtio, sin afectar el SQLite ni datos reales.

Se inventariaron y respaldaron los artefactos sinteticos, se comprobo su contenido
exacto y la ausencia de usuarios Auth, y se retiraron solo esas tablas, sin CASCADE.
Se corrigio la prueba con `schema_translate_map` y comprobacion del esquema antes
de insertar. Se repitieron las pruebas aisladas y, con public vacio verificado,
se realizo el traslado definitivo sin concurrencia; todas las huellas coincidieron.
No reutilizar el helper puntual de limpieza sobre el destino ya restaurado.

## Herramientas y operacion posterior

Los scripts de `Apis/scripts` separan pasos verificables:

1. `prepare_recovery_snapshot.py`: respaldo consistente, migracion de copia y ZIP.
2. `transfer_sqlite_to_postgres.py`: check/rehearse/apply/verify, con destino exacto,
   transaccion, rechazo de destino ocupado y comparacion de todas las tablas.
3. `recover_supabase_auth.py`: identidades reales con UUID original y reconciliacion.
4. `configure_postgres_runtime.py`: rol privado limitado; no rota claves existentes.
5. `activate_supabase_recovery.py`: verifica datos/Auth antes de cambiar configuracion.
6. `inventory_recovery_media.py`: inventario de referencias a archivos, sin escrituras.

Consultar `--help` antes de ejecutarlos. No repetir `apply` contra la base ocupada,
ni ejecutar restauraciones mientras hay escrituras. `.env.supabase-target` es
privado; `MIGRATION_DATABASE_URL` puede ser temporal y nunca se usa para el servidor
operativo. La API usa pooler de sesion con TLS y `AUTO_CREATE_SCHEMA=false`.

**No volver a SQLite despues de nuevas escrituras PostgreSQL sin reconciliarlas.**
Ante una confirmacion perdida, detener cambios y verificar el destino antes de
repetir cualquier paso. Una copia o ensayo no demuestra por si solo un corte real.

## Acceso local y pendientes

- CLIENTES: `http://127.0.0.1:5173/`; Admins: `http://127.0.0.1:5174/`;
  API y esquema: `http://127.0.0.1:8000/docs`. Dependen de mantener procesos locales.
- Renovar la clave del propietario desde Admins y entregarla por un canal privado.
  No usar la clave del superadministrador para otros usuarios.
- El asesor de Supabase solo conserva el aviso **Leaked Password Protection Disabled**.
  Revisar y habilitar esa proteccion antes de produccion. No se cambio silenciosamente
  la politica de Auth. [Guia de contrasenas](https://supabase.com/docs/guides/auth/password-security#password-strength-and-leaked-password-protection).
- Revocar/rotar el token personal de administracion compartido previamente en el
  chat al terminar el trabajo. No es una clave del POS ni debe llegar al navegador.
- Produccion requiere HTTPS, CORS y redirects exactos, SMTP, copias programadas,
  almacenamiento accesible y secretos privados. No se configuro un despliegue,
  arranque automatico ni integracion n8n. La validacion fisica posterior en otros
  dispositivos, PIN, QZ e instalacion PWA sigue siendo una comprobacion separada.
