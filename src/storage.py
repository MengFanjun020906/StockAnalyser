# -*- coding: utf-8 -*-
"""
===================================
A股自选股智能分析系统 - 存储层
===================================

职责：
1. 管理 SQLite 数据库连接（单例模式）
2. 定义 ORM 数据模型
3. 提供数据存取接口
4. 实现智能更新逻辑（断点续传）
"""

import atexit
from contextlib import contextmanager
import hashlib
import json
import logging
import re
import time
from datetime import datetime, date, timedelta
from typing import Optional, List, Dict, Any, TYPE_CHECKING, Tuple, Callable, TypeVar, Iterable

import pandas as pd
from sqlalchemy import (
    create_engine,
    Column,
    String,
    Float,
    Boolean,
    Date,
    DateTime,
    Integer,
    ForeignKey,
    Index,
    UniqueConstraint,
    Text,
    select,
    and_,
    or_,
    delete,
    desc,
    event,
    func,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import (
    declarative_base,
    sessionmaker,
    Session,
)
from sqlalchemy.exc import IntegrityError, OperationalError

from src.config import get_config

logger = logging.getLogger(__name__)
T = TypeVar("T")

# SQLAlchemy ORM 基类
Base = declarative_base()

if TYPE_CHECKING:
    from src.search_service import SearchResponse


# === 数据模型定义 ===

class StockDaily(Base):
    """
    股票日线数据模型
    
    存储每日行情数据和计算的技术指标
    支持多股票、多日期的唯一约束
    """
    __tablename__ = 'stock_daily'
    
    # 主键
    id = Column(Integer, primary_key=True, autoincrement=True)
    
    # 股票代码（如 600519, 000001）
    code = Column(String(10), nullable=False, index=True)
    
    # 交易日期
    date = Column(Date, nullable=False, index=True)
    
    # OHLC 数据
    open = Column(Float)
    high = Column(Float)
    low = Column(Float)
    close = Column(Float)
    
    # 成交数据
    volume = Column(Float)  # 成交量（股）
    amount = Column(Float)  # 成交额（元）
    pct_chg = Column(Float)  # 涨跌幅（%）
    
    # 技术指标
    ma5 = Column(Float)
    ma10 = Column(Float)
    ma20 = Column(Float)
    volume_ratio = Column(Float)  # 量比
    
    # 数据来源
    data_source = Column(String(50))  # 记录数据来源（如 AkshareFetcher）
    
    # 更新时间
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    
    # 唯一约束：同一股票同一日期只能有一条数据
    __table_args__ = (
        UniqueConstraint('code', 'date', name='uix_code_date'),
        Index('ix_code_date', 'code', 'date'),
    )
    
    def __repr__(self):
        return f"<StockDaily(code={self.code}, date={self.date}, close={self.close})>"
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'code': self.code,
            'date': self.date,
            'open': self.open,
            'high': self.high,
            'low': self.low,
            'close': self.close,
            'volume': self.volume,
            'amount': self.amount,
            'pct_chg': self.pct_chg,
            'ma5': self.ma5,
            'ma10': self.ma10,
            'ma20': self.ma20,
            'volume_ratio': self.volume_ratio,
            'data_source': self.data_source,
        }


class StockMinuteBar(Base):
    """股票分钟线数据缓存。

    主要服务于 Agent 入场执行回测。分钟线来自 baostock，按股票、频率、
    复权标记和分钟时间戳做 UPSERT，便于用户每天手动增量同步。
    """

    __tablename__ = 'stock_minute_bars'

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(16), nullable=False, index=True)
    baostock_code = Column(String(16), index=True)
    frequency = Column(String(8), nullable=False, default='5', index=True)
    adjustflag = Column(String(4), nullable=False, default='3', index=True)

    bar_datetime = Column(DateTime, nullable=False, index=True)
    bar_date = Column(Date, nullable=False, index=True)
    bar_time = Column(String(16))

    open = Column(Float)
    high = Column(Float)
    low = Column(Float)
    close = Column(Float)
    volume = Column(Float)
    amount = Column(Float)

    data_source = Column(String(50), default='BaostockMinute')
    fetched_at = Column(DateTime, default=datetime.now, index=True)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint('code', 'bar_datetime', 'frequency', 'adjustflag', name='uix_minute_code_dt_freq_adj'),
        Index('ix_minute_code_date_freq', 'code', 'bar_date', 'frequency'),
        Index('ix_minute_code_dt', 'code', 'bar_datetime'),
    )

    def __repr__(self):
        return f"<StockMinuteBar(code={self.code}, frequency={self.frequency}, bar_datetime={self.bar_datetime})>"

    def to_dict(self) -> Dict[str, Any]:
        return {
            'code': self.code,
            'baostock_code': self.baostock_code,
            'frequency': self.frequency,
            'adjustflag': self.adjustflag,
            'bar_datetime': self.bar_datetime.isoformat() if self.bar_datetime else None,
            'bar_date': self.bar_date.isoformat() if self.bar_date else None,
            'bar_time': self.bar_time,
            'open': self.open,
            'high': self.high,
            'low': self.low,
            'close': self.close,
            'volume': self.volume,
            'amount': self.amount,
            'data_source': self.data_source,
        }


class NewsIntel(Base):
    """
    新闻情报数据模型

    存储搜索到的新闻情报条目，用于后续分析与查询
    """
    __tablename__ = 'news_intel'

    id = Column(Integer, primary_key=True, autoincrement=True)

    # 关联用户查询操作
    query_id = Column(String(64), index=True)

    # 股票信息
    code = Column(String(10), nullable=False, index=True)
    name = Column(String(50))

    # 搜索上下文
    dimension = Column(String(32), index=True)  # latest_news / risk_check / earnings / market_analysis / industry
    query = Column(String(255))
    provider = Column(String(32), index=True)

    # 新闻内容
    title = Column(String(300), nullable=False)
    snippet = Column(Text)
    url = Column(String(1000), nullable=False)
    source = Column(String(100))
    published_date = Column(DateTime, index=True)

    # 入库时间
    fetched_at = Column(DateTime, default=datetime.now, index=True)
    query_source = Column(String(32), index=True)  # bot/web/cli/system
    requester_platform = Column(String(20))
    requester_user_id = Column(String(64))
    requester_user_name = Column(String(64))
    requester_chat_id = Column(String(64))
    requester_message_id = Column(String(64))
    requester_query = Column(String(255))

    __table_args__ = (
        UniqueConstraint('url', name='uix_news_url'),
        Index('ix_news_code_pub', 'code', 'published_date'),
    )

    def __repr__(self) -> str:
        return f"<NewsIntel(code={self.code}, title={self.title[:20]}...)>"


class RawNewsEpisode(Base):
    """Immutable-ish raw news episode used as the source of truth for signal cards."""

    __tablename__ = 'raw_news_episodes'

    id = Column(Integer, primary_key=True, autoincrement=True)
    episode_id = Column(String(80), nullable=False, unique=True, index=True)
    dedup_key = Column(String(128), nullable=False, unique=True, index=True)

    source = Column(String(80), nullable=False, index=True)
    provider = Column(String(80), index=True)
    source_id = Column(String(120), index=True)
    url = Column(String(1000))
    title = Column(String(300), nullable=False)
    summary = Column(Text)
    content = Column(Text)
    normalized_content = Column(Text)
    quality_score = Column(Float, default=0.0, index=True)
    quality_grade = Column(String(24), default='unknown', index=True)
    quality_flags_json = Column(Text)

    published_at = Column(DateTime, index=True)
    ingested_at = Column(DateTime, default=datetime.now, index=True)
    signal_date = Column(Date, nullable=False, index=True)
    session = Column(String(32), default='unknown', index=True)

    subjects_json = Column(Text)
    stocks_json = Column(Text)
    source_chain_json = Column(Text)
    raw_payload_json = Column(Text)
    status = Column(String(32), default='ok', index=True)
    errors_json = Column(Text)

    created_at = Column(DateTime, default=datetime.now, index=True)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, index=True)

    __table_args__ = (
        Index('ix_raw_news_signal_date_source', 'signal_date', 'source'),
        Index('ix_raw_news_published_source', 'published_at', 'source'),
        Index('ix_raw_news_quality_date', 'quality_grade', 'signal_date'),
    )


class NewsExtractedEvent(Base):
    """Structured event extracted from a raw news episode before card generation."""

    __tablename__ = 'news_extracted_events'

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(String(128), nullable=False, unique=True, index=True)
    raw_episode_id = Column(String(80), nullable=False, index=True)
    card_id = Column(String(96), index=True)
    signal_date = Column(Date, nullable=False, index=True)
    event_time = Column(DateTime, index=True)

    event_type = Column(String(64), nullable=False, index=True)
    trigger = Column(String(120))
    subject = Column(String(200))
    object = Column(String(300))
    direction = Column(String(32), default='neutral', index=True)
    metric_value = Column(String(120))
    evidence_sentence = Column(Text)
    source_url = Column(String(1000))
    source = Column(String(80), index=True)

    extractor = Column(String(64), default='rule_fallback', index=True)
    confidence = Column(Float, default=0.0, index=True)
    verification_status = Column(String(32), default='source_only', index=True)
    verification_sources_json = Column(Text)
    entity_links_json = Column(Text)
    diagnostics_json = Column(Text)
    status = Column(String(32), default='active', index=True)

    created_at = Column(DateTime, default=datetime.now, index=True)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, index=True)

    __table_args__ = (
        UniqueConstraint('raw_episode_id', 'event_type', 'trigger', 'evidence_sentence', name='uix_news_event_identity'),
        Index('ix_news_event_card_type', 'card_id', 'event_type'),
        Index('ix_news_event_date_type', 'signal_date', 'event_type'),
        Index('ix_news_event_verification', 'verification_status', 'confidence'),
    )


class NewsSignalCard(Base):
    """Persistent news signal card derived from one or more raw news episodes."""

    __tablename__ = 'news_signal_cards'

    id = Column(Integer, primary_key=True, autoincrement=True)
    card_id = Column(String(96), nullable=False, unique=True, index=True)
    signal_date = Column(Date, nullable=False, index=True)
    session = Column(String(32), default='unknown', index=True)
    signal_layer = Column(String(24), default='industry', index=True)

    summary_short = Column(String(300), nullable=False)
    news_tone = Column(String(24), default='neutral', index=True)
    market_impact = Column(String(24), default='unknown', index=True)
    impact_horizon = Column(String(24), default='short', index=True)
    valid_from = Column(DateTime)
    valid_until = Column(DateTime, index=True)
    decay_rule = Column(String(32), default='3d')
    refresh_trigger = Column(String(300))
    staleness_score = Column(Float, default=0.0)

    evidence_grade = Column(String(32), default='plausible', index=True)
    inference_level = Column(String(32), default='first_order', index=True)
    mapping_status = Column(String(32), default='industry_only', index=True)
    mapping_confidence = Column(Float, default=0.0)
    signal_score = Column(Float, default=0.0, index=True)
    status = Column(String(32), default='active', index=True)

    primary_industries_json = Column(Text)
    secondary_industries_json = Column(Text)
    explicit_entities_json = Column(Text)
    industry_impacts_json = Column(Text)
    company_impacts_json = Column(Text)
    transmission_paths_json = Column(Text)
    raw_episode_ids_json = Column(Text)
    source_chain_json = Column(Text)
    diagnostics_json = Column(Text)

    source_count = Column(Integer, default=0)
    graph_sync_status = Column(String(32), default='pending', index=True)
    graph_retry_count = Column(Integer, default=0)
    graph_last_error = Column(Text)
    embedding_model = Column(String(120))
    embedding_dimension = Column(Integer)
    threshold_profile = Column(String(120))

    created_at = Column(DateTime, default=datetime.now, index=True)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, index=True)

    __table_args__ = (
        Index('ix_news_signal_date_score', 'signal_date', 'signal_score'),
        Index('ix_news_signal_status_date', 'status', 'signal_date'),
        Index('ix_news_signal_layer_date', 'signal_layer', 'signal_date'),
    )


class GraphitiOutbox(Base):
    """Durable queue for asynchronous Graphiti projection work."""

    __tablename__ = 'graphiti_outbox'

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_key = Column(String(180), nullable=False, unique=True, index=True)
    event_type = Column(String(64), nullable=False, index=True)
    aggregate_id = Column(String(128), nullable=False, index=True)
    market = Column(String(24), default='cn', index=True)
    payload_json = Column(Text)
    status = Column(String(24), default='pending', index=True)
    attempt_count = Column(Integer, default=0)
    available_at = Column(DateTime, default=datetime.now, index=True)
    locked_at = Column(DateTime, index=True)
    completed_at = Column(DateTime, index=True)
    last_error = Column(Text)
    created_at = Column(DateTime, default=datetime.now, index=True)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, index=True)

    __table_args__ = (
        Index('ix_graphiti_outbox_ready', 'status', 'available_at'),
        Index('ix_graphiti_outbox_aggregate', 'event_type', 'aggregate_id'),
    )


