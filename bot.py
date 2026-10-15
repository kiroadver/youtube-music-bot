import os
import re
import io
import json
import html
import time
import shutil
import asyncio
import logging
from urllib.parse import quote

import requests
import yt_dlp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "")  # optional, gets full error tracebacks
COOKIES_FILE = os.environ.get("YTDLP_COOKIES", "")  # optional: path to cookies.txt
COOKIES_CONTENT = os.environ.get("YTDLP_COOKIES_CONTENT", "")  # optional: raw cookies.txt pasted here
MAX_FILESIZE_MB = 50
MAX_FILESIZE = MAX_FILESIZE_MB * 1024 * 1024
MAX_DURATION = 15 * 60
MAX_RESULTS = 5
DOWNLOAD_DIR = "/tmp/ytmusic-bot"
DOWNLOAD_TIMEOUT = 180
SEARCH_TIMEOUT = 20

YDL_OPTS_BASE = {
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "nocheckcertificate": True,
    "retries": 0,
}

# (client, use_cookies) tried one-by-one, first success wins.
# Per yt-dlp maintainers (#17497): logged-in cookies break some clients,
# so embedded/mobile clients go WITHOUT cookies; mweb needs a PO token
# (provided by yt-dlp-ejs + deno); web_creator goes WITH login cookies.
PLAYER_CONFIGS = [
    ("mweb", False),
    ("web_creator", True),
    ("android_music", False),
    ("android", False),
    ("web_embedded", False),
    ("tv_embedded", False),
    ("ios", False),
    ("tv", True),
    ("web", True),
]
DOWNLOAD_FORMATS = ("bestaudio[ext=m4a]/bestaudio/best", "best")


def _proxy_opts() -> dict:
    proxy = os.environ.get("YTDLP_PROXY", "").strip()
    return {"proxy": proxy} if proxy else {}


def _client_opts(client: str) -> dict:
    return {
        "extractor_args": {
            "youtube": {
                "player_client": [client],
                "player_skip": ["webpage", "configs"],
            }
        }
    }


def is_bot_blocked_error(e: Exception) -> bool:
    msg = str(e).lower()
    return any(
        k in msg
        for k in (
            "sign in to confirm",
            "not a bot",
            "http error 429",
            "too many requests",
            "the page needs to be reloaded",
        )
    )


def short_error(e: Exception, limit: int = 200) -> str:
    """Extract a readable one-line error for Telegram."""
    msg = str(e).strip().split("\n")[0]
    # Strip yt-dlp "ERROR: " prefix noise
    msg = re.sub(r"^ERROR:\s*(\[[\w-]+\]\s*[\w-]*:?\s*)?", "", msg)
    return msg if len(msg) <= limit else msg[: limit - 1] + "…"


async def report_error(update: Update, context: ContextTypes.DEFAULT_TYPE,
                      where: str, e: Exception) -> None:
    """Log error + send detail to user chat + forward full traceback to admin."""
    logging.exception("%s failed: %s", where, e)
    detail = short_error(e)
    if ADMIN_CHAT_ID:
        try:
            user = update.effective_user
            who = f"@{user.username}" if user and user.username else (user.id if user else "?")
            await context.bot.send_message(
                ADMIN_CHAT_ID,
                f"⚠️ <b>Error di {html.escape(where)}</b>\n"
                f"👤 User: {html.escape(str(who))}\n"
                f"💬 Chat: <code>{update.effective_chat.id}</code>\n"
                f"❌ <code>{html.escape(detail)}</code>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
    return detail


def _cookie_opts() -> dict:
    path = COOKIES_FILE
    # Allow pasting the whole cookies.txt into an env var (easier on Railway:
    # no volume upload needed). Written to /tmp at first use.
    if COOKIES_CONTENT and "youtube.com" in COOKIES_CONTENT:
        path = "/tmp/ytmusic-cookies.txt"
        try:
            with open(path, "w") as f:
                f.write(COOKIES_CONTENT)
                if not COOKIES_CONTENT.endswith("\n"):
                    f.write("\n")
        except Exception as e:
            logging.warning("could not write cookies from env: %s", e)
            path = ""
    if path and os.path.exists(path):
        return {"cookiefile": path}
    return {}


def _js_runtime_opts() -> dict:
    if shutil.which("deno"):
        return {"js_runtimes": {"deno": {"path": "deno"}}}
    return {}


def get_filesize_mb(path: str) -> float:
    return os.path.getsize(path) / (1024 * 1024)


def prepare_thumb(raw: bytes | None) -> bytes | None:
    """Resize cover to Telegram spec (JPEG, <=320px, <200KB) via ffmpeg.

    Raw YouTube thumbs (1280px) get silently rejected by Telegram,
    which is why covers sometimes never show up.
    """
    if not raw:
        return None
    if not shutil.which("ffmpeg"):
        return raw if len(raw) < 200 * 1024 else None
    try:
        import subprocess
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as src:
            src.write(raw)
            src_path = src.name
        dst_path = src_path + ".small.jpg"
        r = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", src_path,
             "-vf", "scale=320:-1", "-q:v", "5", dst_path],
            capture_output=True, timeout=20,
        )
        try:
            os.remove(src_path)
        except Exception:
            pass
        if r.returncode != 0 or not os.path.exists(dst_path):
            return raw if len(raw) < 200 * 1024 else None
        if os.path.getsize(dst_path) >= 200 * 1024:
            try:
                os.remove(dst_path)
            except Exception:
                pass
            return None
        with open(dst_path, "rb") as f:
            out = f.read()
        try:
            os.remove(dst_path)
        except Exception:
            pass
        return out
    except Exception:
        return raw if len(raw) < 200 * 1024 else None


