#!/usr/bin/env python3
from __future__ import annotations

import os
import re
import struct
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional

SUPPORTED_MEDIA_EXTENSIONS = {".mp3", ".m4a", ".mp4"}
SUPPORTED_SUBTITLE_EXTENSIONS = {".vtt"}
# dotenv
import dotenv
dotenv.load_dotenv()

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
            # Atom extends to end of file
            size = end - pos
        elif size == 1:
            # Extended 64-bit size
            if pos + 16 > end:
                return
            size = int.from_bytes(buf[pos + 8:pos + 16], "big")
            header = 16
        if size < header or pos + size > end:
            return
        yield pos, pos + size, atype, header
        pos += size


def find_atom(buf: bytes, start: int, end: int, target: bytes):
    """Find first atom of type `target` within [start, end), return (data_start, data_end) or None."""
    for s, e, t, h in parse_atoms(buf, start, end):
        if t == target:
            return s + h, e
    return None


def find_atom_recursive(buf: bytes, start: int, end: int, *path: bytes):
    """Walk a chain of atom types, return (data_start, data_end) of the final atom or None."""
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

    # Find moov
    moov = find_atom(buf, 0, n, b"moov")
    if not moov:
        return None, None

    # ilst can live under moov/udta/meta/ilst OR moov/meta/ilst
    ilst = None
    for meta_path in [
        (b"udta", b"meta"),
        (b"meta",),
    ]:
        meta = find_atom_recursive(buf, *moov, *meta_path)
        if not meta:
            continue

        # The 'meta' atom is a FullBox: it has a 4-byte version+flags field
        # before its children. Try with and without that offset.
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
        # Each item atom contains a 'data' child
        data_atom = find_atom(buf, s + h, e, b"data")
        if not data_atom:
            continue
        ds, de = data_atom
        # data atom: 4 bytes type indicator + 4 bytes locale, then UTF-8 text
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


def build_subtitle_index(root: Path):
    out = defaultdict(list)
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in SUPPORTED_SUBTITLE_EXTENSIONS:
            out[p.parent / p.stem].append(p)
    return out


def rename_related_files(old_media: Path, new_media: Path):
    """
    Rename any file in the same directory that starts with the same
    stem as the old media file (except the media file itself).
    """
    old_stem = old_media.stem

    for sibling in old_media.parent.iterdir():
        if not sibling.is_file():
            continue
        if sibling == old_media:
            continue

        # Match files that start with old stem
        if sibling.name.startswith(old_stem):
            # Preserve everything after the original stem
            suffix_part = sibling.name[len(old_stem):]

            new_name = new_media.stem + suffix_part
            target = unique_path(new_media.parent / new_name)

            print(f"[related] {sibling} -> {target}")
            sibling.rename(target)
            
def process(root: Path) -> int:
    subtitle_index = build_subtitle_index(root)
    folder_artists = defaultdict(list)
    media_files = [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED_MEDIA_EXTENSIONS]

    for media in media_files:
        title, artist = extract_metadata(media)
        if not title or not artist:
            print(f"[skip] Missing metadata artist/title: {media}")
            continue
        target = unique_path(media.with_name(f"{sanitize_name(title)}{media.suffix.lower()}"))
        if target != media:
            print(f"[media] {media} -> {target}")
            media.rename(target)
            rename_related_files(media, target)
            media = target
        folder_artists[media.parent].append(sanitize_name(artist))

    for folder in sorted(folder_artists, key=lambda p: len(p.parts), reverse=True):
        artist = max(set(folder_artists[folder]), key=folder_artists[folder].count)
        target_folder = unique_path(folder.with_name(artist))
        if target_folder != folder:
            print(f"[folder] {folder} -> {target_folder}")
            folder.rename(target_folder)
    return 0


def main() -> int:
    input_folder = os.getenv("INPUT_FOLDER")
    if not input_folder:
        print("INPUT_FOLDER env var is required", file=sys.stderr)
        return 2
    root = Path(input_folder).expanduser().resolve()
    if not root.is_dir():
        print(f"INPUT_FOLDER does not exist or is not a dir: {root}", file=sys.stderr)
        return 2
    return process(root)


if __name__ == "__main__":
    raise SystemExit(main())