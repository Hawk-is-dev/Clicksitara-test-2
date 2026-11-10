"""
Run this ONCE on your own computer (not on Railway) to log in interactively
and generate a Telethon string session. It will ask for your phone number
and the login code Telegram sends you.

Usage:
    pip install telethon
    python generate_session.py

Copy the printed string into Railway as the SESSION_STRING env variable.
Keep it secret — it gives full access to your Telegram account.
"""

from telethon import TelegramClient
from telethon.sessions import StringSession

API_ID = int(input("Enter your API_ID: "))
API_HASH = input("Enter your API_HASH: ")

with TelegramClient(StringSession(), API_ID, API_HASH) as client:
    print("\nYour session string (keep this secret!):\n")
    print(client.session.save())
