# P-Dex data

The daily price file the P-Dex app reads, built by `.github/workflows/daily.yml`.

Sources: [TCGdex](https://github.com/tcgdex/cards-database) for card and product ids,
[tcgcsv.com](https://tcgcsv.com) for TCGplayer prices, Cardmarket's public price guide.
Files are signed (Ed25519, `<file>.sig`); the app ignores one whose signature doesn't verify.

- `prices.json`: every card's figures today, plus `marketPrice7d`/`marketPrice30d` (TCGplayer) and
  `trend-7d`/`trend-30d` (Cardmarket) when the history goes back that far.
- `history/<set>.json`: one column a day (up to 400): TCGplayer market price per printing, and
  Cardmarket trend as `cm` / `cm-holo`. Each run adds to the published copy; history started 2026-10-19
  (tcgcsv's archive is offline).
