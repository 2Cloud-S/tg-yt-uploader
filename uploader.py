"""
Telegram → YouTube Auto-Uploader
=================================
- Tiered channel priority system
- Max 4-5 uploads per run
- Video filters (brightness, contrast, saturation via ffmpeg)
- Russian → English translation
- UFC SEO hashtags
- Duplicate tracking via uploaded_ids.txt (committed to repo)
- Zero manual input — runs fully automated via GitHub Actions
"""

import asyncio
import os
import subprocess
import time
import pickle
import json
from pathlib import Path
from datetime import datetime

from telethon import TelegramClient
from telethon.tl.types import MessageMediaDocument, DocumentAttributeVideo
from googletrans import Translator
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError

# ============================================================
#  CONFIG — all values come from GitHub Secrets (no hardcoding)
# ============================================================

TELEGRAM_API_ID   = int(os.environ['TELEGRAM_API_ID'])
TELEGRAM_API_HASH = os.environ['TELEGRAM_API_HASH']
CLIENT_ID         = os.environ['YOUTUBE_CLIENT_ID']
CLIENT_SECRET     = os.environ['YOUTUBE_CLIENT_SECRET']
REFRESH_TOKEN     = os.environ['YOUTUBE_REFRESH_TOKEN']

# ---- Tiered channel config ----
# Tier 1 = highest priority (always included if video found)
# Tier 2 = included if slots remain after Tier 1
# Tier 3 = included only if many slots remain
CHANNELS = {
    1: [
        'arman_ufc_official',
        'ufc',
        'kchimaev',
    ],
    2: [
        'mma_org',
        'danawhite',
        'khabib_nurmagomedov',
    ],
    3: [

        #'espnmma',
        #'bellator',
        # add as many as you want — they only get picked if slots available
    ],
}

MAX_UPLOADS       = 4        # Max videos to upload per run
YOUTUBE_CATEGORY  = '17'     # 17 = Sports
UPLOAD_DELAY      = 20       # seconds between uploads
DOWNLOAD_FOLDER   = '/tmp/downloads'
UPLOADED_LOG      = 'uploaded_ids.txt'   # tracked in git repo

UFC_HASHTAGS = """
#UFC #MMA #UFCHighlights #MixedMartialArts #UFCFights #UFCNews
#Knockout #KO #Submission #UFCChampion #FightNight #PPV
#Boxing #Kickboxing #BJJ #Wrestling #Grappling
#Khabib #IslamMakhachev #JonJones #ConorMcGregor #StipeMiocic
#UFCFightPass #UFCWeekend #MMAFighter #CageMatch #Octagon
"""

# ============================================================
#  VIDEO FILTER (ffmpeg)
#  Subtle filter so it looks slightly different — avoids
#  YouTube's duplicate content detection
# ============================================================

