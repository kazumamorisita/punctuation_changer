import os
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from datetime import date
import uuid
from fastapi import Response
import stripe
from fastapi.responses import JSONResponse
from dotenv import load_dotenv

# 環境変数の読み込み
load_dotenv()

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# Stripe設定
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")
if not stripe.api_key:
    raise ValueError("STRIPE_SECRET_KEY環境変数が設定されていません")

# アプリケーション設定
BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
DEBUG_MODE = os.environ.get("DEBUG", "False").lower() == "true"
# Stripe Webhook用のインポート追加
import json
import hashlib
import hmac

# STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
# の後に以下を追加

# ユーザーのサブスクリプション状態を管理（本番では Database を推奨）
user_subscriptions = {}

def verify_webhook_signature(payload: bytes, sig_header: str, webhook_secret: str) -> bool:
    """Webhook署名を検証"""
    if not webhook_secret:
        return False
    
    try:
        elements = sig_header.split(',')
        signature = None
        timestamp = None
        
        for element in elements:
            key, value = element.split('=')
            if key == 'v1':
                signature = value
            elif key == 't':
                timestamp = value
        
        if not signature or not timestamp:
            return False
        
        # 署名を検証
        expected_sig = hmac.new(
            webhook_secret.encode('utf-8'),
            f"{timestamp}.{payload.decode('utf-8')}".encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        
        return hmac.compare_digest(signature, expected_sig)
    except Exception as e:
        print(f"Webhook signature verification failed: {str(e)}")
        return False

@app.post("/webhook/stripe")
async def stripe_webhook(request: Request):
    """Stripe Webhookエンドポイント"""
    payload = await request.body()
    sig_header = request.headers.get('stripe-signature', '')
    
    # 本番環境では署名検証を有効にしてください
    if STRIPE_WEBHOOK_SECRET and not verify_webhook_signature(payload, sig_header, STRIPE_WEBHOOK_SECRET):
        raise HTTPException(status_code=400, detail="Invalid signature")
    
    try:
        event = json.loads(payload.decode('utf-8'))
        print(f"Received webhook: {event['type']}")
        
        # サブスクリプション作成時
        if event['type'] == 'checkout.session.completed':
            session = event['data']['object']
            user_key = session.get('client_reference_id')
            subscription_id = session.get('subscription')
            
            if user_key and subscription_id:
                user_subscriptions[user_key] = {
                    "subscription_id": subscription_id,
                    "status": "active",
                    "created_at": date.today().isoformat()
                }
                print(f"User {user_key} subscribed with {subscription_id}")
        
        # サブスクリプション更新時
        elif event['type'] == 'customer.subscription.updated':
            subscription = event['data']['object']
            subscription_id = subscription['id']
            status = subscription['status']
            
            # user_keyでの逆引きが必要（本番ではDBで管理推奨）
            for user_key, sub_info in user_subscriptions.items():
                if sub_info.get("subscription_id") == subscription_id:
                    user_subscriptions[user_key]["status"] = status
                    print(f"Updated subscription {subscription_id} status to {status}")
                    break
        
        # サブスクリプション削除時
        elif event['type'] == 'customer.subscription.deleted':
            subscription = event['data']['object']
            subscription_id = subscription['id']
            
            # user_keyでの逆引きが必要
            for user_key, sub_info in user_subscriptions.items():
                if sub_info.get("subscription_id") == subscription_id:
                    user_subscriptions[user_key]["status"] = "cancelled"
                    print(f"Cancelled subscription {subscription_id}")
                    break
        
        return {"status": "success"}
    
    except Exception as e:
        print(f"Webhook processing error: {str(e)}")
        raise HTTPException(status_code=400, detail="Webhook processing failed")

def is_user_premium(user_key: str) -> bool:
    """ユーザーが有料プランかどうかを確認"""
    subscription = user_subscriptions.get(user_key, {})
    return subscription.get("status") == "active"

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
    # 有料ユーザーは制限なし
    if is_user_premium(user_key):
        return
    
    count = usage_store.get(user_key, 0)
    print(f"Debug: Current count for {user_key}: {count}, Limit: {USAGE_LIMIT}")
    if count >= USAGE_LIMIT:
        raise HTTPException(
            status_code=429,
            detail="本日の無料利用回数を超えました"
        )
    usage_store[user_key] = count + 1
    print(f"Debug: Updated count for {user_key}: {usage_store[user_key]}")

def get_usage_info(user_key: str):
    # 有料ユーザーの場合
    if is_user_premium(user_key):
        return {
            "used": 0,
            "remaining": -1,  # -1 = 無制限を示す
            "limit": -1,
            "premium": True
        }
    
    # 無料ユーザーの場合
    count = usage_store.get(user_key, 0)
    remaining = max(0, USAGE_LIMIT - count)
    return {
        "used": count,
        "remaining": remaining,
        "limit": USAGE_LIMIT,
        "premium": False
    }


# =========================
# HTML
# =========================
@app.post("/api/create-checkout-session")
def create_checkout_session(request: Request):
    try:
        # ユーザー識別のため（将来的にサブスクリプション管理で使用）
        user_key = request.cookies.get("uid", str(uuid.uuid4()))
        
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="subscription",
            line_items=[{
                "price_data": {
                    "currency": "jpy",
                    "product_data": {
                        "name": "句読点チェッカー 有料プラン",
                        "description": "回数無制限で句読点チェッカーをご利用いただけます",
                    },
                    "unit_amount": 300,
                    "recurring": {"interval": "month"},
                },
                "quantity": 1,
            }],
            success_url=f"{BASE_URL}/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{BASE_URL}/cancel",
            client_reference_id=user_key,  # ユーザー識別用
            metadata={
                "user_key": user_key,
                "plan": "premium"
            }
        )
        return JSONResponse({"url": session.url})
    except stripe.error.StripeError as e:
        print(f"Stripe error: {str(e)}")
        return JSONResponse({"error": "決済システムでエラーが発生しました"}, status_code=400)
    except Exception as e:
        print(f"General error: {str(e)}")
        return JSONResponse({"error": "予期しないエラーが発生しました"}, status_code=500)

