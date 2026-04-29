# 🎬 Telegram → YouTube Auto-Uploader

Fully automated daily uploader. No Colab, no manual input, runs on GitHub Actions for free.

## Features
- ✅ Tiered channel priority (Tier 1 always first, Tier 3 only if slots remain)
- ✅ Max 4 uploads per run (configurable)
- ✅ Subtle video filter (brightness/contrast/saturation via ffmpeg)
- ✅ Russian → English translation
- ✅ UFC SEO hashtags in every description
- ✅ Duplicate tracking via `uploaded_ids.txt` committed to repo
- ✅ Runs daily at your scheduled time — zero manual input

---

## Setup (One Time)

### Step 1 — Fork / create this repo on GitHub
- Go to github.com → New repository → name it `tg-yt-uploader`
- Upload all these files

### Step 2 — Generate Telegram session string (on your PC)
```bash
pip install telethon
python generate_session.py
```
- Enter your phone number + OTP
- Copy the long session string printed at the end

### Step 3 — Get YouTube credentials
1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create project → Enable YouTube Data API v3
3. Credentials → OAuth 2.0 → Web App → add `https://developers.google.com/oauthplayground` as redirect URI
4. Go to [OAuth Playground](https://developers.google.com/oauthplayground)
5. Gear icon → Use your own credentials → enter Client ID + Secret
6. Select `https://www.googleapis.com/auth/youtube.upload` → Authorize → Exchange for tokens
7. Copy the **Refresh Token**

### Step 4 — Add GitHub Secrets
Go to your repo → Settings → Secrets and variables → Actions → New secret

Add these secrets:

| Secret Name | Value |
|---|---|
| `TELEGRAM_API_ID` | `2040` |
| `TELEGRAM_API_HASH` | `b18441a1ff607e10a989891a5462e627` |
| `TELEGRAM_SESSION` | (the long string from Step 2) |
| `YOUTUBE_CLIENT_ID` | from client_secrets.json |
| `YOUTUBE_CLIENT_SECRET` | from client_secrets.json |
| `YOUTUBE_REFRESH_TOKEN` | from OAuth Playground |
| `GH_PAT` | GitHub Personal Access Token (repo scope) |

**To create GH_PAT:** GitHub → Settings → Developer Settings → Personal Access Tokens → Generate → select `repo` scope

### Step 5 — Configure channels and schedule

Edit `uploader.py`:
```python
CHANNELS = {
    1: ['khabib_nurmagomedov', 'ufc'],         # Always picked first
    2: ['mma_org', 'danawhite'],                # Picked if slots remain
    3: ['mmafighting', 'espnmma', 'bellator'],  # Low priority
}
MAX_UPLOADS = 4
```

Edit `.github/workflows/daily_upload.yml`:
```yaml
- cron: '0 4 * * *'   # 04:00 UTC = 09:00 AM PKT
```
Use https://crontab.guru to find your UTC time.

### Step 6 — Test manually
Go to GitHub repo → Actions tab → `Telegram → YouTube Daily Uploader` → Run workflow

---

## How it works daily
1. GitHub Actions wakes up at your cron time
2. Checks each channel in tier order
3. Grabs latest 1 unuploaded video per channel
4. Filters + re-encodes with ffmpeg
5. Translates Russian captions
6. Uploads to YouTube as public
7. Commits updated `uploaded_ids.txt` back to repo
8. Shuts down — costs you $0

## Adjusting video filter strength
In `uploader.py`, find this line:
```python
vf_filter = "eq=brightness=0.04:contrast=1.05:saturation=1.1,unsharp=5:5:0.5:5:5:0.0"
```
- `brightness`: -1.0 to 1.0 (0.04 = slight boost)
- `contrast`: 0 to 2 (1.05 = slight increase)
- `saturation`: 0 to 3 (1.1 = slightly more vivid)
- `unsharp`: sharpening (0.5 = subtle)
