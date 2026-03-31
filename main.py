#!/usr/bin/env python3
"""
RossWatcher — Automated Daily Ross Cameron YouTube Recap Monitor
================================================================
Runs as a standalone Zeabur service alongside GreenClaw.

Every weekday at 5 PM ET it checks Ross's YouTube channel RSS feed
(free, no credits) for new full-length recap videos published today.
When found, it fetches the transcript via TranscriptAPI, sends it to
Claude for structured analysis, then pushes the insight report to
Telegram and to the Google Sheets "Ross Insights" tab.

Required environment variables (set in Zeabur):
  TELEGRAM_BOT_TOKEN      — same as GreenClaw
  TELEGRAM_CHAT_ID        — same as GreenClaw
  TRANSCRIPT_API_KEY      — from transcriptapi.com dashboard
  ANTHROPIC_API_KEY       — from console.anthropic.com
  SHEETS_WEBHOOK_URL      — same Google Apps Script URL as GreenClaw
  CHECK_TIME_ET           — optional, default "17:00" (5 PM ET)
  PORT                    — injected by Zeabur (health check port)
"""

import os
import sys
import json
import time
import signal
import logging
import threading
import urllib.request
import urllib.error
import ssl
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from xml.etree import ElementTree

# ── Config ────────────────────────────────────────────────────────────────────
TG_TOKEN        = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT         = os.environ.get("TELEGRAM_CHAT_ID", "")
TRANSCRIPT_KEY  = os.environ.get("TRANSCRIPT_API_KEY", "")
ANTHROPIC_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
SHEETS_URL      = os.environ.get("SHEETS_WEBHOOK_URL", "")
CHECK_TIME_ET   = os.environ.get("CHECK_TIME_ET", "17:30")  # 5:30 PM — gives captions ~90min to generate
PORT            = int(os.environ.get("PORT") or os.environ.get("ROSSWATCHER_PORT", "8080"))  # Zeabur injects $PORT at runtime

# Ross's confirmed channel details
CHANNEL_ID      = "UCBayuhgYpKNbhJxfExYkPfA"
CHANNEL_HANDLE  = "@DaytradeWarrior"
RSS_URL         = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"

# Shorts / non-recap keywords to filter out (case-insensitive)
SKIP_KEYWORDS   = [
    "shorts", "look inside", "travel van", "pt.", "pt ",
    "why traders", "secret to", "beginner traders",
    "puerto rico", "moving to"
]

# Minimum video duration proxy — full recaps tend to have longer titles
# and we filter by checking the link type (shorts URLs contain /shorts/)
ET_TZ = timezone(timedelta(hours=-4))  # ET (EDT during DST)

# State file — tracks which video IDs have already been processed
STATE_FILE = "rosswatcher_state.json"

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s ET | %(levelname)s | %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("rosswatcher.log"),
    ]
)
log = logging.getLogger(__name__)

# ── SSL context ───────────────────────────────────────────────────────────────
_ssl = ssl.create_default_context()

# ── State management ──────────────────────────────────────────────────────────
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"processed_ids": [], "last_check": None}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ── Telegram ──────────────────────────────────────────────────────────────────
def tg_send(text):
    """Send a Telegram message, splitting if over 4000 chars.
    Tries Markdown parse mode first; falls back to plain text on 400 errors.
    """
    if not TG_TOKEN or not TG_CHAT:
        log.warning("Telegram not configured \u2014 skipping send")
        return
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
    for chunk in chunks:
        for parse_mode in ("Markdown", None):
            try:
                msg = {"chat_id": TG_CHAT, "text": chunk}
                if parse_mode:
                    msg["parse_mode"] = parse_mode
                payload = json.dumps(msg).encode()
                req = urllib.request.Request(
                    f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST"
                )
                urllib.request.urlopen(req, timeout=15, context=_ssl)
                break  # success
            except urllib.error.HTTPError as e:
                if e.code == 400 and parse_mode:
                    log.warning("Telegram Markdown parse failed, retrying as plain text")
                    continue
                log.error(f"Telegram send failed: {e}")
                break
            except Exception as e:
                log.error(f"Telegram send failed: {e}")
                break
        time.sleep(0.3)
