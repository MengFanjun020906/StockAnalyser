# -*- coding: utf-8 -*-
"""
===================================
股票数据访问层
===================================

职责：
1. 封装股票数据的数据库操作
2. 提供日线数据查询接口
"""

import logging
from datetime import date, timedelta
from typing import Optional, List, Dict, Any

import pandas as pd
from sqlalchemy import and_, desc, func, select

from src.storage import DatabaseManager, StockDaily, StockMinuteBar

logger = logging.getLogger(__name__)


class StockRepository:
    """
    股票数据访问层
    
    封装 StockDaily 表的数据库操作
    """
    
    def __init__(self, db_manager: Optional[DatabaseManager] = None):
        """
        初始化数据访问层
        
        Args:
            db_manager: 数据库管理器（可选，默认使用单例）
        """
        self.db = db_manager or DatabaseManager.get_instance()
    
    def get_latest(self, code: str, days: int = 2) -> List[StockDaily]:
        """
        获取最近 N 天的数据
        
        Args:
            code: 股票代码
            days: 获取天数
            
        Returns:
            StockDaily 对象列表（按日期降序）
        """
        try:
            return self.db.get_latest_data(code, days)
        except Exception as e:
            logger.error(f"获取最新数据失败: {e}")
            return []
    
    def get_range(
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
        try:
            return self.db.get_data_range(code, start_date, end_date)
        except Exception as e:
            logger.error(f"获取日期范围数据失败: {e}")
            return []
    
    def save_dataframe(
        self,
        df: pd.DataFrame,
        code: str,
        data_source: str = "Unknown"
    ) -> int:
        """
        保存 DataFrame 到数据库
        
        Args:
            df: 包含日线数据的 DataFrame
            code: 股票代码
            data_source: 数据来源
            
        Returns:
            保存的记录数
        """
        try:
            return self.db.save_daily_data(df, code, data_source)
        except Exception as e:
            logger.error(f"保存日线数据失败: {e}")
            return 0

    def save_minute_bars(self, records: List[Dict[str, Any]], data_source: str = "BaostockMinute") -> int:
        """保存分钟线记录。"""
        try:
            return self.db.save_minute_bars(records, data_source=data_source)
        except Exception as e:
            logger.error(f"保存分钟线数据失败: {e}")
            return 0
    
    def has_today_data(self, code: str, target_date: Optional[date] = None) -> bool:
        """
        检查是否有指定日期的数据
        
        Args:
            code: 股票代码
            target_date: 目标日期（默认今天）
            
        Returns:
            是否存在数据
        """
        try:
            return self.db.has_today_data(code, target_date)
        except Exception as e:
            logger.error(f"检查数据存在失败: {e}")
            return False
    
    def get_analysis_context(
        self, 
        code: str, 
        target_date: Optional[date] = None
    ) -> Optional[Dict[str, Any]]:
        """
        获取分析上下文
        
        Args:
            code: 股票代码
            target_date: 目标日期
            
        Returns:
            分析上下文字典
        """
        try:
            return self.db.get_analysis_context(code, target_date)
        except Exception as e:
            logger.error(f"获取分析上下文失败: {e}")
            return None

    def get_start_daily(self, *, code: str, analysis_date: date) -> Optional[StockDaily]:
        """Return StockDaily for analysis_date (preferred) or nearest previous date."""
        with self.db.get_session() as session:
            row = session.execute(
                select(StockDaily)
                .where(and_(StockDaily.code == code, StockDaily.date <= analysis_date))
                .order_by(desc(StockDaily.date))
                .limit(1)
            ).scalar_one_or_none()
            return row

    def get_forward_bars(self, *, code: str, analysis_date: date, eval_window_days: int) -> List[StockDaily]:
        """Return forward daily bars after analysis_date, up to eval_window_days."""
        with self.db.get_session() as session:
            rows = session.execute(
                select(StockDaily)
                .where(and_(StockDaily.code == code, StockDaily.date > analysis_date))
                .order_by(StockDaily.date)
                .limit(eval_window_days)
            ).scalars().all()
            return list(rows)

    def get_forward_minute_bars(
        self,
        *,
        code: str,
        analysis_date: date,
        eval_window_days: int,
        frequency: str = "5",
        adjustflag: str = "3",
    ) -> List[StockMinuteBar]:
        """Return cached minute bars after analysis_date.

        `eval_window_days` is interpreted as trading-session count by the
        simulator. The SQL range uses a wider calendar window to cover weekends
        and holidays without requiring a trading calendar lookup here.
        """
        calendar_padding = max(10, int(eval_window_days or 1) * 2)
        end_date = analysis_date + timedelta(days=calendar_padding)
        with self.db.get_session() as session:
            rows = session.execute(
                select(StockMinuteBar)
                .where(
                    and_(
                        StockMinuteBar.code == code,
                        StockMinuteBar.bar_date > analysis_date,
                        StockMinuteBar.bar_date <= end_date,
                        StockMinuteBar.frequency == str(frequency),
                        StockMinuteBar.adjustflag == str(adjustflag),
                    )
                )
                .order_by(StockMinuteBar.bar_datetime)
            ).scalars().all()
            return list(rows)

    def get_minute_coverage(
        self,
        *,
        code: str,
        start_date: date,
        end_date: date,
        frequency: str = "5",
        adjustflag: str = "3",
    ) -> Dict[str, Any]:
        """Return count and date range for cached minute bars."""
        with self.db.get_session() as session:
            row = session.execute(
                select(
                    func.count(StockMinuteBar.id),
                    func.min(StockMinuteBar.bar_datetime),
                    func.max(StockMinuteBar.bar_datetime),
                ).where(
                    and_(
                        StockMinuteBar.code == code,
                        StockMinuteBar.bar_date >= start_date,
                        StockMinuteBar.bar_date <= end_date,
                        StockMinuteBar.frequency == str(frequency),
                        StockMinuteBar.adjustflag == str(adjustflag),
                    )
                )
            ).one()
        return {
            "count": int(row[0] or 0),
            "min_datetime": row[1].isoformat() if row[1] else None,
            "max_datetime": row[2].isoformat() if row[2] else None,
            "frequency": str(frequency),
            "adjustflag": str(adjustflag),
        }
