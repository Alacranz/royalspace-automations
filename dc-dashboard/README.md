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

Usuarios demo creados por `scripts/seed.py`:
- `ADMIN_EMAIL` / `ADMIN_PASSWORD` (los que pongas en `.env`) — ROYALSPACE_ADMIN
- `partner@example.com` / `changeme123` — DIXON_MANAGER (rol interno; el login real créalo desde /admin/users)
- `dc1@example.com` / `changeme123` — MEDIA_BUYER

**Cambia esas contraseñas demo antes de usar esto en producción con datos reales.**

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
5. Railway corre `railway.json` en cada deploy: aplica las migraciones,
   corre el seed (idempotente — no duplica nada si ya existe), y levanta el server.
6. El resync nocturno de CallGrid (últimos 7 días) corre automáticamente
   dentro del mismo proceso a las 3:00 AM (`REPORTING_TIMEZONE`) — no depende
   de ningún cron externo (mismo patrón usado en `manychat/main.py` de
   `royalspace-automations`, adoptado tras confirmar que el `schedule:` nativo
   de GitHub Actions no es confiable).

## Estructura

Ver el árbol completo y el detalle del schema/motor de liquidaciones en el
plan de diseño. Resumen rápido:

- `app/models.py` — schema completo (dinero en centavos enteros, nunca float)
- `app/services/settlement_engine.py` — el núcleo financiero
- `app/callgrid/` — cliente + sincronización con CallGrid
- `app/routers/` — rutas por rol (admin, partner, media_buyer)
- `migrations/` — Alembic, incluye las restricciones `EXCLUDE USING gist` de Postgres
- `tests/test_settlement_engine.py` — suite de correctitud financiera

## Fases futuras (no implementadas todavía)

- Webhooks de CallGrid (hoy solo hay sync periódico + manual)
- Integración con Meta Marketing API para gasto en ads automático
  (`app/services/ad_spend.py::AdSpendProvider` ya está preparado para esto)
- Notificaciones
- Soporte para partners adicionales además del primero
