# Pizza House: continuidad de una pizza por mitades

Activado el 2026-10-01 para negocio 2, sucursal 2.

## Correccion

En el borrador actual, despues de pedir Americana, «quiero una familiar,
pero se me antojo que sea mitad vegetariana, se puede?» conserva Americana
en la otra mitad. El agente reconoce una Pizza combinada Familiar de Americana
y Vegetariana, conserva delivery y pregunta por el siguiente dato necesario.
Invalida el preview anterior y vuelve a obtener el importe del POS.

Si existen varias pizzas o una combinada previa, solicita la aclaracion necesaria
sin reemplazar silenciosamente el carrito. Consultas, negaciones, sabores no
disponibles y catalogos incompletos no autorizan cambiar ni cobrar la pizza.
Un reemplazo completo expresamente pedido sigue siendo posible en el borrador.
Los pedidos ya registrados conservan su politica vigente de solo adiciones.

## Publicacion

- Workflow: `HstQEpRLMqONt6v4`, Agente Pizza House.
- Version anterior: `36c0e413-3143-4214-8608-b77ee0448041`.
- Version activa: `6c9675e4-38d7-4198-bb31-6a707811a3de`.
- SHA256 del payload: `b4af2712d82cc34abf30cdfbf63af2f43e74586c39dcf87f345c064cc01df03e`.
- Solo cambia el codigo de Resolve Conversation State. Permanecen 147 nodos,
  conexiones, credenciales, modelo gpt-6-luna y monitor paralelo deshabilitado.
- PUT y activacion con respaldo privado, comprobacion de ejecuciones en curso,
  estado durable de publicacion y lectura posterior de version/hash activo.
  La memoria anterior se conservo durante la publicacion.
- No hubo cambios ni despliegues de API, CLIENTES o gateway para esta correccion.

Los scripts locales de preparacion/publicacion y pruebas estan en el workspace
Impulsa, fuera del repositorio Apis. Sus artefactos y respaldos privados no se
copian a Git. El primer artefacto preparado se retiro antes de cualquier PUT:
las regresiones detectaron una interferencia con «Mitad y mitad mediana»;
el segundo artefacto corrigio esa precedencia y supero las pruebas.

## Verificacion

- 174 pruebas sobre el artefacto final: 38 focalizadas/independientes de mitades,
  44 de adiciones y ciclo de pedidos, y 92 de sabores, errores de escritura,
  seleccion directa y cambio de dia. Todas pasaron.
- Otras 95 pruebas previas de conversacion, checkout y referencia de delivery
  pasaron sobre sus respectivos fixtures y transformadores anteriores.
- Todos los programas Code compilaron. El guard de publicacion rechazo cambios
  de otros nodos, conexiones, credenciales, modelo o ajustes del payload.
- Prueba directa del webhook con un chat sintetico: saludo, carta, Americana
  para delivery, frase exacta de la captura y cambio del borrador a recojo.
  La carta emitio `pos_menu`; cada turno reporto `outcome: none`.
- Se leyo el estado durable de ese chat de prueba: producto 20, variante Familiar,
  cantidad 1 y opciones Americana 8 / Vegetariana 11. Delivery se conservo y el
  posterior cambio explicito del borrador a recojo tambien se guardo.
- El resumen validado fue una Combinada Familiar (Americana, Vegetariana),
  S/ 50.00, seguido de la pregunta Yape o efectivo.

No se eligio un pago, recibio un comprobante, registro un pedido en el POS,
genero una comanda o envio un mensaje a un consumidor durante estas pruebas.
La prueba directa de n8n no sustituye una nueva comprobacion visible desde el
telefono del consumidor. Se mantuvo deshabilitado el guardado de datos de
ejecuciones; la evidencia procede de respuestas y del estado del chat sintetico.

Evidencia privada local: `pizza-house-half-flavor-20261001-v2-receipt.json`,
artefacto/stage del mismo prefijo y `pizza-house-half-flavor-20261001-live-smoke.json`
en Apis/backups. No publicar su contenido ni las sesiones de consumidores.
