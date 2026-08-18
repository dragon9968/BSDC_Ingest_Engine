import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from dotenv import load_dotenv
from pydantic import BaseModel
from google import genai
from google.genai import types

from schemas import ConditionalRuleDSL

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

DB_PATH = BASE_DIR / "workspace" / "rules.db"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")


class LLMParsedRuleOutput(BaseModel):
    if_col: str
    if_val: str
    then_val: str
    else_val: str
    dsl_readable: str  # Human and machine-readable representation e.g. CASE WHEN... or IF...


def get_gemini_client() -> genai.Client:
    """Initialize and return Google GenAI Client using GEMINI_API_KEY."""
    if not GEMINI_API_KEY:
        raise ValueError("❌ GEMINI_API_KEY not found in .env file. Please check your configuration.")
    return genai.Client(api_key=GEMINI_API_KEY)


def build_parsing_prompt(raw_notes: str, target_field: str, data_file: str) -> str:
    return f"""
You are an expert Data Migration Logic Parser.
Analyze the raw mapping note for target field '{target_field}'.

Context:
- Default Data File: "{data_file or 'N/A'}"
- Raw Note: "{raw_notes}"

Instructions:
1. "if_col" MUST BE ONLY THE SINGLE COLUMN LETTER (e.g. "H", "A", "B"). DO NOT include the word "COLUMN".
2. "if_val": Test value (e.g. "2").
3. "then_val": Assigned value if TRUE (e.g. "TRANSFER").
4. "else_val": Remaining condition or fallback value (e.g. "IF COLUMN H = 3 THEN CHECK" or "CHECK").
"""


def parse_unparsed_rules_with_llm(cu_id: str = None):
    """Fetch UNPARSED rules from SQLite DB, process them using Gemini LLM, and update status."""
    if not DB_PATH.exists():
        print(f"❌ Database not found at {DB_PATH}. Please run rule_engine.py first!")
        return

    client = get_gemini_client()

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()

        if cu_id:
            cursor.execute(
                """
                SELECT id, cu_id, sheet_name, section_name, target_field, data_file, raw_notes 
                FROM rule_store 
                WHERE (cu_id = ? OR is_global = 1) AND status = 'NEEDS_REVIEW' AND rule_type = 'UNPARSED'
                AND raw_notes IS NOT NULL AND TRIM(raw_notes) != ''
                """,
                (cu_id,),
            )
        else:
            cursor.execute(
                """
                SELECT id, cu_id, sheet_name, section_name, target_field, data_file, raw_notes 
                FROM rule_store 
                WHERE status = 'NEEDS_REVIEW' AND rule_type = 'UNPARSED'
                AND raw_notes IS NOT NULL AND TRIM(raw_notes) != ''
                """
            )

        unparsed_rows = cursor.fetchall()

        if not unparsed_rows:
            print("🎉 No UNPARSED rules found that need LLM processing!")
            return

        print(f"🚀 Found {len(unparsed_rows)} complex rules needing LLM parsing...")

        for row_id, row_cu, sheet, sec, field, data_file, raw_notes in unparsed_rows:
            print(f"\n🧠 [AI Processing] Field: [{field}] | File: [{data_file}] | Note: '{raw_notes}'")

            prompt = build_parsing_prompt(raw_notes, field, data_file)

            try:
                response = client.models.generate_content(
                    model="gemini-3.5-flash",
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=0.1,
                        response_mime_type="application/json",
                        response_schema=LLMParsedRuleOutput,
                    ),
                )

                llm_out = LLMParsedRuleOutput.model_validate_json(response.text)

                cond_dsl = ConditionalRuleDSL(
                    if_col=llm_out.if_col,
                    if_val=llm_out.if_val,
                    then_val=llm_out.then_val,
                    else_val=llm_out.else_val,
                    raw_condition=raw_notes,
                )

                cursor.execute(
                    """
                    UPDATE rule_store 
                    SET rule_type = 'CONDITIONAL',
                        dsl_json = ?,
                        dsl_readable = ?,
                        status = 'PROVISIONAL_NEEDS_REVIEW',
                        parsed_by = 'LLM_GEMINI'
                    WHERE id = ?
                    """,
                    (cond_dsl.model_dump_json(), llm_out.dsl_readable, row_id),
                )

                print(f"   ✅ [Parsed by Gemini] -> {llm_out.dsl_readable}")

            except Exception as e:
                print(f"   ⚠️ Failed to parse with LLM: {e}")

        conn.commit()
        print("\n✨ LLM Parsing complete! Updated rules to 'PROVISIONAL_NEEDS_REVIEW' for QA inspection.")


if __name__ == "__main__":
    target_cu = sys.argv[1].upper() if len(sys.argv) > 1 else None
    parse_unparsed_rules_with_llm(cu_id=target_cu)