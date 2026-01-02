# OpenSeekInspect_main.py
# Main entry: open → move timeline → (POST-MOVE) update to latest
# → inspect linked occurrence → (NEW) switch linked occurrence directly
# → optionally open/refresh related drawing.

import os, json, re, traceback, time
from typing import Optional, List, Dict, Any, Tuple
from . import occ_owner_switch as sw
from . import Recess_Handle
from . import open_utils as ou
import adsk.core, adsk.fusion
try:
    import adsk.drawing
except Exception:
    adsk.drawing = None  # type: ignore


INPUT_JSON_PATH = r"C:\Temp\etdp_inputs.json"
FILE_PATH = r"P:\ETDP\PDFOutput"
# ──────────────────────────────────────────────────────────────────────────────
# Small pumps/activation helpers
# ──────────────────────────────────────────────────────────────────────────────
def _pump(dt=0.05):
    try:
        adsk.doEvents()
        vp = adsk.core.Application.get().activeViewport
        if vp: vp.refresh()
    except: pass
    time.sleep(dt)

def _yield(dt=0.06):
    try:
        adsk.doEvents()
        vp = adsk.core.Application.get().activeViewport
        if vp: vp.refresh()
    except: pass
    time.sleep(dt)

def _safe_name(x):
    try: return (getattr(x, "name", "") or "")
    except: return ""

# ──────────────────────────────────────────────────────────────────────────────
# Configuration table helpers (literal/exact)
# ──────────────────────────────────────────────────────────────────────────────
def _get_top_config_table_literal(design: adsk.fusion.Design):
    candidates = [
        getattr(design, "configurationTopTable", None),
        getattr(getattr(design, "configurationManager", None), "configurationTopTable", None),
    ]
    for t in candidates:
        try:
            if getattr(t, "rows", None) is not None:
                return t
        except: pass

    for container_attr in ("configurationTables",):
        tables = (
            getattr(design, container_attr, None)
            or getattr(getattr(design, "rootComponent", None), container_attr, None)
            or getattr(getattr(design, "configurationManager", None), container_attr, None)
        )
        try:
            if tables and getattr(tables, "count", 0) > 0:
                return tables.item(0)
        except: pass
    return None

def _find_row_exact(rows, target_name: str):
    want = (target_name or "").strip()
    want_lc = want.lower()
    try:
        byname = getattr(rows, "itemByName", None)
        if callable(byname):
            r = byname(want)
            if r: return r
    except: pass
    try:
        for i in range(int(getattr(rows, "count", 0) or 0)):
            r = rows.item(i)
            nm = (getattr(r, "name", None) or getattr(r, "label", None) or "").strip()
            if nm.lower() == want_lc:
                return r
    except: pass
    return None

def _activate_row_literal(row) -> bool:
    if not row: return False
    try:
        tbl = getattr(row, "table", None)
    except:
        tbl = None
    try:
        row.activate()
        _pump(0.05)
        if not tbl:
            app = adsk.core.Application.get()
            des = adsk.fusion.Design.cast(app.activeProduct)
            tbl = _get_top_config_table_literal(des) if des else None
        return bool(tbl and getattr(tbl, "activeRow", None) and tbl.activeRow.name == row.name)
    except:
        return False

def activate_config_row_by_name(design: adsk.fusion.Design, target_name: str) -> bool:
    try:
        tbl = _get_top_config_table_literal(design)
        if not tbl: return False
        rows = getattr(tbl, "rows", None)
        if not rows or getattr(rows, "count", 0) == 0: return False
        row = _find_row_exact(rows, target_name)
        if not row: return False
        return _activate_row_literal(row)
    except:
        return False

# ──────────────────────────────────────────────────────────────────────────────
# Inputs
# ──────────────────────────────────────────────────────────────────────────────
_cfg_token_rx = re.compile(r'([A-Za-z0-9\-]+-\d+)\b')

def read_inputs() -> Dict[str, Any]:
    defaults = {
        # open targets
        "target_name": "KHF-B2DI-##-PT-FLT-#",
        "project_hint": "parametric_models",
        "folder_hint": ["KHF-EEFF-GG-FLT"],
        "ext": "f3d",

        # update & timeline
        "update_to_latest_first": False,
        "update_timeout_s": 120.0,
        "latest_strategy": "api_then_ui",
        "force_ui_get_latest": False,

        "timeline_query": "Component",
        "timeline_position": "after",
        "timeline_mode": "contains",
        "timeline_nth": "last",

        # search behavior
        "search_timeout_s": 40.0,
        "max_files_scanned": 50000,
        "match_mode": "literal",
        "debug_scan": False,

        # direct switch after open/update
        "attempt_switch": True,
        "config_name": "KHF-B2PM-25MT-PT-FLT-3",
        "probe_only": False,

        # owner/table retry behavior
        "owner_retry_tries": 6,
        "owner_retry_base_s": 0.18,
        "move_marker_to_end": True,

        # drawing
        "open_related_drawing": True,
        "drawing_from_model_name": True,
        "drawing_folder_hint": [],
        "drawing_update_timeout_s": 120.0,
        "drawing_force_ui_get_latest": True,
        "drawing_name": "",
        "drawing_folder_hint": [],

        # Punch Insert flow
        "pi_model_name": "PI-MT-STYLE-DELTA",
        "pi_folder_hint": ["PI-OD-LEN-STYLE"],
        "pi_project_hint": None,
        "pi_base_config": "PI-MT-1-11-0063",
        "pi_recess_name": "MT-1-11",       # owner row we want
        "pi_timeline_anchor": "RemoveInstance",
        "pi_occurrence_token": "MT",               # optional extra token

        # keep the owner (recess) document open so you can inspect it
        "pi_keep_owner_open": True,

        # suppress summary messageBoxes during run
        "suppress_ui_messages": True,
    }
    try:
        if os.path.exists(INPUT_JSON_PATH):
            with open(INPUT_JSON_PATH, 'r', encoding='utf-8') as f:
                data = json.load(f)
            defaults.update({k: v for k, v in data.items() if v is not None})
            ou.log(f"Loaded inputs from JSON: {INPUT_JSON_PATH}")
    except Exception as ex:
        ou.log(f"read_inputs: failed to parse JSON ({ex}); using defaults.")
    return defaults

def _update_inputs_json(patch: Dict[str, Any]) -> None:
    """Merge given keys into the existing input JSON on disk."""
    try:
        # Start from whatever is currently on disk (or empty)
        current: Dict[str, Any] = {}
        if os.path.exists(INPUT_JSON_PATH):
            with open(INPUT_JSON_PATH, 'r', encoding='utf-8') as f:
                current = json.load(f)

        # Merge in our new values
        current.update(patch)

        # Write back
        with open(INPUT_JSON_PATH, 'w', encoding='utf-8') as f:
            json.dump(current, f, indent=2)

        ou.log(f"_update_inputs_json: patched {INPUT_JSON_PATH} with {list(patch.keys())}")
    except Exception as ex:
        ou.log(f"_update_inputs_json: failed to update {INPUT_JSON_PATH}: {ex}")

def _parse_inch_value(v):
    """
    Convert strings like '2.2 in' or '1.5in' or '2 in' → float(2.2).
    Also handles Fusion Value objects (v.value) and plain floats.
    Returns None if unusable.
    """
    try:
        # Fusion value object (has .value attribute)
        if hasattr(v, "value"):
            return float(v.value)

        # Already numeric
        if isinstance(v, (int, float)):
            return float(v)

        if isinstance(v, str):
            s = v.strip().lower()
            # remove trailing 'in' or 'inch' tokens
            if s.endswith("in"):
                s = s[:-2].strip()
            elif s.endswith("inch"):
                s = s[:-4].strip()

            # remove spaces
            s = s.replace(" ", "")
            return float(s)
    except Exception:
        pass

    return None

