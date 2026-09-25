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
import sys, importlib.util, traceback, argparse, shutil, subprocess, tempfile, time, zipfile, json, re
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
    itb.bundle_subfolder = "My Bundle"; itb.auto_organize = False     # classic bundle mirroring (auto-organize off)
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
    # even with a DLL configured the backend row stays away on macOS (LibProsperoPkg: Windows only, hard failure)
    _prev_dll = app.pubtools_dll_var.get(); app.pubtools_dll_var.set("/nonexistent/libScePubTools.dll")
    dlg4x = m.PackDialog(app, source=HBT, fmt="pkg"); root.update()
    ok("dialog.backend-row-windows-only", (not dlg4x._backend_shown and dlg4x.back_var.get() == "builtin") if sys.platform != "win32" else dlg4x._backend_shown, f"shown={dlg4x._backend_shown}")
    px = m.GameItem(HBT); app._as_fpkg_job(px, {"inner": "none", "backend": "publishingtools", "level": 7, "dll": "/nonexistent/x.dll"})
    ok("as-fpkg.publishingtools-forced-builtin", (px.fpkg_kraken_backend == "builtin" and px.fpkg_pubtools_dll == "") if sys.platform != "win32" else px.fpkg_kraken_backend == "publishingtools", f"{px.fpkg_kraken_backend}")
    dlg4x.destroy(); app.pubtools_dll_var.set(_prev_dll)
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
    # 6b) a .ffpfsc source with the .ffpfsc TARGET format → same-format copy job (1.1.8+).
    # The pre-1.1.8 refusal ("already packed — pick .pkg to build an fPKG") is gone: the file
    # is already in the target format, so it's transported as-is via the queue's copy op.
    dlg5 = m.PackDialog(app, source=FF, fmt="ffpfsc"); root.update()
    ok("dialog.ffpfsc.copy-hint", "copied unchanged" in dlg5.src_hint.get().lower(), dlg5.src_hint.get())
    ok("dialog.ffpfsc.copy-row-shown", dlg5.copy_row.winfo_manager() != "", f"manager={dlg5.copy_row.winfo_manager()!r}")
    dlg5.out_var.set(str(OUT)); n3 = len(app.queue); dlg5._add(); root.update()
    ok("dialog.ffpfsc.enqueued-as-copy",
       len(app.queue) == n3 + 1 and app.queue[-1].operation == "copy",
       f"op={getattr(app.queue[-1], 'operation', None) if app.queue else None} errors={errors[-1:]}")
    ok("dialog.ffpfsc.copy.delete-source-default", getattr(app.queue[-1], "copy_delete_source", None) is True,
       f"delete_source={getattr(app.queue[-1], 'copy_delete_source', None)}")
    app.queue.pop()   # keep the fixture queue clean for later tests
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
    _build_opts = {"inner", "backend", "level", "dll", "retail_normalize", "hdr_flag", "regen_playgo", "fake_sign"}
    ok("compression-of.no-identity", set(tpl) == _build_opts and tpl["inner"] == arc.fpkg_inner_mode
       and not ({"content_id", "title_id", "title", "version"} & set(tpl)), str(tpl))
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
    # 14b) AMPR: a PlayGo game that SHIPS fakelib/*.sprx (+ ampr_emu.index) must not ask for the emu folder
    APR = S / "apr_game"
    if APR.exists(): shutil.rmtree(APR)
    shutil.copytree(HBT, APR); (APR / "sce_sys" / "playgo-chunk.dat").write_bytes(b"\0" * 64)
    (APR / "fakelib").mkdir(); (APR / "fakelib" / "libSceAmpr.sprx").write_bytes(b"A" * 32); (APR / "fakelib" / "libScePlayGo.sprx").write_bytes(b"P" * 32)
    (APR / "ampr_emu.index").write_bytes(b"SHIPPED-INDEX")
    prompts = []
    app._ensure_ampr_folder = lambda: (prompts.append(1), False)[1]   # a prompt would block the driver
    _prev_ampr = app.ampr_var.get(); app.ampr_var.set(""); _prev_sign = app.fake_sign_before_pack_var.get(); app.fake_sign_before_pack_var.set(False)
    ai = m.GameItem(APR)
    ok("ampr.detected", ai.ampr_emu is True, str(ai.ampr_emu))
    app._prepare_ampr(ai)
    ok("ampr.shipped.no-prompt", not prompts and (APR / "ampr_emu.index").read_bytes() == b"SHIPPED-INDEX"
       and not getattr(ai, "_ampr_injected", None), f"prompts={len(prompts)}")
    (APR / "ampr_emu.index").unlink(); ai2 = m.GameItem(APR); app._prepare_ampr(ai2)
    ok("ampr.shipped-sprx.index-built", not prompts and (APR / "ampr_emu.index").is_file() and (APR / "ampr_emu.index").read_bytes()[:8] == b"AMPRIDX3",
       f"prompts={len(prompts)} exists={(APR / 'ampr_emu.index').is_file()}")
    shutil.rmtree(APR / "fakelib"); ai3 = m.GameItem(APR); app._prepare_ampr(ai3)
    ok("ampr.nothing-shipped.prompts", len(prompts) == 1, f"prompts={len(prompts)}")
    del app._ensure_ampr_folder; app.ampr_var.set(_prev_ampr); app.fake_sign_before_pack_var.set(_prev_sign)
    # 14c) Auto-organize: names come from the game's own metadata — folder pack, image, fPKG
    app.auto_organize_var.set(True)
    gi = m.GameItem(HBT); gi.output_compressed = True; gi.auto_organize = True; gi.output_path = OUT
    cmdg, _, outg, _ = app.build_command(gi)
    exp_dir = OUT / "LibProsperoPKG [PPSA99099] [v01.000.000]"
    outfile = Path(cmdg[cmdg.index(str(HBT)) + 1])
    ok("organize.pack.folder+file", outg == exp_dir and outfile.parent == exp_dir and outfile.name == "LibProsperoPKG [PPSA99099] [v01.000].ffpfsc", f"{outg} | {outfile.name}")
    idf = app._game_identity(m.GameItem.from_exfat(FF))
    ok("organize.identity.from-ffpfsc", bool(idf) and idf.get("title_id") == "PPSA99099" and str(idf.get("version", "")).startswith("01.000") and idf.get("title") == "LibProsperoPKG", str(idf))
    fi2 = app._fpkg_item_for(FF, dict(app.fpkg_defaults), output_path=str(OUT)); fi2.auto_organize = True
    cmdf2, _, outf2, _ = app.build_command(fi2)
    ok("organize.fpkg.folder+name", outf2 == exp_dir and getattr(fi2, "_organized_pkg_name", None) == "LibProsperoPKG [PPSA99099] [v01.000].pkg", f"{outf2} | {getattr(fi2, '_organized_pkg_name', None)}")
    exp_dir.mkdir(parents=True, exist_ok=True); dummy = exp_dir / "UP9000-PPSA99099_00-PROSPERO00000000-A0100-V0100.pkg"; dummy.write_bytes(b"x")
    renamed = app._finalize_pkg_name(fi2, dummy)
    ok("organize.fpkg.renamed", renamed.name == "LibProsperoPKG [PPSA99099] [v01.000].pkg" and renamed.exists() and not dummy.exists(), str(renamed.name))
    # the real worker path: the backend's "[OK] fPKG complete: <path>" marker pre-sets output_path
    # and _find_output returns from that branch — the rename must happen there (1.1.4 missed it)
    fi3 = app._fpkg_item_for(FF, dict(app.fpkg_defaults), output_path=str(OUT)); fi3.auto_organize = True
    cmd3w, cwd3, out3w, tmp3 = app.build_command(fi3)
    out3w.mkdir(parents=True, exist_ok=True); dummy2 = out3w / "UP9000-PPSA99099_00-PROSPERO00000000-A0100-V0100.pkg"; dummy2.write_bytes(b"pkg")
    w = m.CLIWorker(app, fi3, cmd3w, cwd3, out3w, tmp3); w.start_time = time.time() - 30
    w.output_path = str(dummy2)          # what the marker line sets
    found = w._find_output()
    ok("organize.worker.marker-rename", found and Path(w.output_path).name == "LibProsperoPKG [PPSA99099] [v01.000].pkg"
       and Path(w.output_path).exists() and not dummy2.exists(), f"{found} {w.output_path}")
    ok("organize.title-cleanup", m.canonical_game_title("a large retail title™") == "a large retail title"
       and m.organized_names({"title": "Example Quest Deluxe Edition", "title_id": "PPSA99098", "version": "01.200.007"}, ".ffpfsc")
       == ("Example Quest Deluxe Edition [PPSA99098] [v01.200.007]", "Example Quest Deluxe Edition [PPSA99098] [v01.200].ffpfsc"),
       str(m.organized_names({"title": "Example Quest Deluxe Edition", "title_id": "PPSA99098", "version": "01.200.007"}, ".ffpfsc")))
    # off: a single-archive folder whose parent is the output folder must not mirror into itself; elsewhere it still does
    conv = OUT / "convert"; conv.mkdir(exist_ok=True); shutil.copy2(ZP, conv / ZP.name)
    bi2 = m.GameItem.from_bundle(conv, conv / ZP.name, []); bi2.output_compressed = True; bi2.auto_organize = False; bi2.output_path = OUT
    bi2.path = HBT; bi2.archive_path = None            # as _copy_item_payload leaves it after extraction
    _, _, outb2, _ = app.build_command(bi2)
    ok("mirror.not-into-source", outb2 == OUT, str(outb2))
    bi3 = m.GameItem.from_bundle(conv, conv / ZP.name, []); bi3.output_compressed = True; bi3.auto_organize = False; bi3.output_path = OUT / "lib"
    bi3.path = HBT; bi3.archive_path = None
    _, _, outb3, _ = app.build_command(bi3)
    ok("mirror.elsewhere-kept", outb3 == OUT / "lib" / "convert", str(outb3))
    # dialog: checkbox present, remembered default, out label reflects it
    dlg9 = m.PackDialog(app, source=HBT, fmt="ffpfsc"); root.update()
    ok("dialog.organize.checkbox", hasattr(dlg9, "organize_var") and dlg9.organize_var.get() is True and "auto-organize" in dlg9.out_label.get(), dlg9.out_label.get()[:60])
    dlg9.destroy()
    # snapshot: a fresh pack item gets the remembered flag
    app.auto_organize_var.set(False); si = m.GameItem(HBT); app.queue.append(si); app.update_queue_box()
    ok("organize.snapshot", si.auto_organize is False, str(getattr(si, "auto_organize", None)))
    app.queue.remove(si); app.auto_organize_var.set(True)
    # 14d) fPKG in the PFS browser: the backend routes .pkg to the tool's list-inner /
    #      extract-inner --members and answers in the browser's own JSON / progress format
    if pkg:
        r1 = subprocess.run([sys.executable, "-u", str(CLI), "--list-image", str(pkg)], capture_output=True, text=True, timeout=300)
        line = next((l for l in r1.stdout.splitlines() if l.startswith("PFSBROWSE_JSON:")), "")
        data = json.loads(line.split(":", 1)[1]) if line else {}
        paths = {e["path"] for e in data.get("entries", [])}
        ok("browser.cli.list-pkg", "sce_sys/param.json" in paths and "eboot.bin" in paths and data.get("file_count", 0) >= 3
           and any(e.get("type") == "dir" and e["path"] == "sce_sys" for e in data.get("entries", [])), f"{sorted(paths)[:6]} {r1.stderr[-120:]}")
        mdir = S / "browse_members"; mdir.mkdir(exist_ok=True); mf = mdir / "m.txt"; mf.write_text("sce_sys/param.json\neboot.bin\n")
        dest = mdir / "out"
        r2 = subprocess.run([sys.executable, "-u", str(CLI), "--extract-from", str(pkg), "--dest", str(dest), "--members-file", str(mf)],
                            capture_output=True, text=True, timeout=300)
        ok("browser.cli.extract-pkg-members", r2.returncode == 0 and (dest / "sce_sys" / "param.json").is_file() and (dest / "eboot.bin").is_file()
           and re.search(r"\[#{2,}\]\s*\d{1,3}%", r2.stdout) is not None and not (dest / "sce_sys" / "icon0.png").exists(),
           f"rc={r2.returncode} {r2.stdout[-140:]}")
        br = m.PfsBrowserDialog(app, image_path=pkg); root.update()
        pump(lambda: "files" in br.status_var.get() or br.status_var.get().startswith(("Failed", "Could not", "Bad")), timeout=90)
        ok("browser.dialog.pkg-listing", "files" in br.status_var.get() and any(p.endswith("param.json") for p in br._iid_path.values())
           and "fPKG" in br.title(), br.status_var.get())
        br.destroy()
    # 15) queue save/restore keeps fpkg fields
    app._queue_restored = True; app._save_queue()
    saved = m.load_settings().get("queue") or []
    ok("queue.persist.fpkg-fields", any(d.get("operation") == "fpkg-build" and "fpkg_level" in d and "fpkg_content_id" in d for d in saved), f"{len(saved)} saved")
    ok("settings.persist.format", m.load_settings().get("output_format") == "ffpfsc" and isinstance(m.load_settings().get("fpkg_defaults"), dict), "")

    # ── COPY job, driven through the REAL CLIWorker (1.1.8 regression) ─────────
    # 1.1.8 shipped a copy op whose worker fell through to the pack completion path
    # and raised "Backend exited but no new .ffpfsc output was created" AFTER a
    # successful move. Drive a real copy end to end and assert finish(True).
    copy_src_dir = S / "copy_src"; copy_src_dir.mkdir(exist_ok=True)
    copy_src = copy_src_dir / "CopyMe [PPSA99099] [v01.000].ffpfsc"
    _sh.copy2(FF, copy_src)
    copy_out = S / "copy_out"; copy_out.mkdir(exist_ok=True)
    ci = app._copy_item_for(copy_src, output_path=str(copy_out), delete_source=True, auto_organize=True)
    ok("copy.item.built", ci is not None and ci.operation == "copy" and ci.copy_delete_source is True,
       f"op={getattr(ci, 'operation', None)}")
    ccmd, ccwd, cout, ctmp = app.build_command(ci)
    ok("copy.build_command", "--copy" in ccmd and str(copy_src) in ccmd and "--keep-source" not in ccmd,
       " ".join(ccmd[-6:]))
    ok("copy.organized-dir", cout.name.startswith("LibProsperoPKG [PPSA99099]"), str(cout.name))
    # single-pass + space gate must not route a copy through the mkpfs estimates
    ok("copy.single-pass", m._item_is_single_pass(ci) is True, "")
    ok("copy.space-gate-passes", m._space_preflight_ok(ci, ctmp, cout) is True, "")
    # run it for real through CLIWorker and capture the finish() outcome
    fin = {}
    _real_finish = app.finish
    app.finish = lambda success, msg, cmd=None, **k: fin.update(success=success, msg=msg)
    try:
        cw = m.CLIWorker(app, ci, ccmd, ccwd, cout, ctmp)
        cw.start(); pump(lambda: "success" in fin, timeout=60.0)
    finally:
        app.finish = _real_finish
    ok("copy.worker.finishes-success", fin.get("success") is True,
       f"success={fin.get('success')} msg={fin.get('msg')!r}")
    ok("copy.worker.no-false-ffpfsc-error", "no new .ffpfsc" not in str(fin.get("msg", "")),
       str(fin.get("msg")))
    moved = list(cout.glob("*.ffpfsc"))
    ok("copy.landed-and-source-gone", len(moved) == 1 and not copy_src.exists(),
       f"moved={[p.name for p in moved]} src_exists={copy_src.exists()}")
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
