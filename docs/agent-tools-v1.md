# API del agente de restaurante · contrato 1.1.0

Las rutas existentes de `/api/v1` se conservan. Esta entrega integra la candidata
local, publica contratos de herramientas y prepara el backend para un futuro
agente de n8n. No activa ningún flujo ni conexión de WhatsApp.

## Descubrimiento y permisos

`GET /openapi.json` publica los contratos. Admins genera el paquete por credencial
desde `app/agent_tools.py`, el mismo registro usado por la autorización del perfil
`restaurant_agent`. El paquete v2 conserva `method`, `scope`, `url` y agrega
parámetros, cuerpo, respuestas, errores, scopes completos y requisitos de contexto
e idempotencia. Las herramientas sin todos los permisos necesarios no aparecen.

Admins mantiene el perfil de restaurante predeterminado: lectura de carta y
pedido propio, operaciones permitidas, registro de comprobantes, continuidad de
conversación y avisos al encargado. No concede inventario, cuentas ajenas ni
aprobación de pagos. Las notificaciones operativas usan otra conexión.

Las copias generadas están en `docs/agent-tools-v1/`. Sus IDs 1 son ejemplos;
el paquete real de Admins contiene el negocio y sucursal de cada credencial.
Regenerar sin conexión ni secretos: `python scripts/export_agent_tool_contract.py`.

## Un turno del futuro agente

1. El transporte identifica cada mensaje por el ID estable del proveedor y lo
   encola. Una redelivery conserva ese ID; dos textos iguales con IDs diferentes
   son dos mensajes legítimos. Los mensajes nuevos conservan secuencia y orden.
2. El coordinador obtiene el trabajo y un lease privado. Antes de interpretar,
   llama `POST /integrations/conversations/{conversation_id}/turn-context` con
   `lease_token`, `expected_version` y, si corresponde, `order_id`.
3. La respuesta reúne restaurante vigente, catálogo y pedido del mismo
   consumidor. Tras la primera lectura, enviar `known_catalog_version` permite
   recibir `catalog: null` y `catalog_unchanged: true`. El servidor vuelve a
   comprobar vigencia comercial, horario y disponibilidad incluso al omitirlo.
4. El modelo interpreta intención y elecciones; nunca determina precios,
   aprobación de pagos o estado confirmado. El runtime conserva los tokens de
   lease/entrega fuera del prompt del modelo.
5. Antes de una operación, valida elecciones y comprueba mensajes nuevos. Las
   operaciones usan versión y la misma clave idempotente al reintentar.
6. Guardar operación/contexto, encolar respuesta y confirmar entrega son pasos
   separados. Antes de enviar se comprueba de nuevo si llegó información nueva.
   Una entrega incierta se reconcilia; no se vuelve a enviar a ciegas.

`conversation_auth_fields`, `idempotency_location`, `idempotency_key_field` e
`idempotency_replay` describen cada ruta. No todos los POST producen efectos
comerciales y no todos usan una clave en headers. Algunas lecturas del protocolo
exigen clave para cercar el intento aunque devuelvan estado actual.

## Selección incompleta, sin efectos comerciales

`POST /integrations/orders/validate-selection` requiere `menu:read`; para el perfil
de restaurante también exige `X-Agent-Conversation` y `X-Agent-Lease` vigentes.
No crea pedido, cotización, pago, comanda o impresión, ni marca ejecución comercial.

```json
{
  "channel": "takeaway",
  "payment_method": "cash",
  "items": [{"product_id": 101, "quantity": 1}]
}
```

Si la pizza tiene tamaño y dos sabores obligatorios, devuelve `ready_for_preview:
false`, los dos campos faltantes y sus opciones. `selection_requirements` publica
la variante, grupos, mínimos, máximos, repeticiones y opciones agotadas. Los
mínimos efectivos comparten la regla usada por el validador del POS. Los extras
opcionales no se convierten en preguntas obligatorias.

La selección completa pasa después por `/orders/preview` para obtener precios
oficiales y por la operación correspondiente. Los campos de intención no aceptan
precios ni estados de pago introducidos por el consumidor.

## Contexto y estados

En el `state` existente se puede guardar exclusivamente como contexto público:

