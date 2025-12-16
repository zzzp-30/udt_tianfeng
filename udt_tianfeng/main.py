# import multiprocessing
# import sys
# from concurrent.futures import ALL_COMPLETED, Future, wait
# from datetime import datetime, time
# from logging import DEBUG, INFO
# from pathlib import Path
# from time import sleep

# import akshare as ak
# import pandas as pd
# from vnpy.trader.engine import EventEngine, MainEngine, OmsEngine
# from vnpy.trader.object import AccountData, OrderData, PositionData, TradeData
# from vnpy.trader.setting import SETTINGS
# from vnpy.trader.utility import get_file_path
# from vnpy_ctp import CtpGateway
# from vnpy_simplestrategy import SimpleStrategyApp, StrategyEngine


# # 紫金天风 实盘
# ctp_setting = {
#     "用户名": "89110038",
#     "密码": "wjzb7777",
#     "经纪商代码": "0001",
#     "交易服务器": "101.230.80.85:41206",
#     "行情服务器": "101.230.80.85:41214",
#     "产品名称": "client_unboundream_v2",
#     "授权编码": "3J474CT8DL4EUW6F",
#     "产品信息": "unboundream"
# }


# # Chinese futures market trading period (day/night)
# DAY_START = time(8, 45)
# DAY_END = time(15, 0)

# NIGHT_START = time(20, 45)
# NIGHT_END = time(2, 45)


# def get_trading_days() -> pd.DataFrame:
#     """获取交易日数据，优先从本地文件读取，不存在则从AkShare获取"""
#     trading_days_file_path: Path = get_file_path("trading_days.csv")
    
#     # 检查文件是否存在且未过期
#     if trading_days_file_path.exists():
#         try:
#             file_mtime: float = trading_days_file_path.stat().st_mtime
#             current_time: float = datetime.now().timestamp()
            
#             # 如果文件未过期(6小时内)，直接读取
#             diff: float = current_time - file_mtime
#             if diff <= 6 * 60 * 60:
#                 df: pd.DataFrame = pd.read_csv(trading_days_file_path)
#                 return df
#         except Exception:
#             pass
    
#     # 文件不存在或已过期，从AkShare获取数据
#     try:
#         df = ak.tool_trade_date_hist_sina()
#         df.to_csv(trading_days_file_path, index=False)
#         return df
#     except Exception as e:
#         print(f"获取交易日数据失败: {e}")
#         return pd.DataFrame()

# def is_trading_day() -> bool:
#     """检查当前日期是否为交易日"""
#     try:
#         trading_days_df: pd.DataFrame = get_trading_days()
#         if trading_days_df.empty:
#             return True  # 如果无法获取交易日数据，默认返回True
        
#         current_date: str = datetime.now().strftime('%Y-%m-%d')
#         trading_dates: list[str] = trading_days_df['trade_date'].astype(str).tolist()
        
#         return current_date in trading_dates
#     except Exception as e:
#         print(f"检查交易日失败: {e}")
#         return True  # 出错时默认返回True

# def check_trading_period() -> bool:
#     """检查是否在交易时间段内且为交易日"""
#     # 首先检查是否为交易日
#     if not is_trading_day():
#         return False
    
#     current_time: time = datetime.now().time()
    
#     # 检查是否在交易时间段内
#     is_day_trading: bool = DAY_START <= current_time <= DAY_END
#     is_night_trading: bool = current_time >= NIGHT_START or current_time <= NIGHT_END
    
#     # return True  # 适配 7*24，把这行注释去掉的话，交易策略 7*24 小时都会保持运行
#     return is_day_trading or is_night_trading

# # TODO 如果父进程，子进程都在运行，支持 Ctrl-C 正常中断子进程
# #      如果父进程在运行，而子进程没在运行，按下 Ctrl-C 是终止父进程
# #      也就是说，如果父进程和子进程都在运行，要按两下 Ctrl-C 完全退出程序
# def run_child() -> None:
#     """
#     Running in the child process.
#     """
    
#     # --- 构建主引擎 ---
    
#     # 创建主引擎
#     main_engine: MainEngine = MainEngine()
    
#     # 获取订单引擎
#     oms_engine: OmsEngine = main_engine.get_engine("oms") # type: ignore
    
