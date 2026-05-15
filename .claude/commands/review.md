# /review — Code Review para Royalspace

Ejecuta un code review completo del diff actual contra main. Adaptado para el stack de Royalspace (Python 3.12, GitHub Actions, requests, pytz, gspread).

## Proceso

1. **Detectar cambios**: `git diff main...HEAD --stat` y `git diff main...HEAD`
2. **Revisión crítica** — aplica estos checks en orden:

### Seguridad
- [ ] Secretos hardcodeados (tokens, API keys, passwords)
- [ ] Variables de entorno accedidas sin `.get()` seguro en rutas opcionales
- [ ] Webhooks Discord: ¿se loguea el URL completo (con token)?
- [ ] ManyChat/FastAPI: ¿hay validación de input antes de pasarlo a Claude?
- [ ] Billing: ¿se sanitiza cualquier valor antes de escribirlo a Sheets o Zoho?

### Correctitud
- [ ] Manejo de excepciones: ¿las llamadas a APIs externas tienen try/except?
- [ ] Timezones: ¿se usa pytz correctamente? ¿hay fechas naive mezcladas con aware?
- [ ] Paginación Ringba: ¿se manejan todos los casos de `records < PAGE_SIZE`?
- [ ] Meta API: ¿se usa `or "v25.0"` en lugar de default en `.get()`?
- [ ] Google Sheets: ¿se maneja el caso de tab no existente?

### Calidad
- [ ] ¿Hay lógica duplicada que ya existe en `common/`?
- [ ] ¿Los mensajes de Discord pueden superar 2000 chars sin truncar?
- [ ] ¿Los workflows de GitHub Actions tienen todos los secrets declarados?
- [ ] ¿El `requirements.txt` del módulo afectado está actualizado?

### GitHub Actions
- [ ] ¿El cron está en UTC y equivale al horario deseado?
- [ ] ¿`workflow_dispatch` funciona correctamente para pruebas manuales?
- [ ] ¿Los secrets usan el nombre correcto (no `GITHUB_TOKEN` como custom secret)?

## Auto-fix
Corrige automáticamente cualquier problema mecánico obvio (strings sin truncar, missing `or "default"`, except sin mensaje).

Para problemas de lógica o diseño, muestra el problema y pregunta antes de cambiar.

## Output
Reporta: ✅ sin issues | ⚠️ issues menores corregidos | 🚨 issues críticos encontrados (con línea exacta)
