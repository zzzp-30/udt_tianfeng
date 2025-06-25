import re
import traceback
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, time
from time import sleep
from zoneinfo import ZoneInfo

import akshare as ak  # 从akshare数据库中获取期货历史数据
import pandas as pd
from line_profiler import profile
from pandas import DataFrame, DatetimeIndex, Series, Timedelta, Timestamp

from vnpy.trader.constant import (Direction, Exchange, Offset, OptionType,
                                  OrderType, Product, Status)
from vnpy.trader.converter import OffsetConverter, PositionHolding
from vnpy.trader.engine import MainEngine
from vnpy.trader.object import (CancelRequest, ContractData, OrderData,
                                PositionData, TickData, TradeData)
from vnpy.trader.utility import get_file_path
from vnpy.utility.cooldown import (Cooldown, CooldownMap, StackableCooldown,
                                   StackableCooldownMap)
from vnpy_simplestrategy import StrategyEngine, StrategyTemplate

# FIXME see: https://pandas.pydata.org/pandas-docs/stable/user_guide/copy_on_write.html
pd.options.mode.copy_on_write = False  # 默认值是 'warn'
pd.options.mode.chained_assignment = None  # 默认值是 'warn'

# 东八区
CHINA_TZ: ZoneInfo = ZoneInfo("Asia/Shanghai")

# 本策略使用的飞书自定义机器人
feishu_webhook_url = "https://open.feishu.cn/open-apis/bot/v2/hook/911dd4d6-d892-4723-9a56-671f91b54b82"
# 本策略使用的飞书消息模板
feishu_message_template = lambda ctx: {
    "msg_type": "interactive",
    "card": {
        "type": "template",
        "data": {
            "template_id": "AAqCu6ucc7bwF",
            "template_version_name": "1.0.3",
            "template_variable": {
                "order_info": f"{ctx}"
            }
        }
    }
}


class LoopRiskCtrl:
    """
    循环风控操作.
    
    该类型封装了一个“撤单，再平仓”的操作.
    
    背景:
    在整个策略中有一个基本操作就是“撤单，再平仓”.
    具体来说, 撤单是向交易所发送撤单请求, 它并不是一个"调用了撤单函数就一定能够成功撤单"的简单情况.
    撤单可能会因为网络等问题而撤单失败 (即使概率很小). 判断一个撤单是否成功的唯一标准就是等待柜台的回报.
    其次, 撤单也不是立马生效的. 发起撤单请求到报单真的被撤掉之间大概有0.5秒左右的延迟 (只是大概, 不是绝对).
    也就是说, 一个健壮的"撤单, 再平仓"的操作实际上应该是"撤单, 等待交易所回报, 再平仓"这样一个响应式的逻辑.
    而这个类就封装了这一整个操作, 把整个过程所需要维护的状态封装了一个对象, 保持逻辑模块化, 方便外部使用.
    
    使用方式:
    首先, 确保在报单回调函数 (on_order) 中无条件调用 self.try_close_position.
    也就是说, 无论是什么报单回报, 只要有新的报单回报 (OrderData), 都要传给 self.try_close_position.
    剩下的操作就是在需要""撤单再平仓"的地方调用 self.try_cancel_order 方法, 传入一个 Op1Params 对象作为参数.
    Op1Params 包含了撤单目标, 以及在*未来*收到撤单回报后需要被平仓的持仓参数.
    
    使用效果:
    调用 self.try_cancel_order 后, 如果策略收到了对应的撤单回报, 将自动发起既定的平仓操作.
    """
    
    @dataclass
    class FutureOrder:
        """
        代表一个需要在未来发送的订单的参数.
        """
        vt_symbol: str
        direction: Direction
        price: float
        volume: int
        memo: str
        
    def __init__(self, strategy: "Combined") -> None:
        self.strategy: Combined = strategy
        self.future_order_map: dict[str, list[LoopRiskCtrl.FutureOrder]] = dict()  # ordersysid: list[Op1Params]
    
    def start(
        self,
        ordersysid: str,
        vt_symbol: str,
        direction: Direction,
        price: float,
        volume: int,
        memo: str,
    ) -> None:
        """
        向交易所发送撤单请求.
        
        如果撤单成功, 一个状态为"已撤单"的报单回报会发送到 strategy#on_order 函数.
        在 strategy#on_order 函数内部应该无条件调用 self.try_close_position.
        """
        self.strategy.write_log(f"[循环风控] 发送撤单请求 (vt_symbol={vt_symbol}, ordersysid={ordersysid}")
        self.strategy.cancel_order_by_sysid(ordersysid)
        params_list: list[LoopRiskCtrl.FutureOrder] = self.future_order_map.get(ordersysid, [])
        params_list.append(LoopRiskCtrl.FutureOrder(
            vt_symbol=vt_symbol,
            direction=direction,
            price=price,
            volume=volume,
            memo=memo
        ))
        self.future_order_map[ordersysid] = params_list
    
    def on_order(self, order: OrderData) -> None:
        """
        根据传入的 OrderData 进行平仓操作.
        """
        
        # 检查是否要处理该报单回报
        if order.status != Status.CANCELLED:
            return  # 说明 ordersysid 对应的报单还没有撤单成功
        ordersysid: str | None = order.ordersysid
        if ordersysid is None or len(ordersysid) == 0:
            return  # 说明该报单是由本策略发出去的, 但还未被交易所接受
        params_list: list[LoopRiskCtrl.FutureOrder] | None = self.future_order_map.get(ordersysid, None)
        if params_list is None or len(params_list) == 0:
            return  # 说明 ordersysid 对应的报单不由 Op1 处理

        # 所有检查通过, 进行平仓操作
        for params in params_list:
            self.strategy.write_log(f"[循环风控] 发送平仓请求 (vt_symbol={params.vt_symbol}, direction={params.direction}, volume={params.volume}, memo={params.memo} @{params.price})")
            self.strategy.request_close_position(
                vt_symbol=params.vt_symbol,
                direction=params.direction,
                price=params.price,
                volume=params.volume,
                memo=params.memo
            )
            
        # 操作完成, 重置状态
        self.future_order_map.pop(ordersysid)


