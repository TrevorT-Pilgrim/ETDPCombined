import adsk.core, adsk.fusion, threading, time, traceback
from datetime import datetime
import pyodbc
import os
import re
import math
import subprocess
#from path_utils import *
from . import loggin_utils
from . import config_logic
from . import file_ops
#from loggin_utils import log
#from . import cloud_operations
from . import path_utils
from . import cloud_operations
from . import sql_interface
from . import volume_logic
import os, json, traceback

INPUT_JSON_PATH = r"C:\Temp\etdp_inputs.json"

app = adsk.core.Application.get()
ui = app.userInterface
stop_thread = False
thread_started = False
event_data = None
parts = []

CMD_ID = 'importSqlShowDataCmd'
CMD_NAME = 'Show Imported Data'
CMD_DESC = 'Display last imported part data from SQL'

OPEN_CMD_ID = 'openCloudFileCmd'
OPEN_CMD_NAME = 'Open Cloud File'
OPEN_CMD_DESC = 'Opens a file based on imported SQL data'

handlers = []



def _yield_and_wait(delay_s: float):
    try: adsk.doEvents(); app.activeViewport.refresh()
    except: pass
    time.sleep(delay_s)

def call_with_config_retry(callable_fn, *, max_tries=6, base_pause=0.15, tag="[retry]"):
    """
    Runs callable_fn() and retries on transient 'Configuration ... temporarily unavailable' errors.
    Returns (success_bool, result_or_None).
    """
    for i in range(1, max_tries + 1):
        try:
            result = callable_fn()
            return True, result
        except Exception as e:
            msg = str(e).lower()
            if ("config" in msg and "temporarily" in msg) or ("select" in msg and "config" in msg):
                # transient – backoff and retry
                pause = base_pause * i
                try:
                    loggin_utils.log(f"{tag} config busy; retry {i}/{max_tries} after {pause:.2f}s")
                except: pass
                _yield_and_wait(pause)
                continue
            # Non-retryable
            try:
                loggin_utils.log(f"{tag} non-retryable error: {e}\n{traceback.format_exc()}")
            except: pass
            return False, None
    try:
        loggin_utils.log(f"{tag} gave up after {max_tries} tries")
    except: pass
    return False, None

def finalize_compute_and_save_current(desc: str = "") -> bool:
    """Generate active config (if any), flush events, then save once."""
    try:
        design = adsk.fusion.Design.cast(app.activeProduct)
        if design:
            try:
                tbl = getattr(design, "configurationTopTable", None)
                if tbl and tbl.activeRow:
                    try: tbl.activeRow.generate()
                    except: pass
            except: pass
        try:
            adsk.doEvents()
            app.activeViewport.refresh()
        except: pass

        doc = app.activeDocument
        if not doc:
            return False
        try:
            doc.save(desc)
        except TypeError:
            doc.save("")
        return True
    except Exception as e:
        loggin_utils.log(f"[finalize_compute_and_save_current] {e}\n{traceback.format_exc()}")
        return False
def _active_datafile_needs_latest(app) -> bool:
    try:
        doc = app.activeDocument
        df = getattr(doc, 'dataFile', None)
        if not df:
            return False
        # Prefer explicit flags if available
        if hasattr(df, 'isLatest'):
            return not bool(df.isLatest)
        # Fallback on version numbers
        if hasattr(df, 'versionNumber') and hasattr(df, 'latestVersionNumber'):
            return int(df.versionNumber) < int(df.latestVersionNumber)
    except:
        pass
    return False  # default to "no refresh" to avoid modal

def try_get_all_latest() -> bool:
    """Best-effort ‘Get All Latest’. Silent if not needed/available."""
    try:
        if not _active_datafile_needs_latest(app):
            loggin_utils.log("[Latest] Skip: active doc already latest")
            return False  # no-op; avoids modal

        ui = app.userInterface
        defs = ui.commandDefinitions

        # Prefer a command that implies dependencies/all
        preferred = None
        fallback = None

        for i in range(defs.count):
            d = defs.item(i)
            lid = (d.id or "").lower()
            lname = (d.name or "").lower()
            txt = f"{lid}|{lname}"

            if "latest" in txt and "get" in txt:
                # candidates
                if "all" in txt or "dep" in txt:   # e.g. "...Dependencies..." / "All Latest..."
                    preferred = d if preferred is None else preferred
                elif "plm360refreshdocumentcommand" in lid:  # single-doc “Get Latest”
                    fallback = d if fallback is None else fallback

        cmd = preferred or fallback
        if not cmd:
            loggin_utils.log("[Latest] No matching command definition found")
            return False

        try:
            cmd.execute()
            try:
                adsk.doEvents()
            except:
                pass
            loggin_utils.log(f"[Latest] Executed: {cmd.id} / {cmd.name}")
            return True
        except Exception as e:
            loggin_utils.log(f"[Latest] Execute failed ({cmd.id}): {e}")
    except Exception as e:
        loggin_utils.log(f"[Latest] Not available: {e}")
    return False


def regenerate_row(design: adsk.fusion.Design, row_name: str) -> bool:
    """Generate + activate a specific configuration row."""
    try:
        tbl = getattr(design, "configurationTopTable", None)
        rows = getattr(tbl, "rows", None)
        if not (tbl and rows):
            return False
        for i in range(rows.count):
            r = rows.item(i)
            if r.name == row_name:
                try: r.generate()
                except: pass
                try: r.activate()
                except: pass
                try: adsk.doEvents(); app.activeViewport.refresh()
                except: pass
                return True
    except Exception as e:
        loggin_utils.log(f"[regenerate_row] {e}\n{traceback.format_exc()}")
    return False
def _activate_config_row(design: adsk.fusion.Design,
                         target_name: str,
                         regen: bool = True,
                         save: bool = False,
                         save_desc: str = "") -> bool:
    """
    Activate a configuration row by name on the given design.
    - Exact (case-insensitive) match first
    - Fallback to first row (index 0) if not found
    - Optionally regenerate & save
    """
    try:
        top = getattr(design, "configurationTopTable", None)
        if not top or not hasattr(top, "rows"):
            loggin_utils.log("[config-switch] ❌ No configurationTopTable.")
            return False

        rows = top.rows
        target = None
        low = (target_name or "").strip().lower()

        # exact match
        for i in range(rows.count):
            r = rows.item(i)
            if r and r.name.strip().lower() == low:
                target = r
                break

        # fallback to first row
        if not target and rows.count > 0:
            target = rows.item(0)
            loggin_utils.log(f"[config-switch] ⚠️ '{target_name}' not found. Falling back to first row: {target.name}")

        if not target:
            loggin_utils.log(f"[config-switch] ❌ Could not resolve any row to activate.")
            return False

        try:
            target.activate()
        except:
            # older APIs sometimes need activating via activeRow property
            try: top.activeRow = target
            except Exception as e:
                loggin_utils.log(f"[config-switch] ❌ activate failed: {e}")
                return False

        loggin_utils.log(f"[config-switch] ✅ Activated row: {target.name}")

        # optional regenerate (forces a rebuild on the newly active row)
        if regen:
            try:
                # if you already have a regenerate_row(design, row_name) util, prefer that:
                # regenerate_row(design, target.name)
                design.timeline.moveToEnd()  # minimal nudge often triggers recompute
                app = adsk.core.Application.get()
                try: adsk.doEvents()
                except: pass
                try: app.activeViewport.refresh()
                except: pass
            except Exception as e:
                loggin_utils.log(f"[config-switch] Regen skipped: {e}")

        if save:
            try:
                doc = design.document
                try:
                    doc.save(save_desc or f"Activated {target.name}")
                except TypeError:
                    doc.save("")
                loggin_utils.log("[config-switch] 💾 Saved document after switch.")
            except Exception as e:
                loggin_utils.log(f"[config-switch] Save failed: {e}")

        return True

    except Exception as e:
        loggin_utils.log(f"[config-switch] Exception: {e}\n{traceback.format_exc()}")
        return False

def activate_base_configuration(linked_design, base_config_name):
    try:
        config_table = linked_design.configurationTopTable
        for row in config_table.rows:
            if row.name == base_config_name:
                row.activate()
                loggin_utils.log(f"Activated base configuration: {base_config_name}")
                return row
        loggin_utils.log(f"Base configuration '{base_config_name}' not found.")
        return None
    except Exception as e:
        loggin_utils.log(f"Error activating base configuration: {e}\n{traceback.format_exc()}")
        return None

