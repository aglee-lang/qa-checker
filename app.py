import re
import os
import io
import time
import base64
import requests
import json
import streamlit as st
import gspread
from google.oauth2.service_account import Credentials
from playwright.sync_api import sync_playwright
from PIL import Image

# 自動安裝與準備 Playwright 瀏覽器
os.system("playwright install chromium")

st.set_page_config(page_title="AI 自動 QA 對稿工具", layout="wide")
st.title("🤖 AI 網頁與 Banner 自動 QA 對稿系統")

OPENROUTER_API_KEY = st.secrets.get("OPENROUTER_API_KEY", "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")
MASTER_SHEET_URL = "https://docs.google.com/spreadsheets/d/1oQmf3yeW2KK9bSI8VV8bMpWLC4vXuT0078CLEBa5aIw/edit?gid=0#gid=0"

st.sidebar.header("⚙️ 系統設定")
st.sidebar.success("✅ 系統已順利連線運作")

mode = st.sidebar.radio("選擇對稿模式：", ["📂 批次自動對稿 (預設總控表)", "單一活動對稿"])

def extract_sheet_id(url):
    match = re.search(r'/d/([a-zA-Z0-9-_]+)', url)
    return match.group(1) if match else url

def get_gspread_client():
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    
    if "GCP_CREDENTIALS" in st.secrets:
        try:
            creds_dict = json.loads(st.secrets["GCP_CREDENTIALS"], strict=False)
            if "private_key" in creds_dict:
                pk = creds_dict["private_key"].replace('\\n', '\n')
                if "-----END PRIVATE KEY-----" not in pk:
                    if not pk.endswith('\n'):
                        pk += '\n'
                    pk += "-----END PRIVATE KEY-----\n"
                creds_dict["private_key"] = pk
            creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        except Exception as e:
            raise RuntimeError(f"解析 GCP 金鑰失敗: {e}")
    else:
        creds = Credentials.from_service_account_file("credentials.json", scopes=scopes)
        
    return gspread.authorize(creds)

def fetch_sheet_text(sheet_input):
    client = get_gspread_client()
    sheet_input = str(sheet_input).strip()
    try:
        if "docs.google.com" in sheet_input or "/d/" in sheet_input:
            sheet_id = extract_sheet_id(sheet_input)
            doc = client.open_by_key(sheet_id)
        else:
            doc = client.open(sheet_input)
    except Exception as err:
        raise RuntimeError(f"無法開啟企劃 Excel：{err}")
    
    content_summary = []
    for sheet in doc.worksheets():
        records = sheet.get_all_values()
        clean_rows = []
        # 僅取前 30 列，並剔除完全空白的儲存格與列
        for row in records[:30]:
            row_str = ", ".join([str(cell).strip() for cell in row if str(cell).strip()])
            if row_str:
                clean_rows.append(row_str)
        if clean_rows:
            sheet_text = f"\n--- 分頁: {sheet.title} ---\n" + "\n".join(clean_rows)
            content_summary.append(sheet_text)
    
    full_text = "\n".join(content_summary)
    
    # 限制企劃內文最大長度，防止 Prompt Token 超出 API 限制
    if len(full_text) > 3500:
        full_text = full_text[:3500] + "\n...(企劃內容過長已自動精簡截斷)..."
        
    return doc.title, full_text

def capture_webpage(target_url, output_filename="temp_screenshot.png"):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(target_url)
        page.wait_for_load_state('networkidle')
        page.screenshot(path=output_filename, full_page=True)
        browser.close()
    return output_filename

def compress_image_to_base64(img_path, max_width=1000, quality=75):
    with Image.open(img_path) as img:
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        if img.width > max_width:
            ratio = max_width / float(img.width)
            new_height = int((float(img.height) * float(ratio)))
            img = img.resize((max_width, new_height), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=quality)
        return base64.b64encode(buffer.getvalue()).decode('utf-8')

