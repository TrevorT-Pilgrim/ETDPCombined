
from . import loggin_utils
from . import path_utils
from . import file_ops
import adsk.core, adsk.fusion, threading, time, traceback
import math
import re

def _find_b1_source_row(design, parts):
    """
    Find a B1PM row for the SAME part & size to copy from.
    Looks for names starting with: "<pref>-B1PM-<size>-<pt>-<flt>-"
    Falls back to size-only match, then activeRow.
    """
    try:
        table = design.configurationTopTable
        if not table or table.rows.count == 0:
            loggin_utils.log("[_find_b1_source_row] ❌ No configurationTopTable or no rows.")
            return None

        size = path_utils.extract_size_from_bucket_parts(parts) or ""
        prefix = f"{parts[0]}-B1PM-{size}-{parts[3]}-{parts[4]}-"

        # Best match: exact family/prefix (same part, B1PM, same size, same PT/FLT)
        for row in table.rows:
            n = row.name.strip()
            if n.startswith(prefix):
                loggin_utils.log(f"[_find_b1_source_row] ✅ Using source '{n}'")
                return row

        # Fallback: any B1 row that contains the size token
        for row in table.rows:
            n = row.name.strip().upper()
            if "-B1PM-" in n and (f"-{size}-" in n or n.endswith(f"-{size}") or n.startswith(f"{size}-")):
                loggin_utils.log(f"[_find_b1_source_row] ⚠️ Using size-only fallback '{row.name}'")
                return row

        # Last resort: whatever is active
        if table.activeRow:
            loggin_utils.log(f"[_find_b1_source_row] ⚠️ Using activeRow fallback '{table.activeRow.name}'")
            return table.activeRow

        loggin_utils.log("[_find_b1_source_row] ⚠️ No suitable source row found.")
        return None

    except Exception as e:
        loggin_utils.log(f"[_find_b1_source_row] Error: {e}\n{traceback.format_exc()}")
        return None

def _activate_config_matching_size(design, size_token: str) -> bool:
    """
    Try to activate a configuration whose name contains that size_token as a distinct
    segment. We try a few heuristics: '-{size}-', endswith '-{size}', startswith '{size}-'.
    """
    try:
        table = design.configurationTopTable
        if not table or table.rows.count == 0:
            loggin_utils.log("[_activate_config_matching_size] ❌ No configurationTopTable or no rows.")
            return False

        # Best attempt: find a row that contains the size token as a dash-delimited segment.
        candidates = []
        for row in table.rows:
            n = row.name.strip()
            if (f"-{size_token}-" in n) or n.endswith(f"-{size_token}") or n.startswith(f"{size_token}-"):
                candidates.append(row)

        if not candidates:
            loggin_utils.log(f"[_activate_config_matching_size] ⚠️ No config name matched size token '{size_token}'.")
            return False

        # Pick the first candidate and activate it
        target = candidates[0]
        try:
            target.activate()
            adsk.doEvents()
            time.sleep(0.05)
            if table.activeRow and table.activeRow.name == target.name:
                loggin_utils.log(f"[_activate_config_matching_size] ✅ Activated '{target.name}' for size '{size_token}'")
                return True
        except Exception as e:
            loggin_utils.log(f"[_activate_config_matching_size] Exception while activating: {e}")

        loggin_utils.log(f"[_activate_config_matching_size] ⚠️ Failed to activate candidate '{target.name}'")
        return False

    except Exception as e:
        loggin_utils.log(f"[_activate_config_matching_size] Error: {e}\n{traceback.format_exc()}")
        return False
    
