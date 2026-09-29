# DC Dashboard

Dashboard interno de Royalspace para gestionar a un partner y los media
buyers que trabajan bajo él en el negocio de dental pay-per-call. Sincroniza
payouts de CallGrid, registra gasto en ads, y calcula liquidaciones semanales
(Lunes-Domingo) con reparto 70/30 (partner/media buyer) sobre la ganancia neta,
con arrastre de déficit cuando una semana pierde dinero.

Nombrado deliberadamente en genérico (`dc-dashboard`, sin el nombre real del
partner) porque la URL pública se comparte con el media buyer.

Ver el plan de diseño completo en `/Users/alacranz/.claude/plans/starry-soaring-goblet.md`
(schema, algoritmo del motor de liquidaciones, decisiones de diseño).

**Vive dentro del repo `royalspace-automations`, como subcarpeta** — mismo
patrón que `manychat/` (su propio `railway.json`, se despliega como un
servicio de Railway independiente, con su propia base de datos Postgres).

## Stack

Python 3.12 · FastAPI · PostgreSQL · SQLAlchemy 2.0 · Alembic · Jinja2 + Tailwind CDN + HTMX (sin build de Node).

## Setup local

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Completa CALLGRID_API_KEY, CALLGRID_ORG_ID, DATABASE_URL, SESSION_SECRET_KEY,
# ADMIN_EMAIL, ADMIN_PASSWORD en .env

createdb dc_dashboard
psql -d dc_dashboard -c "CREATE EXTENSION IF NOT EXISTS btree_gist;"

export $(cat .env | xargs)   # o usa python-dotenv / direnv
alembic upgrade head
python scripts/seed.py       # crea el partner demo, DC1, el admin, y liquidaciones demo
uvicorn app.main:app --reload
```

Abre http://localhost:8000 — te redirige a `/login`.

Usuarios creados por `scripts/seed.py`:
- `ADMIN_EMAIL` / `ADMIN_PASSWORD` (los que pongas en `.env`) — ROYALSPACE_ADMIN
- Solo con `SEED_DEMO_DATA=true`: `partner@example.com` / `changeme123` — DIXON_MANAGER,
  `dc1@example.com` / `changeme123` — MEDIA_BUYER, y 3 liquidaciones demo de enero 2026

**Nunca pongas `SEED_DEMO_DATA` en Railway.** Sin ella, el seed (que corre en cada
deploy) borra los datos demo si existen — la liquidación demo bloqueada deja un
déficit de $20 que se arrastraría a la primera liquidación real de DC1.

## Verificar el motor financiero

```bash
pytest tests/ -v
```

Los 23 tests cubren los escenarios del spec original (ganancia normal, período
negativo, arrastre de déficit, cambio de tasa con fecha de vigencia, ajustes
post-liquidación sobre settlements bloqueados, redondeo, etc.) y reproducen
exacto los dos ejemplos numéricos del spec.

## Antes de confiar en la sincronización de CallGrid en producción

`app/callgrid/client.py::get_source_revenue` usa `pivot="SourceName"`, que
**no está verificado todavía contra la API real** (solo `VendorName`/`BuyerName`
fueron confirmados en el otro repo, `royalspace-automations`). Corre esto primero:

```bash
python scripts/discover_source_pivot.py
```

Si falla, inspecciona el Network tab del dashboard de CallGrid filtrando por
Source, y ajusta el valor de `pivot` en `app/callgrid/client.py`.

## Deployment en Railway

1. En el proyecto de Railway existente (el mismo donde corre `manychat`),
   clic en **"+ New"** → **"GitHub Repo"** → selecciona `royalspace-automations`
   otra vez (Railway permite varios servicios del mismo repo).
2. **Importante — evita el bug que ya tuvimos con `manychat`:** en el nuevo
   servicio, entra a **Settings → Root Directory** y ponlo en `dc-dashboard`.
   Sin esto, Railway puede intentar instalar dependencias desde el
   `requirements.txt` equivocado (el de la raíz del repo, pensado para
   `manychat`) en vez del de esta carpeta — exactamente el problema que tumbó
   el bot el 2026-09-28.
3. Agrega el addon de **PostgreSQL** a este nuevo servicio (propio, separado
   del que usa `manychat` si lo tuviera).
4. En las Variables del servicio, agrega:
   - `CALLGRID_API_KEY`, `CALLGRID_ORG_ID` — consíguelos directo de CallGrid
     (los secrets ya guardados en GitHub no se pueden volver a ver/copiar)
   - `DATABASE_URL` — Railway la genera automáticamente al agregar el addon de
     Postgres (usa la referencia `${{Postgres.DATABASE_URL}}`, cambiando el
     prefijo a `postgresql+psycopg://` si Railway te da `postgresql://`)
   - `SESSION_SECRET_KEY` — genera uno nuevo: `python3 -c "import secrets; print(secrets.token_hex(32))"`
   - `ADMIN_EMAIL`, `ADMIN_PASSWORD` — tu login inicial de ROYALSPACE_ADMIN
   - `REPORTING_TIMEZONE` — `America/New_York` (default) o el que corresponda
   - `META_ACCESS_TOKEN`, `META_API_VERSION` — opcionales, solo si quieres que
     el ad spend de algún media buyer se importe automático desde Meta Ads en
     vez de cargarlo a mano (ver sección de Meta Ads más abajo)
