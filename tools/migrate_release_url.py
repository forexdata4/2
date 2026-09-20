#!/usr/bin/env python3
"""Download one annual BarReplay ZIP from a direct GitHub Release asset URL,
split its annual bin_v1 BIN into daily BIN files, and batch-upload to Hugging Face.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import struct
import sys
import tempfile
import time
import zipfile
from array import array
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlparse

import requests
from huggingface_hub import HfApi, hf_hub_download

HEADER = struct.Struct("<I")
ITEM_SIZE = 8
BYTES_PER_TICK = 24
MAX_TICKS_PER_BLOCK = 20_000_000
MIN_TIMESTAMP_MS = 631152000000
MAX_TIMESTAMP_MS = 4133980800000
DAILY_RE = re.compile(r"^(?P<symbol>.+?)_(?P<day>\d{4}-\d{2}-\d{2})\.BIN$", re.I)


@dataclass(frozen=True)
class Entry:
    symbol: str
    path: str
    day: str
    size: int
    source: Path | None = None


def retry(label, fn, attempts=5):
    delay = 5
    last = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:
            last = exc
            if attempt < attempts:
                print(f"{label} failed ({exc}); retrying in {delay}s...", flush=True)
                time.sleep(delay)
                delay = min(delay * 2, 60)
    raise RuntimeError(f"{label} failed after {attempts} attempts: {last}")


def validate_release_asset_url(url: str) -> str:
    parsed = urlparse(url)

    if parsed.scheme != "https":
        raise ValueError("download URL must use https")

    allowed_hosts = {
        "github.com",
        "www.github.com",
        "raw.githubusercontent.com",
    }

    if parsed.hostname not in allowed_hosts:
        raise ValueError(
            "URL must be a GitHub Release asset or a raw file from a Git tag"
        )

    filename = Path(unquote(parsed.path)).name
    if not filename.lower().endswith(".zip"):
        raise ValueError("download URL must point to a .zip file")

    return filename


def download_zip(url: str, destination: Path, github_token: str | None) -> None:
    headers = {"User-Agent": "BarReplay-release-migrator/1.0"}
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"
    with requests.get(url, headers=headers, stream=True, timeout=(30, 300), allow_redirects=True) as response:
        response.raise_for_status()
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("wb") as out:
            for chunk in response.iter_content(chunk_size=8 * 1024 * 1024):
                if chunk:
                    out.write(chunk)
    with destination.open("rb") as downloaded:
        signature = downloaded.read(4)
    if destination.stat().st_size < 4 or signature not in {b"PK\x03\x04", b"PK\x05\x06"}:
        raise ValueError("downloaded response is not a ZIP file; check the asset URL and access permissions")
    print(f"Downloaded {destination.name}: {destination.stat().st_size:,} bytes")


def safe_extract_single_bin(zip_path: Path, output_dir: Path) -> Path:
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            member = PurePosixPath(info.filename)
            if member.is_absolute() or ".." in member.parts:
                raise ValueError(f"unsafe ZIP member: {info.filename}")
        archive.extractall(output_dir)
    bins = [p for p in output_dir.rglob("*") if p.is_file() and p.suffix.lower() == ".bin"]
    if len(bins) != 1:
        raise ValueError(f"expected exactly one annual BIN inside ZIP; found {len(bins)}")
    print(f"Extracted annual BIN: {bins[0]} ({bins[0].stat().st_size:,} bytes)")
    return bins[0]


def read_exact(handle, size: int, label: str) -> bytes:
    data = handle.read(size)
    if len(data) != size:
        raise ValueError(f"truncated {label}: expected {size:,} bytes, got {len(data):,}")
    return data


def timestamps_from_le(data: bytes) -> array:
    values = array("q")
    values.frombytes(data)
    if sys.byteorder != "little":
        values.byteswap()
    return values


def utc_day(timestamp_ms: int) -> str:
    if not MIN_TIMESTAMP_MS <= timestamp_ms < MAX_TIMESTAMP_MS:
        raise ValueError(f"invalid timestamp_ms: {timestamp_ms}")
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).date().isoformat()


def append_segment(out_dir: Path, symbol: str, day: str, columns: tuple[bytes, bytes, bytes], start: int, end: int):
    count = end - start
    if count <= 0:
        return
    path = out_dir / symbol / f"{symbol}_{day}.BIN"
    path.parent.mkdir(parents=True, exist_ok=True)
    lo, hi = start * ITEM_SIZE, end * ITEM_SIZE
    ts_bytes, bid_bytes, ask_bytes = columns
    with path.open("ab", buffering=16 * 1024 * 1024) as out:
        out.write(HEADER.pack(count))
        out.write(ts_bytes[lo:hi])
        out.write(bid_bytes[lo:hi])
        out.write(ask_bytes[lo:hi])


def split_annual_bin(source: Path, out_dir: Path, symbol: str) -> tuple[int, int, int]:
    blocks = 0
    ticks = 0
    daily_ticks: dict[str, int] = defaultdict(int)
    previous_ts = None
    with source.open("rb", buffering=16 * 1024 * 1024) as handle:
        while True:
            header = handle.read(HEADER.size)
            if not header:
                break
            if len(header) != HEADER.size:
                raise ValueError("truncated block header")
            count = HEADER.unpack(header)[0]
            if count == 0 or count > MAX_TICKS_PER_BLOCK:
                raise ValueError(f"invalid tick count {count:,} in block {blocks + 1}")
            columns = (
                read_exact(handle, count * ITEM_SIZE, "timestamp column"),
                read_exact(handle, count * ITEM_SIZE, "bid column"),
                read_exact(handle, count * ITEM_SIZE, "ask column"),
            )
            timestamps = timestamps_from_le(columns[0])
            start = 0
            day = utc_day(timestamps[0])
            for index, value in enumerate(timestamps):
                if previous_ts is not None and value < previous_ts:
                    raise ValueError(f"timestamps are not chronological in block {blocks + 1}")
                previous_ts = value
                current_day = utc_day(value)
                if current_day != day:
                    append_segment(out_dir, symbol, day, columns, start, index)
                    daily_ticks[day] += index - start
                    start = index
                    day = current_day
            append_segment(out_dir, symbol, day, columns, start, count)
            daily_ticks[day] += count - start
            blocks += 1
            ticks += count
    if blocks == 0:
        raise ValueError("no bin_v1 blocks found")
    parsed_size = blocks * HEADER.size + ticks * BYTES_PER_TICK
    if parsed_size != source.stat().st_size:
        raise ValueError(f"BIN size validation failed: parsed={parsed_size:,}, actual={source.stat().st_size:,}")
    print(f"Converted {blocks:,} blocks / {ticks:,} ticks into {len(daily_ticks):,} daily files")
    return blocks, ticks, len(daily_ticks)


def read_existing_index(repo_id: str, token: str):
    entries: dict[str, Entry] = {}
    meta: dict[str, tuple[str, str]] = {}
    try:
        with tempfile.TemporaryDirectory() as td:
            path = hf_hub_download(repo_id=repo_id, repo_type="dataset", filename="index.txt", token=token, local_dir=td)
            text = Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        print(f"No existing index.txt loaded ({exc}); a new catalog will be created.")
        return entries, meta
    for line in text.splitlines():
        parts = line.split("|")
        if line.startswith("SYM|") and len(parts) >= 5:
            meta[parts[1].upper()] = (parts[2], parts[4])
        elif line.startswith("FILE|") and len(parts) >= 4 and "/" in parts[1]:
            try:
                size = int(parts[3])
            except ValueError:
                continue
            name = parts[1].split("/", 1)[1]
            match = DAILY_RE.match(name)
            if not match:
                continue
            symbol = match.group("symbol").upper()
            day = match.group("day")
            rel = f"{symbol}/{symbol}_{day}.BIN"
            entries[rel] = Entry(symbol, rel, day, size)
    return entries, meta


def local_entries(folder: Path) -> dict[str, Entry]:
    result = {}
    for path in folder.rglob("*.BIN"):
        match = DAILY_RE.match(path.name)
        if not match:
            continue
        symbol = match.group("symbol").upper()
        day = match.group("day")
        rel = f"{symbol}/{symbol}_{day}.BIN"
        result[rel] = Entry(symbol, rel, day, path.stat().st_size, path)
    return result


def build_index(entries: dict[str, Entry], meta: dict[str, tuple[str, str]]) -> str:
    grouped: dict[str, list[Entry]] = defaultdict(list)
    for entry in entries.values():
        grouped[entry.symbol].append(entry)
    lines = ["BRIDX1"]
    for symbol in sorted(grouped):
        display, digits = meta.get(symbol, (symbol, "5"))
        lines.append(f"SYM|{symbol}|{display}|0|{digits}")
        for entry in sorted(grouped[symbol], key=lambda item: item.day):
            lines.append(f"FILE|{entry.path}|{entry.day}|{entry.size}")
    return "\n".join(lines) + "\n"


def upload_daily(folder: Path, repo_id: str, token: str, symbol: str, digits: str) -> None:
    api = HfApi(token=token)
    api.create_repo(repo_id=repo_id, repo_type="dataset", exist_ok=True, private=False)
    existing, meta = read_existing_index(repo_id, token)
    local = local_entries(folder)
    if not local:
        raise ValueError("no daily BIN files were created")
    old_display = meta.get(symbol, (symbol, digits))[0]
    meta[symbol] = (old_display, digits)
    changed = {k: v for k, v in local.items() if k not in existing or existing[k].size != v.size}
    if not changed:
        print("All daily files already exist with matching sizes; upload skipped.")
        return
    merged = dict(existing)
    merged.update(local)
    with tempfile.TemporaryDirectory() as td:
        stage = Path(td) / "stage"
        stage.mkdir()
        for rel, entry in changed.items():
            destination = stage / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(entry.source, destination)
            except OSError:
                shutil.copy2(entry.source, destination)
        (stage / "index.txt").write_text(build_index(merged, meta), encoding="utf-8")
        print(f"Uploading {len(changed)} daily BIN file(s) + index.txt to {repo_id}")
        retry("upload_folder", lambda: api.upload_folder(
            repo_id=repo_id,
            repo_type="dataset",
            folder_path=stage,
            path_in_repo="",
            token=token,
            commit_message=f"Migrate {symbol} annual Release BIN to daily files",
        ))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-zip-url", required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--digits", required=True)
    parser.add_argument("--repo-id", default=os.getenv("HF_DATASET", "Esmaeil9ss/Tickdata"))
    parser.add_argument("--hf-token", default=os.getenv("HF_TOKEN"))
    parser.add_argument("--github-token", default=os.getenv("GITHUB_TOKEN"))
    args = parser.parse_args()
    if not args.hf_token:
        raise SystemExit("error: HF_TOKEN is required")
    symbol = args.symbol.strip().upper()
    filename = validate_release_asset_url(args.release_zip_url)
    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        zip_path = work / filename
        download_zip(args.release_zip_url, zip_path, args.github_token)
        annual_bin = safe_extract_single_bin(zip_path, work / "extracted")
        daily_dir = work / "daily"
        split_annual_bin(annual_bin, daily_dir, symbol)
        upload_daily(daily_dir, args.repo_id, args.hf_token, symbol, args.digits)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
