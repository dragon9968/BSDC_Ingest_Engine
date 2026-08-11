import json
import re
import sqlite3
from pathlib import Path
import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "workspace" / "rules.db"


def parse_section_rule(raw_notes: str) -> dict:
    """PARSE RED CELL / SECTION-LEVEL NOTES (SECTION-LEVEL RULE)"""
    filter_cond = None
    join_rule = None

    filter_match = re.search(
        r"(?:ONLY\s+CONSIDERED\s+.*?\s+IF|IF)\s+(COLUMN\s+[A-Za-z0-9_]+\s*=\s*[^\|\n]+)",
        raw_notes,
        re.IGNORECASE,
    )
    if filter_match:
        filter_cond = filter_match.group(1).split("\n")[0].split("|")[0].strip()

    link_match = re.search(
        r"LINK\s+([A-Za-z0-9_]+)\s+COLUMN\s+([A-Za-z0-9_]+)\s+TO\s+([A-Za-z0-9_]+)\s+COLUMN\s+([A-Za-z0-9_]+)",
        raw_notes,
        re.IGNORECASE,
    )
    if link_match:
        join_rule = {
            "source_file": link_match.group(1),
            "source_col": link_match.group(2),
            "target_file": link_match.group(3),
            "target_col": link_match.group(4),
        }

    readable_parts = []
    if filter_cond:
        readable_parts.append(f"FILTER({filter_cond})")
    if join_rule:
        readable_parts.append(
            f"JOIN({join_rule['source_file']}.{join_rule['source_col']} = {join_rule['target_file']}.{join_rule['target_col']})"
        )

    readable_str = " | ".join(readable_parts)

    return {
        "rule_type": "SECTION_RULE",
        "dsl_json": {
            "action": "SECTION_RULE",
            "filter_condition": filter_cond,
            "join_rule": join_rule,
            "raw_notes": raw_notes,
        },
        "dsl_readable": readable_str if readable_str else "SECTION_HEADER_RULE",
        "status": "AUTO_PARSED",
    }


def parse_notes_to_dsl(data_file: str, col: str, notes: str) -> dict:
    """ITEM #8: Deterministic Parser for accurate Rule classification"""
    data_file = data_files.strip() if data_file else ""
    col = col.strip() if col else ""
    notes = notes.strip() if notes else ""
    notes_upper = notes.upper()

    if not data_file and not col and not notes:
        return {
            "rule_type": "NO_MAPPING",
            "dsl_json": {"action": "NONE", "reason": "UNMAPPED_FIELD"},
            "dsl_readable": "NO_MAPPING",
            "status": "AUTO_PARSED",
        }

    if "IF COLUMN" in notes_upper and "ACCUMULATE" not in notes_upper:
        cond_match = re.search(
            r"IF\s+COLUMN\s+([A-Za-z0-9]+)\s*=\s*([A-Za-z0-9_\-\.\/]+)\s+THEN\s+(.*?)\s*(?:;|\b)\s*ELSE\s+(.*)",
            notes,
            re.IGNORECASE,
        )

        if cond_match:
            if_col = cond_match.group(1).strip()
            if_val = cond_match.group(2).strip()
            then_val = cond_match.group(3).strip()
            else_val = cond_match.group(4).strip()

            return {
                "rule_type": "CONDITIONAL",
                "dsl_json": {
                    "action": "CONDITIONAL",
                    "if_col": if_col,
                    "if_val": if_val,
                    "then_val": then_val,
                    "else_val": else_val,
                    "raw_condition": notes,
                },
                "dsl_readable": f"IF COL_{if_col}=='{if_val}' THEN '{then_val}' ELSE '{else_val}'",
                "status": "AUTO_PARSED",
            }

    if "MATRIX" in notes_upper or "USING MATRIX" in notes_upper or "USE MATRIX" in notes_upper or "LOOKUP" in notes_upper:
        match = re.search(r"ASSIGN\s+([A-Za-z0-9_\-\.]+)", notes, re.IGNORECASE)
        ref = match.group(1) if match else "MATRIX_LOOKUP"

        return {
            "rule_type": "MATRIX_LOOKUP",
            "dsl_json": {
                "action": "MATRIX_LOOKUP",
                "target_ref": ref,
                "source_file": data_file,
                "source_column": col,
                "raw_notes": notes,
            },
            "dsl_readable": f"LOOKUP('{ref}')",
            "status": "AUTO_PARSED",
        }

    if data_file and col and (not notes or notes_upper in ["NAN", "NONE"]):
        return {
            "rule_type": "DIRECT",
            "dsl_json": {
                "action": "DIRECT",
                "source_file": data_file,
                "source_column": col,
            },
            "dsl_readable": f"{data_file}.{col}",
            "status": "AUTO_PARSED",
        }

    if notes_upper.startswith("ASSIGN"):
        val = re.sub(r"^ASSIGN\s+(ALL\s+)?", "", notes, flags=re.IGNORECASE).strip()
        return {
            "rule_type": "CONSTANT",
            "dsl_json": {"action": "CONSTANT", "value": val},
            "dsl_readable": f"CONST('{val}')",
            "status": "AUTO_PARSED",
        }

    return {
        "rule_type": "UNPARSED",
        "dsl_json": {"action": "UNPARSED", "raw_notes": notes},
        "dsl_readable": "NEEDS_LLM_PARSING",
        "status": "NEEDS_REVIEW",
    }


