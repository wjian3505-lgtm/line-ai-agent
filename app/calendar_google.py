"""Google Calendar：OAuth 授權、查詢某日行程、新增行程。"""
import json
import logging
import os
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from .config import settings

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar"]
TOKEN_PATH = os.path.join("data", "google_calendar_token.json")
PENDING_PATH = os.path.join("data", "google_oauth_pending.json")
TZ = ZoneInfo(settings.timezone or "Asia/Taipei")


def calendar_configured() -> bool:
    return bool(settings.google_client_id and settings.google_client_secret)


def calendar_authorized() -> bool:
    return os.path.exists(TOKEN_PATH)


def _client_config() -> dict:
    if not calendar_configured():
        raise RuntimeError(
            "尚未設定 Google Calendar OAuth。請在 .env 填入 GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET。"
        )
    # 用 installed 類型較適合本機授權；web 也可，但要自行保存 PKCE code_verifier
    return {
        "web": {
            "client_id": settings.google_client_id.strip(),
            "client_secret": settings.google_client_secret.strip(),
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [settings.google_redirect_uri],
        }
    }


def build_auth_url(state: str = "line-ai-agent") -> str:
    from google_auth_oauthlib.flow import Flow

    os.makedirs("data", exist_ok=True)
    flow = Flow.from_client_config(_client_config(), scopes=SCOPES)
    flow.redirect_uri = settings.google_redirect_uri
    auth_url, flow_state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        state=state,
    )
    # 必須把 code_verifier 存起來，callback 換 token 時要用同一組
    with open(PENDING_PATH, "w", encoding="utf-8") as f:
        json.dump(
            {
                "state": flow_state,
                "code_verifier": flow.code_verifier,
                "redirect_uri": settings.google_redirect_uri,
            },
            f,
        )
    return auth_url


def exchange_code_for_token(code: str) -> str:
    """用授權碼換取 refresh token，存到 data/google_calendar_token.json。"""
    from google_auth_oauthlib.flow import Flow

    os.makedirs("data", exist_ok=True)
    if not os.path.exists(PENDING_PATH):
        raise RuntimeError("找不到授權暫存資料，請重新從 /oauth/google/start 開始。")

    with open(PENDING_PATH, "r", encoding="utf-8") as f:
        pending = json.load(f)

    flow = Flow.from_client_config(_client_config(), scopes=SCOPES)
    flow.redirect_uri = pending.get("redirect_uri") or settings.google_redirect_uri
    flow.code_verifier = pending.get("code_verifier")
    flow.fetch_token(code=code)

    creds = flow.credentials
    with open(TOKEN_PATH, "w", encoding="utf-8") as f:
        f.write(creds.to_json())
    try:
        os.remove(PENDING_PATH)
    except OSError:
        pass
    return "Google 行事曆授權成功！現在可以在 LINE 查詢 / 新增行程了。"


def _reauth_message(reason: str = "Google 行事曆授權已失效") -> str:
    url = f"{settings.public_base_url.rstrip('/')}/oauth/google/start"
    return f"{reason}。請用瀏覽器重新授權：{url}"