#     # 使用 CtpGateway
#     main_engine.add_gateway(CtpGateway)
#     main_gateway: CtpGateway = main_engine.get_gateway("CTP") # type: ignore
    
#     # --- 登录底层接口 CTP ---
    
#     # 尝试登录，如果失败则退出程序

#     # Note: CtpGateway#connect 会开始一个定时任务, 按固定间隔更新 AccountData 和 PositionData
#     main_gateway.connect(ctp_setting)
#     main_engine.write_log("连接CTP接口")

#     tries = 0
#     while not main_gateway.td_api.login_status and tries < 10:
#         tries += 1
#         main_engine.write_log("等待CTP接口登录")
#         sleep(1)
#     if not main_gateway.td_api.login_status:
#         main_engine.write_log("CTP接口登录失败")
#         sys.exit(0)
    
#     tries = 0
#     while not main_gateway.td_api.contract_inited and tries < 180:
#         tries += 1
#         main_engine.write_log("等待CTP合约信息初始化")
#         sleep(1)
#     if not main_gateway.td_api.contract_inited:
#         main_engine.write_log("CTP接口合约初始化失败")
#         sys.exit(0)
    
#     main_engine.write_log("主引擎创建成功")
    
#     # --- 打印投资者账户信息 ---
    
#     main_engine.write_log("订单信息(所有账号):")
#     [main_engine.write_log(f"vt_symbol={order.vt_symbol}, vt_orderid={order.vt_orderid}, ordersysid={order.ordersysid}, direction={order.direction}, offset={order.offset}, status={order.status} @{order.price}") for order in main_engine.get_all_orders()]
    
#     main_engine.write_log("成交信息(所有账号):")
#     [main_engine.write_log(f"vt_symbol={trade.vt_symbol}, vt_orderid={trade.vt_orderid}, direction={trade.direction}, offset={trade.offset} @{trade.price}") for trade in main_engine.get_all_trades()]
    
#     main_engine.write_log("持仓信息(所有账号):")
#     [main_engine.write_log(f"vt_symbol={pos.vt_symbol}, volume={pos.volume}, frozen={pos.frozen}, vt_positionid={pos.vt_positionid} @{pos.price}") for pos in main_engine.get_all_positions()]
    
#     main_engine.write_log("账户信息(所有):")
#     [main_engine.write_log(f"vt_accountid={acc.vt_accountid}, balance={acc.balance}, frozen={acc.frozen}, available={acc.available}") for acc in main_engine.get_all_accounts()]
    
#     # --- 启动 SimpleStrategy App ---
    
#     # 添加 App
#     strategy_engine: StrategyEngine = main_engine.add_app(SimpleStrategyApp) # type: ignore
#     # 初始化 Engine
#     strategy_engine.init_engine()
#     # 打印已加载的 strategy classes
#     strategy_engine.write_log(f"已加载策略: {strategy_engine.get_all_strategy_class_names()}")
#     # 调用 StrategyTemplate#on_init (异步执行，但同步等待)
#     wait(strategy_engine.init_all_strategies().values())
#     # 调用 StrategyTemplate#on_start
#     strategy_engine.start_all_strategies()
    
#     while True:
#         sleep(10)
#         trading = check_trading_period()
#         if not trading:
#             print("关闭子进程")
#             main_engine.close()
#             sys.exit(0)

# def run_parent() -> None:
#     """
#     Running in the parent process.
#     """
#     print("启动守护进程")

#     child_process = None

#     while True:
#         trading = check_trading_period()

#         # Start child process in trading period
#         if trading and child_process is None:
#             print("启动子进程")
#             child_process = multiprocessing.Process(target=run_child)
#             child_process.start()
#             print("子进程启动成功")

#         # Stop child process if not in trading period
#         if not trading and child_process is not None:
#             if not child_process.is_alive():
#                 child_process = None
#                 print("子进程关闭成功")

#         sleep(5)


# if __name__ == "__main__":
#     run_parent()



import multiprocessing
import sys
from concurrent.futures import ALL_COMPLETED, Future, wait
from datetime import datetime, time
from logging import DEBUG, INFO
from pathlib import Path
from time import sleep
import os
import json
import requests

#专门用来以防文件损坏读取备份
import json       # <--- 新增：用于解析 JSON 内容
import shutil     # <--- 新增：用于复制文件
from pathlib import Path  # <--- 新增：用于处理路径
# ... (其他原有的 import 保持不变)

