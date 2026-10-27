#!/usr/bin/env python3
"""Builds P-Dex's daily price file.

Sources (no keys):
  - TCGdex's card database (github.com/tcgdex/cards-database): each card's Cardmarket and
    TCGplayer product ids.
  - tcgcsv.com: TCGplayer's prices for every Pokémon product, refreshed daily around 20:00 UTC.
  - Cardmarket's public price guide (downloads.s3.cardmarket.com).

Cards TCGdex hasn't linked are matched here: TCGplayer by set and number, Cardmarket by name
within the set when the name is unique there.

Output: out/prices.json (signed by the workflow: out/prices.json.sig), {"updated", "sources", "cards": {tcgdexId: {"tcgplayer": {...}, "cardmarket": {...}}}},
the same shape as TCGdex's `pricing` block, so the app reads it with the parser it already has.

Usage: build.py <cards-database checkout> [--cache DIR]
"""

import gzip
import json
import os
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
import urllib.error

import linking
from datetime import datetime, timedelta, timezone

TCGCSV = "https://tcgcsv.com/tcgplayer/3"
CARDMARKET_GUIDE = "https://downloads.s3.cardmarket.com/productCatalog/priceGuide/price_guide_6.json"
CARDMARKET_PRODUCTS = "https://downloads.s3.cardmarket.com/productCatalog/productList/products_singles_6.json"

# TCGdex set id -> TCGplayer group name, where the names don't match on their own (linking.py).
GROUP_ALIASES = {
    "2021swsh": "McDonald's 25th Anniversary Promos",
    "mep": "ME: Mega Evolution Promo",
    "30th-c": "ME: 30th Celebration Classic Collection",
    "cel25cc": "Celebrations: Classic Collection",
}
# Reprint sets whose cards keep their original numbers on TCGplayer: matched by name.
NAME_ONLY_SETS = {"30th-c", "cel25cc"}

CARDMARKET_FIELDS = ["low", "trend", "avg", "avg7", "avg30"]
MIN_PRICED = 15000
SITE = "https://miguelavtomas.github.io/p-dex-data"
HISTORY_DAYS = 400


CACHE = os.environ.get("PDEX_CACHE")  # local runs: keep each download for 20 hours (tcgcsv asks for one a day)


def get_json(url, tries=3):
    if CACHE:
        import hashlib
        path = os.path.join(CACHE, hashlib.sha1(url.encode()).hexdigest() + ".json")
        if os.path.exists(path) and time.time() - os.path.getmtime(path) < 20 * 3600:
            return json.load(open(path))
        data = fetch_json(url, tries)
        os.makedirs(CACHE, exist_ok=True)
        json.dump(data, open(path, "w"))
        return data
    return fetch_json(url, tries)


def fetch_json(url, tries=3):
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "p-dex-data (daily price file)"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except Exception as e:
            if attempt == tries - 1:
                raise
            time.sleep(2 + attempt * 3)


# MARK: TCGdex database

def ts_block(text, key, indent):
    """The `{ ... }` after `key:` at this tab indent, or None."""
    m = re.search(r"^\t{%d}%s:\s*\{" % (indent, key), text, re.M)
    if not m:
        return None
    depth, i = 1, m.end()
    while depth and i < len(text):
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        i += 1
    return text[m.end():i - 1]


def ts_block_list(text, key):
    """The `[ ... ]` after `key:` at the card's top level, or ""."""
    m = re.search(r"^\t%s:\s*\[" % key, text, re.M)
    if not m:
        return ""
    depth, i = 1, m.end()
    while depth and i < len(text):
        depth += {"[": 1, "]": -1}.get(text[i], 0)
        i += 1
    return text[m.end():i - 1]


def ids_in(block):
    if not block:
        return {}
    found = {}
    for market in ("cardmarket", "tcgplayer"):
        m = re.search(r"%s:\s*(\d+)" % market, block)
        if m:
            found[market] = int(m.group(1))
    return found


def english_name(text):
    name = ts_block(text, "name", 1)
    m = name and re.search(r"\ben:\s*(['\"])(.*?)\1\s*,?\s*$", name, re.M)
    return m.group(2).replace("\\'", "'").replace('\\"', '"') if m else None


