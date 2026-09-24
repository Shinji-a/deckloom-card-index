"""Release gate: validate the actual generated artifact, not only fixture behavior."""
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

if __package__:
    from . import japanese_enrichment as e
else:
    import japanese_enrichment as e


def validate(directory):
    directory = Path(directory)
    manifest = json.loads((directory / 'manifest.json').read_text())
    report = json.loads((directory / 'japanese-enrichment.json').read_text())
    if report['policy'] != 'automatic-only-no-OCR':
        raise ValueError('Unexpected enrichment policy')
    if report['summary'] != manifest['automatic_japanese_enrichment']:
        raise ValueError('Manifest enrichment counts mismatch')
    for key in ('database', 'database_gzip'):
        p = directory / manifest[key]
        if p.stat().st_size != manifest[key + '_bytes'] or hashlib.file_digest(p.open('rb'), 'sha256').hexdigest() != manifest[key + '_sha256']:
            raise ValueError('Artifact size/hash mismatch: ' + key)
    with gzip.open(directory / manifest['database_gzip'], 'rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != manifest['database_sha256']:
            raise ValueError('Compressed database differs from SQLite')
    conn = sqlite3.connect(f'file:{directory / manifest["database"]}?mode=ro', uri=True)
    if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
        raise ValueError('SQLite integrity_check failed')
    count = conn.execute('SELECT COUNT(*) FROM cards').fetchone()[0]
    if count != manifest['unique_cards'] or count < 1000:
        raise ValueError('Unexpected card count')
    for name, jpname, typ, text, faces in conn.execute('SELECT english_name,japanese_name,japanese_type_line,japanese_text,japanese_faces_json FROM cards'):
        values = [jpname, typ, text]
        for f in json.loads(faces or '[]'):
            values.extend(f.get(key) for key in e.FIELDS)
        if any(e.normalized(value) in e.PLACEHOLDERS for value in values if value):
            raise ValueError('Placeholder in generated DB: ' + name)
    for card in report['applied']:
        for field in card['applied']:
            if field['source']['kind'] not in ('scryfall_other_printing', 'mtgjson', 'mtg_jp_html'):
                raise ValueError('Unexpected supplement source')
    conn.close()
    print('Release validation passed:', count, 'cards;', report['summary'])


if __name__ == '__main__':
    validate(sys.argv[1] if len(sys.argv) > 1 else 'dist')