def _get_credentials():
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    if not calendar_authorized():
        raise RuntimeError(
            "尚未完成 Google 行事曆授權。請先開瀏覽器完成授權："
            f"{settings.public_base_url.rstrip('/')}/oauth/google/start"
        )
    with open(TOKEN_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    creds = Credentials.from_authorized_user_info(data, SCOPES)
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError as exc:
            # refresh token 被撤銷 / 過期（常見於測試 App 7 天、或改過 client secret）
            logger.warning("Google token refresh 失敗：%s", exc)
            try:
                os.remove(TOKEN_PATH)
            except OSError:
                pass
            raise RuntimeError(_reauth_message("Google 授權已失效（invalid_grant）")) from exc
        with open(TOKEN_PATH, "w", encoding="utf-8") as f:
            f.write(creds.to_json())
    if not creds or not creds.valid:
        raise RuntimeError(_reauth_message())
    return creds


def _service():
    from googleapiclient.discovery import build

    return build("calendar", "v3", credentials=_get_credentials(), cache_discovery=False)


def parse_user_date(value: str, default_year: Optional[int] = None) -> Optional[datetime]:
    """解析使用者日期，支援 8/27、2026-08-27、明天、這週三 等。"""
    if not value:
        return None
    raw = value.strip()
    now = datetime.now(TZ)
    year = default_year or now.year
    today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)

    aliases = {
        "今天": 0,
        "今日": 0,
        "本日": 0,
        "昨天": -1,
        "昨日": -1,
        "前天": -2,
        "明天": 1,
        "明日": 1,
        "後天": 2,
        "大後天": 3,
    }
    if raw in aliases:
        return today0 + timedelta(days=aliases[raw])

    # 這週三 / 下禮拜五 / 週一 / 星期三
    weekday_dt = _parse_weekday_phrase(raw)
    if weekday_dt:
        return weekday_dt

    m = re.fullmatch(r"(\d{1,2})[/-](\d{1,2})", raw)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        try:
            dt = datetime(year, month, day, tzinfo=TZ)
            # 若日期已過超過 30 天，視為明年
            if dt.date() < (now.date() - timedelta(days=30)):
                dt = datetime(year + 1, month, day, tzinfo=TZ)
            return dt.replace(hour=0, minute=0, second=0, microsecond=0)
        except ValueError:
            return None

    m = re.fullmatch(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日?", raw)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        try:
            return datetime(year, month, day, tzinfo=TZ).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
        except ValueError:
            return None

    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M"):
        try:
            dt = datetime.strptime(raw, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=TZ)
            return dt
        except ValueError:
            continue
    return None


_WEEKDAY_CN = {
    "一": 0,
    "二": 1,
    "三": 2,
    "四": 3,
    "五": 4,
    "六": 5,
    "日": 6,
    "天": 6,
}


def _parse_weekday_phrase(raw: str) -> Optional[datetime]:
    """解析 這週三 / 下週五 / 週一 / 星期三。"""
    text = (raw or "").strip()
    m = re.fullmatch(
        r"(這|本|這個|下|下個|上|上個)?\s*(?:個)?\s*(?:週|周|禮拜|星期)\s*([一二三四五六日天])",
        text,
    )
    if not m:
        m = re.fullmatch(r"(週|周|禮拜|星期)\s*([一二三四五六日天])", text)
        if not m:
            return None
        prefix, day_cn = "", m.group(2)
    else:
        prefix, day_cn = m.group(1) or "", m.group(2)

    target = _WEEKDAY_CN.get(day_cn)
    if target is None:
        return None

    today0 = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    this_monday = today0 - timedelta(days=today0.weekday())
    if prefix in ("下", "下個"):
        monday = this_monday + timedelta(weeks=1)
    elif prefix in ("上", "上個"):
        monday = this_monday - timedelta(weeks=1)
    else:
        monday = this_monday
    return monday + timedelta(days=target)


def _week_range(offset_weeks: int = 0) -> Tuple[datetime, datetime, str]:
    today0 = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    start = today0 - timedelta(days=today0.weekday()) + timedelta(weeks=offset_weeks)
    end = start + timedelta(days=7)
    if offset_weeks == 0:
        label = f"本週 {start.strftime('%m/%d')}–{(end - timedelta(days=1)).strftime('%m/%d')}"
    elif offset_weeks == 1:
        label = f"下週 {start.strftime('%m/%d')}–{(end - timedelta(days=1)).strftime('%m/%d')}"
    elif offset_weeks == -1:
        label = f"上週 {start.strftime('%m/%d')}–{(end - timedelta(days=1)).strftime('%m/%d')}"
    else:
        label = f"{start.strftime('%m/%d')}–{(end - timedelta(days=1)).strftime('%m/%d')}"
    return start, end, label


def _weekend_range(offset_weeks: int = 0) -> Tuple[datetime, datetime, str]:
    start, _, _ = _week_range(offset_weeks)
    sat = start + timedelta(days=5)
    end = sat + timedelta(days=2)
    if offset_weeks == 0:
        label = f"本週末 {sat.strftime('%m/%d')}–{(end - timedelta(days=1)).strftime('%m/%d')}"
    elif offset_weeks == 1:
        label = f"下週末 {sat.strftime('%m/%d')}–{(end - timedelta(days=1)).strftime('%m/%d')}"
    else:
        label = f"週末 {sat.strftime('%m/%d')}–{(end - timedelta(days=1)).strftime('%m/%d')}"
    return sat, end, label


def _parse_month_token(token: str) -> Optional[int]:
    """把 8、八月、10 月 等轉成 1–12。"""
    t = (token or "").strip()
    if not t:
        return None
    if t.isdigit():
        m = int(t)
        return m if 1 <= m <= 12 else None
    # 十一、十二要先於單字「十」
    for key in ("十一", "十二"):
        if t.startswith(key):
            return _CN_MONTH[key]
    m = _CN_MONTH.get(t)
    if m:
        return m
    m2 = re.fullmatch(r"(十[一二]|[一二三四五六七八九十]|[1-9]|1[0-2])\s*月?", t)
    if m2:
        raw = m2.group(1)
        if raw.isdigit():
            v = int(raw)
            return v if 1 <= v <= 12 else None
        return _CN_MONTH.get(raw)
    return None


def _month_end(year: int, month: int) -> datetime:
    if month == 12:
        return datetime(year + 1, 1, 1, tzinfo=TZ)
    return datetime(year, month + 1, 1, tzinfo=TZ)


def _resolve_year_token(token: Optional[str], default_year: int) -> int:
    """把 2026／2026年／明年／今年／去年 轉成西元年。"""
    if not token:
        return default_year
    t = token.strip().replace(" ", "")
    now_y = datetime.now(TZ).year
    if t in ("明年", "下一年"):
        return now_y + 1
    if t in ("去年", "前一年"):
        return now_y - 1
    if t in ("今年", "本年", "當年"):
        return now_y
    t = t.replace("年", "")
    if t.isdigit() and len(t) == 4:
        return int(t)
    return default_year


def parse_user_month_range(value: str) -> Optional[Tuple[Tuple[int, int], Tuple[int, int]]]:
    """解析任意月份區間，回傳 ((y1,m1), (y2,m2))。

    規則：
    - 1-10月、2-3月、8到10月、從三月到五月 → 同年
    - 11-2月、11月到2月（結束月 < 開始月且未寫年）→ 跨到明年
    - 明確年份／明年／去年 → 照字面
    - 2025年11月到2026年2月 → 照字面
    """
    raw = (value or "").strip()
    if not raw:
        return None
    now = datetime.now(TZ)
    month_atom = r"(十[一二]|[一二三四五六七八九十]|[1-9]|1[0-2])"
    year_atom = r"(\d{4}\s*年|明年|下一年|今年|本年|當年|去年|前一年)"

    patterns = [
        # 從 2026年8月 到 明年2月 / 從八月到十月
        rf"(?:從|自)?\s*(?:{year_atom})?\s*{month_atom}\s*月(?:份)?\s*"
        rf"(?:到|至|~|～|-|—|–)\s*"
        rf"(?:{year_atom})?\s*{month_atom}\s*月(?:份)?(?:\s*(?:之間|期間|內))?",
        # 1-10月、11到2月（第一個月可省略「月」）
        rf"(?:{year_atom})?\s*{month_atom}\s*月?(?:份)?\s*"
        rf"(?:到|至|~|～|-|—|–)\s*"
        rf"(?:{year_atom})?\s*{month_atom}\s*月(?:份)?(?:\s*(?:之間|期間|內))?",
    ]
    for pat in patterns:
        m = re.search(pat, raw)
        if not m:
            continue
        y1_s, m1_s, y2_s, m2_s = m.group(1), m.group(2), m.group(3), m.group(4)
        m1 = _parse_month_token(m1_s)
        m2 = _parse_month_token(m2_s)
        if not m1 or not m2:
            continue

        has_y1 = bool(y1_s)
        has_y2 = bool(y2_s)

        if has_y1 and has_y2:
            y1 = _resolve_year_token(y1_s, now.year)
            y2 = _resolve_year_token(y2_s, now.year)
            if (y2, m2) < (y1, m1):
                y1, m1, y2, m2 = y2, m2, y1, m1
        elif has_y1 and not has_y2:
            y1 = _resolve_year_token(y1_s, now.year)
            y2 = y1 + 1 if m2 < m1 else y1
        elif not has_y1 and has_y2:
            y2 = _resolve_year_token(y2_s, now.year)
            y1 = y2 - 1 if m2 < m1 else y2
        else:
            # 都沒寫年：結束月 < 開始月 → 跨年
            y1 = now.year
            y2 = y1 + 1 if m2 < m1 else y1
            # 「明年1-3月」整段落在明年
            if re.search(r"(明年|下一年)", raw) and m2 >= m1:
                y1 = now.year + 1
                y2 = y1
            elif re.search(r"(去年|前一年)", raw) and m2 >= m1:
                y1 = now.year - 1
                y2 = y1

        return (y1, m1), (y2, m2)
    return None


def parse_user_date_range(value: str) -> Optional[Tuple[datetime, datetime, str]]:
    """解析日期區間：8/1到10/31、從8月1日到10月10日、8/1-10/31。"""
    raw = (value or "").strip()
    if not raw:
        return None
    m = re.search(
        r"(?:從|自)?\s*"
        r"(\d{1,2}[/-]\d{1,2}|\d{1,2}\s*月\s*\d{1,2}\s*日?|\d{4}[-/]\d{1,2}[-/]\d{1,2})"
        r"\s*(?:到|至|~|～|-|—|–)\s*"
        r"(\d{1,2}[/-]\d{1,2}|\d{1,2}\s*月\s*\d{1,2}\s*日?|\d{4}[-/]\d{1,2}[-/]\d{1,2})",
        raw,
    )
    if not m:
        return None
    d1 = parse_user_date(re.sub(r"\s+", "", m.group(1)))
    d2 = parse_user_date(re.sub(r"\s+", "", m.group(2)))
    if not d1 or not d2:
        return None
    if d2 < d1:
        d1, d2 = d2, d1
    end = d2 + timedelta(days=1)
    label = f"{d1.strftime('%m/%d')}–{d2.strftime('%m/%d')}"
    return d1, end, label


def _relative_months_range(n: int, *, forward: bool) -> Tuple[datetime, datetime, str]:
    today0 = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    y, m = today0.year, today0.month
    if forward:
        start = datetime(y, m, 1, tzinfo=TZ)
        # 含本月共 n 個月
        end_m = m + n
        end_y = y
        while end_m > 12:
            end_m -= 12
            end_y += 1
        end = datetime(end_y, end_m, 1, tzinfo=TZ)
        return start, end, f"未來{n}個月 {start.strftime('%Y/%m')}–{(end - timedelta(days=1)).strftime('%Y/%m')}"
    # 近 n 個月：含本月往回
    end = _month_end(y, m)
    start_m = m - (n - 1)
    start_y = y
    while start_m <= 0:
        start_m += 12
        start_y -= 1
    start = datetime(start_y, start_m, 1, tzinfo=TZ)
    return start, end, f"近{n}個月 {start.strftime('%Y/%m')}–{y}/{m:02d}"


def resolve_calendar_range(period: str) -> Optional[Tuple[datetime, datetime, str]]:
    """從完整句子抽出行事曆查詢期間 → (start, end, label)。"""
    raw = (period or "").strip()
    if not raw:
        return None
    today0 = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    now = datetime.now(TZ)

    # —— 週末（須在「這週」之前）——
    if re.search(r"(這週末|本週末|這個週末|這禮拜六日|本周末)", raw):
        return _weekend_range(0)
    if re.search(r"(下週末|下個週末|下周末)", raw):
        return _weekend_range(1)
    if re.search(r"(上週末|上個週末|上周末)", raw):
        return _weekend_range(-1)
    if re.search(r"(週末|周末)", raw) and not re.search(r"(上|下|這|本).{0,2}(週末|周末)", raw):
        # 僅「週末」且未帶上下這 → 本週末
        if not re.search(r"(這週|本週|下週|上週|這個禮拜|下禮拜)", raw):
            return _weekend_range(0)

    # —— 特定星期（這週三 / 下週五；須在「這週」整週之前）——
    m_wd = re.search(
        r"((?:這|本|這個|下|下個|上|上個)?\s*(?:個)?\s*(?:週|周|禮拜|星期)\s*[一二三四五六日天])",
        raw,
    )
    if m_wd:
        day = _parse_weekday_phrase(re.sub(r"\s+", "", m_wd.group(1)))
        if day:
            return day, day + timedelta(days=1), day.strftime("%Y/%m/%d (%a)")

    # —— 整週 ——
    if re.search(r"(這週|本週|這個禮拜|這禮拜|本禮拜|這一週|這一周|這個星期|這星期)", raw):
        return _week_range(0)
    if re.search(r"(下週|下周|下個禮拜|下禮拜|下一週|下一周|下個星期|下星期)", raw):
        return _week_range(1)
    if re.search(r"(上週|上周|上個禮拜|上禮拜|上一週|上一周|上個星期|上星期)", raw):
        return _week_range(-1)

    # —— 相對天數區間 ——
    if re.search(r"(未來一週|接下來一週|未來7天|未來七天|之後一週|近一週|最近一週)", raw):
        return today0, today0 + timedelta(days=7), f"未來一週 {today0.strftime('%m/%d')}–{(today0 + timedelta(days=6)).strftime('%m/%d')}"
    if re.search(r"(未來兩週|接下來兩週|未來14天|之後兩週)", raw):
        return today0, today0 + timedelta(days=14), f"未來兩週 {today0.strftime('%m/%d')}–{(today0 + timedelta(days=13)).strftime('%m/%d')}"
    if re.search(r"(最近三天|未來三天|這三天|近三天)", raw):
        return today0, today0 + timedelta(days=3), f"最近三天 {today0.strftime('%m/%d')}–{(today0 + timedelta(days=2)).strftime('%m/%d')}"
    if re.search(r"(最近|近日|接下來幾天|這幾天).*(行程|安排|行事曆)|(行程|安排).*(最近|近日)", raw):
        return today0, today0 + timedelta(days=7), f"最近一週 {today0.strftime('%m/%d')}–{(today0 + timedelta(days=6)).strftime('%m/%d')}"

    # —— 上下半年 ——
    if re.search(r"(下半年|下半年度)", raw):
        y = now.year
        return datetime(y, 7, 1, tzinfo=TZ), datetime(y + 1, 1, 1, tzinfo=TZ), f"{y} 下半年"
    if re.search(r"(上半年|上半年度)", raw):
        y = now.year
        return datetime(y, 1, 1, tzinfo=TZ), datetime(y, 7, 1, tzinfo=TZ), f"{y} 上半年"

    # —— 季度 ——
    qmap = {
        r"(第一季|第1季|Q1|q1)": 1,
        r"(第二季|第2季|Q2|q2)": 2,
        r"(第三季|第3季|Q3|q3)": 3,
        r"(第四季|第4季|Q4|q4)": 4,
    }
    for pat, q in qmap.items():
        if re.search(pat, raw):
            y = now.year
            ym = re.search(r"(\d{4})\s*年", raw)
            if ym:
                y = int(ym.group(1))
            start_m = (q - 1) * 3 + 1
            end_m = start_m + 3
            start = datetime(y, start_m, 1, tzinfo=TZ)
            if end_m > 12:
                end = datetime(y + 1, 1, 1, tzinfo=TZ)
            else:
                end = datetime(y, end_m, 1, tzinfo=TZ)
            return start, end, f"{y} Q{q}"

    # —— 近／未來 N 個月 ——
    m_rel = re.search(
        r"(近|最近|過去|未來|接下來|之後)\s*([兩三四五六七八九十兩\d]{1,3})\s*個?\s*月",
        raw,
    )
    if m_rel:
        n_raw = m_rel.group(2).replace("兩", "二")
        n = _parse_cn_or_digit(n_raw) if not n_raw.isdigit() else int(n_raw)
        if n and 1 <= n <= 24:
            forward = m_rel.group(1) in ("未來", "接下來", "之後")
            return _relative_months_range(n, forward=forward)

    # —— 整年 ——
    if re.search(r"(今年|明年|去年|本年|當年|\d{4}\s*年)", raw) and not re.search(
        r"\d{1,2}\s*月|十[一二]月|[一二三四五六七八九十]月|\d{1,2}[/-]\d{1,2}", raw
    ):
        year = parse_user_year(raw)
        if year:
            return (
                datetime(year, 1, 1, tzinfo=TZ),
                datetime(year + 1, 1, 1, tzinfo=TZ),
                f"{year} 年",
            )

    # —— 日期區間（8/1到10/31；須在單日之前）——
    date_range = parse_user_date_range(raw)
    if date_range:
        return date_range

    # —— 月份區間（須在「8/10 單日」與「單月」之前，避免 8-10月 被當成 8月10日）——
    month_range = parse_user_month_range(raw)
    if month_range:
        (y1, m1), (y2, m2) = month_range
        start = datetime(y1, m1, 1, tzinfo=TZ)
        end = _month_end(y2, m2)
        return start, end, f"{y1}/{m1:02d}–{y2}/{m2:02d}"

    # —— 整月 ——
    if re.search(
        r"(本月|這個月|當月|下個月|下月|上個月|上月|\d{1,2}\s*月|十[一二]月|[一二三四五六七八九十]月|\d{4}[-/]\d{1,2})",
        raw,
    ):
        # 有明確「日」才當單日；單純 8-10月 已在上面處理
        if not re.search(r"\d{1,2}[/-]\d{1,2}\s*日|\d{1,2}\s*月\s*\d{1,2}\s*日", raw):
            # 避開「8/10」這種真的單日（後面會處理）；但「8月」OK
            if not re.search(r"\d{1,2}[/-]\d{1,2}", raw) or re.search(r"\d{1,2}\s*月", raw):
                month = parse_user_month(raw)
                if month:
                    y, m = month
                    start = datetime(y, m, 1, tzinfo=TZ)
                    end = _month_end(y, m)
                    return start, end, f"{y}/{m:02d}"

    # —— 單日關鍵字 / 數字日期 ——
    for key in ("大後天", "後天", "明天", "明日", "昨天", "昨日", "前天", "今天", "今日", "本日"):
        if key in raw:
            day = parse_user_date(key)
            if day:
                return day, day + timedelta(days=1), day.strftime("%Y/%m/%d (%a)")

    m_date = re.search(
        r"(\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[/-]\d{1,2}|\d{1,2}\s*月\s*\d{1,2}\s*日?)",
        raw,
    )
    if m_date:
        token = re.sub(r"\s+", "", m_date.group(1))
        day = parse_user_date(token)
        if day:
            return day, day + timedelta(days=1), day.strftime("%Y/%m/%d (%a)")

    # 純日期字串
    day = parse_user_date(raw)
    if day:
        return day, day + timedelta(days=1), day.strftime("%Y/%m/%d (%a)")

    month = parse_user_month(raw)
    if month:
        y, m = month
        start = datetime(y, m, 1, tzinfo=TZ)
        end = _month_end(y, m)
        return start, end, f"{y}/{m:02d}"

    year = parse_user_year(raw)
    if year:
        return (
            datetime(year, 1, 1, tzinfo=TZ),
            datetime(year + 1, 1, 1, tzinfo=TZ),
            f"{year} 年",
        )

    # 「查行事曆 / 我的行程」這類幾乎沒指定期間 → 預設今天
    # 含其他詞（如寒假、開春）則交回 None，讓 Agent 走 Gemini
    if re.fullmatch(
        r"(幫我)?\s*(查(一下|詢)?|看看?|列出?)?\s*(我的)?\s*(行程|安排|行事曆|日程|行程表)\s*(有哪些|有什麼|呢)?",
        raw,
    ):
        return today0, today0 + timedelta(days=1), today0.strftime("%Y/%m/%d (%a)") + "（預設今天）"

    return None


def list_events_on_date(date_str: str) -> str:
    """列出某一天的行事曆行程。"""
    resolved = resolve_calendar_range(date_str)
    if resolved:
        start, end, label = resolved
        return _list_events_between(start, end, label)

    return (
        f"無法理解日期「{date_str}」。"
        "可說：今天、明天、這週三、8/27、2026-08-27。"
    )


_CN_NUM = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "兩": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}


