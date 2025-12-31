from . import loggin_utils
import pyodbc
import adsk.core, adsk.fusion, traceback
import re

def _make_next_config_row_active(design) -> str:
    """
    Create a new configuration row based on the active row's base name and
    activate it. Returns the new configuration name (or None on failure).
    """
    try:
        config_table = design.configurationTopTable
        if not config_table or config_table.rows.count == 0:
            loggin_utils.log("No configuration table or rows found.")
            return None

        active_row = config_table.activeRow
        if not active_row:
            loggin_utils.log("No active configuration row found.")
            return None

        active_name = active_row.name
        # Strip a trailing -<digits> suffix if present
        base_name = active_name
        m = re.match(r"^(.*?)-(\d+)$", active_name, flags=re.IGNORECASE)
        if m:
            base_name = m.group(1)

        # Gather existing names that share the base prefix
        existing_names = [row.name for row in config_table.rows if row.name.startswith(base_name + "-")]

        # Find the next numeric suffix
        suffixes = []
        for nm in existing_names:
            parts = nm.rsplit("-", 1)
            if len(parts) == 2 and parts[1].isdigit():
                suffixes.append(int(parts[1]))
        next_suffix = (max(suffixes) + 1) if suffixes else 1

        new_config_name = f"{base_name}-{next_suffix:03d}"

        # Avoid accidental collision
        if any(r.name == new_config_name for r in config_table.rows):
            # fallback: non-padded integer
            i = next_suffix + 1
            while any(r.name == f"{base_name}-{i}" for r in config_table.rows):
                i += 1
            new_config_name = f"{base_name}-{i}"

        new_row = config_table.rows.add(new_config_name)
        new_row.activate()
        loggin_utils.log(f"✅ Created and activated new configuration: {new_config_name}")
        return new_config_name

    except Exception as e:
        loggin_utils.log(f"_make_next_config_row_active failed: {e}\n{traceback.format_exc()}")
        return None