def fetch_thumbnail(res: dict) -> bytes | None:
    """Fetch best available thumbnail, return bytes or None."""
    for q in ("maxresdefault", "hqdefault"):
        url = res.get("thumbnail", "").replace("hqdefault", q)
        if not url:
            continue
        try:
            r = requests.get(url, timeout=4)
            if r.status_code == 200 and len(r.content) > 1000:
                return r.content
        except Exception:
            continue
    return None


# ----------------------------- Pinterest -----------------------------

PINTEREST_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
PINTEREST_TIMEOUT = 20
_IMAGE_RE = re.compile(r"\.(?:jpe?g|png)$", re.I)
_pin_session: requests.Session | None = None
_pin_state = {"app_version": "7327000", "csrf": ""}


def _pinterest_bootstrap(s: requests.Session) -> None:
    """Grab csrftoken cookie + build appVersion from the homepage."""
    try:
        r = s.get("https://www.pinterest.com/", timeout=PINTEREST_TIMEOUT)
        m = re.search(r'"appVersion":"([^"]+)"', r.text)
        if m:
            _pin_state["app_version"] = m.group(1)
        csrf = s.cookies.get("csrftoken")
        if csrf:
            _pin_state["csrf"] = csrf
    except Exception as e:
        logging.warning("pinterest bootstrap failed: %s", e)


def _pinterest_session() -> requests.Session:
    global _pin_session
    if _pin_session is None:
        s = requests.Session()
        s.headers.update(
            {"User-Agent": PINTEREST_UA, "Accept-Language": "en-US,en;q=0.9"}
        )
        _pinterest_bootstrap(s)
        _pin_session = s
    return _pin_session


def _pick_pin_image(pin: dict) -> str:
    images = pin.get("images") or {}
    for key in ("orig", "originals", "1200x", "736x", "564x", "474x"):
        entry = images.get(key)
        if isinstance(entry, dict) and entry.get("url"):
            return entry["url"]
    return ""


def _pin_request(s: requests.Session, query: str, bookmark) -> dict:
    options = {
        "query": query,
        "scope": "pins",
        "bookmarks": [bookmark] if bookmark else [],
        "no_fetch_context_on_resource": False,
    }
    params = {
        "source_url": f"/search/pins/?q={quote(query)}&rs=typed",
        "data": json.dumps({"options": options, "context": {}}),
    }
    headers = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "X-Pinterest-AppState": "active",
        # The single load-bearing header — without it Pinterest 403s.
        "X-Pinterest-PWS-Handler": "www/search/[scope].js",
        "X-APP-VERSION": _pin_state["app_version"],
        "X-CSRFToken": _pin_state["csrf"],
        "Referer": f"https://www.pinterest.com/search/pins/?q={quote(query)}",
    }
    url = "https://www.pinterest.com/resource/BaseSearchResource/get/"
    r = s.get(url, params=params, headers=headers, timeout=PINTEREST_TIMEOUT)
    if r.status_code == 403:
        _pinterest_bootstrap(s)
        headers["X-APP-VERSION"] = _pin_state["app_version"]
        headers["X-CSRFToken"] = _pin_state["csrf"]
        r = s.get(url, params=params, headers=headers, timeout=PINTEREST_TIMEOUT)
    r.raise_for_status()
    return r.json().get("resource_response") or {}


def pinterest_search(query: str, limit: int = MAX_RESULTS) -> list[str]:
    """Search Pinterest, return up to `limit` image URLs (no API key).

    Videos and non-JPEG/PNG (e.g. webp) are skipped so Telegram can render
    them as photos.
    """
    query = query.strip()
    if not query:
        return []
    s = _pinterest_session()
    found: list[str] = []
    seen: set[str] = set()
    bookmark = None
    for _ in range(5):
        if len(found) >= limit:
            break
        body = _pin_request(s, query, bookmark)
        results = (body.get("data") or {}).get("results") or []
        for pin in results:
            if len(found) >= limit:
                break
            if pin.get("videos"):
                continue
            url = _pick_pin_image(pin)
            if not url or not _IMAGE_RE.search(url):
                continue
            key = url.rsplit("/", 1)[-1]
            if key in seen:
                continue
            seen.add(key)
            found.append(url)
        bookmark = body.get("bookmark")
        if not bookmark:
            break
    return found


