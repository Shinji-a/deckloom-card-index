# DeckLoom Card Index

Automatically builds the app's card search SQLite database from Scryfall,
with Japanese field checking and automatic text-data enrichment from MTGJSON
and the official Japanese card gallery. No fixed per-card corrections or OCR.

See [JAPANESE_COVERAGE.md](JAPANESE_COVERAGE.md) for matching rules, limitations,
reports, publication checks and local commands.

Japanese field enrichment: Scryfall → MTGJSON → cached set lists from
**Wisdom Guild / WHISPER** (https://whisper.wisdom-guild.net/).
日本語補完データは Wisdom Guild / WHISPER より転載。独自の日本語訳を含みます。
See [JAPANESE_COVERAGE.md](JAPANESE_COVERAGE.md) for attribution, serial request
pacing, cache reuse, previous-release retention and validation policy.
