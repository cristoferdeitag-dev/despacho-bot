# Despacho Contable Fiscal SL — Bot WhatsApp

Bot de pre-cualificación de prospectos para el Despacho Contable Fiscal SL (Soraida, Pachuca). Atiende WhatsApp + Instagram + Messenger vía ManyChat, clasifica al prospecto en una de tres rutas (Interesado / Seguimiento / No Interesado) y escala a un humano cuando aplica.

Replica del stack de [fumadorex-bot](https://github.com/cristoferdeitag-dev/fumadorex-bot) adaptada al funnel del despacho.

## Stack

- Python 3.12 + FastAPI + Uvicorn
- Anthropic Claude Haiku 4.5 como LLM
- Supabase (proyecto dedicado, separado por privacidad de RFC y datos fiscales)
- ManyChat (workspace `110240088419870`) como canal de WhatsApp
- Railway para hosting con auto-deploy desde `main`

## Arquitectura

```
Prospecto (WhatsApp)
  ↓
ManyChat (workspace Despacho)
  ↓ POST /api/webhook/manychat (header X-Webhook-Secret)
FastAPI (Railway: despacho-bot-production.up.railway.app)
  ├─ Supabase (memoria + clasificación)
  └─ Claude Haiku 4.5 (genera respuesta + decide acción)
  ↓
Custom field `ai_response` en ManyChat
  ↓ Send Message {{ai_response}}
Prospecto recibe respuesta
```

## Variables de entorno

```
ANTHROPIC_API_KEY=sk-ant-...
SUPABASE_URL=https://<project>.supabase.co
SUPABASE_SERVICE_KEY=<service_role_key>
MANYCHAT_API_TOKEN=<workspace token del Despacho>
WEBHOOK_SECRET=<string aleatorio para firmar requests del flow>
LLM_MODEL=claude-haiku-4-5-20251001

# IDs específicos del workspace de ManyChat (se llenan después de crear los custom fields)
MANYCHAT_AI_RESPONSE_FIELD_ID=
MANYCHAT_CONVERSATION_ENDED_FIELD_ID=
```

## Arrancar local

```
pip install -r requirements.txt
cp .env.example .env  # llenar valores reales
uvicorn app.main:app --reload --port 8000
```

`GET /health` debe devolver `{"status": "healthy"}`.

## Schema de Supabase

Ejecutar `sql/001_initial_schema.sql` completo en el SQL Editor del proyecto del despacho. Crea `customers`, `messages`, `escalations`, `events` con RLS habilitada (el backend usa `service_role` que la ignora; esto solo cierra el acceso anónimo a datos fiscales).

## Setup de ManyChat

En el workspace del Despacho (ID `110240088419870`):

1. **Crear custom fields:** `ai_response` (texto), `conversation_ended` (boolean). Anotar los IDs y guardarlos en las env vars.
2. **Editar el flow principal del WhatsApp:**
   - Acción "External Request" apuntando a `https://despacho-bot-production.up.railway.app/api/webhook/manychat`
   - Método: POST. Headers: `X-Webhook-Secret: <WEBHOOK_SECRET>`.
   - Body JSON:
     ```json
     {
       "user_id": "{{user_id}}",
       "text": "{{last_input_text}}",
       "first_name": "{{first_name}}",
       "phone": "{{phone}}",
       "channel": "whatsapp"
     }
     ```
   - Después del External Request, "Send Message" con `{{ai_response}}`.
   - Condition: si `conversation_ended` es `true` → cortar loop. Si no → volver a esperar input.

## Flujo del bot (lógica)

1. **Saludo + 3 preguntas:** tipo de contribuyente (PF/PM), situación, RFC + e.firma vigente.
2. **Clasificación automática:**
   - **Ruta A — Interesado:** caso encaja en declaración ($1,500) o regularización (sin precio fijo). Aplica tags `Interesado` + `ANÁLISIS FISCAL PENDIENTE`, escala a humano vía notificación al admin.
   - **Ruta B — Seguimiento:** falta RFC, e.firma o cita SAT. Comparte tutorial corto y aplica tag `Seguimiento`. No escala.
   - **Ruta C — No Interesado:** caso fuera de scope. Aplica tag `No Interesado` y cierra amable.
3. **Casos especiales:** groserías → bloqueo 7 días; crisis emocional → recursos SAPTEL/Línea de la Vida + bloqueo.

## Reglas que el bot NUNCA rompe

- No da precio de regularización (depende del caso, lo cotiza el ejecutivo).
- No promete plazos exactos del SAT.
- No da asesoría fiscal específica.
- No atiende clientes existentes (escala directo con tag `CLIENTE`).

## Deploy

Push a `main` → Railway hace build con Nixpacks y reinicia el servicio.