def save_doc_and_wait_for_new_version(doc: adsk.core.Document,
                                      desc: str = "Auto-save",
                                      timeout_s: float = 30.0) -> bool:
    """
    Save the given document and (best-effort) wait until its DataFile.versionNumber
    advances, so the cloud has the new state before drawings / other docs pull it.

    Returns True if save was attempted; does NOT hard-fail on timeout.
    """
    if not doc:
        return False

    app = adsk.core.Application.get()
    df = getattr(doc, "dataFile", None)

    old_ver = None
    if df:
        try:
            old_ver = int(getattr(df, "versionNumber", 0) or 0)
        except Exception:
            old_ver = None

    # Make sure this doc is active before saving
    try:
        doc.activate()
    except Exception:
        pass

    # Do the save
    try:
        adsk.doEvents()
        vp = app.activeViewport
        if vp:
            vp.refresh()
        time.sleep(0.05)
    except Exception:
        pass

    try:
        doc.save(desc or "Auto-save")
    except TypeError:
        try:
            doc.save("Auto-save")
        except Exception as e:
            ou.log(f"[save_wait] doc.save failed: {e}")
            return False
    except Exception as e:
        ou.log(f"[save_wait] doc.save threw: {e}")
        return False

    # If we can't read a previous version number, just bail out early.
    if old_ver is None or not df:
        return True

    # Wait for the DataFile.versionNumber to advance
    start = time.time()
    last_seen = old_ver

    while (time.time() - start) < timeout_s:
        try:
            adsk.doEvents()
            vp = app.activeViewport
            if vp:
                vp.refresh()
        except Exception:
            pass

        time.sleep(0.25)

        df2 = getattr(doc, "dataFile", None)
        if not df2:
            break

        try:
            cur_ver = int(getattr(df2, "versionNumber", 0) or 0)
        except Exception:
            break

        last_seen = cur_ver
        if cur_ver > old_ver:
            ou.log(f"[save_wait] versionNumber advanced {old_ver} → {cur_ver}")
            return True

    ou.log(f"[save_wait] timeout waiting for cloud version; still at {last_seen}")
    return True


# ──────────────────────────────────────────────────────────────────────────────
# Version/open helpers — TRUE LATEST open (fixes “v25 instead of latest”)
# ──────────────────────────────────────────────────────────────────────────────
def _scan_best_version(df: adsk.core.DataFile):
    """Return (best_version_file, best_version_number, total_versions)."""
    try:
        vers = getattr(df, "versions", None)
        count = getattr(vers, "count", 0) if vers else 0
        best_v, best_num = None, -1
        if vers and count > 0:
            for i in range(count):
                v = vers.item(i)
                try:
                    n = int(getattr(v, "versionNumber", 0) or 0)
                except:
                    n = 0
                if n > best_num:
                    best_v, best_num = getattr(v, "dataFile", None), n
        if best_v is None:
            try:
                best_num = int(getattr(df, "versionNumber", 0) or 0)
            except:
                best_num = 0
            best_v = df
        return best_v, best_num, count
    except:
        return df, int(getattr(df, "versionNumber", 0) or 0), 0

def _close_older_sessions_for_lineage(app: adsk.core.Application, lineage_id: str, keep_version: int):
    """Close any open docs with the same lineage but NOT at keep_version."""
    try:
        for i in range(app.documents.count - 1, -1, -1):
            d = app.documents.item(i)
            df = getattr(d, "dataFile", None)
            if not df: 
                continue
            if getattr(df, "id", None) == lineage_id:
                vnum = getattr(df, "versionNumber", None)
                try: vnum = int(vnum) if vnum is not None else None
                except: vnum = None
                if vnum is not None and keep_version is not None and vnum != keep_version:
                    try:
                        ou.log(f"[open-latest] closing older open tab '{_safe_name(df)}' (v={vnum}, want={keep_version})")
                        d.close(False)
                    except: pass
    except: pass

def _open_doc_true_latest(df: adsk.core.DataFile, *, activate: bool = True):
    """
    Open the *true latest* for a lineage:
    - scan max versionNumber
    - close any older open tabs for the same lineage
    - open that specific version's DataFile
    Returns (design, doc, latest_version, opened_version)
    """
    app = adsk.core.Application.get()
    best_df, best_num, _ = _scan_best_version(df)
    lineage_id = getattr(df, "id", None)
    _close_older_sessions_for_lineage(app, lineage_id, keep_version=best_num)

    # If a tab at the right version is already open, use it
    for i in range(app.documents.count):
        d = app.documents.item(i)
        try:
            ddf = getattr(d, "dataFile", None)
            if ddf and getattr(ddf, "id", None) == lineage_id:
                onum = int(getattr(ddf, "versionNumber", 0) or 0)
                if onum == best_num:
                    try: d.activate()
                    except: pass
                    _yield(0.05)
                    prod = d.products.itemByProductType('DesignProductType')
                    return adsk.fusion.Design.cast(prod), d, best_num, onum
        except: pass

    # Open the exact best version
    try:
        doc = app.documents.open(best_df, True)
    except:
        doc = app.documents.open(best_df)

    if activate:
        try: doc.activate()
        except: pass
    _yield(0.06)

    try:
        prod = doc.products.itemByProductType('DesignProductType')
        design = adsk.fusion.Design.cast(prod)
    except:
        design = adsk.fusion.Design.cast(app.activeProduct)

    opened_num = None
    try:
        opened_num = int(getattr(getattr(doc,'dataFile',None), "versionNumber", 0) or 0)
    except:
        pass

    ou.log(f"[open-latest] opened '{_safe_name(getattr(doc,'dataFile',None))}' v={opened_num} (true latest={best_num})")
    return design, doc, best_num, opened_num

def _reopen_doc_to_true_latest_if_needed(doc: adsk.core.Document):
    """If the provided doc is not at true latest, upgrade the tab to the latest version."""
    if not doc or not getattr(doc, "dataFile", None):
        return adsk.fusion.Design.cast(adsk.core.Application.get().activeProduct), doc
    df = doc.dataFile
    design, new_doc, latest_num, opened_num = _open_doc_true_latest(df, activate=True)
    return design, new_doc

# ──────────────────────────────────────────────────────────────────────────────
# Misc helpers (unchanged)
# ──────────────────────────────────────────────────────────────────────────────
def _guess_config_from_text(text: str) -> Optional[str]:
    if not text: return None
    cands = _cfg_token_rx.findall(text)
    if not cands: return None
    cands.sort(key=len, reverse=True)
    return cands[0]

def try_save_document(doc: adsk.core.Document, desc: str = "Auto-save") -> bool:
    if not doc:
        return False
    try:
        adsk.doEvents()
        vp = adsk.core.Application.get().activeViewport
        if vp: vp.refresh()
        time.sleep(0.05)
    except Exception:
        pass
    for d in (desc or "Auto-save", "Auto-save", " "):
        try:
            doc.save(d)
            break
        except TypeError:
            continue
        except Exception:
            break
    try:
        adsk.doEvents()
        vp = adsk.core.Application.get().activeViewport
        if vp: vp.refresh()
        time.sleep(0.03)
    except Exception:
        pass
    return True