def fetch_image(url: str) -> bytes | None:
    """Download an image, returning bytes or None."""
    try:
        r = _pinterest_session().get(url, timeout=PINTEREST_TIMEOUT)
        if r.status_code == 200 and len(r.content) > 500:
            return r.content
    except Exception as e:
        logging.warning("fetch_image failed: %s", e)
    return None


def search_youtube(query: str, limit: int = MAX_RESULTS) -> list[dict]:
    """Search YouTube, skip mixes/live/long results, return list of dicts."""
    search_opts = {
        **YDL_OPTS_BASE,
        **_cookie_opts(),
        "socket_timeout": 8,
        "extract_flat": True,
        "skip_download": True,
    }
    url = f"ytsearch{limit}:{query}"
    with yt_dlp.YoutubeDL(search_opts) as ydl:
        info = ydl.extract_info(url, download=False)
    results = []
    for e in info.get("entries", []):
        if not e or not e.get("id"):
            continue
        title = e.get("title") or ""
        if re.search(r"\b(mix|playlist|full album|kumpulan|compilation)\b", title, re.I):
            continue
        if e.get("is_live"):
            continue
        duration = e.get("duration") or 0
        if duration and duration > MAX_DURATION:
            continue
        results.append(
            {
                "id": e["id"],
                "title": title,
                "uploader": e.get("uploader") or e.get("channel") or "-",
                "duration": duration,
                "url": f"https://www.youtube.com/watch?v={e['id']}",
                "thumbnail": f"https://i.ytimg.com/vi/{e['id']}/hqdefault.jpg",
            }
        )
    return results


def download_audio(video_url: str, progress_hooks: list | None = None,
                   pp_hooks: list | None = None,
                   on_client: object = None) -> tuple[str, dict]:
    """Download best audio (no conversion here). Tries each player client
    one-by-one, each with audio-first then muxed format. First success wins."""
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    errs: list[str] = []
    cookie = _cookie_opts()
    for client, use_cookies in PLAYER_CONFIGS:
        for fmt in DOWNLOAD_FORMATS:
            opts = {
                **YDL_OPTS_BASE,
                **(cookie if use_cookies else {}),
                **_js_runtime_opts(),
                **_proxy_opts(),
                **_client_opts(client),
                "format": fmt,
                "outtmpl": os.path.join(DOWNLOAD_DIR, "%(id)s.%(ext)s"),
                "max_filesize": MAX_FILESIZE,
                "socket_timeout": 15,
                "fragment_retries": 0,
            }
            if progress_hooks:
                opts["progress_hooks"] = progress_hooks
            if pp_hooks:
                opts["postprocessor_hooks"] = pp_hooks
            try:
                if on_client:
                    try:
                        on_client(client, fmt)
                    except Exception:
                        pass
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(video_url, download=True)
                    filepath = ydl.prepare_filename(info)
                logging.info("download ok via %s (%s)", client, fmt)
                return filepath, info
            except Exception as e:
                msg = str(e).strip().split("\n")[0][:120]
                errs.append(f"{client}/{fmt.split('/')[0]}: {msg}")
                logging.warning("download %s %s failed: %s", client, fmt, msg)
    raise RuntimeError("semua client gagal: " + " | ".join(errs))


def convert_to_mp3(src: str, timeout_s: int = 90, cover_path: str | None = None) -> str:
    """Convert audio to MP3 via ffmpeg with a hard timeout (never hangs forever).

    If cover_path is given, the cover is embedded into the MP3 (ID3 APIC)
    so music players show the album art.
    """
    import subprocess
    if src.lower().endswith(".mp3") and not cover_path:
        return src
    dst = os.path.splitext(src)[0] + ".mp3"
    if cover_path and os.path.exists(cover_path):
        cmd = [
            "timeout", str(timeout_s),
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", src, "-i", cover_path,
            "-map", "0:a", "-map", "1:v",
            "-c:a", "libmp3lame", "-b:a", "192k",
            "-c:v", "mjpeg", "-id3v2_version", "3",
            "-metadata:s:v", 'title=Album cover',
            "-metadata:s:v", "comment=Cover (front)",
            dst,
        ]
    else:
        cmd = [
            "timeout", str(timeout_s),
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", src, "-vn", "-c:a", "libmp3lame", "-b:a", "192k", dst,
        ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s + 10)
    except subprocess.TimeoutExpired:
        raise RuntimeError("ffmpeg hang > 90 detik, dibatalkan.")
    if r.returncode != 0 or not os.path.exists(dst):
        err = (r.stderr or "").strip().split("\n")[-1][:200]
        raise RuntimeError(f"ffmpeg gagal (code {r.returncode}): {err}")
    try:
        os.remove(src)
    except Exception:
        pass
    return dst


