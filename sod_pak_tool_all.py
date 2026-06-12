#!/usr/bin/env python3
# =============================================================================
#  State of Decay (Xbox 360) PAK Tool - single-file build
# =============================================================================
#  A combined, dependency-free build of the LZX decompressor, the PAK archive
#  reader/writer, the tkinter GUI, and the command-line interface.
#
#  USAGE
#    Double-click, or:  python sod_pak_tool_all.py          -> launches the GUI
#                       python sod_pak_tool_all.py cli ...   -> command line
#
#  Freezing to a Windows .exe (py2exe or PyInstaller):
#    PyInstaller:  pyinstaller --onefile --windowed sod_pak_tool_all.py
#    py2exe:       use a setup.py with windows=["sod_pak_tool_all.py"]
#
#  Requires only the Python standard library (tkinter is part of the standard
#  Windows/macOS Python installers).
# =============================================================================

import os
import sys
import struct
import zlib
import threading
import traceback


# ==========================================================================
# LZX DECOMPRESSOR  (from sod_lzx.py)
# ==========================================================================

LZX_MIN_MATCH = 2
LZX_NUM_CHARS = 256
LZX_BLOCKTYPE_VERBATIM = 1
LZX_BLOCKTYPE_ALIGNED = 2
LZX_BLOCKTYPE_UNCOMPRESSED = 3
LZX_PRETREE_NUM_ELEMENTS = 20
LZX_ALIGNED_NUM_ELEMENTS = 8
LZX_NUM_PRIMARY_LENGTHS = 7
LZX_NUM_SECONDARY_LENGTHS = 249

LZX_PRETREE_MAXSYMBOLS = LZX_PRETREE_NUM_ELEMENTS
LZX_PRETREE_TABLEBITS = 6
LZX_MAINTREE_MAXSYMBOLS = LZX_NUM_CHARS + 290 * 8
LZX_MAINTREE_TABLEBITS = 12
LZX_LENGTH_MAXSYMBOLS = LZX_NUM_SECONDARY_LENGTHS + 1
LZX_LENGTH_TABLEBITS = 12
LZX_ALIGNED_MAXSYMBOLS = LZX_ALIGNED_NUM_ELEMENTS
LZX_ALIGNED_TABLEBITS = 7

# position base / extra-bits tables (built once for window_bits up to 21)
position_base = []
extra_bits = []


def _build_tables():
    global position_base, extra_bits
    extra_bits = [0] * 52
    j = 0
    for i in range(0, 52, 2):
        extra_bits[i] = j
        extra_bits[i + 1] = j
        if i != 0 and j < 17:
            j += 1
    position_base = [0] * 51
    j = 0
    for i in range(51):
        position_base[i] = j
        j += 1 << extra_bits[i]


_build_tables()


class _BitReader:
    """MSB-first bit reader over 16-bit little-endian input words (LZX style).

    LZX packs bits MSB-first within a stream of 16-bit little-endian words.
    Each refill appends one 16-bit word at the low end of the bit buffer (the
    existing buffer is shifted up by 16). Bits are consumed from the high end.
    """

    __slots__ = ("data", "pos", "bitbuf", "bitcount", "end")

    def __init__(self, data):
        self.data = data
        self.pos = 0
        self.end = len(data)
        self.bitbuf = 0
        self.bitcount = 0

    def _fill_word(self):
        if self.pos + 1 < self.end:
            w = self.data[self.pos] | (self.data[self.pos + 1] << 8)
            self.pos += 2
        elif self.pos < self.end:
            w = self.data[self.pos]
            self.pos += 1
        else:
            w = 0
        self.bitbuf = (self.bitbuf << 16) | w
        self.bitcount += 16

    def ensure(self, n):
        while self.bitcount < n:
            self._fill_word()

    def peek(self, n):
        if n == 0:
            return 0
        self.ensure(n)
        return (self.bitbuf >> (self.bitcount - n)) & ((1 << n) - 1)

    def remove(self, n):
        self.bitcount -= n
        self.bitbuf &= (1 << self.bitcount) - 1

    def read(self, n):
        if n == 0:
            return 0
        v = self.peek(n)
        self.remove(n)
        return v

    def align(self):
        # consume remaining bits up to the next 16-bit boundary
        n = self.bitcount & 15
        if n:
            self.remove(n)

    def read_bytes_aligned(self, count):
        """Read raw bytes (only valid when bitcount is a multiple of 16, i.e.
        right after align()). Pulls from the bit buffer first, then the stream."""
        out = bytearray()
        while count > 0 and self.bitcount >= 8:
            self.bitcount -= 8
            out.append((self.bitbuf >> self.bitcount) & 0xFF)
            self.bitbuf &= (1 << self.bitcount) - 1
            count -= 1
        while count > 0 and self.pos < self.end:
            out.append(self.data[self.pos])
            self.pos += 1
            count -= 1
        return bytes(out)


def _make_decode_table(nsyms, lengths, tablebits=11):
    """Build a fast Huffman decode table.

    Returns (tablebits, table, max_len, long_codes) where `table` maps a
    `tablebits`-bit peek to either a symbol (for codes <= tablebits) or, for
    longer codes, an index resolved via `long_codes` (a dict of
    (length, code)->symbol). Canonical LZX code assignment is used.
    """
    max_len = 0
    for L in lengths:
        if L > max_len:
            max_len = L
    if max_len == 0:
        return (tablebits, [(-1, 0)] * (1 << tablebits), 0, {})
    if tablebits > max_len:
        tablebits = max_len

    bl_count = [0] * (max_len + 1)
    for L in lengths:
        if L:
            bl_count[L] += 1
    next_code = [0] * (max_len + 2)
    code = 0
    for bits in range(1, max_len + 1):
        code = (code + bl_count[bits - 1]) << 1
        next_code[bits] = code

    table = [(-1, 0)] * (1 << tablebits)   # (symbol, length); symbol=-1 -> long
    long_codes = {}
    for sym in range(nsyms):
        L = lengths[sym]
        if not L:
            continue
        c = next_code[L]
        next_code[L] += 1
        if L <= tablebits:
            # left-justify within tablebits and fill all matching slots
            shift = tablebits - L
            start = c << shift
            entry = (sym, L)
            for k in range(1 << shift):
                table[start + k] = entry
        else:
            long_codes[(L, c)] = sym
    return (tablebits, table, max_len, long_codes)


def _huff_read(br, tbl):
    tablebits, table, max_len, long_codes = tbl
    look = tablebits if tablebits < max_len else max_len
    peek = br.peek(look)
    sym, L = table[peek]
    if sym >= 0:
        br.remove(L)
        return sym
    # long code (> tablebits): peek the full max_len window once and match
    bits = br.peek(max_len)
    for length in range(tablebits + 1, max_len + 1):
        code = bits >> (max_len - length)
        s = long_codes.get((length, code))
        if s is not None:
            br.remove(length)
            return s
    raise ValueError("bad huffman code")


def _position_slots(window_bits):
    # number of main-tree position slots for a given window size
    return {15: 30, 16: 32, 17: 34, 18: 36, 19: 38,
            20: 42, 21: 50}[window_bits]


class LZXDecoder:
    def __init__(self, window_bits=17):
        self.window_bits = window_bits
        self.window_size = 1 << window_bits
        self.window = bytearray(self.window_size)
        self.window_posn = 0
        self.R0 = self.R1 = self.R2 = 1
        self.header_read = False
        self.block_remaining = 0
        self.block_type = 0
        # main tree element count = 256 literals + 8 * num_position_slots
        self.main_elems = LZX_NUM_CHARS + 8 * _position_slots(window_bits)
        self.main_lengths = [0] * self.main_elems
        self.length_lengths = [0] * LZX_LENGTH_MAXSYMBOLS
        self.main_table = None
        self.length_table = None
        self.aligned_table = None
        self.output = bytearray()

    def _read_lengths(self, br, lens, first, last):
        """Read and delta-decode Huffman code lengths into lens[first:last],
        following the LZX pretree/run-length scheme (libmspack semantics)."""
        pre_lens = [br.read(4) for _ in range(LZX_PRETREE_NUM_ELEMENTS)]
        pre_table = _make_decode_table(LZX_PRETREE_NUM_ELEMENTS, pre_lens)
        i = first
        while i < last:
            z = _huff_read(br, pre_table)
            if z == 17:
                run = br.read(4) + 4
                while run and i < last:
                    lens[i] = 0; i += 1; run -= 1
            elif z == 18:
                run = br.read(5) + 20
                while run and i < last:
                    lens[i] = 0; i += 1; run -= 1
            elif z == 19:
                run = br.read(1) + 4
                z2 = _huff_read(br, pre_table)
                value = (lens[i] - z2) % 17
                while run and i < last:
                    lens[i] = value; i += 1; run -= 1
            else:
                lens[i] = (lens[i] - z) % 17
                i += 1

    def _start_block(self, br):
        self.block_type = br.read(3)
        hi = br.read(16); lo = br.read(8)
        self.block_remaining = (hi << 8) | lo
        bt = self.block_type
        if bt in (LZX_BLOCKTYPE_VERBATIM, LZX_BLOCKTYPE_ALIGNED):
            if bt == LZX_BLOCKTYPE_ALIGNED:
                al = [br.read(3) for _ in range(LZX_ALIGNED_NUM_ELEMENTS)]
                self.aligned_table = _make_decode_table(LZX_ALIGNED_NUM_ELEMENTS, al)
            # main tree: literals (0..255) then the position/length slots
            self._read_lengths(br, self.main_lengths, 0, LZX_NUM_CHARS)
            self._read_lengths(br, self.main_lengths, LZX_NUM_CHARS, self.main_elems)
            self.main_table = _make_decode_table(self.main_elems, self.main_lengths)
            # length tree
            self._read_lengths(br, self.length_lengths, 0, LZX_NUM_SECONDARY_LENGTHS)
            self.length_table = _make_decode_table(LZX_LENGTH_MAXSYMBOLS, self.length_lengths)
        elif bt == LZX_BLOCKTYPE_UNCOMPRESSED:
            br.align()
            self.R0 = int.from_bytes(br.read_bytes_aligned(4), "little")
            self.R1 = int.from_bytes(br.read_bytes_aligned(4), "little")
            self.R2 = int.from_bytes(br.read_bytes_aligned(4), "little")
        else:
            raise ValueError("bad LZX block type %d" % bt)

    def decode(self, br, out_bytes):
        """Decode out_bytes of output, appending to self.output. br persists
        across calls so a single LZX session can span multiple 32 KB blocks."""
        if not self.header_read:
            intel = br.read(1)
            if intel:
                # 32-bit intel filesize (SoD streams use intel=0; handle anyway)
                br.read(16); br.read(16)
            self.header_read = True

        produced = 0
        win = self.window
        wsize = self.window_size
        mask = wsize - 1
        wpos = self.window_posn

        while produced < out_bytes:
            if self.block_remaining == 0:
                self._start_block(br)

            this_run = self.block_remaining
            if this_run > (out_bytes - produced):
                this_run = out_bytes - produced

            bt = self.block_type
            if bt == LZX_BLOCKTYPE_UNCOMPRESSED:
                raw = br.read_bytes_aligned(this_run)
                for b in raw:
                    win[wpos] = b
                    wpos = (wpos + 1) & mask
                self.output += raw
                produced += len(raw)
                self.block_remaining -= len(raw)
                if len(raw) < this_run:
                    break
                continue

            aligned = (bt == LZX_BLOCKTYPE_ALIGNED)
            done = 0
            out = self.output
            while done < this_run:
                main = _huff_read(br, self.main_table)
                if main < LZX_NUM_CHARS:
                    win[wpos] = main
                    wpos = (wpos + 1) & mask
                    out.append(main)
                    done += 1
                    continue

                main -= LZX_NUM_CHARS
                length = main % 8
                if length == LZX_NUM_PRIMARY_LENGTHS:
                    length += _huff_read(br, self.length_table)
                length += LZX_MIN_MATCH
                pos_slot = main // 8

                if pos_slot == 0:
                    match_offset = self.R0
                elif pos_slot == 1:
                    match_offset = self.R1
                    self.R1 = self.R0; self.R0 = match_offset
                elif pos_slot == 2:
                    match_offset = self.R2
                    self.R2 = self.R0; self.R0 = match_offset
                else:
                    eb = extra_bits[pos_slot]
                    if aligned and eb >= 3:
                        verbatim = br.read(eb - 3) << 3
                        ali = _huff_read(br, self.aligned_table)
                        match_offset = position_base[pos_slot] - 2 + verbatim + ali
                    else:
                        verbatim = br.read(eb)
                        match_offset = position_base[pos_slot] - 2 + verbatim
                    self.R2 = self.R1; self.R1 = self.R0; self.R0 = match_offset

                src = (wpos - match_offset) & mask
                for _ in range(length):
                    b = win[src]
                    win[wpos] = b
                    out.append(b)
                    wpos = (wpos + 1) & mask
                    src = (src + 1) & mask
                done += length

            produced += done
            self.block_remaining -= done

        self.window_posn = wpos
        return produced


