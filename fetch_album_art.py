"""
fetch_album_art.py
------------------
Scans your music library for albums missing artwork, searches Apple's
iTunes API, and for each album:

  • Saves a full-resolution archival copy  →  cover.jpg  (3000x3000px)
  • Embeds a compact thumbnail             →  into each music file (600x600px)

Supports: MP3, FLAC, M4A, OGG, OPUS, WMA, WAV, AIFF
Requires: mutagen, Pillow  (install once with:  pip install mutagen requests Pillow)

Usage:
    python fetch_album_art.py "C:\\Users\\You\\Music"

Review mode: before any artwork is saved or embedded, the script shows
you what Apple matched and opens a 300px preview in your browser.
Press Y to accept, S to skip, or Q to quit.

For each album folder the script will:
  • Skip entirely if cover.jpg is already at or above ART_SIZE (3000px).
  • Upgrade cover.jpg and re-embed the thumbnail if the existing file is
    smaller than ART_SIZE — e.g. a 500px cover shipped with a download.
  • Fetch everything fresh if neither cover.jpg nor embedded art exists.

A log file (fetch_album_art.log) is written alongside the script.
"""

import sys
import os
import time
import logging
import requests
from pathlib import Path

# ---------------------------------------------------------------------------
# Optional: change this to limit how many API requests per second.
# Apple's search API is generous but a small delay is polite.
# ---------------------------------------------------------------------------
API_DELAY_SECONDS = 0.3

# ---------------------------------------------------------------------------
# Artwork resolutions.
# ART_SIZE   — full-resolution file saved as cover.jpg in each album folder.
# EMBED_SIZE — smaller thumbnail embedded into the music files themselves.
# ---------------------------------------------------------------------------
ART_SIZE   = 3000   # saved to cover.jpg
EMBED_SIZE = 600    # embedded into music file tags

# Filename used for the saved archival copy.
COVER_FILENAME = "cover.jpg"

# Cache file — stores folder paths that have already been fully processed.
# Lives next to the script so it persists between runs.
CACHE_FILE = Path(__file__).parent / "fetch_album_art_cache.txt"