def update_config_and_parameters_from_sql(design, is_linked=False):
    try:
        user_params = design.userParameters
        model_params = design.allParameters

        if not event_data:
            loggin_utils.log("No event_data to update parameters from.")
            return

        # Step 1: Fetch SQL data for current varName
        #import pyodbc
        conn = pyodbc.connect(
            "DRIVER={ODBC Driver 17 for SQL Server};"
            "SERVER=pschost1;"
            "DATABASE=ETDP;"
            "UID=sa;"
            "PWD=sa;"
        )
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM dbo.partSubmissions WHERE varName = ?", event_data['varName'])
        row = cursor.fetchone()
        if not row:
            loggin_utils.log("No data returned from SQL for varName.")
            conn.close()
            return
        headers = [desc[0] for desc in cursor.description]
        row_dict = dict(zip(headers, row))
        conn.close()

        loggin_utils.log(f"SQL Row Keys: {list(row_dict.keys())}")

        # Step 2: Try to access the configuration table
        config_table = design.configurationTopTable
        if not config_table or config_table.rows.count == 0:
            loggin_utils.log("No configuration table found or it's empty.")
            return

        existing_names = [row.name for row in config_table.rows]
        loggin_utils.log(f"Existing configurations: {existing_names}")

        # Step 3: Get base spec name safely (if available)
        try:
            if design.isConfiguration:
                config_manager = adsk.fusion.ConfiguredDesign.cast(design)
                if config_manager and config_manager.activeConfiguration:
                    active_config_name = config_manager.activeConfiguration.name
                    base_spec = '-'.join(active_config_name.split('-')[:3])
                else:
                    loggin_utils.log("ConfiguredDesign or activeConfiguration is None.")
                    base_spec = "UNKNOWN"
            else:
                loggin_utils.log("This design does not support configurations.")
                base_spec = "UNKNOWN"
        except Exception as e:
            loggin_utils.log(f"Could not resolve active configuration name: {e}")
            base_spec = "UNKNOWN"

        # Step 4: Create configuration
        if is_linked:
            try:
                mt_pen = float(str(row_dict.get('mtPen', '0')).split()[0])
                p_rec_depth = float(str(row_dict.get('pRecDepth', '0')).split()[0])
                if abs(mt_pen - p_rec_depth) > 1e-5:
                    delta_thou = int(round(abs(mt_pen - p_rec_depth) * 1000))
                    delta_config_name = f"{base_spec}-{delta_thou:03d}-DELTA"
                    if delta_config_name not in existing_names:
                        config_table.rows.add(delta_config_name)
                        loggin_utils.log(f"Created new DELTA config in linked design: {delta_config_name}")
                    else:
                        loggin_utils.log(f"DELTA config already exists: {delta_config_name}")
                else:
                    loggin_utils.log("No delta config needed — mtPen and pRecDepth are equal.")
            except Exception as e:
                loggin_utils.log(f"Error in delta config loggin_utils.logic: {e}\n{traceback.format_exc()}")
        else:
            try:
                base_name = existing_names[0].rsplit('-', 1)[0]
                suffix_nums = [
                    int(name.rsplit('-', 1)[-1])
                    for name in existing_names
                    if '-' in name and name.rsplit('-', 1)[-1].isdigit() and name.startswith(base_name)
                ]
                next_suffix = max(suffix_nums) + 1 if suffix_nums else 1
                new_config_name = f"{base_name}-{next_suffix}"

                config_table.rows.add(new_config_name)
                loggin_utils.log(f"Created and renamed configuration: {new_config_name}")
            except Exception as e:
                loggin_utils.log(f"Error creating new configuration: {e}\n{traceback.format_exc()}")
                return

        # Step 5: Update Parameters
        loggin_utils.log("--- All User Parameter Names ---")
        for param in user_params:
            loggin_utils.log(f"User Param: {param.name}")

        loggin_utils.log("--- All Model Parameter Names ---")
        for param in model_params:
            loggin_utils.log(f"Model Param: {param.name}")

        loggin_utils.log("--- Updating User Parameters ---")
        for param in user_params:
            if param.name in row_dict:
                new_val = row_dict[param.name]
                if new_val is not None:
                    try:
                        param.expression = str(float(str(new_val).split()[0]))
                        loggin_utils.log(f"User Param Updated: {param.name} -> {param.expression}")
                    except:
                        loggin_utils.log(f"User Param Skipped (invalid value): {param.name} = {new_val}")
                else:
                    loggin_utils.log(f"User Param Skipped (null value): {param.name}")
            else:
                loggin_utils.log(f"User Param Skipped (no match): {param.name}")

        loggin_utils.log("--- Updating Model Parameters ---")
        for param in model_params:
            if param.name in row_dict:
                new_val = row_dict[param.name]
                if new_val is not None:
                    try:
                        param.expression = str(float(str(new_val).split()[0]))
                        loggin_utils.log(f"Model Param Updated: {param.name} -> {param.expression}")
                    except:
                        loggin_utils.log(f"Model Param Skipped (invalid value): {param.name} = {new_val}")
                else:
                    loggin_utils.log(f"Model Param Skipped (null value): {param.name}")
            else:
                loggin_utils.log(f"Model Param Skipped (no match): {param.name}")

    except Exception as e:
        loggin_utils.log(f"Exception in update_config_and_parameters_from_sql(): {e}\n{traceback.format_exc()}")
def _derive_b2di_from_parts(parts: list[str]) -> str:
    """
    Convert B2PM row tokens → B2DI row name.
    Expects tokens like: [KHF, B2PM, 25MT, PT, FLT, 3]
    Produces:             KHF-B2DI-25-PT-FLT-3
    """
    try:
        toks = [str(t).strip() for t in parts]
        if len(toks) < 6:
            return "KHF-B2DI-25-PT-FLT-3"
        form = "B2DI"
        size = toks[2].upper().replace("MT", "")
        return f"{toks[0]}-{form}-{size}-{toks[3]}-{toks[4]}-{toks[5]}"
    except Exception:
        return "KHF-B2DI-25-PT-FLT-3"

def _owner_row_from_recess_cfg(recess_cfg: str | None) -> str:
    """
    Accepts something like 'MT-1-11-0063' or 'PI-MT-1-11-0063' and returns owner row only.
    """
    if not recess_cfg:
        return "MT-1-11-0063"
    s = recess_cfg.strip()
    return s[3:] if s.upper().startswith("PI-") else s

def _owner_base_from_recess_cfg(recess_cfg: str | None) -> str:
    """
    Example: MT-1-11-0063 -> MT-1-11
    Falls back to MT-1-11 if recess_cfg is missing.
    """
    if not recess_cfg:
        return "MT-1-11"
    toks = recess_cfg.split("-")
    if len(toks) >= 3:
        return "-".join(toks[0:3])  # keep first 3 tokens
    return recess_cfg

def create_inputs_json(
    event_data: dict,
    parts: list[str],
    folder_name: str,
    recess_cfg: str | None,
    *,
    project_hint: str = "parametric_models",
    open_drawing: bool = True,
    debug_scan: bool = False,
    pHeadDiam: str,
    pShankDiam: str,
    pShankDiamHF: str,
    pLen: str,
    pLenHF: str,
    pB2Machine: str,
    pB1Machine: str,
    mKO_Len: str,
    pRecShape: str,
    pRecSpec: str
) -> str:
    """
    Build etdp_inputs.json for OpenSeekInspect_main.py based on current runtime.
    Returns the written file path.
    """
    try:
        # --- Required base values ---
        p_bucket = (event_data or {}).get("pBucket", "")
        config_name = "-".join([str(t).strip() for t in (parts or []) if str(t).strip()])
        if not config_name:
            config_name = p_bucket if p_bucket else "KHF-B2PM-25MT-PT-FLT-3"

        # Build DI *pattern* with numeric placeholders:
        # {brand}-B2DI-##-{PT}-{FLT}-#
        def _b2di_pattern_from(_parts, fallback="KHF-B2DI-##-PT-FLT-#"):
            try:
                toks = [str(x).strip() for x in (_parts or []) if str(x).strip()]
                brand = toks[0] if len(toks) >= 1 else "KHF"
                pt    = toks[3] if len(toks) >= 4 else "PT"
                flt   = toks[4] if len(toks) >= 5 else "FLT"
                return f"{brand}-B2DI-##-{pt}-{flt}-#".upper()
            except:
                return fallback

        target_name = _b2di_pattern_from(parts or config_name.split("-"))

        # --- Folder hints (keep, helpful for search narrowing) ---
        folder_hint = [folder_name] if folder_name else ["KHF-EEFF-GG-FLT"]

        # --- PI values ---
        owner_row_full = _owner_row_from_recess_cfg(recess_cfg)        # e.g., MT-1-11-0063
        owner_row_base = _owner_base_from_recess_cfg(owner_row_full)   # e.g., MT-1-11
        pi_occ_token   = (owner_row_base.split("-")[0] if owner_row_base else "MT") or "MT"

        #   pi_base_config = "PI-" + full recess (owner config with suffix)
        #   pi_model_name  = "PI-" + pi_occ_token + "-STYLE-DELTA"  (family by token)
        pi_base  = f"PI-{owner_row_base}" if owner_row_base else "PI-MT-1-11"
        pi_model = f"PI-{pi_occ_token}-STYLE-DELTA" if pi_occ_token else "PI-MT-STYLE-DELTA"

        payload = {
            # open targets
            "target_name": target_name,
            "project_hint": project_hint,
            "folder_hint": folder_hint,
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
            "debug_scan": bool(debug_scan),

            # linked occurrence switching
            "attempt_switch": True,
            "config_name": config_name,   # desired B2PM owner row
            "probe_only": False,

            "owner_retry_tries": 6,
            "owner_retry_base_s": 0.18,
            "move_marker_to_end": True,

            # drawing
            "open_related_drawing": bool(open_drawing),
            "drawing_from_model_name": True,
            "drawing_folder_hint": folder_hint,
            "drawing_update_timeout_s": 120.0,
            "drawing_force_ui_get_latest": True,
            "drawing_name": "",

            # Punch Insert flow
            "pi_model_name": pi_model,          # "PI-" + pi_occ_token + "-STYLE-DELTA"
            "pi_folder_hint": ["PI-OD-LEN-STYLE"],
            "pi_project_hint": None,            # fall back to project_hint
            "pi_base_config": pi_base,          # "PI-" + full recess (with suffix)
            "pi_recess_name": owner_row_full,   # base owner row (no suffix), e.g. MT-1-11
            "pi_timeline_anchor": "RemoveInstance",
            "pi_occurrence_token": pi_occ_token,

            "pi_keep_owner_open": True,
            "suppress_ui_messages": True,
            "pHeadDiam": pHeadDiam,
            "pShankDiam": pShankDiam,
            "pShankDiamHF": pShankDiamHF,
            "pLen": pLen,
            "pLenHF": pLenHF,
            "pB2Machine": pB2Machine,
            "pB1Machine": pB1Machine,
            "mKO_Len": mKO_Len,
            "pRecShape": pRecShape,
            "pRecSpec": pRecSpec,
        }

        out_dir = os.path.dirname(INPUT_JSON_PATH)
        if out_dir and not os.path.isdir(out_dir):
            os.makedirs(out_dir, exist_ok=True)

        with open(INPUT_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)

        loggin_utils.log(f"[json] wrote inputs to {INPUT_JSON_PATH}")
        return INPUT_JSON_PATH

    except Exception as e:
        loggin_utils.log(f"[json] failed to write inputs: {e}\n{traceback.format_exc()}")
        return ""



