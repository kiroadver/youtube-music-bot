import os
import re
import html
import asyncio
import logging

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
COOKIES_FILE = os.environ.get("YTDLP_COOKIES", "")  # optional, for bot-blocked IPs
MAX_FILESIZE_MB = 50
MAX_FILESIZE = MAX_FILESIZE_MB * 1024 * 1024
MAX_DURATION = 15 * 60
MAX_RESULTS = 5
DOWNLOAD_DIR = "/tmp/ytmusic-bot"

def _cookie_opts() -> dict:
    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        return {"cookiefile": COOKIES_FILE}
    return {}


def _js_runtime_opts() -> dict:
    """Add deno JS runtime if available (needed to bypass YouTube PO token challenge)."""
    import shutil
    if shutil.which("deno"):
        return {"js_runtimes": {"deno": {"path": "deno"}}}
    return {}


YDL_OPTS_BASE = {
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "nocheckcertificate": True,
    "socket_timeout": 15,
}


def get_filesize_mb(path: str) -> float:
    return os.path.getsize(path) / (1024 * 1024)


def fetch_thumbnail(res: dict) -> bytes | None:
    """Fetch best available thumbnail, return bytes or None."""
    for q in ("maxresdefault", "hqdefault"):
        url = res.get("thumbnail", "").replace("hqdefault", q)
        if not url:
            continue
        try:
            r = requests.get(url, timeout=8)
            if r.status_code == 200 and len(r.content) > 1000:
                return r.content
        except Exception:
            continue
    return None


def search_youtube(query: str, limit: int = MAX_RESULTS) -> list[dict]:
    """Search YouTube, skip mixes/live/long results, return list of dicts."""
    search_opts = {
        **YDL_OPTS_BASE,
        **_cookie_opts(),
        **_js_runtime_opts(),
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


def download_audio(video_url: str) -> tuple[str, dict]:
    """Download audio as MP3 (requires ffmpeg), return (filepath, info)."""
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    opts = {
        **YDL_OPTS_BASE,
        **_cookie_opts(),
        **_js_runtime_opts(),
        "format": "bestaudio/best",
        "outtmpl": os.path.join(DOWNLOAD_DIR, "%(id)s.%(ext)s"),
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            },
            {"key": "FFmpegMetadata"},
        ],
        "max_filesize": MAX_FILESIZE,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(video_url, download=True)
        filepath = ydl.prepare_filename(info)
        # Postprocessor renames output to .mp3
        if not filepath.endswith(".mp3"):
            mp3 = os.path.splitext(filepath)[0] + ".mp3"
            if os.path.exists(mp3):
                filepath = mp3
    return filepath, info


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
    row = [
        InlineKeyboardButton(f"● {i + 1}", callback_data="noop")
        if i == current
        else InlineKeyboardButton(f"{i + 1}", callback_data=f"res:{i}")
        for i in range(len(results))
    ]
    return InlineKeyboardMarkup(
        [
            row,
            [
                InlineKeyboardButton("🎵 Kirim Audio", callback_data="send_audio"),
                InlineKeyboardButton("▶️ YouTube", url=results[current]["url"]),
            ],
        ]
    )


HELP_TEXT = (
    "🎵 <b>YouTube Music Bot</b>\n"
    "Cari lagu di YouTube, langsung dapet info + file MP3-nya.\n\n"
    "━━━━━━━━━━━━━━━\n"
    "📖 <b>Cara Pakai</b>\n\n"
    "1) <b>Cari &amp; kirim lagu</b>\n"
    "<code>/play judul lagu</code>\n"
    "Contoh: <code>/play tulus monokrom</code>\n\n"
    "Bot bakal kirim kartu berisi:\n"
    "• 🎵 Judul + cover\n"
    "• 👤 Artis\n"
    "• ⏱️ Durasi\n"
    "• 💾 Ukuran file\n"
    "• 🔗 Link YouTube\n"
    "• 🎧 File MP3-nya\n\n"
    "2) <b>Hasil pertama bukan yang lu mau?</b>\n"
    "Klik tombol angka (2, 3, 4…) di bawah kartu buat lihat hasil lain.\n\n"
    "3) <b>Download langsung</b>\n"
    "<code>/dl judul lagu</code>\n"
    "Atau kirim link YouTube-nya langsung.\n\n"
    "4) <b>Tombol cepat</b>\n"
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


async def send_audio_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE, res: dict,
    results: list[dict], index: int, filepath: str, status,
) -> None:
    """Send audio + card, remember file_id, cleanup, delete status msg."""
    filesize_mb = get_filesize_mb(filepath) if os.path.exists(filepath) else 0
    if not os.path.exists(filepath) or filesize_mb > MAX_FILESIZE_MB:
        await status.edit_text("❌ File kegedean (limit 50 MB) atau gagal didownload.")
        if os.path.exists(filepath):
            os.remove(filepath)
        return
    try:
        thumb = await asyncio.to_thread(fetch_thumbnail, res)
        with open(filepath, "rb") as f:
            msg = await update.effective_message.reply_audio(
                f,
                title=truncate(res["title"], 60),
                performer=truncate(res["uploader"], 60),
                duration=int(res["duration"] or 0),
                thumbnail=thumb,
                caption=build_song_card(res, filesize_mb),
                parse_mode=ParseMode.HTML,
                reply_markup=build_results_keyboard(results, index),
            )
        if msg.audio and msg.audio.file_id:
            context.user_data.setdefault("file_ids", {})[res["id"]] = msg.audio.file_id
            context.user_data.setdefault("sizes", {})[res["id"]] = filesize_mb
        await status.delete()
    except Exception as e:
        logging.exception("send audio failed: %s", e)
        await status.edit_text("❌ Gagal kirim audio. Coba lagi.")
    finally:
        if os.path.exists(filepath):
            os.remove(filepath)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML)


