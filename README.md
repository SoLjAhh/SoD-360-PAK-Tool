[README.md](https://github.com/user-attachments/files/28866833/README.md)
# Prison Architect — Xbox 360 Archive Tool

<img width="1920" height="1080" alt="Screenshot (1057)" src="https://github.com/user-attachments/assets/703462ff-b14f-4bef-aa82-e3d5c94585f1" />

<img width="1920" height="1080" alt="Screenshot (1058)" src="https://github.com/user-attachments/assets/4280aa41-9512-4f82-a866-0047cf0b817e" />


Open, extract, edit, and repack the Xbox 360 Edition's `main.pkdxbox360`
/ `main.pkixbox360` archive for modding. The Xbox 360 build uses this
custom paired format instead of the PC version's `main.dat` (a RAR).

Pure Python 3 + tkinter — **no external dependencies**, no ffmpeg, no pip
installs.

## Files

| File | What it is |
|------|------------|
| `pa360_gui.pyw` | Double-click GUI (runs without a console on Windows). |
| `pa360_pkg.py`  | The engine — also a standalone command-line tool. |

Keep both in the same folder.

## Quick start (GUI)

1. Double-click **`pa360_gui.pyw`**.
2. **Open Archive…** → pick `main.pkdxbox360` (the `.pki` is found
   automatically — they must be in the same folder).
3. **Extract All…** → choose an empty folder. You get one file per
   asset plus a `manifest.json`. File names look like
   `00311_c10bce5c000072a3.txt` — `index_hash.ext`.
4. Edit the `.txt` / `.png` / `.bin` files you want. **Don't rename
   them** — the leading index is how repack matches them back.
5. **Repack…** → point at that folder, choose where to save the new
   `main.pkdxbox360`. The matching `.pki` is written next to it.

Rows are colour-coded: **green = zlib (fully editable)**, **blue = PNG
(raw)**, **red = Hx (compressed, see limitations)**.

## Command line

```bash
# inspect
python pa360_pkg.py list   main.pkdxbox360

# extract everything to ./out  (+ manifest.json)
python pa360_pkg.py extract main.pkdxbox360 -o out

# rebuild from your edited folder
python pa360_pkg.py repack  main.pkdxbox360 out \
        --out-pkd new/main.pkdxbox360 --out-pki new/main.pkixbox360
```

## What round-trips perfectly

Re-packing the unmodified extraction reproduces the **byte-identical**
original `.pkd` and `.pki` (verified by checksum). zlib assets recompress
to the exact original bytes (level-9 zlib), so edits to text, config,
sprite, and lua files repack cleanly and the game reads them normally.
Files you don't touch are copied through verbatim.

## Limitation: "Hx" files

About 18% of entries (the larger binary assets) use a proprietary block
compressor whose stream starts with the ASCII tag `Hx`. Its decompressor
has **not** been reverse-engineered yet, so those entries:

- extract **verbatim** as `.hx` files (still the raw compressed bytes), and
- are passed through **unchanged** on repack.

This keeps every rebuilt archive valid; you simply can't yet edit the
content *inside* an Hx blob. Editing the green (zlib) and blue (PNG)
files — the bulk of the moddable game data — works fully today.

## Format notes (for the curious / future RE work)

All integers are **big-endian** (Xbox 360 = PowerPC).

```
main.pkd:  u32 version(=3) | u32 0 | u32 count | <blobs concatenated>
main.pki:  u32 version(=3) | u32 0
           header-echo entry (16B): u32 count | u32 file1_size | u32 0 | u32 12
           per file (16B): u8[8] name_hash | u32 size_hint | u32 end_offset
           trailer (8B): u8[8] name_hash of the final file (end implicit = EOF)
```

- A blob's start = the previous entry's `end_offset`; the first real
  blob starts at offset 12 (just past the `.pkd` header).
- `size_hint` in entry *i* = the packed size of file *i+1* **if** that
  file is zlib, else 0. (Reproduced exactly by the repacker.)
- Original path names are not stored — only an opaque 64-bit hash — so
  extracted files are named by index + hash.

Per-blob storage is auto-detected by magic bytes:
`89 50 4E 47` = stored PNG, `78 DA` = zlib, `48 78` (`Hx`) = the
undecoded block format.
