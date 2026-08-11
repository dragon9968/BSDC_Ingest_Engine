import sys
import argparse
import os

sys.stdout.reconfigure(encoding='utf-8')

from config import settings
from io_engine.sharepoint_client import SharePointClient

def clean_sharepoint_path(raw_path: str) -> str:
    """Làm sạch đường dẫn: Xóa dấu xuống dòng, tab và khoảng trắng thừa do copy-paste"""
    if not raw_path:
        return ""
    cleaned = raw_path.replace('\n', '').replace('\r', '').replace('\t', '').strip()
    cleaned = cleaned.strip('"').strip("'")
    parts = [part.strip() for part in cleaned.split('/') if part.strip()]
    return "/".join(parts)

def process_single_path(client: SharePointClient, sp_path: str) -> list[str]:
    """Xử lý tải 1 file hoặc 1 folder cụ thể từ SharePoint"""
    sp_path = clean_sharepoint_path(sp_path)
    if not sp_path:
        return []

    file_name = os.path.basename(sp_path.rstrip('/'))
    if "." in file_name:
        downloaded = client.download_file_by_path(sp_path, settings.LOCAL_INGEST_DIR)
        return [downloaded] if downloaded else []
    else:
        downloaded_list = client.download_folder_by_path(sp_path, settings.LOCAL_INGEST_DIR)
        return downloaded_list or []

def run_ingest(raw_paths: list[str]):
    print("=" * 60)
    print("🚀 KÍCH HOẠT SHAREPOINT INGEST ENGINE - MULTI-PATH (ACTION #3)")
    print("=" * 60)

    all_paths = []
    for item in raw_paths:
        split_items = [clean_sharepoint_path(p) for p in item.replace(',', ';').split(';') if p.strip()]
        all_paths.extend([p for p in split_items if p])

    print(f"📌 Tổng số thư mục/file cần kéo: {len(all_paths)}")
    print(f"📁 Local Target Workspace: {settings.LOCAL_INGEST_DIR}")
    print("-" * 60)

    client = SharePointClient()
    total_downloaded = []

    for idx, p in enumerate(all_paths, 1):
        print(f"⏳ [{idx}/{len(all_paths)}] Đang tải SharePoint Path:\n   👉 '{p}'")
        try:
            downloaded = process_single_path(client, p)
            if downloaded:
                print(f"   ✅ Thành công kéo {len(downloaded)} file.")
                total_downloaded.extend(downloaded)
            else:
                print(f"   ❌ KHÔNG TÌM THẤY FILE NÀO TRONG THƯ MỤC NÀY!")
        except Exception as e:
            print(f"   💥 LỖI TỪ SHAREPOINT API: {e}")

    print("-" * 60)
    print(f"🎉 TỔNG KẾT: Đã tải thành công {len(total_downloaded)} file về workspace/ingest/")
    return total_downloaded

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SharePoint Multi-Path Ingestion")
    parser.add_argument("--path", nargs="+", required=True, help="Một hoặc nhiều đường dẫn SharePoint")
    args = parser.parse_args()

    result = run_ingest(args.path)
    print(f"SUCCESS: {result}")
    sys.exit(0)