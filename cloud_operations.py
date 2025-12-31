# cloud_operations.py

from . import loggin_utils
from . import config_logic
from . import sql_interface
from . import path_utils
from . import file_ops

import adsk.core, adsk.fusion, time, traceback, math, re
from datetime import datetime

# ------------------------ small utils ------------------------

def _yield(dt=0.03):
    try:
        adsk.doEvents()
    except:
        pass
    time.sleep(dt)

def _base_from_title(title: str) -> str:
    t = (title or "").split(" (")[0]
    t = t.split(":")[0]
    return t.strip()

# ------------------------ open/refresh helpers ------------------------

def _open_design_for_datafile(df: adsk.core.DataFile):
    """Open df invisibly if not already; return (design, doc, need_close)."""
    app = adsk.core.Application.get()
    for i in range(app.documents.count):
        d = app.documents.item(i)
        try:
            if d.dataFile and d.dataFile.id == df.id:
                des = adsk.fusion.Design.cast(d.products.itemByProductType('DesignProductType'))
                return des, d, False
        except:
            pass
    doc = app.documents.open(df, False)
    try:
        doc.isVisible = False
    except:
        pass
    des = adsk.fusion.Design.cast(doc.products.itemByProductType('DesignProductType'))
    return des, doc, True

def _get_latest_datafile(df):
    """Return latest revision DataFile for df (best effort)."""
    try:
        if getattr(df, 'isLatest', None) is True:
            return df
        vers = getattr(df, 'versions', None)
        if vers and getattr(vers, 'count', 0) > 0:
            try:
                v = vers.item(vers.count - 1)
                return getattr(v, 'dataFile', None) or df
            except:
                return df
        return df
    except:
        return df

def _close_and_reopen_current() -> bool:
    """Safe reopen to pull latest links when direct replace isn’t available."""
    app = adsk.core.Application.get()
    doc = app.activeDocument
    if not doc:
        return False
    df  = getattr(doc, 'dataFile', None)
    name = doc.name
    try:
        doc.close(False); _yield(0.08)
    except Exception as e:
        loggin_utils.log(f"[reopen] close failed: {e}")
    reopened = None
    try:
        reopened = app.documents.open(df, True) if df else app.documents.open(name)
    except:
        if df:
            reopened = app.documents.open(df)
    if reopened:
        reopened.activate(); _yield(0.08)
        try: app.activeViewport.refresh()
        except: pass
        loggin_utils.log(f"[reopen] Reopened: {reopened.name}")
        return True
    loggin_utils.log("[reopen] ❌ Reopen failed")
    return False

def _refresh_occurrence_to_latest(occ) -> bool:
    """
    Update the linked occurrence to the latest version:
      1) occ.updateToLatestVersion() if available
      2) occ.replaceComponent(latest_df) if available
      3) fallback: close+reopen parent document
    """
    try:
        if hasattr(occ, "isOutOfDate") and hasattr(occ, "updateToLatestVersion"):
            try:
                if occ.isOutOfDate:
                    occ.updateToLatestVersion()
                    _yield(0.15)
                    loggin_utils.log("[latest] updateToLatestVersion() called.")
                    return True
                else:
                    loggin_utils.log("[latest] Already current (isOutOfDate == False).")
            except Exception as e:
                loggin_utils.log(f"[latest] updateToLatestVersion failed: {e}")

        df = getattr(occ, 'configuredDataFile', None) or getattr(getattr(occ, 'component', None), 'dataFile', None)
        if not df:
            loggin_utils.log("[latest] No DataFile on occurrence/component.")
            return False

        latest_df = _get_latest_datafile(df)
        ver_now   = getattr(df, 'versionNumber', None)
        ver_new   = getattr(latest_df, 'versionNumber', None)
        same_id   = getattr(df, 'id', None) == getattr(latest_df, 'id', None)
        if same_id and (ver_now == ver_new or getattr(latest_df, 'isLatest', False)):
            loggin_utils.log(f"[latest] Already at latest (v={ver_now}).")
            return True

        if hasattr(occ, "replaceComponent"):
            try:
                occ.replaceComponent(latest_df)
                _yield(0.12)
                loggin_utils.log(f"[latest] replaceComponent → v={ver_new}")
                return True
            except Exception as e:
                loggin_utils.log(f"[latest] replaceComponent failed: {e}")

        ok = _close_and_reopen_current()
        loggin_utils.log(f"[latest] Parent reopen to pull latest → {'OK' if ok else 'FAILED'}")
        return ok
    except Exception as e:
        loggin_utils.log(f"[latest] Exception: {e}\n{traceback.format_exc()}")
        return False

# ------------------------ occurrence location ------------------------

def _get_recess_occurrence_via_insert_exact(design: adsk.fusion.Design, base_title: str):
    """
    Try to locate recess via a specific ConfigurationInsertColumn base title.
    Useful if you know the column label root (rare).
    """
    top = getattr(design, "configurationTopTable", None)
    if not top:
        return None
    for ci in range(top.columns.count):
        col = top.columns.item(ci)
        if "ConfigurationInsertColumn" not in getattr(col, "objectType", ""):
            continue
        title = getattr(col, "title", f"<col {ci}>")
        if _base_from_title(title).upper() == (base_title or "").upper():
            try:
                occ = getattr(col, "occurrence", None)
                if occ and getattr(occ, "isReferencedComponent", False):
                    return occ
            except:
                return None
    return None

