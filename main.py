import os
import html
import json
import functools
import contextvars
import base64
import logging
import secrets
import asyncio
from io import BytesIO
from copy import deepcopy
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.constants import ParseMode, ChatAction
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================================================
# 1. KONFIGURASI DARI .ENV
# =========================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
KLIQRIS_API_KEY = os.getenv("KLIQRIS_API_KEY", "").strip()
KLIQRIS_MERCHANT_ID = os.getenv("KLIQRIS_MERCHANT_ID", "").strip()
ADMIN_CONTACT = os.getenv("ADMIN_CONTACT", "").strip()

ADMIN_IDS = {
    value.strip()
    for value in os.getenv("ADMIN_IDS", "").split(",")
    if value.strip()
}

VIP11_GROUP_IDS = list(dict.fromkeys(
    value.strip()
    for value in os.getenv("VIP11_GROUP_IDS", "").split(",")
    if value.strip()
))

# Tetap dibaca dari .env.
# Daftar ini berisi 9 ID pada konfigurasi yang diberikan.
VIP8_GROUP_IDS = list(dict.fromkeys(
    value.strip()
    for value in os.getenv("VIP8_GROUP_IDS", "").split(",")
    if value.strip()
))

VIP11_GROUP_NAMES = [
    value.strip()
    for value in os.getenv("VIP11_GROUP_NAMES", "").split("|")
    if value.strip()
]

BACKUP_GROUP_ID = os.getenv("BACKUP_GROUP_ID", "").strip()

PRICE_PER_GROUP = max(
    1, int(os.getenv("PRICE_PER_GROUP", "10000"))
)

INVITE_MINUTES = max(
    1, int(os.getenv("INVITE_MINUTES", "60"))
)

PAYMENT_CHECK_INTERVAL = max(
    5, int(os.getenv("PAYMENT_CHECK_INTERVAL", "5"))
)

# Awalan Order ID, contoh: BCK8953615855179214500176124D
ORDER_ID_PREFIX = (
    "".join(
        ch for ch in os.getenv("ORDER_ID_PREFIX", "BCK").strip()
        if ch.isalnum()
    ).upper()
    or "BCK"
)

PAYMENT_TIMEOUT_MINUTES = max(
    1, int(os.getenv("PAYMENT_TIMEOUT_MINUTES", "15"))
)

# Invoice yang sudah ditandai EXPIRED tetap dicek ulang ke KlikQRIS
# selama jam ini, agar pembayaran yang masuk terlambat tidak terlewat.
LATE_PAYMENT_WINDOW_HOURS = max(
    1, int(os.getenv("LATE_PAYMENT_WINDOW_HOURS", "24"))
)

# Gunakan base URL dari .env.
KLIQRIS_BASE_URL = os.getenv(
    "KLIQRIS_BASE_URL",
    "https://klikqris.com/api",
).strip().rstrip("/")

KLIQRIS_CREATE_URL = f"{KLIQRIS_BASE_URL}/qris/create"
KLIQRIS_STATUS_URL = f"{KLIQRIS_BASE_URL}/qris/status"

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

TRANSACTIONS_FILE = DATA_DIR / "transactions.json"
PAID_GROUPS_FILE = DATA_DIR / "paid_groups.json"
BACKUP_USERS_FILE = DATA_DIR / "backup_users.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("vip_backup_bot")

DATA_LOCK = asyncio.Lock()
PAYMENT_MONITOR_LOCK = asyncio.Lock()
BACKUP_DELIVERY_LOCK = asyncio.Lock()


# =========================================================
# 2. UTILITAS
# =========================================================

def now_utc():
    return datetime.now(timezone.utc)


def iso_now():
    return now_utc().isoformat()


def parse_datetime(value):
    if not value:
        return None

    try:
        result = datetime.fromisoformat(value)

        if result.tzinfo is None:
            result = result.replace(tzinfo=timezone.utc)

        return result.astimezone(timezone.utc)

    except (ValueError, TypeError):
        return None


def is_within_late_window(tx):
    expires_at = parse_datetime(tx.get("expires_at"))

    if not expires_at:
        return False

    return now_utc() - expires_at <= timedelta(
        hours=LATE_PAYMENT_WINDOW_HOURS
    )


def format_rupiah(amount):
    return "Rp" + f"{int(amount):,}".replace(",", ".")


# ---------------------------------------------------------
# Tampilan QRIS (mengikuti desain bot di screenshot)
# ---------------------------------------------------------

WIB = timezone(timedelta(hours=7))


def format_rupiah_spaced(amount):
    """30000 -> 'Rp 30.000' (dipakai pada pesan pilihan paket)."""
    return "Rp " + f"{int(amount):,}".replace(",", ".")


def format_nominal(amount):
    """30395 -> 'Rp 30,395' (dipakai pada caption QRIS)."""
    return "Rp " + f"{int(amount):,}"


# ---------------------------------------------------------
# Indikator interaktif ("sedang mengetik...", "mengirim foto...")
# ---------------------------------------------------------

_ACTION_ACTIVE = contextvars.ContextVar("chat_action_active", default=False)


@asynccontextmanager
async def chat_action(update, action=ChatAction.TYPING):
    """
    Menampilkan indikator aksi Telegram selama blok di dalamnya berjalan.
    Telegram hanya menampilkan indikator ~5 detik, jadi diulang tiap 4 detik.
    """
    chat = update.effective_chat

    if chat is None or _ACTION_ACTIVE.get():
        yield
        return

    token = _ACTION_ACTIVE.set(True)

    async def keep_alive():
        while True:
            try:
                await chat.send_action(action)
            except TelegramError:
                pass

            await asyncio.sleep(4)

    task = asyncio.create_task(keep_alive())

    try:
        yield

    finally:
        task.cancel()

        with suppress(asyncio.CancelledError):
            await task

        _ACTION_ACTIVE.reset(token)


def with_action(action=ChatAction.TYPING):
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(update, context, *args, **kwargs):
            async with chat_action(update, action):
                return await func(update, context, *args, **kwargs)

        return wrapper

    return decorator


async def safe_delete(message):
    try:
        await message.delete()
    except TelegramError:
        pass


def package_title(group_count):
    return f"Tambahan Paket VIP {int(group_count)} Grup"


def build_qris_caption(tx):
    expires = parse_datetime(tx.get("expires_at"))

    expires_text = (
        expires.astimezone(WIB).strftime("%Y-%m-%d %H:%M:%S")
        if expires
        else "-"
    )

    return (
        f"\U0001F4B3 <b>{html.escape(package_title(tx.get('group_count', 1)))}</b>\n\n"
        f"Nominal: <b>{format_nominal(tx['amount'])}</b>\n"
        f"Order ID: <code>{html.escape(str(tx['order_id']))}</code>\n"
        f"Expired: <b>{expires_text}</b>\n\n"
        "Silakan scan QRIS di atas dan bayar sesuai nominal yang tertera.\n\n"
        "\u2705 Setelah pembayaran berhasil, bot akan mengecek "
        "pembayaran secara otomatis.\n"
        "\u274C Tidak perlu mengirim screenshot bukti transfer."
    )


