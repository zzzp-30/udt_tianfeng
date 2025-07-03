Author: Zheyin Zeng  
WeChat: 17775787390

## 项目说明 udt_tianfeng

> udt = Unboundream Trader 😄

该项目是紫金天风实盘账户的交易程序，基于 vnpy 开源框架实现。

### 项目复用

对于宏源期货、国新国证这两个期货公司，可以复用本程序。其他期货公司不保证！

如何复用？

1. 将本项目整个文件夹复制粘贴，重命名为 `udt_<期货公司拼音>`
2. 删除里面的 `.mypy_cache/`, `.venv/` 文件夹，删除 `uv.lock` 文件，因为这会影响新程序运行
3. 用 VSCode 全文搜索整个项目，将 `tianfeng` 字段替换成对应的期货公司拼音，如 `hongyuan`
4. 用 VSCode 全文搜索整个项目，将 `紫金天风` 字段替换成对应的期货公司名字，如 `宏源期货`
5. 修改 `main.py` 文件中的账户信息
6. 更新 `run.bat` 文件中的路径信息

### 依赖管理

##### 虚拟环境

该项目使用虚拟环境来运行程序。虚拟环境默认位于本项目根目录下的 `.venv` 文件夹，以确保程序的 Python 环境稳定，避免被操作系统的全局 Python 环境影响。截止至 2025/7/2，虚拟环境的实际作用就是让新系统能够和无限易一起运行，否则无限易和该系统所使用的 Python 环境会发生冲突。

> Note: 即使不再使用无限易了，也应该继续使用虚拟环境来运行 Python 程序，以保证运行环境的稳定！

> Note: 这下就不得不吐槽，无限易作为一个商业软件，内置的 Python 引擎居然用的是操作系统的全局 Python 环境😭

##### `pyproject.toml`

该项目（包括其他基于 vnpy 的项目）均采用 `pyproject.toml` 方式管理项目依赖，不是 `requirements.txt`，也不是在命令行输 `pip`。如果要安装依赖、修改依赖，你需要在 `pyproject.toml` 更新依赖配置，然后在项目根目录下运行命令行 `uv sync --reinstall` 来更新项目的虚拟环境。

### 文件说明

> Note: `/` 结尾的是文件夹

- `.mypy_cache/`: mypy 的缓存文件夹，通常不需要关注
- `.venv/`: Python 虚拟环境文件夹，通常不需要关注
- `.vscode/`: VSCode 的配置文件夹，主要用里面的 launch.json 来改变调试模式的设置
- `notebooks/`: 笔记本，用于平时的测试和记录
- `udt_tianfeng/`: 程序源代码文件夹
  - `data/`: 数据文件夹，存放程序的配置文件和数据文件
  - `strategies/`: 策略文件夹，存放策略代码
  - `__init__.py`: 不能删除，唯一的作用就是让 `uv` 可以找到项目代码
  - `main.py`: 程序入口文件，启动程序的主文件
- `pyproject.toml`: 项目依赖
- `README.md`: 项目说明
- `run.bat`: 启动脚本，会使用当前的 Python 虚拟环境执行 `main.py` 文件
- `uv.lock`: uv 的锁文件，记录了当前虚拟环境的状态，通常不需要关注

### 使用方式

将从以下几个方面来说明使用方式：
- a. 编写策略
- b. 配置策略
- c. 启动程序

对于日常使用，即策略已编写好，只需要启动的情况，关注 *启动程序* 即可。

对于开发调试，即需要反复修改代码和启动程序，需要关注全部三个方面。

#### a. 编写策略

*编写策略* 指的是要编写新的策略代码或修改现有策略的代码，也就是要创建或者修改 `udt_tianfeng/strategies/` 下的 .py 文件。

所有策略的代码 (.py) 都必须放在 `udt_tianfeng/strategies` 里面，不支持子文件夹。

程序启动时会自动读取这个目录下的所有 .py 文件并且导入 (`import`) 其中的所有内容。

一个大致的从编写策略到运行策略的流程如下：

1. 将新的策略文件 (.py) 放在 `udt_tianfeng/strategies` 目录下
2. 参考下面的示例修改 `udt_tianfeng/data/simple_strategy_setting.json`
```json
{
    // 注意这是一个 JSON 文件中的映射 (Map)
    // 映射由若干个键值对 (Key-Value Pair) 构成
    //
    // 每一个键值对都是一个将要运行的策略实例
    //
    // 键是字符串，是策略的实例的名字；
    // 值是JSON对象，是策略的实例的参数；
    //
    // JSON教程：https://www.runoob.com/json/json-tutorial.html

    // 开始定义第一个键值对, 也是定义的第一个策略实例
    // 这里的键值对的 *键* 是一个字符串，即 "combined"
    // 这里的键值对的 *值* 是一个JSON对象，即 {...}
    "combined": {

        // 数据类型：字符串
        // 这是实例所使用的 class 的名字，大小写敏感，确保该 class 出现在 udt_tianfeng/strategies 中的某个 .py 文件里
        "class_name": "Combined",

        // 数据类型：字符串列表
        // 这是实例所订阅的合约代码，格式是 `代码.交易所`，例如 `p2508-C-10000.DCE`
        // 但需要注意!!! 目前的 Combined 策略和 MacdStrategy 策略都没有实际使用这个参数
        "vt_symbols": [],

        // 数据类型：JSON对象
        // 策略实例的参数，根据不同的 class_name，这里会有所不同，最终取决于策略 class 的具体实现
        "setting": {}
    }, // 注意 Map 的每个键值对之间用逗号分隔

    // 开始定义第二个键值对, 也是定义的第二个策略实例
    "macd": {
        "class_name": "Macd",
        "vt_symbols": [],
        "setting": {}
    },

    // 可以定义更多策略实例
    "test": {
        "class_name": "Test",
        "vt_symbols": [],
        "setting": {}
    } // 注意最后一个键值对后面不需要逗号
}
```

#### b. 配置策略

*配置策略* 指的是修改 `udt/data/simple_strategy_setting.json` 这个文件里的内容。

该配置文件里的内容在程序重启后生效。

#### c. 启动程序

如果你不在 VSCode 编辑器中，仅仅是运行策略，双击运行项目根目录下的 `run.bat` 即可启动交易程序。

如果你已经在 VSCode 编辑器中，并且需要兼具开发调试，在 VSCode 内运行 `main.py` 即可启动交易程序。

整个程序 (`main.py`) 内部的启动流程大致如下：

1. 根据 `udt_tianfeng/main.py` 文件中的CTP验证登录信息 (`ctp_setting`)，登录相应柜台的 CTP 接口
2. 加载 `udt_tianfeng/strategies/` 中的所有 .py 文件，即加载策略的 class 代码（这一步类似无限易当中的“加载策略”）
3. 根据 `udt_tianfeng/data/simple_strategy_setting.json` 中的配置，创建策略的实例（这一步类似无限易当中的“创建实例”）
4. 开始运行先前加载好的所有策略的实例（这一步类似无限易当中的“运行实例”）