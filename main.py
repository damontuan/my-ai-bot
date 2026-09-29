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
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage,
    ImageMessage,
    StickerMessage,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent

app = FastAPI()

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")
STORE_CACHE_SECONDS = int(os.getenv("STORE_CACHE_SECONDS", "60"))

_groq = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None
_handlers = {}
_kb_cache = {}
_store_cache = {"at": 0, "stores": {}}
_sessions = {}
SESSION_TURNS = 8
SESSION_SECONDS = 30 * 60

MODELS = [
    "qwen/qwen3.8-27b",
    "llama-3.3-70b-versatile",
    "openai/gpt-oss-120b",
    "llama-3.1-8b-instant",
]


@app.api_route("/", methods=["GET", "HEAD"])
def home():
    stores = list_stores()
    return {
        "status": "online",
        "message": "NOVA.AI 智慧客服系統運行中",
        "stores": [{"id": s["id"], "name": s["name"]} for s in stores],
    }


def get_gspread_client():
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    return gspread.authorize(creds)


def _clean_store(item):
    store_id = str(item.get("id") or item.get("店家代號") or "").strip()
    name = str(item.get("name") or item.get("店名") or store_id).strip()
    sheet = str(item.get("spreadsheet_key") or item.get("試算表代號") or "").strip()
    token = str(item.get("line_token") or item.get("LINE_ACCESS_TOKEN") or "").strip()
    secret = str(item.get("line_secret") or item.get("LINE_CHANNEL_SECRET") or "").strip()
    if not store_id or not sheet or not token or not secret:
        return None
    return {
        "id": store_id,
        "name": name,
        "spreadsheet_key": sheet,
        "line_token": token,
        "line_secret": secret,
    }


def legacy_store():
    token = (os.getenv("LINE_CHANNEL_ACCESS_TOKEN") or "").strip()
    secret = (os.getenv("LINE_CHANNEL_SECRET") or "").strip()
    sheet = (os.getenv("SPREADSHEET_KEY") or "").strip()
    if not (token and secret and sheet):
        return None
    return {
        "id": "default",
        "name": (os.getenv("STORE_NAME") or "極上居酒屋").strip(),
        "spreadsheet_key": sheet,
        "line_token": token,
        "line_secret": secret,
    }


def stores_from_json():
    raw = (os.getenv("STORES_JSON") or "").strip()
    if not raw:
        return []
    data = json.loads(raw)
    if isinstance(data, dict):
        data = data.get("stores", [])
    found = []
    for item in data:
        store = _clean_store(item)
        if store:
            found.append(store)
    return found


def stores_from_sheet():
    sheet_key = (os.getenv("STORES_SHEET_KEY") or "").strip()
    if not sheet_key:
        return []
    gc = get_gspread_client()
    sh = gc.open_by_key(sheet_key)
    try:
        ws = sh.worksheet("店家清單")
    except Exception:
        ws = sh.get_worksheet(0)
    rows = ws.get_all_values()
    found = []
    for row in rows:
        if not row or not str(row[0]).strip():
            continue
        if str(row[0]).strip() in ("店家代號", "id"):
            continue
        store = _clean_store({
            "id": row[0] if len(row) > 0 else "",
            "name": row[1] if len(row) > 1 else "",
            "spreadsheet_key": row[2] if len(row) > 2 else "",
            "line_token": row[3] if len(row) > 3 else "",
            "line_secret": row[4] if len(row) > 4 else "",
        })
        if store:
            found.append(store)
    return found


def load_registry(force=False):
    now = time.time()
    if not force and _store_cache["stores"] and now - _store_cache["at"] < STORE_CACHE_SECONDS:
        return _store_cache["stores"]
    stores = {}
    try:
        for store in stores_from_json():
            stores[store["id"]] = store
    except Exception as exc:
        print("讀取 STORES_JSON 失敗:", repr(exc))
    try:
        for store in stores_from_sheet():
            stores[store["id"]] = store
    except Exception as exc:
        print("讀取店家清單失敗:", repr(exc))
    _store_cache["stores"] = stores
    _store_cache["at"] = now
    return stores


def list_stores():
    stores = []
    current = legacy_store()
    if current:
        stores.append(current)
    for store in load_registry().values():
        stores.append(store)
    return stores


def resolve_store(store_id):
    if not store_id:
        store = legacy_store()
        if not store:
            raise HTTPException(status_code=404, detail="尚未設定預設店家")
        return store
    registry = load_registry()
    store = registry.get(store_id)
    if not store:
        registry = load_registry(force=True)
        store = registry.get(store_id)
    if not store:
        raise HTTPException(status_code=404, detail="找不到店家")
    return store


