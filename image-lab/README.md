# DeckLoom Image Lab

Disposable Android visual-QA app for DeckLoom card-image experiments.

## Current test

- Import the ZIP artifact produced by **Measure 360px WebP card images**.
- The app extracts only `.webp` files.
- Toggle **1列 / 2列** while scrolling the same 500-card sample.
- Reopening the app keeps the last imported sample in app-private storage.

The goal is not feature completeness. Judge:
1. Can card rules text be read comfortably in 2-column portrait mode?
2. Is 1-column browsing comfortable for deck review?
3. Do thin text, mana symbols and unusual frames look visibly damaged?
4. Is the quality good enough to justify the estimated full-pack size?

The current 360px q80 run averaged ~30.7 KB/image and estimates ~1.19 GiB for 41,579 images.
