# Consumo del medidor: consulta de solo lectura

`GET /api/v1/admin/agent-usage` exige el permiso actual `superadmin`. No reserva,
liquida ni modifica consumo, conversaciones, avisos o datos comerciales.

Parámetros: `from` y `to` ISO8601 con offset, intervalo `[from,to)`; `timezone`
IANA (por defecto `America/Lima`); `business_id`, `branch_id`, `model_id`
opcionales; `interval=day|hour`; `group_by=branch|business|model`; `page=1` y
`page_size=25` (máximo100). Máximo31 días, o7 para horas. Una sucursal sola
delimita también su negocio; una combinación incompatible se rechaza.

Respuesta:

- `metadata`: periodo UTC, zona, intervalo, agrupación, `generated_at`,
  `complete:true`, `available_models` (periodo/ámbito antes de filtrar modelo).
- `totals`: intentos, llamadas por estado, turnos y tokens.
- `pricing_buckets`: todos los consumos conocidos, agrupados por modelo y
  `input_band=up_to_272000|over_272000`. No contiene tarifas ni dólares.
- `series`: periodos con datos, con `bucket_start`, `bucket_end`, métricas y sus
  propios `pricing_buckets` completos.
- `breakdown`: página agregada, `total_rows`, `total_pages`, `rows`; nombres de
  negocio/sucursal o modelo, métricas y precios-base de cada fila. La paginación
  no limita `totals`, `series` ni los buckets globales.

Métricas: `attempts`, `settled_known_calls`, `settled_unknown_calls`,
`uncertain_calls`, `reserved_calls`, `denied_calls`, `model_turns`,
`known_input_tokens` (sin caché), `known_cache_read_input_tokens`,
`known_cache_creation_input_tokens`, `known_output_tokens`, `known_total_tokens`,
`uncertain_reserved_tokens`, `open_reserved_tokens`,
`settled_unknown_reserved_tokens`, `quota_accounted_tokens`.

Los turnos son identidades distintas conversación/mensaje dentro del medidor;
pueden contener varios mensajes de WhatsApp. No se suman turnos de modelos para
obtener el total. Un intento reservado no prueba que se envió al proveedor; uno
incierto no conoce el gasto real. `quota_accounted_tokens` mezcla consumo
conocido y reservas conservadas: no son tokens facturados.

Cada bucket de estimación contiene `model_id`, `input_band`, `known_calls`,
`input_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`,
`output_tokens`. La banda se calcula por llamada antes de agregar: entrada sin
caché + lectura de caché + creación de caché, estrictamente mayor que272000.
Admins aplica únicamente las reglas que correspondan al modelo. Uso incierto,
reservas y consumos sin tarifa no equivalen a cero dólares. Los precios son una
estimación externa, no una factura ni una lectura de saldo de OpenAI.

La fecha es la de reserva (`created_at`), no la de liquidación. Se filtran tanto
las jornadas UTC indexadas como la fecha exacta, y se agrupa en la zona elegida.
No se devuelven remitentes, conversaciones, mensajes, credenciales o huellas.

Protección de lectura: transacción PostgreSQL `READ ONLY`, `REPEATABLE READ`,
presupuesto total de5 segundos, como máximo2 informes simultáneos por proceso,
4000 grupos/modelos y6000 buckets de estimación por gráfico/página. Caché privada
en memoria30 segundos,128 entradas, autorización previa en cada solicitud y
`Cache-Control:no-store`. No se cachean errores ni se devuelven resultados
parciales. Al exceder los límites,503 y se debe reducir el periodo/ámbito.
La caché se pierde al reiniciar y no cambia tablas ni permisos.

No hay migraciones ni índices nuevos. Los índices actuales por negocio/jornada
permiten consultas acotadas; no se promete una consulta global rápida sobre
50 millones de filas. Ese volumen podría requerir una ampliación posterior.
