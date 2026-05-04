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

| Módulo | Carpeta | Propósito |
|---|---|---|
| Monitor de Profit | `profit/` | Ringba + Meta Ads → Discord (cada 30 min) |
| Asistencia | `asistencia/` | Jibble → Discord + Excel (diario y mensual) |
| Facturación | `billing/` | Zoho Books + Google Sheets (facturas automáticas) |
| Monitoreo | `monitoring/` | Costos Railway/Anthropic + Executive Brief diario |
| ManyChat | `manychat/` | Bot de WhatsApp/Messenger para Dentista Latino (FastAPI) |

---

## Estructura de Archivos

```
royalspace-automations/
├── CLAUDE.md
├── .gitignore
├── .claude/
│   ├── commands/
│   │   ├── review.md          ← /review: code review antes de push
│   │   ├── cso.md             ← /cso: auditoría de seguridad
│   │   └── ship.md            ← /ship: review + commit + push
│   └── launch.json            ← dev server ManyChat (puerto 8000)
│
├── .github/
│   └── workflows/
│       ├── profit_true_profit.yml         # cada 30 min, L-V 8-20 / S 8-14 EST
│       ├── profit_mb_daily_summary.yml    # L-V 8:00 AM EST (resumen de ayer)
│       ├── asistencia_diaria.yml          # L-V 10:30 AM VET
│       ├── asistencia_mensual.yml         # día 1 de cada mes, 10:00 AM VET
│       ├── billing_invoices.yml           # día 28 de cada mes (facturas NET)
│       ├── billing_reminders.yml          # recordatorios de pago
│       ├── monitoring_daily.yml           # diario: costos Railway/Anthropic
│       ├── executive_brief.yml            # 8 AM VET L-S: brief ejecutivo
│       ├── accounting_calculate.yml       # contabilidad semanal
│       └── accounting_reminder.yml        # recordatorio contabilidad
│
├── profit/
│   ├── config.json                    ← configuración sin secretos
│   ├── requirements.txt               ← requests>=2.31, pytz>=2024.1
│   ├── true_profit.py                 ← True Profit + MB Alerts (3 mensajes)
│   ├── mb_daily_summary.py            ← resumen diario del día anterior (2 mensajes)
│   └── common/
│       ├── business_hours.py
│       ├── discord_client.py
│       ├── meta_client.py
│       └── ringba_client.py
│
├── asistencia/
│   ├── requirements.txt
│   ├── jibble_client.py
│   ├── daily_report.py
│   └── monthly_report.py
│
├── billing/
│   ├── config.json                    ← buyers, due_days, billing_frequency
│   ├── requirements.txt               ← requests, pytz, gspread, google-auth
│   ├── invoice_generator.py           ← genera y envía facturas Zoho
│   ├── payment_tracker.py             ← Google Sheets: PAGOS 2026 + BILLING_STATE
│   ├── zoho_client.py                 ← Zoho Books OAuth + API
│   └── reminders.py                   ← recordatorios de pago
│
├── monitoring/
│   ├── requirements.txt               ← requests, pytz, gspread, google-auth
│   ├── daily_report.py                ← costos Railway + Anthropic → Discord
│   └── executive_brief.py             ← brief ejecutivo diario con Claude Haiku
│
└── manychat/
    ├── main.py                        ← FastAPI webhook para ManyChat
    ├── requirements.txt
    ├── Procfile
    └── railway.json
```

**Ignorado por git:**
- `AUTOMATIZACIONES/` — scripts PowerShell originales
- `automatizacion/` — carpeta legacy
- `profit/state/` — estado en tiempo de ejecución
- `asistencia/Asistencia_*.xlsx`
- `.env`, `__pycache__/`, `.venv/`

---

## Timezones

| Sistema | Timezone | Pytz |
|---|---|---|
| Profit (Ringba, Meta, horario laboral) | Eastern Time (EST/EDT) | `America/New_York` |
| Asistencia (Jibble, Venezuela) | Venezuela Standard Time (VET, UTC-4) | `America/Caracas` |
| Executive Brief | VET (UTC-4) | `America/Caracas` |

