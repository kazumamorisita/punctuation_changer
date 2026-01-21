import os
from fastapi import FastAPI, HTTPException, Request, Depends
from pydantic import BaseModel
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from datetime import date
import uuid
from fastapi import Response
import stripe
from fastapi.responses import JSONResponse
from dotenv import load_dotenv
from sqlalchemy.orm import Session
from database import get_db, init_db, User, Subscription
from services import UserService, SubscriptionService
from datetime import datetime
import json
import hashlib
import hmac
from typing import Optional

# 環境変数の読み込み
load_dotenv()

# データベース初期化
init_db()

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# Stripe設定
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")
if not stripe.api_key:
    raise ValueError("STRIPE_SECRET_KEY環境変数が設定されていません")

# アプリケーション設定
BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000")

# フィンガープリント機能のON/OFF（本番環境では一時的に無効化可能）
FINGERPRINT_ENABLED = os.environ.get("FINGERPRINT_ENABLED", "true").lower() == "true"

# Render環境の自動検出とHTTPS化
if "RENDER" in os.environ:
    # Render環境の場合、RENDER_EXTERNAL_URLを優先使用
    render_url = os.environ.get("RENDER_EXTERNAL_URL")
    if render_url:
        BASE_URL = render_url
    elif not BASE_URL.startswith("https://"):
        # BASE_URLがHTTPSでない場合、アプリ名からHTTPS URLを推測
        app_name = os.environ.get("RENDER_SERVICE_NAME", "punctuation-checker")
        BASE_URL = f"https://{app_name}.onrender.com"

# 念のため、既存のURL形式も補正
if not BASE_URL.startswith(("http://", "https://")):
    BASE_URL = f"https://{BASE_URL}"

print(f"Application BASE_URL: {BASE_URL}")

STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
DEBUG_MODE = os.environ.get("DEBUG", "False").lower() == "true"

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
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    """Stripe Webhookエンドポイント - データベース統合版"""
    payload = await request.body()
    sig_header = request.headers.get('stripe-signature', '')
    
    # 本番環境では署名検証を有効にしてください
    if STRIPE_WEBHOOK_SECRET and not verify_webhook_signature(payload, sig_header, STRIPE_WEBHOOK_SECRET):
        raise HTTPException(status_code=400, detail="Invalid signature")
    
    try:
        event = json.loads(payload.decode('utf-8'))
        print(f"Received webhook: {event['type']}")
        
        # サブスクリプション作成時（チェックアウト完了）
        if event['type'] == 'checkout.session.completed':
            session = event['data']['object']
            user_key = session.get('client_reference_id')
            
            if user_key:
                # Stripeから詳細情報を取得
                subscription_id = session.get('subscription')
                customer_id = session.get('customer')
                session_id = session['id']
                
                # セッション作成時のリクエスト情報を保存（可能な場合）
                metadata = {
                    "plan": "premium",
                    "payment_status": session.get('payment_status'),
                    "amount_total": session.get('amount_total'),
                    "session_id": session_id
                }
                
                # Stripeメタデータからフィンガープリント情報を取得
                stripe_metadata = session.get('metadata', {})
                fingerprint = stripe_metadata.get('fingerprint')
                payment_ip = stripe_metadata.get('ip')
                payment_user_agent = stripe_metadata.get('user_agent')
                
                # データベースにサブスクリプション情報を記録
                subscription = SubscriptionService.create_subscription(
                    db=db,
                    user_key=user_key,
                    stripe_customer_id=customer_id,
                    stripe_subscription_id=subscription_id,
                    stripe_session_id=session_id,
                    metadata_dict=metadata,
                    fingerprint=fingerprint,
                    payment_ip=payment_ip,
                    payment_user_agent=payment_user_agent
                )
                
                # 確実にプレミアム状態にする
                user = UserService.get_or_create_user(db, user_key)
                user.is_premium = True
                db.commit()
                
                print(f"User {user_key} subscribed with {subscription_id} - Premium activated")
        
        # サブスクリプション更新時
        elif event['type'] == 'customer.subscription.updated':
            subscription = event['data']['object']
            subscription_id = subscription['id']
            status = subscription['status']
            
            # サブスクリプションIDからユーザーを特定
            existing_sub = db.query(Subscription).filter(
                Subscription.stripe_subscription_id == subscription_id
            ).first()
            
            if existing_sub:
                if status in ['active', 'trialing']:
                    existing_sub.is_active = True
                else:
                    existing_sub.is_active = False
                
                existing_sub.updated_at = datetime.utcnow()
                db.commit()
                print(f"Updated subscription {subscription_id} status to {status}")
        
        # サブスクリプション削除時
        elif event['type'] == 'customer.subscription.deleted':
            subscription = event['data']['object']
            subscription_id = subscription['id']
            
            # サブスクリプションを無効化
            existing_sub = db.query(Subscription).filter(
                Subscription.stripe_subscription_id == subscription_id
            ).first()
            
            if existing_sub:
                existing_sub.is_active = False
                existing_sub.canceled_at = datetime.utcnow()
                existing_sub.updated_at = datetime.utcnow()
                
                # ユーザーのプレミアム状態も解除
                user = db.query(User).filter(User.user_key == existing_sub.user_key).first()
                if user:
                    user.is_premium = False
                
                db.commit()
                print(f"Cancelled subscription {subscription_id}")
        
        return {"status": "success"}
    
    except Exception as e:
        print(f"Webhook processing error: {str(e)}")
        raise HTTPException(status_code=400, detail="Webhook processing failed")