def load_database(root):
    """Sets and cards (English ones) with the product ids TCGdex has for them."""
    data = os.path.join(root, "data")
    sets, cards = {}, {}
    for serie in sorted(os.listdir(data)):
        serie_dir = os.path.join(data, serie)
        if not os.path.isdir(serie_dir):
            continue
        for entry in sorted(os.listdir(serie_dir)):
            if not entry.endswith(".ts"):
                continue
            set_name = entry[:-3]
            text = open(os.path.join(serie_dir, entry), encoding="utf-8").read()
            m = re.search(r"^\tid:\s*['\"]([^'\"]+)['\"]", text, re.M)
            name = english_name(text)
            card_dir = os.path.join(serie_dir, set_name)
            if not m or not name or not os.path.isdir(card_dir):
                continue
            set_id = m.group(1)
            sets[set_id] = {"name": name, **ids_in(ts_block(text, "thirdParty", 1))}
            for card_file in os.listdir(card_dir):
                if not card_file.endswith(".ts"):
                    continue
                local_id = card_file[:-3]
                text = open(os.path.join(card_dir, card_file), encoding="utf-8").read()
                card_name = english_name(text)
                if not card_name:
                    continue
                ids = ids_in(ts_block(text, "thirdParty", 1))
                # Older files keep the ids on each printing instead: the first one that has them.
                if len(ids) < 2:
                    for block in re.finditer(r"^\t{3}thirdParty:\s*\{([^}]*)\}", text, re.M):
                        ids = {**ids_in(block.group(1)), **ids}
                # Abilities then attacks, as Cardmarket lists them after the name.
                attacks = []
                for block in ("abilities", "attacks"):
                    part = ts_block_list(text, block)
                    attacks += [m.group(2) for m in re.finditer(r"name:\s*\{\s*en:\s*(['\"])(.*?)\1", part)]
                cards[f"{set_id}-{local_id}"] = {"set": set_id, "localId": local_id, "name": card_name, "attacks": attacks,
                                                 **{"tcgdex" + k.capitalize(): v for k, v in ids.items()}}
    return sets, cards


# MARK: History

def series_of(pricing):
    """Today's figure for each of a card's series: TCGplayer's market price per printing, and
    Cardmarket's trend for the base ("cm") and foil ("cm-holo") versions."""
    series = {}
    for key, entry in pricing.get("tcgplayer", {}).items():
        value = entry.get("marketPrice") or entry.get("midPrice")
        if value:
            series[key] = value
    cm = pricing.get("cardmarket", {})
    for field, key in (("trend", "cm"), ("trend-holo", "cm-holo")):
        if cm.get(field):
            series[key] = cm[field]
    return series