@app.get("/success", response_class=HTMLResponse)
def success(request: Request, session_id: str = None):
    """決済完了ページ"""
    if session_id:
        try:
            # セッション情報を取得して確認
            session = stripe.checkout.Session.retrieve(session_id)
            if session.payment_status == "paid":
                return """
                <html>
                <head><title>決済完了</title></head>
                <body style="font-family: sans-serif; text-align: center; padding: 50px;">
                    <h1>✅ 決済が完了しました</h1>
                    <p>有料プランへのアップグレードが完了いたしました。</p>
                    <p>これで回数制限なくご利用いただけます。</p>
                    <a href="/" style="background: #007bff; color: white; padding: 10px 20px; 
                       text-decoration: none; border-radius: 5px;">アプリに戻る</a>
                </body>
                </html>
                """
        except Exception as e:
            print(f"Session validation error: {str(e)}")
    
    return """
    <html>
    <head><title>決済完了</title></head>
    <body style="font-family: sans-serif; text-align: center; padding: 50px;">
        <h1>✅ 決済が完了しました</h1>
        <p>決済処理が完了いたしました。</p>
        <a href="/" style="background: #007bff; color: white; padding: 10px 20px; 
           text-decoration: none; border-radius: 5px;">アプリに戻る</a>
    </body>
    </html>
    """

@app.get("/cancel", response_class=HTMLResponse)
def cancel():
    """決済キャンセルページ"""
    return """
    <html>
    <head><title>決済キャンセル</title></head>
    <body style="font-family: sans-serif; text-align: center; padding: 50px;">
        <h1>❌ 決済がキャンセルされました</h1>
        <p>決済処理がキャンセルされました。</p>
        <p>必要に応じて、再度お試しください。</p>
        <a href="/" style="background: #6c757d; color: white; padding: 10px 20px; 
           text-decoration: none; border-radius: 5px;">アプリに戻る</a>
    </body>
    </html>
    """


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {"request": request}
    )

@app.get("/api/usage")
def get_usage(request: Request, response: Response):
    user_key = get_user_key(request, response)
    usage_info = get_usage_info(user_key)
    print(f"Debug: User key: {user_key}, Usage info: {usage_info}")  # デバッグ用
    return usage_info

@app.get("/api/debug/usage")
def debug_usage():
    """デバッグ用：全ユーザーの利用状況を表示"""
    if not DEBUG_MODE:
        raise HTTPException(status_code=404, detail="Not found")
    return {
        "usage_store": dict(usage_store),
        "subscriptions": dict(user_subscriptions),
        "limit": USAGE_LIMIT
    }

@app.post("/api/debug/reset")
def reset_usage(request: Request, response: Response):
    """デバッグ用：現在のユーザーの利用回数をリセット"""
    if not DEBUG_MODE:
        raise HTTPException(status_code=404, detail="Not found")
    user_key = get_user_key(request, response)
    if user_key in usage_store:
        del usage_store[user_key]
    return {"message": f"Usage reset for user: {user_key}"}

@app.post("/api/debug/clear-all")
def clear_all_usage():
    """デバッグ用：全ユーザーの利用回数をクリア"""
    if not DEBUG_MODE:
        raise HTTPException(status_code=404, detail="Not found")
    usage_store.clear()
    return {"message": "All usage data cleared"}

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
