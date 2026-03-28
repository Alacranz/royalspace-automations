# Royalspace 2026 — Contexto del Proyecto

Este archivo le da a Claude todo el contexto necesario para trabajar en este repositorio
sin necesidad de re-explicar la empresa, la estructura ni las integraciones.

---

## La Empresa

**Royalspace** es una empresa de marketing de performance en el vertical **dental PPC**.
Genera ingresos conectando llamadas de pacientes dentales con clínicas a través de Ringba.
Los media buyers corren campañas de Meta Ads con objetivo de **mensaje** (no conversión web),
que derivan en llamadas trackeadas por Ringba.

**Repositorio GitHub:** https://github.com/Alacranz/royalspace-automations

---

## Módulos del Sistema

El proyecto tiene dos sistemas de automatización completamente independientes:

| Módulo | Carpeta | Propósito |
|---|---|---|
| Monitor de Profit | `profit/` | Ringba + Meta Ads → Discord (cada 30 min) |
| Asistencia | `asistencia/` | Jibble → Discord + Excel (diario y mensual) |

---

## Estructura de Archivos

```
royalspace-automations/
├── CLAUDE.md                          ← este archivo
├── .gitignore
├── .github/
│   └── workflows/
│       ├── profit_true_profit.yml         # cada 30 min, L-V 8-20 / S 8-14 EST
│       ├── profit_mb_daily_summary.yml    # L-V 8:00 AM EST (resumen de ayer)
│       ├── asistencia_diaria.yml          # L-V 10:30 AM VET
│       └── asistencia_mensual.yml         # día 1 de cada mes, 10:00 AM VET
│
├── profit/
│   ├── config.json                    ← configuración sin secretos (subido al repo)
│   ├── requirements.txt               ← requests>=2.31, pytz>=2024.1
│   ├── true_profit.py                 ← main: True Profit + MB Alerts (3 mensajes)
│   ├── mb_daily_summary.py            ← resumen diario del día anterior (2 mensajes)
│   └── common/
│       ├── __init__.py
│       ├── business_hours.py          ← verificación de horario EST
│       ├── discord_client.py          ← send(webhook, message)
│       ├── meta_client.py             ← Meta Ads Graph API
│       └── ringba_client.py           ← Ringba calllogs API
│
└── asistencia/
    ├── requirements.txt               ← requests>=2.31, pytz>=2024.1, openpyxl>=3.1
    ├── jibble_client.py               ← Jibble OAuth + time entries
    ├── daily_report.py                ← reporte diario 10:30 AM VET
    └── monthly_report.py              ← reporte mensual con Excel
```

**Ignorado por git (no subir nunca):**
- `AUTOMATIZACIONES/` — scripts PowerShell originales (referencia histórica, local only)
- `automatizacion/` — carpeta legacy
- `profit/state/` — archivos de estado en tiempo de ejecución
- `asistencia/Asistencia_*.xlsx` — reportes generados
- `.env`, `__pycache__/`, `.venv/`

---

## Timezones

| Sistema | Timezone | Pytz |
|---|---|---|
| Profit (Ringba, Meta, horario laboral) | Eastern Time (EST/EDT) | `America/New_York` |
| Asistencia (Jibble, horario Venezuela) | Venezuela Standard Time (VET, UTC-4) | `America/Caracas` |

> El config.json usa `"timezone": "America/New_York"` (pytz).
> Windows usaba `"Eastern Standard Time"` — son equivalentes.

---

## Sistema de Profit

### APIs usadas

**Ringba**
- Base URL: `https://api.ringba.com/v2`
- Auth: `Authorization: Token {RINGBA_API_TOKEN}`
- Endpoint principal: `POST /v2/{account_id}/calllogs`
- Body: `{ reportStart, reportEnd, size: 1000, offset }`
- Paginación: hasta 100 páginas de 1000 registros
- Campos relevantes por registro: `publisherName`, `payoutAmount`, `conversionAmount`, `profitNet`, `hasConnected`, `hasConverted`

