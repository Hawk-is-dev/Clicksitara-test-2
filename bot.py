import asyncio
import logging
import os
import re

try:  # faster event loop if available (optional)
    import uvloop
    uvloop.install()
except Exception:
    pass

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import MessageEntitySpoiler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("userbot")

# Telethon logs its own internal update-sync chatter ("Got difference for
# channel..." etc.) at INFO level, which drowns out our actual logs. Quiet
# it down to WARNING so only our [account...] lines show up.
logging.getLogger("telethon").setLevel(logging.WARNING)

# Matches things like "43 + 48 = ?" (addition only, per the target bot's behavior)
MATH_PATTERN = re.compile(r"(\d+)\s*\+\s*(\d+)\s*=\s*\?")

# Text the target bot sends when something glitches during the promo flow
# (seen on profile click and on code submit). When this shows up, we retry
# the promo task for that one account/code only.
PROMO_ERROR_TEXT = "Упс! Что-то сломалось"
PROMO_MAX_RETRIES = int(os.environ.get("PROMO_MAX_RETRIES", "3"))
PROMO_RETRY_DELAY_SECONDS = float(os.environ.get("PROMO_RETRY_DELAY_SECONDS", "3"))
# Per-step wait for the bot's reply during the promo flow (was 30s).
PROMO_STEP_TIMEOUT = float(os.environ.get("PROMO_STEP_TIMEOUT", "25"))
# Optional stagger between accounts (seconds * account index). 0 = max speed.
PROMO_STAGGER_SECONDS = float(os.environ.get("PROMO_STAGGER_SECONDS", "0"))


def find_button(buttons, text_substring: str):
    """Search the 2D button grid for a button whose text contains text_substring."""
    if not buttons:
        return None
    for row in buttons:
        for button in row:
            if button.text and text_substring.lower() in button.text.lower():
                return button
    return None


def format_buttons(buttons):
    """Flatten button grid into a readable list of option labels."""
    if not buttons:
        return "(no buttons found)"
    labels = []
    for row in buttons:
        for button in row:
            if button.text:
                labels.append(button.text)
    return ", ".join(labels)


def extract_spoiler_text(message):
    """Return the first spoiler-tagged text in a message, or None if there isn't one."""
    if not message.entities:
        return None
    pairs = message.get_entities_text(MessageEntitySpoiler)
    if not pairs:
        return None
    _entity, text = pairs[0]
    return text.strip()


