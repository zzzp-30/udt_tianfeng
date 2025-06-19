import asyncio
import re
import time
import traceback
from dataclasses import dataclass
from datetime import datetime
from datetime import time as datetime_time
from datetime import timedelta
from zoneinfo import ZoneInfo

import aiohttp
import akshare as ak  # 从akshare数据库中获取期货历史数据
import pandas as pd
from pandas import DataFrame, Series

from vnpy.trader.constant import (Direction, Exchange, Offset, OptionType,
                                  OrderType, Product, Status)
from vnpy.trader.engine import MainEngine
from vnpy.trader.object import (CancelRequest, OrderData, PositionData,
                                TickData, TradeData)
from vnpy.trader.utility import get_file_path
from vnpy.utility.cooldown import Cooldown
from vnpy_simplestrategy import StrategyEngine, StrategyTemplate


CHINA_TZ: ZoneInfo = ZoneInfo("Asia/Shanghai")

# FIXME see: https://pandas.pydata.org/pandas-docs/stable/user_guide/copy_on_write.html
pd.options.mode.copy_on_write = False  # 默认值是 'warn'
pd.options.mode.chained_assignment = None  # 默认值是 'warn'


@dataclass
class Op1Params:
    """
    撤单、然后平仓操作的参数.
    """
    
    # 撤单用
    ordersysid: str
    
    # 平仓用
    vt_symbol: str  # format: symbol.exchange
    direction: Direction
    price: float
    volume: float
    memo: str


class Op1:
    """
    该类型封装了一个“撤单，等待交易所回报，再平仓”的操作.
    """
    
    def __init__(self, strategy: "Combined") -> None:
        self.strategy: Combined = strategy
        self.params_map: dict[str, list[Op1Params]] = dict()  # ordersysid: list[Op1Params]
    
    def try_cancel_order(self, params: Op1Params) -> None:
        """
        向交易所发送撤单请求.
        如果撤单成功, 一个状态为"已撤单"的报单回报会发送到 strategy#on_order 函数.
        在 strategy#on_order 函数内部应该无条件调用 self.try_close_position.
        """
        
        self.strategy.write_log(f"[OP1] 发送撤单请求 (vt_symbol={params.vt_symbol}, ordersysid={params.ordersysid}")
        self.strategy.cancel_order_by_sysid(params.ordersysid)
        params_list: list[Op1Params] = self.params_map.get(params.ordersysid, [])
        params_list.append(params)
        self.params_map[params.ordersysid] = params_list
    
    def try_close_position(self, order: OrderData) -> None:
        """
        根据传入的 order 进行平仓操作.
        """
        
        # 检查是否要处理该报单回报
        if order.status != Status.CANCELLED:
            return  # 说明 ordersysid 对应的报单还没有撤单成功
        ordersysid: str | None = order.ordersysid
        if ordersysid is None or len(ordersysid) == 0:
            return  # 说明该报单是由本策略发出去的, 但还未被交易所接受
        params_list: list[Op1Params] | None = self.params_map.get(ordersysid, None)
        if params_list is None or len(params_list) == 0:
            return  # 说明 ordersysid 对应的报单不由 Op1 处理

        # 所有检查通过, 进行平仓操作
        for params in params_list:
            self.strategy.write_log(f"[OP1] 发送平仓请求 (vt_symbol={params.vt_symbol}, direction={params.direction}, volume={params.volume}, memo={params.memo} @{params.price})")
            self.strategy.close_order(
                vt_symbol=params.vt_symbol,
                direction=params.direction,
                price=params.price,
                volume=params.volume,
                memo=params.memo
            )
            
        # 移除数据
        self.params_map.pop(ordersysid)