# ---------------------------------------------------------------------------
# Name-matching threshold (0–100).
# Results from Apple where the artist OR album name similarity falls below
# this value are silently filtered out before review.
# 60 = lenient enough to allow Deluxe/Remaster editions through while
#      filtering out genuinely wrong artists or album titles.
# Lower the number if too many good results are being filtered; raise it
# to be stricter. Set to 0 to disable matching entirely.
# ---------------------------------------------------------------------------
MATCH_THRESHOLD = 60

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
log_path = Path(__file__).parent / "fetch_album_art.log"
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(log_path, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)
# Console shows INFO and above; log file captures DEBUG (filtered results etc.)
logging.getLogger().handlers[0].setLevel(logging.DEBUG)
logging.getLogger().handlers[1].setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Lazy imports for mutagen (gives a friendly error if not installed)
# ---------------------------------------------------------------------------
def _require_mutagen():
    try:
        import mutagen
        return mutagen
    except ImportError:
        log.error(
            "The 'mutagen' library is not installed.\n"
            "Run this command in your terminal to install it:\n\n"
            "    pip install mutagen requests Pillow\n"
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# Tag helpers — read artist/album and check/write artwork
# ---------------------------------------------------------------------------

SUPPORTED = {".mp3", ".flac", ".m4a", ".mp4", ".ogg", ".opus", ".wma", ".wav", ".aif", ".aiff"}


def get_tags(path: Path):
    """Return (artist, album) strings from file tags, or (None, None)."""
    from mutagen.mp3 import MP3
    from mutagen.flac import FLAC
    from mutagen.mp4 import MP4
    from mutagen.oggvorbis import OggVorbis
    from mutagen.oggopus import OggOpus
    from mutagen.asf import ASF
    from mutagen.aiff import AIFF
    from mutagen.wave import WAVE
    from mutagen import MutagenError

    suffix = path.suffix.lower()
    try:
        if suffix == ".mp3":
            audio = MP3(path)
            artist = str(audio.tags.get("TPE1", [""])[0]) if audio.tags else ""
            album  = str(audio.tags.get("TALB", [""])[0]) if audio.tags else ""
        elif suffix == ".flac":
            audio = FLAC(path)
            artist = (audio.get("artist") or audio.get("albumartist") or [""])[0]
            album  = (audio.get("album") or [""])[0]
        elif suffix in (".m4a", ".mp4"):
            audio = MP4(path)
            artist = (audio.tags.get("\xa9ART") or audio.tags.get("aART") or [""])[0] if audio.tags else ""
            album  = (audio.tags.get("\xa9alb") or [""])[0] if audio.tags else ""
        elif suffix == ".ogg":
            audio = OggVorbis(path)
            artist = (audio.get("artist") or audio.get("albumartist") or [""])[0]
            album  = (audio.get("album") or [""])[0]
        elif suffix == ".opus":
            audio = OggOpus(path)
            artist = (audio.get("artist") or audio.get("albumartist") or [""])[0]
            album  = (audio.get("album") or [""])[0]
        elif suffix == ".wma":
            audio = ASF(path)
            artist = str((audio.get("Author") or audio.get("WM/AlbumArtist") or [""])[0])
            album  = str((audio.get("WM/AlbumTitle") or [""])[0])
        elif suffix in (".aif", ".aiff"):
            audio = AIFF(path)
            artist = str(audio.tags.get("TPE1", [""])[0]) if audio.tags else ""
            album  = str(audio.tags.get("TALB", [""])[0]) if audio.tags else ""
        elif suffix == ".wav":
            audio = WAVE(path)
            artist = str(audio.tags.get("TPE1", [""])[0]) if audio.tags else ""
            album  = str(audio.tags.get("TALB", [""])[0]) if audio.tags else ""
        else:
            return None, None
        return artist.strip() or None, album.strip() or None
    except MutagenError as e:
        log.warning(f"Could not read tags from {path.name}: {e}")
        return None, None


def has_artwork(path: Path) -> bool:
    """Return True if the file already has embedded artwork."""
    from mutagen.mp3 import MP3
    from mutagen.id3 import APIC
    from mutagen.flac import FLAC, Picture
    from mutagen.mp4 import MP4
    from mutagen.oggvorbis import OggVorbis
    from mutagen.oggopus import OggOpus
    from mutagen.asf import ASF
    from mutagen.aiff import AIFF
    from mutagen.wave import WAVE
    from mutagen import MutagenError

    suffix = path.suffix.lower()
    try:
        if suffix == ".mp3":
            audio = MP3(path)
            return bool(audio.tags and audio.tags.getall("APIC"))
        elif suffix == ".flac":
            audio = FLAC(path)
            return bool(audio.pictures)
        elif suffix in (".m4a", ".mp4"):
            audio = MP4(path)
            return bool(audio.tags and audio.tags.get("covr"))
        elif suffix in (".ogg", ".opus"):
            # Vorbis comment picture block (base64-encoded)
            from mutagen.oggvorbis import OggVorbis
            audio = OggVorbis(path) if suffix == ".ogg" else __import__("mutagen.oggopus", fromlist=["OggOpus"]).OggOpus(path)
            return "metadata_block_picture" in audio
        elif suffix == ".wma":
            audio = ASF(path)
            return "WM/Picture" in audio
        elif suffix in (".aif", ".aiff"):
            audio = AIFF(path)
            return bool(audio.tags and audio.tags.getall("APIC"))
        elif suffix == ".wav":
            audio = WAVE(path)
            return bool(audio.tags and audio.tags.getall("APIC"))
    except MutagenError:
        pass
    return False


def embed_artwork(path: Path, image_data: bytes) -> bool:
    """Embed JPEG image_data into the file. Returns True on success."""
    from mutagen.mp3 import MP3
    from mutagen.id3 import ID3, APIC, ID3NoHeaderError
    from mutagen.flac import FLAC, Picture
    from mutagen.mp4 import MP4, MP4Cover
    from mutagen.oggvorbis import OggVorbis
    from mutagen.oggopus import OggOpus
    from mutagen.asf import ASF, ASFByteArrayAttribute
    from mutagen.aiff import AIFF
    from mutagen.wave import WAVE
    from mutagen import MutagenError
    import base64, struct

    suffix = path.suffix.lower()
    try:
        if suffix == ".mp3":
            try:
                tags = ID3(path)
            except ID3NoHeaderError:
                tags = ID3()
            tags.delall("APIC")
            tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=image_data))
            tags.save(path)

        elif suffix == ".flac":
            audio = FLAC(path)
            audio.clear_pictures()
            pic = Picture()
            pic.type = 3
            pic.mime = "image/jpeg"
            pic.desc = "Cover"
            pic.data = image_data
            audio.add_picture(pic)
            audio.save()

        elif suffix in (".m4a", ".mp4"):
            audio = MP4(path)
            if audio.tags is None:
                audio.add_tags()
            audio.tags["covr"] = [MP4Cover(image_data, imageformat=MP4Cover.FORMAT_JPEG)]
            audio.save()

        elif suffix in (".ogg", ".opus"):
            pic = Picture()
            pic.type = 3
            pic.mime = "image/jpeg"
            pic.desc = "Cover"
            pic.data = image_data
            encoded = base64.b64encode(pic.write()).decode("ascii")
            audio = OggVorbis(path) if suffix == ".ogg" else OggOpus(path)
            audio["metadata_block_picture"] = [encoded]
            audio.save()

        elif suffix == ".wma":
            audio = ASF(path)
            # ASF picture: type(1) + data_size(4) + mime_null + desc_null + data
            mime = b"image/jpeg\x00"
            desc = b"\x00"
            payload = bytes([3]) + struct.pack("<I", len(image_data)) + mime + desc + image_data
            audio["WM/Picture"] = [ASFByteArrayAttribute(payload)]
            audio.save()

        elif suffix in (".aif", ".aiff"):
            try:
                tags = ID3(path)
            except ID3NoHeaderError:
                tags = ID3()
            tags.delall("APIC")
            tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=image_data))
            tags.save(path)

        elif suffix == ".wav":
            try:
                tags = ID3(path)
            except ID3NoHeaderError:
                tags = ID3()
            tags.delall("APIC")
            tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=image_data))
            tags.save(path)

        else:
            return False

        return True
    except MutagenError as e:
        log.error(f"Failed to embed art into {path.name}: {e}")
        return False


