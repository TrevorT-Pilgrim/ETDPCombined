# occ_owner_switch.py
import adsk.core, adsk.fusion
import time, traceback
from typing import Optional, Tuple
from . import open_utils as ou

def _sleep_events(dt: float):
    try:
        adsk.doEvents()
        vp = adsk.core.Application.get().activeViewport
        if vp: vp.refresh()
    except: pass
    time.sleep(dt)

def _get_occ_owner_datafile(occ: adsk.fusion.Occurrence):
    try:
        return getattr(occ, 'configuredDataFile', None)
    except:
        return getattr(getattr(occ, 'component', None), 'dataFile', None)

def _latest_df(df):
    try:
        if getattr(df, "isLatest", None) is True:
            return df
        vers = getattr(df, "versions", None)
        if vers and getattr(vers, "count", 0) > 0:
            vv = vers.item(vers.count - 1)
            return getattr(vv, "dataFile", None) or df
        return df
    except:
        return df

def _refresh_occurrence_to_latest(occ) -> None:
    try:
        if hasattr(occ, "isOutOfDate") and hasattr(occ, "updateToLatestVersion"):
            if occ.isOutOfDate:
                occ.updateToLatestVersion()
                _sleep_events(0.12)
                ou.log("[latest] occ.updateToLatestVersion()")
                return
    except Exception as e:
        ou.log(f"[latest] occ.updateToLatestVersion failed: {e}")

    try:
        df = _get_occ_owner_datafile(occ)
        if not df:
            return
        latest = _latest_df(df)
        if latest and hasattr(occ, "replaceComponent"):
            if getattr(df, "id", None) != getattr(latest, "id", None) or \
               getattr(df, "versionNumber", None) != getattr(latest, "versionNumber", None):
                occ.replaceComponent(latest)
                _sleep_events(0.10)
                ou.log(f"[latest] occ.replaceComponent → v{getattr(latest, 'versionNumber', '?')}")
    except Exception as e:
        ou.log(f"[latest] replaceComponent failed: {e}")

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

def _open_owner_and_get_table_and_row(df, desired_row: str) -> Tuple[Optional[adsk.fusion.Design], Optional[adsk.core.Document], Optional[object], Optional[object], bool]:
    app = adsk.core.Application.get()
    for d in app.documents:
        try:
            if d.dataFile and d.dataFile.id == df.id:
                d.activate(); _sleep_events(0.06)
                des = adsk.fusion.Design.cast(app.activeProduct)
                table = getattr(des, "configurationTopTable", None)
                row = _find_row_in_table(getattr(table, "rows", None), desired_row) if table else None
                return des, d, table, row, False
        except: pass

    doc = app.documents.open(df)
    t0 = time.time()
    while time.time() - t0 < 6.0 and (not doc.isActive):
        _sleep_events(0.05)
    des = adsk.fusion.Design.cast(app.activeProduct)
    table = getattr(des, "configurationTopTable", None)
    row = _find_row_in_table(getattr(table, "rows", None), desired_row) if table else None
    return des, doc, table, row, True

def _dump_rows_sample(rows, limit=50):
    try:
        cnt = getattr(rows, "count", 0)
        ou.log(f"[rows] owner row count = {cnt}")
        for i in range(min(cnt, int(limit))):
            try:
                r = rows.item(i)
                ou.log(f"[rows] {i:>3}: '{getattr(r,'name','')}'")
            except: break
    except Exception as e:
        ou.log(f"[rows] dump failed: {e}")

def switch_linked_occurrence_direct(occ: adsk.fusion.Occurrence,
                                    target_row: str,
                                    *,
                                    probe_only: bool = False,
                                    retry_tries: int = 6,
                                    retry_base_s: float = 0.18) -> dict:
    report = {"ok": False, "probe_only": bool(probe_only), "target": target_row, "attempts": 0}
    try:
        if not occ:
            report["reason"] = "no_occurrence"
            return report

        ou.log(f"[switch] Target occurrence: '{getattr(occ,'name','?')}'")
        _refresh_occurrence_to_latest(occ)

        df = _get_occ_owner_datafile(occ)
        if not df:
            ou.log("[switch] ❌ No owner DataFile on occurrence.")
            report["reason"] = "no_owner_datafile"
            return report
        ou.log(f"[owner] dataFile='{getattr(df,'name','?')}', v{getattr(df,'versionNumber','?')}, id={getattr(df,'id','?')}")

        for attempt in range(1, int(max(1, retry_tries)) + 1):
            report["attempts"] = attempt
            owner_des, owner_doc, table, row, is_new = (None, None, None, None, False)
            try:
                owner_des, owner_doc, table, row, is_new = _open_owner_and_get_table_and_row(df, target_row)
                rows = getattr(table, "rows", None) if table else None

                if rows:
                    _dump_rows_sample(rows, limit=50)
                else:
                    ou.log("[switch] ⚠️ Owner has no configurationTopTable or rows.")

                if probe_only:
                    cnt = getattr(rows, "count", 0) if rows else 0
                    ou.log(f"[switch] PROBE: owner rows visible = {cnt}")
                    report.update({"ok": True, "rows_visible": int(cnt)})
                    return report

                if not row:
                    pause = float(retry_base_s) * attempt
                    ou.log(f"[switch] Owner table has no row '{target_row}'. Retry {attempt}/{retry_tries} after {pause:.2f}s")
                    _sleep_events(pause)
                    continue

                try:
                    occ.switchConfiguration(row)
                    _sleep_events(0.08)
                    picked = getattr(row, "name", target_row)
                    ou.log(f"✅ Switched '{getattr(occ,'name','?')}' to '{picked}'")
                    report.update({"ok": True, "used_row": picked})
                    return report
                except Exception as e:
                    msg = (str(e) or "").lower()
                    if "temporar" in msg and "config" in msg:
                        pause = float(retry_base_s) * attempt
                        ou.log(f"[switch] Busy; retry {attempt}/{retry_tries} after {pause:.2f}s")
                        _sleep_events(pause)
                        continue
                    ou.log(f"[switch] ❌ occ.switchConfiguration failed: {e}")
                    report.update({"reason": f"switch_failed:{e}"})
                    return report
            finally:
                try:
                    if owner_doc and is_new:
                        owner_doc.close(False)
                except: pass

        ou.log(f"❌ Gave up switching to '{target_row}' after retries.")
        report["reason"] = "no_row_after_retries"
        return report

    except Exception as e:
        ou.log(f"[switch] Exception: {e}\n{traceback.format_exc()}")
        report["reason"] = f"exception:{e}"
        return report