async def download_with_progress(url: str, status, title: str,
                                 cover_path: str | None = None) -> tuple[str, dict]:
    """Download in background thread while editing status with live % progress."""
    state = {"pct": 0, "phase": "download"}

    def hook(d):
        try:
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                done = d.get("downloaded_bytes") or 0
                if total:
                    state["pct"] = int(done * 100 / total)
            elif d.get("status") == "finished":
                state["phase"] = "convert"
                state["convert_start"] = time.monotonic()
        except Exception:
            pass

    def pp_hook(d):
        try:
            if d.get("status") in ("started", "processing"):
                state["phase"] = "convert"
                state.setdefault("convert_start", time.monotonic())
        except Exception:
            pass

    loop = asyncio.get_event_loop()
    start = loop.time()
    last_text = ""

    async def pump_status():
        nonlocal last_text
        if state["phase"] == "convert":
            conv_elapsed = int(time.monotonic() - state.get("convert_start", time.monotonic()))
            text = (
                f"⏳ Konversi MP3… {conv_elapsed}s\n"
                f"<b>{html.escape(truncate(title, 50))}</b>"
            )
        elif state["pct"] > 0:
            bar = "█" * (state["pct"] // 10) + "░" * (10 - state["pct"] // 10)
            text = (
                f"⏳ Download {state['pct']}% {bar}\n"
                f"<b>{html.escape(truncate(title, 50))}</b>"
            )
        else:
            text = (
                f"⏳ Menghubungi YouTube… [{html.escape(str(state.get('client', '…')))}]\n"
                f"<b>{html.escape(truncate(title, 50))}</b>"
            )
        if text != last_text:
            last_text = text
            try:
                await status.edit_text(text, parse_mode=ParseMode.HTML)
            except Exception:
                pass

    # Phase 1: download (max 240s — tries 5 clients x 2 formats)
    state["client"] = "…"
    dl_task = loop.run_in_executor(
        None,
        lambda: download_audio(
            url, [hook], [pp_hook],
            on_client=lambda c, f: state.update(client=c),
        ),
    )
    while not dl_task.done():
        if loop.time() - start > 240:
            raise asyncio.TimeoutError("download")
        await asyncio.sleep(4)
        await pump_status()
    filepath, info = dl_task.result()

    # Phase 2: convert with hard timeout (max 100s), timer keeps ticking
    state["phase"] = "convert"
    state["convert_start"] = time.monotonic()
    conv_task = loop.run_in_executor(
        None, lambda: convert_to_mp3(filepath, timeout_s=90, cover_path=cover_path)
    )
    while not conv_task.done():
        if loop.time() - start > DOWNLOAD_TIMEOUT:
            raise asyncio.TimeoutError("convert")
        await asyncio.sleep(4)
        await pump_status()
    mp3_path = conv_task.result()

    size_mb = get_filesize_mb(mp3_path) if os.path.exists(mp3_path) else 0
    await status.edit_text(
        f"📤 Mengirim {size_mb:.1f} MB ke Telegram…\n<b>{html.escape(truncate(title, 50))}</b>",
        parse_mode=ParseMode.HTML,
    )
    return mp3_path, info


def format_duration(seconds) -> str:
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return "?"
    m, s = divmod(seconds, 60)
    return f"{m}:{s:02d}"


def truncate(text: str, limit: int = 60) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def build_song_card(res: dict, filesize_mb: float = 0) -> str:
    size_line = f"💾 Ukuran: {filesize_mb:.1f} MB\n" if filesize_mb else ""
    return (
        f"🎵 <b>{html.escape(truncate(res['title'], 80))}</b>\n"
        f"👤 Artis: {html.escape(truncate(res['uploader'], 50))}\n"
        f"⏱️ Durasi: {format_duration(res['duration'])}\n"
        f"{size_line}"
        f"🔗 {html.escape(res['url'])}"
    )


def build_results_keyboard(results: list[dict], current: int = 0) -> InlineKeyboardMarkup:
    buttons = []
    for i, res in enumerate(results):
        if i == current:
            buttons.append(InlineKeyboardButton(f"✅ {res['title']}", callback_data="noop"))
        else:
            buttons.append(InlineKeyboardButton(res["title"], callback_data=f"res:{i}"))
    row = [buttons[i: i + 2] for i in range(0, len(buttons), 2)]
    keyboard = [
        *row,
        [
            InlineKeyboardButton("🎵 Kirim Audio", callback_data="send_audio"),
            InlineKeyboardButton("▶️ YouTube", url=results[current]["url"]),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


async def clear_previous_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete previously sent media group so chat doesn't stack up."""
    media_ids = context.user_data.get("media_ids", [])
    for msg_id in media_ids:
        try:
            await context.bot.delete_message(update.effective_chat.id, msg_id)
        except Exception as e:
            logging.warning("delete %s failed: %s", msg_id, e)
    context.user_data["media_ids"] = []


async def send_media_group(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    res: dict,
    results: list[dict],
    index: int,
    audio_file,
    filesize_mb: float,
    audio_file_id: str | None = None,
    thumb_bytes: bytes | None = None,
) -> None:
    """Send ONE message: audio with spec-compliant cover thumbnail,
    full info card as caption, and the result keyboard below."""
    if thumb_bytes is None:
        raw = await asyncio.to_thread(fetch_thumbnail, res)
        thumb_bytes = await asyncio.to_thread(prepare_thumb, raw)

    async def _send():
        if audio_file_id:
            audio_msg = await update.effective_message.reply_audio(
                audio_file_id,
                thumbnail=thumb_bytes,
                caption=build_song_card(res, filesize_mb),
                parse_mode=ParseMode.HTML,
                reply_markup=build_results_keyboard(results, index),
            )
        else:
            audio_msg = await update.effective_message.reply_audio(
                audio_file,
                title=truncate(res["title"], 60),
                performer=truncate(res["uploader"], 60),
                duration=int(res["duration"] or 0),
                thumbnail=thumb_bytes,
                caption=build_song_card(res, filesize_mb),
                parse_mode=ParseMode.HTML,
                reply_markup=build_results_keyboard(results, index),
            )
        return audio_msg

    audio_msg = await asyncio.wait_for(_send(), timeout=180)
    context.user_data.setdefault("media_ids", []).append(audio_msg.message_id)

    # Cache file_id of the uploaded audio for instant resend
    if audio_file_id:
        context.user_data.setdefault("file_ids", {})[res["id"]] = audio_file_id
    else:
        context.user_data.setdefault("sizes", {})[res["id"]] = filesize_mb
        if audio_msg.audio and audio_msg.audio.file_id:
            context.user_data.setdefault("file_ids", {})[res["id"]] = audio_msg.audio.file_id


HELP_TEXT = (
    "🎵 <b>YouTube Music Bot</b>\n"
    "Cari lagu di YouTube, langsung dapet info + file MP3-nya.\n\n"
    "━━━━━━━━━━━━━━━\n"
    "📖 <b>Cara Pakai</b>\n\n"
    "1) <b>Cari &amp; kirim lagu</b>\n"
    "<code>/play judul lagu</code>\n"
    "Contoh: <code>/play tulus monokrom</code>\n\n"
    "Bot bakal kirim 1 pesan berisi:\n"
    "• 🖼️ Cover lagu (nempel di player audio)\n"
    "• 🎵 Judul, 👤 artis, ⏱️ durasi, 💾 ukuran, 🔗 link\n"
    "• 🎧 File MP3-nya + tombol pilih hasil lain\n\n"
    "2) <b>Hasil pertama bukan yang lu mau?</b>\n"
    "Klik tombol judul lagu di bawah kartu. Pesan lama otomatis dibersihkan,\n"
    "pesan baru (cover + audio pilihan baru) langsung tampil.\n\n"
    "3) <b>Download langsung</b>\n"
    "<code>/dl judul lagu</code>\n"
    "Atau kirim link YouTube-nya langsung.\n\n"
    "4) <b>Cari foto Pinterest</b> 🖼️\n"
    "<code>/pin kata kunci</code>\n"
    "Contoh: <code>/pin sanrio wallpaper</code>\n"
    "Bot kirim maksimal 5 foto Pinterest, satu per satu.\n\n"
    "5) <b>Tombol cepat</b>\n"
    "• 🎵 Kirim Audio → kirim ulang file MP3-nya\n"
    "• ▶️ Buka di YouTube → buka link lagunya\n\n"
    "━━━━━━━━━━━━━━━\n"
    "💡 <b>Tips</b>\n"
    "• Ketik judul lagu langsung juga bisa (tanpa /play)\n"
    "• Sertakan nama artis biar hasilnya lebih akurat\n"
    "• Hasil kelamaan / numpuk (mix, playlist, kumpulan) otomatis dilewati\n"
    "• Batas file: 50 MB (lagu maksimal ±15 menit)\n"
    "• /help buat lihat panduan ini lagi"
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML)


def _is_admin(update: Update) -> bool:
    if not ADMIN_CHAT_ID:
        return True
    try:
        return str(update.effective_chat.id) == str(ADMIN_CHAT_ID)
    except Exception:
        return False


async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: report ffmpeg/deno/cookies/yt-dlp status + quick player probe."""
    if not _is_admin(update):
        await update.message.reply_text("❌ Khusus admin.")
        return
    import subprocess

    lines = ["🩺 <b>Health check</b>\n"]

    # ffmpeg
    ff = shutil.which("ffmpeg")
    if ff:
        try:
            v = subprocess.run([ff, "-version"], capture_output=True, text=True, timeout=10)
            lines.append(f"✅ ffmpeg: {(v.stdout.splitlines() or ['?'])[0][:60]}")
        except Exception as e:
            lines.append(f"⚠️ ffmpeg ada tapi error: {html.escape(str(e)[:80])}")
    else:
        lines.append("❌ ffmpeg: gak ketemu")

    # deno + ejs solver (needed for PO-token challenges)
    dn = shutil.which("deno")
    lines.append(f"{'✅' if dn else '❌'} deno: {html.escape(dn or 'gak ketemu')}")
    try:
        import yt_dlp_ejs  # noqa: F401
        lines.append("✅ yt-dlp-ejs: terinstall (PO-token solver)")
    except Exception:
        lines.append("❌ yt-dlp-ejs: BELUM terinstall (player bakal sering gagal)")

    # yt-dlp
    try:
        lines.append(f"✅ yt-dlp: {html.escape(yt_dlp.version.__version__)}")
    except Exception:
        lines.append("❌ yt-dlp: unknown")

    # cookies (count #HttpOnly_ lines too — yt-dlp loads those)
    co = _cookie_opts()
    if co.get("cookiefile"):
        p = co["cookiefile"]
        try:
            n = sum(
                1 for line in open(p)
                if line.strip() and not (
                    line.startswith("#") and not line.startswith("#HttpOnly_")
                )
            )
            src = "env YTDLP_COOKIES_CONTENT" if p == "/tmp/ytmusic-cookies.txt" else f"file {p}"
            lines.append(f"✅ cookies: {n} entries ({html.escape(src)})")
        except Exception as e:
            lines.append(f"⚠️ cookies: path ada tapi gak kebaca ({html.escape(str(e)[:80])})")
    else:
        lines.append("❌ cookies: BELUM DI-SET (YTDLP_COOKIES / YTDLP_COOKIES_CONTENT)")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    # quick player probe per (client, cookies) config (stops at first success)
    probe = await update.message.reply_text("⏳ Tes player YouTube per client…")
    def _probe_one(client: str, use_cookies: bool):
        opts = {
            **YDL_OPTS_BASE,
            **(_cookie_opts() if use_cookies else {}),
            **_js_runtime_opts(),
            **_proxy_opts(),
            **_client_opts(client),
            "socket_timeout": 10,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(
                "https://www.youtube.com/watch?v=jNQXAC9IVRw", download=False
            )
            fmts = info.get("formats") or []
            return info.get("title"), len(fmts)
    verdicts = []
    for client, use_cookies in PLAYER_CONFIGS:
        tag = f"{client}{'+ck' if use_cookies else ''}"
        try:
            _t, _n = await asyncio.wait_for(
                asyncio.to_thread(_probe_one, client, use_cookies), timeout=15
            )
            verdicts.append(f"✅ {tag}: {_n} format")
            break
        except Exception as e:
            verdicts.append(f"❌ {tag}: {html.escape(short_error(e, 90))}")
    await probe.edit_text(
        "🔬 <b>Hasil probe per client:</b>\n" + "\n".join(verdicts),
        parse_mode=ParseMode.HTML,
    )


async def cmd_play(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await handle_search(update, context, " ".join(context.args or []))


async def cmd_dl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    arg = " ".join(context.args or []).strip()
    if YT_RE.search(arg):
        await handle_direct(update, context, arg)
    else:
        await handle_search(update, context, arg)


async def cmd_pinterest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args or []).strip()
    if not query:
        await update.message.reply_text(
            "Pake: <code>/pin kata kunci</code>\n"
            "Contoh: <code>/pin sanrio wallpaper</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    await handle_pinterest(update, context, query)


async def handle_pinterest(update: Update, context: ContextTypes.DEFAULT_TYPE, query: str):
    """Search Pinterest and send up to MAX_RESULTS photos, one per message."""
    status = await update.message.reply_text(
        f"🔍 Cari foto Pinterest: <b>{html.escape(query)}</b>…",
        parse_mode=ParseMode.HTML,
    )
    try:
        urls = await asyncio.wait_for(
            asyncio.to_thread(pinterest_search, query), timeout=SEARCH_TIMEOUT + 15
        )
    except asyncio.TimeoutError:
        await status.edit_text("❌ Timeout search Pinterest. Coba lagi.")
        return
    except Exception as e:
        detail = await report_error(update, context, "pinterest-search", e)
        await status.edit_text(
            f"❌ Gagal cari di Pinterest. Coba lagi nanti.\n\n<i>Detail: {html.escape(detail)}</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    if not urls:
        await status.edit_text("❌ Gak ketemu foto. Coba kata kunci lain.")
        return

    await status.edit_text(
        f"🖼️ {len(urls)} foto ditemukan. Mengirim…", parse_mode=ParseMode.HTML
    )
    sent = 0
    for i, url in enumerate(urls, 1):
        data = await asyncio.to_thread(fetch_image, url)
        if not data:
            continue
        try:
            await update.effective_chat.send_action(ChatAction.UPLOAD_PHOTO)
            await update.message.reply_photo(
                data,
                caption=f'🖼️ Pinterest: "{truncate(query, 60)}" ({i}/{len(urls)})',
            )
            sent += 1
        except Exception as e:
            logging.warning("send pinterest photo failed: %s", e)

    if sent == 0:
        await status.edit_text("❌ Gagal mengirim foto Pinterest. Coba lagi.")
        return
    try:
        await status.delete()
    except Exception:
        pass


YT_RE = re.compile(
    r"(https?://)?(www\.|m\.|music\.)?(youtube\.com/(watch\?v=|shorts/)|youtu\.be/)[\w-]{11}"
)


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        return
    if YT_RE.search(text):
        await handle_direct(update, context, text)
    else:
        await handle_search(update, context, text)


async def handle_direct(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    m = YT_RE.search(text)
    url = m.group(0)
    url = url if url.startswith("http") else f"https://{url}"
    status = await update.message.reply_text("⏳ Mengambil lagu…")
    await update.effective_chat.send_action(ChatAction.UPLOAD_VOICE)
    try:
        filepath, info = await download_with_progress(url, status, url)
    except asyncio.TimeoutError:
        await status.edit_text("❌ Timeout download. Coba lagi.")
        return
    except Exception as e:
        detail = await report_error(update, context, "direct-download", e)
        await status.edit_text(
            f"❌ Gagal download. Link mungkin gak valid.\n\n<i>Detail: {html.escape(detail)}</i>",
            parse_mode=ParseMode.HTML,
        )
        return
    res = {
        "id": info.get("id", ""),
        "title": info.get("title", "Unknown"),
        "uploader": info.get("uploader") or info.get("channel") or "-",
        "duration": info.get("duration", 0),
        "url": info.get("webpage_url", url),
        "thumbnail": info.get("thumbnail", ""),
    }
    await clear_previous_media(update, context)
    filesize_mb = get_filesize_mb(filepath) if os.path.exists(filepath) else 0
    with open(filepath, "rb") as f:
        await send_media_group(
            update, context, res, [res], 0, f, filesize_mb,
            audio_file_id=context.user_data.get("file_ids", {}).get(res["id"]),
        )
    await status.delete()
    if os.path.exists(filepath):
        os.remove(filepath)


async def handle_search(update: Update, context: ContextTypes.DEFAULT_TYPE, query: str):
    query = query.strip()
    if not query:
        await update.message.reply_text(
            "Pake: <code>/play judul lagu</code>", parse_mode=ParseMode.HTML
        )
        return
    status = await update.message.reply_text(
        f"🔍 Mencari: <b>{html.escape(query)}</b>…", parse_mode=ParseMode.HTML
    )
    try:
        results = await asyncio.wait_for(
            asyncio.to_thread(search_youtube, query), timeout=SEARCH_TIMEOUT
        )
    except asyncio.TimeoutError:
        await status.edit_text("❌ Timeout search. Coba lagi.")
        return
    except Exception as e:
        detail = await report_error(update, context, "search", e)
        if is_bot_blocked_error(e):
            await status.edit_text(
                "❌ YouTube ngeblokir server (bot-check). Coba lagi 1–2 menit.\n\n"
                f"<i>Detail: {html.escape(detail)}</i>",
                parse_mode=ParseMode.HTML,
            )
        else:
            await status.edit_text(
                f"❌ Gagal search. Coba lagi nanti.\n\n<i>Detail: {html.escape(detail)}</i>",
                parse_mode=ParseMode.HTML,
            )
        return

    if not results:
        await status.edit_text("❌ Gak ketemu. Coba judul lain.")
        return

    context.user_data["results"] = results
    context.user_data["current"] = 0
    context.user_data["media_ids"] = []

    await send_song(update, context, results, 0, status)


async def send_song(update: Update, context: ContextTypes.DEFAULT_TYPE,
                   results: list[dict], index: int, status) -> None:
    """Send media group for result at index. Clear old group first."""
    res = results[index]
    file_id = context.user_data.get("file_ids", {}).get(res["id"])

    await clear_previous_media(update, context)

    if file_id:
        try:
            size_mb = context.user_data.get("sizes", {}).get(res["id"], 0)
            await send_media_group(
                update, context, res, results, index, None, size_mb,
                audio_file_id=file_id,
            )
            await status.delete()
            return
        except Exception as e:
            logging.exception("cached send failed, falling back to download: %s", e)

    await status.edit_text(
        f"⏳ Download: <b>{html.escape(truncate(res['title'], 60))}</b>…",
        parse_mode=ParseMode.HTML,
    )
    await update.effective_chat.send_action(ChatAction.UPLOAD_VOICE)

    # Fetch cover early so it can be embedded into the MP3 + sent as thumbnail.
    raw_thumb = await asyncio.to_thread(fetch_thumbnail, res)
    thumb_bytes = await asyncio.to_thread(prepare_thumb, raw_thumb)
    cover_path = None
    if raw_thumb:
        try:
            os.makedirs(DOWNLOAD_DIR, exist_ok=True)
            cover_path = os.path.join(DOWNLOAD_DIR, f"{res['id']}_cover.jpg")
            with open(cover_path, "wb") as cf:
                cf.write(raw_thumb)
        except Exception:
            cover_path = None

    try:
        filepath, _info = await download_with_progress(
            res["url"], status, res["title"], cover_path=cover_path
        )
    except asyncio.TimeoutError:
        await status.edit_text("❌ Timeout download. Coba lagi atau pilih hasil lain.")
        return
    except Exception as e:
        detail = await report_error(update, context, "download", e)
        if is_bot_blocked_error(e):
            await status.edit_text(
                "❌ YouTube ngeblokir server (bot-check).\n"
                "Coba lagi 1–2 menit. Kalau masih gagal, admin perlu update cookies.\n\n"
                f"<i>Detail: {html.escape(detail)}</i>",
                parse_mode=ParseMode.HTML,
            )
        else:
            await status.edit_text(
                f"❌ Gagal download lagu. Coba judul lain.\n\n<i>Detail: {html.escape(detail)}</i>",
                parse_mode=ParseMode.HTML,
            )
        return
    finally:
        if cover_path and os.path.exists(cover_path):
            try:
                os.remove(cover_path)
            except Exception:
                pass

    filesize_mb = get_filesize_mb(filepath) if os.path.exists(filepath) else 0
    try:
        with open(filepath, "rb") as f:
            await send_media_group(
                update, context, res, results, index, f, filesize_mb,
                thumb_bytes=thumb_bytes,
            )
    except asyncio.TimeoutError:
        detail = await report_error(update, context, "upload", asyncio.TimeoutError("upload > 180s"))
        await status.edit_text(
            f"❌ Upload ke Telegram timeout ({filesize_mb:.1f} MB).\n"
            "Koneksi server lambat — coba lagi atau pilih hasil lain.\n\n"
            f"<i>Detail: {html.escape(detail)}</i>",
            parse_mode=ParseMode.HTML,
        )
        return
    except Exception as e:
        detail = await report_error(update, context, "upload", e)
        await status.edit_text(
            f"❌ Gagal kirim ke Telegram.\n\n<i>Detail: {html.escape(detail)}</i>",
            parse_mode=ParseMode.HTML,
        )
        return
    context.user_data.setdefault("sizes", {})[res["id"]] = filesize_mb
    await status.delete()
    if os.path.exists(filepath):
        os.remove(filepath)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data

    if data == "noop":
        await q.answer()
        return

    results = context.user_data.get("results")
    if not results:
        await q.answer("❌ Data kadaluarsa, cari lagi ya.", show_alert=True)
        return

    if data.startswith("res:"):
        await q.answer()
        index = int(data.split(":")[1])
        if index >= len(results):
            return
        context.user_data["current"] = index
        status = await q.message.reply_text(
            f"⏳ Sedang siapkan: <b>{html.escape(truncate(results[index]['title'], 60))}</b>…",
            parse_mode=ParseMode.HTML,
        )
        await send_song(update, context, results, index, status)
        return

    if data == "send_audio":
        index = context.user_data.get("current", 0)
        res = results[index]
        file_id = context.user_data.get("file_ids", {}).get(res["id"])
        await q.answer()
        status = await q.message.reply_text("⏳ Mengirim ulang audio…")
        if file_id:
            size_mb = context.user_data.get("sizes", {}).get(res["id"], 0)
            await send_media_group(
                update, context, res, results, index, None, size_mb,
                audio_file_id=file_id,
            )
            await status.delete()
        else:
            await send_song(update, context, results, index, status)


def main():
    if not BOT_TOKEN:
        raise SystemExit("Set BOT_TOKEN env var. Get token dari @BotFather.")

    if shutil.which("ffmpeg") is None:
        raise SystemExit(
            "ffmpeg gak ketemu. Install dulu: sudo apt install ffmpeg (Debian/Ubuntu)"
        )

    if not COOKIES_FILE and not COOKIES_CONTENT:
        logging.warning(
            "YTDLP_COOKIES / YTDLP_COOKIES_CONTENT belum di-set. "
            "Kalau download kena blokir bot, set salah satunya."
        )

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("health", cmd_health))
    app.add_handler(CommandHandler("play", cmd_play))
    app.add_handler(CommandHandler("dl", cmd_dl))
    app.add_handler(CommandHandler(["pin", "pinterest"], cmd_pinterest))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.add_handler(CallbackQueryHandler(on_button))

    logging.info("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
