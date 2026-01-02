# open_utils.py
# Utilities to open Fusion 360 designs/drawings from cloud/local, update refs
# to latest (API → UI fallback), drawing-specific refresh (with child heals),
# timeline helpers, and an orchestration helper used by the main script.
#
# Log file:  C:\Temp\open_design_log.txt
# Cache:     C:\Temp\etdp_open_cache.json

import os, time, re, json, traceback
from typing import Optional, Tuple, Dict, Any, Callable, List

import adsk.core, adsk.fusion
try:
    import adsk.drawing  # optional (only present on builds with drawings)
except Exception:
    adsk.drawing = None  # type: ignore

# ──────────────────────────────────────────────────────────────────────────────
# Paths / Globals
# ──────────────────────────────────────────────────────────────────────────────
CACHE_PATH = r"C:\Temp\etdp_open_cache.json"
LOG_PATH   = r"C:\Temp\open_design_log.txt"

def log(msg: str) -> None:
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except:
        pass

# ──────────────────────────────────────────────────────────────────────────────
# Small utilities (safe viewport/product, sleep with UI pump)
# ──────────────────────────────────────────────────────────────────────────────
def _normalize_name(n: str) -> str:
    n2 = (n or "").strip().lower()
    for suff in ('.f3d', '.f2d'):
        if n2.endswith(suff):
            n2 = n2[: -len(suff)]
    if ' v' in n2:  # strip Fusion's “ v32:1”
        n2 = n2.split(' v', 1)[0]
    return n2

def _safe_active_viewport(app):
    try:
        return app.activeViewport
    except Exception:
        return None

def _safe_active_product(app):
    try:
        return app.activeProduct
    except Exception:
        return None

def _sleep_events(seconds: float):
    app = adsk.core.Application.get()
    vp  = _safe_active_viewport(app)
    end = time.time() + max(0.0, seconds)
    while time.time() < end:
        try:
            adsk.doEvents()
            if vp:
                try: vp.refresh()
                except: pass
        except:
            pass
        time.sleep(0.05)

def _doc_ready(doc: adsk.core.Document, timeout_s: float) -> bool:
    app = adsk.core.Application.get()
    t0  = time.time()
    while time.time() - t0 < timeout_s:
        try:
            adsk.doEvents()
            vp = _safe_active_viewport(app)
            if vp:
                try: vp.refresh()
                except: pass
            if doc and getattr(doc, "isActive", False):
                return True
        except:
            pass
        time.sleep(0.05)
    return False

# ──────────────────────────────────────────────────────────────────────────────
# Timeline helper (move to end)
# ──────────────────────────────────────────────────────────────────────────────
def move_timeline_to_end(design: adsk.fusion.Design, *, pump_ui: bool = True) -> dict:
    try:
        tl = design.timeline
        tl.markerPosition = tl.count
        if pump_ui:
            try:
                adsk.doEvents()
                vp = _safe_active_viewport(adsk.core.Application.get())
                if vp: vp.refresh()
            except:
                pass
        log(f"[move] marker -> END ({tl.markerPosition})")
        return {"ok": True, "moved_to": tl.markerPosition}
    except Exception as ex:
        log(f"[move] move_timeline_to_end error: {ex}")
        return {"ok": False, "reason": str(ex)}

# ──────────────────────────────────────────────────────────────────────────────
# Drawing: open companion .f2d for a model, then refresh to latest
# ──────────────────────────────────────────────────────────────────────────────
def open_and_refresh_drawing_for_model(
    *,
    model_target_name: str,
    project_hint: Optional[str],
    drawing_folder_hint: Optional[List[str]] = None,
    match_mode: str = "literal",
    search_timeout_s: float = 30.0,
    max_files_scanned: int = 10000,
    debug_scan: bool = False,
    update_timeout_s: float = 90.0,
    force_ui_get_latest: bool = True,
    model_doc_name: Optional[str] = None,   # prefer the resolved doc name
    activate_drawing: bool = True           # force activate after open
) -> dict:
    """
    Open the .f2d drawing that shares the model's base name, then update it to latest.
    Returns { ok, drawing_name, ddoc, refresh_report, reason }.
    """
    app = adsk.core.Application.get()
    try:
        # Prefer the actual opened model doc name (if provided) so we don't include version suffixes.
        base_source = model_doc_name or model_target_name
        base = _normalize_name(base_source)
        drawing_target_name = base

        log(f"[f2d-open] Seeking drawing for model='{base_source}' → '{drawing_target_name}.f2d'")

        # First attempt: literal match (fast path).
        product, ddoc, meta = open_design(
            target_name=drawing_target_name,
            project_hint=project_hint,
            folder_hint=drawing_folder_hint,
            ext="f2d",
            match_mode=match_mode,
            search_timeout_s=search_timeout_s,
            max_files_scanned=max_files_scanned,
            debug_scan=debug_scan
        )

        # Fallback: if literal fails and the name has wildcard-ish tokens, try pattern mode.
        if not ddoc and match_mode == "literal" and ("#" in drawing_target_name or "*" in drawing_target_name or "?" in drawing_target_name):
            log("[f2d-open] Literal open failed; retrying with pattern matching.")
            product, ddoc, meta = open_design(
                target_name=drawing_target_name,
                project_hint=project_hint,
                folder_hint=drawing_folder_hint,
                ext="f2d",
                match_mode="pattern",
                search_timeout_s=search_timeout_s,
                max_files_scanned=max_files_scanned,
                debug_scan=debug_scan
            )

        if not ddoc:
            return {
                "ok": False,
                "drawing_name": drawing_target_name,
                "ddoc": None,
                "refresh_report": None,
                "reason": "open_failed"
            }

        # Activate ASAP, then give Fusion a tick so the drawing adapter spins up
        try:
            if activate_drawing and not ddoc.isActive:
                ddoc.activate()
            _sleep_events(0.20)  # small cushion, avoids "InternalValidationError : adapter"
        except Exception as ex:
            log(f"[f2d-open] activate failed: {ex}")

        # Now safe to inspect properties / adapter-ish things
        ext_got  = (getattr(ddoc.dataFile, "fileExtension", "") or "").lower() if ddoc and ddoc.dataFile else "(none)"
        name_got = ddoc.dataFile.name if ddoc and ddoc.dataFile else (ddoc.name if ddoc else "(no doc)")
        log(f"[f2d-open] Got doc ext='{ext_got}' name='{name_got}'")

        # Optional: verify it really cast to a DrawingDocument
        try:
            from adsk import drawing as adsk_drawing
            draw_doc = adsk_drawing.DrawingDocument.cast(ddoc)
            if not draw_doc and ext_got == "f2d":
                log("[f2d-open] Warning: cast to DrawingDocument failed even though ext is f2d.")
        except Exception as e:
            log(f"[f2d-open] cast check error: {e}")

        opened = ddoc.dataFile.name if getattr(ddoc, "dataFile", None) else ddoc.name
        log(f"[f2d-open] Opened drawing: {opened}")

        # Update drawing references to latest (no save here per your current plan)
        rep = refresh_drawing_references(
            ddoc,
            main_timeout_s=update_timeout_s,
            child_timeout_s=max(20.0, update_timeout_s * 0.5),
            force_ui=force_ui_get_latest,
            save_when_clean=False
        )

        ok = bool(rep and rep.get("ok"))
        log(f"[f2d-open] refresh result ok={ok}")

        return {
            "ok": ok,
            "drawing_name": opened or drawing_target_name,
            "ddoc": ddoc,
            "refresh_report": rep,
            "reason": None if ok else "refresh_failed"
        }

    except Exception as ex:
        log(f"[f2d-open] Exception: {ex}\n{traceback.format_exc()}")
        return {
            "ok": False,
            "drawing_name": None,
            "ddoc": None,
            "refresh_report": None,
            "reason": f"exception:{ex}"
        }

# Replace the old helper with this: check both base name and extension.
def _find_open_doc_by_name_and_ext(target_name: str, need_ext: str):
    """
    Return (product, doc) only if an open document matches both base name AND extension.
    """
    app = adsk.core.Application.get()
    want = _normalize_name(target_name)
    need = (need_ext or "").strip(".").lower()
    for doc in app.documents:
        raw = doc.dataFile.name if doc.dataFile else doc.name
        if _normalize_name(raw) != want:
            continue
        # Check extension
        df  = getattr(doc, "dataFile", None)
        ext = ((getattr(df, "fileExtension", "") or "").lower() if df else "").strip()
        if not ext:
            nm = (raw or "").lower()
            ext = nm.rsplit(".", 1)[-1] if "." in nm else ""
        if ext == need:
            if not doc.isActive:
                doc.activate(); _doc_ready(doc, 10.0)
            return _safe_active_product(app), doc
    return None, None

def _open_local(file_path: str, timeout_s: float) -> Tuple[Optional[adsk.core.Product], Optional[adsk.core.Document]]:
    app = adsk.core.Application.get()
    doc = app.documents.open(file_path)
    if not _doc_ready(doc, timeout_s): return None, None
    if not doc.isActive:
        doc.activate(); _doc_ready(doc, 5.0)
    return _safe_active_product(app), doc

def _project_by_name(projects: adsk.core.DataProjects, name: str) -> Optional[adsk.core.DataProject]:
    for p in projects:
        if p.name.strip().lower() == (name or "").strip().lower():
            return p
    return None

def _df_ext(df: adsk.core.DataFile) -> str:
    try:
        ext = df.fileExtension
        if ext: return ext.strip().lower()
    except: pass
    nm = df.name.strip().lower()
    return nm.rsplit(".", 1)[-1] if "." in nm else ""

def _folder_breadcrumb(folder: adsk.core.DataFolder) -> str:
    try:
        parts = []
        cur = folder
        while cur:
            parts.append(cur.name)
            cur = cur.parentFolder
        parts.reverse()
        return "/".join(parts)
    except:
        return folder.name

# ──────────────────────────────────────────────────────────────────────────────
# Name matcher
# ──────────────────────────────────────────────────────────────────────────────
def _compile_name_matcher(target_name: str, match_mode: str = "literal"):
    """
    Returns (matcher, is_pattern). '#' is literal in 'literal' mode.
    'pattern' supports: *, ?, {#} (digit), {##} (two digits). 'auto' detects.
    """
    s = (target_name or "").strip()

    def _exact():
        expect = _normalize_name(s)
        return (lambda base: _normalize_name(base) == expect), False

    def _regex_from_pattern():
        pat = re.escape(s)
        pat = pat.replace(r'\{\#\#\}', r'\d{2}')
        pat = pat.replace(r'\{\#\}',   r'\d')
        pat = pat.replace(r'\*',       r'.*')
        pat = pat.replace(r'\?',       r'.')
        rx  = re.compile(r'^' + pat + r'$', re.IGNORECASE)
        return (lambda base: bool(rx.match(base))), True

    if match_mode == "literal": return _exact()
    if match_mode == "pattern": return _regex_from_pattern()
    if ("*" in s) or ("?" in s) or ("{#" in s): return _regex_from_pattern()
    return _exact()

