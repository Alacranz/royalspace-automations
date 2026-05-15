# RoyalSpace - Strategic Roadmap

Este documento guarda las iniciativas estratégicas para mejorar la automatización, monitoreo y visibilidad de los datos en RoyalSpace. 
Claude y Antigravity deben consultar este archivo para entender el rumbo del proyecto.

## Iniciativas Aprobadas / En Progreso

### 1. Sentry (Error Tracking Automatizado)
**Estado:** En Progreso (Iniciando)
**Objetivo:** Integrar Sentry (`sentry-sdk`) en los módulos críticos (Profit, Billing, ManyChat, Asistencia) para capturar excepciones, fallos de API (Meta, Ringba) y caídas de servicio.
**Beneficio:** En lugar de fallos silenciosos o logs de GitHub Actions difíciles de leer, recibiremos trazas de error exactas para arreglar bugs rápidamente.

## Iniciativas Futuras

### 2. Integración MCP de Google Sheets en Antigravity
**Estado:** Pendiente
**Objetivo:** Utilizar el servidor MCP de Google Sheets (`uvx mcp-google-sheets`) configurado en Antigravity para hacer consultas en lenguaje natural sobre el estado de la facturación (`BILLING_STATE` y `PAGOS 2026`).
**Beneficio:** Evita tener que abrir hojas de cálculo manualmente; permite a Antigravity realizar análisis de los datos en tiempo real.

### 3. Looker Studio (Dashboard Visual)
**Estado:** Pendiente
**Objetivo:** Conectar las hojas de cálculo de facturación, métricas de ManyChat y datos de Jibble a Google Looker Studio.
**Beneficio:** Crear un Centro de Comando visual con gráficos de profit diario, alertas y eficiencia, reemplazando la dependencia de leer texto plano en Discord.

### 4. Sistema de "Segundo Cerebro" (Knowledge Items)
**Estado:** Pendiente
**Objetivo:** Usar la carpeta `docs/` y el sistema interno de Knowledge de Antigravity para registrar SOPs, reglas de negocio, historial de problemas y negociaciones con los Media Buyers.
**Beneficio:** Mantiene todo el contexto crítico dentro del repositorio y accesible para la IA, sin necesidad de pagar por Obsidian o Notion AI.
