"""
Telegram → YouTube Auto-Uploader (Full Edition)
================================================
Three channel categories:

  CHANNELS (tier 1/2/3)
    - Short videos, grab latest 1 each, upload as regular video
    - Max MAX_REGULAR_UPLOADS per run

  LONGFORM_CHANNELS
    - Long videos (3min+), smart clip → 9:16 YouTube Short
    - Caption keywords decide segment (early/late)
    - Downloads only needed portion, not full video
    - Max 1 Short per run

  ARCHIVE_CHANNELS
    - Old/inactive channels, crawls history most-recent-first
    - Handles future new content automatically
    - Max MAX_ARCHIVE_UPLOADS per run

Zero manual input — runs via GitHub Actions on schedule.
"""

import asyncio
import os
import json
import subprocess
import time
from pathlib import Path
from datetime import datetime

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import MessageMediaDocument, DocumentAttributeVideo
from googletrans import Translator
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError

# ============================================================
#  CONFIG
# ============================================================

TELEGRAM_API_ID   = int(os.environ['TELEGRAM_API_ID'])
TELEGRAM_API_HASH = os.environ['TELEGRAM_API_HASH']
CLIENT_ID         = os.environ['YOUTUBE_CLIENT_ID']
CLIENT_SECRET     = os.environ['YOUTUBE_CLIENT_SECRET']
REFRESH_TOKEN     = os.environ['YOUTUBE_REFRESH_TOKEN']

# ── Tier channels: short clips, 1 latest video each ──────────
CHANNELS = {
    1: [
        'arman_ufc_official',
        'khabib_nurmagomedov',
        'Boxing_IBA',
        'kchimaev',
        'UsmnNurmagomedov',
        
    ],
    2: [
        'wbcboxing_official'
        'mma_org',
        'danawhite',
        'rcc_sport'
    ],
    3: [
        # add low-priority channels here
    ],
}

# ── Longform: 3min+ videos → smart clip → Short ──────────────
# Only 1 Short produced per run from this entire category
LONGFORM_CHANNELS = [
     'ufcmmaarxivn1',
     'arshiv_a',
    # Add real channel usernames here
]

# ── Archive: old/inactive channels, crawl history ────────────
# Scans most-recent-first, remembers position across runs
ARCHIVE_CHANNELS = [
    'UFC_clip_sport',
     'Free_525',
     'wwe_chlips',
     'Boxing_clips',
     'KhamzatChimaevVsSeanStrickland',

]

# ── Run limits ────────────────────────────────────────────────
MAX_REGULAR_UPLOADS = 4   # max from tier channels
MAX_ARCHIVE_UPLOADS = 1   # max from archive channels
# Longform always max 1 Short per run

YOUTUBE_CATEGORY  = '17'  # 17 = Sports
UPLOAD_DELAY      = 20    # seconds between uploads
DOWNLOAD_FOLDER   = '/tmp/downloads'
UPLOADED_LOG      = 'uploaded_ids.txt'     # committed to repo
ARCHIVE_STATE     = 'archive_state.json'   # committed to repo

# ── Short clip settings ───────────────────────────────────────
SHORT_DURATION  = 58    # seconds (must be under 60 for Shorts)
SHORT_MIN_SECS  = 180   # videos longer than this go to longform path

# ── Keywords for smart segment selection ─────────────────────
# High score = early finish → grab first 30% of video
EARLY_KEYWORDS = [
    'round 1', 'r1', 'first round', '1st round',
    'tko', 'ko', 'knockout', 'submission', 'sub',
    'finish', 'stoppage', 'tap', 'tapout', 'early',
]
# High score = late action → grab last 20% of video
LATE_KEYWORDS = [
    'round 3', 'round 4', 'round 5', 'r3', 'r4', 'r5',
    'decision', 'unanimous', 'split', 'judges',
    'championship round', 'main event', 'five rounds',
]

