#!/usr/bin/env python3
import argparse, hashlib, json, zipfile
from pathlib import Path

def sha256_file(path):
    h=hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda:f.read(1024*1024),b""): h.update(chunk)
    return h.hexdigest()

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--previous",default="dist/previous-image-manifest.json")
    ap.add_argument("--current",default="dist/image-pack/image-manifest.json")
    ap.add_argument("--images",default="dist/image-pack/images")
    ap.add_argument("--output-dir",default="dist/image-pack")
    a=ap.parse_args()
    prev_path=Path(a.previous); cur_path=Path(a.current); images=Path(a.images); out=Path(a.output_dir)
    current=json.loads(cur_path.read_text(encoding="utf-8"))
    previous=json.loads(prev_path.read_text(encoding="utf-8")) if prev_path.exists() else {"entries":{},"archive_sha256":""}
    old=previous.get("entries") or {}; new=current.get("entries") or {}
    changed=[p for p,v in new.items() if p not in old or old[p].get("sha256")!=v.get("sha256")]
    removed=sorted(set(old)-set(new))
    delta_zip=out/"deckloom-small-images-delta.zip"
    with zipfile.ZipFile(delta_zip,"w",compression=zipfile.ZIP_STORED) as z:
        for rel in sorted(changed):
            src=images/rel
            if not src.exists(): raise RuntimeError(f"Missing staged image: {rel}")
            z.write(src,rel)
    manifest={
      "schema_version":1,
      "from_archive_sha256":previous.get("archive_sha256",""),
      "to_archive_sha256":current.get("archive_sha256",""),
      "changed_count":len(changed),
      "removed_count":len(removed),
      "archive":delta_zip.name,
      "archive_bytes":delta_zip.stat().st_size,
      "archive_sha256":sha256_file(delta_zip),
      "changed_paths":sorted(changed),
      "removed_paths":removed,
    }
    (out/"delta-manifest.json").write_text(json.dumps(manifest,ensure_ascii=False,separators=(",",":")),encoding="utf-8")
    print(f"delta changed={len(changed):,} removed={len(removed):,} bytes={manifest['archive_bytes']:,}")

if __name__=="__main__": main()