def _get_recess_occurrence_via_any_insert(design: adsk.fusion.Design, name_token: str = None):
    """
    Search ALL insert columns; if name_token is provided, prefer a referenced
    occurrence whose name contains that token.
    """
    top = getattr(design, "configurationTopTable", None)
    if not top:
        return None

    token_up = (name_token or "").upper().strip()
    first_match = None

    for ci in range(top.columns.count):
        col = top.columns.item(ci)
        if "ConfigurationInsertColumn" not in getattr(col, "objectType", ""):
            continue
        try:
            occ = getattr(col, "occurrence", None)
        except:
            occ = None
        if not occ or not getattr(occ, "isReferencedComponent", False):
            continue

        if token_up:
            if token_up in (occ.name or "").upper():
                return occ
            # keep first referenced occurrence as fallback
            if not first_match:
                first_match = occ
        else:
            return occ

    return first_match

def _find_recess_occurrence_by_name(root: adsk.fusion.Component, token: str):
    """Fallback search: referenced occurrence whose NAME contains token."""
    tok = (token or "").upper()
    for occ in root.allOccurrences:
        if getattr(occ, 'isReferencedComponent', False) and tok in (occ.name or "").upper():
            return occ
    return None

# ------------------------ switching helpers ------------------------

def _switch_occ_using_owner_row(occ: adsk.fusion.Occurrence, owner_tbl, target_cfg_row_name: str) -> bool:
    """
    Use the OWNER design's ConfigurationRow object for switching (most reliable).
    """
    tgt_row = owner_tbl.rows.itemByName(target_cfg_row_name)
    if not tgt_row:
        loggin_utils.log(f"[switch] Owner table has no row '{target_cfg_row_name}'.")
        return False
    try:
        occ.switchConfiguration(tgt_row)
        _yield(0.06)
        return True
    except Exception as e:
        loggin_utils.log(f"[switch] occ.switchConfiguration(owner_row) failed: {e}")
        return False

def switch_b2_recess_to_config(target_cfg_row: str,
                               base_token: str = "MT-1-11",
                               insert_base: str = None) -> bool:
    """
    In the CURRENT **B2** document:
      - locate the recess occurrence:
          1) if insert_base provided, try that insert column
          2) else scan all insert columns (prefer name contains base_token)
          3) else fall back to a global name-contains search
      - update the occurrence to latest
      - open the OWNER design behind the occurrence
      - switch using the OWNER's ConfigurationRow object for `target_cfg_row`
    Returns True on success.
    """
    try:
        app = adsk.core.Application.get()
        design = adsk.fusion.Design.cast(app.activeProduct)
        if not design:
            loggin_utils.log("[switch_b2_recess_to_config] Active product is not a Design.")
            return False

        # Confirm we are in B2
        try:
            loggin_utils.log(f"[switch] Active doc during switch: {app.activeDocument.name}")
        except:
            pass

        # Find occurrence
        occ = None
        if insert_base:
            occ = _get_recess_occurrence_via_insert_exact(design, insert_base)

        if not occ:
            occ = _get_recess_occurrence_via_any_insert(design, base_token)

        if not occ:
            loggin_utils.log(f"[switch] Insert-column lookup failed; falling back to name contains '{base_token}'")
            occ = _find_recess_occurrence_by_name(design.rootComponent, base_token)

        if not occ:
            loggin_utils.log("[switch] ❌ Recess occurrence not found.")
            return False

        loggin_utils.log(f"[switch] Target occurrence: '{occ.name}'")

        # Make sure parent sees latest version of linked recess
        _refresh_occurrence_to_latest(occ)

        # Open OWNER design and switch using its row object
        df_owner = getattr(occ, 'configuredDataFile', None) or getattr(getattr(occ, 'component', None), 'dataFile', None)
        if not df_owner:
            loggin_utils.log("[switch] ❌ No owner DataFile on occurrence/component.")
            return False

        owner_des, owner_doc, need_close = _open_design_for_datafile(df_owner)
        try:
            owner_tbl = getattr(owner_des, "configurationTopTable", None)
            if not owner_tbl:
                loggin_utils.log("[switch] ❌ Owner has no configurationTopTable.")
                return False

            ok = _switch_occ_using_owner_row(occ, owner_tbl, target_cfg_row)
            if not ok:
                # refresh once and retry
                _refresh_occurrence_to_latest(occ)
                ok = _switch_occ_using_owner_row(occ, owner_tbl, target_cfg_row)
            return ok
        finally:
            if need_close and owner_doc:
                try: owner_doc.close(False)
                except: pass

    except Exception as e:
        loggin_utils.log(f"[switch_b2_recess_to_config] Exception: {e}\n{traceback.format_exc()}")
        return False

# ------------------------ your original helpers (refined) ------------------------

def open_first_fusion_design_file(folder, event_data):
    """
    Recursively search for a B2PM .f3d based on event_data['pBucket'] and open it.
    Then update parameters from SQL.
    """
    app = adsk.core.Application.get()
    ui = app.userInterface

    p_bucket = (event_data or {}).get('pBucket', '')
    parts = p_bucket.split('-') if p_bucket else []
    if len(parts) < 5:
        loggin_utils.log(f"[open_first_fusion_design_file] Bad pBucket: {p_bucket!r}")
        return

    # Build target snippet like: KHF-B2PM-##MT-PT-FLT-#
    if len(parts[2]) >= 3:
        mid = parts[2][2:]
    else:
        mid = parts[2]
    target_snippet = f"{parts[0]}-B2PM-##{mid}-{parts[3]}-{parts[4]}-#".upper()

    def recursive_file_search(fldr):
        for file in fldr.dataFiles:
            if file.fileExtension != 'f3d':
                continue
            name_up = (file.name or "").upper()
            loggin_utils.log(f"Comparing pattern: '{target_snippet}' to file: '{name_up}'")
            if target_snippet in name_up:
                loggin_utils.log(f"Found matching file: {file.name} in folder: {fldr.name}, opening...")
                doc = app.documents.open(file, True)

                timeout = 0
                while not app.activeDocument and timeout < 10:
                    time.sleep(1); timeout += 1

                product = app.activeProduct
                if isinstance(product, adsk.fusion.Design):
                    try:
                        sql_interface.update_parameters_from_sql(product, event_data)
                    except Exception as e:
                        loggin_utils.log(f"[open_first_fusion_design_file] param update failed: {e}")
                else:
                    loggin_utils.log("Opened document is not a Design.")
                return True

        for sub in fldr.dataFolders:
            if recursive_file_search(sub):
                return True
        return False

    loggin_utils.log(f"Searching for a '.f3d' file containing 'B2PM' in folder '{folder.name}' and its subfolders...")
    if not recursive_file_search(folder):
        msg = f"No '.f3d' file containing 'B2PM' was found in '{folder.name}' or its subfolders."
        loggin_utils.log(msg)
        ui.messageBox(msg)
    else:
        loggin_utils.log("File opened and parameters updated.")