class AvTrendClosePos:
    """
    AV 走势操作.
    
    该类型封装了一个AV走势下的交易逻辑.
    """
    
    @dataclass
    class FutureOrder:
        """
        代表一个需要在未来发送的订单的参数.
        """
        vt_symbol: str
        short_position: int
        max_volume: int
        price: float
        memo: str
    
    def __init__(self, strategy: "Combined") -> None:
        self.parent: Combined = strategy
        self.future_order_map: dict[str, list[AvTrendClosePos.FutureOrder]] = dict()  # ordersysid: list[AvOp.Params]
        
    def start(
        self,
        ordersysid: str,
        vt_symbol: str,
        short_position: int,
        max_volume: int,
        price: float,
        memo: str,
    ) -> None:
        """
        当 AV 走势条件满足时, 开始执行对应的交易逻辑.
        
        Args:
            ordersysid (str): 需要撤回的订单号
            vt_symbol (str): 需要平仓的合约代码
            short_position (int): 该合约的空头持仓数量
            max_volume (int): 每次平仓的最大数量
            price (float): 平仓价格
            memo (str): Memo
        """
        self.parent.write_log(f"[AV走势] 发送撤单请求 (ordersysid={ordersysid}, vt_symbol={vt_symbol})")
        self.parent.cancel_order_by_sysid(ordersysid)
        params_list: list[AvTrendClosePos.FutureOrder] = self.future_order_map.get(ordersysid, [])
        params_list.append(AvTrendClosePos.FutureOrder(
            vt_symbol=vt_symbol,
            short_position=short_position,  # 该合约的持仓量
            max_volume=max_volume,
            price=price,
            memo=memo,
        ))
        self.future_order_map[ordersysid] = params_list
    
    def on_order(
        self,
        order: OrderData,
    ) -> None:
        """
        收到报单回报的交易逻辑, 即执行之前安排的订单.
        """
        # 检查是否要处理该报单回报
        if order.status != Status.CANCELLED:
            return  # 忽略该报单不是撤单
        ordersysid: str | None = order.ordersysid
        if ordersysid is None or len(ordersysid) == 0:
            return  # 说明该报单还未被交易所接受
        params_list: list[AvTrendClosePos.FutureOrder] | None = self.future_order_map.get(ordersysid, None)
        if params_list is None or len(params_list) == 0:
            return  # 说明 ordersysid 对应的报单不由 AvOp 处理
        
        for params in params_list:
            # 拆单, 发单
            combined_volume: int = round((1 - self.parent.combined_volumes(params.vt_symbol, Direction.LONG) / self.parent.combined_volumes(params.vt_symbol, Direction.SHORT)) * params.short_position)
            split_volume: list[int] = self.parent.split_volume(params.max_volume, combined_volume)
            for sub in split_volume:
                self.parent.write_log(f"[AV走势] 发送平仓请求 (vt_symbol={params.vt_symbol}, volume={sub}, memo={params.memo} @{params.price})")
                self.parent.request_close_position(
                    vt_symbol=params.vt_symbol,
                    direction=Direction.LONG,
                    price=params.price,
                    volume=sub,
                    memo=params.memo
                )
            
        # 操作完成, 重置状态
        self.future_order_map.pop(ordersysid)


class AvTrendTempFix:  # FIXME 更好的类命名
    """
    AV 走势临时修复.
    
    该类型封装了一个“飞书提醒AV走势平仓错误”的逻辑.
    
    之所以写成一个类, 也是为了使代码模块化, 提高代码的可维护性.
    该类是一个临时的措施!!! 该类并没有修复AV走势平仓错误, 仅提醒人工介入.
    等待AV走势平仓错误修复后 (需要重写), 这个类以及相关调用就可以直接删掉了.
    """
    
    def __init__(self, strategy: "Combined") -> None:
        self.strategy: Combined = strategy
        # 用于限制一个合约在5分钟内最多发送1次提醒
        self.cooldown_map: CooldownMap[str] = CooldownMap[str](base=Cooldown(timeout_seconds=300.0))
        # 用于限制一个合约在2小时内最多发送3次提醒
        self.stackable_cooldown_map: StackableCooldownMap[str] = StackableCooldownMap[str](base=Cooldown(timeout_seconds=7200.0), stacks=3)
    
    def notify(self, vt_symbol: str) -> None:
        """
        发送飞书消息, 告知出现了AV走势平仓错误.
        
        Args:
            vt_symbol (str): 无法平仓的合约代码 (vt_symbol)
        """
        # 邹老师: 单个合约每天最多发3次飞书提醒
        # 邹老师: 有些持仓是组合, 但AV走势平仓的逻辑没有考虑到这些, 理想情况不应该考虑为"AV走势平仓错误"
        if (
            self.cooldown_map.test(vt_symbol) and
            self.stackable_cooldown_map.test(vt_symbol)
        ):
            context: str = (
                f'账户：谦量天风\n'
                f'合约：{vt_symbol}\n'
                f'AV走势平仓错误\n'
            )
            self.strategy.main_engine.send_feishu(
                feishu_webhook_url,
                feishu_message_template(context),
            )