---

## Sistema de Profit

### APIs usadas

**Ringba**
- Base URL: `https://api.ringba.com/v2`
- Auth: `Authorization: Token {RINGBA_API_TOKEN}`
- Endpoint: `POST /v2/{account_id}/calllogs`
- Body: `{ reportStart, reportEnd, size: 1000, offset }`
- Campos: `publisherName`, `payoutAmount`, `conversionAmount`, `profitNet`, `hasConnected`, `hasConverted`

**Meta Ads Graph API**
- URL: `https://graph.facebook.com/{version}/act_{id}/insights`
- Params: `fields=spend`, `date_preset=today|yesterday`, `level=account`
- IMPORTANTE: usar `os.environ.get("META_API_VERSION") or "v25.0"` — el `or` es necesario para evitar string vacía que causa doble slash en la URL

**Discord Webhooks**
- Límite: 2000 chars; truncar a 1900
- Formato: bloques de código monoespaciado

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

**Private Group:**
- Publishers: "you", "T.I Angela Monroy"
- Meta Ad Account: act_266001198433130

### Fórmulas de Profit

```
mb_share_amt  = spend × media_buyer_spend_share
mb_profit     = payout - mb_share_amt
rs_profit     = (revenue - payout) - (spend × royalspace_spend_share)
combined_net  = rs_total + mb_mb_profit_total
```

### Status MB

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

---

## Sistema de Facturación (billing/)

### Buyers y términos

| Buyer | Frecuencia | Due Days | Notas |
|---|---|---|---|
| Rex Direct | mensual | 15 | activo |
| Ray Advertising | mensual | 15 | activo |
| MarketCall | mensual | 15 | activo |
| 1800Dentist | mensual | 30 | activo |
| UNIK | mensual | 30 | activo |
| ClickDealer | mensual | 30 | activo |
| Aragon Advertising | bimensual | 15 | `billing_frequency: 2` |

### Reglas de facturación
- Factura del mes M se envía el **día 28 del mes M+1**
- Plazo de pago: **15 días** (algunos buyers 30 días según tabla)
- Threshold: **$500 mínimo** — si revenue < $500, acumular hasta superar el threshold
- Acumulación persistida en Google Sheets tab `BILLING_STATE`
- Facturas se crean como **borrador** en Zoho Books (no se envían automáticamente)

### Google Sheets
- Tab `PAGOS 2026`: facturas pendientes/vencidas
- Tab `BILLING_STATE`: acumulación de revenue por buyer
  - Columnas: Buyer, Pending Revenue, From Month, To Month, Months Accumulated, Last Invoice Month, Last Updated

### Secrets adicionales (billing)

| Secret | Descripción |
|---|---|
| `ZOHO_CLIENT_ID` | OAuth Zoho Books |
| `ZOHO_CLIENT_SECRET` | OAuth Zoho Books |
| `ZOHO_REFRESH_TOKEN` | Refresh token Zoho |
| `ZOHO_ORG_ID` | ID organización Zoho (771911284) |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Service account para Google Sheets |
| `BILLING_SPREADSHEET_ID` | ID del spreadsheet de facturación |
| `DISCORD_WEBHOOK_MOD` | Compartido con profit |

---

## Sistema de Monitoreo (monitoring/)

### daily_report.py
- Corre diariamente via `monitoring_daily.yml`
- Muestra costos de Railway (GraphQL `estimatedUsage`) y Anthropic
- Railway: precios Hobby — CPU: $0.000463/vCPU-min, Memory: $0.000231/GB-min, Network TX: $0.10/GB
- GitHub billing API no funciona para cuentas personales → muestra valores estáticos

### executive_brief.py
- Corre a las **8 AM VET (12:00 UTC)** lunes-sábado via `executive_brief.yml`
- Consolida: Ringba + Meta (ayer), facturas Zoho/Sheets, ManyChat stats
- Usa **Claude Haiku** para generar diagnóstico ejecutivo en 3 secciones:
  - **Operación:** profit total y eficiencia
  - **Media Buyers:** mejor y peor MB
  - **Facturación:** estado de facturas