class NewsEventSentinelRun(Base):
    """Run ledger for the news event sentinel."""

    __tablename__ = 'news_event_sentinel_runs'

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String(64), nullable=False, unique=True, index=True)
    started_at = Column(DateTime, nullable=False, index=True)
    finished_at = Column(DateTime, index=True)
    status = Column(String(32), default='running', index=True)
    watched_symbol_count = Column(Integer, default=0)
    source_query_count = Column(Integer, default=0)
    fetched_count = Column(Integer, default=0)
    unseen_count = Column(Integer, default=0)
    raw_episode_count = Column(Integer, default=0)
    card_count = Column(Integer, default=0)
    trigger_count = Column(Integer, default=0)
    suppressed_by_cooldown = Column(Integer, default=0)
    errors_json = Column(Text)
    diagnostics_json = Column(Text)
    created_at = Column(DateTime, default=datetime.now, index=True)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, index=True)

    __table_args__ = (
        Index('ix_news_event_sentinel_run_status_started', 'status', 'started_at'),
    )


class NewsEventSentinelTrigger(Base):
    """Trigger ledger for dedupe, cooldown and notification audit."""

    __tablename__ = 'news_event_sentinel_triggers'

    id = Column(Integer, primary_key=True, autoincrement=True)
    trigger_id = Column(String(96), nullable=False, unique=True, index=True)
    run_id = Column(String(64), nullable=False, index=True)
    card_id = Column(String(96), nullable=False, index=True)
    event_id = Column(String(128), index=True)
    canonical_symbol = Column(String(32), nullable=False, index=True)
    event_type = Column(String(64), default='unknown', index=True)
    direction = Column(String(32), default='neutral', index=True)
    severity = Column(String(24), default='low', index=True)
    cooldown_key = Column(String(128), nullable=False, index=True)
    triggered_at = Column(DateTime, nullable=False, index=True)
    notification_status = Column(String(32), default='pending', index=True)
    trace_status = Column(String(32), default='skipped', index=True)
    notification_payload_json = Column(Text)
    diagnostics_json = Column(Text)
    created_at = Column(DateTime, default=datetime.now, index=True)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, index=True)

    __table_args__ = (
        Index('ix_news_event_sentinel_cooldown', 'cooldown_key', 'triggered_at'),
        Index('ix_news_event_sentinel_symbol_time', 'canonical_symbol', 'triggered_at'),
        Index('ix_news_event_sentinel_run_symbol', 'run_id', 'canonical_symbol'),
    )


class NewsSignalFeedback(Base):
    """User feedback overlay for a news signal card."""

    __tablename__ = 'news_signal_feedback'

    id = Column(Integer, primary_key=True, autoincrement=True)
    card_id = Column(String(96), nullable=False, index=True)
    feedback_type = Column(String(32), nullable=False, index=True)
    note = Column(Text)
    payload_json = Column(Text)
    user_id = Column(String(80), index=True)
    created_at = Column(DateTime, default=datetime.now, index=True)

    __table_args__ = (
        Index('ix_news_signal_feedback_card_type', 'card_id', 'feedback_type'),
    )


class NewsSignalSeedLink(Base):
    """Link between a news signal card and a seed pool item."""

    __tablename__ = 'news_signal_seed_links'

    id = Column(Integer, primary_key=True, autoincrement=True)
    card_id = Column(String(96), nullable=False, index=True)
    seed_item_id = Column(Integer, ForeignKey('selection_seed_pool_items.id'), index=True)
    source_desk = Column(String(64), index=True)
    gate_result = Column(String(32), default='unknown', index=True)
    signal_score_snapshot = Column(Float)
    mapping_confidence = Column(Float)
    evidence_grade = Column(String(32))
    created_at = Column(DateTime, default=datetime.now, index=True)

    __table_args__ = (
        UniqueConstraint('card_id', 'seed_item_id', 'source_desk', name='uix_news_signal_seed_link'),
    )


class NewsSignalOutcome(Base):
    """Post-hoc outcome projection for card-linked seed items."""

    __tablename__ = 'news_signal_outcomes'

    id = Column(Integer, primary_key=True, autoincrement=True)
    card_id = Column(String(96), nullable=False, index=True)
    seed_item_id = Column(Integer, ForeignKey('selection_seed_pool_items.id'), index=True)
    evaluation_date = Column(Date, nullable=False, index=True)
    alpha_return_pct = Column(Float)
    mfe_pct = Column(Float)
    mae_pct = Column(Float)
    liquidity_status = Column(String(32), default='UNKNOWN', index=True)
    data_status = Column(String(24), default='pending', index=True)
    created_at = Column(DateTime, default=datetime.now, index=True)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, index=True)

    __table_args__ = (
        UniqueConstraint('card_id', 'seed_item_id', 'evaluation_date', name='uix_news_signal_outcome_card_seed_date'),
        Index('ix_news_signal_outcome_card_status', 'card_id', 'data_status'),
    )


class NewsSignalEdge(Base):
    """Generated relation edge for news signal cards."""

    __tablename__ = 'news_signal_edges'

    id = Column(Integer, primary_key=True, autoincrement=True)
    edge_id = Column(String(128), nullable=False, unique=True, index=True)
    source_card_id = Column(String(96), nullable=False, index=True)
    target_card_id = Column(String(96), index=True)
    target_type = Column(String(32), nullable=False, index=True)
    target_id = Column(String(128), nullable=False, index=True)
    edge_class = Column(String(32), nullable=False, index=True)
    edge_type = Column(String(64), nullable=False, index=True)
    weight = Column(Float, default=0.0, index=True)
    edge_quality = Column(Float, default=0.0, index=True)
    quality_grade = Column(String(24), default='unknown', index=True)
    quality_flags_json = Column(Text)
    method = Column(String(32), default='rule', index=True)
    rationale = Column(Text)
    evidence_json = Column(Text)
    embedding_model = Column(String(120))
    threshold_profile = Column(String(120))
    decay_rule = Column(String(32), default='none')
    status = Column(String(32), default='active', index=True)
    created_at = Column(DateTime, default=datetime.now, index=True)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, index=True)

    __table_args__ = (
        UniqueConstraint('source_card_id', 'target_type', 'target_id', 'edge_type', name='uix_news_signal_edge_identity'),
        Index('ix_news_signal_edge_source_class', 'source_card_id', 'edge_class'),
        Index('ix_news_signal_edge_target_card', 'target_card_id', 'edge_class'),
        Index('ix_news_signal_edge_type_weight', 'edge_type', 'weight'),
        Index('ix_news_signal_edge_quality', 'quality_grade', 'edge_quality'),
    )


class FundamentalSnapshot(Base):
    """
    基本面上下文快照（P0 write-only）。

    仅用于写入，主链路不依赖读取该表，便于后续回测/画像扩展。
    """
    __tablename__ = 'fundamental_snapshot'

    id = Column(Integer, primary_key=True, autoincrement=True)
    query_id = Column(String(64), nullable=False, index=True)
    code = Column(String(10), nullable=False, index=True)
    payload = Column(Text, nullable=False)
    source_chain = Column(Text)
    coverage = Column(Text)
    created_at = Column(DateTime, default=datetime.now, index=True)

    __table_args__ = (
        Index('ix_fundamental_snapshot_query_code', 'query_id', 'code'),
        Index('ix_fundamental_snapshot_created', 'created_at'),
    )

    def __repr__(self) -> str:
        return f"<FundamentalSnapshot(query_id={self.query_id}, code={self.code})>"


class AnalysisHistory(Base):
    """
    分析结果历史记录模型

    保存每次分析结果，支持按 query_id/股票代码检索
    """
    __tablename__ = 'analysis_history'

    id = Column(Integer, primary_key=True, autoincrement=True)

    # 关联查询链路
    query_id = Column(String(64), index=True)

    # 股票信息
    code = Column(String(10), nullable=False, index=True)
    name = Column(String(50))
    report_type = Column(String(16), index=True)

    # 核心结论
    sentiment_score = Column(Integer)
    operation_advice = Column(String(20))
    trend_prediction = Column(String(50))
    analysis_summary = Column(Text)

    # 详细数据
    raw_result = Column(Text)
    news_content = Column(Text)
    context_snapshot = Column(Text)

    # 狙击点位（用于回测）
    ideal_buy = Column(Float)
    secondary_buy = Column(Float)
    stop_loss = Column(Float)
    take_profit = Column(Float)

    created_at = Column(DateTime, default=datetime.now, index=True)

    __table_args__ = (
        Index('ix_analysis_code_time', 'code', 'created_at'),
    )

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'id': self.id,
            'query_id': self.query_id,
            'code': self.code,
            'name': self.name,
            'report_type': self.report_type,
            'sentiment_score': self.sentiment_score,
            'operation_advice': self.operation_advice,
            'trend_prediction': self.trend_prediction,
            'analysis_summary': self.analysis_summary,
            'raw_result': self.raw_result,
            'news_content': self.news_content,
            'context_snapshot': self.context_snapshot,
            'ideal_buy': self.ideal_buy,
            'secondary_buy': self.secondary_buy,
            'stop_loss': self.stop_loss,
            'take_profit': self.take_profit,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class BacktestResult(Base):
    """单条分析记录的回测结果。"""

    __tablename__ = 'backtest_results'

    id = Column(Integer, primary_key=True, autoincrement=True)

    analysis_history_id = Column(
        Integer,
        ForeignKey('analysis_history.id'),
        nullable=False,
        index=True,
    )

    # 冗余字段，便于按股票筛选
    code = Column(String(10), nullable=False, index=True)
    analysis_date = Column(Date, index=True)

    # 回测参数
    eval_window_days = Column(Integer, nullable=False, default=10)
    engine_version = Column(String(16), nullable=False, default='v1')

    # 状态
    eval_status = Column(String(16), nullable=False, default='pending')
    evaluated_at = Column(DateTime, default=datetime.now, index=True)

    # 建议快照（避免未来分析字段变化导致回测不可解释）
    operation_advice = Column(String(20))
    position_recommendation = Column(String(8))  # long/cash

    # 价格与收益
    start_price = Column(Float)
    end_close = Column(Float)
    max_high = Column(Float)
    min_low = Column(Float)
    stock_return_pct = Column(Float)

    # 方向与结果
    direction_expected = Column(String(16))  # up/down/flat/not_down
    direction_correct = Column(Boolean, nullable=True)
    outcome = Column(String(16))  # win/loss/neutral

    # 目标价命中（仅 long 且配置了止盈/止损时有意义）
    stop_loss = Column(Float)
    take_profit = Column(Float)
    hit_stop_loss = Column(Boolean)
    hit_take_profit = Column(Boolean)
    first_hit = Column(String(16))  # take_profit/stop_loss/ambiguous/neither/not_applicable
    first_hit_date = Column(Date)
    first_hit_trading_days = Column(Integer)

    # 模拟执行（long-only）
    simulated_entry_price = Column(Float)
    simulated_exit_price = Column(Float)
    simulated_exit_reason = Column(String(24))  # stop_loss/take_profit/window_end/cash/ambiguous_stop_loss
    simulated_return_pct = Column(Float)

    __table_args__ = (
        UniqueConstraint(
            'analysis_history_id',
            'eval_window_days',
            'engine_version',
            name='uix_backtest_analysis_window_version',
        ),
        Index('ix_backtest_code_date', 'code', 'analysis_date'),
    )


class BacktestSummary(Base):
    """回测汇总指标（按股票或全局）。"""

    __tablename__ = 'backtest_summaries'

    id = Column(Integer, primary_key=True, autoincrement=True)

    scope = Column(String(16), nullable=False, index=True)  # overall/stock
    code = Column(String(16), index=True)

    eval_window_days = Column(Integer, nullable=False, default=10)
    engine_version = Column(String(16), nullable=False, default='v1')
    computed_at = Column(DateTime, default=datetime.now, index=True)

    # 计数
    total_evaluations = Column(Integer, default=0)
    completed_count = Column(Integer, default=0)
    insufficient_count = Column(Integer, default=0)
    long_count = Column(Integer, default=0)
    cash_count = Column(Integer, default=0)

    win_count = Column(Integer, default=0)
    loss_count = Column(Integer, default=0)
    neutral_count = Column(Integer, default=0)

    # 准确率/胜率
    direction_accuracy_pct = Column(Float)
    win_rate_pct = Column(Float)
    neutral_rate_pct = Column(Float)

    # 收益
    avg_stock_return_pct = Column(Float)
    avg_simulated_return_pct = Column(Float)

    # 目标价触发统计（仅 long 且配置止盈/止损时统计）
    stop_loss_trigger_rate = Column(Float)
    take_profit_trigger_rate = Column(Float)
    ambiguous_rate = Column(Float)
    avg_days_to_first_hit = Column(Float)

    # 诊断字段（JSON 字符串）
    advice_breakdown_json = Column(Text)
    diagnostics_json = Column(Text)

    __table_args__ = (
        UniqueConstraint(
            'scope',
            'code',
            'eval_window_days',
            'engine_version',
            name='uix_backtest_summary_scope_code_window_version',
        ),
    )


class SelectionSeedPoolSnapshot(Base):
    """One persisted seed-pool generation event."""

    __tablename__ = 'selection_seed_pool_snapshots'

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String(64), nullable=False, index=True)
    trace_id = Column(String(128), nullable=False, index=True)
    seed_date = Column(Date, nullable=False, index=True)
    generated_at = Column(DateTime, default=datetime.now, index=True)
    market = Column(String(16), nullable=False, default='cn', index=True)
    candidate_discovery_mode = Column(String(64), index=True)
    seed_count = Column(Integer, default=0)
    status = Column(String(16), default='ok', index=True)
    error = Column(Text)
    source_summary_json = Column(Text)
    diagnostics_json = Column(Text)
    created_at = Column(DateTime, default=datetime.now, index=True)

    __table_args__ = (
        UniqueConstraint('run_id', 'trace_id', 'seed_date', name='uix_seed_pool_snapshot_run_trace_date'),
        Index('ix_seed_pool_snapshot_seed_date', 'seed_date', 'generated_at'),
    )


