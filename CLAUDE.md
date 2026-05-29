# Reminder Bot — Contexto del Proyecto

## ¿Qué es?
Bot de Telegram personal que recibe mensajes de voz o texto, extrae fecha/hora y mensaje,
y envía una notificación al usuario en el momento indicado.

## Stack
- Python 3.14+
- `python-telegram-bot` v21+ — framework del bot
- `groq` (Whisper large-v3) — transcripción de voz, gratis
- `APScheduler` 3.x — scheduler de recordatorios
- `dateparser` — parseo de fechas en español (fallback)
- `pytz` — zona horaria con soporte DST
- `sqlite3` (stdlib) — persistencia de recordatorios
- Sin backend adicional — todo corre en un único proceso Python

## Estructura
```
reminder-bot/
├── bot.py           # Código principal (único archivo)
├── requirements.txt
├── .env             # Tokens y config (nunca subir a GitHub)
├── .gitignore
├── Procfile         # Para Railway: "worker: python bot.py"
└── reminders.db     # Generado en runtime por SQLite
```

## Variables de entorno (.env)
```
TELEGRAM_TOKEN=...          # Token del bot de @BotFather
GROQ_API_KEY=...            # API key de console.groq.com
TIMEZONE=America/Santiago
DB_PATH=reminders.db        # En Railway: ruta al volumen persistente (ej: /data/reminders.db)
GROQ_TIMEOUT=25             # Segundos máx para transcripción de voz
SUMMARY_TIME=08:00          # Hora del resumen diario automático (formato HH:MM)
```

## Decisiones de diseño

### Zona horaria
- Siempre `America/Santiago` — incluye DST de Chile automáticamente vía pytz
- Se usa `localize_dt()` en lugar de `.replace(tzinfo=...)` para manejar:
  - `AmbiguousTimeError`: al atrasar el reloj → usa horario estándar
  - `NonExistentTimeError`: al adelantar el reloj → salta una hora

### Formato horario
- **Entrada**: acepta 12h (am/pm, a.m./p.m.) y 24h
- **Salida**: siempre 24h (`strftime("%H:%M")`)
- El mensaje "Entendí: ..." y el mensaje del pin de confirmación (📌) siempre
  pasan por `preprocess_time()`, por lo que muestran "23:11" en vez de "23.11".
  En `parse_reminder()` todos los `_clean_message()` usan `processed` (no `text`).
- Conversiones en `preprocess_time()`:
  - `9pm` / `9 p.m.` → `las 21:00`
  - `12am` → `las 0:00` (medianoche)
  - `12pm` → `las 12:00` (mediodía)
  - `HH.MM` → `HH:MM` (punto → dos puntos)
  - `de la noche` / `de la tarde` / `de la mañana` → 24h

### Ambigüedad de hora
- Horas 1–7 sin contexto (tarde/noche/am/pm) → pregunta AM/PM con botones inline
- Si el usuario ya especificó am/pm explícito → no pregunta (detectado en texto original)

### Hora pasada hoy
- Si la hora ya pasó hoy y no se especificó "mañana" → pregunta si agendarlo para mañana

### Confirmación
- Siempre se muestra confirmación con botones ✅/❌ antes de agendar

### Persistencia
- Los recordatorios se guardan en `reminders.db` (SQLite)
- Al iniciar el bot, recarga todos los recordatorios futuros desde la DB
- Al disparar un recordatorio, se elimina de la DB
- ⚠️ En Railway, el filesystem es efímero en cada redeploy → los recordatorios
  activos se pierden si se hace un nuevo deploy (ver Limitaciones)