async def send_qris_photo(message, tx):
    """Kirim gambar QRIS + caption. Return False jika semua cara gagal."""
    caption = build_qris_caption(tx)
    image_value = tx.get("qris_image") or ""
    qris_url = tx.get("qris_url") or ""

    # 1) Gambar base64 (data URI)
    if image_value.startswith("data:image/"):
        try:
            _, encoded = image_value.split(",", 1)
            image_file = BytesIO(base64.b64decode(encoded))
            image_file.name = "qris.png"

            await message.reply_photo(
                photo=image_file,
                caption=caption,
                parse_mode=ParseMode.HTML,
            )
            return True

        except (ValueError, TelegramError):
            logger.exception("Gagal mengirim gambar QRIS base64")

    # 2) URL gambar
    for source in (image_value, qris_url):
        if source.startswith(("https://", "http://")):
            try:
                await message.reply_photo(
                    photo=source,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                )
                return True

            except TelegramError:
                logger.exception("Gagal mengirim gambar QRIS dari URL")

    # Tautan penyedia pembayaran sengaja tidak dikirim ke pengguna.
    return False


def group_name(index):
    if 0 <= index < len(VIP11_GROUP_NAMES):
        return VIP11_GROUP_NAMES[index]

    return f"Grup VIP {index + 1}"


def get_group_name(group_id):
    try:
        index = VIP11_GROUP_IDS.index(str(group_id))
        return group_name(index)
    except ValueError:
        return str(group_id)


def validate_config():
    errors = []

    required = {
        "BOT_TOKEN": BOT_TOKEN,
        "KLIQRIS_API_KEY": KLIQRIS_API_KEY,
        "KLIQRIS_MERCHANT_ID": KLIQRIS_MERCHANT_ID,
        "BACKUP_GROUP_ID": BACKUP_GROUP_ID,
    }

    for name, value in required.items():
        if not value:
            errors.append(f"{name} belum diisi")

    if len(VIP11_GROUP_IDS) != 11:
        errors.append(
            "VIP11_GROUP_IDS harus berisi tepat 11 ID unik"
        )

    try:
        for group_id in VIP11_GROUP_IDS:
            int(group_id)

        int(BACKUP_GROUP_ID)

        for group_id in VIP8_GROUP_IDS:
            int(group_id)

    except ValueError:
        errors.append("Ada ID grup yang bukan angka valid")

    if errors:
        raise RuntimeError(
            "Konfigurasi .env bermasalah:\n- "
            + "\n- ".join(errors)
        )


def load_json(path, default):
    try:
        if not path.exists():
            return deepcopy(default)

        with path.open("r", encoding="utf-8") as file:
            return json.load(file)

    except (OSError, json.JSONDecodeError):
        logger.exception("Gagal membaca file %s", path)
        return deepcopy(default)


def save_json(path, data):
    temporary = path.with_suffix(path.suffix + ".tmp")

    with temporary.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)

    temporary.replace(path)


async def get_transaction(order_id):
    async with DATA_LOCK:
        data = load_json(TRANSACTIONS_FILE, {})
        record = data.get(order_id)

        return deepcopy(record) if record else None


async def create_transaction(order_id, record):
    async with DATA_LOCK:
        data = load_json(TRANSACTIONS_FILE, {})
        data[order_id] = record
        save_json(TRANSACTIONS_FILE, data)


async def update_transaction(order_id, updates):
    async with DATA_LOCK:
        data = load_json(TRANSACTIONS_FILE, {})

        if order_id not in data:
            return False

        data[order_id].update(updates)
        data[order_id]["updated_at"] = iso_now()

        save_json(TRANSACTIONS_FILE, data)
        return True


async def get_paid_groups(user_id):
    async with DATA_LOCK:
        data = load_json(PAID_GROUPS_FILE, {})
        record = data.get(str(user_id), [])

        # Format yang ditulis add_paid_groups() berupa dictionary:
        # {"group_ids": [...], "updated_at": ..., "last_order_id": ...}
        if isinstance(record, dict):
            record = record.get("group_ids", [])

        return list(dict.fromkeys(str(x) for x in record))


async def add_paid_groups(user_id, group_ids, order_id):
    async with DATA_LOCK:
        data = load_json(PAID_GROUPS_FILE, {})
        key = str(user_id)

        record = data.get(key, [])

        # Kompatibilitas jika format lama berupa dictionary.
        if isinstance(record, dict):
            record = record.get("group_ids", [])

        record = [str(x) for x in record]

        for group_id in group_ids:
            group_id = str(group_id)

            if group_id not in record:
                record.append(group_id)

        data[key] = {
            "group_ids": record,
            "updated_at": iso_now(),
            "last_order_id": order_id,
        }

        save_json(PAID_GROUPS_FILE, data)


async def remove_paid_groups(user_id, group_ids):
    """
    Hak grup bersifat sekali pakai: setelah link undangan terkirim,
    hak dihapus. Jika member keluar dari grup, ia harus membayar lagi.
    """
    async with DATA_LOCK:
        data = load_json(PAID_GROUPS_FILE, {})
        key = str(user_id)
        record = data.get(key)

        if record is None:
            return

        current = (
            record.get("group_ids", [])
            if isinstance(record, dict)
            else list(record)
        )

        removed = {str(x) for x in group_ids}
        remaining = [str(x) for x in current if str(x) not in removed]

        if len(remaining) == len(current):
            return

        last_order = (
            record.get("last_order_id")
            if isinstance(record, dict)
            else None
        )

        data[key] = {
            "group_ids": remaining,
            "updated_at": iso_now(),
            "last_order_id": last_order,
        }

        save_json(PAID_GROUPS_FILE, data)


def consume_fulfilled_entitlements():
    """
    Dijalankan saat bot start. Hak grup dari order yang sudah selesai
    dikirim (fulfilled) dihapus, karena link hanya berlaku 1x.
    Hak dari order yang pengirimannya belum tuntas tetap dipertahankan.
    """
    transactions = load_json(TRANSACTIONS_FILE, {})
    paid = load_json(PAID_GROUPS_FILE, {})

    protected = {}

    for tx in transactions.values():
        if tx.get("status") == "PAID" and not tx.get("fulfilled"):
            protected.setdefault(str(tx.get("user_id")), set()).update(
                str(x) for x in tx.get("group_ids", [])
            )

    changed = False

    for tx in transactions.values():
        if tx.get("status") != "PAID" or not tx.get("fulfilled"):
            continue

        key = str(tx.get("user_id"))
        record = paid.get(key)

        if record is None:
            continue

        current = (
            record.get("group_ids", [])
            if isinstance(record, dict)
            else list(record)
        )

        consumed = {str(x) for x in tx.get("group_ids", [])}
        consumed -= protected.get(key, set())

        remaining = [str(x) for x in current if str(x) not in consumed]

        if len(remaining) != len(current):
            paid[key] = {
                "group_ids": remaining,
                "updated_at": iso_now(),
                "last_order_id": (
                    record.get("last_order_id")
                    if isinstance(record, dict)
                    else tx.get("order_id")
                ),
            }
            changed = True

    if changed:
        save_json(PAID_GROUPS_FILE, paid)
        logger.info("Hak grup lama yang sudah terkirim telah dihapus.")