def ensure_target_config_exists_from_parts(design, parts, pm_token: str) -> bool:
    try:
        table = design.configurationTopTable
        if not table or table.rows.count == 0:
            loggin_utils.log("[ensure_target...] ❌ No configurationTopTable or no rows.")
            return False

        pm_token_up = (pm_token or "").upper().strip()
        if pm_token_up == "B1PM":
            target_name = path_utils.build_b1_config_name_from_parts(parts)
        elif pm_token_up == "B2PM":
            target_name = path_utils.build_b2_config_name_from_parts(parts)
        else:
            loggin_utils.log(f"[ensure_target...] ❌ Unknown pm_token '{pm_token}'")
            return False

        # If already exists, just activate
        for row in table.rows:
            if row.name.strip() == target_name:
                try:
                    row.activate()
                    adsk.doEvents(); time.sleep(0.05)
                    if table.activeRow and table.activeRow.name == target_name:
                        loggin_utils.log(f"[ensure_target...] ✅ Activated existing '{target_name}'")
                        return True
                except Exception as e:
                    loggin_utils.log(f"[ensure_target...] Failed to activate existing '{target_name}': {e}")
                return False

        # Choose the correct SOURCE row to copy from
        if pm_token_up == "B1PM":
            source_row = _find_b1_source_row(design, parts)
        else:  # B2PM worked already; copy from current active row as before
            source_row = table.activeRow

        if not source_row:
            loggin_utils.log("[ensure_target...] ❌ No source_row available to copy from.")
            return False

        # Activate source row (good hygiene) then copy
        try:
            source_row.activate()
            adsk.doEvents(); time.sleep(0.05)
        except Exception as e:
            loggin_utils.log(f"[ensure_target...] ⚠️ Could not activate source '{source_row.name}': {e}")

        new_row = source_row.copy(target_name)
        adsk.doEvents(); time.sleep(0.05)

        try:
            new_row.activate()
            adsk.doEvents(); time.sleep(0.05)
            if table.activeRow and table.activeRow.name == target_name:
                loggin_utils.log(f"[ensure_target...] ✅ Created & activated '{target_name}'")
                return True
        except Exception as e:
            loggin_utils.log(f"[ensure_target...] Created but activation failed '{target_name}': {e}")
        return False

    except Exception as e:
        loggin_utils.log(f"[ensure_target...] Error: {e}\n{traceback.format_exc()}")
        return False

    
def _activate_config_row(config_table, target_name):
    # Try exact match first
    for row in config_table.rows:
        if row.name == target_name:
            row.activate()
            loggin_utils.log(f"Activated configuration: {target_name}")
            return True

    # Fallback match disabled for now
    # for row in config_table.rows:
    #     if row.name.startswith(target_name):
    #         row.activate()
    #         loggin_utils.log(f"Activated configuration by partial match: {row.name}")
    #         return True

    loggin_utils.log(f"Configuration '{target_name}' not found.")
    return False

def switch_active_config(target_name: str = "MT-1-11"):
    try:
        app = adsk.core.Application.get()
        design = adsk.fusion.Design.cast(app.activeProduct)
        if not design:
            loggin_utils.log("[switch_active_config] ❌ No active Design.")
            return False

        table = design.configurationTopTable
        if not table or table.rows.count == 0:
            loggin_utils.log("[switch_active_config] ❌ No configurationTopTable or no rows.")
            return False

        # Try exact match
        if _activate_config_row(table, target_name):
            loggin_utils.log(f"[switch_active_config] ✅ Activated '{target_name}'")
            return True

        # Optional fallback: startswith (helpful if you sometimes have suffixes)
        fallback = next((r for r in table.rows if r.name.strip().lower().startswith(target_name.strip().lower())), None)
        if fallback:
            loggin_utils.log(f"[switch_active_config] Trying fallback startswith: '{fallback.name}'")
            try:
                fallback.activate()
                adsk.doEvents()
                time.sleep(0.05)
                if table.activeRow and table.activeRow.name == fallback.name:
                    loggin_utils.log(f"[switch_active_config] ✅ Fallback activated '{fallback.name}'")
                    return True
            except Exception as e:
                loggin_utils.log(f"[switch_active_config] Fallback exception: {e}")

        loggin_utils.log(f"[switch_active_config] ⚠️ Could not activate '{target_name}'")
        return False
    except Exception as e:
        loggin_utils.log(f"[switch_active_config] Error: {e}\n{traceback.format_exc()}")
        return False
    
