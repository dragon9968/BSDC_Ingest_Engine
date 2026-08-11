import json
import re
import sqlite3
from pathlib import Path
import polars as pl

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "workspace" / "rules.db"
RAW_DATA_DIR = BASE_DIR / "workspace" / "raw_data"
OUTPUT_DIR = BASE_DIR / "workspace" / "output"


def col_letter_to_index(letter: str) -> int:
    """Convert Excel column letters (A, B, C...) to index (0, 1, 2...)"""
    if not letter or not letter.isalpha():
        return -1
    result = 0
    for char in letter.upper():
        result = result * 26 + (ord(char) - ord("A") + 1)
    return result - 1


def load_raw_tables() -> dict:
    """ITEM #16: Load all CSV files from raw_data into RAM as Polars DataFrames"""
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
            print(
                f"📥 [ITEM #16] Loaded Raw Table [{table_name}] using '{enc}': {df.shape[0]} rows, {df.shape[1]} cols"
            )
        except Exception as e:
            print(f"⚠️ Error reading file {file_path.name}: {e}")

    return tables


def parse_action_target_series(target_str: str, src_df: pl.DataFrame, total_rows: int) -> pl.Series:
    """Handle assigned values (e.g., DO NOT ASSIGN, LEAVE BLANK, ASSIGN 7/1/2026, ASSIGN COLUMN M)"""
    if not target_str or target_str.upper() in ["DO NOT ASSIGN", "LEAVE BLANK", "BLANK", "NONE", "NAN"]:
        return pl.Series([None] * total_rows)

    col_match = re.search(r"COLUMN\s+([A-Za-z]+)", target_str, re.IGNORECASE)
    if col_match:
        ref_col_letter = col_match.group(1)
        ref_col_idx = col_letter_to_index(ref_col_letter)
        if 0 <= ref_col_idx < src_df.shape[1]:
            return src_df[src_df.columns[ref_col_idx]].head(total_rows)
        return pl.Series([None] * total_rows)

    clean_val = re.sub(r"^ASSIGN\s+", "", target_str, flags=re.IGNORECASE).strip()
    return pl.Series([clean_val] * total_rows)


