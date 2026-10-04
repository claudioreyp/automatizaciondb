# Caja: verificación y activación preparada

Fecha: 2026-10-04. Alcance: CLIENTES y Caja/cancelación en Apis.

## Implementación

- Preview determina actividad por existencia de cobros/movimientos. Fondo positivo
  exige contar efectivo, pero no habilita repetir un corte sin operaciones.
- Efectivo y tarjeta activos empiezan vacíos y obligatorios; cero explícito es
  válido. Inactivos quedan en cero deshabilitado. Transferencias son informativas.
- La API valida identidad y versión del período bajo bloqueo, conserva conteo
  a ciegas y concilia cada método con Decimal. CLIENTES no compensa diferencias
  opuestas para mostrar un corte cuadrado.
- Nuevas escrituras de Caja guardan huella y respuesta idempotente. El cliente
  retiene cuerpo, ruta y clave ante respuesta incierta; no crea otra operación.
- Cancelación con cobros registra la devolución exacta en la caja activa elegida,
  después de confirmar motivo, método(s), período y devolución realizada.
  El POS registra la devolución; no ejecuta transferencias bancarias.
- Pagos originales, total y cortes anteriores permanecen. Histórico sin devolución
  enlazada muestra **Reembolso no registrado**. Se mantienen `/transition`,
  contratos de integración y eventos existentes. No hay impresiones automáticas
  nuevas: el desglose del corte se imprime manualmente.

## Casos numéricos verificados

| Caso | Resultado |
|---|---|
| Fondo 10 + cobro 468 + entrada 10 − retiro 15 | Efectivo esperado 473 |
| Contado 234 frente a esperado 473 | Diferencia −239 |
| Tarjeta: cobro 468 − devolución 468; contado 234 | Esperado 0; diferencia +234 |
| Devolución aislada de tarjeta 234; contado 45 | Esperado −234; diferencia +279 |
| Contado 230; esperado 234; fondo conservado 10 | Diferencia −4; retirado al cerrar 220 |
| Faltante y sobrante iguales en métodos diferentes | Con diferencias; neto cero no es cuadrado |
| Cobro y devolución en el período con neto cero | Conteo del método obligatorio |
| Solo transferencias | Efectivo/tarjeta deshabilitados; sin conteo de transferencias |

## Retiro reportado de ayer: consulta de solo lectura

Se comprobó el movimiento **#2**, retiro de **S/ 3.00**, creado el 3 de octubre
a las 19:17 (America/Lima), caja #2 y corte #7 de Pizza House (negocio/sucursal
2/2). Sus referencias de auditoría son #1271 (movimiento) y #1558 (corte).

El valor guardado coincide con el libro:

`100.00 + 192.50 − 3.00 = 289.50`

La salida se restó correctamente en ese corte. La presentación anterior mostraba
el importe sin signo y sin distinguir visualmente la retirada. Se corrige esa
presentación; no se reescribió el movimiento ni el corte histórico. La consulta
no determina la cantidad física de dinero contada por el restaurante.

## PostgreSQL y conservación

Se usó PostgreSQL **17.11**, temporal en loopback, sin servicio instalado ni
conexión de pruebas a la base operativa. Las consultas ORM se califican mediante
`schema_translate_map`. Cada ensayo crea un esquema con UUID propio, comprueba
aislamiento y retira exclusivamente ese esquema.

Ocho pruebas PostgreSQL pasan:

1. Cuatro cancelaciones concurrentes con la misma clave producen una devolución,
   una cancelación y la misma respuesta.
2. Cobro contra cancelación: una sola operación gana; no se cobra un cancelado ni
   se devuelve un importe distinto del efectivamente cobrado.
3. Cobro con caja implícita contra cierre con caja bloqueada: el cobro pasa al
   período nuevo sin bloqueo circular.
4. Devolución con período obsoleto: rechazo y rollback completo de cancelación,
   devolución y auditoría.
5. Migración aditiva, repetida, conserva todas las columnas y filas anteriores.
6. Reintentos concurrentes del mismo corte crean un solo corte y período sucesor.
7. Reintentos concurrentes del mismo retiro crean un solo movimiento.
8. Restauración integral del respaldo y migración en copia: **53 tablas, 3840 filas**,
   con **106 comparaciones** de huellas antes/después. Se distingue SQL NULL de
   JSON null. Las nuevas referencias `order_id` permanecen NULL inicialmente.

