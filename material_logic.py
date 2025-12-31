import adsk.core, adsk.fusion, re
from datetime import datetime

LOG_PATH = "C:/Temp/import_sql_thread_log.txt"

def _log(msg: str):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now()}] {msg}\n")
    except:
        pass

def _norm(s: str) -> str:
    return (s or "").strip().lower()

def _keyify(s: str) -> str:
    return re.sub(r"[\s_\-]+", "", _norm(s))

def _search_document_material(design: adsk.fusion.Design, target: str):
    """Search only materials in the *document*."""
    try:
        doc_mats = getattr(design, "materials", None)
        if not doc_mats:
            return None
        target_k = _keyify(target)
        for i in range(doc_mats.count):
            m = doc_mats.item(i)
            nm = getattr(m, "name", "") or ""
            desc = getattr(m, "description", "") or ""
            if target in _norm(nm) or target in _norm(desc) or _keyify(nm) == target_k:
                _log(f"[material_logic] 🔎 Found document material: '{nm}'")
                return m
    except Exception as e:
        _log(f"[material_logic] ⚠️ Error scanning document materials: {e}")
    return None

def _apply_material_to_active_component(design: adsk.fusion.Design, mat: adsk.fusion.Material) -> bool:
    """Apply material to active component (or root) only."""
    try:
        comp = design.activeComponent or design.rootComponent
        if hasattr(comp, "material"):
            comp.material = mat
            _log(f"[material_logic] ✅ Applied '{mat.name}' to '{comp.name}'.")
            return True
    except Exception as e:
        _log(f"[material_logic] ⚠️ Failed to apply material to component: {e}")
    return False

def apply_material_from_sql_doc_only(cMatlSpec: str) -> bool:
    """Search only document's materials for cMatlSpec. Log if not found."""
    app = adsk.core.Application.get()
    design = adsk.fusion.Design.cast(app.activeProduct)
    if not design:
        _log("[material_logic] ❌ No active design.")
        return False

    target = _norm(cMatlSpec)
    if not target:
        _log("[material_logic] ❌ Empty cMatlSpec.")
        return False

    mat = _search_document_material(design, target)
    if not mat:
        _log(f"[material_logic] ❌ No material found matching '{cMatlSpec}' in document.")
        return False

    return _apply_material_to_active_component(design, mat)