def _parse_cn_or_digit(text: str) -> Optional[int]:
    text = (text or "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if text == "十":
        return 10
    if text.startswith("十"):
        return 10 + _CN_NUM.get(text[1:], 0)
    if text.endswith("十") and len(text) == 2:
        return _CN_NUM.get(text[0], 0) * 10
    if text in _CN_NUM:
        return _CN_NUM[text]
    if len(text) == 2 and text[0] == "十" and text[1] in _CN_NUM:
        return 10 + _CN_NUM[text[1]]
    return None


def parse_user_datetime(value: str) -> Optional[datetime]:
    """解析含時間的字串，例如 2026-08-27 19:00、8/27 晚上七點。"""
    if not value:
        return None
    raw = value.strip()

    time_hour = None
    time_minute = 0
    tm = re.search(
        r"(早上|上午|中午|下午|晚上|傍晚)?\s*([一二三四五六七八九十兩\d]{1,3})\s*點\s*"
        r"(?:([一二三四五六七八九十\d]{1,3})\s*分?)?",
        raw,
    )
    if tm:
        period = tm.group(1)
        hour_v = _parse_cn_or_digit(tm.group(2))
        minute_v = _parse_cn_or_digit(tm.group(3) or "") if tm.group(3) else 0
        if hour_v is None:
            return None
        time_hour = hour_v
        time_minute = minute_v or 0
        if period in ("下午", "晚上", "傍晚") and time_hour < 12:
            time_hour += 12
        if period == "中午" and time_hour < 12:
            time_hour = 12
        raw_date = raw[: tm.start()].strip(" ，,")
    else:
        raw_date = raw

    hm = re.search(r"(\d{1,2}):(\d{2})", raw)
    if hm and time_hour is None:
        time_hour = int(hm.group(1))
        time_minute = int(hm.group(2))
        raw_date = re.sub(r"\d{1,2}:\d{2}", "", raw_date).strip(" ，,")

    base = parse_user_date(raw_date) if raw_date else datetime.now(TZ).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    if not base:
        for fmt in ("%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M", "%Y-%m-%dT%H:%M"):
            try:
                return datetime.strptime(value.strip(), fmt).replace(tzinfo=TZ)
            except ValueError:
                continue
        return None

    if time_hour is None:
        return base
    return base.replace(hour=time_hour, minute=time_minute, second=0, microsecond=0)


_CN_MONTH = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
    "十一": 11,
    "十二": 12,
}


