import json
import re
import sqlite3
import sys
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
    """Convert Excel column letters (A, B, C...) to 0-based index."""
    if not letter:
        return -1
    words = re.findall(r"[A-Za-z]+", str(letter))
    if not words:
        return -1
    
    clean_letter = words[-1].upper()
    result = 0
    for char in clean_letter:
        result = result * 26 + (ord(char) - ord("A") + 1)
    return result - 1


def load_raw_tables() -> dict:
    """Load all CSV files from raw_data folder into RAM as Polars DataFrames."""
    RAW_DATA_DIR.mkdir(parents=True, exist_ok=True)
    tables = {}

    for file_path in RAW_DATA_DIR.glob("*.csv"):
        table_name = file_path.stem.upper().replace(" ", "_")
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


def get_available_cu_ids(db_path: Path) -> list[str]:
    """Retrieve distinct non-global CU IDs stored in the rule_store database."""
    if not db_path.exists():
        return []
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT DISTINCT cu_id FROM rule_store WHERE cu_id IS NOT NULL AND cu_id != '' AND is_global != 1"
        )
        rows = cursor.fetchall()
        return [r[0] for r in rows if r[0]]


def resolve_column_name(data_file: str, col_letter: str, default_table: str, available_cols: list) -> str | None:
    """Resolve exact DataFrame column name based on Table Name + Column Letter (A, B, C...)."""
    data_file_clean = data_file.strip().upper().replace(" ", "_") if data_file and str(data_file).strip().upper() not in ["", "N/A", "NAN", "NONE"] else None
    col_idx = col_letter_to_index(col_letter)
    if col_idx < 0:
        return None

    if data_file_clean:
        target_col = f"{data_file_clean}::col_{col_idx}"
        if target_col in available_cols:
            return target_col

    if default_table:
        target_col = f"{default_table.upper().replace(' ', '_')}::col_{col_idx}"
        if target_col in available_cols:
            return target_col

    matching_cols = [c for c in available_cols if str(c).endswith(f"::col_{col_idx}")]
    if matching_cols:
        return matching_cols[0]

    return None


def ensure_field_table_joined(src_file: str, src_df: pl.DataFrame, tables: dict) -> pl.DataFrame:
    """Automatically Left Join external table (e.g. CERTIFICATE_ACCRUALS) into src_df if not yet present."""
    if not src_file or src_df is None or src_df.is_empty():
        return src_df

    src_file_upper = src_file.strip().upper().replace(" ", "_")
    if src_file_upper in ["", "N/A", "NAN", "NONE"] or src_file_upper not in tables:
        return src_df

    if any(str(c).startswith(f"{src_file_upper}::") for c in src_df.columns):
        return src_df

    right_raw = tables[src_file_upper]
    new_cols = [f"{src_file_upper}::col_{i}" for i in range(right_raw.shape[1])]
    right_df = right_raw.rename(dict(zip(right_raw.columns, new_cols)))

    best_left_col, best_right_col = None, None
    max_matches = 0

    for r_col in right_df.columns:
        r_series = right_df[r_col].cast(pl.Utf8).str.strip_chars()
        r_vals = set(r_series.drop_nulls().to_list())
        if len(r_vals) < 5:
            continue

        for l_col in src_df.columns:
            l_series = src_df[l_col].cast(pl.Utf8).str.strip_chars()
            l_vals = set(l_series.drop_nulls().to_list())
            if len(l_vals) < 5:
                continue

            common_cnt = len(r_vals.intersection(l_vals))
            if common_cnt > max_matches and common_cnt > 10:
                max_matches = common_cnt
                best_left_col = l_col
                best_right_col = r_col

    if best_left_col and best_right_col:
        print(f"   🔗 Dynamic Auto-Join executed for [{src_file_upper}]: {best_left_col} <-> {best_right_col} ({max_matches} matches)")
        
        src_df_temp = src_df.with_columns(pl.col(best_left_col).cast(pl.Utf8).str.strip_chars().alias("_join_key_l"))
        right_df_temp = right_df.with_columns(pl.col(best_right_col).cast(pl.Utf8).str.strip_chars().alias("_join_key_r"))

        joined_df = src_df_temp.join(right_df_temp, left_on="_join_key_l", right_on="_join_key_r", how="left")
        cols_to_drop = [c for c in ["_join_key_l", "_join_key_r"] if c in joined_df.columns]
        return joined_df.drop(cols_to_drop)

    return src_df


