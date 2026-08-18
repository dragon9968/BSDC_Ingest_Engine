import json
import re
import sqlite3
import sys
from pathlib import Path
import pandas as pd

from schemas import (
    SectionRuleDSL,
    JoinRuleModel,
    ConditionalRuleDSL,
    DirectRuleDSL,
    ConstantRuleDSL,
    MatrixLookupRuleDSL,
    NoMappingRuleDSL,
    UnparsedRuleDSL,
)

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "workspace" / "rules.db"


def parse_section_rule(raw_notes: str) -> dict:
    """Parse section-level filter conditions and table joins using Pydantic v2."""
    filter_cond = None
    join_rule_model = None

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
        join_rule_model = JoinRuleModel(
            source_file=link_match.group(1),
            source_col=link_match.group(2),
            target_file=link_match.group(3),
            target_col=link_match.group(4),
        )

    # Instantiate Pydantic Section Rule Model
    sec_dsl = SectionRuleDSL(
        filter_condition=filter_cond,
        join_rule=join_rule_model,
        raw_notes=raw_notes,
    )

    readable_parts = []
    if filter_cond:
        readable_parts.append(f"FILTER({filter_cond})")
    if join_rule_model:
        readable_parts.append(
            f"JOIN({join_rule_model.source_file}.{join_rule_model.source_col} = {join_rule_model.target_file}.{join_rule_model.target_col})"
        )

    readable_str = " | ".join(readable_parts)

    return {
        "rule_type": "SECTION_RULE",
        "dsl_obj": sec_dsl,
        "dsl_readable": readable_str if readable_str else "SECTION_HEADER_RULE",
        "status": "AUTO_PARSED",
    }


def parse_notes_to_dsl(data_file: str, col: str, notes: str) -> dict:
    """Parse field mapping notes into typed Pydantic v2 models."""
    data_file = data_file.strip() if data_file else ""
    col = col.strip() if col else ""
    notes = notes.strip() if notes else ""
    notes_upper = notes.upper()

    # Case 1: Empty mapping
    if not data_file and not col and not notes:
        no_map = NoMappingRuleDSL()
        return {
            "rule_type": "NO_MAPPING",
            "dsl_obj": no_map,
            "dsl_readable": "NO_MAPPING",
            "status": "AUTO_PARSED",
        }

    # Case 2: Direct Mapping (Prioritize explicitly mapped Source File & Source Column)
    if data_file and col and "IF COLUMN" not in notes_upper and "IF " not in notes_upper:
        direct_dsl = DirectRuleDSL(
            source_file=data_file,
            source_column=col,
        )
        return {
            "rule_type": "DIRECT",
            "dsl_obj": direct_dsl,
            "dsl_readable": f"{data_file}.{col}",
            "status": "AUTO_PARSED",
        }

    # Case 3: Simple Conditional Rule
    if "IF COLUMN" in notes_upper and "ACCUMULATE" not in notes_upper:
        cond_match = re.search(
            r"IF\s+COLUMN\s+([A-Za-z0-9]+)\s*=\s*([A-Za-z0-9_\-\.\/]+)\s+THEN\s+(.*?)\s*(?:;|\b)\s*ELSE\s+(.*)",
            notes,
            re.IGNORECASE,
        )

        if cond_match:
            cond_dsl = ConditionalRuleDSL(
                if_col=cond_match.group(1).strip(),
                if_val=cond_match.group(2).strip(),
                then_val=cond_match.group(3).strip(),
                else_val=cond_match.group(4).strip(),
                raw_condition=notes,
            )
            return {
                "rule_type": "CONDITIONAL",
                "dsl_obj": cond_dsl,
                "dsl_readable": f"IF COL_{cond_dsl.if_col}=='{cond_dsl.if_val}' THEN '{cond_dsl.then_val}' ELSE '{cond_dsl.else_val}'",
                "status": "AUTO_PARSED",
            }

    # Case 4: Matrix Lookup
    if "MATRIX" in notes_upper or "USING MATRIX" in notes_upper or "USE MATRIX" in notes_upper or "LOOKUP" in notes_upper:
        match = re.search(r"ASSIGN\s+([A-Za-z0-9_\-\.]+)", notes, re.IGNORECASE)
        ref = match.group(1) if match else "MATRIX_LOOKUP"

        matrix_dsl = MatrixLookupRuleDSL(
            target_ref=ref,
            source_file=data_file,
            source_column=col,
            raw_notes=notes,
        )
        return {
            "rule_type": "MATRIX_LOOKUP",
            "dsl_obj": matrix_dsl,
            "dsl_readable": f"LOOKUP('{ref}')",
            "status": "AUTO_PARSED",
        }

    # Case 5: Constant Assignment
    if notes_upper.startswith("ASSIGN"):
        val = re.sub(r"^ASSIGN\s+(ALL\s+)?", "", notes, flags=re.IGNORECASE).strip()
        const_dsl = ConstantRuleDSL(value=val)
        return {
            "rule_type": "CONSTANT",
            "dsl_obj": const_dsl,
            "dsl_readable": f"CONST('{val}')",
            "status": "AUTO_PARSED",
        }

    # Case 6: Complex or Multi-IF free-text -> Pass to LLM
    unparsed_dsl = UnparsedRuleDSL(raw_notes=notes)
    return {
        "rule_type": "UNPARSED",
        "dsl_obj": unparsed_dsl,
        "dsl_readable": "NEEDS_LLM_PARSING",
        "status": "NEEDS_REVIEW",
    }