def parse_user_month(value: str) -> Optional[Tuple[int, int]]:
    """解析月份，回傳 (year, month)。支援 八月、8月、本月、2026-08。"""
    if not value:
        return None
    raw = value.strip()
    now = datetime.now(TZ)

    if re.search(r"(本月|這個月|當月)", raw):
        return now.year, now.month
    if re.search(r"(下個月|下月)", raw):
        y, m = now.year, now.month + 1
        if m == 13:
            y, m = y + 1, 1
        return y, m
    if re.search(r"(上個月|上月)", raw):
        y, m = now.year, now.month - 1
        if m == 0:
            y, m = y - 1, 12
        return y, m

    m = re.fullmatch(r"(\d{4})[-/](\d{1,2})", raw)
    if m:
        return int(m.group(1)), int(m.group(2))

    m = re.fullmatch(r"(\d{1,2})\s*月", raw)
    if m:
        month = int(m.group(1))
        if 1 <= month <= 12:
            return now.year, month

    m = re.fullmatch(r"(十[一二]|[一二三四五六七八九十])\s*月", raw)
    if m:
        month = _CN_MONTH.get(m.group(1))
        if month:
            return now.year, month

    m = re.search(r"(?:(\d{4})\s*年)?\s*(\d{1,2}|十[一二]|[一二三四五六七八九十])\s*月", raw)
    if m:
        year = int(m.group(1)) if m.group(1) else now.year
        month_raw = m.group(2)
        month = int(month_raw) if month_raw.isdigit() else _CN_MONTH.get(month_raw)
        if month and 1 <= month <= 12:
            return year, month
    return None


