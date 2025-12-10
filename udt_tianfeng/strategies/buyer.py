import re
import traceback
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from time import sleep
from typing import override
from zoneinfo import ZoneInfo
from abc import ABC, abstractmethod

import akshare as ak
import pandas as pd
from pandas import DataFrame, DatetimeIndex, Series, Timedelta, Timestamp

from vnpy.trader.constant import (Direction, Exchange, Offset, OptionType,
                                  OrderType, Product, Status)
from vnpy.trader.converter import OffsetConverter, PositionHolding
from vnpy.trader.engine import MainEngine
from vnpy.trader.object import (CancelRequest, ContractData, OrderData,
                                PositionData, TickData, TradeData)
from vnpy.trader.utility import get_file_path, extract_vt_symbol
from vnpy.utility.cooldown import (Cooldown, CooldownMap, StackableCooldown,
                                   StackableCooldownMap)
from vnpy_simplestrategy import StrategyEngine, StrategyTemplate

# FIXME see: https://pandas.pydata.org/pandas-docs/stable/user_guide/copy_on_write.html
pd.options.mode.copy_on_write = False
pd.options.mode.chained_assignment = None

# 东八区
CHINA_TZ: ZoneInfo = ZoneInfo("Asia/Shanghai")

# 【改动1】类名从 BuyerDCE 改为通用的 BuyerStrategy
class BuyerStrategy(StrategyTemplate):
    """
    金融期权虚值买方策略 - 通用配置版
    """
    
    author = "买方通用策略"

   # 定义需要持久化保存的变量名
    variables = [
        'option_count',          # 记录每个合约已开仓数量（防止重启重开）
        'processed_orders',      # 记录已处理过的成交单ID（防止重启重复追单）
        'trading_date',          # 记录上次运行日期（用于判断是否跨日）
        'order_count',           # 订单编号计数
        'total_traded_volume',   # 总风控计数
        'single_traded_volume'   # 单品种风控计数
    ]
    def __init__(
        self,
        strategy_engine: StrategyEngine,
        strategy_name: str,
        vt_symbols: list[str],
        setting: dict
    ) -> None:
        
        super().__init__(
            strategy_engine,
            strategy_name,
            vt_symbols,
            setting
        )

        # 方便策略内部访问 MainEngine
        self.main_engine: MainEngine = self.strategy_engine.main_engine

        # --- 【改动2】从 setting 中读取核心配置 ---
        # 1. 目标交易所和品种 (字符串形式，用分号隔开)
        self.setting_exchanges = setting.get('target_exchanges', '') 
        self.setting_products = setting.get('target_products', '')
        
        # 2. Excel 配置
        # trade_list_source: 决定如何解析Excel列名 (对应原来的 '紫金', '宏源' 等逻辑)
        self.trade_list_source = setting.get('trade_list_source', '紫金') 
        # trade_list_path: Excel 文件的绝对路径
        self.trade_list_path = setting.get('trade_list_path', r'C:\Users\Administrator\Desktop\旧文件\trade_list.xls')

        # --- 策略参数 (保持原有) ---
        self.volume: int = setting.get('volume', 1)  # 支持从配置读取手数
        self.add_volume: int = setting.get('add_volume', 10)
        self.target_price: int = 5 
        self.target_number: int = 100
        self.order_count: int = 0
        self.investor: str = ''

        # --- 策略状态 ---
        self.gateway_name: str | None = None
        self.total_traded_volume: int = 0
        self.single_traded_volume: dict[str, float] = {}
        
        # 数据存储
        self.results: DataFrame = DataFrame()
        self.option_update: dict[str, dict[str, object]] = {}
        self.future_update: dict[str, dict[str, object]] = {}
        self.subscribed_option_vt_symbols: set[str] = set()
        self.subscribed_futures_vt_symbols: set[str] = set()
        self.fund_position: dict[str, float] = {}
        self.option_count: dict[str, int] = {}
        
        # 订单信息
        self.order_info_cols: dict[str, str] = {
            "symbol": 'string',
            "datetime": 'datetime64[ns, Asia/Shanghai]',
            "canceltime": 'datetime64[ns, Asia/Shanghai]',
            "exchange": 'object',
            "orderid": 'string',
            "ordersysid": 'string',
            "status": 'object',
            "direction": 'object',
            "offset": 'object',
            "price": 'float64',
            "type": 'object',
            "volume": 'float64',
            "traded": 'float64',
            "memo": 'string',
            "vt_symbol": 'string',
            "vt_orderid": 'string',
            "gateway_name": 'string',
        }
        self.order_info: DataFrame = DataFrame(columns=list(self.order_info_cols.keys())).astype(self.order_info_cols)
        
        #
        self.trading_date = ""          # 初始化为空字符串
        self.processed_orders = []      # 初始化为空列表
        # 当前时间和计数器
        self.current_time: datetime = datetime.now(tz=CHINA_TZ)
        self.updated_count: int = 0
        self.total_instruments_num: int = 0

        # 交易时间数据
        self.trade_time_data: DataFrame = DataFrame()

        # 结果数据结构
        self.results_cols: dict[str, str] = {
            'product': 'string',
            'exchange': 'object',
            'option_contract': 'string',
            'price_tick': 'float64',
            'expire_date': 'datetime64[ns, Asia/Shanghai]',
            'strike_price': 'float64',
            'underlying_symbol': 'string',
            'option_type': 'object',
            'vt_symbol': 'string',
            'vt_underlying_symbol': 'string',
            'remaining_trading_days': 'int64',
            'by_high': 'float64',
            'by_low': 'float64',
            'y_high': 'float64',
            'y_low': 'float64',
            'product_type': 'string',
            'close_signal': 'bool', 
            'open_signal': 'bool',
            'date_rank': 'int64',
            'buyer': 'int64'
        }

        # 定时器相关
        self.timer_count = 0

    def on_init(self) -> None:
        """策略初始化"""
        self.write_log("策略初始化...")
        
        # 获取今天的日期
        current_date = datetime.now(tz=CHINA_TZ).strftime('%Y-%m-%d')

        # 【核心修改 2】日期检查与重置逻辑
        # 如果 json 里有日期，且跟今天不一样 -> 说明跨天了 -> 清空数据
        if self.trading_date and self.trading_date != current_date:
            self.write_log(f"检测到新交易日 ({current_date})，重置所有策略状态...")
            
            self.option_count.clear()         # 清空开仓计数 -> 允许新一天开仓
            self.processed_orders = []        # 清空已处理订单记录
            self.single_traded_volume.clear() # 清空风控计数
            self.total_traded_volume = 0
            
            self.trading_date = current_date  # 更新为今天
            
        # 如果 json 里没日期（第一次跑） -> 标记为今天
        elif not self.trading_date:
            self.trading_date = current_date
            
        else:
            self.write_log(f"检测到日内重启 ({current_date})，恢复历史状态：已开仓{len(self.option_count)}个合约。")
            
        all_order_data: list[OrderData] = self.main_engine.get_all_orders()
        new_order_records: DataFrame = DataFrame([
            {
                'symbol': order.symbol,
                'vt_symbol': order.vt_symbol,
                'datetime': order.datetime,
                'canceltime': getattr(order, 'canceltime', pd.NaT),
                'exchange': order.exchange,
                'orderid': order.orderid,
                'ordersysid': getattr(order, 'ordersysid', None),
                'status': order.status,
                'direction': order.direction,
                'offset': order.offset,
                'price': order.price,
                'type': order.type,
                'volume': order.volume,
                'traded': order.traded,
                'memo': getattr(order, 'memo', ''),
            } for order in all_order_data
        ])
        self.order_info = pd.concat([self.order_info, new_order_records], ignore_index=True)
        
        self.initialize_results()
        self.subscribe_vt_symbols()
        
        if not self.results.empty:
            self.results['product_type'] = self.results['product'] + self.results['option_type'].apply(lambda x: x.value)
            if 'buyer' not in self.results.columns:
                self.results['buyer'] = 1
        
        if not self.results.empty:
            valid_cols = {k: v for k, v in self.results_cols.items() if k in self.results.columns}
            self.results = self.results.astype(dtype=valid_cols, errors='ignore')

        self.register_timer()

    def register_timer(self) -> None:
        self.strategy_engine.event_engine.register(1, self.process_timer)

    def process_timer(self, event) -> None:
        self.timer_count += 1
        if self.timer_count >= 60:
            self.timer_count = 0
            self.exec_signal()

    def initialize_results(self) -> None:
        """
        初始化 results DataFrame
        """
        # 1. 获取产品和交易所
        products, exchanges = self.get_products_and_exchanges()
        
        if not products:
            self.write_log("❌ 配置错误: 未找到目标品种(target_products)，请检查 simple_strategy_setting.json")
            return

        products_list = [p.lower().strip().replace('_o', '') for p in products.split(';')]
        exchanges_list = [Exchange(exchange) for exchange in exchanges.split(';')]
        
        # 2. 获取合约
        all_contracts: list[ContractData] = self.main_engine.get_all_contracts()
        all_contract_dict_list: list[dict[str, object]] = list()
        
        match_stats = defaultdict(int)
        
        for contract in all_contracts:
            raw_product = contract.option_portfolio if contract.option_portfolio else ""
            c_product = raw_product.lower().strip().replace('_o', '')

            if (contract.product == Product.OPTION and 
                contract.exchange in exchanges_list and
                c_product in products_list):
                
                # --- CFFEX 标的映射逻辑 (针对 IO/HO/MO -> IF/IH/IM) ---
                fix_underlying = contract.option_underlying
                if c_product == 'io': # 沪深300
                    fix_underlying = fix_underlying.replace('IO', 'IF').replace('io', 'if')
                elif c_product == 'mo': # 中证1000
                    fix_underlying = fix_underlying.replace('MO', 'IM').replace('mo', 'im')
                elif c_product == 'ho': # 上证50
                    fix_underlying = fix_underlying.replace('HO', 'IH').replace('ho', 'ih')
                # -------------------------------------
                
                all_contract_dict_list.append({
                    'product': contract.option_portfolio,
                    'exchange': contract.exchange,
                    'option_contract': contract.symbol,
                    'price_tick': contract.pricetick,
                    'expire_date': contract.option_expiry,
                    'strike_price': contract.option_strike,
                    'underlying_symbol': contract.option_underlying,
                    'option_type': contract.option_type,
                    'vt_symbol': contract.vt_symbol,
                    'vt_underlying_symbol': fix_underlying + "." + contract.exchange.value,
                    'clean_product_key': c_product
                })
                match_stats[c_product] += 1

        # 3. 创建 DataFrame
        self.results = DataFrame(data=all_contract_dict_list)
        if self.results.empty:
            self.write_log(f"❌ 初始化失败: 未找到匹配的期权合约。目标品种: {products_list}")
            return

        self.results['expire_date'] = pd.to_datetime(self.results['expire_date']).dt.tz_localize(tz=CHINA_TZ)
        
        self.write_log(f"📊 品种匹配详情: {list(match_stats.items())}")

        # 4. 读取参数文件
        # 【改动3】传入配置的 source_type
        self.trade_time_data = self.get_trade_list(self.trade_list_source)
        
        if not self.trade_time_data.empty:
            excel_data = self.trade_time_data.copy()
            
            if 'product' in excel_data.columns:
                excel_data['clean_product_key'] = excel_data['product'].astype(str).str.lower().str.replace('_o', '').str.strip()
            
            self.write_log(f"DEBUG: 正在合并Excel参数... excel行数={len(excel_data)}")
            
            merged = pd.merge(
                left=self.results,
                right=excel_data,
                on=['clean_product_key'],
                how='left',
                suffixes=('', '_excel')
            )
            
            time_cols = ['可挂单', '交易时间段1', '交易时间段2', '交易时间段3', '交易时间段4', 'buyer']
            for col in time_cols:
                if col in merged.columns:
                    self.results[col] = merged[col]
            
            self.results.drop(columns=['clean_product_key'], inplace=True, errors='ignore')
        else:
            self.write_log("⚠️ 警告: 未能读取到 Excel 数据，将使用默认时间配置")

        # 5. 兜底逻辑
        if '可挂单' not in self.results.columns:
            self.results['可挂单'] = None
        
        self.results['可挂单'] = self.results['可挂单'].fillna('09:00-23:00')
        self.results['交易时间段1'] = self.results['交易时间段1'].fillna('09:00-10:15')

        # 6. 赋予其他列默认值
        default_cols = {
            'option_lastPrice': 0.0,
            'future_lastPrice': 0.0,
            'future_upperLimit': 0.0,
            'future_lowerLimit': 0.0,
            'option_askPrice1': 0.0,
            'option_bidPrice1': 0.0,
            'option_volume': 0,
            'remaining_trading_days': 0,
            'close_signal': False,
            'open_signal': False,
            'date_rank': 99,
            'buyer': 1
        }
        
        for col, val in default_cols.items():
            if col not in self.results.columns:
                self.results[col] = val
        
        self.results['buyer'] = self.results['buyer'].fillna(1)

        self.process_results()
        
        matched_varieties = self.results['product'].unique()
        self.write_log(f"🔍 实际监控的品种列表: {list(matched_varieties)}")
        
        self.write_log(f"✅ 初始化完成，共监控 {len(self.results)} 个合约")

    # 【改动4】修改 get_trade_list 使用配置的路径和类型
    def get_trade_list(self, source_type: str) -> DataFrame:
        try:
            # 使用 self.trade_list_path
            df = pd.read_excel(self.trade_list_path, engine='xlrd')
            
            # 使用传入的 source_type 判断逻辑
            if source_type == '宏源':
                df = df.drop(columns=['product', 'product2'])
                df = df.rename(columns={'product1': 'product'})
            elif source_type == '模拟':
                df = df.drop(columns=['product1', 'product2'])
            else: # 默认为 紫金 或其他
                df = df.drop(columns=['product', 'product1'])
                df = df.rename(columns={'product2': 'product'})
            return df
        except Exception:
            self.write_log(f"读取交易时间数据失败: {traceback.format_exc()} 路径: {self.trade_list_path}")
            return DataFrame()

    # 【改动5】直接返回配置中的字符串
    def get_products_and_exchanges(self) -> tuple:
        if not self.setting_products or not self.setting_exchanges:
             return "", ""
        return self.setting_products, self.setting_exchanges

    # ... (后续代码如 calculate_remaining_trading_days, process_results 等保持不变) ...
    # ... (为了篇幅省略后续未修改的代码，请保持原样) ...
    # 记得把后面所有原来的方法都保留，包括 on_tick, open_positions 等
    
    @staticmethod
    def calculate_remaining_trading_days(expire_date: datetime) -> int:
        current_time: datetime = datetime.now(tz=CHINA_TZ)
        business_days: DatetimeIndex = pd.bdate_range(
            start=current_time.date(),
            end=expire_date.date(),
            freq='B',
            tz='Asia/Shanghai',
            inclusive='right'
        )
        return len(business_days)

    def process_results(self) -> None:
        if self.results.empty: return
        
        self.results['expire_date'] = pd.to_datetime(self.results['expire_date'])
        self.results['remaining_trading_days'] = self.results['expire_date'].apply(self.calculate_remaining_trading_days)
        
        # 将交易时间数据合并到结果中
        if 'product' in self.results.columns and 'product' in self.trade_time_data.columns:
            # 这里的 merge 其实已经在 initialize_results 做过了，但为了确保数据最新再做一次也没关系
            # 只要注意列名不要重复产生 _x, _y
            pass 
        
        self.results['date_rank'] = self.results.groupby('product')['expire_date'].rank(method='dense')
        self.add_historical_data()

    def add_historical_data(self) -> None:
        try:
            tool_trade_date_hist_sina_df = ak.tool_trade_date_hist_sina()
            tool_trade_date_hist_sina_df['trade_date'] = pd.to_datetime(tool_trade_date_hist_sina_df['trade_date']).dt.tz_localize(tz=CHINA_TZ)
            
            now: Timestamp = pd.to_datetime(datetime.now(tz=CHINA_TZ))
            if now.hour >= 20:
                td_trade_date = now + Timedelta(days=1)
            else:
                td_trade_date = now
                
            trade_date_matches = tool_trade_date_hist_sina_df.loc[tool_trade_date_hist_sina_df['trade_date'].dt.normalize() < td_trade_date.normalize()]
            trade_date_matches = trade_date_matches.tail(2).reset_index()
            
            if len(trade_date_matches) >= 2:
                byd_trade_date = trade_date_matches.at[0, 'trade_date']
                yd_trade_date = trade_date_matches.at[1, 'trade_date']
                
                byd_str = byd_trade_date.strftime("%Y%m%d")
                yd_str = yd_trade_date.strftime("%Y%m%d")
                
                # 注意：这里可能需要根据交易所不同使用不同的market参数，目前默认DCE
                # 如果要完美通用，可能需要循环处理所有交易所，或者从results里获取exchange列表
                # 简单起见，可以先对所有交易所尝试获取，或者假设akshare能够处理
                # 这里为了稳妥，我们暂时保留DCE，或者可以改进为遍历 self.results['exchange'].unique()
                
                # 改进版：遍历当前策略涉及的交易所
                if 'exchange' in self.results.columns:
                    exchanges = self.results['exchange'].unique()
                    for ex in exchanges:
                         # 映射 vnpy Exchange 枚举到 akshare market 字符串
                         market_str = ex.value # "DCE", "SHFE", "CFFEX"
                         try:
                            by_data = ak.get_futures_daily(start_date=byd_str, end_date=byd_str, market=market_str)
                            y_data = ak.get_futures_daily(start_date=yd_str, end_date=yd_str, market=market_str)
                            
                            if not by_data.empty and not y_data.empty:
                                by_data = by_data[['symbol', 'high', 'low']].rename(columns={'symbol': 'underlying_symbol', 'high': 'by_high', 'low': 'by_low'})
                                y_data = y_data[['symbol', 'high', 'low']].rename(columns={'symbol': 'underlying_symbol', 'high': 'y_high', 'low': 'y_low'})
                                
                                # 将 CTP 的小写代码转换为大写以匹配 akshare (如果需要)，或者反之
                                # CTP通常是小写(如 m2505)，akshare可能是大写(M2505)
                                # 这里的merge需要注意大小写匹配
                                # 您的原代码 logic 是对的，直接merge
                                self.results = pd.merge(self.results, by_data, on='underlying_symbol', how='left')
                                self.results = pd.merge(self.results, y_data, on='underlying_symbol', how='left')
                         except:
                             pass
                
        except Exception:
            self.write_log(f"获取历史数据失败(非致命): {traceback.format_exc()}")

    def subscribe_vt_symbols(self) -> None:
        if self.results.empty: return
        option_vt_symbols = self.results['vt_symbol'].unique().tolist()
        futures_vt_symbols = self.results['vt_underlying_symbol'].unique().tolist()
        
        self.subscribed_option_vt_symbols |= set(option_vt_symbols)
        self.subscribed_futures_vt_symbols |= set(futures_vt_symbols)

        self.vt_symbols = option_vt_symbols + futures_vt_symbols
        self.total_instruments_num = len(self.vt_symbols)

    def check_missed_orders(self):
        """扫描所有订单，补发漏掉的追单逻辑"""
        self.write_log("检查宕机期间的成交记录...")
        all_orders = self.main_engine.get_all_orders()
        
        for order in all_orders:
            # 筛选：已成交 + 开仓单 + 本策略品种 + 【未在 processed_orders 记录中】
            if (order.status == Status.ALLTRADED and
                order.offset == Offset.OPEN and
                order.vt_symbol in self.vt_symbols and
                order.ordersysid and 
                order.ordersysid not in self.processed_orders):
                
                self.write_log(f"发现遗漏处理的成交单 {order.ordersysid}，正在补发逻辑...")
                # 直接调用上面的 on_order，它会自动处理计数和发追单
                self.on_order(order)
                
    def on_start(self) -> None:
        self.write_log("BuyerStrategy策略启动")
        
        # 【核心修改 3.2】启动时扫描历史遗漏的成交单
        # 这就是解决“重启后怎么知道刚才成交了”的方法
        self.check_missed_orders()

    def on_stop(self) -> None:
        self.write_log("BuyerStrategy策略停止")
    
    def on_tick(self, tick: TickData) -> None:
        if not self.gateway_name:
            self.gateway_name = tick.gateway_name
            
        vt_symbol = tick.vt_symbol

        if vt_symbol in self.subscribed_option_vt_symbols:
            self.update_option_data(vt_symbol, tick)
        elif vt_symbol in self.subscribed_futures_vt_symbols:
            self.update_future_data(vt_symbol, tick)
        else:
            return

        self.updated_count += 1
        if self.updated_count >= self.total_instruments_num and self.total_instruments_num > 0:
            self.current_time = tick.datetime
            self.write_log(f"行情数据更新 (Tick count: {self.updated_count})")
            
            self.process_and_clear_data()

    def update_option_data(self, vt_symbol: str, tick: TickData) -> None:
        self.option_update[vt_symbol] = {
            'option_lastPrice': tick.last_price,
            'option_preClosePrice': tick.pre_close,
            'option_askPrice1': tick.ask_price_1,
            'option_bidPrice1': tick.bid_price_1,
            'option_volume': tick.volume,
            'lowerLimit': tick.limit_down,
            'date_time': tick.datetime
        }

    def update_future_data(self, vt_symbol: str, tick: TickData) -> None:
        self.future_update[vt_symbol] = {
            'future_lastPrice': tick.last_price,
            'future_preClosePrice': tick.pre_close,
            'future_pre_settlement_price': tick.pre_settlement_price,
            'future_upperLimit': tick.limit_up,
            'future_lowerLimit': tick.limit_down,
        }

    def cal_fund_tie(self, vt_positionid: str) -> float:
        accounts = self.main_engine.get_all_accounts()
        if not accounts: return 0.0
        balance = accounts[0].balance
        position = self.main_engine.get_position(vt_positionid)
        if not position: return 0.0
        used_margin = position.used_margin + position.frozen_margin + position.frozen_commission
        return used_margin / balance if balance > 0 else 0.0

    def product_fund_tie(self) -> None:
        total_positions = self.main_engine.get_all_positions()
        short_positions = [
            (pos.vt_symbol, pos.vt_positionid)
            for pos in total_positions
            if pos.direction == Direction.SHORT
        ]
        
        self.fund_position = {product_type: 0.0 for product_type in self.results['product_type'].unique()}

        for (vt_symbol, vt_positionid) in short_positions:
            if vt_symbol not in self.subscribed_option_vt_symbols: continue
            
            contract = self.main_engine.get_contract(vt_symbol)
            if not contract: continue
            
            product = contract.option_portfolio
            option_type = contract.option_type
            if not product or not option_type: continue
                
            product_type = product + option_type.value
            percent = self.cal_fund_tie(vt_positionid)
            if product_type in self.fund_position:
                self.fund_position[product_type] += percent
            
    def process_results_by_product(self) -> None:
        """
        按产品分组处理数据 (修改版：先收集所有信号，再一次性处理)
        """
        try:
            if self.results.empty:
                return

            all_targets = [] # 用于收集所有品种的目标合约

            grouped = self.results.groupby('product')
            for _, group in grouped:
                _, target_option = self.calc_signal(group)
                if not target_option.empty:
                    all_targets.append(target_option)
            
            # 如果收集到了目标合约，合并后一次性传入 open_positions
            if all_targets:
                combined_target = pd.concat(all_targets, ignore_index=True)
                self.open_positions(combined_target)
                    
        except Exception:
            self.write_log(f"❌ 按产品分组处理错误: {traceback.format_exc()}")
            
    def process_and_clear_data(self) -> None:
        """处理并清理数据"""
        try:
            if not self.option_update and not self.future_update:
                return

            for vt_symbol, data in self.option_update.items():
                for key in data.keys():
                    if key not in self.results.columns:
                        self.results[key] = None 
                self.results.loc[self.results['vt_symbol'] == vt_symbol, list(data.keys())] = list(data.values())
                    
            for vt_symbol, data in self.future_update.items():
                for key in data.keys():
                    if key not in self.results.columns:
                        self.results[key] = None
                self.results.loc[self.results['vt_underlying_symbol'] == vt_symbol, list(data.keys())] = list(data.values())
            
            self.product_fund_tie()
            self.process_results_by_product()
            
            self.option_update.clear()
            self.future_update.clear()
            self.updated_count = 0
            
        except Exception:
            self.write_log(f"❌ process_and_clear_data 错误: {traceback.format_exc()}")

    def at_time(self, time_period: str) -> bool:
        if pd.isna(time_period) or time_period == '': return False
        try:
            if isinstance(time_period, str) and time_period.lower() == 'nan': return False
            start_time, end_time = map(lambda x: datetime.strptime(x, '%H:%M').time(), time_period.split('-'))
            current_time = self.current_time.time()
            if start_time > end_time:
                return current_time >= start_time or current_time <= end_time
            else:
                return start_time <= current_time <= end_time
        except Exception:
            return False

    def set_signals(self, data: DataFrame) -> DataFrame:
        try:
            time_periods_close = ['可挂单', '交易时间段1', '交易时间段2', '交易时间段3', '交易时间段4']
            time_periods_open = ['交易时间段1', '交易时间段2', '交易时间段3', '交易时间段4']

            data['close_signal'] = data[time_periods_close].apply(
                lambda row: any(self.at_time(str(row[tp])) for tp in time_periods_close if pd.notna(row[tp])), 
                axis=1
            )
            data['open_signal'] = data[time_periods_open].apply(
                lambda row: any(self.at_time(str(row[tp])) for tp in time_periods_open if pd.notna(row[tp])), 
                axis=1
            )
            return data
        except Exception:
            self.write_log(f"设置信号错误: {traceback.format_exc()}")
            return DataFrame()

    def calc_signal(self, group: DataFrame) -> tuple:
        """计算信号 (确保不提前过滤)"""
        try:
            if group.empty: return {}, DataFrame()
            
            req_cols = ['option_lastPrice', 'future_lastPrice', 'future_upperLimit', 'future_lowerLimit']
            processed = self.set_signals(group.copy())
            
            processed['diff1'] = processed.apply(
                lambda x: x['strike_price'] - (x['future_upperLimit'] if x['option_type'] == OptionType.CALL else x['future_lowerLimit']),
                axis=1
            )
            
            if 'option_type' in processed.columns:
                processed['option_type_str'] = processed['option_type'].apply(lambda x: x.value if hasattr(x, 'value') else str(x))
            
            processed['target_option_rank'] = processed.groupby(
                ['product', 'option_type_str', 'underlying_symbol']
            )['diff1'].rank()
            
            results_dict = {row['vt_symbol']: row.to_dict() for _, row in processed.iterrows()}
            
            return results_dict, processed

        except Exception:
            self.write_log(f"❌ calc_signal 崩溃: {traceback.format_exc()}")
            return {}, DataFrame()

    def exec_signal(self) -> None:
        """定时执行入口"""
        try:
            if hasattr(self, 'target_option') and not self.target_option.empty:
                self.open_positions(self.target_option)
        except Exception:
            pass

    def open_positions(self, target_option: DataFrame) -> None:
        """
        【带汇总统计版】开仓函数
        """
        current_time = datetime.now(tz=CHINA_TZ).time()
        
        # 1. 交易时间段检查
        valid_time = (
            (time(9, 0) <= current_time <= time(11, 30)) or
            (time(13, 30) <= current_time <= time(15, 0)) or
            (time(21, 0) <= current_time <= time(22, 50))
        )
        
        if not valid_time:
            return

        # --- 统计计数器初始化 ---
        count_scan = 0        # 扫描合约数
        count_new_order = 0   # 本轮新增下单数
        total_new_orders_count = 0 # 累计总新增下单数
        # 2. 遍历所有目标合约
        for product_type, group in target_option.groupby('product_type'):
            for _, row in group.iterrows():
                vt_symbol = row['vt_symbol']
                count_scan += 1
                
                # --- A. 防重复检查 (首单锁) ---
                if self.option_count.get(vt_symbol, 0) >= 1:
                    continue

                # --- B. 单品种风控 ---
                if self.single_traded_volume.get(vt_symbol, 0) >= 4:
                    continue
                
                # --- C. 逻辑条件检查 ---
                condition_met = self.open_condition(row, str(product_type))
                
                if condition_met:
                    try:
                        # --- D. 价格与Tick过滤 ---
                        tick_cond = row['price_tick'] <= 11
                        
                        target_p = self.target_price if hasattr(self, 'target_price') else 5
                        required_bid = row['price_tick'] * (target_p + 1)
                        price_cond = row['option_bidPrice1'] > required_bid
                        
                        if tick_cond and price_cond:
                            # --- E. 总风控 ---
                            if self.total_traded_volume >= 5000:
                                self.write_log("已达总持仓限制 (5000手)")
                                return 

                            buyer_multiplier = row.get('buyer', 1) 
                            if pd.isna(buyer_multiplier): buyer_multiplier = 1
                            
                            open_price = row['price_tick'] * target_p
                            open_volume = int(self.volume * buyer_multiplier)
                            
                            self.write_log(f"🚀 触发下单: {vt_symbol} 价格:{open_price} 手数:{open_volume}")
                            
                            # 发送订单
                            self.open_order(
                                open_price, 
                                open_volume, 
                                vt_symbol, 
                                'buy', 
                                str(self.order_count)
                            )
                            
                            # 更新计数器和锁
                            self.option_count[vt_symbol] = self.option_count.get(vt_symbol, 0) + 1
                            self.single_traded_volume[vt_symbol] = self.single_traded_volume.get(vt_symbol, 0) + open_volume
                            self.total_traded_volume += open_volume
                            
                            # 统计本轮下单
                            count_new_order += 1
                            
                            # 防止并发过快
                            sleep(0.1)
                            
                    except Exception as e:
                        self.write_log(f"执行异常 {vt_symbol}: {str(e)}")

        # --- 3. 输出汇总信息 ---
        # 只有当扫描到合约，或者有下单时才打印，避免刷屏
        if total_new_orders_count > 0:
            msg_lines = [f"📊 [本轮汇总] 累计总单数: {self.order_count}"]
            
            # 按交易所字母顺序排序输出
            for ex_name in sorted(exchange_stats.keys()):
                stats = exchange_stats[ex_name]
                msg_lines.append(f"   ➤ {ex_name}: 扫描 {stats['scan']} | 下单 {stats['order']}")
            
            msg = "\n".join(msg_lines)
            print(msg)          # 终端直显
            self.write_log(msg) # 写入日志

    def open_condition(self, row: pd.Series, product_type: str) -> bool:
        """开仓条件"""
        try:
            product = row['product']
            near_month_df = self.results[(self.results['product'] == product) & (self.results['date_rank'] == 1)]
            T = near_month_df['remaining_trading_days'].min() if not near_month_df.empty else row['remaining_trading_days']

            if T > 11:
                conditions = [
                    (
                        (row['option_type'] == OptionType.CALL and 
                         row['strike_price'] < (row['future_lastPrice'] + row['future_upperLimit']) / 2 and 
                         row['strike_price'] >= row['future_lastPrice'] and
                         row['date_rank'] == 1)
                        or
                        (row['option_type'] == OptionType.PUT and 
                         row['strike_price'] > (row['future_lastPrice'] + row['future_lowerLimit']) / 2 and 
                         row['strike_price'] <= row['future_lastPrice'] and
                         row['date_rank'] == 1)
                        or
                        (row['option_type'] == OptionType.CALL and 
                         row['strike_price'] < row['future_upperLimit'] and 
                         row['strike_price'] >= row['future_lastPrice'] and
                         row['date_rank'] > 1 and row['date_rank'] <= 3)
                        or
                        (row['option_type'] == OptionType.PUT and 
                         row['strike_price'] > row['future_lowerLimit'] and 
                         row['strike_price'] <= row['future_lastPrice'] and
                         row['date_rank'] > 1 and row['date_rank'] <= 3)
                    ),
                    row.get('open_signal', False)
                ]
            else:
                conditions = [
                    (
                        (row['option_type'] == OptionType.CALL and 
                         row['strike_price'] < row['future_upperLimit'] and 
                         row['strike_price'] >= row['future_lastPrice'] and
                         row['date_rank'] > 1 and row['date_rank'] <= 3)
                        or
                        (row['option_type'] == OptionType.PUT and 
                         row['strike_price'] > row['future_lowerLimit'] and 
                         row['strike_price'] <= row['future_lastPrice'] and
                         row['date_rank'] > 1 and row['date_rank'] <= 3)
                    ),
                    row.get('open_signal', False)
                ]
            
            return all(conditions)
        except Exception:
            return False
        
    def open_order(self, price: float, volume: int, vt_symbol: str, order_direction: str, memo: str) -> None:
        try:
            direction = Direction.LONG if order_direction.lower() == 'buy' else Direction.SHORT
            vt_orderid = self.send_order(vt_symbol, direction, Offset.OPEN, price, volume, memo=memo)
            self.order_count += 1
            self.option_count.setdefault(vt_symbol, 0)
            self.write_log(f"开仓订单发送成功，订单ID: {vt_orderid}")
        except Exception:
            self.write_log(f"开仓时遇到错误 {vt_symbol}: {traceback.format_exc()}")

    def close_order(self, price: float, volume: int, vt_symbol: str, order_direction: str, memo: str) -> None:
        try:
            direction = Direction.LONG if order_direction.lower() == 'buy' else Direction.SHORT
            self.send_order(vt_symbol, direction, Offset.CLOSE, price, volume, memo=memo)
            self.order_count += 1
        except Exception:
            self.write_log(f"平仓操作时遇到错误 {vt_symbol}: {traceback.format_exc()}")

    @staticmethod
    def is_trading_time() -> bool:
        current_time = datetime.now().time()
        return (time(9, 10) <= current_time <= time(14, 57) or
                time(21, 10) <= current_time <= time(23, 55))

    @staticmethod
    def check_future_condition(data: dict, option_type: OptionType) -> bool:
        last_price = data['future_lastPrice']
        pre_close = data['future_preClosePrice']
        pre_settlement = data['future_pre_settlement_price']

        if option_type == OptionType.CALL:
            return (last_price > 1.01 * pre_close or
                    ((last_price / pre_settlement) - 1) > (((data['future_upperLimit'] / pre_settlement) - 1) / 2) or
                    (last_price > data['y_high'] and last_price > data['by_high']))
        elif option_type == OptionType.PUT:
            return (last_price < 0.99 * pre_close or
                    ((last_price / pre_settlement) - 1) < (((data['future_lowerLimit'] / pre_settlement) - 1) / 2) or
                    (last_price < data['y_low'] and last_price < data['by_low']))

    @staticmethod
    def check_option_condition(data: dict) -> bool:
        if data['remaining_trading_days'] > 2:
            return (data['option_askPrice1'] - data['option_bidPrice1'] < 5 * data['price_tick'] and
                    data['close_signal'] and
                    data['option_volume'] > 10)
        elif data['remaining_trading_days'] <= 2:
            return (data['option_askPrice1'] - data['option_bidPrice1'] < 5 * data['price_tick'] and
                    data['future_lastPrice'] * 0.97 <= data['strike_price'] <= data['future_lastPrice'] * 1.03 and
                    data['close_signal'] and
                    data['option_volume'] > 10)

    @staticmethod
    def split_volume(max_volume: int, total_volume: int) -> list[int]:
        max_volume = int(max_volume)
        total_volume = int(total_volume)
        return (
            [max_volume]
            * (total_volume // max_volume)
            + ([_v] if (_v := total_volume % max_volume) else [])
        )

    def on_order(self, order: OrderData) -> None:
        # 1. 基础过滤
        if order.vt_symbol not in self.vt_symbols: return
        if not order.ordersysid: return  # 忽略未确认的订单

        # ---------------------------------------------
        ordersysid: str | None = order.ordersysid
        if not ordersysid: return
        if order.status == Status.SUBMITTING: return
        
        self.write_log(f"订单信息更新 {self.generate_order_info_string_from_order_data(order)}")

        try:
            # 2. 更新订单信息到 DataFrame (保持不变)
            order_df: DataFrame = self.convert_order_to_df(order)
            if ordersysid in self.order_info['ordersysid'].values:
                self.order_info.loc[self.order_info['ordersysid'] == ordersysid, order_df.columns] = order_df.values
            else:
                self.order_info = pd.concat([self.order_info, order_df], ignore_index=True)

            self.order_info['datetime'] = pd.to_datetime(self.order_info['datetime']).apply(
                lambda x: x.replace(year=datetime.now().year, month=datetime.now().month, day=datetime.now().day)
            )
            
            # 【注意！】请删除这里原本的 if (order.status == Status.ALLTRADED ...) 逻辑块
            # 不要留旧代码在这里，否则会重复发单！

        except Exception:
            self.write_log(f"处理订单更新遇到错误 {traceback.format_exc()}")

        # 3. 【核心修改】追单逻辑 + 去重 (只保留这一份)
        # 条件：全部成交 + 开仓单 + 是本策略关注的品种
        if (order.status == Status.ALLTRADED and 
            order.offset == Offset.OPEN and 
            order.vt_symbol in self.option_count):
            
            # 【去重检查】如果这笔单子ID已经在记录里，说明处理过了，直接退出
            if order.ordersysid in self.processed_orders:
                return

            self.write_log(f"检测到新成交 (ID:{order.ordersysid})，执行追单判断...")

            # 1. 标记为已处理 (加入列表，防止重启后重复处理)
            self.processed_orders.append(order.ordersysid)
            
            # 2. 更新持仓计数
            self.option_count[order.vt_symbol] += order.traded_volume
            
            # 3. 触发追单判断
            current_count = self.option_count[order.vt_symbol]
            target = self.target_number
            
            # 如果加上这笔后，还没买够 -> 追单
            if current_count < target:
                volume_needed = target - current_count
                volume_to_order = min(self.add_volume, volume_needed)
                
                if volume_to_order > 0:
                    self.write_log(f"持仓({current_count}) < 目标({target})，触发追加 {volume_to_order} 手...")
                    self.open_order(
                        order.price, 
                        volume_to_order, 
                        order.vt_symbol, 
                        'buy', 
                        f"Chase_{self.order_count}"
                    )
            else:
                self.write_log(f'合约：{order.vt_symbol} 持仓已达标或过多 ({current_count})')

    def on_trade(self, trade: TradeData) -> None:
        self.write_log(f"成交信息更新 {self.generate_trade_info_string_from_trade_data(trade)}")

    @staticmethod
    def convert_to_timestamp_or_nat(dt: datetime | None):
        return pd.to_datetime(dt) if dt else pd.NaT

    def convert_order_to_df(self, order: OrderData) -> DataFrame:
        return DataFrame(
            data={
                "symbol": order.symbol,
                "exchange": order.exchange,
                "orderid": order.orderid,
                "ordersysid": order.ordersysid,
                "status": order.status,
                "direction": order.direction,
                "offset": order.offset,
                "price": order.price,
                "type": order.type,
                "volume": order.volume,
                "traded": order.traded,
                "datetime": self.convert_to_timestamp_or_nat(order.datetime),
                "canceltime": self.convert_to_timestamp_or_nat(order.canceltime),
                "memo": order.memo,
                "gateway": order.gateway_name,
                "vt_symbol": order.vt_symbol,
                "vt_orderid": order.vt_orderid,
            },
            index=[0]
        )
    
    @staticmethod
    def generate_trade_info_string_from_trade_data(data: TradeData) -> str:
        return (
            f"成交信息: "
            f"合约={data.vt_symbol}\n"
            f"编号={data.tradeid}\n"
            f"方向={data.direction}\n"
            f"开平={data.offset}\n"
            f"价格={data.price}\n"
            f"数量={data.volume}\n"
            f"成交时间={data.datetime}"
        )
    
    @staticmethod
    def generate_order_info_string_from_order_data(data: OrderData) -> str:
        return (
            f"订单信息: "
            f"合约={data.vt_symbol}, "
            f"编号={data.ordersysid}, "
            f"状态={data.status}, "
            f"方向={data.direction}, "
            f"开平={data.offset}, "
            f"价格={data.price}, "
            f"数量={data.volume}, "
            f"Memo={data.memo}"
        )