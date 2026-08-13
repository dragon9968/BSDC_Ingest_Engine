import json
import re
import sqlite3
from pathlib import Path
import polars as pl

from schemas import (
    SectionRuleDSL,
    ConditionalRuleDSL,
    ConstantRuleDSL,
    JoinRuleModel,
    RuleDSLAdapter,
)

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "workspace" / "rules.db"
RAW_DATA_DIR = BASE_DIR / "workspace" / "raw_data"
OUTPUT_DIR = BASE_DIR / "workspace" / "output"


def col_letter_to_index(letter: str) -> int:
    """Convert Excel column letters (A, B, C...) to 0-based index (0, 1, 2...)"""
    if not letter or not letter.isalpha():
        return -1
    result = 0
    for char in letter.upper():
        result = result * 26 + (ord(char) - ord("A") + 1)
    return result - 1


def load_raw_tables() -> dict:
    """Load all CSV files from raw_data folder into RAM as Polars DataFrames"""
    RAW_DATA_DIR.mkdir(parents=True, exist_ok=True)
    tables = {}

    for file_path in RAW_DATA_DIR.glob("*.csv"):
        table_name = file_path.stem.upper()
        try:
            try:
                df = pl.read_csv(file_path, infer_schema_length=0, ignore_errors=True, encoding="utf8")
                enc = "utf-8"
            except Exception:
                df = pl.read_csv(file_path, infer_schema_length=0, ignore_errors=True, encoding="latin1")
                enc = "latin1"
            tables[table_name] = df
            print(f"📥 Loaded Raw Table [{table_name}] using '{enc}': {df.shape[0]} rows, {df.shape[1]} cols")
        except Exception as e:
            print(f"⚠️ Error reading file {file_path.name}: {e}")

    return tables


def resolve_column_name(data_file: str, col_letter: str, default_table: str, available_cols: list) -> str | None:
    """Resolve exact DataFrame column name based on Table Name + Column Letter (A, B, C...)"""
    data_file_clean = data_file.strip().upper() if data_file and str(data_file).strip().upper() not in ["", "N/A", "NAN", "NONE"] else None
    tbl = data_file_clean or (default_table or "").upper()
    col_idx = col_letter_to_index(col_letter)
    if col_idx < 0:
        return None

    target_col = f"{tbl}::col_{col_idx}"
    if target_col in available_cols:
        return target_col

    # Fallback ONLY if data_file was not explicitly specified in mapping
    if not data_file_clean:
        for col in available_cols:
            if col.endswith(f"::col_{col_idx}"):
                return col

    return None


def parse_action_target_series(
    target_str: str, src_df: pl.DataFrame, total_rows: int, data_file: str = None, default_table: str = None
) -> pl.Series:
    """Handle assigned values & COLUMN references accurately"""
    if not target_str or str(target_str).upper() in ["DO NOT ASSIGN", "LEAVE BLANK", "BLANK", "NONE", "NAN"]:
        return pl.Series([None] * total_rows)

    col_match = re.search(r"COLUMN\s+([A-Za-z]+)", str(target_str), re.IGNORECASE)
    if col_match:
        ref_col_letter = col_match.group(1)
        col_name = resolve_column_name(data_file, ref_col_letter, default_table, src_df.columns)
        if col_name and col_name in src_df.columns:
            return src_df[col_name].head(total_rows)
        return pl.Series([None] * total_rows)

    clean_val = re.sub(r"^ASSIGN\s+", "", str(target_str), flags=re.IGNORECASE).strip()
    return pl.Series([clean_val] * total_rows)