def move_timeline_before_feature(feature_name: str):
    try:
        app = adsk.core.Application.get()
        design = adsk.fusion.Design.cast(app.activeProduct)

        if not design:
            loggin_utils.log("No active design found.")
            return

        timeline = design.timeline
        for index in range(timeline.count):
            obj = timeline.item(index)
            if feature_name.strip().lower() in (obj.name or "").strip().lower():
                timeline.markerPosition = index
                loggin_utils.log(f"Moved timeline before feature containing '{feature_name}' at index {index}: '{obj.name}'")
                return

        loggin_utils.log(f"No timeline feature found containing: '{feature_name}'")

    except Exception as e:
        loggin_utils.log(f"Error in move_timeline_before_feature(): {e}\n{traceback.format_exc()}")

def _head_from_bucket(p_bucket: str | None) -> str | None:
    """
    Given something like 'KHF-B2PM-10MT-PT-PAN-5',
    return the head token ('PAN').

    We look for the token after 'PT' because in your scheme
    that's the head-shape slot.
    """
    if not p_bucket:
        return None

    toks = [t for t in p_bucket.split("-") if t]
    # Example: ['KHF', 'B2PM', '10MT', 'PT', 'PAN', '5']
    try:
        i = toks.index("PT")
    except ValueError:
        return None

    if i + 1 < len(toks):
        return toks[i + 1]  # 'PAN'
    return None

def _set_user_param_for_active_config(design: adsk.fusion.Design, param_name: str, value) -> tuple[bool, str]:
    """
    Activate row first, then call this to set the param so Fusion stores it into that config row.
    Tries 'in' first, then unitless.
    Returns (ok, reason).
    """
    try:
        if not design:
            return False, "no_design"

        up = getattr(design, "userParameters", None)
        if not up:
            return False, "no_userParameters"

        p = up.itemByName(param_name)
        if not p:
            return False, f"param_not_found:{param_name}"

        try:
            f = float(value)
        except Exception:
            return False, f"bad_value:{value!r}"

        # Try as inches first (common for pen/depth)
        try:
            expr = f"{f:.4f} in"
            p.expression = expr
            try: adsk.doEvents()
            except: pass
            return True, f"set_expression:{expr}"
        except Exception:
            pass

        # Fallback: unitless (some tables use pure numbers)
        try:
            expr = f"{f:.4f}"
            p.expression = expr
            try: adsk.doEvents()
            except: pass
            return True, f"set_expression_unitless:{expr}"
        except Exception as e:
            return False, f"set_failed:{e}"

    except Exception as e:
        return False, f"exception:{e}"


def _set_delta_cell(row_obj, table_obj, delta_value, col_name_candidates=None):
    """
    Sets the delta value in the config table cell for the given row.
    Works around Fusion builds where cfg_table.columns is empty by using _get_columns().
    """
    if row_obj is None or table_obj is None:
        return False, "no_row_or_table"

    try:
        dv = float(delta_value)
    except Exception:
        return False, f"bad_delta:{delta_value!r}"

    if not col_name_candidates:
        col_name_candidates = ["pDeltaPen", "pPenDelta", "mtDeltaPen", "DeltaPen"]

    # ------------------------------------------------------------------
    # 1) Get columns (cfg_table.columns is empty in your build sometimes)
    # ------------------------------------------------------------------
    cols = None
    try:
        cols = getattr(table_obj, "columns", None)
        # If it's a valid collection but empty, treat as unusable
        if cols is not None:
            try:
                # Force materialize to list; some Fusion collections are lazy
                cols = list(cols)
            except Exception:
                pass
        if not cols:
            cols = None
    except Exception:
        cols = None

    if cols is None:
        # Try private helper seen in your logs: _get_columns
        try:
            if hasattr(table_obj, "_get_columns"):
                cols = table_obj._get_columns()
                try:
                    cols = list(cols)
                except Exception:
                    pass
        except Exception as e:
            cols = None

    if not cols:
        # Last resort: we cannot discover names; tell caller why.
        return False, "no_columns_resolved"

    # Build (index, colObj, name) list
    col_info = []
    for i, c in enumerate(cols):
        nm = (getattr(c, "name", None) or getattr(c, "displayName", None) or "").strip()
        col_info.append((i, c, nm))

    # ------------------------------------------------------------------
    # 2) Find matching column by name (case-insensitive)
    # ------------------------------------------------------------------
    hit = None
    for cand in col_name_candidates:
        cand_l = cand.lower()
        for i, c, nm in col_info:
            if nm and nm.lower() == cand_l:
                hit = (i, c, nm)
                break
        if hit:
            break

    if not hit:
        try:
            names = [nm for _, _, nm in col_info if nm]
            loggin_utils.log(f"[Recess] Delta column not found. Candidates={col_name_candidates}. Columns={names[:60]}")
        except:
            pass
        return False, "delta_column_not_found"

    idx, col_obj, nm = hit

    # ------------------------------------------------------------------
    # 3) Resolve cell from row: prefer columnId, else by index
    # ------------------------------------------------------------------
    cell = None

    # Try column id first
    try:
        col_id = getattr(col_obj, "id", None) or getattr(col_obj, "columnId", None)
        if col_id is not None and hasattr(row_obj, "getCellByColumnId"):
            cell = row_obj.getCellByColumnId(col_id)
    except Exception:
        cell = None

    # Fallback to column index
    if cell is None and hasattr(row_obj, "getCellByColumnIndex"):
        try:
            cell = row_obj.getCellByColumnIndex(idx)
        except Exception:
            cell = None

    if cell is None:
        return False, f"cell_not_resolved_for:{nm}"

    # ------------------------------------------------------------------
    # 4) Set cell value
    # ------------------------------------------------------------------
    try:
        s = f"{dv:.4f}"  # "0.0050"
        if hasattr(cell, "value"):
            cell.value = s
            return True, f"set_value:{nm}"
        if hasattr(cell, "expression"):
            cell.expression = s
            return True, f"set_expression:{nm}"
        # Some builds use "text" occasionally
        if hasattr(cell, "text"):
            cell.text = s
            return True, f"set_text:{nm}"
        return False, f"no_value_or_expression:{nm}"
    except Exception as e:
        return False, f"set_failed:{nm}:{e}"

