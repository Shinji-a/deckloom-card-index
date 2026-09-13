import gzip
import io
import json
import os
import sqlite3
import urllib.request
from datetime import datetime, timezone


API_URL = "https://api.scryfall.com/bulk-data"

HEADERS = {
    "User-Agent": "DeckLoom-CardIndex/0.2",
    "Accept": "application/json;q=0.9,*/*;q=0.8",
}


def request(url):
    req = urllib.request.Request(url, headers=HEADERS)
    return urllib.request.urlopen(req, timeout=120)


def get_all_cards_info():
    with request(API_URL) as response:
        data = json.load(response)

    for item in data["data"]:
        if item["type"] == "all_cards":
            return item

    raise RuntimeError("Scryfall all_cards bulk data not found")


def japanese_name(card):
    if card.get("printed_name"):
        return card["printed_name"]

    faces = card.get("card_faces") or []
    names = [
        face.get("printed_name")
        for face in faces
        if face.get("printed_name")
    ]

    if names:
        return " // ".join(names)

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

    if types:
        return " // ".join(types)

    return None


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

    processed = 0
    japanese_printings = 0

    print("Downloading gzipped JSONL from Scryfall...")
    print(download_uri)

    with request(download_uri) as response:
        # Scryfallの現在のBulkは .jsonl.gz
        with gzip.GzipFile(fileobj=response) as gz:
            with io.TextIOWrapper(gz, encoding="utf-8") as text_stream:
                for line in text_stream:
                    line = line.strip()

                    if not line:
                        continue

                    processed += 1

                    card = json.loads(line)

                    if card.get("lang") != "ja":
                        continue

                    oracle_id = card.get("oracle_id")
                    if not oracle_id:
                        continue

                    jp_name = japanese_name(card)

                    if not jp_name:
                        continue

                    japanese_printings += 1

                    color_identity = "".join(
                        card.get("color_identity") or []
                    )

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

                        WHERE
                            cards.released_at IS NULL
                            OR excluded.released_at > cards.released_at
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

                    if japanese_printings % 5000 == 0:
                        conn.commit()
                        print(
                            f"Japanese printings processed: "
                            f"{japanese_printings}"
                        )

    conn.commit()

    unique_count = cur.execute(
        "SELECT COUNT(*) FROM cards"
    ).fetchone()[0]

    conn.close()

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scryfall_bulk_updated_at": bulk_updated_at,
        "total_printings_processed": processed,
        "japanese_printings_processed": japanese_printings,
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
    print(f"Total printings: {processed}")
    print(f"Japanese printings: {japanese_printings}")
    print(f"Unique Japanese cards: {unique_count}")


def main():
    bulk = get_all_cards_info()

    print("Scryfall all_cards")
    print("Updated:", bulk["updated_at"])

    download_uri = bulk.get("jsonl_download_uri")

    if not download_uri:
        raise RuntimeError(
            "jsonl_download_uri was not found in "
            "Scryfall bulk-data response"
        )

    print(
        "Compressed size:",
        bulk.get("compressed_size", "unknown")
    )

    create_database(
        download_uri,
        bulk["updated_at"]
    )


if __name__ == "__main__":
    main()
