import sys
import argparse
from pathlib import Path
from datetime import datetime
from config import settings
from validators.mapping_validator import MappingValidator

sys.stdout.reconfigure(encoding='utf-8')

def run_preflight_check(specified_file: str = None):
    print("=" * 60)
    print("🛡️ KÍCH HOẠT MAPPING PREFLIGHT VALIDATOR (ACTION #6)")
    print("=" * 60)

    ingest_dir = Path(settings.LOCAL_INGEST_DIR)

    # Lấy danh sách file Mapping cần quét
    if specified_file:
        target_files = [Path(specified_file)]
    else:
        target_files = [f for f in ingest_dir.glob("*.xlsx") if "mapping" in f.name.lower()]

    if not target_files:
        print(f"❌ Không tìm thấy file Excel Mapping nào trong thư mục ingest: {ingest_dir}")
        sys.exit(1)

    print(f"📂 Phát hiện {len(target_files)} file Mapping cần kiểm tra trong thư mục ingest.")
    print("-" * 60)

    all_files_valid = True
    report_data = [] # Lưu trữ thông tin toàn bộ file (cả Hợp lệ lẫn Lỗi)
    total_global_errors = 0

    for file_path in target_files:
        try:
            validator = MappingValidator(str(file_path))
            is_valid, errors_by_sheet = validator.validate()

            file_errors_count = sum(len(err_list) for err_list in errors_by_sheet.values())
            total_global_errors += file_errors_count

            if not is_valid:
                all_files_valid = False
                print(f"  ❌ [{file_path.name}] ➔ Có {file_errors_count} lỗi trên {len(errors_by_sheet)} Sheet!")
            else:
                print(f"  ✅ [{file_path.name}] ➔ HỢP LỆ 100%")

            report_data.append({
                "file_name": file_path.name,
                "is_valid": is_valid,
                "errors_count": file_errors_count,
                "errors_by_sheet": errors_by_sheet
            })

        except Exception as e:
            all_files_valid = False
            print(f"  ❌ [{file_path.name}] ➔ Lỗi đọc file: {e}")
            report_data.append({
                "file_name": file_path.name,
                "is_valid": False,
                "errors_count": 1,
                "errors_by_sheet": {"System": [f"❌ Lỗi hệ thống khi đọc file: {e}"]}
            })

    print("-" * 60)

    # Ghi file Báo cáo tổng hợp cho QA
    report_dir = settings.BASE_DIR / "workspace" / "qa_reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_file = report_dir / f"Mapping_Preflight_Report_{timestamp}.txt"

    with open(report_file, "w", encoding="utf-8") as f:
        f.write("=" * 80 + "\n")
        f.write(f" BÁO CÁO TỔNG HỢP KIỂM TRA FILE MAPPING (PREFLIGHT REPORT)\n")
        f.write(f" Thời gian quét: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f" Số file đã kiểm tra: {len(target_files)} file\n")
        f.write(f" Tổng số lỗi phát hiện: {total_global_errors} lỗi\n")
        f.write("=" * 80 + "\n\n")

        # 1. BẢNG TỔNG QUAN TẤT CẢ FILE
        f.write("📊 TỔNG QUAN TRẠNG THÁI CÁC FILE:\n")
        f.write("-" * 80 + "\n")
        for item in report_data:
            status_str = "✅ HỢP LỆ 100%" if item["is_valid"] else f"❌ CÓ LỖI ({item['errors_count']} lỗi)"
            f.write(f"  • [{item['file_name']}] ➔ {status_str}\n")
        f.write("=" * 80 + "\n\n")

        # 2. CHI TIẾT TỪNG FILE
        f.write("🔍 CHI TIẾT TỪNG FILE MAPPING:\n\n")
        for item in report_data:
            f.write(f"📄 FILE MAPPING: [{item['file_name']}]\n")
            f.write("-" * 80 + "\n")
            
            if item["is_valid"]:
                f.write("  ✅ File hoàn toàn hợp lệ! Không phát hiện lỗi cấu trúc nào.\n\n")
            else:
                for sheet_name, err_list in item["errors_by_sheet"].items():
                    f.write(f"  📑 Sheet: [{sheet_name}] ({len(err_list)} lỗi)\n")
                    f.write("  " + "-" * 76 + "\n")
                    for err in err_list:
                        f.write(f"    ❌ {err}\n")
                    f.write("\n")
            f.write("\n")

    # Đưa ra kết luận và trả về Exit Status Code cho n8n
    if all_files_valid:
        print("✅ PREFLIGHT SUCCESS: Tất cả file Mapping đều hợp lệ! Sẵn sàng cho Ingest Engine.")
        print(f"👉 Báo cáo tổng hợp lưu tại: {report_file}")
        print("=" * 60)
        sys.exit(0)
    else:
        print(f"❌ PREFLIGHT FAILED: Phát hiện tổng cộng {total_global_errors} lỗi trên các file Mapping.")
        print(f"👉 Chi tiết báo cáo tổng hợp đã được xuất ra file:\n   {report_file}")
        print("=" * 60)
        sys.exit(1)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Preflight Validate Mapping Files")
    parser.add_argument("--file", required=False, help="Đường dẫn file mapping cụ thể (nếu muốn check lẻ)")
    args = parser.parse_args()

    run_preflight_check(args.file)