def open_linked_referenced_component(event_data):
    """
    Opens the linked recess component (latest revision) located by:
      PRIMARY:
        - Folder name: <BASE>-###-STYLE-DELTA
        - File name:   <BASE>-<HEAD>-STYLE-DELTA.f3d

      FALLBACK (head-agnostic):
        - No subfolder
        - File name:   <BASE>-STYLE-DELTA.f3d directly in the PI-OD-LEN-STYLE folder

    where <BASE> is the 1st token of the occurrence name and <HEAD> is the 2nd token.

    Returns the new/activated recess configuration name, or None on failure.
    """

    try:
        loggin_utils.log(f"[Recess] event_data keys: {sorted(list((event_data or {}).keys()))}")
        loggin_utils.log(f"[Recess] event_data pRecSpec={((event_data or {}).get('pRecSpec'))!r} pRecSize={((event_data or {}).get('pRecSize'))!r}")
    except Exception:
        pass

    try:
        import re, traceback
        app = adsk.core.Application.get()
        design = adsk.fusion.Design.cast(app.activeProduct)
        if not design:
            loggin_utils.log("No active design to search for linked components.")
            return None

        # ----------------------------
        # Helper: parse recess spec
        # ----------------------------
        def _parse_recess_spec(spec: str):
            """
            Accepts:
              - "MT-00-9"        -> base="MT-00-9", suffix4=None, delta=None, parts=["MT","00","9"]
              - "MT-00-9-0050"   -> base="MT-00-9", suffix4="0050", delta=0.0050, parts=["MT","00","9","0050"]
            """
            spec = (spec or "").strip()
            if not spec:
                return {"ok": False, "reason": "empty", "base": None, "suffix4": None, "delta": None, "parts": []}

            spec = re.sub(r"_+", "-", spec)
            spec = re.sub(r"\s+", "", spec)
            parts = [p for p in spec.split("-") if p]

            if len(parts) == 3:
                return {
                    "ok": True,
                    "reason": "base_only",
                    "base": "-".join(parts),
                    "suffix4": None,
                    "delta": None,
                    "parts": parts,
                }

            if len(parts) == 4:
                suffix4 = parts[3]
                if not re.fullmatch(r"\d{4}", suffix4):
                    return {
                        "ok": False,
                        "reason": f"bad_suffix:{suffix4}",
                        "base": "-".join(parts[:3]),
                        "suffix4": suffix4,
                        "delta": None,
                        "parts": parts,
                    }
                delta = float(suffix4) / 10000.0
                return {
                    "ok": True,
                    "reason": "base_plus_delta",
                    "base": "-".join(parts[:3]),
                    "suffix4": suffix4,
                    "delta": delta,
                    "parts": parts,
                }

            return {
                "ok": False,
                "reason": f"unexpected_part_count:{len(parts)}",
                "base": "-".join(parts[:3]) if len(parts) >= 3 else None,
                "suffix4": parts[3] if len(parts) >= 4 else None,
                "delta": None,
                "parts": parts,
            }

        root = design.rootComponent
        occurrences = root.allOccurrences

        # Find the first linked (referenced) occurrence
        target_occ = None
        ref_name = None
        for occ in occurrences:
            if occ.isReferencedComponent:
                target_occ = occ
                ref_name = occ.name
                loggin_utils.log(f"Found linked reference: {ref_name}")
                break

        if not target_occ:
            loggin_utils.log("No linked referenced component found in this design.")
            return None

        # Parse tokens: only the first whitespace segment is considered.
        first_tok = (ref_name or "").strip().split()[0]
        first_tok = re.sub(r"_+", "-", first_tok).strip("-")
        parts = [p for p in first_tok.split("-") if p]

        if len(parts) < 2:
            loggin_utils.log(
                f"Recess name parse error: expected at least '<BASE>-<HEAD>-...', got '{first_tok}' (parts={parts})."
            )
            return None

        base = parts[0]

        # --- Get head from the *part* data first ---
        p_bucket = (event_data or {}).get("pBucket", "")
        head_from_bucket = _head_from_bucket(p_bucket)

        if head_from_bucket:
            head = head_from_bucket
            loggin_utils.log(f"[head] Using head from pBucket '{p_bucket}': head='{head}'")
        else:
            head = parts[1] if len(parts) >= 2 else "UNKNOWN"
            loggin_utils.log(
                f"[head] Could not derive head from pBucket='{p_bucket}', "
                f"falling back to recess tokens: head='{head}' (parts={parts})"
            )

        # Optionally read style/delta numerics if present (NOT used for file/folder lookup)
        style_num = parts[1] if len(parts) > 1 else None
        delta_num = parts[2] if len(parts) > 2 else None
        loggin_utils.log(
            f"Parsed tokens → base='{base}', head='{head}', style_num={style_num!r}, delta_num={delta_num!r}"
        )

        # Data workspace navigation
        project_name    = "Parametric_Models"
        mid_folder_name = "PI-OD-LEN-STYLE"

        data = app.data
        projects = data.dataProjects
        project = next((p for p in projects if (p.name or "").strip() == project_name), None)
        if not project:
            loggin_utils.log(f"Project '{project_name}' not found.")
            return None

        mid_folder = next((f for f in project.rootFolder.dataFolders if (f.name or "").strip() == mid_folder_name), None)
        if not mid_folder:
            loggin_utils.log(f"Middle folder '{mid_folder_name}' not found in project '{project_name}'.")
            return None

        loggin_utils.log(f"Searching inside folder: {mid_folder.name}")

        # ─────────────────────────────────────────────────────────────
        # PRIMARY: Subfolder is literal pattern: <BASE>-###-STYLE-DELTA
        # ─────────────────────────────────────────────────────────────
        folder_key = f"{base}-###-STYLE-DELTA"
        target_folder = None
        for f in mid_folder.dataFolders:
            if (f.name or "").strip().lower() == folder_key.lower():
                target_folder = f
                break

        candidate = None
        search_folder = None
        file_key = None

        if target_folder:
            loggin_utils.log(f"Matched subfolder: {target_folder.name}")
            search_folder = target_folder
            file_key = f"{base}-{head}-STYLE-DELTA"
            loggin_utils.log(f"[Recess] Primary search for file '{file_key}.f3d' in '{search_folder.name}'")

            for df in search_folder.dataFiles:
                nm = (df.name or "").strip()
                ext = (df.fileExtension or "").strip().lower()
                if ext == 'f3d' and nm.lower() == file_key.lower():
                    candidate = df
                    break

        # ─────────────────────────────────────────────────────────────
        # FALLBACK: no subfolder → try <BASE>-STYLE-DELTA.f3d in mid_folder
        # ─────────────────────────────────────────────────────────────
        if not target_folder or not candidate:
            if not target_folder:
                loggin_utils.log(
                    f"Recess subfolder not found: '{folder_key}'. "
                    f"Falling back to head-agnostic file search in '{mid_folder_name}'."
                )
            else:
                loggin_utils.log(
                    f"Recess file '{file_key}.f3d' not found in '{target_folder.name}'. "
                    f"Falling back to head-agnostic file search in '{mid_folder_name}'."
                )

            search_folder = mid_folder
            file_key = f"{base}-STYLE-DELTA"
            loggin_utils.log(f"[Recess] Fallback search for file '{file_key}.f3d' in '{search_folder.name}'")

            candidate = None
            for df in search_folder.dataFiles:
                nm = (df.name or "").strip()
                ext = (df.fileExtension or "").strip().lower()
                if ext == 'f3d' and nm.lower() == file_key.lower():
                    candidate = df
                    break

        if not candidate:
            msg = (
                f"Recess file not found. Tried:\n"
                f"  1) Folder '{folder_key}' → file '<BASE>-<HEAD>-STYLE-DELTA.f3d'\n"
                f"  2) Folder '{mid_folder_name}' → file '{base}-STYLE-DELTA.f3d'\n"
                f"Aborting so you can check naming/head settings."
            )
            loggin_utils.log(msg)
            return None

        loggin_utils.log(f"Found recess file: {candidate.name} (folder='{search_folder.name}')")

        # --- OPEN LATEST REVISION OF THE RECESS FILE (strict)
        try:
            base_no_ext = candidate.name  # DataFile.name has no extension
            linked_design, err = file_ops.open_latest_datafile_strict(app, search_folder, base_no_ext, 'f3d')
            if not linked_design:
                loggin_utils.log(err or f"❌ Could not open latest '{candidate.name}'.")
                return None
        except Exception as e:
            loggin_utils.log(f"Error opening latest recess file: {e}\n{traceback.format_exc()}")
            return None

        # Confirm recess design is active
        doc = app.activeDocument
        linked_product = app.activeProduct
        linked_design = adsk.fusion.Design.cast(linked_product)
        if not linked_design:
            loggin_utils.log("Failed to cast linked document to Design.")
            return None

        loggin_utils.log(f"Linked design opened: {doc.name}")

        # Base config name (strip any delta suffix if present) — mainly for logging now
        config_table = linked_design.configurationTopTable
        active_row = config_table.activeRow if config_table else None
        base_config_name = path_utils.strip_delta_suffix(active_row.name) if active_row else None
        loggin_utils.log(f"Base configuration name extracted: {base_config_name!r}")

        # ------------------------------------------------------------------
        # Determine desired recess configuration from pRecSpec / pRecSize
        # ------------------------------------------------------------------
        rec_spec = str((event_data or {}).get("pRecSpec") or "").strip()
        rec_size = str((event_data or {}).get("pRecSize") or "").strip()

        raw = rec_spec or rec_size
        loggin_utils.log(f"[Recess] raw recess spec = {raw!r}")

        parsed = _parse_recess_spec(raw)
        loggin_utils.log(f"[Recess] parsed ok={parsed['ok']} reason={parsed['reason']} parts={parsed['parts']}")

        if not parsed["ok"]:
            loggin_utils.log(f"[Recess] Invalid recess spec '{raw}': {parsed['reason']}")
            return None

        base_cfg   = parsed["base"]     # e.g. "MT-00-9"
        ConfigName = raw                # final target config row name
        delta_val  = parsed["delta"]    # None or 0.0050

        loggin_utils.log(f"[Recess] base_cfg={base_cfg!r}, ConfigName(target)={ConfigName!r}, delta_val={delta_val!r}")

        # ------------------------------------------------------------------
        # Activate/Create recess config
        # ------------------------------------------------------------------
        cfg_table = linked_design.configurationTopTable
        if not cfg_table:
            loggin_utils.log("[Recess] No configurationTopTable on linked design.")
            return None

        def _yield_and_do_events():
            try:
                _yield(0.05)
            except Exception:
                pass
            try:
                adsk.doEvents()
            except Exception:
                pass

        def _snapshot_rows():
            """Return dict name->row for current table."""
            m = {}
            try:
                rows = getattr(cfg_table, "rows", None)
                if rows is not None:
                    for r in rows:
                        if r and getattr(r, "name", None):
                            m[r.name] = r
                else:
                    for i in range(cfg_table.rowCount):
                        r = cfg_table.item(i)
                        if r and getattr(r, "name", None):
                            m[r.name] = r
            except Exception as e:
                loggin_utils.log(f"[Recess] Error snapshotting rows: {e}")
            return m

        row_by_name = _snapshot_rows()
        loggin_utils.log("[Recess] Available config rows:")
        for nm in row_by_name.keys():
            loggin_utils.log(f"  - {nm}")

        # 1) activate base first if present
        base_row = row_by_name.get(base_cfg)
        if base_row:
            try:
                base_row.activate()
                loggin_utils.log(f"[Recess] Activated base row first: {base_cfg!r}")
                _yield_and_do_events()
            except Exception as e:
                loggin_utils.log(f"[Recess] Failed to activate base '{base_cfg}': {e}")
        else:
            loggin_utils.log(f"[Recess] Base row '{base_cfg}' not found in table.")

        # 2) if target is base-only, just ensure it is active
        target_row = row_by_name.get(ConfigName)
        if ConfigName == base_cfg:
            target_row = base_row

        # 3) if target is delta-row and missing, create from base row
        if (target_row is None) and (delta_val is not None):
            loggin_utils.log(
                f"[Recess] Target '{ConfigName}' missing; creating from base '{base_cfg}' (delta={delta_val!r})"
            )

            created = None
            try:
                # Variant A: ConfigurationRow.copy(name) or copy(name, activate?)
                if base_row and hasattr(base_row, "copy"):
                    try:
                        created = base_row.copy(ConfigName)
                    except TypeError:
                        # Some builds want a second arg (activate flag)
                        created = base_row.copy(ConfigName, False)

                # Variant B: cfg_table.addRowFromRow(base_row, name)
                if (created is None) and hasattr(cfg_table, "addRowFromRow") and base_row:
                    created = cfg_table.addRowFromRow(base_row, ConfigName)

                # Variant C: cfg_table.copyRow(base_row, name)
                if (created is None) and hasattr(cfg_table, "copyRow") and base_row:
                    created = cfg_table.copyRow(base_row, ConfigName)

                if created:
                    loggin_utils.log(f"[Recess] ✅ Created new config row: {ConfigName!r}")
                    _yield_and_do_events()

                    # Refresh row map and re-bind target_row
                    row_by_name = _snapshot_rows()
                    target_row = row_by_name.get(ConfigName)

                    ok_set, why = _set_delta_cell(target_row or created, cfg_table, delta_val)
                    loggin_utils.log(f"[Recess] Set delta cell from suffix delta_val={delta_val!r} → ok={ok_set} ({why})")

                    # One-time introspection to learn how to set delta cell/value
                    try:
                        loggin_utils.log(f"[Recess] created_row type={type(created)}")
                        loggin_utils.log(
                            f"[Recess] created_row dir(cells/values)={[a for a in dir(created) if 'cell' in a.lower() or 'value' in a.lower() or 'item' in a.lower()]}"
                        )
                        loggin_utils.log(
                            f"[Recess] cfg_table dir(columns)={[a for a in dir(cfg_table) if 'col' in a.lower() or 'cell' in a.lower()]}"
                        )
                    except Exception as e:
                        loggin_utils.log(f"[Recess] introspection log failed: {e}")

                else:
                    loggin_utils.log(
                        "[Recess] ❌ No supported row-copy API found; cannot create delta row on this Fusion build."
                    )

            except Exception as e:
                loggin_utils.log(
                    f"[Recess] ❌ Exception while creating '{ConfigName}': {e}\n{traceback.format_exc()}"
                )



        # 4) set delta column value (ALWAYS when delta_val present)
        if target_row and (delta_val is not None):
            ok_set, why = _set_delta_cell(target_row, cfg_table, delta_val)
            loggin_utils.log(
                f"[Recess] Set delta cell for '{ConfigName}' from delta_val={delta_val!r} → ok={ok_set} ({why})"
            )
            _yield_and_do_events()  # helps Fusion commit config-table edits


        try:
            cols = list(cfg_table._get_columns()) if hasattr(cfg_table, "_get_columns") else []
            names = [(getattr(c,"name",None) or getattr(c,"displayName",None) or "").strip() for c in cols]
            loggin_utils.log(f"[Recess] _get_columns() names ({len(names)}): {names}")
        except Exception as e:
            loggin_utils.log(f"[Recess] _get_columns() dump failed: {e}")

        # 5) activate target (if it exists now)
        new_cfg_name = None
        if target_row:
            try:
                target_row.activate()
                new_cfg_name = ConfigName
                loggin_utils.log(f"[Recess] Activated requested recess config '{ConfigName}'.")
                _yield_and_do_events()

                # ✅ Set pDeltaPen while this row is active (stores into that config row)
                if delta_val is not None:
                    ok, why = _set_user_param_for_active_config(linked_design, "pDeltaPen", delta_val)
                    loggin_utils.log(f"[Recess] Set param pDeltaPen for active row '{ConfigName}' delta_val={delta_val!r} → ok={ok} ({why})")
                    _yield_and_do_events()

            except Exception as e:
                loggin_utils.log(f"[Recess] Failed to activate '{ConfigName}': {e}")


        # fallback: whatever is active
        if not new_cfg_name:
            try:
                if cfg_table and cfg_table.activeRow:
                    new_cfg_name = cfg_table.activeRow.name
                else:
                    new_cfg_name = ConfigName
            except Exception:
                new_cfg_name = ConfigName
            loggin_utils.log(f"[Recess] Could not use '{ConfigName}', falling back to '{new_cfg_name}'.")

        loggin_utils.log(f"[Recess] New/active recess config = {new_cfg_name!r}")

        # Save recess doc so B2 can fetch latest
        try:
            desc = f"Recess update: activated/created '{new_cfg_name}'"
            try:
                doc.save(desc)
            except TypeError:
                doc.save("")
            _yield(0.08)
            loggin_utils.log(f"[Recess] Saved after config '{new_cfg_name}'")
        except Exception as e:
            loggin_utils.log(f"[Recess] Save failed: {e}\n{traceback.format_exc()}")

        return new_cfg_name

    except Exception as e:
        loggin_utils.log(f"Error in open_linked_referenced_component(): {e}\n{traceback.format_exc()}")
        return None
    