def is_user_premium(user_key: str) -> bool:
    """互換性のための関数 - 実際はサービス層を使用"""
    # この関数は旧コードとの互換性のため残しているが、
    # 実際はSubscriptionServiceを使用することが推奨
    return False  # プレースホルダー

USAGE_LIMIT = 20

# 旧コードとの互換性のため（実際はデータベースで管理）
usage_store = {}

def get_user_key(request: Request, response: Response):
    """ユーザー識別キーを生成または取得（永続化対応）"""
    # まずCookieからユーザーIDを取得
    user_id = request.cookies.get("uid")
    
    # IPアドレスとUser-Agentの組み合わせでフィンガープリント生成
    client_ip = request.client.host
    user_agent = request.headers.get("user-agent", "")
    fingerprint = hashlib.md5(f"{client_ip}:{user_agent}".encode()).hexdigest()[:16]
    
    if not user_id:
        # Cookieがない場合、フィンガープリントベースのユーザーが存在するかチェック
        user_id = str(uuid.uuid4())
        
        # 新しいCookieを設定
        response.set_cookie(
            key="uid",
            value=user_id,
            max_age=60*60*24*365,  # 1年間
            httponly=True,
            samesite="lax",
            secure=True if request.url.scheme == "https" else False
        )
    
    # フィンガープリント情報も保存してユーザー復元に使用
    response.set_cookie(
        key="ufp",  # user fingerprint
        value=fingerprint,
        max_age=60*60*24*365,
        httponly=True,
        samesite="lax",
        secure=True if request.url.scheme == "https" else False
    )
    
    return user_id

def create_fingerprint(request: Request) -> str:
    """ブラウザフィンガープリントを生成"""
    client_ip = request.client.host
    user_agent = request.headers.get("user-agent", "")
    return hashlib.md5(f"{client_ip}:{user_agent}".encode()).hexdigest()[:16]

def find_user_by_fingerprint(db: Session, request: Request) -> Optional[str]:
    """フィンガープリントベースでユーザーを検索（スキーマ互換性対応）"""
    try:
        fingerprint = create_fingerprint(request)
        print(f"Searching for user with fingerprint: {fingerprint}")
        
        # まず、Userテーブルから直接検索（プレミアムユーザーのみ）
        premium_user = db.query(User).filter(
            User.browser_fingerprint == fingerprint,
            User.is_premium == True
        ).first()
        
        if premium_user:
            print(f"Found premium user in User table: {premium_user.user_key}")
            return premium_user.user_key
        
        # 次に、アクティブなサブスクリプションから検索
        subscription = db.query(Subscription).filter(
            Subscription.browser_fingerprint == fingerprint,
            Subscription.is_active == True
        ).first()
        
        if subscription:
            print(f"Found premium user in Subscription table: {subscription.user_key}")
            return subscription.user_key
        
        print("No premium user found with this fingerprint")
    except Exception as e:
        print(f"Warning: Fingerprint lookup failed (old schema?): {str(e)}")
        # 古いスキーマの場合はNoneを返す
    
    return None

def update_user_fingerprint(db: Session, user_key: str, request: Request):
    """ユーザーのフィンガープリント情報を更新（スキーマ互換性対応）"""
    try:
        fingerprint = create_fingerprint(request)
        client_ip = request.client.host
        user_agent = request.headers.get("user-agent", "")
        
        user = db.query(User).filter(User.user_key == user_key).first()
        if user:
            # 新しいスキーマのフィールドが存在するかチェック
            if hasattr(user, 'browser_fingerprint'):
                user.browser_fingerprint = fingerprint
                user.last_ip = client_ip
                user.last_user_agent = user_agent
            user.last_seen = datetime.utcnow()
            db.commit()
            print(f"Updated fingerprint for user {user_key}: {fingerprint}")
    except Exception as e:
        print(f"Warning: Could not update fingerprint for {user_key}: {str(e)}")
        # スキーマ更新エラーは無視して続行

