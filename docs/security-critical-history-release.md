# Historial de seguridad: correccion de acciones criticas

Fecha: 2026-10-04. Alcance: API y CLIENTES. Estado: implementado y verificado;
publicacion pendiente de autorizacion expresa. No requiere migracion.

## Diagnostico y evidencia de solo lectura

- La base conserva eventos recientes. Al revisar, el ultimo evento era
  `cash.cut_created` 1558, corte 7, del 4 de octubre a las 00:19 America/Lima.
  No habia eventos posteriores que la vista pudiera recuperar.
- Los siete cortes guardados tenian diferencias por metodo y no pertenecian a
  ninguna categoria sensible en la proyeccion anterior. El ultimo tenia
  diferencia de efectivo -67 y tarjeta 0.
- Se confirmaron las relaciones de sucursal de los eventos de cortes,
  movimientos, cancelaciones y revisiones examinados. No se corrigieron ni
  fabricaron filas historicas.
- Las acciones ordinarias de impresion, cobros y cierre de mesa competian con
  las sensibles. Los endpoints de movimientos/cortes tampoco avisaban al socket
  despues del commit; Seguridad no actualizaba al recibir eventos o volver al POS.

## Cambios

- `critical_only=true` es aditivo y filtra antes de contar o paginar. Sin el
  parametro se conserva el listado completo. Contrato detallado en
  `security-audit-contract.md`.
- Se reconocen cortes con diferencias por cada metodo contado y devoluciones
  usando un metodo distinto de los cobros originales. Una diferencia neta cero
  no oculta discrepancias de efectivo/tarjeta. Los gastos son salidas de caja.
- Nuevos cortes y devoluciones congelan los datos requeridos en el evento de
  auditoria transaccional. Historial antiguo usa evidencia guardada y relaciones
  verificadas del mismo negocio/sucursal, nunca importes actuales reconstruidos.
- Movimientos y cortes emiten la notificacion de sucursal despues del commit.
  Reintentos idempotentes y operaciones revertidas no emiten otro evento.
- CLIENTES conserva los ultimos datos confirmados ante un fallo de actualizacion,
  informa del error y permite reintentar. Errores de acceso retiran datos privados.
  La actualizacion automatica se limita a la vista visible y agrupa avisos;
  Actualizar historial vuelve a la primera pagina respetando filtros.
- El enlace de corte comprueba corte, caja y sucursal; no abre otro recurso ni
  escribe datos. Volver al historial restaura filtros, pagina y foco.
- Auditoria completa e historial existentes se conservan. No se modifica el
  agente, n8n, QR, Admins, credenciales ni contratos de integracion.

## Verificacion

- API completa: **906 passed, 16 skipped**. Las omisiones requieren sus entornos
  externos especificos; esta correccion no modifica esquema ni bloqueo financiero.
- Seleccion de Seguridad y Caja: **67 passed**, incluidos 11 nuevos casos de
  paginacion por encima de 200, categorias, diferencias opuestas, instantaneas,
  permisos, aislamiento, orden historico y emisiones posteriores al commit.
- CLIENTES completo: **858 pruebas en 81 archivos**, todas correctas.
- Comprobacion final de componentes afectados: **81 pruebas en cuatro archivos**,
  todas correctas, incluyendo actualizacion, datos confirmados, errores de acceso,
  respuestas tardias y enlaces de corte de otra caja/sucursal.
- Playwright: los cuatro flujos de Seguridad pasan en escritorio, tablet y movil
  (**12 casos**). La ultima correccion fue del test de navegacion: usa el titulo
  real del modal de corte y lo cierra antes de pulsar Regresar.
- Capturas de datos aislados revisadas en
  `CLIENTES/e2e/test-output/security-zones/`: listado y detalle de corte en
  escritorio/movil, sin desbordamientos del contenido de auditoria.
- ESLint y build TypeScript/Vite correctos. Persiste el aviso previo del bundle
  principal mayor de 500 kB; no se introduce una refactorizacion ajena.
- Revision independiente del diff no encontro defectos accionables. `git diff
  --check` sin errores.

Una ejecucion exploratoria de todo `security-zone-navigation.spec.ts` incluyo
el test anterior de impresion de folios. Su fixture no intercepta el endpoint
actual de impresion manual y falla; no se cambio impresion para resolverlo.
Los cuatro flujos de Seguridad se ejecutaron con seleccion especifica y pasan.

## Respaldo y activacion

Respaldo privado `Apis/backups/deployment-20261004T230402Z`: datos de las 52 tablas
de negocio (3839 filas en esa instantanea), configuracion y archivos subidos.
La revision operativa sigue en `20261004_0026`. La preparacion generica se detuvo
despues de esos respaldos al intentar exportar configuracion QZ desde rutas
locales no disponibles; no se altero la identidad publicada ni configuracion.
Esta correccion no necesita cambiar variables de entorno o identidad QZ.

Tras recibir autorizacion, publicar primero API y comprobar el contrato nuevo;
despues CLIENTES. Mantener auto-deploy de Render desactivado y las configuraciones
operativas existentes. Verificacion publicada de solo lectura; no crear cortes,
retiros, pedidos o impresiones reales para probar Seguridad.

Recuperacion compatible: volver a los commits previos de API/CLIENTES si falla
la activacion. Las instantaneas aditivas de auditoria son compatibles con el codigo
anterior y no requieren revertir filas o migraciones. La lectura en navegador
publicado queda pendiente; la conexion de control del navegador no respondio
durante esta verificacion local.
