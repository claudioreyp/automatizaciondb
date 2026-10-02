# Activación de marca, cobro de mesas y archivado

Fecha de preparación y activación: 2026-10-02. Cambios implementados en CLIENTES y Apis.
Las decisiones de producto permanecen en `../../AGENTS.md`.

## Estado y autorización

El usuario autorizó específicamente respaldo, migración, publicación de API y
CLIENTES y recuperación del folio #65. La migración `20261002_0025` agrega
únicamente `restaurant_tables.archived_at` y su índice; se aplicó sobre
`20260920_0024`. API `3162a64` está Live en Render y CLIENTES `9ff1601` está
Ready / Production en Vercel. Esta autorización no se extiende a otras bases,
otros despliegues ni cambios de n8n, gateway o credenciales.

Las pruebas aisladas no crearon pedidos ni cobros reales. La única recuperación
operativa fue el folio #65, con `payments: []`: liberó su mesa sin otro cobro y
generó una cuenta de cliente para su primer ciclo de cierre, pendiente de impresión.

El modelo nuevo necesita la columna antes de iniciar la API actualizada, incluso
para lecturas de mesas. Publicar CLIENTES después de actualizar la base y la API.
La revisión de n8n, gateway, secretos y contratos de integración no forma parte
de esta activación.

## Preparación del respaldo

Respaldo privado preparado en `backups/pos-tables-20261002T185202Z`, excluido de
Git: revisión operativa `20260920_0024`, 52 tablas y 2599 filas. Se obtuvo por
reflexión del esquema real dentro de una transacción PostgreSQL `READ ONLY`
con aislamiento `REPEATABLE READ`, sin usar la columna nueva. El archivo JSON
se releyó y comparó íntegramente, con conteos y SHA-256 verificados; se copiaron
configuraciones privadas y archivos subidos. No se restauró aún en PostgreSQL
aislado. Repetir el respaldo antes de activar si hubo nuevas escrituras.

Para esta activación se repitió el respaldo en
`backups/pos-tables-release-20261002T190716Z-ff3227`: 52 tablas / 2599 filas,
revisión 0024, JSON/SQL NULL diferenciados y archivos/configuración privados.
Los hashes y la lectura íntegra del JSON y ZIP pasaron. La credencial temporal de
migración había vencido; se usó la sesión existente del SQL Editor del proyecto
Supabase autorizado, sin modificar credenciales ni permisos del rol operativo.

El SQL de Alembic se generó con el inspector real, con nombres calificados,
guardias de identidad/revisión, transacción y tiempos máximos de bloqueo. Se
ensayó sobre copias de las 52 tablas en un esquema desechable: 2599 filas y 104
comparaciones exactas antes/después, incluidos JSON NULL y SQL NULL. `ROLLBACK`
retiró la copia y se comprobó que public continuaba en 0024. El ensayo `LIKE`
no copia FK, triggers ni RLS; no se presenta como restauración integral de un
pg_dump. Docker Desktop no pudo iniciar su motor; la restauración del JSON en
otro PostgreSQL y las pruebas específicas de concurrencia PostgreSQL siguen
sin verificarse. Sus protecciones tienen pruebas SQLite y HTTP aisladas.

La activación bloqueó brevemente solo `restaurant_tables` en su transacción,
comparó una copia temporal de todas sus columnas originales y confirmó la
revisión 0025. La verificación posterior de solo lectura comparó las 52 tablas
contra el respaldo: todos los valores originales y 2599 filas conservaron sus
huellas. Las 27 mesas tienen el nuevo marcador NULL. RLS continúa activo y el rol
API no tiene CREATE en public. Reporte privado: `postmigration-verification.json`.

1. Identificar la base y sucursal autorizadas y registrar la revisión Alembic
   actual, sin mostrar la conexión privada. Detener escrituras durante el cambio.
2. Preparar un respaldo consistente mediante la herramienta de respaldo de
   PostgreSQL, con una identidad autorizada y configuración privada de conexión.
   Usar el esquema real anterior a 0025; no reconstruir filas con metadata nueva.
   Guardarlo fuera de Git, en un directorio nuevo de `Apis/backups`, junto con los
   archivos privados de configuración y los archivos subidos. No rotar secretos.
3. Verificar que el respaldo pueda restaurarse en una base aislada y comparar
   conteos/valores previos antes de continuar. No restaurarlo sobre la base activa.
4. Ensayar 0025 en esa copia PostgreSQL. Las tres pruebas de concurrencia de
   archivado requieren `POS_TEST_POSTGRES_URL` y crean esquemas temporales; esa
   variable no estuvo disponible durante esta implementación. Confirmar aislamiento
   antes de usarlas y no apuntarlas a una base de uso.

El script heredado `prepare_test_deployment` utiliza metadata actual y modifica
configuración de despliegue; no usarlo como respaldo previo de una base que aún
no tenga 0025. Este ajuste no necesita transferir imágenes ni cambiar configuración.

## Activación autorizada

