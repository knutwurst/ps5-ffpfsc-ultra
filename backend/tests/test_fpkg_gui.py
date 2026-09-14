"""
In-process GUI test for the fPKG integration. Drives the real App object (hidden root)
and exercises what the CLI harness cannot: build_command() argv for fPKG jobs, the Pack
dialog's '.pkg' format (identity pre-fill from param.json, optional identity for images
and archives, validation, add via the shared classifier, edit, pack ↔ fPKG switch), the
remembered-format behaviour of a browsed/dropped source, the Extract mini dialog, queue
badges/details, double-click dispatch, dialog sizing, and queue persistence.

  /tmp/ps5venv/bin/python backend/tests/test_fpkg_gui.py [--work DIR]

Needs the GUI deps (customtkinter, tkinterdnd2, pillow, psutil) and a display; the
user's settings.json is snapshotted before and restored after the run.
"""
import sys, importlib.util, traceback, argparse, shutil, subprocess, tempfile, time, zipfile
from pathlib import Path
HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(REPO / "backend"))
from test_fpkg_pipelines import fetch_hbt, CLI   # reuse the fixture + cli path
ap = argparse.ArgumentParser(); ap.add_argument("--work", type=Path, default=Path(tempfile.gettempdir()) / "ffpfsc-fpkg-gui-tests")
a = ap.parse_args(); S = a.work
if S.exists(): shutil.rmtree(S)
S.mkdir(parents=True)
HBT = fetch_hbt(S / "hbt"); OUT = S / "gui_drive_out"; OUT.mkdir()
# seed artefacts: one .ffpfsc (image-source path), one .pkg (extract path), one .zip (archive path)
subprocess.run([sys.executable, "-u", str(CLI), str(HBT), str(S / "c2_ffpfsc"), "--pack", "--overwrite"], capture_output=True, timeout=300)
subprocess.run([sys.executable, "-u", str(CLI), str(HBT), str(S / "c1_pkg"), "--fpkg-build", str(HBT)], capture_output=True, timeout=300)
FF = next((S / "c2_ffpfsc").glob("*.ffpfsc"))
ZP = S / "HomebrewTest.zip"
with zipfile.ZipFile(ZP, "w") as z:
    for f in HBT.rglob("*"):
        if f.is_file(): z.write(f, f"HomebrewTest/{f.relative_to(HBT)}")
