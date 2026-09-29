"""Read-only face/field verification of named cards in an actual saved database.

Names select inspection examples only; this script never supplies translations.
"""
import argparse
import json
from pathlib import Path
import sqlite3

if __package__:
    from . import build_index as b, japanese_enrichment as e
else:
    import build_index as b, japanese_enrichment as e


def verify(path, names, require_whisper=False):
    results = []
    with sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        for name in names:
            records = db.execute('SELECT * FROM cards WHERE english_name=?', (name,)).fetchall()
            if len(records) != 1:
                raise ValueError('Expected one canonical card: ' + name)
            row = dict(records[0])
            faces = e.row_faces(row)
            missing = e.issues(faces, b)
            if missing:
                raise ValueError(name + ': ' + json.dumps(missing, ensure_ascii=False))
            sources = {(r['face'], r['field']): json.loads(r['source_json']) for r in db.execute(
                'SELECT * FROM japanese_field_sources WHERE oracle_id=?', (row['oracle_id'],))}
            if require_whisper:
                for face in faces:
                    for field in e.FIELDS:
                        if field == 'printed_text' and not b.japanese_text_required(face):
                            continue
                        source = sources.get((face['name'], field), {})
                        if (source.get('kind') != 'wisdom_guild' or not source.get('sha256')
                                or not source.get('url') or not source.get('attribution')):
                            raise ValueError('Missing WHISPER provenance: ' + face['name'] + ' ' + field)
            results.append({'english_name': name, 'japanese_name': row['japanese_name'],
                            'faces': [{'english_name': f['name'], 'japanese_name': f.get('printed_name'),
                                       'japanese_text_chars': len(f.get('printed_text') or '')} for f in faces],
                            'sources': sorted({s['url'] for s in sources.values() if s.get('url')})})
    return results


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database')
    parser.add_argument('names', nargs='+')
    parser.add_argument('--require-whisper', action='store_true')
    args = parser.parse_args()
    print(json.dumps(verify(args.database, args.names, args.require_whisper), ensure_ascii=False, indent=2))
