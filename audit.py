#!/usr/bin/env python3
"""
GTM Deliverability Audit
Checks SPF/DKIM/DMARC, Smartlead inbox health, and campaign performance.
Outputs markdown report + CSV files.

Usage:
    python audit.py domains --domains send1.co,send2.co --out ./audit
    python audit.py full --out ./audit                    # needs SMARTLEAD_API_KEY
    python audit.py dns --from-csv inboxes.csv --out ./audit
"""

from __future__ import annotations

import argparse
import csv
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Union

import requests

API_BASE = "https://server.smartlead.ai/api/v1"
DKIM_SELECTOR = "default"


def dig_txt(name: str) -> str:
    try:
        out = subprocess.check_output(
            ["dig", "TXT", name, "+short"],
            stderr=subprocess.DEVNULL,
            timeout=10,
            text=True,
        )
        return out.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def check_domain_auth(domain: str, dkim_selector: str = DKIM_SELECTOR) -> dict:
    spf_raw = dig_txt(domain)
    dkim_raw = dig_txt(f"{dkim_selector}._domainkey.{domain}")
    dmarc_raw = dig_txt(f"_dmarc.{domain}")

    spf_lines = [l for l in spf_raw.split("\n") if "v=spf1" in l]
    spf = spf_lines[0].strip('"') if spf_lines else ""
    spf_present = bool(spf)
    spf_strict = "-all" in spf

    dkim_present = "v=DKIM1" in dkim_raw or "p=" in dkim_raw

    dmarc_lines = [l for l in dmarc_raw.split("\n") if "v=DMARC1" in l]
    dmarc = dmarc_lines[0].strip('"') if dmarc_lines else ""
    dmarc_present = bool(dmarc)
    dmarc_policy = ""
    if dmarc_present:
        for part in dmarc.split(";"):
            part = part.strip()
            if part.startswith("p="):
                dmarc_policy = part.split("=", 1)[1]

    notes = []
    if not spf_present:
        notes.append("SPF missing — add v=spf1 include:<provider> ~all")
    elif not spf_strict:
        notes.append("SPF loose (~all) — tighten to -all once stable")
    if not dkim_present:
        notes.append(f"DKIM missing at {dkim_selector}._domainkey")
    if not dmarc_present:
        notes.append("DMARC missing — add v=DMARC1; p=none; rua=mailto:...")
    elif dmarc_policy == "none":
        notes.append("DMARC policy=none — no enforcement yet")

    score = 100
    if not spf_present:
        score -= 35
    elif not spf_strict:
        score -= 10
    if not dkim_present:
        score -= 35
    if not dmarc_present:
        score -= 20
    elif dmarc_policy == "none":
        score -= 5

    return {
        "domain": domain,
        "spf_present": spf_present,
        "spf_strict": spf_strict,
        "spf_record": spf[:120],
        "dkim_present": dkim_present,
        "dkim_selector": dkim_selector,
        "dmarc_present": dmarc_present,
        "dmarc_policy": dmarc_policy or ("missing" if not dmarc_present else "unknown"),
        "auth_score": max(0, score),
        "notes": "; ".join(notes),
    }


def write_csv(path: Path, rows: List[Dict]):
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0].keys())
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def load_domains_from_csv(path: Path) -> List[str]:
    domains = set()
    with path.open() as f:
        for row in csv.DictReader(f):
            if row.get("domain"):
                domains.add(row["domain"].strip().lower())
            elif row.get("email") and "@" in row["email"]:
                domains.add(row["email"].split("@")[1].strip().lower())
    return sorted(domains)


def smartlead_get(path: str, api_key: str, params: Optional[Dict] = None) -> Union[List, Dict]:
    p = {"api_key": api_key, **(params or {})}
    resp = requests.get(f"{API_BASE}{path}", params=p, timeout=30)
    resp.raise_for_status()
    return resp.json()