def sheets_push(payload):
    """Push data to Google Sheets webhook — non-fatal on failure."""
    if not SHEETS_URL:
        return
    try:
        payload["bot_version"] = "RossWatcher v1"
        payload["timestamp"]   = datetime.now(ET_TZ).isoformat()
        body = json.dumps(payload, default=str).encode()
        req  = urllib.request.Request(
            SHEETS_URL, data=body,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        urllib.request.urlopen(req, timeout=15, context=_ssl)
        log.info("Sheets push OK")
    except Exception as e:
        log.warning(f"Sheets push failed (non-fatal): {e}")

# ── RSS feed ──────────────────────────────────────────────────────────────────
def fetch_rss_videos():
    """
    Fetch Ross's YouTube RSS feed and return list of video dicts.
    Free — no API credits consumed.
    """
    try:
        req = urllib.request.Request(RSS_URL,
            headers={"User-Agent": "RossWatcher/1.0"})
        with urllib.request.urlopen(req, timeout=15, context=_ssl) as resp:
            xml_data = resp.read()

        root = ElementTree.fromstring(xml_data)
        ns = {
            "atom":    "http://www.w3.org/2005/Atom",
            "media":   "http://search.yahoo.com/mrss/",
            "yt":      "http://www.youtube.com/xml/schemas/2015",
        }

        videos = []
        for entry in root.findall("atom:entry", ns):
            video_id = entry.findtext("yt:videoId", namespaces=ns) or ""
            title    = entry.findtext("atom:title", namespaces=ns) or ""
            link_el  = entry.find("atom:link", ns)
            link     = link_el.get("href", "") if link_el is not None else ""
            published_str = entry.findtext("atom:published", namespaces=ns) or ""

            # Parse publish time
            try:
                published = datetime.fromisoformat(
                    published_str.replace("Z", "+00:00"))
            except Exception:
                published = datetime.now(timezone.utc)

            videos.append({
                "video_id":  video_id,
                "title":     title,
                "link":      link,
                "published": published,
            })

        log.info(f"RSS: fetched {len(videos)} videos")
        return videos

    except Exception as e:
        log.error(f"RSS fetch failed: {e}")
        return []

# ── Video filtering ───────────────────────────────────────────────────────────
def is_recap_video(video):
    """
    Return True if this looks like a full trading day recap.
    Filters out: YouTube Shorts, van tour series, non-trading content.
    """
    title = video["title"].lower()
    link  = video["link"].lower()

    # Shorts have /shorts/ in the URL
    if "/shorts/" in link:
        return False

    # Skip known non-recap content
    for kw in SKIP_KEYWORDS:
        if kw in title:
            return False

    # Recap titles typically mention trading activity, stocks, P&L, or days
    recap_signals = [
        "day trading", "stock", "trade", "green day", "red day",
        "$", "%", "morning", "recap", "market", "broke", "short squeeze",
        "halted", "gainer", "profit", "loss", "week"
    ]
    if any(s in title for s in recap_signals):
        return True

    # Default: if it's a long-form video (not shorts) by Ross, probably a recap
    return True

def published_today(video):
    """Check if video was published today in ET timezone."""
    now_et   = datetime.now(ET_TZ)
    pub_et   = video["published"].astimezone(ET_TZ)
    return pub_et.date() == now_et.date()

# ── Transcript fetch ──────────────────────────────────────────────────────────
def fetch_transcript(video_id, max_retries=3, retry_delay=600):
    """
    Fetch transcript via TranscriptAPI.com REST endpoint.
    Costs 1 credit per successful fetch.
    Retries up to max_retries times on 403 errors (video too new —
    YouTube blocks transcript access until captions are processed,
    typically within 10-30 min of upload).
    """
    if not TRANSCRIPT_KEY:
        log.error("TRANSCRIPT_API_KEY not set")
        return None

    url = (f"https://transcriptapi.com/api/v2/youtube/transcript"
           f"?video_url={video_id}&format=text&include_timestamp=false")

    for attempt in range(1, max_retries + 1):
        try:
            req = urllib.request.Request(url, headers={
                "Authorization": f"Bearer {TRANSCRIPT_KEY}"
            })
            with urllib.request.urlopen(req, timeout=60, context=_ssl) as resp:
                data = json.loads(resp.read())

            # API returns {"content": "# Metadata\n...\n# Transcript\n..."}
            content = data.get("content", "")
            if "# Transcript" in content:
                transcript = content.split("# Transcript", 1)[1].strip()
            else:
                transcript = content

            log.info(f"Transcript fetched: {len(transcript)} chars")
            return transcript

        except urllib.error.HTTPError as e:
            if e.code == 403 and attempt < max_retries:
                wait_min = retry_delay // 60
                log.warning(
                    f"Transcript 403 for {video_id} (video too new) — "
                    f"attempt {attempt}/{max_retries}, retrying in {wait_min} min")
                tg_send(
                    f"⏳ *RossWatcher* — transcript not ready yet for today's video\n"
                    f"YouTube is still processing captions. "
                    f"Retrying in {wait_min} min (attempt {attempt}/{max_retries})")
                time.sleep(retry_delay)
            else:
                log.error(f"Transcript fetch failed for {video_id}: {e}")
                return None
        except Exception as e:
            log.error(f"Transcript fetch failed for {video_id}: {e}")
            return None

    log.error(f"Transcript fetch gave up after {max_retries} attempts for {video_id}")
    return None

# ── Claude analysis ───────────────────────────────────────────────────────────
ANALYSIS_PROMPT = """You are analysing a Ross Cameron (Warrior Trading) daily trading recap video transcript.
Extract the following information and return it as a structured report.

TRANSCRIPT:
{transcript}

Provide your analysis in this exact format:

## Ross Cameron Daily Recap — {date}

**Result:** [Green/Red/Flat] | P&L: [amount if mentioned]

**Market Condition:** [Hot/Warm/Cold/Mixed — 1 sentence assessment]

**Stocks Traded:**
[For each stock: SYMBOL — setup type, entry reasoning, outcome]

**Setup Types Used Today:**
[List each pattern: bull flag, curl/second-wind, halt play, VWAP reclaim, etc.]

**Key Rules Mentioned:**
[Bullet points of any specific criteria, filters, or rules Ross stated]

**What's Working Right Now:**
[What setups/conditions Ross says are producing wins in current market]

**What's NOT Working:**
[Setups or conditions Ross explicitly avoided or warned against]

**GreenClaw Relevance:**
[Would GreenClaw's scanner have found today's stocks? Were the stocks sub-1M float, 10%+ gap, high RVOL? Any parameter adjustments suggested?]

**Market Condition for Monday:**
[Ross's outlook or any forward-looking comments]

**Quotable Insight:**
[One direct quote from Ross that is most useful for a algorithmic trader to remember]

Keep the entire analysis under 1500 words. Be specific and actionable."""

def analyse_with_claude(transcript, video_title, date_str):
    """Send transcript to Claude API for structured analysis."""
    if not ANTHROPIC_KEY:
        log.error("ANTHROPIC_API_KEY not set")
        return None

    prompt = ANALYSIS_PROMPT.format(
        transcript=transcript[:12000],  # ~3000 tokens, fits well in context
        date=date_str
    )

    try:
        payload = json.dumps({
            "model":      "claude-3-5-sonnet-20241022",
            "max_tokens": 1500,
            "messages": [{"role": "user", "content": prompt}]
        }).encode()

        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=payload,
            headers={
                "Content-Type":      "application/json",
                "x-api-key":         ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
            },
            method="POST"
        )

        with urllib.request.urlopen(req, timeout=120, context=_ssl) as resp:
            data = json.loads(resp.read())

        analysis = data["content"][0]["text"]
        log.info(f"Claude analysis: {len(analysis)} chars")
        return analysis

    except Exception as e:
        log.error(f"Claude analysis failed: {e}")
        return None

