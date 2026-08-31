import os
import uvicorn
from fastapi import FastAPI, Request, Header, HTTPException
from google import genai
from google.genai import types
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import Configuration, ApiClient, MessagingApi, ReplyMessageRequest, TextMessage
from linebot.v3.webhooks import MessageEvent, TextMessageContent

app = FastAPI()

# 讀取環境變數
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")

# 初始化 Google 最新版 GenAI Client
client = genai.Client(api_key=GEMINI_API_KEY)

line_config = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
line_handler = WebhookHandler(LINE_CHANNEL_SECRET)

# 知識庫與人設
SYSTEM_PROMPT = """
你是「極上居酒屋」的專屬 AI 智慧客服店長。
請依據以下店家資料，用熱情、有禮貌的口氣回應用戶：
- 營業時間：週二至週日 18:00 - 01:00（週一公休）
- 地址：台北市信義區忠孝東路 88 號
- 招牌推薦：A5 和牛盛合 ($1680)、特選厚切牛舌 ($280)
- 停車：本店無特約，請停對面地下收費停車場。
"""

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
    
    # 呼叫 Google 最新官方 SDK
    response = client.models.generate_content(
        model="gemini-3.6-flash",
        contents=user_msg,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT
        )
    )
    
    with ApiClient(line_config) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=response.text)]
            )
        )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