# =========================================================
# 3. PENGECEKAN KEANGGOTAAN VIP
# =========================================================

async def check_group_membership(bot, user_id, group_id):
    """
    Hasil:
      member     = terbukti menjadi anggota
      not_member = terbukti bukan anggota
      unknown    = pemeriksaan gagal
    """
    try:
        member = await bot.get_chat_member(
            chat_id=int(group_id),
            user_id=int(user_id),
        )

        raw_status = getattr(member.status, "value", member.status)
        status = str(raw_status).lower()

        if status in {"member", "administrator", "creator"}:
            return "member"

        if status == "restricted":
            return (
                "member"
                if getattr(member, "is_member", False)
                else "not_member"
            )

        if status in {"left", "kicked", "banned"}:
            return "not_member"

        return "unknown"

    except (TelegramError, ValueError, TypeError):
        logger.warning(
            "Tidak bisa memeriksa user %s di grup %s",
            user_id,
            group_id,
            exc_info=True,
        )
        return "unknown"


async def check_all_vip_membership(bot, user_id):
    results = {}

    for index, group_id in enumerate(VIP11_GROUP_IDS):
        results[group_id] = {
            "name": group_name(index),
            "status": await check_group_membership(
                bot, user_id, group_id
            ),
        }

    return results


async def get_missing_groups(bot, user_id):
    results = await check_all_vip_membership(bot, user_id)

    missing = []
    unknown = []

    for group_id, item in results.items():
        if item["status"] == "not_member":
            missing.append(group_id)

        elif item["status"] == "unknown":
            unknown.append(group_id)

    return results, missing, unknown


async def require_vip_access(update, context):
    """
    Pengguna dapat mengakses menu jika terbukti menjadi anggota
    minimal satu dari 11 grup VIP.
    """
    user = update.effective_user

    if not user:
        return False

    results = await check_all_vip_membership(
        context.bot,
        user.id,
    )

    joined = [
        group_id
        for group_id, item in results.items()
        if item["status"] == "member"
    ]

    unknown = [
        group_id
        for group_id, item in results.items()
        if item["status"] == "unknown"
    ]

    if joined:
        return True

    if unknown:
        message = (
            "⚠️ Keanggotaan Anda belum dapat diverifikasi.\n\n"
            "Bot tidak dapat memeriksa sebagian grup VIP. "
            "Pastikan bot memiliki izin yang diperlukan, lalu coba lagi."
        )
    else:
        message = (
            "⛔ AKSES BELUM TERSEDIA\n\n"
            "Anda harus bergabung minimal di satu dari 11 grup VIP "
            "agar dapat menggunakan menu bot.\n\n"
            "Setelah bergabung, kirim /start kembali."
        )

    if update.callback_query:
        await update.callback_query.answer(
            "Keanggotaan VIP belum memenuhi syarat.",
            show_alert=True,
        )

    if update.effective_message:
        await update.effective_message.reply_text(message)

    elif update.callback_query and update.callback_query.message:
        await update.callback_query.message.reply_text(message)

    return False


# =========================================================
# 4. LINK UNDANGAN GRUP
# =========================================================

async def create_single_use_invite(bot, group_id, expire_minutes):
    """
    Tautan hanya dapat digunakan sekali dan memiliki batas waktu.
    Bot harus mempunyai izin membuat tautan undangan.
    """
    expires = now_utc() + timedelta(minutes=expire_minutes)

    invite = await bot.create_chat_invite_link(
        chat_id=int(group_id),
        expire_date=expires,
        member_limit=1,
        creates_join_request=False,
    )

    return invite.invite_link


async def send_paid_group_invites(bot, user_id, group_ids):
    """
    Membuat ulang tautan bagi grup yang sudah dibayar.
    Tidak membuat invoice baru.
    """
    sent = []
    failed = []

    for group_id in group_ids:
        try:
            link = await create_single_use_invite(
                bot,
                group_id,
                INVITE_MINUTES,
            )

            await bot.send_message(
                chat_id=user_id,
                text=(
                    f"🔐 {get_group_name(group_id)}\n\n"
                    f"{link}\n\n"
                    "Tautan hanya dapat digunakan sekali dan berlaku "
                    f"{INVITE_MINUTES} menit. Jika Anda keluar dari grup, "
                    "Anda perlu membayar lagi."
                ),
                disable_web_page_preview=True,
            )

            sent.append(group_id)

        except (TelegramError, ValueError):
            logger.exception(
                "Gagal membuat/mengirim undangan grup %s",
                group_id,
            )
            failed.append(group_id)

    return sent, failed


# =========================================================
# 5. BACKUP
# =========================================================

async def deliver_backup_link(bot, user_id):
    """
    Link backup hanya dikirim jika semua 11 grup sudah diikuti.
    """
    async with BACKUP_DELIVERY_LOCK:
        results, missing, unknown = await get_missing_groups(
            bot,
            user_id,
        )

        if unknown:
            names = [
                get_group_name(group_id)
                for group_id in unknown
            ]

            return (
                False,
                "⚠️ Bot belum dapat memverifikasi grup berikut:\n"
                + "\n".join("• " + name for name in names)
                + "\n\nCoba lagi nanti.",
            )

        if missing:
            names = [
                get_group_name(group_id)
                for group_id in missing
            ]

            return (
                False,
                "⛔ Link backup hanya tersedia setelah Anda bergabung "
                "dengan semua 11 grup VIP.\n\n"
                "Grup yang belum diikuti:\n"
                + "\n".join("• " + name for name in names)
                + "\n\nGunakan menu Beli/Akses Grup VIP untuk "
                "menyelesaikan grup yang belum Anda ikuti.",
            )

        async with DATA_LOCK:
            backup_data = load_json(BACKUP_USERS_FILE, {})
            record = backup_data.get(str(user_id), {})

            if isinstance(record, dict) and record.get("sent"):
                return (
                    False,
                    "Link backup sudah pernah dikirim oleh bot. "
                    "Jika tautan kedaluwarsa atau Anda kehilangan akses, "
                    "silakan hubungi admin.",
                )

        try:
            link = await create_single_use_invite(
                bot,
                BACKUP_GROUP_ID,
                INVITE_MINUTES,
            )

            await bot.send_message(
                chat_id=user_id,
                text=(
                    "✅ Keanggotaan seluruh 11 grup VIP telah diverifikasi.\n\n"
                    "🔗 LINK GRUP BACKUP:\n"
                    f"{link}\n\n"
                    "Tautan hanya dapat digunakan sekali dan berlaku "
                    f"{INVITE_MINUTES} menit. Jangan bagikan tautan ini."
                ),
                disable_web_page_preview=True,
            )

            async with DATA_LOCK:
                backup_data = load_json(BACKUP_USERS_FILE, {})
                backup_data[str(user_id)] = {
                    "sent": True,
                    "sent_at": iso_now(),
                }
                save_json(BACKUP_USERS_FILE, backup_data)

            return True, "Link backup berhasil dikirim."

        except (TelegramError, ValueError):
            logger.exception("Gagal membuat link backup")

            return (
                False,
                "❌ Gagal membuat atau mengirim link backup.\n"
                "Pastikan bot menjadi admin di grup backup dengan izin "
                "membuat tautan undangan, lalu coba lagi atau hubungi admin.",
            )


