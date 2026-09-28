# Carta y respuesta breve de pizza combinada — Pizza House

## Alcance y causas

Correccion del 2026-09-27 para el workflow `HstQEpRLMqONt6v4`, negocio 2,
sucursal 2. Se revisaron las dos conversaciones reportadas por el usuario.

- `Por favor envíeme la carta` no se reconocia como solicitud de imagen. El
  flujo tenia tres copias del detector con diferencias entre ellas. El fallo
  se reprodujo sobre la version activa anterior; no se atribuye a una ejecucion
  historica cuya traza ya no esta retenida.
- `Combiana mediana` no resolvia la pregunta pendiente sobre Americana y Peperoni.
  Ademas, el tamano incluido en una respuesta breve no se incorporaba a la
  eleccion guardada.

## Correccion

- Las tres copias del detector usan el mismo reconocimiento de solicitudes
  naturales y formales. Se mantienen las distinciones entre pedir una imagen,
  consultar productos de la carta, rechazar su envio y pedir carta junto con
  productos.
- Una solicitud exclusiva de carta conserva el pedido, modalidad, pago y reserva
  pendientes y se limita a consultar. Las barreras previas al checkout impiden
  registrar pedidos, cobros, cambios o reservas aunque el modelo clasifique mal
  esa consulta. La salida contiene una sola accion de carta; no agrega una
  afirmacion falsa de que no se pueden adjuntar las imagenes.
- Solo ante una pregunta pendiente de composicion, `combiana` se interpreta como
  `combinada`. Se conservan los sabores confirmados y se aplica el tamano del
  mensaje usando las variantes del catalogo. No se inventan sabores sin contexto.
- Dos pizzas con tres sabores siguen proponiendo dos combinadas identicas y
  requieren aceptar esa interpretacion. Negaciones, pizzas separadas, compras
  nuevas y cambios de disponibilidad mantienen sus controles.
- La galeria sigue descargandose desde endpoints privados del POS con la
  credencial de negocio/sucursal. La primera imagen lleva caption y las demas
  no; no se modifican el gateway ni su relay.

## Verificacion y limites

La lectura real del POS confirmo negocio/sucursal 2/2, una imagen de carta valida
de tipo WEBP y 194972 bytes, y disponibilidad de la combinada, Mediana, Americana
y Peperoni. Esta comprobacion fue de lectura, sin pedidos, pagos ni WhatsApp real.

Las pruebas aisladas cubren la secuencia pedido nuevo → recojo → carta formal,
el estado completo antes y despues de la consulta, respuestas breves, dos y tres
sabores, cantidad, negaciones, variantes no disponibles y las reglas anteriores
de compra y modificaciones de pedidos.

- 95 pruebas pasan, sin omisiones, con el artefacto combinado seleccionado mediante
  `PIZZA_HOUSE_CURRENT_WORKFLOW`. Incluyen 20 de respuestas breves de sabores,
  34 regresiones de intencion, 19 de modificaciones de pedidos y 22 de carta y
  transporte de sus imagenes.
- La revision independiente repitio 27 pruebas sobre el mismo artefacto. La
  matriz de carta incluye 72 combinaciones de metodo, estado, intencion incorrecta
  del modelo y estado de reserva, comprobando el estado despues de la salida.
- El diff exacto contiene cinco nodos de codigo, sin crear nodos ni cambiar
  conexiones, ajustes, modelos o referencias de credenciales. Todos los nodos
  de codigo del artefacto compilan.

No se publican API ni CLIENTES para este ajuste y no hay migraciones. Las
modificaciones previas de pedidos, comprobantes, comandas y avisos del POS se
conservan. No se marcan pagos ficticios como verdaderos ni se generan comandas
de prueba en el restaurante.

## Publicacion

Publicacion unica verificada. Version activa y borrador:
`dd8f3ed9-11bb-487f-bd92-69f67e97e9bc`.

SHA256 del artefacto:
`f78b0a4f0fdd1c429982bf77c7dc5f98da5a8698fdc592c98c89993b3a7c658d`.

La lectura posterior coincide con el artefacto revisado: 147 nodos, 24 referencias
de la credencial POS limitada, `gpt-6-luna` y monitor paralelo deshabilitado. La
comparacion profunda y por hash de todo `staticData` inmediatamente antes y
despues de publicar confirma igualdad completa, incluidas las siete sesiones
activas de compra existentes. No se reinicia ni desvincula WhatsApp.

El respaldo previo, la lectura inmediata anterior al PUT, la lectura posterior
y el recibo privados se conservan fuera de Git. El marcador de escritura se
guarda antes de enviar el PUT y bloquea repetir una publicacion incierta. No se
incluyen secretos en este documento.

La comprobacion tecnica posterior paso cinco turnos HTTP 200 contra el webhook
activo. Cada secuencia uso una misma identidad sintetica `@lid` y conservo el ID
de su turno:

- Pedido nuevo → para llevar → `Por favor envíeme la carta`: una sola accion
  `pos_menu`, sin texto falso de error y conservando `TAKEAWAY`.
- `Quiero una americana con peperoni` → `Combiana mediana`: una sola Pizza
  combinada Mediana con dos sabores en el borrador de conversacion, sin pregunta
  pendiente de composicion. La respuesta fue: «Entendí una Pizza combinada
  Mediana de Americana y Peperoni. ¿Lo recoges o prefieres delivery? 🛵».

Los cinco turnos tuvieron `outcome=none`: cero pedidos solicitados, pagos,
comprobantes enviados o mensajes a WhatsApp real. Las llamadas tecnicas devolvieron
acciones que ningun gateway consumio. La prueba de carta valida la accion del
workflow y la descarga real de sus bytes se comprobo por separado; no equivale
a una nueva confirmacion visual de entrega en el telefono.

La lectura final confirma la misma version y hash activos, y el gateway local
con salud HTTP 200, WhatsApp conectado y barrera de sincronizacion desactivada.
La memoria cambia normalmente despues de los turnos tecnicos; la igualdad
profunda documentada corresponde a la lectura inmediata tras publicar, antes
de esas conversaciones, y su evidencia privada se conserva.