def parse_action_target_expr(
    target_str: str, src_df: pl.DataFrame, data_file: str = None, default_table: str = None
) -> pl.Expr:
    """Return a Polars Expression for assigned values, COLUMN references, arithmetic expressions, or nested IF conditions."""
    if not target_str or str(target_str).upper() in ["DO NOT ASSIGN", "LEAVE BLANK", "BLANK", "NONE", "NAN"]:
        return pl.lit(None)

    target_str_clean = str(target_str).strip()

    # 1. Parse nested IF conditions
    nested_if = re.search(
        r"IF\s+(?:COLUMN\s+)?([A-Za-z0-9_\-\.]+)\s*=\s*([A-Za-z0-9_\-\.]+)\s+THEN\s+(.*?)(?:\s*(?:;|\s+)\s*ELSE\s+(.*)|\s*;\s*(IF.*)|$)",
        target_str_clean,
        re.IGNORECASE,
    )
    if nested_if:
        sub_col, sub_val, sub_then, sub_else1, sub_else2 = nested_if.groups()
        sub_else = sub_else1 or sub_else2 or ""

        cond_col = resolve_column_name(data_file, sub_col, default_table, src_df.columns)
        if not cond_col and sub_col in src_df.columns:
            cond_col = sub_col

        then_expr = parse_action_target_expr(sub_then, src_df, data_file, default_table)
        else_expr = parse_action_target_expr(sub_else, src_df, data_file, default_table)

        if cond_col and cond_col in src_df.columns:
            mask = pl.col(cond_col).cast(pl.Utf8).str.strip_chars() == str(sub_val).strip()
            return pl.when(mask).then(then_expr).otherwise(else_expr)
        else:
            return else_expr if else_expr is not None else then_expr

    # 2. Strip 'ASSIGN ' prefix FIRST
    clean_val = re.sub(r"^ASSIGN\s+", "", target_str_clean, flags=re.IGNORECASE).strip()

    # 3. Check for arithmetic column expressions like 'E / 100', 'COLUMN E / 100', 'E * 100'
    arith_match = re.search(
        r"^(?:COLUMN\s+)?([A-Za-z]+)\s*([/*\+\-])\s*([0-9\.]+)\s*$",
        clean_val,
        re.IGNORECASE,
    )
    if arith_match:
        col_let, op, num_str = arith_match.groups()
        col_name = resolve_column_name(data_file, col_let, default_table, src_df.columns)
        if col_name and col_name in src_df.columns:
            val_num = float(num_str)
            col_expr = pl.col(col_name).cast(pl.Float64, strict=False)
            if op == "/":
                return col_expr / val_num
            elif op == "*":
                return col_expr * val_num
            elif op == "+":
                return col_expr + val_num
            elif op == "-":
                return col_expr - val_num

    # 4. Check if clean_val is a column reference like 'COLUMN M' or 'M'
    col_match = re.search(r"^COLUMN\s+([A-Za-z]+)$", clean_val, re.IGNORECASE)
    if col_match:
        ref_col_letter = col_match.group(1)
        col_name = resolve_column_name(data_file, ref_col_letter, default_table, src_df.columns)
        if col_name and col_name in src_df.columns:
            return pl.col(col_name)
        return pl.lit(None)

    # 5. Fallback to literal value
    return pl.lit(clean_val)


def parse_and_apply_section_rule(sec_dsl: SectionRuleDSL, tables: dict, default_table_key: str = None) -> pl.DataFrame | None:
    """Dynamic Filter & Join Engine using Pydantic SectionRuleDSL with Namespaced Columns."""
    filter_cond = sec_dsl.filter_condition or ""
    join_info = sec_dsl.join_rule

    prefixed_tables = {}
    for tbl_name, df in tables.items():
        new_cols = [f"{tbl_name}::col_{i}" for i in range(df.shape[1])]
        prefixed_tables[tbl_name] = df.rename(dict(zip(df.columns, new_cols)))

    left_df, right_df = None, None
    left_col_name, right_col_name = None, None

    tbl1, col1_let, tbl2, col2_let = None, None, None, None

    if isinstance(join_info, JoinRuleModel):
        tbl1 = join_info.source_file.upper().replace(" ", "_")
        col1_let = join_info.source_col
        tbl2 = join_info.target_file.upper().replace(" ", "_")
        col2_let = join_info.target_col
    elif isinstance(join_info, dict) and join_info:
        tbl1 = str(join_info.get("source_file", "")).upper().replace(" ", "_")
        col1_let = str(join_info.get("source_col", ""))
        tbl2 = str(join_info.get("target_file", "")).upper().replace(" ", "_")
        col2_let = str(join_info.get("target_col", ""))
    elif isinstance(join_info, str) and join_info:
        join_match = re.search(
            r"LINK\s+([A-Za-z0-9_]+)\s+COLUMN\s+([A-Za-z]+)\s+TO\s+([A-Za-z0-9_]+)\s+COLUMN\s+([A-Za-z]+)",
            join_info,
            re.IGNORECASE,
        )
        if join_match:
            tbl1, col1_let, tbl2, col2_let = join_match.groups()
            tbl1, tbl2 = tbl1.upper().replace(" ", "_"), tbl2.upper().replace(" ", "_")

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

    if left_df is not None and right_df is not None and left_col_name and right_col_name:
        joined_df = left_df.join(right_df, left_on=left_col_name, right_on=right_col_name, how="inner")
        print(f"   🔗 Dynamic Join executed: {left_col_name} <-> {right_col_name}")
        return joined_df

    return left_df