import pandas as pd
import akshare as ak
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


accountid_to_name = {
    "CTP.1550550": "宏源价值",
    "CTP.89110038": "紫金天风"
}

def map_accountid_to_name(account_id: str) -> str|None:
    return accountid_to_name[account_id] if account_id in accountid_to_name.keys() else None

# Chinese futures market trading period (day/night)
DAY_START = time(8, 45)
DAY_END = time(17, 20)

NIGHT_START = time(20, 45)
NIGHT_END = time(2, 45)

# 记录各账号在当前跌破阈值条件下已发送提醒次数；当比值恢复至阈值以上时清零
_alert_counts: dict[str, int] = {}

def _compose_and_send_feishu_balance(title: str, curr_balance: float | None, y_balance: float | None) -> None:
    # 组装飞书信息，包含：当前市值权益、昨日收盘市值权益、差值与比值
    # 说明：
    # - 计算逻辑内置于本函数，调用方只需提供当前余额与昨日余额
    # - 若某个数值缺失，则对应文本以“?”占位，避免抛错影响其他账户
    cur_line = (
        f"市值权益：{curr_balance:.2f}" if curr_balance is not None else "当前市值权益：?"
    )
    y_line = (
        f"昨权益：{(float(y_balance) if y_balance is not None else float('nan')):.4f}"
        if y_balance is not None
        else "昨权益：?"
    )
    # 差值：当前 - 昨日；若任一缺失则设为 None
    diff = None
    try:
        if curr_balance is not None and y_balance is not None:
            diff = float(curr_balance) - float(y_balance)
    except Exception:
        diff = None
    # 比值：当前 / 昨日；若昨日为 0 或缺失则设为 None
    ratio = None
    try:
        if curr_balance is not None and y_balance is not None and float(y_balance) != 0.0:
            ratio = float(curr_balance) / float(y_balance)
    except Exception:
        ratio = None
    diff_text = f"{diff:+.2f}" if diff is not None else "?"
    ratio_text = f"{ratio:.4f}" if ratio is not None else "?"
    msg = f"{map_accountid_to_name(title) if map_accountid_to_name(title) is not None else title}\n\n{cur_line}\n\n{y_line}\n\n差值：{diff_text}    比值：{ratio_text}"
    _send_feishu_text(msg)

def _read_yesterday_balance_for_account(vt_accountid: str, folder: str) -> float | None:
    # 从统一的历史工作簿（balance.xlsx）中读取指定账号工作表的“昨日收盘”余额
    # 约定：
    # - 工作簿路径固定为 <folder>/balance.xlsx
    # - 每个账号拥有一个以其纯数字ID命名的工作表（去掉前缀CTP.）
    # - 工作表内包含列：vt_accountid, date, balance, frozen, available
    # 读取策略：
    # 1) 优先选取“昨日”的最后一条记录（按时间升序取最后一条）
    # 2) 若昨日无记录，则回退到“今天之前”的最后一条记录
    # 3) 若工作簿或工作表不存在/无法解析，则返回 None
    try:
        num_id = vt_accountid[4:] if vt_accountid.startswith("CTP.") else vt_accountid
        path = os.path.join(folder, "balance.xlsx")
        if not os.path.exists(path):
            return None
        # 读取指定工作表；若不存在则抛异常，由后续 except 处理
        df = pd.read_excel(path, sheet_name=str(num_id))
        if df.empty or 'date' not in df.columns:
            return None
        df['__dt'] = pd.to_datetime(df['date'], errors='coerce')
        df = df.dropna(subset=['__dt'])
        if df.empty:
            return None
        yesterday = datetime.now().date() - timedelta(days=1)
        # 首选：昨日记录的最后一条
        df_y = df[df['__dt'].dt.date == yesterday]
        if not df_y.empty:
            last_row = df_y.sort_values('__dt').iloc[-1]
            return float(last_row['balance']) if 'balance' in last_row and pd.notna(last_row['balance']) else None
        # 回退：今天之前的最后一条
        today = datetime.now().date()
        df_before = df[df['__dt'].dt.date < today]
        if not df_before.empty:
            last_row = df_before.sort_values('__dt').iloc[-1]
            return float(last_row['balance']) if 'balance' in last_row and pd.notna(last_row['balance']) else None
        return None
    except Exception:
        return None

    
