#!/usr/bin/env python3
"""
World Cup 2026 — MetLife Stadium multi-match ticket monitor (v3).

Monitors ALL 8 MetLife matches: 5 group stage games, Round of 32, Round of 16,
and the Final. Per-match price ceilings live in config.json.

For each match, scrapes whichever sites have URLs configured (TickPick, Vivid
Seats, AXS, SeatPick, SeatGeek, Gametime, Viagogo, StubHub), classifies seats
into Cat 1/2 by section number, and:
  - writes results.json (consumed by the dashboard UI)
  - sends Telegram alerts for new under-ceiling listings, grouped by match
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

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "state.json"
RESULTS_PATH = ROOT / "results.json"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


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


# ---------- HTTP helper ----------------------------------------------------

def _get(url: str, headers: Optional[dict] = None, timeout: int = 25):
    base = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
    if headers:
        base.update(headers)
    try:
        r = requests.get(url, headers=base, timeout=timeout)
        if r.status_code != 200:
            return None
        return r
    except requests.RequestException:
        return None


# ---------- site scrapers --------------------------------------------------

def fetch_tickpick(event_url: str):
    out = []
    m = re.search(r"/(\d{6,})/?", event_url)
    if not m:
        return out, {"status": "fail", "error": "bad URL"}
    event_id = m.group(1)
    api = f"https://www.tickpick.com/api/v3/listings/event/{event_id}/"
    r = _get(api, headers={"Accept": "application/json", "Referer": event_url})
    items = []
    if r is not None:
        try:
            data = r.json()
            items = data.get("listings", data) if isinstance(data, dict) else (data or [])
        except ValueError:
            items = []
    if not items:
        r = _get(event_url)
        if r is None:
            return out, {"status": "fail", "error": "fetch failed"}
        blob = re.search(r'"listings"\s*:\s*(\[[^\]]*\])', r.text)
        if blob:
            try:
                items = json.loads(blob.group(1))
            except json.JSONDecodeError:
                items = []

    for it in items:
        section = it.get("s") or it.get("section") or ""
        row = it.get("r") or it.get("row") or ""
        qty = it.get("q") or it.get("quantity") or 0
        price = it.get("p") or it.get("price") or it.get("currentPrice") or 0
        lid = it.get("id") or f"{section}-{row}-{price}"
        try:
            qty_i, price_f = int(qty), float(price)
        except (TypeError, ValueError):
            continue
        if not section or price_f <= 0 or qty_i <= 0:
            continue
        out.append(Listing("TickPick", str(section), str(row), qty_i, price_f, True, event_url, str(lid)))
    return out, {"status": "ok", "count": len(out)}


def fetch_seatgeek(event_url: str):
    out = []
    r = _get(event_url)
    if r is None:
        return out, {"status": "fail", "error": "fetch failed"}
    html = r.text
    blob = (re.search(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\});", html, re.DOTALL)
            or re.search(r'"listings"\s*:\s*(\[.*?\])\s*,\s*"', html, re.DOTALL))
    if not blob:
        return out, {"status": "fail", "error": "no state block"}
    try:
        parsed = json.loads(blob.group(1))
        items = parsed if isinstance(parsed, list) else (
            parsed.get("listings") or parsed.get("event", {}).get("listings") or [])
    except json.JSONDecodeError:
        return out, {"status": "fail", "error": "json parse"}

    for it in items:
        section = it.get("s") or it.get("section") or ""
        row = it.get("r") or it.get("row") or ""
        qty = it.get("q") or it.get("quantity") or 0
        price = it.get("dp") or it.get("p") or it.get("price") or 0
        lid = it.get("id") or f"sg-{section}-{row}-{price}"
        try:
            qty_i, price_f = int(qty), float(price)
        except (TypeError, ValueError):
            continue
        if not section or price_f <= 0 or qty_i <= 0:
            continue
        out.append(Listing("SeatGeek", str(section), str(row), qty_i, price_f, False, event_url, str(lid)))
    return out, {"status": "ok", "count": len(out)}


def fetch_vividseats(event_url: str):
    out = []
    r = _get(event_url)
    if r is None:
        return out, {"status": "fail", "error": "fetch failed"}
    html = r.text
    items = []
    for pattern in [r"__REDUX_STATE__\s*=\s*(\{.*?\})\s*</script>",
                    r"__NEXT_DATA__[^>]*>\s*(\{.*?\})\s*</script>"]:
        m = re.search(pattern, html, re.DOTALL)
        if not m:
            continue
        try:
            parsed = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        stack = [parsed]
        while stack:
            node = stack.pop()
            if isinstance(node, list) and node and isinstance(node[0], dict) and any(
                k in node[0] for k in ("section", "sectionName", "s")):
                items = node
                break
            if isinstance(node, dict):
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
        if items:
            break

    if not items:
        m = re.search(r'"listings"\s*:\s*(\[.*?\])', html, re.DOTALL)
        if m:
            try:
                items = json.loads(m.group(1))
            except json.JSONDecodeError:
                items = []

    if not items:
        return out, {"status": "fail", "error": "no listings"}

    for it in items:
        section = it.get("section") or it.get("sectionName") or it.get("s") or ""
        row = it.get("row") or it.get("r") or ""
        qty = it.get("quantity") or it.get("q") or 0
        price = (it.get("totalPrice") or it.get("currentPrice") or
                 it.get("price") or it.get("p") or 0)
        lid = it.get("id") or it.get("listingId") or f"vs-{section}-{row}-{price}"
        try:
            qty_i, price_f = int(qty), float(price)
        except (TypeError, ValueError):
            continue
        if not section or price_f <= 0 or qty_i <= 0:
            continue
        out.append(Listing("Vivid Seats", str(section), str(row), qty_i, price_f, True, event_url, str(lid)))
    return out, {"status": "ok", "count": len(out)}


def fetch_gametime(event_url: str):
    out = []
    r = _get(event_url)
    if r is None:
        return out, {"status": "fail", "error": "fetch failed"}
    html = r.text
    m = re.search(r"__NEXT_DATA__[^>]*>\s*(\{.*\})\s*</script>", html, re.DOTALL)
    if not m:
        return out, {"status": "fail", "error": "no next data"}
    try:
        parsed = json.loads(m.group(1))
    except json.JSONDecodeError:
        return out, {"status": "fail", "error": "json parse"}
    items = []
    stack = [parsed]
    while stack:
        node = stack.pop()
        if isinstance(node, list) and node and isinstance(node[0], dict) and any(
            k in node[0] for k in ("section", "sectionName", "row")):
            items = node
            break
        if isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    if not items:
        return out, {"status": "ok", "count": 0}
    for it in items:
        section = it.get("section") or it.get("sectionName") or ""
        row = it.get("row") or ""
        qty = it.get("quantity") or it.get("availableQuantity") or 0
        price = it.get("totalPrice") or it.get("price") or 0
        lid = it.get("id") or f"gt-{section}-{row}-{price}"
        try:
            qty_i, price_f = int(qty), float(price)
        except (TypeError, ValueError):
            continue
        if not section or price_f <= 0 or qty_i <= 0:
            continue
        out.append(Listing("Gametime", str(section), str(row), qty_i, price_f, True, event_url, str(lid)))
    return out, {"status": "ok", "count": len(out)}


def fetch_viagogo(event_url: str):
    out = []
    r = _get(event_url, headers={"Accept": "text/html"})
    if r is None:
        return out, {"status": "fail", "error": "fetch failed (likely anti-bot)"}
    html = r.text
    m = re.search(r'"Listings"\s*:\s*(\[.*?\])\s*,\s*"', html, re.DOTALL)
    if not m:
        m = re.search(r'"listings"\s*:\s*(\[.*?\])\s*,', html, re.DOTALL)
    if not m:
        return out, {"status": "fail", "error": "no listings block"}
    try:
        items = json.loads(m.group(1))
    except json.JSONDecodeError:
        return out, {"status": "fail", "error": "json parse"}

    for it in items:
        section = it.get("SectionName") or it.get("section") or ""
        row = it.get("Row") or it.get("row") or ""
        qty = it.get("Quantity") or it.get("quantity") or 0
        price = it.get("Price") or it.get("price") or 0
        lid = it.get("Id") or it.get("id") or f"vg-{section}-{row}-{price}"
        try:
            qty_i, price_f = int(qty), float(price)
        except (TypeError, ValueError):
            continue
        if not section or price_f <= 0 or qty_i <= 0:
            continue
        out.append(Listing("Viagogo", str(section), str(row), qty_i, price_f, False, event_url, str(lid)))
    return out, {"status": "ok", "count": len(out)}


def fetch_axs(event_url: str):
    out = []
    r = _get(event_url)
    if r is None:
        return out, {"status": "fail", "error": "fetch failed"}
    html = r.text
    m = re.search(r'"resaleListings"\s*:\s*(\[.*?\])', html, re.DOTALL)
    if not m:
        m = re.search(r'"listings"\s*:\s*(\[.*?\])', html, re.DOTALL)
    if not m:
        return out, {"status": "ok", "count": 0}
    try:
        items = json.loads(m.group(1))
    except json.JSONDecodeError:
        return out, {"status": "fail", "error": "json parse"}
    for it in items:
        section = it.get("section") or it.get("sectionName") or ""
        row = it.get("row") or ""
        qty = it.get("quantity") or 0
        price = it.get("price") or it.get("totalPrice") or 0
        lid = it.get("id") or f"axs-{section}-{row}-{price}"
        try:
            qty_i, price_f = int(qty), float(price)
        except (TypeError, ValueError):
            continue
        if not section or price_f <= 0 or qty_i <= 0:
            continue
        out.append(Listing("AXS", str(section), str(row), qty_i, price_f, False, event_url, str(lid)))
    return out, {"status": "ok", "count": len(out)}


def fetch_seatpick(event_url: str):
    out = []
    r = _get(event_url)
    if r is None:
        return out, {"status": "fail", "error": "fetch failed"}
    html = r.text
    m = re.search(r'"listings"\s*:\s*(\[.*?\])', html, re.DOTALL)
    if not m:
        m = re.search(r'"tickets"\s*:\s*(\[.*?\])', html, re.DOTALL)
    if not m:
        return out, {"status": "ok", "count": 0}
    try:
        items = json.loads(m.group(1))
    except json.JSONDecodeError:
        return out, {"status": "fail", "error": "json parse"}
    for it in items:
        section = it.get("section") or ""
        row = it.get("row") or ""
        qty = it.get("quantity") or it.get("qty") or 0
        price = it.get("price") or it.get("totalPrice") or 0
        lid = it.get("id") or f"sp-{section}-{row}-{price}"
        try:
            qty_i, price_f = int(qty), float(price)
        except (TypeError, ValueError):
            continue
        if not section or price_f <= 0 or qty_i <= 0:
            continue
        out.append(Listing("SeatPick", str(section), str(row), qty_i, price_f, True, event_url, str(lid)))
    return out, {"status": "ok", "count": len(out), "agg": True}


def fetch_stubhub(event_url: str):
    out = []
    r = _get(event_url)
    if r is None:
        return out, {"status": "fail", "error": "anti-bot blocked"}
    html = r.text
    m = re.search(r'"listings"\s*:\s*(\[.*?\])', html, re.DOTALL)
    if not m:
        return out, {"status": "fail", "error": "no listings (likely cloaked)"}
    try:
        items = json.loads(m.group(1))
    except json.JSONDecodeError:
        return out, {"status": "fail", "error": "json parse"}
    for it in items:
        section = it.get("sectionName") or it.get("section") or ""
        row = it.get("row") or ""
        qty = it.get("quantity") or 0
        cp = it.get("currentPrice")
        price = cp.get("amount") if isinstance(cp, dict) else (it.get("price") or 0)
        lid = it.get("id") or f"sh-{section}-{row}-{price}"
        try:
            qty_i, price_f = int(qty), float(price or 0)
        except (TypeError, ValueError):
            continue
        if not section or price_f <= 0 or qty_i <= 0:
            continue
        out.append(Listing("StubHub", str(section), str(row), qty_i, price_f, False, event_url, str(lid)))
    return out, {"status": "ok", "count": len(out)}


SCRAPERS = {
    "tickpick":   fetch_tickpick,
    "seatgeek":   fetch_seatgeek,
    "vividseats": fetch_vividseats,
    "gametime":   fetch_gametime,
    "viagogo":    fetch_viagogo,
    "axs":        fetch_axs,
    "seatpick":   fetch_seatpick,
    "stubhub":    fetch_stubhub,
}

SITE_DISPLAY = {
    "tickpick": "TickPick", "seatgeek": "SeatGeek", "vividseats": "Vivid Seats",
    "gametime": "Gametime", "viagogo": "Viagogo", "axs": "AXS",
    "seatpick": "SeatPick", "stubhub": "StubHub",
}


# ---------- per-match scan -------------------------------------------------

def scan_match(match: dict) -> tuple[list[Listing], dict]:
    """Returns (listings, sites_status) for one match."""
    listings: list[Listing] = []
    sites_status: dict = {}
    for site_name, site_cfg in match.get("sites", {}).items():
        scraper = SCRAPERS.get(site_name)
        if scraper is None:
            sites_status[site_name] = {"status": "fail", "error": "no scraper"}
            continue
        if not site_cfg.get("enabled", False) or not site_cfg.get("url"):
            sites_status[site_name] = {"status": "fail", "error": "disabled or no URL"}
            continue
        try:
            site_listings, status = scraper(site_cfg["url"])
            listings += site_listings
            sites_status[site_name] = status
        except Exception as e:
            traceback.print_exc(limit=1)
            sites_status[site_name] = {"status": "fail", "error": str(e)[:80]}
    return listings, sites_status


# ---------- matching -------------------------------------------------------

def find_matches_for(listings: list[Listing], ceilings: dict, fee_multipliers: dict, min_qty: int):
    """Return list of (listing, category, all_in_price) tuples under the ceilings."""
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


def format_alert(by_match: list[tuple[dict, list[tuple]]], max_per_match: int = 3) -> str:
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

def serialize_match(match: dict, listings: list[Listing], sites_status: dict,
                    fee_multipliers: dict, min_qty: int) -> dict:
    """Convert a match's listings into the dashboard-friendly format."""
    serialized = []
    for l in listings:
        cat = classify(l.section)
        if cat is None or l.qty < min_qty:
            continue
        serialized.append({
            "site": l.site,
            "section": l.section,
            "row": l.row,
            "qty": l.qty,
            "price_each": round(l.price_each, 2),
            "all_in": l.all_in(fee_multipliers.get(l.site, 1.0)),
            "category": cat,
            "url": l.url,
            "fingerprint": l.fingerprint(match["id"]),
        })
    serialized.sort(key=lambda x: x["all_in"])

    return {
        "id": match["id"],
        "label": match["label"],
        "match_no": match.get("match_no"),
        "stage": match.get("stage"),
        "date": match.get("date"),
        "kickoff": match.get("kickoff"),
        "ceilings": match["price_ceilings"],
        "sites": {SITE_DISPLAY.get(k, k): v for k, v in sites_status.items()},
        "listings": serialized,
    }