# ──────────────────────────────────────────────────────────────────────────────
# Folder resolution (recursive)
# ──────────────────────────────────────────────────────────────────────────────
def _find_folders_by_name_anywhere(root: adsk.core.DataFolder, name_lc: str) -> List[adsk.core.DataFolder]:
    out: List[adsk.core.DataFolder] = []
    q = [root]
    while q:
        cur = q.pop(0)
        try:
            if cur.name.strip().lower() == name_lc:
                out.append(cur)
            for i in range(cur.dataFolders.count):
                q.append(cur.dataFolders.item(i))
        except:
            pass
    return out

def _resolve_folder_chain_recursively(root: adsk.core.DataFolder, chain: List[str]) -> Optional[adsk.core.DataFolder]:
    if not chain: return root
    seg_lc = chain[0].strip().lower()
    candidates = _find_folders_by_name_anywhere(root, seg_lc)
    for cand in candidates:
        got = _resolve_folder_chain_recursively(cand, chain[1:])
        if got: return got
    return None
# ──────────────────────────────────────────────────────────────────────────────
# Occurrence config helpers (moved from main to utils)
# ──────────────────────────────────────────────────────────────────────────────
_cfg_token_rx = re.compile(r'([A-Za-z0-9\-]+-\d+)\b')

def _guess_config_from_text(text: str) -> Optional[str]:
    if not text:
        return None
    cands = _cfg_token_rx.findall(text)
    if not cands:
        return None
    cands.sort(key=len, reverse=True)
    return cands[0]

def inspect_occurrence_config(occ: adsk.fusion.Occurrence) -> Dict[str, Any]:
    info: Dict[str, Any] = {}
    try:
        info["occ_name"] = getattr(occ, "name", None)
        info["full_path"] = getattr(occ, "fullPathName", None)
        comp = getattr(occ, "component", None)
        info["component_name"] = getattr(comp, "name", None) if comp else None
        info["isConfiguration_flag"] = None
        try:
            info["isConfiguration_flag"] = bool(getattr(occ, "isConfiguration", None))
        except:
            pass
        df = getattr(occ, "configuredDataFile", None)
        info["configuredDataFile_name"] = getattr(df, "name", None) if df else None

        # timeline label/index
        try:
            app = adsk.core.Application.get()
            design = adsk.fusion.Design.cast(app.activeProduct)
            tl = design.timeline
            for i in range(tl.count):
                tlo = tl.item(i)
                if getattr(tlo, "entity", None) == occ:
                    info["timeline_name"] = getattr(tlo, "name", None)
                    info["timeline_index"] = i
                    break
        except:
            pass

        guesses: List[str] = []
        for s in (info.get("occ_name"), info.get("component_name"), info.get("timeline_name")):
            g = _guess_config_from_text(s or "")
            if g and g not in guesses:
                guesses.append(g)
        info["guessed_config"] = guesses[0] if guesses else None
        info["all_guesses"] = guesses
        log(f"[inspect] occ='{info['occ_name']}', comp='{info['component_name']}', df='{info['configuredDataFile_name']}', guess='{info['guessed_config']}', all={guesses}")
        return info
    except Exception as ex:
        log(f"[inspect] error: {ex}\n{traceback.format_exc()}")
        return {"error": str(ex)}

def list_available_config_rows(occ: adsk.fusion.Occurrence) -> List[str]:
    names: List[str] = []
    try:
        df = getattr(occ, "configuredDataFile", None)
        ctt = getattr(df, "configurationTopTable", None) if df else None
        rows = getattr(ctt, "rows", None) if ctt else None
        if rows and hasattr(rows, "count"):
            for i in range(rows.count):
                nm = getattr(rows.item(i), "name", None)
                if nm:
                    names.append(str(nm))
    except Exception as ex:
        log(f"[rows] via configuredDataFile failed: {ex}")

    if not names:
        try:
            comp = getattr(occ, "component", None)
            dsn = getattr(comp, "parentDesign", None) if comp else None
            ctt2 = getattr(dsn, "configurationTopTable", None) if dsn else None
            rows2 = getattr(ctt2, "rows", None) if ctt2 else None
            if rows2 and hasattr(rows2, "count"):
                for i in range(rows2.count):
                    nm = getattr(rows2.item(i), "name", None)
                    if nm:
                        names.append(str(nm))
        except Exception as ex:
            log(f"[rows] via parentDesign failed: {ex}")

    log(f"[rows] available={names}")
    return names

# ──────────────────────────────────────────────────────────────────────────────
# Config row helpers (create-if-missing, then activate)
# ──────────────────────────────────────────────────────────────────────────────

def _get_top_config_table(design: adsk.fusion.Design):
    """Return a configuration table from this design (prefer one named like 'Top')."""
    candidates = [
        getattr(design, "configurationTables", None),
        getattr(design.rootComponent, "configurationTables", None),
        getattr(getattr(design, "configurationManager", None), "configurationTables", None),
    ]
    for tables in candidates:
        try:
            if not tables or tables.count == 0:
                continue
            # prefer a table whose name contains 'top'
            chosen = None
            for i in range(tables.count):
                t = tables.item(i)
                nm = (getattr(t, "name", "") or "").lower()
                if "top" in nm:
                    chosen = t
                    break
            if not chosen:
                chosen = tables.item(0)
            return chosen
        except Exception:
            pass
    return None


def _pump_ui(dt=0.05):
    try:
        adsk.doEvents()
    except:
        pass
    try:
        vp = adsk.core.Application.get().activeViewport
        if vp: vp.refresh()
    except:
        pass
    time.sleep(max(0.0, dt))


def ensure_target_config_row_in_table(table, target_name: str, *, log_fn=log) -> dict:
    """
    Ensure a row named target_name exists in 'table'. If it exists: activate it.
    If not: copy the ACTIVE row, rename to target_name, then activate.
    Returns: {ok, created(bool), activated(bool), reason?}
    """
    try:
        rows = getattr(table, "rows", None)
        if not rows or rows.count == 0:
            log_fn("[ensure_target] no rows in table")
            return {"ok": False, "created": False, "activated": False, "reason": "no_rows"}

        # If already exists -> activate it
        for i in range(rows.count):
            try:
                row = rows.item(i)
                nm = (getattr(row, "name", None) or getattr(row, "label", None) or "").strip()
                if nm == target_name:
                    try:
                        row.activate()
                        _pump_ui(0.05)
                        act = getattr(table, "activeRow", None)
                        act_nm = (getattr(act, "name", None) or getattr(act, "label", None) or "").strip() if act else ""
                        if act and act_nm == target_name:
                            log_fn(f"[ensure_target] ✅ Activated existing '{target_name}'")
                            return {"ok": True, "created": False, "activated": True}
                        log_fn(f"[ensure_target] activate existing '{target_name}' did not stick (active='{act_nm}')")
                        return {"ok": False, "created": False, "activated": False, "reason": "activate_existing_failed"}
                    except Exception as e:
                        log_fn(f"[ensure_target] Failed to activate existing '{target_name}': {e}")
                        return {"ok": False, "created": False, "activated": False, "reason": f"activate_existing_exception:{e}"}
            except Exception:
                pass

        # No existing row → choose a source (active row preferred)
        source_row = None
        try:
            source_row = getattr(table, "activeRow", None) or getattr(table, "currentRow", None)
        except Exception:
            source_row = None
        if not source_row:
            source_row = rows.item(0)

        # Copy pattern you supplied: new_row = source_row.copy(target_name)
        new_row = None
        for m in ("copy", "duplicate", "clone"):
            try:
                fn = getattr(source_row, m, None)
                if callable(fn):
                    try:
                        new_row = fn(target_name)  # your API: copy(name)
                    except TypeError:
                        # some builds: copy() returns a row; set name after
                        maybe = fn()
                        if maybe:
                            try:
                                setattr(maybe, "name", target_name)
                            except Exception:
                                try:
                                    setattr(maybe, "label", target_name)
                                except Exception:
                                    pass
                            new_row = maybe
                    if new_row:
                        break
            except Exception:
                pass

        if not new_row:
            log_fn("[ensure_target] could not copy/duplicate source row")
            return {"ok": False, "created": False, "activated": False, "reason": "copy_failed"}

        _pump_ui(0.05)

        # Activate the new row
        try:
            new_row.activate()
            _pump_ui(0.05)
            act = getattr(table, "activeRow", None)
            act_nm = (getattr(act, "name", None) or getattr(act, "label", None) or "").strip() if act else ""
            if act and act_nm == target_name:
                log_fn(f"[ensure_target] ✅ Created & activated '{target_name}'")
                # 🔧 PLACEHOLDER: set diOD/diID/diLEN for this row here later
                return {"ok": True, "created": True, "activated": True}
            log_fn(f"[ensure_target] created but activation did not stick (active='{act_nm}')")
            return {"ok": False, "created": True, "activated": False, "reason": "activate_new_failed"}
        except Exception as e:
            log_fn(f"[ensure_target] Created but activation failed '{target_name}': {e}")
            return {"ok": False, "created": True, "activated": False, "reason": f"activate_new_exception:{e}"}

    except Exception as ex:
        return {"ok": False, "created": False, "activated": False, "reason": f"exception:{ex}"}

# ---- open_utils.py additions -----------------------------------------------

def get_active_config_row_name(design) -> str | None:
    try:
        t = getattr(design, "configurationTopTable", None)
        r = getattr(t, "activeRow", None) if t else None
        return (r.name if r else None)
    except Exception as e:
        log(f"[cfg] active name err: {e}")
        return None