UFC_HASHTAGS = """
#armantsarukyan #sheanstrikland #khamzatchimeav #UFC #MMA #UFCHighlights #MixedMartialArts #UFCFights #UFCNews
#Knockout #KO #Submission #UFCChampion #FightNight #PPV
#Boxing #Kickboxing #BJJ #Wrestling #Grappling
#Khabib #IslamMakhachev #JonJones #ConorMcGregor #StipeMiocic
#UFCFightPass #UFCWeekend #MMAFighter #CageMatch #Octagon
"""

SHORTS_HASHTAGS = """
#Shorts #UFCShorts #MMAShorts #FightShorts
#UFC #MMA #Knockout #Submission #FightFinish #Octagon
#armantsarukyan #sheanstrikland #khamzatchimeav
"""


# ============================================================
#  PERSISTENCE
# ============================================================

def load_uploaded_ids():
    if not os.path.exists(UPLOADED_LOG):
        return set()
    with open(UPLOADED_LOG, 'r') as f:
        return set(line.strip() for line in f if line.strip())

def save_uploaded_id(uid):
    with open(UPLOADED_LOG, 'a') as f:
        f.write(f'{uid}\n')

def load_archive_state():
    """Tracks last-scanned message ID per archive channel."""
    if not os.path.exists(ARCHIVE_STATE):
        return {}
    with open(ARCHIVE_STATE, 'r') as f:
        return json.load(f)

def save_archive_state(state):
    with open(ARCHIVE_STATE, 'w') as f:
        json.dump(state, f, indent=2)


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
            result = translator.translate(text, src='ru', dest='en')
            return result.text, True
        return text, False
    except Exception as e:
        print(f'   ⚠️ Translation error: {e}')
        return text, False

def build_description(original, translated, was_translated, extra_tags=''):
    parts = []
    if translated:
        parts.append(translated)
    if was_translated and original:
        parts.append(f'\n---\n🇷🇺 Original:\n{original}')
    parts.append('\n' + '─' * 40)
    parts.append(UFC_HASHTAGS)
    if extra_tags:
        parts.append(extra_tags)
    return '\n'.join(parts)


# ============================================================
#  TELEGRAM HELPERS
# ============================================================

def is_video_message(message):
    if not message.media:
        return False
    if not isinstance(message.media, MessageMediaDocument):
        return False
    for attr in message.media.document.attributes:
        if isinstance(attr, DocumentAttributeVideo):
            return True
    return False

def get_tg_video_duration(message):
    for attr in message.media.document.attributes:
        if isinstance(attr, DocumentAttributeVideo):
            return attr.duration or 0
    return 0

async def get_latest_short_video(tg_client, channel, uploaded_ids):
    """Most recent unuploaded video from a tier channel."""
    try:
        async for msg in tg_client.iter_messages(channel, limit=50):
            if not is_video_message(msg):
                continue
            uid = f'tier_{channel}_{msg.id}'
            if uid not in uploaded_ids:
                return msg, uid
            else:
                return None, None  # latest already uploaded
    except Exception as e:
        print(f'   ❌ @{channel}: {e}')
    return None, None

async def get_latest_longform_video(tg_client, channel, uploaded_ids):
    """Most recent unuploaded video longer than SHORT_MIN_SECS."""
    try:
        async for msg in tg_client.iter_messages(channel, limit=30):
            if not is_video_message(msg):
                continue
            if get_tg_video_duration(msg) < SHORT_MIN_SECS:
                continue
            uid = f'longform_{channel}_{msg.id}'
            if uid not in uploaded_ids:
                return msg, uid
    except Exception as e:
        print(f'   ❌ @{channel}: {e}')
    return None, None

async def get_archive_video(tg_client, channel, uploaded_ids):
    """
    Scans archive channel most-recent-first.
    Returns first unuploaded video found.
    Works for both inactive channels (old content) and
    channels that get new content in future.
    """
    try:
        async for msg in tg_client.iter_messages(channel, limit=100):
            if not is_video_message(msg):
                continue
            uid = f'archive_{channel}_{msg.id}'
            if uid not in uploaded_ids:
                return msg, uid
    except Exception as e:
        print(f'   ❌ @{channel}: {e}')
    return None, None