# ---------------------------------------------------------------------------
# Apple iTunes Search API
# ---------------------------------------------------------------------------

def search_apple(artist: str, album: str):
    """
    Search Apple's iTunes API and return a list of result dicts, or [].

    Each dict contains:
        art_url      — full-res artwork URL (ART_SIZE x ART_SIZE)
        preview_url  — 300px thumbnail URL suitable for browser preview
        matched_artist, matched_album — what Apple actually returned
    Tries artist+album first, then just the album title as a fallback.
    Returns up to 10 results so the user can page through them.
    """
    queries = []
    if artist and album:
        queries.append(f"{artist} {album}")
    if album:
        queries.append(album)

    seen_ids = set()
    all_results = []

    for q in queries:
        url = "https://itunes.apple.com/search"
        params = {"term": q, "entity": "album", "limit": 15}
        try:
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            for hit in data.get("results", []):
                cid = hit.get("collectionId")
                if cid and cid in seen_ids:
                    continue
                if cid:
                    seen_ids.add(cid)
                art_url = hit.get("artworkUrl100", "")
                if art_url:
                    all_results.append({
                        "art_url":        art_url.replace("100x100bb", f"{ART_SIZE}x{ART_SIZE}bb"),
                        "preview_url":    art_url.replace("100x100bb", "300x300bb"),
                        "matched_artist": hit.get("artistName", "Unknown artist"),
                        "matched_album":  hit.get("collectionName", "Unknown album"),
                    })
        except requests.RequestException as e:
            log.warning(f"API request failed for query '{q}': {e}")
        time.sleep(API_DELAY_SECONDS)

    return all_results