def apply_filter_and_reencode(input_path):
    """
    Applies subtle video filter + re-encodes to H264/AAC MP4.
    Filter: slight brightness boost, contrast, saturation, sharpness.
    """
    output_path = input_path.rsplit('.', 1)[0] + '_out.mp4'
    print(f'   🎨 Applying filter + re-encoding...')

    # vf filter chain:
    # eq: brightness/contrast/saturation adjustments
    # unsharp: slight sharpening
    vf_filter = (
        "eq=brightness=0.04:contrast=1.05:saturation=1.1,"
        "unsharp=5:5:0.5:5:5:0.0"
    )

    cmd = [
        'ffmpeg', '-y',
        '-i', input_path,
        '-vf', vf_filter,
        '-c:v', 'libx264',
        '-preset', 'fast',
        '-crf', '22',
        '-c:a', 'aac',
        '-b:a', '192k',
        '-movflags', '+faststart',
        output_path
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print(f'   ⚠️ Filter failed, trying plain re-encode...')
        # Fallback: re-encode without filter (still fixes audio)
        cmd_fallback = [
            'ffmpeg', '-y',
            '-i', input_path,
            '-c:v', 'libx264', '-preset', 'fast', '-crf', '23',
            '-c:a', 'aac', '-b:a', '192k',
            '-movflags', '+faststart',
            output_path
        ]
        result2 = subprocess.run(cmd_fallback, capture_output=True, text=True)
        if result2.returncode != 0:
            print(f'   ⚠️ Re-encode also failed, using original')
            return input_path

    if os.path.exists(input_path):
        os.remove(input_path)

    size_mb = os.path.getsize(output_path) / 1024 / 1024
    print(f'   ✅ Processed: {size_mb:.1f} MB')
    return output_path


# ============================================================
#  TRANSLATION
# ============================================================

translator = Translator()

def translate_if_russian(text):
    if not text or len(text.strip()) < 3:
        return text, False
    try:
        detected = translator.detect(text)
        if detected.lang == 'ru':
            translated = translator.translate(text, src='ru', dest='en')
            return translated.text, True
        return text, False
    except Exception as e:
        print(f'   ⚠️ Translation error: {e}')
        return text, False

def build_description(original, translated, was_translated):
    parts = []
    if translated:
        parts.append(translated)
    if was_translated and original:
        parts.append(f'\n---\n🇷🇺 Original:\n{original}')
    parts.append('\n' + '─' * 40)
    parts.append(UFC_HASHTAGS)
    return '\n'.join(parts)


# ============================================================
#  UPLOADED IDs TRACKING
# ============================================================

def load_uploaded_ids():
    if not os.path.exists(UPLOADED_LOG):
        return set()
    with open(UPLOADED_LOG, 'r') as f:
        return set(line.strip() for line in f if line.strip())

def save_uploaded_id(uid):
    with open(UPLOADED_LOG, 'a') as f:
        f.write(f'{uid}\n')


# ============================================================
#  YOUTUBE
# ============================================================

def get_youtube_service():
    creds = Credentials(
        token=None,
        refresh_token=REFRESH_TOKEN,
        token_uri='https://oauth2.googleapis.com/token',
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        scopes=['https://www.googleapis.com/auth/youtube.upload']
    )
    creds.refresh(Request())
    return build('youtube', 'v3', credentials=creds)

def upload_to_youtube(youtube, file_path, title, description):
    body = {
        'snippet': {
            'title': title[:100],
            'description': description,
            'tags': ['UFC','MMA','UFC highlights','mixed martial arts',
                     'knockout','submission','fight night','octagon'],
            'categoryId': YOUTUBE_CATEGORY,
        },
        'status': {'privacyStatus': 'public'},
    }
    media = MediaFileUpload(file_path, mimetype='video/*', resumable=True, chunksize=5*1024*1024)
    request = youtube.videos().insert(part='snippet,status', body=body, media_body=media)
    response = None
    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                print(f'   Upload: {int(status.progress()*100)}%', end='\r')
        except HttpError as e:
            if e.resp.status in [500, 502, 503, 504]:
                print('   Retrying...')
                time.sleep(10)
            else:
                raise
    video_id = response.get('id')
    print(f'   ✅ https://youtube.com/watch?v={video_id}')
    return video_id


# ============================================================
#  TELEGRAM — get latest unuploaded video from channel
# ============================================================

async def get_latest_video(tg_client, channel, uploaded_ids):
    try:
        async for message in tg_client.iter_messages(channel, limit=50):
            if not message.media:
                continue
            if not isinstance(message.media, MessageMediaDocument):
                continue
            for attr in message.media.document.attributes:
                if isinstance(attr, DocumentAttributeVideo):
                    uid = f'{channel}_{message.id}'
                    if uid not in uploaded_ids:
                        return message, uid
                    else:
                        return None, None  # latest already uploaded
    except Exception as e:
        print(f'   ❌ Error reading @{channel}: {e}')
    return None, None


# ============================================================
#  MAIN
# ============================================================

async def main():
    Path(DOWNLOAD_FOLDER).mkdir(parents=True, exist_ok=True)

    print('=' * 60)
    print(f'🚀 Uploader started at {datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")}')
    print('=' * 60)

    # Load history
    uploaded_ids = load_uploaded_ids()
    print(f'📋 {len(uploaded_ids)} videos already uploaded')

    # YouTube
    print('🔑 Authenticating YouTube...')
    youtube = get_youtube_service()
    print('✅ YouTube ready')

    # Telegram — authenticate using session string from env
    print('📡 Connecting Telegram...')
    session_string = os.environ.get('TELEGRAM_SESSION', '')

    tg_client = TelegramClient(
        StringSession(session_string),
        TELEGRAM_API_ID,
        TELEGRAM_API_HASH
    )
    await tg_client.connect()
    print('✅ Telegram ready')

    # Build ordered channel list by tier
    ordered_channels = []
    for tier in sorted(CHANNELS.keys()):
        for ch in CHANNELS[tier]:
            ordered_channels.append((tier, ch))

    print(f'\n📺 {len(ordered_channels)} channels across {len(CHANNELS)} tiers')
    print(f'🎯 Will upload max {MAX_UPLOADS} videos this run\n')

    slots_remaining = MAX_UPLOADS
    total_uploaded = 0

    for tier, channel in ordered_channels:
        if slots_remaining <= 0:
            print(f'✋ Max uploads ({MAX_UPLOADS}) reached, stopping.')
            break

        print(f'\n[Tier {tier}] @{channel}')
        message, uid = await get_latest_video(tg_client, channel, uploaded_ids)

        if message is None:
            print(f'   ⏭️  No new video')
            continue

        size_mb = message.media.document.size / 1024 / 1024
        original_caption = (message.text or '').strip()

        print(f'   🎬 Found: {size_mb:.1f} MB | ID: {message.id}')
        print(f'   Caption: {original_caption[:70] if original_caption else "[None]"}')

        # Translate
        translated, was_translated = translate_if_russian(original_caption)
        if was_translated:
            print(f'   🌐 Translated from Russian')

        # Title
        title = translated.split('\n')[0].strip()[:100] if translated.strip() else \
                f'UFC Highlights {message.date.strftime("%Y-%m-%d")}'
        description = build_description(original_caption, translated, was_translated)
        print(f'   Title: {title}')

        # Download
        print(f'   📥 Downloading...')
        try:
            file_path = await tg_client.download_media(message, file=DOWNLOAD_FOLDER)
            print(f'   ✅ Downloaded: {os.path.basename(str(file_path))}')
        except Exception as e:
            print(f'   ❌ Download failed: {e}')
            continue

        # Filter + re-encode
        file_path = apply_filter_and_reencode(str(file_path))

        # Upload
        try:
            upload_to_youtube(youtube, file_path, title, description)
            save_uploaded_id(uid)
            uploaded_ids.add(uid)
            total_uploaded += 1
            slots_remaining -= 1
        except Exception as e:
            print(f'   ❌ Upload failed: {e}')
        finally:
            if os.path.exists(file_path):
                os.remove(file_path)

        if slots_remaining > 0:
            print(f'   ⏳ Waiting {UPLOAD_DELAY}s...')
            await asyncio.sleep(UPLOAD_DELAY)

    await tg_client.disconnect()

    print('\n' + '=' * 60)
    print(f'✅ Run complete! Uploaded: {total_uploaded} | Slots used: {total_uploaded}/{MAX_UPLOADS}')
    print('=' * 60)


if __name__ == '__main__':
    from telethon.sessions import StringSession
    asyncio.run(main())
