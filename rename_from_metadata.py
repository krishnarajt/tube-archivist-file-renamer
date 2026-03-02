#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import shutil
import struct
import subprocess
import sys
from pathlib import Path
from typing import Optional

SUPPORTED_MEDIA_EXTENSIONS = {".mp3", ".m4a", ".mp4"}
SUPPORTED_SUBTITLE_EXTENSIONS = {".vtt"}

TRACKING_FILE_NAME = ".converted_ids.json"

# # dotenv
import dotenv
dotenv.load_dotenv()


# ---------------------------------------------------------------------------
# Tracking file helpers
# ---------------------------------------------------------------------------

def file_id(path: Path) -> str:
    """A stable ID for a file: name + mtime + size."""
    stat = path.stat()
    return f"{path.name}|{stat.st_mtime}|{stat.st_size}"


def load_tracking(output: Path) -> set[str]:
    tracking = output / TRACKING_FILE_NAME
    if tracking.exists():
        try:
            return set(json.loads(tracking.read_text()))
        except Exception:
            pass
    return set()


def save_tracking(output: Path, ids: set[str]) -> None:
    tracking = output / TRACKING_FILE_NAME
    try:
        tracking.write_text(json.dumps(sorted(ids), indent=2))
    except Exception as e:
        print(f"[warn] Could not save tracking file: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Name helpers
# ---------------------------------------------------------------------------

def sanitize_name(value: str) -> str:
    value = re.sub(r"[\\/:*?\"<>|\x00-\x1F]", "_", value.strip())
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value or "unknown"


def unique_path(target: Path) -> Path:
    if not target.exists():
        return target
    i = 1
    while True:
        cand = target.with_name(f"{target.stem}_{i}{target.suffix}")
        if not cand.exists():
            return cand
        i += 1


# ---------------------------------------------------------------------------
# Metadata extraction (read-only)
# ---------------------------------------------------------------------------

def decode_syncsafe(value: bytes) -> int:
    return ((value[0] & 0x7F) << 21) | ((value[1] & 0x7F) << 14) | ((value[2] & 0x7F) << 7) | (value[3] & 0x7F)


def decode_text_frame(payload: bytes) -> str:
    if not payload:
        return ""
    enc = payload[0]
    data = payload[1:]
    if enc == 0:
        return data.decode("latin1", errors="ignore").strip("\x00 ")
    if enc == 1:
        return data.decode("utf-16", errors="ignore").strip("\x00 ")
    if enc == 2:
        return data.decode("utf-16-be", errors="ignore").strip("\x00 ")
    return data.decode("utf-8", errors="ignore").strip("\x00 ")


def extract_id3_metadata(path: Path) -> tuple[Optional[str], Optional[str]]:
    raw = path.read_bytes()
    if len(raw) < 10 or raw[:3] != b"ID3":
        return None, None
    size = decode_syncsafe(raw[6:10])
    cursor = 10
    end = min(len(raw), 10 + size)
    title = artist = None

    while cursor + 10 <= end:
        frame_id = raw[cursor:cursor + 4].decode("latin1", errors="ignore")
        frame_size = struct.unpack(">I", raw[cursor + 4:cursor + 8])[0]
        if frame_size <= 0 or not frame_id.strip("\x00"):
            break
        payload_start = cursor + 10
        payload_end = payload_start + frame_size
        if payload_end > len(raw):
            break
        payload = raw[payload_start:payload_end]
        if frame_id == "TIT2":
            title = decode_text_frame(payload)
        elif frame_id == "TPE1":
            artist = decode_text_frame(payload)
        cursor = payload_end

    return title or None, artist or None


def parse_atoms(buf: bytes, start: int, end: int):
    """Yield (atom_start, atom_end, atom_type_bytes, header_size) for each atom in [start, end)."""
    pos = start
    while pos + 8 <= end:
        size = int.from_bytes(buf[pos:pos + 4], "big")
        atype = buf[pos + 4:pos + 8]
        header = 8
        if size == 0:
            size = end - pos
        elif size == 1:
            if pos + 16 > end:
                return
            size = int.from_bytes(buf[pos + 8:pos + 16], "big")
            header = 16
        if size < header or pos + size > end:
            return
        yield pos, pos + size, atype, header
        pos += size


def find_atom(buf: bytes, start: int, end: int, target: bytes):
    for s, e, t, h in parse_atoms(buf, start, end):
        if t == target:
            return s + h, e
    return None


def find_atom_recursive(buf: bytes, start: int, end: int, *path: bytes):
    region = (start, end)
    for step in path:
        result = find_atom(buf, *region, step)
        if result is None:
            return None
        region = result
    return region


def extract_mp4_metadata(path: Path) -> tuple[Optional[str], Optional[str]]:
    buf = path.read_bytes()
    n = len(buf)

    moov = find_atom(buf, 0, n, b"moov")
    if not moov:
        return None, None

    ilst = None
    for meta_path in [(b"udta", b"meta"), (b"meta",)]:
        meta = find_atom_recursive(buf, *moov, *meta_path)
        if not meta:
            continue
        for meta_offset in (4, 0):
            meta_start = meta[0] + meta_offset
            meta_end = meta[1]
            if meta_start >= meta_end:
                continue
            result = find_atom(buf, meta_start, meta_end, b"ilst")
            if result:
                ilst = result
                break
        if ilst:
            break

    if not ilst:
        return None, None

    title = artist = None

    for s, e, t, h in parse_atoms(buf, *ilst):
        data_atom = find_atom(buf, s + h, e, b"data")
        if not data_atom:
            continue
        ds, de = data_atom
        if de - ds < 8:
            continue
        text = buf[ds + 8:de].decode("utf-8", errors="ignore").strip("\x00 ")
        if not text:
            continue
        if t == b"\xa9nam":
            title = text
        elif t in (b"\xa9ART", b"aART"):
            artist = text

    return title or None, artist or None


def extract_metadata(path: Path) -> tuple[Optional[str], Optional[str]]:
    ext = path.suffix.lower()
    try:
        if ext == ".mp3":
            return extract_id3_metadata(path)
        if ext in {".mp4", ".m4a"}:
            return extract_mp4_metadata(path)
    except Exception as exc:
        print(f"[warn] Error reading metadata from {path}: {exc}", file=sys.stderr)
    return None, None


# ---------------------------------------------------------------------------
# ffmpeg helpers
# ---------------------------------------------------------------------------

def ffmpeg_available() -> bool:
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        return True
    except Exception:
        return False


def convert_to_mp3(src: Path, dst: Path) -> bool:
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", str(src),
                "-vn",
                "-acodec", "libmp3lame",
                "-q:a", "2",
                "-map_metadata", "0",
                "-id3v2_version", "3",
                str(dst)
            ],
            capture_output=True
        )
        return result.returncode == 0
    except Exception as e:
        print(f"[warn] ffmpeg error converting {src}: {e}", file=sys.stderr)
        return False