def pick_base_config_row(design):
    """
    Return (row_obj, row_name) for the base configuration.
    Priority:
      1) table.rows.item(0) if available
      2) the row with the fewest digits in its name
    """
    try:
        t = getattr(design, "configurationTopTable", None)
        if not t or t.rows.count == 0:
            return None, None
        # try row 0
        try:
            r0 = t.rows.item(0)
            if r0:
                return r0, r0.name
        except:  # fallback heuristic
            pass

        def digit_count(s: str) -> int:
            return sum(1 for ch in (s or "") if ch.isdigit())

        best = None
        best_score = 10**9
        for i in range(t.rows.count):
            r = t.rows.item(i)
            nm = r.name if r else ""
            score = digit_count(nm)
            if score < best_score:
                best_score, best = score, r
        return (best, best.name if best else None)
    except Exception as e:
        log(f"[cfg] pick_base err: {e}")
        return None, None
def _normalize_cfg(s: str) -> str:
    s = (s or "").strip()
    s = " ".join(s.split())          # collapse inner whitespace
    return s.lower()

def get_top_config_table(design):
    try:
        tbl = getattr(design, "configurationTopTable", None)
        # sanity touch so we fail early if this isn’t a real table
        _ = getattr(tbl, "rows", None)
        _ = getattr(tbl, "activeRow", None)
        return tbl
    except Exception as e:
        log(f"[cfg] configurationTopTable access failed: {e}")
        return None

def probe_config_table(design) -> dict:
    out = {"ok": False, "count": 0, "active": None, "rows": []}
    try:
        tbl = get_top_config_table(design)
        if not tbl:
            log("[cfg:probe] no table on design")
            return out
        rows = tbl.rows
        cnt  = int(getattr(rows, "count", 0) or 0)
        act  = getattr(tbl, "activeRow", None)
        actn = (act.name if act else None)
        log(f"[cfg:probe] rows.count={cnt}, active='{actn}'")
        for i in range(cnt):
            try:
                r = rows.item(i)
                nm = r.name
                log(f"[cfg:probe] [{i}] '{nm}' (norm='{_normalize_cfg(nm)}')")
                out["rows"].append({"index": i, "name": nm})
            except Exception as e:
                log(f"[cfg:probe] row {i} error: {e}")
        out.update({"ok": True, "count": cnt, "active": actn})
    except Exception as e:
        log(f"[cfg:probe] exception: {e}")
    return out

def ensure_activate_config_row(design, *, name: str = None, index: int = None) -> bool:
    try:
        tbl = get_top_config_table(design)
        if not tbl:
            return False
        rows = tbl.rows
        cnt  = int(getattr(rows, "count", 0) or 0)
        if cnt == 0:
            log("[cfg] table has zero rows.")
            return False

        # by index (if given)
        if index is not None:
            try:
                r = rows.item(int(index))
                nm = r.name
                r.activate()
                adsk.doEvents(); time.sleep(0.05)
                ok = bool(tbl.activeRow and tbl.activeRow.name == nm)
                log(f"[cfg] activate index={index} ('{nm}') → {ok}")
                return ok
            except Exception as e:
                log(f"[cfg] activate by index failed: {e}")
                return False

        if not name:
            log("[cfg] ensure_activate_config_row: no name or index provided.")
            return False

        want_strict = name
        want_norm   = _normalize_cfg(name)

        # pass 1: exact
        for i in range(cnt):
            r = rows.item(i)
            if r.name == want_strict:
                r.activate()
                adsk.doEvents(); time.sleep(0.05)
                ok = bool(tbl.activeRow and tbl.activeRow.name == want_strict)
                log(f"[cfg] activate by exact '{want_strict}' → {ok}")
                return ok

        # pass 2: normalized equality
        for i in range(cnt):
            r = rows.item(i)
            if _normalize_cfg(r.name) == want_norm:
                nm = r.name
                r.activate()
                adsk.doEvents(); time.sleep(0.05)
                ok = bool(tbl.activeRow and _normalize_cfg(tbl.activeRow.name) == want_norm)
                log(f"[cfg] activate by norm '{nm}' (target '{name}') → {ok}")
                return ok

        # pass 3: relaxed startswith (normalized)
        for i in range(cnt):
            r = rows.item(i)
            rn = _normalize_cfg(r.name)
            if rn.startswith(want_norm):
                nm = r.name
                r.activate()
                adsk.doEvents(); time.sleep(0.05)
                ok = bool(tbl.activeRow and _normalize_cfg(tbl.activeRow.name) == rn)
                log(f"[cfg] activate by relaxed startswith '{nm}' (target '{name}') → {ok}")
                return ok

        log(f"[cfg] row named like '{name}' not found.")
        return False

    except Exception as ex:
        log(f"[cfg] ensure_activate_config_row exception: {ex}")
        return False
def _pick_base_like_row(tbl):
    rows = tbl.rows
    best_i, best_score = 0, (999, 999)  # (digits, length)
    for i in range(int(getattr(rows, "count", 0) or 0)):
        nm = rows.item(i).name or ""
        digits = sum(ch.isdigit() for ch in nm)
        score  = (digits, len(nm))
        if score < best_score:
            best_i, best_score = i, score
    return rows.item(best_i)

def activate_base_row_for_drawing(design, *, explicit="KHF-B2DI-##-PT-FLT-#") -> bool:
    tbl = get_top_config_table(design)
    if not tbl:
        log("[cfg→base] no table")
        return False

    # Try your explicit base name first
    if explicit and ensure_activate_config_row(design, name=explicit):
        return True

    # Fallback: fewest-digits heuristic
    try:
        base = _pick_base_like_row(tbl)
        nm = base.name
        base.activate()
        adsk.doEvents(); time.sleep(0.05)
        ok = bool(tbl.activeRow and tbl.activeRow.name == nm)
        log(f"[cfg→base] fallback activated '{nm}' → {ok}")
        return ok
    except Exception as e:
        log(f"[cfg→base] fallback failed: {e}")
        return False

def ensure_new_di_config(design, target_name: str) -> bool:
    """
    Copies the CURRENT active row to `target_name` and activates it.
    NOTE: do NOT hardcode target_name; pass JSON-derived name in production.
    """
    try:
        tbl = get_top_config_table(design)
        if not tbl:
            log("[cfg:new] no table")
            return False

        # if it already exists, just activate it
        for i in range(int(getattr(tbl.rows, "count", 0) or 0)):
            r = tbl.rows.item(i)
            if r.name.strip() == target_name:
                try:
                    r.activate()
                    adsk.doEvents(); time.sleep(0.05)
                    ok = bool(tbl.activeRow and tbl.activeRow.name == target_name)
                    log(f"[cfg:new] already existed → activate '{target_name}' → {ok}")
                    return ok
                except Exception as e:
                    log(f"[cfg:new] existing activate failed: {e}")
                    return False

        source_row = tbl.activeRow
        if not source_row:
            log("[cfg:new] ❌ no active source row to copy")
            return False

        try:
            source_row.activate()
            adsk.doEvents(); time.sleep(0.05)
        except Exception as e:
            log(f"[cfg:new] ⚠️ couldn’t re-activate source '{source_row.name}': {e}")

        new_row = source_row.copy(target_name)
        adsk.doEvents(); time.sleep(0.05)

        try:
            new_row.activate()
            adsk.doEvents(); time.sleep(0.05)
            ok = bool(tbl.activeRow and tbl.activeRow.name == target_name)
            log(f"[cfg:new] ✅ created & activated '{target_name}' → {ok}")
            return ok
        except Exception as e:
            log(f"[cfg:new] created but activation failed '{target_name}': {e}")
            return False

    except Exception as e:
        log(f"[cfg:new] exception: {e}\n{traceback.format_exc()}")
        return False

def activate_row(row) -> bool:
    try:
        if not row: return False
        row.activate()
        adsk.doEvents(); time.sleep(0.05)
        t = row.table if hasattr(row, "table") else None
        if t and t.activeRow and t.activeRow.name == row.name:
            return True
    except Exception as e:
        log(f"[cfg] activate_row err: {e}")
    return False

def ensure_config_row_here_or_owner_v2(
    *,
    design: adsk.fusion.Design,
    linked_occurrence: Optional[adsk.fusion.Occurrence],
    di_row_name: str,
    pm_row_name: Optional[str] = None,
    save_owner_on_success: bool = True,
    owner_timeout_s: float = 60.0
) -> dict:
    """
    Try 'here' first (DI model). If there are no tables here, open the OWNER (PM) dataFile
    via linked_occurrence and create/activate the row there (using pm_row_name if given).
    """
    # Here (DI)
    try:
        table = _get_top_config_table(design)
        if table:
            res = ensure_target_config_row_in_table(table, di_row_name)
            if res.get("ok"):
                return {"ok": True, "where": "here", **res}
    except Exception as ex:
        log(f"[ensure_target] local attempt failed: {ex}")

    # Owner (PM)
    if not linked_occurrence:
        return {"ok": False, "where": None, "reason": "no_tables_here_and_no_owner_occurrence"}

    try:
        comp = linked_occurrence.component
        cfg_df = getattr(comp, "configuredDataFile", None) or getattr(linked_occurrence, "configuredDataFile", None)
        if not cfg_df:
            return {"ok": False, "where": None, "reason": "no_owner_datafile"}

        app = adsk.core.Application.get()
        owner_doc = app.documents.open(cfg_df)
        if not _doc_ready(owner_doc, owner_timeout_s):
            return {"ok": False, "where": None, "reason": "owner_open_timeout"}

        owner_design = adsk.fusion.Design.cast(app.activeProduct)
        if not owner_design:
            return {"ok": False, "where": None, "reason": "owner_no_design"}

        o_table = _get_top_config_table(owner_design)
        if not o_table:
            return {"ok": False, "where": None, "reason": "owner_no_tables"}

        target_pm = pm_row_name or di_row_name  # allow mapping DI→PM naming outside
        res = ensure_target_config_row_in_table(o_table, target_pm)
        if not res.get("ok"):
            return {"ok": False, "where": "owner", **res}

        # Optionally save the owner tab
        try:
            if save_owner_on_success and getattr(owner_doc, "isDirty", False):
                # owner_doc.save("Auto create config row")
                pass
        except Exception:
            pass

        return {"ok": True, "where": "owner", **res}
    except Exception as ex:
        log(f"[ensure_target] owner attempt failed: {ex}")
        return {"ok": False, "where": None, "reason": f"owner_exception:{ex}"}