class Account:
    """One userbot instance tied to a single Telegram session."""

    def __init__(self, name: str, api_id: int, api_hash: str, session_string: str,
                 target_bot: str, notify_chat: str, interval_minutes: int, button_text: str,
                 source_channel: str = None, profile_button_text: str = "Профиль",
                 promo_button_text: str = "Промокод"):
        self.name = name
        self.target_bot = target_bot
        self.notify_chat = notify_chat
        self.interval_minutes = interval_minutes
        self.button_text = button_text
        self.client = TelegramClient(StringSession(session_string), api_id, api_hash, catch_up=True)

        # Guards any conversation with the target bot, so the Кликер cycle
        # and the promo-code task never talk to it at the same moment.
        self.lock = asyncio.Lock()

        # Promo-code feature (only active if source_channel is set for this account)
        self.source_channel = source_channel
        self.profile_button_text = profile_button_text
        self.promo_button_text = promo_button_text
        self.promo_queue = asyncio.Queue()
        self.promo_pending = 0          # codes queued or being redeemed
        self.seen_promo_msgs = set()    # message ids already handled (dedupe edits)
        self._bg_tasks = set()
        self.profile_msg = None         # last Профиль page (has the Промокод button)

    def log(self, msg, *args):
        log.info(f"[{self.name}] {msg}", *args)

    async def notify_user(self, text: str):
        try:
            await self.client.send_message(self.notify_chat, text)
        except Exception as e:
            log.error("[%s] Failed to notify user: %s", self.name, e)

    def notify_bg(self, text: str):
        """Send a notification without blocking the flow that triggered it."""
        t = asyncio.create_task(self.notify_user(text))
        self._bg_tasks.add(t)
        t.add_done_callback(self._bg_tasks.discard)

    async def wait_for_buttons(self, conv, max_messages: int = 5, timeout: int = 30):
        """
        Some bots send several messages in a row (e.g. a photo/GIF first,
        then the actual menu/question with buttons a moment later).
        Keep reading messages from the conversation until one has buttons,
        or we run out of attempts / time.
        """
        last_msg = None
        for _ in range(max_messages):
            try:
                msg = await conv.get_response(timeout=timeout)
            except asyncio.TimeoutError:
                return last_msg
            last_msg = msg
            if msg.buttons:
                return msg
        return last_msg

    async def run_cycle(self):
        """One full pass: /start -> click button -> check for robot-check question."""
        # Promo has priority: wait (don't skip) until pending promo codes are done.
        while self.promo_pending > 0:
            await asyncio.sleep(0.05)
        self.log("Starting cycle against %s", self.target_bot)
        async with self.lock:
            try:
                async with self.client.conversation(self.target_bot, timeout=30) as conv:
                    await conv.send_message("/start")
                    menu_msg = await self.wait_for_buttons(conv)

                    if menu_msg is None:
                        self.log("No response at all after /start.")
                        await self.notify_user("⚠️ No response at all after /start.")
                        return

                    button = find_button(menu_msg.buttons, self.button_text)
                    if not button:
                        self.log("Could not find '%s' button. Menu text: %r",
                                 self.button_text, menu_msg.text)
                        await self.notify_user(
                            f"⚠️ Couldn't find the '{self.button_text}' button after /start.\n"
                            f"Last message text was:\n{menu_msg.text}"
                        )
                        return

                    self.log("Clicking '%s' button", self.button_text)
                    await button.click()

                    followup = await self.wait_for_buttons(conv, max_messages=3, timeout=30)
                    if followup is None:
                        self.log("No follow-up message this cycle. Nothing to do.")
                        return

                    followup_text = followup.text or ""
                    match = MATH_PATTERN.search(followup_text)

                    if match:
                        a, b = int(match.group(1)), int(match.group(2))
                        answer = a + b
                        self.log("Robot-check detected: %s + %s = ?", a, b)
                        answer_button = find_button(followup.buttons, str(answer))
                        if answer_button:
                            await answer_button.click()
                            self.log("Robot-check solved: clicked '%s'", answer)
                            await self.notify_user(
                                f"🤖 Robot-check appeared on {self.target_bot} — "
                                f"solved automatically: {a} + {b} = {answer} ✅"
                            )
                        else:
                            options = format_buttons(followup.buttons)
                            self.log("Robot-check answer button not found for '%s'", answer)
                            await self.notify_user(
                                "🤖 Robot-check appeared on {bot}!\n\n"
                                "Question: {a} + {b} = ?\n"
                                "Answer options: {options}\n\n"
                                "Couldn't find a matching button — go tap the correct "
                                "button in your chat with {bot}.".format(
                                    bot=self.target_bot, a=a, b=b, options=options
                                )
                            )
                    else:
                        self.log("Follow-up wasn't a robot-check. Ignoring: %r",
                                  followup_text[:200])

            except asyncio.TimeoutError:
                self.log("Conversation timed out waiting for a response from %s", self.target_bot)
            except Exception as e:
                log.exception("[%s] Error during cycle: %s", self.name, e)
                await self.notify_user(f"⚠️ Userbot error during cycle: {e}")

    async def redeem_promo(self, code: str):
        """/start -> Профиль -> Промокод -> send the code -> report the bot's reply.

        Retries this same account/code up to PROMO_MAX_RETRIES times if the bot
        glitches (PROMO_ERROR_TEXT) or doesn't respond at some step. Each attempt
        acquires self.lock fresh and releases it before any retry sleep/notify -
        this matters because asyncio.Lock is NOT reentrant, so re-acquiring it
        while still held (e.g. by recursing on redeem_promo from inside the
        locked block) would deadlock the account forever.
        """
        for attempt in range(PROMO_MAX_RETRIES):
            self.log("Redeeming promo code: %s (attempt %s)", code, attempt + 1)
            glitch_reason = await self._attempt_redeem_promo(code, fast=(attempt == 0))

            if glitch_reason is None:
                return  # done - success, or a non-retryable failure already notified

            if attempt + 1 < PROMO_MAX_RETRIES:
                self.log("Glitch/no-response %s for code '%s'.", glitch_reason, code)
                self.notify_bg(
                    f"⚠️ Bot glitched {glitch_reason} for code '{code}'. "
                    f"Retrying this account only (attempt {attempt + 2}/{PROMO_MAX_RETRIES})..."
                )
                await asyncio.sleep(PROMO_RETRY_DELAY_SECONDS)
            else:
                self.log("Giving up on code '%s' after %s attempts.", code, PROMO_MAX_RETRIES)
                self.notify_bg(
                    f"❌ Promo task for code '{code}' failed {PROMO_MAX_RETRIES} times in a "
                    f"row ({glitch_reason}). Giving up on this account for this code."
                )

    async def _find_profile_msg(self):
        """Look through recent chat history for a Профиль page that has the Промокод button."""
        try:
            async for m in self.client.iter_messages(self.target_bot, limit=50):
                if m.buttons and find_button(m.buttons, self.promo_button_text):
                    self.profile_msg = m
                    return m
        except Exception as e:
            self.log("Couldn't search chat history for the profile page: %s", e)
        return None

    async def _try_fast_promo_click(self, conv):
        """FAST PATH: if a Профиль page is already in the chat, click Промокод on it
        directly and wait for the bot's prompt (skips /start and Профиль).

        Returns the bot's prompt message, or None if anything is off (no old page,
        button missing/expired, no prompt in time, glitch text). On None the caller
        falls back to the full flow, so no step is ever skipped for real.
        """
        msg = self.profile_msg or await self._find_profile_msg()
        if not msg:
            return None
        btn = find_button(msg.buttons, self.promo_button_text)
        if not btn:
            return None
        try:
            await btn.click()
        except Exception as e:
            self.log("Fast path click failed (%s: %s). Falling back to full flow.",
                     type(e).__name__, e)
            self.profile_msg = None
            return None
        try:
            prompt_msg = await conv.get_response(timeout=PROMO_STEP_TIMEOUT)
        except asyncio.TimeoutError:
            self.log("Fast path: no prompt after click. Falling back to full flow.")
            self.profile_msg = None
            return None
        if prompt_msg.text and PROMO_ERROR_TEXT in prompt_msg.text:
            self.profile_msg = None
            return None
        self.log("Fast path worked: reached the code prompt without /start + Профиль.")
        return prompt_msg

    async def _attempt_redeem_promo(self, code: str, fast: bool = False):
        """Runs ONE attempt of the promo flow, holding self.lock only for the
        duration of this attempt (released automatically when this returns).

        With fast=True it first tries the fast path (click Промокод on an existing
        Профиль page); if that doesn't reach the code prompt, it runs the full flow
        right away in the same attempt.

        Returns:
          - None if this attempt is finished and should NOT be retried
            (success, or a non-transient failure like a missing button that
            was already notified to the user).
          - a short string describing what glitched/timed out, if the caller
            should retry.
        """
        async with self.lock:
            try:
                async with self.client.conversation(self.target_bot, timeout=PROMO_STEP_TIMEOUT) as conv:
                    prompt_msg = None
                    if fast:
                        prompt_msg = await self._try_fast_promo_click(conv)

                    if prompt_msg is None:
                        await conv.send_message("/start")
                        menu_msg = await self.wait_for_buttons(conv, timeout=PROMO_STEP_TIMEOUT)
                        if menu_msg is None:
                            return "(no response after /start)"

                        if menu_msg.text and PROMO_ERROR_TEXT in menu_msg.text:
                            return "(glitch text after /start)"

                        profile_btn = find_button(menu_msg.buttons, self.profile_button_text)
                        if not profile_btn:
                            self.notify_bg(
                                f"⚠️ Promo task: couldn't find '{self.profile_button_text}' "
                                f"button. Menu text:\n{menu_msg.text}"
                            )
                            return None
                        await profile_btn.click()

                        profile_msg = await self.wait_for_buttons(conv, timeout=PROMO_STEP_TIMEOUT)
                        if profile_msg is None:
                            return f"(no response after clicking '{self.profile_button_text}')"

                        if profile_msg.text and PROMO_ERROR_TEXT in profile_msg.text:
                            return f"(glitch text after clicking '{self.profile_button_text}')"

                        promo_btn = find_button(profile_msg.buttons, self.promo_button_text)
                        if not promo_btn:
                            self.notify_bg(
                                f"⚠️ Promo task: couldn't find '{self.promo_button_text}' "
                                f"button. Menu text:\n{profile_msg.text}"
                            )
                            return None
                        self.profile_msg = profile_msg  # remember for the fast path
                        await promo_btn.click()

                        # Bot should now prompt "send your code" - wait for that before
                        # sending anything. If nothing comes back in time, we can't be
                        # sure the click actually registered, so we retry the whole
                        # task instead of blindly sending the code into the wrong state.
                        try:
                            prompt_msg = await conv.get_response(timeout=PROMO_STEP_TIMEOUT)
                            if prompt_msg.text and PROMO_ERROR_TEXT in prompt_msg.text:
                                return f"(glitch text after clicking '{self.promo_button_text}')"
                        except asyncio.TimeoutError:
                            return f"(no response after clicking '{self.promo_button_text}')"

                    await conv.send_message(code)

                    try:
                        result_msg = await conv.get_response(timeout=PROMO_STEP_TIMEOUT)
                        result_text = result_msg.text or "(no text in reply)"
                    except asyncio.TimeoutError:
                        return "(no response after submitting the code)"

                    self.log("Promo code '%s' submitted. Reply: %r", code, result_text[:200])

                    if PROMO_ERROR_TEXT in result_text:
                        return "(glitch text after submitting the code)"

                    self.notify_bg(
                        f"🎟 Promo code '{code}' submitted on {self.target_bot}.\n"
                        f"Bot replied:\n{result_text}"
                    )
                    return None

            except asyncio.TimeoutError:
                return "(conversation timed out)"
            except Exception as e:
                log.exception("[%s] Error redeeming promo code '%s': %s", self.name, code, e)
                self.notify_bg(f"⚠️ Error redeeming promo code '{code}': {e}")
                return None

    async def promo_worker(self):
        """Pulls detected codes off the queue one at a time and redeems them."""
        while True:
            code = await self.promo_queue.get()
            if PROMO_STAGGER_SECONDS:
                idx = int(self.name.replace("account", "") or 1) - 1
                await asyncio.sleep(PROMO_STAGGER_SECONDS * idx)
            try:
                await self.redeem_promo(code)
            finally:
                self.promo_pending -= 1

    def register_source_channel_listener(self):
        """Watch the source channel for new/edited posts and queue any spoiler-hidden code."""
        async def _process(event, kind):
            msg_id = event.message.id
            if msg_id in self.seen_promo_msgs:
                return
            self.log("Source channel post %s (id=%s). Checking for spoiler...", kind, msg_id)
            code = extract_spoiler_text(event.message)
            if not code:
                self.log("Source channel post had no spoiler text. Ignoring.")
                return
            self.seen_promo_msgs.add(msg_id)
            self.log("Detected spoiler code in source channel: %s", code)
            # Queue FIRST so redemption starts immediately; notify in background.
            self.promo_pending += 1
            await self.promo_queue.put(code)
            self.notify_bg(
                f"📢 New post detected with promo code: {code}\n"
                f"Queuing redemption (will run before the next Кликер cycle)..."
            )

        @self.client.on(events.NewMessage(chats=self.source_channel))
        async def _new(event):
            await _process(event, "received")

        @self.client.on(events.MessageEdited(chats=self.source_channel))
        async def _edited(event):
            await _process(event, "edited")

    async def run_forever(self):
        await self.client.start()
        me = await self.client.get_me()
        self.log("Logged in as %s (id=%s)", me.username or me.first_name, me.id)

        startup_msg = (
            f"✅ Userbot started. Will click '{self.button_text}' on {self.target_bot} "
            f"every {self.interval_minutes} minutes."
        )
        if self.source_channel:
            self.register_source_channel_listener()
            asyncio.create_task(self.promo_worker())
            await self._find_profile_msg()  # pre-load the Профиль page for the fast path
            startup_msg += (
                f"\nAlso watching {self.source_channel} for spoiler promo codes "
                f"(will pause Кликер briefly to redeem them when found)."
            )
        await self.notify_user(startup_msg)

        while True:
            await self.run_cycle()
            self.log("Sleeping for %s minutes", self.interval_minutes)
            await asyncio.sleep(self.interval_minutes * 60)


