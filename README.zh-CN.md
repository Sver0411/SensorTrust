# SensorTrust

[English](README.md) | **简体中文**

SensorTrust 是一个轻量的嵌入式传感器健康检查器：它能检测数值卡死、超出物理量程、突发尖峰、持续漂移和数据缺失，并把这些信号转换成简单的健康状态与评分。

它只回答关于单个传感器通道的一个问题：

> 当前这个读数可信吗？

```text
传感器在产出数据
        !=
数据是可信的

SensorTrust 在节点上检查数据
        ->
故障标志位（bitmask） + health_score（0..100） + state（HEALTHY / DEGRADED / FAULT）
```

它**不做**的事：决定何时采样、以多高的频率采集、何时上传。那属于节点上采样/调度那一侧的职责。SensorTrust 只对它被喂进来的读数做判断。

## 它能做什么

输入：来自单个通道的一串采样。

```c
typedef struct {
    float    value;
    bool     valid;         /* false = 读取失败、超时、寄存器陈旧 ... */
    uint64_t timestamp_ms;
} sensor_sample_t;
```

输出：每个采样对应一个小结果。

```c
typedef struct {
    int                   health_score;  /* 0..100，严重程度 */
    sensor_health_state_t state;         /* HEALTHY / DEGRADED / FAULT */
    uint32_t              fault_flags;   /* FAULT_* 的位掩码 */
} sensor_health_result_t;
```

核心是可移植的 C11：无动态内存，不依赖 RTOS、Wi-Fi、MQTT、stdio，也不依赖任何硬件头文件。同一份 `core/sensor_trust.c` 既能跑在 ESP32 上，也能跑在 PC 上 —— 这正是下面主机测试和场景回放有意义的原因。

## 能检测的故障

v0.1 恰好检测五类，不多不少。

| 标志 | 位 | 检测什么 | 默认触发条件 |
| --- | --- | --- | --- |
| `FAULT_RANGE` | `1 << 0` | 物理上不可能的值 | `value < min_value` 或 `value > max_value` |
| `FAULT_STUCK` | `1 << 1` | 通道停止变化 | 最近 `stuck_window` 个采样的 `max - min < stuck_epsilon` |
| `FAULT_SPIKE` | `1 << 2` | 突然跳变 | `abs(value - 上一个有效值) > spike_threshold` |
| `FAULT_DRIFT` | `1 << 3` | 持续的单向趋势 | 连续 `drift_window` 个采样 `abs(slope) > drift_threshold`，slope 单位为**每秒**变化量 |
| `FAULT_MISSING` | `1 << 4` | 没有可用读数 | 连续 `missing_limit` 个无效采样 |

其中有三类需要特别说明：