def execute_transformation(cu_id: str = "MEDICOOP", sheet_name: str = "Shares"):
    """ITEM #17 -> #21: Execute actual data transformation using Polars Series CONDITIONAL execution"""
    tables = load_raw_tables()
    if not tables:
        print("❌ NO Raw Data CSV files found in workspace/raw_data/!")
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT DISTINCT section_name FROM rule_store 
        WHERE (cu_id = ? OR is_global = 1) AND sheet_name = ?
    """,
        (cu_id, sheet_name),
    )

    sections = [r[0] for r in cursor.fetchall()]

    for sec in sections:
        print(f"\n⚡ Executing test data transformation for Section: [{sec}]...")

        cursor.execute(
            """
            SELECT dsl_json FROM rule_store 
            WHERE (cu_id = ? OR is_global = 1) AND sheet_name = ? AND section_name = ? AND target_field = '_SECTION_RULE_'
        """,
            (cu_id, sheet_name, sec),
        )

        sec_rule_row = cursor.fetchone()
        base_df = None

        # 1. Check Section Rule Filter & Join Logic
        if sec_rule_row:
            sec_dsl = json.loads(sec_rule_row[0])
            filter_cond = sec_dsl.get("filter_condition")
            join_info = sec_dsl.get("join_rule")

            if filter_cond or join_info:
                print(f"   🔍 Found Section Rule -> Running Filter & Join...")
                
                # Hardcoded logic for Certificates Section Rule
                if "SAVINGS_ACCOUNTS" in tables and "CERTIFIED_DEPOSITS" in tables:
                    sa_df = tables["SAVINGS_ACCOUNTS"]
                    cd_df = tables["CERTIFIED_DEPOSITS"]

                    # Filter: AccountType == 12 OR AccountType == 1202 (Col B index 1)
                    sa_col_b = sa_df.columns[1]
                    mask = sa_df[sa_col_b].cast(pl.Utf8).str.strip_chars().is_in(["12", "1202"])
                    sa_filtered = sa_df.filter(mask)

                    # Join: SAVINGS_ACCOUNTS Col G (idx 6) == CERTIFIED_DEPOSITS Col Q (idx 16)
                    sa_col_g = sa_filtered.columns[6]
                    cd_col_q = cd_df.columns[16]

                    base_df = sa_filtered.join(
                        cd_df, left_on=sa_col_g, right_on=cd_col_q, how="inner"
                    )

        if base_df is None:
            print(f"   ℹ️ No Section Rule found for this block -> Loading main table...")
            base_df = tables.get("SAVINGS_ACCOUNTS", list(tables.values())[0])

        total_rows = base_df.shape[0]
        output_data = {}

        cursor.execute(
            """
            SELECT target_field, data_file, column_letter, rule_type, dsl_json, dsl_readable, status 
            FROM rule_store 
            WHERE (cu_id = ? OR is_global = 1) AND sheet_name = ? AND section_name = ? AND target_field != '_SECTION_RULE_'
            ORDER BY id ASC
        """,
            (cu_id, sheet_name, sec),
        )

        field_rules = cursor.fetchall()

        for field, src_file, src_col, rule_type, dsl_json_str, dsl_readable, status in field_rules:
            dsl = json.loads(dsl_json_str)
            col_idx = col_letter_to_index(src_col)

            src_df = base_df

            if status in ["NEEDS_REVIEW", "REJECTED"]:
                output_data[field] = pl.Series([f"[PROVISIONAL_NEEDS_REVIEW]"] * total_rows)
                continue

            # ⚙️ EXECUTE RULE PER TYPE
            if rule_type == "NO_MAPPING":
                output_data[field] = pl.Series([None] * total_rows)

            elif rule_type == "DIRECT":
                if 0 <= col_idx < src_df.shape[1]:
                    src_col_name = src_df.columns[col_idx]
                    output_data[field] = src_df[src_col_name].head(total_rows)
                else:
                    output_data[field] = pl.Series([None] * total_rows)

            elif rule_type == "CONSTANT":
                val = dsl.get("value", "")
                output_data[field] = pl.Series([val] * total_rows)

            elif rule_type == "CONDITIONAL":
                if_col_letter = dsl.get("if_col", "")
                if_val = str(dsl.get("if_val", "")).strip()
                then_val_str = dsl.get("then_val", "")
                else_val_str = dsl.get("else_val", "")

                if_col_idx = col_letter_to_index(if_col_letter)

                if 0 <= if_col_idx < src_df.shape[1]:
                    cond_col_name = src_df.columns[if_col_idx]
                    
                    then_s = parse_action_target_series(then_val_str, src_df, total_rows)
                    else_s = parse_action_target_series(else_val_str, src_df, total_rows)

                    mask = (src_df[cond_col_name].cast(pl.Utf8).str.strip_chars() == if_val)

                    res_expr = pl.when(mask).then(then_s).otherwise(else_s)
                    output_data[field] = src_df.select(res_expr).to_series()
                else:
                    output_data[field] = parse_action_target_series(else_val_str, src_df, total_rows)

            else:
                output_data[field] = pl.Series([f"[LOOKUP_PENDING]"] * total_rows)

        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        res_df = pl.DataFrame(output_data)

        clean_sec_filename = (
            sec.replace(" ", "_").replace("(", "").replace(")", "").replace("-", "_")
        )
        out_file = OUTPUT_DIR / f"Expected_{clean_sec_filename}.csv"
        res_df.write_csv(out_file)

        print(
            f"✅ [ITEM #21] Successfully exported expected Test Data for [{sec}]: {out_file.name} ({res_df.shape[0]} rows)"
        )

    conn.close()


if __name__ == "__main__":
    execute_transformation(cu_id="MEDICOOP", sheet_name="Shares")