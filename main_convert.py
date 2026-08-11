import sys
import argparse
from config import settings
from io_engine.excel_converter import ExcelConverter

# Ép UTF-8 cho Windows Terminal
sys.stdout.reconfigure(encoding='utf-8')

def run_conversion(file_path: str = None):
    print("=" * 60)
    print("⚡ KÍCH HOẠT EXCEL TO CSV CONVERTER ENGINE (ACTION #4)")
    print("=" * 60)

    converter = ExcelConverter()

    if file_path:
        csv_files = converter.convert_xlsx_to_csv(file_path)
    else:
        # Mặc định convert toàn bộ file Excel nằm trong workspace/ingest
        csv_files = converter.convert_all_in_dir()

    print("-" * 60)
    print(f"🎉 TỔNG KẾT: Đã tạo {len(csv_files)} file CSV tại {settings.LOCAL_CSV_DIR}")
    return csv_files

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Convert Excel files to CSV")
    parser.add_argument("--file", required=False, help="Đường dẫn file Excel cụ thể (không bắt buộc)")
    args = parser.parse_args()

    run_conversion(args.file)