def review_match(search_label: str, results: list, folder: Path, existing_res: int = 0) -> tuple:
    """
    Page through Apple results and ask the user whether to accept one.

    existing_res — width in pixels of the current cover (0 if none).
    Returns ('y', match_dict) to accept, ('s', None) to skip this album,
    or ('q', None) to quit the script entirely.
    """
    import webbrowser
    import subprocess

    # Cache probed resolutions so we don't re-fetch on revisit
    probed: dict[int, int] = {}

    total = len(results)
    for i, match in enumerate(results):
        # Probe Apple resolution for this result if not already done
        if i not in probed:
            probed[i] = probe_apple_resolution(match["art_url"])
        apple_res = probed[i]

        # Build the resolution line
        if existing_res > 0 and apple_res > 0:
            res_line = f"  Resolution    : existing {existing_res}px  →  Apple {apple_res}px"
        elif apple_res > 0:
            res_line = f"  Resolution    : no existing cover  →  Apple {apple_res}px"
        elif existing_res > 0:
            res_line = f"  Resolution    : existing {existing_res}px  →  Apple unknown"
        else:
            res_line = f"  Resolution    : no existing cover"

        print()
        print("=" * 60)
        print(f"  Searching for : {search_label}")
        print(f"  Result        : {i + 1} of {total}")
        print(res_line)
        print(f"  Apple matched : {match['matched_artist']} — {match['matched_album']}")
        print(f"  Preview URL   : {match['preview_url']}")
        print()
        print("  Opening preview in your browser...")
        webbrowser.open(match["preview_url"])
        print()

        is_last = (i == total - 1)
        if is_last:
            prompt = "  [Y]es / [O]pen folder / [S]kip album / [Q]uit : "
            valid  = ("y", "o", "s", "q")
            hint   = "  Please press Y, O, S, or Q."
        else:
            prompt = "  [Y]es / [N]ext result / [O]pen folder / [S]kip album / [Q]uit : "
            valid  = ("y", "n", "o", "s", "q")
            hint   = "  Please press Y, N, O, S, or Q."

        while True:
            raw = input(prompt).strip().lower()
            if raw == "o":
                subprocess.Popen(f'explorer "{folder}"')
                print("  Opened folder in Explorer.")
                continue
            if raw in valid:
                break
            print(hint)

        if raw == "y":
            return "y", match
        if raw == "s":
            return "s", None
        if raw == "q":
            return "q", None
        # raw == "n": loop to next result

    # Exhausted all results without accepting
    print("  No more results for this album.")
    return "s", None


def download_image(url: str, size: int) -> bytes | None:
    """Download image bytes at the requested size, falling back to smaller sizes."""
    fallback_sizes = [size, 1200, 600]
    # Remove duplicates while preserving order
    seen = set()
    sizes_to_try = [s for s in fallback_sizes if not (s in seen or seen.add(s)) and s <= size]
    for s in sizes_to_try:
        sized_url = url.replace(f"{ART_SIZE}x{ART_SIZE}bb", f"{s}x{s}bb")
        try:
            resp = requests.get(sized_url, timeout=15)
            if resp.status_code == 200 and resp.content:
                if s < size:
                    log.info(f"  Fell back to {s}px (Apple doesn't have larger for this title)")
                return resp.content
        except requests.RequestException:
            pass
    return None


def resize_jpeg(image_data: bytes, size: int) -> bytes:
    """
    Resize image_data to size×size pixels using Pillow.
    Falls back to returning the original bytes if Pillow is unavailable.
    """
    try:
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(image_data)).convert("RGB")
        img = img.resize((size, size), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=90)
        return buf.getvalue()
    except Exception as e:
        log.warning(f"  Could not resize image ({e}); embedding original size instead")
        return image_data


