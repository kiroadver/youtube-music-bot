# 🎵 YouTube Music Bot

Cari lagu di YouTube, langsung dapet info + file MP3-nya, langsung dikirim ke Telegram.

## ✨ Fitur

- **Pencarian** — ketik judul lagu, bot cari di YouTube, auto-skip hasil mix/playlist/kumpulan & lagu >15 menit
- **Kartu info** — judul + cover, artis, durasi, ukuran file, link YouTube, dan file audio
- **Pilih hasil** — tombol angka (1–5) buat ganti hasil kalau yang pertama bukan yang lu mau
- **Kirim ulang** — tombol 🎵 Kirim Audio buat resend file tanpa download ulang
- **Download langsung** — `/dl` atau tempel link YouTube apa adanya
- **Batas aman** — file maksimal 50 MB (sesuai limit Telegram)

## 📖 Cara Pakai

### 1) Cari & kirim lagu

```
/play judul lagu
```

Contoh:

```
/play tulus monokrom
```

Bot bakal kirim kartu berisi:

- 🎵 Judul + cover
- 👤 Artis
- ⏱️ Durasi
- 💾 Ukuran file
- 🔗 Link YouTube
- 🎧 File MP3-nya

### 2) Hasil pertama bukan yang lu mau?

Klik tombol angka (2, 3, 4…) di bawah kartu buat lihat hasil lain.

### 3) Download langsung

```
/dl judul lagu
```

Atau kirim link YouTube-nya langsung:

```
https://youtu.be/XXXXXXX
```

### 4) Tombol cepat

- 🎵 **Kirim Audio** → kirim ulang file MP3-nya
- ▶️ **Buka di YouTube** → buka link lagunya

## 💡 Tips

- Ketik judul lagu langsung juga bisa (tanpa `/play`)
- Sertakan nama artis biar hasilnya lebih akurat
- Hasil kelamaan / numpuk (mix, playlist, kumpulan) otomatis dilewati
- Batas file: 50 MB (lagu maksimal ±15 menit)
- `/help` buat lihat panduan ini lagi

## 🚀 Setup

### Prasyarat

- Python 3.10+
- Token bot dari [@BotFather](https://t.me/BotFather)
- `ffmpeg` terinstall (wajib) — buat konversi audio jadi MP3

### Langkah-langkah

1. **Clone repo**

   ```bash
   git clone https://github.com/kiroadver/youtube-music-bot.git
   cd youtube-music-bot
   ```

2. **Install ffmpeg** (kalau belum ada)

   ```bash
   sudo apt install ffmpeg   # Debian/Ubuntu
   ```

3. **Install dependensi Python**

   ```bash
   pip install -r requirements.txt
   ```

4. **Set token bot**

   Dari terminal:

   ```bash
   export BOT_TOKEN="123456789:ABCdef..."
   ```

   atau bikin file `.env` (sudah di-gitignore) kalau pakai wrapper yang support.

5. **Jalankan**

   ```bash
   python bot.py
   ```

   Kalau ffmpeg belum ada, bot bakal nolak start dan kasih petunjuk install-nya.

5. **Tes di Telegram**

   Buka bot-mu, ketik `/start` → `/play tulus monokrom`.

### Menjalankan sebagai service (systemd)

Buat file `/etc/systemd/system/yt-music-bot.service`:

```ini
[Unit]
Description=YouTube Music Bot
After=network-online.target
Wants=network-online.target

[Service]
User=your-user
WorkingDirectory=/path/to/youtube-music-bot
Environment=BOT_TOKEN=123456789:ABCdef...
ExecStart=/usr/bin/python3 bot.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Lalu:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now yt-music-bot
sudo systemctl status yt-music-bot
```

## 📁 Struktur Project

```
.
├── bot.py             # seluruh kode bot
├── requirements.txt   # dependensi
├── .gitignore         # .env, __pycache__, dll
└── README.md
```

## ⚠️ Troubleshooting

| Masalah | Solusi |
|---|---|
| Bot gak start, `Set BOT_TOKEN` | Token belum di-set. Cek export/.env, token harus dari BotFather |
| `ffmpeg: command not found` | Install ffmpeg: `sudo apt install ffmpeg` |
| Download gagal / stuck | Bisa jadi YouTube update anti-scraping. Upgrade: `pip install -U yt-dlp` |
| File > 50 MB ditolak | Sudah di-limit, cari versi lagu lebih pendek atau pake link lain |
| Bot lambat | Tambahin caching / pake VPS dengan koneksi bagus |

## ⚖️ Disclaimer

Gunakan dengan bijak. Jangan download konten berhak cipta secara massal / untuk komersial.