def get_handler(store):
    secret = store["line_secret"]
    if secret in _handlers:
        return _handlers[secret]
    handler = WebhookHandler(secret)
    store_id = store["id"]

    @handler.add(MessageEvent, message=TextMessageContent)
    def on_message(event):
        current = legacy_store() if store_id == "default" else load_registry().get(store_id)
        if current:
            handle_message(event, current)

    _handlers[secret] = handler
    return handler


def get_dynamic_knowledge_base(spreadsheet_key):
    cached = _kb_cache.get(spreadsheet_key)
    if cached and time.time() - cached["at"] < 30:
        return cached["kb"], cached["images"]
    try:
        gc = get_gspread_client()
        sh = gc.open_by_key(spreadsheet_key)
        try:
            worksheet = sh.worksheet("常見問答 (QA)")
        except Exception:
            try:
                worksheet = sh.worksheet("AI 知識庫")
            except Exception:
                worksheet = sh.get_worksheet(0)

        faq_list = []
        image_map = {}
        all_values = worksheet.get_all_values()
        if len(all_values) > 1:
            for row in all_values[1:]:
                if not row or len(row) < 2:
                    continue
                question = str(row[0]).strip()
                answer = str(row[1]).strip()
                note = str(row[3]).strip() if len(row) > 3 else ""
                image_url = ""
                for cell in row:
                    cell_text = str(cell).strip()
                    if cell_text.startswith("http") and (
                        "i.ibb.co" in cell_text
                        or "imgur" in cell_text
                        or cell_text.lower().endswith((".jpg", ".png", ".jpeg", ".webp"))
                    ):
                        image_url = cell_text
                        break
                if answer.startswith("http") and (
                    "i.ibb.co" in answer
                    or "imgur" in answer
                    or answer.lower().endswith((".jpg", ".png", ".jpeg", ".webp"))
                ):
                    image_url = answer
                    answer = "這是我們最新的價目表，請你參考。"
                if question and answer:
                    suffix = f"（備註：{note}）" if note else ""
                    faq_list.append(f"{question}：{answer} {suffix}")
                    if image_url:
                        image_map[question] = image_url

        try:
            supp_sheet = sh.worksheet("待補充問題")
            supp_values = supp_sheet.get_all_values()
            if len(supp_values) > 1:
                for row in supp_values[1:]:
                    question = str(row[1]).strip() if len(row) > 1 else ""
                    answer = str(row[4]).strip() if len(row) > 4 else ""
                    if not answer and len(row) > 3:
                        maybe = str(row[3]).strip()
                        if maybe and maybe not in ("待補充", "已補充"):
                            answer = maybe
                    if question and answer:
                        faq_list.append(f"【補充解答】{question}：{answer}")
        except Exception as exc:
            print("無待補充問題頁面或讀取跳過:", exc)

        kb = "\n".join(faq_list)
        _kb_cache[spreadsheet_key] = {"at": time.time(), "kb": kb, "images": image_map}
        print("已載入知識庫:", spreadsheet_key, "圖片數", len(image_map))
        return kb, image_map
    except Exception as exc:
        print("讀取 Google Sheet 失敗:", repr(exc))
        if cached:
            return cached["kb"], cached["images"]
        return "目前讀取不到店家資料，請稍後再問。", {}


def log_unanswered_question(spreadsheet_key, question_text):
    try:
        gc = get_gspread_client()
        sh = gc.open_by_key(spreadsheet_key)
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
            if question_text and existing_q and (question_text in existing_q or existing_q in question_text):
                found_row_idx = idx
                digits = re.findall(r"\d+", str(row.get("被詢問次數", "")))
                current_count = int(digits[0]) + 1 if digits else 2
                break
        if found_row_idx:
            ws.update_cell(found_row_idx, 1, now_str)
            ws.update_cell(found_row_idx, 3, current_count)
        else:
            ws.append_row([now_str, question_text, 1, "待補充", ""])
    except Exception as exc:
        print("記錄未命中問題失敗:", repr(exc))


def session_key(store, user_id):
    return f"{store['spreadsheet_key']}:{user_id or 'unknown'}"


def recent_turns(key):
    now = time.time()
    turns = [item for item in _sessions.get(key, []) if now - item["at"] < SESSION_SECONDS]
    _sessions[key] = turns
    return turns


def remember_turn(key, role, content):
    turns = recent_turns(key)
    turns.append({"role": role, "content": content[:500], "at": time.time()})
    _sessions[key] = turns[-SESSION_TURNS:]