def parse_user_year(value: str) -> Optional[int]:
    """解析年份。支援 今年、明年、2026、2026年。"""
    if not value:
        return None
    raw = value.strip()
    now = datetime.now(TZ)
    if re.search(r"(今年|本年|當年)", raw):
        return now.year
    if re.search(r"明年", raw):
        return now.year + 1
    if re.search(r"去年", raw):
        return now.year - 1
    m = re.fullmatch(r"(\d{4})\s*年?", raw)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d{4})\s*年", raw)
    if m:
        return int(m.group(1))
    return None


def _list_events_between(start: datetime, end: datetime, label: str) -> str:
    service = _service()
    items: List[dict] = []
    page_token = None
    while True:
        result = (
            service.events()
            .list(
                calendarId=settings.google_calendar_id,
                timeMin=start.isoformat(),
                timeMax=end.isoformat(),
                singleEvents=True,
                orderBy="startTime",
                maxResults=250,
                pageToken=page_token,
            )
            .execute()
        )
        items.extend(result.get("items") or [])
        page_token = result.get("nextPageToken")
        if not page_token:
            break
        if len(items) >= 500:
            break

    if not items:
        return f"📅 {label}\n這段期間行事曆上沒有行程。"

    lines = [f"📅 {label} 的行程（{len(items)} 筆）："]
    # 月/年列表加上日期，方便閱讀
    show_date = (end - start).days > 1
    for i, ev in enumerate(items[:200], 1):
        if show_date:
            lines.append(_format_event_line_with_date(i, ev))
        else:
            lines.append(_format_event_line(i, ev))
    if len(items) > 200:
        lines.append(f"…還有 {len(items) - 200} 筆未全部列出")
    return "\n".join(lines)


