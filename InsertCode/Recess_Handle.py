# open_owner_latest_then_set_insert.py
# 1) Open owner at true latest (no update by default)
# 2) Reactivate PI/B2, move before "Remove"
# 3) Find Insert column for MT occurrence and set its cell to MT-1-11-0063
# 4) Save PI/B2

import adsk.core, adsk.fusion, time, traceback
from ..helpers import loggin_utils

# ======= Settings =======
RUN_UPDATE_AFTER_OPEN = False              # owner update is OFF (crashy on some builds)
TOKEN_TO_FIND_OCC     = "MT"              # token to locate the linked recess occurrence
#TARGET_CFG_NAME       = "MT-1-11-0063"    # owner row name to select in the Insert cell
TIMELINE_BEFORE_NAME  = "Remove"          # move before this feature so the recess link exists/visible
SAVE_PI_AFTER_SWITCH  = True

# ======= tiny utils =======
def _uc_contains(h, n): return (n or "").upper() in (h or "").upper()
def _toi(x):
    try: return int(x)
    except: return None
def _yield(app, secs=0.05):
    try: adsk.doEvents()
    except: pass
    if secs:
        try: time.sleep(secs)
        except: pass
    try: app.activeViewport.refresh()
    except: pass
def _safe_name(x): 
    try: return (getattr(x, "name", "") or "")
    except: return ""

def _same_occ(a, b) -> bool:
    """Best-effort equality for occurrences across API variants."""
    if a is None or b is None: return False
    if a is b: return True
    # Try stable tokens if present
    for attr in ("entityToken", "fullPathName"):
        try:
            if getattr(a, attr, None) and getattr(b, attr, None) and getattr(a, attr) == getattr(b, attr):
                return True
        except: pass
    # Last resort: compare display names
    return _safe_name(a) == _safe_name(b)

def _close_sessions_for(app: adsk.core.Application, ids: set, name_prefixes: set):
    try:
        for i in range(app.documents.count - 1, -1, -1):
            d = app.documents.item(i)
            try:
                df = getattr(d, "dataFile", None)
                did = getattr(df, "id", None)
                dname = (getattr(d, "name", "") or "").strip()
                if (did and did in ids) or any(dname.lower().startswith(p.lower()) for p in name_prefixes if p):
                    loggin_utils.log(f"[close] closing '{dname}' (id-match={did in ids})")
                    try: d.close(False)
                    except: pass
            except: pass
    except: pass

# ======= pick linked occurrence in PI/B2 (selection-first, else token) =======
def _pick_occurrence(design: adsk.fusion.Design, ui, token_default=TOKEN_TO_FIND_OCC):
    occ = None
    if ui and ui.activeSelections and ui.activeSelections.count > 0:
        try:
            ent = ui.activeSelections.item(0).entity
            occ = adsk.fusion.Occurrence.cast(ent) or getattr(ent, "occurrence", None)
            if occ and not getattr(occ, "isReferencedComponent", False):
                occ = None
        except: occ = None
    if not occ:
        best, backup = [], []
        for o in design.rootComponent.allOccurrences:
            if not getattr(o, "isReferencedComponent", False): continue
            comp = getattr(o, "component", None)
            names = [
                _safe_name(o),
                _safe_name(comp) if comp else "",
                _safe_name(getattr(o, "configuredDataFile", None)),
                _safe_name(getattr(comp, "dataFile", None) if comp else None),
            ]
            if any(_uc_contains(n, token_default) for n in names):
                (best if _uc_contains(_safe_name(o), token_default) else backup).append(o)
        occ = best[0] if best else (backup[0] if backup else None)

    if occ:
        try:
            comp = occ.component
            cfg = getattr(occ, "configuredDataFile", None)
            compdf = getattr(comp, "dataFile", None) if comp else None
            loggin_utils.log(f"[pick] occ='{_safe_name(occ)}', comp='{_safe_name(comp)}', "
                             f"cfgDF='{_safe_name(cfg)}', compDF='{_safe_name(compdf)}'")
        except: pass
    else:
        loggin_utils.log("[pick] no linked occurrence found")
    return occ

def _owner_df_from_occ(occ):
    try:
        try: df = getattr(occ, "configuredDataFile", None)
        except: df = None
        if df: return df
        comp = getattr(occ, "component", None)
        return getattr(comp, "dataFile", None) if comp else None
    except: return None

# ======= robust “true latest” open (same safe path you validated) =======
def _scan_best_version(df: adsk.core.DataFile):
    vers = getattr(df, "versions", None)
    count = getattr(vers, "count", 0) if vers else 0
    best_v, best_num = None, -1
    if vers and count > 0:
        for i in range(count):
            v = vers.item(i)
            n = _toi(getattr(v, "versionNumber", None))
            if n is not None and n > best_num:
                best_v, best_num = v, n
    if best_v is None:
        best_num = _toi(getattr(df, "versionNumber", None))
    return best_v, best_num, count

