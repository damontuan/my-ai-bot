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
CACHE_DURATION = 0  # ⚡ 0 秒即時模式：每次顧客發問都讀取最新試算表改動！

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
            
            # 💡 智慧辨識：若「回答」本身就是 http 圖片網址
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
                q = str(r.get("顧客未命中