# =========================================================
# 6. API KLIQRIS
# =========================================================

def find_value(data, keys):
    if not isinstance(data, dict):
        return None

    containers = [data]

    for name in ("data", "result", "transaction", "payment"):
        child = data.get(name)

        if isinstance(child, list) and child and isinstance(child[0], dict):
            child = child[0]

        if isinstance(child, dict):
            containers.append(child)

    for container in containers:
        for key in keys:
            value = container.get(key)

            if value is not None and value != "":
                return value

    return None


def compact_json(data, limit=1200):
    """Ringkas JSON untuk log/diagnosa (string panjang dipotong)."""

    def clean(value):
        if isinstance(value, dict):
            return {k: clean(v) for k, v in value.items()}

        if isinstance(value, list):
            return [clean(v) for v in value[:5]]

        if isinstance(value, str) and len(value) > 120:
            return value[:40] + "...<dipotong>"

        return value

    try:
        return json.dumps(clean(data), ensure_ascii=False)[:limit]
    except (TypeError, ValueError):
        return str(data)[:limit]


async def create_qris_invoice(order_id, amount):
    headers = {
        "x-api-key": KLIQRIS_API_KEY,
        "id_merchant": KLIQRIS_MERCHANT_ID,
        "Content-Type": "application/json",
    }

    payload = {
        "order_id": order_id,
        "id_merchant": KLIQRIS_MERCHANT_ID,
        "amount": amount,
        "keterangan": f"VIP Backup {order_id}",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            KLIQRIS_CREATE_URL,
            headers=headers,
            json=payload,
        )

        response.raise_for_status()
        data = response.json()

    if not isinstance(data, dict):
        raise RuntimeError("Respons pembuatan QRIS tidak valid.")

    qris_url = find_value(
        data,
        [
            "qris_url",
            "qr_url",
            "payment_url",
            "checkout_url",
            "url",
        ],
    )

    qris_image = find_value(
        data,
        [
            "qris_image",
            "qr_image",
            "qr_image_url",
            "image_url",
        ],
    )

    provider_order_id = find_value(
        data,
        [
            "order_id",
            "id_transaksi",
            "transaction_id",
            "reference",
        ],
    )

    total_amount = find_value(
        data,
        ["total_amount", "total", "amount"],
    )

    if not qris_url and not qris_image:
        logger.error(
            "Respons create QRIS tidak memiliki URL/gambar QR: %s",
            data,
        )
        raise RuntimeError(
            "URL atau gambar QR tidak ditemukan pada respons KlikQRIS."
        )

    try:
        total_amount = (
            int(float(total_amount))
            if total_amount is not None
            else int(amount)
        )
    except (ValueError, TypeError):
        total_amount = int(amount)

    return {
        "qris_url": str(qris_url) if qris_url else None,
        "qris_image": str(qris_image) if qris_image else None,
        "provider_order_id": str(provider_order_id or order_id),
        "total_amount": total_amount,
        "raw": data,
    }


async def check_qris_status(order_id):
    headers = {
        "x-api-key": KLIQRIS_API_KEY,
        "id_merchant": KLIQRIS_MERCHANT_ID,
    }

    async with httpx.AsyncClient(timeout=25) as client:
        response = await client.get(
            f"{KLIQRIS_STATUS_URL}/{order_id}",
            headers=headers,
        )

        response.raise_for_status()
        data = response.json()

    if not isinstance(data, dict):
        raise RuntimeError("Respons status KlikQRIS tidak valid.")

    return data


def extract_status_value(data):
    """
    Ambil teks status pembayaran.
    - Kunci yang lebih spesifik (payment_status, dll.) didahulukan.
    - Status di dalam objek "data" didahulukan dibanding status
      tingkat atas (yang sering hanya menandakan request berhasil).
    - Nilai non-teks (true/false/angka) diabaikan.
    """
    if not isinstance(data, dict):
        return None

    containers = [data]

    for name in ("data", "result", "transaction", "payment"):
        child = data.get(name)

        if isinstance(child, list) and child and isinstance(child[0], dict):
            child = child[0]

        if isinstance(child, dict):
            containers.append(child)

    for key in (
        "payment_status",
        "transaction_status",
        "status_pembayaran",
        "status_transaksi",
        "status",
    ):
        for container in reversed(containers):
            value = container.get(key)

            if isinstance(value, str) and value.strip():
                return value

    return None


def normalize_payment_status(data):
    value = extract_status_value(data)

    if value is None:
        return "UNKNOWN"

    status = (
        str(value)
        .strip()
        .upper()
        .replace("-", "_")
        .replace(" ", "_")
    )

    if status in {
        "PAID",
        "SETTLED",
        "COMPLETED",
        "SUCCESS",
        "SUCCESSFUL",
        "CAPTURED",
        "LUNAS",
        "BERHASIL",
    }:
        return "PAID"

    if status in {
        "EXPIRED",
        "EXPIRE",
        "KEDALUWARSA",
    }:
        return "EXPIRED"

    if status in {
        "FAILED",
        "FAIL",
        "CANCELLED",
        "CANCELED",
        "REJECTED",
        "GAGAL",
    }:
        return "FAILED"

    if status in {
        "PENDING",
        "UNPAID",
        "WAITING",
        "MENUNGGU",
        "BELUM_BAYAR",
        "CREATED",
        "PROCESSING",
    }:
        return "PENDING"

    return "UNKNOWN"


# =========================================================
# 7. PEMBAYARAN DAN HAK GRUP
# =========================================================