class SelectionSeedPoolItem(Base):
    """One stock in a persisted seed-pool snapshot."""

    __tablename__ = 'selection_seed_pool_items'

    id = Column(Integer, primary_key=True, autoincrement=True)
    snapshot_id = Column(Integer, ForeignKey('selection_seed_pool_snapshots.id'), nullable=False, index=True)
    code = Column(String(16), nullable=False, index=True)
    name = Column(String(64))
    market = Column(String(16), nullable=False, default='cn', index=True)
    source = Column(String(64), index=True)
    source_diagnostics_json = Column(Text)
    trigger_signals_json = Column(Text)
    catalyst_tags_json = Column(Text)
    catalyst_tier = Column(Integer, default=0, index=True)
    entry_reason = Column(Text)
    freshness = Column(String(64))
    seed_order = Column(Integer, default=0)
    entered_deep_dive = Column(Boolean, default=False, index=True)
    entered_final_report = Column(Boolean, default=False, index=True)
    raw_payload_json = Column(Text)
    created_at = Column(DateTime, default=datetime.now, index=True)

    __table_args__ = (
        UniqueConstraint('snapshot_id', 'code', name='uix_seed_pool_item_snapshot_code'),
        Index('ix_seed_pool_item_snapshot_order', 'snapshot_id', 'seed_order'),
    )


class SelectionSeedPoolDeskOutcome(Base):
    """Per-seed thesis-desk processing result."""

    __tablename__ = 'selection_seed_pool_desk_outcomes'

    id = Column(Integer, primary_key=True, autoincrement=True)
    item_id = Column(Integer, ForeignKey('selection_seed_pool_items.id'), nullable=False, index=True)
    desk = Column(String(64), nullable=False, index=True)
    status = Column(String(24), default='missing', index=True)
    stance = Column(String(24), default='missing', index=True)
    decision = Column(String(24), default='not_evaluated', index=True)
    reason = Column(Text)
    risks_json = Column(Text)
    evidence_json = Column(Text)
    metrics_json = Column(Text)
    errors_json = Column(Text)
    elapsed_ms = Column(Integer)
    created_at = Column(DateTime, default=datetime.now, index=True)

    __table_args__ = (
        UniqueConstraint('item_id', 'desk', name='uix_seed_pool_outcome_item_desk'),
    )


class SelectionSeedPoolEvaluation(Base):
    """Post-hoc T+1 performance evaluation for a seed item."""

    __tablename__ = 'selection_seed_pool_evaluations'

    id = Column(Integer, primary_key=True, autoincrement=True)
    item_id = Column(Integer, ForeignKey('selection_seed_pool_items.id'), nullable=False, index=True)
    evaluation_date = Column(Date, nullable=False, index=True)
    seed_close = Column(Float)
    evaluation_open = Column(Float)
    evaluation_high = Column(Float)
    evaluation_low = Column(Float)
    evaluation_close = Column(Float)
    next_close_return_pct = Column(Float)
    benchmark_code = Column(String(16), default='000001.SH')
    benchmark_return_pct = Column(Float)
    alpha_return_pct = Column(Float)
    mfe_pct = Column(Float)
    mae_pct = Column(Float)
    liquidity_status = Column(String(32), default='UNKNOWN', index=True)
    data_status = Column(String(24), default='pending', index=True)
    error = Column(Text)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, index=True)

    __table_args__ = (
        UniqueConstraint('item_id', 'evaluation_date', name='uix_seed_pool_eval_item_date'),
        Index('ix_seed_pool_eval_status_liquidity', 'data_status', 'liquidity_status'),
    )


class PortfolioAccount(Base):
    """Portfolio account metadata."""

    __tablename__ = 'portfolio_accounts'

    id = Column(Integer, primary_key=True, autoincrement=True)
    owner_id = Column(String(64), index=True)
    name = Column(String(64), nullable=False)
    broker = Column(String(64))
    market = Column(String(8), nullable=False, default='cn', index=True)  # cn/hk/us
    base_currency = Column(String(8), nullable=False, default='CNY')
    is_active = Column(Boolean, nullable=False, default=True, index=True)
    created_at = Column(DateTime, default=datetime.now, index=True)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        Index('ix_portfolio_account_owner_active', 'owner_id', 'is_active'),
    )


class PortfolioTrade(Base):
    """Executed trade events used as the source of truth for replay."""

    __tablename__ = 'portfolio_trades'

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, ForeignKey('portfolio_accounts.id'), nullable=False, index=True)
    trade_uid = Column(String(128))
    symbol = Column(String(16), nullable=False, index=True)
    market = Column(String(8), nullable=False, default='cn')
    currency = Column(String(8), nullable=False, default='CNY')
    trade_date = Column(Date, nullable=False, index=True)
    side = Column(String(8), nullable=False)  # buy/sell
    quantity = Column(Float, nullable=False)
    price = Column(Float, nullable=False)
    fee = Column(Float, default=0.0)
    tax = Column(Float, default=0.0)
    note = Column(String(255))
    dedup_hash = Column(String(64), index=True)
    created_at = Column(DateTime, default=datetime.now, index=True)

    __table_args__ = (
        UniqueConstraint('account_id', 'trade_uid', name='uix_portfolio_trade_uid'),
        UniqueConstraint('account_id', 'dedup_hash', name='uix_portfolio_trade_dedup_hash'),
        Index('ix_portfolio_trade_account_date', 'account_id', 'trade_date'),
    )


class PortfolioCashLedger(Base):
    """Cash in/out events."""

    __tablename__ = 'portfolio_cash_ledger'

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, ForeignKey('portfolio_accounts.id'), nullable=False, index=True)
    event_date = Column(Date, nullable=False, index=True)
    direction = Column(String(8), nullable=False)  # in/out
    amount = Column(Float, nullable=False)
    currency = Column(String(8), nullable=False, default='CNY')
    note = Column(String(255))
    created_at = Column(DateTime, default=datetime.now, index=True)

    __table_args__ = (
        Index('ix_portfolio_cash_account_date', 'account_id', 'event_date'),
    )


class PortfolioCorporateAction(Base):
    """Corporate actions that impact cash or share quantity."""

    __tablename__ = 'portfolio_corporate_actions'

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, ForeignKey('portfolio_accounts.id'), nullable=False, index=True)
    symbol = Column(String(16), nullable=False, index=True)
    market = Column(String(8), nullable=False, default='cn')
    currency = Column(String(8), nullable=False, default='CNY')
    effective_date = Column(Date, nullable=False, index=True)
    action_type = Column(String(24), nullable=False)  # cash_dividend/split_adjustment
    cash_dividend_per_share = Column(Float)
    split_ratio = Column(Float)
    note = Column(String(255))
    created_at = Column(DateTime, default=datetime.now, index=True)

    __table_args__ = (
        Index('ix_portfolio_ca_account_date', 'account_id', 'effective_date'),
    )


class PortfolioPosition(Base):
    """Latest replayed position snapshot for each symbol in one account."""

    __tablename__ = 'portfolio_positions'

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, ForeignKey('portfolio_accounts.id'), nullable=False, index=True)
    cost_method = Column(String(8), nullable=False, default='fifo')
    symbol = Column(String(16), nullable=False, index=True)
    market = Column(String(8), nullable=False, default='cn')
    currency = Column(String(8), nullable=False, default='CNY')
    quantity = Column(Float, nullable=False, default=0.0)
    avg_cost = Column(Float, nullable=False, default=0.0)
    total_cost = Column(Float, nullable=False, default=0.0)
    last_price = Column(Float, nullable=False, default=0.0)
    market_value_base = Column(Float, nullable=False, default=0.0)
    unrealized_pnl_base = Column(Float, nullable=False, default=0.0)
    valuation_currency = Column(String(8), nullable=False, default='CNY')
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, index=True)

    __table_args__ = (
        UniqueConstraint(
            'account_id',
            'symbol',
            'market',
            'currency',
            'cost_method',
            name='uix_portfolio_position_account_symbol_market_currency',
        ),
    )


class PortfolioPositionLot(Base):
    """Lot-level remaining quantities used by FIFO replay."""

    __tablename__ = 'portfolio_position_lots'

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, ForeignKey('portfolio_accounts.id'), nullable=False, index=True)
    cost_method = Column(String(8), nullable=False, default='fifo')
    symbol = Column(String(16), nullable=False, index=True)
    market = Column(String(8), nullable=False, default='cn')
    currency = Column(String(8), nullable=False, default='CNY')
    open_date = Column(Date, nullable=False, index=True)
    remaining_quantity = Column(Float, nullable=False, default=0.0)
    unit_cost = Column(Float, nullable=False, default=0.0)
    source_trade_id = Column(Integer, ForeignKey('portfolio_trades.id'))
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, index=True)

    __table_args__ = (
        Index('ix_portfolio_lot_account_symbol', 'account_id', 'symbol'),
    )


class PortfolioDailySnapshot(Base):
    """Daily account snapshot generated by read-time replay."""

    __tablename__ = 'portfolio_daily_snapshots'

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, ForeignKey('portfolio_accounts.id'), nullable=False, index=True)
    snapshot_date = Column(Date, nullable=False, index=True)
    cost_method = Column(String(8), nullable=False, default='fifo')  # fifo/avg
    base_currency = Column(String(8), nullable=False, default='CNY')
    total_cash = Column(Float, nullable=False, default=0.0)
    total_market_value = Column(Float, nullable=False, default=0.0)
    total_equity = Column(Float, nullable=False, default=0.0)
    unrealized_pnl = Column(Float, nullable=False, default=0.0)
    realized_pnl = Column(Float, nullable=False, default=0.0)
    fee_total = Column(Float, nullable=False, default=0.0)
    tax_total = Column(Float, nullable=False, default=0.0)
    fx_stale = Column(Boolean, nullable=False, default=False)
    payload = Column(Text)
    created_at = Column(DateTime, default=datetime.now, index=True)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint(
            'account_id',
            'snapshot_date',
            'cost_method',
            name='uix_portfolio_snapshot_account_date_method',
        ),
    )


class PortfolioFxRate(Base):
    """Cached FX rates used for cross-currency portfolio conversion."""

    __tablename__ = 'portfolio_fx_rates'

    id = Column(Integer, primary_key=True, autoincrement=True)
    from_currency = Column(String(8), nullable=False, index=True)
    to_currency = Column(String(8), nullable=False, index=True)
    rate_date = Column(Date, nullable=False, index=True)
    rate = Column(Float, nullable=False)
    source = Column(String(32), nullable=False, default='manual')
    is_stale = Column(Boolean, nullable=False, default=False)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint(
            'from_currency',
            'to_currency',
            'rate_date',
            name='uix_portfolio_fx_pair_date',
        ),
    )


class ConversationMessage(Base):
    """
    Agent 对话历史记录表
    """
    __tablename__ = 'conversation_messages'

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String(100), index=True, nullable=False)
    role = Column(String(20), nullable=False)  # user, assistant, system
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.now, index=True)


class LLMUsage(Base):
    """One row per litellm.completion() call — token-usage audit log."""

    __tablename__ = 'llm_usage'

    id = Column(Integer, primary_key=True, autoincrement=True)
    # 'analysis' | 'agent' | 'market_review'
    call_type = Column(String(32), nullable=False, index=True)
    model = Column(String(128), nullable=False)
    stock_code = Column(String(16), nullable=True)
    prompt_tokens = Column(Integer, nullable=False, default=0)
    completion_tokens = Column(Integer, nullable=False, default=0)
    total_tokens = Column(Integer, nullable=False, default=0)
    called_at = Column(DateTime, default=datetime.now, index=True)


