"""Generate ElevenLabs music for every gallery artwork missing a music_file.

Usage:
    python scripts/generate_gallery_music.py            # fill in missing only
    python scripts/generate_gallery_music.py --all      # regenerate every track
    python scripts/generate_gallery_music.py --ids 3,5  # only these artwork ids
"""
import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "revision.db"
AUDIO_DIR = BASE_DIR / "static" / "audio"
AUDIO_DIR.mkdir(parents=True, exist_ok=True)

ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "").strip()
MUSIC_SEGMENTS = max(1, min(5, int(os.environ.get("MUSIC_SEGMENTS", "2"))))


def generate_music(prompt: str, out_path: Path, duration: int = 22, segments: int = MUSIC_SEGMENTS) -> bool:
    if not ELEVENLABS_API_KEY or not prompt:
        return False
    each = max(1, min(22, int(duration)))
    url = "https://api.elevenlabs.io/v1/sound-generation"
    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "accept": "audio/mpeg",
        "content-type": "application/json",
    }

    parts = []
    for i in range(segments):
        if i == 0 or segments == 1:
            phrased = prompt
        elif i == segments - 1:
            phrased = prompt + " Resolves and fades to silence."
        else:
            phrased = prompt + " A continuation that develops the same theme."
        body = {
            "text": phrased[:600],
            "duration_seconds": each,
            "prompt_influence": 0.6,
        }
        try:
            r = requests.post(url, headers=headers, json=body, timeout=180)
        except Exception as e:
            print(f"    segment {i+1}: request failed: {e}", flush=True)
            if not parts:
                return False
            break
        if r.status_code != 200:
            print(f"    segment {i+1}: HTTP {r.status_code}: {r.text[:200]}", flush=True)
            if not parts:
                return False
            break
        parts.append(r.content)
        print(f"    segment {i+1}/{segments} ok ({len(r.content):,} bytes)", flush=True)

    if not parts:
        return False
    out_path.write_bytes(b"".join(parts))
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="Regenerate even artworks that already have music")
    ap.add_argument("--ids", default="", help="Comma-separated artwork ids to limit to")
    args = ap.parse_args()

    if not ELEVENLABS_API_KEY:
        print("ELEVENLABS_API_KEY is not set. Add it to .env or env.", file=sys.stderr)
        sys.exit(1)

    only = {int(x) for x in args.ids.split(",") if x.strip()} if args.ids else None

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    where = "" if args.all else "WHERE music_file IS NULL OR music_file = ''"
    rows = conn.execute(
        f"SELECT id, title, artist, music_file, music_prompt FROM artworks {where} ORDER BY id"
    ).fetchall()
    targets = [r for r in rows if (only is None or r["id"] in only) and r["music_prompt"]]

    if not targets:
        print("Nothing to do — every artwork already has a music_file (use --all to redo).")
        return

    print(f"Will generate music for {len(targets)} artwork(s):")
    for r in targets:
        print(f"  - id={r['id']:>3}  {r['title']} — {r['artist']}")
    print()

    ok = 0
    fail = 0
    for r in targets:
        aid = r["id"]
        print(f"[{aid}] {r['title']} — {r['artist']}", flush=True)
        out = AUDIO_DIR / f"music_seed_{aid}.mp3"
        t0 = time.time()
        success = generate_music(r["music_prompt"], out, duration=22, segments=MUSIC_SEGMENTS)
        dt = time.time() - t0
        if success:
            rel = f"audio/{out.name}"
            conn.execute("UPDATE artworks SET music_file = ? WHERE id = ?", (rel, aid))
            conn.commit()
            print(f"    saved {rel} in {dt:.1f}s", flush=True)
            ok += 1
        else:
            print(f"    FAILED after {dt:.1f}s", flush=True)
            fail += 1

    print()
    print(f"Done. ok={ok}  failed={fail}")


if __name__ == "__main__":
    main()
