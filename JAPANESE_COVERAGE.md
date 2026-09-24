# Automatic Japanese field enrichment

The builder downloads Scryfall's all-cards JSONL and MTGJSON AtomicCards, then
checks missing or suspect Japanese names, type lines and rules per Oracle ID
and per face. There are **no per-card fixed supplements, image downloads, OCR,
or machine translation**. The old card and printing override JSON files and
their loaders have been removed. General UI vocabulary dictionaries are unchanged.

## Source matching

1. Keep Scryfall's existing usable Japanese printing selection. A missing field
   may be filled from another Japanese printing of the same Oracle ID and exact
   English face name only when all usable candidate values agree.
2. MTGJSON candidates require the same Scryfall Oracle ID, layout and English
   face name. `foreignData.language == Japanese` alone is not sufficient: English
   values, placeholders and suspected keyword-only truncations are rejected.
   Multiple different usable values are reported as conflicts and not guessed.
3. Discover Japanese official gallery product URLs from its current index.
   English Scryfall Multiverse IDs must appear as links in the matching set's
   gallery. For modern cards without Multiverse IDs, require a unique exact
   existing Japanese name in that set plus matching English rules on every face
   (only whitespace, punctuation and reminder text normalization). Never infer
   a name from list position, image filename, numeric proximity or fuzzy search.
   Parse `.card-info h2`, `p.type` and `p.text`, preserving inline mana symbols.
   Unknown symbols, ambiguous text or mismatched face counts are not adopted.
4. Meld components and results remain separate Oracle IDs. A component's gallery
   result block is not treated as its back face. The matching block must be
   uniquely identified by its English rules; ambiguous matches are deferred.

Existing usable values win. Only missing or suspect fields are candidates for
replacement. Canonical English Oracle, printing/image selection, legality and
other gameplay columns are not changed by enrichment. Real Japanese Scryfall
printing IDs are not fabricated for data obtained elsewhere.

MTGJSON and Scryfall may have the same underlying omissions; agreement is not
proof of independently verified completeness. Japanese printed text also need
not be identical in structure to current English Oracle. Detection heuristics
are screening, not a semantic translation verifier. Combined `//ADV//` text is
preserved and explicitly marked unverified, never counted as proven complete.

## Autonomous runs

Main-branch code updates, manual runs on main, and the daily schedule generate
and validate the database before publishing. Pull requests also run full generation
and save artifacts, without publishing. Release runs are serialized. Fresh source
failure or MTGJSON checksum mismatch stops publication; it does not silently
publish a reduced-source DB. Previous release assets remain until validation
passes. The manifest is uploaded after the database assets.

Downloaded source bytes (not authored corrections) are cached in Actions.
MTGJSON is refreshed daily, official HTML within seven days. HTML requests are
rate-limited with at most four workers. `DECKLOOM_GALLERY_BUDGET` defaults to 400
uncached detail pages per run. Cached pages do not consume that budget; deferred
cards are recorded and can be processed in following runs. At most two linked
editions are probed per card per run. This budget bounds work; it is not a claim
that all possible source pages have been exhausted.

## Reports and validation

- `japanese-coverage.json`: **pre-enrichment Scryfall printing** screening counts.
  Its legacy `complete` label means expected fields pass language screening,
  not that all abilities have been verified. `scope` makes this explicit.
- `japanese-enrichment.json`: accepted fields with source URL/ID/hash, conflicts,
  unresolved per-face fields, unverified aggregates, parser rejections, deferred
  cards, downloaded source hashes and summary counts.
- `manifest.json`: source version, enrichment summary and artifact hashes.
- `python scripts/validate_index.py dist`: validates actual SQLite integrity,
  row count, SQLite/gzip identity, manifest hashes, absence of known placeholders
  and allowed enrichment sources before release.

Offline regression tests exercise wrong identity/layout/face, conflicting
translations, English values labeled Japanese, placeholder/keyword-only text,
source outage/checksum failure, aggregate Adventure preservation, mana symbol
parsing and separate meld identities. They do not prove every card's Japanese
rules complete. Missing translations remain missing and the app can fall back
to English; failures are not hidden by treating them as successful repairs.

## Running

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python scripts/build_index.py
python scripts/validate_index.py dist
```

No Android code or schema version change is required. On the existing app,
update/check the search DB after the new release has been published.
