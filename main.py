import os
import json
import time
import uvicorn
from fastapi import FastAPI, Request, Header, HTTPException
from google import genai
from google.genai import types
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import Configuration, ApiClient, MessagingApi, ReplyMessageRequest, TextMessage
from linebot.v3.webhooks import MessageEvent, TextMessageContent

app = FastAPI()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
SPREADSHEET_KEY = os.getenv("SPREADSHEET_KEY")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")

client = genai.Client(api_key=GEMINI_API_KEY)
line_config = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
line_handler = WebhookHandler(LINE_CHANNEL_SECRET)

# 快取機制變數（暫存 5 分鐘，極速回覆）
CACHED_KB = ""
LAST_FETCH_TIME = 0
CACHE_DURATION = 300  # 300 秒 = 5 分鐘

@app.get("/")
def home():
    return {"status": "online", "message": "NOVA.AI 智慧客服系統運行中！"}

def get_dynamic_knowledge_base():
    global CACHED_KB, LAST_FETCH_TIME
    current_time = time.time()
    
    # ⚡ 若距離上次讀取小於 5 分鐘，直接從記憶體返回，零延遲秒回！
    if CACHED_KB and (current_time - LAST_FETCH_TIME < CACHE_DURATION):
        return CACHED_KB
        
    try:
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        gc = gspread.authorize(creds)
        
        sh = gc.open_by_key(SPREADSHEET_KEY)
        
        # 優先讀取「常見問答 (QA)」，若沒有則讀取「AI 知識庫」或第一個分頁
        try:
            worksheet = sh.worksheet("常見問答 (QA)")
        except Exception:
            try:
                worksheet = sh.worksheet("AI 知識庫")
            except Exception:
                worksheet = sh.get_worksheet(0)
                
        records = worksheet.get_all_records()
        
        faq_list = []
        for r in records:
            cat = r.get("分類", "")
            q = r.get("問題", "") or r.get("項目/問題", "") or r.get("項目", "")
            a = r.get("回答", "") or r.get("內容/回答", "") or r.get("內容", "")
            note = r.get("備註", "")
            
            if q and a:
                prefix = f"【{cat}】" if cat else ""
                suffix = f"（備註：{note}）" if note else ""
                faq_list.append(f"{prefix}{q}：{a} {suffix}")
        
        CACHED_KB = "\n".join(faq_list)
        LAST_FETCH_TIME = current_time
        return CACHED_KB
    except Exception as e:
        print("❌ 讀取 Google Sheet 失敗詳情:", repr(e))
        return CACHED_KB if CACHED_KB else "營業時間：週二至週五 18:00 - 01:00，週六至週日 17:30 - 01:00（週一公休）。"

@app.post("/callback")
async def callback(request: Request, x_line_signature: str = Header(None)):
    body = (await request.body()).decode("utf-8")
    try:
        line_handler.handle(body, x_line_signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")
    return "OK"

@line_handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_msg = event.message.text
    
    live_kb = get_dynamic_knowledge_base()
    
    system_prompt = f"""
    你是「極上居酒屋」的專屬 AI 智慧客服店長。
    請根據以下最新店家知識庫資料，用熱情、親切、條理清晰且精練（150字以內）的口氣回應用戶：

    【最新店家知識庫】
    {live_kb}

    若用戶詢問的問題不在知識庫
