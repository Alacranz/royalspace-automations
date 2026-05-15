# /ship — Deploy a producción para Royalspace

Ejecuta el pipeline completo: review → commit → push. Solo para cambios listos.

## Pre-flight
```bash
git status
git diff main...HEAD --stat
```
Verificar: estás en `main`, no hay archivos en conflicto, los cambios son los esperados.

## Paso 1: Review automático
Ejecuta `/review` internamente. Si hay issues críticos (🚨), detente y reporta. Issues menores (⚠️) se corrigen antes de continuar.

## Paso 2: Verificar workflows afectados
Si se modificó algún archivo en `.github/workflows/`:
- ¿El cron está en UTC correcto?
- ¿Todos los secrets usados están declarados en el yml?
- ¿El `python` version es 3.12?

Si se modificó `profit/` o `billing/`:
- ¿El `requirements.txt` del módulo está actualizado?
- ¿`config.json` no tiene secretos accidentales?

Si se modificó `manychat/`:
- ¿El `DB_PATH` usa variable de entorno?
- ¿El system prompt no contiene información sensible hardcodeada?

## Paso 3: Commit
Genera un mensaje de commit descriptivo siguiendo el estilo del repo:
```
<módulo>: <descripción concisa en español>

<detalle si es necesario>

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
```

```bash
git add <archivos específicos — nunca git add -A>
git commit -m "..."
```

## Paso 4: Push
```bash
git push origin main
```

## Paso 5: Verificar
```bash
git log --oneline -3
```
Confirma que el push fue exitoso y reporta el commit hash.

## Notas
- Nunca usar `git add -A` ni `git add .` — agregar archivos específicos
- Nunca commitear `.env`, `__pycache__`, `state/`, `*.xlsx`
- Si el push falla, diagnosticar antes de forzar
