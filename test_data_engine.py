import json
import re
import sqlite3
from pathlib import Path
import polars as pl

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "workspace" / "rules.db"
RAW_DATA_DIR = BASE_DIR / "workspace" / "raw_data"
OUTPUT_DIR = BASE_DIR / "workspace" / "output"


def index_to_col_letter(idx: int) -> str:
    result = ""
    while idx >= 0:
        result = chr(idx % 26 + ord("A")) + result
        idx = idx // 26 - 1
    return result


def load_raw_tables() -> dict:
    RAW_DATA_DIR.mkdir(parents=True, exist_ok=True)
    tables = {}
    encodings_to_try = ["utf-8", "utf-8-sig", "latin1", "cp1252", "iso-8859-1"]

    for file_path in RAW_DATA_DIR.glob("*.csv"):
        table_name = file_path.stem.upper()
        df = None

        for enc in encodings_to_try:
            try:
                df = pl.read_csv(
                    file_path,
                    infer_schema_length=0,
                    ignore_errors=True,
                    encoding=enc,
                )
                print(
                    f"📥 [ITEM #16] Loaded Raw Table [{table_name}] using"
                    f" '{enc}': {df.shape[0]} rows, {df.shape[1]} cols"
                )
                break
            except Exception:
                continue

        if df is not None:
            new_cols = [
                f"{table_name}__COL_{index_to_col_letter(i)}"
                for i in range(df.shape[1])
            ]
            df.columns = new_cols
            tables[table_name] = df

    return tables


def apply_section_filter_and_join(sec_dsl: dict, tables: dict) -> pl.DataFrame:
    filter_cond = sec_dsl.get("filter_condition")
    join_info = sec_dsl.get("join_rule")

    src_file_name = "SAVINGS_ACCOUNTS"
    if join_info and join_info.get("source_file"):
        src_file_name = join_info["source_file"].upper()

    if src_file_name not in tables:
        src_file_name = list(tables.keys())[0]

    src_df = tables[src_file_name]

    # 1. FILTER CẤP BẢNG (Chỉ giữ AccountType = 12 hoặc 1202)
    if filter_cond:
        f_match = re.search(
            r"COLUMN\s+([A-Za-z]+)\s*=\s*(.+)", filter_cond, re.IGNORECASE
        )
        if f_match:
            f_col_letter = f_match.group(1).upper()
            f_vals_raw = f_match.group(2)
            raw_allowed = [
                v.strip()
                for v in re.split(r"\s+OR\s+", f_vals_raw, flags=re.IGNORECASE)
            ]
            
            allowed_vals = set(raw_allowed)
            for v in raw_allowed:
                if v.isdigit():
                    allowed_vals.add(f"{v}.0")
            allowed_vals_list = list(allowed_vals)

            col_key = f"{src_file_name}__COL_{f_col_letter}"
            if col_key in src_df.columns:
                src_df = src_df.filter(
                    pl.col(col_key)
                    .cast(pl.Utf8)
                    .str.strip_chars()
                    .is_in(allowed_vals_list)
                )

    # 2. INNER JOIN VỚI BẢNG CERTIFIED_DEPOSITS (SAVINGS_ACCOUNTS.G = CERTIFIED_DEPOSITS.Q)
    if join_info:
        tgt_file_name = join_info.get("target_file", "").upper()
        if tgt_file_name in tables:
            tgt_df = tables[tgt_file_name]
            src_col_let = join_info.get("source_col", "").upper()
            tgt_col_let = join_info.get("target_col", "").upper()

            src_key = f"{src_file_name}__COL_{src_col_let}"
            tgt_key = f"{tgt_file_name}__COL_{tgt_col_let}"

            if src_key in src_df.columns and tgt_key in tgt_df.columns:
                src_df = src_df.join(
                    tgt_df, left_on=src_key, right_on=tgt_key, how="inner"
                )

    return src_df


def parse_action_target_series(
    target_str: str,
    src_file_name: str,
    joint_df: pl.DataFrame,
    total_rows: int,
) -> pl.Series:
    if total_rows == 0:
        return pl.Series([], dtype=pl.Utf8)

    if not target_str or target_str.upper() in [
        "DO NOT ASSIGN",
        "LEAVE BLANK",
        "BLANK",
        "NONE",
        "NAN",
    ]:
        return pl.Series([None] * total_rows)

    col_match = re.search(r"COLUMN\s+([A-Za-z]+)", target_str, re.IGNORECASE)
    if col_match:
        ref_col_letter = col_match.group(1).upper()
        col_key = f"{src_file_name}__COL_{ref_col_letter}"
        if col_key in joint_df.columns:
            return joint_df[col_key]
        return pl.Series([None] * total_rows)

    clean_val = re.sub(r"^ASSIGN\s+", "", target_str, flags=re.IGNORECASE).strip()
    return pl.Series([clean_val] * total_rows)


