import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent.parent.resolve()

SITE_URL = os.getenv("SHAREPOINT_SITE_URL")
USERNAME = os.getenv("SHAREPOINT_USERNAME")
PASSWORD = os.getenv("SHAREPOINT_PASSWORD")
# Thư mục lưu file ingest từ SharePoint
LOCAL_INGEST_DIR = os.path.join(BASE_DIR, os.getenv("LOCAL_INGEST_DIR", "workspace/ingest"))

# Thư mục lưu file CSV sau khi convert (Item #4)
LOCAL_CSV_DIR = os.path.join(BASE_DIR, os.getenv("LOCAL_CSV_DIR", "workspace/csv"))

# Thêm thư mục chứa file Mapping
LOCAL_MAPPING_DIR = os.path.join(BASE_DIR, os.getenv("LOCAL_MAPPING_DIR", "workspace/mapping"))

# Thêm thư mục lưu Báo cáo cho QA
LOCAL_REPORT_DIR = os.path.join(BASE_DIR, os.getenv("LOCAL_REPORT_DIR", "workspace/qa_reports"))