def previous_history(set_id):
    """The set's published history; None for a set that has none yet. Any other failure stops the
    build: publishing without it would lose the history."""
    try:
        req = urllib.request.Request(f"{SITE}/history/{set_id}.json", headers={"User-Agent": "p-dex-data"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def add_day(history, day, today_series):
    """Appends today's column (or replaces it on a second run the same day), keeping HISTORY_DAYS."""
    history = history or {"days": [], "cards": {}}
    days, cards = history["days"], history["cards"]
    if days and days[-1] == day:
        for values in (v for card in cards.values() for v in card.values()):
            values.pop()
        days.pop()
    days.append(day)
    for card_id, series in today_series.items():
        card = cards.setdefault(card_id, {})
        for key, value in series.items():
            card.setdefault(key, [None] * (len(days) - 1))
    for card_id, card in cards.items():
        for key, values in card.items():
            values.append(round(today_series.get(card_id, {}).get(key), 2) if key in today_series.get(card_id, {}) else None)
    cut = max(0, len(days) - HISTORY_DAYS)
    history["days"] = days[cut:]
    history["cards"] = {cid: {k: v[cut:] for k, v in card.items() if any(x is not None for x in v[cut:])}
                        for cid, card in cards.items()}
    history["cards"] = {cid: card for cid, card in history["cards"].items() if card}
    return history


def value_ago(history, card_id, key, days_ago):
    """The figure from `days_ago` days back (the nearest day on or before, at most 3 days earlier)."""
    days = history["days"]
    target = datetime.strptime(days[-1], "%Y-%m-%d") - timedelta(days=days_ago)
    values = history["cards"].get(card_id, {}).get(key)
    if not values:
        return None
    for i in range(len(days) - 1, -1, -1):
        day = datetime.strptime(days[i], "%Y-%m-%d")
        if day <= target:
            return values[i] if (target - day).days <= 3 else None
    return None


# MARK: Build

def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    root = sys.argv[1]
    started = time.time()

    sets, cards = load_database(root)
    print(f"TCGdex: {len(sets)} sets, {len(cards)} English cards")

    groups = get_json(f"{TCGCSV}/groups")["results"]
    groups_by_id = {g["groupId"]: g for g in groups}

    def group_data(group_id):
        products = get_json(f"{TCGCSV}/{group_id}/products")["results"]
        prices = get_json(f"{TCGCSV}/{group_id}/prices")["results"]
        numbered = []
        for p in products:
            number = next((e["value"] for e in p.get("extendedData", []) if e["name"] == "Number"), None)
            if number:
                numbered.append({"productId": p["productId"], "name": p["name"], "number": number, "groupId": group_id})
        return group_id, numbered, prices

    with ThreadPoolExecutor(6) as pool:
        fetched = list(pool.map(group_data, [g["groupId"] for g in groups]))
    group_products = {gid: products for gid, products, _ in fetched}
    tcg_prices = {}
    for _, _, prices in fetched:
        for row in prices:
            key = (row.get("subTypeName") or "Normal").lower().replace(" ", "-")
            entry = {k: row[k] for k in ("marketPrice", "lowPrice", "midPrice") if row.get(k)}
            if entry:
                tcg_prices.setdefault(row["productId"], {})[key] = entry
    print(f"TCGplayer: {len(groups)} groups, {len(tcg_prices)} products with prices")

    guide = get_json(CARDMARKET_GUIDE)
    cm_prices = {g["idProduct"]: g for g in guide["priceGuides"]}
    cm_products = get_json(CARDMARKET_PRODUCTS)["products"]
    print(f"Cardmarket: {len(cm_prices)} priced products, guide of {guide.get('createdAt')}")

    # The links, built from what each source says (see linking.py), then hand corrections.
    tcg_links = linking.link_tcgplayer(cards, sets, groups, group_products, GROUP_ALIASES, NAME_ONLY_SETS)
    tcg_overrides = linking.apply_overrides(tcg_links, "tcgplayer")

    def usd_of(card_id):
        """The card's base printing on TCGplayer: only to compare the markets while linking."""
        entries = tcg_prices.get(tcg_links.get(card_id, (None,))[0], {})
        values = [e.get("marketPrice") for k, e in entries.items() if "reverse" not in k and e.get("marketPrice")]
        return min(values) if values else None

    def eur_of(product_id):
        g = cm_prices.get(product_id) or {}
        trend, month = g.get("trend"), g.get("avg30") or g.get("avg7")
        if trend and month and (trend < month / 4 or trend > month * 4):
            return g.get("avg7") or g.get("avg30")
        return trend or g.get("avg7") or g.get("avg30")

    cm_links = linking.link_cardmarket(cards, sets, cm_products, eur_of, usd_of)
    cm_overrides = linking.apply_overrides(cm_links, "cardmarket")
    for market, links, overrides in (("TCGplayer", tcg_links, tcg_overrides), ("Cardmarket", cm_links, cm_overrides)):
        methods = {}
        for _, method in links.values():
            methods[method] = methods.get(method, 0) + 1
        print(f"{market} links: {len(links)} ({overrides} by hand) — " + ", ".join(f"{m} {n}" for m, n in sorted(methods.items(), key=lambda kv: -kv[1])))
    changed = [f"{cid} {c['name']}: TCGdex {c.get('tcgdexCardmarket')} → {cm_links.get(cid, (None,))[0]}"
               for cid, c in cards.items() if c.get("tcgdexCardmarket") and c.get("tcgdexCardmarket") != cm_links.get(cid, (None,))[0]]
    print(f"Cardmarket links that differ from TCGdex's: {len(changed)}")

    # Hand-checked links every build must reproduce.
    wrong = linking.check_golden(tcg_links, cm_links)
    if wrong:
        sys.exit("Golden links broken, not publishing:\n  " + "\n  ".join(wrong))
    for card_id, card in cards.items():
        card["tcgplayer"] = tcg_links.get(card_id, (None,))[0]
        card["cardmarket"] = cm_links.get(card_id, (None,))[0]

    holo_products = {cid: e["cardmarketHolo"] for cid, e in linking.load_overrides().items() if e.get("cardmarketHolo")}
    out = {}
    counts = {"tcgplayer": 0, "cardmarket": 0, "any": 0}
    for card_id, card in sorted(cards.items()):
        pricing = {}
        tcg = tcg_prices.get(card.get("tcgplayer"))
        if tcg:
            pricing["tcgplayer"] = tcg
            counts["tcgplayer"] += 1
        cm = cm_prices.get(card.get("cardmarket"))
        if cm:
            fields = {}
            for f in CARDMARKET_FIELDS:
                for key in (f, f + "-holo"):
                    if cm.get(key):
                        fields[key] = cm[key]
            # Older sets list the holo / reverse holo as a product of its own: its figures are the
            # card's "-holo" fields (overrides.json "cardmarketHolo", checked by hand).
            holo = cm_prices.get(holo_products.get(card_id)) or {}
            for f in CARDMARKET_FIELDS:
                if holo.get(f):
                    fields[f + "-holo"] = holo[f]
            if fields:
                pricing["cardmarket"] = fields
                counts["cardmarket"] += 1
        if pricing:
            out[card_id] = pricing
            counts["any"] += 1

    # The two markets far apart on a card (more than 4× on a card worth 5 or more) are flagged:
    # one of the links, or one market's few sales, is probably off. The app says so on the card.
    flagged = []
    for card_id, pricing in out.items():
        tcg = pricing.get("tcgplayer", {})
        bases = [e.get("marketPrice") for k, e in tcg.items() if "reverse" not in k and not k.startswith("1st")]
        cm = pricing.get("cardmarket", {})
        trend, month = cm.get("trend"), cm.get("avg30") or cm.get("avg7")
        eur = cm.get("avg7") if trend and month and (trend < month / 4 or trend > month * 4) else trend or cm.get("avg7")
        if len(bases) == 1 and bases[0] and eur and max(bases[0], eur) >= 5 and max(bases[0], eur) > 4 * min(bases[0], eur):
            pricing["check"] = True
            flagged.append({"card": card_id, "name": cards[card_id]["name"], "usd": bases[0], "eur": eur})
    print(f"Flagged (markets more than 4× apart): {len(flagged)}")
    os.makedirs("out", exist_ok=True)
    with open("out/quality.json", "w") as f:
        json.dump({"cardmarketDifferFromTCGdex": changed, "flagged": flagged}, f, indent=1, ensure_ascii=False)
    with open("out/links.json", "w") as f:
        json.dump({cid: {"tcgplayer": tcg_links.get(cid), "cardmarket": cm_links.get(cid)} for cid in sorted(cards)}, f, separators=(",", ":"))

    # History per set: today's column added to what's published; 7- and 30-day figures from it.
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    by_set = {}
    for card_id, pricing in out.items():
        by_set.setdefault(cards[card_id]["set"], {})[card_id] = series_of(pricing)
    with ThreadPoolExecutor(8) as pool:
        previous = dict(zip(by_set, pool.map(previous_history, list(by_set))))
    os.makedirs("out/history", exist_ok=True)
    history_days = 0
    for set_id, series in by_set.items():
        history = add_day(previous[set_id], day, series)
        history_days = max(history_days, len(history["days"]))
        for card_id, pricing in ((cid, out[cid]) for cid in series):
            for key, entry in pricing.get("tcgplayer", {}).items():
                for n in (7, 30):
                    ago = value_ago(history, card_id, key, n)
                    if ago:
                        entry[f"marketPrice{n}d"] = ago
            cm = pricing.get("cardmarket", {})
            for field, key in (("trend", "cm"), ("trend-holo", "cm-holo")):
                for n in (7, 30):
                    ago = value_ago(history, card_id, key, n)
                    if ago and field in cm:
                        cm[f"{field}-{n}d"] = ago
        with open(f"out/history/{set_id}.json", "w") as f:
            json.dump(history, f, separators=(",", ":"))
    print(f"History: {len(by_set)} sets, up to {history_days} days")

    os.makedirs("out", exist_ok=True)
    doc = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sources": {"tcgplayer": "tcgcsv.com", "cardmarket": guide.get("createdAt")},
        "cards": out,
    }
    raw = json.dumps(doc, separators=(",", ":"), sort_keys=True).encode()
    print(f"Priced: {counts['any']}/{len(cards)} cards; TCGplayer {counts['tcgplayer']}, Cardmarket {counts['cardmarket']}")
    print(f"prices.json {len(raw) / 1e6:.1f} MB ({len(gzip.compress(raw)) / 1e6:.1f} MB gzipped), {time.time() - started:.0f} s")
    # A source down or changed: keep yesterday's file rather than publish a thin one.
    if counts["tcgplayer"] < MIN_PRICED or counts["cardmarket"] < MIN_PRICED:
        sys.exit(f"Too few prices (minimum {MIN_PRICED} a market): not publishing")
    open("out/prices.json", "wb").write(raw)


if __name__ == "__main__":
    main()