def enhanced_get_user_key(request: Request, response: Response, db: Session) -> str:
    """拡張ユーザー識別（大幅改善版）- フィンガープリント機能のON/OFF対応"""
    # 通常のCookieベース識別
    user_id = request.cookies.get("uid")
    
    print(f"=== Enhanced User Identification ===")
    print(f"Cookie user_id: {user_id}")
    print(f"Fingerprint enabled: {FINGERPRINT_ENABLED}")
    
    if FINGERPRINT_ENABLED:
        fingerprint = create_fingerprint(request)
        print(f"Current fingerprint: {fingerprint}")
    
    # Cookieがある場合の処理
    if user_id:
        print(f"Cookie found: {user_id}")
        # フィンガープリント情報を更新（有効な場合のみ）
        if FINGERPRINT_ENABLED:
            update_user_fingerprint(db, user_id, request)
        return user_id
    
    # Cookieがない場合、フィンガープリントでプレミアムユーザーを検索（有効な場合のみ）
    if FINGERPRINT_ENABLED:
        existing_user = find_user_by_fingerprint(db, request)
        
        if existing_user:
            # プレミアムユーザーが見つかった場合、そのユーザーIDを復元
            user_id = existing_user
            print(f"Restored premium user from fingerprint: {user_id}")
            
            # Cookieを再設定
            response.set_cookie(
                key="uid",
                value=user_id,
                max_age=60*60*24*365,
                httponly=True,
                samesite="lax",
                secure=True if request.url.scheme == "https" else False
            )
            
            # フィンガープリント情報も更新
            update_user_fingerprint(db, user_id, request)
            
            print(f"Final user_id: {user_id}")
            print("=== End User Identification ===")
            return user_id
    
    # 新規ユーザー
    user_id = str(uuid.uuid4())
    print(f"Creating new user: {user_id}")
    
    response.set_cookie(
        key="uid",
        value=user_id,
        max_age=60*60*24*365,
        httponly=True,
        samesite="lax",
        secure=True if request.url.scheme == "https" else False
    )
    
    # フィンガープリント情報Cookie（デバッグ用、有効な場合のみ）
    if FINGERPRINT_ENABLED:
        fingerprint = create_fingerprint(request)
        response.set_cookie(
            key="ufp",
            value=fingerprint,
            max_age=60*60*24*365,
            httponly=True,
            samesite="lax",
            secure=True if request.url.scheme == "https" else False
        )
    
    print(f"Final user_id: {user_id}")
    print("=== End User Identification ===")
    
    return user_id

def check_usage_limit(user_key: str, db: Session):
    """利用制限をチェック（データベース統合版）"""
    usage_info = UserService.check_and_update_usage(db, user_key, USAGE_LIMIT)
    if not usage_info["can_use"]:
        raise HTTPException(
            status_code=429,
            detail="本日の無料利用回数を超えました"
        )

def get_usage_info(user_key: str, db: Session):
    """利用回数情報を取得（データベース統合版）"""
    return UserService.get_usage_info(db, user_key, USAGE_LIMIT)


# =========================
# HTML
# =========================
@app.post("/api/create-checkout-session")
def create_checkout_session(request: Request, response: Response, db: Session = Depends(get_db)):
    """Stripe チェックアウトセッション作成"""
    try:
        # 拡張ユーザー識別を使用
        user_key = enhanced_get_user_key(request, response, db)
        
        # フィンガープリント情報を生成
        fingerprint = create_fingerprint(request)
        client_ip = request.client.host
        user_agent = request.headers.get("user-agent", "")
        
        print(f"Creating checkout session for user: {user_key}")
        print(f"User fingerprint: {fingerprint}")
        print(f"BASE_URL: {BASE_URL}")
        print(f"Stripe API Key configured: {bool(stripe.api_key)}")
        
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
                "plan": "premium",
                "fingerprint": fingerprint,
                "ip": client_ip,
                "user_agent": user_agent[:100]  # 長すぎる場合は切り詰め
            }
        )
        
        print(f"Checkout session created: {session.id}")
        print(f"Redirecting to: {session.url}")
        
        return JSONResponse({"url": session.url})
        
    except stripe.error.InvalidRequestError as e:
        print(f"Stripe InvalidRequestError: {str(e)}")
        return JSONResponse({"error": f"リクエストエラー: {str(e)}"}, status_code=400)
    except stripe.error.AuthenticationError as e:
        print(f"Stripe AuthenticationError: {str(e)}")
        return JSONResponse({"error": "Stripe認証エラーが発生しました"}, status_code=500)
    except stripe.error.StripeError as e:
        print(f"Stripe error: {str(e)}")
        return JSONResponse({"error": "決済システムでエラーが発生しました"}, status_code=400)
    except Exception as e:
        print(f"General error: {str(e)}")
        return JSONResponse({"error": "予期しないエラーが発生しました"}, status_code=500)

@app.get("/success", response_class=HTMLResponse)
def success(request: Request, response: Response, session_id: str = None, db: Session = Depends(get_db)):
    """決済完了ページ"""
    user_key = enhanced_get_user_key(request, response, db)
    
    if session_id:
        try:
            # セッション情報を取得して確認
            session = stripe.checkout.Session.retrieve(session_id)
            if session.payment_status == "paid":
                # 決済完了時に確実にプレミアム状態を設定
                subscription_id = session.get('subscription')
                customer_id = session.get('customer')
                
                # データベースにサブスクリプションを作成
                SubscriptionService.create_subscription(
                    db=db,
                    user_key=user_key,
                    stripe_customer_id=customer_id,
                    stripe_subscription_id=subscription_id,
                    stripe_session_id=session_id,
                    metadata_dict={
                        "plan": "premium",
                        "payment_status": session.payment_status,
                        "amount_total": session.get('amount_total')
                    }
                )
                print(f"Premium activated for user: {user_key}")
                
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
def get_usage(request: Request, response: Response, db: Session = Depends(get_db)):
    """利用回数情報を取得"""
    user_key = enhanced_get_user_key(request, response, db)
    usage_info = get_usage_info(user_key, db)
    
    # フィンガープリント診断情報を追加
    fingerprint = create_fingerprint(request) if FINGERPRINT_ENABLED else "DISABLED"
    cookie_uid = request.cookies.get("uid")
    cookie_ufp = request.cookies.get("ufp")
    
    # 診断情報をusage_infoに追加
    usage_info["debug_info"] = {
        "user_key": user_key,
        "fingerprint": fingerprint,
        "fingerprint_enabled": FINGERPRINT_ENABLED,
        "cookie_uid": cookie_uid,
        "cookie_ufp": cookie_ufp,
        "ip": request.client.host,
        "user_agent": request.headers.get("user-agent", "")[:50] + "..." if len(request.headers.get("user-agent", "")) > 50 else request.headers.get("user-agent", "")
    }
    
    print(f"Debug: User key: {user_key}, Usage info: {usage_info}")
    return usage_info