import os
import json
import requests
from datetime import datetime, timedelta
import pandas as pd
from vnpy.trader.engine import MainEngine

def save_and_notify_balance_per_account(main_engine: MainEngine, realtime: bool =False, force_notify: bool =False) -> None:
    # 获取所有账户并统一写入单一工作簿（包含多个工作表），同时逐账号发送飞书消息
    # 工作簿选择：realtime=True -> 写入 <folder>/realtime_balance.xlsx；否则写入 <folder>/balance.xlsx
    accounts = main_engine.get_all_accounts()

    # 设置实时市值权益（balance）与昨日收盘时balance的比值 的提醒阈值。当实时获取到的该比值 < 阈值时，发送提醒。
    alert_threshold = 0.997
    # 若比值持续低于阈值，最多连续提醒三次
    alert_max = 3
    folder = r"C:\Users\Administrator\Desktop\历史市值权益"
    os.makedirs(folder, exist_ok=True)
    workbook_path = os.path.join(folder, "realtime_balance.xlsx" if realtime else "balance.xlsx")
    for acc in accounts:
        try:
            # 账号ID与工作表名（去掉CTP.前缀的纯数字ID）
            vt_accountid = str(acc.vt_accountid)
            sheet_name = vt_accountid[4:] if vt_accountid.startswith("CTP.") else vt_accountid
            ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            balance = float(acc.balance) if acc.balance is not None else None
            frozen = float(acc.frozen) if acc.frozen is not None else None
            available = float(acc.available) if acc.available is not None else None
            try:
                # 使用 openpyxl：若工作簿不存在则创建；若工作表不存在则创建并写入表头
                import openpyxl
                if not os.path.exists(workbook_path):
                    wb = openpyxl.Workbook()
                    # 默认工作表可保留为空，实际数据写入各账号命名的工作表
                    wb.save(workbook_path)
                wb = openpyxl.load_workbook(workbook_path)
                if sheet_name in wb.sheetnames:
                    ws = wb[sheet_name]
                else:
                    ws = wb.create_sheet(title=sheet_name)
                    ws.append(["vt_accountid", "date", "balance", "frozen", "available"])
                ws.append([vt_accountid, ts, balance, frozen, available])
                wb.save(workbook_path)
            except Exception:
                # 回退到 pandas：通过 ExcelWriter 写入/追加指定工作表（保留其他工作表内容）
                try:
                    # 读取已有工作簿的该工作表（若不存在则创建空 DataFrame）
                    if os.path.exists(workbook_path):
                        try:
                            df_existing = pd.read_excel(workbook_path, sheet_name=sheet_name)
                        except Exception:
                            df_existing = pd.DataFrame(columns=["vt_accountid", "date", "balance", "frozen", "available"])
                    else:
                        df_existing = pd.DataFrame(columns=["vt_accountid", "date", "balance", "frozen", "available"])
                    new_row = {
                        "vt_accountid": vt_accountid,
                        "date": ts,
                        "balance": balance,
                        "frozen": frozen,
                        "available": available,
                    }
                    # df_updated = pd.concat([df_existing, pd.DataFrame([new_row])], ignore_index=True)
                    df_updated = pd.concat([
                        df_existing.dropna(axis=1, how='all'), 
                        # df_existing,
                        pd.DataFrame([new_row]).dropna(axis=1, how='all')  # Drop all-NA columns
                    ], ignore_index=True)
                    # 使用 ExcelWriter 覆盖该工作表（保留其他工作表）
                    with pd.ExcelWriter(workbook_path, engine="openpyxl", mode="a", if_sheet_exists="replace") as writer:
                        df_updated.to_excel(writer, sheet_name=sheet_name, index=False)
                except Exception:
                    pass
            # 读取该账号昨日余额（从统一的 balance.xlsx 工作簿中对应工作表）并发送飞书消息
            y_balance = _read_yesterday_balance_for_account(vt_accountid, folder)
            if force_notify:
                _compose_and_send_feishu_balance(str(acc.vt_accountid), balance, y_balance)
            # 非强制提醒：当实时余额与昨日收盘余额的比值不高于阈值时，考虑发送提醒
            elif balance is not None and y_balance is not None and balance / y_balance <= alert_threshold:
                # 读取该账号在当前“跌破阈值”条件下已发送的提醒次数
                cnt = _alert_counts.get(vt_accountid, 0)
                # 未达到上限时，发送提醒并将计数 +1（最多连续发送三次）
                if cnt < alert_max:
                    _compose_and_send_feishu_balance(str(acc.vt_accountid), balance, y_balance)
                    _alert_counts[vt_accountid] = cnt + 1
            else:
                # 比值恢复至阈值以上时，重置计数为 0，下一次再次跌破时重新开始计数
                _alert_counts[vt_accountid] = 0
        except Exception:
            pass

