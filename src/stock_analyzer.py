# -*- coding: utf-8 -*-
"""
===================================
趋势交易分析器 - 基于用户交易理念
===================================

交易理念核心原则：
1. 严进策略 - 不追高，追求每笔交易成功率
2. 趋势交易 - MA5>MA10>MA20 多头排列，顺势而为
3. 效率优先 - 关注筹码结构好的股票
4. 买点偏好 - 在 MA5/MA10 附近回踩买入

技术标准：
- 多头排列：MA5 > MA10 > MA20
- 乖离率：(Close - MA5) / MA5 < 5%（不追高）
- 量能形态：缩量回调优先
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional
from enum import Enum

import pandas as pd
import numpy as np

from src.config import get_config

logger = logging.getLogger(__name__)


class TrendStatus(Enum):
    """趋势状态枚举"""
    STRONG_BULL = "强势多头"      # MA5 > MA10 > MA20，且间距扩大
    BULL = "多头排列"             # MA5 > MA10 > MA20
    WEAK_BULL = "弱势多头"        # MA5 > MA10，但 MA10 < MA20
    CONSOLIDATION = "盘整"        # 均线缠绕
    WEAK_BEAR = "弱势空头"        # MA5 < MA10，但 MA10 > MA20
    BEAR = "空头排列"             # MA5 < MA10 < MA20
    STRONG_BEAR = "强势空头"      # MA5 < MA10 < MA20，且间距扩大


class VolumeStatus(Enum):
    """量能状态枚举"""
    HEAVY_VOLUME_UP = "放量上涨"       # 量价齐升
    HEAVY_VOLUME_DOWN = "放量下跌"     # 放量杀跌
    SHRINK_VOLUME_UP = "缩量上涨"      # 无量上涨
    SHRINK_VOLUME_DOWN = "缩量回调"    # 缩量回调（好）
    NORMAL = "量能正常"


class BuySignal(Enum):
    """买入信号枚举"""
    STRONG_BUY = "强烈买入"       # 多条件满足
    BUY = "买入"                  # 基本条件满足
    HOLD = "持有"                 # 已持有可继续
    WAIT = "观望"                 # 等待更好时机
    SELL = "卖出"                 # 趋势转弱
    STRONG_SELL = "强烈卖出"      # 趋势破坏


class MACDStatus(Enum):
    """MACD状态枚举"""
    GOLDEN_CROSS_ZERO = "零轴上金叉"      # DIF上穿DEA，且在零轴上方
    GOLDEN_CROSS = "金叉"                # DIF上穿DEA
    BULLISH = "多头"                    # DIF>DEA>0
    CROSSING_UP = "上穿零轴"             # DIF上穿零轴
    CROSSING_DOWN = "下穿零轴"           # DIF下穿零轴
    BEARISH = "空头"                    # DIF<DEA<0
    DEATH_CROSS = "死叉"                # DIF下穿DEA


class RSIStatus(Enum):
    """RSI状态枚举"""
    OVERBOUGHT = "超买"        # RSI > 70
    STRONG_BUY = "强势买入"    # 50 < RSI < 70
    NEUTRAL = "中性"          # 40 <= RSI <= 60
    WEAK = "弱势"             # 30 < RSI < 40
    OVERSOLD = "超卖"         # RSI < 30


class IndicatorValidity(Enum):
    """单个指标的有效性状态（显式，不从默认枚举推断）。"""
    VALID = "valid"              # 输入窗口完整且输出有限
    INSUFFICIENT = "insufficient"  # 观测不足（样本不够/整段缺失）
    INVALID = "invalid"          # 输入存在缺口或非有限值


class SignalStatus(Enum):
    """综合信号资格状态。

    - OK: 所有 required 指标有效，可生成可执行信号
    - INSUFFICIENT_DATA: 缺少 required 观测，不生成可执行信号
    - INVALID_DATA: required 指标输入存在缺口/非法值，不生成可执行信号
    """
    OK = "ok"
    INSUFFICIENT_DATA = "insufficient_data"
    INVALID_DATA = "invalid_data"


# 参与默认可执行信号判定所必需的指标
REQUIRED_INDICATORS = ("ma", "volume", "macd", "rsi")


@dataclass
class TrendAnalysisResult:
    """趋势分析结果"""
    code: str
    
    # 趋势判断
    trend_status: TrendStatus = TrendStatus.CONSOLIDATION
    ma_alignment: str = ""           # 均线排列描述
    trend_strength: float = 0.0      # 趋势强度 0-100
    
    # 均线数据
    ma5: float = 0.0
    ma10: float = 0.0
    ma20: float = 0.0
    ma60: Optional[float] = None
    current_price: float = 0.0
    
    # 乖离率（与 MA5 的偏离度）
    bias_ma5: float = 0.0            # (Close - MA5) / MA5 * 100
    bias_ma10: float = 0.0
    bias_ma20: float = 0.0
    
    # 量能分析
    volume_status: VolumeStatus = VolumeStatus.NORMAL
    volume_ratio_5d: Optional[float] = None  # 当日成交量/前5日均量
    volume_trend: str = ""           # 量能趋势描述
    
    # 支撑压力
    support_ma5: bool = False        # MA5 是否构成支撑
    support_ma10: bool = False       # MA10 是否构成支撑
    resistance_levels: List[float] = field(default_factory=list)
    support_levels: List[float] = field(default_factory=list)

    # MACD 指标
    macd_dif: Optional[float] = None          # DIF 快线
    macd_dea: Optional[float] = None          # DEA 慢线
    macd_bar: Optional[float] = None           # MACD 柱状图
    macd_status: MACDStatus = MACDStatus.BULLISH
    macd_signal: str = ""            # MACD 信号描述

    # RSI 指标
    rsi_6: Optional[float] = None              # RSI(6) 短期
    rsi_12: Optional[float] = None             # RSI(12) 中期
    rsi_24: Optional[float] = None             # RSI(24) 长期
    rsi_status: RSIStatus = RSIStatus.NEUTRAL
    rsi_signal: str = ""              # RSI 信号描述

    # 买入信号
    buy_signal: BuySignal = BuySignal.WAIT
    signal_score: int = 0            # 综合评分 0-100
    signal_reasons: List[str] = field(default_factory=list)
    risk_factors: List[str] = field(default_factory=list)

    # 信号资格（显式有效性，防止用默认枚举当作有效指标参与评分）
    signal_status: SignalStatus = SignalStatus.OK
    actionable: bool = True          # False 时不生成可执行买卖信号
    score_status: str = "complete"   # complete / partial
    indicator_quality: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'code': self.code,
            'trend_status': self.trend_status.value,
            'ma_alignment': self.ma_alignment,
            'trend_strength': self.trend_strength,
            'ma5': self.ma5,
            'ma10': self.ma10,
            'ma20': self.ma20,
            'ma60': self.ma60,
            'current_price': self.current_price,
            'bias_ma5': self.bias_ma5,
            'bias_ma10': self.bias_ma10,
            'bias_ma20': self.bias_ma20,
            'volume_status': self.volume_status.value,
            'volume_ratio_5d': self.volume_ratio_5d,
            'volume_trend': self.volume_trend,
            'support_ma5': self.support_ma5,
            'support_ma10': self.support_ma10,
            'buy_signal': self.buy_signal.value,
            'signal_score': self.signal_score,
            'signal_reasons': self.signal_reasons,
            'risk_factors': self.risk_factors,
            'signal_status': self.signal_status.value,
            'actionable': self.actionable,
            'score_status': self.score_status,
            'indicator_quality': dict(self.indicator_quality),
            'macd_dif': self.macd_dif,
            'macd_dea': self.macd_dea,
            'macd_bar': self.macd_bar,
            'macd_status': self.macd_status.value,
            'macd_signal': self.macd_signal,
            'rsi_6': self.rsi_6,
            'rsi_12': self.rsi_12,
            'rsi_24': self.rsi_24,
            'rsi_status': self.rsi_status.value,
            'rsi_signal': self.rsi_signal,
        }


class StockTrendAnalyzer:
    """
    股票趋势分析器

    基于用户交易理念实现：
    1. 趋势判断 - MA5>MA10>MA20 多头排列
    2. 乖离率检测 - 不追高，偏离 MA5 超过 5% 不买
    3. 量能分析 - 偏好缩量回调
    4. 买点识别 - 回踩 MA5/MA10 支撑
    5. MACD 指标 - 趋势确认和金叉死叉信号
    6. RSI 指标 - 超买超卖判断
    """
    
    # 交易参数配置（BIAS_THRESHOLD 从 Config 读取，见 _generate_signal）
    VOLUME_SHRINK_RATIO = 0.7   # 缩量判断阈值（当日量/5日均量）
    VOLUME_HEAVY_RATIO = 1.5    # 放量判断阈值
    MA_SUPPORT_TOLERANCE = 0.02  # MA 支撑判断容忍度（2%）

    # MACD 参数（标准12/26/9）
    MACD_FAST = 12              # 快线周期
    MACD_SLOW = 26             # 慢线周期
    MACD_SIGNAL = 9             # 信号线周期

    # RSI 参数
    RSI_SHORT = 6               # 短期RSI周期
    RSI_MID = 12               # 中期RSI周期
    RSI_LONG = 24              # 长期RSI周期
    RSI_OVERBOUGHT = 70        # 超买阈值
    RSI_OVERSOLD = 30          # 超卖阈值
    
    def __init__(self):
        """初始化分析器"""
        pass
    
    def analyze(self, df: pd.DataFrame, code: str) -> TrendAnalysisResult:
        """
        分析股票趋势
        
        Args:
            df: 包含 OHLCV 数据的 DataFrame
            code: 股票代码
            
        Returns:
            TrendAnalysisResult 分析结果
        """
        result = TrendAnalysisResult(code=code)
        
        if df is None or df.empty or len(df) < 20:
            logger.warning(f"{code} 数据不足，无法进行趋势分析")
            result.risk_factors.append("数据不足，无法完成分析")
            result.signal_status = SignalStatus.INSUFFICIENT_DATA
            result.actionable = False
            result.score_status = "partial"
            result.indicator_quality = {
                key: IndicatorValidity.INSUFFICIENT.value for key in REQUIRED_INDICATORS
            }
            result.buy_signal = BuySignal.HOLD
            return result
        
        # 确保数据按日期排序
        df = df.sort_values('date').reset_index(drop=True)
        
        # 计算均线
        df = self._calculate_mas(df)

        # 计算 MACD 和 RSI
        df = self._calculate_macd(df)
        df = self._calculate_rsi(df)

        # 获取最新数据
        latest = df.iloc[-1]
        result.current_price = float(latest['close'])
        result.ma5 = float(latest['MA5'])
        result.ma10 = float(latest['MA10'])
        result.ma20 = float(latest['MA20'])
        result.ma60 = float(latest['MA60']) if pd.notna(latest['MA60']) else None
        if result.ma60 is None:
            result.risk_factors.append("不足60条行情，MA60不可用")

        # 1. 趋势判断
        self._analyze_trend(df, result)

        # 2. 乖离率计算
        self._calculate_bias(result)

        # 3. 量能分析
        self._analyze_volume(df, result)

        # 4. 支撑压力分析
        self._analyze_support_resistance(df, result)

        # 5. MACD 分析
        self._analyze_macd(df, result)

        # 6. RSI 分析
        self._analyze_rsi(df, result)

        # 7. 指标有效性评估（显式 valid/insufficient/invalid，供评分前置判断）
        result.indicator_quality = self._assess_indicator_quality(df, result)

        # 8. 生成买入信号（有效性作为评分前置条件）
        self._generate_signal(result)

        return result
    
    def _calculate_mas(self, df: pd.DataFrame) -> pd.DataFrame:
        """计算均线"""
        df = df.copy()
        df['MA5'] = df['close'].rolling(window=5).mean()
        df['MA10'] = df['close'].rolling(window=10).mean()
        df['MA20'] = df['close'].rolling(window=20).mean()
        if len(df) >= 60:
            df['MA60'] = df['close'].rolling(window=60).mean()
        else:
            df['MA60'] = np.nan
        return df

    def _calculate_macd(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        计算 MACD 指标

        公式：
        - EMA(12)：12日指数移动平均
        - EMA(26)：26日指数移动平均
        - DIF = EMA(12) - EMA(26)
        - DEA = EMA(DIF, 9)
        - MACD = (DIF - DEA) * 2
        """
        df = df.copy()

        # 计算快慢线 EMA
        ema_fast = df['close'].ewm(span=self.MACD_FAST, adjust=False).mean()
        ema_slow = df['close'].ewm(span=self.MACD_SLOW, adjust=False).mean()

        # 计算快线 DIF
        df['MACD_DIF'] = ema_fast - ema_slow

        # 计算信号线 DEA
        df['MACD_DEA'] = df['MACD_DIF'].ewm(span=self.MACD_SIGNAL, adjust=False).mean()

        # 计算柱状图
        df['MACD_BAR'] = (df['MACD_DIF'] - df['MACD_DEA']) * 2

        return df

    def _calculate_rsi(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        计算 RSI 指标

        公式：
        - RS = 平均上涨幅度 / 平均下跌幅度
        - RSI = 100 - (100 / (1 + RS))
        """
        df = df.copy()

        for period in [self.RSI_SHORT, self.RSI_MID, self.RSI_LONG]:
            # 计算价格变化
            delta = df['close'].diff()

            # 分离上涨和下跌
            # 注意：必须保留 NaN。此前用 where(cond, 0) 会把「缺失价格的差分」
            # 静默变成 0 涨跌，使缺口窗口被算成横盘并产生有限 RSI。
            gain = delta.clip(lower=0)
            loss = (-delta).clip(lower=0)

            # 计算平均涨跌幅
            avg_gain = gain.rolling(window=period).mean()
            avg_loss = loss.rolling(window=period).mean()

            # 计算 RS 和 RSI
            rs = avg_gain / avg_loss
            rsi = 100 - (100 / (1 + rs))

            # 填充 NaN 值
            rsi = rsi.where((avg_gain + avg_loss) != 0, 50)  # 完整窗口内横盘才记中性值

            # 添加到 DataFrame
            col_name = f'RSI_{period}'
            df[col_name] = rsi

        return df
    
    def _analyze_trend(self, df: pd.DataFrame, result: TrendAnalysisResult) -> None:
        """
        分析趋势状态
        
        核心逻辑：判断均线排列和趋势强度
        """
        ma5, ma10, ma20 = result.ma5, result.ma10, result.ma20
        
        # 判断均线排列
        if ma5 > ma10 > ma20:
            # 检查间距是否在扩大（强势）
            prev = df.iloc[-5] if len(df) >= 5 else df.iloc[-1]
            prev_spread = (prev['MA5'] - prev['MA20']) / prev['MA20'] * 100 if prev['MA20'] > 0 else 0
            curr_spread = (ma5 - ma20) / ma20 * 100 if ma20 > 0 else 0
            
            if curr_spread > prev_spread and curr_spread > 5:
                result.trend_status = TrendStatus.STRONG_BULL
                result.ma_alignment = "强势多头排列，均线发散上行"
                result.trend_strength = 90
            else:
                result.trend_status = TrendStatus.BULL
                result.ma_alignment = "多头排列 MA5>MA10>MA20"
                result.trend_strength = 75
                
        elif ma5 > ma10 and ma10 <= ma20:
            result.trend_status = TrendStatus.WEAK_BULL
            result.ma_alignment = "弱势多头，MA5>MA10 但 MA10≤MA20"
            result.trend_strength = 55
            
        elif ma5 < ma10 < ma20:
            prev = df.iloc[-5] if len(df) >= 5 else df.iloc[-1]
            prev_spread = (prev['MA20'] - prev['MA5']) / prev['MA5'] * 100 if prev['MA5'] > 0 else 0
            curr_spread = (ma20 - ma5) / ma5 * 100 if ma5 > 0 else 0
            
            if curr_spread > prev_spread and curr_spread > 5:
                result.trend_status = TrendStatus.STRONG_BEAR
                result.ma_alignment = "强势空头排列，均线发散下行"
                result.trend_strength = 10
            else:
                result.trend_status = TrendStatus.BEAR
                result.ma_alignment = "空头排列 MA5<MA10<MA20"
                result.trend_strength = 25
                
        elif ma5 < ma10 and ma10 >= ma20:
            result.trend_status = TrendStatus.WEAK_BEAR
            result.ma_alignment = "弱势空头，MA5<MA10 但 MA10≥MA20"
            result.trend_strength = 40
            
        else:
            result.trend_status = TrendStatus.CONSOLIDATION
            result.ma_alignment = "均线缠绕，趋势不明"
            result.trend_strength = 50
    
    def _calculate_bias(self, result: TrendAnalysisResult) -> None:
        """
        计算乖离率
        
        乖离率 = (现价 - 均线) / 均线 * 100%
        
        严进策略：乖离率超过 5% 不追高
        """
        price = result.current_price
        
        if result.ma5 > 0:
            result.bias_ma5 = (price - result.ma5) / result.ma5 * 100
        if result.ma10 > 0:
            result.bias_ma10 = (price - result.ma10) / result.ma10 * 100
        if result.ma20 > 0:
            result.bias_ma20 = (price - result.ma20) / result.ma20 * 100
    
    def _analyze_volume(self, df: pd.DataFrame, result: TrendAnalysisResult) -> None:
        """
        分析量能
        
        偏好：缩量回调 > 放量上涨 > 缩量上涨 > 放量下跌
        """
        if len(df) < 6:
            result.volume_trend = "不足6条行情，无法计算放量倍数"
            result.risk_factors.append(result.volume_trend)
            return

        latest = df.iloc[-1]
        prior_volumes = df['volume'].iloc[-6:-1].to_numpy(dtype=float)
        latest_volume = latest['volume']

        source_mixed = 'data_source' in df and df['data_source'].iloc[-6:].nunique(dropna=False) > 1
        if source_mixed:
            result.volume_trend = "成交量来源混合，无法计算放量倍数"
            result.risk_factors.append(result.volume_trend)
            return

        # 前5个交易日成交量必须全部有效：缺失一条即视为不足，
        # 不能依赖 pandas mean() 的 skipna 用4条算平均（会与基础指标入口不一致）。
        prior_complete = len(prior_volumes) == 5 and bool(np.isfinite(prior_volumes).all())

        prev_close = df['close'].iloc[-2]
        latest_close = latest['close']
        prices_valid = (
            np.isfinite(prev_close)
            and np.isfinite(latest_close)
            and float(prev_close) > 0
        )

        if not prior_complete or not np.isfinite(latest_volume) or not prices_valid:
            result.volume_trend = "前5个交易日成交量或价格存在缺失，无法计算放量倍数"
            result.risk_factors.append(result.volume_trend)
            return

        vol_5d_avg = float(prior_volumes.mean())
        if not np.isfinite(vol_5d_avg) or vol_5d_avg <= 0:
            result.volume_trend = "前5个交易日均量为0，无法计算放量倍数"
            result.risk_factors.append(result.volume_trend)
            return

        result.volume_ratio_5d = float(latest_volume) / vol_5d_avg
        if not np.isfinite(result.volume_ratio_5d):
            result.volume_ratio_5d = None
            result.volume_trend = "放量倍数非有限值，无法计算"
            result.risk_factors.append(result.volume_trend)
            return

        # 判断价格变化
        price_change = (float(latest_close) - float(prev_close)) / float(prev_close) * 100
        
        # 量能状态判断
        if result.volume_ratio_5d >= self.VOLUME_HEAVY_RATIO:
            if price_change > 0:
                result.volume_status = VolumeStatus.HEAVY_VOLUME_UP
                result.volume_trend = "放量上涨，多头力量强劲"
            else:
                result.volume_status = VolumeStatus.HEAVY_VOLUME_DOWN
                result.volume_trend = "放量下跌，注意风险"
        elif result.volume_ratio_5d <= self.VOLUME_SHRINK_RATIO:
            if price_change > 0:
                result.volume_status = VolumeStatus.SHRINK_VOLUME_UP
                result.volume_trend = "缩量上涨，上攻动能不足"
            else:
                result.volume_status = VolumeStatus.SHRINK_VOLUME_DOWN
                result.volume_trend = "缩量回调，洗盘特征明显（好）"
        else:
            result.volume_status = VolumeStatus.NORMAL
            result.volume_trend = "量能正常"
    
    def _analyze_support_resistance(self, df: pd.DataFrame, result: TrendAnalysisResult) -> None:
        """
        分析支撑压力位
        
        买点偏好：回踩 MA5/MA10 获得支撑
        """
        price = result.current_price
        
        # 检查是否在 MA5 附近获得支撑
        if result.ma5 > 0:
            ma5_distance = abs(price - result.ma5) / result.ma5
            if ma5_distance <= self.MA_SUPPORT_TOLERANCE and price >= result.ma5:
                result.support_ma5 = True
                result.support_levels.append(result.ma5)
        
        # 检查是否在 MA10 附近获得支撑
        if result.ma10 > 0:
            ma10_distance = abs(price - result.ma10) / result.ma10
            if ma10_distance <= self.MA_SUPPORT_TOLERANCE and price >= result.ma10:
                result.support_ma10 = True
                if result.ma10 not in result.support_levels:
                    result.support_levels.append(result.ma10)
        
        # MA20 作为重要支撑
        if result.ma20 > 0 and price >= result.ma20:
            result.support_levels.append(result.ma20)
        
        # 近期高点作为压力
        if len(df) >= 20:
            recent_high = df['high'].iloc[-20:].max()
            if recent_high > price:
                result.resistance_levels.append(recent_high)

    def _analyze_macd(self, df: pd.DataFrame, result: TrendAnalysisResult) -> None:
        """
        分析 MACD 指标

        核心信号：
        - 零轴上金叉：最强买入信号
        - 金叉：DIF 上穿 DEA
        - 死叉：DIF 下穿 DEA
        """
        if len(df) < self.MACD_SLOW:
            result.macd_signal = f"数据不足（需≥{self.MACD_SLOW}条行情），MACD 不可用"
            result.risk_factors.append(result.macd_signal)
            return

        # 缺口规则：EMA 在 NaN 缺口之后仍会继续产出有限值，仅靠输出 NaN 防御会漏判。
        # 要求最近 MACD_SLOW 条收盘价无缺口，否则不认证 MACD 有效。
        recent_closes = df['close'].iloc[-self.MACD_SLOW:].to_numpy(dtype=float)
        if len(recent_closes) < self.MACD_SLOW or not np.isfinite(recent_closes).all():
            result.macd_signal = "MACD 数据无效（最近窗口存在价格缺口）"
            result.risk_factors.append(result.macd_signal)
            return

        latest = df.iloc[-1]
        prev = df.iloc[-2]

        # 获取 MACD 数据
        macd_dif = latest['MACD_DIF']
        macd_dea = latest['MACD_DEA']
        macd_bar = latest['MACD_BAR']
        prev_dif = prev['MACD_DIF']
        prev_dea = prev['MACD_DEA']

        # NaN 防御：close 序列存在缺口时 MACD 可能为 NaN，
        # 若直接参与比较会静默落入「中性区域」分支，产生错误信号。
        if not (
            np.isfinite(macd_dif)
            and np.isfinite(macd_dea)
            and np.isfinite(macd_bar)
            and np.isfinite(prev_dif)
            and np.isfinite(prev_dea)
        ):
            result.macd_signal = "MACD 数据无效（存在缺失值）"
            result.risk_factors.append(result.macd_signal)
            return

        result.macd_dif = float(macd_dif)
        result.macd_dea = float(macd_dea)
        result.macd_bar = float(macd_bar)

        # 判断金叉死叉
        prev_dif_dea = prev_dif - prev_dea
        curr_dif_dea = result.macd_dif - result.macd_dea

        # 金叉：DIF 上穿 DEA
        is_golden_cross = prev_dif_dea <= 0 and curr_dif_dea > 0

        # 死叉：DIF 下穿 DEA
        is_death_cross = prev_dif_dea >= 0 and curr_dif_dea < 0

        # 零轴穿越
        prev_zero = prev['MACD_DIF']
        curr_zero = result.macd_dif
        is_crossing_up = prev_zero <= 0 and curr_zero > 0
        is_crossing_down = prev_zero >= 0 and curr_zero < 0

        # 判断 MACD 状态
        if is_golden_cross and curr_zero > 0:
            result.macd_status = MACDStatus.GOLDEN_CROSS_ZERO
            result.macd_signal = "⭐ 零轴上金叉，强烈买入信号！"
        elif is_crossing_up:
            result.macd_status = MACDStatus.CROSSING_UP
            result.macd_signal = "⚡ DIF上穿零轴，趋势转强"
        elif is_golden_cross:
            result.macd_status = MACDStatus.GOLDEN_CROSS
            result.macd_signal = "✅ 金叉，趋势向上"
        elif is_death_cross:
            result.macd_status = MACDStatus.DEATH_CROSS
            result.macd_signal = "❌ 死叉，趋势向下"
        elif is_crossing_down:
            result.macd_status = MACDStatus.CROSSING_DOWN
            result.macd_signal = "⚠️ DIF下穿零轴，趋势转弱"
        elif result.macd_dif > 0 and result.macd_dea > 0:
            result.macd_status = MACDStatus.BULLISH
            result.macd_signal = "✓ 多头排列，持续上涨"
        elif result.macd_dif < 0 and result.macd_dea < 0:
            result.macd_status = MACDStatus.BEARISH
            result.macd_signal = "⚠ 空头排列，持续下跌"
        else:
            result.macd_status = MACDStatus.BULLISH
            result.macd_signal = " MACD 中性区域"

    def _analyze_rsi(self, df: pd.DataFrame, result: TrendAnalysisResult) -> None:
        """
        分析 RSI 指标

        核心判断：
        - RSI > 70：超买，谨慎追高
        - RSI < 30：超卖，关注反弹
        - 40-60：中性区域
        """
        if len(df) <= self.RSI_LONG:
            result.rsi_signal = f"数据不足（需>{self.RSI_LONG}条行情），RSI 不可用"
            result.risk_factors.append(result.rsi_signal)
            return

        # 缺口规则：RSI(N) 依赖 N 个价格变化（即 N+1 个有效收盘），
        # 窗口内存在缺口时不认证有效，避免缺口被当作零涨跌。
        recent_closes = df['close'].iloc[-(self.RSI_LONG + 1):].to_numpy(dtype=float)
        if len(recent_closes) < self.RSI_LONG + 1 or not np.isfinite(recent_closes).all():
            result.rsi_signal = "RSI 数据无效（最近窗口存在价格缺口）"
            result.risk_factors.append(result.rsi_signal)
            return

        latest = df.iloc[-1]

        # 获取 RSI 数据
        rsi_short = latest[f'RSI_{self.RSI_SHORT}']
        rsi_mid_val = latest[f'RSI_{self.RSI_MID}']
        rsi_long = latest[f'RSI_{self.RSI_LONG}']

        # NaN 防御：close 序列存在缺口时 RSI 可能为 NaN，
        # 若直接参与比较会静默落入「超卖」分支，产生错误信号。
        if not (
            np.isfinite(rsi_short)
            and np.isfinite(rsi_mid_val)
            and np.isfinite(rsi_long)
        ):
            result.rsi_signal = "RSI 数据无效（存在缺失值）"
            result.risk_factors.append(result.rsi_signal)
            return

        result.rsi_6 = float(rsi_short)
        result.rsi_12 = float(rsi_mid_val)
        result.rsi_24 = float(rsi_long)

        # 以中期 RSI(12) 为主进行判断
        rsi_mid = result.rsi_12

        # 判断 RSI 状态
        if rsi_mid > self.RSI_OVERBOUGHT:
            result.rsi_status = RSIStatus.OVERBOUGHT
            result.rsi_signal = f"⚠️ RSI超买({rsi_mid:.1f}>70)，短期回调风险高"
        elif rsi_mid > 60:
            result.rsi_status = RSIStatus.STRONG_BUY
            result.rsi_signal = f"✅ RSI强势({rsi_mid:.1f})，多头力量充足"
        elif rsi_mid >= 40:
            result.rsi_status = RSIStatus.NEUTRAL
            result.rsi_signal = f" RSI中性({rsi_mid:.1f})，震荡整理中"
        elif rsi_mid >= self.RSI_OVERSOLD:
            result.rsi_status = RSIStatus.WEAK
            result.rsi_signal = f"⚡ RSI弱势({rsi_mid:.1f})，关注反弹"
        else:
            result.rsi_status = RSIStatus.OVERSOLD
            result.rsi_signal = f"⭐ RSI超卖({rsi_mid:.1f}<30)，反弹机会大"

    @staticmethod
    def _dedupe(items: List[str]) -> List[str]:
        """去重并保持顺序，避免评分分支覆盖上游已记录的风险/理由。"""
        seen = set()
        output: List[str] = []
        for item in items:
            if item and item not in seen:
                seen.add(item)
                output.append(item)
        return output

    @staticmethod
    def _infer_indicator_quality(key: str, result: TrendAnalysisResult) -> str:
        """缺少显式有效性映射时的兜底推断（例如直接调用 _generate_signal）。

        只能依据已有数值判断，无法校验输入窗口缺口，因此 analyze() 路径必须
        使用 _assess_indicator_quality() 的结果。
        """
        def _finite(value: Any) -> bool:
            try:
                return bool(np.isfinite(value))
            except (TypeError, ValueError):
                return False

        if key == 'ma':
            ok = (
                _finite(result.ma5) and _finite(result.ma10) and _finite(result.ma20)
                and result.ma5 > 0 and result.ma10 > 0 and result.ma20 > 0
            )
        elif key == 'volume':
            ok = _finite(result.volume_ratio_5d)
        elif key == 'macd':
            ok = _finite(result.macd_dif) and _finite(result.macd_dea) and _finite(result.macd_bar)
        elif key == 'rsi':
            ok = _finite(result.rsi_6) and _finite(result.rsi_12) and _finite(result.rsi_24)
        else:
            ok = False
        return IndicatorValidity.VALID.value if ok else IndicatorValidity.INSUFFICIENT.value

    def _assess_indicator_quality(
        self, df: pd.DataFrame, result: TrendAnalysisResult
    ) -> Dict[str, str]:
        """按输入窗口完整性显式判定每个指标的有效性。

        - VALID：窗口完整且输出有限
        - INSUFFICIENT：观测不足（样本不够或整段缺失）
        - INVALID：输入窗口存在缺口 / 非有限值
        """
        valid = IndicatorValidity.VALID.value
        insufficient = IndicatorValidity.INSUFFICIENT.value
        invalid = IndicatorValidity.INVALID.value

        quality: Dict[str, str] = {}
        closes = df['close']
        row_count = len(df)

        def _finite(value: Any) -> bool:
            try:
                return bool(np.isfinite(value))
            except (TypeError, ValueError):
                return False

        def _window_finite(series: pd.Series, size: int) -> bool:
            if row_count < size:
                return False
            values = series.iloc[-size:].to_numpy(dtype=float)
            return len(values) == size and bool(np.isfinite(values).all())

        # 均线 / 趋势：需要最近 20 条收盘有效
        if _window_finite(closes, 20) and _finite(result.ma5) and _finite(result.ma10) and _finite(result.ma20):
            quality['ma'] = valid
        elif row_count < 20:
            quality['ma'] = insufficient
        else:
            quality['ma'] = invalid

        # MA60 为可选指标，不参与 required 判定
        quality['ma60'] = valid if _finite(result.ma60) else insufficient

        # 量能：需要最近 6 条收盘 + 前 5 个交易日成交量有效
        source_mixed = (
            'data_source' in df
            and df['data_source'].iloc[-6:].nunique(dropna=False) > 1
        )
        if row_count < 6 or result.volume_ratio_5d is None:
            quality['volume'] = insufficient
        elif source_mixed or not _window_finite(closes, 6):
            quality['volume'] = invalid
        elif not _finite(result.volume_ratio_5d):
            quality['volume'] = invalid
        else:
            quality['volume'] = valid

        # MACD：预热 + 最近 MACD_SLOW 条收盘无缺口
        if row_count < self.MACD_SLOW:
            quality['macd'] = insufficient
        elif not _window_finite(closes, self.MACD_SLOW):
            quality['macd'] = invalid
        elif result.macd_dif is None or result.macd_dea is None or result.macd_bar is None:
            quality['macd'] = insufficient
        elif not (_finite(result.macd_dif) and _finite(result.macd_dea) and _finite(result.macd_bar)):
            quality['macd'] = invalid
        else:
            quality['macd'] = valid

        # RSI：预热 + 最近 RSI_LONG+1 条收盘无缺口
        rsi_window = self.RSI_LONG + 1
        if row_count < rsi_window:
            quality['rsi'] = insufficient
        elif not _window_finite(closes, rsi_window):
            quality['rsi'] = invalid
        elif result.rsi_6 is None or result.rsi_12 is None or result.rsi_24 is None:
            quality['rsi'] = insufficient
        elif not (_finite(result.rsi_6) and _finite(result.rsi_12) and _finite(result.rsi_24)):
            quality['rsi'] = invalid
        else:
            quality['rsi'] = valid

        return quality

    def _generate_signal(self, result: TrendAnalysisResult) -> None:
        """
        生成买入信号

        综合评分系统：
        - 趋势（30分）：多头排列得分高
        - 乖离率（20分）：接近 MA5 得分高
        - 量能（15分）：缩量回调得分高
        - 支撑（10分）：获得均线支撑得分高
        - MACD（15分）：金叉和多头得分高
        - RSI（10分）：超卖和强势得分高
        """
        score = 0
        reasons = []
        risks = []

        # 显式有效性：analyze() 路径会提供 indicator_quality；
        # 直接调用本方法时按已计算数值兜底推断，绝不从默认枚举假定有效。
        quality = dict(result.indicator_quality or {})
        for key in REQUIRED_INDICATORS:
            quality.setdefault(key, self._infer_indicator_quality(key, result))

        ma_valid = quality.get('ma') == IndicatorValidity.VALID.value
        volume_valid = quality.get('volume') == IndicatorValidity.VALID.value
        macd_valid = quality.get('macd') == IndicatorValidity.VALID.value
        rsi_valid = quality.get('rsi') == IndicatorValidity.VALID.value

        # === 趋势评分（30分）===
        trend_scores = {
            TrendStatus.STRONG_BULL: 30,
            TrendStatus.BULL: 26,
            TrendStatus.WEAK_BULL: 18,
            TrendStatus.CONSOLIDATION: 12,
            TrendStatus.WEAK_BEAR: 8,
            TrendStatus.BEAR: 4,
            TrendStatus.STRONG_BEAR: 0,
        }
        if ma_valid:
            trend_score = trend_scores.get(result.trend_status, 12)
            score += trend_score

            if result.trend_status in [TrendStatus.STRONG_BULL, TrendStatus.BULL]:
                reasons.append(f"✅ {result.trend_status.value}，顺势做多")
            elif result.trend_status in [TrendStatus.BEAR, TrendStatus.STRONG_BEAR]:
                risks.append(f"⚠️ {result.trend_status.value}，不宜做多")
        else:
            risks.append("⚠️ 均线趋势数据不足或存在缺口，本次不计入趋势评分")

        # === 乖离率评分（20分，强势趋势补偿）===
        bias = result.bias_ma5
        if bias != bias or bias is None:  # NaN or None defense
            bias = 0.0
        base_threshold = get_config().bias_threshold

        # Strong trend compensation: relax threshold for STRONG_BULL with high strength
        trend_strength = result.trend_strength if result.trend_strength == result.trend_strength else 0.0
        if result.trend_status == TrendStatus.STRONG_BULL and (trend_strength or 0) >= 70:
            effective_threshold = base_threshold * 1.5
            is_strong_trend = True
        else:
            effective_threshold = base_threshold
            is_strong_trend = False

        if not ma_valid:
            # 均线无效时不参与评分，避免把 NaN 兜底成 0 后仍然加分
            risks.append("⚠️ 乖离率依赖均线，均线无效时不计入评分")
        elif bias < 0:
            # Price below MA5 (pullback)
            if bias > -3:
                score += 20
                reasons.append(f"✅ 价格略低于MA5({bias:.1f}%)，回踩买点")
            elif bias > -5:
                score += 16
                reasons.append(f"✅ 价格回踩MA5({bias:.1f}%)，观察支撑")
            else:
                score += 8
                risks.append(f"⚠️ 乖离率过大({bias:.1f}%)，可能破位")
        elif bias < 2:
            score += 18
            reasons.append(f"✅ 价格贴近MA5({bias:.1f}%)，介入好时机")
        elif bias < base_threshold:
            score += 14
            reasons.append(f"⚡ 价格略高于MA5({bias:.1f}%)，可小仓介入")
        elif bias > effective_threshold:
            score += 4
            risks.append(
                f"❌ 乖离率过高({bias:.1f}%>{effective_threshold:.1f}%)，严禁追高！"
            )
        elif bias > base_threshold and is_strong_trend:
            score += 10
            reasons.append(
                f"⚡ 强势趋势中乖离率偏高({bias:.1f}%)，可轻仓追踪"
            )
        else:
            score += 4
            risks.append(
                f"❌ 乖离率过高({bias:.1f}%>{base_threshold:.1f}%)，严禁追高！"
            )

        # === 量能评分（15分）===
        volume_scores = {
            VolumeStatus.SHRINK_VOLUME_DOWN: 15,  # 缩量回调最佳
            VolumeStatus.HEAVY_VOLUME_UP: 12,     # 放量上涨次之
            VolumeStatus.NORMAL: 10,
            VolumeStatus.SHRINK_VOLUME_UP: 6,     # 无量上涨较差
            VolumeStatus.HEAVY_VOLUME_DOWN: 0,    # 放量下跌最差
        }
        vol_score = volume_scores.get(result.volume_status, 8)
        if volume_valid:
            score += vol_score

            if result.volume_status == VolumeStatus.SHRINK_VOLUME_DOWN:
                reasons.append("✅ 缩量回调，主力洗盘")
            elif result.volume_status == VolumeStatus.HEAVY_VOLUME_DOWN:
                risks.append("⚠️ 放量下跌，注意风险")
        else:
            risks.append("⚠️ 量能数据无效或不足，本次不计入量能评分")

        # === 支撑评分（10分）===
        if ma_valid:
            if result.support_ma5:
                score += 5
                reasons.append("✅ MA5支撑有效")
            if result.support_ma10:
                score += 5
                reasons.append("✅ MA10支撑有效")

        # === MACD 评分（15分）===
        macd_scores = {
            MACDStatus.GOLDEN_CROSS_ZERO: 15,  # 零轴上金叉最强
            MACDStatus.GOLDEN_CROSS: 12,      # 金叉
            MACDStatus.CROSSING_UP: 10,       # 上穿零轴
            MACDStatus.BULLISH: 8,            # 多头
            MACDStatus.BEARISH: 2,            # 空头
            MACDStatus.CROSSING_DOWN: 0,       # 下穿零轴
            MACDStatus.DEATH_CROSS: 0,        # 死叉
        }
        macd_score = macd_scores.get(result.macd_status, 5)
        if macd_valid:
            score += macd_score

            if result.macd_signal:
                if result.macd_status in [MACDStatus.GOLDEN_CROSS_ZERO, MACDStatus.GOLDEN_CROSS]:
                    reasons.append(f"✅ {result.macd_signal}")
                elif result.macd_status in [MACDStatus.DEATH_CROSS, MACDStatus.CROSSING_DOWN]:
                    risks.append(f"⚠️ {result.macd_signal}")
                else:
                    reasons.append(result.macd_signal)
        else:
            risks.append("⚠️ MACD 数据无效或不足，本次不计入 MACD 评分")

        # === RSI 评分（10分）===
        rsi_scores = {
            RSIStatus.OVERSOLD: 10,       # 超卖最佳
            RSIStatus.STRONG_BUY: 8,     # 强势
            RSIStatus.NEUTRAL: 5,        # 中性
            RSIStatus.WEAK: 3,            # 弱势
            RSIStatus.OVERBOUGHT: 0,       # 超买最差
        }
        rsi_score = rsi_scores.get(result.rsi_status, 5)
        if rsi_valid:
            score += rsi_score

            if result.rsi_signal:
                if result.rsi_status in [RSIStatus.OVERSOLD, RSIStatus.STRONG_BUY]:
                    reasons.append(f"✅ {result.rsi_signal}")
                elif result.rsi_status == RSIStatus.OVERBOUGHT:
                    risks.append(f"⚠️ {result.rsi_signal}")
                else:
                    reasons.append(result.rsi_signal)
        else:
            risks.append("⚠️ RSI 数据无效或不足，本次不计入 RSI 评分")

        # === 综合判断 ===
        # 合并上游风险（MA60/量能/MACD/RSI 已写入 result.risk_factors），不覆盖
        result.signal_reasons = self._dedupe(reasons)
        result.risk_factors = self._dedupe(list(result.risk_factors) + risks)
        result.signal_score = int(score) if np.isfinite(score) else 0

        missing = [
            key for key in REQUIRED_INDICATORS
            if quality.get(key) != IndicatorValidity.VALID.value
        ]

        if missing:
            # 数据不足或存在缺口：允许展示已有效指标，但不生成可执行买卖信号
            invalid = [
                key for key in missing
                if quality.get(key) == IndicatorValidity.INVALID.value
            ]
            result.signal_status = (
                SignalStatus.INVALID_DATA if invalid else SignalStatus.INSUFFICIENT_DATA
            )
            result.actionable = False
            result.score_status = "partial"
            result.buy_signal = BuySignal.HOLD
            pending = "、".join(
                ("%s(缺口/非法)" % key) if key in invalid else ("%s(不足)" % key)
                for key in missing
            )
            result.risk_factors = self._dedupe(
                result.risk_factors
                + [f"⚠️ 必需指标未达标：{pending}；本次不生成可执行买卖信号"]
            )
            return

        # 生成买入信号（调整阈值以适应新的100分制）
        result.signal_status = SignalStatus.OK
        result.actionable = True
        result.score_status = "complete"

        if score >= 75 and result.trend_status in [TrendStatus.STRONG_BULL, TrendStatus.BULL]:
            result.buy_signal = BuySignal.STRONG_BUY
        elif score >= 60 and result.trend_status in [TrendStatus.STRONG_BULL, TrendStatus.BULL, TrendStatus.WEAK_BULL]:
            result.buy_signal = BuySignal.BUY
        elif score >= 45:
            result.buy_signal = BuySignal.HOLD
        elif score >= 30:
            result.buy_signal = BuySignal.WAIT
        elif result.trend_status in [TrendStatus.BEAR, TrendStatus.STRONG_BEAR]:
            result.buy_signal = BuySignal.STRONG_SELL
        else:
            result.buy_signal = BuySignal.SELL
    
    def format_analysis(self, result: TrendAnalysisResult) -> str:
        """
        格式化分析结果为文本

        Args:
            result: 分析结果

        Returns:
            格式化的分析文本
        """
        lines = [
            f"=== {result.code} 趋势分析 ===",
            f"",
            f"📊 趋势判断: {result.trend_status.value}",
            f"   均线排列: {result.ma_alignment}",
            f"   趋势强度: {result.trend_strength}/100",
            f"",
            f"📈 均线数据:",
            f"   现价: {result.current_price:.2f}",
            f"   MA5:  {result.ma5:.2f} (乖离 {result.bias_ma5:+.2f}%)",
            f"   MA10: {result.ma10:.2f} (乖离 {result.bias_ma10:+.2f}%)",
            f"   MA20: {result.ma20:.2f} (乖离 {result.bias_ma20:+.2f}%)",
            f"",
            f"📊 量能分析: {result.volume_status.value}",
            f"   放量倍数(vs前5日): {f'{result.volume_ratio_5d:.2f}' if result.volume_ratio_5d is not None else '数据不足'}",
            f"   量能趋势: {result.volume_trend}",
            f"",
            f"📈 MACD指标: {result.macd_status.value}",
            f"   DIF: {format(result.macd_dif, '.4f') if result.macd_dif is not None else '数据不足'}",
            f"   DEA: {format(result.macd_dea, '.4f') if result.macd_dea is not None else '数据不足'}",
            f"   MACD: {format(result.macd_bar, '.4f') if result.macd_bar is not None else '数据不足'}",
            f"   信号: {result.macd_signal}",
            f"",
            f"📊 RSI指标: {result.rsi_status.value}",
            f"   RSI(6): {format(result.rsi_6, '.1f') if result.rsi_6 is not None else '数据不足'}",
            f"   RSI(12): {format(result.rsi_12, '.1f') if result.rsi_12 is not None else '数据不足'}",
            f"   RSI(24): {format(result.rsi_24, '.1f') if result.rsi_24 is not None else '数据不足'}",
            f"   信号: {result.rsi_signal}",
            f"",
            f"🎯 操作建议: {result.buy_signal.value}"
            + ("" if result.actionable else "（数据不足，仅展示有效指标，不构成可执行信号）"),
            f"   综合评分: {result.signal_score}/100"
            + ("" if result.score_status == "complete" else "（有效指标部分评分）"),
            f"   信号状态: {result.signal_status.value}",
        ]

        if result.signal_reasons:
            lines.append(f"")
            lines.append(f"✅ 买入理由:")
            for reason in result.signal_reasons:
                lines.append(f"   {reason}")

        if result.risk_factors:
            lines.append(f"")
            lines.append(f"⚠️ 风险因素:")
            for risk in result.risk_factors:
                lines.append(f"   {risk}")

        return "\n".join(lines)


def analyze_stock(df: pd.DataFrame, code: str) -> TrendAnalysisResult:
    """
    便捷函数：分析单只股票
    
    Args:
        df: 包含 OHLCV 数据的 DataFrame
        code: 股票代码
        
    Returns:
        TrendAnalysisResult 分析结果
    """
    analyzer = StockTrendAnalyzer()
    return analyzer.analyze(df, code)


if __name__ == "__main__":
    # 测试代码
    logging.basicConfig(level=logging.INFO)
    
    # 模拟数据测试
    import numpy as np
    
    dates = pd.date_range(start='2025-01-01', periods=60, freq='D')
    np.random.seed(42)
    
    # 模拟多头排列的数据
    base_price = 10.0
    prices = [base_price]
    for i in range(59):
        change = np.random.randn() * 0.02 + 0.003  # 轻微上涨趋势
        prices.append(prices[-1] * (1 + change))
    
    df = pd.DataFrame({
        'date': dates,
        'open': prices,
        'high': [p * (1 + np.random.uniform(0, 0.02)) for p in prices],
        'low': [p * (1 - np.random.uniform(0, 0.02)) for p in prices],
        'close': prices,
        'volume': [np.random.randint(1000000, 5000000) for _ in prices],
    })
    
    analyzer = StockTrendAnalyzer()
    result = analyzer.analyze(df, '000001')
    print(analyzer.format_analysis(result))
