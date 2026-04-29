"""
Run this ONCE on your PC to generate a Telegram session string.
Copy the output string and save it as TELEGRAM_SESSION in GitHub Secrets.

Run:
    pip install telethon
    python generate_session.py
"""
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

API_ID   = 2040
API_HASH = 'b18441a1ff607e10a989891a5462e627'

with TelegramClient(StringSession(), API_ID, API_HASH) as client:
    session_string = client.session.save()

print('\n' + '='*60)
print('✅ Your session string (copy ALL of this):')
print('='*60)
print(session_string)
print('='*60)
print('\nSave this as TELEGRAM_SESSION in GitHub Secrets.')
print('Never share this string — it gives full account access.')