def execute_transformation(cu_id: str = None, sheet_name: str = None):
    """Execute data transformation dynamically across any Sheet & Section for given CU(s)."""
    if not cu_id:
        available_cus = get_available_cu_ids(DB_PATH)
        if not available_cus:
            print("❌ No valid CU IDs found in rule_store database. Please run rule_engine.py first!")
            return
        print(f"🔍 Auto-detected CU IDs from Database: {available_cus}")
        for target_cu in available_cus:
            execute_transformation(cu_id=target_cu, sheet_name=sheet_name)
        return

    tables = load_raw_tables()
    if not tables:
        print("❌ NO Raw Data CSV files found in workspace/raw_data/!")
        return

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()

        if sheet_name:
            target_sheets = [sheet_name]
        else:
            cursor.execute("SELECT DISTINCT sheet_name FROM rule_store WHERE cu_id = ? OR is_global = 1", (cu_id,))
            target_sheets = [r[0] for r in cursor.fetchall() if r[0]]

        for current_sheet in target_sheets:
            print(f"\n==================================================")
            print(f"📂 PROCESSING SHEET: [{current_sheet}] FOR CU: [{cu_id}]")
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
                    SELECT dsl_json FROM rule_store 
                    WHERE (cu_id = ? OR is_global = 1) AND sheet_name = ? AND section_name = ? AND target_field = '_SECTION_RULE_'
                    """,
                    (cu_id, current_sheet, sec),
                )
                sec_rule_row = cursor.fetchone()

                default_table_key = None
                base_df = None

                if sec_rule_row and sec_rule_row[0]:
                    try:
                        sec_dsl = SectionRuleDSL.model_validate_json(sec_rule_row[0])
                        if sec_dsl.join_rule:
                            if isinstance(sec_dsl.join_rule, JoinRuleModel):
                                default_table_key = (sec_dsl.join_rule.target_file or sec_dsl.join_rule.source_file).upper().replace(" ", "_")
                            elif isinstance(sec_dsl.join_rule, dict):
                                default_table_key = str(sec_dsl.join_rule.get("target_file") or sec_dsl.join_rule.get("source_file")).upper().replace(" ", "_")
                        base_df = parse_and_apply_section_rule(sec_dsl, tables, default_table_key)
                    except Exception as e:
                        print(f"⚠️ Error parsing section rule: {e}")

                if not default_table_key:
                    cursor.execute(
                        """
                        SELECT data_file, COUNT(*) as cnt FROM rule_store 
                        WHERE (cu_id = ? OR is_global = 1) AND sheet_name = ? AND section_name = ? 
                        AND data_file IS NOT NULL AND data_file != '' AND data_file != 'N/A'
                        GROUP BY data_file ORDER BY cnt DESC LIMIT 1
                        """,
                        (cu_id, current_sheet, sec),
                    )
                    data_file_row = cursor.fetchone()
                    default_table_key = Path(data_file_row[0]).stem.upper().replace(" ", "_") if data_file_row and data_file_row[0] else None

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
                    SELECT target_field, data_file, column_letter, rule_type, dsl_json, dsl_readable, status, raw_notes 
                    FROM rule_store 
                    WHERE (cu_id = ? OR is_global = 1) AND sheet_name = ? AND section_name = ? AND target_field != '_SECTION_RULE_'
                    ORDER BY id ASC
                    """,
                    (cu_id, current_sheet, sec),
                )
                field_rules = cursor.fetchall()

                # Loop through each field inside the section
                for field, src_file, src_col, rule_type, dsl_json_str, dsl_readable, status, raw_notes in field_rules:
                    rule_dsl = RuleDSLAdapter.validate_json(dsl_json_str) if dsl_json_str else None
                    
                    # Ensure On-demand Field Join is executed for external tables
                    src_df = ensure_field_table_joined(src_file, base_df, tables)
                    src_cols = src_df.columns if src_df is not None else []

                    if status in ["NEEDS_REVIEW", "REJECTED"]:
                        output_data[field] = pl.Series([f"[PROVISIONAL_NEEDS_REVIEW]"] * total_rows)
                        continue

                    if rule_type == "NO_MAPPING":
                        output_data[field] = pl.Series([None] * total_rows)

                    elif rule_type == "DIRECT":
                        target_col = resolve_column_name(src_file, src_col, default_table_key, src_cols)
                        if src_df is not None and target_col and target_col in src_cols:
                            # Check if src_col specifies arithmetic operation (e.g. 'E / 100')
                            arith_match = re.search(
                                r"^(?:COLUMN\s+)?([A-Za-z]+)\s*([/*\+\-])\s*([0-9\.]+)\s*$",
                                str(src_col or "").strip(),
                                re.IGNORECASE,
                            )
                            if arith_match:
                                col_let, op, num_str = arith_match.groups()
                                val_num = float(num_str)
                                col_expr = pl.col(target_col).cast(pl.Float64, strict=False)
                                if op == "/":
                                    res_expr = col_expr / val_num
                                elif op == "*":
                                    res_expr = col_expr * val_num
                                elif op == "+":
                                    res_expr = col_expr + val_num
                                elif op == "-":
                                    res_expr = col_expr - val_num
                                output_data[field] = src_df.with_columns(res_expr.alias(field))[field]
                            else:
                                output_data[field] = src_df[target_col].head(total_rows)
                        else:
                            output_data[field] = pl.Series([None] * total_rows)

                    elif rule_type == "CONSTANT":
                        val = rule_dsl.value if isinstance(rule_dsl, ConstantRuleDSL) else ""
                        if str(val).strip().upper().startswith("IF "):
                            val_expr = parse_action_target_expr(val, src_df, src_file, default_table_key)
                            output_data[field] = src_df.with_columns(val_expr.alias(field))[field]
                        else:
                            output_data[field] = pl.Series([val] * total_rows)

                    elif rule_type == "CONDITIONAL":
                        if isinstance(rule_dsl, ConditionalRuleDSL):
                            if_col_letter = rule_dsl.if_col
                            if_val = str(rule_dsl.if_val).strip()
                            then_val_str = rule_dsl.then_val
                            else_val_str = rule_dsl.else_val
                        else:
                            if_col_letter, if_val, then_val_str, else_val_str = "", "", "", ""

                        cond_col_name = resolve_column_name(src_file, if_col_letter, default_table_key, src_cols)

                        if src_df is not None and cond_col_name and cond_col_name in src_cols:
                            mask = pl.col(cond_col_name).cast(pl.Utf8).str.strip_chars() == if_val
                            then_expr = parse_action_target_expr(then_val_str, src_df, src_file, default_table_key)
                            else_expr = parse_action_target_expr(else_val_str, src_df, src_file, default_table_key)

                            res_expr = pl.when(mask).then(then_expr).otherwise(else_expr)
                            output_data[field] = src_df.with_columns(res_expr.alias(field))[field]
                        elif src_df is not None and not src_df.is_empty():
                            else_expr = parse_action_target_expr(else_val_str, src_df, src_file, default_table_key)
                            output_data[field] = src_df.with_columns(else_expr.alias(field))[field]
                        else:
                            output_data[field] = pl.Series([None] * total_rows)

                    else:
                        output_data[field] = pl.Series([f"[LOOKUP_PENDING]"] * total_rows)

                # Export CSV for current section
                if output_data:
                    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
                    res_df = pl.DataFrame(output_data)

                    clean_sheet = re.sub(r'[\\/*?:"<>|]', "_", current_sheet).replace(" ", "_")
                    clean_sec = re.sub(r'[\\/*?:"<>|]', "_", sec).replace(" ", "_")
                    out_file = OUTPUT_DIR / f"Expected_{cu_id}_{clean_sheet}_{clean_sec}.csv"
                    res_df.write_csv(out_file)

                    print(f"  ✅ Successfully exported Test Data for [{cu_id}]: {out_file.name} ({res_df.shape[0]} rows)")


if __name__ == "__main__":
    target_cu_id = sys.argv[1].upper() if len(sys.argv) > 1 else None
    execute_transformation(cu_id=target_cu_id, sheet_name=None)