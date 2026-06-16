# GTM Deliverability Audit

Diagnostic audit for a running cold email program. Checks domain authentication (SPF/DKIM/DMARC), Smartlead inbox health, and campaign performance against the **1% reply rule**.

## What it checks

| Layer | What | How |
|-------|------|-----|
| DNS auth | SPF, DKIM, DMARC on each sending domain | `dig` via subprocess |
| Inbox health | Warmup status, SMTP/IMAP, blocks | Smartlead email-accounts API |
| Performance | Reply rate, bounce rate per campaign | Smartlead campaign analytics |
| 1% rule | Flag campaigns <1% reply after 200+ sends | Automatic |

## Setup

```bash
pip install -r requirements.txt
export SMARTLEAD_API_KEY=your_key   # only for full audit
```

Requires `dig` (pre-installed on macOS/Linux).

## Usage

**DNS-only audit (no API key needed):**

```bash
python audit.py domains --domains send1.co,send2.co,send3.co --out ./audit
```

**DNS from inbox CSV:**

```bash
python audit.py dns --from-csv inboxes.csv --out ./audit
```

**Full audit (Smartlead + DNS + report):**

```bash
python audit.py full --out ./audit
python audit.py full --tag active --out ./audit
python audit.py full --domain send1.co --out ./audit
```

## Output files

| File | Contents |
|------|----------|
| `auth.csv` | Per-domain SPF/DKIM/DMARC scores + notes |
| `inboxes.csv` | Inbox fleet health (full audit only) |
| `performance.csv` | Campaign reply/bounce rates with flags |
| `report.md` | Human-readable summary + action items |

## The 1% rule

A healthy domain/campaign should hit **≥1% reply rate after 200 emails sent**. Below that with sufficient volume = something is broken:

- Emails landing in spam
- Domain reputation damaged
- Copy is generic or broken
- List is wrong ICP or unverified (bounces >3%)
- Inbox hasn't warmed enough

## When to run

- Reply rate dropped >30% week-over-week
- Bounces spiked above 2%
- Before scaling a new campaign
- Monthly hygiene check
- Taking over someone else's Smartlead account

## PDF guide

Download: [docs/play-guide.pdf](docs/play-guide.pdf) — setup, commands, 1% rule, when to run.

Regenerate: `python3 ../gtm-portfolio/tools/generate_play_pdfs.py`

## n8n workflow

Import `n8n/weekly-deliverability-audit.json` into n8n.

| Node | What |
|------|------|
| Cron Monday 9am | Triggers weekly |
| Fetch Smartlead inboxes | Fleet health |
| Google DNS API | SPF + DMARC per domain |
| Campaign analytics | 1% reply rule check |
| Slack | Posts summary to `#gtm-alerts` |

**Env vars:** `SMARTLEAD_API_KEY`, `SLACK_DELIVERABILITY_CHANNEL`

## Pair with

- [gtm-list-quality-scorecard](https://github.com/rasulshaikh/gtm-list-quality-scorecard) — grade lists before sending
- [gtm-email-cadences](https://github.com/rasulshaikh/gtm-email-cadences) — signal-led copy library