async def cmd_play(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await handle_search(update, context, " ".join(context.args or []))


async def cmd_dl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    arg = " ".join(context.args or []).strip()
    if YT_RE.search(arg):
        await handle_direct(update, context, arg)
    else:
        await handle_search(update, context, arg)


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
        filepath, info = await asyncio.to_thread(download_audio, url)
    except Exception as e:
        logging.exception("direct dl failed: %s", e)
        await status.edit_text("❌ Gagal download. Link mungkin gak valid.")
        return
    res = {
        "id": info.get("id", ""),
        "title": info.get("title", "Unknown"),
        "uploader": info.get("uploader") or info.get("channel") or "-",
        "duration": info.get("duration", 0),
        "url": info.get("webpage_url", url),
        "thumbnail": info.get("thumbnail", ""),
    }
    await send_audio_message(update, context, res, [res], 0, filepath, status)


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
        results = await asyncio.to_thread(search_youtube, query)
    except Exception as e:
        logging.exception("search failed: %s", e)
        await status.edit_text("❌ Gagal search. Coba lagi nanti.")
        return

    if not results:
        await status.edit_text("❌ Gak ketemu. Coba judul lain.")
        return

    context.user_data["results"] = results
    context.user_data["current"] = 0

    await send_song(update, context, results, 0, status)


async def send_song(update: Update, context: ContextTypes.DEFAULT_TYPE, results: list[dict], index: int, status):
    res = results[index]
    await status.edit_text(
        f"⏳ Download: <b>{html.escape(truncate(res['title'], 60))}</b>…",
        parse_mode=ParseMode.HTML,
    )
    await update.effective_chat.send_action(ChatAction.UPLOAD_VOICE)
    try:
        filepath, info = await asyncio.to_thread(download_audio, res["url"])
    except Exception as e:
        logging.exception("download failed: %s", e)
        await status.edit_text("❌ Gagal download lagu. Coba judul lain.")
        return
    await send_audio_message(update, context, res, results, index, filepath, status)


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
        # Update keyboard on the old card so selection is visible
        try:
            await q.edit_message_reply_markup(
                reply_markup=build_results_keyboard(results, index)
            )
        except Exception:
            pass
        status = await q.message.reply_text(
            f"⏳ Download: <b>{html.escape(truncate(results[index]['title'], 60))}</b>…",
            parse_mode=ParseMode.HTML,
        )
        await send_song(update, context, results, index, status)
        return

    if data == "send_audio":
        index = context.user_data.get("current", 0)
        res = results[index]
        file_id = context.user_data.get("file_ids", {}).get(res["id"])
        if file_id:
            await q.answer()
            status = await q.message.reply_text("⏳ Mengirim ulang audio…")
            try:
                await update.effective_message.reply_audio(
                    file_id,
                    caption=build_song_card(res, context.user_data.get("sizes", {}).get(res["id"], 0)),
                    parse_mode=ParseMode.HTML,
                    reply_markup=build_results_keyboard(results, index),
                )
                await status.delete()
            except Exception as e:
                logging.exception("resend failed: %s", e)
                await status.edit_text("❌ Gagal kirim ulang audio.")
            return
        await q.answer()
        status = await q.message.reply_text("⏳ Mengirim ulang audio…")
        await send_song(update, context, results, index, status)


def main():
    if not BOT_TOKEN:
        raise SystemExit("Set BOT_TOKEN env var. Get token dari @BotFather.")

    import shutil
    if shutil.which("ffmpeg") is None:
        raise SystemExit(
            "ffmpeg gak ketemu. Install dulu: sudo apt install ffmpeg (Debian/Ubuntu)"
        )

    if not COOKIES_FILE:
        logging.warning(
            "YTDLP_COOKIES belum di-set. Kalau download kena blokir bot, "
            "export cookies dari browser ke file .txt lalu set env YTDLP_COOKIES."
        )

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("play", cmd_play))
    app.add_handler(CommandHandler("dl", cmd_dl))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.add_handler(CallbackQueryHandler(on_button))

    logging.info("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