def create_delta_configuration(linked_design, base_config_name, mt_pen_val, p_rec_val, row_dict, config_name):
    try:
        config_table = linked_design.configurationTopTable
        if not config_table or config_table.rows.count == 0:
            loggin_utils.log("No configuration rows found.")
            return
        #loggin_utils.log(f"hadfjhalkfd{config_name}")
        # ---- choose which config to copy FROM, then activate it via your helper ----
        source_name = config_name or base_config_name
        if not _activate_config_row(config_table, source_name):
            loggin_utils.log(f"❌ Source configuration '{source_name}' not found or could not be activated.")
            return

        # after activation, use the active row as the copy source
        source_row = config_table.activeRow
        if not source_row or source_row.name != source_name:
            loggin_utils.log(f"❌ Active row mismatch after activation. Expected '{source_name}', got '{getattr(source_row, 'name', None)}'")
            return
        loggin_utils.log(f"✅ Activated source configuration before copy: {source_name}")
        
        # ---- build the new name (unchanged) ----
        head_diam = float(str(row_dict.get('pHeadDiamHF', '0')).split()[0])
        head_angle = float(str(row_dict.get('pHeadAngle', '0')).split()[0])
        if head_diam <= 0 or head_angle <= 0:
            loggin_utils.log("⚠️ Invalid pHeadDiamHF or pHeadAngle for config name generation.")
            return

        angle_rad = math.radians(head_angle / 2)
        delta_val = (head_diam / 2) / math.tan(angle_rad)
        delta_val = round(delta_val, 4)

        delta_str = f"{int(round(delta_val * 10000)):04d}"
        #clean_base_name = path_utils.strip_delta_suffix(base_config_name)
        new_config_name = f"{source_name}-{delta_str}"
        loggin_utils.log(f"Creating new configuration: {new_config_name}")

        # ---- avoid duplicates ----
        for row in config_table.rows:
            if row.name == new_config_name:
                loggin_utils.log(f"Configuration '{new_config_name}' already exists.")
                row.activate()
                loggin_utils.log(f"Activated existing configuration: {new_config_name}")
                return

        # ---- copy FROM the now-active source row ----
        new_row = source_row.copy(new_config_name)
        loggin_utils.log(f"✅ Created configuration: {new_config_name}")

        new_row.activate()
        loggin_utils.log(f"Activated new configuration: {new_config_name}")

        # ---- set mtDeltaPen (unchanged) ----
        delta_param = next((p for p in linked_design.allParameters if p.name == 'mtDeltaPen'), None)
        if delta_param:
            delta_param.expression = f"{delta_val:.4f}"
            loggin_utils.log(f"✅ Set mtDeltaPen = {delta_val:.4f} using headDiam/angle formula")
        else:
            loggin_utils.log("⚠️ mtDeltaPen parameter not found in model.")
        
    except Exception as e:
        loggin_utils.log(f"❌ Error creating delta configuration: {e}\n{traceback.format_exc()}")    
