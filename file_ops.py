import adsk.core, adsk.fusion, traceback, time, re
from decimal import Decimal, ROUND_HALF_UP, ROUND_HALF_EVEN
from . import loggin_utils
import os, sys, types, importlib, importlib.util

# --- Name helpers ---
# Accepts K-0290-0760-RND or K-03225-1145-RND (4 or 5 digits per token)
_KCFG_RE = re.compile(r'^K-(\d{4,5})-(\d{4,5})-RND$', re.IGNORECASE)




def try_save_active_doc(desc: str = ""):
    import adsk.core
    app = adsk.core.Application.get()
    doc = app.activeDocument
    if not doc:
        return
    try:
        doc.save(desc or "update")
    except TypeError:
        doc.save("")  # some envs require a string, others allow empty


def ensure_row_active(top: adsk.fusion.ConfigurationTable, name: str):
    try:
        for i in range(top.rows.count):
            r = top.rows.item(i)
            if r.name == name:
                r.activate()
                loggin_utils.log(f"[ensure_row_active] Activated existing row '{name}'.")
                return r
    except Exception as e:
        loggin_utils.log(f"[ensure_row_active] Scan error: {e}")
    try:
        new_row = top.rows.add(name)
        loggin_utils.log(f"[ensure_row_active] Created new row '{name}'.")
        try:
            new_row.activate()
            loggin_utils.log(f"[ensure_row_active] Activated new row '{name}'.")
        except Exception as e:
            loggin_utils.log(f"[ensure_row_active] Activation of new row failed: {e}")
        return new_row
    except Exception as e:
        loggin_utils.log(f"[ensure_row_active] Failed to add row '{name}': {e}")
        return None

def open_design_for_datafile(app: adsk.core.Application, df: adsk.core.DataFile):
    try:
        docs = app.documents
        for i in range(docs.count):
            d = docs.item(i)
            try:
                if d.dataFile and d.dataFile.id == df.id:
                    des = adsk.fusion.Design.cast(d.products.itemByProductType('DesignProductType'))
                    return des, d, False
            except:
                pass
    except:
        pass
    doc = app.documents.open(df, False)
    try:
        doc.isVisible = False
    except:
        pass
    des = adsk.fusion.Design.cast(doc.products.itemByProductType('DesignProductType'))
    return des, doc, True

def rows_map_from_design(des: adsk.fusion.Design):
    out = {}
    if not des:
        return out
    try:
        top2 = getattr(des, "configurationTopTable", None)
        if not top2:
            return out
        rows = top2.rows
        for i in range(rows.count):
            r = rows.item(i)
            out[r.name.strip().lower()] = r
    except:
        pass
    return out

def rows_map_from_component(comp: adsk.fusion.Component):
    out = {}
    if not comp:
        return out
    for attr in ("configurationTopTable", "configurationTable"):
        try:
            tbl = getattr(comp, attr, None)
            if tbl and hasattr(tbl, "rows"):
                rows = tbl.rows
                for i in range(rows.count):
                    r = rows.item(i)
                    out[r.name.strip().lower()] = r
                if out:
                    return out
        except:
            pass
    return out

def owner_rows_map_from_insert_column(app: adsk.core.Application, insert_col) -> dict:
    """
    Use the column's occurrence to resolve the owner configured design,
    then return {lower_name: ConfigurationRow}.
    """
    try:
        occ = None
        if hasattr(insert_col, "occurrence"):
            try:
                occ = insert_col.occurrence
            except:
                occ = None
        if not occ:
            loggin_utils.log("  [owner] Column has no 'occurrence' property or it is None.")
            return {}

        df = None
        if hasattr(occ, "configuredDataFile"):
            try:
                df = occ.configuredDataFile
            except:
                df = None

        if df:
            loggin_utils.log(f"  [owner] From occurrence.configuredDataFile → '{df.name}'")
            des, doc, is_new = open_design_for_datafile(app, df)
            try:
                return rows_map_from_design(des)
            finally:
                try:
                    if is_new and doc:
                        doc.close(False)
                except:
                    pass

        comp = getattr(occ, "component", None)
        if comp:
            loggin_utils.log(f"  [owner] Using internal component '{comp.name}'")
            return rows_map_from_component(comp)

        return {}
    except Exception as e:
        loggin_utils.log(f"[owner_rows_map_from_insert_column] error: {e}")
        return {}