def process_mapping_sheet(
    excel_path: str, sheet_name: str, cu_id: str = "MEDICOOP"
):
    print(f"🔄 Reading Mapping Sheet [{sheet_name}] for CU: [{cu_id}]...")

    raw_df = pd.read_excel(excel_path, sheet_name=sheet_name, header=None)

    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL;")
    cursor = conn.cursor()

    stats = {
        "reused": 0,
        "auto_parsed": 0,
        "no_mapping": 0,
        "needs_review": 0,
        "section_rules": 0,
    }
    current_section = "Shares (DP Table) - General"

    field_col_idx = 1       # Column B
    data_file_col_idx = 5   # Column F
    col_letter_idx = 6      # Column G
    notes_col_idx = 7       # Column H

    for idx, row in raw_df.iterrows():
        row_vals_clean = [str(v).strip() for v in row.values if pd.notna(v)]
        row_str = " | ".join(row_vals_clean)

        row_vals_lower = [v.lower() for v in row_vals_clean]
        if "field" in row_vals_lower and any("notes" in v or "additional" in v for v in row_vals_lower):
            for c_idx, val in enumerate(row.values):
                val_str = str(val).strip().lower() if pd.notna(val) else ""
                if val_str == "field":
                    field_col_idx = c_idx
                elif "data file" in val_str or "source file" in val_str:
                    data_file_col_idx = c_idx
                elif val_str == "column" or "source col" in val_str:
                    col_letter_idx = c_idx
                elif "notes" in val_str or "additional" in val_str:
                    notes_col_idx = c_idx

        # 1. Update Section Block name
        if "table)" in row_str.lower() or "(mb-" in row_str.lower() or "(dp" in row_str.lower():
            clean_sec_name = row_str.split("ONLY CONSIDERED")[0].split("LINK")[0].split("|")[0].strip()
            if clean_sec_name and len(clean_sec_name) < 100:
                current_section = clean_sec_name
                print(f"\n📌 Scanning Data Section: [{current_section}]")

        # 2. SAVE _SECTION_RULE_ ONLY WHEN FILTER OR JOIN EXISTS
        if "ONLY CONSIDERED" in row_str.upper() or "LINK " in row_str.upper():
            parsed_sec = parse_section_rule(row_str)
            if parsed_sec["dsl_json"]["filter_condition"] or parsed_sec["dsl_json"]["join_rule"]:
                cursor.execute(
                    """
                    INSERT OR REPLACE INTO rule_store 
                    (cu_id, sheet_name, section_name, target_field, raw_notes, data_file, column_letter, rule_type, dsl_json, dsl_readable, status, parsed_by)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        cu_id,
                        sheet_name,
                        current_section,
                        "_SECTION_RULE_",
                        row_str,
                        "",
                        "",
                        parsed_sec["rule_type"],
                        json.dumps(parsed_sec["dsl_json"]),
                        parsed_sec["dsl_readable"],
                        parsed_sec["status"],
                        "ITEM_8",
                    ),
                )
                stats["section_rules"] += 1
                print(f"   🚩 [SECTION_RULE] Locking filter for [{current_section}] -> {parsed_sec['dsl_readable']}")
                continue

        target_field = str(row.iloc[field_col_idx]).strip() if len(row) > field_col_idx and pd.notna(row.iloc[field_col_idx]) else ""

        is_valid_field = (
            bool(target_field)
            and "." in target_field
            and target_field.lower() not in ["nan", "none", "field", "label"]
        )

        if not is_valid_field:
            continue

        data_file = str(row.iloc[data_file_col_idx]).strip() if len(row) > data_file_col_idx and pd.notna(row.iloc[data_file_col_idx]) else ""
        col = str(row.iloc[col_letter_idx]).strip() if len(row) > col_letter_idx and pd.notna(row.iloc[col_letter_idx]) else ""
        raw_notes = str(row.iloc[notes_col_idx]).strip() if len(row) > notes_col_idx and pd.notna(row.iloc[notes_col_idx]) else ""

        if not raw_notes or raw_notes.lower() in ["nan", "none"]:
            for cell_val in row_vals_clean:
                cell_upper = cell_val.upper()
                if any(kw in cell_upper for kw in ["ASSIGN", "MATRIX", "IF COLUMN", "LOOKUP", "MONTH ="]):
                    raw_notes = cell_val
                    break

        if data_files.lower() in ["nan", "none"]: data_file = ""
        if col.lower() in ["nan", "none"]: col = ""
        if raw_notes.lower() in ["nan", "none"]: raw_notes = ""

        cursor.execute(
            """
            SELECT dsl_readable, status FROM rule_store 
            WHERE (cu_id = ? OR is_global = 1) AND sheet_name = ? AND TRIM(section_name) = TRIM(?) AND target_field = ?
        """,
            (cu_id, sheet_name, current_section, target_field),
        )

        existing_rule = cursor.fetchone()

        if existing_rule:
            stats["reused"] += 1
            print(f"   [#7 REUSED] {target_field} -> Reusing Rule: {existing_rule[0]}")
        else:
            parsed_res = parse_notes_to_dsl(data_file, col, raw_notes)

            cursor.execute(
                """
                INSERT OR REPLACE INTO rule_store 
                (cu_id, sheet_name, section_name, target_field, raw_notes, data_file, column_letter, rule_type, dsl_json, dsl_readable, status, parsed_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    cu_id,
                    sheet_name,
                    current_section,
                    target_field,
                    raw_notes,
                    data_file,
                    col,
                    parsed_res["rule_type"],
                    json.dumps(parsed_res["dsl_json"]),
                    parsed_res["dsl_readable"],
                    parsed_res["status"],
                    "ITEM_8",
                ),
            )

            if parsed_res["rule_type"] == "NO_MAPPING":
                stats["no_mapping"] += 1
                print(f"   [#8 NO_MAPPING] {target_field} -> Skipped (All 3 columns empty)")
            elif parsed_res["status"] == "AUTO_PARSED":
                stats["auto_parsed"] += 1
                print(f"   [#8 PARSED] {target_field} -> {parsed_res['dsl_readable']}")
            else:
                stats["needs_review"] += 1
                print(f"   [#8 UNPARSED] {target_field} -> Needs AI review (Long/Complex Notes)")

    conn.commit()
    conn.close()

    print("\n" + "=" * 50)
    print(f"📊 RULE INGESTION SUMMARY FOR SHEET [{sheet_name}]:")
    print(f"   - Reused (#7): {stats['reused']} fields")
    print(f"   - Section Rules (#8): {stats['section_rules']} rules (Filter/Join)")
    print(f"   - Auto-Parsed (#8): {stats['auto_parsed']} fields")
    print(f"   - No Mapping/Empty (#8): {stats['no_mapping']} fields")
    print(f"   - Needs LLM Review (#9): {stats['needs_review']} fields")
    print("=" * 50 + "\n")


def find_mapping_file(ingest_dir: Path) -> Path:
    for file in ingest_dir.glob("*.xlsx"):
        if files.name.startswith("~$"): continue
        if "mapping" in files.name.lower():
            print(f"🎯 Automatically found mapping file: {files.name}")
            return file
    raise FileNotFoundError("❌ NO mapping file found in workspace/ingest/")


if __name__ == "__main__":
    INGEST_DIR = BASE_DIR / "workspace" / "ingest"
    try:
        excel_file_path = find_mapping_file(INGEST_DIR)
        process_mapping_sheet(str(excel_file_path), sheet_name="Shares", cu_id="MEDICOOP")
    except Exception as e:
        print(f"💥 Error: {e}")