@app.get("/api/debug/config")
def debug_config():
    """デバッグ用：環境設定の確認"""
    return {
        "base_url": BASE_URL,
        "base_url_env": os.environ.get("BASE_URL"),
        "render_external_url": os.environ.get("RENDER_EXTERNAL_URL"),
        "render_service_name": os.environ.get("RENDER_SERVICE_NAME"),
        "is_render": "RENDER" in os.environ,
        "stripe_configured": bool(stripe.api_key),
        "stripe_key_prefix": stripe.api_key[:7] + "..." if stripe.api_key else None,
        "webhook_secret_configured": bool(STRIPE_WEBHOOK_SECRET),
        "debug_mode": DEBUG_MODE,
        "all_env_vars": {k: v for k, v in os.environ.items() if k.startswith(('RENDER', 'BASE'))}
    }

@app.get("/api/debug/usage")
def debug_usage(db: Session = Depends(get_db)):
    """デバッグ用：全ユーザーの利用状況を表示（データベース統合版）"""
    if not DEBUG_MODE:
        raise HTTPException(status_code=404, detail="Not found")
    
    # データベースから全ユーザー情報を取得
    users = db.query(User).all()
    subscriptions = db.query(Subscription).filter(Subscription.is_active == True).all()
    
    user_data = {}
    for user in users:
        user_data[user.user_key] = {
            "daily_usage": user.daily_usage_count,
            "usage_date": user.daily_usage_date,
            "is_premium": user.is_premium,
            "created_at": user.created_at.isoformat() if user.created_at else None
        }
    
    subscription_data = {}
    for sub in subscriptions:
        subscription_data[sub.user_key] = {
            "subscription_id": sub.stripe_subscription_id,
            "is_active": sub.is_active,
            "created_at": sub.created_at.isoformat() if sub.created_at else None
        }
    
    return {
        "users": user_data,
        "active_subscriptions": subscription_data,
        "limit": USAGE_LIMIT
    }

@app.get("/api/debug/user-status")
def debug_user_status(request: Request, response: Response, db: Session = Depends(get_db)):
    """デバッグ用：現在のユーザーの状態を詳細表示"""
    if not DEBUG_MODE:
        raise HTTPException(status_code=404, detail="Not found")
    
    user_key = enhanced_get_user_key(request, response, db)
    fingerprint = create_fingerprint(request)
    
    user = db.query(User).filter(User.user_key == user_key).first()
    is_premium = SubscriptionService.is_user_premium(db, user_key)
    active_sub = SubscriptionService.get_active_subscription(db, user_key)
    usage_info = UserService.get_usage_info(db, user_key, USAGE_LIMIT)
    
    # 同じフィンガープリントの他のユーザーも検索
    other_users = db.query(User).filter(
        User.browser_fingerprint == fingerprint,
        User.user_key != user_key
    ).all()
    
    other_subscriptions = db.query(Subscription).filter(
        Subscription.browser_fingerprint == fingerprint,
        Subscription.user_key != user_key
    ).all()
    
    return {
        "current_user": {
            "user_key": user_key,
            "user_id": user.id if user else None,
            "is_premium_db": user.is_premium if user else False,
            "is_premium_service": is_premium,
            "daily_usage_count": user.daily_usage_count if user else 0,
            "daily_usage_date": user.daily_usage_date if user else None,
            "browser_fingerprint": user.browser_fingerprint if user else None,
            "last_ip": user.last_ip if user else None,
        },
        "current_request": {
            "fingerprint": fingerprint,
            "ip": request.client.host,
            "user_agent": request.headers.get("user-agent", "")[:100]
        },
        "usage_info": usage_info,
        "active_subscription": {
            "id": active_sub.id if active_sub else None,
            "stripe_subscription_id": active_sub.stripe_subscription_id if active_sub else None,
            "is_active": active_sub.is_active if active_sub else None,
            "browser_fingerprint": active_sub.browser_fingerprint if active_sub else None,
            "payment_ip": active_sub.payment_ip if active_sub else None,
        } if active_sub else None,
        "fingerprint_matches": {
            "other_users": len(other_users),
            "other_subscriptions": len(other_subscriptions),
            "users_detail": [{"user_key": u.user_key, "is_premium": u.is_premium} for u in other_users],
            "subscriptions_detail": [{"user_key": s.user_key, "is_active": s.is_active} for s in other_subscriptions]
        }
    }

