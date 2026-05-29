import os
import logging
import asyncio
import re
import random
import sqlite3
import string
import tempfile
import uuid
import calendar
from datetime import datetime, timedelta
from pathlib import Path

import pytz
import dateparser
from dotenv import load_dotenv
from groq import Groq
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

load_dotenv()

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY   = os.getenv("GROQ_API_KEY")
TIMEZONE       = os.getenv("TIMEZONE", "America/Santiago")
DB_PATH        = Path(os.getenv("DB_PATH", "reminders.db"))
GROQ_TIMEOUT   = float(os.getenv("GROQ_TIMEOUT", "25"))
SUMMARY_TIME   = os.getenv("SUMMARY_TIME", "08:00")

groq_client  = Groq(api_key=GROQ_API_KEY)
scheduler    = AsyncIOScheduler(timezone=TIMEZONE)
tz_obj       = pytz.timezone(TIMEZONE)
BOT_USERNAME: str | None = None   # se carga en post_init

# Estados en memoria
pending_confirm: dict = {}   # key → {run_date, message, photo_file_id}
pending_ampm: dict   = {}    # key → {hour, minute, message, processed, photo_file_id, base_date?}
pending_time: dict   = {}    # chat_id → {date, message, photo_file_id}  (fecha sin hora)

MESES_ES = {
    1:"enero", 2:"febrero", 3:"marzo", 4:"abril", 5:"mayo", 6:"junio",
    7:"julio", 8:"agosto", 9:"septiembre", 10:"octubre", 11:"noviembre", 12:"diciembre"
}

def fecha_es(dt: datetime) -> str:
    """Devuelve fecha en español: '15 de junio de 2026'."""
    return f"{dt.day} de {MESES_ES[dt.month]} de {dt.year}"


# ── Zona horaria ──────────────────────────────────────────────────────────────

def now_local() -> datetime:
    return datetime.now(tz_obj)

def localize_dt(naive: datetime) -> datetime:
    try:
        return tz_obj.localize(naive, is_dst=None)
    except pytz.exceptions.AmbiguousTimeError:
        return tz_obj.localize(naive, is_dst=False)
    except pytz.exceptions.NonExistentTimeError:
        return tz_obj.localize(naive + timedelta(hours=1), is_dst=True)


# ── Base de datos ─────────────────────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect(DB_PATH)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS reminders (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id      INTEGER NOT NULL,
            message      TEXT    NOT NULL,
            run_date     TEXT    NOT NULL,
            job_id       TEXT    NOT NULL UNIQUE,
            photo_file_id TEXT   DEFAULT NULL
        )
    """)
    # Migración: agrega columna si tabla ya existía sin ella
    try:
        conn.execute("ALTER TABLE reminders ADD COLUMN photo_file_id TEXT DEFAULT NULL")
    except sqlite3.OperationalError:
        pass

    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            chat_id      INTEGER PRIMARY KEY,
            summary_time TEXT DEFAULT '08:00'
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS shopping_items (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id   INTEGER NOT NULL,
            item      TEXT    NOT NULL,
            quantity  TEXT    DEFAULT NULL,
            added_at  TEXT    NOT NULL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS share_codes (
            code          TEXT    PRIMARY KEY,
            owner_chat_id INTEGER NOT NULL,
            expires_at    TEXT    NOT NULL
        )
    """)

    # Migraciones
    for migration in [
        "ALTER TABLE users ADD COLUMN list_owner INTEGER DEFAULT NULL",
    ]:
        try:
            conn.execute(migration)
        except sqlite3.OperationalError:
            pass

    conn.commit()
    conn.close()

# ── Reminders DB ──────────────────────────────────────────────────────────────

def db_save(chat_id: int, message: str, run_date: datetime, job_id: str,
            photo_file_id: str | None = None):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR REPLACE INTO reminders "
        "(chat_id, message, run_date, job_id, photo_file_id) VALUES (?,?,?,?,?)",
        (chat_id, message, run_date.isoformat(), job_id, photo_file_id)
    )
    conn.commit()
    conn.close()

def db_delete(job_id: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM reminders WHERE job_id = ?", (job_id,))
    conn.commit()
    conn.close()

def db_load_all():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT chat_id, message, run_date, job_id, photo_file_id FROM reminders"
    ).fetchall()
    conn.close()
    return rows

# ── Users DB ──────────────────────────────────────────────────────────────────

def db_register_user(chat_id: int):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR IGNORE INTO users (chat_id) VALUES (?)", (chat_id,))
    conn.commit()
    conn.close()

def db_get_all_users() -> list:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT chat_id FROM users").fetchall()
    conn.close()
    return [r[0] for r in rows]

def db_get_todays_reminders(chat_id: int) -> list:
    today = now_local().date()
    conn  = sqlite3.connect(DB_PATH)
    rows  = conn.execute(
        "SELECT message, run_date FROM reminders WHERE chat_id = ?", (chat_id,)
    ).fetchall()
    conn.close()
    result = []
    for message, run_date_str in rows:
        rd = datetime.fromisoformat(run_date_str)
        if rd.tzinfo is None:
            rd = localize_dt(rd)
        if rd.date() == today:
            result.append((rd, message))
    return sorted(result, key=lambda x: x[0])

# ── Shopping DB ───────────────────────────────────────────────────────────────

def db_get_list_owner(chat_id: int) -> int:
    """Retorna el chat_id efectivo para operaciones de lista (propio o compartido)."""
    try:
        conn = sqlite3.connect(DB_PATH)
        row  = conn.execute("SELECT list_owner FROM users WHERE chat_id = ?", (chat_id,)).fetchone()
        conn.close()
        return row[0] if row and row[0] else chat_id
    except Exception:
        return chat_id   # si falta la columna, usa lista propia sin romper nada

def db_set_list_owner(chat_id: int, owner: int | None):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE users SET list_owner = ? WHERE chat_id = ?", (owner, chat_id))
    conn.commit()
    conn.close()

def db_save_share_code(code: str, owner_chat_id: int):
    expires = (now_local() + timedelta(hours=24)).isoformat()
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR REPLACE INTO share_codes (code, owner_chat_id, expires_at) VALUES (?,?,?)",
        (code, owner_chat_id, expires)
    )
    conn.commit()
    conn.close()