def set_insert_cell_to_rowname(top: adsk.fusion.ConfigurationTable,
                               insert_col,
                               active_row_name: str,
                               target_row_name: str) -> bool:
    """
    For the active design's 'insert_col', set the cell at 'active_row_name'
    to the owner configured design's row named 'target_row_name'.
    """
    try:
        # Get the cell for our active row
        if hasattr(insert_col, "getCellByRowName"):
            cell = insert_col.getCellByRowName(active_row_name)  # ConfigurationInsertCell
        else:
            # Rare fallback via index
            row_idx = None
            for i in range(top.rows.count):
                if top.rows.item(i).name == active_row_name:
                    row_idx = i
                    break
            if row_idx is None:
                loggin_utils.log(f"[set_insert] Could not find row '{active_row_name}' by name.")
                return False
            cell = insert_col.getCellByRowId(top.rows.item(row_idx).id)

        # Build owner rows map from the COLUMN's occurrence
        rows_map = owner_rows_map_from_insert_column(adsk.core.Application.get(), insert_col)
        if not rows_map:
            loggin_utils.log("  [set] No owner rows available (couldn't resolve owner design).")
            return False

        target = rows_map.get(target_row_name.strip().lower())
        if not target:
            loggin_utils.log(f"  [set] Target '{target_row_name}' not found in owner design rows.")
            return False

        # Apply using the supported API: assign the ConfigurationRow to the cell.
        if hasattr(cell, "row"):
            cell.row = target
            return True

        if hasattr(cell, "setByConfigurationRow"):
            cell.setByConfigurationRow(target)
            return True
        if hasattr(cell, "setByConfigurationRowName"):
            cell.setByConfigurationRowName(target.name)
            return True

        # Last resort
        for attr in ("selectedName", "stringValue", "text", "value", "expression"):
            if hasattr(cell, attr):
                try:
                    setattr(cell, attr, target.name)
                    return True
                except:
                    pass

        return False
    except Exception as e:
        loggin_utils.log(f"[set_insert] error: {e}")
        return False

def set_parameter_cell_value_cm(top: adsk.fusion.ConfigurationTable,
                                row_name: str,
                                col_title: str,
                                value_cm: float) -> bool:
    """
    Find the parameter column by exact title and set the cell's value (DB units = cm)
    for the specified row_name.
    """
    try:
        # Locate the column by title and type
        col_index = None
        for ci in range(top.columns.count):
            col = top.columns.item(ci)
            if getattr(col, "title", "") == col_title and \
               "ConfigurationParameterColumn" in getattr(col, "objectType", ""):
                col_index = ci
                break
        if col_index is None:
            loggin_utils.log(f"[param] Column titled '{col_title}' not found or not a parameter column.")
            return False

        # Get the row and cell
        the_row = None
        for i in range(top.rows.count):
            r = top.rows.item(i)
            if r.name == row_name:
                the_row = r
                break
        if not the_row:
            loggin_utils.log(f"[param] Row '{row_name}' not found.")
            return False

        cell = the_row.getCellByColumnIndex(col_index)
        # Prefer .value in DB units (cm)
        try:
            pcell = adsk.fusion.ConfigurationParameterCell.cast(cell)
        except:
            pcell = None

        applied = False
        if pcell and hasattr(pcell, "value"):
            try:
                pcell.value = float(value_cm)
                applied = True
            except Exception as e:
                loggin_utils.log(f"[param] set .value failed on '{col_title}': {e}")

        # Fallback: set expression as inches (Fusion will parse units)
        if not applied:
            expr = f"{value_cm/2.54:.6f} in"  # convert back to inches, but as an expression
            for attr in ("expression", "text", "stringValue", "value"):
                if hasattr(cell, attr):
                    try:
                        setattr(cell, attr, expr)
                        applied = True
                        break
                    except Exception as e:
                        loggin_utils.log(f"[param] fallback set {attr}='{expr}' failed on '{col_title}': {e}")

        return applied
    except Exception as e:
        loggin_utils.log(f"[param] error setting '{col_title}': {e}")
        return False
def base_from_title(title: str) -> str:
    t = (title or "").split(" (")[0]
    t = t.split(":")[0]
    return t.strip()

def apply_insert_updates_for_row(design: adsk.fusion.Design, row_name: str, desired_map: dict) -> None:
    """
    For the active design's top configuration table, ensure/activate `row_name` and then
    apply insert selections based on `desired_map`, where keys are column base titles
    (e.g., 'K-OD-LEN-CONF', 'KHF-B1PM-##-PT-FLT-#') and values are target row names
    (e.g., 'MT-1-11-0063', 'KHF-B1PM-25-PT-FLT-2').
    """
    app = adsk.core.Application.get()
    top = getattr(design, "configurationTopTable", None)
    if not top:
        loggin_utils.log("[apply_insert_updates_for_row] No configurationTopTable on design.")
        return

    # Ensure target row exists & is active
    active_row = ensure_row_active(top, row_name)
    if not active_row:
        loggin_utils.log(f"[apply_insert_updates_for_row] Could not activate row '{row_name}'.")
        return

    # Walk columns, find insert columns, and set the cell for this row
    for ci in range(top.columns.count):
        col = top.columns.item(ci)
        otype = getattr(col, "objectType", "")
        if "ConfigurationInsertColumn" not in otype:
            continue

        title = getattr(col, "title", f"<col {ci}>")
        base  = base_from_title(title)  # your existing canonicalizer

        target_name = desired_map.get(base)
        if not target_name:
            continue  # nothing to set for this column

        # Ensure our row is the active one when we set the cell
        try:
            active_row.activate()
        except:
            pass

        ok = set_insert_cell_to_rowname(top, col, row_name, target_name)
        loggin_utils.log(f"[apply_insert] {base} → '{target_name}' :: {'OK' if ok else 'FAILED'}")

