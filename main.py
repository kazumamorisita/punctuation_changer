import os
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from datetime import date
import uuid
from fastapi import  Response


app = FastAPI()
templates = Jinja2Templates(directory="templates")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8000))
    )

USAGE_LIMIT = 20

usage_store = {}

def get_user_key(request: Request, response: Response):
    ip = request.client.host

    user_id = request.cookies.get("uid")
    if not user_id:
        user_id = str(uuid.uuid4())
        response.set_cookie(
            key="uid",
            value=user_id,
            max_age=60*60*24*365,
            httponly=True,
            samesite="lax"
        )
    
    today = date.today().isoformat()
    return f"{today}:{ip}:{user_id}"

def check_usage_limit(user_key: str):
    count = usage_store.get(user_key, 0)
    if count >= USAGE_LIMIT:
        raise HTTPException(
            status_code=429,
            detail="本日の無料利用回数を超えました"
        )
    usage_store[user_key] = count + 1


# =========================
# HTML
# =========================
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {"request": request}
    )

# =========================
# Request Model
# =========================
class CheckRequest(BaseModel):
    text: str
    mode: str    # check / convert
    style: str   # jp / en / auto
    width: str = "auto" # auto / full / half


class ChangeDetail(BaseModel):
    position: int
    original: str
    converted: str


# =========================
# Canonical roles
# =========================
JP_COMMA  = "jp-comma"
JP_PERIOD = "jp-period"
EN_COMMA_FULL  = "en-comma-full"
EN_COMMA_HALF  = "en-comma-half"
EN_PERIOD_FULL = "en-period-full"
EN_PERIOD_HALF = "en-period-half"

SIGN_MAP = {
    "、": JP_COMMA,
    "。": JP_PERIOD,
    "，": EN_COMMA_FULL,
    ",":  EN_COMMA_HALF,
    "．": EN_PERIOD_FULL,
    ".":  EN_PERIOD_HALF,
}

OUTPUT_MAP = {
    "jp": {
        "full": {
            JP_COMMA: "、",
            JP_PERIOD: "。",
            EN_COMMA_FULL: "、",
            EN_COMMA_HALF: "、",
            EN_PERIOD_FULL: "。",
            EN_PERIOD_HALF: "。",
        },
        "half": {
            JP_COMMA: "､",
            JP_PERIOD: "｡",
            EN_COMMA_FULL: "､",
            EN_COMMA_HALF: "､",
            EN_PERIOD_FULL: "｡",
            EN_PERIOD_HALF: "｡",
        }
    },
    "en": {
        "full": {
            JP_COMMA: "，",
            JP_PERIOD: "．",
            EN_COMMA_FULL: "，",
            EN_COMMA_HALF: "，",
            EN_PERIOD_FULL: "．",
            EN_PERIOD_HALF: "．",
        },
        "half": {
            JP_COMMA: ",",
            JP_PERIOD: ".",
            EN_COMMA_FULL: ",",
            EN_COMMA_HALF: ",",
            EN_PERIOD_FULL: ".",
            EN_PERIOD_HALF: ".",
        }
    }
}

ROLE_TO_STYLE = {
    JP_COMMA: "jp",
    JP_PERIOD: "jp",
    EN_COMMA_FULL: "en",
    EN_COMMA_HALF: "en",
    EN_PERIOD_FULL: "en",
    EN_PERIOD_HALF: "en",
}



# =========================
# Style detection
# =========================
JP_SIGNS = {"、", "。"}
EN_SIGNS = {"，", ",", "．", "."}


def detect_style(text: str) -> str:
    jp_count = 0
    en_count = 0

    for ch in text:
        t = SIGN_MAP.get(ch)
        if t in (JP_COMMA, JP_PERIOD):
            jp_count += 1
        elif t in (EN_COMMA_FULL, EN_COMMA_HALF, EN_PERIOD_FULL, EN_PERIOD_HALF):
            en_count += 1

    if jp_count and en_count:
        return "mixed"
    if jp_count:
        return "jp"
    if en_count:
        return "en"
    return "none"


# =========================
# Message
# =========================
def create_message(sign_type: str) -> str:
    if sign_type == JP_COMMA:
        return "日本語の読点（、）が見つかりました"
    if sign_type == JP_PERIOD:
        return "日本語の句点（。）が見つかりました"
    if sign_type == EN_COMMA_FULL:
        return "英語のコンマ（全角：，）が見つかりました"
    if sign_type == EN_COMMA_HALF:
        return "英語のコンマ（半角：,）が見つかりました"
    if sign_type == EN_PERIOD_FULL:
        return "英語のピリオド（全角：．）が見つかりました"
    if sign_type == EN_PERIOD_HALF:
        return "英語のピリオド（半角：.）が見つかりました"
    return "不明な句読点が見つかりました"


