from vnpy_ctastrategy import (
    CtaTemplate,
    StopOrder,
    TickData,
    BarData,
    TradeData,
    OrderData
)

from time import time


class OrderWatcher(CtaTemplate):
    """"""
    author = "Zheyin Zeng"
    
    tick_count = 0  # 统计 tick 次数
    
    variables = ["tick_count"]

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

    def on_stop(self) -> None:
        """
        Callback when strategy is stopped.
        """
        self.write_log("策略已停止")

    def on_tick(self, tick: TickData) -> None:
        """
        Callback of new tick data update.
        """
        self.tick_count += 1
        if self.tick_count % 10 == 0:
            vt_orderids = self.buy(price=tick.last_price * 0.5, volume=1)
            self.write_log(f"tick={self.tick_count}, vt_orderids={vt_orderids}")
        
        if self.tick_count % 20 == 0:
            self.cancel_all()
            self.write_log(f"tick={self.tick_count}, cancel_all={self.cta_engine.strategy_orderid_map}")

    def on_bar(self, bar: BarData) -> None:
        """
        Callback of new bar data update.
        """
        ...

    def on_order(self, order: OrderData) -> None:
        """
        Callback of new order data update.
        """
        self.put_event()
        self.write_log(f"on_order: symbol={order.symbol}, vt_symbol={order.vt_symbol} orderid={order.orderid} vt_orderid={order.vt_orderid} @{order.price}")

    def on_trade(self, trade: TradeData) -> None:
        """
        Callback of new trade data update.
        """
        self.put_event()
        self.write_log(f"on_trade: symbol={trade.symbol} vt_symbol={trade.vt_symbol} orderid={trade.orderid} vt_orderid={trade.vt_orderid} tradeid={trade.tradeid} vt_tradeid={trade.vt_tradeid} @{trade.price}")

    def on_stop_order(self, stop_order: StopOrder) -> None:
        """
        Callback of stop order update.
        """
        self.put_event()
        self.write_log(f"on_stop_order: symbol={stop_order.vt_symbol} strategy_name={stop_order.strategy_name} vt_orderids={stop_order.vt_orderids} @{stop_order.price}")
