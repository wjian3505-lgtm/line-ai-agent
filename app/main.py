"""FastAPI 進入點：LINE Webhook + 每日推播端點。"""
from __future__ import annotations

import logging
import os

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    FlexContainer,
    FlexMessage,
    ImageMessage,
    MessagingApi,
    PushMessageRequest,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhook import WebhookParser
from linebot.v3.webhooks import MessageEvent, TextMessageContent

from . import calendar_google
from . import flex_ui
from .agent import AgentReply, handle_user_message
from .config import settings
from .db import init_db, session_scope
from .portfolio import list_holding_user_ids, portfolio_summary
from .stock import get_market_summary

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="LINE AI Agent")

configuration = Configuration(access_token=settings.line_channel_access_token)
parser = WebhookParser(settings.line_channel_secret)

# 圓餅圖等靜態圖檔（LINE ImageMessage 需可公開 HTTPS 存取）
_charts_dir = os.path.join("data", "charts")
os.makedirs(_charts_dir, exist_ok=True)
app.mount("/media/charts", StaticFiles(directory=_charts_dir), name="charts")


@app.on_event("startup")
def _startup() -> None:
    init_db()
    logger.info("資料庫初始化完成，服務啟動。")
    logger.info("PUBLIC_BASE_URL=%s", settings.public_base_url)


@app.get("/")
def health() -> dict:
    return {
        "status": "ok",
        "service": "line-ai-agent",
        "public_base_url": settings.public_base_url,
        "google_calendar": {
            "configured": calendar_google.calendar_configured(),
            "authorized": calendar_google.calendar_authorized(),
        },
    }


@app.get("/oauth/google/start")
def google_oauth_start() -> RedirectResponse:
    """開始 Google 行事曆 OAuth 授權。"""
    try:
        url = calendar_google.build_auth_url()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url)


@app.get("/oauth/google/callback")
def google_oauth_callback(code: str = "", error: str = "") -> HTMLResponse:
    """Google OAuth 回呼：儲存 token。"""
    if error:
        return HTMLResponse(f"<h3>授權失敗：{error}</h3>", status_code=400)
    if not code:
        return HTMLResponse("<h3>缺少授權碼 code</h3>", status_code=400)
    try:
        msg = calendar_google.exchange_code_for_token(code)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Google OAuth 換 token 失敗")
        return HTMLResponse(f"<h3>授權失敗：{exc}</h3>", status_code=400)
    return HTMLResponse(
        f"<h3>{msg}</h3><p>可以關閉此頁，回到 LINE 測試查詢 / 新增行程。</p>"
    )


@app.get("/oauth/google/status")
def google_oauth_status() -> dict:
    return {
        "configured": calendar_google.calendar_configured(),
        "authorized": calendar_google.calendar_authorized(),
        "redirect_uri": settings.google_redirect_uri,
        "message": calendar_google.status_message(),
    }


@app.post("/webhook")
async def line_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_line_signature: str = Header(default=""),
) -> str:
    """驗證簽章後「立即」回 200 給 LINE，實際處理丟到背景執行。

    這樣可避免 LINE 因等待 AI 回應逾時而重送同一事件（造成重複觸發 / 重複計費）。
    """
    body = (await request.body()).decode("utf-8")
    try:
        events = parser.parse(body, x_line_signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    for event in events:
        if isinstance(event, MessageEvent) and isinstance(event.message, TextMessageContent):
            background_tasks.add_task(
                _process_text_message,
                event.source.user_id,
                event.message.text,
                event.reply_token,
            )
    return "OK"


def _build_line_messages(reply: AgentReply) -> list:
    """一律優先送 Flex；失敗才退回純文字。

    注意：contents 必須用 FlexContainer.from_dict()，直接塞 dict
    會被 SDK 收成空 bubble（只有 type），LINE 回 400。
    """
    alt = flex_ui.strip_markdown(reply.text or "")[:400] or "訊息"
    raw = reply.flex or flex_ui.plain_card("助理", reply.text or "（無內容）")
    contents = FlexContainer.from_dict(raw) if isinstance(raw, dict) else raw
    messages: list = [
        FlexMessage(alt_text=alt, contents=contents),
    ]
    for url in (reply.image_urls or [])[:4]:
        messages.append(
            ImageMessage(original_content_url=url, preview_image_url=url)
        )
    return messages


def _process_text_message(user_id: str, user_text: str, reply_token: str) -> None:
    """背景處理：呼叫 AI Agent 並回覆使用者。"""
    logger.info("收到訊息 user_id=%s text=%s", user_id, user_text[:80])
    try:
        reply = handle_user_message(user_id, user_text)
    except Exception:  # noqa: BLE001
        logger.exception("背景處理訊息失敗")
        reply = AgentReply(
            text="抱歉，系統忙碌中，請稍後再試一次。",
            flex=flex_ui.plain_card("系統", "抱歉，系統忙碌中，請稍後再試一次。"),
        )

    try:
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).reply_message(
                ReplyMessageRequest(
                    reply_token=reply_token,
                    messages=_build_line_messages(reply),
                )
            )
    except Exception:  # noqa: BLE001
        logger.exception("Flex 回覆失敗，改送純文字")
        try:
            with ApiClient(configuration) as api_client:
                MessagingApi(api_client).reply_message(
                    ReplyMessageRequest(
                        reply_token=reply_token,
                        messages=[
                            TextMessage(
                                text=flex_ui.strip_markdown(reply.text or "（無內容）")[:4900]
                            )
                        ],
                    )
                )
        except Exception:  # noqa: BLE001
            logger.exception("回覆 LINE 失敗（reply token 可能已過期）")


def _check_task_secret(secret: str) -> None:
    if settings.task_secret and secret != settings.task_secret:
        raise HTTPException(status_code=401, detail="Unauthorized")


def _push_text(user_id: str, text: str) -> None:
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).push_message(
            PushMessageRequest(to=user_id, messages=[TextMessage(text=text)])
        )


@app.post("/tasks/daily-summary")
def daily_summary(secret: str = "", user_id: str = "") -> dict:
    """推播每日台股收盤整理。可由 Zeabur Cron 定時呼叫。

    - secret: 若有設定 TASK_SECRET，需帶入相同值才會執行。
    - user_id: 指定推播對象；未帶入則使用 DAILY_PUSH_USER_ID。
    """
    _check_task_secret(secret)
    target = user_id or settings.daily_push_user_id
    if not target:
        raise HTTPException(status_code=400, detail="未指定推播對象 user_id")

    text = get_market_summary()
    _push_text(target, text)
    return {"status": "sent", "to": target}


@app.post("/tasks/portfolio-morning")
def portfolio_morning(secret: str = "", user_id: str = "") -> dict:
    """開盤後推播持股盈虧與報酬率。

    建議排程：週一至週五 09:10（台灣時間，開盤後）。
    - 有指定 user_id / DAILY_PUSH_USER_ID：只推給該人
    - 都沒指定：推給所有「目前有持股」的使用者
    """
    _check_task_secret(secret)

    with session_scope() as session:
        if user_id or settings.daily_push_user_id:
            targets = [user_id or settings.daily_push_user_id]
        else:
            targets = list_holding_user_ids(session)
        if not targets:
            return {"status": "skipped", "reason": "no targets"}

        sent = []
        for uid in targets:
            text = "☀️ 開盤後持股損益\n\n" + portfolio_summary(session, uid)
            _push_text(uid, text)
            sent.append(uid)
    return {"status": "sent", "to": sent}