def db_get_share_code(code: str) -> int | None:
    """Retorna owner_chat_id si el código es válido y no expiró."""
    conn  = sqlite3.connect(DB_PATH)
    row   = conn.execute(
        "SELECT owner_chat_id, expires_at FROM share_codes WHERE code = ?", (code,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    owner, expires_at = row
    if datetime.fromisoformat(expires_at) < now_local():
        return None
    return owner

def generate_share_code() -> str:
    chars = string.ascii_uppercase + string.digits
    return "LISTA-" + "".join(random.choices(chars, k=6))

def db_add_shopping_item(chat_id: int, item: str, quantity: str | None):
    owner = db_get_list_owner(chat_id)   # escribe en la lista del owner
    conn  = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO shopping_items (chat_id, item, quantity, added_at) VALUES (?,?,?,?)",
        (owner, item, quantity, now_local().isoformat())
    )
    conn.commit()
    conn.close()

def db_get_shopping_items(chat_id: int) -> list:
    owner = db_get_list_owner(chat_id)   # lee la lista del owner
    conn  = sqlite3.connect(DB_PATH)
    rows  = conn.execute(
        "SELECT id, item, quantity FROM shopping_items WHERE chat_id = ? ORDER BY id",
        (owner,)
    ).fetchall()
    conn.close()
    return rows

def db_delete_shopping_item(item_id: int):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM shopping_items WHERE id = ?", (item_id,))
    conn.commit()
    conn.close()

def db_clear_shopping_list(chat_id: int):
    owner = db_get_list_owner(chat_id)
    conn  = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM shopping_items WHERE chat_id = ?", (owner,))
    conn.commit()
    conn.close()


# ── Mensajes humanizados ──────────────────────────────────────────────────────

REMINDER_TEMPLATES = [
    "¡Hola! 👋 Te recuerdo que tenés pendiente:\n_{message}_",
    "Oye, no te olvides 🔔\n_{message}_",
    "¡Hey! Soy tu recordatorio 😊 —\n_{message}_",
    "Hola, vengo a recordarte algo importante:\n_{message}_",
    "¡Atención! 🙋 Acordate de esto:\n_{message}_",
    "📌 Recordatorio del momento:\n_{message}_",
    "¡Buenas! 👋 Te paso a recordar:\n_{message}_",
    "Aquí tu asistente 🤖 — no te olvides:\n_{message}_",
]

def _greeting() -> str:
    h = now_local().hour
    if 6 <= h < 12:  return "¡Buenos días! ☀️"
    if 12 <= h < 20: return "¡Buenas tardes! 🌤"
    return "¡Buenas noches! 🌙"


# ── Helpers de tiempo ─────────────────────────────────────────────────────────

def preprocess_time(text: str) -> str:
    # HH.MM → HH:MM
    text = re.sub(r'\b(\d{1,2})\.(\d{2})\b', r'\1:\2', text)
    # PM
    def to_pm(m):
        h = int(m.group(1)); mins = m.group(2) or "00"
        return f"las {h + 12 if h != 12 else 12}:{mins}"
    text = re.sub(r'\b(\d{1,2})(?::(\d{2}))?\s*p\.?\s*m\.?', to_pm, text, flags=re.IGNORECASE)
    # AM
    def to_am(m):
        h = int(m.group(1)); mins = m.group(2) or "00"
        return f"las {0 if h == 12 else h}:{mins}"
    text = re.sub(r'\b(\d{1,2})(?::(\d{2}))?\s*a\.?\s*m\.?', to_am, text, flags=re.IGNORECASE)
    # "de la noche" → 24h
    def to_noche(m):
        h = int(m.group(1)); mins = m.group(2) or "00"
        return f"las {h + 12 if h < 12 else h}:{mins}"
    text = re.sub(r'las?\s+(\d{1,2})(?::(\d{2}))?\s+de\s+la\s+noche', to_noche, text, flags=re.IGNORECASE)
    # "de la tarde" → 24h
    def to_tarde(m):
        h = int(m.group(1)); mins = m.group(2) or "00"
        return f"las {h + 12 if h < 8 else h}:{mins}"
    text = re.sub(r'las?\s+(\d{1,2})(?::(\d{2}))?\s+de\s+la\s+tarde', to_tarde, text, flags=re.IGNORECASE)
    # "de la mañana" → quitar contexto
    text = re.sub(r'(las?\s+\d{1,2}(?::\d{2})?)\s+de\s+la\s+mañana', r'\1', text, flags=re.IGNORECASE)
    return text


def preprocess_date(text: str) -> str:
    """
    Traduce expresiones de fecha relativas en español que dateparser no entiende,
    convirtiéndolas a valores concretos antes de parsear.
    """
    now       = now_local()
    last_day  = calendar.monthrange(now.year, now.month)[1]
    mes_actual = MESES_ES[now.month]

    # "este mes" → nombre del mes actual  (ej: "el 31 de este mes" → "el 31 de mayo")
    text = re.sub(r'\beste\s+mes\b', mes_actual, text, flags=re.IGNORECASE)

    # "el 31 de mayo" → "31 de mayo"  (dateparser entiende mejor sin el artículo)
    # "el día 31" → "31"
    text = re.sub(r'\bel\s+(\d{1,2})\s+de\b', r'\1 de', text, flags=re.IGNORECASE)
    text = re.sub(r'\bel\s+d[ií]a\s+(\d{1,2})\b', r'\1', text, flags=re.IGNORECASE)

    # "el mes que viene" / "próximo mes" / "mes siguiente"
    next_month      = (now.month % 12) + 1
    next_month_year = now.year + (1 if now.month == 12 else 0)
    text = re.sub(
        r'\b(?:el\s+)?(?:mes\s+que\s+viene|pr[oó]ximo\s+mes|siguiente\s+mes)\b',
        f"{MESES_ES[next_month]} de {next_month_year}", text, flags=re.IGNORECASE
    )

    # "a fin(es) de mes" → último día del mes
    text = re.sub(
        r'\ba\s+fines?\s+de\s+(?:este\s+)?mes\b',
        f"{last_day} de {mes_actual}", text, flags=re.IGNORECASE
    )

    # "a principios / comienzos / inicio de mes" → día 1
    text = re.sub(
        r'\ba\s+(?:principios?|comienzos?|inicio)\s+de\s+(?:este\s+)?mes\b',
        f"1 de {mes_actual}", text, flags=re.IGNORECASE
    )

    # "a mediados de mes" → día 15
    text = re.sub(
        r'\ba\s+mediados?\s+de\s+(?:este\s+)?mes\b',
        f"15 de {mes_actual}", text, flags=re.IGNORECASE
    )

    # "esta semana" / "la semana que viene" → dateparser lo maneja bien
    return text


# Mapa inverso de nombres de mes → número
_MES_NUM = {v: k for k, v in MESES_ES.items()}

def _parse_date_es(text: str) -> datetime | None:
    """
    Parsea una expresión de fecha en español.
    1. Aplica preprocess_date + preprocess_time
    2. Intenta regex directo para "DD de MES [de YYYY]"
    3. Fallback a dateparser
    """
    now      = now_local()
    clean    = preprocess_date(preprocess_time(text))
    settings = {"PREFER_DATES_FROM": "future", "RETURN_AS_TIMEZONE_AWARE": False, "TIMEZONE": TIMEZONE}

    # Regex directo: "31 de mayo" / "31 de mayo de 2026"
    m = re.search(
        r'\b(\d{1,2})\s+de\s+(' + '|'.join(MESES_ES.values()) + r')(?:\s+de\s+(\d{4}))?\b',
        clean, re.IGNORECASE
    )
    if m:
        day   = int(m.group(1))
        month = _MES_NUM[m.group(2).lower()]
        year  = int(m.group(3)) if m.group(3) else now.year
        try:
            dt = localize_dt(datetime(year, month, day, 0, 0, 0))
            if dt.date() < now.date():
                dt = localize_dt(datetime(year + 1, month, day, 0, 0, 0))
            return dt
        except ValueError:
            pass

    # Regex directo: solo "el 31" / "31" → día del mes actual o próximo
    m2 = re.match(r'^\s*(?:el\s+)?(\d{1,2})\s*$', clean, re.IGNORECASE)
    if m2:
        day = int(m2.group(1))
        for delta_months in (0, 1):
            month = ((now.month - 1 + delta_months) % 12) + 1
            year  = now.year + ((now.month - 1 + delta_months) // 12)
            try:
                dt = localize_dt(datetime(year, month, day, 0, 0, 0))
                if dt.date() >= now.date():
                    return dt
            except ValueError:
                continue

    # Fallback: dateparser
    dp = dateparser.parse(clean, languages=["es"], settings=settings)
    return localize_dt(dp.replace(tzinfo=None)) if dp else None

def has_explicit_ampm(text: str) -> bool:
    return bool(re.search(r'\b\d{1,2}(?::\d{2})?\s*[ap]\.?\s*m\.?', text, re.IGNORECASE))

def _clean_message(text: str) -> str:
    prefixes = [
        r"hola[,!.]?\s*",
        r"recuérdeme\s+", r"recuerdeme\s+",
        r"recuérdame\s+", r"recordame\s+",
        r"avísame\s+",    r"avisame\s+",
        r"poneme\s+un\s+recordatorio\s+",
        r"poné\s+un\s+recordatorio\s+",
        r"recordatorio\s+",
    ]
    msg = text
    for p in prefixes:
        msg = re.sub(p, "", msg, flags=re.IGNORECASE)
    return msg.strip()

def is_hour_ambiguous(hour: int, text: str) -> bool:
    if hour > 7 or hour == 0:
        return False
    return not bool(re.search(r'(tarde|noche|mañana|madrugada|mediod|am|pm)', text, re.IGNORECASE))

def _build_datetime(h: int, mins: int, processed: str, now: datetime) -> datetime:
    """
    Combina la hora explícita con la fecha del contexto.
    Prioridad: pasado mañana > mañana > dateparser > hoy.
    """
    if re.search(r'pasado\s+mañana', processed, re.IGNORECASE):
        base_date = (now + timedelta(days=2)).date()
    elif re.search(r'mañana', processed, re.IGNORECASE):
        base_date = (now + timedelta(days=1)).date()
    else:
        # Intentar extraer fecha (ej: "el 15 de junio", "el lunes", "el 31 de este mes")
        dp = _parse_date_es(processed)
        if dp and dp.date() > now.date():
            base_date = dp.date()
        else:
            base_date = now.date()
    return localize_dt(datetime(base_date.year, base_date.month, base_date.day, h, mins, 0))

def _has_explicit_time(processed: str, original: str) -> bool:
    """True si el texto tiene una hora concreta (no solo fecha)."""
    return bool(
        re.search(r'\b\d{1,2}:\d{2}\b', processed) or
        re.search(r'\ba\s+las?\s+\d', processed, re.IGNORECASE) or
        re.search(r'\ben\s+\d+\s+(?:minutos?|horas?)\b', processed, re.IGNORECASE) or
        re.search(r'\b(medianoche|mediod[ií]a)\b', processed, re.IGNORECASE) or
        has_explicit_ampm(original)
    )


def parse_reminder(text: str):
    """
    Retorna (run_date, message, ambiguous_h, ambiguous_m, date_only).
    - run_date    → datetime completo listo para agendar
    - ambiguous_h → hora 1-7 sin contexto AM/PM (pedir al usuario)
    - date_only   → datetime con solo fecha (sin hora), pedir hora al usuario
    Los tres son mutuamente excluyentes; los no usados vienen en None/0.
    """
    processed     = preprocess_time(text)
    now           = now_local()
    explicit_ampm = has_explicit_ampm(text)

    # Relativo: "en X minutos / horas"
    m = re.search(r'\ben\s+(\d+)\s+minuto', processed, re.IGNORECASE)
    if m:
        return now + timedelta(minutes=int(m.group(1))), _clean_message(processed), None, 0, None

    m = re.search(r'\ben\s+(\d+)\s+hora', processed, re.IGNORECASE)
    if m:
        return now + timedelta(hours=int(m.group(1))), _clean_message(processed), None, 0, None

    # Absoluto con HH:MM
    tm = re.search(r'\b(\d{1,2}):(\d{2})\b', processed)
    if tm:
        h, mins = int(tm.group(1)), int(tm.group(2))
        if 0 <= h <= 23 and 0 <= mins <= 59:
            if not explicit_ampm and is_hour_ambiguous(h, processed):
                return None, _clean_message(processed), h, mins, None
            return _build_datetime(h, mins, processed, now), _clean_message(processed), None, 0, None

    # Absoluto "a las X"
    m2 = re.search(r'\ba\s+las?\s+(\d{1,2})\b', processed, re.IGNORECASE)
    if m2:
        h, mins = int(m2.group(1)), 0
        if not explicit_ampm and is_hour_ambiguous(h, processed):
            return None, _clean_message(processed), h, mins, None
        return _build_datetime(h, mins, processed, now), _clean_message(processed), None, 0, None

    # Fallback: parser robusto en español (regex + dateparser)
    parsed = _parse_date_es(processed)
    if parsed:
        if _has_explicit_time(processed, text):
            return parsed, _clean_message(processed), None, 0, None
        # Fecha sin hora → pedir hora al usuario
        return None, _clean_message(processed), None, 0, parsed

    return None, _clean_message(processed), None, 0, None


# ── Shopping: detección y parseo ──────────────────────────────────────────────

# Verbos que implican AGREGAR ítems (requieren acción explícita)
SHOPPING_ADD_TRIGGER = re.compile(
    r'\b(agrega[r]?|a[ñn]ade|añadir|compra[r]?)\b',
    re.IGNORECASE
)

# Frases que implican VER la lista (mostrar, consultar, etc.)
SHOPPING_VIEW_TRIGGER = re.compile(
    r'\b(mostrar?|ver?\b|muéstrame|muestrame|muestra[s]?|ens[eé]ña[s]?|enseñame'
    r'|ver\s+la|tengo\s+en|hay\s+en|qué\s+(?:hay|tengo)|cuál\s+es)\b'
    r'.*\b(s[uú]per(?:mercado)?|lista(?:\s+de\s+(?:compras?|s[uú]per))?|compras?)\b'
    r'|\b(s[uú]per(?:mercado)?|lista(?:\s+de\s+(?:compras?|s[uú]per))?)\b'
    r'.*\b(mostrar?|ver?\b|muestra[s]?)',
    re.IGNORECASE
)

QUANTITY_RE = re.compile(
    r'^(\d+(?:[,\.]\d+)?)\s*'
    r'(kilos?|kg|gramos?|g|litros?|lt|l(?=\s)|unidades?|u(?=\s)|'
    r'paquetes?|botellas?|latas?|docenas?|cajas?|sobres?|bolsas?|tazas?|cucharadas?)\s*(?:de\s+)?',
    re.IGNORECASE
)

def is_shopping_view_intent(text: str) -> bool:
    """True si el usuario quiere VER la lista (no agregar)."""
    return bool(SHOPPING_VIEW_TRIGGER.search(text))

def is_shopping_add_intent(text: str) -> bool:
    """True si el usuario quiere AGREGAR ítems a la lista."""
    return bool(SHOPPING_ADD_TRIGGER.search(text))

def parse_shopping_items(text: str) -> list:
    """Devuelve lista de {item, quantity} desde texto como 'agrega 2 litros de leche y pan'."""
    # Quitar palabras de intención
    cleaned = re.sub(
        r'\b(agrega[r]?|a[ñn]ade|añadir|al?\s+s[uú]per(?:mercado)?|a\s+la\s+lista(?:\s+de\s+compras?)?)\b',
        '', text, flags=re.IGNORECASE
    ).strip().strip(',').strip()

    # Separar por coma y "y"
    parts = re.split(r',\s*|\s+y\s+', cleaned)
    items = []
    for part in parts:
        part = part.strip().strip(',').strip()
        if not part:
            continue
        m = QUANTITY_RE.match(part)
        if m:
            quantity = f"{m.group(1)} {m.group(2)}"
            item = part[m.end():].strip()
        else:
            # Número suelto al inicio ("3 yogures")
            m2 = re.match(r'^(\d+)\s+', part)
            if m2:
                quantity = m2.group(1)
                item = part[m2.end():].strip()
            else:
                quantity = None
                item = part
        if item:
            items.append({"item": item, "quantity": quantity})
    return items


# ── Recordatorio: disparador ──────────────────────────────────────────────────

async def fire_reminder(bot, chat_id: int, message: str, job_id: str,
                        photo_file_id: str | None = None):
    template = random.choice(REMINDER_TEMPLATES)
    text = template.format(message=message)
    if photo_file_id:
        await bot.send_photo(chat_id=chat_id, photo=photo_file_id,
                             caption=text, parse_mode="Markdown")
    else:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
    db_delete(job_id)


# ── Confirmación de recordatorio ──────────────────────────────────────────────

async def ask_confirmation(reply_target, chat_id: int, run_date: datetime,
                           message: str, photo_file_id: str | None = None):
    uid   = uuid.uuid4().hex[:8]
    key   = f"{chat_id}:{uid}"
    pending_confirm[key] = {"run_date": run_date, "message": message, "photo_file_id": photo_file_id}
    fecha = run_date.strftime("%d/%m/%Y a las %H:%M")
    photo_note = " _(📷 con foto)_" if photo_file_id else ""
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Confirmar", callback_data=f"confirm:{key}"),
        InlineKeyboardButton("❌ Cancelar",  callback_data=f"cancel:{key}"),
    ]])
    txt = f"📋 *¿Confirmo este recordatorio?*\n\n⏰ {fecha}\n📌 _{message}_{photo_note}"

    if hasattr(reply_target, 'edit_message_text'):
        await reply_target.edit_message_text(txt, parse_mode="Markdown", reply_markup=keyboard)
    else:
        await reply_target.reply_text(txt, parse_mode="Markdown", reply_markup=keyboard)


