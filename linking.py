"""Which TCGplayer and Cardmarket product each TCGdex card is.

The links are built here, from what each source says about a card, and each gets the method that
made it and a confidence. TCGdex's own links are one vote among the others, never taken on trust:
they're wrong for hundreds of cards (Scorbunny → Litten, Mewtwo → Mew, secret rares → the regular
card).

TCGplayer (tcgcsv) gives each product its set (group), number and name, so a card is found by
set → number → name.

Cardmarket's public files give a product's expansion and name (with its attacks), but no card
number. A card is found by expansion → name → attacks; cards still sharing a name and attacks
(a secret rare and the regular card, a reprint in the same set) are told apart by votes:
  - order: the set's cards by number against the products in the order Cardmarket added them,
  - TCGdex's link,
  - price: the product whose price is clearly the closest to the card's TCGplayer price.
Two votes for the same product make the link, or TCGdex's alone when no other vote goes elsewhere;
otherwise the card gets no Cardmarket price rather than a guess.

overrides.json corrects a link by hand (it always wins); golden.json lists hand-checked links
that every build must reproduce.
"""

import difflib
import json
import math
import os
import re
import unicodedata

# MARK: Names

def words(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()  # Pokémon → Pokemon
    return [w for w in re.split(r"[^a-z0-9]+", s.lower()) if w and w != "pokemon"]


def same_name(ours, theirs):
    """TCGplayer's names carry extras ("Turtwig - 4/25", "Pikachu (Surfing)"): every word of one
    name in the other."""
    a, b = set(words(ours)), set(words(theirs))
    return bool(a) and bool(b) and (a <= b or b <= a)


def plain_number(s):
    s = (s or "").split("/")[0].strip().lower()
    stripped = s.lstrip("0")
    return stripped or s


ENERGY_CODES = {"G": "grass", "R": "fire", "W": "water", "L": "lightning", "P": "psychic",
                "F": "fighting", "D": "darkness", "M": "metal", "Y": "fairy", "C": "colorless"}


def canonical(name):
    """The same card's name however it's written: symbols spelled out (☆ = Gold Star, ♀ = [F],
    α = alpha), energy codes ("Rocky [F] Energy"), and the words one site adds ("δ Delta Species",
    "Basic Grass Energy")."""
    name = (name.replace("☆", " star ").replace("Gold Star", " star ").replace("♀", " F").replace("♂", " M")
            .replace("α", " alpha").replace("β", " beta").replace("Ω", " omega").replace("δ", " delta "))
    name = re.sub(r"\b([GRWLPFDMYC])\s+Energy\b", lambda m: ENERGY_CODES[m.group(1)] + " Energy", name)
    extra = {"delta", "species", "basic"}
    return " ".join(w for w in words(name) if w not in extra)


def cardmarket_name(product):
    """"Mew [Clairvoyance | Psychic]" → "Mew"; "Floatzel Lv.29" → "Floatzel"; "Muk (V.2)" → "Muk";
    "Professor's Research - Professor Sada" → "Professor's Research"."""
    name = re.sub(r"\s*\[[^\[\]]*\]\s*$", "", product["name"])  # the attacks, last
    name = re.sub(r"\[([^\[\]|]{1,3})\]", r"\1", name)            # "Unown [M]", "Chatot [G]", "[FB]"
    name = re.sub(r"\s+-\s+.*$", "", name)
    name = re.sub(r"\s+Lv\.?\s*(\d+|X)\b", lambda m: " LV.X" if m.group(1) == "X" else "", name)
    return re.sub(r"\s*\(V\.\d+\)", "", name).strip()


def cardmarket_attacks(product):
    """The attack names Cardmarket puts in brackets after the card's name."""
    m = re.search(r"\[([^\[\]]*)\]\s*$", product["name"])
    if not m or ("|" not in m.group(1) and len(m.group(1)) <= 3):
        return ()
    return tuple(canonical(a) for a in m.group(1).split("|"))


def same_cardmarket_name(ours, theirs):
    """Cardmarket names a card exactly, so the names must be the same, give or take a spelling
    ("Impostor" / "Imposter") — not one inside the other ("Rayquaza" isn't "Rayquaza EX")."""
    a, b = canonical(ours), canonical(theirs)
    if a.replace(" ", "") == b.replace(" ", ""):  # "M Tyranitar EX" / "MTyranitar EX"
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.85 and len(a.split()) == len(b.split())


def number_key(card):
    """Collector order: "TG07" after "TG06", "100" after "99", "74a" before "74b"."""
    m = re.match(r"(\D*)(\d+)(.*)", card["localId"])
    return (m.group(1), int(m.group(2)), m.group(3)) if m else (card["localId"], 0, "")


# MARK: TCGplayer

def group_for(set_id, set_info, groups, aliases):
    """The TCGplayer group of a TCGdex set: TCGdex's link, an alias, or the closest name."""
    by_id = {g["groupId"]: g for g in groups}
    if set_info.get("tcgplayer") in by_id:
        return set_info["tcgplayer"]
    if set_id in aliases:
        return next((g["groupId"] for g in groups if g["name"] == aliases[set_id]), None)
    ours = set(words(set_info["name"]))
    fits = [g for g in groups if ours and ours <= set(words(g["name"]))]
    return min(fits, key=lambda g: len(words(g["name"])))["groupId"] if fits else None


def fits_tcgplayer(card, product):
    """The product is this card: same name, and same number when the product has one."""
    if not same_name(card["name"], product["name"]):
        return False
    return product["number"] is None or plain_number(product["number"]) == plain_number(card["localId"])


def link_tcgplayer(cards, sets, groups, group_products, aliases):
    """card id → (product id, method). Set → number → name; TCGdex's link only to choose among
    several equally good products, or when the card's set has no group."""
    products = {p["productId"]: p for ps in group_products.values() for p in ps}
    set_groups = {sid: group_for(sid, info, groups, aliases) for sid, info in sets.items()}
    links = {}
    for card_id, card in cards.items():
        group = set_groups.get(card["set"])
        theirs = card.get("tcgdexTcgplayer")
        candidates = [p for p in group_products.get(group, []) if fits_tcgplayer(card, p)] if group else []
        if len(candidates) > 1:
            # Several with the card's number and name: the plain one, not "(Prerelease)" or "[Staff]".
            plain = [p for p in candidates if "(" not in p["name"] and "[" not in p["name"]]
            candidates = plain or candidates
        if len(candidates) == 1:
            links[card_id] = (candidates[0]["productId"], "set+number+name")
        elif candidates and theirs in {p["productId"] for p in candidates}:
            links[card_id] = (theirs, "set+number+name, TCGdex to choose")
        elif not candidates and theirs in products and fits_tcgplayer(card, products[theirs]):
            links[card_id] = (theirs, "TCGdex, number and name checked")
    return links


# MARK: Cardmarket

def link_cardmarket(cards, sets, cm_products, eur_of, usd_of):
    """card id → (product id, method). Expansion → name → attacks; then votes (see the module notes).
    eur_of(product id) and usd_of(card id) are only used to compare the two markets' prices, never
    shown converted."""
    by_name = {}
    for p in cm_products:
        by_name.setdefault((p["idExpansion"], canonical(cardmarket_name(p))), []).append(p)
    # A set's expansion: TCGdex's, or, when it has none (McDonald's 2021, Trainer Galleries), the one
    # most of TCGdex's card links in the set point to.
    expansion_of_product = {p["idProduct"]: p["idExpansion"] for p in cm_products}
    seen = {}
    for card in cards.values():
        if (e := expansion_of_product.get(card.get("tcgdexCardmarket"))) is not None:
            seen.setdefault(card["set"], {}).setdefault(e, 0)
            seen[card["set"]][e] += 1
    expansions = {sid: info.get("cardmarket") or (max(seen[sid].items(), key=lambda kv: kv[1])[0] if sid in seen else None)
                  for sid, info in sets.items()}
    groups = {}
    for card_id, card in cards.items():
        expansion = expansions.get(card["set"])
        if expansion:
            groups.setdefault((expansion, canonical(card["name"])), []).append(card_id)

    def price_vote(card_id, candidates):
        """The candidate whose price is at least twice as close to the card's TCGplayer price as any other."""
        usd = usd_of(card_id)
        if not usd or usd < 1:
            return None
        scored = sorted((abs(math.log(eur / (usd * 0.9))), p["idProduct"])
                        for p in candidates if (eur := eur_of(p["idProduct"])))
        if len(scored) == 1 or (len(scored) > 1 and scored[1][0] - scored[0][0] > math.log(2)):
            return scored[0][1]
        return None

    links = {}
    for key, card_ids in groups.items():
        products = by_name.get(key, [])
        if not products:
            continue
        # Split by attacks where both sides list them; cards without attacks (trainers, energy)
        # meet the products without them.
        by_attacks = {}
        for card_id in card_ids:
            by_attacks.setdefault(tuple(canonical(a) for a in cards[card_id].get("attacks", [])), ([], []))[0].append(card_id)
        for p in products:
            sig = cardmarket_attacks(p)
            if sig in by_attacks:
                by_attacks[sig][1].append(p)
        unplaced = [p for p in products if not any(p in ps for _, ps in by_attacks.values())]
        for sig, (ids, ps) in by_attacks.items():
            # Cardmarket without attack names for these: every product with the name is a candidate.
            if not ps:
                ps = unplaced if unplaced else products
            ids = sorted(ids, key=lambda i: number_key(cards[i]))
            ps = sorted(ps, key=lambda p: p["idProduct"])
            if len(ids) == 1 and len(ps) == 1:
                links[ids[0]] = (ps[0]["idProduct"], "expansion+name" + ("+attacks" if sig else ""))
                continue
            by_order = dict(zip(ids, (p["idProduct"] for p in ps))) if len(ids) == len(ps) else {}
            for card_id in ids:
                votes = {}
                for source, product in (("order", by_order.get(card_id)),
                                        ("TCGdex", cards[card_id].get("tcgdexCardmarket")),
                                        ("price", price_vote(card_id, ps))):
                    if product in {p["idProduct"] for p in ps}:
                        votes.setdefault(product, []).append(source)
                best = max(votes.items(), key=lambda kv: len(kv[1]), default=None)
                if best and len(best[1]) >= 2:
                    links[card_id] = (best[0], "votes: " + ", ".join(best[1]))
                elif best and best[1] == ["TCGdex"] and len(votes) == 1:
                    # TCGdex's pick among same-name, same-attack products, with nothing against it.
                    links[card_id] = (best[0], "TCGdex, unopposed")
    # One product, one card per set: a product two cards share stays with the better-voted one.
    owners = {}
    for card_id, (product, method) in sorted(links.items(), key=lambda kv: (-kv[1][1].count(","), number_key(cards[kv[0]]))):
        owner = owners.setdefault((cards[card_id]["set"], product), card_id)
        if owner != card_id:
            del links[card_id]
    return links


# MARK: Hand corrections

def apply_overrides(links, market, path="overrides.json"):
    """overrides.json: {"card id": {"tcgplayer": 123 or null, "cardmarket": 456 or null, "why": "…"}}."""
    if not os.path.exists(path):
        return 0
    overrides = json.load(open(path))
    for card_id, entry in overrides.items():
        if market in entry:
            if entry[market] is None:
                links.pop(card_id, None)
            else:
                links[card_id] = (entry[market], "hand-checked")
    return sum(1 for e in overrides.values() if market in e)


def check_golden(tcgplayer, cardmarket, path="golden.json"):
    """golden.json: hand-checked links, {"card id": {"tcgplayer": 123, "cardmarket": 456}}.
    Returns the ones the build got wrong."""
    if not os.path.exists(path):
        return []
    wrong = []
    for card_id, entry in json.load(open(path)).items():
        for market, links in (("tcgplayer", tcgplayer), ("cardmarket", cardmarket)):
            if market in entry and links.get(card_id, (None,))[0] != entry[market]:
                wrong.append(f"{card_id} {market}: expected {entry[market]}, got {links.get(card_id, (None,))[0]}")
    return wrong