- Envía a `DISCORD_WEBHOOK_MOD`
- Secret requerido: `ANTHROPIC_API_KEY`
- ManyChat stats: `WEBHOOK_STATS_URL` = `https://royalspace-automations-production.up.railway.app/stats`
  - Campo usado: `conversations_today`

### Secrets adicionales (monitoring)

| Secret | Descripción |
|---|---|
| `RAILWAY_TOKEN` | Token API de Railway |
| `ANTHROPIC_API_KEY` | API key de Anthropic para Claude Haiku |
| `WEBHOOK_STATS_URL` | URL del endpoint /stats de ManyChat en Railway |
| `ANTHROPIC_BALANCE` | Balance manual de Anthropic (variable, no secret) |

---

## Sistema ManyChat (manychat/)

### Arquitectura
- **FastAPI** desplegado en Railway
- URL: `https://royalspace-automations-production.up.railway.app`
- Base de datos: SQLite (`conversations.db`) con tablas `messages` y `token_log`
- Modelo: Claude Haiku (`claude-haiku-4-5-20251001`)

### Endpoints
- `POST /chat` — recibe mensajes de ManyChat y responde via Claude
- `GET /health` — health check
- `GET /stats` — estadísticas: `messages_today`, `messages_month`, `conversations_today`, `conversations_month`, `cost_today_usd`, `cost_month_usd`

### Reglas críticas del bot
- **IDENTITY RULE (absoluta):** Nunca revelar que es bot, IA o asistente virtual. Si el usuario pregunta, no confirmar ni negar — actuar normal y redirigir a llamar
- **Imágenes:** Responder naturalmente como si pudiera verlas, no mencionar que no puede verlas
- **Zip code:** Extraer del texto primero (`extract_zip(text)`), usar campo ManyChat solo como fallback
- **Humanos:** Si piden hablar con humano, redirigir a llamar sin revelar naturaleza del bot
- **Tono:** Nunca repetir la misma frase o call-to-action dos veces seguidas

### Request body (ManyChat → webhook)
```json
{
  "subscriber_id": "string",
  "last_input_text": "string",
  "first_name": "string (opcional)",
  "zip_code": "string (opcional)"
}
```

### Dev server local
```bash
uvicorn manychat.main:app --host 0.0.0.0 --port 8000 --reload
```
Configurado en `.claude/launch.json`

---

## Secrets de GitHub Actions — Resumen Completo

### Profit
| Secret | Descripción |
|---|---|
| `RINGBA_API_TOKEN` | Token Ringba |
| `RINGBA_ACCOUNT_ID` | Account ID Ringba |
| `META_ACCESS_TOKEN` | Token Meta Ads Graph API |
| `DISCORD_WEBHOOK_MOD` | Webhook #mod |
| `DISCORD_WEBHOOK_MB_INTERNAL` | Webhook #mb-alerts interno |
| `DISCORD_WEBHOOK_MB_EXTERNAL` | Webhook servidor externo MB |

### Asistencia
| Secret | Descripción |
|---|---|
| `JIBBLE_CLIENT_ID` | OAuth Jibble |
| `JIBBLE_CLIENT_SECRET` | OAuth Jibble |
| `JIBBLE_ORG_ID` | Org ID Jibble |
| `DISCORD_WEBHOOK_ASISTENCIA` | Webhook #asistencia-ti |

### Billing
| Secret | Descripción |
|---|---|
| `ZOHO_CLIENT_ID` | OAuth Zoho |
| `ZOHO_CLIENT_SECRET` | OAuth Zoho |
| `ZOHO_REFRESH_TOKEN` | Refresh token Zoho |
| `ZOHO_ORG_ID` | Org Zoho (771911284) |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Service account Google |
| `BILLING_SPREADSHEET_ID` | Spreadsheet facturación |