# =========================
# Line processor
# =========================
def process_line(line, line_no, mode, active_style, width, global_start_pos=0):
    new_chars = []
    issues = []
    changes = []

    for i, ch in enumerate(line):
        if ch not in SIGN_MAP:
            new_chars.append(ch)
            continue

        sign_type = SIGN_MAP[ch]
        sign_style = ROLE_TO_STYLE[sign_type]

        issues.append({
            "line": line_no,
            "index": i,
            "type": sign_type,
            "message": create_message(sign_type)
        })

        if mode == "convert" and active_style:
            if width == "auto":
                target_width = "full" if active_style == "jp" else "half"
            else:
                target_width = width

            new_ch = OUTPUT_MAP[active_style][target_width].get(sign_type, ch)
            new_chars.append(new_ch)

            if new_ch != ch:
                changes.append({
                    "line": line_no,
                    "position": global_start_pos + i,
                    "original": ch,
                    "converted": new_ch
                })
        else:
            new_chars.append(ch)

    return "".join(new_chars), issues, changes


# =========================
# API
# =========================
@app.post("/api/punctuation/check")
def check_punctuation(
    req: CheckRequest,
    request: Request,
    response: Response
):
    user_key = get_user_key(request, response)
    check_usage_limit(user_key)

    # ---- validation ----
    if not req.text.strip():
        raise HTTPException(status_code=400, detail="text が空です")

    if len(req.text) > 10000:
        raise HTTPException(status_code=400, detail="text が長すぎます")

    if req.mode not in ("check", "convert"):
        raise HTTPException(status_code=400, detail="mode が不正です")

    if req.style not in ("jp", "en", "auto"):
        raise HTTPException(status_code=400, detail="style が不正です")
    
    if req.width not in ("auto", "full", "half"):
        raise HTTPException(status_code=400, detail="width が不正です")


    detected_style = detect_style(req.text)

    # ---- active style ----
    if req.style == "auto":
        active_style = detected_style if detected_style in ("jp", "en") else None
    else:
        active_style = req.style

    # ---- processing ----
    result_lines = []
    all_issues = []
    all_changes = []
    global_position = 0
    stats = {
        JP_COMMA: 0,
        JP_PERIOD: 0,
        EN_COMMA_FULL: 0,
        EN_COMMA_HALF: 0,
        EN_PERIOD_FULL: 0,
        EN_PERIOD_HALF: 0
    }

    for line_no, line in enumerate(req.text.split("\n"), start=1):
        new_line, issues, line_changes = process_line(
            line=line,
            line_no=line_no,
            mode=req.mode,
            active_style=active_style,
            width=req.width,
            global_start_pos=global_position
        )

        result_lines.append(new_line)
        all_issues.extend(issues)
        all_changes.extend(line_changes)
        
        # 次の行のためにグローバル位置を更新（改行文字+1を考慮）
        global_position += len(line) + 1
        
        # 統計情報を更新
        for issue in issues:
            stats[issue["type"]] += 1

    # 統計情報をわかりやすい形に変換
    readable_stats = []
    if stats[JP_COMMA] > 0:
        readable_stats.append(f"日本語の読点（、）が{stats[JP_COMMA]}個")
    if stats[JP_PERIOD] > 0:
        readable_stats.append(f"日本語の句点（。）が{stats[JP_PERIOD]}個")
    if stats[EN_COMMA_FULL] > 0:
        readable_stats.append(f"英語のコンマ（全角：，）が{stats[EN_COMMA_FULL]}個")
    if stats[EN_COMMA_HALF] > 0:
        readable_stats.append(f"英語のコンマ（半角：,）が{stats[EN_COMMA_HALF]}個")
    if stats[EN_PERIOD_FULL] > 0:
        readable_stats.append(f"英語のピリオド（全角：．）が{stats[EN_PERIOD_FULL]}個")
    if stats[EN_PERIOD_HALF] > 0:
        readable_stats.append(f"英語のピリオド（半角：.）が{stats[EN_PERIOD_HALF]}個")

    return {
        "result_text": "\n".join(result_lines),
        "issues": all_issues,
        "changes": all_changes,
        "statistics": readable_stats,
        "summary": {
            "detected_style": detected_style,
            "applied_style": active_style,
            "total_changes": len(all_changes)
        }
    }