5. Railway corre `railway.json` en cada deploy: aplica las migraciones,
   corre el seed (idempotente — no duplica nada si ya existe), y levanta el server.
6. El resync nocturno (CallGrid + Meta Ads, últimos 7 días) corre automáticamente
   dentro del mismo proceso a las 3:00 AM (`REPORTING_TIMEZONE`) — no depende
   de ningún cron externo (mismo patrón usado en `manychat/main.py` de
   `royalspace-automations`, adoptado tras confirmar que el `schedule:` nativo
   de GitHub Actions no es confiable).

## Ad spend automático desde Meta Ads

Por partner (ej. Dixon), cada media buyer puede tener su propia cuenta de
Meta Ads. Para activar la importación automática:

1. La persona que va a leer los datos (ej. tú) necesita acceso de al menos
   **"Ver rendimiento"** sobre la cuenta de anuncios del media buyer —
   pídelo directo (Business Settings → Cuentas → Solicitar acceso a una
   cuenta publicitaria, si el dueño no tiene Business Manager, esto le
   manda una solicitud que aprueba desde su propio Ads Manager).
2. Genera un Access Token con permiso `ads_read` que tenga ese acceso, y
   ponlo como `META_ACCESS_TOKEN` en las variables de Railway.
3. En `/admin/media-buyers`, en la columna "Cuenta de Meta Ads", pega el ID
   numérico de la cuenta (sin el prefijo `act_`) para ese media buyer.
4. Desde esa noche en adelante el gasto de ese media buyer se sincroniza
   solo. Para forzarlo ya mismo: botón "Sincronizar desde Meta ahora" en
   `/admin/ad-spend`.

Un media buyer sin cuenta de Meta configurada sigue usando la carga manual
sin ningún problema — ambos métodos conviven (`app/services/ad_spend.py`).

## Fecha de inicio del media buyer

`/admin/media-buyers` → "Fecha de inicio". Antes de esa fecha nada cuenta en
sus liquidaciones: ni payout, ni ad spend (ej. gasto de pruebas en su cuenta
de Meta), ni déficit arrastrado de una semana anterior; el sync de Meta
tampoco importa esos días. La semana en la que empieza se liquida solo desde
ese día. DC1 empieza el 2026-10-01 (lo setea la migración `c3d51e7a9b20`).

## Estructura

Ver el árbol completo y el detalle del schema/motor de liquidaciones en el
plan de diseño. Resumen rápido:

- `app/models.py` — schema completo (dinero en centavos enteros, nunca float)
- `app/services/settlement_engine.py` — el núcleo financiero
- `app/callgrid/` — cliente + sincronización con CallGrid
- `app/meta/` — cliente + sincronización con Meta Ads (ad spend automático)
- `app/routers/` — rutas por rol (admin, partner, media_buyer)
- `migrations/` — Alembic, incluye las restricciones `EXCLUDE USING gist` de Postgres
- `tests/test_settlement_engine.py` — suite de correctitud financiera

## Fases futuras (no implementadas todavía)

- Webhooks de CallGrid (hoy solo hay sync periódico + manual)
- Notificaciones
- Soporte para partners adicionales además del primero