# ── Lógica central de recordatorios ──────────────────────────────────────────

async def _process_text(update: Update, context: ContextTypes.DEFAULT_TYPE,
                        text: str, photo_file_id: str | None = None):
    chat_id = update.effective_chat.id
    now     = now_local()

    run_date, message, ambiguous_h, ambiguous_m, date_only = parse_reminder(text)

    # Fecha encontrada pero sin hora → preguntar hora
    if date_only is not None:
        pending_time[chat_id] = {"date": date_only, "message": message, "photo_file_id": photo_file_id}
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Cancelar", callback_data=f"cancel_time:{chat_id}")
        ]])
        await update.message.reply_text(
            f"📅 Entendí: *{fecha_es(date_only)}*\n"
            f"📌 _{message}_\n\n"
            f"¿A qué hora te lo recuerdo?",
            parse_mode="Markdown", reply_markup=keyboard
        )
        return

    if ambiguous_h is not None:
        uid = uuid.uuid4().hex[:8]
        key = f"{chat_id}:{uid}"
        pending_ampm[key] = {
            "hour": ambiguous_h, "minute": ambiguous_m,
            "message": message,  "processed": preprocess_time(text),
            "photo_file_id": photo_file_id
        }
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"🌅 {ambiguous_h:02d}:{ambiguous_m:02d} AM",
                                 callback_data=f"ampm:{key}:0"),
            InlineKeyboardButton(f"☀️ {ambiguous_h + 12:02d}:{ambiguous_m:02d} PM",
                                 callback_data=f"ampm:{key}:1"),
        ]])
        await update.message.reply_text(
            f"⏰ ¿A qué hora es?\n*{ambiguous_h}:{ambiguous_m:02d}*",
            parse_mode="Markdown", reply_markup=keyboard
        )
        return

    if not run_date:
        # Nada reconocido → flujo guiado: preguntar día primero
        pending_time[chat_id] = {
            "date": None, "message": message,
            "photo_file_id": photo_file_id, "step": "date"
        }
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Cancelar", callback_data=f"cancel_time:{chat_id}")
        ]])
        await update.message.reply_text(
            f"📌 _{message}_\n\n"
            "📅 ¿Para qué día te lo recuerdo?\n"
            "_Ej: \"mañana\", \"el lunes\", \"el 15 de junio\"_",
            parse_mode="Markdown", reply_markup=keyboard
        )
        return

    if run_date < now:
        tomorrow = run_date + timedelta(days=1)
        uid = uuid.uuid4().hex[:8]
        key = f"{chat_id}:{uid}"
        pending_confirm[key] = {"run_date": tomorrow, "message": message, "photo_file_id": photo_file_id}
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("📅 Sí, mañana",   callback_data=f"confirm:{key}"),
            InlineKeyboardButton("❌ No, cancelar", callback_data=f"cancel:{key}"),
        ]])
        await update.message.reply_text(
            f"⚠️ Las *{run_date.strftime('%H:%M')}* ya pasaron hoy.\n"
            f"¿Lo agendo para mañana a las *{run_date.strftime('%H:%M')}*?",
            parse_mode="Markdown", reply_markup=keyboard
        )
        return

    await ask_confirmation(update.message, chat_id, run_date, message, photo_file_id)