def _send_feishu_text(text: str) -> None:
    # 通过环境变量 FEISHU_WEBHOOK_URL 发送文本到飞书机器人；未配置则静默跳过
    url = "https://open.feishu.cn/open-apis/bot/v2/hook/ff4ebf76-fddf-4d3b-8337-0b94b8e4ed89"
    if not url:
        return
    headers = {"Content-Type": "application/json", "charset": "utf-8"}
    payload = {"msg_type": "text", "content": {"text": text}}
    try:
        requests.post(url, headers=headers, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), timeout=10)
    except Exception:
        pass
    
    
def monitor_order_count(order_count: int, cancel_count: int) -> list:
    """监测总报单笔数，超过100则停止交易操作"""
    # 如果报单超过20笔，发出警告
    alert_message = []
    if order_count >= 100:
        message = f"🚨警告🚨：当前报单数已达 {order_count} 笔，超过阈值 100 笔"
        alert_message.append(message)
    
    # 如果撤单超过20笔，发出警告
    if cancel_count >= 20:
        message = f"🚨警告🚨：当前撤单数已达 {order_count} 笔，超过阈值 20 笔"
        alert_message.append(message)
        
    return alert_message



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

def _read_excel_b2(path: str) -> float | None:
    try:
        import openpyxl  # noqa: F401
        wb = openpyxl.load_workbook(path, data_only=True)
        ws = wb.active
        v = ws["B2"].value
        try:
            return float(v) if v is not None else None
        except Exception:
            return None
    except Exception:
        try:
            df = pd.read_excel(path)
            return float(df.iloc[1, 1])
        except Exception:
            return None