# ------------------------ end-to-end orchestrator ------------------------
def open_cloud_file(event_data):
    """
    End-to-end:
      - open B2 (from pBucket)
      - move timeline before 'Remove'
      - open recess, ensure/save config
      - **REACTIVATE B2**, update linked recess to latest, switch to new config
      - move timeline to END
    """
    try:
        app = adsk.core.Application.get()
        ui  = app.userInterface

        if not event_data:
            loggin_utils.log("No SQL data to determine file/folder path.")
            return

        project_name = "Parametric_Models"
        p_bucket     = event_data.get('pBucket', '')
        folder_name  = path_utils.parse_folder_name_from_bucket(p_bucket)

        target_folder = file_ops.find_target_folder(project_name, folder_name)

        data = app.data
        projects = data.dataProjects
        target_project = next((proj for proj in projects if proj.name == project_name), None)

        if not target_project:
            err = f"Project '{project_name}' not found."
            loggin_utils.log(err); ui.messageBox(err, "ETDPFusion")
            return

        root_folder = target_project.rootFolder

        if not target_folder:
            msg = f"Folder '{folder_name}' not found in project '{project_name}'. Attempting to create it..."
            loggin_utils.log(msg)
            try:
                target_folder = root_folder.dataFolders.add(folder_name)
                loggin_utils.log(f"Created missing folder: {folder_name} in project: {project_name}")
                ui.messageBox(f"Folder '{folder_name}' was missing and has been created.", "ETDPFusion")
            except Exception as create_err:
                loggin_utils.log(f"Failed to create folder '{folder_name}': {create_err}")
                ui.messageBox(f"Failed to create folder '{folder_name}': {create_err}", "ETDPFusion")
                return

        loggin_utils.log(f"Final folder found: {target_folder.name}")
        loggin_utils.log(f"Starting linked file search from project root: {root_folder.name}")

        # --- Open B2 ---
        open_first_fusion_design_file(target_folder, event_data)

        # Remember B2 doc reference
        doc_b2 = app.activeDocument
        b2_doc_name = doc_b2.name if doc_b2 else "<none>"

        # --- Move before 'Remove' so recess is visible ---
        move_timeline_before_feature("Remove")
        loggin_utils.log(f"[B2] Before 'Remove'. Active doc: {app.activeDocument.name}")

        # --- Open recess, create/activate target config, save recess ---
        recess_cfg = open_linked_referenced_component(event_data)
        loggin_utils.log(f"[B2] After opening linked. Active doc: {app.activeDocument.name}")

        # === CRITICAL FIX: Reactivate B2 BEFORE switching so we search in B2, not in recess ===
        try:
            if app.activeDocument is not doc_b2 and doc_b2:
                doc_b2.activate(); _yield(0.05)
                try: app.activeViewport.refresh()
                except: pass
            loggin_utils.log(f"[B2] Reactivated original doc: {b2_doc_name}")
        except Exception as e:
            loggin_utils.log(f"[B2] Reactivate original doc failed: {e}\n{traceback.format_exc()}")

        # --- Switch the recess occurrence in B2 to that new config (owner-row method) ---
        if recess_cfg:
            base_token = "-".join(recess_cfg.split("-")[:3])  # e.g. MT-1-11-0063 -> MT-1-11
            ok = switch_b2_recess_to_config(
                target_cfg_row=recess_cfg,
                base_token=base_token,
                insert_base=None  # search any insert first, then fallback by name
            )
            loggin_utils.log(f"[B2] Switch recess to '{recess_cfg}' → {'OK' if ok else 'FAILED'}")
        else:
            loggin_utils.log("[B2] Recess config name was None; skipping switch.")

        # --- Move timeline to END ---
        try:
            # If you have a central helper:
            file_ops.move_timeline_to_end()
        except Exception:
            # Fallback: local simple move to end
            try:
                design = adsk.fusion.Design.cast(app.activeProduct)
                if design:
                    design.timeline.markerPosition = design.timeline.count - 1
            except: pass

        try:
            _yield(0.05)
            app.activeViewport.refresh()
        except:
            pass
        loggin_utils.log(f"[B2] Timeline at END. Active doc: {app.activeDocument.name}")

    except Exception as e:
        loggin_utils.log(f"Exception in open_cloud_file(): {e}\n{traceback.format_exc()}")
        
