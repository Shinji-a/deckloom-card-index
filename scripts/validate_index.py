"""Release gate: validate the actual generated artifact, not only fixture behavior."""
import gzip
import hashlib
import json
import os
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
            if field['source']['kind'] not in ('scryfall_other_printing', 'mtgjson', 'mtg_jp_html', 'wisdom_guild', 'previous_release'):
                raise ValueError('Unexpected supplement source')
            if field['source']['kind'] == 'wisdom_guild':
                from urllib.parse import urlsplit
                source = field['source']
                if (urlsplit(source['url']).hostname != 'whisper.wisdom-guild.net'
                        or not source.get('attribution') or not source.get('sha256')
                        or source.get('official_printed_text') is not False):
                    raise ValueError('Incomplete WHISPER attribution')
    if report.get('whisper'):
        w = report['whisper']
        if w['max_parallel_requests'] != 1 or w['minimum_interval_seconds'] < 5 or w['requests'] > 50:
            raise ValueError('WHISPER access policy violated')
    if not (directory / 'SOURCES.txt').exists() or 'Wisdom Guild' not in (directory / 'SOURCES.txt').read_text():
        raise ValueError('Source attribution missing')
    cache = directory / manifest['whisper_cache']
    if hashlib.sha256(cache.read_bytes()).hexdigest() != manifest['whisper_cache_sha256']:
        raise ValueError('WHISPER cache hash mismatch')
    previous_dir = os.environ.get('DECKLOOM_PREVIOUS_DIR')
    if report.get('previous_release', {}).get('enabled') and not previous_dir:
        raise ValueError('Previous release comparison requires DECKLOOM_PREVIOUS_DIR')
    if previous_dir:
        if __package__:
            from . import build_index as helpers
        else:
            import build_index as helpers
        old = sqlite3.connect(f'file:{Path(previous_dir).resolve() / "index.sqlite"}?mode=ro', uri=True)
        old.row_factory = sqlite3.Row
        conn.row_factory = sqlite3.Row
        current = {r['oracle_id']: dict(r) for r in conn.execute('SELECT * FROM cards')}
        checked = 0
        for previous in old.execute('SELECT * FROM cards'):
            prior = dict(previous); row = current.get(prior['oracle_id'])
            if not row or row['layout'] != prior['layout'] or row['english_name'] != prior['english_name']:
                continue
            faces = {f['name']: f for f in e.row_faces(row)}
            for face in e.row_faces(prior):
                new = faces.get(face['name'])
                if not new:
                    continue
                for field in e.FIELDS:
                    if field == 'printed_text' and e.normalized(face.get('oracle_text')) != e.normalized(new.get('oracle_text')):
                        continue
                    if field == 'printed_type_line' and face.get('type_line') != new.get('type_line'):
                        continue
                    if not e.content_issue(face.get(field), field, face.get('oracle_text'), helpers):
                        checked += 1
                        if e.content_issue(new.get(field), field, new.get('oracle_text'), helpers):
                            raise ValueError('Japanese field lost from previous release: ' + row['english_name'] + ' ' + field)
        old.close()
        print('Previous valid Japanese fields retained/replaced:', checked)
    conn.close()
    print('Release validation passed:', count, 'cards;', report['summary'])


if __name__ == '__main__':
    validate(sys.argv[1] if len(sys.argv) > 1 else 'dist')
