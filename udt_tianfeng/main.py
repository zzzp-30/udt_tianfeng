import multiprocessing
import sys
from concurrent.futures import ALL_COMPLETED, Future, wait
from datetime import datetime, time
from logging import DEBUG, INFO
from pathlib import Path
from time import sleep

import akshare as ak
import pandas as pd
from vnpy.trader.engine import EventEngine, MainEngine, OmsEngine
from vnpy.trader.object import AccountData, OrderData, PositionData, TradeData
from vnpy.trader.setting import SETTINGS
from vnpy.trader.utility import get_file_path
from vnpy_ctp import CtpGateway
from vnpy_simplestrategy import SimpleStrategyApp, StrategyEngine


# 紫金天风 实盘
ctp_setting = {
    "用户名": "89110038",
    "密码": "wjzb7777",
    "经纪商代码": "0001",
    "交易服务器": "101.230.80.85:41206",
    "行情服务器": "101.230.80.85:41214",
    "产品名称": "client_unboundream_v2",
    "授权编码": "3J474CT8DL4EUW6F",
    "产品信息": "unboundream"
}


# Chinese futures market trading period (day/night)
DAY_START = time(8, 45)
DAY_END = time(15, 0)

NIGHT_START = time(20, 45)
NIGHT_END = time(2, 45)


def get_trading_days() -> pd.DataFrame:
    """获取交易日数据，优先从本地文件读取，不存在则从AkShare获取"""
    trading_days_file_path: Path = get_file_path("trading_days.csv")
    
    # 检查文件是否存在且未过期
    if trading_days_file_path.exists():
        try:
            file_mtime: float = trading_days_file_path.stat().st_mtime
            current_time: float = datetime.now().timestamp()
            
            # 如果文件未过期(6小时内)，直接读取
            diff: float = current_time - file_mtime
            if diff <= 6 * 60 * 60:
                df: pd.DataFrame = pd.read_csv(trading_days_file_path)
                return df
        except Exception:
            pass
    
    # 文件不存在或已过期，从AkShare获取数据
    try:
        df = ak.tool_trade_date_hist_sina()
        df.to_csv(trading_days_file_path, index=False)
        return df
    except Exception as e:
        print(f"获取交易日数据失败: {e}")
        return pd.DataFrame()

def is_trading_day() -> bool:
    """检查当前日期是否为交易日"""
    try:
        trading_days_df: pd.DataFrame = get_trading_days()
        if trading_days_df.empty:
            return True  # 如果无法获取交易日数据，默认返回True
        
        current_date: str = datetime.now().strftime('%Y-%m-%d')
        trading_dates: list[str] = trading_days_df['trade_date'].astype(str).tolist()
        
        return current_date in trading_dates
    except Exception as e:
        print(f"检查交易日失败: {e}")
        return True  # 出错时默认返回True

def check_trading_period() -> bool:
    """检查是否在交易时间段内且为交易日"""
    # 首先检查是否为交易日
    if not is_trading_day():
        return False
    
    current_time: time = datetime.now().time()
    
    # 检查是否在交易时间段内
    is_day_trading: bool = DAY_START <= current_time <= DAY_END
    is_night_trading: bool = current_time >= NIGHT_START or current_time <= NIGHT_END
    
    # return True  # 适配 7*24，把这行注释去掉的话，交易策略 7*24 小时都会保持运行
    return is_day_trading or is_night_trading