'''
def create_delta_configuration(linked_design, base_config_name, mt_pen_val, p_rec_val, row_dict, configName):
    try:
        config_table = linked_design.configurationTopTable
        if not config_table or config_table.rows.count == 0:
            loggin_utils.log("No configuration rows found.")
            return
        loggin_utils.log(f"!!!!{base_config_name}")
        # ✅ Activate the correct base config row
        base_row = None
        for row in config_table.rows:
            if row.name == base_config_name:
                base_row = row
                base_row.activate()
                loggin_utils.log(f"Activated base configuration before copy: {base_config_name}")
                break

        if not base_row:
            loggin_utils.log(f"Base configuration '{base_config_name}' not found.")
            return

        # 🧠 Calculate suffix using same formula as mtDeltaPen
        head_diam = float(str(row_dict.get('pHeadDiamHF', '0')).split()[0])
        head_angle = float(str(row_dict.get('pHeadAngle', '0')).split()[0])

        if head_diam <= 0 or head_angle <= 0:
            loggin_utils.log("Invalid pHeadDiamHF or pHeadAngle for config name generation.")
            return

        angle_rad = math.radians(head_angle / 2)
        delta_val = (head_diam / 2) / math.tan(angle_rad)
        delta_val = round(delta_val, 4)

        # 🔧 Build 4-digit suffix (e.g., 0.0063 → '0063')
        delta_str = f"{int(round(delta_val * 10000)):04d}"
        clean_base_name = path_utils.strip_delta_suffix(base_config_name)
        new_config_name = f"{clean_base_name}-{delta_str}"
        loggin_utils.log(f"Creating new configuration: {new_config_name}")

        # ✅ Check if config already exists
        for row in config_table.rows:
            if row.name == new_config_name:
                loggin_utils.log(f"Configuration '{new_config_name}' already exists.")
                row.activate()
                loggin_utils.log(f"Activated existing configuration: {new_config_name}")
                return

        # ✅ Copy the activated base row
        new_row = base_row.copy(new_config_name)
        loggin_utils.log(f"Created configuration: {new_config_name}")

        # ✅ Activate the new configuration
        new_row.activate()
        loggin_utils.log(f"Activated new configuration: {new_config_name}")

        # ✅ Set mtDeltaPen using formula: (pHeadDiamHF/2) / tan(pHeadAngle/2)
        delta_param = next((p for p in linked_design.allParameters if p.name == 'mtDeltaPen'), None)
        if delta_param:
            delta_param.expression = f"{delta_val:.4f}"
            loggin_utils.log(f"Set mtDeltaPen = {delta_val:.4f} using headDiam/angle formula")
        else:
            loggin_utils.log("mtDeltaPen parameter not found in model.")

    except Exception as e:
        loggin_utils.log(f"Error creating delta configuration: {e}\n{traceback.format_exc()}")
'''
def compare_mtpen_to_predepth(row_dict, linked_design, base_config_name, configName):
    try:
        p_rec_val = float(str(row_dict.get('pRecDepth', '0')).split()[0])
        loggin_utils.log(f"pRecDepth from SQL: {p_rec_val}")

        mt_pen_param = next((p for p in linked_design.allParameters if p.name == 'mtPen'), None)
        if not mt_pen_param:
            loggin_utils.log("mtPen parameter not found in linked design.")
            return

        mt_pen_val = float(str(mt_pen_param.expression).split()[0])
        loggin_utils.log(f"mtPen from model: {mt_pen_val}")

        if abs(mt_pen_val - p_rec_val) < 1e-5:
            loggin_utils.log(f"mtPen and pRecDepth MATCH: {mt_pen_val} == {p_rec_val}")
        else:
            loggin_utils.log(f"mtPen and pRecDepth DIFFER: {mt_pen_val} ≠ {p_rec_val}")
            create_delta_configuration(linked_design, base_config_name, mt_pen_val, p_rec_val, row_dict, configName)

    except Exception as e:
        loggin_utils.log(f"Error comparing mtPen and pRecDepth: {e}\n{traceback.format_exc()}")

import adsk.core, adsk.fusion, time, traceback
from . import loggin_utils

# ----------------- internal helpers (kept local to avoid collisions) -----------------
def _base_from_title(title: str) -> str:
    t = (title or "").split(" (")[0]
    t = t.split(":")[0]
    return t.strip()

def _open_design_for_datafile(app: adsk.core.Application, df: adsk.core.DataFile):
    """Re-use an already-open design if possible; else open hidden and return (design, doc, is_new)."""
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
    try: doc.isVisible = False
    except: pass
    des = adsk.fusion.Design.cast(doc.products.itemByProductType('DesignProductType'))
    return des, doc, True

def _owner_rows_map_from_insert_column(app: adsk.core.Application, insert_col) -> dict:
    """
    Resolve the owner configured design for this insert column's occurrence,
    and return {lower_name: ConfigurationRow}.
    """
    try:
        occ = None
        if hasattr(insert_col, "occurrence"):
            try:
                occ = insert_col.occurrence
            except:
                occ = None
        if not occ:
            loggin_utils.log("  [_owner_rows] Column has no 'occurrence' or it is None.")
            return {}

        df = None
        if hasattr(occ, "configuredDataFile"):
            try:
                df = occ.configuredDataFile
            except:
                df = None

        if df:
            loggin_utils.log(f"  [_owner_rows] From occurrence.configuredDataFile → '{df.name}'")
            des, doc, is_new = _open_design_for_datafile(app, df)
            try:
                out = {}
                top2 = getattr(des, "configurationTopTable", None)
                if not top2:
                    return out
                rows = top2.rows
                for i in range(rows.count):
                    r = rows.item(i)
                    out[r.name.strip().lower()] = r
                return out
            finally:
                try:
                    if is_new and doc:
                        doc.close(False)
                except:
                    pass

        # fallback: internal component
        comp = getattr(occ, "component", None)
        if comp:
            out = {}
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
        return {}
    except Exception as e:
        loggin_utils.log(f"[_owner_rows_map_from_insert_column] error: {e}\n{traceback.format_exc()}")
        return {}

