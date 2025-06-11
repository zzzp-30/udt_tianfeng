from vnpy.trader.engine import OmsEngine
from vnpy.trader.object import *
from vnpy.trader.utility import extract_vt_symbol
from vnpy_ctastrategy import (
    CtaEngine,
    CtaTemplate,
    StopOrder,
    TickData,
    BarData,
    TradeData,
    OrderData
)

from time import time


class CtptestCancelOrder(CtaTemplate):
    """"""
    author = "Zheyin Zeng"
    parameters = []
    variables = []

    def on_init(self) -> None:
        """
        Callback when strategy is inited.
        """
        self.write_log("策略已初始化")

    def on_start(self) -> None:
        """
        Callback when strategy is started.
        """
        self.write_log("策略已启动")
        
        cta_engine: CtaEngine = self.cta_engine
        oms_engine: OmsEngine = cta_engine.main_engine.get_engine("oms")
        
        # 报几单不太可能成交的，之后会撤单
        self.trading = True
        self.send_order(direction=Direction.LONG, offset=Offset.OPEN, price=3300, volume=1)
        self.send_order(direction=Direction.LONG, offset=Offset.OPEN, price=3350, volume=1)

    def on_stop(self) -> None:
        """
        Callback when strategy is stopped.
        """
        self.write_log("策略已停止")

    def on_tick(self, tick: TickData) -> None:
        """
        Callback of new tick data update.
        """
        self.write_log(f"on_tick: {tick}")

    def on_bar(self, bar: BarData) -> None:
        """
        Callback of new bar data update.
        """
        self.write_log(f"on_bar: {bar}")

    def on_order(self, order: OrderData) -> None:
        """
        Callback of new order data update.
        """
        self.put_event()
        self.write_log(f"on_order: vt_symbol={order.vt_symbol} vt_orderid={order.vt_orderid} @{order.price}")
        
        # if order.status != Status.CANCELLED:
        #     self.write_log(f"撤单: {order.vt_orderid}")
        #     self.cancel_order(order.vt_orderid)

    def on_trade(self, trade: TradeData) -> None:
        """
        Callback of new trade data update.
        """
        self.put_event()
        self.write_log(f"on_trade: vt_symbol={trade.vt_symbol} vt_orderid={trade.vt_orderid} vt_tradeid={trade.vt_tradeid} @{trade.price}")

    def on_stop_order(self, stop_order: StopOrder) -> None:
        """
        Callback of stop order update.
        """
        self.put_event()
        self.write_log(f"on_stop_order: vt_symbol={stop_order.vt_symbol} strategy_name={stop_order.strategy_name} vt_orderids={stop_order.vt_orderids} @{stop_order.price}")
