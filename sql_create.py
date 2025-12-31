# sql_create.py
from __future__ import annotations
import csv, os
from typing import List, Tuple, Optional
import pyodbc

SERVER   = "pschost1"
DATABASE = "ETDP"
USER     = "sa"
PASSWORD = "sa"

PARTS_TABLE = "dbo.PartSubmissions"
MATS_TABLE  = "dbo.Materials"

PART_CSV_OUT     = r"P:\ETDP\Scripts\AutomateScripts\partSub_row.csv"
MATERIAL_CSV_OUT = r"P:\ETDP\Scripts\AutomateScripts\partSub_row_material.csv"


# ---------------- Helpers ----------------
def ensure_dir(path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

def rows_to_csv(rows, colnames, out_path):
    ensure_dir(out_path)
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(colnames)
        for r in rows:
            w.writerow(list(r))

def choose_driver() -> str:
    for name in ("ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server"):
        if name in pyodbc.drivers():
            return name
    return "ODBC Driver 17 for SQL Server"

def connect_mssql() -> pyodbc.Connection:
    driver = choose_driver()
    conn_str = (
        f"Driver={{{driver}}};Server={SERVER};Database={DATABASE};"
        f"UID={USER};PWD={PASSWORD};Encrypt=yes;TrustServerCertificate=yes;"
    )
    return pyodbc.connect(conn_str, timeout=15)

def trim(x) -> str:
    return "" if x is None else str(x).strip()


# ---------------- Core API ----------------
def export_part_and_material(varname: str) -> tuple[str, str]:
    """
    Export partSubmissions row + materials rows for a single varName.
    Returns (part_csv_path, material_csv_path)
    """
    cn = connect_mssql()
    try:
        cur = cn.cursor()

        sql_part = f"""
        SELECT *
        FROM {PARTS_TABLE} WITH (NOLOCK)
        WHERE varName = ?
        """
        cur.execute(sql_part, varname)
        rows = cur.fetchall()
        if not rows:
            raise RuntimeError(f"No PartSubmissions row found for varName='{varname}'")

        part_cols = [c[0] for c in cur.description]
        rows_to_csv(rows, part_cols, PART_CSV_OUT)

        # Extract material keys
        def idx(name: str) -> Optional[int]:
            for i, c in enumerate(part_cols):
                if c.lower() == name.lower():
                    return i
            return None

        i_spec = idx("cMatlSpec")
        i_proc = idx("cMatlProc")

        if i_spec is None or i_proc is None:
            raise RuntimeError("Missing cMatlSpec / cMatlProc in part row")

        cMatlSpec = trim(rows[0][i_spec])
        cMatlProc = trim(rows[0][i_proc])

        if cMatlSpec and cMatlProc:
            sql_mat = f"""
            SELECT *
            FROM {MATS_TABLE} WITH (NOLOCK)
            WHERE LTRIM(RTRIM(Material)) = LTRIM(RTRIM(?))
              AND LTRIM(RTRIM(Spec))     = LTRIM(RTRIM(?))
            """
            cur.execute(sql_mat, cMatlSpec, cMatlProc)
            mat_rows = cur.fetchall()
            mat_cols = [c[0] for c in cur.description] if cur.description else ["Material", "Spec"]
            rows_to_csv(mat_rows, mat_cols, MATERIAL_CSV_OUT)
        else:
            rows_to_csv([], ["Material", "Spec"], MATERIAL_CSV_OUT)

        return PART_CSV_OUT, MATERIAL_CSV_OUT

    finally:
        cn.close()


# ---------------- CLI (optional) ----------------
if __name__ == "__main__":
    import sys
    varname = sys.argv[1] if len(sys.argv) > 1 else "ps12"
    part_csv, mat_csv = export_part_and_material(varname)
    print(part_csv)
    print(mat_csv)
