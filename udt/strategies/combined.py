import re
import time
import asyncio
import aiohttp
import pandas as pd
import akshare as ak  # 从akshare数据库中获取期货历史数据
from vnpy.trader.object import *
from vnpy.trader.constant import *
from vnpy.trader.utility import get_file_path
from vnpy_simplestrategy import StrategyTemplate, StrategyEngine
# from time import time
from typing import Dict, List, Optional, Union
from datetime import datetime, timedelta, time as datetime_time
import traceback


class Combined(StrategyTemplate):
    """"""
    author = "Minghao Guan & Zhengyi Zhang"
    
    tick_count = 0  # 统计 tick 次数
    
    variables = ["tick_count"]
    
    def __init__(self, strategy_engine: StrategyEngine, strategy_name: str, vt_symbols: list[str], setting: dict) -> None:
        super().__init__(strategy_engine, strategy_name, vt_symbols, setting)
        
        self.main_engine = self.strategy_engine.main_engine
        # self.params_map = Params()
        # self.state_map = State()
        self.volume: int = 1  # 每次交易的合约数量
        self.max_volume: int = 10  # 每次启动程序最多交易的合约数量
        self.single_max: int = 1  # 每次启动程序每个合约最多交易的数量
        self.traded_volume: int = 0
        self.single_traded_volume: Dict[str, float] = {}
        self.order_count: int = 0
        self.investor: str = ''
        self.results: Optional[pd.DataFrame] = None
        self.option_update: Dict[str, Dict[str, Union[float, datetime]]] = {}
        self.future_update: Dict[str, Dict[str, float]] = {}
        self.option_vt_symbols: set = set()
        self.future_vt_symbols: set = set()
        self.fund_position: Dict[str, float] = {}
        self.orderID: Dict[str, list[str]] = {}
        self.order_info: Optional[pd.DataFrame] = None
        self.current_time: datetime = datetime.now()
        self.trade_time: datetime = datetime.now()
        self.updated_count: int = 0
        self.total_instruments_num: int = 0
        self.total_position: list[PositionData] = []
        self.contract_send_count: Dict[str, str] = {}
        self.trade_time_data: pd.DataFrame = pd.read_excel(get_file_path("params(GXHYTF).xlsx"))

    def on_init(self) -> None:
        """策略初始化"""
        # self.investor = self.get_investor_data().investor_id
        self.total_position = self.main_engine.get_all_positions()
        exchange_list = [Exchange.CZCE, ] # Exchange.CFFEX, Exchange.DCE, Exchange.SHFE, Exchange.INE
        self.initialize_results(exchange_list)
        self.subscribe_vt_symbols()
        self.results['product_type'] = self.results['product'] + self.results['option_type'].apply(lambda x: x.value)
        self.write_log(self.results.head(30))
        # self.state_map.contract_sum = len(self.results)
        self.write_log("策略已初始化")

    def on_start(self) -> None:
        """策略启动"""
        self.write_log("策略已启动")

    # def initialize_results(self, product_list: List[str]) -> None:
    def initialize_results(self, exchange_list: List[Exchange]) -> None:
        """ 初始化 results DataFrame"""
        self.results = pd.DataFrame(columns=['product', 'exchange', 'symbol', 'vt_symbol', 'price_tick', 'expire_date', 'strike_price', 'underlying_symbol', 'underlying_vt_symbol', 'option_type'])
        self.order_info = pd.DataFrame(columns=['symbol', 'vt_symbol', 'datetime', 'canceltime', 'exchange', 'ordersysid', 'status', 'direction', 'offset',
                                                'price', 'type', 'volume', 'traded', 'memo'])
        all_contracts = self.main_engine.get_all_contracts()
        for contract_info in all_contracts:
            # if contract_info.product == Product.OPTION and contract_info.option_portfolio in product_list:
            if contract_info.product == Product.OPTION and contract_info.exchange in exchange_list:
                contracts_data = []
                contracts_data.append({
                    'product': contract_info.option_portfolio, # 品种，注意与contract_info.product区别，后者是金融产品种类
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
                self.results = pd.concat([self.results, pd.DataFrame(contracts_data)], ignore_index=True)
        self.process_results()

    def calculate_remaining_trading_days(self, end_date: datetime) -> int:
        """计算剩余交易日"""
        return sum(1 for day in range((end_date - self.current_time).days + 1)
                   if (self.current_time + timedelta(day)).weekday() < 5)

    def process_results(self) -> None:
        """计算剩余交易日，筛选剩余交易日最少的两个的合约"""
        self.results['remained_trading'] = self.results['expire_date'].apply(self.calculate_remaining_trading_days)
        self.trade_time_data['exchange'] = self.trade_time_data['exchange'].apply(lambda x: Exchange(x))
        self.trade_time_data['product'] = self.trade_time_data['product_name'] # TODO: 临时处理
        self.results = pd.merge(self.results, self.trade_time_data, on=['product', 'exchange'], how='left')
        self.results['date_rank'] = self.results.groupby('product')['expire_date'].rank(method='dense')
        self.results = self.results[self.results['date_rank'].isin([1, 2])]
        self.add_historical_data()

    def add_historical_data(self) -> None:
        """添加期货历史价格数据"""
        tool_trade_date_hist_sina_df = ak.tool_trade_date_hist_sina()
        tool_trade_date_hist_sina_df['trade_date'] = pd.to_datetime(tool_trade_date_hist_sina_df['trade_date'])
        current_time = self.current_time
        current_date = pd.to_datetime(current_time.strftime("%Y-%m-%d"))
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

        if len(previous_dates) == 2:
            by_day, y_day = previous_dates[0], previous_dates[1]
            try:
                # 尝试从 ak 获取期货历史数据
                by_data = ak.get_futures_daily(start_date=by_day, end_date=by_day, market="CZCE")
                y_data = ak.get_futures_daily(start_date=y_day, end_date=y_day, market="CZCE")

            except Exception as e:
                # 捕获异常并回退到本地数据库
                self.write_log(f"获取数据失败，错误信息. 尝试从本地数据库加载数据...")
                by_day = pd.to_datetime(by_day, format='%Y%m%d')
                y_day = pd.to_datetime(y_day, format='%Y%m%d')
                
                df = pd.read_csv('C:/python/exchange_data/czce.csv')
                df['tradedate'] = pd.to_datetime(df['tradedate'])
                df['symbol'] = df['symbol'].str.replace(r'([a-zA-Z])\d', r'\1', regex=True) # 针对郑商所代码进行修改
                by_data: pd.DataFrame = df[df['tradedate'] == by_day]
                y_data: pd.DataFrame = df[df['tradedate'] == y_day]
                
                if by_data.empty or y_data.empty:
                    self.write_log("无法从本地数据库获取数据")
                    return

            # 如果获取数据成功，则处理数据
            by_data = by_data[['symbol', 'high', 'low']].rename(columns={'symbol': 'underlying_symbol', 'high': 'by_high', 'low': 'by_low'})
            y_data = y_data[['symbol', 'high', 'low']].rename(columns={'symbol': 'underlying_symbol', 'high': 'y_high', 'low': 'y_low'})

            self.results = pd.merge(self.results, by_data, on='underlying_symbol', how='left')
            self.results = pd.merge(self.results, y_data, on='underlying_symbol', how='left')

        else:
            self.write_log("无法获取前两个交易日的历史最高最低价数据")

    def subscribe_vt_symbols(self) -> None:
        """订阅合约"""
        option_vt_symbols = self.results['vt_symbol'].unique().tolist()
        future_vt_symbols = self.results['underlying_vt_symbol'].unique().tolist()

        self.vt_symbols = option_vt_symbols + future_vt_symbols
        self.future_vt_symbols = set(future_vt_symbols)
        self.option_vt_symbols = set(option_vt_symbols)
        self.total_instruments_num = len(option_vt_symbols + future_vt_symbols)

    def on_stop(self) -> None:
        """策略停止"""
        self.write_log("策略已停止")

    def on_tick(self, tick: TickData) -> None:
        """处理tick数据"""
        # if not tick.last_price:
        #     return
        # self.write_log(f"{tick.symbol} {tick.last_price}")

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
        self.total_position = self.main_engine.get_all_positions()
        close_positions = [
            (close.vt_symbol, close.vt_positionid)
            for close in self.total_position
            if close.direction == Direction.SHORT
        ]
        self.fund_position = {product_type: 0.0 for product_type in self.results['product_type'].unique()}

        if not close_positions:
            return

        for vt_symbol, vt_positionid in close_positions:
            if vt_symbol in self.option_vt_symbols:
                symbol_info = self.main_engine.get_contract(vt_symbol)
                product = symbol_info.option_portfolio
                option_type = symbol_info.option_type.value
                product_type = product + option_type
                percent = self.cal_fund_tie(vt_positionid)
                self.fund_position[product_type] += percent
            else:
                continue

    def process_and_clear_data(self) -> None:
        """处理并清理数据"""
        try:
            self.current_time = datetime.now()
            for vt_symbol, data in self.option_update.items():
                self.results.loc[self.results['vt_symbol'] == vt_symbol, data.keys()] = list(data.values())
            for vt_symbol, data in self.future_update.items():
                self.results.loc[self.results['underlying_vt_symbol'] == vt_symbol, data.keys()] = list(data.values())

            self.product_fund_tie()
            self.process_results_by_product()
            self.option_update.clear()
            self.future_update.clear()
            self.updated_count = 0
            self.trade_time = datetime.now()
            # self.state_map.num += 1
            # self.state_map.trade_tick = datetime.now()
            # self.update_status_bar()
        except Exception as e:
            self.write_log(f"处理并清理数据时遇到错误")

    def process_results_by_product(self) -> None:
        """按产品分组处理数据"""
        try:
            grouped = self.results.groupby('product')
            for _, group in grouped:
                self.process_group(group)
        except Exception as e:
            self.write_log(f"按产品分组处理数据时遇到错误")

    def process_group(self, group: pd.DataFrame) -> None:
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
        except Exception as e:
            self.write_log(f"时间判断错误 for time_period: {time_period}")
            return False

    def set_signals(self, data: pd.DataFrame) -> pd.DataFrame:
        """设置交易信号"""
        try:
            time_periods_close = ['可挂单', '交易时间段1', '交易时间段2', '交易时间段3', '交易时间段4']
            time_periods_open = ['交易时间段1', '交易时间段2', '交易时间段3', '交易时间段4']

            data['close_signal'] = data[time_periods_close].apply(lambda row: any(self.at_time(row[time_period]) for time_period in time_periods_close), axis=1)
            data['open_signal'] = data[time_periods_open].apply(lambda row: any(self.at_time(row[time_period]) for time_period in time_periods_open), axis=1)

            return data
        except Exception as e:
            self.write_log(f"设置信号时遇到错误")
            return pd.DataFrame()

    def calc_signal(self, group: pd.DataFrame) -> tuple[Dict, pd.DataFrame]:
        """计算信号并返回处理后的结果字典和目标期权"""
        try:
            if group.empty or not all(col in group.columns for col in ['option_lastPrice', 'future_lastPrice']):
                return {}, pd.DataFrame()

            processed_results = self.set_signals(group.dropna(subset=['option_lastPrice', 'future_lastPrice']))
            if processed_results.empty:
                return {}, pd.DataFrame()

            processed_results['diff1'] = processed_results.apply(
                lambda x: x['strike_price'] - (x['future_upperLimit'] if x['option_type'] == OptionType.CALL else x['future_lowerLimit']),
                axis=1
            )
            processed_results['target_option_rank'] = processed_results.groupby(['option_type', 'underlying_vt_symbol'], sort=False)['diff1'].rank()
            results_dict = {row['vt_symbol']: row.to_dict() for _, row in processed_results.iterrows()}

            target = []
            for (option_type, _), group in processed_results.groupby(['option_type', 'underlying_vt_symbol'], sort=False):
                if option_type == OptionType.CALL:
                    condition = (group['diff1'] > 0) & (group['option_bidPrice1'] > 3 * group['price_tick'])
                    largest = group[condition].nlargest(1, 'target_option_rank')
                    target.append(largest)
                elif option_type == OptionType.PUT:
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
                target_option = pd.DataFrame()

            return results_dict, target_option
        except Exception as e:
            self.write_log(f"计算信号时遇到错误")
            return {}, pd.DataFrame()

    def exec_signal(self, results_dict: Dict[str, Dict], target_option: pd.DataFrame) -> None:
        """执行信号"""
        # if (not self.trading or
        #     (self.current_time - self.trade_time).total_seconds() < 2 or
        #         target_option.empty):
        #     return

        try:
            self.close_positions(results_dict)
            self.closed_positions(results_dict)
            self.offset_close(results_dict)
            self.AV_close(results_dict)
            self.open_positions(target_option)  # 邹老师: 法定节假日前关闭开仓
            # self.cancel_orders()
        except Exception as e:
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
                vt_symbol = row['vt_symbol']
                ordersysid = row['ordersysid'] if not pd.isna(row['ordersysid']) else None
                exchange = row['exchange']
                if row['symbol'] == vt_symbol and (row['price'] >= price if direction == 'buy' else row['price'] <= price):
                    # req = CancelRequest(ordersysid=ordersysid, symbol=vt_symbol, exchange=exchange)
                    # self.cancel_order_by_sysid(req)
                    self.write_log(f"撤单: {vt_symbol} {ordersysid}")
                    conflict_found = True
            if conflict_found:
                time.sleep(0.5)
        except Exception as e:
            self.write_log(f"避免自成交异常 {vt_symbol}")
            return False  # 出现异常时默认禁止下单

        return not conflict_found  # 返回是否可以安全下单
    def open_positions(self, target_option: pd.DataFrame) -> None:
        """开仓操作"""
        current_time = self.current_time.time()
        if (datetime.strptime('09:10', '%H:%M').time() <= current_time <= datetime.strptime('11:27', '%H:%M').time() or
            datetime.strptime('13:05', '%H:%M').time() <= current_time <= datetime.strptime('13:55', '%H:%M').time() or
                datetime.strptime('21:30', '%H:%M').time() <= current_time <= datetime.strptime('22:50', '%H:%M').time()):
            for product_type, group in target_option.groupby('product_type', sort=False):
                for _, row in group.iterrows():
                    if self.open_condition(row, product_type):
                        try:
                            self.open_order(row['vt_symbol'], Direction.SHORT, row['option_askPrice1'], self.volume, str(self.order_count))
                            self.open_order(row['vt_symbol'], Direction.SHORT, row['option_bidPrice1'], self.volume, str(self.order_count))
                            self.write_log(f"开仓: {row['vt_symbol']}")
                            self.traded_volume += self.volume * 2
                            self.single_traded_volume[row['vt_symbol']] = self.single_traded_volume.get(row['vt_symbol'], 0) + self.volume * 2
                            time.sleep(5)  # 防止一秒内连续多次下单
                        except Exception:
                            self.write_log(f"开仓时遇到错误 {row['vt_symbol']} {traceback.format_exc()}")

    def open_condition(self, row: pd.Series, product_type: str) -> bool:
        """开仓条件判断"""
        coefficient = row['vix']
        return (((row["option_type"] == OptionType.CALL and (row["future_lastPrice"] < (1 - 0.01 * coefficient) * row["future_preClosePrice"] or row["future_lastPrice"] < (1 - 0.01 * coefficient) * row["future_openPrice"]))
                or (row["option_type"] == OptionType.PUT and (row["future_lastPrice"] > (1 + 0.01 * coefficient) * row["future_preClosePrice"] or row["future_lastPrice"] > (1 + 0.01 * coefficient) * row["future_openPrice"])))
                and row["option_volume"] > 100
                and ((row["option_lastPrice"] > row["price_tick"] * 4 and row["remained_trading"] <= 14)
                or (row["option_lastPrice"] >= row["price_tick"] * 6 and row["remained_trading"] > 14))
                and row["option_askPrice1"] - row["option_bidPrice1"] < 3 * row["price_tick"]
                and row["remained_trading"] <= 45
                and (pd.to_datetime(row["date_time"]) - self.current_time).total_seconds() < 20
                and self.main_engine.get_all_accounts()[0].available > 0.1 * self.main_engine.get_all_accounts()[0].balance
                and self.fund_position[product_type] < 0.1 
                and self.traded_volume < self.max_volume
                and self.single_traded_volume.get(row['vt_symbol'], 0) < self.single_max
                and row["open_signal"])

    # TODO:
    def open_order(self, vt_symbol: str, direction: Direction, price: float, volume: int, memo: str) -> None:
        """发送开仓订单"""
        try:
            can_proceed = self.avoid_self_dealing(vt_symbol, direction, price)
            if not can_proceed:
                self.write_log(f"自成交风险未解除，跳过开仓: {vt_symbol}")
                return
            
            self.orderID[memo] = self.send_order(
                vt_symbol=vt_symbol,
                direction=direction,
                offset=Offset.OPEN,
                price=price,
                volume=volume,
                memo=memo
            )
            self.order_count += 1
        except Exception as e:
            self.write_log(f"开仓时遇到错误 {vt_symbol} {traceback.format_exc()}")

    # TODO:
    def close_order(self, vt_symbol: str, direction: Direction, price: float, volume: int, memo: str) -> None:
        """发送平仓订单"""
        try:
            can_proceed = self.avoid_self_dealing(vt_symbol, direction, price)
            if not can_proceed:
                self.write_log(f"自成交风险未解除，跳过开仓: {vt_symbol}")
                return
            
            self.orderID[memo] = self.send_order(
                vt_symbol=vt_symbol,
                direction=direction,
                offset=Offset.CLOSE,
                price=price,
                volume=volume,
                memo=memo
            )
            self.order_count += 1
        except Exception as e:
            self.write_log(f"平仓操作时遇到错误 {vt_symbol}")


    @staticmethod
    def is_trading_time() -> bool:
        current_time = datetime.now().time()
        return (datetime_time(9, 10) <= current_time <= datetime_time(14, 57) or
                datetime_time(21, 10) <= current_time <= datetime_time(23, 55))

    @staticmethod
    def check_future_condition(data: Dict, option_type: OptionType) -> bool:
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

    @staticmethod
    def check_option_condition(data: Dict) -> bool:
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

    def closed_positions(self, results_dict: Dict[str, Dict]) -> None:
        """"再风控策略"""
        closed_order = self.order_info[
            (self.order_info['offset'].isin([Offset.CLOSE, Offset.CLOSETODAY, Offset.CLOSEYESTERDAY])) &
            (self.order_info['status'].isin([Status.NOTTRADED])) &
            (~self.order_info['memo'].str.contains('风控'))
        ]

        for _, row in closed_order.drop_duplicates().iterrows():
            vt_symbol = row['vt_symbol']
            ordersysid = row['ordersysid'] if not pd.isna(row['ordersysid']) else None
            exchange = row['exchange']

            if vt_symbol not in results_dict:
                continue

            try:
                data = results_dict[vt_symbol]
                option_type = data['option_type']

                if (self.is_trading_time() and
                    self.check_future_condition(data, option_type) and
                    self.check_option_condition(data)):

                    delta_volume = self.combined_volumes(data['vt_symbol'], Direction.SHORT) - self.combined_volumes(data['vt_symbol'], Direction.LONG)
                    if delta_volume > 0:
                        req = CancelRequest(ordersysid=ordersysid, vt_symbol=vt_symbol, exchange=exchange)
                        self.cancel_order_by_sysid(req)
                        self.write_log(f"撤单: {vt_symbol}")
                        self.order_info.loc[self.order_info['ordersysid'] == ordersysid, 'status'] = Status.CANCELLED
            except Exception as e:
                self.write_log(f"再风控时遇到错误 {vt_symbol}")

    @staticmethod
    def split_volume(max_volume: int, total_volume: int) -> list[int]:
        """自动拆单"""
        return (
            [max_volume]
            * (total_volume // max_volume)
            + ([_v] if (_v := total_volume % max_volume) else [])
        )

    def close_positions(self, results_dict: Dict[str, Dict]) -> None:
        """平仓及风控平仓"""
        self.total_position = self.main_engine.get_all_positions() # 更新账户持仓信息
        close_positions = [
            (close.vt_symbol, (close.volume - close.frozen))
            for close in self.total_position
            if (close.volume - close.frozen) > 0 and close.direction == Direction.SHORT
        ]
        unclosed_positions = [
            close.vt_symbol
            for close in self.total_position
            if (close.volume - close.frozen) == 0 and close.direction == Direction.SHORT and close.volume > 0
        ]
        self.write_log(f"close_positions: {close_positions}")

        for close, volume in close_positions:
            if close not in results_dict:
                continue

            try:
                data = results_dict[close]
                option_type = data['option_type']
                
                if (self.is_trading_time() and
                    self.check_future_condition(data, option_type) and
                    self.check_option_condition(data)):
                    self.total_position = self.main_engine.get_all_positions() # 更新账户持仓信息
                    combined_volume = round((1 - self.combined_volumes(data['vt_symbol'], Direction.LONG)/self.combined_volumes(data['vt_symbol'], Direction.SHORT)) * volume)
                    if combined_volume > 0:
                        # 获取有效买一价，如果不存在或为0则使用最小价格单位，并确保不低于最小价格单位
                        bid_price = max(data.get('option_bidPrice1', data['price_tick']), data['price_tick'])
                        self.avoid_self_dealing(close, 'sell', bid_price)
                        volume_list = self.split_volume(int(data['max_volume']), combined_volume)
                        for sub in volume_list:
                            self.close_order(close, Direction.LONG, bid_price, sub, f'风控{self.order_count}')
                            self.write_log(f"平仓 {close} {Direction.LONG} {bid_price} {sub} @{self.order_count}")
                            time.sleep(10)  # 防止一秒内连续多次下单
                            
                elif 19 < data['remained_trading'] <= 130 and data['close_signal']:
                    volume_list = self.split_volume(int(data['max_volume']), volume)
                    for sub in volume_list:
                        self.close_order(close, Direction.LONG, data['price_tick'] * 3, sub, str(self.order_count))
                        self.write_log(f"平仓 {close} {Direction.LONG} {data['price_tick'] * 3} {sub} @{self.order_count}")
                elif 11 < data['remained_trading'] <= 19 and data['close_signal']:
                    volume_list = self.split_volume(int(data['max_volume']), volume)
                    for sub in volume_list:
                        self.close_order(close, Direction.LONG, data['price_tick'], sub, str(self.order_count))
                        self.write_log(f"平仓 {close} {Direction.LONG} {data['price_tick']} {sub} @{self.order_count}")
                elif 6 < data['remained_trading'] <= 11 and data['close_signal']:
                    volume_list = self.split_volume(int(data['max_volume']), volume)
                    for sub in volume_list:
                        self.close_order(close, Direction.LONG, data['price_tick'], sub, str(self.order_count))
                        self.write_log(f"平仓 {close} {Direction.LONG} {data['price_tick']} {sub} @{self.order_count}")
            except Exception as e:
                self.write_log(f"平仓时遇到错误 {close}")

        for close in unclosed_positions:
            if close not in results_dict:
                continue

            try:
                data = results_dict[close]
                option_type = data['option_type']
                if (self.is_trading_time() and
                    self.check_future_condition(data, option_type)
                    and (self.contract_send_count.get(close, 0) < 1)):
                    context = (
                        f'账户：谦量天风\n合约：{close}\n风控平仓待报入'
                    )
                    self.write_log(context)
                    asyncio.run(self.send_feishu_async(context))
                    self.contract_send_count[close] = 1
            except Exception as e:
                self.write_log(f"平仓时遇到错误 {close}")

    def pre_close_positions(self) -> None:
        """盘前止盈平仓"""
        self.write_log("盘前止盈平仓开始")
        results = self.results
        results['close_signal'] = results['可挂单'].apply(self.at_time)
        results_dict = {row['vt_symbol']: row.to_dict() for _, row in results.iterrows()}
        close_positions = [
            (close.vt_symbol, (close.volume - close.frozen))
            for close in self.main_engine.get_all_positions()
            if (close.volume - close.frozen) > 0 and close.direction == Direction.SHORT
        ]
        self.write_log(f"close_positions: {close_positions}")

        for close, volume in close_positions:
            if close not in results_dict:
                continue

            try:
                self.write_log(close, results_dict[close]['close_signal'])
                if not results_dict[close]['close_signal']:
                    continue
                data = results_dict[close]
                volume_list = self.split_volume(int(data['max_volume']), volume)

                if 19 < data['remained_trading'] <= 130 and data['close_signal']:
                    for sub in volume_list:
                        # self.close_order(close, Direction.LONG, data['price_tick'] * 2, sub, str(self.order_count))
                        self.write_log(f"平仓 {close} {Direction.LONG} {data['price_tick'] * 2} {sub} @{self.order_count}")
                elif 11 < data['remained_trading'] <= 19 and data['close_signal']:
                    for sub in volume_list:
                        # self.close_order(close, Direction.LONG, data['price_tick'], sub, str(self.order_count))
                        self.write_log(f"平仓 {close} {Direction.LONG} {data['price_tick']} {sub} @{self.order_count}")
                elif 6 < data['remained_trading'] <= 11 and data['close_signal']:
                    for sub in volume_list:
                        # self.close_order(close, Direction.LONG, data['price_tick'], sub, str(self.order_count))
                        self.write_log(f"平仓 {close} {Direction.LONG} {data['price_tick']} {sub} @{self.order_count}")
            except Exception as e:
                self.write_log(f"平仓时遇到错误 {close}")

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
        
    def combined_volumes(self, vt_symbol: str, direction: Direction) -> None:
        """匹配相同标的，到期日，方向的合约数量"""
        info = self.instrument_info(vt_symbol)
        # self.write_log(info)

        if direction == Direction.LONG:
            close_positions = [
                (close.vt_symbol, close.volume)
                for close in self.total_position
                if close.volume > 0 and close.direction == direction
            ]
        else:
            close_positions = [
                (close.vt_symbol, (close.volume - close.frozen))
                for close in self.total_position
                if (close.volume - close.frozen) > 0 and close.direction == direction
            ]
        volumes = 0
        
        for vt_symbol, volume in close_positions:
            if self.instrument_info(vt_symbol) == info:
                volumes += volume
        return volumes

    def offset_close(self, results_dict: Dict[str, Dict]) -> None:
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
                vt_symbol = row['vt_symbol']
                ordersysid = row['ordersysid'] if not pd.isna(row['ordersysid']) else None
                exchange = row['exchange']

                if vt_symbol not in results_dict:
                    continue

                result_data = results_dict[vt_symbol]
                signal = result_data['open_signal']
                if current_time > row['cancel_time1'] and signal:
                    req = CancelRequest(ordersysid=ordersysid, vt_symbol=vt_symbol, exchange=exchange)
                    self.cancel_order_by_sysid(req)
                    self.write_log(f"撤单 {vt_symbol} {ordersysid}")
                    self.order_info.loc[index, 'status'] = Status.CANCELLED
                    time.sleep(0.5)
                    self.close_order(
                        result_data['option_bidPrice1'] + result_data['price_tick'],
                        row['volume'],
                        vt_symbol,
                        row['exchange'],
                        'buy',
                        f'风控{str(self.order_count)}'
                    )
                    self.write_log(f"平仓 {vt_symbol} {Direction.LONG} {result_data['option_bidPrice1'] + result_data['price_tick']} {row['volume']} @{self.order_count}")
        except Exception as e:
            self.write_log(f"风控平仓遇到错误")

    # def cancel_orders(self) -> None:
    #     """撤销订单"""
    #     if (self.current_time.time() > datetime.strptime('22:50', '%H:%M').time() or
    #             datetime.strptime('11:30', '%H:%M').time() > self.current_time.time() > datetime.strptime('11:28', '%H:%M').time()):
    #         try:
    #             for index, row in self.order_info[(self.order_info['offset'] == Offset.OPEN) & (self.order_info['status'] == Status.NOTTRADED)].iterrows():
    #                 self.cancel_order(row['order_id'])
    #                 self.order_info.loc[index, 'status'] = Status.CANCELLED
    #         except Exception as e:
    #             self.write_log(f"撤销订单遇到错误")

    @staticmethod
    def AV_future_condition(data: Dict, option_type: OptionType) -> bool:
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

    def AV_close(self, results_dict: Dict[str, Dict]) -> None:
        """AV型走势平仓"""
        closed_order = self.order_info[(self.order_info['offset'].isin([Offset.CLOSE, Offset.CLOSETODAY, Offset.CLOSEYESTERDAY])) &
                                       (self.order_info['status'].isin([Status.NOTTRADED])) &
                                       (~self.order_info['memo'].str.contains('风控')) &
                                       (~self.order_info['memo'].str.contains('特别'))]

        for _, row in closed_order.drop_duplicates().iterrows():
            vt_symbol = row['vt_symbol']
            ordersysid = row['ordersysid'] if not pd.isna(row['ordersysid']) else None
            exchange = row['exchange']

            if vt_symbol not in results_dict:
                continue

            try:
                data = results_dict[vt_symbol]
                option_type = data['option_type']
                positions = self.main_engine.get_all_positions()
                for position in positions:
                    if position.vt_symbol == vt_symbol and position.direction == Direction.SHORT:
                        volume = position.volume
                if self.AV_future_condition(data, option_type):
                    combined_volume = round((1 - self.combined_volumes(data['vt_symbol'], Direction.LONG)/self.combined_volumes(data['vt_symbol'], Direction.SHORT)) * volume)
                    if combined_volume > 0:
                        volume_list = self.split_volume(int(data['max_volume']), combined_volume)
                        req = CancelRequest(ordersysid=ordersysid, vt_symbol=vt_symbol, exchange=exchange)
                        self.cancel_order_by_sysid(req)
                        self.write_log(f"撤单 {vt_symbol} {ordersysid}")
                        self.order_info.loc[self.order_info['ordersysid'] == ordersysid, 'status'] = Status.CANCELLED
                        time.sleep(2)  # 延迟2秒，系统需要处理时间
                        bid_price = max(data.get('option_bidPrice1', data['price_tick']), data['price_tick'])
                        for sub in volume_list:
                            self.close_order(bid_price, sub, vt_symbol, exchange, 'buy', f'特别{str(self.order_count)}')
                            self.write_log(f"平仓 {vt_symbol} {Direction.LONG} {bid_price} {sub} @{self.order_count}")
            except Exception as e:
                self.write_log(f"AV走势特别平仓遇到错误 {vt_symbol}")

    # def shutdown_orders(self) -> None:
    #     """策略结束或暂停撤销所有订单"""
    #     try:
    #         for index, row in self.order_info[self.order_info['status'] == Status.NOTTRADED].iterrows():
    #             self.cancel_order(row['order_id'])
    #             self.order_info.loc[index, 'status'] = Status.CANCELLED
    #     except Exception as e:
    #         self.write_log(f"策略结束或暂停撤单遇到错误")
             
    def on_order(self, order: OrderData) -> None:
        """处理订单更新"""
        if not order:
            return

        try:
            status = order.status
            memo = order.memo
            self.write_log(f"订单信息更新: ID={order.ordersysid}, Status={status}, Memo={memo}")
            if status in (Status.CANCELLED, Status.ALLTRADED, Status.PARTTRADED):
                self.orderID[memo] = None

            order_df = pd.DataFrame([order.__dict__])
            # order_df.columns = order_df.columns.str.replace('^_', '', regex=True)
            if memo in self.order_info['memo'].values:
                self.order_info.loc[self.order_info['memo'] == memo, order_df.columns] = order_df.values
            else:
                self.order_info = pd.concat([self.order_info, order_df], ignore_index=True)

            self.order_info['datetime'] = pd.to_datetime(self.order_info['datetime']).apply(
                lambda x: datetime.combine(datetime.now().date(), x.time())
            )
            self.order_info['cancel_time1'] = self.order_info.apply(
                lambda row: row['datetime'] + timedelta(seconds=120)
                if '风控' in str(row['memo']) and row['status'] == Status.NOTTRADED
                else pd.NaT,
                axis=1
            )
            if '风控' in str(memo) and status == Status.NOTTRADED:
                context = (
                    f'账户：紫金天风期货Combined\n合约：{order.vt_symbol}\n价格：{order.price}\n数量：{order.volume}\n备注：{memo}'
                )
                self.write_log(f"发送飞书 {context}")
                asyncio.run(self.send_feishu_async(context))
        except Exception as e:
            self.write_log(f"处理订单更新遇到错误 {traceback.format_exc()}")
        
    def on_trade(self, trade: TradeData) -> None:
        """处理成交更新"""
        self.put_event()
        self.write_log(f"on_trade: vt_symbol={trade.vt_symbol} direction={trade.direction} volume={trade.volume} @{trade.price}")

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
        except Exception as e:
            self.write_log(f"发送飞书消息失败: {str(e)}")