- **STUCK** 并不是"两个读数相等"。稳定的环境是正常的，所以判据是**整个窗口的跨度**：只有当整个窗口的变化都小于 `stuck_epsilon` 时，才算卡死。用整个窗口的变化范围来判断，比只比较相邻采样要少很多误报，但它仍然**无法证明**"环境真的没有变化"和"传感器输出冻结"的区别 —— 见[局限性](#局限性)。
- **SPIKE** 会自我确认。偏离上一个值的跳变会被上报；如果下一个读数又回到跳变前的值附近，这次越界就被计为**已确认**的尖峰。而跳变后再也没回来的情况（真实的量级变化）只上报一次，随后被接受为新的量级。
- **DRIFT** 以真实时间为基准衡量。`drift_threshold` 是**每秒**变化量的斜率 —— 仓库里两套配置分别是 0.01 °C/s 和 0.05 %RH/s —— 它取自采样的时间戳，而不是采样序号。因此同一个物理趋势在 1 Hz 和 0.2 Hz 下会得到相同的判断，这在采样间隔可能被调度器在运行时改变时很重要。**不**以时间为基准的是确认环节：判断必须连续保持 `drift_window` 个采样，所以一段斜坡需要持续约两个窗口才会被标记。

## 工作原理

每个通道有各自的配置和各自的上下文。

```c
typedef struct {
    float min_value;
    float max_value;
    float stuck_epsilon;
    int   stuck_window;
    float spike_threshold;
    int   drift_window;
    float drift_threshold;
    int   missing_limit;
} sensor_trust_config_t;
```

检测逻辑里没有任何与具体传感器绑定的硬编码：上面的量程与阈值是描述一个物理通道的唯一位置，因此同一套核心既能覆盖温度通道（-40..85 °C），也能覆盖湿度通道（0..100 %），而算法一行都不用改。

实现层面的事实：

- 历史数据是一个固定环形缓冲区，保存最近 **有效** 的采样，容量 `SENSOR_TRUST_MAX_WINDOW`（64）个点，静态分配；每个点同时保存数值**和**它的 `timestamp_ms`，这才是 DRIFT 能衡量时间的原因；
- `sensor_trust_update()` 的时间复杂度为 `O(window)` 且不做内存分配，因此可以在每次读数时安全调用；
- STUCK 的窗口以**采样个数**计数，而不是秒："读数没有动"是关于读数本身的陈述，与墙上时钟无关；
- DRIFT 的斜率以**秒**计数：它是对采样时间戳做的最小二乘拟合，输出单位为每秒变化量；
- 无效采样只上报 `FAULT_MISSING`：它不携带数值，因此既不触发也不清除基于数值的检测器，并且会重置漂移计数器；
- 只要有一个浮点字段不是有限值，配置就会被直接拒绝。NaN 和 ±Inf 不是阈值，因此 `min_value`、`max_value`、`stuck_epsilon`、`spike_threshold`、`drift_threshold` 全都必须是真实的数；
- `sensor_trust_reset()` 只重启一个"已成功初始化且配置仍然合法"的通道。重置一个从未成功初始化的 context，会让它保持未初始化、不可用，而不会凭空造出一个可用的通道；
- `sensor_trust_format_flags()` 遵循 `snprintf` 契约：返回完整字符串所需的长度（哪怕只写进去一部分），并且**绝不**写出给定缓冲区之外；
- 各检测器相互独立，所以一个采样可以同时带多个标志（跳进不可能区间的读数同时是 `RANGE` 和 `SPIKE`）。

### 健康评分

评分从 100 开始，每命中一个故障减去一项罚分，并截断到 0..100。

| 故障 | 罚分 | 仅此一项故障时的评分 | 对应的状态 |
| --- | ---: | ---: | --- |
| `FAULT_RANGE` | 55 | 45 | FAULT |
| `FAULT_MISSING` | 55 | 45 | FAULT |
| `FAULT_STUCK` | 35 | 65 | DEGRADED |
| `FAULT_SPIKE` | 25 | 75 | DEGRADED |
| `FAULT_DRIFT` | 25 | 75 | DEGRADED |

这些权重是特意选取的，使得**单个已确认故障永远不会让通道停留在 `HEALTHY`**。它们可以在编译期覆盖（`SENSOR_TRUST_PENALTY_*`）。

状态映射：

| 评分 | 状态 |
| --- | --- |
| 80..100 | `HEALTHY` |
| 50..79 | `DEGRADED` |
| 0..49 | `FAULT` |

**健康评分是一个启发式的严重程度分数，不是标定过的概率。** `health_score = 72` 不表示"有 72 % 的概率健康"，而是表示"100 减去当前可见故障的罚分"。

## 示例

ESP32 演示程序向一个温度通道喂入 60 个采样（30 个合理读数、一次 36 °C 的跳变、随后一个不再变化的寄存器），每个采样打印一行，最后给出结论：

```text
SensorTrust v0.1 demo, one temperature channel, 1 Hz
  1    25.000  HEALTHY   score=100  flags=NONE
...
 29    25.000  HEALTHY   score=100  flags=NONE
 30    24.950  HEALTHY   score=100  flags=NONE
 31    61.000  DEGRADED  score= 75  flags=SPIKE
 32    24.950  DEGRADED  score= 75  flags=SPIKE
 33    25.000  HEALTHY   score=100  flags=NONE
...
 59    25.213  HEALTHY   score=100  flags=NONE
 60    25.213  DEGRADED  score= 65  flags=STUCK

sensor state: DEGRADED
score: 65
flags: STUCK
```

从这段输出里可以读出两件事：

- 第 55..59 个采样在数值已经冻结时，仍然显示 `HEALTHY / score=100`。只有当一个完整窗口都没有变化时，检测器才判定为 `STUCK` —— 这正是让一段短暂的平坦区间不被误报为故障的原因。而长期的恒定输出**会**被上报，并且是以*疑似*的形式上报：见[局限性](#局限性)。
- 故障类型在 `flags` 里。只看状态无法知道*哪里*出了问题，而且一个瞬时故障到最后一个采样时可能已经消失了。

该输出来自在 PC 上把 `firmware/main/main.c` 与 `core/sensor_trust.c` 一起编译 —— 该文件只使用 stdio 和核心，因此无需硬件即可验证这个演示。

## 场景结果

七个合成场景通过 `simulator/run.py` 回放进真实的 C 核心。`Detected`（实际检测）是整轮运行中出现过的标志的并集，`Health Score`（健康评分）是运行过程中达到的最低分。

| 场景 | 预期 | 实际检测 | 健康评分 |
| --- | --- | --- | ---: |
| healthy | NONE | NONE | 100 |
| healthy_dynamic | NONE | NONE | 100 |
| stuck | STUCK | STUCK | 65 |
| spike | SPIKE | SPIKE | 75 |
| drift | DRIFT | DRIFT | 75 |
| out_of_range | RANGE | RANGE, STUCK, SPIKE | 10 |
| missing_data | MISSING | MISSING | 45 |

机器可读版本（含每轮运行的结束状态）：`results/scenarios.csv`。

有两行值得单独说明：

- `healthy_dynamic` 是误报对照组。房间在缓慢升温，一阵气流带来一个会逐渐衰减掉的阶跃：数据是变化的，评分仍保持 100。它的存在是为了说明并非所有变化都是故障。
- `out_of_range` 报出三个标志而不是一个，这是诚实而不是"整齐"：湿度通道走进了不可能的数值并停在那里，因此它超出量程（`RANGE`），跳进该值的那个跳变是 `SPIKE`，而冻结住的垃圾值同时也是 `STUCK`。评分 10，状态 `FAULT`。

## ESP32 现状

```text
Board:                  not connected
Sensor:                 none (no BME280 driver yet, by design)
Real sensor validation: Not measured yet
```

`firmware/` 是一个 ESP-IDF 工程，但它只用于演示这套核心：它构造合成采样、喂给 SensorTrust、打印结果。它不与真实传感器通信，也不包含任何传感器驱动 —— 核心接收的是通用的 `sensor_sample_t` 值，因此以后可以在不改动检测逻辑的前提下把驱动加上。

构建状态：

```text
ESP-IDF:         v5.4.4, target esp32s3
Command:         idf.py set-target esp32s3 && idf.py build
Result:          Project build complete, 0 compiler warnings
App binary:      192,256 bytes (82% of the 1 MB app partition free)
Core in firmware: core/sensor_trust.c is compiled into the main component
```

标志位格式化改用有界拷贝、不再调用 `snprintf`，因此核心不会链接 printf 家族中的任何函数：镜像里不存在 `snprintf`、`vsnprintf`、`sprintf` 符号，app 二进制比改动前小了 13,120 字节。（演示程序自己仍然使用 `printf`；**核心**不用，这才是它被嵌进别人固件时真正重要的一点。）

因此固件和主机测试跑的是同一份 `core/sensor_trust.c`，而不是它的两份副本。尚未发生的事情是：在真实硬件上运行它 —— 上面演示的打印输出来自在主机上编译同一份 `firmware/main/main.c`，因为该文件只用了 stdio（而且没有接任何传感器）。

## 运行方式

```bash
# 1. 生成合成场景 -> results/dataset/*.csv
python simulator/generate.py

# 2. 通过真实的 C 核心回放 -> results/scenarios.csv 以及上面的表格
python simulator/run.py

# 3. 测试（C 核心 + 模拟器）
python -m pytest tests/ -v

# 4. 只跑 C 核心测试，不需要 Python。核心必须在严格选项下保持零警告，
#    所以带 -Werror 的这条才是真正的构建命令。
cc -std=c11 -Wall -Wextra -Werror core/sensor_trust.c tests/test_core.c -o test_core -lm
./test_core

# 5. ESP-IDF 演示（需要已安装 ESP-IDF）
cd firmware
idf.py set-target esp32s3
idf.py build
```

开发机上的当前状态：

```text
C 核心测试:  21 passed, 0 failed, 0 compiler warnings
            (-std=c11 -Wall -Wextra -Werror)
Python 测试:  9 passed
CI:          .github/workflows/tests.yml，单个 job，只跑 pytest
```

CI 刻意只有一个 job：在 Ubuntu 上运行 `python -m pytest tests/ -v`。其中
`test_c_host_tests_pass` 会在同一次运行里编译并执行 `tests/test_core.c`，
所以一步就覆盖了 C 与 Python 两侧。没有构建矩阵、没有 Docker、没有 ESP-IDF
CI，也没有覆盖率上传。

`simulator/run.py` 会把 `core/sensor_trust.c` 与 `simulator/replay_main.c` 一起编译成一个小型主机程序，并把数据集喂进去，因此 Python 侧从不重复实现检测逻辑。Python 代码只负责生成数据流，并汇总 C 核心报出的结果。

## 项目结构

```text
SensorTrust/
├── core/
│   ├── sensor_trust.c        # 检测逻辑，可移植 C11
│   └── sensor_trust.h        # 类型、配置、API
├── simulator/
│   ├── scenarios.py          # 7 条合成数据流 + 通道配置
│   ├── generate.py           # 写出 results/dataset/*.csv
│   ├── replay_main.c         # 主机驱动：把数据集喂给核心
│   └── run.py                # 构建驱动、回放、写出报告
├── firmware/
│   ├── CMakeLists.txt
│   ├── sdkconfig.defaults
│   └── main/
│       ├── CMakeLists.txt
│       └── main.c            # ESP32 演示（合成采样，打印结论）
├── results/
│   ├── dataset/*.csv         # 生成的场景数据流（自描述）
│   └── scenarios.csv         # 生成的汇总
├── tests/
│   ├── test_core.c           # 21 个 C 核心测试
│   └── test_simulator.py     # 9 个模拟器流水线 Python 测试
├── .github/workflows/tests.yml  # 单个 CI job：pytest（其中也会跑 C 测试）
├── README.md
├── README.zh-CN.md
└── LICENSE
```

## 局限性

- **一个上下文只管一个通道，不做融合。** SensorTrust 不在通道之间做比较，也不做跨节点投票。单个通道无法区分真实的环境变化和传感器故障，这也是漂移只被报为*疑似*的原因。
- **只使用合成数据。** 本 README 中所有数字都来自生成的数据流。这里还没有任何东西跑在真实的故障传感器上。
- **检测到故障是怀疑，不是判决。** 冻结的读数可能来自真的恒定的环境；不可能的数值可能是接线问题而不是传感器损坏。SensorTrust 报告的是数据质量上的可疑情况，由调用方决定如何处理该读数。
- **STUCK 仍然可能把"环境真的没有变化"和"传感器输出冻结"混为一谈。** 使用完整窗口的变化范围可以减少把正常稳定环境误判为 STUCK 的情况，但无法彻底消除：分辨率较粗的传感器处在一个确实不变的环境中，看起来依然是冻结的，而单个数值通道无法证明自己看到的是哪一种。
- **DRIFT 是疑似漂移，不是传感器故障的证据。** 一段真实且持续的环境变化，在单个通道上会产生完全相同的斜率。多窗口确认过滤掉了短暂瞬态，但并不会把一个真实趋势变成故障。
- **时钟异常时退化为"不上报"，而不是新增一种故障。** 如果一个窗口内的时间戳并非全部向前推进，这个窗口就完全不会给出 DRIFT 判断。RANGE、SPIKE、STUCK 不受影响，因为它们都不使用时间。v0.1 只有五种故障类型，不会为时间戳新增一种。
- **不做间隔/超时检测。** 这套 API 是推送式的：它只能看到被喂进来的采样。一个彻底不再调用 `sensor_trust_update()` 的设备根本不会产生采样，因此这种情况在核心内部是看不到的，必须由调用方看守（那是调度器的职责，不是健康检查器的）。
- **STUCK 的窗口仍然以采样个数计。** 采样间隔变慢或变快时，同样的 `stuck_window` 代表不同的时长；只有 DRIFT 的斜率是以时间为基准的，所以只有 DRIFT 阈值能原封不动地跨过采样间隔的变化。
- **阈值是手工设定的默认值，未经调优。** 它们没有做联合优化，但集中记录在一处（模拟器见 `scenarios.py`，通用默认见 `sensor_trust_default_config()`），便于审阅和修改，而不必在代码里到处找。
- **v0.1 未实现：** 真实传感器验证、多传感器融合、ML 故障检测、跨节点投票、云端诊断。

v0.1 在这五个检测器上冻结。

## 许可证

MIT —— 见 [LICENSE](LICENSE)。