def decompress_stream(body, out_size, window_bits=17):
    """Decompress one continuous LZX stream (concatenated chunk bodies)."""
    br = _BitReader(body)
    dec = LZXDecoder(window_bits)
    remaining = out_size
    while remaining > 0:
        want = 32768 if remaining > 32768 else remaining
        n = dec.decode(br, want)
        if n <= 0:
            break
        remaining -= n
    return bytes(dec.output[:out_size])

# ==========================================================================
# PAK ARCHIVE READER / WRITER  (from sod_pak.py)
# ==========================================================================

CHUNK = 32768
SIG_LOCAL = b"PK\x03\x04"
SIG_CENTRAL = b"PK\x01\x02"
SIG_EOCD = b"PK\x05\x06"
METHOD_SOD = 13


def _crc(b):
    return zlib.crc32(b) & 0xFFFFFFFF


class PakEntry:
    __slots__ = ("name", "method", "flag", "crc", "csize", "usize",
                 "mtime", "mdate", "payload", "local_offset")

    def __init__(self, name, method, flag, crc, csize, usize, mtime, mdate,
                 payload, local_offset):
        self.name = name
        self.method = method
        self.flag = flag
        self.crc = crc
        self.csize = csize
        self.usize = usize
        self.mtime = mtime
        self.mdate = mdate
        self.payload = payload          # raw stored bytes for this entry
        self.local_offset = local_offset

    @property
    def is_stored(self):
        return self.usize == 0 or (
            self.payload[:2] == b"\xff\x00" and self.csize >= self.usize)


def _decode_stored(p, usize, crc):
    if usize == 0:
        return b""
    limit = min(40, len(p) - usize + 1)
    for h in range(0, max(1, limit)):
        cand = p[h:h + usize]
        if len(cand) == usize and _crc(cand) == crc:
            return cand
    raise ValueError("stored entry: could not locate raw bytes")


def _gather_lzx_bodies(p, usize):
    """Strip per-block framing headers and return the concatenated LZX bodies."""
    nblocks = (usize + CHUNK - 1) // CHUNK
    bodies = []
    o = 0
    for i in range(nblocks):
        last = (i == nblocks - 1)
        if o >= len(p):
            raise ValueError("ran out of payload while gathering blocks")
        if p[o] == 0xFF:
            o += 5
            if last:
                bodies.append(p[o:])
                o = len(p)
            else:
                # A non-final 0xFF block whose compressed length is not given by
                # a 2-byte prefix. Not present in the shipped gamedata.pak; if a
                # future archive needs it we would length-probe here.
                raise ValueError("unsupported consecutive 0xFF block framing")
        else:
            complen = struct.unpack_from(">H", p, o)[0]
            o += 2
            bodies.append(p[o:o + complen])
            o += complen
    return b"".join(bodies)


def decode_entry(entry):
    """Return the decompressed bytes for a PakEntry."""
    p = entry.payload
    usize = entry.usize
    crc = entry.crc
    if usize == 0:
        return b""

    # Stored entries (raw bytes after a short header) only occur when the
    # payload is at least as large as the data. Try the CRC-locating scan first;
    # if it fails, fall through to the LZX path (covers writer-produced frames
    # and any 0xFF00 stream that merely looks stored).
    if entry.payload[:2] == b"\xff\x00" and entry.csize >= usize:
        try:
            return _decode_stored(p, usize, crc)
        except ValueError:
            pass

    body = _gather_lzx_bodies(p, usize)
    br = _BitReader(body)
    dec = LZXDecoder(17)
    remaining = usize
    first = True
    while remaining > 0:
        # XMemCompress emits each 32 KB output block as a separate CFDATA frame;
        # the bitstream realigns to a 16-bit boundary at each block boundary.
        if not first:
            br.align()
        first = False
        want = CHUNK if remaining > CHUNK else remaining
        n = dec.decode(br, want)
        if n <= 0:
            break
        remaining -= n
    out = bytes(dec.output[:usize])
    if _crc(out) != crc:
        raise ValueError("CRC mismatch decoding %r" % entry.name)
    return out


class PakArchive:
    def __init__(self, path=None):
        self.entries = []
        self.raw = b""
        self.path = path
        if path:
            self.load(path)

    def load(self, path):
        with open(path, "rb") as f:
            self.raw = f.read()
        self.path = path
        self._parse()

    def _parse(self):
        data = self.raw
        self.entries = []
        off = 0
        n = len(data)
        while off < n - 4:
            sig = data[off:off + 4]
            if sig == SIG_LOCAL:
                (ver, flag, method, mtime, mdate, crc, csize, usize,
                 nlen, elen) = struct.unpack_from("<HHHHHIIIHH", data, off + 4)
                name = data[off + 30:off + 30 + nlen].decode("latin-1")
                pstart = off + 30 + nlen + elen
                payload = data[pstart:pstart + csize]
                self.entries.append(PakEntry(
                    name.replace("\\", "/"), method, flag, crc, csize, usize,
                    mtime, mdate, payload, off))
                off = pstart + csize
            elif sig == SIG_CENTRAL or sig == SIG_EOCD:
                break
            else:
                off += 1

    def extract(self, entry):
        return decode_entry(entry)

    def find(self, name):
        name = name.replace("\\", "/")
        for e in self.entries:
            if e.name == name:
                return e
        return None