def update_parameters_from_sql(design, event_data):
    try:
        user_params = design.userParameters
        model_params = design.allParameters

        if not event_data:
            loggin_utils.log("No event_data to update parameters from.")
            return

        var_name = event_data.get('varName')
        if not var_name:
            loggin_utils.log("No varName provided in event_data.")
            return

        # Helper: turn OFF the 'Stamp' column for the ACTIVE configuration row
        def _turn_off_stamp_on_active_row(design):
            try:
                table = getattr(design, "configurationTopTable", None)
                if not table or not getattr(table, "activeRow", None):
                    loggin_utils.log("[stamp] No configurationTopTable or no active row.")
                    return False

                active_row = table.activeRow
                active_name = getattr(active_row, "name", "<none>")

                # Find 'Stamp' column by label
                def _label(col):
                    for k in ("title", "displayName", "name", "caption", "headerText"):
                        try:
                            v = getattr(col, k)
                            if callable(v): v = v()
                            if v: return str(v)
                        except:
                            pass
                    return ""

                cols = table.columns
                stamp_idx, stamp_col = -1, None
                for i in range(int(cols.count)):
                    c = cols.item(i)
                    if _label(c).strip().lower() == "stamp":
                        stamp_idx, stamp_col = i, c
                        break
                if stamp_idx < 0:
                    loggin_utils.log("[stamp] Column 'Stamp' not found.")
                    return False

                # Resolve active row index
                rows = table.rows
                row_index = -1
                for i in range(int(rows.count)):
                    r = rows.item(i)
                    if getattr(r, "name", "") == active_name:
                        row_index = i
                        break
                if row_index < 0:
                    loggin_utils.log("[stamp] Could not resolve active row index.")
                    return False

                # Try several write paths on the cell
                def _try_cell_writes(cell, origin_tag):
                    try:
                        if hasattr(cell, "booleanValue"):
                            cell.booleanValue = False
                            loggin_utils.log(f"[stamp] Set via {origin_tag}.booleanValue=False")
                            return True
                    except Exception as e:
                        loggin_utils.log(f"[stamp] {origin_tag}.booleanValue failed: {e}")

                    try:
                        if hasattr(cell, "value"):
                            cell.value = False
                            loggin_utils.log(f"[stamp] Set via {origin_tag}.value=False")
                            return True
                    except Exception as e:
                        loggin_utils.log(f"[stamp] {origin_tag}.value failed: {e}")

                    try:
                        m = getattr(cell, "setValue", None)
                        if m:
                            m(False)
                            loggin_utils.log(f"[stamp] Set via {origin_tag}.setValue(False)")
                            return True
                    except Exception as e:
                        loggin_utils.log(f"[stamp] {origin_tag}.setValue(False) failed: {e}")

                    try:
                        m = getattr(cell, "setBooleanValue", None)
                        if m:
                            m(False)
                            loggin_utils.log(f"[stamp] Set via {origin_tag}.setBooleanValue(False)")
                            return True
                    except Exception as e:
                        loggin_utils.log(f"[stamp] {origin_tag}.setBooleanValue(False) failed: {e}")

                    return False

                # A) row -> cell
                try:
                    cell = active_row.getCellByColumnIndex(stamp_idx)
                    if _try_cell_writes(cell, "rowCell"):
                        return True
                except Exception as e:
                    loggin_utils.log(f"[stamp] row.getCellByColumnIndex failed: {e}")

                # B) table -> cell
                try:
                    cell = table.getCell(row_index, stamp_idx)
                    if _try_cell_writes(cell, "tableCell"):
                        return True
                except Exception as e:
                    loggin_utils.log(f"[stamp] table.getCell failed: {e}")

                # C) column -> cell by row name
                try:
                    cell = stamp_col.getCellByRowName(active_name)
                    if _try_cell_writes(cell, "colCell"):
                        return True
                except Exception as e:
                    loggin_utils.log(f"[stamp] col.getCellByRowName failed: {e}")

                # D) Fallback: toggle the bound feature for this active row
                try:
                    feat = getattr(stamp_col, "feature", None)
                    if feat and hasattr(feat, "isSuppressed"):
                        if not feat.isSuppressed:
                            feat.isSuppressed = True
                            loggin_utils.log("[stamp] Set via feature.isSuppressed=True (active row).")
                        else:
                            loggin_utils.log("[stamp] Feature already suppressed for active row.")
                        return True
                except Exception as e:
                    loggin_utils.log(f"[stamp] feature.isSuppressed path failed: {e}")

                loggin_utils.log("[stamp] No supported setter worked for 'Stamp' in this build.")
                return False

            except Exception as e:
                import traceback
                loggin_utils.log(f"[stamp] Exception: {e}\n{traceback.format_exc()}")
                return False

        # Confirm active config (optional)
        try:
            table = design.configurationTopTable
            active_name = table.activeRow.name if table and table.activeRow else "<none>"
            loggin_utils.log(f"[update_parameters_from_sql] Active config: {active_name}")
        except Exception:
            loggin_utils.log("[update_parameters_from_sql] Could not read active config")

        # SQL fetch
        try:
            import pyodbc
            conn = pyodbc.connect(
                "DRIVER={ODBC Driver 17 for SQL Server};"
                "SERVER=pschost1;"
                "DATABASE=ETDP;"
                "UID=sa;"
                "PWD=sa;"
            )
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM dbo.partSubmissions WHERE varName = ?", var_name)
            row = cursor.fetchone()
            if not row:
                loggin_utils.log(f"No SQL row returned for varName: {var_name}")
                return
            headers = [desc[0] for desc in cursor.description]
            row_dict = dict(zip(headers, row))
            conn.close()
            loggin_utils.log(f"SQL row fetched for varName: {var_name}")
        except Exception as sql_err:
            import traceback
            loggin_utils.log(f"SQL fetch failed: {sql_err}\n{traceback.format_exc()}")
            return

        # Update user parameters
        loggin_utils.log("--- Updating User Parameters ---")
        for param in user_params:
            if param.name == "b1HeadDiam":
                loggin_utils.log(f"User Param Skipped (ignored by rule): {param.name}")
                continue
            if param.name in row_dict:
                new_val = row_dict[param.name]
                if new_val is not None:
                    try:
                        float(str(new_val).split()[0])  # validation only
                        old_val = param.expression
                        param.expression = str(new_val)
                        loggin_utils.log(f"User Param Updated: {param.name}: {old_val} -> {param.expression}")
                    except Exception:
                        loggin_utils.log(f"User Param Skipped (invalid): {param.name} = {new_val}")
                else:
                    loggin_utils.log(f"User Param Skipped (null): {param.name}")
            else:
                loggin_utils.log(f"User Param Skipped (no match): {param.name}")

        # Update model parameters
        loggin_utils.log("--- Updating Model Parameters ---")
        for param in model_params:
            if param.name == "b1HeadDiam":
                loggin_utils.log(f"Model Param Skipped (ignored by rule): {param.name}")
                continue
            if param.name in row_dict:
                new_val = row_dict[param.name]
                if new_val is not None:
                    try:
                        float(str(new_val).split()[0])  # validation only
                        old_val = param.expression
                        param.expression = str(new_val)
                        loggin_utils.log(f"Model Param Updated: {param.name}: {old_val} -> {param.expression}")
                    except Exception:
                        loggin_utils.log(f"Model Param Skipped (invalid): {param.name} = {new_val}")
                else:
                    loggin_utils.log(f"Model Param Skipped (null): {param.name}")
            else:
                loggin_utils.log(f"Model Param Skipped (no match): {param.name}")

        # Finally, force Stamp OFF on the active row (no UI, no save)
        _turn_off_stamp_on_active_row(design)

    except Exception as e:
        import traceback
        loggin_utils.log(f"Exception in update_parameters_from_sql(): {e}\n{traceback.format_exc()}")