def _build_filename_pattern(parts, pm_token: str) -> str:
    """
    Compose the search snippet used to match .f3d filenames.
    Examples:
      - B2PM: 'KHF-B2PM-##MT-PT-FLT-#'
      - B1PM: 'KHF-B1PM-##-PT-FLT-#'
      - B0PP: 'KHF-B0PP-##MT-PT-FLT-#'
    """
    if not parts or len(parts) < 5:
        raise ValueError(f"parts not in expected shape: {parts}")

    t = pm_token.upper()
    if t in ("B2PM", "B0PP"):
        # Keep the MT segment from parts[2] (e.g., '25MT' -> 'MT'), and prefix with '##'
        mid = parts[2][2:] if len(parts[2]) >= 3 else parts[2]
        snippet = f"{parts[0]}-{t}-##{mid}-{parts[3]}-{parts[4]}-#"
    elif t == "B1PM":
        # Omit the MT segment entirely
        snippet = f"{parts[0]}-{t}-##-{parts[3]}-{parts[4]}-#"
    else:
        raise ValueError(f"Unsupported pm_token: {pm_token}")

    return snippet.upper()

    
def open_first_fusion_design_file(folder, parts, event_data, pm_token: str) -> bool:
    """
    Recursively searches `folder` for an .f3d file whose name contains the pattern
    built with the given PM token (B1PM/B2PM), then opens it and updates parameters.
    """
    app = adsk.core.Application.get()
    ui = app.userInterface

    try:
        target_snippet = _build_filename_pattern(parts, pm_token)
        loggin_utils.log(f"[open_first_fusion_design_file] Target pattern: {target_snippet}")
    except Exception as e:
        loggin_utils.log(f"[open_first_fusion_design_file] Pattern build error: {e}")
        return False

    def recursive_file_search(fldr):
        for file in fldr.dataFiles:
            if file.fileExtension != 'f3d':
                continue

            name_up = file.name.upper()
            loggin_utils.log(
                f"Comparing pattern: '{target_snippet}' to file: '{name_up}'"
            )

            if target_snippet in name_up:
                loggin_utils.log(f"Found matching file: {file.name} in folder: {fldr.name}, opening...")
                doc = app.documents.open(file, True)

                timeout = 0
                while not app.activeDocument and timeout < 10:
                    time.sleep(1)
                    timeout += 1

                product = app.activeProduct
                if isinstance(product, adsk.fusion.Design):
                    # Ensure/activate the correct config by PM flavor BEFORE updating params
                    if config_logic.ensure_target_config_exists_from_parts(product, parts, pm_token):
                        sql_interface.update_parameters_from_sql(product, event_data)
                    else:
                        loggin_utils.log(f"[open_first_fusion_design_file] ❌ Could not ensure target config for {pm_token}")

                    # 2) Then update parameters (no new config creation inside)
                    sql_interface.update_parameters_from_sql(product, event_data)
                else:
                    loggin_utils.log("Opened document is not a Design.")
                return True

        for sub in fldr.dataFolders:
            if recursive_file_search(sub):
                return True
        return False

    loggin_utils.log(
        f"Searching for a '.f3d' file containing '{pm_token}' in folder '{folder.name}' and subfolders..."
    )
    if not recursive_file_search(folder):
        msg = f"No '.f3d' file containing '{pm_token}' was found in '{folder.name}' or its subfolders."
        loggin_utils.log(msg)
        ui.messageBox(msg)
        return False

    loggin_utils.log("File opened and parameters updated.")
    return True

def try_save_active_doc(desc: str = ""):
    """Small helper to safely save whatever document is active."""
    import adsk.core
    app_ = adsk.core.Application.get()
    doc_ = app_.activeDocument
    if not doc_:
        return
    try:
        doc_.save(desc or "update")
    except TypeError:
        # Some environments insist on a string but ignore contents
        doc_.save("")
    try:
        adsk.doEvents()
    except:
        pass

def _set_state(var_name: str, state: str):
    conn = pyodbc.connect(
        "DRIVER={ODBC Driver 17 for SQL Server};SERVER=pschost1;DATABASE=ETDP;UID=sa;PWD=sa",
        autocommit=True
    )
    try:
        with conn.cursor() as cur:
            try:
                # Try stored proc first (if you later create it)
                cur.execute("EXEC dbo.etdp_set_state ?, ?", var_name, state)
            except pyodbc.ProgrammingError as e:
                # Fallback: direct UPDATE (works even if the proc doesn't exist)
                if 'Could not find stored procedure' in str(e):
                    cur.execute(
                        "UPDATE dbo.partSubmissions SET [State] = ? WHERE varName = ?;",
                        state, var_name
                    )
                else:
                    raise
    finally:
        conn.close()

def process_one(var_name: str | None = None):
    global event_data
    # derive var_name if not supplied
    if not var_name:
        var_name = (event_data or {}).get('varName')

    if not var_name:
        loggin_utils.log("[process_one] No var_name available; aborting.")
        return

    # If you already did an atomic claim to 'Running' elsewhere, skip this next line
    _set_state(var_name, 'Running')
    try:
        open_cloud_file()  # ← your existing no-arg function
        _set_state(var_name, 'Finished')
    except Exception:
        _set_state(var_name, 'Error')
        raise

def fetch_Machine_Field(
    Machine: str,
    column_name: str,
    default=None,
):
    """
    Look up a single column from dbo.machAssembly for the varName(MachineName)
    given.

    Example:
        mKO_Len = fetch_event_field_from_sql("HF2", "mKO_Len")

    Returns the value, or `default` if anything fails.
    """

    col = (column_name or "").strip()

    if not Machine:
        loggin_utils.log(f"[sql] event_data has no varName; cannot fetch '{col}'")
        return default

    if not col:
        loggin_utils.log("[sql] empty column_name passed to fetch_event_field_from_sql")
        return default

    # Basic safety: only allow letters, digits, underscore in column name.
    # This prevents SQL injection via column_name.
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", col):
        loggin_utils.log(f"[sql] invalid column name blocked: '{col}'")
        return default

    try:
        conn = pyodbc.connect(
            "DRIVER={ODBC Driver 17 for SQL Server};"
            "SERVER=pschost1;"
            "DATABASE=ETDP;"
            "UID=sa;"
            "PWD=sa;"
        )
    except Exception as ex:
        loggin_utils.log(f"[sql] connection failed: {ex}")
        return default

    try:
        cur = conn.cursor()

        # Column name is injected as identifier (after validation),
        # varName is parameterized.
        sql = f"""
            SELECT TOP 1 [{col}]
            FROM dbo.machAssembly
            WHERE varName = ?
        """
        cur.execute(sql, (Machine,))
        row = cur.fetchone()
        if row is None:
            loggin_utils.log(f"[sql] no row found for varName='{Machine}' when fetching '{col}'")
            return default

        val = row[0]
        loggin_utils.log(f"[sql] {col} for varName='{Machine}' → {val!r}")
        return val

    except Exception as ex:
        loggin_utils.log(f"[sql] error fetching '{col}' for varName='{Machine}': {ex}")
        return default

    finally:
        try:
            conn.close()
        except Exception:
            pass

def fetch_event_field_from_sql(
    event_data: dict,
    column_name: str,
    default=None,
):
    """
    Look up a single column from dbo.partSubmissions for the varName
    found in event_data.

    Example:
        pHeadDiam = fetch_event_field_from_sql(event_data, "pHeadDiam")

    Returns the value, or `default` if anything fails.
    """
    if not isinstance(event_data, dict):
        loggin_utils.log("[sql] event_data is not a dict; aborting SQL fetch.")
        return default

    var_name = str(event_data.get("varName", "") or "").strip()
    col = (column_name or "").strip()

    if not var_name:
        loggin_utils.log(f"[sql] event_data has no varName; cannot fetch '{col}'")
        return default

    if not col:
        loggin_utils.log("[sql] empty column_name passed to fetch_event_field_from_sql")
        return default

    # Basic safety: only allow letters, digits, underscore in column name.
    # This prevents SQL injection via column_name.
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", col):
        loggin_utils.log(f"[sql] invalid column name blocked: '{col}'")
        return default

    try:
        conn = pyodbc.connect(
            "DRIVER={ODBC Driver 17 for SQL Server};"
            "SERVER=pschost1;"
            "DATABASE=ETDP;"
            "UID=sa;"
            "PWD=sa;"
        )
    except Exception as ex:
        loggin_utils.log(f"[sql] connection failed: {ex}")
        return default

    try:
        cur = conn.cursor()

        # Column name is injected as identifier (after validation),
        # varName is parameterized.
        sql = f"""
            SELECT TOP 1 [{col}]
            FROM dbo.partSubmissions
            WHERE varName = ?
            ORDER BY subDate DESC
        """
        cur.execute(sql, (var_name,))
        row = cur.fetchone()
        if row is None:
            loggin_utils.log(f"[sql] no row found for varName='{var_name}' when fetching '{col}'")
            return default

        val = row[0]
        loggin_utils.log(f"[sql] {col} for varName='{var_name}' → {val!r}")
        return val

    except Exception as ex:
        loggin_utils.log(f"[sql] error fetching '{col}' for varName='{var_name}': {ex}")
        return default

    finally:
        try:
            conn.close()
        except Exception:
            pass