# Candidate filenames searched in priority order.
# The script never renames or converts these — it only reads them to check
# resolution, and writes cover.jpg only when it fetches something better.
COVER_CANDIDATES = [
    "cover.png",  "Cover.png",
    "cover.jpg",  "Cover.jpg",
    "folder.png", "Folder.png",
    "folder.jpg", "Folder.jpg",
    "artwork.png","Artwork.png",
    "artwork.jpg","Artwork.jpg",
    "front.png",  "Front.png",
    "front.jpg",  "Front.jpg",
]


def find_best_existing_cover(folder: Path):
    """
    Scan the folder for any known cover image file.

    Returns (path, resolution_px, is_png) for the first match found, where
    resolution_px is the image width in pixels (0 if unreadable), and
    is_png indicates the file is a PNG (so we never overwrite it with a JPEG).

    Returns (None, 0, False) when no candidate exists.
    """
    from PIL import Image

    for name in COVER_CANDIDATES:
        candidate = folder / name
        if not candidate.exists():
            continue
        is_png = candidate.suffix.lower() == ".png"
        try:
            with Image.open(candidate) as img:
                return candidate, img.width, is_png
        except Exception:
            # File exists but can't be opened as an image — report and skip
            return candidate, 0, is_png

    return None, 0, False




# ---------------------------------------------------------------------------
# Quick resolution probe
# ---------------------------------------------------------------------------

def probe_apple_resolution(art_url: str) -> int:
    """
    Fetch just enough of an Apple artwork JPEG to read its dimensions,
    without downloading the entire file.  Returns width in pixels, or 0.
    """
    from PIL import Image
    import io as _io
    try:
        resp = requests.get(art_url, headers={"Range": "bytes=0-65535"}, timeout=10)
        if resp.status_code not in (200, 206):
            return 0
        img = Image.open(_io.BytesIO(resp.content))
        return img.width
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Name matching
# ---------------------------------------------------------------------------

def name_similarity(a: str, b: str) -> int:
    """
    Return a 0–100 similarity score between two strings.
    Comparison is case-insensitive and ignores common noise words like
    'the', 'a', 'an' so e.g. 'The Beatles' matches 'Beatles' well.
    Also strips content in parentheses/brackets so 'Abbey Road (Remaster)'
    still matches 'Abbey Road' cleanly.
    """
    import difflib
    import re

    def normalise(s: str) -> str:
        s = s.lower()
        s = re.sub(r"[\(\[\{][^\)\]\}]*[\)\]\}]", "", s)  # strip (Remaster) etc.
        s = re.sub(r"\b(the|a|an)\b", "", s)               # strip noise words
        s = re.sub(r"[^a-z0-9\s]", "", s)                  # strip punctuation
        return s.strip()

    na, nb = normalise(a), normalise(b)
    if not na or not nb:
        return 0
    ratio = difflib.SequenceMatcher(None, na, nb).ratio()
    return int(ratio * 100)


def filter_results_by_name(results: list, artist: str, album: str) -> list:
    """
    Filter Apple results to those where the matched artist AND album name
    both meet MATCH_THRESHOLD.  If MATCH_THRESHOLD is 0, returns all results.
    Results that don't meet the threshold are logged as filtered.
    """
    if MATCH_THRESHOLD == 0 or (not artist and not album):
        return results

    filtered = []
    for r in results:
        artist_score = name_similarity(artist or "", r["matched_artist"]) if artist else 100
        album_score  = name_similarity(album  or "", r["matched_album"])  if album  else 100
        if artist_score >= MATCH_THRESHOLD and album_score >= MATCH_THRESHOLD:
            filtered.append(r)
        else:
            log.debug(
                f"  Filtered out: {r['matched_artist']} — {r['matched_album']} "
                f"(artist {artist_score}%, album {album_score}%)"
            )
    return filtered


# ---------------------------------------------------------------------------
# Processed-folder cache
# ---------------------------------------------------------------------------

