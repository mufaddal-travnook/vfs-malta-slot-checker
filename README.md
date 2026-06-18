# VFS Malta Slot Checker

A small, single-purpose tool extracted from the larger VFS bot. It logs in to the
**UAE → Malta** VFS Global portal, reaches Step 1 (Appointment Details), reads the
*"Earliest available slot"* banner for several centre/category/sub-category
combinations, and sends a combined report to **Telegram**. It never books anything.

It keeps the original schema-driven architecture (a JSON route file describes the
combinations) but ships only the slot-check slice.

```
src/
  main.py                  # CLI entry (defaults to AE -> MT)
  utils/
    config_reader.py       # reads config/*.ini (+ VFS_BOT_CONFIG_PATH override)
    route_schema.py        # loads config/routes/<SRC>-<DST>.json
    telegram.py            # sends the report via the Telegram Bot API
  vfs_bot/
    vfs_bot.py             # the slot-check flow (login -> Step 1 -> read slots)
    vfs_bot_factory.py     # builds the schema-driven bot
config/
  config.ini               # [browser] [vfs-credential] [telegram]
  vfs_urls.ini             # AE-MT login URL
  routes/AE-MT.json        # the combinations to check
run_loop.py                # optional interval scheduler
```

## Setup

Python 3.9+.

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

## Configure

Edit [config/config.ini](config/config.ini):

- `[vfs-credential]` — a VFS account registered on the UAE→Malta portal.
- `[telegram]` — `bot_token` (from **@BotFather**) and `chat_id` (from **@userinfobot**).
  Leave blank to only log the report.
- `[browser] headless` — `true` for servers, `false` locally to watch it.

Edit the combinations to check in [config/routes/AE-MT.json](config/routes/AE-MT.json)
(`slot_check.combinations`). Each entry's `centre` / `category` / `sub_category`
must match the dropdown option text (case-insensitive substring).

### Secrets in production

Don't commit real credentials. Point `VFS_BOT_CONFIG_PATH` at a private file that
overrides only the sensitive keys:

```bash
export VFS_BOT_CONFIG_PATH=/etc/vfs/config.local.ini
```

It's read last, so its values win over the committed `config.ini`.

### Secret-leak guard (enable after cloning)

A pre-commit hook in [`scripts/pre-commit`](scripts/pre-commit) blocks any commit
whose staged changes contain a real-looking credential, so a secret can't slip
into `config/config.ini` by accident. Enable it once per clone:

```bash
git config core.hooksPath scripts
```

Real credentials belong in `config/config.local.ini` (gitignored) — never in the
tracked `config/config.ini`.

## Run

One-off:

```bash
python -m src.main            # AE -> MT
python -m src.main -sc AE -dc MT
```

On an interval (built-in scheduler — no cron needed):

```bash
python run_loop.py --interval 30      # every 30 minutes
python run_loop.py --once             # single cycle
```

Or via **cron** (Linux), every 30 minutes:

```cron
*/30 * * * * cd /opt/vfs-malta-slot-checker && /usr/bin/python3 -m src.main >> app.log 2>&1
```

Output goes to stdout and `app.log`. The Telegram report is sent at the end of
each run.

## Production: self-healing hourly runs on EC2

VFS sits behind Cloudflare, which blocks headless/automation browsers (a plain
headless run gets a 403 or hangs on the spinner). So in production the bot drives
a **real, headed Chrome** over CDP, running under a **virtual display (Xvfb)** so
"headed" works on a server with no monitor.

The **supervisor** ([`src/supervisor.py`](src/supervisor.py)) makes each hourly run
self-healing:

- Launches a fresh Chrome that it **owns**, runs the full flow, and **kills Chrome
  on the way out — success or failure** (no zombie Chrome processes accumulating
  across hourly runs).
- Retries the whole flow with a brand-new browser on any failure — Cloudflare not
  passed / **Sign In stayed disabled** / page closed / dashboard not reached / CDP
  connect failure — up to **4 attempts** with a 15s backoff.
- If all attempts fail, sends a **Telegram alert** so you know that hour needs
  attention, and exits non-zero.

### One-time setup on the box

```bash
sudo apt update
sudo apt install -y xvfb google-chrome-stable    # or: chromium
git clone <your-repo> /opt/vfs-malta-slot-checker
cd /opt/vfs-malta-slot-checker
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium             # Playwright client deps

# Put your real credentials in a gitignored local override:
cp config/config.local.ini.example config/config.local.ini  # if present, else create it
$EDITOR config/config.local.ini                   # [vfs-credential], [telegram]
```

Set `[browser] headless = false` (we run headed under Xvfb). Chrome is found
automatically; pin it with the `CHROME_PATH` env var if needed.

### Run it (one command)

```bash
./run_ec2.sh                  # one self-healing run (AE -> MT)
```

[`run_ec2.sh`](run_ec2.sh) wraps the supervisor with `flock` (a lockfile so
overlapping runs can't pile up) and `xvfb-run -a` (a fresh virtual display per
run, torn down after).

### Schedule hourly (cron)

```cron
0 * * * * /opt/vfs-malta-slot-checker/run_ec2.sh >> /opt/vfs-malta-slot-checker/app.log 2>&1
```

That's it — each hour spins up a clean browser, checks the slots, reports via
Telegram, cleans up, and retries/alerts on failure on its own.

## Notes / gotchas

- **Captcha:** the portal shows a Cloudflare *Verify Captcha* dialog that usually
  auto-solves; the bot clicks *Submit* for it. Validate this works in your headless
  prod environment on the first run — a rare interactive challenge can't be
  automated and would need a manual/headed solve.
- **Slot dates shift in real time** — the bot reports whatever is live at run time.
- The browser is launched fresh each run (headless), so no persistent profile is
  needed in prod. Set `[browser] cdp_url` only if you want to attach to a Chrome
  you launched yourself (handy locally).