def _ensure_row_active(config_table: adsk.fusion.ConfigurationTable, name: str):
    """Activate existing row by name, or create+activate it."""
    try:
        for i in range(config_table.rows.count):
            r = config_table.rows.item(i)
            if r.name == name:
                r.activate()
                try: adsk.doEvents()
                except: pass
                time.sleep(0.02)
                loggin_utils.log(f"[ensure_row_active] Activated existing row '{name}'.")
                return r
    except Exception as e:
        loggin_utils.log(f"[ensure_row_active] Scan error: {e}")

    try:
        new_row = config_table.rows.add(name)
        loggin_utils.log(f"[ensure_row_active] Created new row '{name}'.")
        try:
            new_row.activate()
            try: adsk.doEvents()
            except: pass
            time.sleep(0.02)
            loggin_utils.log(f"[ensure_row_active] Activated new row '{name}'.")
        except Exception as e:
            loggin_utils.log(f"[ensure_row_active] Activation of new row failed: {e}")
        return new_row
    except Exception as e:
        loggin_utils.log(f"[ensure_row_active] Failed to add row '{name}': {e}")
        return None

def _set_insert_cell_to_rowname(config_table: adsk.fusion.ConfigurationTable,
                                insert_col,
                                active_row_name: str,
                                target_row_name: str) -> bool:
    """
    Set the insert cell at 'active_row_name' to owner-row 'target_row_name'.
    Retries a few times because the owner doc may lag cloud publishing.
    """
    import time
    try:
        # Resolve the cell on our active row (by name or id)
        if hasattr(insert_col, "getCellByRowName"):
            cell = insert_col.getCellByRowName(active_row_name)
            row_id = None
        else:
            row_id = None
            for i in range(config_table.rows.count):
                if config_table.rows.item(i).name == active_row_name:
                    row_id = config_table.rows.item(i).id
                    break
            if row_id is None:
                loggin_utils.log(f"[_set_insert] Could not find row '{active_row_name}' by name.")
                return False
            cell = insert_col.getCellByRowId(row_id)

        app = adsk.core.Application.get()

        # Try up to 6 times (total ~5s) to find target in owner rows
        target = None
        attempts = 6
        delay = 0.2
        for k in range(attempts):
            rows_map = _owner_rows_map_from_insert_column(app, insert_col)
            target = rows_map.get((target_row_name or "").strip().lower())
            if target:
                break
            try: adsk.doEvents()
            except: pass
            time.sleep(delay)
            delay *= 1.4  # backoff

        if not target:
            loggin_utils.log(f"  [_set_insert] Target '{target_row_name}' not visible yet after retries.")
            return False

        # Prefer explicit APIs; fall back to string props
        for setter in ("row", "setByConfigurationRow", "setByConfigurationRowName"):
            if setter == "row" and hasattr(cell, "row"):
                try:
                    cell.row = target
                    return True
                except: pass
            elif setter == "setByConfigurationRow" and hasattr(cell, "setByConfigurationRow"):
                try:
                    cell.setByConfigurationRow(target)
                    return True
                except: pass
            elif setter == "setByConfigurationRowName" and hasattr(cell, "setByConfigurationRowName"):
                try:
                    cell.setByConfigurationRowName(target.name)
                    return True
                except: pass

        for attr in ("selectedName", "stringValue", "text", "value", "expression"):
            if hasattr(cell, attr):
                try:
                    setattr(cell, attr, target.name)
                    return True
                except: pass

        return False

    except Exception as e:
        loggin_utils.log(f"[_set_insert] error: {e}\n{traceback.format_exc()}")
        return False


