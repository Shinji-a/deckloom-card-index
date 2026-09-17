# Japanese localization selection and coverage

## Problem

The former Japanese-printing selector ranked only paper availability and release
date. A newer `lang=ja` printing without `printed_name`, `printed_type_line`, or
`printed_text` could overwrite a complete older translation with NULL/blank
values. The same problem affected tokens and individual double-faced card faces.
The input order does not prevent this regression.

## Change

- Rank actual Japanese content completeness before paper preference and recency.
- Check every face. An explicitly empty Oracle text is valid for textless cards;
  a missing Oracle field is unknown and does not establish that text is optional.
- Keep names, types, text, faces, and localization metadata from one printing.
  Never fabricate translations or merge fields from unrelated printings.
- Among equally complete candidates, keep the existing paper/date preferences;
  use the Scryfall ID as a deterministic final tie-breaker.
- Preserve the independent preferred-printing/image selection. Token Japanese
  image URLs remain paired with their Japanese source printing.
- Keep schema version 4. Coverage fields added to the manifest are additive.

## Audit output

Each build writes `dist/japanese-coverage.json`, separately for cards and tokens:

- `without_japanese_printing`: no Japanese printing was seen in this bulk input;
  this does not prove that no Japanese physical printing exists anywhere.
- `complete`: selected printing has all expected Japanese fields.
- `incomplete`: no fully populated Japanese candidate was selected. Each entry
  includes the card ID/name, selected printing, and missing fields/faces.
- `recovered_from_incomplete_printing`: content was selected from a different
  printing because the preferred Japanese printing had missing fields. This can
  include partial recovery; `incomplete` remains the source for unresolved gaps.

The manifest includes counts, and the scheduled release uploads the full report
alongside the database. This makes cross-card gaps inspectable on every build.

## Verification

Run from the repository root with Python 3.11 or later (CI uses 3.13):

```sh
python -m unittest discover -s tests -v
```

Twelve offline integration tests exercise the actual gzipped-JSONL-to-SQLite
builder, including input order, blank fields, DFC faces, tokens, textless cards,
paper/digital preference, same-date determinism, audit categories, and manifest
counts. Each generated SQLite database also passes `PRAGMA integrity_check`.
Before the fix, the new localization assertions failed six times across five
test methods. All twelve test methods pass with this change.

## Scope and remaining checks

This is a general selector fix, not a verified diagnosis of the current release's
Rampaging Baloths row. The live release database and full current Scryfall bulk
were not rebuilt or inspected in this session. Real affected-card counts remain
unknown until the next full build and coverage review.

`printed_text` is the text of a particular printing, not a Japanese translation
of current Oracle. For example, Rampaging Baloths received an Oracle update in
August 2025 removing “you may”; recovering an older printed text does not apply
that erratum. Keep the English Oracle available and label printed translations
appropriately in the app.

Primary source: https://magic.wizards.com/en/news/announcements/edge-of-eternities-update-bulletin

DeckLoom's existing saved deck entries and network-printing selector have their
own fallback paths. Updating this search database does not retroactively rewrite
those saved entries. They require separate app-side review/refresh validation.
