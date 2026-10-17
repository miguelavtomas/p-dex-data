# P-Dex data

The daily price file the P-Dex app reads, built by `.github/workflows/daily.yml`.

Sources: [TCGdex](https://github.com/tcgdex/cards-database) for card and product ids,
[tcgcsv.com](https://tcgcsv.com) for TCGplayer prices, Cardmarket's public price guide.
The file is signed (Ed25519); the app ignores one whose signature doesn't verify.