def process_mapping_sheet(
    raw_df: pd.DataFrame, sheet_name: str, conn: sqlite3.Connection, cu_id: str
):
    """Process a single excel mapping DataFrame and persist Pydantic validated rules to SQLite."""
    print(f"\n🔄 Reading Mapping Sheet [{sheet_name}] for CU: [{cu_id}]...")

    cursor = conn.cursor()

    stats = {
        "reused": 0,
        "auto_parsed": 0,
        "no_mapping": 0,
        "needs_review": 0,
        "section_rules": 0,
    }
    current_section = f"{sheet_name} - General"

    field_col_idx = 1       # Default Column B
    data_file_col_idx = 5   # Default Column F
    col_letter_idx = 6      # Default Column G
    notes_col_idx = 7       # Default Column H

    for idx, row in raw_df.iterrows():
        row_vals_clean = [str(v).strip() for v in row.values if pd.notna(v)]
        row_str = " | ".join(row_vals_clean)

        # Dynamically detect header positions
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

        # Detect Data Section Headers
        if any(kw in row_str.lower() for kw in ["table)", "(mb-", "(dp", "table", "section"]):
            clean_sec_name = row_str.split("ONLY CONSIDERED")[0].split("LINK")[0].split("|")[0].strip()
            if clean_sec_name and len(clean_sec_name) < 100:
                current_section = clean_sec_name
                print(f"📌 Scanning Data Section: [{current_section}]")

        # Save _SECTION_RULE_ if Filter or Join is present
        if "ONLY CONSIDERED" in row_str.upper() or "LINK " in row_str.upper():
            parsed_sec = parse_section_rule(row_str)
            sec_dsl_obj: SectionRuleDSL = parsed_sec["dsl_obj"]

            if sec_dsl_obj.filter_condition or sec_dsl_obj.join_rule:
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
                        sec_dsl_obj.model_dump_json(),
                        parsed_sec["dsl_readable"],
                        parsed_sec["status"],
                        "SYSTEM",
                    ),
                )
                stats["section_rules"] += 1
                print(f"   🚩 [SECTION_RULE] Locking filter for [{current_section}] -> {parsed_sec['dsl_readable']}")
                continue

        target_field = str(row.iloc[field_col_idx]).strip() if len(row) > field_col_idx and pd.notna(row.iloc[field_col_idx]) else ""

        is_valid_field = (
            bool(target_field)
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

        if data_file.lower() in ["nan", "none"]: data_file = ""
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
        else:
            parsed_res = parse_notes_to_dsl(data_file, col, raw_notes)
            rule_dsl_obj = parsed_res["dsl_obj"]

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
                    rule_dsl_obj.model_dump_json(),
                    parsed_res["dsl_readable"],
                    parsed_res["status"],
                    "SYSTEM",
                ),
            )

            if parsed_res["rule_type"] == "NO_MAPPING":
                stats["no_mapping"] += 1
            elif parsed_res["status"] == "AUTO_PARSED":
                stats["auto_parsed"] += 1
            else:
                stats["needs_review"] += 1

    print("=" * 50)
    print(f"📊 SUMMARY FOR SHEET [{sheet_name}]: Auto-Parsed: {stats['auto_parsed']} | Section Rules: {stats['section_rules']} | Needs Review: {stats['needs_review']}")
    print("=" * 50)