def find_occurrence_by_name_contains(root: adsk.fusion.Component, token: str):
    token_up = (token or "").upper()
    for occ in root.allOccurrences:
        nm = (occ.name or "").upper()
        if token_up in nm:
            return occ
    return None

def refresh_and_update_external_refs(app):
    try:
        # Cheap way to make B2 re-read latest
        app.executeTextCommand('Document.Refresh')
        adsk.doEvents()
        time.sleep(0.05)
        loggin_utils.log("[B2] Document.Refresh executed")
    except Exception as e:
        loggin_utils.log(f"[B2] Document.Refresh failed: {e}")

    # Try a few known “update refs” command IDs; ignore if not present
    try:
        ui = app.userInterface
        for cid in ('UpdateAllOutOfDateReferencesCmd', 'GetLatestExternalReferencesCmd', 'UpdateReferencesCmd'):
            cmd_def = ui.commandDefinitions.itemById(cid)
            if cmd_def:
                cmd_def.execute()
                adsk.doEvents()
                time.sleep(0.05)
                loggin_utils.log(f"[B2] Executed command: {cid}")
                break
    except Exception as e:
        loggin_utils.log(f"[B2] Update refs command failed: {e}")


def switch_occurrence_to_config(occ, cfg_name: str) -> bool:
    try:
        if not occ:
            loggin_utils.log("[B2] No occurrence to switch.")
            return False

        row_now = getattr(occ, 'configurationRow', None)
        table = getattr(row_now, 'table', None)
        if not table:
            table = getattr(getattr(occ, 'component', None), 'configurationTopTable', None)
        if not table:
            loggin_utils.log("[B2] Occurrence has no configuration table; cannot switch.")
            return False

        target = table.rows.itemByName(cfg_name)
        if not target:
            loggin_utils.log(f"[B2] Config row '{cfg_name}' not found in occurrence table.")
            return False

        occ.switchConfiguration(target)
        adsk.doEvents()
        time.sleep(0.02)
        loggin_utils.log(f"[B2] Switched occurrence '{occ.name}' to '{cfg_name}'")
        return True
    except Exception as e:
        loggin_utils.log(f"[B2] switch_occurrence_to_config error: {e}\n{traceback.format_exc()}")
        return False


def find_occurrence_by_token(root_comp: adsk.fusion.Component, token: str):
    token_up = (token or "").upper()
    for occ in root_comp.allOccurrences:
        nm = (occ.name or "").upper()
        if occ.isReferencedComponent and token_up in nm:
            return occ
    return None
    
def fmt_no_dot_3(x: float) -> str:
    """Round HALF_UP to 3 decimals, remove '.', pad to 4 digits for <1.000.
       Examples: 0.290 -> '0290', 0.7595 -> '0760', 1.145 -> '1145'."""
    q = Decimal(x).quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)
    token = int((q * 1000).to_integral_value(rounding=ROUND_HALF_UP))
    s = str(token)
    if q < Decimal('1.000'):
        s = s.zfill(4)
    return s

def build_name_from_od_len(od_in: float, ln_in: float) -> str:
    return f"K-{fmt_no_dot_3(od_in)}-{fmt_no_dot_3(ln_in)}-RND"

def _token_to_in(token: str) -> float:
    """'0290' -> 0.290, '1145' -> 1.145, '03225' -> 0.3225."""
    try:
        return int(token) / (10 ** (len(token) - 1))
    except:
        return None

def _parse_kcfg_name(name: str):
    m = _KCFG_RE.match(name or "")
    if not m:
        return None
    od_in = _token_to_in(m.group(1))
    ln_in = _token_to_in(m.group(2))
    if od_in is None or ln_in is None:
        return None
    return od_in, ln_in

# --- Data access helpers (robust to API differences) ---
def _get_project_by_name(data, name: str):
    projs = data.dataProjects
    try:
        for p in projs:
            if (p.name or '').lower() == name.lower():
                return p
    except:
        pass
    for i in range(getattr(projs, 'count', 0)):
        p = projs.item(i)
        if (p.name or '').lower() == name.lower():
            return p
    return None

def _get_subfolder_by_name(parent_folder, name: str):
    dfs = parent_folder.dataFolders
    # Fast path by name API
    try:
        sub = getattr(dfs, 'itemByName', None)
        if sub:
            got = sub(name)
            if got: 
                return got
    except:
        pass
    # Manual scan (case-insensitive)
    for i in range(getattr(dfs, 'count', 0)):
        f = dfs.item(i)
        if (getattr(f, 'name', '') or '').strip().lower() == name.strip().lower():
            return f
    return None