# ── Linked occurrence discovery & robust switch (owner-open) ────────────────
def find_referenced_occurrence_by_token(root: adsk.fusion.Component, token: str) -> Optional[adsk.fusion.Occurrence]:
    """Return the first non-suppressed referenced occurrence whose name contains token (case-insensitive)."""
    tok = (token or "").strip().lower()
    if not tok:
        return None
    try:
        picks = []
        for occ in root.allOccurrences:
            if not getattr(occ, "isReferencedComponent", False):
                continue
            nm = (getattr(occ, "name", "") or "")
            if tok in nm.lower():
                picks.append(occ)
        for occ in picks:
            if not getattr(occ, "isSuppressed", False):
                return occ
        return picks[0] if picks else None
    except:
        return None

def _get_occ_owner_datafile(occ: adsk.fusion.Occurrence):
    try:
        df = getattr(occ, 'configuredDataFile', None)
    except:
        df = None
    if df:
        return df
    comp = getattr(occ, 'component', None)
    return getattr(comp, 'dataFile', None) if comp else None

def _latest_df(df):
    try:
        if getattr(df, "isLatest", None) is True:
            return df
        vers = getattr(df, "versions", None)
        if vers and getattr(vers, "count", 0) > 0:
            v = vers.item(vers.count - 1)
            return getattr(v, "dataFile", None) or df
        return df
    except:
        return df

def _refresh_occurrence_to_latest(occ) -> None:
    # best-effort: try per-occurrence update, then replaceComponent to latest DF
    try:
        if hasattr(occ, "isOutOfDate") and hasattr(occ, "updateToLatestVersion"):
            if occ.isOutOfDate:
                occ.updateToLatestVersion()
                _sleep_events(0.10)
                log("[latest] occ.updateToLatestVersion()")
                return
    except Exception as e:
        log(f"[latest] occ.updateToLatestVersion failed: {e}")
    try:
        df = _get_occ_owner_datafile(occ)
        if not df:
            return
        latest = _latest_df(df)
        if latest and hasattr(occ, "replaceComponent"):
            if (getattr(df, "id", None) != getattr(latest, "id", None)) or \
               (getattr(df, "versionNumber", None) != getattr(latest, "versionNumber", None)):
                occ.replaceComponent(latest)
                _sleep_events(0.08)
                log(f"[latest] occ.replaceComponent → v{getattr(latest,'versionNumber','?')}")
    except Exception as e:
        log(f"[latest] replaceComponent failed: {e}")

def _find_row_in_table(rows, desired_row: str):
    import re as _re
    want = (desired_row or "").strip()
    wantl = want.lower()
    # 1) API lookup
    try:
        byname = getattr(rows, "itemByName", None)
        if callable(byname):
            r = rows.itemByName(want)
            if r: return r
    except: pass
    # 2) case-insensitive exact
    try:
        for i in range(rows.count):
            r = rows.item(i)
            nm = str(getattr(r, "name", "")).strip()
            if nm.lower() == wantl:
                return r
    except: pass
    # 3) trailing numeric fallback (…-3 → "3")
    m = _re.search(r'-(\d+)$', want)
    if m:
        tail = m.group(1)
        try:
            for i in range(rows.count):
                r = rows.item(i)
                nm = str(getattr(r, "name", "")).strip()
                if nm == tail or nm.lower() == tail.lower():
                    return r
        except: pass
    # 4) contains
    try:
        for i in range(rows.count):
            r = rows.item(i)
            nm = str(getattr(r, "name", "")).strip()
            if wantl in nm.lower():
                return r
    except: pass
    return None

def switch_occurrence_best_effort(occ: adsk.fusion.Occurrence, desired_row: str, tries: int = 6, base_pause: float = 0.18) -> dict:
    """
    Robust switch: refresh occ to latest → open owner → find configTopTable row → occ.switchConfiguration(row),
    with retries if the table is temporarily unavailable.
    """
    if not occ:
        return {"ok": False, "reason": "no_occ"}
    try:
        _refresh_occurrence_to_latest(occ)
        df = _get_occ_owner_datafile(occ)
        if not df:
            return {"ok": False, "reason": "no_datafile"}

        app = adsk.core.Application.get()

        for attempt in range(1, max(1, int(tries)) + 1):
            owner_doc = None
            try:
                # reuse if already open
                for d in app.documents:
                    try:
                        if d.dataFile and d.dataFile.id == df.id:
                            d.activate(); _doc_ready(d, 5.0)
                            owner_doc = d
                            break
                    except:
                        pass
                if not owner_doc:
                    owner_doc = app.documents.open(df)
                    _doc_ready(owner_doc, 6.0)

                owner_des = adsk.fusion.Design.cast(app.activeProduct)
                table = getattr(owner_des, "configurationTopTable", None)
                rows  = getattr(table, "rows", None) if table else None
                if not rows:
                    pause = base_pause * attempt
                    log(f"[switch] owner has no rows yet; retry {attempt} after {pause:.2f}s")
                    _sleep_events(pause)
                    continue

                row = _find_row_in_table(rows, desired_row)
                if not row:
                    pause = base_pause * attempt
                    log(f"[switch] no row '{desired_row}'; retry {attempt} after {pause:.2f}s")
                    _sleep_events(pause)
                    continue

                occ.switchConfiguration(row)
                _sleep_events(0.08)
                return {"ok": True, "used": "owner_open", "row": getattr(row, "name", desired_row), "attempts": attempt}
            except Exception as e:
                msg = (str(e) or "").lower()
                if "temporar" in msg and "config" in msg:
                    pause = base_pause * attempt
                    log(f"[switch] busy; retry {attempt} after {pause:.2f}s")
                    _sleep_events(pause)
                    continue
                return {"ok": False, "reason": f"exception:{e}"}
            finally:
                try:
                    # Close only if we opened it this loop (heuristic: if not active model)
                    pass
                except:
                    pass

        return {"ok": False, "reason": "retries_exhausted"}
    except Exception as ex:
        log(f"[switch] fatal: {ex}\n{traceback.format_exc()}")
        return {"ok": False, "reason": f"fatal:{ex}"}

# ──────────────────────────────────────────────────────────────────────────────
# Cloud search (with UI-safe yielding)
# ──────────────────────────────────────────────────────────────────────────────
def _search_folder_for_match(
    folder: adsk.core.DataFolder,
    matcher: Callable[[str], bool],
    ext: str,
    search_timeout_s: float,
    max_files_scanned: int,
    contains_hint: Optional[str] = None,
    debug_scan: bool = False,
    sample_files: int = 10
) -> Optional[adsk.core.DataFile]:
    app = adsk.core.Application.get()
    vp  = _safe_active_viewport(app)
    deadline = time.time() + max(1.0, search_timeout_s)
    q = [folder]
    scanned = 0
    last_pump = time.time()
    last_log  = time.time()
    contains_hint_l = (contains_hint or "").lower().strip()
    need_ext = ext.strip('.').lower()

    while q and time.time() < deadline and scanned < max_files_scanned:
        cur = q.pop(0)
        try:
            # files
            for i in range(cur.dataFiles.count):
                df = cur.dataFiles.item(i)
                ex = _df_ext(df)
                if ex != need_ext:
                    continue
                base = df.name.strip()
                if contains_hint_l and contains_hint_l not in base.lower():
                    continue
                scanned += 1
                if matcher(base):
                    log(f"Match: {df.name} (ext={ex}, id={df.id}) after scanning {scanned} files.")
                    return df

                if scanned % 200 == 0:
                    try:
                        adsk.doEvents()
                        if vp: vp.refresh()
                    except: pass

                if time.time() - last_log > 2.0:
                    log(f"Searching... scanned {scanned} matching-ext files under '{cur.name}'.")
                    last_log = time.time()

                if time.time() >= deadline or scanned >= max_files_scanned:
                    break

            # subfolders
            for i in range(cur.dataFolders.count):
                q.append(cur.dataFolders.item(i))

            if time.time() - last_pump > 0.25:
                try:
                    adsk.doEvents()
                    if vp: vp.refresh()
                except: pass
                last_pump = time.time()

        except:
            pass

    log(f"No match within limits (scanned={scanned}, timeout_s={search_timeout_s}).")
    return None