class Combined(StrategyTemplate):
    """"""
    author = "Minghao Guan & Zhengyi Zhang"
    
    def __init__(self, strategy_engine: StrategyEngine, strategy_name: str, vt_symbols: list[str], setting: dict) -> None:
        super().__init__(strategy_engine, strategy_name, vt_symbols, setting)
        
        self.main_engine: MainEngine = self.strategy_engine.main_engine
        
        # --- 策略参数 ---
        
        # TODO 每个策略应该对应一个投资者账号
        self.investor: str = ''
        self.volume: int = 1  # 每次交易的合约数量
        self.max_volume: int = 10  # 每次启动程序最多交易的合约数量
        self.single_max: int = 1  # 每次启动程序每个合约最多交易的数量
        
        # --- 策略状态 ---
        
        # TODO 用于在测试时控制开仓数量 (和单品种不超过10%不是一回事)
        self.traded_volume: int = 0
        self.single_traded_volume: dict[str, float] = {}
        
        # 仅仅用于标记订单操作序号, 无实际用途
        self.order_count: int = 0
        # 由 self.on_tick 无条件递增, 当达到订阅的合约数量时, 执行一次交易逻辑
        self.updated_count: int = 0
        # 每个合约的参数和状态, 包括期权和期货
        self.results_cols: dict[str, str] = {
            'product': 'string',
            'exchange': 'object',  # enum: Exchange
            'symbol': 'string',
            'vt_symbol': 'string',
            'price_tick': 'float64',
            'expire_date': 'datetime64[ns, Asia/Shanghai]',
            'strike_price': 'float64',
            'underlying_symbol': 'string',
            'underlying_vt_symbol': 'string',
            'option_type': 'object',  # enum: OptionType
            # TODO 将所有可能的列在这里定义好, 以提高代码可读性
        }
        self.results: DataFrame = DataFrame(columns=list(self.results_cols.keys())).astype(self.results_cols)
        
        # 积累的行情数据
        self.option_update: dict[str, dict[str, float | datetime]] = {}  # vt_symbol: 关注的期权 tick 数据
        self.future_update: dict[str, dict[str, float]] = {}  # vt_symbol: 关注的期货 tick 数据
        
        # 本策略订阅的合约
        self.option_vt_symbols: set = set()
        self.future_vt_symbols: set = set()
        
        # 每个品种的资金占用比例
        self.fund_position: dict[str, float] = {}  # 品种: 资金占用比例
        
        # 积累的订单信息
        self.order_info_cols: dict[str, str] = {
            "symbol": 'string',
            "datetime": 'datetime64[ns, Asia/Shanghai]',
            "canceltime": 'datetime64[ns, Asia/Shanghai]',
            "cancel_time1": 'datetime64[ns, Asia/Shanghai]',
            "exchange": 'object',  # enum: Exchange
            "orderid": 'string',
            "ordersysid": 'string',
            "status": 'object',  # enum: Status
            "direction": 'object',  # enum: Direction
            "offset": 'object',  # enum: Offset
            "price": 'float64',
            "type": 'object',  # enum: OrderType
            "volume": 'float64',
            "traded": 'float64',
            "memo": 'string',
            "vt_symbol": 'string',
            "vt_orderid": 'string',
            "gateway_name": 'string',
            # TODO 将所有可能的列在这里定义好, 以提高代码可读性
        }
        self.order_info: DataFrame = DataFrame(columns=list(self.order_info_cols.keys())).astype(self.order_info_cols)
        
        # 当前时间
        self.current_time: datetime = datetime.now()  # FIXME 支持 datetime.now().replace(tzinfo=CHINA_TZ)
        
        # 订阅的总合约数
        self.total_instruments_num: int = 0
        self.contract_send_count: dict[str, str] = {}
        
        # 开仓操作的冷却
        self.open_position_cooldown: Cooldown = Cooldown(timeout_seconds=5.0)
        # 平仓操作的冷却
        self.close_positon_cooldown: Cooldown = Cooldown(timeout_seconds=5.0)
        # Op1 实例, 用于执行 Op1 操作, 关于什么是 Op1 操作详见 class Op1 的 docstring
        self.op1: Op1 = Op1(self)

    def on_init(self) -> None:
        """策略初始化"""
        
        # FIXME 每个策略需要区分投资者账号
        # 将投资者当前的全部订单写入 self.order_info
        all_order_data: list[OrderData] = self.main_engine.get_all_orders()
        new_order_records = DataFrame([
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
                'memo': getattr(order, 'memo', '')
            } for order in all_order_data
        ])
        self.order_info = pd.concat([self.order_info, new_order_records], ignore_index=True)
        
        # 初始化 self.results
        self.initialize_results(exchange_list=[Exchange.CZCE])  # Exchange.DCE, Exchange.SHFE, Exchange.INE, Exchange.GFEX
        
        # 订阅行情
        self.subscribe_vt_symbols()
        self.results['product_type'] = self.results['product'] + self.results['option_type'].apply(lambda x: x.value)
        self.write_log(self.results.head(30))

    def initialize_results(self, exchange_list: list[Exchange]) -> None:
        """ 初始化 results DataFrame"""
        all_contracts = self.main_engine.get_all_contracts()
        for contract_info in all_contracts:
            # if contract_info.product == Product.OPTION and contract_info.option_portfolio in product_list:
            if contract_info.product == Product.OPTION and contract_info.exchange in exchange_list:
                contracts_data = []
                contracts_data.append({
                    'product': contract_info.option_portfolio, # 品种，注意与 contract_info.product 区别，后者是金融产品种类
                    'exchange': contract_info.exchange,
                    'symbol': contract_info.symbol,
                    'vt_symbol': contract_info.vt_symbol,
                    'price_tick': contract_info.pricetick,
                    'expire_date': contract_info.option_expiry,
                    'strike_price': contract_info.option_strike,
                    'underlying_symbol': contract_info.option_underlying,
                    'underlying_vt_symbol': contract_info.option_underlying + "." + contract_info.exchange.value,
                    'option_type': contract_info.option_type
                })
                self.results = pd.concat([self.results, DataFrame(contracts_data)], ignore_index=True)
        self.process_results()

    def calculate_remaining_trading_days(self, end_date: datetime) -> int:
        """计算剩余交易日"""
        return sum(1 for day in range((end_date - self.current_time).days + 1)
                   if (self.current_time + timedelta(day)).weekday() < 5)

    def process_results(self) -> None:
        """计算剩余交易日，筛选剩余交易日最少的两个的合约"""
        
        # 添加列: 剩余交易日
        self.results['remained_trading'] = self.results['expire_date'].apply(self.calculate_remaining_trading_days)
        
        # 读取每个品种的固定参数, 详见这里读取的文件
        params: DataFrame = pd.read_excel(get_file_path("params(GXHYTF).xlsx"))
        
        # 转换列: 转成 enum 方便后面比较
        params['exchange'] = params['exchange'].map(Exchange)
        
        # 将 self.results 中的 product 转换为 canonical_product
        # 具体的映射关系请直接参考这里加载的 product_mapping.csv 文件
        # 不同柜台返回的 product 不尽相同, 转换成标准格式以方便执行后续算法
        product_mapping: DataFrame = pd.read_csv(get_file_path("product_mapping.csv"), encoding='utf-8', dtype={'exchange': 'str', 'canonical_product': 'str', 'tianfeng_product': 'str'})
        product_mapping_dict: dict[str, str] = dict(zip(product_mapping['tianfeng_product'], product_mapping['canonical_product']))
        self.results['product'] = self.results['product'].map(product_mapping_dict).fillna(self.results['product'])
        
        # 将固定参数 params LEFT JOIN 到 self.results
        self.results = pd.merge(left=self.results, right=params, on=['product', 'exchange'], how='left')
        
        # 筛选出剩余交易日最少的两个合约
        date_rank: Series = self.results.groupby('product')['expire_date'].rank(method='dense')
        self.results = self.results[date_rank.isin([1, 2])]
        
        # 添加历史数据到 self.results
        self.add_historical_data()

    def add_historical_data(self) -> None:
        """添加历史数据"""

        tool_trade_date_hist_sina_df = ak.tool_trade_date_hist_sina()
        tool_trade_date_hist_sina_df['trade_date'] = pd.to_datetime(tool_trade_date_hist_sina_df['trade_date'])
        current_time = self.current_time
        current_date = pd.to_datetime(current_time.strftime("%Y-%m-%d"))
        # 如果当前时间在 20:00 之后，则算为下一个交易日
        if current_time.hour >= 20:
            search_index = tool_trade_date_hist_sina_df.index[tool_trade_date_hist_sina_df['trade_date'] == current_date]
            trade_date = tool_trade_date_hist_sina_df.loc[search_index[0] + 1, 'trade_date'].strftime("%Y%m%d")
        else:
            trade_date = pd.to_datetime(current_time.strftime("%Y-%m-%d"))

        matching_index = tool_trade_date_hist_sina_df.index[tool_trade_date_hist_sina_df['trade_date'] == trade_date]

        if matching_index.empty:
            self.write_log("没有可匹配的历史最高最低价数据")
            return

        previous_dates = tool_trade_date_hist_sina_df.loc[matching_index[0] - 2:matching_index[0] - 1, 'trade_date'].dt.strftime("%Y%m%d").values

        by_data_list = []
        y_data_list = []
        for exchange_id in self.results['exchange'].unique():
            if len(previous_dates) == 2:
                exchange_id = exchange_id.value
                by_day, y_day = previous_dates[0], previous_dates[1]
                
                try:
                    # 尝试从 ak 获取期货历史数据
                    by_data = ak.get_futures_daily(start_date=by_day, end_date=by_day, market=exchange_id)
                    y_data = ak.get_futures_daily(start_date=y_day, end_date=y_day, market=exchange_id)
                except Exception:
                    self.write_log(f"获取数据失败 {traceback.format_exc()}")

                by_data = by_data[['symbol', 'high', 'low']].rename(columns={'symbol': 'underlying_symbol', 'high': 'by_high', 'low': 'by_low'})
                y_data = y_data[['symbol', 'high', 'low']].rename(columns={'symbol': 'underlying_symbol', 'high': 'y_high', 'low': 'y_low'})
                if exchange_id == (Exchange.SHFE.value or Exchange.INE.value or Exchange.GFEX.value): # TODO
                    by_data['underlying_symbol'] = by_data['underlying_symbol'].str.lower()
                    y_data['underlying_symbol'] = y_data['underlying_symbol'].str.lower()
                by_data_list.append(by_data)
                y_data_list.append(y_data)
            else:
                self.write_log("无法获取前两个交易日的历史最高最低价数据")

        combined_by_data = pd.concat(by_data_list, ignore_index=True)
        combined_y_data = pd.concat(y_data_list, ignore_index=True)
        self.results = pd.merge(self.results, combined_by_data, on='underlying_symbol', how='left')
        self.results = pd.merge(self.results, combined_y_data, on='underlying_symbol', how='left')

    def subscribe_vt_symbols(self) -> None:
        """订阅合约"""
        option_vt_symbols = self.results['vt_symbol'].unique().tolist()
        future_vt_symbols = self.results['underlying_vt_symbol'].unique().tolist()
        
        self.future_vt_symbols = set(future_vt_symbols)
        self.option_vt_symbols = set(option_vt_symbols)

        # 直接重写 StrategyTemplate#vt_symbols
        self.vt_symbols = option_vt_symbols + future_vt_symbols  # TODO 暂时这么写😭未来应该使用专门的函数来订阅合约
        
        self.total_instruments_num = len(self.vt_symbols)

    def on_start(self) -> None:
        """策略启动"""
        ...

    def on_stop(self) -> None:
        """策略停止"""
        self.write_log("策略已停止")

    def on_tick(self, tick: TickData) -> None:
        """处理 tick 数据"""
        vt_symbol = tick.vt_symbol

        if vt_symbol in self.option_vt_symbols:
            self.update_option_data(vt_symbol, tick)
        elif vt_symbol in self.future_vt_symbols:
            self.update_future_data(vt_symbol, tick)
        # else:
        #     return

        self.updated_count += 1
        if self.updated_count == self.total_instruments_num:
            self.write_log(f"行情数据更新")
            self.current_time = tick.datetime  # 接下来的逻辑将使用该时间戳
            self.process_and_clear_data()

    def update_option_data(self, vt_symbol: str, tick: TickData) -> None:
        """更新期权数据"""
        self.option_update[vt_symbol] = {
            'option_lastPrice': tick.last_price,
            'option_preClosePrice': tick.pre_close,
            'option_askPrice1': tick.ask_price_1,
            'option_bidPrice1': tick.bid_price_1,
            'option_volume': tick.volume,
            'date_time': tick.datetime
        }

    def update_future_data(self, vt_symbol: str, tick: TickData) -> None:
        """更新期货数据"""
        self.future_update[vt_symbol] = {
            'future_lastPrice': tick.last_price,
            'future_preClosePrice': tick.pre_close,
            'future_pre_settlement_price': tick.pre_settlement_price,
            'future_upperLimit': tick.limit_up,
            'future_lowerLimit': tick.limit_down,
            'future_lowPrice': tick.low_price,
            'future_highPrice': tick.high_price,
            'future_openPrice': tick.open_price
        }

    def cal_fund_tie(self, vt_positionid: str) -> float:
        """计算合约资金占用"""
        balance = self.main_engine.get_all_accounts()[0].balance
        short_frozen = self.main_engine.get_position(vt_positionid).frozen
        percent = short_frozen / balance

        return percent

    def product_fund_tie(self) -> None:
        """品种资金占用"""
        total_positions: list[PositionData] = self.main_engine.get_all_positions()
        close_positions = [
            (close.vt_symbol, close.vt_positionid)
            for close in total_positions
            if close.direction == Direction.SHORT
        ]
        self.fund_position = {product_type: 0.0 for product_type in self.results['product_type'].unique()}

        if not close_positions:
            return

        for vt_symbol, vt_positionid in close_positions:
            if vt_symbol in self.option_vt_symbols:
                symbol_info = self.main_engine.get_contract(vt_symbol)
                product = symbol_info.option_portfolio  # 品种, 例如 lc2508-C-94000 就是 lc_o
                option_type = symbol_info.option_type.value
                product_type = product + option_type
                percent = self.cal_fund_tie(vt_positionid)
                self.fund_position[product_type] += percent
            else:
                continue

    def process_and_clear_data(self) -> None:
        """处理并清理数据"""
        try:
            for vt_symbol, data in self.option_update.items():
                self.results.loc[self.results['vt_symbol'] == vt_symbol, data.keys()] = list(data.values())
            for vt_symbol, data in self.future_update.items():
                self.results.loc[self.results['underlying_vt_symbol'] == vt_symbol, data.keys()] = list(data.values())

            self.product_fund_tie()
            self.process_results_by_product()
            self.option_update.clear()
            self.future_update.clear()
            self.updated_count = 0
        except Exception:
            self.write_log(f"处理并清理数据时遇到错误 {traceback.format_exc()}")

    def process_results_by_product(self) -> None:
        """按产品分组处理数据"""
        try:
            grouped = self.results.groupby('product')
            for _, group in grouped:
                self.process_group(group)
        except Exception:
            self.write_log(f"按产品分组处理数据时遇到错误 {traceback.format_exc()}")

    def process_group(self, group: DataFrame) -> None:
        """处理数据组"""
        results_dict, target_option = self.calc_signal(group)
        self.exec_signal(results_dict, target_option)

    def at_time(self, time_period: str) -> bool:
        """交易时间判断函数"""
        if pd.isna(time_period) or time_period == '':
            return False
        try:
            if isinstance(time_period, str) and time_period.lower() == 'nan':
                return False
            start_time, end_time = map(lambda x: datetime.strptime(x, '%H:%M').time(), time_period.split('-'))
            current_time = datetime.now().time()
            if start_time > end_time:
                return current_time >= start_time or current_time <= end_time
            else:
                return start_time <= current_time <= end_time
        except Exception:
            self.write_log(f"时间判断错误 ({time_period}) {traceback.format_exc()}")
            return False

    def set_signals(self, data: DataFrame) -> DataFrame:
        """设置交易信号"""
        try:
            time_periods_close = ['可挂单', '交易时间段1', '交易时间段2', '交易时间段3', '交易时间段4']
            time_periods_open = ['交易时间段1', '交易时间段2', '交易时间段3', '交易时间段4']

            data['close_signal'] = data[time_periods_close].apply(lambda row: any(self.at_time(row[time_period]) for time_period in time_periods_close), axis=1)
            data['open_signal'] = data[time_periods_open].apply(lambda row: any(self.at_time(row[time_period]) for time_period in time_periods_open), axis=1)

            return data
        except Exception:
            self.write_log(f"设置信号时遇到错误 {traceback.format_exc()}")
            return DataFrame()

    def calc_signal(self, group: DataFrame) -> tuple[dict, DataFrame]:
        """计算信号并返回处理后的结果字典和目标期权"""
        try:
            if group.empty or not all(col in group.columns for col in ['option_lastPrice', 'future_lastPrice']):
                return {}, DataFrame()

            processed_results = self.set_signals(group.dropna(subset=['option_lastPrice', 'future_lastPrice']))
            if processed_results.empty:
                return {}, DataFrame()

            processed_results['diff1'] = processed_results.apply(
                lambda x: x['strike_price'] - (x['future_upperLimit'] if x['option_type'] == OptionType.CALL else x['future_lowerLimit']),
                axis=1
            )
            processed_results['target_option_rank'] = processed_results.groupby(['option_type', 'underlying_vt_symbol'], sort=False)['diff1'].rank()
            results_dict = {row['vt_symbol']: row.to_dict() for _, row in processed_results.iterrows()}

            target = []
            for (option_type, _), group in processed_results.groupby(['option_type', 'underlying_vt_symbol'], sort=False):
                if option_type == OptionType.CALL:
                    # 将涨停板外满足卖一价大于3个最小变动价的第一个最靠近实值的期权作为目标
                    condition = (group['diff1'] > 0) & (group['option_bidPrice1'] > 3 * group['price_tick'])
                    largest = group[condition].nlargest(1, 'target_option_rank')
                    target.append(largest)
                elif option_type == OptionType.PUT:
                    # 将跌停板外满足买一价大于3个最小变动价的第一个最靠近实值的期权作为目标
                    condition = (group['diff1'] < 0) & (group['option_bidPrice1'] > 3 * group['price_tick'])
                    smallest = group[condition].nsmallest(1, 'target_option_rank')
                    target.append(smallest)

            if target:
                target_option = pd.concat(target)
                target_option = target_option[
                    (target_option['remained_trading'] >= 1) &
                    (target_option['remained_trading'] <= 45)
                ].reset_index(drop=True)
            else:
                target_option = DataFrame()

            return results_dict, target_option
        except Exception:
            self.write_log(f"计算信号时遇到错误 {traceback.format_exc()}")
            return {}, DataFrame()

    def exec_signal(self, results_dict: dict[str, dict], target_option: DataFrame) -> None:
        """执行信号"""

        try:
            self.close_positions(results_dict)
            self.closed_positions(results_dict)
            self.offset_close(results_dict)
            self.AV_close(results_dict)
            self.open_positions(target_option)  # 邹老师: 法定节假日前关闭开仓
            self.cancel_wrong_order(results_dict)
        except Exception:
            self.write_log(f"执行信号时遇到错误 {traceback.format_exc()}")

    def avoid_self_dealing(self, vt_symbol: str, direction: Direction, price: float) -> bool:
        """
        避免自成交
        Args:
            vt_symbol: 合约代码
            direction: 开仓方向
        Returns:
            None
        """
        
        try:
            open_order = self.order_info[
                (self.order_info['direction'] != direction) &
                (self.order_info['status'] == Status.NOTTRADED)
            ]
            conflict_found = False

            for _, row in open_order.iterrows():
                vt_symbol: str = row['vt_symbol']  # FIXME 这里的 vt_symbol 和函数参数冲突
                ordersysid: str = row['ordersysid']
                if (
                    row['symbol'] == vt_symbol
                    and
                    (
                        row['price'] >= price
                        if direction == 'buy'
                        else row['price'] <= price
                    )
                ):
                    self.cancel_order_by_sysid(ordersysid)
                    self.write_log(f"撤单: {vt_symbol} {ordersysid}")
                    conflict_found = True
            if conflict_found:
                time.sleep(0.5)
        except Exception:
            self.write_log(f"避免自成交异常 ({vt_symbol}) {traceback.format_exc()}")
            return False  # 出现异常时默认禁止下单

        return not conflict_found  # 返回是否可以安全下单

    def open_positions(self, target_option: DataFrame) -> None:
        """开仓操作"""
        current_time = self.current_time.time()
        if (
            datetime.strptime('09:10', '%H:%M').time() <= current_time <= datetime.strptime('11:27', '%H:%M').time() or
            datetime.strptime('13:05', '%H:%M').time() <= current_time <= datetime.strptime('13:55', '%H:%M').time() or
            datetime.strptime('21:30', '%H:%M').time() <= current_time <= datetime.strptime('22:50', '%H:%M').time()
        ):
            for product_type, group in target_option.groupby('product_type', sort=False):
                for _, row in group.iterrows():
                    if self.open_condition(row, product_type):
                        try:
                            self.open_order(row['vt_symbol'], Direction.SHORT, row['option_askPrice1'], self.volume, str(self.order_count))
                            self.open_order(row['vt_symbol'], Direction.SHORT, row['option_bidPrice1'], self.volume, str(self.order_count))
                            self.write_log(f"开仓: {row['vt_symbol']}")
                            self.traded_volume += self.volume * 2
                            self.single_traded_volume[row['vt_symbol']] = self.single_traded_volume.get(row['vt_symbol'], 0) + self.volume * 2
                        except Exception:
                            self.write_log(f"开仓时遇到错误 {row['vt_symbol']} {traceback.format_exc()}")

    def open_condition(self, row: pd.Series, product_type: str) -> bool:
        """开仓条件判断"""
        coefficient = row['vix']
        return (
            (
                (
                    row["option_type"] == OptionType.CALL
                    and
                    (
                        row["future_lastPrice"] < (1 - 0.01 * coefficient) * row["future_preClosePrice"]
                        or
                        row["future_lastPrice"] < (1 - 0.01 * coefficient) * row["future_openPrice"]
                    )
                )
                or
                (
                    row["option_type"] == OptionType.PUT
                    and
                    (
                        row["future_lastPrice"] > (1 + 0.01 * coefficient) * row["future_preClosePrice"]
                        or
                        row["future_lastPrice"] > (1 + 0.01 * coefficient) * row["future_openPrice"]
                    )
                )
            )
            and
            (
                (
                    row["option_lastPrice"] > row["price_tick"] * 4
                    and
                    row["remained_trading"] <= 14
                )
                or
                (
                    row["option_lastPrice"] >= row["price_tick"] * 6
                    and
                    row["remained_trading"] > 14
                )
            )
            and
            row["open_signal"]
            and
            row["option_volume"] > 100
            and
            row["option_askPrice1"] - row["option_bidPrice1"] < 3 * row["price_tick"]
            and
            row["remained_trading"] <= 45
            and
            (pd.to_datetime(row["date_time"]) - self.current_time).total_seconds() < 20
            and
            self.main_engine.get_all_accounts()[0].available > 0.1 * self.main_engine.get_all_accounts()[0].balance
            and
            self.fund_position[product_type] < 0.1
            and
            self.traded_volume < self.max_volume
            and
            self.single_traded_volume.get(row['vt_symbol'], 0) < self.single_max
            and
            self.open_position_cooldown.test()
        )

    # TODO:
    def open_order(self, vt_symbol: str, direction: Direction, price: float, volume: int, memo: str) -> None:
        """发送开仓订单"""
        try:
            can_proceed = self.avoid_self_dealing(vt_symbol, direction, price)
            if not can_proceed:
                self.write_log(f"自成交风险未解除，跳过开仓: {vt_symbol}")
                return
            
            self.send_order(
                vt_symbol=vt_symbol,
                direction=direction,
                offset=Offset.OPEN,
                price=price,
                volume=volume,
                memo=memo
            )
            self.order_count += 1
        except Exception:
            self.write_log(f"开仓时遇到错误 {vt_symbol} {traceback.format_exc()}")

    # TODO:
    def close_order(self, vt_symbol: str, direction: Direction, price: float, volume: int, memo: str) -> None:
        """发送平仓订单"""
        try:
            can_proceed = self.avoid_self_dealing(vt_symbol, direction, price)
            if not can_proceed:
                # TODO 这个平仓函数可能会因为"防自成交机制"而平仓失败
                self.write_log(f"自成交风险未解除，跳过开仓: {vt_symbol}")
                return
            
            self.send_order(
                vt_symbol=vt_symbol,
                direction=direction,
                offset=Offset.CLOSE,
                price=price,
                volume=volume,
                memo=memo
            )
            self.order_count += 1
        except Exception:
            self.write_log(f"平仓操作时遇到错误 {vt_symbol} {traceback.format_exc()}")


    @staticmethod
    def is_trading_time() -> bool:
        current_time = datetime.now().time()
        return (datetime_time(9, 10) <= current_time <= datetime_time(14, 57) or
                datetime_time(21, 10) <= current_time <= datetime_time(23, 55))

    @staticmethod
    def check_future_condition(data: dict, option_type: OptionType) -> bool:
        last_price = data['future_lastPrice']
        open_price = data['future_openPrice']
        pre_close = data['future_preClosePrice']
        pre_settlement = data['future_pre_settlement_price']

        if option_type == OptionType.CALL:
            return (last_price > 1.015 * pre_close or
                    ((last_price / pre_settlement) - 1) > (((data['future_upperLimit'] / pre_settlement) - 1) / 2) or
                    (last_price > data['y_high'] and last_price > data['by_high'] and (last_price > 1.005 * pre_close or last_price > 1.005 * open_price)))
        elif option_type == OptionType.PUT:
            return (last_price < 0.985 * pre_close or
                    ((last_price / pre_settlement) - 1) < (((data['future_lowerLimit'] / pre_settlement) - 1) / 2) or
                    (last_price < data['y_low'] and last_price < data['by_low'] and (last_price < 0.995 * pre_close or last_price < 0.995 * open_price)))
        else:
            return False

    @staticmethod
    def check_option_condition(data: dict) -> bool:
        if data['remained_trading'] <= 5:
            return (data['option_askPrice1'] - data['option_bidPrice1'] < 5 * data['price_tick'] and
                    data['close_signal'] and
                    data['option_volume'] > 15 and 
                    ((data['option_type'] == OptionType.CALL and data['strike_price'] < ((data['future_upperLimit'] / data['future_pre_settlement_price']) + 0.03) * data['future_lastPrice'])
                     or (data['option_type'] == OptionType.PUT and data['strike_price'] > ((data['future_lowerLimit'] / data['future_pre_settlement_price']) - 0.03) * data['future_lastPrice'])))
        else:
            return (data['option_askPrice1'] - data['option_bidPrice1'] < 5 * data['price_tick'] and
                    data['close_signal'] and
                    data['option_volume'] > 15)

    def closed_positions(self, results_dict: dict[str, dict]) -> None:
        """再风控策略"""
        closed_order = self.order_info[
            (self.order_info['offset'].isin([Offset.CLOSE, Offset.CLOSETODAY, Offset.CLOSEYESTERDAY])) &
            (self.order_info['status'].isin([Status.NOTTRADED])) &
            (~self.order_info['memo'].str.contains('RiskCtrl'))
        ]

        for _, row in closed_order.drop_duplicates().iterrows():
            vt_symbol: str = row['vt_symbol']
            ordersysid: str = row['ordersysid']

            if vt_symbol not in results_dict:
                continue

            try:
                data = results_dict[vt_symbol]
                option_type = data['option_type']

                if (self.is_trading_time() and
                    self.check_future_condition(data, option_type) and
                    self.check_option_condition(data)):

                    # delta_volume = self.combined_volumes(data['vt_symbol'], Direction.SHORT) - self.combined_volumes(data['vt_symbol'], Direction.LONG)
                    # if delta_volume > 0:
                    self.cancel_order_by_sysid(ordersysid)
                    self.write_log(f"撤单: {vt_symbol}")
                    # self.order_info.loc[self.order_info['ordersysid'] == ordersysid, 'status'] = Status.CANCELLED  # FIXME 要留着吗? 实际上要等  on_order 更新才是真的撤单
            except Exception:
                self.write_log(f"再风控时遇到错误 {vt_symbol}")

    def cancel_wrong_order(self, results_dict: dict[str, dict]) -> None:
        """开仓前风控"""
        open_order = self.order_info[
            (self.order_info['offset'] == Offset.OPEN) &
            (self.order_info['status'] == Status.NOTTRADED) &
            (self.order_info['direction'] == Direction.SHORT)
        ]

        for _, row in open_order.drop_duplicates().iterrows():
            vt_symbol: str = row['vt_symbol']
            ordersysid: str = row['ordersysid']

            if vt_symbol not in results_dict:
                continue

            try:
                data = results_dict[vt_symbol]
                option_type = data['option_type']

                if (
                    self.is_trading_time() and
                    self.check_future_condition(data, option_type) and
                    self.check_option_condition(data)
                ):
                    self.cancel_order_by_sysid(ordersysid)
                    self.write_log(f"撤单: {vt_symbol}")
            except Exception:
                self.write_log(f"开仓前风控时遇到错误 {vt_symbol}")

    @staticmethod
    def split_volume(max_volume: int, total_volume: int) -> list[int]:
        """自动拆单"""
        return (
            [max_volume]
            * (total_volume // max_volume)
            + ([_v] if (_v := total_volume % max_volume) else [])
        )

    def close_positions(self, results_dict: dict[str, dict]) -> None:
        """平仓及风控平仓"""
        total_positions = self.main_engine.get_all_positions()
        
        # 可平量大于 0 的空头持仓
        closable_positions: list[tuple[str, float]] = [
            (close.vt_symbol, (close.volume - close.frozen))
            for close in total_positions
            if (close.volume - close.frozen) > 0 and close.direction == Direction.SHORT
        ]
        self.write_log(f"closable_positions: {closable_positions}")
        
        # 可平量为 0 的空头持仓
        unclosable_positions: list[tuple[str, float]] = [
            (close.vt_symbol, 0)
            for close in total_positions
            if close.volume > 0 and (close.volume - close.frozen) == 0 and close.direction == Direction.SHORT
        ]

        # 对于可平的空头持仓, 如果满足条件则平仓
        for vt_symbol, close_available_volume in closable_positions:
            if vt_symbol not in results_dict:
                continue

            try:
                data = results_dict[vt_symbol]
                option_type = data['option_type']
                
                # 达到风控条件，挂风控平仓单
                if (
                    self.is_trading_time()
                    and
                    self.check_future_condition(data, option_type)
                    and
                    self.check_option_condition(data)
                ):
                    if self.close_positon_cooldown.test():  # TODO 冷却
                        total_positions = self.main_engine.get_all_positions() # 更新账户持仓信息
                        combined_volume = round((1 - self.combined_volumes(data['vt_symbol'], Direction.LONG) / self.combined_volumes(data['vt_symbol'], Direction.SHORT)) * close_available_volume)
                        if combined_volume > 0:
                            # 获取有效买一价，如果不存在或为0则使用最小价格单位，并确保不低于最小价格单位
                            bid_price = max(data.get('option_bidPrice1', data['price_tick']), data['price_tick'])
                            self.avoid_self_dealing(vt_symbol, Direction.LONG, bid_price)
                            volume_list = self.split_volume(int(data['max_volume']), combined_volume)
                            for sub in volume_list:
                                self.close_order(vt_symbol, Direction.LONG, bid_price, sub, f'RiskCtrl{self.order_count}')
                                self.write_log(f"平仓 {vt_symbol} {Direction.LONG} {bid_price} {sub} @{self.order_count}")
                                # time.sleep(10)  # 防止一秒内连续多次下单  # FIXME 移除
                
                # 否则，挂止盈平仓单
                elif self.AV_future_condition(data, option_type):
                    ...  # 遇到 AV 走势则不挂止盈平仓单
                elif 19 < data['remained_trading'] <= 130 and data['close_signal']:
                    volume_list = self.split_volume(int(data['max_volume']), close_available_volume)
                    for sub in volume_list:
                        self.close_order(vt_symbol, Direction.LONG, data['price_tick'] * 3, sub, str(self.order_count))
                        self.write_log(f"平仓 {vt_symbol} {Direction.LONG} {data['price_tick'] * 3} {sub} @{self.order_count}")
                elif 11 < data['remained_trading'] <= 19 and data['close_signal']:
                    volume_list = self.split_volume(int(data['max_volume']), close_available_volume)
                    for sub in volume_list:
                        self.close_order(vt_symbol, Direction.LONG, data['price_tick'], sub, str(self.order_count))
                        self.write_log(f"平仓 {vt_symbol} {Direction.LONG} {data['price_tick']} {sub} @{self.order_count}")
                elif 6 < data['remained_trading'] <= 11 and data['close_signal']:
                    volume_list = self.split_volume(int(data['max_volume']), close_available_volume)
                    for sub in volume_list:
                        self.close_order(vt_symbol, Direction.LONG, data['price_tick'], sub, str(self.order_count))
                        self.write_log(f"平仓 {vt_symbol} {Direction.LONG} {data['price_tick']} {sub} @{self.order_count}")
            except Exception:
                self.write_log(f"平仓时遇到错误 {vt_symbol}")

        # 对于没有可平量的空头持仓, 发送飞书提醒平仓待报入
        for vt_symbol, _ in unclosable_positions:
            if vt_symbol not in results_dict:
                continue

            try:
                data = results_dict[vt_symbol]
                option_type = data['option_type']
                product_name = data['期货']
                if (
                    self.is_trading_time()
                    and
                    self.check_future_condition(data, option_type)
                    and
                    self.contract_send_count.get(vt_symbol, 0) < 1
                ):
                    context = (
                        f'账户：谦量天风\n合约：{product_name} {vt_symbol}\n风控平仓待报入'
                    )
                    self.write_log(context)
                    asyncio.run(self.send_feishu_async(context))
                    self.contract_send_count[vt_symbol] = 1
            except Exception:
                self.write_log(f"平仓时遇到错误 {vt_symbol} {traceback.format_exc()}")

    def instrument_info(self, vt_symbol: str) -> dict:
        """使用正则表达式匹配标的物（字母）、到期日（数字）、方向（'P'或'C'）、价格（数字）"""
        vt_symbol = vt_symbol.split(".")[0]
        vt_symbol = vt_symbol.replace('-', '')
        match = re.match(r"([a-zA-Z]+)(\d+)(P|C)(\d+)$", vt_symbol)
        if match:
            underlying = match.group(1)  # 标的物
            expiry = match.group(2)      # 到期日
            direction = match.group(3)   # 方向
            price = match.group(4)       # 价格
            return {
                "underlying": underlying,
                "expiry": expiry,
                "direction": direction
            }
        else:
            return {}
        
    def combined_volumes(self, vt_symbol: str, direction: Direction) -> float:
        """匹配相同标的，到期日，方向的合约数量"""

        total_positions: list[PositionData] = self.main_engine.get_all_positions()
        
        close_positions: list[tuple[str, float]]
        if direction == Direction.LONG:
            close_positions = [
                (close.vt_symbol, close.volume)
                for close in total_positions
                if close.volume > 0 and close.direction == direction
            ]
        else:
            close_positions = [
                (close.vt_symbol, close.volume - close.frozen)
                for close in total_positions
                if (close.volume - close.frozen) > 0 and close.direction == direction
            ]
        
        volumes: float = 0.0
        
        info: dict = self.instrument_info(vt_symbol)
        for vt_symbol, volume in close_positions:
            if self.instrument_info(vt_symbol) == info:
                volumes += volume
        
        return volumes

    # TODO 根据功能“若未平完，再循环风控”将这个函数改名
    def offset_close(self, results_dict: dict[str, dict]) -> None:
        """抵消平仓操作"""
        if self.order_info.empty:
            return

        try:
            current_time = self.current_time
            orders_to_process = self.order_info[
                (self.order_info['cancel_time1'].notna()) &
                (self.order_info['status'].isin([Status.NOTTRADED, Status.PARTTRADED]))
            ]

            for index, row in orders_to_process.iterrows():
                vt_symbol: str = row['vt_symbol']
                ordersysid: str = row['ordersysid']
                    
                if vt_symbol not in results_dict:
                    continue

                result_data = results_dict[vt_symbol]
                signal = result_data['open_signal']
                if current_time > row['cancel_time1'] and signal:
                    direction: Direction = Direction.LONG
                    price: float = result_data['option_bidPrice1'] + result_data['price_tick']
                    volume: int = row['volume']
                    memo: str = f"RiskCtrl{str(self.order_count)}"
                    
                    params: Op1Params = Op1Params(
                        ordersysid=ordersysid,
                        vt_symbol=vt_symbol,
                        direction=direction,
                        price=price,
                        volume=volume,
                        memo=memo,
                    )
                    
                    self.op1.try_cancel_order(params)
        except Exception:
            self.write_log(f"风控平仓遇到错误 {traceback.format_exc()}")

    @staticmethod
    def AV_future_condition(data: dict, option_type: OptionType) -> bool:
        """AV型走势，及行情短时间剧烈下跌，但随后又迅速反弹的情况"""
        last_price = data['future_lastPrice']  # 期货最新价
        pre_close = data['future_preClosePrice']  # 期货昨收价
        open_price = data['future_openPrice']  # 期货开盘价
        low_price = data['future_lowPrice']  # 期货最低价
        high_price = data['future_highPrice']  # 期货最高价
        upper_price = data['future_upperLimit']  # 期货涨停价
        lower_price = data['future_lowerLimit']  # 期货跌停价

        if option_type == OptionType.CALL and last_price < 1.03 * upper_price:  # 认购合约，且标的期货价格在1.03倍涨停价以下
            return ((low_price <= 0.98 * open_price) and
                    (last_price > low_price + 0.618 * (open_price - low_price)) and
                    (last_price > 0.99 * pre_close))
        elif option_type == OptionType.PUT and last_price > 0.97 * lower_price:  # 认沽合约，且标的期货价格在0.97倍跌停价以上
            return ((high_price >= 1.02 * open_price) and
                    (last_price < high_price - 0.618 * (high_price - open_price)) and
                    (last_price < 1.01 * pre_close))
        else:
            return False

    def AV_close(self, results_dict: dict[str, dict]) -> None:
        """AV型走势平仓"""
        closed_order = self.order_info[(self.order_info['offset'].isin([Offset.CLOSE, Offset.CLOSETODAY, Offset.CLOSEYESTERDAY])) &
                                       (self.order_info['status'].isin([Status.NOTTRADED])) &
                                       (~self.order_info['memo'].str.contains('RiskCtrl')) &
                                       (~self.order_info['memo'].str.contains('Special'))]

        for _, row in closed_order.drop_duplicates().iterrows():
            vt_symbol: str = row['vt_symbol']
            ordersysid: str = row['ordersysid']

            if vt_symbol not in results_dict:
                continue

            try:
                data = results_dict[vt_symbol]
                option_type = data['option_type']
                volume: int
                for position in self.main_engine.get_all_positions():
                    if position.vt_symbol == vt_symbol and position.direction == Direction.SHORT:
                        volume = int(position.volume)
                if self.AV_future_condition(data, option_type):
                    combined_volume: int = round((1 - self.combined_volumes(data['vt_symbol'], Direction.LONG) / self.combined_volumes(data['vt_symbol'], Direction.SHORT)) * volume)
                    volume_list: list[int] = self.split_volume(int(data['max_volume']), combined_volume)
                    for sub in volume_list:
                        direction: Direction = Direction.LONG
                        price: float = max(data.get('option_bidPrice1', data['price_tick']), data['price_tick'])
                        sub_volume: int = sub
                        memo: str = f'Special{self.order_count}'
                        
                        params: Op1Params = Op1Params(
                            ordersysid=ordersysid,
                            vt_symbol=vt_symbol,
                            direction=direction,
                            price=price,
                            volume=sub_volume,
                            memo=memo,
                        )
                        
                        self.op1.try_cancel_order(params)
            except Exception:
                self.write_log(f"AV走势特别平仓遇到错误 {vt_symbol} {traceback.format_exc()}")
             
    def on_order(self, order: OrderData) -> None:
        """处理订单更新"""
        
        # TODO 忽略 ordersysid 为 None 或 len(ordersysid) == 0 的订单
        ordersysid: str | None = order.ordersysid
        if not ordersysid:
            # 不存在 ordersysid 则直接忽略该回报
            # 这种情况是由 vnpy 内部直接调用 on_order 时发生的
            # 具体参考底层接口的实现, 例如 vnpy_ctp
            return
        status: Status = order.status
        if status == Status.SUBMITTING:
            # 如果订单状态是 SUBMITTING，则忽略该回报
            # 这种情况对应交易所返回的 未知单 回报, 说明交易所已经收到报单请求了
            # 但是还没有为该订单生成 ordersysid
            return

        # 响应 OP1
        try:
            self.op1.try_close_position(order)
        except Exception:
            self.write_log(f"执行OP1操作时发生错误 {traceback.format_exc()}")
        
        memo: str = order.memo
        try:
            self.write_log(f"订单信息更新: ID={order.ordersysid}, Status={status}, Memo={memo}")

            order_df: DataFrame = self.convert_order_to_df(order)
            if ordersysid in self.order_info['ordersysid'].values:
                # 已存在则更新
                self.order_info.loc[self.order_info['ordersysid'] == ordersysid, order_df.columns] = order_df.values
            else:
                # 不存在则添加
                self.order_info = pd.concat([self.order_info, order_df], ignore_index=True)

            # Note: datetime 这一列必须都处于同一时区，注意数据来源的样子
            self.order_info['datetime'] = pd.to_datetime(self.order_info['datetime']).apply(
                lambda x: x.replace(year=datetime.now().year, month=datetime.now().month, day=datetime.now().day)
            )
            
            # 更新还未成交的风控单的 cancel_time1，使其能够在 def offset_close 中进行再风控操作
            # Note: 截止至 2025/6/18 如果存在 cancel_time1 则说明需要循环风控, 没有则说明不需要
            mask_orders_in_risk_ctrl = self.order_info['memo'].str.contains('RiskCtrl')
            mask_orders_on_pending = (self.order_info['status'] == Status.NOTTRADED) | (self.order_info['status'] == Status.PARTTRADED)
            mask_orders_to_risk_ctrl_loop = mask_orders_in_risk_ctrl & mask_orders_on_pending
            self.order_info['cancel_time1'] = pd.Series(pd.NaT, dtype='datetime64[ns, Asia/Shanghai]')
            self.order_info.loc[mask_orders_to_risk_ctrl_loop, 'cancel_time1'] = self.order_info.loc[mask_orders_to_risk_ctrl_loop, 'datetime'] + timedelta(seconds=40)
            
            if 'RiskCtrl' in str(memo) and status == Status.NOTTRADED:
                product_name: str = self.results.loc[self.results['vt_symbol'] == order.vt_symbol, '期货'].item()
                context = (
                    f'账户：谦量天风\n合约：{product_name} {order.vt_symbol}\n价格：{order.price}\n数量：{order.volume}\n备注：{memo}'
                )
                self.write_log(f"发送飞书 {context}")
                asyncio.run(self.send_feishu_async(context))  # FIXME 采用消息队列而非事件循环以避免阻塞事件线程
        except Exception:
            self.write_log(f"处理订单更新遇到错误 {traceback.format_exc()}")
        
    def on_trade(self, trade: TradeData) -> None:
        """处理成交更新"""
        self.put_event()
        self.write_log(f"on_trade: vt_symbol={trade.vt_symbol} direction={trade.direction} volume={trade.volume} @{trade.price}")
    
    def cancel_order_auto(self, vt_orderid: str, ordersysid: str | None = None) -> None:
        """
        撤销报单.
        
        如果 ordersysid 存在，则优先使用它进行撤单, 否则使用 vt_orderid 进行撤单.
        """
        # TODO 写进 StrategyTemplate
        if ordersysid:
            self.cancel_order_by_sysid(ordersysid)
        else:
            self.cancel_order(vt_orderid)
            
    def to_timestamp_or_nat(self, dt: datetime | None):
        """
        将 datetime 对象转换为 pd.Timestamp, 若为 None 则返回 pd.NaT.
        """
        
        return pd.to_datetime(dt) if dt else pd.NaT
    
    def convert_order_to_df(self, order: OrderData) -> DataFrame:
        """
        将 OrderData 转换为一个 DataFrame.
        """
        
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
                "datetime": self.to_timestamp_or_nat(order.datetime),
                "canceltime": self.to_timestamp_or_nat(order.canceltime),
                "memo": order.memo,
                "gateway": order.gateway_name,
                "vt_symbol": order.vt_symbol,
                "vt_orderid": order.vt_orderid,
            },
            index=[0]
        )

    async def send_feishu_async(self, context):
        webhook_url = "https://open.feishu.cn/open-apis/bot/v2/hook/911dd4d6-d892-4723-9a56-671f91b54b82"  # 请替换为实际的 webhook URL
        message = {
            "msg_type": "interactive",
            "card": {
                "type": "template",
                "data": {
                    "template_id": "AAqCu6ucc7bwF",
                    "template_version_name": "1.0.3",
                    "template_variable": {
                        "order_info": f"{context}"}}}
            }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(webhook_url, json=message) as response:
                    return await response.json()
        except Exception:
            self.write_log(f"发送飞书消息失败 {context} {traceback.format_exc()}")