# ──────────────────────────────────────────────────────────────
# Upload queue barrier (no global app needed)
# ──────────────────────────────────────────────────────────────
def _pump_local(dt=0.08):
    try:
        import adsk.core
        adsk.doEvents()
        app = adsk.core.Application.get()
        vp = getattr(app, "activeViewport", None)
        if vp: vp.refresh()
    except:
        pass
    time.sleep(dt)

def _df_numbers(df) -> tuple[int, int]:
    """Returns (versionNumber, latestVersionNumber) as ints (best-effort)."""
    try:
        vn  = int(getattr(df, "versionNumber", 0) or 0)
        lvn = int(getattr(df, "latestVersionNumber", vn) or vn)
        return vn, lvn
    except:
        return 0, 0

def _reload_datafile(df):
    """
    Best-effort refresh of DataFile properties by re-resolving the entry
    from the parent folder listing.
    """
    try:
        parent = getattr(df, "parentFolder", None)
        if not parent:
            return df
        files = getattr(parent, "dataFiles", None)
        if not files:
            return df
        name = getattr(df, "name", None)
        ext  = str(getattr(df, "fileExtension", "")).lower()
        matches = []
        for i in range(files.count):
            f = files.item(i)
            if getattr(f, "name", None) == name and str(getattr(f, "fileExtension", "")).lower() == ext:
                matches.append(f)
        if matches:
            matches.sort(key=lambda d: int(getattr(d, "versionNumber", 0) or 0))
            return matches[-1]
    except:
        pass
    return df