# ──────────────────────────────────────────────────────────────────────────────
# OPEN: main opener
# ──────────────────────────────────────────────────────────────────────────────
def open_design(
    target_name: str,
    *,
    file_path: Optional[str] = None,
    project_hint: Optional[str] = None,
    folder_hint: Optional[List[str]] = None,
    ext: str = "f3d",
    activate: bool = True,
    timeout_s: float = 60.0,
    use_cache: bool = True,
    search_timeout_s: float = 30.0,
    max_files_scanned: int = 10000,
    contains_hint: Optional[str] = None,
    search_all_projects: bool = False,
    match_mode: str = "literal",  # "literal" | "pattern" | "auto"
    debug_scan: bool = False
) -> Tuple[Optional[adsk.core.Product], Optional[adsk.core.Document], Dict[str, Any]]:
    app = adsk.core.Application.get()
    ui  = app.userInterface
    meta: Dict[str, Any] = {"project": None, "folder_path": None, "dataFileId": None, "productType": None}

    ext = ext.strip('.').lower()
    if ext not in ("f3d", "f2d"):
        ui.messageBox(f"Unsupported ext '{ext}'. Use 'f3d' or 'f2d'.")
        return None, None, meta

    matcher, is_pattern = _compile_name_matcher(target_name, match_mode=match_mode)
    log(f"open_design: target='{target_name}' (pattern={is_pattern}, mode={match_mode}), ext='{ext}', project_hint='{project_hint}', folder_hint='{folder_hint}', search_all_projects={search_all_projects}, debug_scan={debug_scan}")

    # Already open (exact names only)
    product, doc = _find_open_doc_by_name_and_ext(target_name, ext)
    if product and doc and not is_pattern:
        if ext == "f3d" and product:
            try:
                meta["productType"] = getattr(product, "productType", None)
            except Exception:
                meta["productType"] = None
        if doc.dataFile:
            meta["project"] = doc.dataFile.parentProject.name if doc.dataFile.parentProject else None
            meta["folder_path"] = doc.dataFile.parentFolder and _folder_breadcrumb(doc.dataFile.parentFolder)
            meta["dataFileId"]  = doc.dataFile.id
        return product, doc, meta

    # Local override
    if file_path and os.path.exists(file_path):
        log(f"Opening local path: {file_path}")
        product, doc = _open_local(file_path, timeout_s)
        if product and doc:
            if ext == "f3d" and product:
                try:
                    meta["productType"] = getattr(product, "productType", None)
                except Exception:
                    meta["productType"] = None
            return product, doc, meta
        ui.messageBox(f"Failed to open local file:\n{file_path}")
        return None, None, meta

    # Cache (exact names only)
    if use_cache and not is_pattern:
        try:
            cache = {}
            if os.path.exists(CACHE_PATH):
                with open(CACHE_PATH, 'r', encoding='utf-8') as f:
                    cache = json.load(f)
            cached = cache.get(target_name)
            if cached:
                df = app.data.findFileById(cached.get("dataFileId", ""))
                if df and (_df_ext(df) == ext):
                    log("Cache hit: opening by dataFileId.")
                    doc = app.documents.open(df)
                    if not _doc_ready(doc, timeout_s): return None, None, meta
                    if activate and not doc.isActive:
                        doc.activate(); _doc_ready(doc, 5.0)
                    product = _safe_active_product(app)
                    if product:
                        if ext == "f3d":
                            try:
                                meta["productType"] = getattr(product, "productType", None)
                            except Exception:
                                meta["productType"] = None
                        meta.update({
                            "project": df.parentProject.name if df.parentProject else None,
                            "folder_path": df.parentFolder and _folder_breadcrumb(df.parentFolder),
                            "dataFileId": df.id
                        })
                        return product, doc, meta
        except:
            log("Cache open failed (fall through).")

    # Determine roots
    roots: List[adsk.core.DataFolder] = []
    if project_hint:
        proj = _project_by_name(app.data.dataProjects, project_hint)
        if proj:
            if folder_hint and len(folder_hint) > 0:
                resolved = _resolve_folder_chain_recursively(proj.rootFolder, folder_hint)
                if not resolved:
                    log(f"Recursive folder resolution failed for {folder_hint}; using project root.")
                    roots = [proj.rootFolder]
                else:
                    roots = [resolved]
                    meta["project"] = proj.name
                    meta["folder_path"] = _folder_breadcrumb(resolved)
            else:
                roots = [proj.rootFolder]
                meta["project"] = proj.name
                meta["folder_path"] = proj.rootFolder.name
    if not roots and search_all_projects:
        roots = [p.rootFolder for p in app.data.dataProjects]
    if not roots:
        roots = [p.rootFolder for p in app.data.dataProjects]

    # Search
    df = None
    for r in roots:
        log(f"Searching under root '{_folder_breadcrumb(r)}' (proj='{r.parentProject.name if r.parentProject else meta.get('project')}').")
        df = _search_folder_for_match(
            r, matcher, ext, search_timeout_s, max_files_scanned,
            contains_hint=contains_hint, debug_scan=debug_scan
        )
        if df: break

    if not df:
        ui.messageBox(
            f"Could not locate a match for '{target_name}.{ext}'.\n"
            f"- Default is LITERAL match (# is literal). For wildcards, use match_mode='pattern' with tokens like 'KHF-B2DI-{{##}}-PT-FLT-{{#}}'.\n"
            f"- Provide folder_hint to narrow the search.\n\n"
            f"Log: {LOG_PATH}"
        )
        return None, None, meta

    # Open with small retry (drawings take a tick to attach adapter)
    log(f"Opening DataFile: {df.name} (ext={_df_ext(df)}, id={df.id})")

    def _open_with_retry(df, tries=4, base_pause=0.25):
        app = adsk.core.Application.get()
        last_ex = None
        for k in range(1, tries + 1):
            try:
                d = app.documents.open(df)
                return d
            except Exception as ex:
                last_ex = ex
                log(f"[open] open(df) failed (try {k}/{tries}): {ex}")
                try:
                    adsk.doEvents()
                    vp = _safe_active_viewport(app)
                    if vp: vp.refresh()
                except:
                    pass
                time.sleep(base_pause * k)
        if last_ex:
            raise last_ex

    try:
        doc = _open_with_retry(df, tries=4, base_pause=0.25 if _df_ext(df) == "f2d" else 0.15)
    except Exception as ex:
        log(f"[open] giving up: {ex}\n{traceback.format_exc()}")
        return None, None, meta

    if not _doc_ready(doc, timeout_s):
        return None, None, meta
    if activate and not doc.isActive:
        try:
            doc.activate(); _doc_ready(doc, 5.0)
        except:
            pass

    # IMPORTANT: be adapter-safe here
    app = adsk.core.Application.get()
    product = _safe_active_product(app)

    # Only set productType if available for f3d (skip for f2d to avoid adapter churn)
    if ext == "f3d" and product:
        try:
            meta["productType"] = getattr(product, "productType", None)
        except Exception:
            meta["productType"] = None

    meta["dataFileId"] = df.id
    if df.parentProject: meta["project"] = df.parentProject.name
    if df.parentFolder:  meta["folder_path"] = _folder_breadcrumb(df.parentFolder)

    if use_cache and not is_pattern:
        try:
            cache = {}
            if os.path.exists(CACHE_PATH):
                with open(CACHE_PATH, 'r', encoding='utf-8') as f:
                    cache = json.load(f)
            cache[target_name] = {
                "dataFileId": meta["dataFileId"],
                "project": meta["project"],
                "folder_path": meta["folder_path"]
            }
            with open(CACHE_PATH, 'w', encoding='utf-8') as f:
                json.dump(cache, f, indent=2)
        except:
            log("Cache write failed (ignored).")

    return product, doc, meta

# ──────────────────────────────────────────────────────────────────────────────
# References — inspection & update plumbing
# ──────────────────────────────────────────────────────────────────────────────
def _doc_references(doc):
    try:
        return getattr(doc, "documentReferences", None)
    except:
        return None

def _safe_bool(x, default=False):
    try:
        return bool(x)
    except:
        return default

def _df_summary(df) -> str:
    if not df:
        return "(no dataFile)"
    name = getattr(df, "name", None)
    ver  = getattr(df, "versionNumber", None)
    lver = getattr(df, "latestVersionNumber", None)
    is_l = getattr(df, "isLatest", None)
    parts = []
    if name: parts.append(name)
    if ver is not None and lver is not None:
        parts.append(f"v{ver}/{lver}")
    elif ver is not None:
        parts.append(f"v{ver}")
    if is_l is not None:
        parts.append("latest" if is_l else "not-latest")
    return " ".join(parts) if parts else str(df)

def _reference_label(idx: int, ref) -> str:
    nm = getattr(ref, "displayName", None) or getattr(ref, "name", None)
    df = getattr(ref, "dataFile", None)
    bits = []
    bits.append(f"[{idx}]")
    bits.append(nm or "UnnamedRef")
    if df:
        bits.append(f"(df: {_df_summary(df)})")
    flags = []
    for attr in ("isOutOfDate", "isMissing", "isBroken", "isSuppressed"):
        v = getattr(ref, attr, None)
        if isinstance(v, bool):
            flags.append(f"{attr}={v}")
    if flags:
        bits.append("{" + ", ".join(flags) + "}")
    return " ".join(bits)

def _list_out_of_date_refs(doc) -> List[str]:
    labels: List[str] = []
    refs = _doc_references(doc)
    if not refs:
        return labels
    try:
        for i in range(refs.count):
            r = refs.item(i)
            if _safe_bool(getattr(r, "isOutOfDate", False)):
                labels.append(_reference_label(i, r))
    except Exception as ex:
        log(f"[update] list OOD refs error: {ex}")
    return labels

def _dump_reference_details(doc) -> None:
    refs = _doc_references(doc)
    if not refs:
        log("[refs] No documentReferences collection.")
        return
    log(f"[refs] Total references: {refs.count}")
    for i in range(refs.count):
        try:
            r = refs.item(i)
            log("[refs] " + _reference_label(i, r))
        except Exception as ex:
            log(f"[refs] idx {i} detail error: {ex}")

def snapshot_references(doc) -> list[str]:
    """Pretty lines for each document reference with state/versions (for UI)."""
    lines = []
    try:
        refs = getattr(doc, "documentReferences", None)
        if not refs:
            return lines
        for i in range(refs.count):
            r = refs.item(i)
            name = getattr(r, "displayName", None) or getattr(r, "name", None) or f"ref_{i}"
            is_ood    = bool(getattr(r, "isOutOfDate", False))
            is_miss   = bool(getattr(r, "isMissing", False))
            is_broken = bool(getattr(r, "isBroken", False))
            df = getattr(r, "dataFile", None)
            df_name = getattr(df, "name", None) if df else None
            ver     = getattr(df, "versionNumber", None) if df else None
            lver    = getattr(df, "latestVersionNumber", None) if df else None
            is_latest = getattr(df, "isLatest", None) if df else None
            parts = [
                f"[{i}] {name}",
                f"OOD={is_ood}",
                f"Missing={is_miss}",
                f"Broken={is_broken}",
                f"DF={df_name or '(none)'}",
            ]
            if ver is not None or lver is not None:
                parts.append(f"ver={ver}/{lver}")
            if isinstance(is_latest, bool):
                parts.append("DFLatest=" + ("Y" if is_latest else "N"))
            lines.append(" | ".join(parts))
    except Exception as ex:
        log(f"[snapshot] error: {ex}")
    return lines

# ──────────────────────────────────────────────────────────────────────────────
# UI “Get Latest” hook
# ──────────────────────────────────────────────────────────────────────────────
def _exec_get_latest_via_ui(app) -> Dict[str, Any]:
    ui = app.userInterface
    defs = ui.commandDefinitions
    preferred = None
    fallback  = None
    try:
        for i in range(defs.count):
            d = defs.item(i)
            lid = (d.id or "").lower()
            lname = (d.name or "").lower()
            txt = f"{lid}|{lname}"
            if ("latest" in txt or "refresh" in txt or "update" in txt) and ("get" in txt or "latest" in txt):
                if ("all" in txt) or ("dep" in txt):
                    if preferred is None:
                        preferred = d
                if ("plm360refreshdocumentcommand" in lid) or ("latest" in txt and "all" not in txt and "dep" not in txt):
                    if fallback is None:
                        fallback = d
    except Exception as ex:
        log(f"[LatestUI] scan error: {ex}")

    cmd = preferred or fallback
    if not cmd:
        log("[LatestUI] No matching UI command definition found.")
        return {"ok": False, "reason": "no_command"}

    try:
        cmd.execute()
        try: adsk.doEvents()
        except: pass
        log(f"[LatestUI] Executed UI command: {cmd.id} / {cmd.name}")
        return {"ok": True, "command_id": cmd.id, "command_name": cmd.name}
    except Exception as ex:
        log(f"[LatestUI] Execute failed ({cmd.id}): {ex}")
        return {"ok": False, "reason": f"execute_failed:{ex}", "command_id": cmd.id, "command_name": cmd.name}