# ── Core watcher logic ────────────────────────────────────────────────────────
def run_check(force=False):
    """
    Main check — called once at CHECK_TIME_ET each weekday.
    1. Fetch RSS feed (free)
    2. Find today's full recap videos not yet processed
    3. Fetch transcript (1 credit each)
    4. Analyse with Claude
    5. Push to Telegram + Sheets
    """
    state = load_state()
    now_et = datetime.now(ET_TZ)
    date_str = now_et.strftime("%A %b %d, %Y")

    log.info(f"=== RossWatcher check: {date_str} ===")
    tg_send(f"🔍 *RossWatcher* — Checking for Ross's recap ({date_str})...")

    videos = fetch_rss_videos()
    if not videos:
        tg_send("⚠️ RossWatcher: Could not fetch YouTube RSS feed.")
        return

    # Find recap videos — force mode: any recent recap, ignore today/processed filters
    if force:
        new_recaps = [
            v for v in videos
            if is_recap_video(v)
        ]
        # In force mode, take the most recent one we haven't processed
        # Try unprocessed first, fall back to most recent overall
        unprocessed = [v for v in new_recaps if v["video_id"] not in state["processed_ids"]]
        new_recaps  = unprocessed if unprocessed else new_recaps[:1]
        log.info(f"Force mode: {len(new_recaps)} recap(s) found (ignoring date/processed filters)")
        if new_recaps:
            tg_send(f"🔍 Force mode — analysing: _{new_recaps[0]['title']}_")
    else:
        new_recaps = [
            v for v in videos
            if published_today(v)
            and is_recap_video(v)
            and v["video_id"] not in state["processed_ids"]
        ]
        log.info(f"Found {len(new_recaps)} new recap(s) today")

    if not new_recaps:
        if force:
            tg_send("📭 *RossWatcher* — No recap videos found in RSS feed at all.")
        else:
            tg_send(
                f"📭 *RossWatcher* — No new recap video found yet for {date_str}.\n"
                f"_Ross may not have posted today. Try /rw check on a weekday after 3 PM ET._"
            )
        return

    for video in new_recaps:
        vid_id = video["video_id"]
        title  = video["title"]
        link   = video["link"]

        log.info(f"Processing: {title} ({vid_id})")
        tg_send(f"📹 *RossWatcher* — Found: _{title}_\nFetching transcript...")

        # 1. Fetch transcript
        transcript = fetch_transcript(vid_id)
        if not transcript:
            tg_send(f"⚠️ RossWatcher: Could not fetch transcript for _{title}_")
            continue

        # 2. Analyse with Claude
        tg_send("🤖 Analysing with Claude...")
        analysis = analyse_with_claude(transcript, title, date_str)
        if not analysis:
            tg_send(f"⚠️ RossWatcher: Claude analysis failed for _{title}_")
            continue

        # 3. Send to Telegram
        header = (
            f"📊 *Ross Cameron Recap Analysis*\n"
            f"_{title}_\n"
            f"🔗 {link}\n"
            f"{'─' * 35}\n\n"
        )
        tg_send(header + analysis)

        # 4. Push to Google Sheets
        sheets_push({
            "type":       "ross_insight",
            "date":       now_et.strftime("%Y-%m-%d"),
            "day_name":   now_et.strftime("%A"),
            "video_id":   vid_id,
            "video_title":title,
            "video_url":  link,
            "analysis":   analysis,
            "transcript_length": len(transcript),
        })

        # 5. Mark as processed
        state["processed_ids"].append(vid_id)
        # Keep only last 60 IDs to prevent state file growing indefinitely
        state["processed_ids"] = state["processed_ids"][-60:]
        state["last_check"] = now_et.isoformat()
        save_state(state)

        log.info(f"Done: {title}")
        time.sleep(2)  # brief pause between multiple videos

    tg_send("✅ *RossWatcher* — Check complete.")

