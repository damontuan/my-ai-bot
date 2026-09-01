import os
import json
import time
import re
import datetime
import uvicorn
from fastapi import FastAPI, Request, Header, HTTPException
from groq import Groq
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import Configuration, ApiClient, MessagingApi, ReplyMessageRequest, TextMessage, ImageMessage
from linebot.v3.webhooks import MessageEvent, TextMessageContent

app = FastAPI()

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
SPREADSHEET_KEY = os.getenv("SPREADSHEET_KEY")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")

client = Groq(api_key=GROQ_API_KEY)
line_config = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
line_handler = WebhookHandler(LINE_CHANNEL_SECRET)

CACHED_KB = ""
CACHED_IMAGE_MAP = {}
LAST_FETCH_TIME = 0
CACHE_DURATION = 0

@app.get("/")
def home():
    return {"status": "online", "message": "NOVA.AI 智慧客服系統 (0秒即時同步版) 運行中！"}

def get_gspread_client():
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    return gspread.authorize(creds)

def get_dynamic_knowledge_base():
    global CACHED_KB, CACHED_IMAGE_MAP, LAST_FETCH_TIME
    current_time = time.time()
    
    if CACHED_KB and (current_time - LAST_FETCH_TIME < CACHE_DURATION):
        return CACHED_KB, CACHED_IMAGE_MAP
        
    try:
        gc = get_gspread_client()
        sh = gc.open_by_key(SPREADSHEET_KEY)
        
        faq_list = []
        image_map = {}
        
        # 1. 讀取主頁面「常見問答 (QA)」
        try:
            worksheet = sh.worksheet("常見問答 (QA)")
        except Exception:
            try:
                worksheet = sh.worksheet("AI 知識庫")
            except Exception:
                worksheet = sh.get_worksheet(0)
                
        records = worksheet.get_all_records()
        for r in records:
            cat = r.get("分類", "")
            q = str(r.get("問題", "") or r.get("項目/問題", "") or r.get("項目", "")).strip()
            a = str(r.get("回答", "") or r.get("內容/回答", "") or r.get("內容", "")).strip()
            note = r.get("備註", "")
            img_url = str(r.get("圖片網址", "") or r.get("圖片", "")).strip()
            
            if a.startswith("http") and ("i.ibb.co" in a or "imgur" in a or a.endswith((".jpg", ".png", ".jpeg", ".webp"))):
                img_url = a
                a = "這是我們最新的菜單/照片，請您參考！"
            
            if q and a:
                prefix = f"【{cat}】" if cat else ""
                suffix = f"（備註：{note}）" if note else ""
                faq_list.append(f"{prefix}{q}：{a} {suffix}")
                
                if img_url and img_url.startswith("http"):
                    image_map[q] = img_url

        # 2. 讀取第二分頁「待補充問題」
        try:
            supp_sheet = sh.worksheet("待補充問題")
            supp_records = supp_sheet.get_all_records()
            for r in supp_records:
                q = str(r.get("顧客未命中問題", "")).strip()
                a = str(r.get("老闆補充答案", "")).strip()
                if q and a:
                    faq_list.append(f"【補充解答】{q}：{a}")
        except Exception as e:
            print("無待補充問題頁面或讀取跳過:", e)
        
        CACHED_KB = "\n".join(faq_list)
        CACHED_IMAGE_MAP = image_map
        LAST_FETCH_TIME = current_time
        return CACHED_KB, CACHED_IMAGE_MAP
    except Exception as e:
        print("❌ 讀取 Google Sheet 失敗詳情:", repr(e))
        return CACHED_KB if CACHED_KB else ("營業時間：18:00 - 01:00", {})

def log_unanswered_question(question_text: str):
    """智慧去重 ＋ 熱度計數器功能"""
    try:
        gc = get_gspread_client()
        sh = gc.open_by_key(SPREADSHEET_KEY)
        
        try:
            ws = sh.worksheet("待補充問題")
        except Exception:
            ws = sh.add_worksheet(title="待補充問題", rows="100", cols="5")
            ws.append_row(["最後詢問時間", "顧客未命中問題", "被詢問次數", "狀態", "老闆補充答案"])
            
        now_str = (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")
        records = ws.get_all_records()
        
        found_row_idx = None
        current_count = 2
        
        for idx, row in enumerate(records, start=2):
            existing_q = str(row.get("顧客未命中問題", ""))
            if question_text in existing_q or existing_q in question_text or ("寵物" in question_text and "寵物" in existing_q):
                found_row_idx = idx
                raw_count = str(row.get("被詢問次數", ""))
                digits = re.findall(r'\d+', raw_count)
                if digits:
                    current_count = int(digits[0]) + 1
                else:
                    current_count = 2
                break
                
        if found_row_idx:
            ws.update_cell(found_row_idx, 1, now_str)
            ws.update_cell(found_row_idx, 3, current_count)
            print(f"🔥 成功為重複問題 [{question_text}] 增加熱度計數至: {current_count} 次！")
        else:
            ws.append_row([now_str, question_text, 1, "待補充", ""])
            print(f"📝 成功記錄全新未命中問題: {question_text}")
    except Exception as e:
        print("❌ 自動記錄未命中問題失敗:", repr(e))

@