@app.post("/api/debug/reset")
def reset_usage(request: Request, response: Response, db: Session = Depends(get_db)):
    """デバッグ用：現在のユーザーの利用回数をリセット"""
    if not DEBUG_MODE:
        raise HTTPException(status_code=404, detail="Not found")
    
    user_key = get_user_key(request, response)
    user = db.query(User).filter(User.user_key == user_key).first()
    
    if user:
        user.daily_usage_count = 0
        user.daily_usage_date = date.today().isoformat()
        db.commit()
        return {"message": f"Usage reset for user: {user_key}"}
    else:
        return {"message": f"User not found: {user_key}"}

@app.post("/api/debug/clear-all")
def clear_all_usage(db: Session = Depends(get_db)):
    """デバッグ用：全ユーザーの利用回数をクリア"""
    if not DEBUG_MODE:
        raise HTTPException(status_code=404, detail="Not found")
    
    # 全ユーザーの利用回数をリセット
    users = db.query(User).all()
    for user in users:
        user.daily_usage_count = 0
        user.daily_usage_date = date.today().isoformat()
    
    db.commit()
    return {"message": f"Usage cleared for {len(users)} users"}

@app.post("/api/debug/recreate-db")
def recreate_database():
    """緊急用：データベースを新しいスキーマで再作成"""
    try:
        from database import Base, engine
        # 全テーブルを削除
        Base.metadata.drop_all(bind=engine)
        # 新しいスキーマでテーブルを再作成
        Base.metadata.create_all(bind=engine)
        return {"success": True, "message": "Database recreated with new schema"}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.get("/api/admin/backup")