def _find_datafile_in_folder(folder, base_name: str, want_ext: str = 'f3d'):
    """Return the DataFile whose *actual* name equals base_name and (optionally) extension matches."""
    files = folder.dataFiles
    for i in range(getattr(files, 'count', 0)):
        df  = files.item(i)
        nm  = (getattr(df, 'name', '') or '').strip()
        ext = (getattr(df, 'fileExtension', '') or '').strip().lower()
        if nm.lower() == base_name.strip().lower() and (not want_ext or ext == want_ext.lower()):
            return df
    # If exact match not found, try a defensive fallback: strictly “base_name v#”
    best, best_num = None, -1
    for i in range(getattr(files, 'count', 0)):
        df  = files.item(i)
        nm  = (getattr(df, 'name', '') or '').strip()
        ext = (getattr(df, 'fileExtension', '') or '').strip().lower()
        if nm.lower().startswith(base_name.strip().lower() + ' v') and (not want_ext or ext == want_ext.lower()):
            vernum = int(getattr(df, 'versionNumber', 0) or 0)
            if vernum >= best_num:
                best, best_num = df, vernum
    return best

def _close_all_docs_for_item(app, datafile):
    try:
        # Close any open docs whose dataFile id matches either the item or the latest pinned file
        df_id = getattr(datafile, 'id', None)
        for i in range(app.documents.count - 1, -1, -1):
            d  = app.documents.item(i)
            df = getattr(d, 'dataFile', None)
            if df and hasattr(df, 'id') and (df.id == df_id):
                try: d.close(False)
                except: pass
    except: pass


def _get_latest_version_and_pinned_df(df):
    """
    Returns (latest_version_obj, pinned_datafile_for_that_version or None).
    On older APIs, pinned_datafile may be None; in that case we fall back later.
    """
    vers = getattr(df, 'versions', None)
    if vers and getattr(vers, 'count', 0) > 0:
        try:
            latest_ver = vers.item(vers.count - 1)
        except:
            latest_ver = None
        pinned_df = None
        if latest_ver is not None:
            pinned_df = getattr(latest_ver, 'dataFile', None)
        return latest_ver, pinned_df
    return None, None


def _find_exact_versioned_name_in_folder(folder, base_name: str, version_num: int, want_ext: str = 'f3d'):
    """
    Fallback: some hubs expose versioned *display names* like 'K-OD-LEN-CONF v5.f3d'.
    This searches by exact 'base_name v{version_num}'.
    """
    files = folder.dataFiles
    target_name = f"{base_name} v{version_num}".lower()
    for i in range(getattr(files, 'count', 0)):
        df  = files.item(i)
        nm  = (getattr(df, 'name', '') or '').strip().lower()
        ext = (getattr(df, 'fileExtension', '') or '').strip().lower()
        if nm == target_name and (not want_ext or ext == want_ext):
            return df
    return None


def open_latest_datafile_strict(app, hub_folder, base_name, want_ext='f3d'):
    """
    Opens the *exact latest revision* for a file with name == base_name in hub_folder.
    Strategy:
      - get item (DataFile) by exact name
      - close any open docs for that item
      - get latest DataFileVersion → pinned DataFile and open it
      - if that fails, try opening the exact 'base_name vN' file by name
    """
    # 1) locate the item by exact name
    files = hub_folder.dataFiles
    item = None
    for i in range(getattr(files, 'count', 0)):
        df  = files.item(i)
        nm  = (getattr(df, 'name', '') or '').strip()
        ext = (getattr(df, 'fileExtension', '') or '').strip().lower()
        if nm.lower() == base_name.strip().lower() and (not want_ext or ext == want_ext.lower()):
            item = df
            break
    if not item:
        return None, f"❌ DataFile '{base_name}.{want_ext}' not found in '{hub_folder.name}'."

    # 2) close any open docs for this item to prevent reuse of an older session
    _close_all_docs_for_item(app, item)

    # 3) resolve latest version + pinned DF
    latest_ver, pinned_df = _get_latest_version_and_pinned_df(item)
    latest_num = None
    try:
        # safest “latest number” we can report
        latest_num = getattr(latest_ver, 'versionNumber', None)
        if latest_num is None:
            latest_num = getattr(item, 'versionNumber', None)
    except:
        pass

    # 4) try to open the pinned DF first (best path)
    try:
        target = pinned_df if pinned_df else item
        try:
            doc = app.documents.open(target, True)
        except:
            doc = app.documents.open(target)
        if not doc:
            raise RuntimeError("documents.open returned None")

        # Activate and sanity-check
        try:
            doc.activate(); adsk.doEvents(); app.activeViewport.refresh()
        except: pass

        design = adsk.fusion.Design.cast(app.activeProduct)
        if not design:
            return None, "❌ Active product is not a Design after open."

        # If we could read a latest version number, double-check we truly opened it
        try:
            active_df = design.document.dataFile
            open_num  = getattr(active_df, 'versionNumber', None)
            if latest_num is not None and open_num is not None and int(open_num) < int(latest_num):
                # Fallback: open by *display name* 'base_name vN'
                raise RuntimeError(f"Opened v{open_num} but latest is v{latest_num}; forcing name-specific open.")
        except:
            pass

        return design, None

    except Exception as e:
        # 5) Fallback: explicit name 'base_name vN'
        if latest_num:
            alt = _find_exact_versioned_name_in_folder(hub_folder, base_name, int(latest_num), want_ext.lower())
            if alt:
                try:
                    doc = app.documents.open(alt, True)
                except:
                    doc = app.documents.open(alt)
                if doc:
                    try:
                        doc.activate(); adsk.doEvents(); app.activeViewport.refresh()
                    except: pass
                    design = adsk.fusion.Design.cast(app.activeProduct)
                    if design:
                        return design, None
        return None, f"open_latest_datafile_strict failure: {e}"