def open_cloud_file():
    """
    Opens the B2PM file for the current request, updates its linked recess,
    then moves the B2 timeline to END and performs a single compute+save so
    downstream links (B1/K/B0PP) see the correct, finalized B2 version.
    Finally, hands off to process_b2_b1_and_knockout().
    """
    import time
    global event_data, parts  # Ensure access to both globals
    try:
        if not event_data:
            loggin_utils.log("No SQL data to determine file/folder path.")
            return

        project_name = "Parametric_Models"
        p_bucket = event_data.get('pBucket', '')
        parts = p_bucket.split('-')  # Assign parts
        folder_name = path_utils.parse_folder_name_from_bucket(p_bucket)

        data = app.data
        projects = data.dataProjects
        target_project = next((proj for proj in projects if proj.name == project_name), None)
        if not target_project:
            err = f"Project '{project_name}' not found."
            loggin_utils.log(err)
            ui.messageBox(err, "ETDPFusion")
            return

        root_folder = target_project.rootFolder

        # Find (or create) the target folder
        target_folder = file_ops.find_target_folder(project_name, folder_name)
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

        # -------- B2PM: open & prep --------
        open_first_fusion_design_file(target_folder, parts, event_data, "B2PM")
        #Maybe use the below doc_b2 instead for update
        doc = adsk.core.Application.get().activeDocument
        doc.updateAllReferences()
        # Remember the B2 document so we can come back after opening the recess
        doc_b2 = app.activeDocument
        b2_doc_name = doc_b2.name if doc_b2 else "<none>"
        

        recess_cfg = None  # ensure defined for finally-block

        # Temporarily go BEFORE "Remove" so the recess link is visible/resolvable
        try:
            cloud_operations.move_timeline_before_feature("Remove")
            loggin_utils.log(f"[B2] Before 'Remove'. Active doc: {app.activeDocument.name}")

            # Open the recess design (latest), create/activate the needed config
            recess_cfg = cloud_operations.open_linked_referenced_component(event_data)
            loggin_utils.log(f"[B2] After opening linked. Active doc: {app.activeDocument.name}")

            # === IMPORTANT: Reactivate B2 BEFORE attempting to switch the linked occurrence ===
            try:
                if app.activeDocument is not doc_b2 and doc_b2:
                    doc_b2.activate()
                    try: adsk.doEvents()
                    except: pass
                    time.sleep(0.05)
                    try: app.activeViewport.refresh()
                    except: pass
                loggin_utils.log(f"[B2] Back in B2 for switch: {app.activeDocument.name}")
            except Exception as e:
                loggin_utils.log(f"[B2] Reactivate B2 before switch failed: {e}\n{traceback.format_exc()}")

            # (Optional) make sure we are still before the 'Remove' feature
            try:
                cloud_operations.move_timeline_before_feature("Remove")
            except:
                pass
            # Switch the recess occurrence in B2 to the new config (with retry on transient config-busy)
            if recess_cfg:
                base_token = "-".join((recess_cfg or "").split("-")[:3])  # e.g., "MT-1-11-0063" -> "MT-1-11"

                def _do_switch():
                    # re-yield each attempt to give Fusion time to settle the config table
                    _yield_and_wait(0.05)
                    return cloud_operations.switch_b2_recess_to_config(
                        target_cfg_row=recess_cfg,
                        base_token=base_token,
                        insert_base=None
                    )

                ok_call, ok = call_with_config_retry(_do_switch, tag="[B2 switch]")
                ok = bool(ok) if ok_call else False
                doc = adsk.core.Application.get().activeDocument
                doc.updateAllReferences()
                loggin_utils.log(f"[B2] Switch recess to '{recess_cfg}' → {'OK' if ok else 'FAILED'}")
            else:
                loggin_utils.log("[B2] open_linked_referenced_component did not return a recess config name.")
    
        finally:
            # Reactivate original B2 doc (if needed)
            try:
                if doc_b2 and app.activeDocument is not doc_b2:
                    doc_b2.activate()
                    try: adsk.doEvents()
                    except: pass
                    try: app.activeViewport.refresh()
                    except: pass
                    loggin_utils.log(f"[B2] Reactivated original doc: {b2_doc_name}")
            except Exception as e:
                loggin_utils.log(f"[B2] Reactivate original doc failed: {e}\n{traceback.format_exc()}")

            # Move timeline to END (strong), then single compute+save
            try:
                file_ops.move_timeline_to_end()
            except Exception:
                # Fallback: local simple move to end if helper not available
                try:
                    design = adsk.fusion.Design.cast(app.activeProduct)
                    if design:
                        design.timeline.markerPosition = design.timeline.count - 1
                except:
                    pass

            try:
                adsk.doEvents()
                app.activeViewport.refresh()
            except:
                pass

            desc = f"B2: switched recess to {recess_cfg}" if recess_cfg else "B2: updated"
            if not finalize_compute_and_save_current(desc):
                loggin_utils.log("[B2] final save failed")
            loggin_utils.log(f"[B2] Timeline at END and saved. Active doc: {app.activeDocument.name}")

        # Hand off to your processing step (measure, SQL, balance, cutoff, K-config, B0PP)
        process_b2_b1_and_knockout(root_folder, target_folder, parts, event_data)
        # Build JSON for OpenSeekInspect_main run

        pHeadDiam = fetch_event_field_from_sql(event_data, "pHeadDiam", default=None)

        pShankDiam = fetch_event_field_from_sql(event_data, "pShankDiam", default=None)

        pLen = fetch_event_field_from_sql(event_data, "pLen", default=None)

        pB1Machine = fetch_event_field_from_sql(event_data, "pB1Machine", default=None)

        pB2Machine = fetch_event_field_from_sql(event_data, "pB2Machine", default=None)

        mKO_Len = fetch_Machine_Field("HF2", "mKO_Len", default=None)

        pShankDiamHF = fetch_event_field_from_sql(event_data, "pShankDiamHF", default=None)

        pLenHF = fetch_event_field_from_sql(event_data, "pLenHF", default=None)

        pRecShape = fetch_event_field_from_sql(event_data, "pRecShape", default=None)

        pRecSpec = fetch_event_field_from_sql(event_data, "pRecSpec", default=None)


        if (pLen == "NA"):
            pLen = fetch_event_field_from_sql(event_data, "pShankLen", default=None)
            pLenHF = fetch_event_field_from_sql(event_data, "pShankLenHF", default=None)


        #pHeadDiam = fetch_event_field_from_sql(event_data, "pHeadDiam", default=None)

        if pHeadDiam is not None:
            # Option 1: store back into event_data so JSON can see it
            event_data["pHeadDiam"] = pHeadDiam
        else:
            loggin_utils.log("[open_cloud_file] pHeadDiam not found; leaving unset.")

        try:
            create_inputs_json(
                event_data=event_data,
                parts=parts,
                folder_name=folder_name,
                recess_cfg=recess_cfg,           # returned earlier from open_linked_referenced_component
                project_hint="parametric_models",
                open_drawing=True,
                debug_scan=False,
                pHeadDiam=pHeadDiam,
                pShankDiam=pShankDiam,
                pShankDiamHF=pShankDiamHF,
                pLen=pLen,
                pLenHF=pLenHF,
                pB2Machine=pB2Machine,
                pB1Machine=pB1Machine,
                mKO_Len=mKO_Len,
                pRecShape=pRecShape,
                pRecSpec=pRecSpec
            )
        except Exception:
            loggin_utils.log("[json] create_inputs_json failed (non-fatal).")
    except Exception as e:
        loggin_utils.log(f"Exception in open_cloud_file(): {e}\n{traceback.format_exc()}")

