# Activación de marca, cobro de mesas y archivado

Fecha de preparación: 2026-10-02. Cambios implementados en CLIENTES y Apis.
Las decisiones de producto permanecen en `../../AGENTS.md`.

## Estado y autorización

La migración `20261002_0025` agrega únicamente `restaurant_tables.archived_at`
y su índice. Su revisión anterior es `20260920_0024`. No se aplicó a la base
operativa, no se publicaron aplicaciones y no se recuperó todavía el folio #65.
Esas operaciones requieren autorización específica del usuario, como establece
el plan aprobado. Las pruebas usan SQLite aislado, respuestas HTTP simuladas y
transporte de impresión simulado; no crearon pedidos, cobros ni impresiones reales.

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

## Verificación registrada

- CLIENTES: suite completa 811 pruebas; lint y build correctos. Las regresiones
  posteriores del botón de recuperación y concurrencia se verificaron junto con
  el archivo completo de Pedidos: 52 pruebas correctas. Lint y build finales
  también correctos.
- API: suite completa 871 pruebas y 8 omitidas; entre ellas, tres pruebas nuevas
  de concurrencia que requieren PostgreSQL aislado.
  La corrección posterior de ocupación con pedidos históricos entregados pasa
  junto con las pruebas de cobro y archivado (28 pruebas).
- Migración aislada: mantiene filas, valores, restricciones y referencias; repetir
  upgrade es compatible con el bootstrap que usa metadata actual.
- Navegador aislado: 15 combinaciones de cinco casos en escritorio, tablet y móvil,
  con borrador/guardar/salir, zonas con mesas, editor, cobro y marca.
- PWA: cinco PNG, hashes de originales/derivados, metadata y manifest verificados.
  La instalación física y la actualización del icono por el sistema operativo
  quedan por comprobar tras publicar; no se cambia la identidad instalada.
- Revisión visual: logos legibles y controles de borrado accesibles en las tres
  resoluciones. El detector Impeccable informó advertencias del CSS existente
  y su documentación de diseño desactualizada; no se amplió este ajuste para
  rediseñar otras pantallas.

## Reversión

Conservar el respaldo y el código previo. La migración es aditiva y su downgrade
no borra el marcador ni su índice. Restaurar código antiguo tras haber archivado
recursos podría mostrarlos otra vez: detener operaciones y revisar compatibilidad
antes de hacerlo. No desarchivar filas, borrar historial ni restaurar una base
antigua sobre escrituras nuevas como solución automática.