def _open_doc_return_design(app, target):
    try:
        try: doc = app.documents.open(target, True)
        except: doc = app.documents.open(target)
        if not doc: return None, None
        try: doc.activate()
        except: pass
        _yield(app, 0.05)
        try:
            prod = doc.products.itemByProductType('DesignProductType')
            design = adsk.fusion.Design.cast(prod)
            if design: return design, doc
        except: pass
        return adsk.fusion.Design.cast(app.activeProduct), doc
    except:
        return None, None

def open_owner_latest_only(app: adsk.core.Application, df: adsk.core.DataFile):
    if not df:
        loggin_utils.log("[latest] ❌ no DataFile")
        return None, None, None, None

    base = _safe_name(df)
    ext  = (getattr(df, "fileExtension", "") or "").strip().lower()
    best_ver, latest_num, count = _scan_best_version(df)
    pinned_name = _safe_name(getattr(best_ver, 'dataFile', None))
    loggin_utils.log(f"[latest] base='{base}', ext='{ext}', versions={count}, latest={latest_num}, pinnedDF='{pinned_name}'")

    ids = set(filter(None, [getattr(df,"id",None), getattr(getattr(best_ver,'dataFile',None),"id",None)]))
    name_prefixes = set(filter(None, [base, f"{base} v{latest_num}" if latest_num else None]))
    _close_sessions_for(app, ids, name_prefixes)

    if best_ver:
        design, doc = _open_doc_return_design(app, best_ver)
        if design and doc:
            opened_num = _toi(getattr(doc.dataFile, "versionNumber", None))
            loggin_utils.log(f"[latest] tryA opened='{_safe_name(doc.dataFile)}', ver={opened_num}")
            return design, doc, latest_num, opened_num

    design, doc = _open_doc_return_design(app, df)
    opened_num = _toi(getattr(getattr(doc,'dataFile',None), "versionNumber", None)) if doc else None
    loggin_utils.log(f"[latest] tryB opened='{_safe_name(getattr(doc,'dataFile',None))}', ver={opened_num}")
    return design, doc, latest_num, opened_num

def _safe_update_opened_owner(app):
    try:
        _yield(app, 0.10)
        try: app.executeTextCommand("Document.Refresh")
        except: pass
        _yield(app, 0.10)
        ui = app.userInterface
        for cid in ('UpdateAllOutOfDateReferencesCmd','GetLatestExternalReferencesCmd','UpdateReferencesCmd'):
            cmd = ui.commandDefinitions.itemById(cid)
            if cmd:
                cmd.execute()
                _yield(app, 0.10)
                loggin_utils.log(f"[update] ran '{cid}' in owner doc")
                break
    except Exception as e:
        loggin_utils.log(f"[update] error: {e}\n{traceback.format_exc()}")

# ======= timeline: move before feature (use your helper if available, else fallback) =======
def _move_timeline_before_feature_safe(design: adsk.fusion.Design, feat_name: str) -> bool:
    try:
        from . import cloud_operations
        try:
            cloud_operations.move_timeline_before_feature(feat_name)
            loggin_utils.log(f"[timeline] moved before '{feat_name}' via cloud_operations")
            return True
        except Exception as e:
            loggin_utils.log(f"[timeline] cloud_operations move failed: {e}")
    except Exception:
        pass
    try:
        tl = design.timeline
        idx = None
        for i in range(getattr(tl, "count", 0)):
            try:
                tobj = tl.item(i)
                if _uc_contains(getattr(tobj, "name", "") or "", feat_name):
                    idx = max(0, i - 1)
                    break
            except: pass
        if idx is not None:
            try:
                if hasattr(tl, "moveToPosition"): tl.moveToPosition(idx)
                else: tl.markerPosition = idx
            except: pass
            _yield(adsk.core.Application.get(), 0.05)
            loggin_utils.log(f"[timeline] moved marker to {idx} (before '{feat_name}')")
            return True
    except Exception as e:
        loggin_utils.log(f"[timeline] fallback move failed: {e}\n{traceback.format_exc()}")
    return False

# ======= Insert-column path (key bit) =======
def _get_top_cfg_table(design: adsk.fusion.Design):
    try:
        return getattr(design, "configurationTopTable", None)
    except: return None

def _ensure_active_row(top):
    """Return an active row (ensure one is active); prefer existing activeRow."""
    try:
        if getattr(top, "activeRow", None):
            return top.activeRow
    except: pass
    try:
        if getattr(top, "rows", None) and top.rows.count > 0:
            r0 = top.rows.item(0)
            try:
                r0.activate()
                _yield(adsk.core.Application.get(), 0.02)
            except: pass
            return r0
    except: pass
    return None