def _encode_continuous_lzx(data):
    """Encode `data` as ONE continuous LZX bitstream of back-to-back
    uncompressed blocks, one per 32 KB of output, with a 16-bit alignment
    between blocks (matching how the decoder re-aligns at each block boundary).

    Returns (stream_bytes, block_spans) where block_spans gives the (start,end)
    byte slice of each 32 KB block within stream_bytes.

    An LZX uncompressed block is: 3-bit type(3) + 24-bit size, then the
    bitstream is aligned to a 16-bit boundary and the three R-registers (12
    bytes) plus the raw payload are written as plain little-endian bytes
    directly into the byte stream (not through the 16-bit bit-packer).
    """
    nblocks = max(1, (len(data) + CHUNK - 1) // CHUNK)
    stream = bytearray()
    block_starts = []

    bits = []

    def put(val, n):
        for i in range(n - 1, -1, -1):
            bits.append((val >> i) & 1)

    def flush_bits():
        # pack accumulated bits into 16-bit little-endian words (MSB-first) and
        # append to stream. Caller guarantees len(bits) % 16 == 0.
        for j in range(0, len(bits), 16):
            w = 0
            for b in bits[j:j + 16]:
                w = (w << 1) | b
            stream.extend(struct.pack("<H", w))
        bits.clear()

    # global intel-preprocessing flag, only meaningful for the first block's
    # header word, but the decoder reads it once at stream start.
    for i in range(nblocks):
        block = data[i * CHUNK:(i + 1) * CHUNK]
        # block byte boundary is 16-bit aligned (stream is always even here)
        if len(stream) % 2:
            stream.append(0)
        block_starts.append(len(stream))
        if i == 0:
            put(0, 1)             # intel preprocessing disabled (stream-level)
        put(3, 3)                 # uncompressed block type
        size = len(block)
        put((size >> 8) & 0xFFFF, 16)
        put(size & 0xFF, 8)
        while len(bits) % 16:
            bits.append(0)
        flush_bits()
        # R0,R1,R2 then raw bytes, written directly
        stream.extend(struct.pack("<III", 1, 1, 1))
        stream.extend(block)
        if len(stream) % 2:
            stream.append(0)      # keep next block 16-bit aligned

    block_spans = []
    for i in range(nblocks):
        start = block_starts[i]
        end = block_starts[i + 1] if i + 1 < nblocks else len(stream)
        block_spans.append((start, end))
    return bytes(stream), block_spans


def _build_payload(data):
    """Build the .pak entry payload for `data` (uncompressed LZX blocks),
    framed exactly as the decoder expects: a 2-byte big-endian length prefix on
    non-final blocks and a 5-byte 0xFF header on the final block."""
    if len(data) == 0:
        return b"\xff\x00\x00\x00\x00\x00"

    stream, block_spans = _encode_continuous_lzx(data)
    nblocks = len(block_spans)
    payload = bytearray()
    for i, (sb, eb) in enumerate(block_spans):
        last = (i == nblocks - 1)
        body = stream[sb:eb]
        if last:
            block_usize = len(data) - i * CHUNK
            payload += b"\xff\x00" + struct.pack("<H", block_usize) + b"\x00"
            payload += body
        else:
            payload += struct.pack(">H", len(body)) + body
    payload += b"\x00" * 5
    return bytes(payload)


def _dos_datetime():
    import time as _t
    t = _t.localtime()
    dt = ((t.tm_year - 1980) << 9) | (t.tm_mon << 5) | t.tm_mday
    tm = (t.tm_hour << 11) | (t.tm_min << 5) | (t.tm_sec // 2)
    return tm, dt


def write_pak(out_path, entries, edits=None):
    """Write a .pak. `entries` is a list of PakEntry; `edits` maps entry name ->
    new decompressed bytes. Unedited entries are copied byte-for-byte; edited
    entries are re-framed as uncompressed LZX blocks (method 13)."""
    edits = edits or {}
    local_blobs = []
    central = []
    offset = 0
    for e in entries:
        name_b = e.name.replace("/", "\\").encode("latin-1")
        if e.name in edits:
            data = edits[e.name]
            crc = _crc(data)
            payload = _build_payload(data)
            usize = len(data)
            csize = len(payload)
            mtime, mdate = _dos_datetime()
        else:
            payload = e.payload
            crc = e.crc
            usize = e.usize
            csize = e.csize
            mtime, mdate = e.mtime, e.mdate

        local = struct.pack("<4sHHHHHIIIHH", SIG_LOCAL, 10, 0, METHOD_SOD,
                            mtime, mdate, crc, csize, usize,
                            len(name_b), 0) + name_b + payload
        central.append((name_b, crc, csize, usize, mtime, mdate, offset))
        local_blobs.append(local)
        offset += len(local)

    cd = bytearray()
    cd_offset = offset
    for (name_b, crc, csize, usize, mtime, mdate, lho) in central:
        cd += struct.pack("<4sHHHHHHIIIHHHHHII", SIG_CENTRAL, 20, 10, 0,
                          METHOD_SOD, mtime, mdate, crc, csize, usize,
                          len(name_b), 0, 0, 0, 0, 0, lho) + name_b
    eocd = struct.pack("<4sHHHHIIH", SIG_EOCD, 0, 0, len(central),
                       len(central), len(cd), cd_offset, 0)
    with open(out_path, "wb") as f:
        for b in local_blobs:
            f.write(b)
        f.write(cd)
        f.write(eocd)

# ==========================================================================
# DDS TEXTURE CONVERTER  (from sod_dds.py)
# ==========================================================================

_sod_dds_DDS_MAGIC = b"DDS "
_sod_dds_DDS_HEADER_SIZE = 124              # not counting the 4-byte magic
_sod_dds_DDS_DATA_OFFSET = 4 + _sod_dds_DDS_HEADER_SIZE  # 128

# A texture is stored tiled once it is at least one macro-tile (32 blocks) tall.
_sod_dds_TILE_BLOCKS = 32


def _sod_dds_is_dds(data):
    return len(data) >= 4 and data[:4] == _sod_dds_DDS_MAGIC


def _sod_dds_parse_header(data):
    """Return a dict of the useful DDS header fields."""
    if not _sod_dds_is_dds(data):
        raise ValueError("not a DDS file")
    size, flags, height, width, pitch, depth, mips = struct.unpack_from(
        "<IIIIIII", data, 4)
    fourcc = data[84:88]
    dx10 = fourcc == b"DX10"
    data_offset = _sod_dds_DDS_DATA_OFFSET + (20 if dx10 else 0)
    return {
        "width": width,
        "height": height,
        "mips": mips,
        "fourcc": fourcc,
        "data_offset": data_offset,
        "dx10": dx10,
    }


def _sod_dds__swap16(buf):
    """Swap the bytes of every 16-bit word. Involutive."""
    b = bytearray(buf)
    n = len(b) - (len(b) & 1)
    b[0:n:2], b[1:n:2] = b[1:n:2], b[0:n:2]
    return bytes(b)


# --------------------------------------------------------------------------
# Xbox 360 tiled-address arithmetic (block granularity).
# --------------------------------------------------------------------------
def _sod_dds__xg_tiled_x(offset, width_blocks, texel_pitch):
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


def _sod_dds__xg_tiled_y(offset, width_blocks, texel_pitch):
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


def _sod_dds__block_params(fourcc):
    """Return (texel_pitch_bytes, is_block) for a DDS fourcc."""
    fourcc = fourcc.upper()
    if fourcc in (b"DXT1", b"CTX1"):
        return 8, True
    if fourcc in (b"DXT2", b"DXT3", b"DXT4", b"DXT5", b"ATI2"):
        return 16, True
    return 0, False   # uncompressed or unknown: no tiling handled


def _sod_dds_build_dds_from_mip(header_dds, mip_data, width, height):
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


def _sod_dds_mip_dimensions(base_w, base_h, level, max_level):
    """Dimensions of mip `level` (0 = smallest stored) given the highest stored
    level corresponds to the base resolution. Each step halves the size."""
    shift = max_level - level
    w = max(1, base_w >> shift)
    h = max(1, base_h >> shift)
    return w, h


def _sod_dds_assemble_texture(header_bytes, mip_data, level, max_level):
    """Given a `.dds.N` mip (raw Xbox-360 bytes) plus the `.dds.0` header, return
    a PC-format standalone DDS for that mip (byte-swapped + de-tiled), ready for
    decoding or saving. `level` is this mip's index, `max_level` the highest."""
    hdr = _sod_dds_parse_header(header_bytes)
    w, h = _sod_dds_mip_dimensions(hdr["width"], hdr["height"], level, max_level)
    full = _sod_dds_build_dds_from_mip(header_bytes, mip_data, w, h)
    return _sod_dds_to_pc(full)


def _sod_dds__is_tiled(width, height, fourcc):
    tp, block = _sod_dds__block_params(fourcc)
    if not block:
        return False
    return (height // 4) >= _sod_dds_TILE_BLOCKS


def _sod_dds__untile_blocks(data, width, height, texel_pitch):
    """Tiled -> linear (de-tile)."""
    out = bytearray(len(data))
    bw, bh = width // 4, height // 4
    for j in range(bh):
        base = j * bw
        for i in range(bw):
            x = _sod_dds__xg_tiled_x(base + i, bw, texel_pitch)
            y = _sod_dds__xg_tiled_y(base + i, bw, texel_pitch)
            src = (base + i) * texel_pitch
            dst = (y * bw + x) * texel_pitch
            if dst + texel_pitch <= len(data):
                out[dst:dst + texel_pitch] = data[src:src + texel_pitch]
    return bytes(out)


def _sod_dds__tile_blocks(data, width, height, texel_pitch):
    """Linear -> tiled (inverse of _sod_dds__untile_blocks)."""
    out = bytearray(len(data))
    bw, bh = width // 4, height // 4
    for j in range(bh):
        base = j * bw
        for i in range(bw):
            x = _sod_dds__xg_tiled_x(base + i, bw, texel_pitch)
            y = _sod_dds__xg_tiled_y(base + i, bw, texel_pitch)
            dst = (base + i) * texel_pitch
            src = (y * bw + x) * texel_pitch
            if src + texel_pitch <= len(data):
                out[dst:dst + texel_pitch] = data[src:src + texel_pitch]
    return bytes(out)


def _sod_dds_to_pc(data):
    """Convert an Xbox 360 DDS to a normal PC DDS (byte-swap, then untile)."""
    if not _sod_dds_is_dds(data):
        raise ValueError("not a DDS file")
    h = _sod_dds_parse_header(data)
    off = h["data_offset"]
    pixels = _sod_dds__swap16(data[off:])
    tp, block = _sod_dds__block_params(h["fourcc"])
    if block and _sod_dds__is_tiled(h["width"], h["height"], h["fourcc"]):
        pixels = _sod_dds__untile_blocks(pixels, h["width"], h["height"], tp)
    return data[:off] + pixels


def _sod_dds_to_xbox(data):
    """Convert a PC DDS to Xbox 360 order (tile, then byte-swap)."""
    if not _sod_dds_is_dds(data):
        raise ValueError("not a DDS file")
    h = _sod_dds_parse_header(data)
    off = h["data_offset"]
    pixels = data[off:]
    tp, block = _sod_dds__block_params(h["fourcc"])
    if block and _sod_dds__is_tiled(h["width"], h["height"], h["fourcc"]):
        pixels = _sod_dds__tile_blocks(pixels, h["width"], h["height"], tp)
    return data[:off] + _sod_dds__swap16(pixels)


def _sod_dds_import_replace_pixels(original_360, pc_dds):
    """Build a new Xbox-360 entry by keeping the ORIGINAL 360 header (with its
    engine-specific tags/flags, e.g. FYRC/NVTT) and replacing only the pixel
    data with the imported PC texture's pixels, converted to 360 order.

    This is what should be written back into the pak: the game relies on the
    original header, so a freshly-authored PC DDS header (from Paint.NET / GIMP)
    must not be used. Only the base surface is taken from the import (any extra
    mip levels a PC editor appended are ignored)."""
    oh = _sod_dds_parse_header(original_360)
    ph = _sod_dds_parse_header(pc_dds)
    if (oh["width"], oh["height"]) != (ph["width"], ph["height"]):
        raise ValueError(
            "imported texture is %dx%d but the original is %dx%d" %
            (ph["width"], ph["height"], oh["width"], oh["height"]))

    pc_pixels = pc_dds[ph["data_offset"]:]
    tp, block = _sod_dds__block_params(oh["fourcc"])
    if block:
        base_size = (oh["width"] // 4) * (oh["height"] // 4) * tp
        pc_pixels = pc_pixels[:base_size]          # drop any appended mips
        if _sod_dds__is_tiled(oh["width"], oh["height"], oh["fourcc"]):
            pc_pixels = _sod_dds__tile_blocks(pc_pixels, oh["width"], oh["height"], tp)
    else:
        # uncompressed: keep exactly the original surface size
        base_size = len(original_360) - oh["data_offset"]
        pc_pixels = pc_pixels[:base_size]

    return original_360[:oh["data_offset"]] + _sod_dds__swap16(pc_pixels)


# Back-compat alias (older code called _sod_dds_convert() for the symmetric swap-only).
_sod_dds_convert = _sod_dds_to_pc


def _sod_dds_describe(data):
    """Short human description for the UI."""
    try:
        h = _sod_dds_parse_header(data)
        cc = h["fourcc"].decode("latin-1", "replace").strip("\x00") or "uncompressed"
        tiled = "tiled" if _sod_dds__is_tiled(h["width"], h["height"], h["fourcc"]) else "linear"
        return "DDS texture  %dx%d  %s  (Xbox 360, %s)" % (
            h["width"], h["height"], cc, tiled)
    except Exception:
        return "DDS texture"


import types as _types
sod_dds = _types.SimpleNamespace()
sod_dds.DDS_MAGIC = _sod_dds_DDS_MAGIC
sod_dds.is_dds = _sod_dds_is_dds
sod_dds.parse_header = _sod_dds_parse_header
sod_dds.convert = _sod_dds_convert
sod_dds.to_pc = _sod_dds_to_pc
sod_dds.to_xbox = _sod_dds_to_xbox
sod_dds.describe = _sod_dds_describe
sod_dds.build_dds_from_mip = _sod_dds_build_dds_from_mip
sod_dds.mip_dimensions = _sod_dds_mip_dimensions
sod_dds.assemble_texture = _sod_dds_assemble_texture
sod_dds.import_replace_pixels = _sod_dds_import_replace_pixels

# ==========================================================================
# DDS TEXTURE DECODER/PREVIEW  (from sod_ddsview.py)
# ==========================================================================

def _sod_ddsview__unpack565(c):
    r = (c >> 11) & 0x1F
    g = (c >> 5) & 0x3F
    b = c & 0x1F
    return (r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2)


def _sod_ddsview__color_table(c0, c1, dxt1):
    a = _sod_ddsview__unpack565(c0)
    b = _sod_ddsview__unpack565(c1)
    col = [a, b, None, None]
    if c0 > c1 or not dxt1:
        col[2] = tuple((2 * a[k] + b[k]) // 3 for k in range(3))
        col[3] = tuple((a[k] + 2 * b[k]) // 3 for k in range(3))
        a3 = 255
    else:
        col[2] = tuple((a[k] + b[k]) // 2 for k in range(3))
        col[3] = (0, 0, 0)
        a3 = 0  # transparent black (DXT1 1-bit alpha)
    return col, a3


def _sod_ddsview_decode(data, width, height, fourcc):
    """Decode block-compressed pixel data to RGBA bytes (width*height*4)."""
    out = bytearray(width * height * 4)
    fourcc = fourcc.upper()
    bi = 0
    if fourcc == b"DXT1":
        block = 8
    else:
        block = 16  # DXT3 / DXT5

    for by in range(0, height, 4):
        for bx in range(0, width, 4):
            blk = data[bi:bi + block]
            bi += block
            if len(blk) < block:
                continue

            if fourcc == b"DXT1":
                c0, c1 = struct.unpack_from("<HH", blk, 0)
                cbits = struct.unpack_from("<I", blk, 4)[0]
                col, a3 = _sod_ddsview__color_table(c0, c1, dxt1=True)
                alpha = None
            elif fourcc == b"DXT3":
                alpha_raw = int.from_bytes(blk[0:8], "little")
                c0, c1 = struct.unpack_from("<HH", blk, 8)
                cbits = struct.unpack_from("<I", blk, 12)[0]
                col, _ = _sod_ddsview__color_table(c0, c1, dxt1=False)
                alpha = []
                for i in range(16):
                    a4 = (alpha_raw >> (4 * i)) & 0xF
                    alpha.append(a4 * 17)
            else:  # DXT5
                a0, a1 = blk[0], blk[1]
                abits = int.from_bytes(blk[2:8], "little")
                c0, c1 = struct.unpack_from("<HH", blk, 8)
                cbits = struct.unpack_from("<I", blk, 12)[0]
                col, _ = _sod_ddsview__color_table(c0, c1, dxt1=False)
                atab = [a0, a1]
                if a0 > a1:
                    for i in range(1, 7):
                        atab.append(((7 - i) * a0 + i * a1) // 7)
                else:
                    for i in range(1, 5):
                        atab.append(((5 - i) * a0 + i * a1) // 5)
                    atab += [0, 255]
                alpha = None

            for py in range(4):
                for px in range(4):
                    idx = py * 4 + px
                    x = bx + px
                    y = by + py
                    if x >= width or y >= height:
                        continue
                    ci = (cbits >> (2 * idx)) & 3
                    r, g, b = col[ci]
                    if fourcc == b"DXT1":
                        a = 255 if (c0 > c1 or ci != 3) else a3
                    elif fourcc == b"DXT3":
                        a = alpha[idx]
                    else:
                        ai = (abits >> (3 * idx)) & 7
                        a = atab[ai]
                    o = (y * width + x) * 4
                    out[o] = r
                    out[o + 1] = g
                    out[o + 2] = b
                    out[o + 3] = a
    return bytes(out)


def _sod_ddsview__decode_bc4_channel(block):
    """Decode one 8-byte BC4 block to 16 single-channel values."""
    r0, r1 = block[0], block[1]
    bits = int.from_bytes(block[2:8], "little")
    r = [r0, r1]
    if r0 > r1:
        for i in range(1, 7):
            r.append(((7 - i) * r0 + i * r1) // 7)
    else:
        for i in range(1, 5):
            r.append(((5 - i) * r0 + i * r1) // 5)
        r += [0, 255]
    return [r[(bits >> (3 * i)) & 7] for i in range(16)]


def _sod_ddsview__decode_ati2(data, width, height):
    """ATI2 / BC5 / 3Dc two-channel (normal map). 16-byte blocks."""
    import math
    out = bytearray(width * height * 4)
    bi = 0
    for by in range(0, height, 4):
        for bx in range(0, width, 4):
            rb = data[bi:bi + 8]
            gb = data[bi + 8:bi + 16]
            bi += 16
            if len(gb) < 8:
                continue
            rr = _sod_ddsview__decode_bc4_channel(rb)
            gg = _sod_ddsview__decode_bc4_channel(gb)
            for i in range(16):
                x = bx + (i % 4)
                y = by + (i // 4)
                if x >= width or y >= height:
                    continue
                nx = rr[i] / 127.5 - 1.0
                ny = gg[i] / 127.5 - 1.0
                nz = math.sqrt(max(0.0, 1.0 - nx * nx - ny * ny))
                o = (y * width + x) * 4
                out[o] = rr[i]
                out[o + 1] = gg[i]
                out[o + 2] = int((nz * 0.5 + 0.5) * 255)
                out[o + 3] = 255
    return bytes(out)


def _sod_ddsview__decode_ctx1(data, width, height):
    """CTX1 — Xbox 360 two-channel format (normal maps). 8-byte blocks."""
    import math
    out = bytearray(width * height * 4)
    bi = 0
    for by in range(0, height, 4):
        for bx in range(0, width, 4):
            blk = data[bi:bi + 8]
            bi += 8
            if len(blk) < 8:
                continue
            r0, g0, r1, g1 = blk[0], blk[1], blk[2], blk[3]
            idx = struct.unpack_from("<I", blk, 4)[0]
            R = [r0, r1, (2 * r0 + r1) // 3, (r0 + 2 * r1) // 3]
            G = [g0, g1, (2 * g0 + g1) // 3, (g0 + 2 * g1) // 3]
            for i in range(16):
                x = bx + (i % 4)
                y = by + (i // 4)
                if x >= width or y >= height:
                    continue
                ci = (idx >> (2 * i)) & 3
                nx = R[ci] / 127.5 - 1.0
                ny = G[ci] / 127.5 - 1.0
                nz = math.sqrt(max(0.0, 1.0 - nx * nx - ny * ny))
                o = (y * width + x) * 4
                out[o] = R[ci]
                out[o + 1] = G[ci]
                out[o + 2] = int((nz * 0.5 + 0.5) * 255)
                out[o + 3] = 255
    return bytes(out)


def _sod_ddsview__decode_uncompressed(data, width, height, rgb_bits):
    """A8R8G8B8 / X8R8G8B8 uncompressed surface (32-bit)."""
    out = bytearray(width * height * 4)
    n = min(len(data), width * height * 4)
    # DDS stores BGRA; swap to RGBA
    for i in range(0, n, 4):
        b, g, r, a = data[i], data[i + 1], data[i + 2], data[i + 3]
        out[i] = r
        out[i + 1] = g
        out[i + 2] = b
        out[i + 3] = a if rgb_bits == 32 else 255
    return bytes(out)


def _sod_ddsview_decode_dds(dds_bytes):
    """Decode a whole DDS file (header + first mip) to (width, height, rgba).

    `dds_bytes` should already be in PC byte order (run sod_dds.to_pc first for
    Xbox 360 textures)."""
    if dds_bytes[:4] != b"DDS ":
        raise ValueError("not a DDS file")
    h = dds_bytes[4:128]
    height = struct.unpack_from("<I", h, 8)[0]
    width = struct.unpack_from("<I", h, 12)[0]
    fourcc = dds_bytes[84:88]
    data = dds_bytes[128:]
    if fourcc in (b"DXT1", b"DXT2", b"DXT3", b"DXT4", b"DXT5"):
        return width, height, _sod_ddsview_decode(data, width, height, fourcc)
    if fourcc == b"ATI2":
        return width, height, _sod_ddsview__decode_ati2(data, width, height)
    if fourcc == b"CTX1":
        return width, height, _sod_ddsview__decode_ctx1(data, width, height)
    if fourcc == b"\x00\x00\x00\x00":
        rgb_bits = struct.unpack_from("<I", h, 88)[0]
        return width, height, _sod_ddsview__decode_uncompressed(data, width, height, rgb_bits)
    raise ValueError("unsupported texture format: %r" % fourcc)


def _sod_ddsview_rgba_to_ppm(width, height, rgba, bg=(64, 64, 64)):
    """Composite RGBA over a solid background and return binary PPM (P6) bytes,
    which tkinter's PhotoImage can load via the `data=` argument."""
    out = bytearray()
    out += b"P6\n%d %d\n255\n" % (width, height)
    body = bytearray(width * height * 3)
    j = 0
    for i in range(0, len(rgba), 4):
        r, g, b, a = rgba[i], rgba[i + 1], rgba[i + 2], rgba[i + 3]
        if a != 255:
            r = (r * a + bg[0] * (255 - a)) // 255
            g = (g * a + bg[1] * (255 - a)) // 255
            b = (b * a + bg[2] * (255 - a)) // 255
        body[j] = r
        body[j + 1] = g
        body[j + 2] = b
        j += 3
    out += body
    return bytes(out)

import types as _types
sod_ddsview = _types.SimpleNamespace()
sod_ddsview.decode = _sod_ddsview_decode
sod_ddsview.decode_dds = _sod_ddsview_decode_dds
sod_ddsview.rgba_to_ppm = _sod_ddsview_rgba_to_ppm

# ==========================================================================
# BTXT STRING TABLE  (from sod_btxt.py)
# ==========================================================================

_sod_btxt_BTXT_MAGIC = b"TXDB"


def _sod_btxt_is_btxt(data):
    return len(data) >= 4 and data[:4] == _sod_btxt_BTXT_MAGIC


class _sod_btxt_StringTable:
    def __init__(self, version, flags, hashes, strings):
        self.version = version
        self.flags = flags
        self.hashes = hashes          # list[int]
        self.strings = strings        # list[bytes] (raw UTF-8, no terminator)

    @property
    def count(self):
        return len(self.hashes)


def _sod_btxt_parse(data):
    if not _sod_btxt_is_btxt(data):
        raise ValueError("not a TXDB/.btxt file")
    version, flags, count = struct.unpack_from(">III", data, 4)
    hashes = list(struct.unpack_from(">%dI" % count, data, 16))
    blob = data[16 + count * 4:]
    parts = blob.split(b"\x00")
    # there should be `count` strings followed by a trailing empty segment
    strings = parts[:count]
    if len(strings) < count:
        raise ValueError("string table truncated: expected %d strings, found %d"
                         % (count, len(strings)))
    return _sod_btxt_StringTable(version, flags, hashes, strings)


def _sod_btxt_build(table):
    """Serialize a _sod_btxt_StringTable back to bytes (byte-identical to the original
    when nothing was edited)."""
    out = bytearray()
    out += _sod_btxt_BTXT_MAGIC
    out += struct.pack(">III", table.version, table.flags, table.count)
    out += struct.pack(">%dI" % table.count, *table.hashes)
    for s in table.strings:
        out += s
        out += b"\x00"
    return bytes(out)


def _sod_btxt_get_text(table, index, encoding="utf-8"):
    try:
        return table.strings[index].decode(encoding)
    except UnicodeDecodeError:
        return table.strings[index].decode("latin-1")


def _sod_btxt_set_text(table, index, text, encoding="utf-8"):
    table.strings[index] = text.encode(encoding)


def _sod_btxt_describe(data):
    try:
        t = _sod_btxt_parse(data)
        return "TXDB string table  (%d strings)" % t.count
    except Exception:
        return "TXDB string table"

import types as _types
sod_btxt = _types.SimpleNamespace()
sod_btxt.BTXT_MAGIC = _sod_btxt_BTXT_MAGIC
sod_btxt.is_btxt = _sod_btxt_is_btxt
sod_btxt.StringTable = _sod_btxt_StringTable
sod_btxt.parse = _sod_btxt_parse
sod_btxt.build = _sod_btxt_build
sod_btxt.get_text = _sod_btxt_get_text
sod_btxt.set_text = _sod_btxt_set_text
sod_btxt.describe = _sod_btxt_describe

# ==========================================================================
# COMMAND-LINE INTERFACE  (from sod_pak_cli.py)
# ==========================================================================

def cmd_list(path):
    pak = PakArchive(path)
    for e in pak.entries:
        kind = "stored" if e.is_stored else "lzx"
        print("%10d  %-6s  %s" % (e.usize, kind, e.name))
    print("\n%d entries" % len(pak.entries))


def cmd_verify(path):
    pak = PakArchive(path)
    ok = 0
    bad = []
    for e in pak.entries:
        try:
            data = pak.extract(e)
            if _crc(data) == e.crc:
                ok += 1
            else:
                bad.append(e.name + " (crc)")
        except Exception as ex:
            bad.append("%s (%s)" % (e.name, ex))
    print("OK %d/%d" % (ok, len(pak.entries)))
    for b in bad[:50]:
        print("  FAIL", b)
    return 0 if not bad else 1


def cmd_extract(path, out_dir, substr=None):
    pak = PakArchive(path)
    n = 0
    for e in pak.entries:
        if substr and substr.lower() not in e.name.lower():
            continue
        data = pak.extract(e)
        dest = os.path.join(out_dir, e.name.replace("/", os.sep))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as f:
            f.write(data)
        n += 1
    print("Extracted %d files to %s" % (n, out_dir))


def cmd_replace(path, entry_name, file_path, out_path):
    pak = PakArchive(path)
    if not pak.find(entry_name):
        print("Entry not found:", entry_name)
        return 1
    with open(file_path, "rb") as f:
        data = f.read()
    write_pak(out_path, pak.entries, {entry_name.replace("\\", "/"): data})
    # verify
    p2 = PakArchive(out_path)
    d2 = p2.extract(p2.find(entry_name))
    print("Wrote %s; replaced entry round-trips: %s" %
          (out_path, d2 == data))
    return 0


def cmd_dds(direction, in_path, out_path):
    """Convert a .dds.0 between Xbox-360 and PC byte order (symmetric)."""
    with open(in_path, "rb") as f:
        data = f.read()
    if not sod_dds.is_dds(data):
        print("Not a DDS file:", in_path)
        return 1
    with open(out_path, "wb") as f:
        f.write(sod_dds.convert(data))
    print("Wrote %s (%s)" % (out_path, sod_dds.describe(data)))
    return 0


def cmd_btxt_dump(path):
    with open(path, "rb") as f:
        data = f.read()
    t = sod_btxt.parse(data)
    for i in range(t.count):
        print("%05d\t0x%08X\t%s" % (i, t.hashes[i],
              sod_btxt.get_text(t, i).replace("\n", "\\n")))
    return 0


def _cli_main(argv):
    if len(argv) < 3:
        print(__doc__)
        return 2
    cmd = argv[1]
    if cmd == "list":
        return cmd_list(argv[2])
    if cmd == "verify":
        return cmd_verify(argv[2])
    if cmd == "extract":
        return cmd_extract(argv[2], argv[3], argv[4] if len(argv) > 4 else None)
    if cmd == "replace":
        return cmd_replace(argv[2], argv[3], argv[4], argv[5])
    if cmd == "dds":          # dds <in.dds.0> <out.dds>   (also reverses)
        return cmd_dds(None, argv[2], argv[3])
    if cmd == "btxt-dump":    # btxt-dump <file.btxt>
        return cmd_btxt_dump(argv[2])
    print("Unknown command:", cmd)
    print(__doc__)
    return 2

# ==========================================================================
# GRAPHICAL USER INTERFACE  (from sod_pak_tool.pyw)
# ==========================================================================

try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, font as tkfont
    _TK_OK = True
except Exception:
    _TK_OK = False


if _TK_OK:



    # Plain-text entries editable directly in the text pane.
    TEXT_EXTS = {".xml", ".lua", ".ent", ".txt", ".cfg", ".drl",
                 ".csv", ".json", ".fxc", ".mtl"}

    APP_TITLE = "SoD 360 PAK Tool"


    def is_texty(name):
        ext = os.path.splitext(name)[1].lower()
        return ext in TEXT_EXTS


    def detect_format(name, data):
        """Return one of: 'dds', 'btxt', 'gfx', 'text', 'binary'."""
        low = name.lower()
        # split-mip textures: name.dds.0, name.dds.1, ... (any have the magic only at .0)
        if ".dds." in low:
            tail = low.rsplit(".dds.", 1)[1]
            if tail.isdigit():
                return "dds"
        if data[:4] == sod_dds.DDS_MAGIC or low.endswith(".dds"):
            if sod_dds.is_dds(data):
                return "dds"
        if data[:4] == sod_btxt.BTXT_MAGIC:
            return "btxt"
        if data[:4] == b"GFX\n" or data[:4] == b"CFX\n":
            return "gfx"
        if is_texty(name):
            return "text"
        return "binary"


    def texture_group_key(name):
        """For a split-mip texture entry name return (base_including_dds_dot, level)
        or None if it isn't a `.dds.N` entry. e.g. 'a/b.dds.3' -> ('a/b.dds.', 3)."""
        marker = ".dds."
        low = name.lower()
        pos = low.rfind(marker)
        if pos == -1:
            return None
        tail = name[pos + len(marker):]
        if tail.isdigit():
            return name[:pos + len(marker)], int(tail)
        return None


    class PakTool(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title(APP_TITLE)
            self.geometry("1080x680")
            self.minsize(820, 520)

            self.pak = None
            self.pak_path = None
            self.edits = {}          # name -> new bytes
            self.current_name = None
            self.node_to_name = {}   # tree item id -> entry name

            self._build_menu()
            self._build_ui()
            self._set_status("Open a .pak file to begin.")

            # allow "open file passed on command line"
            if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):
                self.after(100, lambda: self.load_pak(sys.argv[1]))

        # ---------------------------------------------------------------- UI build
        def _build_menu(self):
            m = tk.Menu(self)
            filem = tk.Menu(m, tearoff=0)
            filem.add_command(label="Open PAK…", command=self.on_open, accelerator="Ctrl+O")
            filem.add_command(label="Save PAK As…", command=self.on_save_as, accelerator="Ctrl+S")
            filem.add_separator()
            filem.add_command(label="Extract Selected…", command=self.on_extract_selected)
            filem.add_command(label="Extract All…", command=self.on_extract_all)
            filem.add_separator()
            filem.add_command(label="Exit", command=self.destroy)
            m.add_cascade(label="File", menu=filem)

            editm = tk.Menu(m, tearoff=0)
            editm.add_command(label="Import File Into Entry…", command=self.on_import_entry)
            editm.add_command(label="Save Edits to Entry", command=self.on_apply_text_edit)
            editm.add_command(label="Revert Entry", command=self.on_revert_entry)
            m.add_cascade(label="Edit", menu=editm)

            helpm = tk.Menu(m, tearoff=0)
            helpm.add_command(label="About", command=self.on_about)
            m.add_cascade(label="Help", menu=helpm)

            self.config(menu=m)
            self.bind("<Control-o>", lambda e: self.on_open())
            self.bind("<Control-s>", lambda e: self.on_save_as())

        def _build_ui(self):
            toolbar = ttk.Frame(self, padding=(8, 6))
            toolbar.pack(side=tk.TOP, fill=tk.X)
            ttk.Button(toolbar, text="Open PAK", command=self.on_open).pack(side=tk.LEFT)
            ttk.Button(toolbar, text="Extract Selected", command=self.on_extract_selected).pack(side=tk.LEFT, padx=(6, 0))
            ttk.Button(toolbar, text="Extract All", command=self.on_extract_all).pack(side=tk.LEFT, padx=(6, 0))
            ttk.Button(toolbar, text="Save PAK As", command=self.on_save_as).pack(side=tk.LEFT, padx=(6, 0))
            ttk.Label(toolbar, text="   Filter:").pack(side=tk.LEFT, padx=(12, 2))
            self.filter_var = tk.StringVar()
            self.filter_var.trace_add("write", lambda *a: self._populate_tree())
            ttk.Entry(toolbar, textvariable=self.filter_var, width=28).pack(side=tk.LEFT)

            main = ttk.Panedwindow(self, orient=tk.HORIZONTAL)
            main.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

            # left: tree
            left = ttk.Frame(main)
            self.tree = ttk.Treeview(left, columns=("size", "status"), selectmode="extended")
            self.tree.heading("#0", text="File")
            self.tree.heading("size", text="Size")
            self.tree.heading("status", text="")
            self.tree.column("#0", width=360, anchor="w")
            self.tree.column("size", width=90, anchor="e")
            self.tree.column("status", width=60, anchor="center")
            ysb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
            self.tree.configure(yscrollcommand=ysb.set)
            self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            ysb.pack(side=tk.RIGHT, fill=tk.Y)
            self.tree.bind("<<TreeviewSelect>>", self.on_select)
            main.add(left, weight=2)

            # right: preview / edit
            right = ttk.Frame(main)
            header = ttk.Frame(right)
            header.pack(side=tk.TOP, fill=tk.X)
            self.preview_label = ttk.Label(header, text="(no selection)", anchor="w")
            self.preview_label.pack(side=tk.LEFT, fill=tk.X, expand=True)
            self.save_edit_btn = ttk.Button(header, text="Save Edits to Entry",
                                            command=self.on_apply_text_edit, state="disabled")
            self.save_edit_btn.pack(side=tk.RIGHT)
            # format-specific actions (shown/hidden per selection)
            self.dds_export_btn = ttk.Button(header, text="Export as PC .dds",
                                             command=self.on_dds_export)
            self.dds_import_btn = ttk.Button(header, text="Import PC .dds",
                                             command=self.on_dds_import)
            self.btxt_edit_btn = ttk.Button(header, text="Edit Strings…",
                                            command=self.on_btxt_edit)

            mono = tkfont.nametofont("TkFixedFont")
            # image preview (used for DDS textures); a scrollable canvas so textures
            # of any size display fully without being clipped at the edges.
            self.image_holder = ttk.Frame(right)
            self.image_canvas = tk.Canvas(self.image_holder, background="#404040",
                                          highlightthickness=0)
            img_vsb = ttk.Scrollbar(self.image_holder, orient="vertical",
                                    command=self.image_canvas.yview)
            img_hsb = ttk.Scrollbar(self.image_holder, orient="horizontal",
                                    command=self.image_canvas.xview)
            self.image_canvas.configure(yscrollcommand=img_vsb.set,
                                        xscrollcommand=img_hsb.set)
            img_vsb.pack(side=tk.RIGHT, fill=tk.Y)
            img_hsb.pack(side=tk.BOTTOM, fill=tk.X)
            self.image_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            self.preview_photo = None     # keep a reference so it isn't GC'd

            self.text = tk.Text(right, wrap="none", undo=True, font=mono)
            txsb = ttk.Scrollbar(right, orient="vertical", command=self.text.yview)
            txsbx = ttk.Scrollbar(right, orient="horizontal", command=self.text.xview)
            self.text.configure(yscrollcommand=txsb.set, xscrollcommand=txsbx.set)
            txsb.pack(side=tk.RIGHT, fill=tk.Y)
            txsbx.pack(side=tk.BOTTOM, fill=tk.X)
            self.text.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
            main.add(right, weight=3)

            self.status = ttk.Label(self, relief="sunken", anchor="w", padding=(6, 3))
            self.status.pack(side=tk.BOTTOM, fill=tk.X)

        def _set_status(self, msg):
            self.status.config(text=msg)
            self.update_idletasks()

        # ---------------------------------------------------------------- load
        def on_open(self):
            path = filedialog.askopenfilename(
                title="Open State of Decay .pak",
                filetypes=[("PAK archives", "*.pak"), ("All files", "*.*")])
            if path:
                self.load_pak(path)

        def load_pak(self, path):
            self._set_status("Loading %s…" % os.path.basename(path))
            try:
                self.pak = PakArchive(path)
            except Exception as e:
                messagebox.showerror(APP_TITLE, "Failed to read archive:\n%s" % e)
                self._set_status("Load failed.")
                return
            self.pak_path = path
            self.edits = {}
            self.current_name = None
            self.title("%s — %s" % (APP_TITLE, os.path.basename(path)))
            self._populate_tree()
            self._set_status("Loaded %d entries from %s" %
                             (len(self.pak.entries), os.path.basename(path)))

        def _populate_tree(self):
            self.tree.delete(*self.tree.get_children())
            self.node_to_name = {}
            if not self.pak:
                return
            flt = self.filter_var.get().strip().lower()
            dirs = {"": ""}  # path -> tree id

            def ensure_dir(path):
                if path in dirs:
                    return dirs[path]
                parent, _, leaf = path.rpartition("/")
                pid = ensure_dir(parent) if parent else ""
                nid = self.tree.insert(pid, "end", text=leaf, open=False, values=("", ""))
                dirs[path] = nid
                return nid

            for e in self.pak.entries:
                if flt and flt not in e.name.lower():
                    continue
                d, _, leaf = e.name.rpartition("/")
                pid = ensure_dir(d) if d else ""
                status = "edited" if e.name in self.edits else ""
                nid = self.tree.insert(pid, "end", text=leaf,
                                       values=(self._fmt_size(e.usize), status))
                self.node_to_name[nid] = e.name

        @staticmethod
        def _fmt_size(n):
            if n < 1024:
                return "%d B" % n
            if n < 1024 * 1024:
                return "%.1f KB" % (n / 1024)
            return "%.1f MB" % (n / (1024 * 1024))

        # ---------------------------------------------------------------- select
        def on_select(self, event=None):
            sel = self.tree.selection()
            name = None
            for nid in sel:
                if nid in self.node_to_name:
                    name = self.node_to_name[nid]
                    break
            if not name:
                return
            self.current_name = name
            self._preview(name)

        def _entry_bytes(self, name):
            if name in self.edits:
                return self.edits[name]
            e = self.pak.find(name)
            return self.pak.extract(e)

        def _hide_format_buttons(self):
            for b in (self.dds_export_btn, self.dds_import_btn, self.btxt_edit_btn):
                b.pack_forget()

        def _hide_image(self):
            self.image_holder.pack_forget()
            self.image_canvas.delete("all")
            self.preview_photo = None

        def _resolve_texture(self, name):
            """For a `.dds.N` (or standalone DDS) entry, return a PC-format DDS of
            the full-resolution texture, plus a label. Handles split-mip chains by
            combining the `.dds.0` header with the largest mip."""
            gk = texture_group_key(name)
            if gk is None:
                # standalone DDS (gamedata.pak style): data already has a header
                data = self._entry_bytes(name)
                return sod_dds.to_pc(data), sod_dds.describe(data)

            base, _ = gk
            # gather sibling levels present in the archive
            levels = {}
            prefix = base.lower()
            for e in self.pak.entries:
                k = texture_group_key(e.name)
                if k and k[0].lower() == prefix:
                    levels[k[1]] = e.name

            # A standalone DDS (gamedata.pak: only `.dds.0`, with the whole texture
            # and its own header inside) must NOT go through mip assembly — its data
            # already includes the header. Only treat as a split-mip chain when there
            # are sibling levels (`.dds.1`, `.dds.2`, …).
            if len(levels) <= 1:
                data = self._entry_bytes(name)
                return sod_dds.to_pc(data), sod_dds.describe(data)

            if 0 not in levels:
                data = self._entry_bytes(name)
                return sod_dds.to_pc(data), sod_dds.describe(data)

            header_bytes = self._entry_bytes(levels[0])
            max_level = max(levels)
            mip_bytes = self._entry_bytes(levels[max_level])
            pc = sod_dds.assemble_texture(header_bytes, mip_bytes, max_level, max_level)
            hdr = sod_dds.parse_header(header_bytes)
            cc = hdr["fourcc"].decode("latin-1", "replace").strip("\x00") or "uncompressed"
            label = "DDS texture  %dx%d  %s  (%d mip levels)" % (
                hdr["width"], hdr["height"], cc, len(levels))
            return pc, label

        def _show_dds_image(self, name):
            """Decode and display the full texture for a DDS entry. Returns status."""
            try:
                pc, _label = self._resolve_texture(name)
                w, h, rgba = sod_ddsview.decode_dds(pc)
            except Exception as e:
                return "Preview unavailable: %s" % e
            # scale up small textures; cap displayed size for very large ones
            disp_w, disp_h = w, h
            scale = 1
            while w * (scale + 1) <= 512 and h * (scale + 1) <= 512 and scale < 8:
                scale += 1
            shrink = 1
            while (w // (shrink + 1)) >= 4 and (max(w, h) // shrink) > 1024:
                shrink += 1
            ppm = sod_ddsview.rgba_to_ppm(w, h, rgba, bg=(64, 64, 64))
            try:
                photo = tk.PhotoImage(data=ppm)
                if scale > 1:
                    photo = photo.zoom(scale, scale)
                elif shrink > 1:
                    photo = photo.subsample(shrink, shrink)
            except Exception as e:
                return "Preview unavailable: %s" % e
            self.preview_photo = photo
            self.image_canvas.delete("all")
            iw, ih = photo.width(), photo.height()
            # centre small images; large ones anchor at top-left and scroll
            self.image_canvas.create_image(0, 0, anchor="nw", image=photo)
            self.image_canvas.configure(scrollregion=(0, 0, iw, ih))
            self.image_holder.pack(side=tk.TOP, fill=tk.BOTH, expand=True,
                                   before=self.text)
            if scale > 1:
                return "Showing %dx%d texture (zoomed %dx)" % (w, h, scale)
            if shrink > 1:
                return "Showing %dx%d texture (shrunk to fit)" % (w, h)
            return "Showing %dx%d texture" % (w, h)

        def _preview(self, name):
            try:
                data = self._entry_bytes(name)
            except Exception as e:
                self.preview_label.config(text=name + "  —  DECODE ERROR")
                self.text.delete("1.0", tk.END)
                self.text.insert("1.0", "Failed to decode entry:\n\n" + traceback.format_exc())
                self.save_edit_btn.config(state="disabled")
                self._hide_format_buttons()
                self._hide_image()
                return

            fmt = detect_format(name, data)
            self._hide_format_buttons()
            self._hide_image()
            tag = " [edited]" if name in self.edits else ""

            if fmt == "dds":
                self.save_edit_btn.config(state="disabled")
                self.dds_export_btn.pack(side=tk.RIGHT, padx=(0, 6))
                self.dds_import_btn.pack(side=tk.RIGHT, padx=(0, 6))
                img_status = self._show_dds_image(name)
                try:
                    _pc, desc = self._resolve_texture(name)
                except Exception:
                    desc = "DDS texture"
                self.preview_label.config(text="%s   (%s)%s\n%s   —   %s" %
                                          (name, self._fmt_size(len(data)), tag,
                                           desc, img_status))
                self.text.delete("1.0", tk.END)
                self.text.insert("1.0",
                    "Live preview above (transparent areas shown over grey).\n\n"
                    "  • Export as PC .dds  →  converts to a normal DDS (byte order +\n"
                    "    de-tiled, full resolution) you can edit in Paint.NET / GIMP.\n"
                    "  • Import PC .dds  →  converts your edited DDS back to Xbox 360\n"
                    "    order and stores it in this entry.\n\n"
                    "Note: textures may be split into mip levels (.dds.0 = header +\n"
                    "smallest mip, .dds.1.. = larger mips); the preview/export use the\n"
                    "largest. Transparent regions show as a checkerboard in editors.\n\n"
                    + self._hexdump(data[:256]))
                self.text.config(state="normal")
                return

            if fmt == "btxt":
                try:
                    t = sod_btxt.parse(data)
                    cnt = t.count
                except Exception:
                    cnt = "?"
                self.preview_label.config(text="%s   (%s)%s   —  TXDB string table (%s strings)"
                                          % (name, self._fmt_size(len(data)), tag, cnt))
                self.save_edit_btn.config(state="disabled")
                self.btxt_edit_btn.pack(side=tk.RIGHT, padx=(0, 6))
                self.text.delete("1.0", tk.END)
                self.text.insert("1.0",
                    "This is a localized string table (TXDB). Click “Edit Strings…” to\n"
                    "browse and edit the text. String IDs are preserved automatically;\n"
                    "you can change wording but not add or remove entries.\n")
                self.text.config(state="normal")
                return

            if fmt == "gfx":
                self.preview_label.config(text="%s   (%s)%s   —  Scaleform GFX (compiled UI)"
                                          % (name, self._fmt_size(len(data)), tag))
                self.save_edit_btn.config(state="disabled")
                self.text.delete("1.0", tk.END)
                self.text.insert("1.0",
                    "This is a Scaleform GFX file (compiled Flash/SWF used for the HUD\n"
                    "and menus). It can't be edited as text. To replace it, build a new\n"
                    ".gfx elsewhere and use Edit ▸ Import File Into Entry…\n\n"
                    + self._hexdump(data[:512]))
                self.text.config(state="normal")
                return

            self.preview_label.config(text="%s   (%s)%s" %
                                      (name, self._fmt_size(len(data)), tag))
            self.text.delete("1.0", tk.END)
            if fmt == "text":
                try:
                    txt = data.decode("utf-8")
                except UnicodeDecodeError:
                    txt = data.decode("latin-1")
                self.text.insert("1.0", txt)
                self.text.config(state="normal")
                self.save_edit_btn.config(state="normal")
            else:
                self.text.insert("1.0", self._hexdump(data[:4096]))
                if len(data) > 4096:
                    self.text.insert(tk.END, "\n… (%d bytes total; binary preview truncated)\n"
                                     % len(data))
                self.text.config(state="normal")
                self.save_edit_btn.config(state="disabled")

        @staticmethod
        def _hexdump(data):
            lines = []
            for i in range(0, len(data), 16):
                chunk = data[i:i + 16]
                hexpart = " ".join("%02x" % b for b in chunk)
                asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
                lines.append("%08x  %-47s  %s" % (i, hexpart, asc))
            return "\n".join(lines)

        # ---------------------------------------------------------------- edit
        def on_apply_text_edit(self):
            if not self.current_name or not is_texty(self.current_name):
                return
            txt = self.text.get("1.0", "end-1c")
            self.edits[self.current_name] = txt.encode("utf-8")
            self._mark_edited(self.current_name)
            self._set_status("Saved edits to %s (in memory — use Save PAK As to write)."
                             % self.current_name)

        def on_import_entry(self):
            if not self.current_name:
                messagebox.showinfo(APP_TITLE, "Select an entry first.")
                return
            path = filedialog.askopenfilename(title="Import file into selected entry")
            if not path:
                return
            with open(path, "rb") as f:
                self.edits[self.current_name] = f.read()
            self._mark_edited(self.current_name)
            self._preview(self.current_name)
            self._set_status("Imported %s into %s." %
                             (os.path.basename(path), self.current_name))

        def on_revert_entry(self):
            if self.current_name and self.current_name in self.edits:
                del self.edits[self.current_name]
                self._mark_edited(self.current_name)
                self._preview(self.current_name)
                self._set_status("Reverted %s." % self.current_name)

        # ------------------------------------------------------------ DDS textures
        def on_dds_export(self):
            if not self.current_name:
                return
            try:
                pc, _desc = self._resolve_texture(self.current_name)
            except Exception as e:
                messagebox.showerror(APP_TITLE, "Could not convert texture:\n%s" % e)
                return
            base = os.path.basename(self.current_name)
            # strip a trailing ".dds.N" or ".N" mip suffix
            gk = texture_group_key(self.current_name)
            if gk is not None:
                base = os.path.basename(gk[0]).rstrip(".")  # ".../name.dds." -> "name.dds"
            for suff in (".dds.0", "_dds.0", ".0"):
                if base.lower().endswith(suff):
                    base = base[:-len(suff)]
                    break
            if not base.lower().endswith(".dds"):
                base += ".dds"
            path = filedialog.asksaveasfilename(
                title="Export PC-format .dds", defaultextension=".dds",
                initialfile=base, filetypes=[("DDS texture", "*.dds")])
            if not path:
                return
            with open(path, "wb") as f:
                f.write(pc)
            self._set_status("Exported editable DDS to %s" % os.path.basename(path))
            messagebox.showinfo(APP_TITLE,
                                "Saved a PC-format DDS you can edit in Paint.NET, "
                                "Photoshop or GIMP.\n\nWhen you're done, use "
                                "“Import PC .dds” to put it back.")

        def _is_split_mip(self, name):
            """True only when `name` belongs to a real multi-level mip chain
            (`.dds.0` + at least one `.dds.1`/`.dds.2`/… sibling). A standalone
            `.dds.0` (gamedata.pak style) returns False."""
            gk = texture_group_key(name)
            if gk is None:
                return False
            prefix = gk[0].lower()
            count = 0
            for e in self.pak.entries:
                k = texture_group_key(e.name)
                if k and k[0].lower() == prefix:
                    count += 1
                    if count > 1:
                        return True
            return False

        def on_dds_import(self):
            if not self.current_name:
                return
            if self._is_split_mip(self.current_name):
                messagebox.showinfo(
                    APP_TITLE,
                    "This texture is split into mip levels (.dds.0/.1/.2…).\n\n"
                    "Re-importing an edited image would require rebuilding the whole "
                    "mip chain, which this version doesn't do yet. Export and preview "
                    "work fully; editing these textures back into the pak is a planned "
                    "addition. Standalone .dds textures (as in gamedata.pak) can be "
                    "imported normally.")
                return
            path = filedialog.askopenfilename(
                title="Import edited PC .dds", filetypes=[("DDS texture", "*.dds"),
                                                          ("All files", "*.*")])
            if not path:
                return
            with open(path, "rb") as f:
                pc = f.read()
            if not sod_dds.is_dds(pc):
                messagebox.showerror(APP_TITLE, "That file is not a DDS texture.")
                return
            try:
                orig = self._entry_bytes(self.current_name)
                oh, nh = sod_dds.parse_header(orig), sod_dds.parse_header(pc)
                if (oh["width"], oh["height"]) != (nh["width"], nh["height"]):
                    messagebox.showerror(
                        APP_TITLE,
                        "The imported texture must match the original size.\n\n"
                        "  original: %dx%d\n  imported: %dx%d\n\n"
                        "Resize your edit to the original dimensions and try again."
                        % (oh["width"], oh["height"], nh["width"], nh["height"]))
                    return
                if oh["fourcc"].rstrip(b"\x00") != nh["fourcc"].rstrip(b"\x00"):
                    if not messagebox.askyesno(
                            APP_TITLE,
                            "The imported texture uses a different compression than "
                            "the original:\n\n  original: %s\n  imported: %s\n\n"
                            "For best results, save your edit in the original format. "
                            "Import anyway?" % (
                                oh["fourcc"].decode("latin-1", "replace").strip("\x00"),
                                nh["fourcc"].decode("latin-1", "replace").strip("\x00"))):
                        return
            except Exception:
                orig = self._entry_bytes(self.current_name)
            try:
                self.edits[self.current_name] = sod_dds.import_replace_pixels(orig, pc)
            except Exception as e:
                messagebox.showerror(APP_TITLE, "Could not import texture:\n%s" % e)
                return
            self._mark_edited(self.current_name)
            self._preview(self.current_name)
            self._set_status("Imported texture into %s (converted to 360 order)."
                             % self.current_name)

        # ------------------------------------------------------------ btxt strings
        def on_btxt_edit(self):
            if not self.current_name:
                return
            try:
                data = self._entry_bytes(self.current_name)
                table = sod_btxt.parse(data)
            except Exception as e:
                messagebox.showerror(APP_TITLE, "Could not read string table:\n%s" % e)
                return
            StringEditor(self, self.current_name, table, self._on_btxt_saved)

        def _on_btxt_saved(self, name, table):
            self.edits[name] = sod_btxt.build(table)
            self._mark_edited(name)
            self._preview(name)
            self._set_status("Updated strings in %s (use Save PAK As to write)." % name)

        def _mark_edited(self, name):
            for nid, nm in self.node_to_name.items():
                if nm == name:
                    status = "edited" if name in self.edits else ""
                    vals = list(self.tree.item(nid, "values"))
                    vals[1] = status
                    self.tree.item(nid, values=vals)
                    break

        # ---------------------------------------------------------------- extract
        def _selected_names(self):
            names = []
            for nid in self.tree.selection():
                if nid in self.node_to_name:
                    names.append(self.node_to_name[nid])
                else:
                    # a directory: include all descendants
                    names.extend(self._descendant_names(nid))
            return names

        def _descendant_names(self, nid):
            out = []
            for child in self.tree.get_children(nid):
                if child in self.node_to_name:
                    out.append(self.node_to_name[child])
                else:
                    out.extend(self._descendant_names(child))
            return out

        def on_extract_selected(self):
            if not self.pak:
                return
            names = self._selected_names()
            if not names:
                messagebox.showinfo(APP_TITLE, "Select one or more files (or a folder) first.")
                return
            outdir = filedialog.askdirectory(title="Extract selected to…")
            if not outdir:
                return
            self._run_extract(names, outdir)

        def on_extract_all(self):
            if not self.pak:
                return
            outdir = filedialog.askdirectory(title="Extract all to…")
            if not outdir:
                return
            self._run_extract([e.name for e in self.pak.entries], outdir)

        def _run_extract(self, names, outdir):
            def worker():
                ok = 0
                errors = []
                for i, name in enumerate(names):
                    try:
                        data = self._entry_bytes(name)
                        dest = os.path.join(outdir, name.replace("/", os.sep))
                        os.makedirs(os.path.dirname(dest), exist_ok=True)
                        with open(dest, "wb") as f:
                            f.write(data)
                        ok += 1
                    except Exception as e:
                        errors.append("%s: %s" % (name, e))
                    if i % 25 == 0:
                        self._set_status("Extracting… %d/%d" % (i + 1, len(names)))
                msg = "Extracted %d/%d files to %s" % (ok, len(names), outdir)
                self._set_status(msg)
                if errors:
                    messagebox.showwarning(APP_TITLE, "%d errors:\n%s" %
                                           (len(errors), "\n".join(errors[:15])))
                else:
                    messagebox.showinfo(APP_TITLE, msg)
            threading.Thread(target=worker, daemon=True).start()

        # ---------------------------------------------------------------- save
        def on_save_as(self):
            if not self.pak:
                return
            default = os.path.basename(self.pak_path or "gamedata.pak")
            path = filedialog.asksaveasfilename(
                title="Save PAK As", defaultextension=".pak",
                initialfile=default, filetypes=[("PAK archives", "*.pak")])
            if not path:
                return
            if os.path.abspath(path) == os.path.abspath(self.pak_path or ""):
                if not messagebox.askyesno(APP_TITLE,
                                           "Overwrite the original archive?\n"
                                           "It's safer to save a copy."):
                    return

            def worker():
                self._set_status("Writing %s…" % os.path.basename(path))
                try:
                    write_pak(path, self.pak.entries, self.edits)
                except Exception as e:
                    self._set_status("Save failed.")
                    messagebox.showerror(APP_TITLE, "Failed to write archive:\n%s" % e)
                    return
                self._set_status("Saved %s (%d entries, %d edited)." %
                                 (os.path.basename(path), len(self.pak.entries),
                                  len(self.edits)))
                messagebox.showinfo(APP_TITLE, "Saved successfully.")
            threading.Thread(target=worker, daemon=True).start()

        def on_about(self):
            messagebox.showinfo(
                "About " + APP_TITLE,
                "State of Decay (Xbox 360) PAK Extractor & Editor\n\n"
                "Reads the custom method-13 / Microsoft-LZX compression used by the\n"
                "360 Arcade build, which standard ZIP tools and the PC-era SoD tools\n"
                "cannot extract.\n\n"
                "Extract and edit text entries (.xml/.lua/.ent/…), edit .btxt string\n"
                "tables, swap Xbox-360 .dds textures to/from PC format, import\n"
                "replacement files, and repack — all without external dependencies.")


    class StringEditor(tk.Toplevel):
        """Editor for a TXDB (.btxt) string table: searchable list + edit box."""

        def __init__(self, master, name, table, on_save):
            super().__init__(master)
            self.title("Edit Strings — " + name)
            self.geometry("820x560")
            self.table = table
            self.on_save = on_save
            self.entry_name = name
            self.visible = list(range(table.count))   # indices currently listed
            self.current_index = None

            bar = ttk.Frame(self, padding=(8, 6))
            bar.pack(side=tk.TOP, fill=tk.X)
            ttk.Label(bar, text="Search:").pack(side=tk.LEFT)
            self.search_var = tk.StringVar()
            self.search_var.trace_add("write", lambda *a: self._refilter())
            ttk.Entry(bar, textvariable=self.search_var, width=40).pack(side=tk.LEFT, padx=(4, 0))
            ttk.Button(bar, text="Save Changes", command=self._save).pack(side=tk.RIGHT)

            body = ttk.Panedwindow(self, orient=tk.HORIZONTAL)
            body.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

            leftf = ttk.Frame(body)
            self.listbox = tk.Listbox(leftf, activestyle="dotbox")
            lsb = ttk.Scrollbar(leftf, orient="vertical", command=self.listbox.yview)
            self.listbox.configure(yscrollcommand=lsb.set)
            self.listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            lsb.pack(side=tk.RIGHT, fill=tk.Y)
            self.listbox.bind("<<ListboxSelect>>", self._on_pick)
            body.add(leftf, weight=2)

            rightf = ttk.Frame(body)
            self.id_label = ttk.Label(rightf, text="(select a string)", anchor="w")
            self.id_label.pack(side=tk.TOP, fill=tk.X)
            self.editor = tk.Text(rightf, wrap="word", height=10,
                                  font=tkfont.nametofont("TkTextFont"))
            self.editor.pack(side=tk.TOP, fill=tk.BOTH, expand=True, pady=(4, 4))
            rowb = ttk.Frame(rightf)
            rowb.pack(side=tk.TOP, fill=tk.X)
            ttk.Button(rowb, text="Apply to String", command=self._apply).pack(side=tk.LEFT)
            self.dirty_label = ttk.Label(rowb, text="", anchor="e")
            self.dirty_label.pack(side=tk.RIGHT)
            body.add(rightf, weight=3)

            self._dirty = set()
            self._populate()

        def _populate(self):
            self.listbox.delete(0, tk.END)
            for idx in self.visible:
                s = sod_btxt.get_text(self.table, idx)
                preview = s.replace("\n", " ")[:60]
                star = "* " if idx in self._dirty else "  "
                self.listbox.insert(tk.END, "%s[%05d] %s" % (star, idx, preview))

        def _refilter(self):
            q = self.search_var.get().lower()
            if not q:
                self.visible = list(range(self.table.count))
            else:
                self.visible = [i for i in range(self.table.count)
                                if q in sod_btxt.get_text(self.table, i).lower()]
            self._populate()

        def _on_pick(self, event=None):
            sel = self.listbox.curselection()
            if not sel:
                return
            idx = self.visible[sel[0]]
            self.current_index = idx
            self.id_label.config(text="String #%d   (ID 0x%08X)" %
                                 (idx, self.table.hashes[idx]))
            self.editor.delete("1.0", tk.END)
            self.editor.insert("1.0", sod_btxt.get_text(self.table, idx))

        def _apply(self):
            if self.current_index is None:
                return
            txt = self.editor.get("1.0", "end-1c")
            sod_btxt.set_text(self.table, self.current_index, txt)
            self._dirty.add(self.current_index)
            self.dirty_label.config(text="%d edited" % len(self._dirty))
            # refresh just the visible label
            self._populate()

        def _save(self):
            # apply any pending edit in the box
            if self.current_index is not None:
                self._apply()
            self.on_save(self.entry_name, self.table)
            self.destroy()

# ----------------------------------------------------------------------------
#  Entry point: GUI by default; CLI when the first argument is "cli".
# ----------------------------------------------------------------------------
if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "cli":
        sys.exit(_cli_main(["sod_pak"] + sys.argv[2:]) or 0)
    if not _TK_OK:
        sys.stderr.write(
            "tkinter is not available, so the GUI cannot start.\n"
            "Use the command line instead, e.g.:\n"
            "  python %s cli verify gamedata.pak\n" % os.path.basename(sys.argv[0]))
        sys.exit(1)
    PakTool().mainloop()