class Combined(StrategyTemplate):
    """
    所谓的"主策略".
    
    TODO 想个更加具体点儿的策略名. "比较级命名"没有比较对象的话信息量太低.
    """
    
    author = "Minghao Guan & Zheyin Zeng"
    
    @profile
    def __init__(
        self,
        strategy_engine: StrategyEngine,
        strategy_name: str,
        vt_symbols: list[str],
        setting: dict
    ) -> None:
        """
        实例的初始化函数.
        
        该函数仅仅用于定义所有使用到的成员变量 (即 self...), 不执行任何复杂或者耗时的操作.
        """
        super().__init__(
            strategy_engine,
            strategy_name,
            vt_symbols,  # vt_symbols 会在 on_init 里被重写  # TODO 更加规范的订阅合约写法
            setting
        )
        
        self.main_engine: MainEngine = self.strategy_engine.main_engine
        
        # --- 策略参数 ---
        
        self.investor: str = ''  # TODO 每个策略应该对应一个投资者账号, 之后会用到
        self.volume_per_open_position: int = 1  # 每次开仓时交易的合约数量
        self.max_open_position_volume_in_total: int = 10  # 每次启动程序最多交易的合约数量
        self.max_open_position_volume_per_contract: int = 1  # 每次启动程序每个合约最多交易的数量
        self.loop_risk_ctrl_cooldown: int = 40  # 同个报单两个循环风控的最小间隔, 单位: 秒
        
        # --- 策略状态 ---
        
        # 整个策略所使用的 gateway_name, 由第一个收到的行情 tick 重新赋值
        self.gateway_name: str | None = None
        
        # TODO 这两个变量用于在测试时控制开仓数量
        # 对的, 策略已经有"单品种开仓不超过10%可用资金"的限制, 但这个限制主要是用于测试时控制开仓数量.
        # 按照邹老师的说法, 正式运行时不需要此限制.
        self.total_traded_volume: int = 0
        self.single_traded_volume: dict[str, float] = {}
        
        # 仅仅用于标记订单操作序号, 无实际用途
        self.order_count: int = 0
        # 由 self.on_tick 无条件递增, 当达到订阅的合约数量时, 执行一次交易逻辑
        self.updated_count: int = 0
        # 来自柜台的品种代码 到 标准品种代码 的映射. 不同柜台(或者不同投资者账号)的映射有所不同
        self.product_mapping_dict: dict[str, str] = {}
        # 每个合约的参数和状态, 包括期权和期货
        self.results_cols: dict[str, str] = {
            'symbol': 'string',
            'exchange': 'object',  # enum: Exchange
            'product': 'string',
            'price_tick': 'float64',
            'expire_date': 'datetime64[ns, Asia/Shanghai]',
            'strike_price': 'float64',
            'underlying_symbol': 'string',
            'option_type': 'object',  # enum: OptionType
            'vt_symbol': 'string',
            'vt_underlying_symbol': 'string',
            
            # 计算得到的剩余交易日
            'remaining_trading_days': 'int64',
            
            # 从历史行情获取的价格信息
            'by_high': 'float64',  # Before-Yesterday 最高价
            'by_low': 'float64',  # Before-Yesterday 最低价
            'y_high': 'float64',  # Yesterday 最高价
            'y_low': 'float64',  # Yesterday 最低价
            
            # 从表格读取到的固定参数
            '期货': 'string',
            'product_name': 'string',
            '可挂单': 'string',
            '交易时间段1': 'string',
            '交易时间段2': 'string',
            '交易时间段3': 'string',
            '交易时间段4': 'string',
            'max_volume': 'float64',
            'vix': 'float64',
            'buyer': 'float64',
            
            # 用于计算品种资金占用的字段
            'product_type': 'string',  # 形如: MA看涨期权, ao看跌期权
        }
        self.results: DataFrame = DataFrame()
        
        # 积累的行情数据
        self.option_update: dict[str, dict[str, object]] = {}  # vt_symbol: 关注的期权 tick 数据
        self.future_update: dict[str, dict[str, object]] = {}  # vt_symbol: 关注的期货 tick 数据
        
        # 本策略订阅的合约
        self.option_vt_symbols: set = set()
        self.future_vt_symbols: set = set()
        
        # 每个品种的资金占用比例
        self.fund_position: dict[str, float] = {}  # 品种: 资金占用比例 (品种是形如 'MA看涨期权' 这样的字符串, 不包含C/P, 也不包含到期日)
        
        # 积累的订单信息
        self.order_info_cols: dict[str, str] = {
            # 以下列是通用的
            "symbol": 'string',
            "datetime": 'datetime64[ns, Asia/Shanghai]',
            "canceltime": 'datetime64[ns, Asia/Shanghai]',
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
            # 以下列是策略独有的
            "loop_risk_ctrl_time": 'datetime64[ns, Asia/Shanghai]',
        }
        self.order_info: DataFrame = DataFrame(columns=list(self.order_info_cols.keys())).astype(self.order_info_cols)
        
        # 当前时间
        self.current_time: datetime = datetime.now(tz=CHINA_TZ)
        # 订阅的总合约数
        self.total_instruments_num: int = 0
        # FIXME 不清楚这是做什么的
        self.contract_send_count: dict[str, int] = {}
        
        # 开仓操作的冷却
        self.open_position_cooldown: Cooldown = Cooldown(timeout_seconds=5.0)
        # 平仓操作的冷却
        self.close_positon_cooldown: Cooldown = Cooldown(timeout_seconds=5.0)
        # LoopRiskCtrl 实例, 用于执行循环风控操作
        self.loop_risk_ctrl: LoopRiskCtrl = LoopRiskCtrl(self)
        # AvTrendClosePos 实例, 用于执行 AV 走势平仓
        self.av_trend_close_pos: AvTrendClosePos = AvTrendClosePos(self)
        # AvTempFix1 实例, 用于 AV 走势平仓错误的临时解决方案  # TODO 临时措施. 在修复 AV 走势平仓错误后应该将其移除
        self.av_trend_temp_fix: AvTrendTempFix = AvTrendTempFix(self)

    @profile
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
                'memo': getattr(order, 'memo', ''),
            } for order in all_order_data
        ])
        self.order_info = pd.concat([self.order_info, new_order_records], ignore_index=True)
        
        # 初始化 self.results
        # TODO 将 CFFEX 也纳入到本策略的负责范围内
        self.initialize_results(exchange_list=[Exchange.CZCE, Exchange.DCE, Exchange.SHFE, Exchange.INE, Exchange.GFEX])
        
        # 订阅行情
        self.subscribe_vt_symbols()
        
        # 构建 product_type 列, 形如: MA看涨期权, ao看跌期权
        self.results['product_type'] = self.results['product'] + self.results['option_type'].apply(lambda x: x.value)
        
        # 转换列类型以提高处理速度
        self.results = self.results.astype(self.results_cols)
    
    ############################################################
    # 初始化逻辑 - 开始
    ############################################################
    
    # TODO 使用 DataFrame.pipe 来提高代码可读性
    @profile
    def initialize_results(self, exchange_list: list[Exchange]) -> None:
        """ 初始化 results DataFrame"""
        # 获取底层接口已知的所有合约
        all_contracts: list[ContractData] = self.main_engine.get_all_contracts()
        # 字典形式的 ContractData, 用于构建初始的 DataFrame
        all_contract_dict_list: list[dict[str, object]] = list()
        # 收集特定 ContractData 的字典形式的数据
        for contract in all_contracts:
            if (
                contract.product == Product.OPTION and
                contract.exchange in exchange_list
            ):
                all_contract_dict_list.append({
                    # 原生字段
                    'symbol': contract.symbol,
                    'exchange': contract.exchange,
                    'product': contract.option_portfolio, # 品种(e.g. lc, sc)，注意与 ContractData#product 区别，后者是金融产品种类
                    'price_tick': contract.pricetick,
                    'expire_date': contract.option_expiry,
                    'strike_price': contract.option_strike,
                    'underlying_symbol': contract.option_underlying,
                    'option_type': contract.option_type,
                    
                    # 衍生字段
                    'vt_symbol': contract.vt_symbol,
                    'vt_underlying_symbol': contract.option_underlying + "." + contract.exchange.value,
                })
        # 把收集到的期权合约拼接到 self.results
        self.results = pd.concat([self.results, DataFrame(data=all_contract_dict_list)])
        self.results['expire_date'] = pd.to_datetime(self.results['expire_date']).dt.tz_localize(tz=CHINA_TZ)
        # 接着处理 self.results
        self.process_results()

    @staticmethod
    def calculate_remaining_trading_days(expire_date: datetime) -> int:
        """计算剩余交易日(自然日)"""
        current_time: datetime = datetime.now(tz=CHINA_TZ)
        business_days: DatetimeIndex = pd.bdate_range(
            start=current_time.date(),
            end=expire_date.date(),
            freq='B',  # 'B' 表示工作日频率
            tz='Asia/Shanghai',
            inclusive='right'  # 不包含开始日期, 仅包含结束日期, 即 (current_date. end_date]
        )
        
        return len(business_days)

    @profile
    def process_results(self) -> None:
        """计算剩余交易日，筛选剩余交易日最少的两个的合约"""
        
        # 添加列: 剩余交易日
        self.results['remaining_trading_days'] = self.results['expire_date'].apply(self.calculate_remaining_trading_days)
        
        # FIXME 新品种需要手动加入到这个表格. 如果新品种不存在于这个表格, 则新品种将缺失对应的交易时间等固定参数
        # 读取每个品种的固定参数, 详见这里读取的文件
        params: DataFrame = pd.read_excel(get_file_path("params(GXHYTF).xlsx"))
        
        # 转换列: 转成 enum 方便后面比较
        params['exchange'] = params['exchange'].map(Exchange)
        
        # 将 self.results 中的 product 转换为 canonical_product
        # 具体的映射关系请直接参考这里加载的 product_mapping.csv 文件
        # 不同柜台返回的 product 不尽相同, 转换成标准格式以方便执行后续算法
        product_mapping: DataFrame = pd.read_csv(get_file_path("product_mapping.csv"), encoding='utf-8', dtype={'exchange': 'str', 'canonical_product': 'str', 'tianfeng_product': 'str'})
        self.product_mapping_dict |= dict(zip(product_mapping['tianfeng_product'], product_mapping['canonical_product']))
        self.results['product'] = self.results['product'].map(self.product_mapping_dict).fillna(self.results['product'])
        
        # 将固定参数 params LEFT JOIN 到 self.results
        self.results = pd.merge(left=self.results, right=params, on=['product', 'exchange'], how='left')
        
        # 筛选出剩余交易日最少的两个合约
        date_rank: Series = self.results.groupby('product')['expire_date'].rank(method='dense')
        self.results = self.results[date_rank.isin([1, 2])]
        
        # 添加历史数据到 self.results
        self.add_historical_data()

    @profile
    def add_historical_data(self) -> None:
        """添加历史数据"""
        # 从 AkShare 获取所有交易日
        tool_trade_date_hist_sina_df = ak.tool_trade_date_hist_sina()
        tool_trade_date_hist_sina_df['trade_date'] = pd.to_datetime(tool_trade_date_hist_sina_df['trade_date']).dt.tz_localize(tz=CHINA_TZ)
        
        # 今天日期
        now: Timestamp = pd.to_datetime(datetime.now(tz=CHINA_TZ))
        # 今天交易日
        td_trade_date: Timestamp
        # 计算当前交易日
        # 如果当前时间在 20:00 之后，则算为下一个交易日; 否则就是今天
        if now.hour >= 20:
            td_trade_date = now + Timedelta(days=1)
        else:
            td_trade_date = now
        # 计算今天之前的两个交易日(不包含今天)
        trade_date_matches: DataFrame = tool_trade_date_hist_sina_df.copy()
        # normalize() 作用是将时间戳的时分秒部分归零, 只保留日期部分
        trade_date_matches = trade_date_matches.loc[trade_date_matches['trade_date'].dt.normalize() < td_trade_date.normalize()]
        trade_date_matches = trade_date_matches.tail(2).reset_index()
        # 筛出前交易日 (before-yesterday trade date)
        byd_trade_date: Timestamp = trade_date_matches.at[0, 'trade_date']
        # 筛出昨交易日 (yesterday trade date)
        yd_trade_date: Timestamp = trade_date_matches.at[1, 'trade_date']
        # 前交易日的数据 (ak.get_futures_daily), 列表里的每个元素是单个交易所的所有数据
        byd_df_list: list[DataFrame] = []
        # 昨交易日的数据...
        yd_df_list: list[DataFrame] = []
        # 遍历每个交易所
        for exchange_id in self.results['exchange'].map(lambda x: x.value).unique():
            # 关注的 ak.get_futures_daily 中的列
            data_cols: list[str] = ['symbol', 'high', 'low']
            # 定义好列
            byd_df: DataFrame = DataFrame(columns=data_cols)
            yd_df: DataFrame = DataFrame(columns=data_cols)
            # 尝试从 AkShare 获取期货历史数据
            try:
                # AkShare 只接受 str 形式的日期
                byd_trade_date_str: str = byd_trade_date.strftime("%Y%m%d")
                yd_trade_date_str: str = yd_trade_date.strftime("%Y%m%d")
                # 开爬!
                byd_df = ak.get_futures_daily(start_date=byd_trade_date_str, end_date=byd_trade_date_str, market=exchange_id)
                yd_df = ak.get_futures_daily(start_date=yd_trade_date_str, end_date=yd_trade_date_str, market=exchange_id)
            except Exception:
                self.write_log(f"从 AkShare 获取历史数据失败 ({exchange_id}) {traceback.format_exc()}")
            # 重命名列以准备合并到 self.results
            byd_df = byd_df[data_cols].rename(columns={'symbol': 'underlying_symbol', 'high': 'by_high', 'low': 'by_low'})
            yd_df = yd_df[data_cols].rename(columns={'symbol': 'underlying_symbol', 'high': 'y_high', 'low': 'y_low'})
            # 特别处理 SHFE, INE, GFEX
            if exchange_id == (Exchange.SHFE.value or Exchange.INE.value or Exchange.GFEX.value): # TODO
                byd_df['underlying_symbol'] = byd_df['underlying_symbol'].str.lower()
                yd_df['underlying_symbol'] = yd_df['underlying_symbol'].str.lower()
            # 收集数据
            byd_df_list.append(byd_df)
            yd_df_list.append(yd_df)

        combined_by_data: DataFrame = pd.concat(byd_df_list, ignore_index=True)
        combined_y_data: DataFrame = pd.concat(yd_df_list, ignore_index=True)
        self.results = pd.merge(self.results, combined_by_data, on='underlying_symbol', how='left')
        self.results = pd.merge(self.results, combined_y_data, on='underlying_symbol', how='left')

    @profile
    def subscribe_vt_symbols(self) -> None:
        """订阅合约"""
        option_vt_symbols = self.results['vt_symbol'].unique().tolist()
        future_vt_symbols = self.results['vt_underlying_symbol'].unique().tolist()
        
        self.future_vt_symbols = set(future_vt_symbols)
        self.option_vt_symbols = set(option_vt_symbols)

        # 直接重写 StrategyTemplate#vt_symbols
        self.vt_symbols = option_vt_symbols + future_vt_symbols  # TODO 暂时这么写😭未来应该使用专门的函数来订阅合约
        
        self.total_instruments_num = len(self.vt_symbols)
    
    ############################################################
    # 初始化逻辑 - 结束
    ############################################################
    
    def on_start(self) -> None:
        """策略启动"""
        ...

    def on_stop(self) -> None:
        """策略停止"""
        ...

    def on_tick(self, tick: TickData) -> None:
        """处理 tick 数据"""
        # 将第一个 tick 中的 gateway_name 作为整个策略后续所使用的 gateway_name
        if not self.gateway_name:
            self.gateway_name = tick.gateway_name
            
        vt_symbol = tick.vt_symbol

        if vt_symbol in self.option_vt_symbols:
            self.update_option_data(vt_symbol, tick)
        elif vt_symbol in self.future_vt_symbols:
            self.update_future_data(vt_symbol, tick)
        else:
            self.write_log(f"未订阅的合约 {vt_symbol}")
            return

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
        balance = self.main_engine.get_all_accounts()[0].balance  # FIXME 原版这是动态权益
        position = self.main_engine.get_position(vt_positionid)
        if not position:
            return .0
        else:
            used_margin = position.used_margin + position.frozen_margin + position.frozen_commission
            percent = used_margin / balance
            return percent

    def product_fund_tie(self) -> None:
        """计算品种资金占用"""
        total_positions: list[PositionData] = self.main_engine.get_all_positions()
        short_positions: list[tuple[str, str]] = [
            (pos.vt_symbol, pos.vt_positionid)
            for pos in total_positions
            if pos.direction == Direction.SHORT
        ]
        if not short_positions:
            return
        
        self.fund_position = {product_type: .0 for product_type in self.results['product_type'].unique()}
        
        for (vt_symbol, vt_positionid) in short_positions:
            if vt_symbol not in self.option_vt_symbols:
                continue
            
            contract: ContractData | None = self.main_engine.get_contract(vt_symbol)
            if not contract:
                self.write_log(f"计算品种资金时未找到合约: {vt_symbol}")
                continue
            
            option_portfolio: str | None = contract.option_portfolio
            if not option_portfolio:
                raise ValueError(f"合约 {vt_symbol} 的期权品种信息缺失")
            
            option_type: OptionType | None = contract.option_type
            if not option_type:
                raise ValueError(f"合约 {vt_symbol} 的期权类型信息缺失")
            
            product = self.convert_product_to_canconinal(option_portfolio)  # 品种, 例如 lc2508-C-94000 就是 "lc_o"
            product_type = product + option_type.value  # 品种 + 期权类型, 例如 lc2508-C-94000 就是 "lc_o看跌期权"
            percent = self.cal_fund_tie(vt_positionid)
            self.fund_position[product_type] += percent

    def process_and_clear_data(self) -> None:
        """处理并清理数据"""
        try:
            for vt_symbol, data in self.option_update.items():
                self.results.loc[self.results['vt_symbol'] == vt_symbol, list(data.keys())] = list(data.values())
            for vt_symbol, data in self.future_update.items():
                self.results.loc[self.results['vt_underlying_symbol'] == vt_symbol, list(data.keys())] = list(data.values())

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
            self.write_log(f"判断交易时间时遇到错误 ({time_period}) {traceback.format_exc()}")
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
            self.write_log(f"设置交易信号时遇到错误 {traceback.format_exc()}")
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
            processed_results['target_option_rank'] = processed_results.groupby(['option_type', 'vt_underlying_symbol'], sort=False)['diff1'].rank()
            results_dict = {row['vt_symbol']: row.to_dict() for _, row in processed_results.iterrows()}

            target = []
            for (option_type, _), group in processed_results.groupby(['option_type', 'vt_underlying_symbol'], sort=False):
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
                    (target_option['remaining_trading_days'] >= 1) &
                    (target_option['remaining_trading_days'] <= 45)
                ].reset_index(drop=True)
            else:
                target_option = DataFrame()

            # results_dict 仅仅是把所有的 results 变成 dict
            # target_option 用于开仓
            return results_dict, target_option
        except Exception:
            self.write_log(f"计算交易信号时遇到错误 {traceback.format_exc()}")
            return {}, DataFrame()

    def exec_signal(self, results_dict: dict[str, dict], target_option: DataFrame) -> None:
        """执行信号"""
        if (
            not self.trading or
            target_option.empty  # 目标期权必须不为空, 否则 groupby 会报错
        ):
            return

        try:
            self.close_positions_for_risk_ctrl_and_take_profit(results_dict)
            self.cancel_orders_before_risk_ctrl(results_dict)
            self.close_positions_for_loop_risk_ctrl(results_dict)
            self.close_positions_for_AV(results_dict)
            self.open_positions(target_option)  # 邹老师: 法定节假日前关闭开仓
            self.cancel_wrong_orders(results_dict)
        except Exception:
            self.write_log(f"执行交易信号时遇到错误 {traceback.format_exc()}")

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
                (self.order_info['status'].isin([Status.NOTTRADED, Status.PARTTRADED]))
            ]
            conflict_found = False

            for _, row in open_order.iterrows():
                ordersysid: str = row['ordersysid']
                if (
                    row['vt_symbol'] == vt_symbol and
                    (
                        row['price'] >= price
                        if direction == Direction.SHORT
                        else row['price'] <= price
                    )
                ):
                    self.write_log(f"避免自成交请求撤单 {self.generate_order_info_string_from_series(row)}")
                    self.cancel_order_by_sysid(ordersysid)
                    conflict_found = True
            if conflict_found:
                sleep(0.5)  # FIXME 这会阻塞 run 线程
        except Exception:
            self.write_log(f"避免自成交请求撤单时遇到错误 ({vt_symbol}) {traceback.format_exc()}")
            return False  # 出现异常时默认禁止下单

        return not conflict_found  # 返回是否可以安全下单

    def open_positions(self, target_option: DataFrame) -> None:
        """开仓"""
        current_time = self.current_time.time()
        if (
            datetime.strptime('09:10', '%H:%M').time() <= current_time <= datetime.strptime('11:27', '%H:%M').time() or
            datetime.strptime('13:05', '%H:%M').time() <= current_time <= datetime.strptime('13:55', '%H:%M').time() or
            datetime.strptime('21:30', '%H:%M').time() <= current_time <= datetime.strptime('22:50', '%H:%M').time()
        ):
            for product_type, group in target_option.groupby('product_type', sort=False):
                for _, row in group.iterrows():
                    if self.open_condition(row, str(product_type)):
                        try:
                            vt_symbol: str = row['vt_symbol']
                            direction: Direction = Direction.SHORT
                            ask_price_1: float = row['option_askPrice1']
                            bid_price_1: float = row['option_bidPrice1']
                            volume: int = self.volume_per_open_position
                            memo: str = str(self.order_count)
                            
                            self.write_log(f"请求以卖一价开仓 合约={vt_symbol} 方向={direction} 手数={volume} Memo={memo} @{ask_price_1}")
                            self.request_open_position(vt_symbol, direction, ask_price_1, volume, memo)
                            
                            self.write_log(f"请求以买一价开仓 合约={vt_symbol} 方向={direction} 手数={volume} Memo={memo} @{bid_price_1}")
                            self.request_open_position(vt_symbol, direction, bid_price_1, volume, memo)
                            
                            # 更新计数器, 限制策略开仓数量
                            # Note: 主要是为了测试而添加的限制, 除此之外已经有根据品种占用的资金比例来限制开仓的机制了
                            self.total_traded_volume += self.volume_per_open_position * 2
                            self.single_traded_volume[vt_symbol] = self.single_traded_volume.get(vt_symbol, 0) + volume * 2
                        except Exception:
                            self.write_log(f"开仓时遇到错误 ({row['vt_symbol']}) {traceback.format_exc()}")

    def open_condition(self, row: pd.Series, product_type: str) -> bool:
        """开仓条件判断"""
        coefficient = row['vix']  # vix 越低对浮动要求越低, 也就越容易开仓, 反之亦然
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
                    row["remaining_trading_days"] <= 14
                )
                or
                (
                    row["option_lastPrice"] >= row["price_tick"] * 6
                    and
                    row["remaining_trading_days"] > 14
                )
            )
            and
            row["open_signal"]
            and
            row["option_volume"] > 100
            and
            row["option_askPrice1"] - row["option_bidPrice1"] < 3 * row["price_tick"]
            and
            row["remaining_trading_days"] <= 45
            and
            (pd.to_datetime(row["date_time"]) - self.current_time).total_seconds() < 20
            and
            self.main_engine.get_all_accounts()[0].available > 0.1 * self.main_engine.get_all_accounts()[0].balance
            and
            self.fund_position[product_type] < 0.1
            and
            self.total_traded_volume < self.max_open_position_volume_in_total
            and
            self.single_traded_volume.get(row['vt_symbol'], 0) < self.max_open_position_volume_per_contract
            and
            self.open_position_cooldown.test()
        )

    # TODO:
    def request_open_position(self, vt_symbol: str, direction: Direction, price: float, volume: int, memo: str) -> None:
        """发送开仓订单"""
        try:
            can_proceed = self.avoid_self_dealing(vt_symbol, direction, price)
            if not can_proceed:
                self.write_log(f"自成交风险未解除，跳过开仓 {vt_symbol}")
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
            self.write_log(f"开仓时遇到错误 ({vt_symbol}) {traceback.format_exc()}")

    # TODO:
    def request_close_position(self, vt_symbol: str, direction: Direction, price: float, volume: int, memo: str) -> None:
        """发送平仓订单"""
        try:
            can_proceed = self.avoid_self_dealing(vt_symbol, direction, price)
            if not can_proceed:
                # TODO 这个平仓函数可能会因为"防自成交机制"而平仓失败
                self.write_log(f"自成交风险未解除，跳过开仓 {vt_symbol}")
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
            self.write_log(f"平仓操作时遇到错误 ({vt_symbol}) {traceback.format_exc()}")


    @staticmethod
    def is_trading_time() -> bool:
        current_time = datetime.now(tz=CHINA_TZ).time()
        return (
            time(9, 10) <= current_time <= time(14, 57) or
            time(21, 10) <= current_time <= time(23, 55)
        )

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
        if data['remaining_trading_days'] <= 5:
            return (data['option_askPrice1'] - data['option_bidPrice1'] < 5 * data['price_tick'] and
                    data['close_signal'] and
                    data['option_volume'] > 15 and 
                    ((data['option_type'] == OptionType.CALL and data['strike_price'] < ((data['future_upperLimit'] / data['future_pre_settlement_price']) + 0.03) * data['future_lastPrice'])
                     or (data['option_type'] == OptionType.PUT and data['strike_price'] > ((data['future_lowerLimit'] / data['future_pre_settlement_price']) - 0.03) * data['future_lastPrice'])))
        else:
            return (data['option_askPrice1'] - data['option_bidPrice1'] < 5 * data['price_tick'] and
                    data['close_signal'] and
                    data['option_volume'] > 15)

    def cancel_orders_before_risk_ctrl(self, results_dict: dict[str, dict]) -> None:
        """
        风控前撤单.
        
        你可能会想, 为什么这个函数的说明里写着 "风控前撤单", 但却不是第一个执行的操作?
        比如撤单后, 等待交易所回报, 确认报单已撤销, 然后再发新的风控平仓单.
        原因是那么那么写有点复杂, 但实际上应该是要这样的.
        等有机会再重构这一块代码吧.
        """
        # 目标报单为: 平仓,未成交,非风控(止盈)
        target_orders = self.order_info[
            (self.order_info['offset'].isin([Offset.CLOSE, Offset.CLOSETODAY, Offset.CLOSEYESTERDAY])) &
            (self.order_info['status'].isin([Status.NOTTRADED])) &
            (~self.order_info['memo'].str.contains('RiskCtrl'))  # 未成交的止盈平仓单
        ]

        for _, row in target_orders.drop_duplicates().iterrows():
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
                    self.write_log(f"风控前撤单 请求撤单 {self.generate_order_info_string_from_series(row)}")
                    self.cancel_order_by_sysid(ordersysid)
                    self.order_info.loc[self.order_info['ordersysid'] == ordersysid, 'status'] = Status.CANCELLED  # FIXME 要留着吗? 实际上要等  on_order 更新才是真的撤单
            except Exception:
                self.write_log(f"再风控时遇到错误 ({vt_symbol}) {traceback.format_exc()}")

    def cancel_wrong_orders(self, results_dict: dict[str, dict]) -> None:
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
                    self.write_log(f"开仓前风控 请求撤单 {self.generate_order_info_string_from_series(row)}")
                    self.cancel_order_by_sysid(ordersysid)
            except Exception:
                self.write_log(f"开仓前风控时遇到错误 {self.generate_order_info_string_from_series(row)}")

    @staticmethod
    def split_volume(max_volume: int, total_volume: int) -> list[int]:
        """自动拆单"""
        return (
            [max_volume]
            * (total_volume // max_volume)
            + ([_v] if (_v := total_volume % max_volume) else [])
        )

    def close_positions_for_risk_ctrl_and_take_profit(self, results_dict: dict[str, dict]) -> None:
        """
        风控平仓(Risk-Ctrl)和止盈平仓(Take-Profit)
        """
        offset_converter: OffsetConverter = self.get_offset_converter()
        pos_holding_map: dict[str, PositionHolding] = offset_converter.holdings
        
        # 遍历每个合约 (vt_symbol) 和对应的持仓信息 (pos_holding)
        # 注意这个遍历会涉及所有合约 (有持仓的, 无持仓的, 都算在内)
        for (vt_symbol, pos_holding) in pos_holding_map.items():
            
            # FIXME 这个 if 可能发生吗?
            if vt_symbol not in results_dict:
                continue
            
            # 空头持仓量
            short_pos: int = int(pos_holding.short_pos)
            
            # 空头持仓可平量
            short_pos_available: int = int(pos_holding.short_pos - pos_holding.short_pos_frozen)
            
            # 跳过没有空头持仓的合约
            if short_pos <= 0:
                continue
            
            # 如果空头持仓可平量大于 0
            if short_pos_available > 0:
                try:
                    data = results_dict[vt_symbol]
                    option_type = data['option_type']
                    
                    # 如果达到风控条件，则挂风控平仓单
                    if (
                        self.is_trading_time() and
                        self.check_future_condition(data, option_type) and
                        self.check_option_condition(data)
                    ):
                        if self.close_positon_cooldown.test():
                            combined_volume = round((1 - self.combined_volumes(data['vt_symbol'], Direction.LONG) / self.combined_volumes(data['vt_symbol'], Direction.SHORT)) * short_pos_available)
                            if combined_volume > 0:
                                # 获取有效买一价，如果不存在或为0则使用最小价格单位，并确保不低于最小价格单位
                                bid_price = max(data.get('option_bidPrice1', data['price_tick']), data['price_tick'])
                                self.avoid_self_dealing(vt_symbol, Direction.LONG, bid_price)
                                volume_list = self.split_volume(int(data['max_volume']), combined_volume)
                                for sub in volume_list:
                                    self.write_log(f"[风控] 请求平仓{self.order_count} 合约={vt_symbol} 方向={Direction.LONG} 手数={sub} @{bid_price}")
                                    self.request_close_position(vt_symbol, Direction.LONG, bid_price, sub, f'RiskCtrl{self.order_count}')
                    
                    # 如果满足AV走势, 则什么也不做
                    elif self.AV_future_condition(data, option_type):
                        ...
                    
                    # 如果满足止盈条件, 则挂止盈平仓单
                    elif 19 < data['remaining_trading_days'] <= 130 and data['close_signal']:
                        volume_list = self.split_volume(int(data['max_volume']), short_pos_available)
                        for sub in volume_list:
                            self.write_log(f"[止盈] 请求平仓{self.order_count} 合约={vt_symbol} 方向={Direction.LONG} 手数={sub} @{data['price_tick'] * 3}")
                            self.request_close_position(vt_symbol, Direction.LONG, data['price_tick'] * 3, sub, str(self.order_count))
                    elif 11 < data['remaining_trading_days'] <= 19 and data['close_signal']:
                        volume_list = self.split_volume(int(data['max_volume']), short_pos_available)
                        for sub in volume_list:
                            self.write_log(f"[止盈] 请求平仓{self.order_count} 合约={vt_symbol} 方向={Direction.LONG} 手数={sub} @{data['price_tick']}")
                            self.request_close_position(vt_symbol, Direction.LONG, data['price_tick'], sub, str(self.order_count))
                    elif 6 < data['remaining_trading_days'] <= 11 and data['close_signal']:
                        volume_list = self.split_volume(int(data['max_volume']), short_pos_available)
                        for sub in volume_list:
                            self.write_log(f"[止盈] 请求平仓{self.order_count} 合约={vt_symbol} 方向={Direction.LONG} 手数={sub} @{data['price_tick']}")
                            self.request_close_position(vt_symbol, Direction.LONG, data['price_tick'], sub, str(self.order_count))
                except Exception:
                    self.write_log(f"平仓时遇到错误 ({vt_symbol})")
            
            # 如果空头持仓可平量小于等于 0
            else:
                try:
                    data = results_dict[vt_symbol]
                    option_type = data['option_type']
                    product_name = data['期货']
                    if (
                        self.is_trading_time() and
                        self.check_future_condition(data, option_type) and
                        self.contract_send_count.get(vt_symbol, 0) < 1
                    ):
                        context = (
                            f"账户：谦量天风\n"
                            f"合约：{product_name} {vt_symbol}\n"
                            f"风控平仓待报入"
                        )
                        self.write_log(context)
                        self.main_engine.send_feishu(
                            feishu_webhook_url,
                            feishu_message_template(context)
                        )
                        self.contract_send_count[vt_symbol] = 1
                except Exception:
                    self.write_log(f"发送风控平仓待报入时遇到错误 ({vt_symbol}) {traceback.format_exc()}")
        
    def combined_volumes(self, vt_symbol: str, direction: Direction) -> float:
        """匹配相同标的，到期日，方向的合约数量"""
        pos_holding: PositionHolding = self.get_position_holding(vt_symbol)
        # 为什么多头算持仓量, 而空头算可平量?
        # 如果多空都算可平量, 则多头可平量始终为0, 因为多头不会被程序撤单
        # 如果多空都算持仓量, 则空头的持仓量不变, 就会被程序不断的AV平仓
        match direction:
            # 如果是多头, 就计算持仓量
            case Direction.LONG: return pos_holding.long_pos
            # 如果是空头, 则计算可平量
            case Direction.SHORT: return pos_holding.short_pos - pos_holding.short_pos_frozen
            # 如果不是多也不是空, 则抛出异常终止程序
            case _:
                raise ValueError(f"Unsupported direction: {direction}")

    def close_positions_for_loop_risk_ctrl(self, results_dict: dict[str, dict]) -> None:
        """循环风控"""
        if self.order_info.empty:
            return

        try:
            orders_to_process = self.order_info[
                (self.order_info['loop_risk_ctrl_time'].notna()) &
                (self.order_info['status'].isin([Status.NOTTRADED, Status.PARTTRADED]))
            ]

            for index, row in orders_to_process.iterrows():
                vt_symbol: str = row['vt_symbol']
                ordersysid: str = row['ordersysid']
                    
                if vt_symbol not in results_dict:
                    continue

                result_data = results_dict[vt_symbol]
                signal = result_data['open_signal']
                if self.current_time > row['loop_risk_ctrl_time'] and signal:
                    direction: Direction = Direction.LONG
                    price: float = result_data['option_bidPrice1'] + result_data['price_tick']
                    volume: int = row['volume']
                    memo: str = f"RiskCtrl{str(self.order_count)}"
                    
                    self.loop_risk_ctrl.start(
                        ordersysid=ordersysid,
                        vt_symbol=vt_symbol,
                        direction=direction,
                        price=price,
                        volume=volume,
                        memo=memo,
                    )
        except Exception:
            self.write_log(f"循环风控平仓遇到错误 {traceback.format_exc()}")

    @staticmethod
    def AV_future_condition(data: dict, option_type: OptionType) -> bool:
        """AV型走势，即行情短时间剧烈下跌，但随后又迅速反弹的情况"""
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

    def close_positions_for_AV(self, results_dict: dict[str, dict]) -> None:
        """AV型走势平仓"""
        # 筛选出止盈单 (即 非风控单/非特别单/手动单)
        matched_order_info: DataFrame = self.order_info[(self.order_info['offset'].isin([Offset.CLOSE, Offset.CLOSETODAY, Offset.CLOSEYESTERDAY])) &
                                       (self.order_info['status'].isin([Status.NOTTRADED])) &
                                       (self.order_info['direction'] == Direction.LONG) &
                                       (~self.order_info['memo'].str.contains('RiskCtrl')) &
                                       (~self.order_info['memo'].str.contains('Special'))]

        for _, row in matched_order_info.drop_duplicates().iterrows():
            vt_symbol: str = row['vt_symbol']
            ordersysid: str = row['ordersysid']

            if vt_symbol not in results_dict:
                continue

            try:
                data = results_dict[vt_symbol]
                option_type = data['option_type']
                if self.AV_future_condition(data, option_type):
                    pos_holding: PositionHolding = self.get_position_holding(vt_symbol)
                    short_pos: int = int(pos_holding.short_pos)
                    if short_pos <= 0:
                        self.write_log(f"{vt_symbol} 不存在空头持仓, 无法执行 AV 走势平仓")
                        self.write_log(f"{row}")
                        continue
                    
                    # 开始执行 AV 走势平仓
                    max_volume: int = data['max_volume']
                    price: float = max(data.get('option_bidPrice1', data['price_tick']), data['price_tick'])
                    self.av_trend_close_pos.start(
                        ordersysid=ordersysid,
                        vt_symbol=vt_symbol,
                        short_position=short_pos,
                        max_volume=max_volume,
                        price=price,
                        memo=f'Special{self.order_count}'
                    )
            except Exception:
                # 发送飞书消息  # TODO 临时措施. 等AV走势平仓错误修复后应该移除
                self.av_trend_temp_fix.notify(vt_symbol)
                
                # 写入日志文件
                self.write_log(f"AV走势特别平仓遇到错误 ({vt_symbol}) {traceback.format_exc()}")

    def on_order(self, order: OrderData) -> None:
        """处理订单更新"""
        
        ordersysid: str | None = order.ordersysid
        if not ordersysid:
            # 不存在 ordersysid 则直接忽略该回报
            # 这种情况是由 vnpy 内部直接调用 on_order 时发生的
            # 具体参考底层接口的实现, 例如 vnpy_ctp
            return
        if order.status == Status.SUBMITTING:
            # 如果订单状态是 SUBMITTING，则忽略该回报
            # 这种情况对应交易所返回的 未知单 回报, 说明交易所已经收到报单请求了
            # 但是还没有为该订单生成 ordersysid
            return
        
        self.write_log(f"订单信息更新 {self.generate_order_info_string_from_order_data(order)}")

        # 响应 LoopRiskCtrl
        try:
            self.loop_risk_ctrl.on_order(order)
        except Exception:
            self.write_log(f"执行OP1操作时发生错误 {traceback.format_exc()}")
        
        # 响应 AvTrendClosePose
        try:
            self.av_trend_close_pos.on_order(order)
        except Exception:
            self.write_log(f"执行 AvTrendClosePos 操作时发生错误 {traceback.format_exc()}")
        
        try:
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
            
            # 更新还未成交的风控单的 'loop_risk_ctrl_time'，使其能够在 def close_positons_for_loop_risk_ctrl 中进行再风控操作
            # 目前的程序逻辑: 如果 self.order_info 的 'loop_risk_ctrl_time' 列不为 NaT 则说明需要循环风控, 没有则说明不需要
            mask_orders_in_risk_ctrl = self.order_info['memo'].str.contains('RiskCtrl')
            mask_orders_on_pending = (self.order_info['status'] == Status.NOTTRADED) | (self.order_info['status'] == Status.PARTTRADED)
            mask_orders_to_loop_risk_ctrl = mask_orders_in_risk_ctrl & mask_orders_on_pending
            self.order_info['loop_risk_ctrl_time'] = pd.Series(pd.NaT, dtype='datetime64[ns, Asia/Shanghai]')  # Note: 必须指定 dtype 使 NaT 带上时区
            self.order_info.loc[mask_orders_to_loop_risk_ctrl, 'loop_risk_ctrl_time'] = self.order_info.loc[mask_orders_to_loop_risk_ctrl, 'datetime'] + pd.Timedelta(seconds=self.loop_risk_ctrl_cooldown)
            
            if (
                order.status == Status.NOTTRADED and
                (
                    'RiskCtrl' in order.memo or
                    'Special' in order.memo
                )
            ):
                product_name: str = self.results.loc[self.results['vt_symbol'] == order.vt_symbol, '期货'].item()
                context = (
                    f'账户：谦量天风\n'
                    f'合约：{product_name} {order.vt_symbol}\n'
                    f'价格：{order.price}\n'
                    f'数量：{order.volume}\n'
                    f'备注：{order.memo}'
                )
                self.write_log(context)
                self.main_engine.send_feishu(
                    feishu_webhook_url,
                    feishu_message_template(context),
                )
        except Exception:
            self.write_log(f"处理订单更新遇到错误 {traceback.format_exc()}")

    def on_trade(self, trade: TradeData) -> None:
        """处理成交更新"""
        self.write_log(f"成交信息更新 {self.generate_trade_info_string_from_trade_data(trade)}")
    
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
    
    @staticmethod
    def convert_to_timestamp_or_nat(dt: datetime | None):
        """
        将 datetime 对象转换为 pd.Timestamp, 若为 None 则返回 pd.NaT.
        
        Args:
            dt (datetime | None): 要转换的 datetime 对象.
        Returns:
            pd.Timestamp | pd.NaT: 转换后的时间戳, 如果 dt 为 None 则返回 pd.NaT.
        """
        return pd.to_datetime(dt) if dt else pd.NaT
            
    def get_offset_converter(self) -> OffsetConverter:
        """获取开平转换器"""
        gateway_name: str | None = self.gateway_name
        if not gateway_name:
            raise Exception("Gateway name is not set.")
        offset_converter: OffsetConverter | None = self.main_engine.get_converter(gateway_name)
        if not offset_converter:
            raise Exception("Converter not found for gateway: {gateway_name}")
        return offset_converter

    def get_position_holding(self, vt_symbol: str) -> PositionHolding:
        """获取实时持仓信息"""
        offset_converter: OffsetConverter = self.get_offset_converter()
        position_holding: PositionHolding | None = offset_converter.get_position_holding(vt_symbol)
        if not position_holding:
            raise Exception("PositionHolding not found for vt_symbol: {vt_symbol}")
        return position_holding

    # TODO 需要更好的抽象, 不然将同一个策略用于不同的柜台时, 将不得不复制粘贴几乎全部的代码
    def convert_product_to_canconinal(self, tianfeng_product: str) -> str:
        """
        将柜台返回的 symbol 转换为标准形式.
        通常需要在外部数据进入到内部逻辑前就进行转换.
        标准形式将用于策略内部的逻辑编写和数据处理.
        
        为什么需要这个函数?
        因为不同柜台返回的 symbol 不尽相同, 需要进行转换.
        比如紫金天风柜台返回的郑商所甲醇是 MA, 而标准形式是 MA_O.
        又比如大友期货柜台返回的跟很多柜台的都不一样.
        
        Args:
            tianfeng_symbol (str): 天风柜台返回的 symbol.
        Returns:
            str: 标准形式的 symbol.
        """
        return self.product_mapping_dict[tianfeng_product]
    
    def convert_order_to_df(self, order: OrderData) -> DataFrame:
        """
        将 OrderData 转换为一个 DataFrame.
        """
        return DataFrame(
            data={
                # 原生字段
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
                
                # 衍生字段
                "vt_symbol": order.vt_symbol,
                "vt_orderid": order.vt_orderid,
            },
            index=[0]
        )
    
    @staticmethod
    def generate_trade_info_string_from_trade_data(data: TradeData) -> str:
        """
        生成成交信息的日志字符串.
        
        Args:
            data: TradeData 对象, 通常是传入到 on_trade 回调中的成交数据.
        
        Returns:
            str: 格式化后的成交信息字符串.
        """
        # 换行记录成交信息的各个部分, 在日志中看起来比较显眼
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
        """
        生成订单信息的日志字符串.
        
        Args:
            data: OrderData 对象, 通常是传入到 on_order 回调中的订单数据.
        
        Returns:
            str: 格式化后的订单信息字符串.
        """
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
    
    @staticmethod
    def generate_order_info_string_from_series(series: Series) -> str:
        """
        生成订单信息的日志字符串.
        
        Args:
            series: 订单信息的 Series 对象, 通常是 order_info 的一行 (row).
        
        Returns:
            str: 格式化后的订单信息字符串.
        """
        return (
            f"订单信息: "
            f"合约={series['vt_symbol']}, "
            f"编号={series['ordersysid']}, "
            f"状态={series['status']}, "
            f"方向={series['direction']}, "
            f"开平={series['offset']}, "
            f"价格={series['price']}, "
            f"数量={series['volume']}, "
            f"Memo={series['memo']}"
        )
