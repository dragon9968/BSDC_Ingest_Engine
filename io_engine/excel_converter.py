import os
from pathlib import Path
import polars as pl
import openpyxl
from zipfile import BadZipFile
from config import settings

class ExcelConverter:
    def __init__(self, output_dir=None):
        self.output_dir = Path(output_dir or settings.LOCAL_CSV_DIR)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def convert_xlsx_to_csv(self, file_path: str) -> list[str]:
        """Convert 1 file Excel (xử lý toàn bộ các Sheet) sang CSV"""
        file_path = Path(file_path)
        if not file_path.exists():
            print(f"❌ File không tồn tại: {file_path}")
            return []

        print(f"📖 Đang đọc file Excel: {file_path.name}...")
        
        try:
            # Mở workbook lấy danh sách tên toàn bộ các Sheet
            wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
            sheet_names = wb.sheetnames
            wb.close()
        except BadZipFile:
            print(f"⚠️ BỎ QUA: File [{file_path.name}] bị lỗi/hỏng hoặc không đúng định dạng XLSX.")
            return []
        except Exception as e:
            print(f"⚠️ BỎ QUA: Không đọc được file [{file_path.name}]: {e}")
            return []

        created_csv_files = []

        for sheet in sheet_names:
            try:
                df = pl.read_excel(file_path, sheet_name=sheet)

                if df.is_empty():
                    print(f"   ⏩ Bỏ qua Sheet rỗng: [{sheet}]")
                    continue
                
                clean_stem = file_path.stem.replace(" ", "_")
                if len(sheet_names) == 1:
                    csv_filename = f"{clean_stem}.csv"
                else:
                    clean_sheet = sheet.strip().replace(" ", "_")
                    csv_filename = f"{clean_stem}_{clean_sheet}.csv"

                csv_path = self.output_dir / csv_filename
                
                df.write_csv(csv_path)
                created_csv_files.append(str(csv_path))
                print(f"   ✅ Convert thành công Sheet [{sheet}] -> {csv_filename}")
            except Exception as e:
                print(f"   ❌ Lỗi convert Sheet [{sheet}]: {e}")

        return created_csv_files

    def convert_all_in_dir(self, input_dir=None) -> list[str]:
        """Tự động convert các file Excel dữ liệu sang CSV (Bỏ qua file Mapping)"""
        import shutil
        input_path = Path(input_dir or settings.LOCAL_INGEST_DIR)

        excel_files = []
        all_csvs = []
        
        for file in input_path.iterdir():
            if file.is_file() and not file.name.startswith("~$"):
                ext = file.suffix.lower()
                
                # 🎯 BƯỚC LỌC MỚI: Nếu tên file chứa chữ "mapping" -> Bỏ qua không convert
                if "mapping" in file.name.lower():
                    print(f"⏩ Bỏ qua file Mapping (không convert sang CSV): {file.name}")
                    continue

                # 1. Nếu là file Excel dữ liệu thô -> Cho vào danh sách Convert
                if ext in ['.xlsx', '.xls', '.xlsb', '.xlsm']:
                    excel_files.append(file)
                
                # 2. Nếu là file CSV có sẵn -> Copy thẳng sang workspace/csv
                elif ext == '.csv':
                    dest_path = self.output_dir / file.name
                    shutil.copy2(file, dest_path)
                    all_csvs.append(str(dest_path))
                    print(f"   ⏩ Đã copy nguyên bản file CSV: {file.name}")

        if not excel_files and not all_csvs:
            print(f"⚠️ Không tìm thấy file Excel dữ liệu nào cần convert trong thư mục: {input_path}")
            return []

        print(f"🔄 Tìm thấy {len(excel_files)} file Excel dữ liệu. Bắt đầu convert...")
        for file in excel_files:
            csvs = self.convert_xlsx_to_csv(str(file))
            all_csvs.extend(csvs)

        return all_csvs