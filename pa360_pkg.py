#!/usr/bin/env python3
"""
pa360_pkg.py  -  Prison Architect Xbox 360 .pkd / .pki archive tool
====================================================================

Reverse-engineered container used by the Xbox 360 Edition of
Prison Architect (main.pkdxbox360 + main.pkixbox360).

This module is the engine. It can be imported, run as a CLI, or driven
by the bundled GUI (pa360_gui.pyw).

------------------------------------------------------------------------
FORMAT (reverse-engineered)
------------------------------------------------------------------------
Two files work as a pair:

  main.pkd<plat>   "package data"  - concatenated file blobs
  main.pki<plat>   "package index" - the table of contents

All integers are BIG-ENDIAN (Xbox 360 is a big-endian PowerPC machine).

.pkd layout:
    offset 0 : u32  version   (observed: 3)
    offset 4 : u32  reserved  (0)
    offset 8 : u32  count     (number of entries)
    offset 12: ... file blobs, tightly concatenated, no padding ...

.pki layout:
    offset 0 : u32  version   (3)
    offset 4 : u32  reserved  (0)
    then `count` entries of 16 bytes each:
        u8[8] name_hash      (opaque 64-bit hash of original path - names
                              themselves are NOT stored)
        u32   reserved       (always 0)
        u32   end_offset     (absolute offset in .pkd where this blob ENDS)

    A blob's start = previous blob's end_offset (first real blob starts
    at 12, just after the .pkd header). The final end_offset equals the
    total .pkd size.

    The very first index entry is a self-referential header echo
    (end_offset == 12, zero-length) and is skipped.

------------------------------------------------------------------------
PER-BLOB STORAGE
------------------------------------------------------------------------
Each blob is stored in one of three ways:

  1. STORED   - raw bytes, no compression. Currently only seen for PNGs
                (magic 89 50 4E 47).

  2. ZLIB     - a standard zlib stream (magic 78 DA, level 9). This is
                the overwhelming majority of assets: text/config files,
                lua, sprite atlases, name tables, etc. Recompressing the
                decompressed payload with zlib level 9 reproduces the
                original bytes EXACTLY, so these round-trip perfectly.

  3. HX       - a proprietary block compressor, magic ASCII "Hx"
                (48 78). Header:
                    u8[2] 'Hx'
                    u16   header_length (bytes, points at payload start)
                    u16   checksum
                    u32   total_blob_length
                    ... per-block size table ...
                The block decompressor is not yet reversed; these blobs
                are extracted verbatim (with a .hx extension) and skipped
                on repack. They are a minority (~18%) and tend to be the
                larger binary assets.

------------------------------------------------------------------------
"""

import os
import sys
import json
import struct
import zlib
import argparse

PKD_HEADER_LEN = 12
ENTRY_LEN = 16
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
ZLIB_MAGIC = b"\x78\xda"
HX_MAGIC = b"Hx"


class Entry:
    __slots__ = ("index", "name_hash", "start", "end")

    def __init__(self, index, name_hash, start, end):
        self.index = index
        self.name_hash = name_hash      # bytes(8)
        self.start = start
        self.end = end

    @property
    def size(self):
        return self.end - self.start

    @property
    def hash_hex(self):
        return self.name_hash.hex()