def pull_inboxes(api_key: str, tag: Optional[str] = None, domain: Optional[str] = None) -> List[Dict]:
    inboxes = []
    offset = 0
    while True:
        batch = smartlead_get("/email-accounts", api_key, {"offset": offset, "limit": 100})
        if not isinstance(batch, list) or not batch:
            break
        inboxes.extend(batch)
        if len(batch) < 100:
            break
        offset += 100

    rows = []
    for i in inboxes:
        email = i.get("from_email") or i.get("email") or ""
        dom = email.split("@")[1] if "@" in email else ""
        tags = "|".join(t.get("name", "") for t in i.get("tags", []))
        w = i.get("warmup_details") or {}
        if tag and tag not in tags.split("|"):
            continue
        if domain and dom != domain:
            continue
        rows.append({
            "id": i.get("id"),
            "email": email,
            "domain": dom,
            "from_name": i.get("from_name", ""),
            "tags": tags,
            "warmup_status": w.get("status", ""),
            "warmup_reputation": w.get("warmup_reputation", ""),
            "max_warmup_per_day": w.get("max_email_per_day", ""),
            "is_warmup_blocked": w.get("is_warmup_blocked", False),
            "daily_sent_count": i.get("daily_sent_count", 0),
            "message_per_day": i.get("message_per_day", ""),
            "is_smtp_success": i.get("is_smtp_success", False),
            "is_imap_success": i.get("is_imap_success", False),
            "is_blocked": not i.get("is_smtp_success", True) or w.get("is_warmup_blocked", False),
        })
    return rows


def pull_campaign_performance(api_key: str, days: int = 30) -> List[Dict]:
    campaigns = smartlead_get("/campaigns", api_key)
    if not isinstance(campaigns, list):
        return []

    perf = []
    for camp in campaigns:
        if camp.get("status") not in ("ACTIVE", "PAUSED", "COMPLETED", "START"):
            continue
        cid = camp.get("id")
        try:
            analytics = smartlead_get(f"/campaigns/{cid}/analytics", api_key)
        except requests.HTTPError:
            continue

        sent = int(analytics.get("sent_count") or analytics.get("total_sent") or 0)
        replies = int(analytics.get("reply_count") or analytics.get("total_replied") or 0)
        bounces = int(analytics.get("bounce_count") or analytics.get("total_bounced") or 0)
        reply_rate = round((replies / sent) * 100, 2) if sent else 0
        bounce_rate = round((bounces / sent) * 100, 2) if sent else 0

        perf.append({
            "campaign_id": cid,
            "campaign_name": camp.get("name", ""),
            "status": camp.get("status", ""),
            "sent": sent,
            "replies": replies,
            "bounces": bounces,
            "reply_rate_pct": reply_rate,
            "bounce_rate_pct": bounce_rate,
            "flag_low_reply": sent >= 200 and reply_rate < 1.0,
            "flag_high_bounce": sent >= 50 and bounce_rate > 3.0,
        })
    return perf


def generate_report(out_dir: Path, auth_rows: List[Dict], inbox_rows: List[Dict], perf_rows: List[Dict]) -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [f"# Deliverability Audit — {today}", ""]

    if inbox_rows:
        blocked = sum(1 for r in inbox_rows if r.get("is_blocked"))
        lines += [
            "## Inbox Fleet",
            f"- {len(inbox_rows)} inboxes audited",
            f"- {blocked} blocked or unhealthy ({round(blocked / len(inbox_rows) * 100, 1)}%)",
            "",
        ]

    if auth_rows:
        failing = [r for r in auth_rows if r["auth_score"] < 80]
        lines += [
            "## Domain Authentication",
            f"- {len(auth_rows)} domains checked",
            f"- {len(failing)} domains with auth issues",
            "",
        ]
        for r in failing[:10]:
            lines.append(f"- **{r['domain']}** (score {r['auth_score']}): {r['notes']}")
        lines.append("")

    if perf_rows:
        low_reply = [r for r in perf_rows if r["flag_low_reply"]]
        high_bounce = [r for r in perf_rows if r["flag_high_bounce"]]
        lines += [
            "## Campaign Performance (1% Rule)",
            f"- {len(perf_rows)} campaigns analyzed",
            f"- {len(low_reply)} below 1% reply rate after 200+ sends",
            f"- {len(high_bounce)} above 3% bounce rate",
            "",
        ]
        for r in low_reply[:5]:
            lines.append(
                f"- **{r['campaign_name']}**: {r['reply_rate_pct']}% reply "
                f"({r['sent']} sent) — check copy, list, or spam placement"
            )
        lines.append("")

    lines += [
        "## Action Items",
        "1. Fix any domain missing SPF, DKIM, or DMARC before scaling volume",
        "2. Pause blocked inboxes until SMTP/IMAP reconnects",
        "3. Campaigns below 1% reply after 200 sends: audit copy + list quality",
        "4. Bounce rate >3%: stop sending, re-verify list, check ICP fit",
        "5. Re-run audit weekly as routine hygiene",
        "",
    ]

    report = "\n".join(lines)
    (out_dir / "report.md").write_text(report)
    return report