def _normalize_pi_and_recess_names(pi_base_raw: str,
                                   recess_raw: str) -> tuple[str, str, str]:
    """
    Normalize PI + recess names for both plain and delta-suffix cases.

    Assumes both names come from JSON and are for the same "family".

    Examples:
      1) pi_base_raw = "PI-MT-1-11"
         recess_raw   = "MT-1-11"
         → base_config_name = "PI-MT-1-11"
           new_pi_row       = "PI-MT-1-11"
           owner_target_row = "MT-1-11"

      2) pi_base_raw = "PI-MT-1-11-0063"
         recess_raw   = "MT-1-11"
         → base_config_name = "PI-MT-1-11"
           new_pi_row       = "PI-MT-1-11-0063"
           owner_target_row = "MT-1-11-0063"
    """
    pi_base_raw = (pi_base_raw or "").strip()
    recess_raw  = (recess_raw or "").strip()

    # Start with "no-suffix" assumption
    base_config_name = pi_base_raw
    new_pi_row       = pi_base_raw
    owner_target_row = recess_raw

    # If either is missing, just return what we have – caller already checks empties
    if not pi_base_raw or not recess_raw:
        return base_config_name, new_pi_row, owner_target_row

    # Parse PI base into tokens
    parts = [p.strip() for p in pi_base_raw.split("-") if p.strip()]
    if len(parts) <= 4:
        # No extra suffix: just use given values, nothing MT/PI-specific
        return base_config_name, new_pi_row, owner_target_row

    # There is at least one extra segment after the 4th dash
    # e.g. ["PI","MT","1","11","0063"] → base_root="PI-MT-1-11", suffix="0063"
    base_root = "-".join(parts[:4])
    suffix    = "-".join(parts[4:])

    base_config_name = base_root      # copy FROM this
    new_pi_row       = pi_base_raw    # full JSON-specified row name

    # If recess already has the same suffix-ish tail, leave it alone.
    r_parts = [p.strip() for p in recess_raw.split("-") if p.strip()]
    if len(r_parts) > 4:
        owner_target_row = recess_raw
    else:
        owner_target_row = f"{recess_raw}-{suffix}"

    return base_config_name, new_pi_row, owner_target_row

def _refresh_occurrence_to_latest(occ) -> None:
    try:
        if hasattr(occ, "isOutOfDate") and occ.isOutOfDate and hasattr(occ, "updateToLatestVersion"):
            occ.updateToLatestVersion()
            _yield(0.10)
            ou.log("[switch] occ.updateToLatestVersion()")
            return
    except Exception as e:
        ou.log(f"[switch] occ.updateToLatestVersion failed: {e}")
    try:
        df = getattr(occ, 'configuredDataFile', None) or getattr(getattr(occ, 'component', None), 'dataFile', None)
        if not df: return
        # open true latest DataFile for lineage
        latest_df, latest_num, _ = _scan_best_version(df)
        if latest_df and hasattr(occ, "replaceComponent"):
            cur_num = getattr(df, "versionNumber", None)
            if cur_num != latest_num:
                occ.replaceComponent(latest_df)
                _yield(0.08)
                ou.log(f"[switch] occ.replaceComponent → v{latest_num}")
    except Exception as e:
        ou.log(f"[switch] replaceComponent failed: {e}")

def _get_param(design: adsk.fusion.Design, name: str):
    try:
        up = design.userParameters
        if not up: return None
        p = getattr(up, "itemByName", lambda _n: None)(name)
        if p: return p
        for i in range(up.count):
            pi = up.item(i)
            if pi and (pi.name or "").strip().lower() == name.lower():
                return pi
    except: pass
    return None

def _read_param_value(design: adsk.fusion.Design, name: str):
    p = _get_param(design, name)
    if not p: return None
    try:
        expr = p.expression
        if expr not in (None, ""):
            return expr
    except: pass
    try:
        return p.value
    except: pass
    return None

def _write_param_value(design: adsk.fusion.Design, name: str, value):
    p = _get_param(design, name)
    if not p: return False
    if isinstance(value, str) and value.strip():
        try:
            p.expression = value
            return True
        except: pass
    try:
        p.value = float(value)
        return True
    except: pass
    return False

def _col_by_name(table, name: str):
    cols = getattr(table, "columns", None)
    if not cols: return None
    try:
        c = getattr(cols, "itemByName", None)
        if callable(c):
            got = c(name)
            if got: return got
    except: pass
    try:
        for i in range(getattr(cols, "count", 0) or 0):
            col = cols.item(i)
            nm = (getattr(col, "name", None) or getattr(col, "label", None) or "").strip()
            if nm and nm.lower() == name.lower():
                return col
    except: pass
    return None

def _cell_from_row_and_column(row, column, col_name: str = None):
    if not row: return None
    try:
        if col_name:
            cbn = getattr(row, "cellByName", None)
            if callable(cbn):
                c = cbn(col_name)
                if c: return c
    except: pass
    try:
        gc = getattr(row, "getCell", None)
        if callable(gc) and column:
            c = gc(column)
            if c: return c
    except: pass
    try:
        gcc = getattr(column, "getCell", None)
        if callable(gcc):
            c = gcc(row)
            if c: return c
    except: pass
    try:
        cells = getattr(row, "cells", None)
        if cells and col_name:
            byn = getattr(cells, "itemByName", None)
            if callable(byn):
                c = byn(col_name)
                if c: return c
    except: pass
    return None

def _read_cell_value(cell):
    if not cell: return None
    for attr in ("expression", "text", "stringValue"):
        try:
            v = getattr(cell, attr, None)
            if v not in (None, ""): return v
        except: pass
    for attr in ("value", "numericValue", "quantity"):
        try:
            v = getattr(cell, attr, None)
            if hasattr(v, "value"): v = v.value
            if v not in (None, ""): return v
        except: pass
    try:
        s = str(cell)
        return s if s not in ("", "None") else None
    except: return None

def _write_cell_value(cell, value):
    if not cell: return False
    if isinstance(value, str) and value.strip():
        for a in ("expression", "text", "stringValue"):
            try:
                setattr(cell, a, value)
                return True
            except: pass
    for a in ("value", "numericValue"):
        try:
            setattr(cell, a, value)
            return True
        except: pass
    for m in ("setExpression", "setValue"):
        try:
            fn = getattr(cell, m, None)
            if callable(fn):
                fn(value)
                return True
        except: pass
    return False

def _capture_piOD_to_json(design: adsk.fusion.Design) -> None:
    """
    Read the PI model's 'piOD' parameter, convert to float inches,
    and patch it into etdp_inputs.json as 'piOD'.
    """
    try:
        raw = _read_param_value(design, "piOD")
        val = _parse_inch_value(raw)

        ou.log(f"[pi/json] piOD raw='{raw}' parsed='{val}'")

        if val is not None:
            _update_inputs_json({"piOD": val})
        else:
            ou.log("[pi/json] piOD could not be parsed; JSON not updated.")
    except Exception as ex:
        ou.log(f"[pi/json] exception while capturing piOD: {ex}")

def _snapshot_param_values(design: adsk.fusion.Design,
                           names=("diOD","diID","diLen")) -> tuple[dict, str]:
    out = {nm: _read_param_value(design, nm) for nm in names}
    tbl = _get_top_config_table_literal(design)
    row = getattr(tbl, "activeRow", None) if tbl else None
    try:
        row_name = getattr(row, "name", None) or getattr(row, "label", None)
    except: row_name = None

    missing = [nm for nm,v in out.items() if v in (None, "")]
    if missing and tbl and row:
        for nm in missing:
            col = _col_by_name(tbl, nm)
            cell = _cell_from_row_and_column(row, col, nm)
            out[nm] = _read_cell_value(cell)
    return out, row_name

