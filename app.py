import re
import os
import io
import time
import base64
import requests
import json
import urllib.parse
import streamlit as st
import gspread
from google.oauth2.service_account import Credentials
from PIL import Image

st.set_page_config(page_title="AI 自動 QA 對稿工具", layout="wide")
st.title("🤖 AI 網頁與 Banner 自動 QA 對稿系統")

OPENROUTER_API_KEY = st.secrets.get("OPENROUTER_API_KEY", "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")
MASTER_SHEET_URL = "https://docs.google.com/spreadsheets/d/1oQmf3yeW2KK9bSI8VV8bMpWLC4vXuT0078CLEBa5aIw/edit?gid=0#gid=0"

LANG_MAP = {
    "葡文": "pt", "葡萄牙文": "pt", "英文": "en", "英語": "en",
    "簡中": "cn", "簡體中文": "cn", "越文": "vi", "越南文": "vi", "越南語": "vi",
    "泰文": "th", "泰語": "th", "加祿文": "tl", "他加祿語": "tl", "菲律賓語": "tl",
    "印地語": "hi", "印地文": "hi", "印尼文": "id", "印尼語": "id", "西文": "es", "西班牙文": "es"
}

TIMEZONE_RULES = {
    "GMT+8": {"start": "12:00 PM", "end": "11:59 AM"},
    "GMT+7": {"start": "11:00 AM", "end": "10:59 AM"},
    "GMT+6": {"start": "10:00 AM", "end": "09:59 AM"},
    "GMT+5.5": {"start": "09:30 AM", "end": "09:29 AM"},
    "GMT+5": {"start": "09:00 AM", "end": "08:59 AM"},
    "GMT-3": {"start": "01:00 AM", "end": "00:59 AM"}
}

st.sidebar.header("⚙️ 系統設定")
st.sidebar.success("✅ 系統運作正常")

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

def get_smart_column_value(row_dict, target_keywords):
    for key, val in row_dict.items():
        clean_key = str(key).strip().replace(" ", "").lower()
        for kw in target_keywords:
            if kw.lower() in clean_key and str(val).strip():
                return str(val).strip()
    return ""

def fetch_sheet_text_and_languages(sheet_input):
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
    detected_languages = []
    target_timezone = "未指定"
    
    first_sheet = doc.worksheets()[0]
    records_first = first_sheet.get_all_values()
    
    for row_idx, row in enumerate(records_first[:20]):
        row_str = "".join(row)
        if "預設語言" in row_str or "次要語言" in row_str or row_idx == 9:
            if len(row) > 1 and row[1].strip() in LANG_MAP:
                lang_b = row[1].strip()
                if lang_b not in detected_languages:
                    detected_languages.append(lang_b)
            if len(row) > 2 and row[2].strip() in LANG_MAP:
                lang_c = row[2].strip()
                if lang_c not in detected_languages:
                    detected_languages.append(lang_c)
        
        if "活動時差統一" in row_str or row_idx == 10:
            if len(row) > 1 and row[1].strip():
                target_timezone = row[1].strip()

    for sheet in doc.worksheets():
        records = sheet.get_all_values()
        clean_rows = []
        for row in records[:40]:
            row_str = ", ".join([str(cell).strip() for cell in row if str(cell).strip()])
            if row_str:
                clean_rows.append(row_str)
        if clean_rows:
            sheet_text = f"\n--- 分頁: {sheet.title} ---\n" + "\n".join(clean_rows)
            content_summary.append(sheet_text)
            
    return doc.title, "\n".join(content_summary), detected_languages, target_timezone

def build_lang_url(base_url, lang_code):
    if "lang=" in base_url:
        return re.sub(r'lang=[a-zA-Z0-9-]+', f'lang={lang_code}', base_url)
    elif "?" in base_url:
        return f"{base_url}&lang={lang_code}"
    else:
        return f"{base_url}?lang={lang_code}"