def parse_and_apply_section_rule(sec_dsl: SectionRuleDSL, tables: dict, default_table_key: str = None) -> pl.DataFrame | None:
    """Dynamic Filter & Join Engine using Pydantic SectionRuleDSL with Namespaced Columns"""
    filter_cond = sec_dsl.filter_condition or ""
    join_info = sec_dsl.join_rule

    # Namespace all table columns: TBL_NAME::col_0, TBL_NAME::col_1...
    prefixed_tables = {}
    for tbl_name, df in tables.items():
        new_cols = [f"{tbl_name}::col_{i}" for i in range(df.shape[1])]
        prefixed_tables[tbl_name] = df.rename(dict(zip(df.columns, new_cols)))

    left_df, right_df = None, None
    left_col_name, right_col_name = None, None

    tbl1, col1_let, tbl2, col2_let = None, None, None, None

    # Parse Join Rule using Pydantic type checking
    if isinstance(join_info, JoinRuleModel):
        tbl1 = join_info.source_file.upper()
        col1_let = join_info.source_col
        tbl2 = join_info.target_file.upper()
        col2_let = join_info.target_col
    elif isinstance(join_info, dict) and join_info:
        tbl1 = str(join_info.get("source_file", "")).upper()
        col1_let = str(join_info.get("source_col", ""))
        tbl2 = str(join_info.get("target_file", "")).upper()
        col2_let = str(join_info.get("target_col", ""))
    elif isinstance(join_info, str) and join_info:
        join_match = re.search(
            r"LINK\s+([A-Za-z0-9_]+)\s+COLUMN\s+([A-Za-z]+)\s+TO\s+([A-Za-z0-9_]+)\s+COLUMN\s+([A-Za-z]+)",
            join_info,
            re.IGNORECASE,
        )
        if join_match:
            tbl1, col1_let, tbl2, col2_let = join_match.groups()
            tbl1, tbl2 = tbl1.upper(), tbl2.upper()

    if tbl1 and tbl2 and col1_let and col2_let:
        if tbl1 in prefixed_tables and tbl2 in prefixed_tables:
            left_df = prefixed_tables[tbl1]
            right_df = prefixed_tables[tbl2]

            c1_idx = col_letter_to_index(col1_let)
            c2_idx = col_letter_to_index(col2_let)

            left_col_name = f"{tbl1}::col_{c1_idx}"
            right_col_name = f"{tbl2}::col_{c2_idx}"

    if left_df is None and default_table_key and default_table_key in prefixed_tables:
        left_df = prefixed_tables[default_table_key]

    # Parse Filter Condition: COLUMN <COL> = <VAL1> OR <VAL2>
    if filter_cond and left_df is not None:
        col_match = re.search(r"COLUMN\s+([A-Za-z]+)", str(filter_cond), re.IGNORECASE)
        if col_match:
            f_col_let = col_match.group(1)
            f_col_idx = col_letter_to_index(f_col_let)
            left_tbl_name = tbl1 if tbl1 else (default_table_key if default_table_key else list(prefixed_tables.keys())[0])
            f_col_name = f"{left_tbl_name}::col_{f_col_idx}"

            if f_col_name in left_df.columns:
                vals_raw = re.sub(r".*?COLUMN\s+[A-Za-z]+\s*=\s*", "", str(filter_cond), flags=re.IGNORECASE)
                vals = [v.strip("'\" ") for v in re.split(r"\s+OR\s+|\s*,\s*", vals_raw, flags=re.IGNORECASE) if v.strip()]

                if vals:
                    mask = left_df[f_col_name].cast(pl.Utf8).str.strip_chars().is_in(vals)
                    left_df = left_df.filter(mask)
                    print(f"   🎯 Applied dynamic filter on [{f_col_name}] with values {vals}")

    # Execute Join
    if left_df is not None and right_df is not None and left_col_name and right_col_name:
        joined_df = left_df.join(right_df, left_on=left_col_name, right_on=right_col_name, how="inner")
        print(f"   🔗 Dynamic Join executed: {left_col_name} <-> {right_col_name}")
        return joined_df

    return left_df


