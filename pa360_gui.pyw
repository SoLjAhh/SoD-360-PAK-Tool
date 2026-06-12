#!/usr/bin/env python3
"""
pa360_gui.pyw  -  GUI front-end for the Prison Architect Xbox 360
                  .pkd / .pki archive tool.

Double-click to launch (the .pyw extension runs without a console on
Windows). Requires pa360_pkg.py in the same folder. Pure standard
library + tkinter; no external dependencies.

Workflow
--------
  1. Open archive   -> pick main.pkdxbox360 (or the .pki); the pair is
                       auto-detected. The file list populates.
  2. Extract All    -> dumps every file to a folder + manifest.json.
                       Edit the .png / .txt / .bin files you want.
  3. Repack         -> point at that folder; rebuilds main.pkd*/main.pki*.
                       Unmodified and Hx-compressed files are preserved
                       verbatim, so the archive is always valid.

You can also select a single row and "Extract Selected" to pull one file.
"""

import os
import sys
import threading
import traceback
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# --- import the engine that lives next to this script --------------------
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from pa360_pkg import Archive, _guess_pair
except Exception as exc:  # pragma: no cover
    # Fail loudly but gracefully if the engine file is missing.
    root = tk.Tk()
    root.withdraw()
    messagebox.showerror(
        "Missing engine",
        "Could not import pa360_pkg.py.\n\n"
        "Keep pa360_gui.pyw and pa360_pkg.py in the same folder.\n\n"
        f"Details: {exc}")
    sys.exit(1)


# Windows: suppress any stray console windows from child processes.
# (This tool spawns none, but the flag is harmless and matches a safe
# default for packaged builds.)
if os.name == "nt":
    try:
        import ctypes
        ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
    except Exception:
        pass


METHOD_COLORS = {
    "zlib": "#1b5e20",     # green  - fully editable
    "stored": "#0d47a1",   # blue   - raw (PNG)
    "hx": "#b71c1c",       # red    - compressed, preserved verbatim
    "empty": "#616161",
    "unknown": "#616161",
}