### Monitoring / ManyChat
| Secret | Descripción |
|---|---|
| `RAILWAY_TOKEN` | Token Railway |
| `ANTHROPIC_API_KEY` | API key Anthropic |
| `WEBHOOK_STATS_URL` | https://royalspace-automations-production.up.railway.app/stats |
| `GH_PAT_TOKEN` | GitHub Personal Access Token (billing) |

### Variables (no secretos)
| Variable | Valor | Descripción |
|---|---|---|
| `META_API_VERSION` | `v25.0` | Versión Graph API Meta |

---

## Herramientas del Entorno de Desarrollo

### Comandos disponibles (`.claude/commands/`)
- `/review` — audita el diff actual antes de hacer push (SQL, timezones, secrets, Discord limits)
- `/cso` — auditoría de seguridad completa (secrets, webhooks, billing, ManyChat, CI/CD)
- `/ship` — pipeline completo: review → commit → push con formato correcto

### Configuración global Claude Code (`~/.claude/settings.json`)
- **Permisos pre-aprobados:** `git *`, `python *`, `pip *`, `ls`, `mkdir`, `Read`, `Write`, `Edit`, `Glob`, `Grep`
- **Hook PreToolUse:** detecta comandos destructivos (`rm -rf`, `DROP TABLE`, `git push --force`, etc.) y avisa
- **Hook PostToolUse:** después de editar `.py`, corre `python -m py_compile` para validar sintaxis
- **Hook Stop:** verifica si quedan tareas pendientes antes de terminar el turno

### Dev Server
- ManyChat FastAPI configurado en `.claude/launch.json` (puerto 8000)
- Iniciar con: `uvicorn manychat.main:app --host 0.0.0.0 --port 8000 --reload`

---

## Convenciones del Proyecto

- **Python 3.12** en todos los workflows
- **pytz** para timezones (no `zoneinfo`)
- **requests** para HTTP (sin httpx ni aiohttp)
- **openpyxl** solo en asistencia
- **gspread + google-auth** en billing y monitoring
- Path setup: `sys.path.insert(0, os.path.dirname(__file__))`
- Formato monetario: `f"${v:.2f}"` — dos decimales siempre
- `META_API_VERSION`: usar `os.environ.get("META_API_VERSION") or "v25.0"` (no `.get("key", "default")`)
- Truncar Discord a 1900 chars
- Commits: siempre agregar `Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>`
- Git add: siempre archivos específicos, nunca `git add -A` ni `git add .`

---

## Cómo Añadir un Nuevo Media Buyer

1. Editar `profit/config.json` — agregar en `media_buyers`:
   - `display_name`: máx. 20 chars
   - `publisher_name`: exacto como aparece en Ringba
   - `facebook_ad_account_id`: con prefijo `act_`
   - `royalspace_spend_share` y `media_buyer_spend_share`: típicamente 0.5
   - `category`: `"internal"` o `"external"`
2. Si tiene cuenta propia, agregar en `accounts_private_groups`
3. Agregar su ad account a los permisos del `META_ACCESS_TOKEN`
4. No se necesita cambiar ningún script Python

## Cómo Hacer Pruebas Manuales

- **GitHub → Actions → [workflow] → Run workflow**
- `profit_true_profit.yml`: pasa `FORCE_RUN=true` automáticamente
- Los demás workflows no tienen restricción de horario

---

## Contexto Histórico

Scripts originales en PowerShell corriendo en Windows Task Scheduler.
Migrados a GitHub Actions para eliminar dependencia de PC física.

- `royalspace_true_profit.ps1` → `profit/true_profit.py`
- `royalspace_mb_alerts.ps1` → fusionado en `profit/true_profit.py`
- `royalspace_mb_daily_summary.ps1` → `profit/mb_daily_summary.py`
- `jibble_asistencia_1030.ps1` → `asistencia/daily_report.py`
- `jibble_resumen_mensual.ps1` → `asistencia/monthly_report.py`