# ── Scheduler ─────────────────────────────────────────────────────────────────
def parse_check_time():
    """Parse CHECK_TIME_ET into (hour, minute) tuple."""
    try:
        hh, mm = CHECK_TIME_ET.split(":")
        return int(hh), int(mm)
    except Exception:
        return 17, 0  # default 5 PM ET

def scheduler_loop():
    """
    Main scheduler loop. Runs indefinitely, triggering run_check()
    at the configured time on weekdays.
    Also responds to /check Telegram command for manual triggers.
    """
    check_hour, check_minute = parse_check_time()
    last_triggered_date = None

    log.info(f"Scheduler started — will check at {check_hour:02d}:{check_minute:02d} ET on weekdays")

    while True:
        now_et = datetime.now(ET_TZ)
        today  = now_et.date()
        is_weekday = now_et.weekday() < 5  # Mon–Fri

        # Trigger at the configured time on weekdays, once per day
        if (is_weekday
                and now_et.hour == check_hour
                and now_et.minute == check_minute
                and last_triggered_date != today):
            last_triggered_date = today
            # Run in background thread so Telegram polling stays responsive
            def _run():
                try:
                    run_check()
                except Exception as e:
                    log.error(f"run_check error: {e}")
                    tg_send(f"⚠️ RossWatcher error: {e}")
            threading.Thread(target=_run, daemon=True).start()

        # Poll Telegram for /check command (manual trigger)
        try:
            poll_telegram_commands()
        except Exception as e:
            log.debug(f"Telegram poll error: {e}")

        time.sleep(30)  # check every 30 seconds