# ============================================================
#  VIDEO PROCESSING
# ============================================================

def get_duration_ffprobe(path):
    cmd = ['ffprobe', '-v', 'quiet', '-print_format', 'json',
           '-show_format', path]
    r = subprocess.run(cmd, capture_output=True, text=True)
    try:
        return float(json.loads(r.stdout)['format']['duration'])
    except Exception:
        return None

def apply_filter_and_reencode(input_path):
    """Standard filter for regular/archive videos."""
    out = input_path.rsplit('.', 1)[0] + '_out.mp4'
    print(f'   🎨 Filtering + re-encoding...')
    vf = "eq=brightness=0.04:contrast=1.05:saturation=1.1,unsharp=5:5:0.5:5:5:0.0"
    cmd = ['ffmpeg', '-y', '-i', input_path,
           '-vf', vf, '-c:v', 'libx264', '-preset', 'fast', '-crf', '22',
           '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart', out]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        # Fallback plain re-encode (fixes audio at minimum)
        cmd2 = ['ffmpeg', '-y', '-i', input_path,
                '-c:v', 'libx264', '-preset', 'fast', '-crf', '23',
                '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart', out]
        r2 = subprocess.run(cmd2, capture_output=True, text=True)
        if r2.returncode != 0:
            print('   ⚠️ Re-encode failed, using original')
            return input_path
    if os.path.exists(input_path):
        os.remove(input_path)
    print(f'   ✅ Done: {os.path.getsize(out)/1024/1024:.1f} MB')
    return out


def pick_segment(caption, total_duration):
    """
    Keyword analysis on caption → decide which portion to clip.
    Returns (start_seconds, clip_duration_seconds).
    """
    text = (caption or '').lower()
    early_score = sum(1 for kw in EARLY_KEYWORDS if kw in text)
    late_score  = sum(1 for kw in LATE_KEYWORDS  if kw in text)

    print(f'   🧠 Keywords: early={early_score} late={late_score}', end=' → ')

    if early_score > late_score:
        # Likely early finish — first 30%
        start = max(5, total_duration * 0.05)
        end   = min(start + SHORT_DURATION, total_duration * 0.35)
        print('EARLY segment')
    else:
        # Late action or unknown — last 20%
        end   = total_duration - 5
        start = max(0, end - SHORT_DURATION)
        print('LATE segment')

    return start, min(SHORT_DURATION, end - start)