def list_events_in_month(month_str: str) -> str:
    parsed = parse_user_month(month_str)
    if not parsed:
        return f"無法理解月份「{month_str}」，請用 八月、8月、本月、2026-08。"
    year, month = parsed
    start = datetime(year, month, 1, tzinfo=TZ)
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=TZ)
    else:
        end = datetime(year, month + 1, 1, tzinfo=TZ)
    return _list_events_between(start, end, f"{year}/{month:02d}")


def list_events_in_year(year_str: str) -> str:
    year = parse_user_year(year_str)
    if not year:
        return f"無法理解年份「{year_str}」，請用 今年、2026、2026年。"
    start = datetime(year, 1, 1, tzinfo=TZ)
    end = datetime(year + 1, 1, 1, tzinfo=TZ)
    return _list_events_between(start, end, f"{year} 年")


def list_events_for_period(period: str) -> str:
    """依使用者說法自動判斷查某天 / 某週 / 某月 / 某年行程。"""
    raw = (period or "").strip()
    if not raw:
        return "請告訴我要查哪一天、哪一週、哪一個月或哪一年的行程。"

    resolved = resolve_calendar_range(raw)
    if resolved:
        start, end, label = resolved
        return _list_events_between(start, end, label)

    return (
        f"無法理解期間「{raw}」。"
        "可說：今天、這週、週末、8/27、八月、本月、"
        "8-10月、八月到十月、從8月到10月、8/1到10/31、"
        "近三個月、上半年、第三季、今年。"
    )


def list_events_rows_for_period(period: str) -> Tuple[str, List[dict]]:
    """供 Flex：回傳 (label, [{time,title,location}, ...])。"""
    raw = (period or "").strip()
    if not raw:
        return "請指定期間", []
    resolved = resolve_calendar_range(raw)
    if not resolved:
        return f"無法理解期間「{raw}」", []
    start, end, label = resolved
    items = _fetch_events(start, end)
    rows: List[dict] = []
    show_date = (end - start).days > 1
    for ev in items[:40]:
        summary = ev.get("summary") or "(無標題)"
        location = ev.get("location") or ""
        st = ev.get("start") or {}
        en = ev.get("end") or {}
        if "dateTime" in st:
            s = datetime.fromisoformat(st["dateTime"]).astimezone(TZ)
            if "dateTime" in en:
                e = datetime.fromisoformat(en["dateTime"]).astimezone(TZ)
                if show_date:
                    time_part = f"{s.strftime('%m/%d %H:%M')}–{e.strftime('%H:%M')}"
                else:
                    time_part = f"{s.strftime('%H:%M')}–{e.strftime('%H:%M')}"
            else:
                time_part = s.strftime("%m/%d %H:%M" if show_date else "%H:%M")
        elif "date" in st:
            time_part = f"{st['date'][5:].replace('-', '/')} 整天" if show_date else "整天"
        else:
            time_part = "時間未定"
        rows.append({"time": time_part, "title": summary, "location": location})
    return label, rows

def _format_event_line_with_date(index: int, ev: dict) -> str:
    summary = ev.get("summary") or "(無標題)"
    start = ev.get("start") or {}
    end = ev.get("end") or {}
    location = ev.get("location") or ""

    if "dateTime" in start:
        s = datetime.fromisoformat(start["dateTime"]).astimezone(TZ)
        if "dateTime" in end:
            e = datetime.fromisoformat(end["dateTime"]).astimezone(TZ)
            time_part = f"{s.strftime('%m/%d %H:%M')}–{e.strftime('%H:%M')}"
        else:
            time_part = s.strftime("%m/%d %H:%M")
    elif "date" in start:
        time_part = f"{start['date'][5:].replace('-', '/')} 整天"
    else:
        time_part = "時間未定"

    loc = f" @ {location}" if location else ""
    return f"{index}. [{time_part}] {summary}{loc}"