def _find_insert_col_for_occ(top, occ):
    try:
        cols = getattr(top, "columns", None)
        if not cols: return None
        for ci in range(cols.count):
            col = cols.item(ci)
            if "ConfigurationInsertColumn" not in (getattr(col, "objectType", "") or ""):
                continue
            try:
                cocc = getattr(col, "occurrence", None)
            except:
                cocc = None
            if cocc and _same_occ(cocc, occ):
                loggin_utils.log(f"[insert] matched Insert column '{_safe_name(col)}' to occ '{_safe_name(occ)}'")
                return col
        # Fallback: pick any insert column if only one exists
        only = None
        for ci in range(cols.count):
            col = cols.item(ci)
            if "ConfigurationInsertColumn" in (getattr(col, "objectType", "") or ""):
                if only is None: only = col
                else: only = None; break
        if only:
            loggin_utils.log("[insert] using single Insert column fallback")
            return only
    except: pass
    return None

def _get_owner_row(owner_design: adsk.fusion.Design, target_name: str):
    """Get the owner ConfigurationRow by exact name, else token fallback."""
    try:
        tbl = getattr(owner_design, "configurationTopTable", None)
        if not tbl: return None
        try:
            r = tbl.rows.itemByName(target_name)
            if r: return r
        except: pass
        token = (target_name or "").split("-")[-1]
        for i in range(tbl.rows.count):
            rr = tbl.rows.item(i)
            nm = _safe_name(rr)
            if nm == target_name: return rr
            if token and (f"-{token}-" in nm or nm.endswith(f"-{token}") or nm.startswith(f"{token}-")):
                return rr
    except: pass
    return None

def _set_insert_cell_to_rowname(top, insert_col, pi_row_name: str, owner_row) -> bool:
    """
    Set the Insert cell at PI row 'pi_row_name' to the owner ConfigurationRow 'owner_row'.
    Robust fallbacks: row object, row name, text properties.
    """
    try:
        # Locate the cell for the desired PI row
        cell = None
        if hasattr(insert_col, "getCellByRowName"):
            try: cell = insert_col.getCellByRowName(pi_row_name)
            except: cell = None
        if not cell:
            # index-based fallback
            rid = None
            rows = getattr(top, "rows", None)
            if rows:
                for i in range(rows.count):
                    r = rows.item(i)
                    if _safe_name(r) == pi_row_name:
                        rid = getattr(r, "id", None)
                        break
            if rid is not None:
                try: cell = insert_col.getCellByRowId(rid)
                except: cell = None
        if not cell:
            loggin_utils.log("[insert] ❌ could not get Insert cell for PI row")
            return False

        # Primary: assign owner row object
        try:
            if hasattr(cell, "row") and owner_row:
                cell.row = owner_row
                return True
        except: pass
        try:
            if hasattr(cell, "setByConfigurationRow") and owner_row:
                cell.setByConfigurationRow(owner_row)
                return True
        except: pass

        # Name-based fallbacks
        target_name = _safe_name(owner_row)
        for attr in ("setByConfigurationRowName", "selectedName", "stringValue", "text", "value", "expression"):
            try:
                if hasattr(cell, attr):
                    getattr(cell, attr)(target_name) if callable(getattr(cell, attr)) else setattr(cell, attr, target_name)
                    return True
            except: pass

        loggin_utils.log("[insert] ❌ failed to apply owner row by any method")
        return False
    except Exception as e:
        loggin_utils.log(f"[insert] exception: {e}\n{traceback.format_exc()}")
        return False