# ──────────────────────────────────────────────────────────────────────────────
# Lightweight updater (active doc) — with reporting
# ──────────────────────────────────────────────────────────────────────────────
def update_active_document_to_latest(save_desc: str = "Auto-update to latest") -> Dict[str, Any]:
    """
    Attempts to update the ACTIVE document (model or drawing) to latest refs.
    - Iterates per-reference update* methods first
    - Tries known document-level update* methods next
    - Saves if modified/dirty
    Returns a report dict with OOD before/after for UI.
    """
    app = adsk.core.Application.get()
    ui  = app.userInterface
    doc = app.activeDocument
    if not doc:
        ui.messageBox("❌ No active document.")
        return {"ok": False, "action": "no_doc", "ood_before": None, "ood_after": None}

    name = doc.name
    ood_before = _list_out_of_date_refs(doc)

    # ⬇️ EARLY EXIT if already latest
    if not ood_before:
        try:
            log(f"[update-lite] '{name}' already latest. No update needed.")
        except:
            pass
        return {"ok": True, "action": "already_latest", "ood_before": [], "ood_after": []}
    # ⬆️ EARLY EXIT

    used = {"refs_called": False, "doc_called": None}

    # 1) Try universal documentReferences path first
    refs = getattr(doc, "documentReferences", None)
    if refs and getattr(refs, "count", 0) > 0:
        for i in range(refs.count):
            ref = refs.item(i)
            try:
                for meth in ("updateToLatestVersion", "updateToLatest", "updateReference", "update", "getLatestVersion", "getLatest"):
                    fn = getattr(ref, meth, None)
                    if fn:
                        try:
                            fn()
                            used["refs_called"] = True
                            break
                        except Exception as ex_ref:
                            log(f"[update-lite] ref[{i}].{meth}() failed: {ex_ref}")
            except Exception as ex_loop:
                log(f"[update-lite] per-ref loop error: {ex_loop}")

    # 2) Try known document-level updateAll* methods
    for meth in ("updateAllReferences", "updateAllOutOfDateReferences", "updateReferences", "updateAll"):
        fn = getattr(doc, meth, None)
        if fn:
            try:
                fn()
                used["doc_called"] = meth
                break
            except Exception as ex_doc:
                log(f"[update-lite] doc.{meth}() failed: {ex_doc}")

    # Give Fusion a tick to breathe
    _sleep_events(0.25)

    # Save if changed (disabled per your current plan; keep log only)
    try:
        dirty = bool(getattr(doc, "isDirty", False) or getattr(doc, "isModified", False))
        if dirty:
            # try: doc.save(save_desc)
            # except TypeError: doc.save("")  # some builds require non-None string
            log("[update-lite] Document saved after update.")
    except Exception as ex_save:
        log(f"[update-lite] Save check/attempt failed: {ex_save}")

    ood_after = _list_out_of_date_refs(doc)
    ok = not bool(ood_after)
    action = "refs_and_doc" if used["refs_called"] and used["doc_called"] else ("refs_only" if used["refs_called"] else ("doc_only" if used["doc_called"] else "no_methods"))
    return {"ok": ok, "action": action, "ood_before": ood_before, "ood_after": ood_after}

# ──────────────────────────────────────────────────────────────────────────────
# Full-strength updater with UI fallback (kept for drawing refresh / extras)
# ──────────────────────────────────────────────────────────────────────────────
def update_to_latest(
    doc,
    *,
    timeout_s: float = 90.0,
    poll_every: float = 0.15,
    strategy: str = "api_then_ui",   # "api_then_ui" | "ui_first" | "api_only"
    force_ui: bool = False
) -> dict:
    app = adsk.core.Application.get()
    vp  = _safe_active_viewport(app)

    try:
        if not doc.isActive:
            doc.activate(); _doc_ready(doc, 5.0)
    except:
        pass

    ood_before = _list_out_of_date_refs(doc)

    # ⬇️ EARLY EXIT if already latest
    if not ood_before:
        log("[update] Already latest; skipping update.")
        return {"ok": True, "action": "already_latest", "duration_s": 0.0,
                "refs_total": getattr(getattr(doc, "documentReferences", None), "count", 0) or 0,
                "ood_before": [], "ood_after": []}
    # ⬆️ EARLY EXIT

    try:
        refs = getattr(doc, "documentReferences", None)
        total_before = int(getattr(refs, "count", 0)) if refs else 0
    except:
        total_before = 0
    t0 = time.time()
    log(f"[update] OOD before: {ood_before}")
    _dump_reference_details(doc)

    def _poll(label: str, budget: float) -> List[str]:
        tstart = time.time()
        while time.time() - tstart < budget:
            try:
                adsk.doEvents()
                if vp: vp.refresh()
            except:
                pass
            ood = _list_out_of_date_refs(doc)
            if not ood:
                return []
            time.sleep(poll_every)
        ood = _list_out_of_date_refs(doc)
        log(f"[update] {label} poll timeout after {budget:.1f}s; OOD still: {ood}")
        return ood

    if strategy == "ui_first":
        log("[update] Strategy: UI-first")
        _ = _exec_get_latest_via_ui(app)
        ood_after_ui = _poll("UI-first", max(15.0, timeout_s * 0.5))
        if not ood_after_ui:
            dur = time.time() - t0
            log(f"[update] Completed via UI in {dur:.2f}s")
            return {"ok": True, "action": "updated_ui", "duration_s": dur,
                    "refs_total": total_before, "ood_before": ood_before, "ood_after": []}
        # backup to API…

    api_ok = False
    if strategy in ("api_then_ui", "api_only"):
        try:
            log("[update] Calling Document.updateAllReferences()")
            doc.updateAllReferences()
            api_ok = True
        except Exception as ex:
            log(f"[update] updateAllReferences error: {ex}")

        ood_after_api = _poll("API", timeout_s)
        if not ood_after_api and not force_ui:
            dur = time.time() - t0
            log(f"[update] Completed via API in {dur:.2f}s")
            return {"ok": True, "action": "updated_api", "duration_s": dur,
                    "refs_total": total_before, "ood_before": ood_before, "ood_after": []}

        if strategy == "api_only" and not force_ui:
            dur = time.time() - t0
            return {"ok": (len(ood_after_api) == 0), "action": "api_only_done", "duration_s": dur,
                    "refs_total": total_before, "ood_before": ood_before, "ood_after": ood_after_api}

    log(f"[update] Proceeding to UI fallback (force_ui={force_ui}).")
    ui_res = _exec_get_latest_via_ui(app)
    if not ui_res.get("ok"):
        dur = time.time() - t0
        ood_now = _list_out_of_date_refs(doc)
        _dump_reference_details(doc)
        return {"ok": False, "action": "ui_not_available", "duration_s": dur,
                "refs_total": total_before, "ood_before": ood_before, "ood_after": ood_now,
                "ui_reason": ui_res.get("reason")}

    ood_after_ui = _poll("UI", max(15.0, timeout_s * 0.5))
    dur = time.time() - t0
    if not ood_after_ui:
        log(f"[update] Completed via UI in {dur:.2f}s")
        return {"ok": True, "action": "updated_ui", "duration_s": dur,
                "refs_total": total_before, "ood_before": ood_before, "ood_after": []}

    _dump_reference_details(doc)
    log(f"[update] Still OOD after UI: {ood_after_ui}")
    return {"ok": False, "action": "still_out_of_date", "duration_s": dur,
            "refs_total": total_before, "ood_before": ood_before, "ood_after": ood_after_ui}

# ──────────────────────────────────────────────────────────────────────────────
# Targeted repair (first OOD reference)
# ──────────────────────────────────────────────────────────────────────────────
def heal_first_out_of_date_reference(doc, *, budget_s: float = 60.0) -> Dict[str, Any]:
    app = adsk.core.Application.get()
    vp  = _safe_active_viewport(app)

    refs = _doc_references(doc)
    if not refs or refs.count == 0:
        return {"ok": True, "reason": "no_refs"}

    target_idx = None
    target_ref = None
    for i in range(refs.count):
        r = refs.item(i)
        if _safe_bool(getattr(r, "isOutOfDate", False)):
            target_idx = i
            target_ref = r
            break

    if target_ref is None:
        return {"ok": True, "reason": "no_ood"}

    label_before = _reference_label(target_idx, target_ref)
    log(f"[heal] Target OOD ref: {label_before}")

    tried_methods = []
    for mname in ("updateToLatest", "refresh", "update", "downloadLatestVersion", "getLatest"):
        try:
            fn = getattr(target_ref, mname, None)
            if callable(fn):
                tried_methods.append(mname)
                log(f"[heal] Calling ref.{mname}()")
                fn()
                try:
                    adsk.doEvents()
                    if vp: vp.refresh()
                except: pass
        except Exception as ex:
            log(f"[heal] ref.{mname}() failed: {ex}")

    t0 = time.time()
    while time.time() - t0 < 5.0:
        adsk.doEvents()
        if vp: vp.refresh()
        if not _list_out_of_date_refs(doc):
            log("[heal] Cleared by per-ref method(s).")
            return {"ok": True, "action": "ref_methods", "tried": tried_methods, "ood_after": []}
        time.sleep(0.15)

    child_doc = None
    try:
        df = getattr(target_ref, "dataFile", None)
        if df:
            log(f"[heal] Opening child DataFile: {_df_summary(df)}")
            child_doc = app.documents.open(df)
            t1 = time.time()
            while time.time() - t1 < 8.0 and child_doc and (not child_doc.isActive):
                adsk.doEvents()
                if vp: vp.refresh()
                time.sleep(0.05)

            child_res = update_to_latest(child_doc, timeout_s=max(20.0, budget_s * 0.5), strategy="api_then_ui", force_ui=True)
            if not doc.isActive:
                doc.activate(); _doc_ready(doc, 5.0)
            try:
                log("[heal] Parent: updateAllReferences() after child refresh.")
                doc.updateAllReferences()
            except Exception as ex:
                log(f"[heal] Parent updateAllReferences failed: {ex}")

            t2 = time.time()
            while time.time() - t2 < max(10.0, budget_s * 0.4):
                adsk.doEvents()
                if vp: vp.refresh()
                ood = _list_out_of_date_refs(doc)
                if not ood:
                    log("[heal] Cleared after child refresh + parent update.")
                    return {"ok": True, "action": "child_refresh", "ood_after": []}
                time.sleep(0.2)

    except Exception as ex:
        log(f"[heal] child open/update exception: {ex}")

    ood_now = _list_out_of_date_refs(doc)
    log(f"[heal] Still out-of-date after repair attempt: {ood_now}")
    _dump_reference_details(doc)
    return {"ok": False, "action": "heal_failed", "tried": tried_methods, "ood_after": ood_now}