# ── Resolución de fecha/hora pendiente ───────────────────────────────────────

def _extract_time(processed: str):
    """Extrae (hour, minute) del texto preprocesado. Retorna (None, None) si no hay."""
    tm = re.search(r'\b(\d{1,2}):(\d{2})\b', processed)
    if tm:
        return int(tm.group(1)), int(tm.group(2))
    m2 = re.search(r'\ba\s+las?\s+(\d{1,2})\b', processed, re.IGNORECASE)
    if m2:
        return int(m2.group(1)), 0
    m3 = re.match(r'^\s*(\d{1,2})\s*$', processed)   # respuesta rápida: "10"
    if m3:
        return int(m3.group(1)), 0
    return None, None


async def _resolve_pending_time(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                text: str, photo_file_id=None) -> bool:
    """
    Flujo guiado para completar información faltante del recordatorio.
      step='date' → esperamos el día
      step='time' → esperamos la hora
    Retorna True si consumió el mensaje, False si no había pending_time.
    """
    chat_id   = update.effective_chat.id
    pt        = pending_time.get(chat_id)
    if not pt:
        return False

    processed = preprocess_time(text)
    now       = now_local()
    step      = pt.get("step", "time")

    # ── PASO: necesitamos el DÍA ─────────────────────────────────────────────
    if step == "date":
        dp = _parse_date_es(processed)

        if dp is None:
            await update.message.reply_text(
                "⚠️ No pude identificar el día.\n"
                "Intentá: _\"mañana\"_, _\"el lunes\"_, _\"el 15 de junio\"_",
                parse_mode="Markdown"
            )
            return True   # seguimos esperando

        # Si la respuesta incluye también la hora → resolver todo de una
        if _has_explicit_time(processed, text):
            h, mins = _extract_time(processed)
            if h is not None:
                run_date = localize_dt(datetime(dp.date().year, dp.date().month, dp.date().day, h, mins, 0))
                if run_date < now:
                    run_date = localize_dt(datetime(dp.year + 1, dp.month, dp.day, h, mins, 0))
                pending_time.pop(chat_id, None)
                await ask_confirmation(update.message, chat_id, run_date,
                                       pt["message"], pt.get("photo_file_id") or photo_file_id)
                return True

        # Solo día → guardar y pedir hora
        pending_time[chat_id] = {
            "date": localize_dt(dp.replace(tzinfo=None)),
            "message": pt["message"],
            "photo_file_id": pt.get("photo_file_id") or photo_file_id,
            "step": "time"
        }
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Cancelar", callback_data=f"cancel_time:{chat_id}")
        ]])
        await update.message.reply_text(
            f"📅 *{fecha_es(dp)}*\n⏰ ¿A qué hora te lo recuerdo?",
            parse_mode="Markdown", reply_markup=keyboard
        )
        return True

    # ── PASO: necesitamos la HORA ─────────────────────────────────────────────
    h, mins = _extract_time(processed)

    if h is None:
        await update.message.reply_text(
            "⚠️ No pude identificar la hora.\n"
            "Decime la hora, por ejemplo: _\"a las 10\"_, _\"9:30\"_ o simplemente _\"10\"_",
            parse_mode="Markdown"
        )
        return True

    # Hora ambigua → preguntar AM/PM conservando la fecha base
    if is_hour_ambiguous(h, processed) and not has_explicit_ampm(text):
        uid = uuid.uuid4().hex[:8]
        key = f"{chat_id}:{uid}"
        pending_ampm[key] = {
            "hour": h, "minute": mins,
            "message": pt["message"],
            "processed": processed,
            "photo_file_id": pt.get("photo_file_id") or photo_file_id,
            "base_date": pt["date"].date()
        }
        pending_time.pop(chat_id, None)
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"🌅 {h:02d}:{mins:02d} AM", callback_data=f"ampm:{key}:0"),
            InlineKeyboardButton(f"☀️ {h+12:02d}:{mins:02d} PM", callback_data=f"ampm:{key}:1"),
        ]])
        await update.message.reply_text(
            f"⏰ ¿A qué hora es?\n*{h}:{mins:02d}*",
            parse_mode="Markdown", reply_markup=keyboard
        )
        return True

    # Combinar fecha guardada + hora
    base_date = pt["date"].date()
    run_date  = localize_dt(datetime(base_date.year, base_date.month, base_date.day, h, mins, 0))
    if run_date < now:
        run_date = localize_dt(datetime(base_date.year + 1, base_date.month, base_date.day, h, mins, 0))

    pending_time.pop(chat_id, None)
    await ask_confirmation(update.message, chat_id, run_date,
                           pt["message"], pt.get("photo_file_id") or photo_file_id)
    return True