def wait_for_upload_idle(*, after_target_version: int | None = None,
                         idle_window_s: float = 3.0,
                         poll_s: float = 0.35,
                         timeout_s: float = 180.0,
                         tag: str = "[upload-wait]") -> bool:
    """
    Polls the active document's DataFile version numbers until:
      1) latestVersionNumber >= after_target_version (if provided), AND
      2) the pair (versionNumber, latestVersionNumber) stops changing for idle_window_s.
    Returns True if idle detected before timeout. Safe no-op if no active doc/df.
    """
    try:
        import adsk.core
        app = adsk.core.Application.get()
        doc = getattr(app, "activeDocument", None)
        if not doc:
            return True
        df = getattr(doc, "dataFile", None)
        if not df:
            return True

        start = time.time()
        stable_since = None
        last_pair = (-1, -1)

        while True:
            df = _reload_datafile(df)
            vn, lvn = _df_numbers(df)
            now_pair = (vn, lvn)

            # Phase 1: ensure target version appeared (if requested)
            if after_target_version is not None and lvn < after_target_version:
                loggin_utils.log(f"{tag} waiting for server >= v{after_target_version} (now v{lvn})")
                _pump_local(poll_s)
            else:
                # Phase 2: require an idle window
                if now_pair != last_pair:
                    stable_since = time.time()
                    last_pair = now_pair
                else:
                    if stable_since and (time.time() - stable_since) >= idle_window_s:
                        loggin_utils.log(f"{tag} idle detected at v{vn}/latest v{lvn}")
                        return True
                _pump_local(poll_s)

            if (time.time() - start) > timeout_s:
                loggin_utils.log(f"{tag} timeout after {timeout_s:.1f}s (v={vn}, latest={lvn})")
                return False
    except Exception as e:
        loggin_utils.log(f"{tag} exception: {e}\n{traceback.format_exc()}")
        return False

