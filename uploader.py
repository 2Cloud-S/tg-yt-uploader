"""
Telegram → YouTube Auto-Uploader
=================================
Config-driven — supports multiple niches (UFC, Animals, etc.)
Pass niche via --niche argument or NICHE env var.

Usage:
    python uploader.py --niche ufc
    python uploader.py --niche animals
    NICHE=animals python uploader.py
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

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
#  LOAD CONFIG
# ============================================================

def load_config(niche: str) -> dict:
    path = Path(__file__).parent / 'configs' / f'{niche}.json'
    if not path.exists():
        print(f'❌ Config not found: {path}')
        print(f'   Available: {[p.stem for p in Path("configs").glob("*.json")]}')
        sys.exit(1)
    with open(path) as f:
        cfg = json.load(f)
    print(f'✅ Loaded config: {niche}')
    return cfg

def get_secret(key: str) -> str:
    val = os.environ.get(key, '')
    if not val:
        print(f'⚠️  Missing env var: {key}')
    return val


# ============================================================
#  PERSISTENCE
# ============================================================

def load_uploaded_ids(cfg: dict) -> set:
    path = cfg['uploaded_log']
    if not os.path.exists(path):
        return set()
    with open(path) as f:
        return set(line.strip() for line in f if line.strip())

def save_uploaded_id(cfg: dict, uid: str):
    with open(cfg['uploaded_log'], 'a') as f:
        f.write(f'{uid}\n')

def load_archive_state(cfg: dict) -> dict:
    path = cfg['archive_state']
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)

def save_archive_state(cfg: dict, state: dict):
    with open(cfg['archive_state'], 'w') as f:
        json.dump(state, f, indent=2)


# ============================================================
#  TRANSLATION
# ============================================================

translator = Translator()

def translate_if_russian(text: str):
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

def build_description(cfg, original, translated, was_translated, is_short=False):
    parts = []
    if translated:
        parts.append(translated)
    if was_translated and original:
        parts.append(f'\n---\n🇷🇺 Original:\n{original}')
    parts.append('\n' + '─' * 40)
    tags = cfg['shorts_hashtags'] if is_short else cfg['video_hashtags']
    parts.append(' '.join(tags))
    return '\n'.join(parts)


# ============================================================
#  TELEGRAM HELPERS
# ============================================================

def is_video_message(message) -> bool:
    if not message.media:
        return False
    if not isinstance(message.media, MessageMediaDocument):
        return False
    for attr in message.media.document.attributes:
        if isinstance(attr, DocumentAttributeVideo):
            return True
    return False

def get_tg_video_duration(message) -> float:
    for attr in message.media.document.attributes:
        if isinstance(attr, DocumentAttributeVideo):
            return attr.duration or 0
    return 0

async def get_latest_short_video(tg_client, channel, uploaded_ids, uid_prefix='tier'):
    try:
        async for msg in tg_client.iter_messages(channel, limit=50):
            if not is_video_message(msg):
                continue
            uid = f'{uid_prefix}_{channel}_{msg.id}'
            if uid not in uploaded_ids:
                return msg, uid
            return None, None   # latest already done
    except Exception as e:
        print(f'   ❌ @{channel}: {e}')
    return None, None

async def get_latest_longform_video(tg_client, channel, uploaded_ids, min_secs):
    try:
        async for msg in tg_client.iter_messages(channel, limit=30):
            if not is_video_message(msg):
                continue
            if get_tg_video_duration(msg) < min_secs:
                continue
            uid = f'longform_{channel}_{msg.id}'
            if uid not in uploaded_ids:
                return msg, uid
    except Exception as e:
        print(f'   ❌ @{channel}: {e}')
    return None, None

async def get_archive_video(tg_client, channel, uploaded_ids):
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
#  DOWNLOAD WITH RETRY
# ============================================================

async def download_with_retry(tg_client, msg, dest, retries=3, timeout=300):
    for attempt in range(1, retries + 1):
        try:
            path = await asyncio.wait_for(
                tg_client.download_media(msg, file=dest),
                timeout=timeout
            )
            if path:
                print(f'   ✅ Downloaded: {os.path.basename(str(path))}')
                return str(path)
        except asyncio.TimeoutError:
            print(f'   ⏱️  Timeout (attempt {attempt}/{retries})')
        except Exception as e:
            print(f'   ⚠️  Attempt {attempt}/{retries}: {e}')
        if attempt < retries:
            wait = attempt * 5
            print(f'   🔄 Retry in {wait}s...')
            await asyncio.sleep(wait)
            try:
                await tg_client.connect()
            except Exception:
                pass
    print(f'   ❌ Download failed after {retries} attempts')
    return None


# ============================================================
#  VIDEO PROCESSING
# ============================================================

def get_duration_ffprobe(path) -> float:
    cmd = ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_format', path]
    r = subprocess.run(cmd, capture_output=True, text=True)
    try:
        return float(json.loads(r.stdout)['format']['duration'])
    except Exception:
        return 0.0

def apply_filter_and_reencode(input_path) -> str:
    out = input_path.rsplit('.', 1)[0] + '_out.mp4'
    print('   🎨 Filtering + re-encoding...')
    vf = "eq=brightness=0.04:contrast=1.05:saturation=1.1,unsharp=5:5:0.5:5:5:0.0"
    cmd = ['ffmpeg', '-y', '-i', input_path, '-vf', vf,
           '-c:v', 'libx264', '-preset', 'fast', '-crf', '22',
           '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart', out]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
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

def pick_segment(cfg, caption, total_duration):
    text = (caption or '').lower()
    early_score = sum(1 for kw in cfg['early_keywords'] if kw in text)
    late_score  = sum(1 for kw in cfg['late_keywords']  if kw in text)
    short_dur   = cfg['short_duration']

    print(f'   🧠 Keywords: early={early_score} late={late_score}', end=' → ')

    if early_score > late_score:
        start = max(5, total_duration * 0.05)
        end   = min(start + short_dur, total_duration * 0.35)
        print('EARLY segment')
    else:
        end   = total_duration - 5
        start = max(0, end - short_dur)
        print('LATE segment')

    return start, min(short_dur, end - start)

def make_short(input_path, start_s, duration_s, output_path) -> bool:
    print(f'   ✂️  Clipping {start_s:.0f}s–{start_s+duration_s:.0f}s → 9:16...')
    vf = (
        "crop=ih*9/16:ih,"
        "scale=1080:1920,"
        "eq=brightness=0.04:contrast=1.05:saturation=1.15,"
        "unsharp=5:5:0.8:5:5:0.0"
    )
    cmd = [
        'ffmpeg', '-y',
        '-ss', str(start_s), '-i', input_path,
        '-t', str(duration_s),
        '-vf', vf,
        '-c:v', 'libx264', '-preset', 'fast', '-crf', '22',
        '-c:a', 'aac', '-b:a', '192k',
        '-movflags', '+faststart', output_path
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(f'   ❌ Clip failed: {r.stderr[-200:]}')
        return False
    print(f'   ✅ Short: {os.path.getsize(output_path)/1024/1024:.1f} MB')
    return True


# ============================================================
#  YOUTUBE
# ============================================================

def get_youtube_service(cfg):
    creds = Credentials(
        token=None,
        refresh_token=get_secret(cfg['youtube_refresh_token_secret']),
        token_uri='https://oauth2.googleapis.com/token',
        client_id=get_secret(cfg['youtube_client_id_secret']),
        client_secret=get_secret(cfg['youtube_client_secret_secret']),
        scopes=['https://www.googleapis.com/auth/youtube.upload']
    )
    creds.refresh(Request())
    return build('youtube', 'v3', credentials=creds)

def upload_to_youtube(youtube, cfg, file_path, title, description,
                      tags=None, is_short=False):
    body = {
        'snippet': {
            'title': title[:100],
            'description': description,
            'tags': tags or cfg['video_tags'],
            'categoryId': cfg['youtube_category'],
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

async def process_regular(tg_client, youtube, cfg, msg, channel,
                           uid, uploaded_ids, download_folder):
    size_mb = msg.media.document.size / 1024 / 1024
    if size_mb > cfg['max_video_size_mb']:
        print(f'   ⏭️  {size_mb:.1f} MB > limit, skipping')
        return False

    caption = (msg.text or '').strip()
    print(f'   🎬 {size_mb:.1f} MB | ID: {msg.id}')
    print(f'   Caption: {caption[:70] or "[None]"}')

    translated, was_russian = translate_if_russian(caption)
    if was_russian:
        print('   🌐 Translated from Russian')

    title = (translated.split('\n')[0].strip()[:100] if translated.strip()
             else f'{cfg["fallback_title_prefix"]} {msg.date.strftime("%Y-%m-%d")}')
    desc  = build_description(cfg, caption, translated, was_russian)
    print(f'   Title: {title}')

    path = await download_with_retry(tg_client, msg, download_folder)
    if not path:
        return False

    path = apply_filter_and_reencode(path)
    try:
        upload_to_youtube(youtube, cfg, path, title, desc)
        save_uploaded_id(cfg, uid)
        uploaded_ids.add(uid)
        return True
    except Exception as e:
        print(f'   ❌ Upload failed: {e}')
        return False
    finally:
        if os.path.exists(path):
            os.remove(path)


async def process_longform(tg_client, youtube, cfg, msg, channel,
                            uid, uploaded_ids, download_folder):
    total_dur = get_tg_video_duration(msg)
    size_mb   = msg.media.document.size / 1024 / 1024
    caption   = (msg.text or '').strip()

    print(f'   📹 {size_mb:.1f} MB | {total_dur/60:.1f} min | ID: {msg.id}')
    print(f'   Caption: {caption[:70] or "[None]"}')

    start_s, clip_dur = pick_segment(cfg, caption, total_dur)

    # Under 150 MB: download full (fast enough on GitHub Actions)
    # Over 150 MB: attempt partial byte-range download
    if size_mb <= 150:
        print(f'   📥 Downloading ({size_mb:.1f} MB)...')
        raw_path = await download_with_retry(tg_client, msg, download_folder)
        if not raw_path:
            return False
    else:
        total_bytes = msg.media.document.size
        b_start = int((start_s / total_dur) * total_bytes)
        b_end   = int(((start_s + clip_dur * 1.25) / total_dur) * total_bytes)
        partial_mb = (b_end - b_start) / 1024 / 1024
        raw_path = os.path.join(download_folder, f'lf_{msg.id}_raw.mp4')
        print(f'   📥 Partial download ~{partial_mb:.1f} MB of {size_mb:.1f} MB...')
        try:
            await tg_client.download_media(msg, file=raw_path,
                                           offset=b_start, limit=b_end - b_start)
            print('   ✅ Partial downloaded')
            start_s = 2   # partial file starts at our segment
        except TypeError:
            print('   ⚠️ Partial unsupported, downloading full...')
            raw_path = await download_with_retry(tg_client, msg, download_folder)
            if not raw_path:
                return False

    short_path = os.path.join(download_folder, f'lf_{msg.id}_short.mp4')
    ok = make_short(raw_path, start_s, clip_dur, short_path)
    if os.path.exists(raw_path):
        os.remove(raw_path)
    if not ok:
        return False

    translated, was_russian = translate_if_russian(caption)
    base = (translated.split('\n')[0].strip()
            if translated.strip()
            else f'{cfg["fallback_short_title_prefix"]} {msg.date.strftime("%Y-%m-%d")}')
    title = f'{base} #Shorts'[:100]
    desc  = build_description(cfg, caption, translated, was_russian, is_short=True)
    print(f'   Title: {title}')

    try:
        upload_to_youtube(youtube, cfg, short_path, title, desc,
                          tags=cfg['shorts_tags'], is_short=True)
        save_uploaded_id(cfg, uid)
        uploaded_ids.add(uid)
        return True
    except Exception as e:
        print(f'   ❌ Short upload failed: {e}')
        return False
    finally:
        if os.path.exists(short_path):
            os.remove(short_path)


async def process_archive(tg_client, youtube, cfg, msg, channel,
                           uid, uploaded_ids, download_folder):
    duration = get_tg_video_duration(msg)
    caption  = (msg.text or '').strip()
    size_mb  = msg.media.document.size / 1024 / 1024

    print(f'   🗂️  {size_mb:.1f} MB | {duration:.0f}s | ID: {msg.id}')

    if duration > cfg['short_min_secs']:
        print('   📹 Long archive clip → Short pipeline')
        uid_lf = uid.replace('archive_', 'longform_')
        return await process_longform(tg_client, youtube, cfg, msg, channel,
                                      uid_lf, uploaded_ids, download_folder)

    translated, was_russian = translate_if_russian(caption)
    if was_russian:
        print('   🌐 Translated from Russian')

    title = (translated.split('\n')[0].strip()[:100]
             if translated.strip()
             else f'{cfg["fallback_archive_title_prefix"]} {msg.date.strftime("%Y-%m-%d")}')
    desc  = build_description(cfg, caption, translated, was_russian)
    print(f'   Title: {title}')

    path = await download_with_retry(tg_client, msg, download_folder)
    if not path:
        return False

    path = apply_filter_and_reencode(path)
    try:
        upload_to_youtube(youtube, cfg, path, title, desc)
        save_uploaded_id(cfg, uid)
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

async def main(cfg):
    download_folder = f'/tmp/downloads_{cfg["niche"]}'
    Path(download_folder).mkdir(parents=True, exist_ok=True)

    print('=' * 60)
    print(f'🚀 Niche: {cfg["niche"].upper()} | {datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")}')
    print('=' * 60)

    uploaded_ids  = load_uploaded_ids(cfg)
    archive_state = load_archive_state(cfg)
    print(f'📋 {len(uploaded_ids)} videos tracked')

    print('🔑 YouTube auth...')
    youtube = get_youtube_service(cfg)
    print('✅ YouTube ready')

    print('📡 Telegram connecting...')
    tg_client = TelegramClient(
        StringSession(os.environ.get('TELEGRAM_SESSION', '')),
        int(os.environ['TELEGRAM_API_ID']),
        os.environ['TELEGRAM_API_HASH']
    )
    await tg_client.connect()
    print('✅ Telegram ready\n')

    total = 0

    # ── PHASE 1: Tier channels ────────────────────────────────
    print('─' * 60)
    print('📺 PHASE 1: Tier channels')
    print('─' * 60)

    channels_map = {int(k): v for k, v in cfg['channels'].items()}
    slots = cfg['max_regular_uploads']

    for tier in sorted(channels_map):
        for channel in channels_map[tier]:
            if slots <= 0:
                break
            print(f'\n[Tier {tier}] @{channel}')
            msg, uid = await get_latest_short_video(tg_client, channel, uploaded_ids)
            if msg is None:
                print('   ⏭️  No new video')
                continue
            ok = await process_regular(tg_client, youtube, cfg, msg,
                                       channel, uid, uploaded_ids, download_folder)
            if ok:
                total += 1
                slots -= 1
                if slots > 0:
                    await asyncio.sleep(cfg['upload_delay'])

    # ── PHASE 2: Longform → Short ─────────────────────────────
    print('\n' + '─' * 60)
    print('📹 PHASE 2: Longform → Short (max 1)')
    print('─' * 60)

    short_done = False
    for channel in cfg.get('longform_channels', []):
        if short_done:
            break
        print(f'\n[Longform] @{channel}')
        msg, uid = await get_latest_longform_video(
            tg_client, channel, uploaded_ids, cfg['short_min_secs'])
        if msg is None:
            print('   ⏭️  No new longform')
            continue
        ok = await process_longform(tg_client, youtube, cfg, msg,
                                    channel, uid, uploaded_ids, download_folder)
        if ok:
            total += 1
            short_done = True
            await asyncio.sleep(cfg['upload_delay'])

    if not cfg.get('longform_channels'):
        print('   ℹ️  No longform channels configured')
    elif not short_done:
        print('   ℹ️  No new longform video found')

    # ── PHASE 3: Archive ──────────────────────────────────────
    print('\n' + '─' * 60)
    print(f'🗂️  PHASE 3: Archive (max {cfg["max_archive_uploads"]})')
    print('─' * 60)

    arch_count = 0
    for channel in cfg.get('archive_channels', []):
        if arch_count >= cfg['max_archive_uploads']:
            break
        print(f'\n[Archive] @{channel}')
        msg, uid = await get_archive_video(tg_client, channel, uploaded_ids)
        if msg is None:
            print('   ⏭️  No unuploaded clips')
            continue
        ok = await process_archive(tg_client, youtube, cfg, msg,
                                   channel, uid, uploaded_ids, download_folder)
        if ok:
            total += 1
            arch_count += 1
            if arch_count < cfg['max_archive_uploads']:
                await asyncio.sleep(cfg['upload_delay'])

    if not cfg.get('archive_channels'):
        print('   ℹ️  No archive channels configured')

    # ── Wrap up ───────────────────────────────────────────────
    await tg_client.disconnect()
    save_archive_state(cfg, archive_state)

    print('\n' + '=' * 60)
    print(f'✅ {cfg["niche"].upper()} run complete!')
    print(f'   Regular : {cfg["max_regular_uploads"] - slots}/{cfg["max_regular_uploads"]}')
    print(f'   Shorts  : {"1" if short_done else "0"}/1')
    print(f'   Archive : {arch_count}/{cfg["max_archive_uploads"]}')
    print(f'   Total   : {total}')
    print('=' * 60)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--niche', default=os.environ.get('NICHE', 'ufc'),
                        help='Niche to run (e.g. ufc, animals)')
    args = parser.parse_args()

    cfg = load_config(args.niche)
    asyncio.run(main(cfg))