class MarketRegimeState(Base):
    """Persisted market-regime state for damping and confirmation."""

    __tablename__ = 'market_regime_state'

    id = Column(Integer, primary_key=True, autoincrement=True)
    market = Column(String(16), nullable=False, index=True)
    as_of = Column(Date, nullable=True, index=True)
    regime = Column(String(32), nullable=False)
    raw_regime = Column(String(32))
    volatility_bucket = Column(String(32))
    raw_volatility_bucket = Column(String(32))
    pending_regime = Column(String(32))
    pending_count = Column(Integer, nullable=False, default=0)
    payload = Column(Text, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, index=True)

    __table_args__ = (
        UniqueConstraint('market', name='uix_market_regime_state_market'),
        Index('ix_market_regime_state_market_updated', 'market', 'updated_at'),
    )

    def to_dict(self) -> Dict[str, Any]:
        try:
            payload = json.loads(self.payload or "{}")
        except Exception:
            payload = {}
        return {
            "market": self.market,
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "regime": self.regime,
            "raw_regime": self.raw_regime,
            "volatility_bucket": self.volatility_bucket,
            "raw_volatility_bucket": self.raw_volatility_bucket,
            "pending_regime": self.pending_regime,
            "pending_count": self.pending_count,
            "payload": payload,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class DatabaseManager:
    """
    数据库管理器 - 单例模式
    
    职责：
    1. 管理数据库连接池
    2. 提供 Session 上下文管理
    3. 封装数据存取操作
    """
    
    _instance: Optional['DatabaseManager'] = None
    _initialized: bool = False
    
    def __new__(cls, *args, **kwargs):
        """单例模式实现"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self, db_url: Optional[str] = None):
        """
        初始化数据库管理器
        
        Args:
            db_url: 数据库连接 URL（可选，默认从配置读取）
        """
        if getattr(self, '_initialized', False):
            return

        config = get_config()
        if db_url is None:
            db_url = config.get_db_url()

        self._db_url = db_url
        self._sqlite_wal_enabled = config.sqlite_wal_enabled
        self._sqlite_busy_timeout_ms = config.sqlite_busy_timeout_ms
        self._sqlite_write_retry_max = config.sqlite_write_retry_max
        self._sqlite_write_retry_base_delay = config.sqlite_write_retry_base_delay

        engine_kwargs = {
            "echo": False,
            "pool_pre_ping": True,
        }
        if str(db_url).startswith("sqlite:") and self._sqlite_busy_timeout_ms > 0:
            engine_kwargs["connect_args"] = {
                "timeout": self._sqlite_busy_timeout_ms / 1000,
            }

        # 创建数据库引擎
        self._engine = create_engine(
            db_url,
            **engine_kwargs,
        )
        self._is_sqlite_engine = self._engine.url.get_backend_name() == 'sqlite'
        self._sqlite_file_db = self._is_sqlite_engine and self._is_file_sqlite_database()
        self._install_sqlite_pragma_handler()
        
        # 创建 Session 工厂
        self._SessionLocal = sessionmaker(
            bind=self._engine,
            autocommit=False,
            autoflush=False,
        )
        
        # 创建所有表
        Base.metadata.create_all(self._engine)
        self._run_schema_migrations()

        self._initialized = True
        logger.info(f"数据库初始化完成: {db_url}")

        # 注册退出钩子，确保程序退出时关闭数据库连接
        atexit.register(DatabaseManager._cleanup_engine, self._engine)
    
    @classmethod
    def get_instance(cls) -> 'DatabaseManager':
        """获取单例实例"""
        if cls._instance is None or not getattr(cls._instance, '_initialized', False):
            cls._instance = cls()
        return cls._instance
    
    @classmethod
    def reset_instance(cls) -> None:
        """重置单例（用于测试）"""
        if cls._instance is not None:
            if hasattr(cls._instance, '_engine') and cls._instance._engine is not None:
                cls._instance._engine.dispose()
            cls._instance._initialized = False
            cls._instance = None

    @classmethod
    def _cleanup_engine(cls, engine) -> None:
        """
        清理数据库引擎（atexit 钩子）

        确保程序退出时关闭所有数据库连接，避免 ResourceWarning

        Args:
            engine: SQLAlchemy 引擎对象
        """
        try:
            if engine is not None:
                engine.dispose()
                logger.debug("数据库引擎已清理")
        except Exception as e:
            logger.warning(f"清理数据库引擎时出错: {e}")

    def _install_sqlite_pragma_handler(self) -> None:
        """为 SQLite 连接安装竞争保护参数。"""
        if not self._is_sqlite_engine:
            return

        @event.listens_for(self._engine, "connect")
        def _configure_sqlite_connection(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute(f"PRAGMA busy_timeout={int(self._sqlite_busy_timeout_ms)}")
                if self._sqlite_file_db and self._sqlite_wal_enabled:
                    cursor.execute("PRAGMA journal_mode=WAL")
            except Exception as exc:
                logger.warning("初始化 SQLite PRAGMA 失败: %s", exc)
            finally:
                cursor.close()

    def _is_file_sqlite_database(self) -> bool:
        database = (self._engine.url.database or "").strip()
        return bool(database) and database.lower() != ":memory:"

    def _run_schema_migrations(self) -> None:
        if not self._is_sqlite_engine:
            return
        self._migrate_raw_news_episode_quality()
        self._migrate_news_extracted_events()
        self._migrate_news_signal_edge_quality()
        self._migrate_news_signal_card_layer()
        self._migrate_graphiti_outbox()
        self._migrate_news_event_sentinel()
        self._migrate_seed_pool_snapshot_unique_key()

    def _migrate_news_event_sentinel(self) -> None:
        with self._engine.begin() as conn:
            conn.exec_driver_sql(
                """
                CREATE TABLE IF NOT EXISTS news_event_sentinel_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id VARCHAR(64) NOT NULL UNIQUE,
                    started_at DATETIME NOT NULL,
                    finished_at DATETIME,
                    status VARCHAR(32) DEFAULT 'running',
                    watched_symbol_count INTEGER DEFAULT 0,
                    source_query_count INTEGER DEFAULT 0,
                    fetched_count INTEGER DEFAULT 0,
                    unseen_count INTEGER DEFAULT 0,
                    raw_episode_count INTEGER DEFAULT 0,
                    card_count INTEGER DEFAULT 0,
                    trigger_count INTEGER DEFAULT 0,
                    suppressed_by_cooldown INTEGER DEFAULT 0,
                    errors_json TEXT,
                    diagnostics_json TEXT,
                    created_at DATETIME,
                    updated_at DATETIME
                )
                """
            )
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_news_event_sentinel_runs_run_id ON news_event_sentinel_runs (run_id)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_news_event_sentinel_runs_started_at ON news_event_sentinel_runs (started_at)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_news_event_sentinel_runs_status ON news_event_sentinel_runs (status)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_news_event_sentinel_run_status_started ON news_event_sentinel_runs (status, started_at)")

            conn.exec_driver_sql(
                """
                CREATE TABLE IF NOT EXISTS news_event_sentinel_triggers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trigger_id VARCHAR(96) NOT NULL UNIQUE,
                    run_id VARCHAR(64) NOT NULL,
                    card_id VARCHAR(96) NOT NULL,
                    event_id VARCHAR(128),
                    canonical_symbol VARCHAR(32) NOT NULL,
                    event_type VARCHAR(64) DEFAULT 'unknown',
                    direction VARCHAR(32) DEFAULT 'neutral',
                    severity VARCHAR(24) DEFAULT 'low',
                    cooldown_key VARCHAR(128) NOT NULL,
                    triggered_at DATETIME NOT NULL,
                    notification_status VARCHAR(32) DEFAULT 'pending',
                    trace_status VARCHAR(32) DEFAULT 'skipped',
                    notification_payload_json TEXT,
                    diagnostics_json TEXT,
                    created_at DATETIME,
                    updated_at DATETIME
                )
                """
            )
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_news_event_sentinel_triggers_trigger_id ON news_event_sentinel_triggers (trigger_id)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_news_event_sentinel_triggers_run_id ON news_event_sentinel_triggers (run_id)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_news_event_sentinel_triggers_card_id ON news_event_sentinel_triggers (card_id)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_news_event_sentinel_triggers_event_id ON news_event_sentinel_triggers (event_id)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_news_event_sentinel_triggers_canonical_symbol ON news_event_sentinel_triggers (canonical_symbol)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_news_event_sentinel_triggers_event_type ON news_event_sentinel_triggers (event_type)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_news_event_sentinel_triggers_direction ON news_event_sentinel_triggers (direction)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_news_event_sentinel_triggers_severity ON news_event_sentinel_triggers (severity)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_news_event_sentinel_triggers_cooldown_key ON news_event_sentinel_triggers (cooldown_key)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_news_event_sentinel_triggers_triggered_at ON news_event_sentinel_triggers (triggered_at)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_news_event_sentinel_triggers_notification_status ON news_event_sentinel_triggers (notification_status)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_news_event_sentinel_triggers_trace_status ON news_event_sentinel_triggers (trace_status)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_news_event_sentinel_cooldown ON news_event_sentinel_triggers (cooldown_key, triggered_at)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_news_event_sentinel_symbol_time ON news_event_sentinel_triggers (canonical_symbol, triggered_at)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_news_event_sentinel_run_symbol ON news_event_sentinel_triggers (run_id, canonical_symbol)")

    def _migrate_graphiti_outbox(self) -> None:
        with self._engine.begin() as conn:
            conn.exec_driver_sql(
                """
                CREATE TABLE IF NOT EXISTS graphiti_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_key VARCHAR(180) NOT NULL UNIQUE,
                    event_type VARCHAR(64) NOT NULL,
                    aggregate_id VARCHAR(128) NOT NULL,
                    market VARCHAR(24) DEFAULT 'cn',
                    payload_json TEXT,
                    status VARCHAR(24) DEFAULT 'pending',
                    attempt_count INTEGER DEFAULT 0,
                    available_at DATETIME,
                    locked_at DATETIME,
                    completed_at DATETIME,
                    last_error TEXT,
                    created_at DATETIME,
                    updated_at DATETIME
                )
                """
            )
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_graphiti_outbox_event_key ON graphiti_outbox (event_key)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_graphiti_outbox_event_type ON graphiti_outbox (event_type)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_graphiti_outbox_aggregate_id ON graphiti_outbox (aggregate_id)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_graphiti_outbox_market ON graphiti_outbox (market)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_graphiti_outbox_status ON graphiti_outbox (status)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_graphiti_outbox_available_at ON graphiti_outbox (available_at)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_graphiti_outbox_locked_at ON graphiti_outbox (locked_at)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_graphiti_outbox_completed_at ON graphiti_outbox (completed_at)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_graphiti_outbox_created_at ON graphiti_outbox (created_at)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_graphiti_outbox_updated_at ON graphiti_outbox (updated_at)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_graphiti_outbox_ready ON graphiti_outbox (status, available_at)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_graphiti_outbox_aggregate ON graphiti_outbox (event_type, aggregate_id)")

    def _migrate_raw_news_episode_quality(self) -> None:
        with self._engine.begin() as conn:
            table = conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='raw_news_episodes'"
            ).fetchone()
            if table is None:
                return
            self._add_sqlite_column_if_missing(
                conn,
                "raw_news_episodes",
                "normalized_content",
                "ALTER TABLE raw_news_episodes ADD COLUMN normalized_content TEXT",
            )
            self._add_sqlite_column_if_missing(
                conn,
                "raw_news_episodes",
                "quality_score",
                "ALTER TABLE raw_news_episodes ADD COLUMN quality_score FLOAT DEFAULT 0.0",
            )
            self._add_sqlite_column_if_missing(
                conn,
                "raw_news_episodes",
                "quality_grade",
                "ALTER TABLE raw_news_episodes ADD COLUMN quality_grade VARCHAR(24) DEFAULT 'unknown'",
            )
            self._add_sqlite_column_if_missing(
                conn,
                "raw_news_episodes",
                "quality_flags_json",
                "ALTER TABLE raw_news_episodes ADD COLUMN quality_flags_json TEXT",
            )
            conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_raw_news_quality_date ON raw_news_episodes (quality_grade, signal_date)"
            )

    def _migrate_news_extracted_events(self) -> None:
        with self._engine.begin() as conn:
            conn.exec_driver_sql(
                """
                CREATE TABLE IF NOT EXISTS news_extracted_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id VARCHAR(128) NOT NULL,
                    raw_episode_id VARCHAR(80) NOT NULL,
                    card_id VARCHAR(96),
                    signal_date DATE NOT NULL,
                    event_time DATETIME,
                    event_type VARCHAR(64) NOT NULL,
                    trigger VARCHAR(120),
                    subject VARCHAR(200),
                    object VARCHAR(300),
                    direction VARCHAR(32) DEFAULT 'neutral',
                    metric_value VARCHAR(120),
                    evidence_sentence TEXT,
                    source_url VARCHAR(1000),
                    source VARCHAR(80),
                    extractor VARCHAR(64) DEFAULT 'rule_fallback',
                    confidence FLOAT DEFAULT 0.0,
                    verification_status VARCHAR(32) DEFAULT 'source_only',
                    verification_sources_json TEXT,
                    entity_links_json TEXT,
                    diagnostics_json TEXT,
                    status VARCHAR(32) DEFAULT 'active',
                    created_at DATETIME,
                    updated_at DATETIME,
                    UNIQUE (event_id),
                    UNIQUE (raw_episode_id, event_type, trigger, evidence_sentence)
                )
                """
            )
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_news_extracted_events_event_id ON news_extracted_events (event_id)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_news_extracted_events_raw_episode_id ON news_extracted_events (raw_episode_id)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_news_extracted_events_card_id ON news_extracted_events (card_id)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_news_event_card_type ON news_extracted_events (card_id, event_type)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_news_event_date_type ON news_extracted_events (signal_date, event_type)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_news_event_verification ON news_extracted_events (verification_status, confidence)")

    def _migrate_news_signal_edge_quality(self) -> None:
        with self._engine.begin() as conn:
            table = conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='news_signal_edges'"
            ).fetchone()
            if table is None:
                return
            self._add_sqlite_column_if_missing(
                conn,
                "news_signal_edges",
                "edge_quality",
                "ALTER TABLE news_signal_edges ADD COLUMN edge_quality FLOAT DEFAULT 0.0",
            )
            self._add_sqlite_column_if_missing(
                conn,
                "news_signal_edges",
                "quality_grade",
                "ALTER TABLE news_signal_edges ADD COLUMN quality_grade VARCHAR(24) DEFAULT 'unknown'",
            )
            self._add_sqlite_column_if_missing(
                conn,
                "news_signal_edges",
                "quality_flags_json",
                "ALTER TABLE news_signal_edges ADD COLUMN quality_flags_json TEXT",
            )
            conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_news_signal_edge_quality ON news_signal_edges (quality_grade, edge_quality)"
            )

    def _migrate_news_signal_card_layer(self) -> None:
        with self._engine.begin() as conn:
            table = conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='news_signal_cards'"
            ).fetchone()
            if table is None:
                return
            self._add_sqlite_column_if_missing(
                conn,
                "news_signal_cards",
                "signal_layer",
                "ALTER TABLE news_signal_cards ADD COLUMN signal_layer VARCHAR(24) DEFAULT 'industry'",
            )
            conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_news_signal_layer_date ON news_signal_cards (signal_layer, signal_date)"
            )

    @staticmethod
    def _sqlite_column_exists(conn, table_name: str, column_name: str) -> bool:
        columns = {
            str(row[1])
            for row in conn.exec_driver_sql(f"PRAGMA table_info('{table_name}')").fetchall()
        }
        return column_name in columns

    @staticmethod
    def _is_sqlite_duplicate_column_error(exc: OperationalError) -> bool:
        return "duplicate column name" in str(exc).lower()

    @classmethod
    def _add_sqlite_column_if_missing(
        cls,
        conn,
        table_name: str,
        column_name: str,
        alter_sql: str,
    ) -> None:
        if cls._sqlite_column_exists(conn, table_name, column_name):
            return
        try:
            conn.exec_driver_sql(alter_sql)
        except OperationalError as exc:
            if cls._is_sqlite_duplicate_column_error(exc) and cls._sqlite_column_exists(
                conn,
                table_name,
                column_name,
            ):
                logger.info(
                    "SQLite schema migration skipped already-added column: %s.%s",
                    table_name,
                    column_name,
                )
                return
            raise

    def _migrate_seed_pool_snapshot_unique_key(self) -> None:
        with self._engine.begin() as conn:
            leftover = conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='selection_seed_pool_snapshots_old'"
            ).fetchone()
            if leftover is not None:
                new_count = conn.exec_driver_sql("SELECT COUNT(*) FROM selection_seed_pool_snapshots").scalar() or 0
                if int(new_count) == 0:
                    conn.exec_driver_sql(
                        """INSERT INTO selection_seed_pool_snapshots (
                            id, run_id, trace_id, seed_date, generated_at, market,
                            candidate_discovery_mode, seed_count, status, error,
                            source_summary_json, diagnostics_json, created_at
                        )
                        SELECT
                            id, run_id, trace_id, seed_date, generated_at, market,
                            candidate_discovery_mode, seed_count, status, error,
                            source_summary_json, diagnostics_json, created_at
                        FROM selection_seed_pool_snapshots_old"""
                    )
                conn.exec_driver_sql("DROP TABLE selection_seed_pool_snapshots_old")

            rows = conn.exec_driver_sql("PRAGMA index_list('selection_seed_pool_snapshots')").fetchall()
            unique_indexes = {str(row[1]) for row in rows if int(row[2] or 0) == 1}
            if "sqlite_autoindex_selection_seed_pool_snapshots_1" not in unique_indexes:
                return
            columns = conn.exec_driver_sql(
                "PRAGMA index_info('sqlite_autoindex_selection_seed_pool_snapshots_1')"
            ).fetchall()
            column_names = [str(row[2]) for row in columns]
            if column_names != ["run_id", "trace_id"]:
                return
            conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
            try:
                conn.exec_driver_sql("ALTER TABLE selection_seed_pool_snapshots RENAME TO selection_seed_pool_snapshots_old")
                old_indexes = conn.exec_driver_sql(
                    "PRAGMA index_list('selection_seed_pool_snapshots_old')"
                ).fetchall()
                for row in old_indexes:
                    index_name = str(row[1])
                    if index_name.startswith("sqlite_autoindex_"):
                        continue
                    conn.exec_driver_sql(f'DROP INDEX IF EXISTS "{index_name}"')
                SelectionSeedPoolSnapshot.__table__.create(conn)
                conn.exec_driver_sql(
                    """INSERT INTO selection_seed_pool_snapshots (
                        id, run_id, trace_id, seed_date, generated_at, market,
                        candidate_discovery_mode, seed_count, status, error,
                        source_summary_json, diagnostics_json, created_at
                    )
                    SELECT
                        id, run_id, trace_id, seed_date, generated_at, market,
                        candidate_discovery_mode, seed_count, status, error,
                        source_summary_json, diagnostics_json, created_at
                    FROM selection_seed_pool_snapshots_old"""
                )
                conn.exec_driver_sql("DROP TABLE selection_seed_pool_snapshots_old")
            finally:
                conn.exec_driver_sql("PRAGMA foreign_keys=ON")

    def _run_write_transaction(
        self,
        operation_name: str,
        write_operation: Callable[[Session], T],
    ) -> T:
        max_retries = self._sqlite_write_retry_max if self._is_sqlite_engine else 0

        for attempt in range(max_retries + 1):
            session = self.get_session()
            try:
                if self._is_sqlite_engine:
                    # Acquire the SQLite writer lock before any reads inside
                    # `write_operation()` so pre-write existence checks and the
                    # later upsert share one consistent write window.
                    session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                result = write_operation(session)
                session.commit()
                return result
            except OperationalError as exc:
                session.rollback()
                if (
                    self._is_sqlite_engine
                    and self._is_sqlite_locked_error(exc)
                    and attempt < max_retries
                ):
                    delay = self._sqlite_write_retry_base_delay * (2 ** attempt)
                    logger.warning(
                        "SQLite 写入锁冲突，准备重试: %s (%s/%s, %.2fs)",
                        operation_name,
                        attempt + 1,
                        max_retries,
                        delay,
                    )
                    if delay > 0:
                        time.sleep(delay)
                    continue
                raise
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()

    @staticmethod
    def _is_sqlite_locked_error(exc: OperationalError) -> bool:
        err_text = str(getattr(exc, "orig", exc)).lower()
        return any(
            token in err_text
            for token in (
                "database is locked",
                "database schema is locked",
                "database table is locked",
            )
        )

    @staticmethod
    def _normalize_daily_date(value: Any) -> Any:
        if isinstance(value, str):
            return datetime.strptime(value, '%Y-%m-%d').date()
        if isinstance(value, pd.Timestamp):
            return value.date()
        if isinstance(value, datetime):
            return value.date()
        return value

    @staticmethod
    def _normalize_minute_datetime(value: Any) -> Optional[datetime]:
        if value is None or value == "":
            return None
        if isinstance(value, pd.Timestamp):
            return value.to_pydatetime()
        if isinstance(value, datetime):
            return value
        if isinstance(value, date):
            return datetime.combine(value, datetime.min.time())
        text = str(value).strip()
        if not text:
            return None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y%m%d%H%M%S%f", "%Y%m%d%H%M%S"):
            try:
                return datetime.strptime(text[: len(fmt.replace('%f', '000000'))] if "%f" in fmt else text, fmt)
            except ValueError:
                continue
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            return None

    @staticmethod
    def _normalize_sql_value(value: Any) -> Any:
        return None if pd.isna(value) else value
    
    def get_session(self) -> Session:
        """
        获取数据库 Session
        
        使用示例:
            with db.get_session() as session:
                # 执行查询
                session.commit()  # 如果需要
        """
        if not getattr(self, '_initialized', False) or not hasattr(self, '_SessionLocal'):
            raise RuntimeError(
                "DatabaseManager 未正确初始化。"
                "请确保通过 DatabaseManager.get_instance() 获取实例。"
            )
        session = self._SessionLocal()
        try:
            return session
        except Exception:
            session.close()
            raise

    @contextmanager
    def session_scope(self):
        """Provide a transactional scope around a series of operations."""
        session = self.get_session()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
    
    def has_today_data(self, code: str, target_date: Optional[date] = None) -> bool:
        """
        检查是否已有指定日期的数据
        
        用于断点续传逻辑：如果已有数据则跳过网络请求
        
        Args:
            code: 股票代码
            target_date: 目标日期（默认今天）
            
        Returns:
            是否存在数据
        """
        if target_date is None:
            target_date = date.today()
        # 注意：这里的 target_date 语义是“自然日”，而不是“最新交易日”。
        # 在周末/节假日/非交易日运行时，即使数据库已有最新交易日数据，这里也会返回 False。
        # 该行为目前保留（按需求不改逻辑）。
        
        with self.get_session() as session:
            result = session.execute(
                select(StockDaily).where(
                    and_(
                        StockDaily.code == code,
                        StockDaily.date == target_date
                    )
                )
            ).scalar_one_or_none()
            
            return result is not None
    
    def get_latest_data(
        self, 
        code: str, 
        days: int = 2
    ) -> List[StockDaily]:
        """
        获取最近 N 天的数据
        
        用于计算"相比昨日"的变化
        
        Args:
            code: 股票代码
            days: 获取天数
            
        Returns:
            StockDaily 对象列表（按日期降序）
        """
        with self.get_session() as session:
            results = session.execute(
                select(StockDaily)
                .where(StockDaily.code == code)
                .order_by(desc(StockDaily.date))
                .limit(days)
            ).scalars().all()
            
            return list(results)

    def save_news_intel(
        self,
        code: str,
        name: str,
        dimension: str,
        query: str,
        response: 'SearchResponse',
        query_context: Optional[Dict[str, str]] = None
    ) -> int:
        """
        保存新闻情报到数据库

        去重策略：
        - 优先按 URL 去重（唯一约束）
        - URL 缺失时按 title + source + published_date 进行软去重

        关联策略：
        - query_context 记录用户查询信息（平台、用户、会话、原始指令等）
        """
        if not response or not response.results:
            return 0

        saved_count = 0
        query_ctx = query_context or {}
        current_query_id = (query_ctx.get("query_id") or "").strip()

        def _write(session: Session) -> int:
            local_saved_count = 0

            for item in response.results:
                title = (item.title or '').strip()
                url = (item.url or '').strip()
                source = (item.source or '').strip()
                snippet = (item.snippet or '').strip()
                published_date = self._parse_published_date(item.published_date)

                if not title and not url:
                    continue

                url_key = url or self._build_fallback_url_key(
                    code=code,
                    title=title,
                    source=source,
                    published_date=published_date
                )

                existing = session.execute(
                    select(NewsIntel).where(NewsIntel.url == url_key)
                ).scalar_one_or_none()

                if existing:
                    existing.name = name or existing.name
                    existing.dimension = dimension or existing.dimension
                    existing.query = query or existing.query
                    existing.provider = response.provider or existing.provider
                    existing.snippet = snippet or existing.snippet
                    existing.source = source or existing.source
                    existing.published_date = published_date or existing.published_date
                    existing.fetched_at = datetime.now()

                    if query_context:
                        if not existing.query_id and current_query_id:
                            existing.query_id = current_query_id
                        existing.query_source = (
                            query_context.get("query_source") or existing.query_source
                        )
                        existing.requester_platform = (
                            query_context.get("requester_platform") or existing.requester_platform
                        )
                        existing.requester_user_id = (
                            query_context.get("requester_user_id") or existing.requester_user_id
                        )
                        existing.requester_user_name = (
                            query_context.get("requester_user_name") or existing.requester_user_name
                        )
                        existing.requester_chat_id = (
                            query_context.get("requester_chat_id") or existing.requester_chat_id
                        )
                        existing.requester_message_id = (
                            query_context.get("requester_message_id") or existing.requester_message_id
                        )
                        existing.requester_query = (
                            query_context.get("requester_query") or existing.requester_query
                        )
                    continue

                try:
                    with session.begin_nested():
                        record = NewsIntel(
                            code=code,
                            name=name,
                            dimension=dimension,
                            query=query,
                            provider=response.provider,
                            title=title,
                            snippet=snippet,
                            url=url_key,
                            source=source,
                            published_date=published_date,
                            fetched_at=datetime.now(),
                            query_id=current_query_id or None,
                            query_source=query_ctx.get("query_source"),
                            requester_platform=query_ctx.get("requester_platform"),
                            requester_user_id=query_ctx.get("requester_user_id"),
                            requester_user_name=query_ctx.get("requester_user_name"),
                            requester_chat_id=query_ctx.get("requester_chat_id"),
                            requester_message_id=query_ctx.get("requester_message_id"),
                            requester_query=query_ctx.get("requester_query"),
                        )
                        session.add(record)
                        session.flush()
                    local_saved_count += 1
                except IntegrityError:
                    logger.debug("新闻情报重复（已跳过）: %s %s", code, url_key)

            return local_saved_count

        try:
            saved_count = self._run_write_transaction(
                f"save_news_intel[{code}]",
                _write,
            )
            logger.info(f"保存新闻情报成功: {code}, 新增 {saved_count} 条")
        except Exception as e:
            logger.error(f"保存新闻情报失败: {e}")
            raise

        return saved_count

    def save_fundamental_snapshot(
        self,
        query_id: str,
        code: str,
        payload: Optional[Dict[str, Any]],
        source_chain: Optional[Any] = None,
        coverage: Optional[Any] = None,
    ) -> int:
        """
        保存基本面快照（P0 write-only）。失败不抛异常，返回写入条数 0/1。
        """
        if not query_id or not code or payload is None:
            return 0

        try:
            def _write(session: Session) -> int:
                session.add(
                    FundamentalSnapshot(
                        query_id=query_id,
                        code=code,
                        payload=self._safe_json_dumps(payload),
                        source_chain=self._safe_json_dumps(source_chain or []),
                        coverage=self._safe_json_dumps(coverage or {}),
                    )
                )
                return 1
            return self._run_write_transaction(
                f"save_fundamental_snapshot[{query_id}:{code}]",
                _write,
            )
        except Exception as e:
            logger.debug(
                "基本面快照写入失败（fail-open）: query_id=%s code=%s err=%s",
                query_id,
                code,
                e,
            )
            return 0

    def get_latest_fundamental_snapshot(
        self,
        query_id: str,
        code: str,
    ) -> Optional[Dict[str, Any]]:
        """
        获取指定 query_id + code 的最新基本面快照 payload。

        读取失败或不存在时返回 None（fail-open）。
        """
        if not query_id or not code:
            return None

        with self.get_session() as session:
            try:
                row = session.execute(
                    select(FundamentalSnapshot)
                    .where(
                        and_(
                            FundamentalSnapshot.query_id == query_id,
                            FundamentalSnapshot.code == code,
                        )
                    )
                    .order_by(desc(FundamentalSnapshot.created_at))
                    .limit(1)
                ).scalar_one_or_none()
            except Exception as e:
                logger.debug(
                    "基本面快照读取失败（fail-open）: query_id=%s code=%s err=%s",
                    query_id,
                    code,
                    e,
                )
                return None

            if row is None:
                return None
            try:
                payload = json.loads(row.payload or "{}")
                return payload if isinstance(payload, dict) else None
            except Exception:
                return None

    def get_recent_news(self, code: str, days: int = 7, limit: int = 20) -> List[NewsIntel]:
        """
        获取指定股票最近 N 天的新闻情报
        """
        cutoff_date = datetime.now() - timedelta(days=days)

        with self.get_session() as session:
            results = session.execute(
                select(NewsIntel)
                .where(
                    and_(
                        NewsIntel.code == code,
                        NewsIntel.fetched_at >= cutoff_date
                    )
                )
                .order_by(desc(NewsIntel.fetched_at))
                .limit(limit)
            ).scalars().all()

            return list(results)

    def get_news_intel_by_query_id(self, query_id: str, limit: int = 20) -> List[NewsIntel]:
        """
        根据 query_id 获取新闻情报列表

        Args:
            query_id: 分析记录唯一标识
            limit: 返回数量限制

        Returns:
            NewsIntel 列表（按发布时间或抓取时间倒序）
        """
        from sqlalchemy import func

        with self.get_session() as session:
            results = session.execute(
                select(NewsIntel)
                .where(NewsIntel.query_id == query_id)
                .order_by(
                    desc(func.coalesce(NewsIntel.published_date, NewsIntel.fetched_at)),
                    desc(NewsIntel.fetched_at)
                )
                .limit(limit)
            ).scalars().all()

            return list(results)

    def save_analysis_history(
        self,
        result: Any,
        query_id: str,
        report_type: str,
        news_content: Optional[str],
        context_snapshot: Optional[Dict[str, Any]] = None,
        save_snapshot: bool = True
    ) -> int:
        """
        保存分析结果历史记录
        """
        if result is None:
            return 0

        sniper_points = self._extract_sniper_points(result)
        raw_result = self._build_raw_result(result)
        context_text = None
        if save_snapshot and context_snapshot is not None:
            context_text = self._safe_json_dumps(context_snapshot)

        try:
            def _write(session: Session) -> int:
                session.add(
                    AnalysisHistory(
                        query_id=query_id,
                        code=result.code,
                        name=result.name,
                        report_type=report_type,
                        sentiment_score=result.sentiment_score,
                        operation_advice=result.operation_advice,
                        trend_prediction=result.trend_prediction,
                        analysis_summary=result.analysis_summary,
                        raw_result=self._safe_json_dumps(raw_result),
                        news_content=news_content,
                        context_snapshot=context_text,
                        ideal_buy=sniper_points.get("ideal_buy"),
                        secondary_buy=sniper_points.get("secondary_buy"),
                        stop_loss=sniper_points.get("stop_loss"),
                        take_profit=sniper_points.get("take_profit"),
                        created_at=datetime.now(),
                    )
                )
                return 1
            return self._run_write_transaction(
                f"save_analysis_history[{result.code}]",
                _write,
            )
        except Exception as e:
            logger.error(f"保存分析历史失败: {e}")
            return 0

    def get_analysis_history(
        self,
        code: Optional[str] = None,
        query_id: Optional[str] = None,
        days: int = 30,
        limit: int = 50,
        exclude_query_id: Optional[str] = None,
    ) -> List[AnalysisHistory]:
        """
        Query analysis history records.

        Notes:
        - If query_id is provided, perform exact lookup and ignore days window.
        - If query_id is not provided, apply days-based time filtering.
        - exclude_query_id: exclude records with this query_id (for history comparison).
        """
        cutoff_date = datetime.now() - timedelta(days=days)

        with self.get_session() as session:
            conditions = []

            if query_id:
                conditions.append(AnalysisHistory.query_id == query_id)
            else:
                conditions.append(AnalysisHistory.created_at >= cutoff_date)

            if code:
                conditions.append(AnalysisHistory.code == code)

            # exclude_query_id only applies when not doing exact lookup (query_id is None)
            if exclude_query_id and not query_id:
                conditions.append(AnalysisHistory.query_id != exclude_query_id)

            results = session.execute(
                select(AnalysisHistory)
                .where(and_(*conditions))
                .order_by(desc(AnalysisHistory.created_at))
                .limit(limit)
            ).scalars().all()

            return list(results)
    
    def get_analysis_history_paginated(
        self,
        code: Optional[str] = None,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        offset: int = 0,
        limit: int = 20
    ) -> Tuple[List[AnalysisHistory], int]:
        """
        分页查询分析历史记录（带总数）
        
        Args:
            code: 股票代码筛选
            start_date: 开始日期（含）
            end_date: 结束日期（含）
            offset: 偏移量（跳过前 N 条）
            limit: 每页数量
            
        Returns:
            Tuple[List[AnalysisHistory], int]: (记录列表, 总数)
        """
        from sqlalchemy import func
        
        with self.get_session() as session:
            conditions = []
            
            if code:
                conditions.append(AnalysisHistory.code == code)
            if start_date:
                # created_at >= start_date 00:00:00
                conditions.append(AnalysisHistory.created_at >= datetime.combine(start_date, datetime.min.time()))
            if end_date:
                # created_at < end_date+1 00:00:00 (即 <= end_date 23:59:59)
                conditions.append(AnalysisHistory.created_at < datetime.combine(end_date + timedelta(days=1), datetime.min.time()))
            
            # 构建 where 子句
            where_clause = and_(*conditions) if conditions else True
            
            # 查询总数
            total_query = select(func.count(AnalysisHistory.id)).where(where_clause)
            total = session.execute(total_query).scalar() or 0
            
            # 查询分页数据
            data_query = (
                select(AnalysisHistory)
                .where(where_clause)
                .order_by(desc(AnalysisHistory.created_at))
                .offset(offset)
                .limit(limit)
            )
            results = session.execute(data_query).scalars().all()
            
            return list(results), total
    
    def get_analysis_history_by_id(self, record_id: int) -> Optional[AnalysisHistory]:
        """
        根据数据库主键 ID 查询单条分析历史记录
        
        由于 query_id 可能重复（批量分析时多条记录共享同一 query_id），
        使用主键 ID 确保精确查询唯一记录。
        
        Args:
            record_id: 分析历史记录的主键 ID
            
        Returns:
            AnalysisHistory 对象，不存在返回 None
        """
        with self.get_session() as session:
            result = session.execute(
                select(AnalysisHistory).where(AnalysisHistory.id == record_id)
            ).scalars().first()
            return result

    def delete_analysis_history_records(self, record_ids: List[int]) -> int:
        """
        删除指定的分析历史记录。

        同时清理依赖这些历史记录的回测结果，避免外键约束失败。

        Args:
            record_ids: 要删除的历史记录主键 ID 列表

        Returns:
            实际删除的历史记录数量
        """
        ids = sorted({int(record_id) for record_id in record_ids if record_id is not None})
        if not ids:
            return 0

        with self.session_scope() as session:
            session.execute(
                delete(BacktestResult).where(BacktestResult.analysis_history_id.in_(ids))
            )
            result = session.execute(
                delete(AnalysisHistory).where(AnalysisHistory.id.in_(ids))
            )
            return result.rowcount or 0

    def get_latest_analysis_by_query_id(self, query_id: str) -> Optional[AnalysisHistory]:
        """
        根据 query_id 查询最新一条分析历史记录

        query_id 在批量分析时可能重复，故返回最近创建的一条。

        Args:
            query_id: 分析记录关联的 query_id

        Returns:
            AnalysisHistory 对象，不存在返回 None
        """
        with self.get_session() as session:
            result = session.execute(
                select(AnalysisHistory)
                .where(AnalysisHistory.query_id == query_id)
                .order_by(desc(AnalysisHistory.created_at))
                .limit(1)
            ).scalars().first()
            return result
    
    def get_data_range(
        self, 
        code: str, 
        start_date: date, 
        end_date: date
    ) -> List[StockDaily]:
        """
        获取指定日期范围的数据
        
        Args:
            code: 股票代码
            start_date: 开始日期
            end_date: 结束日期
            
        Returns:
            StockDaily 对象列表
        """
        with self.get_session() as session:
            results = session.execute(
                select(StockDaily)
                .where(
                    and_(
                        StockDaily.code == code,
                        StockDaily.date >= start_date,
                        StockDaily.date <= end_date
                    )
                )
                .order_by(StockDaily.date)
            ).scalars().all()
            
            return list(results)
    
    def save_daily_data(
        self, 
        df: pd.DataFrame, 
        code: str,
        data_source: str = "Unknown"
    ) -> int:
        """
        保存日线数据到数据库
        
        策略：
        - 按 `(code, date)` 做批量 UPSERT，已存在记录会覆盖更新
        - 同一批次内若存在重复日期，以最后一条记录为准
        - SQLite 分支按 chunk 写入以避免绑定参数上限
        
        Args:
            df: 包含日线数据的 DataFrame
            code: 股票代码
            data_source: 数据来源名称
            
        Returns:
            本次实际新增的记录数（不含更新）
        """
        if df is None or df.empty:
            logger.warning(f"保存数据为空，跳过 {code}")
            return 0

        now = datetime.now()
        records_by_date: Dict[date, Dict[str, Any]] = {}
        for row in df.to_dict(orient='records'):
            row_date = self._normalize_daily_date(row.get('date'))
            records_by_date[row_date] = {
                'code': code,
                'date': row_date,
                'open': self._normalize_sql_value(row.get('open')),
                'high': self._normalize_sql_value(row.get('high')),
                'low': self._normalize_sql_value(row.get('low')),
                'close': self._normalize_sql_value(row.get('close')),
                'volume': self._normalize_sql_value(row.get('volume')),
                'amount': self._normalize_sql_value(row.get('amount')),
                'pct_chg': self._normalize_sql_value(row.get('pct_chg')),
                'ma5': self._normalize_sql_value(row.get('ma5')),
                'ma10': self._normalize_sql_value(row.get('ma10')),
                'ma20': self._normalize_sql_value(row.get('ma20')),
                'volume_ratio': self._normalize_sql_value(row.get('volume_ratio')),
                'data_source': data_source,
                'created_at': now,
                'updated_at': now,
            }

        if not records_by_date:
            return 0

        records = list(records_by_date.values())
        batch_dates = list(records_by_date.keys())

        def _write(session: Session) -> int:
            if self._is_sqlite_engine:
                # SQLite has a per-statement bind-parameter limit (commonly 999).
                # Each record has ~15 columns, so chunk upserts to stay within bounds.
                _SQLITE_CHUNK = 50
                # `_run_write_transaction()` opens SQLite writes with
                # `BEGIN IMMEDIATE`, so existence checks and upsert execute
                # within one stable write window.
                existing_dates = set()
                _COUNT_CHUNK = 500
                for j in range(0, len(batch_dates), _COUNT_CHUNK):
                    chunk_dates = batch_dates[j : j + _COUNT_CHUNK]
                    if not chunk_dates:
                        continue
                    existing_dates.update(
                        session.execute(
                            select(StockDaily.date).where(
                                and_(
                                    StockDaily.code == code,
                                    StockDaily.date.in_(chunk_dates),
                                )
                            )
                        ).scalars().all()
                    )
                new_records = [
                    record for record in records if record['date'] not in existing_dates
                ]
                for i in range(0, len(records), _SQLITE_CHUNK):
                    chunk = records[i : i + _SQLITE_CHUNK]
                    stmt = sqlite_insert(StockDaily).values(chunk)
                    excluded = stmt.excluded
                    session.execute(
                        stmt.on_conflict_do_update(
                            index_elements=['code', 'date'],
                            set_={
                                'open': excluded.open,
                                'high': excluded.high,
                                'low': excluded.low,
                                'close': excluded.close,
                                'volume': excluded.volume,
                                'amount': excluded.amount,
                                'pct_chg': excluded.pct_chg,
                                'ma5': excluded.ma5,
                                'ma10': excluded.ma10,
                                'ma20': excluded.ma20,
                                'volume_ratio': excluded.volume_ratio,
                                'data_source': excluded.data_source,
                                'updated_at': excluded.updated_at,
                            },
                        )
                    )
                return len(new_records)
            else:
                existing_rows = {
                    row.date: row
                    for row in session.execute(
                        select(StockDaily).where(
                            and_(
                                StockDaily.code == code,
                                StockDaily.date.in_(batch_dates),
                            )
                        )
                    ).scalars().all()
                }
                new_count = 0
                for record in records:
                    existing = existing_rows.get(record['date'])
                    if existing is None:
                        session.add(StockDaily(**record))
                        new_count += 1
                        continue
                    existing.open = record['open']
                    existing.high = record['high']
                    existing.low = record['low']
                    existing.close = record['close']
                    existing.volume = record['volume']
                    existing.amount = record['amount']
                    existing.pct_chg = record['pct_chg']
                    existing.ma5 = record['ma5']
                    existing.ma10 = record['ma10']
                    existing.ma20 = record['ma20']
                    existing.volume_ratio = record['volume_ratio']
                    existing.data_source = record['data_source']
                    existing.updated_at = record['updated_at']
                return new_count

        try:
            saved_count = self._run_write_transaction(
                f"save_daily_data[{code}]",
                _write,
            )
            logger.info(f"保存 {code} 数据成功，新增 {saved_count} 条")
            return saved_count
        except Exception as e:
            logger.error(f"保存 {code} 数据失败: {e}")
            raise

    def save_minute_bars(
        self,
        records: Iterable[Dict[str, Any]],
        *,
        data_source: str = "BaostockMinute",
    ) -> int:
        """保存分钟线数据到 `stock_minute_bars`。

        Args:
            records: 每条至少包含 code/bar_datetime/open/high/low/close。
            data_source: 数据来源标记。

        Returns:
            本次实际新增的记录数（不含更新）。
        """
        now = datetime.now()
        normalized_by_key: Dict[Tuple[str, datetime, str, str], Dict[str, Any]] = {}
        for row in records or []:
            code = str(row.get('code') or '').strip().upper()
            bar_dt = self._normalize_minute_datetime(row.get('bar_datetime') or row.get('datetime'))
            if not code or bar_dt is None:
                continue
            frequency = str(row.get('frequency') or '5').strip()
            adjustflag = str(row.get('adjustflag') or '3').strip()
            bar_time = str(row.get('bar_time') or bar_dt.strftime('%H:%M:%S'))
            key = (code, bar_dt, frequency, adjustflag)
            normalized_by_key[key] = {
                'code': code,
                'baostock_code': str(row.get('baostock_code') or '').strip().lower() or None,
                'frequency': frequency,
                'adjustflag': adjustflag,
                'bar_datetime': bar_dt,
                'bar_date': bar_dt.date(),
                'bar_time': bar_time,
                'open': self._normalize_sql_value(row.get('open')),
                'high': self._normalize_sql_value(row.get('high')),
                'low': self._normalize_sql_value(row.get('low')),
                'close': self._normalize_sql_value(row.get('close')),
                'volume': self._normalize_sql_value(row.get('volume')),
                'amount': self._normalize_sql_value(row.get('amount')),
                'data_source': data_source,
                'fetched_at': now,
                'updated_at': now,
            }

        if not normalized_by_key:
            return 0

        records_to_write = list(normalized_by_key.values())
        keys = list(normalized_by_key.keys())

        def _write(session: Session) -> int:
            existing_keys = set()
            _COUNT_CHUNK = 500
            for i in range(0, len(keys), _COUNT_CHUNK):
                chunk = keys[i : i + _COUNT_CHUNK]
                conditions = [
                    and_(
                        StockMinuteBar.code == code,
                        StockMinuteBar.bar_datetime == bar_dt,
                        StockMinuteBar.frequency == frequency,
                        StockMinuteBar.adjustflag == adjustflag,
                    )
                    for code, bar_dt, frequency, adjustflag in chunk
                ]
                if not conditions:
                    continue
                existing_keys.update(
                    (
                        row.code,
                        row.bar_datetime,
                        row.frequency,
                        row.adjustflag,
                    )
                    for row in session.execute(
                        select(StockMinuteBar).where(or_(*conditions))
                    ).scalars().all()
                )

            new_count = sum(1 for key in keys if key not in existing_keys)
            if self._is_sqlite_engine:
                _SQLITE_CHUNK = 100
                for i in range(0, len(records_to_write), _SQLITE_CHUNK):
                    chunk = records_to_write[i : i + _SQLITE_CHUNK]
                    stmt = sqlite_insert(StockMinuteBar).values(chunk)
                    excluded = stmt.excluded
                    session.execute(
                        stmt.on_conflict_do_update(
                            index_elements=['code', 'bar_datetime', 'frequency', 'adjustflag'],
                            set_={
                                'baostock_code': excluded.baostock_code,
                                'bar_date': excluded.bar_date,
                                'bar_time': excluded.bar_time,
                                'open': excluded.open,
                                'high': excluded.high,
                                'low': excluded.low,
                                'close': excluded.close,
                                'volume': excluded.volume,
                                'amount': excluded.amount,
                                'data_source': excluded.data_source,
                                'fetched_at': excluded.fetched_at,
                                'updated_at': excluded.updated_at,
                            },
                        )
                    )
                return new_count

            existing_rows = {
                (row.code, row.bar_datetime, row.frequency, row.adjustflag): row
                for row in session.execute(
                    select(StockMinuteBar).where(or_(*[
                        and_(
                            StockMinuteBar.code == code,
                            StockMinuteBar.bar_datetime == bar_dt,
                            StockMinuteBar.frequency == frequency,
                            StockMinuteBar.adjustflag == adjustflag,
                        )
                        for code, bar_dt, frequency, adjustflag in keys
                    ]))
                ).scalars().all()
            }
            for record in records_to_write:
                key = (
                    record['code'],
                    record['bar_datetime'],
                    record['frequency'],
                    record['adjustflag'],
                )
                existing = existing_rows.get(key)
                if existing is None:
                    session.add(StockMinuteBar(**record))
                    continue
                for field, value in record.items():
                    setattr(existing, field, value)
            return new_count

        try:
            saved_count = self._run_write_transaction("save_minute_bars", _write)
            logger.info("保存分钟线数据成功，新增 %s 条，写入/更新 %s 条", saved_count, len(records_to_write))
            return saved_count
        except Exception as e:
            logger.error("保存分钟线数据失败: %s", e)
            raise
    
    def get_analysis_context(
        self, 
        code: str,
        target_date: Optional[date] = None
    ) -> Optional[Dict[str, Any]]:
        """
        获取分析所需的上下文数据
        
        返回今日数据 + 昨日数据的对比信息
        
        Args:
            code: 股票代码
            target_date: 目标日期（默认今天）
            
        Returns:
            包含今日数据、昨日对比等信息的字典
        """
        if target_date is None:
            target_date = date.today()
        # 注意：尽管入参提供了 target_date，但当前实现实际使用的是“最新两天数据”（get_latest_data），
        # 并不会按 target_date 精确取当日/前一交易日的上下文。
        # 因此若未来需要支持“按历史某天复盘/重算”的可解释性，这里需要调整。
        # 该行为目前保留（按需求不改逻辑）。
        
        # 获取最近2天数据
        recent_data = self.get_latest_data(code, days=2)
        
        if not recent_data:
            logger.warning(f"未找到 {code} 的数据")
            return None
        
        today_data = recent_data[0]
        yesterday_data = recent_data[1] if len(recent_data) > 1 else None
        
        context = {
            'code': code,
            'date': today_data.date.isoformat(),
            'today': today_data.to_dict(),
        }
        
        if yesterday_data:
            context['yesterday'] = yesterday_data.to_dict()
            
            # 计算相比昨日的变化
            if yesterday_data.volume and yesterday_data.volume > 0:
                context['volume_change_ratio'] = round(
                    today_data.volume / yesterday_data.volume, 2
                )
            
            if yesterday_data.close and yesterday_data.close > 0:
                context['price_change_ratio'] = round(
                    (today_data.close - yesterday_data.close) / yesterday_data.close * 100, 2
                )
            
            # 均线形态判断
            context['ma_status'] = self._analyze_ma_status(today_data)
        
        return context
    
    def _analyze_ma_status(self, data: StockDaily) -> str:
        """
        分析均线形态
        
        判断条件：
        - 多头排列：close > ma5 > ma10 > ma20
        - 空头排列：close < ma5 < ma10 < ma20
        - 震荡整理：其他情况
        """
        # 注意：这里的均线形态判断基于“close/ma5/ma10/ma20”静态比较，
        # 未考虑均线拐点、斜率、或不同数据源复权口径差异。
        # 该行为目前保留（按需求不改逻辑）。
        close = data.close or 0
        ma5 = data.ma5 or 0
        ma10 = data.ma10 or 0
        ma20 = data.ma20 or 0
        
        if close > ma5 > ma10 > ma20 > 0:
            return "多头排列 📈"
        elif close < ma5 < ma10 < ma20 and ma20 > 0:
            return "空头排列 📉"
        elif close > ma5 and ma5 > ma10:
            return "短期向好 🔼"
        elif close < ma5 and ma5 < ma10:
            return "短期走弱 🔽"
        else:
            return "震荡整理 ↔️"

    @staticmethod
    def _parse_published_date(value: Optional[str]) -> Optional[datetime]:
        """
        解析发布时间字符串（失败返回 None）
        """
        if not value:
            return None

        if isinstance(value, datetime):
            return value

        text = str(value).strip()
        if not text:
            return None

        # 优先尝试 ISO 格式
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            pass

        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d",
            "%Y/%m/%d %H:%M:%S",
            "%Y/%m/%d %H:%M",
            "%Y/%m/%d",
        ):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue

        return None

    @staticmethod
    def _safe_json_dumps(data: Any) -> str:
        """
        安全序列化为 JSON 字符串
        """
        try:
            return json.dumps(data, ensure_ascii=False, default=str)
        except Exception:
            return json.dumps(str(data), ensure_ascii=False)

    @staticmethod
    def _build_raw_result(result: Any) -> Dict[str, Any]:
        """
        生成完整分析结果字典
        """
        data = result.to_dict() if hasattr(result, "to_dict") else {}
        data.update({
            'data_sources': getattr(result, 'data_sources', ''),
            'raw_response': getattr(result, 'raw_response', None),
        })
        return data

    @staticmethod
    def _parse_sniper_value(value: Any) -> Optional[float]:
        """
        Parse a sniper point value from various formats to float.

        Handles: numeric types, plain number strings, Chinese price formats
        like "18.50元", range formats like "18.50-19.00", and text with
        embedded numbers while filtering out MA indicators.
        """
        if value is None:
            return None
        if isinstance(value, (int, float)):
            v = float(value)
            return v if v > 0 else None

        text = str(value).replace(',', '').replace('，', '').strip()
        if not text or text == '-' or text == '—' or text == 'N/A':
            return None

        # 尝试直接解析纯数字字符串
        try:
            return float(text)
        except ValueError:
            pass

        # 优先截取 "：" 到 "元" 之间的价格，避免误提取 MA5/MA10 等技术指标数字
        colon_pos = max(text.rfind("："), text.rfind(":"))
        yuan_pos = text.find("元", colon_pos + 1 if colon_pos != -1 else 0)
        if yuan_pos != -1:
            segment_start = colon_pos + 1 if colon_pos != -1 else 0
            segment = text[segment_start:yuan_pos]
            
            # 使用 finditer 并过滤掉 MA 开头的数字
            matches = list(re.finditer(r"-?\d+(?:\.\d+)?", segment))
            valid_numbers = []
            for m in matches:
                # 检查前面是否是 "MA" (忽略大小写)
                start_idx = m.start()
                if start_idx >= 2:
                    prefix = segment[start_idx-2:start_idx].upper()
                    if prefix == "MA":
                        continue
                valid_numbers.append(m.group())
            
            if valid_numbers:
                try:
                    return abs(float(valid_numbers[-1]))
                except ValueError:
                    pass

        # 兜底：无"元"字时，先截去第一个括号后的内容，避免误提取括号内技术指标数字
        # 例如 "1.52-1.53 (回踩MA5/10附近)" → 仅在 "1.52-1.53 " 中搜索
        paren_pos = len(text)
        for paren_char in ('(', '（'):
            pos = text.find(paren_char)
            if pos != -1:
                paren_pos = min(paren_pos, pos)
        search_text = text[:paren_pos].strip() or text  # 括号前为空时降级用全文

        valid_numbers = []
        for m in re.finditer(r"\d+(?:\.\d+)?", search_text):
            start_idx = m.start()
            if start_idx >= 2 and search_text[start_idx-2:start_idx].upper() == "MA":
                continue
            valid_numbers.append(m.group())
        if valid_numbers:
            try:
                return float(valid_numbers[-1])
            except ValueError:
                pass
        return None

    def _extract_sniper_points(self, result: Any) -> Dict[str, Optional[float]]:
        """
        Extract sniper point values from an AnalysisResult.

        Tries multiple extraction paths to handle different dashboard structures:
        1. result.get_sniper_points() (standard path)
        2. Direct dashboard dict traversal with various nesting levels
        3. Fallback from raw_result dict if available
        """
        raw_points = {}

        # Path 1: standard method
        if hasattr(result, "get_sniper_points"):
            raw_points = result.get_sniper_points() or {}

        # Path 2: direct dashboard traversal when standard path yields empty values
        if not any(raw_points.get(k) for k in ("ideal_buy", "secondary_buy", "stop_loss", "take_profit")):
            dashboard = getattr(result, "dashboard", None)
            if isinstance(dashboard, dict):
                raw_points = self._find_sniper_in_dashboard(dashboard) or raw_points

        # Path 3: try raw_result for agent mode results
        if not any(raw_points.get(k) for k in ("ideal_buy", "secondary_buy", "stop_loss", "take_profit")):
            raw_response = getattr(result, "raw_response", None)
            if isinstance(raw_response, dict):
                raw_points = self._find_sniper_in_dashboard(raw_response) or raw_points

        return {
            "ideal_buy": self._parse_sniper_value(raw_points.get("ideal_buy")),
            "secondary_buy": self._parse_sniper_value(raw_points.get("secondary_buy")),
            "stop_loss": self._parse_sniper_value(raw_points.get("stop_loss")),
            "take_profit": self._parse_sniper_value(raw_points.get("take_profit")),
        }

    @staticmethod
    def _find_sniper_in_dashboard(d: dict) -> Optional[Dict[str, Any]]:
        """
        Recursively search for sniper_points in a dashboard dict.
        Handles various nesting: dashboard.battle_plan.sniper_points,
        dashboard.dashboard.battle_plan.sniper_points, etc.
        """
        if not isinstance(d, dict):
            return None

        # Direct: d has sniper_points keys at top level
        if "ideal_buy" in d:
            return d

        # d.sniper_points
        sp = d.get("sniper_points")
        if isinstance(sp, dict) and sp:
            return sp

        # d.battle_plan.sniper_points
        bp = d.get("battle_plan")
        if isinstance(bp, dict):
            sp = bp.get("sniper_points")
            if isinstance(sp, dict) and sp:
                return sp

        # d.dashboard.battle_plan.sniper_points (double-nested)
        inner = d.get("dashboard")
        if isinstance(inner, dict):
            bp = inner.get("battle_plan")
            if isinstance(bp, dict):
                sp = bp.get("sniper_points")
                if isinstance(sp, dict) and sp:
                    return sp

        return None

    @staticmethod
    def _build_fallback_url_key(
        code: str,
        title: str,
        source: str,
        published_date: Optional[datetime]
    ) -> str:
        """
        生成无 URL 时的去重键（确保稳定且较短）
        """
        date_str = published_date.isoformat() if published_date else ""
        raw_key = f"{code}|{title}|{source}|{date_str}"
        digest = hashlib.md5(raw_key.encode("utf-8")).hexdigest()
        return f"no-url:{code}:{digest}"

    def save_conversation_message(self, session_id: str, role: str, content: str) -> None:
        """
        保存 Agent 对话消息
        """
        with self.session_scope() as session:
            msg = ConversationMessage(
                session_id=session_id,
                role=role,
                content=content
            )
            session.add(msg)

    def get_conversation_history(self, session_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        """
        获取 Agent 对话历史
        """
        with self.session_scope() as session:
            stmt = select(ConversationMessage).filter(
                ConversationMessage.session_id == session_id
            ).order_by(ConversationMessage.created_at.desc()).limit(limit)
            messages = session.execute(stmt).scalars().all()

            # 倒序返回，保证时间顺序
            return [{"role": msg.role, "content": msg.content} for msg in reversed(messages)]

    def conversation_session_exists(self, session_id: str) -> bool:
        """Return True when at least one message exists for the given session."""
        with self.session_scope() as session:
            stmt = (
                select(ConversationMessage.id)
                .where(ConversationMessage.session_id == session_id)
                .limit(1)
            )
            return session.execute(stmt).scalar() is not None

    def get_chat_sessions(
        self,
        limit: int = 50,
        session_prefix: Optional[str] = None,
        extra_session_ids: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        获取聊天会话列表（从 conversation_messages 聚合）

        Args:
            limit: Maximum number of sessions to return.
            session_prefix: If provided, only return sessions whose session_id
                starts with this prefix.  Used for per-user isolation (e.g.
                ``"telegram_12345"``).
            extra_session_ids: Optional exact session ids to include in
                addition to the scoped prefix.

        Returns:
            按最近活跃时间倒序的会话列表，每条包含 session_id, title, message_count, last_active
        """
        from sqlalchemy import func

        with self.session_scope() as session:
            normalized_prefix = None
            if session_prefix:
                normalized_prefix = session_prefix if session_prefix.endswith(":") else f"{session_prefix}:"
            exact_ids = [sid for sid in (extra_session_ids or []) if sid]

            # 聚合每个 session 的消息数和最后活跃时间
            base = (
                select(
                    ConversationMessage.session_id,
                    func.count(ConversationMessage.id).label("message_count"),
                    func.min(ConversationMessage.created_at).label("created_at"),
                    func.max(ConversationMessage.created_at).label("last_active"),
                )
            )
            conditions = []
            if normalized_prefix:
                conditions.append(ConversationMessage.session_id.startswith(normalized_prefix))
            if exact_ids:
                conditions.append(ConversationMessage.session_id.in_(exact_ids))
            if conditions:
                base = base.where(or_(*conditions))
            stmt = (
                base
                .group_by(ConversationMessage.session_id)
                .order_by(desc(func.max(ConversationMessage.created_at)))
                .limit(limit)
            )
            rows = session.execute(stmt).all()

            results = []
            for row in rows:
                sid = row.session_id
                # 取该会话第一条 user 消息作为标题
                first_user_msg = session.execute(
                    select(ConversationMessage.content)
                    .where(
                        and_(
                            ConversationMessage.session_id == sid,
                            ConversationMessage.role == "user",
                        )
                    )
                    .order_by(ConversationMessage.created_at)
                    .limit(1)
                ).scalar()
                title = (first_user_msg or "新对话")[:60]

                results.append({
                    "session_id": sid,
                    "title": title,
                    "message_count": row.message_count,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                    "last_active": row.last_active.isoformat() if row.last_active else None,
                })
            return results

    def get_conversation_messages(self, session_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        """
        获取单个会话的完整消息列表（用于前端恢复历史）
        """
        with self.session_scope() as session:
            stmt = (
                select(ConversationMessage)
                .where(ConversationMessage.session_id == session_id)
                .order_by(ConversationMessage.created_at)
                .limit(limit)
            )
            messages = session.execute(stmt).scalars().all()
            return [
                {
                    "id": str(msg.id),
                    "role": msg.role,
                    "content": msg.content,
                    "created_at": msg.created_at.isoformat() if msg.created_at else None,
                }
                for msg in messages
            ]

    def delete_conversation_session(self, session_id: str) -> int:
        """
        删除指定会话的所有消息

        Returns:
            删除的消息数
        """
        with self.session_scope() as session:
            result = session.execute(
                delete(ConversationMessage).where(
                    ConversationMessage.session_id == session_id
                )
            )
            return result.rowcount

    # ------------------------------------------------------------------
    # LLM usage tracking
    # ------------------------------------------------------------------

    def record_llm_usage(
        self,
        call_type: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        stock_code: Optional[str] = None,
    ) -> None:
        """Append one LLM call record to llm_usage."""
        row = LLMUsage(
            call_type=call_type,
            model=model or "unknown",
            stock_code=stock_code,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        )
        with self.session_scope() as session:
            session.add(row)

    # ------------------------------------------------------------------
    # Market regime state
    # ------------------------------------------------------------------

    def get_market_regime_state(self, market: str = "cn") -> Optional[Dict[str, Any]]:
        """Return the latest persisted regime state for a market."""
        market_key = (market or "cn").strip().lower()
        with self.get_session() as session:
            row = session.execute(
                select(MarketRegimeState).where(MarketRegimeState.market == market_key)
            ).scalar_one_or_none()
            return row.to_dict() if row else None

    def save_market_regime_state(self, market: str, payload: Dict[str, Any]) -> None:
        """Upsert one market regime state snapshot."""
        market_key = (market or "cn").strip().lower()
        confirmation = payload.get("confirmation") or {}
        as_of_raw = payload.get("as_of")
        as_of_date = None
        if isinstance(as_of_raw, date):
            as_of_date = as_of_raw
        elif isinstance(as_of_raw, str) and as_of_raw:
            try:
                as_of_date = datetime.strptime(as_of_raw[:10], "%Y-%m-%d").date()
            except ValueError:
                as_of_date = None
        now = datetime.now()
        row_payload = {
            "market": market_key,
            "as_of": as_of_date,
            "regime": str(payload.get("regime") or "unknown"),
            "raw_regime": str(confirmation.get("raw_regime") or payload.get("regime") or "unknown"),
            "volatility_bucket": str(payload.get("volatility_bucket") or "unknown"),
            "raw_volatility_bucket": str(payload.get("raw_volatility_bucket") or "unknown"),
            "pending_regime": confirmation.get("pending_regime"),
            "pending_count": int(confirmation.get("pending_count") or 0),
            "payload": json.dumps(payload, ensure_ascii=False, default=str),
            "updated_at": now,
        }

        def _write(session: Session) -> None:
            if self._is_sqlite_engine:
                stmt = sqlite_insert(MarketRegimeState).values(**row_payload)
                excluded = stmt.excluded
                session.execute(
                    stmt.on_conflict_do_update(
                        index_elements=['market'],
                        set_={
                            'as_of': excluded.as_of,
                            'regime': excluded.regime,
                            'raw_regime': excluded.raw_regime,
                            'volatility_bucket': excluded.volatility_bucket,
                            'raw_volatility_bucket': excluded.raw_volatility_bucket,
                            'pending_regime': excluded.pending_regime,
                            'pending_count': excluded.pending_count,
                            'payload': excluded.payload,
                            'updated_at': excluded.updated_at,
                        },
                    )
                )
                return

            row = session.execute(
                select(MarketRegimeState).where(MarketRegimeState.market == market_key)
            ).scalar_one_or_none()
            if row is None:
                session.add(MarketRegimeState(**row_payload))
                return
            for key, value in row_payload.items():
                setattr(row, key, value)

        self._run_write_transaction("market_regime_state", _write)

    def get_llm_usage_summary(
        self,
        from_dt: datetime,
        to_dt: datetime,
    ) -> Dict[str, Any]:
        """Return aggregated token usage between from_dt and to_dt.

        Returns a dict with keys:
          total_calls, total_tokens,
          by_call_type: list of {call_type, calls, total_tokens},
          by_model:     list of {model, calls, total_tokens}
        """
        with self.session_scope() as session:
            base_filter = and_(
                LLMUsage.called_at >= from_dt,
                LLMUsage.called_at <= to_dt,
            )

            # Overall totals
            totals = session.execute(
                select(
                    func.count(LLMUsage.id).label("calls"),
                    func.coalesce(func.sum(LLMUsage.total_tokens), 0).label("tokens"),
                ).where(base_filter)
            ).one()

            # Breakdown by call_type
            by_type_rows = session.execute(
                select(
                    LLMUsage.call_type,
                    func.count(LLMUsage.id).label("calls"),
                    func.coalesce(func.sum(LLMUsage.total_tokens), 0).label("tokens"),
                )
                .where(base_filter)
                .group_by(LLMUsage.call_type)
                .order_by(desc(func.sum(LLMUsage.total_tokens)))
            ).all()

            # Breakdown by model
            by_model_rows = session.execute(
                select(
                    LLMUsage.model,
                    func.count(LLMUsage.id).label("calls"),
                    func.coalesce(func.sum(LLMUsage.total_tokens), 0).label("tokens"),
                )
                .where(base_filter)
                .group_by(LLMUsage.model)
                .order_by(desc(func.sum(LLMUsage.total_tokens)))
            ).all()

        return {
            "total_calls": totals.calls,
            "total_tokens": totals.tokens,
            "by_call_type": [
                {"call_type": r.call_type, "calls": r.calls, "total_tokens": r.tokens}
                for r in by_type_rows
            ],
            "by_model": [
                {"model": r.model, "calls": r.calls, "total_tokens": r.tokens}
                for r in by_model_rows
            ],
        }


# 便捷函数
def get_db() -> DatabaseManager:
    """获取数据库管理器实例的快捷方式"""
    return DatabaseManager.get_instance()


def persist_llm_usage(
    usage: Dict[str, Any],
    model: str,
    call_type: str,
    stock_code: Optional[str] = None,
) -> None:
    """Fire-and-forget: write one LLM call record to llm_usage. Never raises."""
    try:
        db = DatabaseManager.get_instance()
        db.record_llm_usage(
            call_type=call_type,
            model=model,
            prompt_tokens=usage.get("prompt_tokens", 0) or 0,
            completion_tokens=usage.get("completion_tokens", 0) or 0,
            total_tokens=usage.get("total_tokens", 0) or 0,
            stock_code=stock_code,
        )
    except Exception as exc:
        logging.getLogger(__name__).warning("[LLM usage] failed to persist usage record: %s", exc)


if __name__ == "__main__":
    # 测试代码
    logging.basicConfig(level=logging.DEBUG)
    
    db = get_db()
    
    print("=== 数据库测试 ===")
    print(f"数据库初始化成功")
    
    # 测试检查今日数据
    has_data = db.has_today_data('600519')
    print(f"茅台今日是否有数据: {has_data}")
    
    # 测试保存数据
    test_df = pd.DataFrame({
        'date': [date.today()],
        'open': [1800.0],
        'high': [1850.0],
        'low': [1780.0],
        'close': [1820.0],
        'volume': [10000000],
        'amount': [18200000000],
        'pct_chg': [1.5],
        'ma5': [1810.0],
        'ma10': [1800.0],
        'ma20': [1790.0],
        'volume_ratio': [1.2],
    })
    
    saved = db.save_daily_data(test_df, '600519', 'TestSource')
    print(f"保存测试数据: {saved} 条")
    
    # 测试获取上下文
    context = db.get_analysis_context('600519')
    print(f"分析上下文: {context}")