def check_and_restore_setting():
    """
    启动前检查：
    1. 如果配置文件不存在，完全从模板恢复。
    2. 如果配置文件存在，但其中的某些 BuyerStrategy (如 CFFEX_Strategy) 配置为空 {}，
       则仅从模板中复制该特定策略的 setting 内容，而不覆盖整个文件。
    3. 如果配置文件中原本就没有某个策略（说明用户故意删除了），则不进行恢复。
    """
    try:
        # 获取当前脚本所在目录 (即 udt_tianfeng/)
        current_dir = Path(__file__).parent
        
        # 定义文件路径
        setting_path = current_dir / "data" / "simple_strategy_setting.json"
        template_path = current_dir / "data" / "setting.json.template"

        # 1. 如果没有模板文件，报错提醒 (无法进行任何修复)
        if not template_path.exists():
            print(f"⚠️ [警告] 未找到模板文件: {template_path}")
            print("   请务必先复制一份正确的配置命名为 .template 后缀，否则无法自动修复！")
            return

        # 2. 如果配置文件完全不存在，直接从模板完整复制一份
        if not setting_path.exists():
            print("⚠️ 配置文件丢失，正在从模板完整创建...")
            shutil.copy(template_path, setting_path)
            return

        # 3. 读取当前配置和模板配置
        need_save = False
        try:
            with open(setting_path, 'r', encoding='utf-8') as f:
                current_data = json.load(f)
            
            with open(template_path, 'r', encoding='utf-8') as f:
                template_data = json.load(f)

            # 需要检查的特定策略列表 (只检查这些交易所策略，不检查 Combined 或 Macd)
            strategies_to_check = ["CFFEX_Strategy", "DCE_Strategy", "CZCE_Strategy", "SHFE_Strategy"]
            
            for strategy_name in strategies_to_check:
                # 规则 A：如果当前配置中根本没有这个策略（例如用户只跑 Combined），则跳过，不强行添加
                if strategy_name not in current_data:
                    continue

                # 规则 B：如果策略存在，检查其 setting 是否为空 (None 或 {})
                # 注意：这里假设正常情况下 setting 不应该为空
                current_setting = current_data[strategy_name].get("setting")
                
                if not current_setting: # 如果是 {} 或 None
                    print(f"🛑 检测到 [{strategy_name}] 存在但配置参数丢失 (为 {{}})，准备修复...")
                    
                    # 尝试从模板中获取对应的 setting
                    if strategy_name in template_data and template_data[strategy_name].get("setting"):
                        # 执行局部复制：只把模板里的 setting 塞给当前配置
                        current_data[strategy_name]["setting"] = template_data[strategy_name]["setting"]
                        need_save = True
                        print(f"   ✅ 已从模板恢复 [{strategy_name}] 的配置参数。")
                    else:
                        print(f"   ⚠️ 模板文件中也没有 [{strategy_name}] 的有效配置，无法修复。")

        except json.JSONDecodeError:
            print("🛑 配置文件格式严重损坏 (JSON解析失败)，正在执行完整恢复...")
            shutil.copy(template_path, setting_path)
            return
        except Exception as e:
            print(f"🛑 读取检查时发生错误: {e}，跳过修复步骤。")
            return

        # 4. 如果有修改，将数据写回文件
        if need_save:
            print(f"💾 正在保存修复后的配置文件...")
            with open(setting_path, 'w', encoding='utf-8') as f:
                # ensure_ascii=False 保证中文字符（如'紫金'）正常显示，indent=4 保持格式美观
                json.dump(current_data, f, ensure_ascii=False, indent=4)
            print("✅ 配置文件修复完成。")
        else:
            print("✅ 配置文件检查正常 (无缺失参数或无需修复)。")

    except Exception as e:
        print(f"❌ 检查配置过程发生未知错误: {e}")
        



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
    
    last_tick_ts: float | None = None
    last_daily_date: str | None = None
    last_morning_date: str | None = None
    
 
    while True:
        sleep(1)
        trading = check_trading_period()
        if not trading:
            print("关闭子进程")
            main_engine.close()
            sys.exit(0)
        now = datetime.now()
        now_ts = now.timestamp()
        if last_tick_ts is None or now_ts - last_tick_ts >= 120:
            save_and_notify_balance_per_account(main_engine, realtime=True)
            last_tick_ts = now_ts
        
        # 早上9:30强制提醒账户余额（市值权益）
        if now.hour == 9 and now.minute == 30:
            today = now.strftime('%Y-%m-%d')
            if last_morning_date != today:
                save_and_notify_balance_per_account(main_engine, realtime=True, force_notify=True)
                last_morning_date = today

        # 下午2:30提醒账户余额（市值权益），并保存至 balance.xlsx（balance.xlsx专门用来存每日14:55的账户余额）
        if now.hour == 14 and now.minute == 55:
            today = now.strftime('%Y-%m-%d')
            if last_daily_date != today:
                save_and_notify_balance_per_account(main_engine, realtime=False, force_notify=True)
                
                for strategy_name, strategy in strategy_engine.strategies.items():  # 遍历所有策略，统计报、撤单数
                    order_count = strategy.get_order_count # 该策略的报单数
                    cancel_count = strategy.get_cancel_count # 该策略的撤单数
                    
                    # 日志记录当前报单数和撤单数
                    strategy_engine.write_log(f"账户：{map_accountid_to_name('CTP.'+ ctp_setting['用户名'])}  策略：{strategy_name}  当前报单数: {order_count}, 撤单数: {cancel_count}")
                    
                    # 调用monitor_order_count函数检测当前报单数和撤单数
                    alert_message = monitor_order_count(order_count, cancel_count)
                    if alert_message:
                        for message in alert_message:
                            composed_message = f"账户：{map_accountid_to_name('CTP.'+ ctp_setting['用户名'])}  策略：{strategy_name}"+message
                            strategy_engine.write_log(composed_message)
                            _send_feishu_text(composed_message)
                
                last_daily_date = today
         
        
            

            

def run_parent() -> None:
    """
    Running in the parent process.
    """
    print("启动守护进程")

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
    
    # 【新增】程序启动第一件事：检查配置是否坏了，坏了就修
    check_and_restore_setting()
    
    run_parent()
    
