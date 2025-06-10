import multiprocessing
import sys
from concurrent.futures import ALL_COMPLETED, Future, wait
from datetime import datetime, time
from logging import DEBUG, INFO
from pathlib import Path
from time import sleep

from vnpy.trader.setting import SETTINGS

# --- 设置 Log --- (必须早于 vnpy 其他模块导入，因为其他模块可能依赖 SETTINGS 这个全局变量)
SETTINGS["log.active"] = True
SETTINGS["log.level"] = DEBUG
SETTINGS["log.console"] = True
SETTINGS["log.file"] = True
# 2025/5/30
# 需要设置一下 datafeed，不然脚本启动不了
# 但考虑到目前提供的都是收费的，所以这一块需要再写点代码，要么绕过去不用，要么用免费的 akshare
# 目前我用的是迅投的免费试用14天，首次注册就可以获得14天的 token: https://xuntou.net/
# 2025/5/31
# 发现 tushare，一个免费的数据源，类似 akshare，但数据是整理过的，也许可以完全替换掉 akshare
# 2025/6/1
# 设置 Datafeed，用于获取历史行情，应用场景有回测和行情分析
SETTINGS["datafeed.name"] = "tushare"
SETTINGS["datafeed.username"] = "token"
SETTINGS["datafeed.password"] = "c3a110417f08f26d2c221edc0c50d4a8a5001502eea89cf5"

from vnpy.trader.engine import MainEngine, EventEngine, OmsEngine
from vnpy.trader.object import *
# from vnpy_tts import TtsGateway
# from vnpy_ctp import CtpGateway
from vnpy_ctptest import CtptestGateway
from vnpy_portfoliostrategy import PortfolioStrategyApp, StrategyEngine
from vnpy_ctastrategy import CtaEngine, CtaStrategyApp


# SimNow 仿真 (周末/节假日完全无法访问)
# ctp_setting = {
#     "用户名": "242558",  # 这里用的 SimNow 提供的模拟接口: https://www.simnow.com.cn/product.action
#     "密码": "787390@aaa",
#     "经纪商代码": "9999",
#     "交易服务器": "180.168.146.187:10201",
#     "行情服务器": "180.168.146.187:10211",
#     "产品名称": "simnow_client_test",
#     "授权编码": "0000000000000000",
#     "产品信息": "www"
# }

# SimNow 7*24
# ctp_setting = {
#     "用户名": "242558",  # 这里用的 SimNow 提供的模拟接口: https://www.simnow.com.cn/product.action
#     "密码": "787390@aaa",
#     "经纪商代码": "9999",
#     "交易服务器": "180.168.146.187:10130",
#     "行情服务器": "180.168.146.187:10131",
#     "产品名称": "simnow_client_test",
#     "授权编码": "0000000000000000",
#     "产品信息": "www"
# }

# TTS 7*24
# ctp_setting = {
#     "用户名": "12821",
#     "密码": "123456",
#     "经纪商代码": "",
#     "交易服务器": "121.37.80.177:20002",
#     "行情服务器": "121.37.80.177:20004",
#     "产品名称": "",
#     "授权编码": "",
#     "产品信息": ""
# }

# TTS 仿真
# ctp_setting = {
#     "用户名": "12478",
#     "密码": "123456",
#     "经纪商代码": "",
#     "交易服务器": "121.37.90.193:20002",
#     "行情服务器": "121.37.80.177:20004",  # TTS 仿真的行情服务器与 7*24 是共享的
#     "产品名称": "",
#     "授权编码": "",
#     "产品信息": ""
# }

# 紫金天风 仿真
ctp_setting = {
    "用户名": "61130",
    "密码": "tfqh@123",
    "经纪商代码": "0001",
    "交易服务器": "114.80.55.98:64205",
    "行情服务器": "114.80.55.98:64213",
    "产品名称": "client_unboundream_v2",
    "授权编码": "3J474CT8DL4EUW6F",
    "产品信息": "unboundream"
}

# 紫金天风 实盘
# ctp_setting = {
#     "用户名": "61130",
#     "密码": "tfqh@123",
#     "经纪商代码": "0001",
#     "交易服务器": "101.230.80.85:41206",
#     "行情服务器": "101.230.80.85:41214",
#     "产品名称": "client_unboundream_v2",
#     "授权编码": "3J474CT8DL4EUW6F",
#     "产品信息": "unboundream"
# }

# 宏源期货 仿真
# ctp_setting = {
#     "用户名": "",
#     "密码": "",
#     "经纪商代码": "",
#     "交易服务器": "",
#     "行情服务器": "",
#     "产品名称": "client_unboundream_v2",
#     "授权编码": "",
#     "产品信息": "unboundream"
# }


# Chinese futures market trading period (day/night)
DAY_START = time(8, 45)
DAY_END = time(15, 0)

NIGHT_START = time(20, 45)
NIGHT_END = time(2, 45)