# ======= ENTRY POINT =======
def enter_recess(recessType, recessName):
    try:
        app = adsk.core.Application.get()
        ui  = app.userInterface if app else None
        loggin_utils.log("=== run(): open owner latest → reactivate PI → move before 'Remove' → set Insert cell ===")

        # Capture PI/B2 doc to return later
        doc_b2 = app.activeDocument
        des_b2 = adsk.fusion.Design.cast(app.activeProduct)
        if not des_b2:
            if ui: ui.messageBox("No active PI/B2 design.")
            return
        b2_name = _safe_name(doc_b2)

        # Find the linked recess occurrence in PI/B2 (to resolve the owner)
        occ_b2 = _pick_occurrence(des_b2, ui, recessType)
        if not occ_b2:
            if ui: ui.messageBox(f"No linked occurrence found containing '{recessType}'.")
            return

        owner_df = _owner_df_from_occ(occ_b2)
        if not owner_df:
            if ui: ui.messageBox("Could not resolve owner DataFile.")
            return

        # Open owner at TRUE latest (no update by default)
        owner_design, owner_doc, latest_num, opened_num = open_owner_latest_only(app, owner_df)
        if not owner_design:
            if ui: ui.messageBox("Failed to open owner at latest. See log.")
            return
        loggin_utils.log(f"[owner] opened '{_safe_name(getattr(owner_doc,'dataFile',None))}' "
                         f"(ver={getattr(getattr(owner_doc,'dataFile',None),'versionNumber',None)}, latest={latest_num})")

        if RUN_UPDATE_AFTER_OPEN:
            _safe_update_opened_owner(app)

        # === Reactivate PI/B2 BEFORE attempting to change anything
        try:
            if app.activeDocument is not doc_b2 and doc_b2:
                doc_b2.activate()
            _yield(app, 0.06)
            loggin_utils.log(f"[B2] Back in B2: {_safe_name(app.activeDocument)}")
        except Exception as e:
            loggin_utils.log(f"[B2] Reactivate failed: {e}\n{traceback.format_exc()}")

        # === Reactivate PI/B2 BEFORE attempting to change anything
        try:
            if app.activeDocument is not doc_b2 and doc_b2:
                doc_b2.activate()
            _yield(app, 0.06)
            loggin_utils.log(f"[B2] Back in B2: {_safe_name(app.activeDocument)}")
        except Exception as e:
            loggin_utils.log(f"[B2] Reactivate failed: {e}\n{traceback.format_exc()}")

        # Try to find & set without moving the timeline first
        occ_b2 = _pick_occurrence(des_b2, ui, recessType)
        if not occ_b2:
            loggin_utils.log("[B2] ❌ occurrence not found (pre-move).")
            if ui: ui.messageBox(f"No linked occurrence found containing '{recessType}'.")
            return

        top = _get_top_cfg_table(des_b2)
        if not top:
            loggin_utils.log("[B2] ❌ no configurationTopTable on PI/B2.")
            if ui: ui.messageBox("PI/B2 has no configuration top table.")
            return

        pi_row = _ensure_active_row(top)
        if not pi_row:
            loggin_utils.log("[B2] ❌ no active PI row available.")
            if ui: ui.messageBox("No active configuration row in PI/B2.")
            return

        ins_col = _find_insert_col_for_occ(top, occ_b2)

        # If the insert column isn't visible/addressable yet, then (and only then) move before 'Remove' and retry once
        if not ins_col:
            try:
                moved = _move_timeline_before_feature_safe(des_b2, TIMELINE_BEFORE_NAME)
                if moved:
                    # rebind handles after the move
                    occ_b2 = _pick_occurrence(des_b2, ui, recessType)
                    top    = _get_top_cfg_table(des_b2)
                    pi_row = _ensure_active_row(top)
                    ins_col = _find_insert_col_for_occ(top, occ_b2)
            except Exception:
                pass

        if not ins_col:
            loggin_utils.log("[B2] ❌ Insert column for occurrence not found (even after optional move).")
            if ui: ui.messageBox("Could not locate the Insert column for the linked occurrence.")
            return


        # Locate the Insert column tied to this occurrence
        ins_col = _find_insert_col_for_occ(top, occ_b2)
        if not ins_col:
            loggin_utils.log("[B2] ❌ Insert column for occurrence not found.")
            if ui: ui.messageBox("Could not locate the Insert column for the linked occurrence.")
            return

        # Get the owner row object for the target config name
        owner_row = _get_owner_row(owner_design, recessName)
        if not owner_row:
            loggin_utils.log(f"[owner] ❌ row '{recessName}' not found on owner.")
            if ui: ui.messageBox(f"Owner does not contain row '{recessName}'.")
            return

        # Apply: set Insert cell (PI row → owner row)
        ok = _set_insert_cell_to_rowname(top, ins_col, _safe_name(pi_row), owner_row)
        loggin_utils.log(f"[B2] Set Insert to '{_safe_name(owner_row)}' for PI row '{_safe_name(pi_row)}' → {'OK' if ok else 'FAILED'}")

        # Save PI/B2
        if ok and SAVE_PI_AFTER_SWITCH and doc_b2:
            try:
                desc = f"B2: set Insert to '{_safe_name(owner_row)}'"
                try: doc_b2.save(desc)
                except TypeError: doc_b2.save("")
                _yield(app, 0.05)
                loggin_utils.log("[B2] Saved after Insert change.")
            except Exception as e:
                loggin_utils.log(f"[B2] Save failed: {e}\n{traceback.format_exc()}")

        #if ui:
        #    ui.messageBox(
        #        f"{'Updated' if ok else 'Attempted to update'} Insert to '{_safe_name(owner_row)}' in '{b2_name}'.",
        #        "B2 Set Insert"
        #    )

        loggin_utils.log("=== run(): DONE ===")
    except Exception as e:
        loggin_utils.log(f"run() error: {e}\n{traceback.format_exc()}")
        try: adsk.core.Application.get().userInterface.messageBox(f"Error: {e}")
        except: pass