def backup_database(db: Session = Depends(get_db)):
    """データベースの全データをJSONでエクスポート（緊急時のバックアップ用）"""
    try:
        users = db.query(User).all()
        subscriptions = db.query(Subscription).all()
        
        backup_data = {
            "backup_date": datetime.utcnow().isoformat(),
            "users": [
                {
                    "id": user.id,
                    "user_key": user.user_key,
                    "email": user.email,
                    "created_at": user.created_at.isoformat() if user.created_at else None,
                    "daily_usage_count": user.daily_usage_count,
                    "daily_usage_date": user.daily_usage_date,
                    "is_premium": user.is_premium,
                    "browser_fingerprint": getattr(user, 'browser_fingerprint', None),
                    "last_ip": getattr(user, 'last_ip', None),
                    "last_user_agent": getattr(user, 'last_user_agent', None)
                } for user in users
            ],
            "subscriptions": [
                {
                    "id": sub.id,
                    "user_key": sub.user_key,
                    "stripe_customer_id": sub.stripe_customer_id,
                    "stripe_subscription_id": sub.stripe_subscription_id,
                    "is_active": sub.is_active,
                    "created_at": sub.created_at.isoformat() if sub.created_at else None,
                    "browser_fingerprint": getattr(sub, 'browser_fingerprint', None),
                    "payment_ip": getattr(sub, 'payment_ip', None)
                } for sub in subscriptions
            ]
        }
        
        return backup_data
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.get("/api/admin/db-status")
def check_database_status(db: Session = Depends(get_db)):
    """データベースの登録状況を確認"""
    try:
        # 各テーブルの件数を取得
        user_count = db.query(User).count()
        subscription_count = db.query(Subscription).count()
        active_subscriptions = db.query(Subscription).filter(Subscription.is_active == True).count()
        premium_users = db.query(User).filter(User.is_premium == True).count()
        
        # 最新のユーザー5件を取得
        recent_users = db.query(User).order_by(User.id.desc()).limit(5).all()
        recent_subscriptions = db.query(Subscription).order_by(Subscription.id.desc()).limit(5).all()
        
        return {
            "summary": {
                "total_users": user_count,
                "total_subscriptions": subscription_count,
                "active_subscriptions": active_subscriptions,
                "premium_users": premium_users,
                "database_type": "PostgreSQL" if str(db.bind.url).startswith("postgres") else "SQLite"
            },
            "recent_users": [
                {
                    "id": user.id,
                    "user_key": user.user_key[:8] + "...",  # プライバシー配慮で短縮
                    "is_premium": user.is_premium,
                    "daily_usage_count": user.daily_usage_count,
                    "created_at": user.created_at.isoformat() if user.created_at else None,
                    "has_fingerprint": bool(getattr(user, 'browser_fingerprint', None))
                } for user in recent_users
            ],
            "recent_subscriptions": [
                {
                    "id": sub.id,
                    "user_key": sub.user_key[:8] + "...",  # プライバシー配慮で短縮
                    "is_active": sub.is_active,
                    "stripe_customer_id": sub.stripe_customer_id[:8] + "..." if sub.stripe_customer_id else None,
                    "created_at": sub.created_at.isoformat() if sub.created_at else None,
                    "has_fingerprint": bool(getattr(sub, 'browser_fingerprint', None))
                } for sub in recent_subscriptions
            ]
        }
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.get("/api/admin/user-search/{user_key}")
def search_user_by_key(user_key: str, db: Session = Depends(get_db)):
    """特定のユーザーキーでユーザー情報を検索"""
    try:
        user = db.query(User).filter(User.user_key == user_key).first()
        if not user:
            return {"found": False, "message": "User not found"}
        
        # ユーザーのサブスクリプション情報も取得
        subscriptions = db.query(Subscription).filter(Subscription.user_key == user_key).all()
        
        return {
            "found": True,
            "user": {
                "id": user.id,
                "user_key": user.user_key,
                "email": user.email,
                "is_premium": user.is_premium,
                "daily_usage_count": user.daily_usage_count,
                "daily_usage_date": user.daily_usage_date,
                "created_at": user.created_at.isoformat() if user.created_at else None,
                "last_seen": user.last_seen.isoformat() if getattr(user, 'last_seen', None) else None,
                "browser_fingerprint": getattr(user, 'browser_fingerprint', None),
                "last_ip": getattr(user, 'last_ip', None),
                "last_user_agent": getattr(user, 'last_user_agent', None)
            },
            "subscriptions": [
                {
                    "id": sub.id,
                    "stripe_customer_id": sub.stripe_customer_id,
                    "stripe_subscription_id": sub.stripe_subscription_id,
                    "is_active": sub.is_active,
                    "created_at": sub.created_at.isoformat() if sub.created_at else None,
                    "browser_fingerprint": getattr(sub, 'browser_fingerprint', None),
                    "payment_ip": getattr(sub, 'payment_ip', None)
                } for sub in subscriptions
            ]
        }
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.get("/api/admin/fingerprint-search/{fingerprint}")
def search_by_fingerprint(fingerprint: str, db: Session = Depends(get_db)):
    """フィンガープリントで関連ユーザーを検索"""
    try:
        # ユーザーテーブルから検索
        users = []
        subscriptions = []
        
        try:
            users = db.query(User).filter(User.browser_fingerprint == fingerprint).all()
        except Exception:
            pass  # browser_fingerprintカラムが存在しない場合
            
        try:
            subscriptions = db.query(Subscription).filter(Subscription.browser_fingerprint == fingerprint).all()
        except Exception:
            pass  # browser_fingerprintカラムが存在しない場合
        
        return {
            "fingerprint": fingerprint,
            "found_users": len(users),
            "found_subscriptions": len(subscriptions),
            "users": [
                {
                    "user_key": user.user_key,
                    "is_premium": user.is_premium,
                    "daily_usage_count": user.daily_usage_count,
                    "created_at": user.created_at.isoformat() if user.created_at else None
                } for user in users
            ],
            "subscriptions": [
                {
                    "user_key": sub.user_key,
                    "is_active": sub.is_active,
                    "stripe_customer_id": sub.stripe_customer_id,
                    "created_at": sub.created_at.isoformat() if sub.created_at else None
                } for sub in subscriptions
            ]
        }
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.post("/api/admin/restore")
async def restore_database(request: Request, db: Session = Depends(get_db)):
    """バックアップデータからデータベースを復元"""
    try:
        backup_data = await request.json()
        
        # 既存データをクリア
        db.query(Subscription).delete()
        db.query(User).delete()
        
        # ユーザーデータを復元
        for user_data in backup_data.get("users", []):
            user = User(
                user_key=user_data["user_key"],
                email=user_data.get("email"),
                daily_usage_count=user_data.get("daily_usage_count", 0),
                daily_usage_date=user_data.get("daily_usage_date"),
                is_premium=user_data.get("is_premium", False)
            )
            if hasattr(User, 'browser_fingerprint') and user_data.get("browser_fingerprint"):
                user.browser_fingerprint = user_data["browser_fingerprint"]
            if hasattr(User, 'last_ip') and user_data.get("last_ip"):
                user.last_ip = user_data["last_ip"]
            if hasattr(User, 'last_user_agent') and user_data.get("last_user_agent"):
                user.last_user_agent = user_data["last_user_agent"]
                
            db.add(user)
            
        db.flush()  # IDを取得するため
        
        # サブスクリプションデータを復元
        for sub_data in backup_data.get("subscriptions", []):
            # user_keyからuser_idを取得
            user = db.query(User).filter(User.user_key == sub_data["user_key"]).first()
            if user:
                subscription = Subscription(
                    user_id=user.id,
                    user_key=sub_data["user_key"],
                    stripe_customer_id=sub_data.get("stripe_customer_id"),
                    stripe_subscription_id=sub_data.get("stripe_subscription_id"),
                    is_active=sub_data.get("is_active", False)
                )
                if hasattr(Subscription, 'browser_fingerprint') and sub_data.get("browser_fingerprint"):
                    subscription.browser_fingerprint = sub_data["browser_fingerprint"]
                if hasattr(Subscription, 'payment_ip') and sub_data.get("payment_ip"):
                    subscription.payment_ip = sub_data["payment_ip"]
                    
                db.add(subscription)
        
        db.commit()
        return {"success": True, "message": "Database restored successfully"}
        
    except Exception as e:
        db.rollback()
        return {"success": False, "error": str(e)}

