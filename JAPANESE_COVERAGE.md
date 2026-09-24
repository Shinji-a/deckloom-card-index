# Japanese localization selection and coverage

## Problem

Ranking Japanese printings only by paper availability and release date lets a
newer incomplete printing replace a usable older translation. Nonempty fields
are insufficient: current EOE/EOC `lang=ja` records can contain English
`printed_text`, even with a Japanese name. Commander legality is independent of
this selection problem.

## Selection and language screening

- Rank completeness, usable text, number of usable fields, then fewer suspected
  untranslated fields. Only then compare paper availability, release date and ID.
- Screen printed names, types and text for English-only content. For mixed text,
  also flag whole Latin paragraphs (three or more words, or a matching Oracle
  line) without Japanese. Latin names embedded in Japanese remain usable.
- Mana symbols, numbers, explicitly textless cards and basic lands with only
  intrinsic mana text are legitimate exceptions. Missing Oracle text is unknown,
  not proof that a translation is unnecessary.
- Keep localized fields and provenance from one printing. Do not combine
  unrelated editions. Preferred card/image selection remains independent;
  Japanese token images stay paired with their Japanese printing.
- Preserve usable root aggregate fields and partial face translations. Some
  Adventure records put both texts, including `//ADV//`, in the first face.
  A missing second face must not erase that Japanese text. The coverage report
  still marks missing face fields as incomplete; retained text alone is not
  evidence that every face is translated. Full English Oracle remains available.
- English-only printed values become NULL in Japanese display fields. This lets
  the existing app use its English fallback instead of treating English as a
  completed Japanese translation. No automatic translation is performed.
- Keep schema version 4; new manifest fields are additive.

These are conservative heuristics, not translation validation. They can miss
short English fragments or English mixed into a Japanese sentence. Aggregate
text can contain translations for a face whose own fields are absent; coverage
then deliberately remains conservative. It is not a count of broken UI screens.

## Reviewed image-based correction

`data/japanese-printing-overrides.json` contains the inspected Japanese EOE #85
Uthros Scanship printing. Its type and text were transcribed from that printing's
Japanese image. The record includes the exact printing/Oracle IDs, set, collector
number, review date, image URL and SHA-256 of the inspected image.

Overrides fill only missing or language-screened fields. Valid upstream Japanese
wins if Scryfall fixes its data later. Identity mismatches and invalid provenance
fail the build. Each build records applied fields and unobserved override IDs.
The loader validates provenance metadata; it does not download or recheck the
image on every build. Adding another correction requires reviewing its source.
This currently supports single-face records only.

## Audit output

`dist/japanese-coverage.json` reports cards and tokens separately:

- `without_japanese_printing`: no Japanese printing observed in this bulk input.
- `complete`: all expected per-face Japanese fields passed screening.
- `incomplete`: selected printing still has `missing` fields or `untranslated`
  fields, with field paths and reasons (`english_only` / `english_paragraph`).
- `recovered_from_incomplete_printing`: a different printing was selected to
  recover content; `fully_resolved` distinguishes complete from partial recovery.
- Top-level `reviewed_overrides`: observed image-based corrections and IDs not
  observed in this input. `applied_fields=[]` means upstream Japanese was usable.

Manifest counts summarize coverage and applied reviewed corrections. The release
workflow runs regression tests before building and uploads the audit with the DB.

## Verification and release

```sh
python -m unittest discover -s tests -v
python scripts/build_index.py
```

29 offline tests exercise the real gzipped-JSONL-to-SQLite builder, including
selection order, English/mixed content, symbols, level ranges, intrinsic mana, faces, partial
Adventures, root aggregates, tokens, provenance, and upstream corrections.
Each fixture database passes `PRAGMA integrity_check`.

The 2026-09-17T09:18:30.465+00:00 bulk snapshot was rebuilt locally:
542,479 printings, 37,776 cards and 1,094 tokens. SQLite integrity and both
manifest hashes passed. Compared with the same-day published DB:

- 199 cards gained text containing Japanese; zero previously Japanese-containing
  card texts became empty/non-Japanese under that same character-screen check.
  This metric does not guarantee a complete translation of every face.