def _apply_param_values_to_active_row(design: adsk.fusion.Design,
                                      values: dict,
                                      names=("diOD","diID","diLen")) -> bool:
    ok_all = True
    for nm in names:
        v = values.get(nm, None)
        if v in (None, ""):
            ok_all = False
            continue
        if not _write_param_value(design, nm, v):
            ok_all = False

    if not ok_all:
        tbl = _get_top_config_table_literal(design)
        row = getattr(tbl, "activeRow", None) if tbl else None
        if row:
            for nm in names:
                v = values.get(nm, None)
                if v in (None, ""): 
                    continue
                if _read_param_value(design, nm) != v:
                    col = _col_by_name(tbl, nm)
                    cell = _cell_from_row_and_column(row, col, nm)
                    if not _write_cell_value(cell, v):
                        ok_all = False
    return ok_all

def list_available_config_rows(occ: adsk.fusion.Occurrence) -> List[str]:
    names: List[str] = []
    try:
        df = getattr(occ, "configuredDataFile", None)
        ctt = getattr(df, "configurationTopTable", None) if df else None
        rows = getattr(ctt, "rows", None) if ctt else None
        if rows and hasattr(rows, "count"):
            for i in range(rows.count):
                nm = getattr(rows.item(i), "name", None)
                if nm: names.append(str(nm))
    except Exception as ex:
        ou.log(f"[rows] via configuredDataFile failed: {ex}")

    if not names:
        try:
            comp = getattr(occ, "component", None)
            dsn  = getattr(comp, "parentDesign", None) if comp else None
            ctt2 = getattr(dsn, "configurationTopTable", None) if dsn else None
            rows2 = getattr(ctt2, "rows", None) if ctt2 else None
            if rows2 and hasattr(rows2, "count"):
                for i in range(rows2.count):
                    nm = getattr(rows2.item(i), "name", None)
                    if nm: names.append(str(nm))
        except Exception as ex:
            ou.log(f"[rows] via parentDesign failed: {ex}")

    ou.log(f"[rows] available={names}")
    return names

# ──────────────────────────────────────────────────────────────────────────────
# Direct switch helper bits (unchanged)
# ──────────────────────────────────────────────────────────────────────────────
def _sleep_events(dt: float):
    try:
        adsk.doEvents()
        vp = adsk.core.Application.get().activeViewport
        if vp: vp.refresh()
    except: pass
    time.sleep(dt)

def _latest_df(df):
    # kept but no longer relied on for final opening
    try:
        vers = getattr(df, "versions", None)
        if vers and getattr(vers, "count", 0) > 0:
            vv = vers.item(vers.count - 1)
            return getattr(vv, "dataFile", None) or df
        return df
    except:
        return df

def _find_row_in_table(rows, desired_row: str):
    import re as _re
    want  = (desired_row or "").strip()
    wantl = want.lower()

    try:
        byname = getattr(rows, "itemByName", None)
        if callable(byname):
            r = rows.itemByName(want)
            if r: return r
    except: pass

    try:
        for i in range(rows.count):
            r = rows.item(i)
            nm = str(getattr(r, "name", "")).strip()
            if nm.lower() == wantl:
                return r
    except: pass

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

    try:
        for i in range(rows.count):
            r = rows.item(i)
            nm = str(getattr(r, "name", "")).strip()
            if wantl in nm.lower():
                return r
    except: pass
    return None

# ──────────────────────────────────────────────────────────────────────────────
# Export drawing (unchanged)
# ──────────────────────────────────────────────────────────────────────────────
def export_active_drawing_hardcoded(filepath) -> bool:
    try:
        app = adsk.core.Application.get()
        doc = app.activeDocument
        if not doc:
            ou.log("[export] No active document.")
            return False
        ext = (getattr(doc.dataFile, "fileExtension", "") or "").lower()
        if ext != "f2d":
            ou.log(f"[export] Active doc not a drawing (ext='{ext}').")
            return False

        out_path = filepath
        try:
            mgr = getattr(app, "exportManager", None)
            if mgr and hasattr(mgr, "createPDFExportOptions"):
                opts = mgr.createPDFExportOptions(out_path)
                ok = bool(mgr.execute(opts))
                if ok:
                    ou.log(f"[export] PDF via app.exportManager → {out_path}")
                    return True
        except Exception as e:
            ou.log(f"[export] app.exportManager failed: {e}")

        try:
            prod = getattr(app, "activeProduct", None)
            if prod and hasattr(prod, "exportManager"):
                dmgr = prod.exportManager
                if dmgr and hasattr(dmgr, "createPDFExportOptions"):
                    opts = dmgr.createPDFExportOptions(out_path)
                    ok = bool(dmgr.execute(opts))
                    if ok:
                        ou.log(f"[export] PDF via drawing exportManager → {out_path}")
                        return True
        except Exception as e:
            ou.log(f"[export] drawing exportManager failed: {e}")

        ou.log("[export] No drawing export API.")
        return False
    except Exception as e:
        ou.log(f"[export] exception: {e}")
        return False

# ──────────────────────────────────────────────────────────────────────────────
# Punch Insert helpers (insert-column path, with true-latest opens)
# ──────────────────────────────────────────────────────────────────────────────
def _find_recess_occurrence_by_name(root: adsk.fusion.Component, token: str):
    try:
        tok = (token or "").strip().upper()
        if not tok or not root:
            return None
        occs = getattr(root, 'allOccurrences', None)
        if not occs:
            return None
        for occ in occs:
            try:
                if getattr(occ, 'isReferencedComponent', False):
                    nm = (getattr(occ, 'name', '') or '').upper()
                    if tok in nm:
                        return occ
            except:
                pass
    except:
        pass
    return None

def _find_recess_occurrence_by_name_any_token(root: adsk.fusion.Component, tokens: List[str]):
    for t in (tokens or []):
        o = _find_recess_occurrence_by_name(root, t)
        if o: return o
    return None

def _suffix_from_recess_name(recess: str) -> str:
    try:
        return recess.strip().split("-")[-1]
    except Exception:
        return recess.strip()

def _try_save_active_document(desc: str = "Punch Insert save") -> bool:
    try:
        app = adsk.core.Application.get()
        doc = app.activeDocument
        if not doc:
            return False
        try:
            adsk.doEvents()
            vp = app.activeViewport
            if vp: vp.refresh()
            time.sleep(0.05)
        except Exception:
            pass
        saved = False
        try:
            doc.save(desc or "Auto-save")
            saved = True
        except TypeError:
            try:
                doc.save("Auto-save")
                saved = True
            except Exception as e:
                ou.log(f"[pi/save] save(desc) variants failed: {e}")
        except Exception as e:
            ou.log(f"[pi/save] save threw: {e}")
        try:
            adsk.doEvents()
            vp = app.activeViewport
            if vp: vp.refresh()
            time.sleep(0.03)
        except Exception:
            pass
        return saved
    except Exception as e:
        ou.log(f"[pi/save] exception: {e}")
        return False

def _owner_row_from_pi_config(pi_cfg: str) -> str:
    s = (pi_cfg or "").strip()
    if not s:
        return ""
    return s[3:] if s.upper().startswith("PI-") else s

