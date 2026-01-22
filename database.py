from sqlalchemy import create_engine, Column, String, DateTime, Boolean, Integer, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime
import os

from sqlalchemy import create_engine, Column, String, DateTime, Boolean, Integer, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime
import os

# 強制的に環境変数を最優先で読み込み
def get_database_url():
    """環境変数からDATABASE_URLを取得（詳細ログ付き）"""
    # 直接環境変数から取得
    database_url = os.environ.get("DATABASE_URL")
    
    print(f"[DATABASE.PY] Raw DATABASE_URL: {database_url}")
    
    # PostgreSQL環境変数を探す
    if not database_url or not database_url.startswith("postgres"):
        for key, value in os.environ.items():
            if "postgres" in value.lower() and "supabase" in value.lower():
                print(f"[DATABASE.PY] Found PostgreSQL URL in {key}: {value[:20]}...")
                database_url = value
                break
    
    # デフォルトはSQLite
    if not database_url:
        database_url = "sqlite:///./punctuation_checker.db"
        print(f"[DATABASE.PY] Using default SQLite")
    
    print(f"[DATABASE.PY] Final DATABASE_URL type: {'PostgreSQL' if database_url.startswith('postgres') else 'SQLite'}")
    return database_url

DATABASE_URL = get_database_url()

# PostgreSQL用の設定
if DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://"):
    # Render環境でpostgresql://をpostgresql+psycopg2://に変換
    if DATABASE_URL.startswith("postgresql://"):
        DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1)
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    print(f"[DATABASE.PY] Created PostgreSQL engine")
else:
    # SQLite用の設定
    engine = create_engine(
        DATABASE_URL, 
        connect_args={"check_same_thread": False}
    )
    print(f"[DATABASE.PY] Created SQLite engine")
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True, index=True)
    user_key = Column(String, unique=True, index=True)  # 既存のuser_key形式
    email = Column(String, unique=True, index=True, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_seen = Column(DateTime, default=datetime.utcnow)
    
    # 利用回数管理
    daily_usage_count = Column(Integer, default=0)
    daily_usage_date = Column(String)  # YYYY-MM-DD形式
    
    # プレミアム状態
    is_premium = Column(Boolean, default=False)
    
    # ユーザー識別用フィンガープリント情報
    browser_fingerprint = Column(String, index=True)  # IP + User-Agent のハッシュ
    last_ip = Column(String)
    last_user_agent = Column(String)

class Subscription(Base):
    __tablename__ = "subscriptions"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, index=True)  # users.id への外部キー
    user_key = Column(String, index=True)  # 既存コードとの互換性
    
    # Stripe情報
    stripe_customer_id = Column(String, index=True)
    stripe_subscription_id = Column(String, unique=True, index=True)
    stripe_session_id = Column(String, index=True)
    
    # 状態管理
    plan_type = Column(String, default="premium")
    
    # ユーザー識別用フィンガープリント情報（バックアップ）
    browser_fingerprint = Column(String, index=True)  # 決済時のフィンガープリント
    payment_ip = Column(String)  # 決済時のIP
    payment_user_agent = Column(String)  # 決済時のUser-Agent
    
    # メタデータ（JSON形式）- metadataは予約語なのでmeta_dataに変更
    meta_data = Column(Text)
    
    # フラグ
    is_active = Column(Boolean, default=True)
    
    # 日時情報
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    canceled_at = Column(DateTime, nullable=True)

# テーブル作成
def create_tables():
    Base.metadata.create_all(bind=engine)

# データベース初期化（main.pyとの互換性のため）
def init_db():
    create_tables()

# データベースセッション取得
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()