def load_accounts() -> list:
    """
    Loads one or more accounts from environment variables.

    Shared defaults (used unless overridden per-account):
      API_ID, API_HASH, TARGET_BOT, NOTIFY_CHAT, INTERVAL_MINUTES, BUTTON_TEXT

    Per-account session strings, numbered starting at 1:
      SESSION_STRING_1, SESSION_STRING_2, SESSION_STRING_3, ...

    Any per-account override, e.g. for account 2:
      API_ID_2, API_HASH_2, TARGET_BOT_2, NOTIFY_CHAT_2, INTERVAL_MINUTES_2, BUTTON_TEXT_2

    Backward compatible: if SESSION_STRING (no suffix) is set instead, it's
    treated as a single account "1".

    Promo-code feature (single designated account only):
      SOURCE_CHANNEL       - channel username/id to watch for spoiler codes
      PROMO_ACCOUNT        - which account name watches it, default "account1"
      PROFILE_BUTTON_TEXT  - default "Профиль"
      PROMO_CODE_BUTTON_TEXT - default "Промокод"
    """
    accounts = []

    # Backward-compatible single-account fallback
    if "SESSION_STRING" in os.environ and "SESSION_STRING_1" not in os.environ:
        os.environ["SESSION_STRING_1"] = os.environ["SESSION_STRING"]

    def _clean(v):
        if v is None:
            return v
        return v.strip().strip("\"'").strip()

    for i in range(1, 51):
        session_key = f"SESSION_STRING_{i}"
        if session_key not in os.environ:
            continue

        def get(key_base, default=None, required=False, i=i):
            val = os.environ.get(f"{key_base}_{i}", os.environ.get(key_base, default))
            if required and val is None:
                raise RuntimeError(f"Missing required env var: {key_base}_{i} or {key_base}")
            return _clean(val)

        try:
            accounts.append(Account(
                name=f"account{i}",
                api_id=int(get("API_ID", required=True)),
                api_hash=get("API_HASH", required=True),
                session_string=_clean(os.environ[session_key]),
                target_bot=get("TARGET_BOT", required=True),
                notify_chat=get("NOTIFY_CHAT", default="me"),
                interval_minutes=int(get("INTERVAL_MINUTES", default="10")),
                button_text=get("BUTTON_TEXT", default="Кликер"),
            ))
        except Exception as e:
            print(f"[SKIPPED] {session_key} is invalid ({type(e).__name__}: {e}). Fix or delete this variable.", flush=True)

    if not accounts:
        raise RuntimeError(
            "No accounts configured. Set SESSION_STRING_1 (and API_ID_1/"
            "API_HASH_1 or shared API_ID/API_HASH, plus TARGET_BOT_1 or "
            "shared TARGET_BOT)."
        )

    # Wire up the promo-code feature on ALL loaded accounts
    source_channel = os.environ.get("SOURCE_CHANNEL")

    if source_channel:
        for target in accounts:
            target.source_channel = source_channel
            target.profile_button_text = os.environ.get("PROFILE_BUTTON_TEXT", "Профиль")
            target.promo_button_text = os.environ.get("PROMO_CODE_BUTTON_TEXT", "Промокод")
            log.info("Promo-code watching enabled on %s for channel %s",
                     target.name, source_channel)

    return accounts


async def main():
    accounts = load_accounts()
    log.info("Loaded %d account(s): %s", len(accounts), ", ".join(a.name for a in accounts))

    async def run_isolated(account: Account):
        """Wrap each account's loop so one crashing doesn't kill the others."""
        try:
            async with account.client:
                await account.run_forever()
        except Exception as e:
            log.exception("[%s] Fatal error, this account stopped: %s", account.name, e)

    batch_size = int(os.environ.get("BATCH_SIZE", "5"))
    batch_delay = float(os.environ.get("BATCH_DELAY_SECONDS", "5"))

    tasks = []
    for start in range(0, len(accounts), batch_size):
        batch = accounts[start:start + batch_size]
        log.info("Starting batch: %s", ", ".join(a.name for a in batch))
        for account in batch:
            tasks.append(asyncio.create_task(run_isolated(account)))
        # Wait before starting the next batch, unless this was the last one
        if start + batch_size < len(accounts):
            await asyncio.sleep(batch_delay)

    await asyncio.gather(*tasks)


if __name__ == "__main__":
    from keepalive import keep_alive
    keep_alive()
    asyncio.run(main())
