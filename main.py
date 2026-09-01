import os
import json
import time
import re
import uvicorn
from fastapi import FastAPI, Request, Header, HTTPException
from groq import Groq
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import Configuration, ApiClient, MessagingApi, ReplyMessageRequest, TextMessage
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
LAST_FETCH_TIME = 0
CACHE_DURATION = 300

@app.get("/")
def home():
    return {"status": "online", "message": "NOVA.AI 智慧客服系統 (Groq 極速過濾版) 運行中！"}

def get_dynamic_knowledge_base():
    global CACHED_KB, LAST_FETCH_TIME
    current_time = time.time()
    
    if CACHED_KB and (current_time - LAST_FETCH_TIME < CACHE_DURATION):
        return CACHED_KB
        
    try:
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        gc = gspread.authorize(creds)
        
        sh = gc.open_by_key(SPREADSHEET_KEY)
        
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
    
    system_prompt = f"你是極上居酒屋的專屬 AI 智慧客服店長。請根據以下最新店家知識庫資料，用熱情、親切、條理清晰且精練（150字以內）的口氣回應用戶。請直接給出回答，不要輸出任何思考過程：\n\n【最新店家知識庫】\n{live_kb}\n\n若用戶詢問的問題不在知識庫中，請委婉告知會轉由店長親自確認。"
    
    try:
        all_models = [m.id for m in client.models.list().data if "whisper" not in m.id]
    except Exception:
        all_models = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]

    reply_text = None
    for m in all_models:
        try:
            response = client.chat.completions.create(
                model=m,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_msg}
                ],
                max_tokens=300,
                temperature=0.7
            )
            reply_text = response.choices[0].message.content
            print(f"✅ 成功使用 Groq 模型 [{m}] 生成回答！")
            break
        except Exception as e:
            print(f"⚠️ 嘗試模型 [{m}] 失敗:", repr(e))
            continue

    if reply_text:
        # ✂️ 完美過濾：強效移除所有 <think>...</think> 思考過程標籤
        if "<think>" in reply_text and "</think>" in reply_text:
            reply_text = re.sub(r'<think>.*?</think>', '', reply_text, flags=re.DOTALL).strip()
        elif "</think>" in reply_text:
            reply_text = reply_text.split("</think>")[-1].strip()

    if not reply_text:
        reply_text = "店長目前正在忙碌中，請稍後再試或致電給我們！"

    if reply_text:
        with ApiClient(line_config) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=reply_text)]
                )
            )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