def execute_transformation(cu_id: str = "MEDICOOP", sheet_name: str = None):
    """Execute data transformation dynamically across any Sheet & Section"""
    tables = load_raw_tables()
    if not tables:
        print("❌ NO Raw Data CSV files found in workspace/raw_data/!")
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    if sheet_name:
        target_sheets = [sheet_name]
    else:
        cursor.execute("SELECT DISTINCT sheet_name FROM rule_store WHERE cu_id = ? OR is_global = 1", (cu_id,))
        target_sheets = [r[0] for r in cursor.fetchall() if r[0]]

    for current_sheet in target_sheets:
        print(f"\n==================================================")
        print(f"📂 PROCESSING SHEET: [{current_sheet}]")
        print(f"==================================================")

        cursor.execute(
            """
            SELECT DISTINCT section_name FROM rule_store 
            WHERE (cu_id = ? OR is_global = 1) AND sheet_name = ? AND section_name IS NOT NULL
            """,
            (cu_id, current_sheet),
        )
        sections = [r[0] for r in cursor.fetchall()]

        for sec in sections:
            print(f"\n⚡ Transforming Section: [{sec}]...")

            cursor.execute(
                """
                SELECT DISTINCT data_file FROM rule_store 
                WHERE (cu_id = ? OR is_global = 1) AND sheet_name = ? AND section_name = ? 
                AND data_file IS NOT NULL AND data_file != '' AND data_file != 'N/A'
                LIMIT 1
                """,
                (cu_id, current_sheet, sec),
            )
            data_file_row = cursor.fetchone()
            default_table_key = Path(data_file_row[0]).stem.upper() if data_file_row and data_file_row[0] else None

            cursor.execute(
                """
                SELECT dsl_json FROM rule_store 
                WHERE (cu_id = ? OR is_global = 1) AND sheet_name = ? AND section_name = ? AND target_field = '_SECTION_RULE_'
                """,
                (cu_id, current_sheet, sec),
            )
            sec_rule_row = cursor.fetchone()

            base_df = None
            if sec_rule_row and sec_rule_row[0]:
                try:
                    # Validate DSL JSON via Pydantic model
                    sec_dsl = SectionRuleDSL.model_validate_json(sec_rule_row[0])
                    base_df = parse_and_apply_section_rule(sec_dsl, tables, default_table_key)
                except Exception as e:
                    print(f"⚠️ Error parsing section rule: {e}")

            if base_df is None and default_table_key in tables:
                df = tables[default_table_key]
                new_cols = [f"{default_table_key}::col_{i}" for i in range(df.shape[1])]
                base_df = df.rename(dict(zip(df.columns, new_cols)))

            if base_df is None or base_df.shape[0] == 0:
                print(f"❌ No valid data rows found for Section [{sec}]. Skipping...")
                continue

            total_rows = base_df.shape[0]
            output_data = {}

            cursor.execute(
                """
                SELECT target_field, data_file, column_letter, rule_type, dsl_json, dsl_readable, status 
                FROM rule_store 
                WHERE (cu_id = ? OR is_global = 1) AND sheet_name = ? AND section_name = ? AND target_field != '_SECTION_RULE_'
                ORDER BY id ASC
                """,
                (cu_id, current_sheet, sec),
            )
            field_rules = cursor.fetchall()

            for field, src_file, src_col, rule_type, dsl_json_str, dsl_readable, status in field_rules:
                # Parse DSL model via Pydantic TypeAdapter validation
                rule_dsl = RuleDSLAdapter.validate_json(dsl_json_str) if dsl_json_str else None
                src_df = base_df

                if status in ["NEEDS_REVIEW", "REJECTED"]:
                    output_data[field] = pl.Series([f"[PROVISIONAL_NEEDS_REVIEW]"] * total_rows)
                    continue

                if rule_type == "NO_MAPPING":
                    output_data[field] = pl.Series([None] * total_rows)

                elif rule_type == "DIRECT":
                    target_col = resolve_column_name(src_file, src_col, default_table_key, src_df.columns)
                    if target_col and target_col in src_df.columns:
                        output_data[field] = src_df[target_col].head(total_rows)
                    else:
                        output_data[field] = pl.Series([None] * total_rows)

                elif rule_type == "CONSTANT":
                    val = rule_dsl.value if isinstance(rule_dsl, ConstantRuleDSL) else ""
                    output_data[field] = pl.Series([val] * total_rows)

                elif rule_type == "CONDITIONAL":
                    if isinstance(rule_dsl, ConditionalRuleDSL):
                        if_col_letter = rule_dsl.if_col
                        if_val = str(rule_dsl.if_val).strip()
                        then_val_str = rule_dsl.then_val
                        else_val_str = rule_dsl.else_val
                    else:
                        if_col_letter, if_val, then_val_str, else_val_str = "", "", "", ""

                    cond_col_name = resolve_column_name(src_file, if_col_letter, default_table_key, src_df.columns)

                    if cond_col_name and cond_col_name in src_df.columns:
                        then_s = parse_action_target_series(then_val_str, src_df, total_rows, src_file, default_table_key)
                        else_s = parse_action_target_series(else_val_str, src_df, total_rows, src_file, default_table_key)

                        mask = src_df[cond_col_name].cast(pl.Utf8).str.strip_chars() == if_val
                        res_expr = pl.when(mask).then(then_s).otherwise(else_s)
                        output_data[field] = src_df.select(res_expr).to_series()
                    else:
                        output_data[field] = parse_action_target_series(else_val_str, src_df, total_rows, src_file, default_table_key)

                else:
                    output_data[field] = pl.Series([f"[LOOKUP_PENDING]"] * total_rows)

            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            res_df = pl.DataFrame(output_data)

            # Sanitize Sheet and Section names (remove forbidden filename characters like / \ : * ? " < > |)
            clean_sheet = current_sheet.replace(" ", "_").replace("/", "_").replace("\\", "_")
            clean_sec = (
                sec.replace(" ", "_")
                .replace("/", "_")
                .replace("\\", "_")
                .replace("(", "")
                .replace(")", "")
                .replace("-", "_")
            )
            out_file = OUTPUT_DIR / f"Expected_{clean_sheet}_{clean_sec}.csv"
            res_df.write_csv(out_file)

            print(f"✅ Successfully exported Test Data: {out_file.name} ({res_df.shape[0]} rows)")

    conn.close()


if __name__ == "__main__":
    execute_transformation(cu_id="MEDICOOP", sheet_name=None)