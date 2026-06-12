"""
State of Decay (Xbox 360) texture (`*.dds.0` / `*_dds.0`) support.

These are DirectDraw Surface (DDS) textures stored in Xbox 360 GPU memory
order. Two transforms separate the on-disk 360 data from a normal PC DDS:

  1. **Byte order** — the 360 reads texture memory big-endian, so the bytes of
     every 16-bit word are swapped. (Involutive: swapping twice is a no-op.)

  2. **Tiling** — larger textures are stored in the 360's tiled/swizzled memory
     layout (XGAddress2DTiledOffset). Block order must be de-tiled to get a
     linear PC texture, and re-tiled when writing back. Small textures (under a
     full 32-block-tall macro tile) are stored linearly and are left alone.

Converting 360 -> PC:  byte-swap the pixel data, then untile it.
Converting PC -> 360:  tile the pixel data, then byte-swap it.

The DDS header (width/height/format/mips) is preserved unchanged.

Untiling code adapted from leeao's public-domain Noesis Xbox 360 tile library
(XGAddress2DTiledOffset), which in turn follows the Xbox 360 XDK.
"""

import struct

DDS_MAGIC = b"DDS "
DDS_HEADER_SIZE = 124              # not counting the 4-byte magic
DDS_DATA_OFFSET = 4 + DDS_HEADER_SIZE  # 128

# A texture is stored tiled once it is at least one macro-tile (32 blocks) tall.
TILE_BLOCKS = 32


def is_dds(data):
    return len(data) >= 4 and data[:4] == DDS_MAGIC


def parse_header(data):
    """Return a dict of the useful DDS header fields."""
    if not is_dds(data):
        raise ValueError("not a DDS file")
    size, flags, height, width, pitch, depth, mips = struct.unpack_from(
        "<IIIIIII", data, 4)
    fourcc = data[84:88]
    dx10 = fourcc == b"DX10"
    data_offset = DDS_DATA_OFFSET + (20 if dx10 else 0)
    return {
        "width": width,
        "height": height,
        "mips": mips,
        "fourcc": fourcc,
        "data_offset": data_offset,
        "dx10": dx10,
    }


def _swap16(buf):
    """Swap the bytes of every 16-bit word. Involutive."""
    b = bytearray(buf)
    n = len(b) - (len(b) & 1)
    b[0:n:2], b[1:n:2] = b[1:n:2], b[0:n:2]
    return bytes(b)


# --------------------------------------------------------------------------
# Xbox 360 tiled-address arithmetic (block granularity).
# --------------------------------------------------------------------------
def _xg_tiled_x(offset, width_blocks, texel_pitch):
    aw = (width_blocks + 31) & ~31
    logbpp = (texel_pitch >> 2) + ((texel_pitch >> 1) >> (texel_pitch >> 2))
    ob = offset << logbpp
    ot = ((ob & ~4095) >> 3) + ((ob & 1792) >> 2) + (ob & 63)
    om = ot >> (7 + logbpp)
    macro_x = (om % (aw >> 5)) << 2
    tile = (((ot >> (5 + logbpp)) & 2) + (ob >> 6)) & 3
    macro = (macro_x + tile) << 3
    micro = ((((ot >> 1) & ~15) + (ot & 15)) & ((texel_pitch << 3) - 1)) >> logbpp
    return macro + micro


