# Conversaciones completas de Pizza House

## Alcance y decisiones

- Workflow existente `HstQEpRLMqONt6v4`; modelo, memoria, credenciales y monitor
  duplicado deshabilitado se conservan. Sin cambios en CLIENTES, Admins o impresion.
- Decision posterior del usuario: el agente solo toma delivery y para llevar/recojo.
  Comer en el local sigue disponible en el POS manual. No se convierte una peticion
  de consumo en local en recojo sin eleccion del consumidor.
- Tono cercano, breve, normalmente una pregunta y como maximo un emoji pertinente.
  El resumen enumera cantidades/productos, importe confirmado y modalidad/destino.
  Envio por cotizar se muestra aparte, no como gratuito ni total definitivo.
- Modalidad, pago, productos y destino se conservan con referencia al mensaje del
  consumidor. Las alternativas sugeridas quedan separadas de la seleccion.
- La misma guarda comprueba integridad antes de QR, efectivo, imagen y creacion.
  La API revalida seleccion, disponibilidad y precio. Comprobante no es aprobacion.
- Solo una nueva compra explicita o la finalizacion confirmada y otra solicitud de
  compra abre una compra limpia. La carta sola no crea venta. Despachado pregunta
  una vez por otra compra. Preparado no significa entregado.

## API compatible, sin migracion

`POST /integrations/orders/draft` exige `channel` explicito. Para `delivery` se
requiere direccion de al menos cinco caracteres, enlace Maps HTTPS reconocido o
coordenadas validas, y referencia de entrega no vacia. Devuelve 422 con codigo
`ORDER_CHANNEL_REQUIRED`, `DELIVERY_DESTINATION_REQUIRED` o
`DELIVERY_REFERENCE_REQUIRED`. No cambia el formulario manual ni pagos historicos.
La consulta idempotente de una creacion previamente confirmada sigue funcionando.

## Gateway

- Mantiene 3500 ms de silencio. Une textos contiguos que esperan un turno lento;
  capturas y ubicaciones son barreras ordenadas, conservando sus bytes y datos.
- La preparacion de archivos tambien se serializa por chat para que una descarga
  lenta no permita que otro texto adelante al comprobante.
- `context.turnId` deriva de restaurante, remitente e IDs originales;
  `context.messages` conserva cada texto. Respuesta `turn:{id,outcome}` indica
  `none`, `committed` o `uncertain`. Solo informacion superada con `none` y el mismo
  turno puede suprimirse. Resultados de escritura inciertos no se reintentan.
- Se apaga escribiendo antes del envio y tambien en errores. Un unico relay conserva
  activacion y entregas. No se envian ni reconocen eventos historicos.

## Verificacion local

- 787 Pytest aprobados; cinco pruebas opcionales PostgreSQL omitidas en esta corrida.
  Las pruebas usan SQLite aislado; no se ejecutaron migraciones en la base de uso.
- 94 pruebas gateway/workflow aprobadas, TypeScript y build correctos.
  Incluyen modalidad inventada, Americana/Yape sin sustituto aceptado, media/destino,
  rafagas durante respuesta lenta, chats simultaneos, estados terminales, proteccion
  de confirmaciones y regresiones previas de comprobantes, relay y escritura.
- Fixture saneada de la version publicada anterior, sin datos de conversacion ni
  secretos: `tests/fixtures/pizza-house-published.json` en el gateway.
- Respaldo completo y recibo de publicacion en `Apis/backups/pizza-complete-*`,
  excluidos de Git. No copiar estos respaldos a un repositorio.

## Activacion

Primero publicar esta API en el servicio existente, luego el workflow preparado y
gateway compilado. El script verifica version publicada y ausencia de ejecuciones
activas antes del PUT y no repite una publicacion incierta. No sobrescribe staticData.
Las pruebas de webhook con identidades tecnicas no equivalen a verificar la recepcion
visual en un telefono. WhatsApp necesita el equipo/gateway encendidos y vinculados.

## Resultado del 2026-09-20

- API publicada: `d3853dd700f2d73a42ab5c1de0a48b452adf5262`, repositorio
  `claudioreyp/automatizaciondb`, rama `agent/escalar-ai-pos-api`.
  Render `dep-dao6ag3m8hqs73den2vg` en el servicio existente, sin migracion ni
  cambios de configuracion. Health HTTPS 200 y rechazo real 422
  `ORDER_CHANNEL_REQUIRED` con cuerpo incompleto, sin crear pedido.
- Workflow activo: `e6d442a7-0a92-4762-a48a-a43947bdc3e7`. Verificado contra el
  artefacto preparado; 105 nodos, mismas credenciales/modelo/memoria y monitor
  duplicado deshabilitado. La publicacion no sobrescribe conversaciones.
- Nueve turnos tecnicos finales via webhook autenticado: alternativa Americana
  no aceptada, modalidad ausente, rechazo de comer en local, recojo, direccion y
  referencia de delivery, envio por cotizar, carta y bloqueo de consulta ajena.
  El QR solo salio tras completar seleccion/modalidad/destino; bytes identicos al
  endpoint privado del POS. Cada respuesta devolvio su turno y `outcome=none`.
  No se enviaron comprobantes, mensajes reales, cobros, pedidos ni impresiones.
- Durante la comprobacion se corrigieron dos regresiones antes de reconectar el
  gateway: referencias dinamicas `$(name)` bloqueaban el task runner al calcular
  el resultado; ahora se generan referencias literales. La guarda de cotizacion
  se restringe a la ruta de pago para no interceptar mensajes antes del agente.
  Hay pruebas para ambos casos. No se repitieron escrituras inciertas.
- Gateway compilado y reiniciado solamente en `127.0.0.1:3008`. Registro de relay
  identico antes/despues por SHA256, sin cambiar activacion ni borrar sesiones.
  Codigo y fixture permanecen en el workspace del gateway; este directorio no
  tiene un repositorio Git configurado, no se afirma una publicacion Git de el.
- Se revisaron los archivos nuevos contra los secretos privados conocidos: sin
  coincidencias. Respaldos y recibos de prueba permanecen excluidos de Git.

## Limites pendientes

- WhatsApp pide escaneo en `http://127.0.0.1:3008/api/bots/impulsa/qr?format=view`.
  Gateway saludable no equivale a telefono vinculado. Falta la conversacion
  controlada y comprobacion visual en el telefono tras ese escaneo.
- Las cinco pruebas PostgreSQL opcionales no se ejecutaron: no habia URL de
  pruebas aisladas configurada y Docker Desktop no tenia el motor iniciado.
  No se uso la base de produccion como sustituto. No hay migracion en esta tarea.
- Efectivo, comprobantes, adiciones, continuidad y concurrencia se verificaron
  con servicios/datos aislados; no se crearon nuevos pedidos reales para probarlos.
- Las guardas conservan decisiones respaldadas; borradores antiguos sin evidencia
  requieren aclaracion antes de pagar. Pedidos ya registrados no se reescriben.
- La calidad conversacional requiere seguir observando ejemplos nuevos; estas
  pruebas no garantizan que toda expresion posible se interprete perfectamente.