async def deliver_paid_transaction(app, order_id):
    """
    Setelah status PAID:
    1. Simpan hak grup yang telah dibayar.
    2. Buat undangan untuk grup dalam invoice.
    3. Jangan menghapus hak grup jika pengiriman link gagal.
    """
    async with DATA_LOCK:
        transactions = load_json(TRANSACTIONS_FILE, {})
        tx = transactions.get(order_id)

        if not tx or tx.get("status") != "PAID":
            return

        if tx.get("delivery_in_progress"):
            return

        tx["delivery_in_progress"] = True
        tx["updated_at"] = iso_now()
        save_json(TRANSACTIONS_FILE, transactions)
        tx = deepcopy(tx)

    user_id = int(tx["user_id"])
    target_groups = [
        str(group_id)
        for group_id in tx.get("group_ids", [])
    ]

    try:
        # Hak dibukukan sebelum mencoba mengirim tautan.
        # Jika bot restart, pengguna tidak ditagih ulang.
        await add_paid_groups(
            user_id,
            target_groups,
            order_id,
        )

        results, missing, unknown = await get_missing_groups(
            app.bot,
            user_id,
        )

        # Kirim tautan untuk grup invoice yang masih belum diikuti.
        # Jika pengguna sudah bergabung, tidak perlu mengirim undangan.
        # Link yang sudah pernah dikirim untuk order ini tidak dikirim lagi.
        already_sent = {
            str(x) for x in tx.get("sent_group_ids", [])
        }

        groups_to_invite = [
            group_id
            for group_id in target_groups
            if group_id not in already_sent
            and results.get(group_id, {}).get("status") == "not_member"
        ]

        sent, failed = await send_paid_group_invites(
            app.bot,
            user_id,
            groups_to_invite,
        )

        sent_all = sorted(already_sent | {str(x) for x in sent})

        # Grup yang statusnya unknown tidak boleh dianggap sudah bergabung.
        unknown_targets = [
            group_id
            for group_id in target_groups
            if results.get(group_id, {}).get("status") == "unknown"
        ]

        completed = not failed and not unknown_targets

        await update_transaction(
            order_id,
            {
                "delivery_in_progress": False,
                "fulfilled": completed,
                "sent_group_ids": sent_all,
                "delivery_failed_group_ids": failed,
                "delivery_unknown_group_ids": unknown_targets,
                "delivered_at": iso_now() if completed else None,
            },
        )

        if completed:
            # Hak sekali pakai: setelah link terkirim, hak dihapus.
            await remove_paid_groups(user_id, target_groups)

            await app.bot.send_message(
                chat_id=user_id,
                text=(
                    "✅ Pembayaran berhasil dikonfirmasi oleh SERVER.\n\n"
                    "Link undangan sudah dikirim dan hanya berlaku 1x. "
                    "Jika Anda keluar dari grup, Anda perlu membayar "
                    "lagi untuk bergabung kembali.\n\n"
                    "Gunakan menu Cek Keanggotaan VIP setelah bergabung."
                ),
            )

        else:
            await app.bot.send_message(
                chat_id=user_id,
                text=(
                    "✅ Pembayaran sudah dikonfirmasi.\n\n"
                    "Sebagian undangan belum dapat dikirim karena "
                    "pemeriksaan grup gagal. Hak grup Anda tetap tersimpan "
                    "dan bot akan mencoba kembali. Jangan membayar ulang."
                ),
            )

    except Exception:
        logger.exception(
            "Gagal menyelesaikan pengiriman order %s",
            order_id,
        )

        await update_transaction(
            order_id,
            {"delivery_in_progress": False},
        )


async def refresh_payment(app, order_id):
    tx = await get_transaction(order_id)

    if not tx:
        return "UNKNOWN"

    if tx.get("status") == "FAILED":
        return "FAILED"

    if tx.get("status") == "PAID":
        await deliver_paid_transaction(app, order_id)
        return "PAID"

    if tx.get("status") == "EXPIRED":
        # Cek ulang maksimal 1x per menit selama jendela pembayaran
        # terlambat; di luar itu langsung dianggap expired.
        last_check = parse_datetime(tx.get("last_expired_check"))

        recently_checked = (
            last_check is not None
            and (now_utc() - last_check).total_seconds() < 60
        )

        if not is_within_late_window(tx) or recently_checked:
            return "EXPIRED"

        await update_transaction(
            order_id,
            {"last_expired_check": iso_now()},
        )

    try:
        data = await check_qris_status(order_id)
        status = normalize_payment_status(data)

    except (httpx.HTTPError, ValueError, RuntimeError):
        logger.exception(
            "Gagal memeriksa status KlikQRIS untuk %s",
            order_id,
        )
        return "UNKNOWN"

    if status == "UNKNOWN":
        logger.warning(
            "Status KlikQRIS tidak dikenali untuk %s. Respons: %s",
            order_id,
            compact_json(data),
        )

    if status == "PAID":
        await update_transaction(
            order_id,
            {
                "status": "PAID",
                "paid_at": iso_now(),
            },
        )

        await deliver_paid_transaction(app, order_id)
        return "PAID"

    if status in {"EXPIRED", "FAILED"}:
        await update_transaction(
            order_id,
            {
                "status": status,
                "status_updated_at": iso_now(),
            },
        )

        return status

    expires_at = parse_datetime(tx.get("expires_at"))

    if expires_at and now_utc() >= expires_at:
        # Cek status API sudah dilakukan terlebih dahulu.
        # Jika masih pending setelah batas waktu, invoice ditandai expired.
        await update_transaction(
            order_id,
            {"status": "EXPIRED"},
        )
        return "EXPIRED"

    return status


# =========================================================
# 8. ALUR AKSES DAN INVOICE
# =========================================================

async def send_missing_group_invites(bot, user_id):
    """
    Kirim ulang tautan untuk grup yang telah dibayar tetapi
    pengguna belum bergabung. Tidak membuat invoice baru.
    """
    results, missing, unknown = await get_missing_groups(
        bot,
        user_id,
    )

    if unknown:
        return (
            False,
            "Bot belum dapat memverifikasi sebagian grup VIP. "
            "Coba lagi nanti.",
        )

    paid = await get_paid_groups(user_id)

    eligible = [
        group_id
        for group_id in missing
        if group_id in paid
    ]

    if not eligible:
        return False, ""

    sent, failed = await send_paid_group_invites(
        bot,
        user_id,
        eligible,
    )

    if failed:
        return (
            False,
            "Sebagian link belum dapat dikirim. Hak grup Anda tetap "
            "tersimpan; silakan coba kembali nanti.",
        )

    return True, (
        "🔐 Link untuk grup yang sudah Anda bayar tetapi belum diikuti "
        "telah dikirim ulang."
    )


