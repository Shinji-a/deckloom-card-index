# DeckLoom Card Index

Automatically builds the app's card search SQLite database from Scryfall,
with Japanese field checking and automatic text-data enrichment from MTGJSON
and Wisdom Guild / WHISPER. Previously verified official Japanese gallery fields
are retained; normal builds do not crawl the gallery. No fixed per-card corrections or OCR.

See [JAPANESE_COVERAGE.md](JAPANESE_COVERAGE.md) for matching rules, limitations,
reports, publication checks and local commands.

Japanese field enrichment: Scryfall → MTGJSON → cached set lists from
**Wisdom Guild / WHISPER** (https://whisper.wisdom-guild.net/).
日本語補完データは Wisdom Guild / WHISPER より転載。独自の日本語訳を含みます。
日本語名・本文が両方ないカードも補完対象です。片面・両面・分割・出来事を
英語の各面名、収録セット、マナコスト、該当する場合はP/Tで照合し、
取得できた項目だけを保存します。競合・未取得項目は監査レポートに残します。
See [JAPANESE_COVERAGE.md](JAPANESE_COVERAGE.md) for attribution, serial request
pacing, cache reuse, previous-release retention and validation policy.
