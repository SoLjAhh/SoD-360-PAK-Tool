[README.md](https://github.com/user-attachments/files/28866966/README.md)
# State of Decay (Xbox 360) PAK Tool

<img width="1920" height="1080" alt="Screenshot (1066)" src="https://github.com/user-attachments/assets/f48f5b70-309a-41c1-a1f6-f60b3e74e160" />

<img width="1920" height="1080" alt="Screenshot (1070)" src="https://github.com/user-attachments/assets/5eab7780-6198-4a15-8d7c-67ed69edbf37" />

Extract, view, edit and repack the **State of Decay** 360 Arcade `.pak`
archives (`gamedata.pak` and friends). Pure Python, **no external
dependencies**, no compiler needed.

## Why this tool exists

The 360 Arcade build stores its `.pak` files as a ZIP container, but the
entries use a **custom compression method (id 13)** that is actually
**Microsoft LZX** — the same codec used by Xbox/CAB compression — with a
17‑bit window and a chunked, continuous‑window framing.

That's why the archive *opens* in WinRAR/7‑Zip (it sees the ZIP directory) but
**fails to extract** the contents, and why the PC‑era tools (SoDET, SOD_Tools,
quickBMS) error out — those expect the **PC** build, which uses plain
zlib/deflate. This tool implements the 360 format directly, so it extracts
every entry with a byte‑exact CRC match.

## Contents

| File | What it is |
|------|------------|
| `sod_pak_tool_all.py` | **Single-file build** — everything below combined into one script. Use this for py2exe / PyInstaller. |
| `sod_pak_tool.pyw` | The GUI (double‑click on Windows). Browse, preview, edit, extract, repack. |
| `sod_pak_cli.py`   | Command‑line interface for scripting / quick verification. |
| `sod_pak.py`       | Archive reader/writer (parsing, framing, repacking). |
| `sod_lzx.py`       | Pure‑Python LZX decompressor. |
| `sod_dds.py`       | Xbox‑360 ⇄ PC `.dds` texture converter. |
| `sod_ddsview.py`   | DXT1/3/5 decoder for the in‑tool texture preview. |
| `sod_btxt.py`      | `.btxt` (`TXDB`) string‑table parser / builder. |

The four‑file version and the single‑file `sod_pak_tool_all.py` are
functionally identical — pick whichever is more convenient.

## Building a Windows .exe

`sod_pak_tool_all.py` has no imports outside the Python standard library, so it
freezes cleanly:

**PyInstaller** (recommended, simplest):
```
pip install pyinstaller
pyinstaller --onefile --windowed sod_pak_tool_all.py
```
The `.exe` appears in `dist/`. (`--windowed` hides the console for the GUI; drop
it if you also want to use the `cli` sub‑command from a terminal.)

**py2exe** — a minimal `setup.py`:
```python
from distutils.core import setup
import py2exe
setup(windows=["sod_pak_tool_all.py"])
```
then `python setup.py py2exe`.

Run the frozen program with no arguments to launch the GUI, or
`sod_pak_tool_all.exe cli verify gamedata.pak` for the command line.


## Requirements

* Python 3.8 or newer (with `tkinter`, which is included in the standard
  Windows and macOS Python installers).

## Using the GUI

Double‑click `sod_pak_tool.pyw` (or run `python sod_pak_tool.pyw`), then:

1. **Open PAK** and pick `gamedata.pak`.
2. Browse the file tree. Click any entry to preview it. The tool recognises
   several formats and adapts:
   * **Text** (`.xml`, `.lua`, `.ent`, `.txt`, …) — shows as editable text;
     edit in place and **Save Edits to Entry**.
   * **Textures** (`.dds.0`) — Xbox‑360 DDS. The tool shows a **live preview**
     of the decoded texture (transparent areas drawn over grey so you can see
     them). Buttons appear to **Export as PC .dds** (byte‑swaps to a normal DDS
     you can open in Paint.NET / Photoshop / GIMP) and **Import PC .dds** (swaps
     your edited texture back into 360 order). Note: many of these textures have
     transparent edges, so a checkerboard or blank border in your image editor
     is normal — that's the alpha channel, not a conversion error. The live
     preview confirms what the texture actually contains.
   * **String tables** (`.btxt`, magic `TXDB`) — click **Edit Strings…** for a
     searchable list of every in‑game string; edit the wording and save. String
     IDs are preserved automatically.
   * **Scaleform UI** (`.gfx`) — compiled Flash, not text‑editable; replace it
     with **Edit ▸ Import File Into Entry…**.
   * Everything else shows a hex preview; replace via **Import File Into Entry…**.
3. **Extract Selected** / **Extract All** to write files to a folder.
4. **Save PAK As** to write a new archive with your edits. Unmodified entries
   are copied byte‑for‑byte; edited entries are re‑framed as method‑13 blocks
   the game can read.

> Tip: always **Save PAK As** a copy and keep your original `gamedata.pak`
> backed up.

## Using the CLI

```
python sod_pak_cli.py list      gamedata.pak
python sod_pak_cli.py verify    gamedata.pak
python sod_pak_cli.py extract   gamedata.pak ./out
python sod_pak_cli.py extract   gamedata.pak ./out scripts/   # filter by substring
python sod_pak_cli.py replace   gamedata.pak entities/aispawner.ent myedit.ent new.pak
python sod_pak_cli.py dds       class3_banners_ia.dds.0 banner.dds   # 360 <-> PC (reversible)
python sod_pak_cli.py btxt-dump english.xbox360.btxt                 # list every string
```

(For the single‑file build, prefix with `cli`, e.g.
`python sod_pak_tool_all.py cli verify gamedata.pak`.)

## Format notes (for the curious / for other SoD files)

* The container is a standard ZIP (`PK\x03\x04` local headers, central
  directory, EOCD) with `method = 13`.
* Each entry payload is one or more **32 KB output blocks**:
  * **Stored** small files: a short `FF 00 …` header followed by the raw bytes
    (located by CRC).
  * **LZX** files: each block body is preceded by either a **5‑byte `FF`
    header** (final block) or a **2‑byte big‑endian compressed‑length prefix**
    (earlier blocks). All blocks of one entry feed a **single continuous LZX
    window** — the dictionary is *not* reset between blocks — and the bitstream
    re‑aligns to a 16‑bit boundary at each 32 KB block boundary.
* Repacking writes edited entries as LZX **uncompressed blocks** (valid
  method‑13 frames), so the game reads them without needing a full LZX
  compressor.

All 1135 entries of the shipped `gamedata.pak` extract and re‑verify with exact
CRC matches, and edited‑and‑repacked archives re‑read cleanly.

## The asset formats inside the pak

* **`.dds.0` textures** — DDS files stored in Xbox‑360 GPU order. Supported
  formats: **DXT1, DXT5, ATI2/3Dc** (normal maps), **CTX1** (360 two‑channel
  normal maps) and **uncompressed** 32‑bit. Two transforms separate the on‑disk
  data from a normal PC DDS: a 16‑bit **byte swap** (the 360 reads texture
  memory big‑endian) and, for larger textures, a **tiled/swizzled** block
  layout (`XGAddress2DTiledOffset`). The tool de‑tiles + byte‑swaps on export
  and re‑tiles + byte‑swaps on import, so the round‑trip is exact. Small
  textures (under one 32‑block‑tall macro tile) are stored linearly and pass
  through untiled.

  Some paks (e.g. `textures.pak`) split a texture across **mip‑level files**:
  `name.dds.0` holds the DDS header plus the smallest mips, and `name.dds.1`,
  `.dds.2`, … hold progressively larger mips (the highest number is the full
  resolution). The tool groups these automatically and uses the header + the
  largest mip for the live preview and for export. (Re‑importing an edited
  split‑mip texture isn't supported yet — that requires rebuilding the whole
  mip chain; standalone `.dds` textures as in `gamedata.pak` import normally.)

  This is why a raw `.dds.0`/`.dds.N` looks wrong in an image editor — the live
  preview in the tool decodes it correctly so you can see the real texture.

  When importing an edited texture, the tool **keeps the original 360 header**
  (with its engine‑specific tags and flags) and replaces only the pixel data,
  converted back to 360 order. A PC editor's own DDS header (Paint.NET / GIMP)
  is not written into the pak, since the game relies on the original header.
  Keep the same dimensions and ideally the same compression format as the
  original; extra mip levels a PC editor appends are ignored.

* **`.btxt` string tables** (`TXDB`) — localized text. Big‑endian header
  (`TXDB`, version, flags, count), then `count` sorted 32‑bit string‑ID hashes,
  then `count` null‑terminated UTF‑8 strings in the same order. The tool keeps
  the hashes exactly as found and only rewrites the strings, so you can change
  any wording but can't add or remove entries.

* **`.gfx` UI** — Scaleform GFX (compiled Flash/SWF), used for the HUD and
  menus. Not text‑editable; replace by importing a `.gfx` built elsewhere.