# TODO 如果父进程，子进程都在运行，支持 Ctrl-C 正常中断子进程
#      如果父进程在运行，而子进程没在运行，按下 Ctrl-C 是终止父进程
#      也就是说，如果父进程和子进程都在运行，要按两下 Ctrl-C 完全退出程序
def run_child() -> None:
    """
    Running in the child process.
    """
    
    # --- 构建主引擎 ---
    
    # 创建主引擎
    main_engine: MainEngine = MainEngine()
    
    # 获取订单引擎
    oms_engine: OmsEngine = main_engine.get_engine("oms") # type: ignore
    
    # 使用 CtpGateway
    main_engine.add_gateway(CtpGateway)
    main_gateway: CtpGateway = main_engine.get_gateway("CTP") # type: ignore
    
    # --- 登录底层接口 CTP ---
    
    # 尝试登录，如果失败则退出程序

    # Note: CtpGateway#connect 会开始一个定时任务, 按固定间隔更新 AccountData 和 PositionData
    main_gateway.connect(ctp_setting)
    main_engine.write_log("连接CTP接口")

    tries = 0
    while not main_gateway.td_api.login_status and tries < 10:
        tries += 1
        main_engine.write_log("等待CTP接口登录")
        sleep(1)
    if not main_gateway.td_api.login_status:
        main_engine.write_log("CTP接口登录失败")
        sys.exit(0)
    
    tries = 0
    while not main_gateway.td_api.contract_inited and tries < 180:
        tries += 1
        main_engine.write_log("等待CTP合约信息初始化")
        sleep(1)
    if not main_gateway.td_api.contract_inited:
        main_engine.write_log("CTP接口合约初始化失败")
        sys.exit(0)
    
    main_engine.write_log("主引擎创建成功")
    
    # --- 打印投资者账户信息 ---
    
    main_engine.write_log("订单信息(所有账号):")
    [main_engine.write_log(f"vt_symbol={order.vt_symbol}, vt_orderid={order.vt_orderid}, ordersysid={order.ordersysid}, direction={order.direction}, offset={order.offset}, status={order.status} @{order.price}") for order in main_engine.get_all_orders()]
    
    main_engine.write_log("成交信息(所有账号):")
    [main_engine.write_log(f"vt_symbol={trade.vt_symbol}, vt_orderid={trade.vt_orderid}, direction={trade.direction}, offset={trade.offset} @{trade.price}") for trade in main_engine.get_all_trades()]
    
    main_engine.write_log("持仓信息(所有账号):")
    [main_engine.write_log(f"vt_symbol={pos.vt_symbol}, volume={pos.volume}, frozen={pos.frozen}, vt_positionid={pos.vt_positionid} @{pos.price}") for pos in main_engine.get_all_positions()]
    
    main_engine.write_log("账户信息(所有):")
    [main_engine.write_log(f"vt_accountid={acc.vt_accountid}, balance={acc.balance}, frozen={acc.frozen}, available={acc.available}") for acc in main_engine.get_all_accounts()]
    
    # --- 启动 SimpleStrategy App ---
    
    # 添加 App
    strategy_engine: StrategyEngine = main_engine.add_app(SimpleStrategyApp) # type: ignore
    # 初始化 Engine
    strategy_engine.init_engine()
    # 打印已加载的 strategy classes
    strategy_engine.write_log(f"已加载策略: {strategy_engine.get_all_strategy_class_names()}")
    # 调用 StrategyTemplate#on_init (异步执行，但同步等待)
    wait(strategy_engine.init_all_strategies().values())
    # 调用 StrategyTemplate#on_start
    strategy_engine.start_all_strategies()
    
    while True:
        sleep(10)
        trading = check_trading_period()
        if not trading:
            print("关闭子进程")
            main_engine.close()
            sys.exit(0)

def run_parent() -> None:
    """
    Running in the parent process.
    """
    print("启动CTA策略守护父进程")

    child_process = None

    while True:
        trading = check_trading_period()

        # Start child process in trading period
        if trading and child_process is None:
            print("启动子进程")
            child_process = multiprocessing.Process(target=run_child)
            child_process.start()
            print("子进程启动成功")

        # Stop child process if not in trading period
        if not trading and child_process is not None:
            if not child_process.is_alive():
                child_process = None
                print("子进程关闭成功")

        sleep(5)


if __name__ == "__main__":
    run_parent()