def _get_latest_datafile(df):
    """
    Given a DataFile handle, return a handle to the *latest* revision of that file.
    Works even if df itself isn’t the latest.
    """
    try:
        # Some environments expose df.isLatest and df.versionNumber
        is_latest = getattr(df, 'isLatest', None)
        if is_latest is True:
            return df

        # Fall back to the versions collection:
        vers = getattr(df, 'versions', None)
        if vers and getattr(vers, 'count', 0) > 0:
            # In Fusion API, DataFileVersions.item(i) returns a DataFileVersion,
            # which usually has a .dataFile property.
            try:
                latest_ver = vers.item(vers.count - 1)
                latest_df = getattr(latest_ver, 'dataFile', None) or latest_ver  # defensive
                return latest_df
            except Exception as e:
                loggin_utils.log(f"_get_latest_datafile: versions fallback failed: {e}")
                return df
        # If no versions API, just return original
        return df
    except Exception as e:
        loggin_utils.log(f"_get_latest_datafile error: {e}\n{traceback.format_exc()}")
        return df
    
def _get_open_document_for_datafile(app, datafile):
    try:
        for i in range(app.documents.count):
            d = app.documents.item(i)
            df = getattr(d, 'dataFile', None)
            if df and hasattr(df, 'id') and df.id == getattr(datafile, 'id', None):
                return d
    except: pass
    # fallback by name prefix
    try:
        for i in range(app.documents.count):
            d = app.documents.item(i)
            if (d.name or '').lower().startswith((datafile.name or '').lower()):
                return d
    except: pass
    return None

def open_latest_datafile(app, df):
    """
    Ensure the *latest* version of df is open and active.
    If an older version is open, close it and open the latest.
    Returns the active Design (or None on failure).
    """
    try:
        latest_df = _get_latest_datafile(df)

        # Check if any version is already open
        open_doc = _get_open_document_for_datafile(app, latest_df)
        if open_doc:
            try:
                open_doc.activate()
                adsk.doEvents()
                loggin_utils.log(f"Activated already-open latest document: {open_doc.name}")
            except:
                pass
        else:
            # There might be an older version open; close it to avoid reuse
            old_open = _get_open_document_for_datafile(app, df)
            if old_open:
                try:
                    loggin_utils.log(f"Closing older open version: {old_open.name}")
                    old_open.close(False)
                    adsk.doEvents()
                    time.sleep(0.05)
                except Exception as e:
                    loggin_utils.log(f"Could not close older open doc: {e}")

            # Open the latest
            try:
                doc = app.documents.open(latest_df, True)  # True => skip UI if supported
            except:
                doc = app.documents.open(latest_df)
            if not doc:
                loggin_utils.log("❌ Failed to open latest data file document.")
                return None

            try:
                doc.activate()
                adsk.doEvents()
                app.activeViewport.refresh()
            except:
                pass

        design = adsk.fusion.Design.cast(app.activeProduct)
        if not design:
            loggin_utils.log("❌ Active product is not a Design after open.")
            return None

        # Log version info for sanity
        try:
            active_df = design.document.dataFile
            v_open = getattr(active_df, 'versionNumber', None)
            v_latest = getattr(latest_df, 'versionNumber', None)
            loggin_utils.log(f"Now open: {active_df.name} (open ver={v_open}, latest ver={v_latest})")
        except:
            pass

        return design
    except Exception as e:
        loggin_utils.log(f"open_latest_datafile error: {e}\n{traceback.format_exc()}")
        return None

# --- Configuration helpers ---
def _rows_iter(rows):
    try:
        for r in rows:
            yield r
        return
    except:
        pass
    for i in range(getattr(rows, 'count', 0)):
        yield rows.item(i)

def _find_row_by_name(rows, name: str):
    try:
        itm = rows.itemByName(name)
        if itm: return itm
    except: pass
    name_lc = (name or '').lower()
    for r in _rows_iter(rows):
        if (r.name or '').lower() == name_lc:
            return r
    return None

def _find_rows_matching_token(rows, token: str):
    token = str(token).strip()
    hits = []
    for r in _rows_iter(rows):
        n = (r.name or '').strip()
        if (f"-{token}-" in n) or n.endswith(f"-{token}") or n.startswith(f"{token}-"):
            hits.append(r)
    return hits

