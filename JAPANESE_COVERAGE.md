# Automatic Japanese field enrichment

The scheduled GitHub Actions build uses this priority:

1. Scryfall selected Japanese printing and agreeing alternate Japanese printings.
2. MTGJSON AtomicCards (same Oracle ID, layout and exact English face).
3. Wisdom Guild / WHISPER cached **set card lists**, only for remaining Japanese
   name/rules gaps on cards that already have at least one usable Japanese name
   or rules field. A card missing both entirely is deferred. Partial faces remain
   eligible. Art cards and tokens are excluded from WHISPER enrichment.
4. Retain usable previous-release fields when the higher-priority sources cannot
   fill them. English face identity/layout must match, and rules/type fields
   additionally require unchanged corresponding English fields.

There are no per-card fixed supplements, OCR or machine translation. Official
mtg-jp gallery parsing remains available for explicit diagnostic calls/tests;
normal builds no longer crawl that gallery. Existing verified gallery-derived
fields are retained from the previous release with their original attribution.

## WHISPER access policy (mandatory)

- One in-flight request. A lock covers the entire HTTP response and cache write.
- At least **5 seconds after completion** before another request starts.
- Maximum **50 requests per build**, including the set index. No implicit redirects.
- No automatic retries. An HTTP/network/parser failure stops new WHISPER network
  requests for that run. Already validated cached pages may still be used.
- Index cache: 7 days; set-page cache: 30 days. Good stale cache is retained if a
  refresh fails. Challenge/error pages never replace validated source pages.
- Only serialized main-branch builds make WHISPER network requests. PR builds
  use existing cache offline, so PR/main jobs cannot duplicate WHISPER traffic.
- Cache is shipped as a hash-verified release asset, `whisper-cache.tar.gz`, and
  restored on the next run. It does not depend on the Actions cache retention period.
- The app queries its own database, never WHISPER on each user search.

Requests prefer HTML but also allow a low-priority `*/*` media type. The public
Apache route negotiates an `application/x-httpd-php` handler before returning
HTML; an HTML-only `Accept` incorrectly caused HTTP 406. This is content
negotiation, not an access-control bypass. The identifying DeckLoom User-Agent,
request limits and response-body validation are unchanged. HTTP failures record
status, content type, advertised alternatives and a bounded response excerpt so
configuration problems are distinguishable from source restrictions.

The set index and exact normalized set names discover URLs; no guessed numeric
URLs or individually crawled card pages are used. Set selection prefers cached
pages and then larger coverage of still-missing cards, with stable tie breaking.
The request cap bounds work; it does **not** promise full coverage in one run.

Policy: https://www.wisdom-guild.net/welcome/
Card lists also explicitly permit redistribution and attributed adaptation.
Japanese supplement data is reproduced from **Wisdom Guild / WHISPER**:
https://whisper.wisdom-guild.net/
It includes independently translated Japanese and is not represented as official
printed text. Canonical English Oracle is never replaced.

## Identity, content and attribution

WHISPER candidates require an exact English face name and known set membership,
matching mana cost, and matching power/toughness when present. Names are never
matched fuzzily. Card blocks must include the expected heading/type/illustrator
structure. Only rules paragraphs are extracted; names, type and statistics do not
leak into rules. Known mana/tap symbols are converted to `{G}` / `{T}` notation.
Unsupported/ambiguous structures stay unresolved. Conflicting translations from
variants in one list are reported, not chosen arbitrarily.

Existing usable values win. English-only values mislabeled Japanese, placeholders
and narrow keyword-only truncations are rejected. Empty Oracle fields and basic
lands' intrinsic mana reminders do not count as missing Japanese rules. Combined
`//ADV//` text is preserved but marked unverified. Presence/language screening is
not proof of semantic translation completeness or current-Oracle equivalence.

Attribution is carried in all of:

- `SOURCES.txt` distributed with each release and named by the manifest;
- `japanese-enrichment.json`: per-field source URL, hash, fetched time, matching
  method, stale-cache flag and attribution (including retained original sources);
- SQLite `japanese_field_sources` for supplements, so provenance travels with DB;
- this document and README.

## Autonomous updates and release checks

Daily main-branch builds, main code pushes and manual main runs generate and
validate before publishing. PRs run the same build with WHISPER offline and save
artifacts without publishing. The previous release's SQLite and source cache are
verified against the previous manifest before use. MTGJSON download/hash failure
still stops the build. A WHISPER failure can produce a partial update using saved
sources; it is reported and does not erase previous valid Japanese fields.

Validation checks SQLite integrity, row counts, SQLite/gzip hashes, manifest
consistency, placeholders, known source kinds, WHISPER attribution and request
policy, cache hash and no loss of usable previous Japanese fields with unchanged
English identity/rules. Publication uploads the manifest last. The optional new
provenance table requires no Android application/schema migration.

`japanese-coverage.json` remains explicitly a pre-enrichment Scryfall screening
report. Use `japanese-enrichment.json` for final unresolved fields, deferred cards,
source failures, cache/network counts and accepted supplements.

## Running

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
# Actions first downloads previous release assets into previous/.
python scripts/previous_database.py
DECKLOOM_PREVIOUS_DIR=previous python scripts/build_index.py
DECKLOOM_PREVIOUS_DIR=previous python scripts/validate_index.py dist
```

Set `DECKLOOM_WHISPER_OFFLINE=1` for offline WHISPER use. The pacing/concurrency
constants have no environment-variable override. Existing Android clients only
need to update their search database after publication.