**Meta Ads Graph API**
- URL: `https://graph.facebook.com/{version}/act_{id}/insights`
- Auth: query param `access_token`
- Params: `fields=spend`, `date_preset=today|yesterday`, `level=account`
- Retorna: `data[0].spend` (string, convertir a float)

**Discord Webhooks**
- Método: `POST` con `Content-Type: application/json`, body `{ "content": "..." }`
- Límite: 2000 caracteres por mensaje; el código trunca a 1900 con `\n...`
- Formato de mensajes: bloques de código (triple backtick) con tablas de texto monoespaciado

### Estructura de config.json

```json
{
  "company_name": "Royalspace",
  "timezone": "America/New_York",
  "accounts_private_groups": [
    {
      "group_name": "Royalspace Private",
      "publishers": ["you", "T.I Angela Monroy"],
      "facebook_ad_account_id": "act_266001198433130",
      "active": true
    }
  ],
  "media_buyers": [
    {
      "display_name": "Nombre para Discord",
      "publisher_name": "Nombre exacto en Ringba",
      "facebook_ad_account_id": "act_XXXXXXXXXXXXXXXXX",
      "royalspace_spend_share": 0.5,
      "media_buyer_spend_share": 0.5,
      "category": "internal|external",
      "active": true
    }
  ]
}
```

### Media Buyers actuales

**Internos (category: "internal") — 6 buyers:**

| display_name | publisher_name (Ringba) | Meta Ad Account |
|---|---|---|
| Edixon Cordova | T.I Edixon Cordova Royalspace | act_1253430079671063 |
| Esteban Ramirez | T.I Esteban Ramirez Royalspace | act_393472116969724 |
| Luis Salas | T.I Luis Salas | act_444438538017708 |
| Clara Castro | T.I Clara Castro | act_1013839483941679 |
| Douglas Contreras | T.I Douglas Contreras Royalspace | act_449298417453786 |
| Kevin Pernia | T.I Kevin Pernia | act_1059538342347336 |

**Externos (category: "external") — 2 buyers:**

| display_name | publisher_name (Ringba) | Meta Ad Account |
|---|---|---|
| Sebastian Reyes | Sebastian Reyes | act_5618895554900759 |
| Caribay Flores | Caribay Flores | act_602432986039588 |

**Private Group (no es MB, es cuenta propia de RS):**
- Publishers: "you", "T.I Angela Monroy"
- Meta Ad Account: act_266001198433130

### Fórmulas de Profit

```
mb_share_amt  = spend × media_buyer_spend_share
mb_profit     = payout - mb_share_amt
rs_profit     = (revenue - payout) - (spend × royalspace_spend_share)
combined_net  = rs_total + mb_mb_profit_total
```

Donde:
- `payout` = suma de `payoutAmount` en Ringba
- `revenue` = suma de `conversionAmount` en Ringba
- `spend` = Meta Ads spend del ad account del MB

### Clasificación de Status MB (true_profit.py)

| Estado | Condición |
|---|---|
| CRITICAL | mb_profit ≤ -$10 |
| NEGATIVE | mb_profit < $0 |
| LOW | mb_profit < $11 |
| PROFITABLE | mb_profit ≥ $11 |

### Normalización de nombres (Ringba)

```python
normalize_name(name) → strip() → remove "^\(\d+\)\s*" → lower()
```

Ejemplo: `"(123) T.I Edixon Cordova Royalspace"` → `"t.i edixon cordova royalspace"`

### Horario laboral (business_hours.py)

- Lunes–Viernes: 08:00–19:59 EST
- Sábado: 08:00–13:59 EST
- Domingo: nunca
- Variable `FORCE_RUN=true` bypasea el check (para pruebas manuales via `workflow_dispatch`)

### Workflows de Profit