def save_current_with_upload_barrier(desc: str = "",
                                     *,
                                     max_retries: int = 6,
                                     base_pause: float = 0.25,
                                     idle_window_s: float = 3.0,
                                     timeout_s: float = 180.0,
                                     tag: str = "[save+barrier]") -> bool:
    """
    Saves the ACTIVE document, then blocks until the cloud upload queue is idle.
    If Fusion throws transient 'could not be uploaded... insufficient permissions'
    or other upload-related errors, waits and retries with exponential backoff.
    """
    try:
        import adsk.core
        app = adsk.core.Application.get()
        doc = getattr(app, "activeDocument", None)
        if not doc:
            return False

        # Snapshot server latest before the save to detect the next version
        df_pre = getattr(doc, "dataFile", None)
        pre_lvn = 0
        if df_pre:
            _, pre_lvn = _df_numbers(df_pre)

        for i in range(1, max_retries + 1):
            try:
                try:
                    doc.save(desc or "")
                except TypeError:
                    doc.save("")
                _pump_local(0.15)

                # Wait for server to advance (pre+1) and then idle
                target = (pre_lvn + 1) if pre_lvn else None
                ok = wait_for_upload_idle(after_target_version=target,
                                          idle_window_s=idle_window_s,
                                          timeout_s=timeout_s,
                                          tag=tag)
                loggin_utils.log(f"{tag} save ok; queue idle → {ok}")
                return True
            except Exception as e:
                msg = (str(e) or "").lower()
                retriable = ("could not be uploaded" in msg) or ("insufficient permissions" in msg) or ("upload" in msg)
                if not retriable:
                    loggin_utils.log(f"{tag} non-retryable save error: {e}\n{traceback.format_exc()}")
                    return False

                pause = base_pause * (2 ** (i - 1))  # exponential backoff
                loggin_utils.log(f"{tag} transient upload error; retry {i}/{max_retries} after {pause:.2f}s")
                # Proactively wait for queue to settle before retrying
                wait_for_upload_idle(after_target_version=None,
                                     idle_window_s=idle_window_s,
                                     timeout_s=timeout_s,
                                     tag=f"{tag}-pre-retry")
                _pump_local(pause)

        loggin_utils.log(f"{tag} gave up after {max_retries} retries")
        return False

    except Exception as e:
        loggin_utils.log(f"{tag} exception outer: {e}\n{traceback.format_exc()}")
        return False