def copy_with_metadata(src: Path, dst: Path) -> bool:
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", str(src),
                "-c", "copy",
                "-map_metadata", "0",
                "-id3v2_version", "3",
                str(dst)
            ],
            capture_output=True
        )
        return result.returncode == 0
    except Exception as e:
        print(f"[warn] ffmpeg error copying {src}: {e}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Main processing
# ---------------------------------------------------------------------------

def process(root: Path, output: Path, use_ffmpeg: bool) -> int:
    converted_ids = load_tracking(output)

    media_files = [
        p for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in SUPPORTED_MEDIA_EXTENSIONS
    ]

    for media in media_files:
        ext = media.suffix.lower()

        fid = file_id(media)
        if fid in converted_ids:
            print(f"[skip] Already converted: {media}")
            continue

        title, artist = extract_metadata(media)
        if not title or not artist:
            print(f"[skip] Missing metadata artist/title: {media}")
            continue

        safe_title = sanitize_name(title)
        safe_artist = sanitize_name(artist)

        out_folder = output / safe_artist
        out_folder.mkdir(exist_ok=True)

        out_media = unique_path(out_folder / f"{safe_title}.mp3")

        if ext == ".mp4":
            if not use_ffmpeg:
                print(f"[skip] ffmpeg not available, cannot convert {media}", file=sys.stderr)
                continue
            print(f"[convert] {media} -> {out_media}")
            ok = convert_to_mp3(media, out_media)
            if not ok:
                print(f"[error] Conversion failed for {media}", file=sys.stderr)
                if out_media.exists():
                    out_media.unlink()
                continue

        elif ext in {".mp3", ".m4a"}:
            print(f"[copy] {media} -> {out_media}")
            ok = copy_with_metadata(media, out_media) if use_ffmpeg else False
            if not ok:
                shutil.copy2(media, out_media)
                print(f"[warn] ffmpeg copy failed or unavailable; used plain copy for {media}", file=sys.stderr)

        else:
            continue

        # Copy subtitle files to the output folder, renamed to match the output stem
        old_stem = media.stem
        for sibling in media.parent.iterdir():
            if not sibling.is_file():
                continue
            if sibling.suffix.lower() not in SUPPORTED_SUBTITLE_EXTENSIONS:
                continue
            if sibling.stem == old_stem:
                new_sub = unique_path(out_folder / f"{safe_title}{sibling.suffix.lower()}")
                print(f"[subtitle] {sibling} -> {new_sub}")
                shutil.copy2(sibling, new_sub)

        # Mark as converted only after everything succeeded
        converted_ids.add(fid)
        save_tracking(output, converted_ids)

    return 0


def main() -> int:
    input_folder = os.getenv("INPUT_FOLDER")
    output_folder = os.getenv("OUTPUT_FOLDER")
    if not input_folder or not output_folder:
        print("INPUT_FOLDER and OUTPUT_FOLDER env vars are required", file=sys.stderr)
        return 2
    root = Path(input_folder).expanduser().resolve()
    output = Path(output_folder).expanduser().resolve()
    if not root.is_dir():
        print(f"INPUT_FOLDER does not exist or is not a dir: {root}", file=sys.stderr)
        return 2
    output.mkdir(parents=True, exist_ok=True)

    use_ffmpeg = ffmpeg_available()
    if not use_ffmpeg:
        print("[warn] ffmpeg not found — MP4 conversion and metadata-preserving copy will be skipped.", file=sys.stderr)

    return process(root, output, use_ffmpeg)


if __name__ == "__main__":
    raise SystemExit(main())