#!/usr/bin/env python3
"""
World Cup 2026 — MetLife Stadium multi-match ticket monitor (v4, Playwright).

Uses headless Chromium so JavaScript-rendered ticket sites actually return
data. Slower than v3 (~5-7 min per run vs 30 sec) but produces real listings
on TickPick, Vivid Seats, Gametime, SeatPick, and (sometimes) StubHub/Viagogo.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from playwright.sync_api import sync_playwright, Page, BrowserContext

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "state.json"
RESULTS_PATH = ROOT / "results.json"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")


# ---------- config / state -------------------------------------------------

def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except json.JSONDecodeError:
            pass
    return {"alerted": []}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2))


# ---------- listing model --------------------------------------------------

@dataclass
class Listing:
    site: str
    section: str
    row: str
    qty: int
    price_each: float
    fees_included: bool
    url: str
    raw_id: str

    def all_in(self, fee_multiplier: float) -> float:
        if self.fees_included:
            return round(self.price_each, 2)
        return round(self.price_each * fee_multiplier, 2)

    def fingerprint(self, match_id: str) -> str:
        s = f"{match_id}|{self.site}|{self.raw_id}|{self.section}|{self.row}|{self.qty}|{self.price_each}"
        return hashlib.md5(s.encode()).hexdigest()[:12]


# ---------- classification -------------------------------------------------

def classify(section: str) -> Optional[str]:
    """MetLife: 100s = Cat 1, 200s = Cat 2, 300s+ = skip."""
    digits = re.sub(r"\D", "", str(section))
    if not digits:
        return None
    n = int(digits)
    if 100 <= n <= 199:
        return "Cat 1"
    if 200 <= n <= 299:
        return "Cat 2"
    return None


# ---------- network response captor ---------------------------------------

class ResponseCaptor:
    """Captures JSON responses during a page load. Site-specific filtering."""
    def __init__(self, site_hint: str):
        self.site_hint = site_hint
        self.responses: list[dict] = []

    def on_response(self, response):
        try:
            url = response.url
            if response.status != 200:
                return
            ct = (response.headers or {}).get("content-type", "")
            if "json" not in ct.lower():
                return
            url_l = url.lower()
            keywords = ["listing", "ticket", "inventory", "offer", "production", "event/"]
            if not any(k in url_l for k in keywords):
                return
            try:
                body = response.json()
            except Exception:
                return
            self.responses.append({"url": url, "body": body})
        except Exception:
            pass


def _walk_for_items(node, keys_hint, depth=0, max_depth=8):
    """Walk a JSON tree looking for a list of dicts that look like listings."""
    if depth > max_depth:
        return None
    if isinstance(node, list) and node and isinstance(node[0], dict):
        keys = set()
        for it in node[:5]:
            if isinstance(it, dict):
                keys.update(it.keys())
        if any(k in keys for k in keys_hint):
            return node
    if isinstance(node, dict):
        for v in node.values():
            r = _walk_for_items(v, keys_hint, depth + 1, max_depth)
            if r is not None:
                return r
    if isinstance(node, list):
        for v in node:
            r = _walk_for_items(v, keys_hint, depth + 1, max_depth)
            if r is not None:
                return r
    return None


# ---------- per-site parsers ----------------------------------------------

def parse_generic_listings(bodies, site_name, event_url, fees_included=False,
                           section_keys=("section", "sectionName", "s"),
                           row_keys=("row", "r"),
                           qty_keys=("quantity", "q", "availableQuantity", "qty"),
                           price_keys=("totalPrice", "currentPrice", "price", "p", "dp")):
    listings = []
    seen = set()
    for resp in bodies:
        items = _walk_for_items(resp["body"],
                                set(section_keys) | set(price_keys) | {"id", "listingId"})
        if not items:
            continue
        for it in items:
            if not isinstance(it, dict):
                continue
            section = ""
            for k in section_keys:
                v = it.get(k)
                if v:
                    section = str(v)
                    break
            row = ""
            for k in row_keys:
                v = it.get(k)
                if v:
                    row = str(v)
                    break
            qty = 0
            for k in qty_keys:
                v = it.get(k)
                if v:
                    qty = v
                    break
            price = 0
            for k in price_keys:
                v = it.get(k)
                if isinstance(v, dict):
                    price = v.get("amount") or v.get("value") or 0
                    if price:
                        break
                elif v:
                    price = v
                    break
            try:
                qty_i, price_f = int(qty), float(price)
            except (TypeError, ValueError):
                continue
            if not section or price_f <= 0 or qty_i <= 0:
                continue
            lid = str(it.get("id") or it.get("listingId") or f"{section}-{row}-{price}")
            sig = (section, row, qty_i, round(price_f, 2))
            if sig in seen:
                continue
            seen.add(sig)
            listings.append(Listing(site_name, section, row, qty_i, price_f,
                                     fees_included, event_url, lid))
    return listings


SITE_PARSE_CFG = {
    "tickpick":   {"display": "TickPick",    "fees_inc": True},
    "vividseats": {"display": "Vivid Seats", "fees_inc": True},
    "seatgeek":   {"display": "SeatGeek",    "fees_inc": False},
    "gametime":   {"display": "Gametime",    "fees_inc": True},
    "viagogo":    {"display": "Viagogo",     "fees_inc": False},
    "axs":        {"display": "AXS",         "fees_inc": False},
    "seatpick":   {"display": "SeatPick",    "fees_inc": True},
    "stubhub":    {"display": "StubHub",     "fees_inc": False},
}


# ---------- single-page scrape via Playwright -----------------------------

def scrape_site(context: BrowserContext, site_key: str, event_url: str):
    """Open the URL in a fresh page, capture API responses, parse listings."""
    cfg = SITE_PARSE_CFG.get(site_key)
    if not cfg:
        return [], {"status": "fail", "error": "no parser config"}

    page = context.new_page()
    captor = ResponseCaptor(site_key)
    page.on("response", captor.on_response)

    try:
        page.goto(event_url, wait_until="domcontentloaded", timeout=30000)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        page.wait_for_timeout(2500)
    except Exception as e:
        page.close()
        return [], {"status": "fail", "error": f"navigation: {str(e)[:80]}"}

    page.close()

    if not captor.responses:
        return [], {"status": "fail", "error": "no JSON responses captured"}

    listings = parse_generic_listings(
        captor.responses, cfg["display"], event_url, fees_included=cfg["fees_inc"]
    )
    if not listings:
        return [], {"status": "ok",
                    "count": 0,
                    "error": f"captured {len(captor.responses)} responses, no listings parsed"}
    return listings, {"status": "ok", "count": len(listings)}


# ---------- per-match scan -------------------------------------------------

def scan_match(context: BrowserContext, match: dict):
    listings: list[Listing] = []
    sites_status: dict = {}
    for site_key, site_cfg in match.get("sites", {}).items():
        if not site_cfg.get("enabled", False) or not site_cfg.get("url"):
            sites_status[site_key] = {"status": "fail", "error": "disabled or no URL"}
            continue
        try:
            site_listings, status = scrape_site(context, site_key, site_cfg["url"])
            listings += site_listings
            sites_status[site_key] = status
        except Exception as e:
            traceback.print_exc(limit=1)
            sites_status[site_key] = {"status": "fail", "error": str(e)[:80]}
    return listings, sites_status


# ---------- matching -------------------------------------------------------

def find_matches_for(listings, ceilings, fee_multipliers, min_qty):
    out = []
    cat1_cap = float(ceilings["cat1"])
    cat2_cap = float(ceilings["cat2"])
    for l in listings:
        cat = classify(l.section)
        if cat is None or l.qty < min_qty:
            continue
        all_in = l.all_in(fee_multipliers.get(l.site, 1.0))
        cap = cat1_cap if cat == "Cat 1" else cat2_cap
        if all_in > cap:
            continue
        out.append((l, cat, all_in))
    out.sort(key=lambda x: (x[2], -x[0].qty))
    return out


# ---------- alerting -------------------------------------------------------

def send_telegram(text: str, config: dict) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or config.get("telegram_bot_token", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID") or config.get("telegram_chat_id", "")
    if not token or not chat:
        print("  ! Telegram creds missing — would have alerted")
        print(text)
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text, "parse_mode": "Markdown",
                  "disable_web_page_preview": False},
            timeout=15,
        )
        r.raise_for_status()
        return True
    except requests.RequestException as e:
        print(f"  ! Telegram error: {e}")
        return False


def format_alert(by_match, max_per_match: int = 3) -> str:
    lines = ["🚨 *WC '26 TICKET MATCH*", ""]
    for match, hits in by_match:
        if not hits:
            continue
        lines.append(f"*{match['label']}* — {match['date']}")
        for l, cat, all_in in hits[:max_per_match]:
            row_part = f" R{l.row}" if l.row else ""
            lines.append(f"  • {cat} sec {l.section}{row_part} · qty {l.qty} · "
                         f"*${all_in:.0f}/ea* · {l.site}")
            lines.append(f"    [Buy →]({l.url})")
        if len(hits) > max_per_match:
            lines.append(f"  _(+{len(hits) - max_per_match} more for this match)_")
        lines.append("")
    return "\n".join(lines)


# ---------- results.json --------------------------------------------------

def serialize_match(match, listings, sites_status, fees, min_qty):
    serialized = []
    for l in listings:
        cat = classify(l.section)
        if cat is None or l.qty < min_qty:
            continue
        serialized.append({
            "site": l.site, "section": l.section, "row": l.row, "qty": l.qty,
            "price_each": round(l.price_each, 2),
            "all_in": l.all_in(fees.get(l.site, 1.0)),
            "category": cat, "url": l.url,
            "fingerprint": l.fingerprint(match["id"]),
        })
    serialized.sort(key=lambda x: x["all_in"])

    return {
        "id": match["id"], "label": match["label"],
        "match_no": match.get("match_no"), "stage": match.get("stage"),
        "date": match.get("date"), "kickoff": match.get("kickoff"),
        "ceilings": match["price_ceilings"],
        "sites": {SITE_PARSE_CFG.get(k, {}).get("display", k): v
                  for k, v in sites_status.items()},
        "listings": serialized,
    }


def write_results(matches_payload):
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "venue": "MetLife Stadium",
        "venue_label": "New York New Jersey Stadium",
        "matches": matches_payload,
    }
    RESULTS_PATH.write_text(json.dumps(payload, indent=2))
    total = sum(len(m["listings"]) for m in matches_payload)
    print(f"  wrote results.json — {len(matches_payload)} matches, {total} total listings")


# ---------- main -----------------------------------------------------------

def main() -> int:
    config = load_config()
    state = load_state()
    fees = config.get("fee_multipliers", {})
    min_qty = int(config.get("min_quantity", 2))

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"[{now}] launching browser...")

    matches_payload = []
    all_alerts: list[tuple[dict, list]] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            user_agent=UA,
            viewport={"width": 1280, "height": 800},
            locale="en-US",
            timezone_id="America/New_York",
        )
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )

        for match in config.get("matches", []):
            if not match.get("enabled", True):
                print(f"  ~ skip {match['label']} (disabled)")
                continue
            print(f"  > {match['label']} ({match['date']})")
            try:
                listings, sites_status = scan_match(context, match)
            except Exception as e:
                print(f"    ! match-level exception: {e}")
                sites_status = {"error": {"status": "fail", "error": str(e)[:80]}}
                listings = []

            for site, st in sites_status.items():
                count = st.get("count", "")
                err = f" err={st['error']}" if st.get("error") and st.get("status") == "fail" else ""
                print(f"      {site}: {st.get('status')} {count}{err}".rstrip())

            payload = serialize_match(match, listings, sites_status, fees, min_qty)
            matches_payload.append(payload)

            hits = find_matches_for(listings, match["price_ceilings"], fees, min_qty)
            if hits:
                print(f"      ✓ {len(hits)} under-ceiling")
                all_alerts.append((match, hits))

        context.close()
        browser.close()

    write_results(matches_payload)

    alerted = set(state.get("alerted", []))
    new_by_match: list[tuple[dict, list]] = []
    all_new_fingerprints = set()
    for match, hits in all_alerts:
        new_hits = []
        for l, cat, all_in in hits:
            fp = l.fingerprint(match["id"])
            if fp not in alerted:
                new_hits.append((l, cat, all_in))
                all_new_fingerprints.add(fp)
        if new_hits:
            new_by_match.append((match, new_hits))

    if not new_by_match:
        print("  no new under-ceiling listings to alert")
        return 0

    print(f"  🚨 alerting on {sum(len(h) for _, h in new_by_match)} new listings "
          f"across {len(new_by_match)} matches")
    if send_telegram(format_alert(new_by_match), config):
        alerted.update(all_new_fingerprints)
        state["alerted"] = list(alerted)[-1000:]
        state["last_alert_at"] = now
        save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())