1. Confirmar un único head: `python -m alembic heads`.
2. Con una conexión temporal de migración y dentro de la ventana autorizada,
   aplicar `python -m alembic upgrade 20261002_0025`. El rol de ejecución de la
   API permanece limitado, sin permisos DDL. No ejecutar Alembic al arrancar.
3. Comprobar columna, índice, revisión y conservación de los valores anteriores.
   Iniciar/publicar la API revisada y después CLIENTES mediante el procedimiento
   de `deployment-runbook.md`. No activar otros trabajos pendientes históricos.
4. Verificar acceso normal, aislamiento, listado de mesas y contratos existentes.
   La comprobación de PWA usa `node scripts/verify-pwa-build.mjs --url <URL_POS>`
   desde CLIENTES; conserva nombre, id, ámbito y tema de la app.

## Recuperación específica del folio #65

1. Consultar el pedido por folio en el negocio/sucursal autorizados y comprobar
   su ID real. El folio no es un ID global y no se sustituye por `orders/65`.
2. Leer su detalle nuevo: versión, mesa, ocupación actual, pagos confirmados,
   saldo, fecha de liberación y cierre. Si la mesa tiene otra cuenta activa,
   detener la recuperación de ocupación y revisar ambos pedidos.
3. Si ya está liberada, no escribir. Si el saldo no es cero, no registrar pagos
   inventados ni usar una lista vacía. Si está pagada y aún ocupada, usar
   **Liberar mesa** desde Pedidos o Mesas.
4. Si falta el cierre, `/table-checkout/start` lo confirma con versión y clave
   idempotente. Puede preparar una cuenta impresa para ese ciclo: coordinarlo
   antes de ejecutar la recuperación real. Conservar la clave ante incertidumbre.
5. `/table-checkout/pay` usa la versión recién confirmada y `payments: []`.
   Comprobar liberación y pagos originales sin duplicados; comandas pendientes
   de cocina permanecen. Un reintento recupera el resultado, no otra impresión.

Recuperación realizada el 2026-10-02 desde **Liberar mesa** del POS publicado:
folio #65, ID real 66, negocio/sucursal 2/2, mesa ID 22 (Mesa 1). Lectura previa:
pedido versión 14, mesa versión 4, una sola cuenta vigente, S/ 33.50 pagados
en dos pagos confirmados (S/ 3.50 cash y S/ 30.00 Yape), saldo cero y sin cierre
ni liberación. Lectura posterior: pedido versión 16, mesa versión 5, cierre y
liberación confirmados, mesa `available`. Los dos pagos y la comanda permanecen
exactamente iguales. Se agregó únicamente un `customer_receipt`, pendiente:
la impresora configurada Xprinter XP-E200L no está disponible en este equipo.
No se reintentó despacho, no se cambió la impresora ni se imprimió otra comanda.
Reportes privados `recovery-65-before.json` y `recovery-65-after.json`.

## Verificación registrada

- CLIENTES: suite final completa 814 pruebas; lint y build correctos. El archivo
  completo de Pedidos incluye 52 pruebas. Iconos y manifest verificados tanto en
  fuentes como en dist.
- API: suite completa 871 pruebas y 8 omitidas; entre ellas, tres pruebas nuevas
  de concurrencia que requieren PostgreSQL aislado.
  La corrección posterior de ocupación con pedidos históricos entregados pasa
  junto con las pruebas de cobro y archivado (28 pruebas).
- Migración aislada: mantiene filas, valores, restricciones y referencias; repetir
  upgrade es compatible con el bootstrap que usa metadata actual.
- Navegador aislado: 15 combinaciones de cinco casos en escritorio, tablet y móvil,
  con borrador/guardar/salir, zonas con mesas, editor, cobro y marca.
- PWA: cinco PNG, hashes de originales/derivados, metadata y manifest verificados.
  La producción publicada pasó el verificador anónimo: manifest, cinco PNG,
  dos logos originales, raíz y 14 rutas SPA coinciden con el build local.
  La instalación física y la actualización del icono por el sistema operativo
  quedan por comprobar tras publicar; no se cambia la identidad instalada.
- Revisión visual: logos legibles y controles de borrado accesibles en las tres
  resoluciones. El detector Impeccable informó advertencias del CSS existente
  y su documentación de diseño desactualizada; no se amplió este ajuste para
  rediseñar otras pantallas.

Despliegues comprobados: Render `dep-db0065btqb8s73e19lug` / `3162a64` Live;
Vercel `dpl_ALYHKd59VNWEPjxCqTa7YMCoThgU` / `9ff1601` Ready / Production / Current.
La API pública responde health 200 y expone el nuevo DELETE de mesas.
Auto-Deploy de Render permanece Off; no se modificó configuración de servicios.

## Reversión

Conservar el respaldo y el código previo. La migración es aditiva y su downgrade
no borra el marcador ni su índice. Restaurar código antiguo tras haber archivado
recursos podría mostrarlos otra vez: detener operaciones y revisar compatibilidad
antes de hacerlo. No desarchivar filas, borrar historial ni restaurar una base
antigua sobre escrituras nuevas como solución automática.