def process_all_mapping_sheets(excel_path: str, cu_id: str, target_sheet: str = None):
    """Automatically process mapping sheets, optionally filtering for a specific sheet."""
    print(f"📖 Opening Excel file: {excel_path} for CU: {cu_id}")
    xl = pd.ExcelFile(excel_path)
    ignore_sheets = ["cover", "index", "readme", "instruction", "instructions", "summary"]
    
    valid_sheets = [s for s in xl.sheet_names if s.strip().lower() not in ignore_sheets]

    # Filter for specific sheet if target_sheet is provided
    if target_sheet:
        valid_sheets = [s for s in valid_sheets if target_sheet.lower() in s.lower()]
        print(f"🎯 Target Sheet Filter Enabled: Processing only [{target_sheet}] -> Found: {valid_sheets}")
    else:
        print(f"🚀 Found {len(valid_sheets)} valid mapping sheets: {valid_sheets}")

    if not valid_sheets:
        print(f"⚠️ No matching sheets found for filter: '{target_sheet}'")
        return

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        
        for sheet in valid_sheets:
            try:
                raw_df = xl.parse(sheet, header=None)
                process_mapping_sheet(raw_df, sheet_name=sheet, conn=conn, cu_id=cu_id)
            except Exception as e:
                print(f"❌ Error processing sheet [{sheet}]: {e}")
        
        conn.commit()

def find_mapping_file(ingest_dir: Path) -> Path:
    """Find the mapping Excel file inside workspace/ingest/."""
    for file in ingest_dir.glob("*.xlsx"):
        if file.name.startswith("~$"): continue
        if "mapping" in file.name.lower():
            print(f"🎯 Automatically found mapping file: {file.name}")
            return file
    raise FileNotFoundError("❌ NO mapping file found in workspace/ingest/")


def extract_cu_id_from_filename(filename: str) -> str:
    """Extract CU ID dynamically by filtering out generic keywords like 'Data', 'Mapping', etc."""
    clean_stem = Path(filename).stem  # Removes .xlsx extension
    
    # Ignore common workflow keywords to isolate the actual CU Name
    ignore_keywords = {"data", "mapping", "file", "sheet", "discovery", "matrix", "test", "v1", "v2"}
    
    # Extract all alphanumeric words
    words = re.findall(r"[A-Za-z0-9]+", clean_stem)
    
    # Find the first word that is not a generic keyword
    for word in words:
        if word.lower() not in ignore_keywords:
            return word.upper()
            
    raise ValueError(f"❌ Cannot automatically detect CU ID from filename: '{filename}'.")

if __name__ == "__main__":
    INGEST_DIR = BASE_DIR / "workspace" / "ingest"
    try:
        excel_file_path = find_mapping_file(INGEST_DIR)
        
        # Read CU ID and Target Sheet dynamically from CLI arguments
        target_cu_id = sys.argv[1].upper() if len(sys.argv) > 1 else extract_cu_id_from_filename(excel_file_path.name)
        target_sheet = sys.argv[2] if len(sys.argv) > 2 else "Shares"  # Default to 'Shares' if not specified
        
        process_all_mapping_sheets(str(excel_file_path), cu_id=target_cu_id, target_sheet=target_sheet)
    except Exception as e:
        print(f"💥 Error: {e}")