**profit_true_profit.yml**
- Cron: `*/30 * * * *` (cada 30 min, GitHub Actions UTC)
- El script verifica horario internamente con `is_business_hours()`
- `workflow_dispatch` pasa `FORCE_RUN=true` automáticamente
- Envía 3 mensajes por run:
  1. True Profit completo → `DISCORD_WEBHOOK_MOD` (#mod)
  2. MB Internal Performance → `DISCORD_WEBHOOK_MB_INTERNAL` (#mb-alerts interno)
  3. MB External Performance → `DISCORD_WEBHOOK_MB_EXTERNAL` (servidor externo)

**profit_mb_daily_summary.yml**
- Cron: `0 13 * * 1-5` (8:00 AM EST, lunes a viernes)
- Sin verificación de horario — corre siempre
- Usa `date_preset=yesterday` en Meta y rango de ayer en Ringba
- Envía 2 mensajes:
  1. MB INTERNAL DAILY SUMMARY - DD/MM/YYYY → `DISCORD_WEBHOOK_MB_INTERNAL`
  2. MB EXTERNAL DAILY SUMMARY - DD/MM/YYYY → `DISCORD_WEBHOOK_MB_EXTERNAL`
- Columnas: Name | Payout | Spend | MB Share | Profit (sin Status, sin Revenue)

---

## Sistema de Asistencia

### API Jibble

- **Token:** `POST https://identity.prod.jibble.io/connect/token`
  - Body form-urlencoded: `grant_type=client_credentials`, `client_id`, `client_secret`
- **Personas:** `GET https://workspace.prod.jibble.io/v1/People`
  - Header: `Authorization: Bearer {token}`
  - Query: `$filter=organizationId eq '{org_id}'`
- **Entradas de tiempo:** `GET https://time-tracking.prod.jibble.io/v1/TimeEntries`
  - Paginación: `$skip`, `$top=200`
  - Ordenamiento: `$orderby=createdAt desc` (diario) o `$orderby=time desc` (mensual)

**Campos importantes por entrada:**
- `personId` — ID del empleado
- `type` — `"In"` | `"Out"` | etc.
- `time` — timestamp de la entrada (puede ser UTC o local, ver parse_time_smart)
- `createdAt` — cuándo se registró en el sistema

### Resolución de ambigüedad UTC/local (parse_time_smart)

Jibble puede retornar timestamps en UTC o en hora local (Venezuela). El código prueba ambas
interpretaciones y elige la que caiga en rango razonable (06:00–13:00 VET):

```python
parse_time_smart(s, prefer_utc_fallback=False)
# prefer_utc_fallback=False → prefiere interpretación local (daily_report.py)
# prefer_utc_fallback=True  → prefiere conversión UTC→VET (monthly_report.py)
```

### Clasificación de asistencia

**Diario (daily_report.py):**

| Categoría | Rango de entrada | Delta desde 09:00 |
|---|---|---|
| A TIEMPO | 08:30 – 09:15 VET | negativo o pequeño |
| TARDE | 09:16 – 11:00 VET | positivo |
| FUERA DE RANGO | después de 11:00 VET | — |
| SIN MARCAR | sin entrada "In" | — |

- Excluidos: empleados cuyo nombre normalizado contiene `"edwar"`
- Solo se toma la **primera** entrada "In" del día

**Mensual (monthly_report.py):**

| Estado | Condición | Puntos |
|---|---|---|
| ONTIME | entrada ≤ 09:15 VET | 0.0 |
| LATE | entrada 09:16–11:00 VET | -1.5 |
| OUT | entrada después de 11:00 VET | -2.0 |
| MISSING | sin entrada "In" | -2.0 |
| (fin de semana) | sábado / domingo | omitido, sin penalidad |

- Excluidos: "edwar", "angela vanesa", "sebastian reyes", "angelica flores"
- El Excel se genera con openpyxl, colores: verde/amarillo/amarillo oscuro/rojo pastel/azul header
- Se sube como GitHub Actions artifact (retención 90 días)
- Se envía al canal Discord como archivo adjunto (multipart form)

### Workflows de Asistencia

**asistencia_diaria.yml**
- Cron: `30 14 * * 1-5` → 10:30 AM VET (UTC-4), lunes a viernes
- Envía a: `DISCORD_WEBHOOK_ASISTENCIA`

**asistencia_mensual.yml**
- Cron: `0 14 1 * *` → 10:00 AM VET, día 1 de cada mes
- Genera Excel `Asistencia_{Mes}_{Año}.xlsx`
- Sube artifact y envía archivo a Discord

---

## Secrets de GitHub Actions

Todos los secretos viven en **GitHub → Settings → Secrets and variables → Actions**.
**Nunca van en el código ni en config.json.**

### Profit

| Secret | Descripción |
|---|---|
| `RINGBA_API_TOKEN` | Token de API de Ringba |
| `RINGBA_ACCOUNT_ID` | ID de cuenta Ringba |
| `META_ACCESS_TOKEN` | Token de acceso Meta Ads (Graph API) |
| `DISCORD_WEBHOOK_MOD` | Webhook Discord canal #mod |
| `DISCORD_WEBHOOK_MB_INTERNAL` | Webhook Discord canal #mb-alerts interno |
| `DISCORD_WEBHOOK_MB_EXTERNAL` | Webhook Discord servidor externo MB |

### Asistencia

| Secret | Descripción |
|---|---|
| `JIBBLE_CLIENT_ID` | OAuth client ID de Jibble |
| `JIBBLE_CLIENT_SECRET` | OAuth client secret de Jibble |
| `JIBBLE_ORG_ID` | ID de organización en Jibble |
| `DISCORD_WEBHOOK_ASISTENCIA` | Webhook Discord canal #asistencia-ti |

### Variables (no secretos)

| Variable | Valor por defecto | Descripción |
|---|---|---|
| `META_API_VERSION` | `v25.0` | Versión de la Graph API de Meta |

---

## Convenciones del Proyecto

- **Python 3.12** en todos los workflows
- **pytz** para timezones (no `zoneinfo` — compatibilidad con 3.8+)
- **requests** para todas las llamadas HTTP (sin httpx ni aiohttp)
- **openpyxl** solo en asistencia (no instalar en profit)
- Los scripts de profit importan módulos comunes via `sys.path.insert(0, os.path.dirname(__file__))`
- Formato monetario: `f"${v:.2f}"` — siempre dos decimales, sin separador de miles
- Truncar mensajes Discord a 1900 chars (`discord_client.send` lo hace automáticamente)
- Ordenamiento de tablas: por `rs_profit` desc (true_profit) o por `profit` desc (daily_summary) o por `mb_profit` desc (mb_alerts)

---

## Cómo Añadir un Nuevo Media Buyer

1. Editar `profit/config.json` — agregar entrada en `media_buyers` con:
   - `display_name`: nombre para Discord (máx. 20 caracteres visibles)
   - `publisher_name`: nombre exacto como aparece en Ringba
   - `facebook_ad_account_id`: con prefijo `act_`
   - `royalspace_spend_share` y `media_buyer_spend_share`: típicamente 0.5 cada uno
   - `category`: `"internal"` o `"external"`
2. Si tiene cuenta privada propia, agregar también en `accounts_private_groups`
3. Agregar su `facebook_ad_account_id` a los permisos del `META_ACCESS_TOKEN`
4. No se necesita cambiar ningún script Python

## Cómo Hacer Pruebas Manuales

- Ir a **GitHub → Actions → [nombre del workflow] → Run workflow**
- Para `profit_true_profit.yml`: pasa `FORCE_RUN=true` automáticamente (bypasea horario)
- Para los demás: no tienen restricción de horario, corren siempre
- Los logs del run muestran cada paso (fetch Meta, fetch Ringba, envío Discord)

---

## Contexto Histórico

Los scripts originales eran **PowerShell (.ps1)** corriendo en **Windows Task Scheduler**
en una PC física de la empresa. La migración a GitHub Actions elimina esa dependencia.

Carpeta `AUTOMATIZACIONES/` (local, no en repo): contiene los .ps1 originales como referencia.
- `royalspace_true_profit.ps1` → `profit/true_profit.py`
- `royalspace_mb_alerts.ps1` → fusionado en `profit/true_profit.py`
- `royalspace_mb_daily_summary.ps1` → `profit/mb_daily_summary.py`
- `jibble_asistencia_1030.ps1` → `asistencia/daily_report.py`
- `jibble_resumen_mensual.ps1` → `asistencia/monthly_report.py`