def _activate_config_row_by_name(design, name: str) -> bool:
    try:
        tbl = getattr(design, "configurationTopTable", None)
        rows = getattr(tbl, "rows", None) if tbl else None
        if not rows or getattr(rows, "count", 0) == 0:
            ou.log("[pi] no config rows"); return False
        for i in range(rows.count):
            r = rows.item(i)
            if (getattr(r, "name", "") or "").strip() == name:
                r.activate()
                adsk.doEvents(); time.sleep(0.05)
                ok = bool(tbl.activeRow and tbl.activeRow.name == name)
                ou.log(f"[pi] activate '{name}' → {ok}")
                return ok
        want = name.strip().lower()
        for i in range(rows.count):
            r = rows.item(i); nm = (getattr(r, "name", "") or "").strip()
            if nm.lower() == want:
                r.activate(); adsk.doEvents(); time.sleep(0.05)
                ok = bool(tbl.activeRow and tbl.activeRow.name == nm)
                ou.log(f"[pi] activate (ci) '{nm}' → {ok}")
                return ok
        ou.log(f"[pi] row '{name}' not found")
        return False
    except Exception as e:
        ou.log(f"[pi] activate by name error: {e}")
        return False

def _copy_active_row_to(design, target_name: str) -> bool:
    try:
        tbl = getattr(design, "configurationTopTable", None)
        if not tbl: 
            ou.log("[pi] no configurationTopTable"); return False
        rows = getattr(tbl, "rows", None)
        if not rows or rows.count == 0:
            ou.log("[pi] table has zero rows"); return False
        for i in range(rows.count):
            r = rows.item(i)
            if (getattr(r, "name", "") or "").strip() == target_name:
                r.activate(); adsk.doEvents(); time.sleep(0.05)
                ok = bool(tbl.activeRow and tbl.activeRow.name == target_name)
                ou.log(f"[pi] target row existed → activate '{target_name}' → {ok}")
                return ok
        source = getattr(tbl, "activeRow", None)
        if not source:
            ou.log("[pi] no activeRow to copy from"); return False
        try:
            source.activate(); adsk.doEvents(); time.sleep(0.05)
        except Exception as e:
            ou.log(f"[pi] re-activate source failed: {e}")
        new_row = None
        for meth in ("copy", "duplicate", "clone"):
            fn = getattr(source, meth, None)
            if callable(fn):
                try:
                    new_row = fn(target_name)
                except TypeError:
                    maybe = fn()
                    if maybe:
                        try: setattr(maybe, "name", target_name)
                        except Exception: pass
                        new_row = maybe
                break
        if not new_row:
            ou.log("[pi] could not copy active row"); return False
        adsk.doEvents(); time.sleep(0.05)
        try:
            new_row.activate(); adsk.doEvents(); time.sleep(0.05)
        except Exception as e:
            ou.log(f"[pi] activate new row failed: {e}")
        ok = bool(tbl.activeRow and tbl.activeRow.name == target_name)
        ou.log(f"[pi] created & activated '{target_name}' → {ok}")
        return ok
    except Exception as e:
        ou.log(f"[pi] copy row exception: {e}\n{traceback.format_exc()}")
        return False

def _same_occ(a, b) -> bool:
    if a is None or b is None: return False
    if a is b: return True
    for attr in ("entityToken", "fullPathName"):
        try:
            if getattr(a, attr, None) and getattr(b, attr, None) and getattr(a, attr) == getattr(b, attr):
                return True
        except: pass
    return (getattr(a, "name", None) or "") == (getattr(b, "name", None) or "")

def _ensure_active_row(table):
    try:
        if getattr(table, "activeRow", None):
            return table.activeRow
    except: pass
    try:
        rows = getattr(table, "rows", None)
        if rows and rows.count > 0:
            r0 = rows.item(0)
            try:
                r0.activate()
                _pump(0.03)
            except: pass
            return r0
    except: pass
    return None

def _find_insert_col_for_occ(top, occ):
    try:
        cols = getattr(top, "columns", None)
        if not cols: return None
        # exact match by column.occurrence
        for ci in range(cols.count):
            col = cols.item(ci)
            if "ConfigurationInsertColumn" not in (getattr(col, "objectType", "") or ""):
                continue
            try:
                cocc = getattr(col, "occurrence", None)
            except:
                cocc = None
            if cocc and _same_occ(cocc, occ):
                ou.log(f"[insert] matched Insert column to occ '{getattr(occ,'name','')}'")
                return col
        # fallback: only insert column present
        only = None
        for ci in range(cols.count):
            col = cols.item(ci)
            if "ConfigurationInsertColumn" in (getattr(col, "objectType", "") or ""):
                if only is None: only = col
                else: only = None; break
        if only:
            ou.log("[insert] using single Insert column fallback")
            return only
    except: pass
    return None

def _get_insert_cell(top, insert_col, pi_row_name: str):
    cell = None
    if hasattr(insert_col, "getCellByRowName"):
        try: cell = insert_col.getCellByRowName(pi_row_name)
        except: cell = None
    if not cell:
        rid = None
        rows = getattr(top, "rows", None)
        if rows:
            for i in range(rows.count):
                r = rows.item(i)
                if (_safe_name(r) == (pi_row_name or "").strip()):
                    rid = getattr(r, "id", None); break
        if rid is not None:
            try: cell = insert_col.getCellByRowId(rid)
            except: cell = None
    return cell

def _set_insert_cell_to_rowname(top, insert_col, pi_row_name: str, owner_row) -> bool:
    try:
        cell = _get_insert_cell(top, insert_col, pi_row_name)
        if not cell:
            ou.log("[insert] ❌ could not get Insert cell for PI row")
            return False

        # Prefer setting by ConfigurationRow
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

        # Fallback to row name
        target_name = (_safe_name(owner_row)).strip()
        for attr in ("setByConfigurationRowName", "selectedName", "stringValue", "text", "value", "expression"):
            try:
                if hasattr(cell, attr):
                    fn = getattr(cell, attr)
                    if callable(fn):
                        fn(target_name)
                    else:
                        setattr(cell, attr, target_name)
                    return True
            except: pass

        ou.log("[insert] ❌ failed to apply owner row by any method")
        return False
    except Exception as e:
        ou.log(f"[insert] exception: {e}\n{traceback.format_exc()}")
        return False

def _verify_insert_cell(top, insert_col, pi_row_name: str, expect_name: str) -> bool:
    try:
        cell = _get_insert_cell(top, insert_col, pi_row_name)
        if not cell: return False
        exp = (expect_name or "").strip().lower()

        # Try to read back what's selected
        try:
            rn = getattr(getattr(cell, "row", None), "name", None)
            if rn and rn.strip().lower() == exp:
                return True
        except: pass
        for attr in ("selectedName", "stringValue", "text", "value", "expression"):
            try:
                val = getattr(cell, attr, None)
                if callable(val):
                    val = val()
                if isinstance(val, str) and val.strip().lower() == exp:
                    return True
            except: pass
    except: pass
    return False

def _maybe_switch_via_owner_direct(design, occ, owner_target_row: str,
                                   anchor_label: str,
                                   tries: int, base_s: float,
                                   occ_tokens: List[str]) -> bool:
    # First try as-is (no move)
    try:
        res = sw.switch_linked_occurrence_direct(
            occ,
            owner_target_row,
            probe_only=False,
            retry_tries=int(tries),
            retry_base_s=float(base_s)
        )
        ou.log(f"[pi/fallback] direct switch (no-move) → {res}")
        if res.get("ok"):
            return True
    except Exception as e:
        ou.log(f"[pi/fallback] exception (no-move): {e}")

    # Lazy move before anchor, then retry once
    try:
        moved = ou.move_timeline_marker_to(
            design,
            query=anchor_label,
            position="before",
            mode="contains",
            nth="last"
        )
        if moved.get("ok"):
            _yield(0.06)
            occ2 = _find_recess_occurrence_by_name_any_token(design.rootComponent, occ_tokens) or occ

            res2 = sw.switch_linked_occurrence_direct(
                occ2,
                owner_target_row,
                probe_only=False,
                retry_tries=int(tries),
                retry_base_s=float(base_s)
            )
            ou.log(f"[pi/fallback] direct switch (after-move) → {res2}")
            return bool(res2.get("ok"))
    except Exception as e:
        ou.log(f"[pi/fallback] exception (after-move): {e}")

    return False