def execute_transformation(cu_id: str = "MEDICOOP", sheet_name: str = "Shares"):
    tables = load_raw_tables()
    if not tables:
        print("❌ Không tìm thấy file Raw Data CSV nào trong workspace/raw_data/!")
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

    if not sections:
        print("⚠️ Không tìm thấy Rule nào trong DB! Hãy chạy python rule_engine.py trước.")
        conn.close()
        return

    for sec in sections:
        print(f"\n⚡ Đang thực thi chuyển đổi tạo Test Data cho Khối: [{sec}]...")

        cursor.execute(
            """
            SELECT dsl_json FROM rule_store 
            WHERE (cu_id = ? OR is_global = 1) AND sheet_name = ? AND TRIM(section_name) = TRIM(?) AND target_field = '_SECTION_RULE_'
        """,
            (cu_id, sheet_name, sec),
        )

        sec_rule_row = cursor.fetchone()

        if sec_rule_row:
            sec_dsl = json.loads(sec_rule_row[0])
            print(f"   🔍 Tìm thấy Luật Cấp Bảng -> Chạy Filter & Join...")
            base_df = apply_section_filter_and_join(sec_dsl, tables)
        else:
            print(f"   ℹ️ Khối này không có Luật Cấp Bảng -> Lấy toàn bộ bảng chính...")
            base_df = tables.get("SAVINGS_ACCOUNTS", list(tables.values())[0])

        total_rows = base_df.shape[0]
        output_data = {}

        cursor.execute(
            """
            SELECT target_field, data_file, column_letter, rule_type, dsl_json, dsl_readable, status 
            FROM rule_store 
            WHERE (cu_id = ? OR is_global = 1) AND sheet_name = ? AND TRIM(section_name) = TRIM(?) AND target_field != '_SECTION_RULE_'
            ORDER BY id ASC
        """,
            (cu_id, sheet_name, sec),
        )

        field_rules = cursor.fetchall()

        for (
            field,
            src_file,
            src_col,
            rule_type,
            dsl_json_str,
            dsl_readable,
            status,
        ) in field_rules:
            dsl = json.loads(dsl_json_str)
            src_file_clean = (
                src_file.upper() if src_file else "SAVINGS_ACCOUNTS"
            )
            src_col_let = src_col.upper() if src_col else ""

            if total_rows == 0:
                output_data[field] = pl.Series([], dtype=pl.Utf8)
                continue

            if status in ["NEEDS_REVIEW", "REJECTED"]:
                output_data[field] = pl.Series(
                    ["[PROVISIONAL_NEEDS_REVIEW]"] * total_rows
                )
                continue

            col_key = f"{src_file_clean}__COL_{src_col_let}"

            # ⚙️ EXECUTE RULE PER TYPE (Lấy trực tiếp từ bảng base_df đã Filter/Join)
            if rule_type == "NO_MAPPING":
                output_data[field] = pl.Series([None] * total_rows)

            elif rule_type == "DIRECT":
                if col_key in base_df.columns:
                    output_data[field] = base_df[col_key]
                else:
                    output_data[field] = pl.Series([None] * total_rows)

            elif rule_type == "CONSTANT":
                val = dsl.get("value", "")
                output_data[field] = pl.Series([val] * total_rows)

            elif rule_type == "CONDITIONAL":
                if_col_letter = dsl.get("if_col", "").upper()
                if_val = str(dsl.get("if_val", "")).strip()
                then_val_str = dsl.get("then_val", "")
                else_val_str = dsl.get("else_val", "")

                cond_col_key = f"{src_file_clean}__COL_{if_col_letter}"

                if cond_col_key in base_df.columns:
                    then_s = parse_action_target_series(
                        then_val_str, src_file_clean, base_df, total_rows
                    )
                    else_s = parse_action_target_series(
                        else_val_str, src_file_clean, base_df, total_rows
                    )
                    mask = (
                        base_df[cond_col_key]
                        .cast(pl.Utf8)
                        .str.strip_chars()
                        == if_val
                    )

                    res_expr = pl.when(mask).then(then_s).otherwise(else_s)
                    output_data[field] = base_df.select(res_expr).to_series()
                else:
                    output_data[field] = parse_action_target_series(
                        else_val_str, src_file_clean, base_df, total_rows
                    )

            elif rule_type in ["MATRIX_LOOKUP", "FIELD_LOOKUP"]:
                output_data[field] = pl.Series(["[LOOKUP_PENDING]"] * total_rows)

            else:
                output_data[field] = pl.Series(["[LOOKUP_PENDING]"] * total_rows)

        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        res_df = pl.DataFrame(output_data)

        clean_sec_filename = (
            sec.replace(" ", "_")
            .replace("(", "")
            .replace(")", "")
            .replace("-", "_")
        )
        out_file = OUTPUT_DIR / f"Expected_{clean_sec_filename}.csv"
        res_df.write_csv(out_file)

        print(
            f"✅ [ITEM #21] Đã xuất thành công Test Data kỳ vọng cho [{sec}]:"
            f" {out_file.name} ({res_df.shape[0]} rows)"
        )

    conn.close()


if __name__ == "__main__":
    execute_transformation(cu_id="MEDICOOP", sheet_name="Shares")