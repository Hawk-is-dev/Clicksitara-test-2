# Кликер Userbot

Automates: `/start` -> click "✨ Кликер" -> repeat every N minutes.

If the target bot sends the robot-check addition question afterward, the
userbot does **not** answer it. Instead it sends you a message (in your own
Saved Messages by default) with the question and the answer options, so you
can tap the correct button yourself.

## 1. Get your API credentials

- Go to https://my.telegram.org -> API Development Tools
- Create an app, note your `api_id` and `api_hash`

## 2. Generate a session string (run locally, NOT on Railway)

```bash
pip install telethon
python generate_session.py
```

Follow the prompts (phone number, login code, 2FA password if enabled).
Copy the printed string — this is your `SESSION_STRING`. Keep it private;
anyone with it can log into your Telegram account.

## 3. Deploy to Railway

1. Push this folder to a GitHub repo (or use `railway up` from the CLI).
2. Create a new Railway project from that repo.
3. Add environment variables in Railway's dashboard. This version supports
   running multiple accounts (multiple string sessions) in one service.

   **Shared defaults** (used by every account unless overridden below):

   | Variable | Value |
   |---|---|
   | `API_ID` | your api_id |
   | `API_HASH` | your api_hash |
   | `TARGET_BOT` | your bot's username, e.g. `my_target_bot` |
   | `NOTIFY_CHAT` | `me` (Saved Messages) or another chat username |
   | `INTERVAL_MINUTES` | `10` (default) |
   | `BUTTON_TEXT` | `Кликер` (default) |

   **Per-account session strings**, numbered starting at 1:

   | Variable | Value |
   |---|---|
   | `SESSION_STRING_1` | your first account's string session |
   | `SESSION_STRING_2` | your second account's string session |

   Add `SESSION_STRING_3`, `SESSION_STRING_4`, etc. for more accounts —
   the bot picks up however many are set.

   If any account needs a *different* target bot, notify chat, interval,
   or button text than the shared default, add a numbered override, e.g.
   for account 2: `TARGET_BOT_2`, `NOTIFY_CHAT_2`, `API_ID_2`, `API_HASH_2`.

   (Single-account setups can still just use `SESSION_STRING` with no
   suffix — it's treated the same as `SESSION_STRING_1`.)

   **Batching** (optional, useful with many accounts):

   | Variable | Value |
   |---|---|
   | `BATCH_SIZE` | `5` (default) — how many accounts start at once |
   | `BATCH_DELAY_SECONDS` | `5` (default) — pause between starting each batch |

   With the defaults, accounts 1–5 all send `/start` right away, then the
   bot waits 5 seconds before starting accounts 6–10, then another 5
   seconds before 11–15, and so on. Each account still runs its own
   independent loop afterward (every `INTERVAL_MINUTES`).

4. Railway will detect the `Procfile` and run `python bot.py` as a worker.
5. Check the deploy logs — you should see "Logged in as ..." and a
   confirmation message will land in your Saved Messages.

## Optional: promo-code redemption feature (single account only)

One designated account can also watch your source channel for new posts
that contain a **spoiler-hidden** promo code (Telegram's blurred-text
formatting), and redeem it automatically: `/start` -> click "Профиль" ->
click "Промокод" -> send the code -> report the bot's reply to you.

This briefly pauses that account's normal Кликер cycle so the two tasks
never talk to the target bot at the same time — the promo task always
gets priority, and normal Кликер cycling resumes right after.

| Variable | Value |
|---|---|
| `SOURCE_CHANNEL` | your channel's username or ID (unset = feature off) |
| `PROMO_ACCOUNT` | which account watches it, default `account1` |
| `PROFILE_BUTTON_TEXT` | default `Профиль` |
| `PROMO_CODE_BUTTON_TEXT` | default `Промокод` |

This only runs on the one account named in `PROMO_ACCOUNT` — it does not
redeem the code across your other accounts.

## Notes

- This clicks a button on your own bot on a timer; it does not solve or
  bypass the anti-bot math challenge — that part is left for you to do
  manually when notified.
- If Telegram flags automated behavior on your account, that's a risk
  inherent to userbots in general (they run on your personal account, not
  the Bot API) — use judgment about how aggressively you schedule this.