def check_trading_period() -> bool:
    """"""
    current_time = datetime.now().time()

    trading = False
    if (
        (current_time >= DAY_START and current_time <= DAY_END)
        or (current_time >= NIGHT_START)
        or (current_time <= NIGHT_END)
    ):
        trading = True

    # return trading
    return True  # 适配 7*24


def run_child() -> None:
    """
    Running in the child process.
    """
    
    # --- 构建主引擎 ---
    
    # 创建主引擎
    main_engine: MainEngine = MainEngine()
    # 获取订单引擎
    oms_engine: OmsEngine = main_engine.get_engine("oms")
    # 使用 TtsGateway，参考：https://github.com/vnpy/vnpy_tts
    # main_engine.add_gateway(TtsGateway)
    # main_engine.add_gateway(CtpGateway)
    main_engine.add_gateway(CtptestGateway)
    # 获取 TtsGateway 实例，后面会用于判断 CTP 登录状态
    # main_gateway: TtsGateway = main_engine.get_gateway("TTS")
    # main_gateway: CtpGateway = main_engine.get_gateway("CTP")
    main_gateway: CtptestGateway = main_engine.get_gateway("CTPTEST")
    # 添加 CtaStrategy App
    cta_engine: CtaEngine = main_engine.add_app(CtaStrategyApp)
    # 加载 CTA 策略 class
    cta_engine.load_strategy_class()  # it does work - 把策略 class 放在 ~/strategies 下即可
    # 打印加载的 CTA 策略 class
    cta_engine.write_log(f"已加载策略: {cta_engine.get_all_strategy_class_names()}")
    
    # --- 创建 CTA 策略 object ---
    # 测试报单、撤单
    cta_engine.add_strategy("Ctptest1", "ctptest_1", "ao2507C4000.SHFE", {})
    # 测试成交
    cta_engine.add_strategy("Ctptest2", "ctptest_2", "ao2507C4200.SHFE", {})
    
    # --- 登录 CTP ---
    
    # 尝试登录，如果失败则退出程序
    # main_engine.connect(ctp_setting, "TTS")
    # main_engine.connect(ctp_setting, "CTP")
    main_engine.connect(ctp_setting, "CTPTEST")
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

    # --- 启动 CTA 策略 ---
    
    # cta_engine.init_engine()
    # strategy_futures: dict[str, Future] = cta_engine.init_all_strategies()
    # wait(strategy_futures.values(), return_when=ALL_COMPLETED)
    # cta_engine.start_all_strategies()
    
    main_gateway.cancel_order(req=CancelRequest(
        symbol="ao2507C4200",
        exchange=Exchange.SHFE,
        ordersysid="      140465"
    ))
    main_gateway.cancel_order(req=CancelRequest(
        symbol="ao2507C4200",
        exchange=Exchange.SHFE,
        ordersysid="      140466"
    ))
    
    sleep(1)
    
    # --- 账户相关信息 ---
    
    main_engine.write_log("当前账号的所有订单信息:")
    [main_engine.write_log(f"vt_symbol={order.vt_symbol}, vt_orderid={order.vt_orderid}, ordersysid={order.ordersysid}, direction={order.direction}, offset={order.offset}, status={order.status} @{order.price}") for order in main_engine.get_all_orders()]
    
    main_engine.write_log("当前账号的所有成交信息:")
    [main_engine.write_log(f"vt_symbol={trade.vt_symbol}, vt_orderid={trade.vt_orderid}, direction={trade.direction}, offset={trade.offset} @{trade.price}") for trade in main_engine.get_all_trades()]
    
    main_engine.write_log("当前账号的所有持仓信息:")
    [main_engine.write_log(f"vt_symbol={pos.vt_symbol}, volume={pos.volume}, frozen={pos.frozen}, vt_positionid={pos.vt_positionid} @{pos.price}") for pos in main_engine.get_all_positions()]
    
    main_engine.write_log("当前账号的账户信息:")
    [main_engine.write_log(f"vt_accountid={acc.vt_accountid}, balance={acc.balance}, frozen={acc.frozen}, available={acc.available}") for acc in main_engine.get_all_accounts()]
    
    # --- 订阅所有期权合约 ---
    
    main_gateway.write_log("开始订阅行情")
    all_contract_list: list[ContractData] = main_engine.get_all_contracts()
    counter = 0  # 新增计数器
    for contract in all_contract_list:
        if (contract.product == Product.OPTION):
            counter += 1
            if counter % 1000 == 0:  # 防止 4096 错误
                sleep(1)
            main_gateway.subscribe(req=SubscribeRequest(
                symbol=contract.symbol,
                exchange=contract.exchange,
            ))
    main_gateway.write_log("行情订阅完毕")
    
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

        # 非记录时间则退出子进程
        if not trading and child_process is not None:
            if not child_process.is_alive():
                child_process = None
                print("子进程关闭成功")

        sleep(5)


if __name__ == "__main__":
    run_parent()
