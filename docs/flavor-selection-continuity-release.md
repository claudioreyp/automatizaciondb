# Cambio de sabores y recuperacion de conversacion — Pizza House

## Requisito y diagnostico

Correccion del 2026-09-27 para negocio 2, sucursal 2 y workflow
`HstQEpRLMqONt6v4`. La captura reportada muestra una pregunta pendiente sobre
Americana y Peperoni, un saludo posterior y la nueva peticion «Quiero una diabla
con boloñesa». El agente repite la pregunta de los sabores anteriores.

Se reprodujo el fallo sobre la version activa
`dd8f3ed9-11bb-487f-bd92-69f67e97e9bc`, con el catalogo actual del POS. La
evidencia de conversacion conserva la peticion nueva, pero el interprete sigue
priorizando la pregunta pendiente anterior. Incluso «Combinada» despues de esa
peticion materializa Americana y Peperoni en el borrador de conversacion.

Una lectura del estado real confirma una sesion de compra sin venta registrada
y sin productos aceptados, con la pregunta vieja y la peticion nueva guardada.
La prueba de recuperacion conserva esa forma de datos y sustituye identidades,
nombre e identificadores por valores tecnicos; no se publica informacion del chat.

## Criterios de correccion

- Una nueva seleccion explicita se interpreta con los sabores actuales y el
  catalogo vigente. Una pregunta de aclaracion no debe imponer una seleccion
  anterior sobre una peticion nueva.
- Diabla con Boloñesa, sin indicar composicion, pregunta combinada o dos pizzas
  de esos sabores. La siguiente respuesta «Combiana mediana» elige una combinada
  mediana de Diabla y Boloñesa.
- El estado ya atascado recupera la peticion explicita posterior desde su
  evidencia de conversacion al recibir otro turno, sin repetir mensajes reales
  ni editar manualmente la memoria del workflow.
- El ultimo pedido explicito prevalece; una peticion antigua no vuelve a
  imponerse sobre una posterior. Consultas y negaciones no se convierten en una
  seleccion de compra. La disponibilidad se vuelve a comprobar contra el POS.
- Se conservan otros productos aceptados, la separacion entre compra nueva y
  pedido registrado, la confirmacion de dos pizzas con tres sabores y las
  correcciones anteriores de carta y respuestas breves.

## Verificacion y publicacion

### Pruebas del artefacto

Se aprobaron 128 pruebas, sin fallos ni omisiones: 95 regresiones anteriores de
carta, interpretacion, pago y modalidad, y 33 casos nuevos. Una revision
independiente aprobo 14 comprobaciones adicionales. Se verificaron, entre otros:

- Cambio de Americana/Peperoni a Diabla/Boloñesa, incluso compartiendo un sabor
  con la seleccion anterior o terminando el mensaje con un signo de pregunta.
- «Combiana mediana» despues del cambio; dos pizzas identicas con tres sabores
  mantienen su propuesta y exigen la confirmacion correspondiente.
- Recuperacion de la sesion afectada, con sus datos estructurales anonimizados,
  usando el normalizador real y dos mensajes agrupados. El ID del encabezado no
  coincide con las hojas originales: aun asi selecciona el producto 20,
  Mediana, cantidad 1 y modificadores 12/14, nunca 8/10.
- Ultima declaracion posterior prevalece, incluidos cambios de cantidad y una
  vuelta explicita a los sabores iniciales. Sin el ID de origen no se infiere
  una sustitucion historica.
- Consultas y charla no eliminan la aclaracion ni autorizan cobrar otros
  productos de un carrito ya completo en efectivo. Un sabor desconocido, una
  combinada con un solo sabor o un producto adicional sin interpretar mantienen
  bloqueada la confirmacion; no se omiten silenciosamente.
- Pedidos registrados y escrituras inciertas conservan sus protecciones.

La lectura pasiva inmediatamente anterior a publicar confirmo que el chat
afectado seguia sin venta ni productos aceptados, con la pregunta anterior y la
peticion nueva conservadas. No se repitieron mensajes ni se edito su memoria.

### Version publicada

Se publico una sola vez la version
`7d7a20fd-5551-445d-b5c7-4271cf423126`. La lectura posterior confirma que el
workflow activo y su borrador coinciden con el artefacto aprobado:

- SHA256 del artefacto: `e1198c814b5593aecf2a4b316866c2a49f992a4829938409eb136cb20f316991`.
- SHA256 del transformador: `5f602fb0feb44abda912afdf810fbe0e6d162c0c7f024f8cc8993f6113d84ccf`.
- Solo cambia `Resolve Conversation State`. Se conservan los 147 nodos,
  conexiones, ajustes, credenciales y `gpt-6-luna`. El monitor paralelo permanece
  deshabilitado; no se reinicio el gateway ni se publico la API o CLIENTES.
- Los respaldos inmediatamente anterior y posterior al PUT tienen todo el
  `staticData` exactamente igual, por comparacion profunda y huella; las ocho
  sesiones existentes permanecen. Una comprobacion independiente verifico ambos
  respaldos contra sus huellas guardadas.

El recibo y los respaldos privados estan en
`Apis/backups/pizza-house-flavor-replacement-dd8f3ed9-v1-*`, excluidos de Git.
Las fuentes de pruebas y scripts se identifican por sus huellas en el recibo.

### Comprobacion del workflow activo

Cuatro turnos consecutivos del mismo remitente tecnico pasaron en el webhook
activo, una sola vez cada uno: Americana/Peperoni, Hola, Diabla/Boloñesa y
«Combiana mediana». Todos devolvieron HTTP 200, una respuesta de texto y resultado
`none`; se conservaron los IDs de turno. La lectura del estado final confirma una
Pizza combinada Mediana, cantidad 1, modificadores 12/14 y sin aclaracion de
composicion pendiente.

Respuestas verificadas para los ultimos dos turnos:

> ¿Quieres una pizza combinada de Diabla y Boloñesa, o dos pizzas, una de cada sabor? 🍕

> Entendí una Pizza combinada Mediana de Diabla y Boloñesa. ¿Lo recoges o prefieres delivery? 🛵

Se consulto el catalogo actual del POS. La prueba no creo pedidos, pagos,
comprobantes, comandas ni mensajes reales de WhatsApp: las acciones del webhook
se recibieron directamente, sin entregarlas al gateway. La prueba estructural
del chat atascado acredita su recuperacion programatica; no sustituye una
respuesta visible posterior desde ese telefono.

La memoria cambia normalmente durante estos turnos tecnicos. La afirmacion de
preservacion exacta corresponde a los respaldos inmediatos de la publicacion,
no a una comparacion con el estado posterior a nuevas conversaciones.