@app.post("/api/debug/create-test-premium")
def create_test_premium_user(request: Request, db: Session = Depends(get_db)):
    """テスト用：現在のユーザーをプレミアムに設定"""
    try:
        # 現在のユーザー情報を取得
        user_key = request.cookies.get("uid")
        if not user_key:
            return {"success": False, "error": "No user cookie found"}
        
        fingerprint = create_fingerprint(request) if FINGERPRINT_ENABLED else None
        
        # ユーザーを取得または作成
        user = UserService.get_or_create_user(
            db=db, 
            user_key=user_key,
            fingerprint=fingerprint,
            ip=request.client.host,
            user_agent=request.headers.get("user-agent", "")
        )
        
        # プレミアム状態に設定
        user.is_premium = True
        
        # テスト用サブスクリプションを作成
        subscription = SubscriptionService.create_subscription(
            db=db,
            user_key=user_key,
            stripe_customer_id="test_customer",
            stripe_subscription_id="test_sub_" + user_key[:8],
            stripe_session_id="test_session",
            fingerprint=fingerprint,
            payment_ip=request.client.host,
            payment_user_agent=request.headers.get("user-agent", "")
        )
        
        return {
            "success": True, 
            "message": f"User {user_key} set to premium",
            "fingerprint": fingerprint,
            "subscription_id": subscription.id
        }
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.get("/api/debug/recreate-db-get")
def recreate_database_get():
    """緊急用：データベースを新しいスキーマで再作成（GET版）"""
    try:
        from database import Base, engine
        # 全テーブルを削除
        Base.metadata.drop_all(bind=engine)
        # 新しいスキーマでテーブルを再作成
        Base.metadata.create_all(bind=engine)
        return {"success": True, "message": "Database recreated with new schema (GET method)"}
    except Exception as e:
        return {"success": False, "error": str(e)}

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
    response: Response,
    db: Session = Depends(get_db)
):
    user_key = enhanced_get_user_key(request, response, db)
    check_usage_limit(user_key, db)

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
# =========================
# サブスクリプション管理 API
# =========================
@app.get("/api/subscription/status")
def get_subscription_status(request: Request, response: Response, db: Session = Depends(get_db)):
    """ユーザーのサブスクリプション状況を取得"""
    user_key = enhanced_get_user_key(request, response, db)
    
    is_premium = SubscriptionService.is_user_premium(db, user_key)
    subscription = SubscriptionService.get_active_subscription(db, user_key)
    
    if subscription:
        return {
            "is_premium": is_premium,
            "subscription_id": subscription.stripe_subscription_id,
            "customer_id": subscription.stripe_customer_id,
            "created_at": subscription.created_at.isoformat() if subscription.created_at else None,
            "metadata": json.loads(subscription.meta_data) if subscription.meta_data else {}
        }
    else:
        return {
            "is_premium": False,
            "subscription_id": None,
            "customer_id": None,
            "created_at": None,
            "metadata": {}
        }

@app.post("/api/subscription/cancel")
def cancel_subscription(request: Request, response: Response, db: Session = Depends(get_db)):
    """サブスクリプションをキャンセル"""
    user_key = enhanced_get_user_key(request, response, db)
    
    # まずアクティブなサブスクリプションを取得
    subscription = SubscriptionService.get_active_subscription(db, user_key)
    
    if not subscription:
        return {"message": "アクティブなサブスクリプションが見つかりません", "success": False}
    
    stripe_error_message = None
    
    # Stripe側でキャンセル処理を実行
    if subscription.stripe_subscription_id:
        try:
            # Stripe APIを使ってサブスクリプションを即座にキャンセル
            canceled_subscription = stripe.Subscription.cancel(subscription.stripe_subscription_id)
            print(f"Stripe subscription immediately canceled: {subscription.stripe_subscription_id}")
            print(f"Canceled at: {canceled_subscription.canceled_at}")
            
        except stripe.error.StripeError as e:
            print(f"Stripe cancellation error: {str(e)}")
            stripe_error_message = str(e)
            # Stripe側でエラーが発生した場合、データベースの処理は行わない
            return {
                "message": f"Stripe側での解約処理でエラーが発生しました: {stripe_error_message}", 
                "success": False
            }
    
    # Stripe側で成功した場合のみ、データベースでサブスクリプションをキャンセル
    db_success = SubscriptionService.cancel_subscription(db, user_key)
    
    if db_success:
        return {"message": "サブスクリプションを即座にキャンセルしました。無料プランに戻りました。", "success": True}
    else:
        return {"message": "データベース側の解約処理でエラーが発生しました", "success": False}

@app.post("/api/subscription/cancel-immediately")
def cancel_subscription_immediately(request: Request, response: Response, db: Session = Depends(get_db)):
    """サブスクリプションを即座にキャンセル（デバッグ用）"""
    if not DEBUG_MODE:
        raise HTTPException(status_code=404, detail="Not found")
    
    user_key = get_user_key(request, response)
    
    # アクティブなサブスクリプションを取得
    subscription = SubscriptionService.get_active_subscription(db, user_key)
    
    if not subscription:
        return {"message": "アクティブなサブスクリプションが見つかりません", "success": False}
    
    # Stripe側で即座にキャンセル
    if subscription.stripe_subscription_id:
        try:
            canceled_subscription = stripe.Subscription.cancel(subscription.stripe_subscription_id)
            print(f"Stripe subscription immediately canceled: {subscription.stripe_subscription_id}")
            
        except stripe.error.StripeError as e:
            print(f"Stripe immediate cancellation error: {str(e)}")
            return {
                "message": f"Stripe側での即座解約処理でエラーが発生しました: {str(e)}", 
                "success": False
            }
    
    # データベースでサブスクリプションをキャンセル
    db_success = SubscriptionService.cancel_subscription(db, user_key)
    
    if db_success:
        return {"message": "サブスクリプションを即座にキャンセルしました", "success": True}
    else:
        return {"message": "データベース側の解約処理でエラーが発生しました", "success": False}

