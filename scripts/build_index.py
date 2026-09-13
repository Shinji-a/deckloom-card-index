import gzip
import json
import os
import sqlite3
import urllib.request
from datetime import datetime, timezone

import ijson


API_URL = "https://api.scryfall.com/bulk-data"

HEADERS = {
    "User-Agent": "DeckLoom-CardIndex/0.1",
    "Accept": "application/json;q=0.9,*/*;q=0.8",
}


def request(url):
    req = urllib.request.Request(url, headers=HEADERS)
    return urllib.request.urlopen(req)


def get_all_cards_info():
    with request(API_URL) as response:
        data = json.load(response)

    for item in data["data"]:
        if item["type"] == "all_cards":
            return item

    raise RuntimeError("Scryfall all_cards bulk data not found")


def japanese_name(card):
    # 通常カード
    if card.get("printed_name"):
        return card["printed_name"]

    # 両面カード等
    faces = card.get("card_faces") or []
    face_names = [
        face.get("printed_name")
        for face in faces
        if face.get("printed_name")
    ]

    if face_names:
        return " // ".join(face_names)

    return None


def japanese_type(card):
    if card.get("printed_type_line"):
        return card["printed_type_line"]

    faces = card.get("card_faces") or []
    types = [
        face.get("printed_type_line")
        for face in faces
        if face.get("printed_type_line")
    ]

    return " // ".join(types) if types else None


def create_database(download_uri, bulk_updated_at):
    os.makedirs("dist", exist_ok=True)

    db_path = "dist/deckloom-card-index.sqlite"

    if os.path.exists(db_path):
        os.remove(db_path)

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE cards (
            oracle_id TEXT PRIMARY KEY,
            scryfall_id TEXT NOT NULL,
            english_name TEXT NOT NULL,
            japanese_name TEXT,
            type_line TEXT,
            mana_value REAL,
            color_identity TEXT,
            set_code TEXT,
            collector_number TEXT,
            released_at TEXT
        )
    """)

    cur.execute("""
        CREATE INDEX idx_cards_english_name
        ON cards(english_name COLLATE NOCASE)
    """)

    cur.execute("""
        CREATE INDEX idx_cards_japanese_name
        ON cards(japanese_name)
    """)

    count = 0

    print("Downloading and streaming Scryfall all_cards...")

    with request(download_uri) as response:
        cards = ijson.items(response, "item")

        for card in cards:
            if card.get("lang") != "ja":
                continue

            oracle_id = card.get("oracle_id")
            if not oracle_id:
                continue

            jp_name = japanese_name(card)

            if not jp_name:
                continue

            color_identity = "".join(card.get("color_identity") or [])

            # 同じOracleカードに日本語版が複数ある場合、
            # released_atが新しい印刷を代表として残す
            cur.execute("""
                INSERT INTO cards (
                    oracle_id,
                    scryfall_id,
                    english_name,
                    japanese_name,
                    type_line,
                    mana_value,
                    color_identity,
                    set_code,
                    collector_number,
                    released_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)

                ON CONFLICT(oracle_id) DO UPDATE SET
                    scryfall_id = excluded.scryfall_id,
                    english_name = excluded.english_name,
                    japanese_name = excluded.japanese_name,
                    type_line = excluded.type_line,
                    mana_value = excluded.mana_value,
                    color_identity = excluded.color_identity,
                    set_code = excluded.set_code,
                    collector_number = excluded.collector_number,
                    released_at = excluded.released_at

                WHERE excluded.released_at > cards.released_at
            """, (
                oracle_id,
                card["id"],
                card["name"],
                jp_name,
                japanese_type(card),
                card.get("cmc"),
                color_identity,
                card.get("set"),
                card.get("collector_number"),
                card.get("released_at"),
            ))

            count += 1

            if count % 5000 == 0:
                conn.commit()
                print(f"Processed {count} Japanese printings...")

    conn.commit()

    unique_count = cur.execute(
        "SELECT COUNT(*) FROM cards"
    ).fetchone()[0]

    conn.close()

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scryfall_bulk_updated_at": bulk_updated_at,
        "japanese_printings_processed": count,
        "unique_cards": unique_count,
        "database": "deckloom-card-index.sqlite",
        "schema_version": 1,
    }

    with open(
        "dist/manifest.json",
        "w",
        encoding="utf-8"
    ) as f:
        json.dump(
            manifest,
            f,
            ensure_ascii=False,
            indent=2
        )

    # SQLiteもgzip版を作っておく
    with open(db_path, "rb") as src:
        with gzip.open(
            db_path + ".gz",
            "wb",
            compresslevel=9
        ) as dst:
            while True:
                chunk = src.read(1024 * 1024)

                if not chunk:
                    break

                dst.write(chunk)

    print()
    print("Done!")
    print(f"Japanese printings: {count}")
    print(f"Unique Japanese cards: {unique_count}")


def main():
    bulk = get_all_cards_info()

    print("Scryfall all_cards:")
    print("Updated:", bulk["updated_at"])
    print("Size:", bulk.get("size"))

    create_database(
        bulk["download_uri"],
        bulk["updated_at"]
    )


if __name__ == "__main__":
    main()
