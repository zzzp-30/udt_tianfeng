from datetime import datetime
from zoneinfo import ZoneInfo
import pandas as pd

CHINA_TZ = ZoneInfo("Asia/Shanghai") 

if __name__ == "__main__":
    # 定义日期字符串列表
    date_strings = [
        "2017-11-02 19:49:28-07:00",  # FIXME 如果在同一列中存在不同时区的日期，在使用 pd.to_datetime 转换时会出现问题
        "2017-11-27 07:32:22-08:00",
        "2017-12-27 17:01:15-08:00"
    ]

    # 将字符串转换为 datetime 对象
    date_times = [datetime.fromisoformat(date) for date in date_strings]

    # 创建 DataFrame
    df1 = pd.DataFrame({"datetime": date_times})
    
    df1["datetime"] = pd.to_datetime(df1["datetime"])
    df1["datetime"] = df1["datetime"].apply(
        lambda x: x.replace(year=datetime.now().year, month=datetime.now().month, day=datetime.now().day)
    )

    print(df1)