def write_results(matches_payload: list[dict]):
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "venue": "MetLife Stadium",
        "venue_label": "New York New Jersey Stadium",
        "matches": matches_payload,
    }
    RESULTS_PATH.write_text(json.dumps(payload, indent=2))
    print(f"  wrote results.json — {len(matches_payload)} matches, "
          f"{sum(len(m['listings']) for m in matches_payload)} total listings")


# ---------- main -----------------------------------------------------------

def main() -> int:
    config = load_config()
    state = load_state()
    fees = config.get("fee_multipliers", {})
    min_qty = int(config.get("min_quantity", 2))

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"[{now}] scanning all MetLife matches...")

    matches_payload = []
    all_alerts: list[tuple[dict, list]] = []

    for match in config.get("matches", []):
        if not match.get("enabled", True):
            print(f"  ~ skipping {match['label']} (disabled)")
            continue
        print(f"  > {match['label']} ({match['date']})")
        listings, sites_status = scan_match(match)
        for site, st in sites_status.items():
            print(f"      {site}: {st.get('status')} {st.get('count', '')}".rstrip())

        payload = serialize_match(match, listings, sites_status, fees, min_qty)
        matches_payload.append(payload)

        hits = find_matches_for(listings, match["price_ceilings"], fees, min_qty)
        if hits:
            print(f"      ✓ {len(hits)} under-ceiling")
            all_alerts.append((match, hits))

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
    msg = format_alert(new_by_match)
    if send_telegram(msg, config):
        alerted.update(all_new_fingerprints)
        state["alerted"] = list(alerted)[-1000:]
        state["last_alert_at"] = now
        save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())