#remove -W
def open_neighbor_working_f2d(add_suffix: str = "-W") -> bool:
    """
    Open the .f2d 'working copy' that lives next to the active .f3d.
    Example: active = KHF-B0PP-##MT-PT-FLT-#.f3d  -> open KHF-B0PP-##MT-PT-FLT-#-W.f2d
    Returns True on success.
    """
    try:
        app = adsk.core.Application.get()
        doc = app.activeDocument
        if not doc:
            try: loggin_utils.log("[draw-open] ❌ No active document.")
            except: pass
            return False

        df = getattr(doc, 'dataFile', None)
        if not df:
            try: loggin_utils.log("[draw-open] ❌ Active document is not a cloud data file (no DataFile).")
            except: pass
            return False

        base_name = df.name                   # name WITHOUT extension
        parent    = getattr(df, 'parentFolder', None)
        if not parent:
            try: loggin_utils.log("[draw-open] ❌ Can't resolve parent folder.")
            except: pass
            return False

        target_name = f"{base_name}"  # e.g. "…-W"
        try: loggin_utils.log(f"[draw-open] Looking for '{target_name}.f2d' in '{parent.name}'")
        except: pass

        # ---- Fast path: your helper, if present ----
        try:
            if 'file_ops' in globals() and hasattr(file_ops, 'open_latest_datafile_strict'):
                design, err = file_ops.open_latest_datafile_strict(app, parent, target_name, 'f2d')
                if design:
                    try: loggin_utils.log(f"[draw-open] ✅ Opened via helper: {target_name}.f2d")
                    except: pass
                    return True
                else:
                    try: loggin_utils.log(f"[draw-open] helper miss: {err or 'not found'}")
                    except: pass
        except Exception as e:
            try: loggin_utils.log(f"[draw-open] helper error: {e}\n{traceback.format_exc()}")
            except: pass

        # ---- Fallback: raw API scan in the same folder ----
        matches = []
        try:
            files = getattr(parent, 'dataFiles', None)
            if files:
                for i in range(files.count):
                    f = files.item(i)
                    if (getattr(f, 'name', '') == target_name and
                        str(getattr(f, 'fileExtension', '')).lower() == 'f2d'):
                        matches.append(f)
        except Exception as e:
            try: loggin_utils.log(f"[draw-open] folder scan failed: {e}")
            except: pass

        if not matches:
            try: loggin_utils.log(f"[draw-open] ❌ No '{target_name}.f2d' found next to the active file.")
            except: pass
            return False

        # If multiple, grab highest version number
        def _ver(dfobj): return int(getattr(dfobj, 'versionNumber', 0) or 0)
        target_df = sorted(matches, key=_ver)[-1]

        # Open it
        opened = None
        try:
            # Most builds support this:
            opened = app.data.open(target_df)
        except Exception:
            try:
                # Some builds accept Documents.open(DataFile)
                opened = app.documents.open(target_df)
            except Exception as e2:
                try: loggin_utils.log(f"[draw-open] ❌ Open failed: {e2}")
                except: pass
                return False

        try:
            adsk.doEvents()
            app.activeViewport.refresh()
        except: pass

        try:
            dn = getattr(target_df, 'name', '<unnamed>')
            vv = getattr(target_df, 'versionNumber', None)
            loggin_utils.log(f"[draw-open] ✅ Opened drawing: {dn}.f2d (v{vv})")
        except: pass

        return opened is not None

    except Exception as e:
        try: loggin_utils.log(f"[draw-open] Exception: {e}\n{traceback.format_exc()}")
        except: pass
        return False
import time, traceback
import adsk.core, adsk.fusion

def _sleep_events(sec: float = 0.20):
    try:
        adsk.doEvents()
    except:
        pass
    time.sleep(sec)