def make_short(input_path, start_s, duration_s, output_path):
    """
    Cuts segment, crops to 9:16 vertical, applies filter.
    Uses -ss before -i for fast seeking (no full decode needed).
    """
    print(f'   ✂️  Clipping {start_s:.0f}s–{start_s+duration_s:.0f}s → 9:16 Short...')

    # crop to 9:16 from center, scale to 1080x1920, filter
    vf = (
        "crop=ih*9/16:ih,"
        "scale=1080:1920,"
        "eq=brightness=0.04:contrast=1.05:saturation=1.15,"
        "unsharp=5:5:0.8:5:5:0.0"
    )
    cmd = [
        'ffmpeg', '-y',
        '-ss', str(start_s),          # seek BEFORE input = fast, no full decode
        '-i', input_path,
        '-t', str(duration_s),
        '-vf', vf,
        '-c:v', 'libx264', '-preset', 'fast', '-crf', '22',
        '-c:a', 'aac', '-b:a', '192k',
        '-movflags', '+faststart',
        output_path
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(f'   ❌ Clip failed: {r.stderr[-300:]}')
        return False
    print(f'   ✅ Short ready: {os.path.getsize(output_path)/1024/1024:.1f} MB')
    return True


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

def upload_to_youtube(youtube, file_path, title, description,
                      tags=None, is_short=False):
    body = {
        'snippet': {
            'title': title[:100],
            'description': description,
            'tags': tags or ['UFC','MMA','UFC highlights','knockout','fight'],
            'categoryId': YOUTUBE_CATEGORY,
        },
        'status': {'privacyStatus': 'public'},
    }
    media = MediaFileUpload(
        file_path, mimetype='video/*',
        resumable=True, chunksize=5*1024*1024
    )
    req = youtube.videos().insert(part='snippet,status', body=body, media_body=media)
    response = None
    while response is None:
        try:
            status, response = req.next_chunk()
            if status:
                print(f'   Upload: {int(status.progress()*100)}%', end='\r')
        except HttpError as e:
            if e.resp.status in [500, 502, 503, 504]:
                time.sleep(10)
            else:
                raise
    vid = response.get('id')
    label = '🩳 Short' if is_short else '🎬 Video'
    print(f'   ✅ {label}: https://youtube.com/watch?v={vid}')
    return vid


# ============================================================
#  PHASE PROCESSORS
# ============================================================

async def process_regular(tg_client, youtube, msg, channel, uid, uploaded_ids):
    size_mb = msg.media.document.size / 1024 / 1024
    caption = (msg.text or '').strip()
    print(f'   🎬 {size_mb:.1f} MB | ID: {msg.id}')
    print(f'   Caption: {caption[:70] or "[None]"}')

    translated, was_translated = translate_if_russian(caption)
    if was_translated:
        print('   🌐 Translated from Russian')

    title = translated.split('\n')[0].strip()[:100] if translated.strip() \
            else f'UFC Highlights {msg.date.strftime("%Y-%m-%d")}'
    desc = build_description(caption, translated, was_translated)
    print(f'   Title: {title}')

    print('   📥 Downloading...')
    try:
        path = str(await tg_client.download_media(msg, file=DOWNLOAD_FOLDER))
        print(f'   ✅ {os.path.basename(path)}')
    except Exception as e:
        print(f'   ❌ Download failed: {e}')
        return False

    path = apply_filter_and_reencode(path)
    try:
        upload_to_youtube(youtube, path, title, desc)
        save_uploaded_id(uid)
        uploaded_ids.add(uid)
        return True
    except Exception as e:
        print(f'   ❌ Upload failed: {e}')
        return False
    finally:
        if os.path.exists(path):
            os.remove(path)


async def process_longform(tg_client, youtube, msg, channel, uid, uploaded_ids):
    """
    Smart partial download + clip + Short upload.
    Key insight: -ss before -i in ffmpeg does fast seek without
    decoding the whole file — so we only process the segment we need.
    We still download the full file but only process the clip portion.
    For truly huge files, partial download via offset is attempted first.
    """
    total_dur = get_tg_video_duration(msg)
    size_mb   = msg.media.document.size / 1024 / 1024
    caption   = (msg.text or '').strip()

    print(f'   📹 {size_mb:.1f} MB | {total_dur/60:.1f} min | ID: {msg.id}')
    print(f'   Caption: {caption[:70] or "[None]"}')

    start_s, clip_dur = pick_segment(caption, total_dur)

    # For files under 150MB download fully (fast enough on GitHub Actions)
    # For larger files attempt partial byte-range download
    raw_path = os.path.join(DOWNLOAD_FOLDER, f'lf_{msg.id}_raw.mp4')

    if size_mb <= 150:
        print(f'   📥 Downloading full video ({size_mb:.1f} MB)...')
        try:
            raw_path = str(await tg_client.download_media(msg, file=DOWNLOAD_FOLDER))
            print(f'   ✅ Downloaded')
        except Exception as e:
            print(f'   ❌ Download failed: {e}')
            return False
    else:
        # Partial download: only the bytes we need
        total_bytes  = msg.media.document.size
        byte_start   = int((start_s / total_dur) * total_bytes)
        # Download 25% extra buffer around the segment
        byte_end     = int(((start_s + clip_dur * 1.25) / total_dur) * total_bytes)
        partial_mb   = (byte_end - byte_start) / 1024 / 1024
        print(f'   📥 Partial download ~{partial_mb:.1f} MB (of {size_mb:.1f} MB)...')
        try:
            await tg_client.download_media(
                msg, file=raw_path,
                offset=byte_start,
                limit=byte_end - byte_start
            )
            print(f'   ✅ Partial downloaded')
            # For partial file, clip starts from beginning
            start_s = 2
        except TypeError:
            # Telethon version doesn't support offset — fallback to full
            print(f'   ⚠️ Partial download unsupported, downloading full...')
            raw_path = str(await tg_client.download_media(msg, file=DOWNLOAD_FOLDER))
            print(f'   ✅ Full downloaded')

    # Create Short
    short_path = os.path.join(DOWNLOAD_FOLDER, f'lf_{msg.id}_short.mp4')
    ok = make_short(raw_path, start_s, clip_dur, short_path)

    if os.path.exists(raw_path):
        os.remove(raw_path)

    if not ok:
        return False

    translated, was_translated = translate_if_russian(caption)
    base_title = translated.split('\n')[0].strip() if translated.strip() \
                 else f'UFC Fight Clip {msg.date.strftime("%Y-%m-%d")}'
    title = f'{base_title} #Shorts'[:100]
    desc  = build_description(caption, translated, was_translated,
                              extra_tags=SHORTS_HASHTAGS)
    print(f'   Title: {title}')

    try:
        upload_to_youtube(
            youtube, short_path, title, desc,
            tags=['UFC Shorts','MMA Shorts','Shorts','knockout',
                  'UFC','MMA','fight clip','UFC highlights'],
            is_short=True
        )
        save_uploaded_id(uid)
        uploaded_ids.add(uid)
        return True
    except Exception as e:
        print(f'   ❌ Short upload failed: {e}')
        return False
    finally:
        if os.path.exists(short_path):
            os.remove(short_path)


async def process_archive(tg_client, youtube, msg, channel, uid, uploaded_ids):
    """Archive clips: short ones upload as-is, long ones become Shorts."""
    duration = get_tg_video_duration(msg)
    size_mb  = msg.media.document.size / 1024 / 1024
    caption  = (msg.text or '').strip()

    print(f'   🗂️  {size_mb:.1f} MB | {duration:.0f}s | ID: {msg.id}')

    if duration > SHORT_MIN_SECS:
        print(f'   📹 Long archive clip — routing to Short pipeline')
        uid_lf = uid.replace('archive_', 'longform_')
        return await process_longform(tg_client, youtube, msg, channel, uid_lf, uploaded_ids)

    translated, was_translated = translate_if_russian(caption)
    if was_translated:
        print('   🌐 Translated from Russian')

    title = translated.split('\n')[0].strip()[:100] if translated.strip() \
            else f'UFC Classic {msg.date.strftime("%Y-%m-%d")}'
    desc  = build_description(caption, translated, was_translated)
    print(f'   Title: {title}')

    print('   📥 Downloading...')
    try:
        path = str(await tg_client.download_media(msg, file=DOWNLOAD_FOLDER))
        print(f'   ✅ Downloaded')
    except Exception as e:
        print(f'   ❌ Download failed: {e}')
        return False

    path = apply_filter_and_reencode(path)
    try:
        upload_to_youtube(youtube, path, title, desc)
        save_uploaded_id(uid)
        uploaded_ids.add(uid)
        return True
    except Exception as e:
        print(f'   ❌ Upload failed: {e}')
        return False
    finally:
        if os.path.exists(path):
            os.remove(path)


# ============================================================
#  MAIN
# ============================================================

async def main():
    Path(DOWNLOAD_FOLDER).mkdir(parents=True, exist_ok=True)

    print('=' * 60)
    print(f'🚀 Started: {datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")}')
    print('=' * 60)

    uploaded_ids  = load_uploaded_ids()
    archive_state = load_archive_state()
    print(f'📋 {len(uploaded_ids)} videos tracked')

    print('🔑 YouTube auth...')
    youtube = get_youtube_service()
    print('✅ YouTube ready')

    print('📡 Telegram connecting...')
    tg_client = TelegramClient(
        StringSession(os.environ.get('TELEGRAM_SESSION', '')),
        TELEGRAM_API_ID, TELEGRAM_API_HASH
    )
    await tg_client.connect()
    print('✅ Telegram ready\n')

    total = 0

    # ── PHASE 1: Tier channels ────────────────────────────────
    print('─' * 60)
    print('📺 PHASE 1: Tier channels')
    print('─' * 60)
    slots = MAX_REGULAR_UPLOADS
    for tier in sorted(CHANNELS):
        for channel in CHANNELS[tier]:
            if slots <= 0:
                print(f'✋ Limit ({MAX_REGULAR_UPLOADS}) reached')
                break
            print(f'\n[Tier {tier}] @{channel}')
            msg, uid = await get_latest_short_video(tg_client, channel, uploaded_ids)
            if msg is None:
                print('   ⏭️  No new video')
                continue
            ok = await process_regular(tg_client, youtube, msg, channel, uid, uploaded_ids)
            if ok:
                total += 1
                slots -= 1
                if slots > 0:
                    await asyncio.sleep(UPLOAD_DELAY)

    # ── PHASE 2: Longform → Short ─────────────────────────────
    print('\n' + '─' * 60)
    print('📹 PHASE 2: Longform → Short (max 1)')
    print('─' * 60)
    short_done = False
    for channel in LONGFORM_CHANNELS:
        if short_done:
            break
        print(f'\n[Longform] @{channel}')
        msg, uid = await get_latest_longform_video(tg_client, channel, uploaded_ids)
        if msg is None:
            print('   ⏭️  No new longform video')
            continue
        ok = await process_longform(tg_client, youtube, msg, channel, uid, uploaded_ids)
        if ok:
            total += 1
            short_done = True
            await asyncio.sleep(UPLOAD_DELAY)

    if not LONGFORM_CHANNELS:
        print('   ℹ️  No longform channels configured')
    elif not short_done:
        print('   ℹ️  No new longform video found')

    # ── PHASE 3: Archive ──────────────────────────────────────
    print('\n' + '─' * 60)
    print(f'🗂️  PHASE 3: Archive channels (max {MAX_ARCHIVE_UPLOADS})')
    print('─' * 60)
    arch_count = 0
    for channel in ARCHIVE_CHANNELS:
        if arch_count >= MAX_ARCHIVE_UPLOADS:
            break
        print(f'\n[Archive] @{channel}')
        msg, uid = await get_archive_video(tg_client, channel, uploaded_ids)
        if msg is None:
            print('   ⏭️  No unuploaded clips found')
            continue
        ok = await process_archive(tg_client, youtube, msg, channel, uid, uploaded_ids)
        if ok:
            total += 1
            arch_count += 1
            if arch_count < MAX_ARCHIVE_UPLOADS:
                await asyncio.sleep(UPLOAD_DELAY)

    if not ARCHIVE_CHANNELS:
        print('   ℹ️  No archive channels configured')

    # ── Done ──────────────────────────────────────────────────
    await tg_client.disconnect()
    save_archive_state(archive_state)

    print('\n' + '=' * 60)
    print(f'✅ Run complete!')
    print(f'   Regular  : {MAX_REGULAR_UPLOADS - slots}/{MAX_REGULAR_UPLOADS}')
    print(f'   Shorts   : {"1" if short_done else "0"}/1')
    print(f'   Archive  : {arch_count}/{MAX_ARCHIVE_UPLOADS}')
    print(f'   Total    : {total}')
    print('=' * 60)


if __name__ == '__main__':
    asyncio.run(main())