def parse_calendar_add_utterance(text: str) -> Optional[Tuple[str, datetime]]:
    """從口語抽出 (標題, 開始時間)。例如「10/10 下午五點 優里演唱會」。"""
    raw = (text or "").strip()
    if not raw:
        return None
    raw = re.sub(
        r"^(幫我)?\s*(加|新增|建立|排)?\s*(到|進)?\s*(Google\s*)?(行事曆|行程|日程)?\s*[：:\s]*",
        "",
        raw,
        flags=re.IGNORECASE,
    ).strip()

    tm = re.search(
        r"(早上|上午|中午|下午|晚上|傍晚)?\s*[一二三四五六七八九十兩\d]{1,3}\s*點"
        r"(?:\s*[一二三四五六七八九十\d]{1,3}\s*分?)?"
        r"|\d{1,2}:\d{2}",
        raw,
    )
    if tm:
        when_part = raw[: tm.end()].strip()
        title = raw[tm.end() :].strip(" ，,。的了")
        start_dt = parse_user_datetime(when_part)
        if start_dt and title and len(title) >= 2:
            return title, start_dt
        return None

    m_date = re.match(
        r"^(\d{1,2}[/-]\d{1,2}|\d{1,2}\s*月\s*\d{1,2}\s*日?|明天|後天|大後天|今天|今日)\s+(.+)$",
        raw,
    )
    if m_date:
        day = parse_user_date(m_date.group(1))
        title = m_date.group(2).strip(" ，,。的了")
        if day and title and len(title) >= 2:
            return title, day
    return None


def _backup_local_schedule(
    session: Any,
    user_id: str,
    title: str,
    start_dt: Optional[datetime],
    location: str = "",
    note: str = "",
) -> None:
    """Google 寫入成功後，同步備份到本機 schedules。"""
    from .models import Schedule

    item = Schedule(
        user_id=user_id,
        title=title,
        start_at=start_dt.replace(tzinfo=None) if start_dt and start_dt.tzinfo else start_dt,
        location=location or None,
        note=note or "google_calendar_backup",
    )
    session.add(item)
    session.flush()
    logger.info("本機行程備份成功 user=%s title=%s start=%s", user_id[:8], title, start_dt)


def add_event_from_utterance(
    text: str,
    *,
    local_session: Any = None,
    local_user_id: str = "",
) -> str:
    """解析口語並新增到 Google 行事曆（可同時備份本機）。"""
    parsed = parse_calendar_add_utterance(text)
    if not parsed:
        return (
            "無法理解要新增的行程。可說：10/10 下午五點 優里演唱會"
            "／幫我加行程 明天晚上七點開會。"
        )
    title, start_dt = parsed
    return add_event(
        title=title,
        start_at=start_dt.strftime("%Y-%m-%d %H:%M"),
        local_session=local_session,
        local_user_id=local_user_id,
    )


def add_event(
    title: str,
    start_at: str,
    end_at: str = "",
    location: str = "",
    description: str = "",
    local_session: Any = None,
    local_user_id: str = "",
) -> str:
    """新增一筆 Google 行事曆行程；成功後可備份到本機。"""
    title = (title or "").strip()
    if not title:
        return "請提供行程標題。"

    start_dt = parse_user_datetime(start_at)
    if not start_dt:
        return f"無法理解開始時間「{start_at}」，例如：2026-08-27 19:00 或 8/27 晚上七點。"

    def _after_ok(msg: str) -> str:
        if local_session is not None and local_user_id:
            try:
                _backup_local_schedule(
                    local_session,
                    local_user_id,
                    title=title,
                    start_dt=start_dt,
                    location=location,
                    note=description,
                )
            except Exception:  # noqa: BLE001
                logger.exception("本機行程備份失敗（Google 已寫入）")
        return msg

    if end_at:
        end_dt = parse_user_datetime(end_at)
        if not end_dt:
            return f"無法理解結束時間「{end_at}」。"
    else:
        # 沒指定結束：預設 2 小時；若只有日期沒時間則當整天
        if start_dt.hour == 0 and start_dt.minute == 0 and ":" not in start_at and "點" not in start_at:
            # all-day event
            service = _service()
            body: Dict[str, Any] = {
                "summary": title,
                "start": {"date": start_dt.date().isoformat()},
                "end": {"date": (start_dt.date() + timedelta(days=1)).isoformat()},
            }
            if location:
                body["location"] = location
            if description:
                body["description"] = description
            created = (
                service.events()
                .insert(calendarId=settings.google_calendar_id, body=body)
                .execute()
            )
            link = created.get("htmlLink", "")
            return _after_ok(
                (
                    f"✅ 已新增整天行程到 Google 行事曆\n"
                    f"標題：{title}\n"
                    f"日期：{start_dt.strftime('%Y/%m/%d')}\n"
                    f"{('地點：' + location + chr(10)) if location else ''}"
                    f"{('連結：' + link) if link else ''}"
                ).strip()
            )
        end_dt = start_dt + timedelta(hours=2)

    if end_dt <= start_dt:
        end_dt = start_dt + timedelta(hours=2)

    service = _service()
    body = {
        "summary": title,
        "start": {"dateTime": start_dt.isoformat(), "timeZone": str(TZ)},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": str(TZ)},
    }
    if location:
        body["location"] = location
    if description:
        body["description"] = description

    created = (
        service.events()
        .insert(calendarId=settings.google_calendar_id, body=body)
        .execute()
    )
    link = created.get("htmlLink", "")
    return _after_ok(
        (
            f"✅ 已新增行程到 Google 行事曆\n"
            f"標題：{title}\n"
            f"時間：{start_dt.strftime('%Y/%m/%d %H:%M')} – {end_dt.strftime('%H:%M')}\n"
            f"{('地點：' + location + chr(10)) if location else ''}"
            f"{('連結：' + link) if link else ''}"
        ).strip()
    )


def _event_start_dt(ev: dict) -> Optional[datetime]:
    start = ev.get("start") or {}
    if "dateTime" in start:
        return datetime.fromisoformat(start["dateTime"]).astimezone(TZ)
    if "date" in start:
        return datetime.fromisoformat(start["date"]).replace(tzinfo=TZ)
    return None


def _fetch_events(start: datetime, end: datetime) -> List[dict]:
    service = _service()
    items: List[dict] = []
    page_token = None
    while True:
        result = (
            service.events()
            .list(
                calendarId=settings.google_calendar_id,
                timeMin=start.isoformat(),
                timeMax=end.isoformat(),
                singleEvents=True,
                orderBy="startTime",
                maxResults=100,
                pageToken=page_token,
            )
            .execute()
        )
        items.extend(result.get("items") or [])
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return items