@with_action(ChatAction.TYPING)
async def start_purchase_flow(update, context):
    if not await require_vip_access(update, context):
        return

    user_id = update.effective_user.id

    results, missing, unknown = await get_missing_groups(
        context.bot,
        user_id,
    )

    if unknown:
        names = [get_group_name(group_id) for group_id in unknown]

        await update.effective_message.reply_text(
            "⚠️ Bot tidak dapat memeriksa beberapa grup:\n"
            + "\n".join("• " + name for name in names)
            + "\n\nCoba lagi nanti. Pastikan bot memiliki akses "
            "untuk memeriksa keanggotaan."
        )
        return

    if not missing:
        await update.effective_message.reply_text(
            "✅ Anda sudah bergabung dengan semua 11 grup VIP.\n\n"
            "Anda dapat menggunakan menu Link Backup."
        )
        return

    paid = await get_paid_groups(user_id)

    already_paid_missing = [
        group_id
        for group_id in missing
        if group_id in paid
    ]

    if already_paid_missing:
        _, message = await send_missing_group_invites(
            context.bot,
            user_id,
        )

        if message:
            await update.effective_message.reply_text(message)

        # Periksa kembali setelah memberikan hak yang sudah dibayar.
        paid = await get_paid_groups(user_id)

    unpaid_missing = [
        group_id
        for group_id in missing
        if group_id not in paid
    ]

    if not unpaid_missing:
        await update.effective_message.reply_text(
            "Tidak ada grup baru yang perlu dibayar. "
            "Silakan bergabung menggunakan link yang sudah dikirim."
        )
        return

    amount = len(unpaid_missing) * PRICE_PER_GROUP

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "\U0001F4B3 Buat QRIS Dinamis",
                callback_data="createinvoice",
            )
        ]
    ])

    context.user_data["pending_group_ids"] = unpaid_missing

    await update.effective_message.reply_text(
        f"Anda memilih <b>{package_title(len(unpaid_missing))} "
        f"\u2014 {format_rupiah_spaced(amount)}.</b>\n\n"
        "Klik tombol di bawah untuk membuat QRIS pembayaran baru.",
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML,
    )


@with_action(ChatAction.UPLOAD_PHOTO)
async def create_invoice_callback(update, context):
    if not await require_vip_access(update, context):
        return

    query = update.callback_query
    await query.answer("\u23F3 Membuat QRIS...")

    user = query.from_user

    results, missing, unknown = await get_missing_groups(
        context.bot,
        user.id,
    )

    if unknown:
        await query.message.reply_text(
            "Bot belum dapat memverifikasi semua grup. "
            "Coba lagi nanti."
        )
        return

    paid = await get_paid_groups(user.id)

    unpaid_missing = [
        group_id
        for group_id in missing
        if group_id not in paid
    ]

    if not unpaid_missing:
        await query.message.reply_text(
            "Tidak ada grup yang perlu dibayar. "
            "Jika masih ada grup yang belum Anda ikuti, gunakan "
            "menu Beli/Akses Grup VIP untuk mengirim ulang link."
        )
        return

    amount = len(unpaid_missing) * PRICE_PER_GROUP

    # Cegah invoice aktif ganda untuk grup yang sama.
    async with DATA_LOCK:
        transactions = load_json(TRANSACTIONS_FILE, {})

        for order_id, tx in transactions.items():
            if (
                int(tx.get("user_id", 0)) == user.id
                and tx.get("status") == "PENDING"
            ):
                pending_expiry = parse_datetime(tx.get("expires_at"))

                if pending_expiry and now_utc() < pending_expiry:
                    await query.message.reply_text(
                        "Anda masih memiliki invoice aktif.\n\n"
                        f"ID pesanan: {order_id}\n"
                        f"Total: {format_rupiah(tx.get('amount', 0))}\n\n"
                        "Selesaikan atau tunggu invoice kedaluwarsa "
                        "sebelum membuat invoice baru."
                    )
                    return

                tx["status"] = "EXPIRED"

        save_json(TRANSACTIONS_FILE, transactions)

    order_id = (
        f"{ORDER_ID_PREFIX}{user.id}{int(now_utc().timestamp())}"
        f"{secrets.token_hex(3).upper()}"
    )

    progress = await query.message.reply_text(
        "⏳ Sedang menyiapkan QRIS pembayaran..."
    )

    try:
        invoice = await create_qris_invoice(order_id, amount)

    except (httpx.HTTPError, ValueError, RuntimeError):
        logger.exception("Gagal membuat invoice QRIS")

        await safe_delete(progress)

        await query.message.reply_text(
            "❌ Invoice QRIS gagal dibuat.\n\n"
            "Coba lagi nanti. Jika ada dana terpotong, hubungi admin "
            "dan jangan melakukan pembayaran ulang sebelum diperiksa."
        )
        return

    created = now_utc()
    expires = created + timedelta(minutes=PAYMENT_TIMEOUT_MINUTES)

    total_amount = invoice["total_amount"]

    await create_transaction(
        order_id,
        {
            "order_id": order_id,
            "provider_order_id": invoice["provider_order_id"],
            "user_id": user.id,
            "username": user.username,
            "group_ids": unpaid_missing,
            "group_count": len(unpaid_missing),
            "amount": total_amount,
            "base_amount": amount,
            "status": "PENDING",
            "created_at": created.isoformat(),
            "expires_at": expires.isoformat(),
            "qris_url": invoice["qris_url"],
            "qris_image": invoice["qris_image"],
            "fulfilled": False,
            "delivery_in_progress": False,
        },
    )

    tx = await get_transaction(order_id)

    sent = bool(tx) and await send_qris_photo(query.message, tx)

    await safe_delete(progress)

    if not sent:
        await query.message.reply_text(
            "QRIS tidak dapat ditampilkan. Hubungi admin dengan ID pesanan:\n"
            f"{order_id}"
        )


# =========================================================
# 9. MENU TELEGRAM
# =========================================================

def main_keyboard():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("🔎 Cek Keanggotaan VIP")],
            [KeyboardButton("🛒 Beli/Akses Grup VIP")],
            [KeyboardButton("🔗 Link Backup")],
            [KeyboardButton("💬 Hubungi Admin")],
        ],
        resize_keyboard=True,
    )


@with_action(ChatAction.TYPING)
async def start_command(update, context):
    user = update.effective_user

    if not user:
        return

    if not await require_vip_access(update, context):
        return

    username = (
        f"@{user.username}"
        if user.username
        else "Tidak tersedia"
    )

    full_name = " ".join(
        part for part in [user.first_name, user.last_name] if part
    )

    await update.effective_message.reply_text(
        "Selamat datang di Bot VIP Backup!\n\n"
        f"ID Telegram: {user.id}\n"
        f"Username: {username}\n"
        f"Nama: {full_name or 'Tidak tersedia'}\n\n"
        "Pilih menu di bawah.",
        reply_markup=main_keyboard(),
    )


async def myid_command(update, context):
    user = update.effective_user

    if not user:
        return

    await update.effective_message.reply_text(
        f"ID Telegram Anda: {user.id}"
    )


async def buy_command(update, context):
    await start_purchase_flow(update, context)