def cmd_dns(args):
    out_dir = Path(args.out)
    if args.domains:
        domains = [d.strip().lower() for d in args.domains.split(",") if d.strip()]
    elif args.from_csv:
        domains = load_domains_from_csv(Path(args.from_csv))
    else:
        print("Provide --domains or --from-csv", file=sys.stderr)
        sys.exit(1)

    rows = [check_domain_auth(d, args.dkim_selector) for d in domains]
    write_csv(out_dir / "auth.csv", rows)
    print(f"Checked {len(rows)} domains → {out_dir / 'auth.csv'}")
    for r in rows:
        status = "OK" if r["auth_score"] >= 80 else "ISSUE"
        print(f"  [{status}] {r['domain']}: score={r['auth_score']} — {r['notes'] or 'all good'}")


def cmd_full(args):
    api_key = os.environ.get("SMARTLEAD_API_KEY")
    if not api_key:
        print("Set SMARTLEAD_API_KEY for full audit", file=sys.stderr)
        sys.exit(1)

    out_dir = Path(args.out)
    inbox_rows = pull_inboxes(api_key, tag=args.tag, domain=args.domain)
    write_csv(out_dir / "inboxes.csv", inbox_rows)

    domains = sorted({r["domain"] for r in inbox_rows if r.get("domain")})
    auth_rows = [check_domain_auth(d, args.dkim_selector) for d in domains]
    write_csv(out_dir / "auth.csv", auth_rows)

    perf_rows = pull_campaign_performance(api_key, days=args.days)
    write_csv(out_dir / "performance.csv", perf_rows)

    report = generate_report(out_dir, auth_rows, inbox_rows, perf_rows)
    print(report)
    print(f"\nFiles written to {out_dir}/")


def main():
    parser = argparse.ArgumentParser(description="GTM Deliverability Audit")
    sub = parser.add_subparsers(dest="command", required=True)

    p_dns = sub.add_parser("dns", help="Check SPF/DKIM/DMARC only")
    p_dns.add_argument("--domains", help="Comma-separated domains")
    p_dns.add_argument("--from-csv", help="CSV with domain or email column")
    p_dns.add_argument("--dkim-selector", default=DKIM_SELECTOR)
    p_dns.add_argument("--out", default="./audit")
    p_dns.set_defaults(func=cmd_dns)

    p_full = sub.add_parser("full", help="Full audit via Smartlead API + DNS")
    p_full.add_argument("--tag", help="Filter inboxes by tag")
    p_full.add_argument("--domain", help="Filter inboxes by domain")
    p_full.add_argument("--days", type=int, default=30)
    p_full.add_argument("--dkim-selector", default=DKIM_SELECTOR)
    p_full.add_argument("--out", default="./audit")
    p_full.set_defaults(func=cmd_full)

    # alias: domains = dns
    p_dom = sub.add_parser("domains", help="Alias for dns --domains")
    p_dom.add_argument("--domains", required=True)
    p_dom.add_argument("--dkim-selector", default=DKIM_SELECTOR)
    p_dom.add_argument("--out", default="./audit")
    p_dom.set_defaults(func=cmd_dns)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()