# /cso — Security Audit para Royalspace

Auditoría de seguridad enfocada en los vectores de riesgo reales de Royalspace: datos financieros, webhooks, APIs externas y el bot de ManyChat.

## Fases de auditoría

### 1. Secrets Archaeology
```bash
git log --all --full-history -- "*.env" "*.json" "*.yml" "*.yaml" 2>/dev/null | head -20
git grep -i "token\|secret\|password\|api_key" -- "*.py" "*.json" 2>/dev/null | grep -v "os.environ\|secrets\.\|#"
```
Busca: tokens hardcodeados, credenciales en commits pasados, secrets en config.json.

### 2. Webhook Security (Discord + ManyChat)
- ¿Los webhooks de Discord se validan antes de usarse?
- ¿El endpoint `/chat` de ManyChat valida que el request viene de ManyChat (header secreto)?
- ¿Se loguea el contenido completo del webhook (puede incluir tokens en URL)?

### 3. Billing & Financial Data
- ¿Los montos de Ringba/Meta se sanitizan antes de escribirse a Zoho/Sheets?
- ¿El `GOOGLE_SERVICE_ACCOUNT_JSON` se expone en logs?
- ¿Las facturas de Zoho tienen validación de importe antes de enviarse?
- ¿El threshold de $500 puede ser manipulado por datos externos?

### 4. ManyChat / FastAPI
- ¿Hay prompt injection posible desde mensajes de usuarios?
- ¿El historial de conversaciones en SQLite tiene límite de crecimiento?
- ¿Se sanitiza `last_input_text` antes de pasarlo al system prompt?
- ¿El endpoint `/stats` está protegido o es público?

### 5. GitHub Actions / CI-CD
- ¿Hay secrets expuestos en logs (`echo $SECRET`)?
- ¿Las actions usan versiones pinned (`@v4`) o floating (`@main`)?
- ¿`workflow_dispatch` puede ser disparado por un fork externo?

### 6. Supply Chain
- ¿Los `requirements.txt` tienen versiones fijas (`>=`) o exactas (`==`)?
- ¿Hay dependencias con CVEs conocidos?
```bash
pip install safety 2>/dev/null && safety check -r profit/requirements.txt 2>/dev/null || echo "safety no disponible"
```

## Filtros de falsos positivos
Ignorar: DoS teórico, memory leaks, issues solo en tests, ausencia de rate limiting en endpoints internos.

## Output
Lista de findings con: severidad (CRÍTICO/ALTO/MEDIO), archivo:línea, descripción del exploit, remediación concreta.
