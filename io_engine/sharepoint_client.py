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
        
        if self.session_files.exists():
            print("🔑 Found saved Session, reusing...")
            context = browser.new_context(storage_state=str(self.session_file))
        else:
            print("🌐 No Session found! Opening browser for SharePoint login...")
            context = browser.new_context()
            page = context.new_page()
            page.goto(settings.SITE_URL)

            print("\n" + "=" * 75)
            print("👉 PLEASE LOG IN AND COMPLETE 2FA AUTHENTICATION ON YOUR PHONE")
            print("⏳ System automatically waiting for your authentication (Max 2 minutes)...")
            print("=" * 75 + "\n")

            try:
                # Playwright actively listens for browser redirection to SharePoint
                page.wait_for_url(lambda url: "sharepoint.com" in url.lower() and "login" not in url.lower(), timeout=120000)
                page.wait_for_timeout(3000)  # Wait 3s for full cookie/session recording
                
                context.storage_state(path=str(self.session_file))
                print("✅ Authentication successful & saved new Session to workspace/.auth/state.json!")
            except Exception as e:
                browser.close()
                raise TimeoutError("❌ Exceeded 2 minutes without completing login/2FA on phone!")

        return browser, context

    def _validate_and_save_download(self, body: bytes, local_file_path: str, file_name: str):
        """Check: If SharePoint returns HTML (expired session), automatically destroy session and report error"""
        if b"<html" in body[:100].lower() or b"<!doctype" in body[:100].lower():
            if self.session_files.exists():
                self.session_files.unlink() # Auto-delete corrupted Session
            raise PermissionError(f"\n❌ ERROR: SESSION EXPIRED! SharePoint refused to serve file [{file_name}] and forced login.\n"
                                  f"💡 System automatically deleted old Session. PLEASE RE-RUN THE COMMAND ON n8n to open the browser again!")
        
        # Save normally if it's a real file
        with open(local_file_path, "wb") as f:
            f.write(body)

    def download_file_by_path(self, sp_file_path: str, local_dir: str) -> str:
        """Download a specific file from SharePoint using raw Playwright API Request Context"""
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

        print(f"📥 Downloading individual file via Playwright API:\n   👉 Path: {server_relative_url}")

        try:
            with sync_playwright() as p:
                # If no Session file exists (deleted due to expiration), trigger creation of new Session
                if not self.session_files.exists():
                    browser, _ = self._get_authenticated_context(p)
                    browser.close()

                context = p.request.new_context(storage_state=str(self.session_file))
                response = context.get(api_url, timeout=0)

                if response.status == 200:
                    os.makedirs(local_dir, exist_ok=True)
                    self._validate_and_save_download(response.body(), local_file_path, file_name)
                    print(f"   ✅ Successfully downloaded individual file: {file_name}")
                    return local_file_path
                else:
                    print(f"   ❌ SHAREPOINT API ERROR ({response.status}): Failed to download files.")
        except Exception as e:
            print(f"   💥 File download error: {e}")

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

        print(f"📂 Scanning file list in directory: [{folder_relative_path}]...")

        with sync_playwright() as p:
            browser, context = self._get_authenticated_context(p)

            response = context.request.get(api_url, headers={"Accept": "application/json;odata=verbose"})

            if response.status != 200:
                browser.close()
                raise FileNotFoundError(f"❌ Cannot read directory (Status {response.status})")

            data = response.json()
            files_list = data.get("d", {}).get("results", [])
            print(f"🎉 Found {len(files_list)} files in directory!")

            for file_info in files_list:
                file_name = file_info.get("Name")
                file_server_url = file_info.get("ServerRelativeUrl")
                local_file_path = os.path.join(target_local_dir, file_name)

                encoded_file_url = urllib.parse.quote(file_server_url, safe='/')
                download_url = f"{settings.SITE_URL}/_layouts/15/download.aspx?SourceUrl={encoded_file_url}"

                print(f"⬇️ Downloading: {file_name}...")
                file_resp = context.request.get(download_url, timeout=0)
                
                if file_resp.status == 200:
                    self._validate_and_save_download(file_resp.body(), local_file_path, file_name)
                    downloaded_files.append(local_file_path)
                    print(f"   ✅ Saved file: {file_name}")
                else:
                    print(f"   ❌ Error downloading file {file_name}: Status {file_resp.status}")

            browser.close()

        return downloaded_files