- All non-Japanese card columns (including Oracle, legality and image choice),
  non-Japanese token columns, set memberships and alias tables are unchanged.
- Card coverage: 23,845 complete, 6,916 incomplete, 7,015 without a Japanese
  printing in the input. Selection recovered content from a different printing
  in 1,226 cases, including partial recoveries.
- Uthros Scanship uses the reviewed EOE image correction. Shivan Reef recovers
  from TDC and Soul of Windgrace from DMU. Other missing EOE translations remain
  explicit gaps; this does not claim that EOE as a whole is repaired.

Local regeneration does not update the published release. After merging this PR,
run **Update DeckLoom Card Index** on main (or wait for its daily schedule), then
update the search DB in DeckLoom. Android Studio Run alone will not refresh it.

`printed_text` is a particular printing's wording, not a translation of current
Oracle. Keep English Oracle available. Existing saved deck entries and the app's
network-printing selector have independent fallback paths; updating this search
DB does not automatically rewrite saved entries. UI labels for incomplete
Japanese and those app-side paths are outside this generator change.

## 2026-09-24: face identity and reviewed missing-card supplements

The published 2026-09-24 DB and Scryfall bulk were inspected, rather than treating
all bilingual display failures as one bug:

- Virtue of Strength's sole Japanese printing has an English main-face
  `printed_name`, but a Japanese Adventure name. Removing unusable names and
  joining the remaining faces promoted the Adventure to the card's identity.
- Argoth, Phyrexian Dragon Engine, The Mightstone and Weakstone, and Heliod,
  the Radiant Dawn have no Japanese printing in this bulk snapshot. Selection
  ranking alone cannot restore translations that are absent from the input.
- Glacier Godmaw's Japanese printing has English printed rules. Aftermath
  Analyst already has Japanese rules in the published DB; the Android app's
  independent network path can overwrite saved rules with English.

`japanese_name` now preserves face order with English fallback for a missing
Japanese face name. Legacy split cards storing both names on the first face
remain supported, without appending the second name twice. Per-face Japanese
names are normalized to remove reading annotations.

`data/japanese-card-supplements.json` supplies reviewed missing fields by
Oracle ID, expected English name, layout and exact English face name. It keeps
meld components and results separate. `scripts/japanese_supplements.py` rejects
identity mismatches and invalid provenance. Valid upstream Japanese fields win.
No fake Scryfall printing IDs or Japanese image IDs are created. English Oracle,
legality, mana values, selected images, and actual printing provenance remain
unchanged. Each applied field's source appears in `japanese-coverage.json`.

The source records distinguish:

- `printed_card_image`: an inspected Japanese image, with URL and SHA-256.
  These cover the reported cards, both Heliod faces, the BRO meld groups, and
  missing Virtue names. The three already translated meld partners are left
  unchanged by the fill-only policy.
- `official_card_gallery`: **names only**, matched by the Japanese printing's
  Multiverse ID to the Japanese official gallery's image entry/caption. URL,
  downloaded page SHA-256, Multiverse ID and Scryfall printing ID are recorded.
  These recover 63 further main-face names from WOE, CLB and TDM. Gallery
  metadata is never used to invent rules text.

The original `cards.complete/incomplete/without_japanese_printing` counters
continue to describe upstream printing coverage. The separate
`reviewed_card_supplements` section and manifest's
`reviewed_japanese_cards_supplemented` describe the supplemental output.
Neither count claims complete Japanese coverage across Magic. In the inspected
snapshot, 88 records had only a later face's Japanese name; all 88 main-face names
are recovered from verified sources in this change. Beyond the official-gallery
matches, remaining names were transcribed from the Japanese printing images.
This fixes the observed name pattern, not every missing translation in the DB.

Android v0.48.1 is the companion fix: it reads per-face data, falls back per
face, prefers index Japanese during image resolution, and refreshes saved deck
localizations by matching Oracle ID after a successful DB check. Updating the
DB alone cannot repair every previously saved/network-overwritten field.

Apply: merge the DB PR, run **Update DeckLoom Card Index** on main (or wait for
its schedule), confirm success, install/run Android v0.48.1, then update/check
the search DB. Android Studio Run does not publish or download the new DB by
itself. UI redesign/drag behavior remains a separate task.