import os; os.chdir(REPO)
spec = importlib.util.spec_from_file_location("ultra", str(REPO / "PS5_FFPFSC_ULTRA_v1.0.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
errors = []
def showerror(title, msg, **kw): errors.append(f"{title}: {msg}")
m.messagebox.showerror = showerror
m.messagebox.showinfo = lambda *a, **k: None
m.messagebox.askyesno = lambda *a, **k: True
import signal, shutil as _sh
signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(SystemExit("driver timeout")))
signal.alarm(240)
# Protect the user's real settings/queue: snapshot settings.json, restore in finally.
_settings = m.APP_DIR / "settings.json"
_backup = _settings.with_suffix(".json.driver-backup")
if _settings.exists():
    _sh.copy2(_settings, _backup)
m.ensure_app_dir()
root = m._CTkDnD(); root.withdraw()
app = m.App(root)
app.queue.clear()
res = []
def ok(name, cond, detail=""):
    res.append((name, bool(cond), detail))
def pump(cond, timeout=20.0):
    """Run the Tk loop (after-callbacks included: the scan_q consumer) until cond() or timeout."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        root.update()
        if cond(): return True
        time.sleep(0.05)
    return cond()
def close_toplevels():
    for w in root.winfo_children():
        try:
            if isinstance(w, m.ctk.CTkToplevel): w.destroy()
        except Exception: pass
try:
    # 1) build_command for an fpkg-build item (folder) — identity fields ride along as fallbacks
    it = m.GameItem.from_fpkg_build(HBT, output_path=str(OUT), content_id="UP9000-PPSA99099_00-PROSPERO00000000",
                                    title_id="PPSA99099", title="HBT", inner_mode="kraken", kraken_backend="builtin", level=5)
    cmd, cwd, outdir, temp = app.build_command(it)
    ok("build_command.fpkg-build", "--fpkg-build" in cmd and "--temp-dir" in cmd and "--compression-level" in cmd
       and cmd[cmd.index("--compression-level")+1] == "5" and "--fpkg-inner" in cmd and cmd[cmd.index("--fpkg-inner")+1] == "kraken"
       and "--content-id" in cmd and "--fpkg-version" not in cmd,   # default version is not passed (param.json decides)
       " ".join(cmd[-14:]))
    ok("fpkg-build.size-set", it.size > 0, f"size={it.size}")
    # 1b) a bundle fPKG job mirrors its folder at the destination
    itb = m.GameItem.from_fpkg_build(HBT, output_path=str(OUT), content_id="UP9000-PPSA99099_00-PROSPERO00000000", title_id="PPSA99099")
    itb.bundle_subfolder = "My Bundle"
    cmdb, _, outb, _ = app.build_command(itb)
    ok("build_command.fpkg-bundle-subfolder", outb == OUT / "My Bundle" and str(OUT / "My Bundle") in cmdb, str(outb))
    # 2) build_command for fpkg-extract
    pkg = next((S / "c1_pkg").glob("*.pkg"), None)
    ok("seed.pkg-built-without-ids", pkg is not None, "the seed build passed no --content-id: param.json supplied it")
    if pkg:
        ie = m.GameItem.from_fpkg_extract(pkg, output_path=str(OUT / "ext"))
        cmd2, *_ = app.build_command(ie)
        ok("build_command.fpkg-extract", "--fpkg-extract" in cmd2 and str(OUT / "ext") in cmd2, " ".join(cmd2[-4:]))
    # 3) Pack dialog, .pkg format, FOLDER source → identity pre-filled from param.json; add goes through
    #    the shared classifier (async scan) and lands as an fpkg-build job carrying that identity
    dlg = m.PackDialog(app, source=HBT, fmt="pkg"); root.update()
    ok("dialog.autofill.cid", dlg.cid_var.get() == "UP9000-PPSA99099_00-PROSPERO00000000", dlg.cid_var.get())
    ok("dialog.autofill.tid", dlg.tid_var.get() == "PPSA99099", dlg.tid_var.get())
    ok("dialog.pkg.panel-shown", dlg._panel_shown and dlg.fmt_key == "pkg", dlg.geometry())
    dlg.out_var.set(str(OUT)); dlg.inner_var.set("kraken"); dlg.speed_var.set("fast"); dlg._refresh_hints()
    n0 = len(app.queue); dlg._add(); pump(lambda: len(app.queue) > n0)
    ok("dialog.add.queued", len(app.queue) == n0 + 1 and app.queue[-1].operation == "fpkg-build", f"queue={len(app.queue)} errors={errors}")
    q = app.queue[-1]
    ok("dialog.add.fields", q.fpkg_inner_mode == "kraken" and q.fpkg_level == -4 and q.fpkg_content_id == "UP9000-PPSA99099_00-PROSPERO00000000"
       and q.files > 0 and q.path == HBT, f"{q.fpkg_inner_mode}/{q.fpkg_level}/{q.fpkg_content_id}/files={q.files}")
    ok("dialog.add.remembered", app.output_format_var.get() == "pkg" and app.fpkg_defaults["inner"] == "kraken"
       and app.fpkg_defaults["level"] == -4 and app._pending_fpkg_identity is None, f"{app.output_format_var.get()} {app.fpkg_defaults}")
    cmdq, *_ = app.build_command(q)
    ok("build_command.fast-preset", cmdq[cmdq.index("--compression-level")+1] == "-4", " ".join(cmdq[-6:]))
    # 4) .pkg format with an IMAGE source → identity stays empty (auto at build time), direct fPKG job
    dlg2 = m.PackDialog(app, source=FF, fmt="pkg"); root.update()
    ok("dialog.image.identity-empty", dlg2.cid_var.get() == "" and dlg2.tid_var.get() == "", f"{dlg2.cid_var.get()!r}")
    ok("dialog.image.hint", "unwrapped" in dlg2.src_hint.get(), dlg2.src_hint.get())
    dlg2.out_var.set(str(OUT)); n1 = len(app.queue); dlg2._add(); root.update()
    ok("dialog.image.queued", len(app.queue) == n1 + 1 and app.queue[-1].path == FF and app.queue[-1].operation == "fpkg-build", f"errors={errors}")
    cmd3, *_ = app.build_command(app.queue[-1])
    ok("build_command.image-source", cmd3[cmd3.index("--fpkg-build")+1] == str(FF) and "--content-id" not in cmd3, " ".join(cmd3[-10:]))
    # 5) edit round trip (same source, stays fPKG)
    dlg3 = m.PackDialog(app, item=q); root.update()
    ok("dialog.edit.preselects-pkg", dlg3.fmt_key == "pkg" and dlg3._panel_shown and dlg3.speed_var.get() == "fast", f"{dlg3.fmt_key}/{dlg3.speed_var.get()}")
    dlg3.speed_var.set("normal"); dlg3.title_var.set("Edited"); dlg3._add(); root.update()
    ok("dialog.edit.saved", q.fpkg_level == 7 and q.fpkg_title == "Edited", f"{q.fpkg_level}/{q.fpkg_title} errors={errors}")
    # 5b) edit: switch fPKG → .ffpfsc on the SAME source converts the job in place (same object,
    #     same index, fpkg_* fields dropped) — and back
    idx = app.queue.index(q)
    dlg3b = m.PackDialog(app, item=q); root.update(); dlg3b.set_format("ffpfsc"); root.update()
    ok("dialog.edit.panel-hidden", not dlg3b._panel_shown, dlg3b.geometry())
    dlg3b._add(); root.update()
    rep = app.queue[idx]
    ok("dialog.edit.to-pack", rep is q and rep.operation == "pack" and rep.output_compressed is True and rep.path == HBT
       and "fpkg_level" not in vars(rep), f"{rep.operation} errors={errors}")
    dlg3c = m.PackDialog(app, item=rep); root.update(); dlg3c.set_format("pkg"); root.update(); dlg3c._add(); root.update()
    q = app.queue[idx]
    ok("dialog.edit.back-to-pkg", q.operation == "fpkg-build" and q.fpkg_content_id == "UP9000-PPSA99099_00-PROSPERO00000000", f"{q.operation} errors={errors}")
    # 6) validation rejects a bad content id (uses our patched showerror) — and reveals the identity rows
    dlg4 = m.PackDialog(app, source=HBT, fmt="pkg"); root.update()
    ok("dialog.compact-by-default", not dlg4._adv_shown and not dlg4._ident_forced and dlg4.irow.winfo_manager() == ""
       and dlg4.crow.winfo_manager() == "", f"adv={dlg4._adv_shown} forced={dlg4._ident_forced}")
    # the summary reflects the REMEMBERED compression choice (test 3 picked kraken/fast for this app)
    ok("dialog.summary", "identity from param.json (PPSA99099)" in dlg4.sum_var.get()
       and f"codec layer {app.fpkg_defaults['inner']}" in dlg4.sum_var.get()
       and ("Kraken fast" if app.fpkg_defaults["level"] < 0 else "Kraken normal") in dlg4.sum_var.get(), dlg4.sum_var.get())
    ok("dialog.backend-row-hidden", not dlg4._backend_shown and dlg4.back_var.get() == "builtin", f"dll={app.pubtools_dll_var.get()!r}")
    dlg4.out_var.set(str(OUT)); dlg4.cid_var.set("garbage"); n2 = len(app.queue); dlg4._add(); root.update()
    ok("dialog.validation.bad-cid", len(app.queue) == n2 and any("Content ID" in e for e in errors), str(errors[-1:]))
    ok("dialog.validation.reveals-identity", dlg4._ident_forced and dlg4.irow.winfo_manager() == "pack", f"forced={dlg4._ident_forced}")
    dlg4.destroy()
    # 6a) a game folder WITHOUT param.json opens the identity rows by itself and marks them required
    NOP = S / "hbt_noparam"
    if NOP.exists(): shutil.rmtree(NOP)
    shutil.copytree(HBT, NOP); (NOP / "sce_sys" / "param.json").unlink()
    dlg4b = m.PackDialog(app, source=NOP, fmt="pkg"); root.update(); dlg4b.update_idletasks()
    ok("dialog.noparam.identity-forced", dlg4b._ident_forced and dlg4b.irow.winfo_manager() == "pack"
       and "REQUIRED" in dlg4b.ident_head.get() and "REQUIRED" in dlg4b.sum_var.get() and dlg4b.crow.winfo_manager() == "",
       f"forced={dlg4b._ident_forced} head={dlg4b.ident_head.get()[:40]}")
    ok("dialog.noparam.fits", 0 < dlg4b.winfo_reqheight() <= dlg4b._height, f"req={dlg4b.winfo_reqheight()} h={dlg4b._height}")
    dlg4b.out_var.set(str(OUT)); n2b = len(app.queue); dlg4b._add(); root.update()
    ok("dialog.noparam.requires-identity", len(app.queue) == n2b and any("Identity needed" in e for e in errors), str(errors[-1:]))
    dlg4b.src_var.set(str(HBT)); root.update()
    ok("dialog.noparam.unforced-on-good-source", not dlg4b._ident_forced and dlg4b.irow.winfo_manager() == "", f"forced={dlg4b._ident_forced}")
    dlg4b.destroy()
    # 6b) a .ffpfsc is refused for the .ffpfsc / .ffpfs formats (already packed) …
    dlg5 = m.PackDialog(app, source=FF, fmt="ffpfsc"); root.update()
    ok("dialog.ffpfsc.hint", "already packed" in dlg5.src_hint.get().lower(), dlg5.src_hint.get())
    dlg5.out_var.set(str(OUT)); n3 = len(app.queue); dlg5._add(); root.update()
    ok("dialog.ffpfsc.refused-for-pack", len(app.queue) == n3 and any("Already packed" in e for e in errors), str(errors[-1:]))
    dlg5.destroy()
    # 7) ARCHIVE source with .pkg → queued as an fPKG placeholder; the payload copy after extraction keeps it fPKG
    dlg6 = m.PackDialog(app, source=ZP, fmt="pkg"); root.update()
    ok("dialog.archive.hint", "extracted" in dlg6.src_hint.get().lower(), dlg6.src_hint.get())
    # identity typed for an archive (e.g. a homebrew zip without param.json) must ride along as the fallback
    dlg6.cid_var.set("UP9000-PPSA99099_00-PROSPERO00000000"); dlg6.tid_var.set("PPSA99099")
    dlg6.out_var.set(str(OUT)); n4 = len(app.queue); dlg6._add(); pump(lambda: len(app.queue) > n4)
    arc = app.queue[-1]
    ok("archive.queued-as-fpkg", len(app.queue) == n4 + 1 and arc.operation == "fpkg-build" and arc.archive_path == ZP
       and arc.fpkg_inner_mode == app.fpkg_defaults["inner"], f"{getattr(arc, 'operation', None)} {getattr(arc, 'archive_path', None)} errors={errors}")
    ok("archive.carries-typed-identity", arc.fpkg_content_id == "UP9000-PPSA99099_00-PROSPERO00000000" and arc.fpkg_title_id == "PPSA99099", f"{arc.fpkg_content_id!r}")
    app._copy_item_payload(arc, m.GameItem(HBT))
    ok("archive.payload-keeps-fpkg", arc.operation == "fpkg-build" and arc.path == HBT and arc.fpkg_inner_mode == app.fpkg_defaults["inner"], f"{arc.operation}")
    # 7b) sibling jobs from one archive share COMPRESSION only — never the first game's identity
    tpl = app._fpkg_compression_of(arc)
    ok("compression-of.no-identity", set(tpl) == {"inner", "backend", "level", "dll"} and tpl["inner"] == arc.fpkg_inner_mode, str(tpl))
    # 7c) a scan-detected patch is dropped (with a WARN) when the bundle becomes an fPKG job; a .ffpfsc source is sized at 2x for the gate
    pi = m.GameItem(HBT); pi.patch_source = Path("/nonexistent/patch"); app._as_fpkg_job(pi, dict(app.fpkg_defaults))
    ok("as-fpkg.drops-patch", pi.patch_source is None and pi.operation == "fpkg-build", str(pi.patch_source))
    fi = app._fpkg_item_for(FF, dict(app.fpkg_defaults), output_path=str(OUT))
    ok("as-fpkg.ffpfsc-size-estimate", fi is not None and fi.size > 0 and fi.extracted_size == 2 * fi.size, f"{getattr(fi, 'extracted_size', None)}")
    # 8) remembered format drives a browsed / dropped .ffpfsc: .pkg → fPKG build, .ffpfsc → unpack
    app.output_format_var.set("pkg"); app.source_var.set(str(FF)); n5 = len(app.queue); app.add_source_to_queue(); root.update()
    ok("drop.ffpfsc.remembered-pkg", len(app.queue) == n5 + 1 and app.queue[-1].operation == "fpkg-build", getattr(app.queue[-1], "operation", None))
    app.output_format_var.set("ffpfsc"); app.source_var.set(str(FF)); n6 = len(app.queue); app.add_source_to_queue(); root.update()
    ok("drop.ffpfsc.remembered-ffpfsc", len(app.queue) == n6 + 1 and app.queue[-1].operation == "unpack", getattr(app.queue[-1], "operation", None))
    ok("format.legacy-bool-synced", app.output_compressed_var.get() is True, str(app.output_compressed_var.get()))
    # 8b) edit IN PLACE keeps bundle tags and queue position across pack → pkg → pack
    bi = m.GameItem(HBT); bi.bundle_subfolder = "Bundle X"; bi.output_compressed = True; app.queue.append(bi); app.update_queue_box()
    bidx = app.queue.index(bi)
    e1 = m.PackDialog(app, item=bi); root.update(); e1.set_format("pkg"); root.update(); e1._add(); root.update()
    ok("edit.inplace.pack-to-pkg", app.queue[bidx] is bi and bi.operation == "fpkg-build" and bi.bundle_subfolder == "Bundle X"
       and bi.fpkg_content_id == "UP9000-PPSA99099_00-PROSPERO00000000", f"{bi.operation} errors={errors[-1:]}")
    e2 = m.PackDialog(app, item=bi); root.update(); e2.set_format("ffpfs"); root.update(); e2._add(); root.update()
    ok("edit.inplace.pkg-to-pack", app.queue[bidx] is bi and bi.operation == "pack" and bi.output_compressed is False
       and "fpkg_level" not in vars(bi) and bi.bundle_subfolder == "Bundle X", f"{bi.operation} errors={errors[-1:]}")
    # 8c) a pending identity never lingers past a failed add; fPKG extract bypasses the pack space gate
    app._pending_fpkg_identity = (HBT, {"content_id": "X"}); app.source_var.set(str(S / "does-not-exist")); app.add_source_to_queue(); root.update()
    ok("pending-identity.cleared-on-error", app._pending_fpkg_identity is None, str(app._pending_fpkg_identity))
    if pkg:
        ok("space-gate.fpkg-extract-proceeds", app._space_gate(ie, OUT) == "proceed", "")
    # 9) mini edit dialog for fpkg-extract
    if pkg:
        app.queue.append(ie); mini = m.JobEditMiniDialog(app, ie); root.update()
        mini.out_var.set(str(OUT / "ext2")); mini._save(); root.update()
        ok("mini.edit.fpkg-extract", str(ie.output_path) == str(OUT / "ext2"), str(ie.output_path))
    # 10) queue rendering with badges + details + tune-bar note
    app.update_queue_box(); root.update()
    rows = [app.queue_listbox.get(i) for i in range(app.queue_listbox.size())]
    ok("queue.badges", any("fPKG-BD" in r for r in rows) and (not pkg or any("fPKG-EX" in r for r in rows)), rows[:2])
    app.update_game_details(q); ok("details.mode", "Build fPKG" in app.title_var.get(), app.title_var.get())
    ok("details.tune-note", "fPKG build selected" in app.tune_note_var.get(), app.tune_note_var.get()[:60])
    # 11) double-click dispatch opens the Pack dialog for fpkg-build (no exception)
    app.queue_listbox.selection_clear(0, "end"); idx = app.queue.index(q); app.queue_listbox.selection_set(idx)
    app._on_queue_double_click(None); root.update()
    ok("doubleclick.dispatch", not any("Could not open editor" in str(x) for x in errors), "")
    close_toplevels()
    # 12) one door: the separate Build-fPKG dialog is gone
    ok("one-door.no-FpkgBuildDialog", not hasattr(m, "FpkgBuildDialog"), "")
    # 13) dialog sizing: the window follows its content in every state (compact / Edit… / pack)
    dlg7 = m.PackDialog(app, fmt="pkg"); root.update(); dlg7.update_idletasks()
    req_c = dlg7.winfo_reqheight(); h_c = dlg7._height
    ok("dialog.pkg.compact.fits", 0 < req_c <= h_c and h_c < 620, f"req={req_c} h={h_c}")
    dlg7.set_advanced(True); root.update(); dlg7.update_idletasks()
    req_a = dlg7.winfo_reqheight(); h_a = dlg7._height
    ok("dialog.pkg.advanced.fits", 0 < req_a <= h_a and h_a > h_c and dlg7.irow.winfo_manager() == "pack" and dlg7.crow.winfo_manager() == "pack"
       and "Hide" in dlg7._edit_btn.cget("text"), f"req={req_a} h={h_a}")
    dlg7.set_advanced(False); root.update()
    ok("dialog.pkg.hide-again", dlg7.irow.winfo_manager() == "" and dlg7._height == h_c, f"h={dlg7._height}")
    dlg7.set_format("ffpfsc"); root.update(); dlg7.update_idletasks()
    req_pack = dlg7.winfo_reqheight(); ok("dialog.pack.fits", 0 < req_pack <= dlg7._height <= 460, f"req={req_pack} h={dlg7._height}")
    dlg7.destroy()
    # 14) settings var present
    ok("settings.pubtools_var", hasattr(app, "pubtools_dll_var"), "")
    # 15) queue save/restore keeps fpkg fields
    app._queue_restored = True; app._save_queue()
    saved = m.load_settings().get("queue") or []
    ok("queue.persist.fpkg-fields", any(d.get("operation") == "fpkg-build" and "fpkg_level" in d and "fpkg_content_id" in d for d in saved), f"{len(saved)} saved")
    ok("settings.persist.format", m.load_settings().get("output_format") == "ffpfsc" and isinstance(m.load_settings().get("fpkg_defaults"), dict), "")
except Exception:
    res.append(("driver", False, traceback.format_exc()))
finally:
    # restore the user's real settings.json (the driver mutated queue + defaults)
    try:
        if _backup.exists():
            _sh.copy2(_backup, _settings); _backup.unlink()
    except Exception as e:
        print("WARN could not restore settings backup:", e)
    try: root.destroy()
    except Exception: pass
bad = 0
for name, okv, det in res:
    print(("PASS " if okv else "FAIL ") + f"{name:34s} {det}"[:200]); bad += (not okv)
    if name == "driver": open(str(S / "driver_traceback.txt"), "w").write(det)
print(f"{len(res)-bad} passed, {bad} failed")
sys.exit(1 if bad else 0)
