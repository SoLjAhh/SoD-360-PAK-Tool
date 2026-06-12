#!/usr/bin/env python3
"""
State of Decay (Xbox 360) .pak Extractor & Editor
=================================================

A self-contained tkinter tool to browse, extract, edit and repack the
State of Decay 360 Arcade `gamedata.pak` (and the other SoD `.pak` files,
which share the custom method-13 / LZX format).

Why this exists: the 360 build compresses entries with Microsoft LZX, so
WinRAR/7-Zip and the PC-era tools (SoDET, quickBMS) fail to extract it. This
tool implements the format directly (see sod_pak.py / sod_lzx.py).

No third-party dependencies. Works on any machine with Python 3.8+ and tkinter.
"""

import os
import sys
import threading
import traceback

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, font as tkfont

import sod_pak
import sod_dds
import sod_btxt
import sod_ddsview

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
            self.pak = sod_pak.PakArchive(path)
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
                sod_pak.write_pak(path, self.pak.entries, self.edits)
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


if __name__ == "__main__":
    PakTool().mainloop()