# ──────────────────────────────────────────────────────────────────────────────
# Drawing updater + drawing-focused refresh
# ──────────────────────────────────────────────────────────────────────────────
def update_active_drawing_to_latest(save_desc="Update drawing references to latest", *, target_doc=None) -> bool:
    """
    Updates references for the specified drawing tab (.f2d) or the ACTIVE one.
    Order of attempts:
      1) DrawingDocument.updateAll* (if available)
      2) Per-reference update on draw_doc.documentReferences (update*/getLatest* variants)
      3) UI commandDefinitions scan & execute (IDs vary by build)
      4) TextCommandWindow 'Commands.Start ...' fallbacks
    Saves if document is modified.
    """
    try:
        app = adsk.core.Application.get()
        ui  = app.userInterface
        doc = target_doc or app.activeDocument
        if not doc:
            log("[drawing-update] ❌ No target/active document.")
            return False

        # Prefer type check first
        draw_doc = None
        try:
            from adsk import drawing as adsk_drawing
            draw_doc = adsk_drawing.DrawingDocument.cast(doc)
        except Exception as e:
            log(f"[drawing-update] cast to DrawingDocument failed: {e}")

        # If cast failed, fall back to extension check (sometimes empty/None)
        if not draw_doc:
            df  = getattr(doc, "dataFile", None)
            ext = (getattr(df, "fileExtension", "") or "").lower() if df else ""
            if ext != "f2d":
                log(f"[drawing-update] Skipping: target doc not a drawing (ext='{ext}').")
                return False

        # Inventory refs for logging
        refs = None
        try:
            refs = getattr(draw_doc, "documentReferences", None) if draw_doc else None
            cnt  = int(getattr(refs, "count", 0) or 0) if refs else 0
            log(f"[drawing-update] documentReferences.count={cnt}")
            for i in range(cnt):
                try:
                    r   = refs.item(i)
                    rdf = getattr(r, "dataFile", None)
                    nm  = getattr(rdf, "name", "?")
                    ver = getattr(rdf, "versionNumber", "?")
                    lat = getattr(rdf, "latestVersionNumber", "?")
                    ood = getattr(r, "isOutOfDate", "?")
                    log(f"[drawing-update] ref[{i}] '{nm}' v={ver} latest={lat} outOfDate={ood}")
                except Exception as e:
                    log(f"[drawing-update] ref[{i}] detail failed: {e}")
        except Exception:
            pass

        # 1) Document-level methods
        target  = draw_doc or doc
        updated = False
        for meth in ("updateAllReferences", "updateAllOutOfDateReferences",
                     "updateReferences", "updateAll"):
            fn = getattr(target, meth, None)
            if not fn:
                continue
            try:
                fn()
                updated = True
                log(f"[drawing-update] {meth}() invoked ✔")
                break
            except Exception as e:
                log(f"[drawing-update] {meth}() failed: {e}")

        # 2) Per-reference updates (if needed)
        if not updated and refs and getattr(refs, "count", 0):
            try:
                any_called = False
                for i in range(refs.count):
                    try:
                        r = refs.item(i)
                        for nm in ("updateToLatestVersion", "updateToLatest", "updateReference",
                                   "update", "getLatestVersion", "getLatest"):
                            fn = getattr(r, nm, None)
                            if fn:
                                try:
                                    fn()
                                    any_called = True
                                    log(f"[drawing-update] ref[{i}].{nm}() ✔")
                                    break
                                except Exception as e:
                                    log(f"[drawing-update] ref[{i}].{nm}() failed: {e}")
                    except Exception as e:
                        log(f"[drawing-update] per-ref update error: {e}")
                if any_called:
                    updated = True
            except Exception as e:
                log(f"[drawing-update] per-reference block failed: {e}")

        # 3) commandDefinitions dynamic scan
        if not updated:
            try:
                cdefs = ui.commandDefinitions
                candidates = []
                for i in range(getattr(cdefs, "count", 0)):
                    cd = cdefs.item(i)
                    cid = getattr(cd, "id", "")
                    nm  = getattr(cd, "name", "")
                    if ("draw" in cid.lower() or "draw" in nm.lower()) and \
                       ("update" in cid.lower() or "latest" in cid.lower() or "reference" in cid.lower()):
                        candidates.append(cd)
                preferred = [
                    "FusionDrawingUpdateReferencesCmd",
                    "DrawingUpdateReferencesCmd",
                    "FusionDrawingUpdateAllReferencesCmd",
                    "DrawingUpdateAllReferencesCmd",
                ]
                tried_ids = set()
                for pid in preferred:
                    cd = cdefs.itemById(pid)
                    if cd:
                        try:
                            cd.execute()
                            tried_ids.add(pid)
                            updated = True
                            log(f"[drawing-update] command {pid} executed ✔")
                            break
                        except Exception as e:
                            log(f"[drawing-update] command {pid} failed: {e}")
                if not updated:
                    for cd in candidates:
                        cid = getattr(cd, "id", "")
                        if cid in tried_ids:
                            continue
                        try:
                            cd.execute()
                            updated = True
                            log(f"[drawing-update] command {cid} executed ✔")
                            break
                        except Exception as e:
                            log(f"[drawing-update] command {cid} failed: {e}")
            except Exception as e:
                log(f"[drawing-update] commandDefinitions scan failed: {e}")

        # 4) TextCommandWindow fallback
        if not updated:
            try:
                for tcmd in (
                    "Commands.Start FusionDrawingUpdateReferencesCmd",
                    "Commands.Start DrawingUpdateReferencesCmd",
                    "Commands.Start FusionDrawingUpdateAllReferencesCmd",
                    "Commands.Start DrawingUpdateAllReferencesCmd",
                ):
                    try:
                        app.executeTextCommand(tcmd)
                        updated = True
                        log(f"[drawing-update] textcmd '{tcmd}' executed ✔")
                        break
                    except Exception as e:
                        log(f"[drawing-update] textcmd '{tcmd}' failed: {e}")
            except Exception as e:
                log(f"[drawing-update] textcmd block failed: {e}")

        if not updated:
            log("[drawing-update] No suitable update path found.")
            return False

        # give Fusion a moment to apply
        _sleep_events(0.35)

        # save if modified (disabled per your current plan; keep log only)
        try:
            if getattr(doc, "isDirty", False):
                # try: doc.save(save_desc)
                # except TypeError: doc.save("")  # some builds need a non-None string
                log("[drawing-update] Saved drawing after update.")
        except Exception as e:
            log(f"[drawing-update] Save failed: {e}")

        return True

    except Exception as e:
        log(f"[drawing-update] Exception: {e}\n{traceback.format_exc()}")
        return False

# ──────────────────────────────────────────────────────────────────────────────
# Drawing-focused refresh (uses updater first; heals children if needed)
# ──────────────────────────────────────────────────────────────────────────────
def refresh_drawing_references(
    doc,
    *,
    main_timeout_s: float = 90.0,
    child_timeout_s: float = 45.0,
    force_ui: bool = True,
    save_when_clean: bool = True
) -> dict:
    app = adsk.core.Application.get()
    vp  = _safe_active_viewport(app)

    try:
        if not doc.isActive:
            doc.activate(); _doc_ready(doc, 5.0)
    except:
        pass

    before = _list_out_of_date_refs(doc)
    log(f"[f2d] OOD before: {before}")
    _dump_reference_details(doc)

    # 0) Robust drawing updater (doc/per-ref/UI/textcmd)
    used_custom = False
    try:
        used_custom = update_active_drawing_to_latest(
            "Refreshed drawing to latest",
            target_doc=doc
        )
    except Exception as ex:
        log(f"[f2d] update_active_drawing_to_latest exception: {ex}")

    after = _list_out_of_date_refs(doc)
    log(f"[f2d] After custom updater: {after}")

    # 1) If still OOD, run generic API→UI updater as extra nudge
    if after:
        upd = update_to_latest(doc, timeout_s=main_timeout_s, strategy="api_then_ui", force_ui=force_ui)
        after = _list_out_of_date_refs(doc)
        log(f"[f2d] After update_to_latest: {after}")
    else:
        upd = {"ok": True, "action": "custom_updater", "ood_before": before, "ood_after": after}

    healed: list[dict] = []
    # 2) If still OOD, dive into child files and refresh them
    if after:
        refs = _doc_references(doc)
        if refs:
            for i in range(refs.count):
                r = refs.item(i)
                if not bool(getattr(r, "isOutOfDate", False)):
                    continue
                df = getattr(r, "dataFile", None)
                label = _reference_label(i, r)
                if not df:
                    log(f"[f2d] OOD ref without DataFile: {label}")
                    healed.append({"idx": i, "label": label, "ok": False, "reason": "no_datafile"})
                    continue
                log(f"[f2d] Healing OOD child: {label}")
                child_doc = None
                try:
                    child_doc = app.documents.open(df)
                    t0 = time.time()
                    while time.time() - t0 < 6.0 and child_doc and (not child_doc.isActive):
                        try:
                            adsk.doEvents()
                            if vp: vp.refresh()
                        except: pass
                        time.sleep(0.05)
                    child_res = update_to_latest(child_doc, timeout_s=child_timeout_s, strategy="api_then_ui", force_ui=True)
                    if not doc.isActive:
                        doc.activate(); _doc_ready(doc, 5.0)
                    try:
                        doc.updateAllReferences()
                    except Exception as ex_up:
                        log(f"[f2d] Parent updateAllReferences failed: {ex_up}")
                    healed.append({"idx": i, "label": label, "child_result": child_res, "ok": True})
                except Exception as ex:
                    log(f"[f2d] Heal exception for child: {ex}")
                    healed.append({"idx": i, "label": label, "ok": False, "reason": f"exception:{ex}"})

        try:
            adsk.doEvents()
            if vp: vp.refresh()
        except: pass
        after = _list_out_of_date_refs(doc)
        log(f"[f2d] After child heals: {after}")
        _dump_reference_details(doc)

    saved = False
    if not after and save_when_clean:
        try:
            if doc.isDirty:
                # doc.save("Refreshed to latest")
                saved = True
                log("[f2d] Drawing saved after refresh.")
        except Exception as ex:
            log(f"[f2d] Save failed: {ex}")

    return {
        "ok": not after,
        "before": before,
        "after": after,
        "healed": healed,
        "saved": saved,
        "update_report": upd,
        "used_custom_updater": bool(used_custom)
    }