def capture_webpage_safe(target_url, output_filename="temp_screenshot.png"):
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    '--no-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-blink-features=AutomationControlled',
                    '--disable-gpu'
                ]
            )
            context = browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                bypass_csp=True,
                ignore_https_errors=True
            )
            page = context.new_page()
            try:
                page.goto(target_url, timeout=15000, wait_until='domcontentloaded')
                page.wait_for_timeout(6000)
                page.mouse.wheel(0, 300)
                page.wait_for_timeout(1000)
            except Exception:
                pass
            page.screenshot(path=output_filename, full_page=True)
            browser.close()
            if os.path.exists(output_filename) and os.path.getsize(output_filename) > 5000:
                return output_filename
    except Exception:
        pass

    try:
        encoded_url = urllib.parse.quote(target_url, safe='')
        api_url = f"https://api.microlink.io/?url={encoded_url}&screenshot=true&meta=false"
        res = requests.get(api_url, timeout=20)
        res_data = res.json()
        if res_data.get("status") == "success":
            img_url = res_data["data"]["screenshot"]["url"]
            img_bytes = requests.get(img_url, timeout=20).content
            with open(output_filename, "wb") as f:
                f.write(img_bytes)
            return output_filename
    except Exception:
        pass

    raise RuntimeError("無法存取目標網頁截圖 (請確認網址是否可正常連線)")

def compress_image_to_base64(img_path, max_width=1000, quality=80):
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

def is_ai_refusal(text):
    refusal_keywords = ["抱歉", "無法進行", "無法協助", "無法提供", "sorry", "cannot assist", "unable to process", "as an ai"]
    return len(text) < 200 and any(kw in text.lower() for kw in refusal_keywords)

def run_ai_qa(sheet_context, img_path, lang_name="", target_timezone="未指定"):
    base64_image = compress_image_to_base64(img_path)
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://streamlit.io",
        "X-Title": "QA Checker"
    }
    
    candidate_models = [
        "google/gemini-2.0-flash-001",
        "google/gemini-2.0-flash-lite-001",
        "google/gemini-flash-1.5",
        "openai/gpt-4o-mini"
    ]

    tz_rule_info = TIMEZONE_RULES.get(target_timezone, {})
    expected_start = tz_rule_info.get("start", "")
    expected_end = tz_rule_info.get("end", "")

    prompt = f"""
    你是一名商業數位行銷內容的專案核對人員。請比對宣傳頁面截圖（目標語系：【{lang_name}】）與企劃規格檔案：

    【🚨 網頁連線狀態優先檢查】：
    - 請先確認圖片是否為 HTTP 錯誤頁面（例如印有 "502 Bad Gateway"、"404 Not Found"、"500 Internal Server Error" 或 nginx 錯誤）。
    - ⚠️ 若圖片為 502/404 等錯誤畫面，請【第一行直接輸出】：
      【判定結果】：❌ 網頁無法存取 (502 Bad Gateway / 目標伺服器未開啟或連線異常)

    【🎯 時間驗證任務（正常網頁時執行）】：
    1. 企劃指定之【活動時差統一】：【{target_timezone}】。
    2. 依據時區規範，Banner 紅色/黃色時間區塊內【正確應顯示的時間】為：開始【{expected_start}】、結束【{expected_end}】。
    3. 🔍 請放大檢視圖片 Banner 底部時間框內的文字：
       - 如果圖片上明確印著【{expected_start}】與【{expected_end}】（例如 "11:00 AM - 10:59 AM"），代表時間完全正確！【必須判定為 ✅ 通過】！
       - 只有當圖片上顯示的時間文字真的不符合【{expected_start} - {expected_end}】時，才判定為 ❌ 異常。

    【🌐 網頁翻譯與規則比對】：
    - 比對活動規則說明、榜單金額與【{lang_name}】標題翻譯是否吻合。

    【首行格式要求（必須放在第一行）】：
    - 若完全無誤：【判定結果】：✅ 通過
    - 若有任何不符或連線失敗：【判定結果】：❌ 異常（簡短指出錯處）

    【企劃規格內容】：
    {sheet_context}
    """

    err_logs = []
    for model_name in candidate_models:
        payload = {
            "model": model_name,
            "max_tokens": 1200,
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
            res = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=payload, timeout=30)
            res_data = res.json()
            if "choices" in res_data and len(res_data["choices"]) > 0:
                answer = res_data["choices"][0]["message"]["content"]
                if is_ai_refusal(answer):
                    err_logs.append(f"[{model_name}]: 觸發過濾，切換模型")
                    continue
                return answer, model_name
            else:
                msg = res_data.get("error", {}).get("message", str(res_data))
                err_logs.append(f"[{model_name}]: {msg}")
        except Exception as e:
            err_logs.append(f"[{model_name}]: {e}")

    raise RuntimeError(" | ".join(err_logs))