# ── Telegram command polling ──────────────────────────────────────────────────
_tg_offset = 0

def poll_telegram_commands():
    """Poll Telegram for manual /check or /status commands."""
    global _tg_offset

    if not TG_TOKEN:
        return

    url = (f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates"
           f"?offset={_tg_offset}&timeout=5")
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10, context=_ssl) as resp:
            data = json.loads(resp.read())
    except Exception:
        return

    for update in data.get("result", []):
        _tg_offset = update["update_id"] + 1
        msg  = update.get("message", {})
        text = msg.get("text", "").strip().lower()
        cid  = str(msg.get("chat", {}).get("id", ""))

        if cid != str(TG_CHAT):
            continue  # ignore messages from other chats

        # RossWatcher uses /rw prefix to avoid conflicts with GreenClaw
        # Commands: /rw check | /rw status | /rw help
        if text in ("/rw check", "/rw check@zeaburgreenbot"):
            tg_send("🔄 *RossWatcher* — Manual check triggered (bypassing weekday/date filters)...")
            # Background thread keeps polling responsive during long API calls
            def _force_run():
                try:
                    run_check(force=True)
                except Exception as e:
                    tg_send(f"⚠️ Error: {e}")
            threading.Thread(target=_force_run, daemon=True).start()

        elif text in ("/rw status", "/rw status@zeaburgreenbot"):
            state   = load_state()
            now_et  = datetime.now(ET_TZ)
            is_wd   = now_et.weekday() < 5
            ch, cm  = parse_check_time()
            tg_send(
                f"👁 *RossWatcher Status*\n"
                f"Time (ET): {now_et.strftime('%H:%M')}\n"
                f"Daily check: {ch:02d}:{cm:02d} ET (weekdays)\n"
                f"Today is a: {'weekday ✅' if is_wd else 'weekend ⏸'}\n"
                f"Last check: {state.get('last_check', 'never')}\n"
                f"Videos processed: {len(state.get('processed_ids', []))}\n"
                f"Transcript API: {'✅' if TRANSCRIPT_KEY else '❌ not set'}\n"
                f"Anthropic API: {'✅' if ANTHROPIC_KEY else '❌ not set'}\n"
                f"Sheets webhook: {'✅' if SHEETS_URL else '❌ not set'}\n"
                f"\n_Commands: /rw check | /rw status | /rw help_"
            )

        elif text in ("/rw help", "/rw help@zeaburgreenbot"):
            tg_send(
                "👁 *RossWatcher Commands*\n"
                "_(prefix /rw to avoid conflicts with GreenClaw)_\n\n"
                "/rw check  — fetch & analyse today's recap now\n"
                "/rw status — show config, last check, counts\n"
                "/rw help   — this message"
            )

