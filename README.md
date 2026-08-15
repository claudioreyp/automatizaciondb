# Impulsa Restaurant POS API

Backend multiempresa para el POS de restaurantes de Impulsa. Expone una API FastAPI versionada, protege cada operación por negocio y sucursal, conserva compatibilidad temporal con el workflow actual y deja contratos idempotentes para una futura integración con n8n.

## Alcance implementado

- Supabase Auth con membresías y roles `superadmin`, `owner`, `manager`, `cashier`, `waiter`, `kitchen` y `dispatcher`.
- Negocios, sucursales, módulos, invitaciones y auditoría administrativa.
- Catálogo, variantes, modificadores, ingredientes, recetas y movimientos de stock.
- Pedidos por canal, mesas, comandas/KDS, pagos parciales y división de cuenta.
- Cajas, turnos, ingresos, retiros y cierre declarado.
- Reservas, delivery, clientes, evidencia Yape/Plin y reportes diarios en `America/Lima`.
- WebSocket por sucursal para actualización en tiempo real, con polling como respaldo en los paneles.
- Integraciones con token de servicio e `Idempotency-Key`.
- OpenAPI en `/docs` y health check en `/api/v1/health`.

SUNAT, conciliación bancaria, cobro transaccional y despacho con terceros quedan fuera de esta fase. Una captura Yape/Plin es evidencia provisional y requiere aprobación humana por defecto.

## Desarrollo local

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
alembic upgrade head
python -m scripts.seed_demo
uvicorn main:app --reload --port 8000
```

El seed crea `Bazar pizzas`, una sucursal, mesas, productos, inventario, recetas y caja. El modo de desarrollo acepta `X-Dev-Auth`; nunca configure `DEV_AUTH_TOKEN` en producción.

## Supabase y producción

1. Cree el proyecto Supabase y use su PostgreSQL como `DATABASE_URL`.
2. Configure `SUPABASE_URL`, `SUPABASE_JWKS_URL` y `SUPABASE_SERVICE_ROLE_KEY` únicamente en Render.
3. Cree un bucket privado para evidencias de pago y archivos sensibles. Las claves de servicio nunca deben llegar a Vercel.
4. Deje `AUTO_CREATE_SCHEMA=false` y ejecute `alembic upgrade head` antes del arranque.
5. Restrinja `CORS_ORIGINS` a los dominios reales de ambos paneles.
6. Cree un usuario en Supabase Auth y vincúlelo como primer superadmin:

```powershell
python -m scripts.bootstrap_superadmin --auth-user-id UUID_DE_SUPABASE --email admin@dominio.com --name "Administrador Impulsa"
```

`render.yaml` contiene el blueprint de despliegue sin secretos. Render ejecuta Alembic en predeploy y luego inicia Uvicorn.

## Integración futura con n8n

Las lecturas de contexto están bajo `/api/v1/integrations/context/*`. Toda escritura de integración exige:

```http
X-Integration-Token: <secret>
Idempotency-Key: <uuid-unico-por-operacion>
```

Esto evita que reintentos de WhatsApp creen dos pedidos, dos confirmaciones o dos reservas. El token se guarda en credenciales de n8n, nunca en el navegador.

Durante la transición siguen disponibles:

- `GET /api/datos/negocios`
- `GET /api/datos/restaurantes_perfiles`
- `POST /api/datos/pedidos_draft`

La creación o eliminación arbitraria de tablas está deshabilitada. No elimine las tablas antiguas hasta verificar paneles, migración y workflow en producción.

## Migración y pruebas

```powershell
python -m scripts.migrate_legacy
pytest -q
python -m compileall app scripts tests
```

El orden recomendado es: desplegar API compatible, ejecutar migraciones no destructivas, validar datos, desplegar paneles y recién después endurecer los endpoints legacy.