# ──────────────────────────────────────────────────────────────────────────────
# Config helpers (compact)
# ──────────────────────────────────────────────────────────────────────────────
def make_die_insert_config_name(source_config_name: str) -> str:
    """
    From JSON like "KHF-B2PM-25MT-PT-FLT-3" → "KHF-B2DI-25-PT-FLT-3"
      - B2PM -> B2DI
      - '{NN}MT' -> '{NN}'
    """
    s = (source_config_name or "").strip()
    s = s.replace("B2PM", "B2DI")
    s = re.sub(r"-([0-9]+)MT-", r"-\1-", s)
    return s

def ensure_configuration(design, config_name: str, *, activate: bool = True) -> dict:
    """
    Ensure a configuration named `config_name` exists on `design`. If present, optionally activate.
    Returns { ok, existed, activated, name, reason }
    """
    try:
        # Configuration manager varies by build name; try both
        mgr = getattr(design, "configurations", None) or getattr(design, "configurationManager", None)
        if not mgr:
            log(f"[cfg] No configurations manager; cannot ensure '{config_name}'.")
            return {"ok": False, "existed": False, "activated": False, "name": config_name, "reason": "no_manager"}

        confs = getattr(mgr, "configurations", None) or getattr(mgr, "items", None) or mgr
        found = None
        count = getattr(confs, "count", 0) if hasattr(confs, "count") else 0
        for i in range(int(count or 0)):
            c = confs.item(i)
            if (getattr(c, "name", "") or "").strip() == config_name:
                found = c
                break

        if not found:
            add_fn = getattr(mgr, "add", None) or getattr(confs, "add", None)
            if not callable(add_fn):
                return {"ok": False, "existed": False, "activated": False, "name": config_name, "reason": "no_add"}
            found = add_fn(config_name)
            log(f"[cfg] Created configuration '{config_name}'.")

        activated = False
        if activate:
            try:
                if hasattr(design, "activeConfiguration"):
                    setattr(design, "activeConfiguration", found)
                    activated = True
                elif hasattr(found, "activate") and callable(getattr(found, "activate")):
                    found.activate()
                    activated = True
                else:
                    log(f"[cfg] Cannot activate '{config_name}' on this build.")
            except Exception as ex:
                log(f"[cfg] activate('{config_name}') failed: {ex}")

        return {"ok": True, "existed": True, "activated": activated, "name": config_name}
    except Exception as ex:
        log(f"[cfg] ensure_configuration exception: {ex}\n{traceback.format_exc()}")
        return {"ok": False, "existed": False, "activated": False, "name": config_name, "reason": f"exception:{ex}"}

# ──────────────────────────────────────────────────────────────────────────────
# Timeline helpers
# ──────────────────────────────────────────────────────────────────────────────
def _make_matcher(query: str, mode: str = "contains", case_insensitive: bool = True):
    q = query or ""
    flags = re.IGNORECASE if case_insensitive else 0
    if mode == "regex":
        rx = re.compile(q, flags)
        return lambda s: bool(rx.search(s or ""))
    if mode == "exact":
        ql = q.lower() if case_insensitive else q
        return (lambda s: (s or "").lower() == ql) if case_insensitive else (lambda s: (s or "") == q)
    ql = q.lower() if case_insensitive else q
    return (lambda s: ql in (s or "").lower()) if case_insensitive else (lambda s: q in (s or ""))

def _display_info(tlo: adsk.fusion.TimelineObject):
    all_names = []
    try:
        tlon = getattr(tlo, "name", None)
        if tlon:
            all_names.append(("tlo.name", tlon))
    except: pass
    try:
        if tlo.isGroup:
            g = adsk.fusion.TimelineGroup.cast(tlo)
            if g and g.name:
                all_names.append(("group.name", g.name))
    except: pass
    ent = None
    try:
        ent = tlo.entity
    except:
        ent = None
    aux_type = "(unknown)"
    try:
        if ent is not None:
            nm = getattr(ent, "name", None)
            if nm:
                all_names.append(("entity.name", nm))
    except: pass
    try:
        if ent is not None:
            ct = ent.classType()
            short = ct.split("::")[-1] if isinstance(ct, str) else str(ct)
            aux_type = short
            all_names.append(("entity.classType", short))
    except: pass
    if all_names:
        src, primary = all_names[0]
        return primary, aux_type, src, [n for _, n in all_names]
    return "(unnamed)", aux_type, "unknown", []

def collect_timeline_matches(
    design: adsk.fusion.Design,
    query: str,
    *,
    mode: str = "contains",
    case_insensitive: bool = True,
    include_groups: bool = True
):
    tl = design.timeline
    n = tl.count
    test = _make_matcher(query, mode=mode, case_insensitive=case_insensitive)
    hits = []
    log(f"[scan] Timeline has {n} objects. Looking for '{query}' (mode={mode}, ci={case_insensitive}).")
    for i in range(n):
        try:
            tlo = tl.item(i)
            if tlo.isGroup and not include_groups:
                continue
            primary, typ, src, all_names = _display_info(tlo)
            matched = any(test(cand) for cand in all_names) or (mode == "regex" and test(typ))
            if matched:
                hits.append({"index": i, "name": primary, "type": typ, "source": src})
                log(f"[hit] idx={i} | primary='{primary}' | type='{typ}' | names={all_names}")
            elif i < 40 and i % 5 == 0:
                log(f"[peek] idx={i} | primary='{primary}' | type='{typ}' | names={all_names[:3]}")
        except:
            pass
    log(f"[scan] Found {len(hits)} match(es).")
    return hits

def move_timeline_marker_to(
    design: adsk.fusion.Design,
    query: str,
    *,
    position: str = "after",       # "before" | "after" | "at"
    mode: str = "contains",        # "contains" | "exact" | "regex"
    nth: "int|str" = "last",       # "first" | "last" | 1-based integer
    case_insensitive: bool = True,
    include_groups: bool = True,
    pump_ui: bool = True
) -> dict:
    app = adsk.core.Application.get()
    vp  = _safe_active_viewport(app)
    try:
        hits = collect_timeline_matches(
            design, query, mode=mode, case_insensitive=case_insensitive,
            include_groups=include_groups
        )
        if not hits:
            return {"ok": False, "moved_to": None, "match": None, "count": 0, "reason": "no_match"}

        if isinstance(nth, str):
            match = hits[0] if nth.lower() == "first" else hits[-1]
        else:
            idx = max(1, int(nth)) - 1
            if idx >= len(hits):
                return {"ok": False, "moved_to": None, "match": None, "count": len(hits), "reason": f"nth_out_of_range({nth})"}
            match = hits[idx]

        target_idx = match["index"]
        tl = design.timeline
        pos = position.lower().strip()
        if pos == "before":
            marker = target_idx
        elif pos == "after":
            marker = target_idx + 1
        else:
            marker = target_idx

        marker = max(0, min(marker, tl.count))
        tl.markerPosition = marker

        if pump_ui:
            try:
                adsk.doEvents()
                if vp: vp.refresh()
                time.sleep(0.05)
            except: pass

        log(f"[move] marker -> {marker} (pos='{position}') at match idx={target_idx} | '{match['name']}' ({match['type']})")
        return {"ok": True, "moved_to": marker, "match": match, "count": len(hits), "reason": None}

    except Exception as ex:
        log(f"[error] move_timeline_marker_to: {ex}\n{traceback.format_exc()}")
        return {"ok": False, "moved_to": None, "match": None, "count": 0,
 "reason": "exception"}

def occ_from_timeline_index(design: adsk.fusion.Design, idx: int) -> Optional[adsk.fusion.Occurrence]:
    try:
        tlo = design.timeline.item(idx)
        ent = getattr(tlo, "entity", None)
        return adsk.fusion.Occurrence.cast(ent)
    except:
        return None

# ──────────────────────────────────────────────────────────────────────────────
# High-level: open → (optional) update → (optional) move
# ──────────────────────────────────────────────────────────────────────────────
def open_and_prepare(
    *,
    target_name: str,
    project_hint: Optional[str],
    folder_hint: Optional[List[str]],
    ext: str = "f3d",
    match_mode: str = "literal",
    search_timeout_s: float = 30.0,
    max_files_scanned: int = 10000,
    debug_scan: bool = False,
    update_to_latest_first: bool = True,
    update_timeout_s: float = 90.0,
    latest_strategy: str = "api_then_ui",
    force_ui_get_latest: bool = False,
    move_timeline: bool = True,
    timeline_query: str = "Component",
    timeline_position: str = "after",
    timeline_mode: str = "contains",
    timeline_nth: "int|str" = "last"
) -> Tuple[
    Optional[adsk.fusion.Design], Optional[adsk.core.Document], Dict[str, Any],
    Optional[dict], Optional[dict], Optional[adsk.fusion.Occurrence]
]:
    product, doc, meta = open_design(
        target_name=target_name,
        project_hint=project_hint,
        folder_hint=folder_hint,
        ext=ext,
        match_mode=match_mode,
        search_timeout_s=search_timeout_s,
        max_files_scanned=max_files_scanned,
        debug_scan=debug_scan
    )
    if not product or not doc:
        return None, None, meta, None, None, None

    update_report = None
    if update_to_latest_first:
        try:
            update_report = update_to_latest(
                doc,
                timeout_s=update_timeout_s,
                strategy=latest_strategy,
                force_ui=force_ui_get_latest
            )
            if update_report and update_report.get("ood_after"):
                log("[update] Still OOD after update → attempting targeted heal on first reference.")
                heal = heal_first_out_of_date_reference(doc, budget_s=max(30.0, update_timeout_s * 0.5))
                update_report["heal_attempt"] = heal
        except Exception as ex:
            log(f"[update] exception: {ex}\n{traceback.format_exc()}")

    design = adsk.fusion.Design.cast(product) if ext.lower() == "f3d" else None

    move_result = None
    occ = None
    if move_timeline and design:
        move_result = move_timeline_marker_to(
            design,
            query=timeline_query,
            position=timeline_position,
            mode=timeline_mode,
            nth=timeline_nth
        )
        if move_result and move_result.get("ok"):
            idx = move_result["match"]["index"]
            occ = occ_from_timeline_index(design, idx)

    return design, doc, meta, update_report, move_result, occ