def ask_model(store_name, knowledge, user_msg, history):
    if _groq is None:
        return None
    system_prompt = f"""
你是{store_name}的 AI 智能店長。對客人要像值班店長：聽得懂同一段對話的前後文，會把知識庫裡的資料自己整理成回答。語氣親切、具體，150 字以內。

【店家知識庫，含店長後來補充的答案】
{knowledge}

怎麼回答：
1. 客人問介紹、位置、營業、推薦、怎麼來、怎麼預約時，用知識庫裡的店名、地址、時間、招牌、注意事項組合成完整介紹。題目文字不必一模一樣。
2. 「這個、那個、剛剛那個」要接上一句在問什麼。對話紀錄看得到剛才的菜名、價格或服務。
3. 有相關圖片時，回答後加一句「請參考下方圖片」。
4. 價格、地址、時間、規定只能引用知識庫原文，不能猜想。
5. 知識庫完全沒有這件事時，才在回覆加上 [UNANSWERED]，並請客人稍候。已經能回答一部分時，先回答知道的部分，不要加這個標籤。
6. 標成「【補充解答】」的內容是店長確認過的，優先採用。
"""
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend({"role": item["role"], "content": item["content"]} for item in history)
    messages.append({"role": "user", "content": user_msg})
    for model in MODELS:
        try:
            response = _groq.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=300,
                temperature=0.4,
            )
            print(f"已使用模型 {model}，店家 {store_name}")
            return response.choices[0].message.content
        except Exception as exc:
            print(f"模型 {model} 失敗:", repr(exc))
    return None


def pick_sticker(user_msg):
    text = user_msg.lower()
    if any(word in text for word in ("hi", "hello", "嗨", "你好", "哈囉", "在嗎", "介紹", "歡迎")):
        return "11537", "52002734"
    if any(word in text for word in ("謝謝", "感謝", "掰", "再見")):
        return "11537", "52002735"
    if any(word in text for word in ("價目", "價格", "菜單", "招牌")):
        return "11537", "52002738"
    return "11537", "52002736"


def match_image(user_msg, image_map):
    if not image_map:
        return None
    for question, link in image_map.items():
        clean = question.replace("請問", "").replace("圖片", "").replace("嗎", "").replace("？", "").strip()
        if clean and (clean in user_msg or user_msg in clean):
            return link
    if any(word in user_msg for word in ("價目", "價格", "菜單", "款式")):
        return next(iter(image_map.values()))
    return None


def handle_message(event, store):
    user_msg = event.message.text
    user_id = getattr(event.source, "user_id", None)
    key = session_key(store, user_id)
    history = [{"role": item["role"], "content": item["content"]} for item in recent_turns(key)]
    knowledge, image_map = get_dynamic_knowledge_base(store["spreadsheet_key"])
    reply_text = ask_model(store["name"], knowledge, user_msg, history)
    if reply_text:
        if "</think>" in reply_text:
            reply_text = reply_text.split("</think>")[-1].strip()
        if "[UNANSWERED]" in reply_text:
            reply_text = reply_text.replace("[UNANSWERED]", "").strip()
            log_unanswered_question(store["spreadsheet_key"], user_msg)
    if not reply_text:
        reply_text = "店長目前正在確認，請稍後再問一次，或直接留訊息給我們。"
        log_unanswered_question(store["spreadsheet_key"], user_msg)
    remember_turn(key, "user", user_msg)
    remember_turn(key, "assistant", reply_text)
    package_id, sticker_id = pick_sticker(user_msg)
    image_url = match_image(user_msg, image_map)
    messages = [
        StickerMessage(package_id=package_id, sticker_id=sticker_id),
        TextMessage(text=reply_text),
    ]
    if image_url:
        messages.append(ImageMessage(original_content_url=image_url, preview_image_url=image_url))
    config = Configuration(access_token=store["line_token"])
    with ApiClient(config) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(reply_token=event.reply_token, messages=messages)
        )


async def dispatch(store_id, request, signature):
    body = (await request.body()).decode("utf-8")
    store = resolve_store(store_id)
    handler = get_handler(store)
    try:
        handler.handle(body, signature or "")
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")
    return "OK"


@app.post("/callback")
async def callback_default(request: Request, x_line_signature: str = Header(None)):
    return await dispatch(None, request, x_line_signature)


@app.post("/callback/{store_id}")
async def callback_store(store_id: str, request: Request, x_line_signature: str = Header(None)):
    return await dispatch(store_id, request, x_line_signature)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