def _activate_doc_for_datafile_id(app: adsk.core.Application, df_id: str):
    """Activate any open document whose DataFile.id matches df_id."""
    try:
        for i in range(app.documents.count):
            d = app.documents.item(i)
            did = getattr(getattr(d, 'dataFile', None), 'id', None)
            if did and df_id and did == df_id:
                try:
                    d.activate()
                    try: adsk.doEvents()
                    except: pass
                    try: app.activeViewport.refresh()
                    except: pass
                    time.sleep(0.05)
                except: pass
                return d
    except: pass
    return None

# ──────────────────────────────────────────────────────────────────────────────
# Punch Insert flow (true-latest opens for PI and for owner; keep owner open)
# ──────────────────────────────────────────────────────────────────────────────
def do_punch_insert_flow(inp: Dict[str, Any]) -> str:
    model_name   = str(inp.get("pi_model_name", "PI-MT-STYLE-DELTA"))
    folder_hint  = inp.get("pi_folder_hint") or ["PI-OD-LEN-STYLE"]
    project_hint = inp.get("pi_project_hint") or inp.get("project_hint")

    # STRICT: must come from JSON, no hard-coded family defaults
    pi_base_raw   = str(inp.get("pi_base_config") or "").strip()
    pi_recess_raw = str(inp.get("pi_recess_name") or "").strip()

    if not pi_base_raw or not pi_recess_raw:
        msg = (
            "[pi] ERROR: missing pi_base_config and/or pi_recess_name in JSON. "
            f"pi_base_config='{pi_base_raw}', pi_recess_name='{pi_recess_raw}'"
        )
        ou.log(msg)
        return msg

    # Normalize base, PI row, and owner row (handles suffix logic)
    base_config_name, new_pi_row, owner_target_row = _normalize_pi_and_recess_names(
        pi_base_raw,
        pi_recess_raw
    )

    anchor_label = str(inp.get("pi_timeline_anchor") or "RemoveInstance")
    keep_owner   = bool(inp.get("pi_keep_owner_open", True))

    # occurrence tokens (for finding the recess occurrence in PI model)
    tokens: list[str] = []
    try:
        first = owner_target_row.split("-")[0]            # e.g. MT
        fam   = "-".join(owner_target_row.split("-")[:3]) # e.g. MT-1-11
        for t in (inp.get("pi_occurrence_token"), first, fam):
            if t and t not in tokens:
                tokens.append(str(t))
    except:
        pass

    ou.log(f"[pi] owner target row = '{owner_target_row}'")
    ou.log(f"[pi] PI row           = '{new_pi_row}'")
    ou.log(f"[pi] base config      = '{base_config_name}'")
    ou.log(f"[pi] occurrence tokens = {tokens}")

    # 1) Open PI (via open_utils), then FORCE TRUE LATEST if needed
    product, doc, meta = ou.open_design(
        target_name=model_name,
        project_hint=project_hint,
        folder_hint=folder_hint,
        ext="f3d",
        match_mode="literal",
        search_timeout_s=float(inp.get("search_timeout_s", 40.0)),
        max_files_scanned=int(inp.get("max_files_scanned", 50000)),
        debug_scan=bool(inp.get("debug_scan", False))
    )
    if not product or not doc:
        return f"Punch Insert open FAILED for '{model_name}'. See log: {ou.LOG_PATH}"

    design = adsk.fusion.Design.cast(product)
    # upgrade PI tab to true latest if necessary
    design, doc = _reopen_doc_to_true_latest_if_needed(doc)

    # 2) Activate base PI config (from JSON)
    if not _activate_config_row_by_name(design, base_config_name):
        return f"Base config '{base_config_name}' not found/activated."

    # 3) Copy → new PI config & activate (if it doesn't already exist)
    if not _copy_active_row_to(design, new_pi_row):
        return f"Failed to create/activate '{new_pi_row}'."

    ou.log(f"[pi] owner target row: '{owner_target_row}'  (PI row: '{new_pi_row}')")


    # Table & PI row
    top = _get_top_config_table_literal(design)
    if not top:
        return "[pi] No configurationTopTable on PI."
    pi_row = _ensure_active_row(top)
    if not pi_row:
        return "[pi] No active PI row available."

    # Find the recess occurrence (prefer Insert-columns; else by token)'''
    '''pi_occ = None
    try:
        cols = getattr(top, "columns", None)
        if cols:
            for ci in range(cols.count):
                col = cols.item(ci)
                if "ConfigurationInsertColumn" in (getattr(col, "objectType", "") or ""):
                    occ = getattr(col, "occurrence", None)
                    if occ and getattr(occ, "isReferencedComponent", False):
                        nm = (_safe_name(occ)).upper()
                        if any(t.upper() in nm for t in tokens if t):
                            pi_occ = occ
                            break
    except: pass
    if not pi_occ:
        pi_occ = _find_recess_occurrence_by_name_any_token(design.rootComponent, tokens)
'''
    # Move timeline marker before the RemoveInstance anchor, so config changes are “in effect”
    moved = ou.move_timeline_marker_to(
        design,
        query=anchor_label,
        position="before",
        mode="contains",
        nth="last"
    )

    # For this PI flow, we rely on our own Insert-column logic
    # and do NOT let Recess_Handle modify the PI table.
    # If you want, you can keep this commented as a reminder:
    #
    # try:
    #     Recess_Handle.enter_recess(
    #         inp.get("pi_occurrence_token", "MT"),
    #         owner_target_row
    #     )
    # except Exception as e:
    #     ou.log(f"[pi] Recess_Handle.enter_recess error: {e}")


    # Now try to find the recess occurrence by tokens in the PI model
    pi_occ = _find_recess_occurrence_by_name_any_token(design.rootComponent, tokens)

    ou.log(f"[pi] target recess occ: '{_safe_name(pi_occ)}'")

    if pi_occ:
        _refresh_occurrence_to_latest(pi_occ)
    else:
        ou.log("[pi] WARNING: recess occurrence not found in PI model")

    # Try to get the Insert column for this occ without moving first
    ins_col = _find_insert_col_for_occ(top, pi_occ)
    if not ins_col:
        moved = ou.move_timeline_marker_to(design, query=anchor_label, position="before", mode="contains", nth="last")
        if moved.get("ok"):
            _yield(0.06)
            top    = _get_top_config_table_literal(design)
            pi_row = _ensure_active_row(top)
            pi_occ = _find_recess_occurrence_by_name_any_token(design.rootComponent, tokens) or pi_occ
            ins_col= _find_insert_col_for_occ(top, pi_occ)

    if not ins_col:
        ou.log("[pi] ❌ Insert column for occurrence not found.")
        return "[pi] Insert column not found."

    # Save PI doc id so we can reactivate after we open owner
    app = adsk.core.Application.get()
    pi_df_id = getattr(getattr(doc, "dataFile", None), "id", None)

    # 4) Open OWNER at TRUE LATEST and resolve row (keep open)
    df_owner = getattr(pi_occ, 'configuredDataFile', None) or getattr(getattr(pi_occ, 'component', None), 'dataFile', None)
    if not df_owner:
        return "[pi] No owner DataFile found on recess occurrence."

    owner_des, owner_doc, latest_num, opened_num = _open_doc_true_latest(df_owner, activate=True)
    ou.log(f"[pi/owner] latest={latest_num}, opened={opened_num}")

    try:
        try:
            cm = getattr(owner_des, "configurationManager", None)
            if cm and hasattr(cm, "refreshTables"):
                cm.refreshTables(); _yield(0.08)
        except: pass

        owner_tbl = getattr(owner_des, "configurationTopTable", None)
        if not owner_tbl or not getattr(owner_tbl, "rows", None):
            return "[pi] Owner has no configurationTopTable."
        owner_row = _find_row_in_table(owner_tbl.rows, owner_target_row)
        if not owner_row:
            return f"[pi] Owner row '{owner_target_row}' not found."

        # 5) Switch back to PI BEFORE writing PI's Insert cell
        if pi_df_id:
            _activate_doc_for_datafile_id(app, pi_df_id)
            _yield(0.08)

        # Rebind PI handles after activation
        design = adsk.fusion.Design.cast(app.activeProduct)
        design, doc = _reopen_doc_to_true_latest_if_needed(doc)  # (should already be latest; harmless if so)

        top = _get_top_config_table_literal(design)
        if not top:
            return "[pi] No configurationTopTable on PI after reactivation."

        # Make absolutely sure our intended PI row is the active one
        if not _activate_config_row_by_name(design, new_pi_row):
            ou.log(f"[pi] WARNING: could not re-activate PI row '{new_pi_row}' after owner open")

        # Re-find occurrence & Insert column in this PI context
        pi_occ = _find_recess_occurrence_by_name_any_token(design.rootComponent, tokens) or pi_occ
        ins_col = _find_insert_col_for_occ(top, pi_occ) or ins_col
        if not ins_col:
            return "[pi] Insert column not found after reactivation."


                # 6) Robust set with retries + verification
        ok = False
        for attempt in range(1, 7):
            try:
                # Use the explicit PI row name we derived earlier
                ok = _set_insert_cell_to_rowname(top, ins_col, new_pi_row, owner_row)
                _yield(0.06)
                ok = ok and _verify_insert_cell(top, ins_col, new_pi_row, _safe_name(owner_row))
            except Exception as e:
                ou.log(f"[pi] set Insert attempt {attempt} exception: {e}")
                ok = False
            ou.log(f"[pi] set Insert → attempt {attempt} → {'OK' if ok else 'FAIL'}")
            if ok:
                break

            # Retry: keep events pumping, refresh tables & handles
            try:
                adsk.doEvents()
                app.activeViewport.refresh()
            except:
                pass
            time.sleep(0.15)
            try:
                cm = getattr(owner_des, "configurationManager", None)
                if cm and hasattr(cm, "refreshTables"):
                    cm.refreshTables()
            except:
                pass
            top    = _get_top_config_table_literal(design)
            ins_col= _find_insert_col_for_occ(top, pi_occ) or ins_col

        if not ok:
            tries  = int(inp.get("owner_retry_tries", 6))
            base_s = float(inp.get("owner_retry_base_s", 0.18))
            ok = _maybe_switch_via_owner_direct(
                design, pi_occ, owner_target_row, anchor_label, tries, base_s, tokens
            )
            ou.log(f"[pi] fallback direct switch → {'OK' if ok else 'FAILED'}")

        if ok:
            _refresh_occurrence_to_latest(pi_occ)
            _try_save_active_document(f"Punch Insert: set recess to {owner_target_row}")

            # NEW: capture piOD from the PI model and push it into JSON
            try:
                _capture_piOD_to_json(design)
            except Exception as e:
                ou.log(f"[pi/json] failed to capture piOD: {e}")
        else:
            return f"[pi] Unable to set recess to '{owner_target_row}' (insert+fallback failed)."


    finally:
        # KEEP owner open unless disabled
        if not keep_owner:
            try: owner_doc.close(False)
            except: pass

    opened = doc.dataFile.name if getattr(doc, "dataFile", None) else doc.name
    return (
        "Punch Insert flow:\n"
        f"- Opened: {opened}\n"
        f"- Base config: {base_config_name}\n"
        f"- New PI row:  {new_pi_row}\n"
        f"- Insert switch → {owner_target_row} (see log)\n"
    )


