# Homologacion de n8n con Escalar AI POS

## Alcance

El workflow local `Agente de Escalar AI - Homologacion` (`yxkVU1GUqlPsWod1`) es la copia aislada para validar el cambio desde Fudo hacia Escalar AI POS. Permanece inactivo y usa el webhook de prueba `escalar-ai-pos-homologacion`.

El workflow productivo y el gateway QR no se modifican durante la homologacion. El corte se realiza unicamente despues de superar las pruebas funcionales y de seguridad.

## Configuracion de n8n

Configurar estas variables en n8n, sin escribir secretos dentro de los nodos:

- `ESCALAR_POS_API_BASE`: URL publica terminada en `/api/v1`.
- `ESCALAR_POS_API_TOKEN`: token opaco emitido para una sola sucursal.
- `ESCALAR_RESTAURANT_NAME`: nombre comercial mostrado al cliente.

El token se muestra una sola vez al crearlo desde Admins. La base de datos conserva solo su hash.

Scopes recomendados para el agente conversacional:

- `menu:read`
- `inventory:read`
- `orders:read`
- `orders:write`
- `payments:write`
- `reservations:write`
- `events:read`

No conceder `inventory:write` al agente que atiende consumidores. Los ajustes de stock deben ejecutarse desde el POS por usuarios autorizados o mediante una credencial administrativa separada.

## Contrato operativo

- El gateway sigue enviando el payload normalizado actual.
- La respuesta sincronica conserva exclusivamente `actions[]`.
- Los pedidos en efectivo se confirman y envian a cocina con cobro pendiente.
- Yape exige una imagen binaria real. La evidencia queda bajo revision humana y no se envia a cocina hasta ser aprobada.
- El monitor de eventos consulta eventos durables, envia la notificacion de WhatsApp y confirma cada `event_id` para evitar duplicados.
- Las escrituras usan `Idempotency-Key` estable por mensaje, pedido o evento.
- Las tools conectadas al agente son de solo lectura. Las escrituras pasan por validaciones deterministas fuera del modelo.

## Corte controlado

1. Crear una credencial de integracion para la sucursal con los scopes anteriores.
2. Configurar las variables de n8n y desplegar la API con sus migraciones.
3. Probar saludo, carta, agotados, delivery, ubicacion, efectivo, Yape con y sin imagen, reservas y prompt injection contra el webhook de homologacion.
4. Aprobar y rechazar una evidencia desde CLIENTES, comprobando que WhatsApp reciba un solo evento.
5. Verificar que un fallo de escritura nunca produzca una confirmacion verbal.
6. Cambiar el webhook del restaurante solamente despues de aprobar la homologacion.
7. Mantener el workflow anterior disponible para rollback durante la ventana inicial.

Cada restaurante recibira una copia completa de la plantilla validada. Solo cambian el tenant, la credencial de sucursal y las variables de identidad del restaurante.
