# Historial de seguridad: correccion de acciones criticas

Fecha: 2026-10-04. Alcance: API y CLIENTES. Estado: implementado y verificado;
publicacion expresamente autorizada por el usuario el 2026-10-04. API y CLIENTES
publicados y verificados mediante lecturas publicas y autenticadas de Pizza House.
No requiere migracion.

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

Commits publicados en sus ramas remotas: API
`0d07e8b696e3f557c09c1aec3cddf24dca6a373c` y CLIENTES
`06b2b8052083f2b2c95e4775bffdd400d1f30e2d`. Render confirma Live en
`dep-db1e60c9v7es73f58v7g`, iniciado a las 18:36:34 America/Lima y listo a las
18:37:39. Auto-deploy sigue apagado; no se cambiaron comandos, variables,
instancias, credenciales ni la revision Alembic 0026.

Lecturas publicas despues del despliegue: salud HTTP 200, auditoria/listado,
detalle 1558 y corte 7 sin sesion HTTP 401. OpenAPI ya expone `critical_only`
booleano con valor predeterminado false y las seis categorias criticas.

GitHub y Vercel confirman la construccion CLIENTES completada para el SHA exacto,
despliegue `Cfc4pkebdrjPHeG8f6N4vAYNPjzr`, Ready, entorno Production y dominio
actual `pos.escalarai.tech`, iniciado a las 18:42:09 America/Lima (22 segundos).
No hizo falta otra promocion ni modificar proyecto, dominio o configuracion.

El archivo principal `index-CStR-LtM.js` conserva su hash; esta correccion vive
en los chunks de carga diferida. La comprobacion inicial solo del principal no
era suficiente para identificar la version. El GET del dominio de
`SettingsWorkspace-DZ3EeyMw.js` confirma HTTP 200 y contiene `critical_only`,
las categorias nuevas, el enlace `cash_cut` y Actualizar historial. Vercel
identifica tambien `Operations-BS-XXwjA.js` y `settings-Bc-5ZTB8.js` en su build.
Una revision publica independiente confirma que el archivo principal referencia
los tres chunks del build; no son archivos nuevos sin uso por la aplicacion.

Lectura autenticada completada con la cuenta del propietario de Pizza House:

- Seguridad muestra 30 acciones criticas, 10 por pagina, y los seis filtros.
  La primera es el corte con diferencias del 4 de octubre a las 00:19 Lima;
  se observan tambien cancelaciones/reducciones y el retiro del 3 de octubre
  a las 19:17. Las operaciones ordinarias no compiten en esta primera pagina.
- Actualizar historial completa sin error y conserva el listado confirmado.
- El evento 1558 abre `/caja?register_id=2&cut_id=7`: corte #7 en Caja principal,
  efectivo contado 222.50, esperado 289.50 y diferencia -67.00; tarjeta 0/0,
  transferencias esperadas 543.00. No se cambia ningun valor guardado.
- Cerrar el detalle y Regresar al historial recupera pagina 1 y el foco del
  evento 1558. No se pulsaron impresion ni controles de escritura.
- Evidencia privada de la publicacion y de la lectura real en
  `Apis/backups/security-release-proof-20261004/`.

La primera sesion abierta era de superadministrador sin negocio elegido y la
API rechazo ese contexto, una guardia anterior a Seguridad. Se cerro solo esa
sesion del navegador integrado y el usuario inicio alli la cuenta del propietario.
El acceso correcto confirma Pizza House; no se eligio un negocio supuesto,
no se ampliaron permisos ni se modifico Auth. La sesion independiente de Edge
no se altero; su conexion de control agotaba el tiempo de espera.

Recuperacion compatible: volver a los commits previos de API/CLIENTES si falla
la activacion. Las instantaneas aditivas de auditoria son compatibles con el codigo
anterior y no requieren revertir filas o migraciones. El usuario inicio sesion
en Render/Vercel y el navegador integrado permitio verificar la publicacion;
no se extrajeron credenciales del navegador. No se ejecutaron migraciones,
operaciones financieras, pedidos ni impresiones reales durante esta activacion.