def run_ai_qa(sheet_context, img_path, lang_hint=""):
    base64_image = compress_image_to_base64(img_path)
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://streamlit.io",
        "X-Title": "QA Checker"
    }
    
    # 穩定且高效的 Vision 模型順序
    candidate_models = [
        "google/gemini-2.0-flash-001",
        "openai/gpt-4o-mini",
        "google/gemini-flash-1.5"
    ]

    prompt = f"""
    你是一名極度嚴苛的資深 QA 測試工程師。
    這是一張前端活動網頁與 Banner 的完整截圖{f'（指定語系：{lang_hint}）' if lang_hint else ''}。

    請比對下方提供之 Excel 企劃規格，進行「字對字」嚴格比對：

    【1. 🖼️ Banner 專屬檢查（必須極度嚴格）】
    - 請先提取圖片 Banner 上顯示的「活動時間」與「時區（如 GMT-3, GMT+8 等）」。
    - 逐字比對 Excel 規範：如果 Banner 上的時區或時間與 Excel 企劃不符（例如 Excel 寫 GMT-3，但 Banner 寫 GMT+8），必須判定為 ❌ 異常！

    【2. 🌐 網頁頁面檢查】
    - 檢查頁面活動規則、榜單獎金金額、名次與單字翻譯是否與 Excel 完全吻合。

    【首行總結判定要求（必須放在最第一行）】
    - 若完全無錯：【判定結果】：✅ 通過
    - 若有任何錯誤：【判定結果】：❌ 異常（必須明確寫出錯處，例如：Banner時區不符、榜單金額錯誤）

    【Excel 企劃規格資料】：
    {sheet_context}
    """

    err_logs = []
    for model_name in candidate_models:
        payload = {
            "model": model_name,
            "max_tokens": 1000,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}
                        }
                    ]
                }
            ]
        }
        try:
            res = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=payload, timeout=45)
            res_data = res.json()
            if "choices" in res_data and len(res_data["choices"]) > 0:
                return res_data["choices"][0]["message"]["content"], model_name
            else:
                msg = res_data.get("error", {}).get("message", str(res_data))
                err_logs.append(f"[{model_name}]: {msg}")
        except Exception as e:
            err_logs.append(f"[{model_name}]: {e}")

    raise RuntimeError(" | ".join(err_logs))

if mode == "📂 批次自動對稿 (預設總控表)":
    st.subheader("📂 批次全自動對稿模式")
    st.info(f"🔗 已自動連接預設總控表：`{MASTER_SHEET_URL}`")
    if st.button("🚀 開始批次全自動對稿", type="primary"):
        try:
            client = get_gspread_client()
            master_doc = client.open_by_key(extract_sheet_id(MASTER_SHEET_URL))
            master_sheet = master_doc.get_worksheet(0)
            rows = master_sheet.get_all_records()
            if rows:
                total_items = len(rows)
                progress_bar = st.progress(0)
                all_reports = []
                os.makedirs("reports", exist_ok=True)
                for index, row in enumerate(rows):
                    campaign_name = row.get("活動名稱", f"活動_{index+1}")
                    sheet_url = row.get("Excel網址", "")
                    web_url = row.get("網頁網址", "")
                    lang = row.get("語系", "")
                    row_number = index + 2
                    st.markdown(f"--- \n### 🔄 正在處理 [{index+1}/{total_items}]：**{campaign_name}** ({lang})")
                    if not sheet_url or not web_url:
                        master_sheet.update_cell(row_number, 6, "❌ 跳過 (資料不完整)")
                        continue
                    try:
                        doc_title, sheet_context = fetch_sheet_text(sheet_url)
                        img_filename = f"temp_{index}.png"
                        capture_webpage(web_url, img_filename)
                        report, model_used = run_ai_qa(sheet_context, img_filename, lang_hint=lang)
                        
                        first_line = report.strip().split('\n')[0]
                        short_summary = first_line.replace("【判定結果】：", "").replace("【判定結果】:", "").strip()
                        master_sheet.update_cell(row_number, 5, True)
                        master_sheet.update_cell(row_number, 6, short_summary if short_summary else "✅ 完成")
                        
                        st.markdown(report)
                        time.sleep(5)
                    except Exception as row_err:
                        st.error(f"❌ 處理失敗：{row_err}")
                    progress_bar.progress((index + 1) / total_items)
        except Exception as e:
            st.error(f"執行失敗：{e}")
