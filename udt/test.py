if __name__ == "__main__":
    import talib
    import numpy as np
    
    class TestUtility:
        def __init__(self, close: np.ndarray, size: int):
            self.close = close
            self.size = size

        def macd_window(
            self,
            fast_period: int,
            slow_period: int,
            signal_period: int,
            array: bool = False,
        ) -> list[tuple[np.ndarray, np.ndarray, np.ndarray]] | list[tuple[float, float, float]]:
            size: int = self.size
            close: np.ndarray = self.close
            ret: list = []
            for until in np.arange(start=fast_period, stop=size):  # 从 fast_period 开始，确保窗口足够大
                macd, signal, hist = talib.MACD(
                    close[:until], fast_period, slow_period, signal_period
                )
                if array:
                    ret.append((macd, signal, hist))
                else:
                    ret.append((macd[-1], signal[-1], hist[-1]))
            return ret

    # 测试数据
    close_prices = np.arange(start=0, stop=300, dtype=float)
    size = len(close_prices)

    # 创建测试实例
    utility = TestUtility(close_prices, size)

    # 测试 macd_window 函数
    fast_period = 12
    slow_period = 26
    signal_period = 9

    for i in np.arange(0, 21):
        print("Testing macd_window with array=True:")
        result_array = utility.macd_window(fast_period, slow_period, signal_period, array=True)
        for idx, (macd, signal, hist) in enumerate(result_array):
            print(f"Window {idx}:")
            print(f"MACD: {macd}")
            print(f"Signal: {signal}")
            print(f"Hist: {hist}")

        print("\nTesting macd_window with array=False:")
        result_values = utility.macd_window(fast_period, slow_period, signal_period, array=False)
        for idx, (macd, signal, hist) in enumerate(result_values):
            print(f"Window {idx}:")
            print(f"MACD: {macd}")
            print(f"Signal: {signal}")
            print(f"Hist: {hist}")