### Fecha sin hora
- `parse_reminder()` retorna 5 valores: `(run_date, message, ambiguous_h, ambiguous_m, date_only)`
- Si dateparser encuentra una fecha pero sin hora explícita → `date_only` es un datetime (rest None)
- El bot pregunta "¿A qué hora te lo recuerdo?" y espera respuesta en `pending_time[chat_id]`
- El siguiente mensaje (texto o voz) se interpreta como la hora → `_resolve_pending_time()`
- Si la fecha ya pasó este año (ej: cumpleaños) → se agenda automáticamente para el año siguiente
- `pending_ampm` acepta campo `base_date` opcional para conservar la fecha fija del flujo date_only
- `pending_time[chat_id]` tiene campo `step`: `"date"` (esperando día) o `"time"` (esperando hora)
- Cuando no se reconoce NADA → `step="date"`, el bot pregunta el día primero
- Si la respuesta al día incluye también la hora → se resuelve todo sin paso extra
- `_extract_time(processed)` extrae (h, mins) desde texto preprocesado (helper reutilizable)
- `_parse_date_es(text)` es el parser robusto de fechas en español (reemplaza dateparser directo):
  1. Aplica `preprocess_date` + `preprocess_time`
  2. Regex directo para "DD de MES [de YYYY]" (más fiable que dateparser)
  3. Regex para "DD" solo → día del mes actual o próximo
  4. Fallback a dateparser
  Todos los sitios que antes llamaban `dateparser.parse(preprocess_date(...))` ahora usan `_parse_date_es()`.
- `preprocess_date(text)` traduce expresiones que dateparser no entiende — se aplica ANTES
  de cada llamada a dateparser. Expresiones soportadas:
  - "este mes" → nombre del mes actual (ej: "el 31 de este mes" → "el 31 de mayo")
  - "el mes que viene" / "próximo mes" → mes siguiente con año
  - "a fin(es) de mes" → último día del mes actual
  - "a principios/comienzos de mes" → "1 de {mes}"
  - "a mediados de mes" → "15 de {mes}"

### Parseo de recordatorios
Orden de prioridad en `parse_reminder()`:
1. Relativo: `"en X minutos"` / `"en X horas"`
2. Absoluto con HH:MM explícito
3. Absoluto con `"a las X"` (sin minutos)
4. Fallback: `dateparser` con `languages=["es"]`

## Flujo del bot
```
Audio/texto → [transcripción Whisper si es audio]
           → preprocess_time() → parse_reminder()
           → ¿hora ambigua? → pedir AM/PM
           → ¿hora pasada? → preguntar si es mañana
           → mostrar confirmación (botones ✅/❌)
           → agendar en APScheduler + guardar en SQLite
           → [a la hora indicada] → enviar notificación + borrar de DB
```

## Lista del súper
- Tabla `shopping_items`: id, chat_id, item, quantity, added_at
- Detección por keywords en `SHOPPING_TRIGGER` regex (agrega, añade, súper, etc.)
- `parse_shopping_items()` extrae ítems + cantidades, soporta múltiples en un mensaje
- `/super` muestra lista con botones inline 🗑 por ítem (callback `del_item:{id}`)
- `/super_listo` limpia toda la lista (compra completada)
- El bot diferencia entre tres intenciones de shopping:
  - `is_shopping_view_intent()` → "me muestras la lista", "ver el super", etc. → muestra lista
  - `is_shopping_add_intent()` → "agrega", "añade", "compra" + ítem → agrega ítems
  - Sin match → flujo de recordatorio normal
  - La vista tiene prioridad sobre el agregar para evitar falsos positivos

## Imágenes en recordatorios
- Handler `handle_photo` recibe foto + caption
- Guarda el `file_id` de Telegram (no descarga la imagen, solo la referencia)
- Se almacena en columna `photo_file_id` de la tabla `reminders`
- Al disparar el recordatorio: si tiene foto → `send_photo`, sino → `send_message`
- En la confirmación se indica "_(📷 con foto)_" si hay imagen adjunta
- Migración automática en `init_db()` con `ALTER TABLE` protegido por try/except

## Comandos disponibles
- `/start` — Bienvenida e instrucciones
- `/lista` — Ver recordatorios pendientes
- `/cancelar` — Borrar todos los recordatorios
- `/resumen` — Ver los recordatorios de hoy (también se envía automáticamente cada mañana)
- `/super` — Ver lista del súper con botones para borrar ítems
- `/super_listo` — Marcar compra como completada y limpiar la lista
- `/super_compartir` — Genera deep link `t.me/bot?start=LISTA-XXXXXX` (válido 24hs)
- `/super_unirse CÓDIGO` — Une al usuario a la lista (fallback manual)
- Deep link: al tocar `t.me/bot?start=LISTA-XXXXXX` Telegram envía `/start LISTA-XXXXXX`
  automáticamente → `cmd_start` detecta el prefijo "LISTA-" y hace el join sin escribir nada