def _xg_tiled_y(offset, width_blocks, texel_pitch):
    aw = (width_blocks + 31) & ~31
    logbpp = (texel_pitch >> 2) + ((texel_pitch >> 1) >> (texel_pitch >> 2))
    ob = offset << logbpp
    ot = ((ob & ~4095) >> 3) + ((ob & 1792) >> 2) + (ob & 63)
    om = ot >> (7 + logbpp)
    macro_y = (om // (aw >> 5)) << 2
    tile = ((ot >> (6 + logbpp)) & 1) + ((ob & 2048) >> 10)
    macro = (macro_y + tile) << 3
    micro = ((((ot & (((texel_pitch << 6) - 1) & ~31)) + ((ot & 15) << 1))
             >> (3 + logbpp)) & ~1)
    return macro + micro + ((ot & 16) >> 4)


def _block_params(fourcc):
    """Return (texel_pitch_bytes, is_block) for a DDS fourcc."""
    fourcc = fourcc.upper()
    if fourcc in (b"DXT1", b"CTX1"):
        return 8, True
    if fourcc in (b"DXT2", b"DXT3", b"DXT4", b"DXT5", b"ATI2"):
        return 16, True
    return 0, False   # uncompressed or unknown: no tiling handled


def build_dds_from_mip(header_dds, mip_data, width, height):
    """Construct a standalone DDS (PC layout) from a `.dds.0` header and a raw
    mip's pixel bytes, overriding the width/height to that mip's dimensions and
    clearing the mip count (single surface).

    `header_dds` is the full `.dds.0` entry bytes (>=128). `mip_data` is the raw
    (still Xbox-360-order) bytes of the chosen mip level."""
    hdr = bytearray(header_dds[:128])
    struct.pack_into("<I", hdr, 12, height)   # dwHeight
    struct.pack_into("<I", hdr, 16, width)    # dwWidth
    struct.pack_into("<I", hdr, 28, 1)        # dwMipMapCount = 1
    return bytes(hdr) + mip_data


def mip_dimensions(base_w, base_h, level, max_level):
    """Dimensions of mip `level` (0 = smallest stored) given the highest stored
    level corresponds to the base resolution. Each step halves the size."""
    shift = max_level - level
    w = max(1, base_w >> shift)
    h = max(1, base_h >> shift)
    return w, h


def assemble_texture(header_bytes, mip_data, level, max_level):
    """Given a `.dds.N` mip (raw Xbox-360 bytes) plus the `.dds.0` header, return
    a PC-format standalone DDS for that mip (byte-swapped + de-tiled), ready for
    decoding or saving. `level` is this mip's index, `max_level` the highest."""
    hdr = parse_header(header_bytes)
    w, h = mip_dimensions(hdr["width"], hdr["height"], level, max_level)
    full = build_dds_from_mip(header_bytes, mip_data, w, h)
    return to_pc(full)


def _is_tiled(width, height, fourcc):
    tp, block = _block_params(fourcc)
    if not block:
        return False
    return (height // 4) >= TILE_BLOCKS


def _untile_blocks(data, width, height, texel_pitch):
    """Tiled -> linear (de-tile)."""
    out = bytearray(len(data))
    bw, bh = width // 4, height // 4
    for j in range(bh):
        base = j * bw
        for i in range(bw):
            x = _xg_tiled_x(base + i, bw, texel_pitch)
            y = _xg_tiled_y(base + i, bw, texel_pitch)
            src = (base + i) * texel_pitch
            dst = (y * bw + x) * texel_pitch
            if dst + texel_pitch <= len(data):
                out[dst:dst + texel_pitch] = data[src:src + texel_pitch]
    return bytes(out)


def _tile_blocks(data, width, height, texel_pitch):
    """Linear -> tiled (inverse of _untile_blocks)."""
    out = bytearray(len(data))
    bw, bh = width // 4, height // 4
    for j in range(bh):
        base = j * bw
        for i in range(bw):
            x = _xg_tiled_x(base + i, bw, texel_pitch)
            y = _xg_tiled_y(base + i, bw, texel_pitch)
            dst = (base + i) * texel_pitch
            src = (y * bw + x) * texel_pitch
            if src + texel_pitch <= len(data):
                out[dst:dst + texel_pitch] = data[src:src + texel_pitch]
    return bytes(out)


def to_pc(data):
    """Convert an Xbox 360 DDS to a normal PC DDS (byte-swap, then untile)."""
    if not is_dds(data):
        raise ValueError("not a DDS file")
    h = parse_header(data)
    off = h["data_offset"]
    pixels = _swap16(data[off:])
    tp, block = _block_params(h["fourcc"])
    if block and _is_tiled(h["width"], h["height"], h["fourcc"]):
        pixels = _untile_blocks(pixels, h["width"], h["height"], tp)
    return data[:off] + pixels


def to_xbox(data):
    """Convert a PC DDS to Xbox 360 order (tile, then byte-swap)."""
    if not is_dds(data):
        raise ValueError("not a DDS file")
    h = parse_header(data)
    off = h["data_offset"]
    pixels = data[off:]
    tp, block = _block_params(h["fourcc"])
    if block and _is_tiled(h["width"], h["height"], h["fourcc"]):
        pixels = _tile_blocks(pixels, h["width"], h["height"], tp)
    return data[:off] + _swap16(pixels)


def import_replace_pixels(original_360, pc_dds):
    """Build a new Xbox-360 entry by keeping the ORIGINAL 360 header (with its
    engine-specific tags/flags, e.g. FYRC/NVTT) and replacing only the pixel
    data with the imported PC texture's pixels, converted to 360 order.

    This is what should be written back into the pak: the game relies on the
    original header, so a freshly-authored PC DDS header (from Paint.NET / GIMP)
    must not be used. Only the base surface is taken from the import (any extra
    mip levels a PC editor appended are ignored)."""
    oh = parse_header(original_360)
    ph = parse_header(pc_dds)
    if (oh["width"], oh["height"]) != (ph["width"], ph["height"]):
        raise ValueError(
            "imported texture is %dx%d but the original is %dx%d" %
            (ph["width"], ph["height"], oh["width"], oh["height"]))

    pc_pixels = pc_dds[ph["data_offset"]:]
    tp, block = _block_params(oh["fourcc"])
    if block:
        base_size = (oh["width"] // 4) * (oh["height"] // 4) * tp
        pc_pixels = pc_pixels[:base_size]          # drop any appended mips
        if _is_tiled(oh["width"], oh["height"], oh["fourcc"]):
            pc_pixels = _tile_blocks(pc_pixels, oh["width"], oh["height"], tp)
    else:
        # uncompressed: keep exactly the original surface size
        base_size = len(original_360) - oh["data_offset"]
        pc_pixels = pc_pixels[:base_size]

    return original_360[:oh["data_offset"]] + _swap16(pc_pixels)


# Back-compat alias (older code called convert() for the symmetric swap-only).
convert = to_pc


def describe(data):
    """Short human description for the UI."""
    try:
        h = parse_header(data)
        cc = h["fourcc"].decode("latin-1", "replace").strip("\x00") or "uncompressed"
        tiled = "tiled" if _is_tiled(h["width"], h["height"], h["fourcc"]) else "linear"
        return "DDS texture  %dx%d  %s  (Xbox 360, %s)" % (
            h["width"], h["height"], cc, tiled)
    except Exception:
        return "DDS texture"

