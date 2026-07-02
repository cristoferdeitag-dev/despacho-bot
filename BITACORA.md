
## 2026-07-02 — cris — Fix: bot escribía literal [Nombre]
- Síntoma (Cris, captura Juan Carlos Fragoso): el bot escribía 'Perfecto, *[Nombre]*.' en vez del nombre real.
- Causa: los ejemplos del prompt usan '[Nombre]' como marcador; el modelo (Haiku) a veces lo copiaba literal. El nombre SÍ se captura bien (Supabase, 0 nulos).
- Fix doble (commit c4c96e2): (1) _fix_name_placeholder en claude_client.py sustituye [Nombre]->nombre real antes de enviar (respeta *negrita*; si no hay nombre, lo quita + limpia puntuación); (2) regla explícita en el bloque DATOS DEL CLIENTE del prompt. Probado con casos reales. Activo, health 200.
# 📒 Bitácora — Despacho Bot (Despacho Contable Pachuca)

> Memoria viva de este proyecto. Web HTM la **LEE** antes de trabajar aquí y la **ACTUALIZA** al terminar.
> Lo más reciente arriba. No borres historial — agrega entradas. Espejo en Obsidian: `memory/bitacoras/despacho-bot.md`.

**Stack:** Python · bot ManyChat (WhatsApp) · corre en VPS Hetzner como `despacho-bot.service` (:8003) desde `/opt/despacho-bot`
**Deploy:** auto por cron poll cada minuto: editar en `/root/despacho-bot` → push a origin/main → cron hace `git pull` + `systemctl restart` en `/opt`. Detalle: memoria `ref_despacho_bot_prod`, `ref_vps_manychat_bots_deploy`.
**Cliente:** Despacho contable en Pachuca, ~18 clientes mensuales. Captación FB Leads→n8n→ManyChat→bot.
**Estado actual:** Bot vivo. Oportunidad estratégica grande en mesa: gancho "Diagnóstico Fiscal Gratis" (estilo Heru) + implementación Siigo.

---

## 2026-06-26 — cris — Repaso de la estrategia "software"
- **Qué se hizo:** Repasé con Cris la estrategia fiscal-tech. Confirmamos el software que le gustó = **Heru** (fintech fiscal MX, API B2B + RPA al SAT). NO confundir con **Siigo** (Aspel) = software contable interno del despacho.
- **Decisiones:** Plan en 2 capas: Capa 1 = "Diagnóstico Fiscal Gratis" (gancho/lead magnet sobre API tipo Heru, conexión por CIEC, IA da status fiscal, CTA→lead al bot WhatsApp+n8n). Capa 2 = "te damos de alta/declaramos" (servicio de pago del despacho, semi-automatizable). Ventaja del despacho = confianza local.
- **Conversación ética/posicionamiento (importante):** Cris preguntó cómo hacen otros despachos para "salir a no pagar" y si era soborno al SAT. Aclaré los DOS caminos: 🟢 LEGAL (RESICO 1-2.5% ISR, deducciones, depreciación, amortizar pérdidas, estímulos, timing) vs 🔴 ILEGAL (comprar facturas falsas = factureras/EFOS-EDOS, lista negra 69-B, delincuencia organizada con prisión preventiva; el soborno directo es raro). **Decisión de posicionamiento: el despacho se vende por PLANEACIÓN FISCAL LEGAL** ("pagar lo mínimo legal y dormir tranquilo"), NUNCA acercar la marca HTM ni las herramientas a evasión. Las herramientas (diagnóstico, Siigo Fiscal) sirven para DETECTAR el camino rojo, no esconderlo.
- **Pendientes:** Cotizar/pedir demo API Heru (precio por consulta, no público) + 2-3 alternativas de API de datos SAT. Armar plan del gancho con números para OK de Cris. (Ofrecí cuadro de "técnicas legales de planeación fiscal" como argumento de venta — Cris cerró antes de pedirlo, retomar). Pregunta abierta: ¿el despacho factura con Siigo Nube (única versión con API) o solo COI+Siigo Fiscal?
- **Archivos clave:** memorias `ref_despacho_siigo` (MUY completa: Siigo, API Siigo, Heru, plan 2 capas, research mercado), `project_despacho_pachuca`, `MOC_despacho`, `project_whatsapp_n8n_plan`.