def _set_parameter_cell_value_cm(config_table: adsk.fusion.ConfigurationTable,
                                 row_name: str,
                                 col_title: str,
                                 value_cm: float) -> bool:
    """Find a parameter column by exact title (ConfigurationParameterColumn) and set value in cm."""
    try:
        col_index = None
        for ci in range(config_table.columns.count):
            col = config_table.columns.item(ci)
            if getattr(col, "title", "") == col_title and \
               "ConfigurationParameterColumn" in getattr(col, "objectType", ""):
                col_index = ci
                break
        if col_index is None:
            loggin_utils.log(f"[_set_param] Column titled '{col_title}' not found or not a parameter column.")
            return False

        # locate row
        the_row = None
        for i in range(config_table.rows.count):
            r = config_table.rows.item(i)
            if r.name == row_name:
                the_row = r
                break
        if not the_row:
            loggin_utils.log(f"[_set_param] Row '{row_name}' not found.")
            return False

        cell = the_row.getCellByColumnIndex(col_index)
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
                loggin_utils.log(f"[_set_param] set .value failed on '{col_title}': {e}")

        if not applied:
            # fallback: set as expression in inches (Fusion parses units)
            expr = f"{value_cm/2.54:.6f} in"
            for attr in ("expression", "text", "stringValue", "value"):
                if hasattr(cell, attr):
                    try:
                        setattr(cell, attr, expr)
                        applied = True
                        break
                    except Exception as e:
                        loggin_utils.log(f"[_set_param] fallback set {attr}='{expr}' failed on '{col_title}': {e}")

        return applied
    except Exception as e:
        loggin_utils.log(f"[_set_param] error setting '{col_title}': {e}\n{traceback.format_exc()}")
        return False
# -------------------------------------------------------------------------------------


def configure_current_doc_with_inserts_and_params(
    row_name: str,
    insert_targets: dict,
    b0_diam_in: float = None,
    multiplier: float = 4.0,
    b1x_col_title: str = "b1X",
    b2x_col_title: str = "b2X",
    save: bool = True,
    save_desc_prefix: str = "Configured"
) -> bool:
    """
    Generic configurator for the ACTIVE document:
      - Ensures/activates configuration row `row_name`
      - Sets any insert columns whose base title appears in `insert_targets`
        (e.g. {"K-OD-LEN-CONF": "K-0290-0760-RND", "KHF-B1PM-##-PT-FLT-#": "<row>"})
      - If `b0_diam_in` is provided, sets parameter columns `b1x_col_title` and `b2x_col_title`
        to (b0_diam_in * multiplier), in DB units (cm).
      - Optionally saves the document.

    Returns True if all requested updates succeed; False otherwise.
    """
    try:
        app = adsk.core.Application.get()
        design = adsk.fusion.Design.cast(app.activeProduct)
        if not design:
            loggin_utils.log("[configure_current_doc] ❌ Active product is not a Design.")
            return False

        top = getattr(design, "configurationTopTable", None)
        if not top or top.rows.count is None:
            loggin_utils.log("[configure_current_doc] ❌ No configurationTopTable.")
            return False

        # 1) ensure/activate the row
        active_row = _ensure_row_active(top, row_name)
        if not active_row:
            loggin_utils.log(f"[configure_current_doc] ❌ Could not create/activate row '{row_name}'.")
            return False

        ok_all_inserts = True
        # 2) apply insert selections (if provided)
        if insert_targets:
            for ci in range(top.columns.count):
                col = top.columns.item(ci)
                otype = getattr(col, "objectType", "")
                if "ConfigurationInsertColumn" not in otype:
                    continue

                base = _base_from_title(getattr(col, "title", f"<col {ci}>"))
                target_name = insert_targets.get(base)
                if not target_name:
                    continue  # nothing to set for this column

                try:
                    active_row.activate()
                except:
                    pass

                applied = _set_insert_cell_to_rowname(top, col, row_name, target_name)
                loggin_utils.log(f"[configure_current_doc] Insert '{base}' -> '{target_name}' :: {'OK' if applied else 'FAILED'}")
                ok_all_inserts = ok_all_inserts and applied

        # 3) set b1X / b2X if we have diameter
        ok_params = True
        if b0_diam_in is not None and b0_diam_in > 0.0 and multiplier is not None:
            target_in = float(b0_diam_in) * float(multiplier)
            target_cm = target_in * 2.54
            b1_ok = _set_parameter_cell_value_cm(top, row_name, b1x_col_title, target_cm)
            b2_ok = _set_parameter_cell_value_cm(top, row_name, b2x_col_title, target_cm)
            ok_params = b1_ok and b2_ok
            loggin_utils.log(f"[configure_current_doc] Params {b1x_col_title}/{b2x_col_title} = {target_in:.6f} in ({target_cm:.6f} cm) :: {'OK' if ok_params else 'PARTIAL/FAILED'}")
        else:
            loggin_utils.log("[configure_current_doc] Skipped params: missing b0_diam_in or multiplier.")

        # 4) save (optional)
        if save:
            try:
                doc = app.activeDocument
                desc = f"{save_desc_prefix}: {row_name}"
                try:
                    doc.save(desc)   # cloud docs require a string
                except TypeError:
                    doc.save("")     # fallback if environment allows
                try: adsk.doEvents()
                except: pass
                time.sleep(0.02)
            except Exception as e:
                loggin_utils.log(f"[configure_current_doc] Save failed: {e}")

        return ok_all_inserts and ok_params

    except Exception as e:
        loggin_utils.log(f"[configure_current_doc] Exception: {e}\n{traceback.format_exc()}")
        return False
    