```json
{
  "consumer_context": {
    "schema_version": 1,
    "intent": "purchase",
    "selection": {"channel": "takeaway", "items": [{"product_id": 101}]},
    "order_id": 123
  }
}
```

La referencia se comprueba contra negocio, sucursal y consumidor. El contexto
público no incluye lease, cola ni metadatos privados. Precios, pagos y estados
se vuelven a leer del POS; `selection_validation` refleja requisitos vigentes.

`customer-state` mantiene separados `status`, `payment_status`, `payment_evidence`,
`payment_requests`, `tickets`, inicio/fin de cocina y `dispatch`. La asignación de
un repartidor no implica despacho. `dispatch.dispatched` considera el despacho
real incluso si la copia de `Order.status` quedó atrasada. `allowed_actions` y
sus motivos usan las mismas guardas que las operaciones.

Horario incluye zona, instante de evaluación, apertura y próxima apertura cuando
es verificable. Configuración incompleta se declara `unverified`, sin inventar
horarios. Un pedido ya aceptado conserva sus operaciones permitidas al cerrar.
Preparación, recogida y viaje no se deducen entre sí. Número, titular y QR de Yape
comparten revisión; la descarga del QR admite `expected_revision` y rechaza una
configuración que cambió.

## Reglas comerciales conservadas

- El agente ofrece efectivo o Yape entre los habilitados para el canal.
- Distrito, distancia y bandas requieren cotización humana. Envío fijo/gratis
  respeta la configuración y devuelve importes desconocidos como pendientes.
- Cambiar entrega antes del despacho aplica el cambio y deja aviso en el POS.
  Después del despacho, corresponde aviso urgente y resolución humana.
- Agregar productos conserva el pago original. Efectivo genera la comanda del
  agregado; Yape espera aprobación humana y entonces genera esa comanda.
  No se emite otra boleta por el agregado.
- Recibir un comprobante lo deja en revisión. Los datos enviados por el flujo
  son indicios; no hay segunda llamada oculta al modelo ni aprobación automática.
- Resultados incluyen IDs, versión, importes, solicitudes de pago y comandas
  cuando corresponde. Una respuesta perdida se recupera conservando la clave.

Errores de integración conservan `detail` y agregan `code`, `request_id`,
`retryable`, `issues`. Un conflicto de versión requiere refrescar contexto y
revalidar; cambiar de clave no es una forma de evadir el conflicto. Un fallo de
transporte requiere reconciliar la operación antes de decidir otro intento.

## Verificación, publicación y reversión

No cambia `models.py`, migraciones, dependencias ni configuración de Render.
No requiere migración de esquema. Render mantiene `AUTO_CREATE_SCHEMA=false`,
sin Alembic en build, predeploy o start. La identidad runtime no ejecuta DDL.
Desplegar APIs primero; después Admins/POS con sus variables Vercel de producción.
Las carpetas originales y candidatas congeladas permanecen preservadas.

La reversión de código usa las revisiones anteriores API `e8fd497`, Admins
`75ac5e4` y POS `3f80da2`, sin borrar datos ni ejecutar downgrade de base.
Los nuevos campos son aditivos y los consumidores anteriores siguen funcionando.

Reproducir PostgreSQL aislado: configurar `POS_TEST_PG_BIN` y ejecutar
`python scripts/verify_agent_tools_postgres.py`. Crea una base sintética nueva,
escucha únicamente en loopback y siempre detiene su servidor al terminar.
El ensayo visual usa `scripts/serve_agent_tools_sandbox.py` y los Vite locales;
no configura transporte externo ni impresoras. Los resultados y capturas se
guardan fuera del paquete productivo, bajo `.test-results`.

El benchmark `scripts/benchmark_agent_catalog.py` compara el código base con la
candidata, misma semilla rica, calentamiento y cinco mediciones por tamaño.
Con 50 productos: 121 → 27 SELECT; 500: 1.021 → 27 SELECT. El catálogo completo
crece aproximadamente 78% por los requisitos explícitos. Las mediciones SQLite
locales no acreditan mejora general de latencia; la omisión por revisión reduce
el contenido transferido de turnos posteriores.
