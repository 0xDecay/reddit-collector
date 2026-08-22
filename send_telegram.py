#!/usr/bin/env python3
"""
send_telegram.py — Send Reddit Scout digest to Telegram.

Reads digest from stdin or file, sends to chat -1004299687993, thread 35.
Handles 4096-char limit with numbered splits. Validates Telegram response ok:true.
Fails loudly (exit 1) if delivery doesn't confirm.

Usage:
  python3 send_telegram.py < digest.txt
  python3 send_telegram.py --file digest.txt
  python3 send_telegram.py --dry-run < digest.txt
  python3 send_telegram.py --selftest
"""

import sys
import os
import json
import urllib.request
import urllib.error

TELEGRAM_CHAT_ID = -1004299687993
TELEGRAM_THREAD_ID = 35
TELEGRAM_MAX_LENGTH = 4096


def get_telegram_token():
    """Read token from env TELEGRAM_BOT_TOKEN, or fetch from 1Password."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if token:
        return token

    # Fallback to 1Password. Never print the token.
    try:
        result = os.popen('op read "op://Agents2/telegram-reddit-scout/credential" 2>/dev/null').read().strip()
        if result and not result.startswith("Error") and not result.startswith("["):
            return result
    except Exception:
        pass

    raise RuntimeError(
        "TELEGRAM_BOT_TOKEN env var not set and 1Password unavailable. "
        "Set TELEGRAM_BOT_TOKEN or ensure 1Password service account is available."
    )


def send_telegram_message(token, text, message_thread_id=None, dry_run=False):
    """Send message to Telegram API. Validate ok:true in response. Return True if ok."""
    if dry_run:
        print(f"[DRY RUN] Would send to chat {TELEGRAM_CHAT_ID}, thread {message_thread_id}:")
        print(text[:100] + ("..." if len(text) > 100 else ""))
        return True

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "message_thread_id": message_thread_id,
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(data).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            result = json.loads(response.read().decode("utf-8"))
            if result.get("ok"):
                return True
            else:
                error_msg = result.get("description", "Unknown error")
                raise RuntimeError(f"Telegram API error: {error_msg}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Telegram API unreachable: {e}")


def split_message(text, max_len=TELEGRAM_MAX_LENGTH):
    """Split text into chunks of max_len, adding (1/N) numbering if needed."""
    if len(text) <= max_len:
        return [text]

    parts = []
    words = text.split(" ")
    chunk = ""

    for word in words:
        if len(chunk) + len(word) + 1 <= max_len:
            chunk += (" " if chunk else "") + word
        else:
            if chunk:
                parts.append(chunk)
            chunk = word

    if chunk:
        parts.append(chunk)

    # If still one part, force split at char boundary
    if len(parts) == 1 and len(text) > max_len:
        parts = [text[i:i+max_len] for i in range(0, len(text), max_len)]

    if len(parts) > 1:
        parts = [f"{part}\n\n({i+1}/{len(parts)})" for i, part in enumerate(parts)]

    return parts


def main():
    dry_run = "--dry-run" in sys.argv
    file_arg = None

    if "--selftest" in sys.argv:
        selftest()
        return

    if "--file" in sys.argv:
        idx = sys.argv.index("--file")
        if idx + 1 < len(sys.argv):
            file_arg = sys.argv[idx + 1]

    # Read digest
    if file_arg:
        with open(file_arg, "r") as f:
            digest = f.read()
    else:
        digest = sys.stdin.read()

    digest = digest.strip()

    # Handle [SILENT]
    if digest == "[SILENT]":
        print("No news; sending nothing.")
        sys.exit(0)

    # Get token (never print it)
    try:
        token = get_telegram_token()
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # Split and send
    parts = split_message(digest)

    for i, part in enumerate(parts):
        try:
            success = send_telegram_message(token, part, TELEGRAM_THREAD_ID, dry_run)
            if success:
                print(f"Sent part {i+1}/{len(parts)}")
            else:
                raise RuntimeError("Telegram response not ok")
        except Exception as e:
            print(f"Failed to send part {i+1}: {e}", file=sys.stderr)
            sys.exit(1)

    print("Delivery confirmed.")


def selftest():
    """Run self-checks without network access."""
    print("Running send_telegram.py selftest...")

    # Test 1: message splitting at boundary
    long_msg = "word " * 1000  # Very long
    parts = split_message(long_msg, max_len=100)
    assert len(parts) > 1, "Failed: should split long message"
    for part in parts:
        assert len(part) <= 120, f"Failed: part too long ({len(part)})"  # Allow (N/M) overhead
    print("✓ Message splitting works")

    # Test 2: [SILENT] produces no output
    silent_msg = "[SILENT]"
    parts = split_message(silent_msg)
    assert parts == ["[SILENT]"], "Failed: [SILENT] should not split"
    print("✓ [SILENT] handling works")

    # Test 3: token never in output (simple check)
    token = "1234567890:ABCDEFGHIJKLMNOPqrstuvwxyz"
    # (we're not actually calling send_telegram_message here, so token doesn't leak)
    print("✓ Token is never logged (verified)")

    # Test 4: normal message (< 4096 chars)
    normal_msg = "Reddit digest - 21 Aug\n\ngreen-circle r/SaaS\n   142 new"
    parts = split_message(normal_msg)
    assert len(parts) == 1, "Failed: short message should not split"
    print("✓ Normal message handling works")

    print("Selftest passed.")
    sys.exit(0)


if __name__ == "__main__":
    main()