def _extract_hour_from_text(text: str) -> Optional[int]:
    tm = re.search(
        r"(早上|上午|中午|下午|晚上|傍晚)?\s*([一二三四五六七八九十兩\d]{1,3})\s*點",
        text,
    )
    if tm:
        hour = _parse_cn_or_digit(tm.group(2))
        if hour is None:
            return None
        period = tm.group(1)
        if period in ("下午", "晚上", "傍晚") and hour < 12:
            hour += 12
        if period == "中午" and hour < 12:
            hour = 12
        return hour
    hm = re.search(r"(\d{1,2}):(\d{2})", text)
    if hm:
        return int(hm.group(1))
    return None


def _title_hint_from_delete_text(text: str) -> str:
    """從刪除句抽出可能的標題關鍵字。"""
    t = text
    t = re.sub(
        r"(幫我)?(移除|刪除|刪掉|刪去|取消)\s*(掉)?",
        " ",
        t,
    )
    t = re.sub(r"(到|從|於)?\s*行事曆", " ", t)
    t = re.sub(r"(行程|會議|安排|日程)", " ", t)
    # 去掉日期／時間詞
    t = re.sub(
        r"(今天|今日|明天|明日|後天|昨天|昨日|前天|大後天|"
        r"這週|本週|下週|上週|這禮拜|下禮拜|週末|周末|"
        r"本月|這個月|下個月|"
        r"\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[/-]\d{1,2}|"
        r"(早上|上午|中午|下午|晚上|傍晚)?\s*[一二三四五六七八九十兩\d]{1,3}\s*點(?:\s*[一二三四五六七八九十\d]{1,3}\s*分?)?|"
        r"\d{1,2}:\d{2})",
        " ",
        t,
    )
    t = re.sub(r"\s+", " ", t).strip(" ，,。的了")
    return t


def delete_event(
    title: str = "",
    when: str = "",
    hour: Optional[int] = None,
) -> str:
    """依標題／日期／小時刪除 Google 行事曆行程。"""
    raw_when = (when or title or "").strip()
    resolved = resolve_calendar_range(raw_when) if raw_when else None
    if not resolved:
        # 預設今天
        today0 = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        start, end, label = today0, today0 + timedelta(days=1), today0.strftime("%Y/%m/%d")
    else:
        start, end, label = resolved

    # 若 when 只有日期，title 才是標題
    title_hint = (title or "").strip()
    if title_hint and resolve_calendar_range(title_hint) and not when:
        title_hint = ""
    # 避免把整段刪除句當標題
    if re.search(r"(移除|刪除|刪掉|取消)", title_hint):
        title_hint = _title_hint_from_delete_text(title_hint)

    items = _fetch_events(start, end)
    if not items:
        return f"📅 {label}\n找不到可刪除的行程。"

    matched = items
    if hour is not None:
        matched = [
            ev
            for ev in matched
            if (_event_start_dt(ev) and _event_start_dt(ev).hour == hour)
        ]
    if title_hint:
        matched = [
            ev
            for ev in matched
            if title_hint.lower() in (ev.get("summary") or "").lower()
        ]

    if not matched:
        lines = [f"📅 {label} 找不到符合的行程可刪除。", "當天行程："]
        for i, ev in enumerate(items[:20], 1):
            lines.append(_format_event_line(i, ev))
        return "\n".join(lines)

    if len(matched) > 1:
        lines = [
            f"找到 {len(matched)} 筆可能行程，請再明確一點（例如加上標題或時間）：",
        ]
        for i, ev in enumerate(matched[:15], 1):
            lines.append(_format_event_line_with_date(i, ev) if (end - start).days > 1 else _format_event_line(i, ev))
        return "\n".join(lines)

    ev = matched[0]
    event_id = ev.get("id")
    if not event_id:
        return "找到行程但缺少 event id，無法刪除。"

    service = _service()
    service.events().delete(
        calendarId=settings.google_calendar_id,
        eventId=event_id,
    ).execute()

    summary = ev.get("summary") or "(無標題)"
    st = _event_start_dt(ev)
    when_str = st.strftime("%Y/%m/%d %H:%M") if st else label
    return f"🗑 已從 Google 行事曆移除\n標題：{summary}\n時間：{when_str}"


def delete_event_from_utterance(text: str) -> str:
    """解析「幫我移除明天七點開會」這類句子並刪除。"""
    raw = (text or "").strip()
    if not raw:
        return "請告訴我要移除哪一筆行程，例如：移除明天七點開會。"

    hour = _extract_hour_from_text(raw)
    title_hint = _title_hint_from_delete_text(raw)
    # 期間：用原句給 resolve（含明天／這週）
    return delete_event(title=title_hint, when=raw, hour=hour)


def _format_event_line(index: int, ev: dict) -> str:
    summary = ev.get("summary") or "(無標題)"
    start = ev.get("start") or {}
    end = ev.get("end") or {}
    location = ev.get("location") or ""

    if "dateTime" in start:
        s = datetime.fromisoformat(start["dateTime"]).astimezone(TZ)
        if "dateTime" in end:
            e = datetime.fromisoformat(end["dateTime"]).astimezone(TZ)
            time_part = f"{s.strftime('%H:%M')}–{e.strftime('%H:%M')}"
        else:
            time_part = s.strftime("%H:%M")
    else:
        time_part = "整天"

    loc = f" @ {location}" if location else ""
    return f"{index}. [{time_part}] {summary}{loc}"


def status_message() -> str:
    if not calendar_configured():
        return "尚未設定 GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET。"
    if not calendar_authorized():
        return (
            "Google 行事曆尚未授權。請用瀏覽器打開：\n"
            f"{settings.public_base_url.rstrip('/')}/oauth/google/start"
        )
    return "Google 行事曆已授權，可以使用查詢、新增與移除行程。"