def update_active_drawing_to_latest(save_desc: str = "Update drawing references to latest") -> bool:
    """
    Updates references for the ACTIVE drawing tab (.f2d).
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
        doc = app.activeDocument
        if not doc:
            loggin_utils.log("[drawing-update] ❌ No active document.")
            return False

        # Prefer type check first
        draw_doc = None
        try:
            # GOOD inside the function:
            from adsk import drawing as adsk_drawing
            draw_doc = adsk_drawing.DrawingDocument.cast(doc)
        except Exception as e:
            loggin_utils.log(f"[drawing-update] cast to DrawingDocument failed: {e}")

        # If cast failed, fall back to extension check (sometimes empty/None)
        if not draw_doc:
            df  = getattr(doc, "dataFile", None)
            ext = (getattr(df, "fileExtension", "") or "").lower() if df else ""
            if ext != "f2d":
                loggin_utils.log(f"[drawing-update] Skipping: active doc not a drawing (ext='{ext}').")
                return False

        # -------- (optional) inventory refs for logging --------
        refs = None
        try:
            refs = getattr(draw_doc, "documentReferences", None) if draw_doc else None
            cnt  = int(getattr(refs, "count", 0) or 0) if refs else 0
            loggin_utils.log(f"[drawing-update] documentReferences.count={cnt}")
            for i in range(cnt):
                try:
                    r   = refs.item(i)
                    rdf = getattr(r, "dataFile", None)
                    nm  = getattr(rdf, "name", "?")
                    ver = getattr(rdf, "versionNumber", "?")
                    lat = getattr(rdf, "latestVersionNumber", "?")
                    ood = getattr(r, "isOutOfDate", "?")
                    loggin_utils.log(f"[drawing-update] ref[{i}] '{nm}' v={ver} latest={lat} outOfDate={ood}")
                except Exception as e:
                    loggin_utils.log(f"[drawing-update] ref[{i}] detail failed: {e}")
        except Exception:
            pass

        # -------- 1) Document-level method attempts --------
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
                loggin_utils.log(f"[drawing-update] {meth}() invoked ✔")
                break
            except Exception as e:
                loggin_utils.log(f"[drawing-update] {meth}() failed: {e}")

        # -------- 2) Per-reference updates (if needed) --------
        if not updated and refs and getattr(refs, "count", 0):
            try:
                any_called = False
                for i in range(refs.count):
                    try:
                        r = refs.item(i)
                        # try a bunch of plausible method names
                        for nm in ("updateToLatestVersion", "updateToLatest", "updateReference",
                                   "update", "getLatestVersion", "getLatest"):
                            fn = getattr(r, nm, None)
                            if fn:
                                try:
                                    fn()
                                    any_called = True
                                    loggin_utils.log(f"[drawing-update] ref[{i}].{nm}() ✔")
                                    break
                                except Exception as e:
                                    loggin_utils.log(f"[drawing-update] ref[{i}].{nm}() failed: {e}")
                    except Exception as e:
                        loggin_utils.log(f"[drawing-update] per-ref update error: {e}")
                if any_called:
                    updated = True
            except Exception as e:
                loggin_utils.log(f"[drawing-update] per-reference block failed: {e}")

        # -------- 3) commandDefinitions dynamic scan --------
        if not updated:
            try:
                cdefs = ui.commandDefinitions
                candidates = []
                # collect anything that looks like a drawing update / latest
                for i in range(getattr(cdefs, "count", 0)):
                    cd = cdefs.item(i)
                    cid = getattr(cd, "id", "")
                    nm  = getattr(cd, "name", "")
                    if ("draw" in cid.lower() or "draw" in nm.lower()) and \
                       ("update" in cid.lower() or "latest" in cid.lower() or "reference" in cid.lower()):
                        candidates.append(cd)
                # try known IDs first (common builds), then the rest
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
                            loggin_utils.log(f"[drawing-update] command {pid} executed ✔")
                            break
                        except Exception as e:
                            loggin_utils.log(f"[drawing-update] command {pid} failed: {e}")
                if not updated:
                    for cd in candidates:
                        cid = getattr(cd, "id", "")
                        if cid in tried_ids:
                            continue
                        try:
                            cd.execute()
                            updated = True
                            loggin_utils.log(f"[drawing-update] command {cid} executed ✔")
                            break
                        except Exception as e:
                            loggin_utils.log(f"[drawing-update] command {cid} failed: {e}")
            except Exception as e:
                loggin_utils.log(f"[drawing-update] commandDefinitions scan failed: {e}")

        # -------- 4) TextCommandWindow fallback --------
        if not updated:
            try:
                # try a few plausible command IDs via text commands
                for tcmd in (
                    "Commands.Start FusionDrawingUpdateReferencesCmd",
                    "Commands.Start DrawingUpdateReferencesCmd",
                    "Commands.Start FusionDrawingUpdateAllReferencesCmd",
                    "Commands.Start DrawingUpdateAllReferencesCmd",
                ):
                    try:
                        app.executeTextCommand(tcmd)
                        updated = True
                        loggin_utils.log(f"[drawing-update] textcmd '{tcmd}' executed ✔")
                        break
                    except Exception as e:
                        loggin_utils.log(f"[drawing-update] textcmd '{tcmd}' failed: {e}")
            except Exception as e:
                loggin_utils.log(f"[drawing-update] textcmd block failed: {e}")

        if not updated:
            loggin_utils.log("[drawing-update] No suitable update path found.")
            return False

        # give Fusion a moment to apply
        _sleep_events(0.35)

        # save if modified
        try:
            if getattr(doc, "isModified", False):
                try:
                    doc.save(save_desc)
                except TypeError:
                    doc.save("")
                loggin_utils.log("[drawing-update] Saved drawing after update.")
        except Exception as e:
            loggin_utils.log(f"[drawing-update] Save failed: {e}")

        return True

    except Exception as e:
        loggin_utils.log(f"[drawing-update] Exception: {e}\n{traceback.format_exc()}")
        return False

#this looks to be a doubled up method
def open_neighbor_working_f2d(add_suffix: str = "-W", return_doc: bool = False):
    """
    Open the .f2d 'working' drawing next to the active .f3d and ACTIVATE it.
    Returns True, or (True, Document) when return_doc=True.
    """
    opened_doc = None
    try:
        app = adsk.core.Application.get()
        act = app.activeDocument
        if not act:
            try: loggin_utils.log("[draw-open] ❌ No active document.")
            except: pass
            return (False, None) if return_doc else False

        df = getattr(act, 'dataFile', None)
        if not df:
            try: loggin_utils.log("[draw-open] ❌ Active doc is not a cloud DataFile.")
            except: pass
            return (False, None) if return_doc else False

        parent = getattr(df, 'parentFolder', None)
        if not parent:
            try: loggin_utils.log("[draw-open] ❌ Could not resolve parent folder.")
            except: pass
            return (False, None) if return_doc else False

        base_name   = df.name  # no extension
        target_name = f"{base_name}"
        try: loggin_utils.log(f"[draw-open] Looking for '{target_name}.f2d' in '{parent.name}'")
        except: pass

        # Prefer your helper
        try:
            if 'file_ops' in globals() and hasattr(file_ops, 'open_latest_datafile_strict'):
                design_or_doc, err = file_ops.open_latest_datafile_strict(app, parent, target_name, 'f2d')
                if design_or_doc:
                    # Normalize to Document
                    opened_doc = getattr(design_or_doc, 'document', None) or (
                        design_or_doc if isinstance(design_or_doc, adsk.core.Document) else None
                    )
                else:
                    try: loggin_utils.log(f"[draw-open] helper miss: {err or 'not found'}")
                    except: pass
        except Exception as e:
            try: loggin_utils.log(f"[draw-open] helper error: {e}\n{traceback.format_exc()}")
            except: pass

        # Fallback: scan folder & open highest version
        if opened_doc is None:
            files = getattr(parent, 'dataFiles', None)
            matches = []
            if files:
                for i in range(files.count):
                    f = files.item(i)
                    if f.name == target_name and str(getattr(f, 'fileExtension', '')).lower() == 'f2d':
                        matches.append(f)
            if not matches:
                try: loggin_utils.log(f"[draw-open] ❌ No '{target_name}.f2d' found.")
                except: pass
                return (False, None) if return_doc else False

            matches.sort(key=lambda d: int(getattr(d, 'versionNumber', 0) or 0))
            target_df = matches[-1]
            try:
                opened_doc = app.data.open(target_df)  # returns Document
            except Exception:
                opened_doc = app.documents.open(target_df)

        # Ensure it is ACTIVE
        if opened_doc:
            try:
                opened_doc.activate()
            except:
                pass
            _sleep_events(0.25)
            try:
                loggin_utils.log(f"[draw-open] ✅ Opened/activated: {opened_doc.name}")
            except:
                pass
            return ((True, opened_doc) if return_doc else True)

        return (False, None) if return_doc else False

    except Exception as e:
        try: loggin_utils.log(f"[draw-open] Exception: {e}\n{traceback.format_exc()}")
        except: pass
        return (False, None) if return_doc else False

def process_b2_b1_and_knockout(root_folder, target_folder, parts, event_data):
    """
    Runs after B2 is opened and timeline is restored to END.
    - Measure B2 volume/mass
    - Fetch SQL (pB1Machine, b0Diam)
    - If needed, open B1 and balance to B2  --> SAVE B1 (once, after generate)
    - Compute cutoff length and knockout config name
    - Open K-OD-LEN-CONF and activate that config  --> SAVE K
    - Finally, open B0PP and configure it, then Get Latest + regenerate row + SAVE once
    """
    #import math, traceback, pyodbc

    CC_PER_IN3 = 16.387064

    # Small helpers (local) to build expected config row names
    def _b1_size_token(p2: str) -> str:
        pu = (p2 or "").upper()
        return p2[:-2] #if pu.endswith("MT") else p2

    def _build_b1_row(parts_list):
        if len(parts_list) < 6:
            return None
        return f"{parts_list[0]}-B1PM-{_b1_size_token(parts_list[2])}-{parts_list[3]}-{parts_list[4]}-{parts_list[5]}"

    def _build_b2_row(parts_list):
        if len(parts_list) < 6:
            return None
        return f"{parts_list[0]}-B2PM-{parts_list[2]}-{parts_list[3]}-{parts_list[4]}-{parts_list[5]}"

    def _build_b0pp_row(parts_list):
        if len(parts_list) < 6:
            return None
        return f"{parts_list[0]}-B0PP-{parts_list[2]}-{parts_list[3]}-{parts_list[4]}-{parts_list[5]}"

    try:
        # Measure B2 at END of timeline
        b2 = volume_logic.measure_current_design()
        if not b2.get("ok"):
            loggin_utils.log(f"[B2] ❌ Measure failed: {b2.get('reason')}")
            return

        if "volume_in3" in b2:
            b2_in3 = float(b2["volume_in3"])
            b2_cc  = float(b2.get("volume_cc", b2_in3 * CC_PER_IN3))
            b2_mass = float(b2.get("mass_g", 0.0))
        else:
            b2_cc  = float(b2.get("volume", 0.0))
            b2_in3 = b2_cc / CC_PER_IN3
            b2_mass = float(b2.get("mass", 0.0))
        loggin_utils.log(f"[B2] volume = {b2_in3:.6f} in^3 ({b2_cc:.6f} cm^3), mass = {b2_mass:.6f} g")

        # -------- SQL: get pB1Machine and b0Diam (inches) --------
        try:
            conn = pyodbc.connect(
                "DRIVER={ODBC Driver 17 for SQL Server};"
                "SERVER=pschost1;DATABASE=ETDP;UID=sa;PWD=sa;"
            )
            cursor = conn.cursor()
            var_name = event_data.get('varName')
            cursor.execute(
                "SELECT pB1Machine, b0Diam FROM dbo.partSubmissions WHERE varName = ?",
                var_name
            )
            row = cursor.fetchone()
        except Exception as e:
            loggin_utils.log(f"SQL error reading pB1Machine/b0Diam: {e}\n{traceback.format_exc()}")
            row = None
        finally:
            try: conn.close()
            except: pass

        b0Diam_in = None
        b0Len_in = None
        pB1Machine_val = "NA"

        if row:
            pB1Machine_val = str(row[0]).strip().upper() if row[0] is not None else "NA"
            try:
                b0Diam_in = float(row[1]) if row[1] is not None else None
            except Exception:
                b0Diam_in = None
            loggin_utils.log(f"SQL pB1Machine value: {pB1Machine_val}")
        else:
            loggin_utils.log(f"No SQL row found for varName: {event_data.get('varName')} — skipping B1PM and cutoff calc")

        # -------- B1PM (only if pB1Machine != 'NA') --------
        if row and pB1Machine_val != "NA":
            open_first_fusion_design_file(target_folder, parts, event_data, "B1PM")
            doc = adsk.core.Application.get().activeDocument
            doc.updateAllReferences()
            b1_pre = volume_logic.measure_current_design()
            if b1_pre.get("ok"):
                if "volume_in3" in b1_pre:
                    loggin_utils.log(
                        f"[B1] initial volume = {b1_pre['volume_in3']:.6f} in^3 "
                        f"({b1_pre.get('volume_cc', 0.0):.6f} cm^3), "
                        f"mass = {b1_pre.get('mass_g', 0.0):.6f} g"
                    )
                else:
                    _cc = float(b1_pre.get("volume", 0.0))
                    _in3 = _cc / CC_PER_IN3
                    _g = float(b1_pre.get("mass", 0.0))
                    loggin_utils.log(f"[B1] initial volume = {_in3:.6f} in^3 ({_cc:.6f} cm^3), mass = {_g:.6f} g")
            else:
                loggin_utils.log(f"[B1] ⚠️ Pre-measure failed: {b1_pre.get('reason')}")

            # Balance B1 to match B2 volume (in³)
            result = volume_logic.balance_b1_to_volume(
                target_b2_volume=b2_in3,
                primary_tol=0.01, final_tol=0.001,
                primary_step=0.01, fine_step=0.001,
                max_iters=200, autodetect_direction=True,
            )
            loggin_utils.log(f"[B1 balance] {result}")

            # Save B1 once with generate before saving (consistent with B2/B0PP)
            try:
                finalize_compute_and_save_current("B1: balanced to B2 volume")
            except Exception as e:
                loggin_utils.log(f"[B1] save failed: {e}")

        elif row:
            loggin_utils.log("Skipping B1PM: pB1Machine is NA")

        # ---- Cutoff AFTER balancing (B2 volume in in³) ----
        if b0Diam_in is not None and b0Diam_in > 0:
            area_in2 = math.pi * (b0Diam_in * 0.5) ** 2
            b0Len_in = b2_in3 / area_in2
            loggin_utils.log(
                f"[Cutoff] Using B2={b2_in3:.6f} in^3 and B0Diam={b0Diam_in:.6f} in -> B0Len={b0Len_in:.6f} in"
            )
            loggin_utils.log(f"{b0Len_in:.6f} = {b2_in3:.6f}/(pi({b0Diam_in}/2)^2)")

        # ---- Knockout config (only if we have both OD and LEN) ----
        knockoutpin = None
        if b0Diam_in and b0Len_in:
            try:
                od_tok  = file_ops.fmt_no_dot_3(b0Diam_in)
                len_tok = file_ops.fmt_no_dot_3(b0Len_in)
            except Exception:
                from decimal import Decimal, ROUND_HALF_UP
                def _fmt_no_dot_3_local(x: float) -> str:
                    q = Decimal(x).quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)
                    token = int((q * 1000).to_integral_value(rounding=ROUND_HALF_UP))
                    s = str(token)
                    if q < Decimal('1.000'):
                        s = s.zfill(4)
                    return s
                od_tok  = _fmt_no_dot_3_local(b0Diam_in)
                len_tok = _fmt_no_dot_3_local(b0Len_in)

            knockoutpin = f"K-{od_tok}-{len_tok}-RND"
            loggin_utils.log(f"[Knockout] Target config: {knockoutpin} (OD={b0Diam_in:.3f} in, LEN={b0Len_in:.3f} in)")

            # ---- Open/activate K-OD-LEN-CONF.f3d directly ----
            try:
                k_folder = file_ops._get_subfolder_by_name(root_folder, 'K-OD-LEN-CONF')
                if not k_folder:
                    loggin_utils.log("❌ K folder 'K-OD-LEN-CONF' not found under project root.")
                    return

                design_k, err = file_ops.open_latest_datafile_strict(app, k_folder, 'K-OD-LEN-CONF', 'f3d')
                if not design_k:
                    loggin_utils.log(err or "❌ Could not open latest K-OD-LEN-CONF.")
                    return

                try:
                    df_now   = design_k.document.dataFile
                    open_num = getattr(df_now, 'versionNumber', None)
                    loggin_utils.log(f"[K] Opened '{df_now.name}' at version {open_num}")
                except:
                    pass

                try:
                    adsk.doEvents()
                    app.activeViewport.refresh()
                except:
                    pass

                ok = file_ops.activate_knockout_config(design_k, knockoutpin)
                doc = adsk.core.Application.get().activeDocument
                doc.updateAllReferences()
                try:
                    table = design_k.configurationTopTable
                    act = table.activeRow.name if table and table.activeRow else "<none>"
                    loggin_utils.log(f"[Knockout] Active config after activation: {act}")
                except:
                    pass

                if not ok:
                    ok = file_ops.ensure_knockout_config_exists(design_k, knockoutpin, b0Diam_in, b0Len_in)

                ar = getattr(getattr(design_k, 'configurationTopTable', None), 'activeRow', None)
                loggin_utils.log(f"[K] activeRow = {getattr(ar, 'name', None)!r} (ok={ok})")

                # Save K so the created/activated row is published
                try:
                    kdoc = design_k.document
                    try:
                        kdoc.save(f"K: activated/ensured {knockoutpin}")
                    except TypeError:
                        kdoc.save("")
                    try:
                        adsk.doEvents()
                    except:
                        pass
                except Exception as e:
                    loggin_utils.log(f"[K] save failed: {e}")

            except Exception as e:
                loggin_utils.log(f"[Knockout] Open/activate failed: {e}\n{traceback.format_exc()}")
        else:
            loggin_utils.log("[Knockout] Skipped: missing b0Diam_in or b0Len_in.")
            
        cloud_operations.wait_for_upload_idle(tag="[B2→B0PP barrier]", idle_window_s=2.5, timeout_s=180.0)
        # --------------------------------------------------------
        # 6) Open B0PP design and configure it (no globals) + mirror into row 0
        # --------------------------------------------------------
        try:
            loggin_utils.log("[B0PP] Opening B0PP design...")
            opened = open_first_fusion_design_file(target_folder, parts, event_data, "B0PP")
            doc = adsk.core.Application.get().activeDocument
            doc.updateAllReferences()
            if not opened:
                loggin_utils.log("[B0PP] ❌ Could not locate/open B0PP file with pattern {}.")
            else:
                loggin_utils.log("[B0PP] ✅ Opened B0PP design.")
            
                row_name_b0pp = _build_b0pp_row(parts)
                b1_row_name   = _build_b1_row(parts)
                b2_row_name   = _build_b2_row(parts)
                loggin_utils.log(f"B2:{b2_row_name} B1:{b1_row_name} B0:{row_name_b0pp}")
                # right after you compute:
                #   row_name_b0pp = _build_b0pp_row(parts)
                #   b1_row_name   = _build_b1_row(parts)
                #   b2_row_name   = _build_b2_row(parts)

                try:
                    b1_key   = _build_filename_pattern(parts, "B1PM")   # e.g. KHF-B1PM-##-PT-FLT-#
                    b2_key   = _build_filename_pattern(parts, "B2PM")   # e.g. KHF-B2PM-##MT-PT-FLT-#
                    b0pp_key = _build_filename_pattern(parts, "B0PP")   # e.g. KHF-B0PP-##MT-PT-FLT-#
                except ValueError as e:
                    loggin_utils.log(f"[pattern] parts malformed: {e}")
                    return  # or handle however you prefer
                loggin_utils.log(f"B2 key:{b2_key} B1 key:{b1_key} B0 key:{b0pp_key}")

                # Knockout family can come from SQL/event_data later; default to your current name
                k_family = (event_data or {}).get("kFamilyName") or "K-OD-LEN-CONF"

                insert_targets = {}
                if knockoutpin:
                    insert_targets[k_family] = knockoutpin
                if b1_row_name:
                    insert_targets[b1_key] = b1_row_name
                if b2_row_name:
                    insert_targets[b2_key] = b2_row_name

                # ... later, when you switch back to the B0PP template:
                template_name = b0pp_key

                loggin_utils.log(f"[B0PP] row={row_name_b0pp}, inserts={insert_targets}")

                # Configure B0PP at the computed row (your existing logic)
                ok_cfg = config_logic.configure_current_doc_with_inserts_and_params(
                    row_name=row_name_b0pp,
                    insert_targets=insert_targets,
                    b0_diam_in=b0Diam_in,          # inches from SQL
                    multiplier=6,
                    b1x_col_title="b1X",
                    b2x_col_title="b2X",
                    save=False,                    # save after pulling latest refs
                    save_desc_prefix="B0PP configured"
                )

                # Pull latest linked versions (B2/B1/K), regenerate configured row, then mirror into row 0 and save once
                try:
                    design_b0pp = adsk.fusion.Design.cast(app.activeProduct)

                    got_latest = try_get_all_latest()
                    loggin_utils.log(f"[B0PP] Get All Latest -> {'OK' if got_latest else 'N/A'}")

                    # Ensure the freshly configured row is regenerated
                    if row_name_b0pp:
                        regenerate_row(design_b0pp, row_name_b0pp)

                    # --- Switch back to the template/first ("##") row ---
                    try:
                        template_name = _build_filename_pattern(parts, "B0PP")
                        loggin_utils.log(f"B0PP search name: {template_name}")
                        #template_name = "KHF-B0PP-##MT-PT-FLT-#"
                        '''
                        switched = _activate_config_row(
                            design_b0pp,
                            target_name=template_name,
                            regen=True,          # keep True if you want a recompute on the template row
                            save=False,          # set True if you want a save here
                            save_desc=f"B0PP switched back to {template_name}"
                        )
                        loggin_utils.log(f"[B0PP] Switch back to '{template_name}' -> {'OK' if switched else 'FAILED'}")'''
                        # Configure B0PP at the computed row (your existing logic)
                        ok_cfg = config_logic.configure_current_doc_with_inserts_and_params(
                            row_name=template_name,
                            insert_targets=insert_targets,
                            b0_diam_in=b0Diam_in,          # inches from SQL
                            multiplier=6,
                            b1x_col_title="b1X",
                            b2x_col_title="b2X",
                            save=False,                    # save after pulling latest refs
                            save_desc_prefix="B0PP configured"
                        )
                        got_latest = try_get_all_latest()
                        loggin_utils.log(f"[B0PP] Get All Latest -> {'OK' if got_latest else 'N/A'}")
                        try:
                            # If you have a regenerate_row(design_b0pp, name) util, call it for the template:
                            try:
                                #KHF-B0PP-##MT-PT-FLT-#
                                regenerate_row(design_b0pp, template_name)
                            except: pass

                            doc = adsk.core.Application.get().activeDocument
                            try:
                                doc.updateAllReferences()
                                doc.save("B0PP: set 25MT + mirrored to ##MT")

                                # IMPORTANT: wait for cloud to finish uploading the new B0PP version
                                cloud_operations.wait_for_upload_idle(
                                    tag="[B0PP post-save barrier]",
                                    idle_window_s=3.0,
                                    timeout_s=240.0
                                )

                                # (Optional but helps): force another reference update after upload settles
                                try:
                                    doc.updateAllReferences()
                                except:
                                    pass

                            except TypeError:
                                doc.updateAllReferences()
                                doc.save("")
                            loggin_utils.log("[B0PP] 💾 Saved after mirroring to template.")
                        except Exception as e:
                            loggin_utils.log(f"[B0PP] Save failed: {e}")
                    except Exception as e:
                        loggin_utils.log(f"[B0PP] Switch-back error: {e}\n{traceback.format_exc()}")
                except Exception as e:
                    loggin_utils.log(f"[B0PP] finalize error: {e}\n{traceback.format_exc()}")

                loggin_utils.log(f"[B0PP] Final configure result: {'OK' if ok_cfg else 'FAILED'}")

        except Exception as e:
            loggin_utils.log(f"[B0PP] Exception while opening/configuring: {e}\n{traceback.format_exc()}")
        # Ensure any previous save(s) finished uploading before opening B0PP
        cloud_operations.wait_for_upload_idle(tag="[B2→B0PP barrier]", idle_window_s=2.5, timeout_s=180.0)
        help = try_get_all_latest()
        loggin_utils.log(f"[B0PP] Get All Latest -> {'OK' if help else 'N/A'}")
        #update_active_drawing_to_latest()
        #_active_datafile_needs_latest(app)
        # … your B0PP configure + mirror + save …
        # --------------------------------------------------------
        # Open working drawing (B0PP) and update to latest SAFELY
        # --------------------------------------------------------
        ok_draw, draw_doc = open_neighbor_working_f2d(return_doc=True)
        loggin_utils.log(f"[B0PP] Open working drawing -> {'OK' if ok_draw else 'FAILED'}")

        if ok_draw and draw_doc:
            try:
                draw_doc.activate()
            except:
                pass

            # CRITICAL: wait until the B0PP save/upload is fully committed
            cloud_operations.wait_for_upload_idle(
                tag="[Drawing pre-latest barrier]",
                idle_window_s=2.5,
                timeout_s=180.0
            )

            # small UI settle
            _sleep_events(1.0)

            # NOW it is safe to update drawing references
            upd_ok = update_active_drawing_to_latest()
            loggin_utils.log(f"[B0PP] Drawing update to latest -> {'OK' if upd_ok else 'FAILED/NO-OP'}")
        
        #unsure if the above is working below is to create the .csv
        try:
            PY_EXE     = r"C:\Users\ETDP\AppData\Local\Programs\Python\Python313\python.exe"
            SQL_CREATE = r"P:\ETDP\Scripts\AutomateScripts\sql_create.py"

            # Optional: set varname dynamically later (your sql_create.py supports env override)
            # If you want to keep 'ps12' just leave this commented.
            # desired_varname = (row_name_b0pp or "").strip() or "ps12"
            # env = {**os.environ, "PARTSUB_VARNAME": desired_varname}
            env = os.environ.copy()  # uses default 'ps12' inside sql_create.py
            
            try:
                from .sql_create import export_part_and_material  # adjust import if needed

                desired_varname = (row_name_b0pp or "").strip()
                if not desired_varname:
                    raise RuntimeError("row_name_b0pp is empty; cannot export CSVs")

                part_csv, mat_csv = export_part_and_material(desired_varname)

                loggin_utils.log(
                    f"[B0PP] CSVs created:\n"
                    f"  part={part_csv}\n"
                    f"  mat={mat_csv}"
                )

                if part_csv and not os.path.exists(part_csv):
                    loggin_utils.log(f"[B0PP][WARN] Part CSV not found on disk: {part_csv}")
                if mat_csv and not os.path.exists(mat_csv):
                    loggin_utils.log(f"[B0PP][WARN] Material CSV not found on disk: {mat_csv}")

            except Exception as e:
                loggin_utils.log(
                    f"[B0PP] export_part_and_material failed for varName='{row_name_b0pp}': "
                    f"{e}\n{traceback.format_exc()}"
                )
        except Exception as e:
            loggin_utils.log(f"[B0PP] Exception running sql_create.py: {e}\n{traceback.format_exc()}")
# ---------------------------------------------------------------------------
    except Exception as e:
        loggin_utils.log(f"Exception in process_b2_b1_and_knockout(): {e}\n{traceback.format_exc()}")

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────
def _exec_rowcount(sql: str, params=()):
    """Use your sql_interface; fallback to pyodbc if needed."""
    try:
        return sql_interface.exec_rowcount(sql, params)
    except AttributeError:
        import pyodbc
        cn = pyodbc.connect(
            "DRIVER={ODBC Driver 17 for SQL Server};SERVER=pschost1;DATABASE=ETDP;UID=sa;PWD=sa",
            autocommit=True
        )
        try:
            cur = cn.cursor()
            cur.execute(sql, params)
            return cur.rowcount
        finally:
            cn.close()

def _wait_until_done(var_name: str, timeout_sec: float = 600.0, poll_sec: float = 1.0) -> str | None:
    """
    Poll the row's State until it is FINISHED or ERROR, or timeout.
    Returns the final state ('Finished'/'Error') or None on timeout/unknown.
    """
    import time
    deadline = time.time() + timeout_sec
    q = """
        SELECT UPPER(LTRIM(RTRIM([State])))
        FROM dbo.partSubmissions
        WHERE varName = ?
    """
    while time.time() < deadline and not stop_thread:
        try:
            s = sql_interface.scalar_value(q, (var_name,))
        except AttributeError:
            # minimal fallback if scalar_value isn't in your sql_interface
            import pyodbc
            cn = pyodbc.connect(
                "DRIVER={ODBC Driver 17 for SQL Server};SERVER=pschost1;DATABASE=ETDP;UID=sa;PWD=sa",
                autocommit=True
            )
            try:
                cur = cn.cursor()
                cur.execute(q, (var_name,))
                row = cur.fetchone()
                s = row[0] if row else None
            finally:
                cn.close()

        if s in ('FINISHED', 'ERROR'):
            return 'Finished' if s == 'FINISHED' else 'Error'
        time.sleep(poll_sec)
    return None  # timed out or stopping

# ──────────────────────────────────────────────────────────────────────────────
# Continuous watcher
# ──────────────────────────────────────────────────────────────────────────────

def threaded_worker():
    """
    Continuous background loop:
      - fetch newest candidate (your existing fetch_recent_rows logic)
      - ensure Ready
      - claim → Running
      - set event_data and trigger cmd_def.execute()
      - wait until Finished/Error
      - repeat until stop_thread is True
    """
    global event_data, stop_thread

    import time
    loggin_utils.log("Worker thread started (continuous mode).")

    POLL_NO_ROW_SEC    = 10.0
    POLL_NOT_READY_SEC = 5.0
    COOLDOWN_AFTER_JOB = 1.0

    while not stop_thread:
        try:
            # 1) newest candidate (you already filter last 2 days)
            row = sql_interface.fetch_recent_rows(OPEN_CMD_ID)
            if not row:
                time.sleep(POLL_NO_ROW_SEC)
                continue

            var_name = row.get('varName') if isinstance(row, dict) else None
            if not var_name:
                time.sleep(POLL_NO_ROW_SEC)
                continue

            # 2) ensure Ready
            try:
                is_ready = sql_interface.scalar_exists(
                    """
                    SELECT 1
                    FROM dbo.partSubmissions
                    WHERE varName = ?
                      AND UPPER(LTRIM(RTRIM([State]))) = 'READY';
                    """,
                    (var_name,)
                )
            except AttributeError:
                # If your fetch already enforces Ready, assume True
                is_ready = True

            if not is_ready:
                time.sleep(POLL_NOT_READY_SEC)
                continue

            # 3) atomic claim → Running
            rc = _exec_rowcount(
                """
                UPDATE dbo.partSubmissions
                SET [State] = 'Running'
                WHERE varName = ?
                  AND UPPER(LTRIM(RTRIM([State]))) = 'READY';
                """,
                (var_name,)
            )
            if rc != 1:
                # someone else grabbed it; try again
                time.sleep(0.5)
                continue

            # 4) stash data & trigger your existing UI command (same approach you use)
            event_data = row
            loggin_utils.log(f"[watcher] Claimed {var_name}; executing UI command.")

            try:
                adsk.doEvents()
            except:
                pass
            time.sleep(0.2)

            try:
                cmd_def = ui.commandDefinitions.itemById(CMD_ID)
                if cmd_def:
                    cmd_def.execute()
                    loggin_utils.log("[watcher] Executed show data command.")
                else:
                    loggin_utils.log("[watcher] CMD_ID not found; skipping.")
            except Exception as e:
                loggin_utils.log(f"[watcher] execute() error: {e}\n{traceback.format_exc()}")

            # 5) wait until this row completes (Finished/Error) before moving on
            final_state = _wait_until_done(var_name, timeout_sec=1800.0, poll_sec=1.0)
            if final_state:
                loggin_utils.log(f"[watcher] {var_name} completed with state: {final_state}.")
            else:
                loggin_utils.log(f"[watcher] {var_name} did not signal completion before timeout or stop; continuing.")

            # 6) small cooldown, then loop again
            time.sleep(COOLDOWN_AFTER_JOB)

        except Exception as e:
            loggin_utils.log(f"[watcher] ERROR: {e}\n{traceback.format_exc()}")
            time.sleep(1.0)

    loggin_utils.log("Worker thread exited (stopped by user).")


def show_message_boxes():
    global event_data
    try:
        if event_data:
            sub_date = event_data.get('subDate', 'N/A')
            var_name = event_data.get('varName', 'N/A')
            p_bucket = event_data.get('pBucket', 'N/A')
            loggin_utils.log(f"(suppressed message) subDate={sub_date}, varName={var_name}, pBucket={p_bucket}")
        else:
            loggin_utils.log("No SQL data loaded yet (message suppressed).")
    except Exception as e:
        loggin_utils.log(f"Error in show_message_boxes(): {e}\n{traceback.format_exc()}")

class ShowDataCommandCreatedHandler(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        try:
            loggin_utils.log("CommandCreated event (ShowDataCommand)")
            show_message_boxes()
        except Exception as e:
            loggin_utils.log(f"Error in ShowDataCommandCreatedHandler: {e}\n{traceback.format_exc()}")

class OpenFileCommandCreatedHandler(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        try:
            loggin_utils.log("CommandCreated event (OpenFileCommand)")
            process_one()
        except Exception as e:
            loggin_utils.log(f"Error in OpenFileCommandCreatedHandler: {e}\n{traceback.format_exc()}")

def run(context):
    global thread_started, stop_thread
    try:
        log_path = r"C:/Temp/import_sql_thread_log.txt"
        if os.path.exists(log_path):
            os.remove(log_path)

        loggin_utils.log("run() called")

        if not thread_started:
            stop_thread = False
            t = threading.Thread(target=threaded_worker, daemon=True)
            t.start()
            thread_started = True
            loggin_utils.log("Thread launched")

        cmd_def = ui.commandDefinitions.itemById(CMD_ID)
        if not cmd_def:
            cmd_def = ui.commandDefinitions.addButtonDefinition(CMD_ID, CMD_NAME, CMD_DESC)
        show_handler = ShowDataCommandCreatedHandler()
        cmd_def.commandCreated.add(show_handler)
        handlers.append(show_handler)

        file_cmd_def = ui.commandDefinitions.itemById(OPEN_CMD_ID)
        if not file_cmd_def:
            file_cmd_def = ui.commandDefinitions.addButtonDefinition(OPEN_CMD_ID, OPEN_CMD_NAME, OPEN_CMD_DESC)
        open_handler = OpenFileCommandCreatedHandler()
        file_cmd_def.commandCreated.add(open_handler)
        handlers.append(open_handler)

        panel = ui.allToolbarPanels.itemById('SolidScriptsAddinsPanel')
        if not panel.controls.itemById(CMD_ID):
            panel.controls.addCommand(cmd_def)
        if not panel.controls.itemById(OPEN_CMD_ID):
            panel.controls.addCommand(file_cmd_def)

        loggin_utils.log("Commands registered in UI")
    except Exception as e:
        loggin_utils.log(f"Exception in run(): {e}\n{traceback.format_exc()}")

def stop(context):
    global stop_thread, thread_started, event_data
    stop_thread = True
    thread_started = False
    event_data = None
    loggin_utils.log("Add-in stopped and event_data cleared.")
