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
from datetime import datetime, timezone

TCGCSV = "https://tcgcsv.com/tcgplayer/3"
CARDMARKET_GUIDE = "https://downloads.s3.cardmarket.com/productCatalog/priceGuide/price_guide_6.json"
CARDMARKET_PRODUCTS = "https://downloads.s3.cardmarket.com/productCatalog/productList/products_singles_6.json"

# TCGdex set id -> TCGplayer group name, where the names don't match on their own.
GROUP_ALIASES = {
    "2021swsh": "McDonald's 25th Anniversary Promos",
    "mep": "ME: Mega Evolution Promo",
}

CARDMARKET_FIELDS = ["low", "trend", "avg", "avg7", "avg30"]
MIN_PRICED = 15000


def get_json(url, tries=3):
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
                cards[f"{set_id}-{local_id}"] = {"set": set_id, "localId": local_id, "name": card_name, **ids}
    return sets, cards


# MARK: Matching

def words(s):
    return [w for w in re.split(r"[^a-z0-9]+", s.lower()) if w and w != "pokemon"]


def plain_number(s):
    s = (s or "").split("/")[0].strip().lower()
    stripped = s.lstrip("0")
    return stripped or s


def group_for(set_id, set_info, groups_by_id, groups):
    if set_info.get("tcgplayer") in groups_by_id:
        return set_info["tcgplayer"]
    alias = GROUP_ALIASES.get(set_id)
    if alias:
        return next((g["groupId"] for g in groups if g["name"] == alias), None)
    ours = set(words(set_info["name"]))
    fits = [g for g in groups if ours and ours <= set(words(g["name"]))]
    return min(fits, key=lambda g: len(words(g["name"])))["groupId"] if fits else None


def product_for(card, products):
    """The group's product with this card's number, preferring the one named like the card."""
    number = plain_number(card["localId"])
    same = [p for p in products if plain_number(p["number"]) == number]
    if len(same) > 1:
        named = [p for p in same if p["name"].lower().startswith(card["name"].lower())]
        same = named or same
        plain = [p for p in same if "(" not in p["name"]]
        same = plain or same
    return same[0]["productId"] if same else None


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
                numbered.append({"productId": p["productId"], "name": p["name"], "number": number})
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
    by_expansion = {}
    for p in cm_products:
        base = re.sub(r"\s*\[.*\]\s*$", "", p["name"]).strip().lower()
        by_expansion.setdefault((p["idExpansion"], base), []).append(p["idProduct"])
    print(f"Cardmarket: {len(cm_prices)} priced products, guide of {guide.get('createdAt')}")

    # Link the cards TCGdex hasn't.
    linked = {"tcgplayer": 0, "cardmarket": 0}
    set_groups = {sid: group_for(sid, info, groups_by_id, groups) for sid, info in sets.items()}
    for card in cards.values():
        if "tcgplayer" not in card:
            group = set_groups.get(card["set"])
            product = group and product_for(card, group_products.get(group, []))
            if product:
                card["tcgplayer"] = product
                linked["tcgplayer"] += 1
        if "cardmarket" not in card:
            expansion = sets[card["set"]].get("cardmarket")
            found = by_expansion.get((expansion, card["name"].lower()), []) if expansion else []
            if len(found) == 1:
                card["cardmarket"] = found[0]
                linked["cardmarket"] += 1
    print(f"Linked here: {linked['tcgplayer']} TCGplayer, {linked['cardmarket']} Cardmarket")

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
            if fields:
                pricing["cardmarket"] = fields
                counts["cardmarket"] += 1
        if pricing:
            out[card_id] = pricing
            counts["any"] += 1

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