@with_action(ChatAction.TYPING)
async def check_vip_command(update, context):
    if not await require_vip_access(update, context):
        return

    user_id = update.effective_user.id

    await update.effective_message.reply_text(
        "🔎 Sedang memeriksa 11 grup VIP..."
    )

    results, missing, unknown = await get_missing_groups(
        context.bot,
        user_id,
    )

    lines = ["📋 HASIL PEMERIKSAAN VIP\n"]

    for group_id, item in results.items():
        if item["status"] == "member":
            status_text = "✅ Sudah bergabung"

        elif item["status"] == "not_member":
            status_text = "❌ Belum bergabung"

        else:
            status_text = "⚠️ Belum dapat diverifikasi"

        lines.append(f"{status_text} — {item['name']}")

    paid = await get_paid_groups(user_id)

    unpaid_missing = [
        group_id
        for group_id in missing
        if group_id not in paid
    ]

    lines.append("")
    lines.append(
        f"Sudah diikuti: {len(VIP11_GROUP_IDS) - len(missing) - len(unknown)}"
        f"/{len(VIP11_GROUP_IDS)}"
    )
    lines.append(f"Belum diikuti: {len(missing)}")
    lines.append(f"Belum dibayar: {len(unpaid_missing)}")

    if unknown:
        lines.append(
            "\nSebagian status belum dapat diverifikasi. "
            "Hasil akhir mungkin belum lengkap."
        )

    await update.effective_message.reply_text("\n".join(lines))

    if not unknown and missing:
        await update.effective_message.reply_text(
            "Gunakan menu Beli/Akses Grup VIP untuk melanjutkan."
        )


@with_action(ChatAction.TYPING)
async def backup_command(update, context):
    if not await require_vip_access(update, context):
        return

    await update.effective_message.reply_text(
        "🔗 Sedang memeriksa syarat grup backup..."
    )

    success, message = await deliver_backup_link(
        context.bot,
        update.effective_user.id,
    )

    if not success and message:
        await update.effective_message.reply_text(message)


@with_action(ChatAction.TYPING)
async def admin_command(update, context):
    if not await require_vip_access(update, context):
        return

    if ADMIN_CONTACT:
        await update.effective_message.reply_text(
            f"💬 Hubungi admin: {ADMIN_CONTACT}"
        )
    else:
        await update.effective_message.reply_text(
            "Kontak admin belum dikonfigurasi."
        )


# =========================================================
# 10. TAMPILKAN QRIS
# =========================================================

@with_action(ChatAction.UPLOAD_PHOTO)
async def show_qris_callback(update, context):
    if not await require_vip_access(update, context):
        return

    query = update.callback_query
    order_id = query.data.removeprefix("showqr_")
    tx = await get_transaction(order_id)

    if not tx or int(tx.get("user_id", 0)) != query.from_user.id:
        await query.answer(
            "Invoice tidak ditemukan atau bukan milik Anda.",
            show_alert=True,
        )
        return

    await query.answer()

    if tx.get("status") == "PAID":
        await query.message.reply_text(
            "Pembayaran sudah dikonfirmasi."
        )
        return

    if tx.get("status") != "PENDING":
        await query.message.reply_text(
            "Invoice sudah tidak aktif. Buat invoice baru jika diperlukan."
        )
        return

    expires = parse_datetime(tx.get("expires_at"))

    if expires and now_utc() >= expires:
        await update_transaction(order_id, {"status": "EXPIRED"})

        await query.message.reply_text(
            "⏰ Invoice sudah kedaluwarsa. Silakan buat invoice baru."
        )
        return

    if not await send_qris_photo(query.message, tx):
        await query.message.reply_text(
            "QRIS tidak dapat ditampilkan. Hubungi admin dengan ID pesanan:\n"
            f"{order_id}"
        )


# =========================================================
# 11. CEK PEMBAYARAN MANUAL
# =========================================================

@with_action(ChatAction.TYPING)
async def check_payment_callback(update, context):
    if not await require_vip_access(update, context):
        return

    query = update.callback_query
    order_id = query.data.removeprefix("checkpay_")
    tx = await get_transaction(order_id)

    if not tx or int(tx.get("user_id", 0)) != query.from_user.id:
        await query.answer(
            "Invoice tidak ditemukan atau bukan milik Anda.",
            show_alert=True,
        )
        return

    await query.answer("Memeriksa pembayaran...")

    status = await refresh_payment(
        context.application,
        order_id,
    )

    if status == "PAID":
        await query.message.reply_text(
            "✅ Pembayaran telah dikonfirmasi SERVER.\n"
            "Hak grup Anda telah disimpan. Jika ada link yang gagal "
            "dikirim, bot akan mencoba lagi secara otomatis."
        )

    elif status == "PENDING":
        await query.message.reply_text(
            "⏳ Pembayaran belum terdeteksi. Jika sudah membayar, "
            "tunggu sebentar lalu periksa kembali."
        )

    elif status == "EXPIRED":
        await query.message.reply_text(
            "⏰ Invoice kedaluwarsa. Anda dapat membuat invoice baru "
            "untuk grup yang belum dibayar."
        )

    elif status == "FAILED":
        await query.message.reply_text(
            "❌ Status pembayaran gagal atau dibatalkan."
        )

    else:
        await query.message.reply_text(
            "⚠️ Status pembayaran belum dapat dipastikan. "
            "Coba lagi nanti dan jangan membayar ulang dahulu."
        )


@with_action(ChatAction.TYPING)
async def cekbayar_command(update, context):
    """
    Khusus admin (ADMIN_IDS): /cekbayar [order_id]
    Menampilkan status lokal, respons mentah KlikQRIS, lalu memaksa
    pengecekan + pengiriman link bila sudah dibayar.
    """
    user = update.effective_user
    message = update.effective_message

    if not user or not message or str(user.id) not in ADMIN_IDS:
        return

    if context.args:
        order_id = context.args[0].strip()

    else:
        async with DATA_LOCK:
            transactions = load_json(TRANSACTIONS_FILE, {})

        mine = [
            (tx.get("created_at", ""), oid)
            for oid, tx in transactions.items()
            if str(tx.get("user_id")) == str(user.id)
        ]

        if not mine:
            await message.reply_text(
                "Tidak ada order untuk akun Anda. "
                "Gunakan: /cekbayar ORDER_ID"
            )
            return

        order_id = max(mine)[1]

    tx = await get_transaction(order_id)

    if not tx:
        await message.reply_text(f"Order tidak ditemukan: {order_id}")
        return

    local_before = tx.get("status")

    try:
        data = await check_qris_status(order_id)

    except (httpx.HTTPError, ValueError, RuntimeError) as exc:
        await message.reply_text(
            f"Order: {order_id}\n"
            f"Status lokal: {local_before}\n\n"
            f"Gagal menghubungi KlikQRIS: {exc!r}"
        )
        return

    parsed = normalize_payment_status(data)

    result = await refresh_payment(context.application, order_id)
    after = await get_transaction(order_id) or {}

    await message.reply_text(
        (
            f"Order: {order_id}\n"
            f"Status lokal sebelum: {local_before}\n"
            f"Status terbaca dari KlikQRIS: {parsed}\n"
            f"Hasil proses: {result}\n"
            f"Status lokal sesudah: {after.get('status')}\n"
            f"fulfilled: {after.get('fulfilled')}\n\n"
            f"Respons KlikQRIS:\n{compact_json(data, 1500)}"
        )[:3900]
    )