# 預設執行批次自動對稿模式
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
            
            for index, row in enumerate(rows):
                campaign_name = get_smart_column_value(row, ["活動名稱", "活動", "名稱", "campaign"]) or f"活動_{index+1}"
                sheet_url = get_smart_column_value(row, ["活動文件網址", "文件網址", "文件", "excel網址", "企劃網址", "試算表"])
                web_url = get_smart_column_value(row, ["活動網頁網址", "網頁網址", "網頁", "測試網址", "連結", "url"])
                row_number = index + 2
                
                with st.status(f"🔄 正在處理 [{index+1}/{total_items}]：**{campaign_name}**", expanded=True) as status:
                    if not sheet_url or not web_url:
                        master_sheet.update_cell(row_number, 5, False)
                        master_sheet.update_cell(row_number, 6, "❌ 跳過 (網址未填寫完全)")
                        status.update(label=f"⚠️ 跳過 [{index+1}/{total_items}]：{campaign_name} (網址未填寫完全)", state="complete")
                        continue
                        
                    try:
                        st.write("📄 **[1/3]** 正在讀取 Google Sheet 企劃書內容與時區規範...")
                        doc_title, sheet_context, target_langs, target_timezone = fetch_sheet_text_and_languages(sheet_url)
                        
                        if not target_langs:
                            target_langs = ["預設語系"]
                        
                        st.write(f"🌐 **[2/3]** 檢測語系：`{', '.join(target_langs)}` | 統一時區：`{target_timezone}`")
                        
                        overall_passed = True
                        summary_list = []
                        
                        for lang_name in target_langs:
                            lang_code = LANG_MAP.get(lang_name, "")
                            target_lang_url = build_lang_url(web_url, lang_code) if lang_code else web_url
                            
                            st.write(f"📸 正在抓取網頁截圖（{lang_name}）：`{target_lang_url}`")
                            img_filename = f"temp_{index}_{lang_name}.png"
                            capture_webpage_safe(target_lang_url, img_filename)
                            
                            st.write(f"🤖 **[3/3]** Vision AI 依據時區【{target_timezone}】進行精準對照驗證...")
                            report, model_used = run_ai_qa(sheet_context, img_filename, lang_name=lang_name, target_timezone=target_timezone)
                            
                            first_line = report.strip().split('\n')[0]
                            if "❌" in first_line or "異常" in first_line or "不符" in first_line:
                                overall_passed = False
                                summary_list.append(f"❌ {lang_name}異常")
                            else:
                                summary_list.append(f"✅ {lang_name}通過")

                            st.markdown(report)
                            if os.path.exists(img_filename):
                                st.image(img_filename, caption=f"📸 網頁截圖 ({lang_name})：{campaign_name}", use_container_width=True)
                            time.sleep(1)

                        final_summary = " | ".join(summary_list)
                        master_sheet.update_cell(row_number, 5, overall_passed)
                        master_sheet.update_cell(row_number, 6, final_summary)
                        
                        status.update(label=f"✅ 完成 [{index+1}/{total_items}]：{campaign_name} ({final_summary})", state="complete")
                        
                    except Exception as row_err:
                        err_msg = str(row_err)
                        st.error(f"❌ 處理失敗：{err_msg}")
                        try:
                            master_sheet.update_cell(row_number, 5, False)
                            master_sheet.update_cell(row_number, 6, f"❌ 失敗：{err_msg[:30]}")
                        except Exception:
                            pass
                        status.update(label=f"❌ 異常 [{index+1}/{total_items}]：{campaign_name}", state="error")
                        
                progress_bar.progress((index + 1) / total_items)
    except Exception as e:
        st.error(f"執行失敗：{e}")