class App(ttk.Frame):
    def __init__(self, master):
        super().__init__(master, padding=8)
        self.master = master
        self.archive = None
        self.pack(fill="both", expand=True)
        self._build_ui()

    # ------------------------------------------------------------------ #
    # UI construction
    # ------------------------------------------------------------------ #
    def _build_ui(self):
        self.master.title("Prison Architect Xbox 360 — Archive Tool")
        self.master.geometry("860x560")
        self.master.minsize(700, 420)

        # top button bar
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 6))

        ttk.Button(bar, text="Open Archive…",
                   command=self.on_open).pack(side="left")
        self.btn_extract = ttk.Button(bar, text="Extract All…",
                                       command=self.on_extract_all,
                                       state="disabled")
        self.btn_extract.pack(side="left", padx=4)
        self.btn_extract_sel = ttk.Button(bar, text="Extract Selected…",
                                          command=self.on_extract_selected,
                                          state="disabled")
        self.btn_extract_sel.pack(side="left", padx=4)
        self.btn_repack = ttk.Button(bar, text="Repack…",
                                     command=self.on_repack,
                                     state="disabled")
        self.btn_repack.pack(side="left", padx=4)

        self.filter_var = tk.StringVar()
        ttk.Label(bar, text="Filter:").pack(side="left", padx=(16, 2))
        ent = ttk.Entry(bar, textvariable=self.filter_var, width=18)
        ent.pack(side="left")
        ent.bind("<KeyRelease>", lambda e: self._refill())

        # tree / file list
        cols = ("idx", "hash", "method", "ext", "stored", "extracted")
        self.tree = ttk.Treeview(self, columns=cols, show="headings",
                                 selectmode="browse")
        headers = {
            "idx": ("#", 56), "hash": ("Name hash", 170),
            "method": ("Storage", 90), "ext": ("Type", 60),
            "stored": ("Packed", 90), "extracted": ("Unpacked", 90),
        }
        for c, (txt, w) in headers.items():
            self.tree.heading(c, text=txt)
            anchor = "w" if c in ("hash",) else ("center"
                     if c in ("method", "ext") else "e")
            self.tree.column(c, width=w, anchor=anchor,
                             stretch=(c == "hash"))
        for m, col in METHOD_COLORS.items():
            self.tree.tag_configure(m, foreground=col)

        vsb = ttk.Scrollbar(self, orient="vertical",
                            command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="left", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        # bottom status + progress
        bottom = ttk.Frame(self.master, padding=(8, 4))
        bottom.pack(fill="x", side="bottom")
        self.status = tk.StringVar(value="Open an archive to begin.")
        ttk.Label(bottom, textvariable=self.status,
                  anchor="w").pack(side="left", fill="x", expand=True)
        self.progress = ttk.Progressbar(bottom, length=200,
                                         mode="determinate")
        self.progress.pack(side="right")

        self._all_rows = []  # cached (entry, method, ok, ext, dec_size)

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    def _set_busy(self, busy, msg=None):
        state = "disabled" if busy else "normal"
        if self.archive is not None:
            self.btn_extract.config(state=state)
            self.btn_extract_sel.config(state=state)
            self.btn_repack.config(state=state)
        if msg:
            self.status.set(msg)
        self.update_idletasks()

    def _run_bg(self, fn):
        """Run a long task in a thread so the UI stays responsive."""
        def wrapper():
            try:
                fn()
            except Exception:
                err = traceback.format_exc()
                self.master.after(0, lambda: self._fail(err))
        threading.Thread(target=wrapper, daemon=True).start()

    def _fail(self, err):
        self._set_busy(False)
        self.progress["value"] = 0
        messagebox.showerror("Error", err)
        self.status.set("Error — see dialog.")

    # ------------------------------------------------------------------ #
    # actions
    # ------------------------------------------------------------------ #
    def on_open(self):
        path = filedialog.askopenfilename(
            title="Open main.pkdxbox360 or main.pkixbox360",
            filetypes=[("PA Xbox 360 archive",
                        "*.pkd* *.pki* main.pkd* main.pki*"),
                       ("All files", "*.*")])
        if not path:
            return
        try:
            pkd, pki = _guess_pair(path)
        except ValueError as e:
            messagebox.showerror("Open", str(e))
            return
        if not (os.path.exists(pkd) and os.path.exists(pki)):
            messagebox.showerror(
                "Open",
                "Need BOTH files of the pair:\n\n"
                f"  {os.path.basename(pkd)}\n  {os.path.basename(pki)}\n\n"
                "Keep them in the same folder.")
            return

        self.status.set("Loading index…")
        self.update_idletasks()
        try:
            ar = Archive(pkd, pki).load()
        except Exception as e:
            messagebox.showerror("Open", f"Failed to parse archive:\n{e}")
            return
        self.archive = ar
        self._index_rows()
        self._refill()
        self.btn_extract.config(state="normal")
        self.btn_extract_sel.config(state="normal")
        self.btn_repack.config(state="normal")
        self.status.set(
            f"{os.path.basename(pkd)} — {len(ar.entries)} files "
            f"(green=editable, red=Hx compressed/verbatim).")

    def _index_rows(self):
        """Decode just enough per entry to classify + size it."""
        rows = []
        ar = self.archive
        n = len(ar.entries)
        self.progress["maximum"] = n
        for i, e in enumerate(ar.entries):
            data, method, ok = ar.decompress(e)
            ext = ar.detect_ext(data, method)
            rows.append((e, method, ok, ext, len(data)))
            if i % 64 == 0:
                self.progress["value"] = i
                self.update_idletasks()
        self.progress["value"] = 0
        self._all_rows = rows

    def _refill(self):
        self.tree.delete(*self.tree.get_children())
        flt = self.filter_var.get().strip().lower()
        for e, method, ok, ext, dec in self._all_rows:
            if flt:
                hay = f"{e.index} {e.hash_hex} {method} {ext}"
                if flt not in hay.lower():
                    continue
            self.tree.insert(
                "", "end", iid=str(e.index), tags=(method,),
                values=(e.index, e.hash_hex, method, ext,
                        f"{e.size:,}", f"{dec:,}"))

    def _on_select(self, _evt):
        sel = self.tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        for e, method, ok, ext, dec in self._all_rows:
            if e.index == idx:
                note = "" if ok else "  (compressed format not decoded — " \
                                     "extracts verbatim)"
                self.status.set(
                    f"#{e.index}  {e.hash_hex}  ·  {method}/.{ext}  ·  "
                    f"packed {e.size:,} → unpacked {dec:,} bytes{note}")
                break

    def on_extract_all(self):
        out = filedialog.askdirectory(title="Choose an output folder")
        if not out:
            return
        self._set_busy(True, "Extracting…")
        ar = self.archive
        n = len(ar.entries)
        self.progress["maximum"] = n

        def task():
            def prog(i, total, name):
                if i % 32 == 0 or i == total:
                    self.master.after(
                        0, lambda i=i: self.progress.config(value=i))
            manifest, stats = ar.extract_all(out, progress=prog)
            msg = (f"Extracted {len(manifest['entries'])} files → {out}  "
                   f"(zlib {stats.get('zlib',0)}, png "
                   f"{stats.get('stored',0)}, hx {stats.get('hx',0)})")
            self.master.after(0, lambda: self._done_extract(msg))
        self._run_bg(task)

    def _done_extract(self, msg):
        self._set_busy(False)
        self.progress["value"] = 0
        self.status.set(msg)
        messagebox.showinfo("Extract complete", msg)

    def on_extract_selected(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Extract selected", "Select a row first.")
            return
        idx = int(sel[0])
        entry = next(e for e, *_ in self._all_rows if e.index == idx)
        row = next(r for r in self._all_rows if r[0].index == idx)
        ext = row[3]
        default = f"{entry.index:05d}_{entry.hash_hex}.{ext}"
        out = filedialog.asksaveasfilename(
            title="Save extracted file", initialfile=default)
        if not out:
            return
        method, ok, size = self.archive.extract_one(entry, out)
        self.status.set(f"Saved {os.path.basename(out)} "
                        f"({size:,} bytes, {method}).")

    def on_repack(self):
        mod_dir = filedialog.askdirectory(
            title="Folder with your edited files (from Extract All)")
        if not mod_dir:
            return
        out_pkd = filedialog.asksaveasfilename(
            title="Save rebuilt .pkd as…",
            initialfile="main.pkdxbox360")
        if not out_pkd:
            return
        # pair the pki name to the pkd name automatically
        out_pki = out_pkd.replace("pkd", "pki")
        if out_pki == out_pkd:
            out_pki = out_pkd + ".pki"

        self._set_busy(True, "Repacking…")
        ar = self.archive
        n = len(ar.entries)
        self.progress["maximum"] = n

        def task():
            def prog(i, total, _idx):
                if i % 32 == 0 or i == total:
                    self.master.after(
                        0, lambda i=i: self.progress.config(value=i))
            res = ar.repack(mod_dir, out_pkd, out_pki, progress=prog)
            msg = (f"Repacked → {os.path.basename(out_pkd)} + "
                   f"{os.path.basename(out_pki)}  "
                   f"(replaced {res['replaced']}, "
                   f"Hx preserved {res['skipped_hx']}, "
                   f"total {res['total']})")
            self.master.after(0, lambda: self._done_repack(msg))
        self._run_bg(task)

    def _done_repack(self, msg):
        self._set_busy(False)
        self.progress["value"] = 0
        self.status.set(msg)
        messagebox.showinfo("Repack complete", msg)


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("clam")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