La prueba 3 reprodujo un bloqueo real en el código anterior: el bloqueo de
sucursal `FOR UPDATE` impedía el `KEY SHARE` de la FK al insertar el período
sucesor. Se conserva la serialización usando `FOR NO KEY UPDATE` en sucursal;
la selección explícita sigue bloqueando su caja y vuelve a cargar el período.

## Respaldo privado y migración preparada

`Apis/backups/cash-release-20261004T215339536292Z/` contiene el snapshot de base,
configuración privada, medios locales, consulta del retiro y SQL generado desde
la migración ensayada. Está excluido de Git; no distribuir sus archivos privados.
La preparación abrió una transacción `REPEATABLE READ / READ ONLY` y verificó
las huellas serializadas. La restauración se ejecutó solamente en PostgreSQL
aislado; no se copiaron datos a otro servicio externo.

Migración: `20261004_0026_cash_order_refunds.py`, sobre `20261002_0025`.
Agrega columna nullable, FK e índices en `cash_movements`; no modifica registros
existentes. El downgrade conserva el libro de devoluciones deliberadamente.

## Pruebas de regresión

- API: ejecución general **909 pruebas aprobadas**; las otras dos únicamente
  esperaban la cabecera anterior 0025. Se actualizaron a 0026 y se volvieron a
  ejecutar ambas, **2 aprobadas**. Total verificado: **911**, incluidas las ocho
  PostgreSQL, contratos de integración, mesas, cocina, impresión y permisos.
- Python: compilación de módulos de aplicación y preparación del respaldo correcta.
- CLIENTES: **840 pruebas aprobadas en 81 archivos**, lint y build correctos.
  Vite conserva el aviso de tamaño de un bundle superior a 500 kB; no se cambió
  la distribución de módulos fuera de esta corrección.
- Navegador con API interceptada: **9 casos de Caja y 3 de cancelación aprobados**
  entre escritorio, tablet y móvil. Verifican conteo, movimiento negativo,
  devolución separada, diferencias por método e impresión con todos los desgloses.
  Una comprobación de impresión usaba el diálogo oculto en media print; se corrigió
  el selector al artículo imprimible y los tres tamaños aprobaron sin nuevas capturas.
- Revisión visual conjunta completada en dos rondas acotadas. El formulario pasa
  a una columna en móvil; la tabla del detalle conserva desplazamiento horizontal
  para consultar Contado / Monto esperado / Diferencia. Las capturas de revisión
  quedan localmente en `CLIENTES/.impeccable/review/` y usan datos de prueba.
- El detector de diseño se ejecutó una vez: sus avisos de paleta en colores de
  error/reembolso y de la tipografía Inter corresponden a la estética operativa
  conservada. No son fallos de cálculo o permisos; no se amplió el alcance para
  reescribir la identidad visual ni los documentos de diseño.
- La revisión independiente del código de cancelación, devoluciones, permisos,
  conservación e idempotencia no encontró defectos accionables.

## Activación autorizada

El usuario autorizó específicamente la migración 0026 y la publicación de
API/CLIENTES el 2026-10-04, después de la verificación. La activación está en curso.
No se modificaron Admins, n8n, gateway, credenciales ni registros operativos.

Con autorización:

1. Coordinar una ventana sin cobros, cortes, cancelaciones ni movimientos en curso.
2. Repetir `python -m scripts.prepare_cash_release` para obtener un respaldo fresco
   y comprobar la versión operativa 0025; no reutilizar a ciegas un respaldo viejo.
3. Aplicar 0026 en una transacción autorizada, con timeout de bloqueo, usando la
   credencial administrativa temporal. El runtime limitado conserva sus permisos,
   RLS y credenciales; no requiere acceso directo del navegador a tablas.
4. Comprobar todas las columnas/filas originales contra ese respaldo, los índices,
   FK, versión y referencias nuevas NULL. Si falla antes del commit, hacer rollback.
5. Publicar API y después CLIENTES, conservando contratos y variables existentes.
6. Verificar lecturas de preview/detalles/permisos en el entorno activado. Escrituras
   reales de prueba requieren un caso controlado acordado con el restaurante.
7. Ante regresión, revertir el código; conservar la migración y devoluciones ya
   registradas. No restaurar una base vieja sobre escrituras nuevas ni eliminar
   filas para regresar a 0025.

La impresión se verifica en navegador con modo de impresión simulado; no se
envió papel a una impresora física ni se crearon pedidos reales de prueba.