def _try_activate_row(design, table, row) -> bool:
    try:
        row.activate()
        adsk.doEvents(); time.sleep(0.05)
        if getattr(table, 'activeRow', None) and table.activeRow.name == row.name:
            return True
    except: pass
    try:
        table.activateRow(row)
        adsk.doEvents(); time.sleep(0.05)
        if getattr(table, 'activeRow', None) and table.activeRow.name == row.name:
            return True
    except: pass
    try:
        design.activateConfiguration(row)
        adsk.doEvents(); time.sleep(0.05)
        if getattr(table, 'activeRow', None) and table.activeRow.name == row.name:
            return True
    except: pass
    try:
        design.activeConfiguration = row
        adsk.doEvents(); time.sleep(0.05)
        if getattr(table, 'activeRow', None) and table.activeRow.name == row.name:
            return True
    except: pass
    return False

def _activate_cfg_row(design: adsk.fusion.Design, cfg_table, row) -> bool:
    """Alternate activator path."""
    try:
        cfg_table.activateRow(row); return True
    except: pass
    try:
        design.activateConfiguration(row); return True
    except: pass
    try:
        design.activeConfiguration = row; return True
    except: pass
    return False

def activate_knockout_config(design: adsk.fusion.Design, cfg_name: str) -> bool:
    """
    Activate configuration 'cfg_name' (e.g. 'K-0290-0760-RND').
    Falls back to matching the LEN token (3–5 digits) if exact not found.
    """
    try:
        table = getattr(design, 'configurationTopTable', None)
        if not table or table.rows.count == 0:
            loggin_utils.log("[activate_knockout_config] ❌ No configuration table/rows.")
            return False

        # 1) Exact match
        row = _find_row_by_name(table.rows, cfg_name)
        if row:
            ok = _try_activate_row(design, table, row)
            loggin_utils.log(f"[activate_knockout_config] {'✅' if ok else '⚠️'} exact '{cfg_name}'")
            return ok

        # 2) Token fallback (use LEN chunk like '0760')
        parts = cfg_name.split('-')
        token = parts[2] if len(parts) >= 3 else cfg_name
        cands = _find_rows_matching_token(table.rows, token)
        if cands:
            ok = _try_activate_row(design, table, cands[0])
            loggin_utils.log(f"[activate_knockout_config] "
                             f"{'✅' if ok else '⚠️'} token '{token}' -> '{cands[0].name}'")
            return ok

        loggin_utils.log(f"[activate_knockout_config] ❌ No match for '{cfg_name}'")
        return False

    except Exception as e:
        loggin_utils.log(f"[activate_knockout_config] Error: {e}\n{traceback.format_exc()}")
        return False

# --- Create-if-missing: locate B0Diam/B0Len columns, add row, set values, activate ---
def _norm(s: str) -> str:
    return ''.join(ch for ch in (s or '').lower() if ch.isalnum())

def _find_param_col_indexes(cfg_table, candidates):
    """
    Find column indexes for B0Diam/B0Len by checking parameter.name and column.title.
    candidates = {'diam': ['b0diam', 'od', ...], 'len': ['b0len','length',...]}
    """
    cols = cfg_table.columns
    want = {k: {_norm(v) for v in lst} for k, lst in candidates.items()}
    out = {'diam': None, 'len': None}

    for i in range(cols.count):
        col = cols.item(i)
        title_norm = _norm(getattr(col, 'title', ''))

        pname_norm = ''
        try:
            pcol = adsk.fusion.ConfigurationParameterColumn.cast(col)
            if pcol:
                par = getattr(pcol, 'parameter', None)
                pname_norm = _norm(getattr(par, 'name', ''))
        except:
            pass

        if out['diam'] is None and (title_norm in want['diam'] or pname_norm in want['diam']):
            out['diam'] = i
            continue
        if out['len'] is None and (title_norm in want['len'] or pname_norm in want['len']):
            out['len'] = i
            continue

    return out
    
# --- round inches to 3 d.p. (HALF_UP) to match knockoutpin, THEN convert to cm ---
def _round3_half_up(x: float) -> float:
    return float(Decimal(str(x)).quantize(Decimal('0.001'), rounding=ROUND_HALF_UP))

def _round3_nearest(x: float) -> float:
        return float(Decimal(str(x)).quantize(Decimal('0.001'), rounding=ROUND_HALF_EVEN))

