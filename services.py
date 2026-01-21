from sqlalchemy.orm import Session
from datetime import date, datetime
import json
from database import User, Subscription
from typing import Optional

class UserService:
    @staticmethod
    def get_or_create_user(db: Session, user_key: str) -> User:
        """ユーザーを取得または作成"""
        user = db.query(User).filter(User.user_key == user_key).first()
        if not user:
            user = User(
                user_key=user_key,
                daily_usage_date=date.today().isoformat(),
                daily_usage_count=0
            )
            db.add(user)
            db.commit()
            db.refresh(user)
        else:
            # 最終アクセス日時を更新
            user.last_seen = datetime.utcnow()
            db.commit()
        
        return user
    
    @staticmethod
    def check_and_update_usage(db: Session, user_key: str, usage_limit: int = 20) -> dict:
        """利用回数をチェックして更新"""
        user = UserService.get_or_create_user(db, user_key)
        today = date.today().isoformat()
        
        # プレミアムユーザーは制限なし（サブスクリプションベースで判定）
        is_premium = SubscriptionService.is_user_premium(db, user_key)
        if is_premium:
            print(f"DEBUG: User {user_key} is premium - unlimited usage")
            return {
                "used": 0,
                "remaining": -1,
                "limit": -1,
                "premium": True,
                "can_use": True
            }
        
        print(f"DEBUG: User {user_key} is not premium - checking usage limits")
        
        # 日付が変わっていたらリセット
        if user.daily_usage_date != today:
            user.daily_usage_date = today
            user.daily_usage_count = 0
            db.commit()
        
        # 利用制限チェック
        can_use = user.daily_usage_count < usage_limit
        
        if can_use:
            user.daily_usage_count += 1
            db.commit()
        
        return {
            "used": user.daily_usage_count,
            "remaining": max(0, usage_limit - user.daily_usage_count),
            "limit": usage_limit,
            "premium": False,
            "can_use": can_use
        }
    
    @staticmethod
    def get_usage_info(db: Session, user_key: str, usage_limit: int = 20) -> dict:
        """利用回数情報を取得（更新なし）"""
        user = UserService.get_or_create_user(db, user_key)
        today = date.today().isoformat()
        
        # プレミアム判定をサブスクリプションベースで行う
        is_premium = SubscriptionService.is_user_premium(db, user_key)
        if is_premium:
            return {
                "used": 0,
                "remaining": -1,
                "limit": -1,
                "premium": True
            }
        
        # 日付が変わっていたら利用回数をリセット
        if user.daily_usage_date != today:
            user.daily_usage_date = today
            user.daily_usage_count = 0
            db.commit()
        
        return {
            "used": user.daily_usage_count,
            "remaining": max(0, usage_limit - user.daily_usage_count),
            "limit": usage_limit,
            "premium": False
        }

class SubscriptionService:
    @staticmethod
    def create_subscription(
        db: Session,
        user_key: str,
        stripe_customer_id: str = None,
        stripe_subscription_id: str = None,
        stripe_session_id: str = None,
        metadata_dict: dict = None
    ) -> Subscription:
        """サブスクリプションを作成"""
        # ユーザーを取得または作成
        user = UserService.get_or_create_user(db, user_key)
        
        # 既存のアクティブなサブスクリプションがあるかチェック
        existing = db.query(Subscription).filter(
            Subscription.user_key == user_key,
            Subscription.is_active == True
        ).first()
        
        if existing:
            # 既存のサブスクリプションを更新
            existing.stripe_customer_id = stripe_customer_id or existing.stripe_customer_id
            existing.stripe_subscription_id = stripe_subscription_id or existing.stripe_subscription_id
            existing.stripe_session_id = stripe_session_id or existing.stripe_session_id
            existing.is_active = True  # 確実にアクティブ状態を設定
            existing.updated_at = datetime.utcnow()
            if metadata_dict:
                existing.meta_data = json.dumps(metadata_dict)
        else:
            # 新規サブスクリプション作成
            existing = Subscription(
                user_id=user.id,
                user_key=user_key,
                stripe_customer_id=stripe_customer_id,
                stripe_subscription_id=stripe_subscription_id,
                stripe_session_id=stripe_session_id,
                is_active=True,  # 明示的にアクティブ状態を設定
                meta_data=json.dumps(metadata_dict) if metadata_dict else None
            )
            db.add(existing)
        
        # ユーザーをプレミアム状態に設定
        user.is_premium = True
        
        db.commit()
        db.refresh(existing)
        
        return existing
    
    @staticmethod
    def is_user_premium(db: Session, user_key: str) -> bool:
        """ユーザーがプレミアムかどうかをアクティブなサブスクリプションで判定"""
        # アクティブなサブスクリプションがあるかチェック
        active_subscription = db.query(Subscription).filter(
            Subscription.user_key == user_key,
            Subscription.is_active == True
        ).first()
        
        print(f"DEBUG: Checking premium status for user {user_key}")
        print(f"DEBUG: Active subscription found: {active_subscription is not None}")
        
        if active_subscription:
            print(f"DEBUG: Active subscription ID: {active_subscription.stripe_subscription_id}")
            # アクティブなサブスクリプションがある場合、ユーザーフラグも確実に更新
            user = db.query(User).filter(User.user_key == user_key).first()
            if user and not user.is_premium:
                print(f"DEBUG: Updating user {user_key} premium flag to True")
                user.is_premium = True
                db.commit()
            return True
        else:
            print(f"DEBUG: No active subscription found for user {user_key}")
            # アクティブなサブスクリプションがない場合、ユーザーフラグも確実に解除
            user = db.query(User).filter(User.user_key == user_key).first()
            if user and user.is_premium:
                print(f"DEBUG: Updating user {user_key} premium flag to False")
                user.is_premium = False
                db.commit()
            return False
    
    @staticmethod
    def get_active_subscription(db: Session, user_key: str) -> Optional[Subscription]:
        """アクティブなサブスクリプションを取得"""
        return db.query(Subscription).filter(
            Subscription.user_key == user_key,
            Subscription.is_active == True
        ).first()
    
    @staticmethod
    def cancel_subscription(db: Session, user_key: str) -> bool:
        """サブスクリプションをキャンセル"""
        subscription = SubscriptionService.get_active_subscription(db, user_key)
        if subscription:
            subscription.is_active = False
            subscription.canceled_at = datetime.utcnow()
            subscription.updated_at = datetime.utcnow()
            
            # ユーザーのプレミアム状態も解除
            user = db.query(User).filter(User.user_key == user_key).first()
            if user:
                user.is_premium = False
            
            db.commit()
            return True
        return False