- `BOT_USERNAME` se carga en `post_init` via `application.bot.get_me()`
- `/super_salir` — Desvincula al usuario, vuelve a su lista propia

## Lista compartida del súper
- Tabla `share_codes`: code, owner_chat_id, expires_at (24hs)
- Columna `list_owner INTEGER DEFAULT NULL` en tabla `users`
- `db_get_list_owner(chat_id)` → retorna el owner efectivo (propio o compartido)
- Todas las operaciones de shopping (add, get, clear) pasan por `db_get_list_owner()`
- La lista muestra "🔗 (compartida)" en el header cuando el owner es otro usuario
- Los ítems viven en el chat_id del owner, no del usuario que los agregó

## Mensajes humanizados
- `fire_reminder()` usa `random.choice(REMINDER_TEMPLATES)` — 8 frases distintas
- `_greeting()` devuelve saludo según hora: buenos días (6-12), tardes (12-20), noches (resto)
- El resumen diario incluye saludo + lista + frase de cierre

## Resumen diario
- Tabla `users` en SQLite — se registra automáticamente con cualquier interacción
- Cron job APScheduler a la hora definida en `SUMMARY_TIME` (default 08:00)
- Si no hay recordatorios para hoy, no envía nada (salvo que sea /resumen manual)
- Para cambiar la hora del resumen: modificar `SUMMARY_TIME` en `.env`

## Limitaciones conocidas / resueltas
1. **Railway filesystem efímero** → RESUELTO: `DB_PATH` es configurable vía env var.
   En Railway: crear un volumen persistente y apuntar `DB_PATH=/data/reminders.db`.
2. **Pending en memoria**: Si el bot se cae entre que muestra los botones de confirmación
   y el usuario presiona uno, la confirmación se pierde. Comportamiento aceptable.
3. **Dos audios seguidos sin confirmar** → RESUELTO: cada pending usa un UUID único
   (`{chat_id}:{uid}`), los dos conviven sin pisarse.
4. **Groq timeout** → RESUELTO: `asyncio.wait_for(..., timeout=GROQ_TIMEOUT)` con
   mensaje de error claro. El archivo temporal siempre se borra en el bloque `finally`.
5. **Groq rate limit**: Free tier = 30 req/min para audio. Suficiente para uso personal.

## UX / Navegación
- `WELCOME_TEXT` constante centralizada — usada por `/start`, saludos y botón 🏠 Salir
- `GREETING_RE` detecta "hola", "hi", "hello", "hey", "buenas", etc. → muestra bienvenida
- La lista del súper tiene botón "🏠 Salir" (callback `goto_menu`) que dispara `_send_welcome()`
- Al confirmar un recordatorio, el bot edita el mensaje del inline keyboard Y envía un
  mensaje nuevo amigable (elegido aleatoriamente de `confirmaciones[]`)
- Bug fix: `_build_datetime()` ahora consulta dateparser para extraer la fecha cuando el
  texto contiene una fecha específica ("el 15 de junio") junto con una hora explícita.
  Sin este fix, el regex "a las X" capturaría la hora antes que dateparser leyera la fecha
  y el recordatorio se agendaba para hoy.

## Reglas para cambios futuros
1. Siempre usar `now_local()` en lugar de `datetime.now()` — nunca naive datetimes
2. Siempre usar `localize_dt()` para construir datetimes con fecha/hora específica
3. El bot siempre responde en formato 24h (`%H:%M`)
4. Cualquier nuevo formato de entrada de hora va en `preprocess_time()`
5. El `.env` nunca se sube a GitHub (está en `.gitignore`)
6. No usar `asyncio.get_event_loop()` directamente — Python 3.14 lo deprecó;
   usar `asyncio.set_event_loop(asyncio.new_event_loop())` antes de `main()`

## Despliegue
- Local: activar `.venv` → `python bot.py`
- Railway: conectar repo GitHub → Railway detecta `Procfile` → deploy automático
  - Configurar env vars en Railway dashboard (TELEGRAM_TOKEN, GROQ_API_KEY, TIMEZONE)
  - El proceso corre como `worker` (no duerme, a diferencia de web services)