def ensure_knockout_config_exists(design_k: adsk.fusion.Design,
                                  knockoutpin: str,
                                  b0Diam_in: float,
                                  b0Len_in: float,
                                  logger=loggin_utils.log) -> bool:
    """
    If a row named `knockoutpin` doesn't exist on the K file, create it,
    set B0Diam/B0Len from inches (converted to cm), and activate it.
    If a NEW row was created, save the K design's document.
    Returns True on success (row exists/activated); save success is logged but not required.
    """
    try:
        cfg = getattr(design_k, 'configurationTopTable', None)
        if not cfg or cfg.rows.count == 0:
            logger("No configurationTopTable/rows on K design.")
            return False

        # Already exists?
        row = _find_row_by_name(cfg.rows, knockoutpin)
        created_new_row = False
        if row:
            logger(f"[K] Row '{knockoutpin}' already exists — leaving values unchanged.")
            ok = _try_activate_row(design_k, cfg, row)
            logger(f"[K] Activated existing config: {row.name}" if ok else "[K] Activate existing config failed.")
            return ok

        # Create new row
        try:
            row = cfg.rows.add(knockoutpin)  # preferred: name at creation
        except Exception as e:
            logger(f"[K] rows.add('{knockoutpin}') failed: {e}; trying add() then rename...")
            row = cfg.rows.add()
            row.name = knockoutpin
        created_new_row = True
        logger(f"[K] Created K row: {knockoutpin}")

        # Locate columns (match parameter name OR column title)
        col_idx = _find_param_col_indexes(cfg, {
            'diam': ['b0diam', 'bodiam', 'od', 'outside_diameter', 'diam', 'diameter'],
            'len' : ['b0len', 'b0length', 'len', 'length']
        })
        if col_idx['diam'] is None or col_idx['len'] is None:
            logger(f"[K] Could not locate B0Diam/B0Len columns (diam={col_idx['diam']} len={col_idx['len']}).")
            return False

        # Convert inches to cm (design/database units)
        diam_in_rounded = b0Diam_in
        diam_name = _round3_nearest(b0Diam_in)
        len_in_rounded  = _round3_nearest(b0Len_in)

        um = design_k.unitsManager
        diam_cm = um.convert(diam_in_rounded, 'in', 'cm')
        len_cm  = um.convert(len_in_rounded,  'in', 'cm')

        # Set parameter cell values
        diam_cell = row.getCellByColumnIndex(col_idx['diam'])
        len_cell  = row.getCellByColumnIndex(col_idx['len'])
        diam_pcell = adsk.fusion.ConfigurationParameterCell.cast(diam_cell)
        len_pcell  = adsk.fusion.ConfigurationParameterCell.cast(len_cell)
        if not (diam_pcell and len_pcell):
            logger("[K] Target cells are not parameter cells; cannot write values.")
            return False

        diam_pcell.value = diam_cm
        len_pcell.value  = len_cm
        logger(f"[K] Set B0Diam={diam_in_rounded:.3f} in, B0Len={len_in_rounded:.3f} in "
               f"({diam_cm:.5f} cm, {len_cm:.5f} cm)")

        # Activate the new row
        ok = _try_activate_row(design_k, cfg, row)
        logger(f"[K] {'Activated' if ok else 'Failed to activate'} created config: {row.name}")

        # Save only if we just created a new row
        if created_new_row:
            try:
                app = adsk.core.Application.get()
                desc = f"K: created config '{knockoutpin}' (B0Diam {diam_in_rounded:.3f} in, B0Len {len_in_rounded:.3f} in)"

                # Prefer the owning document if exposed
                parent_doc = getattr(design_k, 'parentDocument', None)
                if parent_doc:
                    try:
                        parent_doc.save(desc)
                    except TypeError:
                        parent_doc.save("")
                    logger(f"[K] Saved K document after creating '{knockoutpin}'.")
                else:
                    # Fallback: save only if K is the active design
                    active_design = adsk.fusion.Design.cast(app.activeProduct)
                    if active_design is design_k and app.activeDocument:
                        try:
                            app.activeDocument.save(desc)
                        except TypeError:
                            app.activeDocument.save("")
                        logger(f"[K] Saved active K document after creating '{knockoutpin}'.")
                    else:
                        logger("[K] K design is not active and parentDocument is unavailable; skipping save.")
            except Exception as e:
                import traceback
                logger(f"[K] Save attempt failed: {e}\n{traceback.format_exc()}")

        return ok

    except Exception as e:
        import traceback
        logger(f"[ensure_knockout_config_exists] Error: {e}\n{traceback.format_exc()}")
        return False

# --- Project browsing helpers you already had ---
def find_target_folder(project_name, folder_name):
    try:
        app = adsk.core.Application.get()
        ui = app.userInterface

        data = app.data
        projects = data.dataProjects
        target_project = next((proj for proj in projects if proj.name == project_name), None)

        if not target_project:
            loggin_utils.log(f"Project '{project_name}' not found.")
            return None

        def recursive_folder_search(folder):
            loggin_utils.log(f"Checking folder: {folder.name}")
            actual = folder.name.strip().lower()
            expected = folder_name.strip().lower()
            if actual == expected:
                loggin_utils.log(f"Folder matched: '{folder.name}'")
                return folder
            for sub in folder.dataFolders:
                result = recursive_folder_search(sub)
                if result:
                    return result
            return None

        result = recursive_folder_search(target_project.rootFolder)
        if result:
            loggin_utils.log(f"Target folder located: {result.name}")
        else:
            loggin_utils.log(f"Folder '{folder_name}' not found in project '{project_name}'.")

        return result

    except Exception as e:
        loggin_utils.log(f"Exception in find_target_folder(): {e}\n{traceback.format_exc()}")
        return None

