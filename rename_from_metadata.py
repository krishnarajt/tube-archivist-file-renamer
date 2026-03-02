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
    pos = start
    while pos + 8 <= end:
        size = int.from_bytes(buf[pos:pos + 4], "big")
        atype = buf[pos + 4:pos + 8]
        if size == 0:
            size = end - pos
        elif size == 1:
            if pos + 16 > end:
                return
            size = int.from_bytes(buf[pos + 8:pos + 16], "big")
            header = 16
        else:
            header = 8
        if size < header or pos + size > end:
            return
        yield pos, pos + size, atype, header
        pos += size


def extract_mp4_metadata(path: Path) -> tuple[Optional[str], Optional[str]]:
    buf = path.read_bytes()

    def find_child_region(parent_start, parent_end, child):
        for s, e, t, h in parse_atoms(buf, parent_start, parent_end):
            if t == child:
                return s + h, e
        return None

    root = (0, len(buf))
    moov = find_child_region(*root, b"moov")
    if not moov:
        return None, None
    udta = find_child_region(*moov, b"udta") or moov
    meta = find_child_region(*udta, b"meta")
    if not meta:
        return None, None
    ilst = find_child_region(*meta, b"ilst")
    if not ilst:
        return None, None

    title = artist = None
    for s, e, t, h in parse_atoms(buf, *ilst):
        key = t
        for cs, ce, ct, ch in parse_atoms(buf, s + h, e):
            if ct != b"data":
                continue
            payload = buf[cs + ch + 8:ce]
            text = payload.decode("utf-8", errors="ignore").strip("\x00 ")
            if key == b"\xa9nam" and text:
                title = text
            elif key in (b"\xa9ART", b"aART") and text:
                artist = text
    return title, artist


def extract_metadata(path: Path) -> tuple[Optional[str], Optional[str]]:
    ext = path.suffix.lower()
    try:
        if ext == ".mp3":
            return extract_id3_metadata(path)
        if ext in {".mp4", ".m4a"}:
            return extract_mp4_metadata(path)
    except Exception:
        pass
    return None, None


def build_subtitle_index(root: Path):
    out = defaultdict(list)
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in SUPPORTED_SUBTITLE_EXTENSIONS:
            out[p.parent / p.stem].append(p)
    return out


def rename_related_subtitles(old_media: Path, new_media: Path, idx):
    for sub in idx.get(old_media.with_suffix(""), []):
        target = unique_path((new_media.parent / new_media.stem).with_suffix(sub.suffix.lower()))
        print(f"[subtitle] {sub} -> {target}")
        sub.rename(target)


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
            rename_related_subtitles(media, target, subtitle_index)
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