# =========================================================
# 12. ROUTER MENU
# =========================================================

@with_action(ChatAction.TYPING)
async def text_router(update, context):
    if not await require_vip_access(update, context):
        return

    text = (update.effective_message.text or "").strip()

    if text == "🛒 Beli/Akses Grup VIP":
        await start_purchase_flow(update, context)

    elif text == "🔎 Cek Keanggotaan VIP":
        await check_vip_command(update, context)

    elif text == "🔗 Link Backup":
        await backup_command(update, context)

    elif text == "💬 Hubungi Admin":
        await admin_command(update, context)

    else:
        await update.effective_message.reply_text(
            "Silakan pilih menu yang tersedia.",
            reply_markup=main_keyboard(),
        )


# =========================================================
# 13. MONITOR PEMBAYARAN OTOMATIS
# =========================================================

async def payment_monitor(context):
    if PAYMENT_MONITOR_LOCK.locked():
        return

    async with PAYMENT_MONITOR_LOCK:
        async with DATA_LOCK:
            transactions = load_json(TRANSACTIONS_FILE, {})

            pending_ids = [
                order_id
                for order_id, tx in transactions.items()
                if tx.get("status") == "PENDING"
                or (
                    tx.get("status") == "EXPIRED"
                    and is_within_late_window(tx)
                )
            ]

            retry_ids = [
                order_id
                for order_id, tx in transactions.items()
                if tx.get("status") == "PAID"
                and (
                    not tx.get("fulfilled")
                    or tx.get("delivery_failed_group_ids")
                    or tx.get("delivery_unknown_group_ids")
                )
                and not tx.get("delivery_in_progress")
            ]

        for order_id in pending_ids:
            try:
                await refresh_payment(
                    context.application,
                    order_id,
                )

            except Exception:
                logger.exception(
                    "Monitor pembayaran gagal untuk %s",
                    order_id,
                )

        for order_id in retry_ids:
            try:
                await deliver_paid_transaction(
                    context.application,
                    order_id,
                )

            except Exception:
                logger.exception(
                    "Pengiriman ulang gagal untuk %s",
                    order_id,
                )


# =========================================================
# 14. MONITOR BACKUP OTOMATIS
# =========================================================

async def backup_monitor(context):
    """
    Mengirim backup secara otomatis hanya kepada pengguna yang:
    - pernah melakukan pembayaran yang berhasil;
    - sudah bergabung di semua 11 grup;
    - belum pernah menerima link backup dari bot.

    Pengguna tetap dapat meminta link melalui menu jika belum terkirim.
    """
    async with DATA_LOCK:
        transactions = load_json(TRANSACTIONS_FILE, {})
        backup_data = load_json(BACKUP_USERS_FILE, {})

        paid_users = {
            str(tx.get("user_id"))
            for tx in transactions.values()
            if tx.get("status") == "PAID" and tx.get("user_id")
        }

        pending_users = [
            user_id
            for user_id in paid_users
            if not (
                isinstance(backup_data.get(user_id), dict)
                and backup_data[user_id].get("sent")
            )
        ]

    for user_id in pending_users:
        try:
            await deliver_backup_link(
                context.bot,
                int(user_id),
            )

        except Exception:
            logger.exception(
                "Monitor backup gagal untuk user %s",
                user_id,
            )


# =========================================================
# 15. ERROR HANDLER
# =========================================================

async def error_handler(update, context):
    logger.error(
        "Terjadi error saat memproses update",
        exc_info=context.error,
    )


# =========================================================
# 16. MENJALANKAN BOT
# =========================================================

_INSTANCE_LOCK = None


def acquire_single_instance_lock():
    """
    Mencegah bot dijalankan dua kali pada komputer/server yang sama.
    Dua proses bot dengan token yang sama membuat setiap pesan
    dijawab dua kali.
    """
    global _INSTANCE_LOCK

    handle = open(DATA_DIR / "bot.lock", "a+")

    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)

    except OSError:
        handle.close()
        raise RuntimeError(
            "Bot sudah berjalan di proses lain. Tutup proses bot yang "
            "lama sebelum menjalankan yang baru."
        )

    _INSTANCE_LOCK = handle


# Bot hanya melayani chat pribadi. Pesan di grup (termasuk grup VIP
# dan grup backup) diabaikan sepenuhnya.
PRIVATE_ONLY = filters.ChatType.PRIVATE


def main():
    validate_config()
    acquire_single_instance_lock()
    consume_fulfilled_entitlements()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start_command, filters=PRIVATE_ONLY)
    )
    application.add_handler(
        CommandHandler("myid", myid_command, filters=PRIVATE_ONLY)
    )
    application.add_handler(
        CommandHandler("buy", buy_command, filters=PRIVATE_ONLY)
    )
    application.add_handler(
        CommandHandler("cekvip", check_vip_command, filters=PRIVATE_ONLY)
    )
    application.add_handler(
        CommandHandler("backup", backup_command, filters=PRIVATE_ONLY)
    )
    application.add_handler(
        CommandHandler("admin", admin_command, filters=PRIVATE_ONLY)
    )
    application.add_handler(
        CommandHandler("cekbayar", cekbayar_command, filters=PRIVATE_ONLY)
    )

    application.add_handler(
        CallbackQueryHandler(
            create_invoice_callback,
            pattern=r"^createinvoice$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            show_qris_callback,
            pattern=r"^showqr_.+$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            check_payment_callback,
            pattern=r"^checkpay_.+$",
        )
    )

    application.add_handler(
        MessageHandler(
            PRIVATE_ONLY & filters.TEXT & ~filters.COMMAND,
            text_router,
        )
    )

    application.add_error_handler(error_handler)

    if application.job_queue is None:
        raise RuntimeError(
            "JobQueue tidak tersedia. Pastikan requirements.txt berisi:\n"
            "python-telegram-bot[job-queue]"
        )

    application.job_queue.run_repeating(
        payment_monitor,
        interval=PAYMENT_CHECK_INTERVAL,
        first=5,
        name="payment_monitor",
    )

    application.job_queue.run_repeating(
        backup_monitor,
        interval=60,
        first=15,
        name="backup_monitor",
    )

    logger.info("Bot VIP Backup + KlikQRIS berjalan.")

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