# --- Timeline helpers you already had ---
def get_timeline_marker_position():
    try:
        app = adsk.core.Application.get()
        design = adsk.fusion.Design.cast(app.activeProduct)
        tl = design.timeline
        # Prefer markerPosition if present
        if hasattr(tl, "markerPosition"):
            return tl.markerPosition
        # Fallback: use the last index as a safe default
        if hasattr(tl, "count"):
            return max(0, tl.count - 1)
        if hasattr(tl, "timelineObjects") and hasattr(tl.timelineObjects, "count"):
            return max(0, tl.timelineObjects.count - 1)
    except Exception as e:
        loggin_utils.log(f"[timeline] get position failed: {e}\n{traceback.format_exc()}")
    return 0

def move_timeline_to_position(idx: int) -> bool:
    try:
        app = adsk.core.Application.get()
        design = adsk.fusion.Design.cast(app.activeProduct)
        tl = design.timeline
        if hasattr(tl, "moveToPosition"):
            tl.moveToPosition(idx)
        elif hasattr(tl, "markerPosition"):
            tl.markerPosition = idx
        else:
            return False
        adsk.doEvents(); app.activeViewport.refresh()
        return True
    except Exception as e:
        loggin_utils.log(f"[timeline] move to {idx} failed: {e}\n{traceback.format_exc()}")
        return False

def move_timeline_to_end() -> bool:
    try:
        app = adsk.core.Application.get()
        design = adsk.fusion.Design.cast(app.activeProduct)
        tl = design.timeline
        if hasattr(tl, "moveToEnd"):
            tl.moveToEnd()
        else:
            # compute last index and go there
            if hasattr(tl, "count"):
                last_idx = max(0, tl.count - 1)
            else:
                last_idx = max(0, tl.timelineObjects.count - 1)
            move_timeline_to_position(last_idx)
        adsk.doEvents(); app.activeViewport.refresh()
        return True
    except Exception as e:
        loggin_utils.log(f"[timeline] move to end failed: {e}\n{traceback.format_exc()}")
        return False

def run_external_insertcode_fresh(insert_code_path: str) -> str:
    """
    Runs InsertCode.py by absolute path as a fresh synthetic package so:
    - relative imports inside InsertCode work
    - python module caching cannot cause old versions to run
    Returns a short status string (also logs to Text Commands via print()).
    """
    app = adsk.core.Application.get()
    ui  = app.userInterface

    if not insert_code_path or not os.path.isfile(insert_code_path):
        return f"❌ InsertCode not found: {insert_code_path}"

    script_dir  = os.path.dirname(insert_code_path)      # ...\InsertCode
    parent_dir  = os.path.dirname(script_dir)            # ...\PythonScripts
    file_base   = os.path.splitext(os.path.basename(insert_code_path))[0]

    base_package_name = "InsertCode"
    unique_pkg = f"{base_package_name}_RUNTIME_{int(time.time() * 1000)}"
    fqmn = f"{unique_pkg}.{file_base}"

    try:
        # Ensure parent dir is importable FIRST
        if parent_dir in sys.path:
            sys.path.remove(parent_dir)
        sys.path.insert(0, parent_dir)

        # Purge cached modules related to InsertCode (and our unique run pkg)
        for name in list(sys.modules.keys()):
            if (
                name == base_package_name
                or name.startswith(base_package_name + ".")
                or name == unique_pkg
                or name.startswith(unique_pkg + ".")
            ):
                del sys.modules[name]

        try:
            importlib.invalidate_caches()
        except Exception:
            pass

        # Create synthetic package for relative imports
        pkg = types.ModuleType(unique_pkg)
        pkg.__path__ = [script_dir]  # type: ignore[attr-defined]
        sys.modules[unique_pkg] = pkg

        spec = importlib.util.spec_from_file_location(fqmn, insert_code_path)
        if spec is None or spec.loader is None:
            return f"❌ Failed to create spec for: {insert_code_path}"

        mod = importlib.util.module_from_spec(spec)
        mod.__package__ = unique_pkg
        sys.modules[fqmn] = mod

        print("--------------------------------------------------")
        print("[ETDPFusion → InsertCode external runner]")
        print(" Executing file:", insert_code_path)
        print(" Package name:  ", unique_pkg)
        print(" Module name:   ", fqmn)
        print(" File mtime:    ", time.ctime(os.path.getmtime(insert_code_path)))
        print("--------------------------------------------------")

        spec.loader.exec_module(mod)  # type: ignore

        if not hasattr(mod, "run"):
            return f"❌ InsertCode has no run(context): {insert_code_path}"

        # Small settle (mirrors manual “Fusion is idle”)
        try:
            adsk.doEvents()
        except Exception:
            pass
        time.sleep(0.25)

        mod.run("")

        return "✅ InsertCode ran (external fresh load)"

    except Exception:
        return "❌ InsertCode external run failed:\n" + traceback.format_exc()