def fetch_recent_rows(OPEN_CMD_ID):
    import pyodbc
    app = adsk.core.Application.get()
    ui = app.userInterface

    loggin_utils.log("Starting SQL fetch...")

    try:
        conn = pyodbc.connect(
            "DRIVER={ODBC Driver 17 for SQL Server};"
            "SERVER=pschost1;"
            "DATABASE=ETDP;"
            "UID=sa;"
            "PWD=sa;"
        )
        loggin_utils.log("Connected to SQL Server")

        # Deterministic, explicit columns, alias pRecSize to a canonical name
        query = """
        SELECT TOP (1)
            subDate,
            varName,
            pBucket,
            pRecSize AS pRecSize,
            pRecSpec AS pRecSpec
        FROM dbo.partSubmissions
        WHERE TRY_CAST([subDate] AS DATE) >= CAST(DATEADD(DAY, -1, GETDATE()) AS DATE)
        AND UPPER(LTRIM(RTRIM([State]))) = 'READY'
        ORDER BY subDate DESC;
        """

        cursor = conn.cursor()
        cursor.execute(query)
        rows = cursor.fetchall()
        headers = [c[0] for c in cursor.description]
        loggin_utils.log(f"SQL headers: {headers}")
        loggin_utils.log(f"Fetched {len(rows)} rows")

        if not rows:
            #loggin_utils.log("No rows returned.")
            conn.close()
            return None  # nothing to return

        # Inspect first row
        row = rows[0]
        row_dict = dict(zip(headers, row))
        # Safe, short preview
        preview = {k: (str(v)[:80] if v is not None else None) for k, v in row_dict.items()}
        loggin_utils.log(f"Row[0] preview: {preview}")

        sub_date = str(row_dict.get('subDate', 'N/A'))[:32]
        var_name = str(row_dict.get('varName', 'N/A'))[:32]
        p_bucket = str(row_dict.get('pBucket', 'N/A'))[:64]
        p_rec_size = row_dict.get('pRecSize')  # may be None
        p_rec_size = (str(p_rec_size)[:64]) if p_rec_size is not None else 'N/A'
        p_rec_spec = row_dict.get('pRecSpec')  # may be None
        p_rec_spec = (str(p_rec_spec)[:64]) if p_rec_spec is not None else 'N/A'

        loggin_utils.log(f"subDate: {sub_date}")
        loggin_utils.log(f"varName: {var_name}")
        loggin_utils.log(f"pBucket: {p_bucket}")
        loggin_utils.log(f"pRecSize: {p_rec_size}")
        loggin_utils.log(f"pRecSpec: {p_rec_spec}")


        event_data = {
            'subDate': sub_date,
            'varName': var_name,
            'pBucket': p_bucket,
            'pRecSize': p_rec_size,
            'pRecSpec': p_rec_spec
        }

        conn.close()
        loggin_utils.log("SQL connection closed")

        # Kick the command, same as before
        loggin_utils.log("Triggering openCloudFileCmd command")
        file_cmd_def = ui.commandDefinitions.itemById(OPEN_CMD_ID)
        if file_cmd_def:
            file_cmd_def.execute()
        else:
            loggin_utils.log("openCloudFileCmd definition not found")

        return event_data#, rows

    except Exception as e:
        loggin_utils.log(f"FAILED: pyodbc.connect or fetch — {e}\n{traceback.format_exc()}")
        return None