import os
import urllib.parse
from pathlib import Path
from playwright.sync_api import sync_playwright
from config import settings

class SharePointClient:
    def __init__(self):
        self.auth_dir = settings.BASE_DIR / "workspace" / ".auth"
        self.auth_dir.mkdir(parents=True, exist_ok=True)
        self.session_file = self.auth_dir / "state.json"

    def _get_authenticated_context(self, p):
        browser = p.chromium.launch(headless=False)
        
        if self.session_file.exists():
            print("🔑 Tìm thấy Session đã lưu, đang tái sử dụng...")
            context = browser.new_context(storage_state=str(self.session_file))
        else:
            print("🌐 Chưa có Session! Đang mở trình duyệt để anh đăng nhập Sharetec...")
            context = browser.new_context()
            page = context.new_page()
            page.goto(settings.SITE_URL)

            print("\n" + "=" * 75)
            print("👉 ANH VUI LÒNG ĐĂNG NHẬP VÀ XÁC THỰC 2 SỐ TRÊN IPHONE")
            print("⏳ Hệ thống đang tự động chờ anh bấm xác thực (Tối đa 2 phút)...")
            print("=" * 75 + "\n")

            try:
                # Playwright tự ngóng trình duyệt chuyển hướng hẳn về SharePoint (không dùng input nữa)
                page.wait_for_url(lambda url: "sharepoint.com" in url.lower() and "login" not in url.lower(), timeout=120000)
                page.wait_for_timeout(3000)  # Chờ thêm 3s để cookie/session ghi nhận hoàn chỉnh
                
                context.storage_state(path=str(self.session_file))
                print("✅ Đã xác thực thành công & lưu Session mới vào workspace/.auth/state.json!")
            except Exception as e:
                browser.close()
                raise TimeoutError("❌ Quá 2 phút mà chưa hoàn tất đăng nhập/xác thực 2FA trên điện thoại!")

        return browser, context

    def _validate_and_save_download(self, body: bytes, local_file_path: str, file_name: str):
        """Kiểm tra: Nếu SharePoint trả về HTML (hết hạn session) thì tự hủy session và báo lỗi"""
        if b"<html" in body[:100].lower() or b"<!doctype" in body[:100].lower():
            if self.session_file.exists():
                self.session_file.unlink() # Tự động xóa Session hỏng
            raise PermissionError(f"\n❌ LỖI: SESSION ĐÃ HẾT HẠN! SharePoint không nhả file [{file_name}] mà bắt đăng nhập lại.\n"
                                  f"💡 Hệ thống đã tự động xóa Session cũ. Anh hãy CHẠY LẠI LỆNH TRÊN N8N MỘT LẦN NỮA để mở trình duyệt nhé!")
        
        # Nếu là file thật thì lưu bình thường
        with open(local_file_path, "wb") as f:
            f.write(body)

    def download_file_by_path(self, sp_file_path: str, local_dir: str) -> str:
        """Tải 1 file cụ thể từ SharePoint bằng Playwright API Request Context gốc"""
        sp_file_path = sp_file_path.strip().strip('"').strip("'").replace('\\', '/').lstrip('/')
        file_name = os.path.basename(sp_file_path)
        local_file_path = str(Path(local_dir) / file_name)

        if not sp_file_path.startswith('/'):
            server_relative_url = f"/sites/professional_services/{sp_file_path}"
        else:
            server_relative_url = sp_file_path

        encoded_url = urllib.parse.quote(server_relative_url, safe='/')
        base_site = getattr(self, 'site_url', 'https://sharetec.sharepoint.com/sites/professional_services').rstrip('/')
        api_url = f"{base_site}/_api/web/GetFileByServerRelativeUrl('{encoded_url}')/$value"

        print(f"📥 Đang tải file lẻ qua Playwright API:\n   👉 Path: {server_relative_url}")

        try:
            with sync_playwright() as p:
                # Nếu chưa có file Session (do bị xóa vì hết hạn), tự động kích hoạt tạo Session mới
                if not self.session_file.exists():
                    browser, _ = self._get_authenticated_context(p)
                    browser.close()

                context = p.request.new_context(storage_state=str(self.session_file))
                response = context.get(api_url, timeout=0)

                if response.status == 200:
                    os.makedirs(local_dir, exist_ok=True)
                    self._validate_and_save_download(response.body(), local_file_path, file_name)
                    print(f"   ✅ Tải thành công file lẻ: {file_name}")
                    return local_file_path
                else:
                    print(f"   ❌ LỖI SHAREPOINT API ({response.status}): Không tải được file.")
        except Exception as e:
            print(f"   💥 Lỗi tải file: {e}")

        return None
    
    def download_folder_by_path(self, folder_relative_path: str, target_local_dir: str) -> list:
        os.makedirs(target_local_dir, exist_ok=True)
        downloaded_files = []

        parsed_site = urllib.parse.urlparse(settings.SITE_URL)
        site_path = parsed_site.path.rstrip('/')
        clean_rel_path = folder_relative_path.strip('/')
        server_relative_folder_url = f"{site_path}/{clean_rel_path}"
        encoded_folder_url = urllib.parse.quote(server_relative_folder_url, safe='/')

        api_url = f"{settings.SITE_URL}/_api/web/GetFolderByServerRelativeUrl('{encoded_folder_url}')/Files"

        print(f"📂 Đang quét danh sách file trong thư mục: [{folder_relative_path}]...")

        with sync_playwright() as p:
            browser, context = self._get_authenticated_context(p)

            response = context.request.get(api_url, headers={"Accept": "application/json;odata=verbose"})

            if response.status != 200:
                browser.close()
                raise FileNotFoundError(f"❌ Không đọc được thư mục (Status {response.status})")

            data = response.json()
            files_list = data.get("d", {}).get("results", [])
            print(f"🎉 Tìm thấy {len(files_list)} file trong thư mục!")

            for file_info in files_list:
                file_name = file_info.get("Name")
                file_server_url = file_info.get("ServerRelativeUrl")
                local_file_path = os.path.join(target_local_dir, file_name)

                encoded_file_url = urllib.parse.quote(file_server_url, safe='/')
                download_url = f"{settings.SITE_URL}/_layouts/15/download.aspx?SourceUrl={encoded_file_url}"

                print(f"⬇️ Đang tải: {file_name}...")
                file_resp = context.request.get(download_url, timeout=0)
                
                if file_resp.status == 200:
                    self._validate_and_save_download(file_resp.body(), local_file_path, file_name)
                    downloaded_files.append(local_file_path)
                    print(f"   ✅ Đã lưu file: {file_name}")
                else:
                    print(f"   ❌ Lỗi tải file {file_name}: Status {file_resp.status}")

            browser.close()

        return downloaded_files