def _ui_yield(delay_s: float = 0.05):
    """Yield UI safely without relying on a global `app`."""
    try:
        import adsk.core
        _app = adsk.core.Application.get()
        try:
            adsk.doEvents()
        except:
            pass
        try:
            if _app and _app.activeViewport:
                _app.activeViewport.refresh()
        except:
            pass
    except:
        pass
    import time
    time.sleep(delay_s)

def wait_for_quiet_ui(total_s: float = 2.0):
    """Small cool-down to let Fusion finish background work (Get Latest, config rebuild, etc.)."""
    import math
    steps = max(1, int(math.ceil(total_s / 0.05)))
    for _ in range(steps):
        _ui_yield(0.05)

def wait_for_config_table(design, timeout_s: float = 30.0, need_rows: bool = False):
    """
    Spin until the configuration table is available & stable.
    - If need_rows=True, we also wait until rowCount > 0.
    """
    import time
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        try:
            cfg = getattr(design, "configurations", None)
            if cfg is not None:
                # Touch properties; if Fusion is still busy this may throw.
                rc = cfg.rowCount
                if not need_rows or rc > 0:
                    # Also try to iterate rowNames to ensure materialized
                    try:
                        _ = list(cfg.rowNames)
                    except:
                        _ = None
                    return cfg
        except:
            pass
        _ui_yield(0.1)  # slower polling
    return None

def get_row_name_at_index(design, index: int, timeout_s: float = 30.0, require_non_empty: bool = True) -> str:
    """Row name with retries while the table becomes available."""
    import time
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        cfg = wait_for_config_table(design, timeout_s=2.0, need_rows=require_non_empty)
        if cfg:
            try:
                n = cfg.rowCount
                if 0 <= index < n:
                    names = list(cfg.rowNames)
                    return names[index]
                if not require_non_empty and n == 0:
                    # Table is ready but empty
                    return ""
            except:
                pass
        _ui_yield(0.1)
    return ""

def retry_ensure_row_active(row_name: str, attempts: int = 8) -> bool:
    """Wrap ensure_row_active with UI yields and backoff."""
    backoff = 0.15
    for i in range(attempts):
        ok = False
        try:
            ok = _ensure_row_active(row_name)
        except Exception as e:
            loggin_utils.log(f"[cfg] ensure_row_active error (try {i+1}): {e}")
        if ok:
            return True
        _ui_yield(backoff)
        backoff = min(backoff * 1.5, 1.2)  # cap the wait
    return False
def get_first_row_name(design: adsk.fusion.Design) -> str:
    return get_row_name_at_index(design, 0)