# ── Health check HTTP server ──────────────────────────────────────────────────
class HealthHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # silence access logs

    def do_GET(self):
        # Respond 200 on all paths — Zeabur probes / for health check
        body = json.dumps({
            "status":  "ok",
            "service": "RossWatcher",
            "time":    datetime.now(ET_TZ).isoformat(),
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

def start_health_server():
    """
    Bind health check port synchronously so Zeabur sees it immediately.
    Uses SO_REUSEADDR to avoid 'address already in use' on restart.
    """
    import socketserver
    class ReusableTCPServer(HTTPServer):
        allow_reuse_address = True
    try:
        server = ReusableTCPServer(("0.0.0.0", PORT), HealthHandler)
        log.info(f"Health server bound on port {PORT} (Zeabur ready)")
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
    except OSError as e:
        # Port already in use — try to continue anyway
        log.warning(f"Health server port {PORT} bind failed: {e} — continuing")

# ── Startup validation ────────────────────────────────────────────────────────
def validate_config():
    """Check required env vars and warn about missing ones."""
    required = {
        "TELEGRAM_BOT_TOKEN": TG_TOKEN,
        "TELEGRAM_CHAT_ID":   TG_CHAT,
        "TRANSCRIPT_API_KEY": TRANSCRIPT_KEY,
        "ANTHROPIC_API_KEY":  ANTHROPIC_KEY,
    }
    optional = {
        "SHEETS_WEBHOOK_URL": SHEETS_URL,
    }

    all_ok = True
    for name, val in required.items():
        if not val:
            log.error(f"Missing required env var: {name}")
            all_ok = False
        else:
            log.info(f"  {name}: ✅")

    for name, val in optional.items():
        status = "✅" if val else "⚠️  not set (Sheets push disabled)"
        log.info(f"  {name}: {status}")

    return all_ok

# ── Signal handling ───────────────────────────────────────────────────────────
def _shutdown(sig, _):
    log.info("Shutdown signal received")
    tg_send("🔴 RossWatcher stopped")
    sys.exit(0)

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    # ── Bind health check port IMMEDIATELY ───────────────────────────────────
    # Zeabur probes this port within seconds of container start.
    # Must bind before any other blocking calls (signal setup, API calls, etc.)
    start_health_server()
    time.sleep(0.5)  # give OS a moment to confirm port is listening

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT,  _shutdown)

    log.info("=" * 55)
    log.info("RossWatcher v1 — Ross Cameron YouTube Monitor")
    log.info("=" * 55)

    # Validate config
    if not validate_config():
        log.error("Config validation failed — check environment variables")
        # Don't exit — keep health server alive so Zeabur doesn't crash-loop
        # Just skip the scheduler and wait
        tg_send(
            "⚠️ *RossWatcher* failed to start — missing environment variables.\n"
            "Check Zeabur variables: TRANSCRIPT_API_KEY, ANTHROPIC_API_KEY"
        )
        while True:
            time.sleep(60)

    ch, cm = parse_check_time()
    tg_send(
        f"👁 *RossWatcher v1 Started*\n"
        f"Channel: {CHANNEL_HANDLE}\n"
        f"Daily check: {ch:02d}:{cm:02d} ET (weekdays)\n"
        f"Transcript API: {'✅' if TRANSCRIPT_KEY else '❌'}\n"
        f"Claude analysis: {'✅' if ANTHROPIC_KEY else '❌'}\n"
        f"Sheets push: {'✅' if SHEETS_URL else '⚠️ disabled'}\n"
        f"Commands: /rw check | /rw status | /rw help"
    )

    # Start main scheduler loop
    scheduler_loop()

if __name__ == "__main__":
    main()