class Archive:
    """Read / extract / repack a PA Xbox 360 pkd+pki pair."""

    def __init__(self, pkd_path, pki_path):
        self.pkd_path = pkd_path
        self.pki_path = pki_path
        self.version = None
        self.entries = []
        self._pkd = None  # lazily memory-mapped bytes
        self._header_echo = None

    # ------------------------------------------------------------------ #
    # loading
    # ------------------------------------------------------------------ #
    def load(self):
        with open(self.pki_path, "rb") as f:
            pki = f.read()
        if len(pki) < 8:
            raise ValueError("pki file too small to be valid")

        self.version = struct.unpack_from(">I", pki, 0)[0]
        # reserved = pki[4:8]
        pkd_size = os.path.getsize(self.pkd_path)

        off = 8
        raw = []           # full 16-byte entries: (name_hash, end)
        while off + ENTRY_LEN <= len(pki):
            name_hash = pki[off:off + 8]
            # bytes 8:12 reserved (0)
            end = struct.unpack_from(">I", pki, off + 12)[0]
            raw.append((name_hash, end))
            off += ENTRY_LEN

        # An 8-byte trailer (just a hash) may remain: it belongs to the
        # final physical file, whose end offset is implicit == pkd size.
        trailer_hash = None
        if off + 8 <= len(pki):
            trailer_hash = pki[off:off + 8]

        prev_end = PKD_HEADER_LEN
        idx = 0
        self._header_echo = None
        for ridx, (name_hash, end) in enumerate(raw):
            start = prev_end
            if end < start:
                start = end
            if idx == 0 and end == PKD_HEADER_LEN and prev_end == PKD_HEADER_LEN:
                # leading self-referential header entry. It encodes
                # [u32 count][u32 first_file_size][u32 0][u32 12].
                # Preserve its raw 16 bytes for exact round-tripping.
                self._header_echo = pki[8:24]
                prev_end = end
                idx += 1
                continue
            self.entries.append(Entry(idx, name_hash, start, end))
            prev_end = end
            idx += 1

        # final file from the trailer hash, ending at the data file's end
        if trailer_hash is not None and prev_end < pkd_size:
            self.entries.append(Entry(idx, trailer_hash, prev_end, pkd_size))

        return self

    def _pkd_bytes(self):
        if self._pkd is None:
            with open(self.pkd_path, "rb") as f:
                self._pkd = f.read()
        return self._pkd

    # ------------------------------------------------------------------ #
    # per-blob access
    # ------------------------------------------------------------------ #
    def raw_blob(self, entry):
        return self._pkd_bytes()[entry.start:entry.end]

    @staticmethod
    def classify(blob):
        if len(blob) == 0:
            return "empty"
        if blob[:8] == PNG_MAGIC:
            return "stored"
        if blob[:2] == ZLIB_MAGIC:
            return "zlib"
        if blob[:2] == HX_MAGIC:
            return "hx"
        return "unknown"

    def decompress(self, entry):
        """Return (data_bytes, method, ok).

        method: 'stored' | 'zlib' | 'hx' | 'empty' | 'unknown'
        ok: True if `data_bytes` is usable game content,
            False if the blob is returned verbatim because we can't decode it
            (currently only the 'hx' compressor).
        """
        blob = self.raw_blob(entry)
        kind = self.classify(blob)
        if kind == "empty":
            return b"", "empty", True
        if kind == "stored":
            return blob, "stored", True
        if kind == "zlib":
            try:
                return zlib.decompress(blob), "zlib", True
            except zlib.error:
                return blob, "zlib", False
        if kind == "hx":
            # not yet decodable -> hand back the raw compressed blob
            return blob, "hx", False
        return blob, "unknown", False

    @staticmethod
    def detect_ext(data, method):
        if method == "hx":
            return "hx"
        if not data:
            return "bin"
        if data[:8] == PNG_MAGIC:
            return "png"
        if data[:4] == b"DDS ":
            return "dds"
        if data[:4] == b"RIFF":
            return "wav"
        # text heuristics (PA config files are ASCII/UTF-8)
        head = data[:64]
        if head[:3] == b"\xef\xbb\xbf":
            return "txt"
        if b"\x00" not in head:
            try:
                head.decode("utf-8")
                return "txt"
            except UnicodeDecodeError:
                pass
        return "bin"

    # ------------------------------------------------------------------ #
    # extraction
    # ------------------------------------------------------------------ #
    def extract_all(self, out_dir, progress=None):
        os.makedirs(out_dir, exist_ok=True)
        manifest = {
            "version": self.version,
            "pkd": os.path.basename(self.pkd_path),
            "pki": os.path.basename(self.pki_path),
            "entries": [],
        }
        n = len(self.entries)
        stats = {"stored": 0, "zlib": 0, "hx": 0, "empty": 0, "unknown": 0}
        for i, e in enumerate(self.entries):
            data, method, ok = self.decompress(e)
            ext = self.detect_ext(data, method)
            name = f"{e.index:05d}_{e.hash_hex}.{ext}"
            with open(os.path.join(out_dir, name), "wb") as fo:
                fo.write(data)
            stats[method] = stats.get(method, 0) + 1
            manifest["entries"].append({
                "index": e.index,
                "hash": e.hash_hex,
                "file": name,
                "method": method,
                "decoded": ok,
                "stored_size": e.size,
                "extracted_size": len(data),
            })
            if progress:
                progress(i + 1, n, name)
        with open(os.path.join(out_dir, "manifest.json"), "w") as fm:
            json.dump(manifest, fm, indent=2)
        return manifest, stats

    def extract_one(self, entry, out_path):
        data, method, ok = self.decompress(entry)
        with open(out_path, "wb") as fo:
            fo.write(data)
        return method, ok, len(data)

    # ------------------------------------------------------------------ #
    # repacking
    # ------------------------------------------------------------------ #
    def repack(self, mod_dir, out_pkd, out_pki, progress=None):
        """Rebuild a pkd+pki pair.

        For each entry, look for a replacement file in `mod_dir` named by
        the manifest convention (`{index:05d}_{hash}.{ext}`). If found and
        the original method is 'stored' or 'zlib', the new content is
        (re)compressed and substituted. 'hx' entries and any entry without
        a replacement keep their ORIGINAL bytes verbatim, so the archive
        stays valid even though we can't yet author Hx blobs.
        """
        # index modded files by the index prefix
        mod_by_index = {}
        if mod_dir and os.path.isdir(mod_dir):
            for fn in os.listdir(mod_dir):
                if fn == "manifest.json":
                    continue
                pfx = fn.split("_", 1)[0]
                if pfx.isdigit():
                    mod_by_index[int(pfx)] = os.path.join(mod_dir, fn)

        blobs = []
        n = len(self.entries)
        replaced = 0
        skipped_hx = 0
        for i, e in enumerate(self.entries):
            orig = self.raw_blob(e)
            kind = self.classify(orig)
            new_path = mod_by_index.get(e.index)

            if new_path and kind in ("stored", "zlib"):
                with open(new_path, "rb") as f:
                    content = f.read()
                if kind == "zlib":
                    blob = zlib.compress(content, 9)   # 78 DA, matches game
                else:  # stored
                    blob = content
                replaced += 1
            else:
                if new_path and kind == "hx":
                    skipped_hx += 1
                blob = orig  # verbatim
            blobs.append(blob)
            if progress:
                progress(i + 1, n, e.index)

        # write pkd
        with open(out_pkd, "wb") as fd:
            fd.write(struct.pack(">III", self.version or 3, 0, len(self.entries)))
            for b in blobs:
                fd.write(b)

        # Helper: the per-entry "size hint" field.
        # Rule (verified byte-exact against the original):
        #   field at slot i  =  stored size of file (i+1)  IF that file is
        #                       zlib-compressed, else 0.
        # The header-echo's first_size field is the special exception: it
        # always carries file 1's true stored size, even for PNG.
        def is_zlib(b):
            return b[:2] == ZLIB_MAGIC

        # write pki: header (8) + header-echo entry + (N-1) full entries +
        # 8-byte trailer hash for the final file (its end is implicit).
        with open(out_pki, "wb") as fi:
            fi.write(struct.pack(">II", self.version or 3, 0))
            # header-echo entry: [u32 count][u32 file1_size][u32 0][u32 12]
            count = len(self.entries)
            first_size = len(blobs[0]) if blobs else 0
            fi.write(struct.pack(">IIII", count, first_size, 0, PKD_HEADER_LEN))
            pos = PKD_HEADER_LEN
            last = len(self.entries) - 1
            for i, (e, b) in enumerate(zip(self.entries, blobs)):
                pos += len(b)
                # size hint = compressed size of NEXT file if it's zlib else 0
                if i + 1 < len(blobs) and is_zlib(blobs[i + 1]):
                    hint = len(blobs[i + 1])
                else:
                    hint = 0
                if i == last:
                    # final file: store only its hash; end offset is implicit
                    fi.write(e.name_hash)
                else:
                    fi.write(e.name_hash + struct.pack(">II", hint, pos))

        return {"replaced": replaced, "skipped_hx": skipped_hx,
                "total": len(self.entries)}