def load_cache() -> set:
    """
    Load the set of already-processed folder paths from the cache file.
    Returns an empty set if the file does not exist yet.
    """
    if not CACHE_FILE.exists():
        return set()
    with open(CACHE_FILE, encoding="utf-8") as fh:
        return {line.strip() for line in fh if line.strip()}


def save_cache_entry(folder: Path) -> None:
    """Append a single folder path to the cache file."""
    with open(CACHE_FILE, "a", encoding="utf-8") as fh:
        fh.write(str(folder.resolve()) + "\n")


def scan_library(root: Path):
    """
    Walk root, group music files by their parent folder (album folder),
    and process each album folder once.
    """
    _require_mutagen()

    # Load the set of already-processed folders
    cache = load_cache()
    if cache:
        log.info(f"Cache loaded — {len(cache)} folders already processed, skipping them.")

    # Collect all music files
    all_files = [
        p for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in SUPPORTED
    ]

    if not all_files:
        log.warning(f"No supported music files found under: {root}")
        return

    # Group by album folder
    from collections import defaultdict
    folders: dict[Path, list[Path]] = defaultdict(list)
    for f in all_files:
        folders[f.parent].append(f)

    total_folders = len(folders)
    skipped = processed = failed = 0

    log.info(f"Found {len(all_files)} files across {total_folders} album folders in: {root}")
    log.info("-" * 60)

    for folder, files in sorted(folders.items()):
        rel = folder.relative_to(root)

        # Skip folders that were fully handled in a previous run
        if str(folder.resolve()) in cache:
            log.info(f"[CACHE] {rel}")
            skipped += 1
            continue

        # Find the best existing cover image across all common filenames/formats.
        existing_path, cover_res, cover_is_png = find_best_existing_cover(folder)
        cover_exists  = existing_path is not None
        cover_is_full = cover_res >= ART_SIZE
        any_embedded  = any(has_artwork(f) for f in files)

        # A PNG cover always takes priority — never overwrite it with a JPEG,
        # regardless of resolution (a 150 MB PNG is a lossless master).
        if cover_is_png:
            if any_embedded:
                log.info(f"[SKIP]  {rel}  (PNG cover present at {cover_res}px, embedded art present)")
                save_cache_entry(folder)
                skipped += 1
                continue
            else:
                # PNG cover exists but no embedded thumbnail — generate one from it
                log.info(f"[EMBED] {rel}  →  PNG cover at {cover_res}px, embedding thumbnail only")
                thumb_image = resize_jpeg(existing_path.read_bytes(), EMBED_SIZE)
                ok_count = sum(1 for f in files if embed_artwork(f, thumb_image))
                log.info(f"  ✓ Embedded thumbnail from PNG cover into {ok_count}/{len(files)} files")
                save_cache_entry(folder)
                processed += 1
                continue

        if cover_is_full and any_embedded:
            log.info(f"[SKIP]  {rel}  ({existing_path.name} is {cover_res}px, embedded art present)")
            save_cache_entry(folder)
            skipped += 1
            continue

        if cover_exists and cover_res > 0:
            log.info(f"[UPSCALE] {rel}  →  {existing_path.name} is only {cover_res}px, seeking upgrade")
        elif cover_exists:
            log.info(f"[FETCH] {rel}  →  {existing_path.name} unreadable, replacing")
        else:
            log.info(f"[FETCH] {rel}  →  no artwork found")

        # Read artist/album from the first file that has tags
        artist, album = None, None
        for f in files:
            a, b = get_tags(f)
            if a or b:
                artist, album = a, b
                break

        # Fall back to folder/parent names if tags are missing
        if not album:
            album = folder.name
        if not artist:
            artist = folder.parent.name if folder.parent != root else None

        search_label = f"{artist} — {album}" if artist else album
        log.info(f"  Searching: \"{search_label}\"")

        results = search_apple(artist or "", album or "")
        if not results:
            log.warning(f"  No results found on Apple for: {search_label}")
            save_cache_entry(folder)
            failed += 1
            continue

        # Filter results by name similarity before review
        results = filter_results_by_name(results, artist or "", album or "")
        if not results:
            log.warning(f"  All Apple results filtered out (name mismatch) for: {search_label}")
            save_cache_entry(folder)
            failed += 1
            continue

        # --- Silent resolution pre-check ---
        # If we already have a cover, probe Apple's best result before prompting.
        # Skip quietly if Apple can't beat what we already have.
        if cover_exists and cover_res > 0:
            apple_res = probe_apple_resolution(results[0]["art_url"])
            if apple_res > 0 and apple_res <= cover_res:
                log.info(
                    f"  Apple's best result is {apple_res}px — "
                    f"not better than existing {cover_res}px, skipping."
                )
                save_cache_entry(folder)
                skipped += 1
                continue
            elif apple_res > 0:
                log.info(f"  Apple has {apple_res}px vs existing {cover_res}px — prompting for review.")

        # --- Review mode: page through results and ask user before downloading ---
        decision, match = review_match(search_label, results, folder, existing_res=cover_res)
        if decision == "q":
            log.info("  User quit — stopping.")
            break
        if decision == "s":
            log.info(f"  Skipped by user.")
            save_cache_entry(folder)
            skipped += 1
            continue
        # decision == "y": fall through and download
        log.info(f"  Accepted: {match['matched_artist']} — {match['matched_album']}")
        art_url = match["art_url"]

        # Download full-resolution archival copy
        full_image = download_image(art_url, ART_SIZE)
        if not full_image:
            log.warning(f"  Image download failed for: {search_label}")
            failed += 1
            continue

        # Check the resolution we actually received before overwriting
        from PIL import Image
        import io as _io
        try:
            with Image.open(_io.BytesIO(full_image)) as _img:
                new_res = _img.width
        except Exception:
            new_res = ART_SIZE  # assume it's fine if we can't read it

        # Determine the save path — always write/overwrite as cover.jpg
        # (existing low-res JPEGs like folder.jpg are left untouched alongside it)
        save_path = folder / COVER_FILENAME

        if cover_is_full:
            # Existing cover is already full-res; skip saving, just embed if needed
            log.info(f"  {existing_path.name} already at {cover_res}px — skipping file save")
        elif cover_exists and new_res <= cover_res:
            log.info(
                f"  Apple returned {new_res}px — not better than existing {cover_res}px, keeping original"
            )
            # Still update embedded art if it was missing
            if not any_embedded:
                thumb_image = resize_jpeg(existing_path.read_bytes(), EMBED_SIZE)
                ok_count = sum(1 for f in files if embed_artwork(f, thumb_image))
                log.info(f"  ✓ Embedded thumbnail from existing cover into {ok_count}/{len(files)} files")
            save_cache_entry(folder)
            processed += 1
            continue
        else:
            # Save the improved image as cover.jpg
            save_path.write_bytes(full_image)
            log.info(f"  ✓ Saved cover.jpg  ({len(full_image) // 1024} KB,  {new_res}px)")

        # Resize down to thumbnail for embedding (skip if already embedded and cover unchanged)
        if not any_embedded or not cover_is_full:
            thumb_image = resize_jpeg(full_image, EMBED_SIZE)
            log.info(f"  ✓ Resized thumbnail  ({len(thumb_image) // 1024} KB,  {EMBED_SIZE}px)")
            ok_count = sum(1 for f in files if embed_artwork(f, thumb_image))
            log.info(f"  ✓ Embedded thumbnail into {ok_count}/{len(files)} files")

        save_cache_entry(folder)
        processed += 1
        time.sleep(API_DELAY_SECONDS)

    log.info("-" * 60)
    log.info(
        f"Done.  {processed} updated,  {skipped} skipped (had art),  {failed} not found"
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        print("\nUsage:  python fetch_album_art.py \"C:\\Users\\You\\Music\"")
        sys.exit(1)

    music_root = Path(sys.argv[1])
    if not music_root.is_dir():
        print(f"Error: folder not found:  {music_root}")
        sys.exit(1)

    scan_library(music_root)