# ── Handlers principales ──────────────────────────────────────────────────────

WELCOME_TEXT = (
    "👋 ¡Hola! Soy tu asistente personal.\n"
    "_Podés hablarme por voz o texto en cualquier momento._\n\n"

    "⏰ *RECORDATORIOS*\n"
    "• _\"Recordame mañana a las 9 que tengo reunión\"_\n"
    "• _\"Avisame en 20 minutos\"_\n"
    "• _\"Recuérdame el 15 de junio el cumple de Jaime\"_\n\n"
    "/lista — 📋 Ver recordatorios pendientes\n"
    "/resumen — 🌅 Recordatorios de hoy\n"
    "/cancelar — 🗑 Borrar todos los recordatorios\n\n"

    "🛒 *LISTA DEL SÚPER*\n"
    "• _\"Agrega leche, pan y 2 kilos de arroz\"_\n"
    "• _\"Me muestras la lista del súper?\"_\n\n"
    "/super — 📋 Ver lista\n"
    "/super\\_listo — ✅ Compra completada\n"
    "/super\\_compartir — 🔗 Compartir lista\n"
    "/super\\_unirse — 🤝 Unirse a lista de otro\n"
    "/super\\_salir — 🚪 Volver a mi lista propia"
)

GREETING_RE = re.compile(
    r'^\s*(hola+|h[eé]llo+|hi+|hey+|buenas?'
    r'|buen[oa]s?\s+(?:d[ií]as?|tardes?|noches?)'
    r'|saludos?|qu[eé]\s+tal|buenos?|ey+|ola)\s*[!¡?.]*\s*$',
    re.IGNORECASE
)