# ====================================================================== #
# CLI
# ====================================================================== #
def _guess_pair(path):
    """Given either the .pkd or .pki, return (pkd, pki)."""
    if "pkd" in os.path.basename(path):
        return path, path.replace("pkd", "pki")
    if "pki" in os.path.basename(path):
        return path.replace("pki", "pkd"), path
    raise ValueError("Could not tell whether this is a pkd or pki file")


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Prison Architect Xbox 360 .pkd/.pki archive tool")
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("list", help="list archive contents")
    pl.add_argument("archive", help="path to main.pkd... or main.pki...")

    pe = sub.add_parser("extract", help="extract all files")
    pe.add_argument("archive")
    pe.add_argument("-o", "--out", default="extracted")

    pr = sub.add_parser("repack", help="rebuild archive from a mod folder")
    pr.add_argument("archive")
    pr.add_argument("mod_dir")
    pr.add_argument("--out-pkd", required=True)
    pr.add_argument("--out-pki", required=True)

    args = p.parse_args(argv)
    pkd, pki = _guess_pair(args.archive)
    ar = Archive(pkd, pki).load()

    if args.cmd == "list":
        from collections import Counter
        c = Counter()
        for e in ar.entries:
            data, method, ok = ar.decompress(e)
            ext = ar.detect_ext(data, method)
            c[(method, ext)] += 1
            print(f"{e.index:5d}  {e.hash_hex}  {method:7s}  "
                  f"{e.size:9d} -> {len(data):9d}  .{ext}")
        print(f"\n{len(ar.entries)} entries")
        for (m, ext), n in sorted(c.items()):
            print(f"  {m:8s} .{ext:4s} : {n}")

    elif args.cmd == "extract":
        def prog(i, n, name):
            if i % 100 == 0 or i == n:
                print(f"\r  {i}/{n}", end="", flush=True)
        manifest, stats = ar.extract_all(args.out, progress=prog)
        print(f"\nExtracted {len(manifest['entries'])} files to {args.out}/")
        print("  by method:", dict(stats))
        print("  (edit the .png/.txt/.bin files, then `repack` to rebuild)")

    elif args.cmd == "repack":
        def prog(i, n, idx):
            if i % 100 == 0 or i == n:
                print(f"\r  {i}/{n}", end="", flush=True)
        res = ar.repack(args.mod_dir, args.out_pkd, args.out_pki, progress=prog)
        print(f"\nRepacked. replaced={res['replaced']} "
              f"hx_skipped={res['skipped_hx']} total={res['total']}")
        print(f"  wrote {args.out_pkd} + {args.out_pki}")


if __name__ == "__main__":
    main()