@app.get("/api/debug/stripe-subscription/{user_key}")
def debug_stripe_subscription(user_key: str, db: Session = Depends(get_db)):
    """デバッグ用：StripeサブスクリプションのAPIから直接情報を取得"""
    if not DEBUG_MODE:
        raise HTTPException(status_code=404, detail="Not found")
    
    subscription = SubscriptionService.get_active_subscription(db, user_key)
    
    if not subscription or not subscription.stripe_subscription_id:
        return {"message": "アクティブなサブスクリプションが見つかりません"}
    
    try:
        # Stripe APIから直接サブスクリプション情報を取得
        stripe_subscription = stripe.Subscription.retrieve(subscription.stripe_subscription_id)
        
        return {
            "database_subscription": {
                "id": subscription.id,
                "stripe_subscription_id": subscription.stripe_subscription_id,
                "is_active": subscription.is_active,
                "created_at": subscription.created_at.isoformat() if subscription.created_at else None,
                "canceled_at": subscription.canceled_at.isoformat() if subscription.canceled_at else None
            },
            "stripe_subscription": {
                "id": stripe_subscription.id,
                "status": stripe_subscription.status,
                "cancel_at_period_end": stripe_subscription.cancel_at_period_end,
                "canceled_at": stripe_subscription.canceled_at,
                "current_period_start": stripe_subscription.current_period_start,
                "current_period_end": stripe_subscription.current_period_end,
                "created": stripe_subscription.created
            }
        }
        
    except stripe.error.StripeError as e:
        return {"error": f"Stripe API エラー: {str(e)}"}

# =========================
# 管理者用 API（デバッグモード時のみ）
# =========================
@app.get("/api/admin/users")
def get_all_users(db: Session = Depends(get_db)):
    """全ユーザー一覧を取得（管理者用）"""
    if not DEBUG_MODE:
        raise HTTPException(status_code=404, detail="Not found")
    
    users = db.query(User).all()
    user_list = []
    
    for user in users:
        # ユーザーのサブスクリプション情報も取得
        active_sub = SubscriptionService.get_active_subscription(db, user.user_key)
        
        user_info = {
            "id": user.id,
            "user_key": user.user_key,
            "is_premium": user.is_premium,
            "daily_usage_count": user.daily_usage_count,
            "daily_usage_date": user.daily_usage_date,
            "created_at": user.created_at.isoformat() if user.created_at else None,
            "last_seen": user.last_seen.isoformat() if user.last_seen else None,
            "active_subscription": {
                "id": active_sub.id if active_sub else None,
                "stripe_subscription_id": active_sub.stripe_subscription_id if active_sub else None,
                "created_at": active_sub.created_at.isoformat() if active_sub and active_sub.created_at else None
            } if active_sub else None
        }
        user_list.append(user_info)
    
    return {"users": user_list, "total": len(user_list)}

@app.get("/api/admin/subscriptions")
def get_all_subscriptions(db: Session = Depends(get_db)):
    """全サブスクリプション一覧を取得（管理者用）"""
    if not DEBUG_MODE:
        raise HTTPException(status_code=404, detail="Not found")
    
    subscriptions = db.query(Subscription).all()
    sub_list = []
    
    for sub in subscriptions:
        sub_info = {
            "id": sub.id,
            "user_key": sub.user_key,
            "stripe_customer_id": sub.stripe_customer_id,
            "stripe_subscription_id": sub.stripe_subscription_id,
            "stripe_session_id": sub.stripe_session_id,
            "is_active": sub.is_active,
            "created_at": sub.created_at.isoformat() if sub.created_at else None,
            "updated_at": sub.updated_at.isoformat() if sub.updated_at else None,
            "canceled_at": sub.canceled_at.isoformat() if sub.canceled_at else None,
            "metadata": json.loads(sub.meta_data) if sub.meta_data else {}
        }
        sub_list.append(sub_info)
    
    return {"subscriptions": sub_list, "total": len(sub_list)}

@app.post("/api/admin/user/{user_key}/premium")
def toggle_user_premium(user_key: str, db: Session = Depends(get_db)):
    """ユーザーのプレミアム状態を切り替え（管理者用）"""
    if not DEBUG_MODE:
        raise HTTPException(status_code=404, detail="Not found")
    
    user = db.query(User).filter(User.user_key == user_key).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    user.is_premium = not user.is_premium
    db.commit()
    
    return {
        "user_key": user_key,
        "is_premium": user.is_premium,
        "message": f"ユーザー {user_key} のプレミアム状態を {'有効' if user.is_premium else '無効'} にしました"
    }

# =========================
# アプリケーション起動
# =========================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8000))
    )