def inspect_occurrence_config(occ: adsk.fusion.Occurrence) -> dict:
    info = {}
    try:
        info["occ_name"] = getattr(occ, "name", None)
        info["full_path"] = getattr(occ, "fullPathName", None)

        comp = getattr(occ, "component", None)
        info["component_name"] = getattr(comp, "name", None) if comp else None

        try:
            info["isConfiguration_flag"] = bool(getattr(occ, "isConfiguration", None))
        except:
            info["isConfiguration_flag"] = None

        df = None
        try:
            df = getattr(occ, "configuredDataFile", None)
        except:
            df = None
        info["configuredDataFile_name"] = getattr(df, "name", None) if df else None

        try:
            app = adsk.core.Application.get()
            des = adsk.fusion.Design.cast(app.activeProduct)
            tl = getattr(des, "timeline", None)
            if tl:
                for i in range(getattr(tl, "count", 0)):
                    tlo = tl.item(i)
                    if getattr(tlo, "entity", None) is occ:
                        info["timeline_name"]  = getattr(tlo, "name", None)
                        info["timeline_index"] = i
                        break
        except:
            pass

        import re as _re
        def _guess(txt: str):
            if not txt:
                return None
            cand = None
            for m in _re.finditer(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+", txt):
                s = m.group(0)
                if not cand or len(s) > len(cand):
                    cand = s
            return cand

        guesses = []
        for s in (info.get("occ_name"), info.get("component_name"), info.get("timeline_name")):
            g = _guess(s)
            if g and g not in guesses:
                guesses.append(g)

        info["guessed_config"] = guesses[0] if guesses else None
        info["all_guesses"]    = guesses

        try:
            ou.log(f"[inspect] occ='{info.get('occ_name')}', comp='{info.get('component_name')}', "
                   f"df='{info.get('configuredDataFile_name')}', guess='{info.get('guessed_config')}', "
                   f"all={guesses}")
        except:
            pass

        return info
    except Exception as ex:
        try:
            ou.log(f"[inspect] error: {ex}\n{traceback.format_exc()}")
        except:
            pass
        return {"error": str(ex)}

# ──────────────────────────────────────────────────────────────────────────────
# Main run
# ──────────────────────────────────────────────────────────────────────────────
_size_rx = re.compile(r'-([0-9]{2})(?:MT)?-', re.IGNORECASE)
def _extract_size_digits_from_config_name(config_name: str) -> str | None:
    if not config_name:
        return None
    m = _size_rx.search(config_name)
    return m.group(1) if m else None

def _derive_di_target_from_pm(config_name: str) -> str | None:
    if not config_name:
        return None
    parts = [p.strip() for p in config_name.split('-')]
    if len(parts) < 6:
        return None
    di_form = 'B2DI' if parts[1].upper() == 'B2PM' else parts[1]
    size_digits = parts[2].upper().replace('MT', '')
    return f"{parts[0]}-{di_form}-{size_digits}-{parts[3]}-{parts[4]}-{parts[5]}"

def _activate_di_row_for_size(design: adsk.fusion.Design, size_digits: str) -> bool:
    table = getattr(design, 'configurationTopTable', None)
    rows  = getattr(table, 'rows', None) if table else None
    if not rows:
        return False
    candidate = None
    for i in range(getattr(rows, 'count', 0) or 0):
        r = rows.item(i)
        nm = (getattr(r, 'name', None) or getattr(r, 'label', None) or '').strip()
        if not nm: 
            continue
        toks = nm.split('-')
        if len(toks) < 6:
            continue
        if toks[1].upper() == 'B2DI' and toks[2] == size_digits:
            candidate = r
            break
    if candidate:
        try:
            candidate.activate()
            try:
                adsk.doEvents()
                adsk.core.Application.get().activeViewport.refresh()
            except: 
                pass
            time.sleep(0.05)
            return bool(table.activeRow and table.activeRow.name == getattr(candidate, 'name', ''))
        except:
            return False
    return False

def run_insert_code() -> str:
    """
    Callable entrypoint for InsertCode.

    - Reads all inputs from etdp_inputs.json via read_inputs()
    - Opens documents itself using ou.open_and_prepare()
    - Performs B2DI config work, drawing update/export, and PI flow
    - Returns a short status string; details go to ou.log
    """

    ui = None

    try:
        app = adsk.core.Application.get()
        ui  = app.userInterface

        # 🔑 SINGLE SOURCE OF TRUTH
        inp = read_inputs()
        suppress_ui = bool(inp.get("suppress_ui_messages", True))

        # ─────────────────────────────────────────────────────────
        # 1) OPEN TARGET DESIGN
        # ─────────────────────────────────────────────────────────
        design, doc, meta, update_report, move_result, occ = ou.open_and_prepare(
            target_name=inp["target_name"],
            project_hint=inp["project_hint"],
            folder_hint=inp["folder_hint"],
            ext=inp["ext"],
            match_mode=inp["match_mode"],
            search_timeout_s=float(inp["search_timeout_s"]),
            max_files_scanned=int(inp["max_files_scanned"]),
            debug_scan=bool(inp["debug_scan"]),
            update_to_latest_first=bool(inp.get("update_to_latest_first", False)),
            update_timeout_s=float(inp.get("update_timeout_s", 120.0)),
            latest_strategy=str(inp.get("latest_strategy", "api_then_ui")),
            force_ui_get_latest=bool(inp.get("force_ui_get_latest", False)),
            move_timeline=True,
            timeline_query=inp["timeline_query"],
            timeline_position=inp["timeline_position"],
            timeline_mode=inp["timeline_mode"],
            timeline_nth=inp["timeline_nth"],
        )

        if not doc:
            msg = "InsertCode: open failed (see log)"
            if ui and not suppress_ui:
                ui.messageBox(msg)
            return msg

        opened = doc.dataFile.name if getattr(doc, "dataFile", None) else doc.name
        ou.log(f"[InsertCode] Opened: {opened}")

        # ─────────────────────────────────────────────────────────
        # 1a) Ensure correct DI source row (size match)
        # ─────────────────────────────────────────────────────────
        cfg_name = str(inp.get("config_name", "") or "")
        size_digits = _extract_size_digits_from_config_name(cfg_name)

        if size_digits:
            ok = _activate_di_row_for_size(design, size_digits)
            ou.log(f"[InsertCode] DI source row size '{size_digits}' → {'OK' if ok else 'NOT FOUND'}")
        else:
            ou.log("[InsertCode] No size parsed from config_name; using active row")

        # ─────────────────────────────────────────────────────────
        # 1b) Create / activate target DI row
        # ─────────────────────────────────────────────────────────
        table = getattr(design, "configurationTopTable", None)
        if not table or not table.activeRow:
            return "InsertCode: no active configuration row"

        target_name = _derive_di_target_from_pm(cfg_name) or "KHF-B2DI-25-PT-FLT-3"
        active_name = (table.activeRow.name or "").strip()

        if active_name != target_name:
            src = table.activeRow
            src.activate()
            adsk.doEvents(); time.sleep(0.05)

            try:
                new_row = src.copy(target_name)
            except TypeError:
                new_row = src.copy()
                setattr(new_row, "name", target_name)

            new_row.activate()
            adsk.doEvents(); time.sleep(0.05)

            ou.log(f"[InsertCode] Created & activated '{target_name}'")
        else:
            ou.log(f"[InsertCode] Already on '{target_name}'")

        _try_save_active_document()

        # ─────────────────────────────────────────────────────────
        # 2) POST-MOVE UPDATE TO LATEST
        # ─────────────────────────────────────────────────────────
        try:
            doc.updateAllReferences()
            ou.log("[InsertCode] doc.updateAllReferences()")
        except Exception as e:
            ou.log(f"[InsertCode] updateAllReferences failed: {e}")

        # ─────────────────────────────────────────────────────────
        # 3) LINKED OCCURRENCE SWITCH
        # ─────────────────────────────────────────────────────────
        if move_result and move_result.get("ok"):
            idx = move_result["match"]["index"]
            occ = ou.occ_from_timeline_index(design, idx)

            if occ and bool(inp.get("attempt_switch", False)):
                desired = str(inp.get("config_name", "") or "")
                res = sw.switch_linked_occurrence_direct(
                    occ,
                    desired,
                    probe_only=bool(inp.get("probe_only", False)),
                    retry_tries=int(inp.get("owner_retry_tries", 6)),
                    retry_base_s=float(inp.get("owner_retry_base_s", 0.18)),
                )
                ou.log(f"[InsertCode] switch result: {res}")

        # ─────────────────────────────────────────────────────────
        # 4) FINAL SAVE (IMPORTANT FOR DRAWINGS)
        # ─────────────────────────────────────────────────────────
        save_doc_and_wait_for_new_version(
            doc,
            desc="InsertCode final save before drawing",
            timeout_s=45.0,
        )

        # ─────────────────────────────────────────────────────────
        # 5) OPTIONAL DRAWING OPEN + EXPORT
        # ─────────────────────────────────────────────────────────
        if bool(inp.get("open_related_drawing", False)):
            model_name = doc.dataFile.name if getattr(doc, "dataFile", None) else doc.name

            ou.open_and_refresh_drawing_for_model(
                model_target_name=inp.get("target_name", "") or model_name,
                project_hint=inp.get("project_hint"),
                drawing_folder_hint=inp.get("drawing_folder_hint") or inp.get("folder_hint"),
                match_mode=inp.get("match_mode", "literal"),
                search_timeout_s=float(inp.get("search_timeout_s", 30.0)),
                max_files_scanned=int(inp.get("max_files_scanned", 10000)),
                debug_scan=bool(inp.get("debug_scan", False)),
                update_timeout_s=float(inp.get("drawing_update_timeout_s", 120.0)),
                force_ui_get_latest=bool(inp.get("drawing_force_ui_get_latest", True)),
                model_doc_name=model_name,
                activate_drawing=True,
            )

            pdf_path = os.path.join(FILE_PATH, f"{target_name}.pdf")
            export_active_drawing_hardcoded(pdf_path)

        # ─────────────────────────────────────────────────────────
        # 6) PUNCH INSERT FLOW
        # ─────────────────────────────────────────────────────────
        try:
            report = do_punch_insert_flow(inp)
            ou.log(report.replace("\n", " | "))
        except Exception as e:
            ou.log(f"[InsertCode] Punch Insert failed: {e}")

        return "InsertCode: OK"

    except Exception:
        ou.log("Exception in run_insert_code():\n" + traceback.format_exc())
        try:
            if ui and not bool(read_inputs().get("suppress_ui_messages", True)):
                ui.messageBox("InsertCode exception:\n\n" + traceback.format_exc())
        except:
            pass
        return "InsertCode: exception"

# ------------------------------------------------------------------------------
# Legacy Fusion entrypoint (standalone use)
# ------------------------------------------------------------------------------
def run(context):
    inp = read_inputs()
    run_insert_code()


def stop(context):
    pass