async def _send_welcome(bot, chat_id: int):
    await bot.send_message(chat_id=chat_id, text=WELCOME_TEXT, parse_mode="Markdown")

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    db_register_user(chat_id)

    # Deep link: /start LISTA-XXXXXX → unirse automáticamente
    if context.args and context.args[0].startswith("LISTA-"):
        code  = context.args[0]
        owner = db_get_share_code(code)
        if not owner:
            await update.message.reply_text(
                "❌ El link expiró o es inválido.\n"
                "Pedile a tu contacto que genere uno nuevo con /super\\_compartir.",
                parse_mode="Markdown"
            )
        elif owner == chat_id:
            await update.message.reply_text("⚠️ Ese es tu propio link.")
        else:
            db_set_list_owner(chat_id, owner)
            items = db_get_shopping_items(chat_id)
            await update.message.reply_text(
                f"✅ ¡Listo! Ahora compartís la lista del súper.\n"
                f"Tiene *{len(items)}* ítem(s) actualmente.\n\n"
                f"Usá /super para verla o /super\\_salir para volver a tu lista propia.",
                parse_mode="Markdown"
            )
        return

    await update.message.reply_text(WELCOME_TEXT, parse_mode="Markdown")


async def cmd_lista(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    jobs = [j for j in scheduler.get_jobs()
            if j.args and len(j.args) >= 2 and j.args[1] == chat_id
            and j.id != "daily_summary"]
    if not jobs:
        await update.message.reply_text("📭 No tenés recordatorios pendientes.")
        return
    lines = ["📋 *Recordatorios pendientes:*\n"]
    for j in sorted(jobs, key=lambda x: x.next_run_time):
        fecha = j.next_run_time.strftime("%d/%m/%Y %H:%M")
        lines.append(f"• {fecha} — {j.args[2]}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    jobs = [j for j in scheduler.get_jobs()
            if j.args and len(j.args) >= 2 and j.args[1] == chat_id
            and j.id != "daily_summary"]
    for j in jobs:
        db_delete(j.id)
        j.remove()
    await update.message.reply_text(
        f"🗑️ {len(jobs)} recordatorio(s) cancelado(s)." if jobs else "📭 No había recordatorios activos."
    )


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db_register_user(update.effective_chat.id)
    await update.message.reply_text("🎤 Transcribiendo tu audio...")
    tmp_path = None
    try:
        voice   = update.message.voice or update.message.audio
        tg_file = await context.bot.get_file(voice.file_id)
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp_path = tmp.name
        await tg_file.download_to_drive(tmp_path)

        loop = asyncio.get_event_loop()
        try:
            text = await asyncio.wait_for(
                loop.run_in_executor(None, _transcribe, tmp_path),
                timeout=GROQ_TIMEOUT
            )
        except asyncio.TimeoutError:
            await update.message.reply_text(
                f"⏱️ La transcripción tardó más de {int(GROQ_TIMEOUT)}s. "
                "Intentá con un audio más corto."
            )
            return

        display = preprocess_time(text)
        await update.message.reply_text(f"📝 Entendí: _{display}_", parse_mode="Markdown")

        if await _resolve_pending_time(update, context, text):
            return
        if is_shopping_view_intent(text):
            await cmd_super(update, context)
        elif is_shopping_add_intent(text):
            await _handle_shopping_add(update, text)
        else:
            await _process_text(update, context, text)

    except Exception:
        logger.exception("Error en handle_voice")
        await update.message.reply_text("❌ Ocurrió un error. Intentá de nuevo.")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db_register_user(update.effective_chat.id)
    text = update.message.text
    if GREETING_RE.match(text):
        await update.message.reply_text(WELCOME_TEXT, parse_mode="Markdown")
        return
    if await _resolve_pending_time(update, context, text):
        return
    if is_shopping_view_intent(text):          # "me muestras la lista del super?"
        await cmd_super(update, context)
    elif is_shopping_add_intent(text):         # "agrega leche"
        await _handle_shopping_add(update, text)
    else:
        await _process_text(update, context, text)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Foto con caption → recordatorio con imagen adjunta."""
    db_register_user(update.effective_chat.id)
    caption = update.message.caption
    if not caption:
        await update.message.reply_text(
            "📷 Foto recibida.\nAgrégale un caption con la hora del recordatorio, "
            "por ejemplo: _\"Recordame mañana a las 9 llevar esto\"_",
            parse_mode="Markdown"
        )
        return
    photo_file_id = update.message.photo[-1].file_id   # mejor resolución
    display = preprocess_time(caption)
    await update.message.reply_text(f"📝 Entendí: _{display}_", parse_mode="Markdown")
    await _process_text(update, context, caption, photo_file_id=photo_file_id)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    now  = now_local()

    # ── Confirmar recordatorio
    if data.startswith("confirm:"):
        key     = data[len("confirm:"):]
        cid     = int(key.split(":")[0])
        pending = pending_confirm.pop(key, None)
        if not pending:
            await query.edit_message_text("⚠️ Este recordatorio ya fue procesado o expiró.")
            return
        run_date      = pending["run_date"]
        message       = pending["message"]
        photo_file_id = pending.get("photo_file_id")
        job_id        = f"rem_{cid}_{run_date.timestamp()}"
        scheduler.add_job(
            fire_reminder, "date", run_date=run_date,
            args=[context.bot, cid, message, job_id, photo_file_id],
            id=job_id, replace_existing=True
        )
        db_save(cid, message, run_date, job_id, photo_file_id)
        fecha = run_date.strftime("%d/%m/%Y a las %H:%M")
        photo_note = " _(📷 con foto)_" if photo_file_id else ""
        await query.edit_message_text(
            f"✅ *Recordatorio agendado*\n⏰ {fecha}\n📌 _{message}_{photo_note}",
            parse_mode="Markdown"
        )
        # Confirmación amigable en nuevo mensaje
        confirmaciones = [
            f"¡Listo! 🎉 Te aviso el *{fecha}*.",
            f"¡Anotado! 📝 Te recuerdo el *{fecha}*.",
            f"¡Perfecto! ✅ No te vas a olvidar — te aviso el *{fecha}*.",
            f"¡Hecho! 🙌 Recordatorio guardado para el *{fecha}*.",
        ]
        await context.bot.send_message(
            chat_id=cid,
            text=random.choice(confirmaciones),
            parse_mode="Markdown"
        )

    # ── Cancelar confirmación
    elif data.startswith("cancel:"):
        key = data[len("cancel:"):]
        pending_confirm.pop(key, None)
        pending_ampm.pop(key, None)
        await query.edit_message_text("❌ Recordatorio cancelado.")

    # ── Cancelar espera de hora (date_only flow)
    elif data.startswith("cancel_time:"):
        cid = int(data.split(":")[1])
        pending_time.pop(cid, None)
        await query.edit_message_text("❌ Recordatorio cancelado.")

    # ── Resolver AM/PM
    elif data.startswith("ampm:"):
        parts = data.split(":")
        is_pm = parts[-1] == "1"
        key   = ":".join(parts[1:-1])
        cid   = int(key.split(":")[0])
        ph    = pending_ampm.pop(key, None)
        if not ph:
            await query.edit_message_text("⚠️ Esta pregunta ya fue respondida o expiró.")
            return
        h    = ph["hour"] + (12 if is_pm else 0)
        mins = ph["minute"]

        if "base_date" in ph:
            # Viene del flujo date_only → usar fecha guardada, no calcular desde hoy
            bd   = ph["base_date"]
            base = localize_dt(datetime(bd.year, bd.month, bd.day, h, mins, 0))
            if base < now:
                base = localize_dt(datetime(bd.year + 1, bd.month, bd.day, h, mins, 0))
        else:
            base = _build_datetime(h, mins, ph["processed"], now)
            if base < now:
                base += timedelta(days=1)

        await ask_confirmation(query, cid, base, ph["message"], ph.get("photo_file_id"))

    # ── Borrar ítem del súper
    elif data.startswith("del_item:"):
        item_id = int(data.split(":")[1])
        db_delete_shopping_item(item_id)
        await _edit_shopping_message(query, query.message.chat_id)

    # ── Compra completada desde botón de la lista
    elif data == "super_listo_confirm":
        cid   = query.message.chat_id
        items = db_get_shopping_items(cid)
        db_clear_shopping_list(cid)
        await query.edit_message_text(
            f"✅ ¡Compra completada! Se borraron {len(items)} ítem(s).\n"
            "La lista está lista para la próxima vez 🛒"
        )

    # ── Salir del súper → bienvenida
    elif data == "goto_menu":
        await query.edit_message_text("👋 ¡Hasta luego del súper!")
        await _send_welcome(context.bot, query.message.chat_id)

    # ── Botón sin acción
    elif data == "noop":
        pass


# ── Shopping handlers ─────────────────────────────────────────────────────────

async def _handle_shopping_add(update: Update, text: str):
    chat_id = update.effective_chat.id
    parsed  = parse_shopping_items(text)
    if not parsed:
        await update.message.reply_text(
            "⚠️ No pude identificar los ítems. "
            "Intentá: _\"Agrega leche, pan y 2 kilos de arroz\"_",
            parse_mode="Markdown"
        )
        return
    for p in parsed:
        db_add_shopping_item(chat_id, p["item"], p["quantity"])

    names = [f"*{p['item']}*" + (f" ({p['quantity']})" if p["quantity"] else "")
             for p in parsed]
    await update.message.reply_text(
        f"🛒 Agregado al súper:\n" + "\n".join(f"• {n}" for n in names),
        parse_mode="Markdown"
    )


async def cmd_super(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _show_shopping_list(update.message, update.effective_chat.id)


async def cmd_super_compartir(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    db_register_user(chat_id)
    try:
        username  = BOT_USERNAME or (await context.bot.get_me()).username
        code      = generate_share_code()
        db_save_share_code(code, chat_id)
        deep_link = f"https://t.me/{username}?start={code}"
        await update.message.reply_text(
            f"🔗 <b>Compartir lista del súper</b>\n\n"
            f"Mandá este link a quien quieras:\n"
            f"{deep_link}\n\n"
            f"Al tocarlo se une automáticamente — sin escribir nada.\n"
            f"<i>El link expira en 24 horas.</i>",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.exception(f"Error en cmd_super_compartir: {e}")
        await update.message.reply_text(f"❌ Error: {e}")


async def cmd_super_unirse(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    db_register_user(chat_id)

    if not context.args:
        await update.message.reply_text(
            "Usá: `/super_unirse LISTA-XXXXXX`", parse_mode="Markdown"
        )
        return

    code  = context.args[0].upper()
    owner = db_get_share_code(code)

    if not owner:
        await update.message.reply_text(
            "❌ Código inválido o expirado.\n"
            "Pedile a tu contacto que genere uno nuevo con `/super_compartir`.",
            parse_mode="Markdown"
        )
        return

    if owner == chat_id:
        await update.message.reply_text("⚠️ Ese es tu propio código.")
        return

    db_set_list_owner(chat_id, owner)
    items = db_get_shopping_items(chat_id)
    await update.message.reply_text(
        f"✅ ¡Listo! Ahora compartís la lista del súper.\n"
        f"Tiene *{len(items)}* ítem(s) actualmente.\n\n"
        f"Usá `/super_salir` cuando quieras volver a tu lista propia.",
        parse_mode="Markdown"
    )


async def cmd_super_salir(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    db_set_list_owner(chat_id, None)
    await update.message.reply_text(
        "✅ Ahora usás tu propia lista del súper.\n"
        "Los ítems que agregaste siguen en la lista compartida.",
        parse_mode="Markdown"
    )


async def cmd_super_listo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    items   = db_get_shopping_items(chat_id)
    if not items:
        await update.message.reply_text("📭 La lista ya estaba vacía.")
        return
    db_clear_shopping_list(chat_id)
    await update.message.reply_text(
        f"✅ ¡Compra completada! Se borraron {len(items)} ítem(s).\n"
        "La lista está lista para la próxima vez 🛒"
    )


async def _show_shopping_list(target, chat_id: int):
    items = db_get_shopping_items(chat_id)
    if not items:
        await target.reply_text(
            "📭 La lista del súper está vacía.\n"
            "Agregá ítems con voz o texto, por ejemplo:\n"
            "_\"Agrega leche y pan\"_",
            parse_mode="Markdown"
        )
        return

    owner    = db_get_list_owner(chat_id)
    txt, kbd = _build_shopping_view(items, owner != chat_id)

    await target.reply_text(txt, parse_mode="Markdown", reply_markup=kbd)


async def _edit_shopping_message(query, chat_id: int):
    items = db_get_shopping_items(chat_id)
    if not items:
        await query.edit_message_text("🛒 Lista vacía. ¡Todo comprado! ✅")
        return

    owner    = db_get_list_owner(chat_id)
    txt, kbd = _build_shopping_view(items, owner != chat_id)
    await query.edit_message_text(txt, parse_mode="Markdown", reply_markup=kbd)


def _build_shopping_view(items: list, shared: bool):
    """Construye el texto y teclado de la lista del súper."""
    header = "🛒 *Lista del súper* 🔗 _(compartida)_\n" if shared else "🛒 *Lista del súper:*\n"
    lines  = [header]

    # Una fila por ítem: [nombre del ítem (solo texto)] [❌]
    btn_rows = []
    for i, (row_id, item, qty) in enumerate(items, 1):
        label = f"{item}" + (f"  ({qty})" if qty else "")
        lines.append(f"{i}. {label}")
        btn_rows.append([
            InlineKeyboardButton(f"  {i}. {label}", callback_data="noop"),
            InlineKeyboardButton("❌", callback_data=f"del_item:{row_id}"),
        ])

    btn_rows.append([
        InlineKeyboardButton("✅ Listo, compré todo",  callback_data="super_listo_confirm"),
        InlineKeyboardButton("🏠 Inicio", callback_data="goto_menu"),
    ])

    txt = "\n".join(lines) + "\n\n_Tocá ❌ para eliminar un ítem._"
    return txt, InlineKeyboardMarkup(btn_rows)


# ── Resumen diario ────────────────────────────────────────────────────────────

async def send_daily_summary(bot, chat_id: int | None = None):
    targets = [chat_id] if chat_id else db_get_all_users()
    for cid in targets:
        reminders = db_get_todays_reminders(cid)
        if not reminders:
            if chat_id:
                await bot.send_message(
                    chat_id=cid,
                    text=f"{_greeting()}\n\n📭 No tenés recordatorios para hoy. ¡Buen día!",
                    parse_mode="Markdown"
                )
            continue
        lines = [f"{_greeting()}\n\n📋 *Tus recordatorios para hoy:*\n"]
        for rd, msg in reminders:
            lines.append(f"• *{rd.strftime('%H:%M')}* — {msg}")
        lines.append("\n¡Que tengas un gran día! 💪")
        try:
            await bot.send_message(chat_id=cid, text="\n".join(lines), parse_mode="Markdown")
        except Exception:
            logger.exception(f"Error enviando resumen a {cid}")


async def cmd_resumen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db_register_user(update.effective_chat.id)
    await send_daily_summary(context.bot, update.effective_chat.id)


# ── Transcripción ─────────────────────────────────────────────────────────────

def _transcribe(file_path: str) -> str:
    with open(file_path, "rb") as f:
        result = groq_client.audio.transcriptions.create(
            model="whisper-large-v3", file=f, language="es"
        )
    return result.text


# ── Arranque ──────────────────────────────────────────────────────────────────

async def post_init(application: Application):
    global BOT_USERNAME
    init_db()        # siempre primero — crea tablas antes de cualquier otra cosa
    scheduler.start()
    try:
        me = await application.bot.get_me()
        BOT_USERNAME = me.username
        logger.info(f"Bot username: @{BOT_USERNAME}")
    except Exception:
        logger.exception("No se pudo obtener el username del bot — deep links desactivados")
    now   = now_local()
    rows  = db_load_all()
    count = 0
    for chat_id, message, run_date_str, job_id, photo_file_id in rows:
        run_date = datetime.fromisoformat(run_date_str)
        if run_date.tzinfo is None:
            run_date = localize_dt(run_date)
        if run_date > now:
            scheduler.add_job(
                fire_reminder, "date", run_date=run_date,
                args=[application.bot, chat_id, message, job_id, photo_file_id],
                id=job_id, replace_existing=True
            )
            count += 1
        else:
            db_delete(job_id)

    summary_h, summary_m = map(int, SUMMARY_TIME.split(":"))
    scheduler.add_job(
        send_daily_summary, "cron",
        hour=summary_h, minute=summary_m,
        args=[application.bot, None],
        id="daily_summary", replace_existing=True
    )
    logger.info(f"✅ Bot listo. {count} recordatorio(s) recargado(s). Resumen a las {SUMMARY_TIME}.")


async def post_shutdown(application: Application):
    scheduler.shutdown()


def main():
    if not TELEGRAM_TOKEN or not GROQ_API_KEY:
        raise ValueError("Faltan TELEGRAM_TOKEN y/o GROQ_API_KEY en .env")

    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("lista",       cmd_lista))
    app.add_handler(CommandHandler("cancelar",    cmd_cancelar))
    app.add_handler(CommandHandler("resumen",     cmd_resumen))
    app.add_handler(CommandHandler("super",           cmd_super))
    app.add_handler(CommandHandler("super_listo",     cmd_super_listo))
    app.add_handler(CommandHandler("super_compartir", cmd_super_compartir))
    app.add_handler(CommandHandler("super_unirse",    cmd_super_unirse))
    app.add_handler(CommandHandler("super_salir",     cmd_super_salir))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(handle_callback))

    logger.info("🤖 Bot iniciado. Esperando mensajes...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    asyncio.set_event_loop(asyncio.new_event_loop())
    main()
