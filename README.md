# Lattice

**一键把 Windows 窗口摆成预设布局 —— 只靠 ctypes，零第三方依赖。**

> Lattice = 格构 / 网格骨架。每个窗口各归其格，一套格构管所有布局。

Windows 上的分屏工具不少，但大多只管「把窗口塞进矩形」，不管**看起来**是否对齐。Chrome 的窗口矩形外有 7px 隐形拖拽边框，MuMu 模拟器有 3px 自己的深色边，Electron 应用（如 WorkBuddy）是 0px —— 同样按矩形排布，肉眼看到的缝隙可能差 8px 以上。

Lattice 的做法是：排完位后**逐像素实测每个窗口真正画内容的边界**，再按实测值补偿一次。所以无论什么程序，相邻窗口之间看得见的缝隙都等于配置里的 `gap`。

- 纯 Python 标准库，`lattice.py` 单文件，不需要 `pip install` 任何东西
- 三套预设布局 + 托盘图标 + 系统级独占热键
- 任何程序都能进格子（`process` 支持 `|*` 兜底通配）
- 排完自动整理层叠顺序，消除 DWM 阴影外溢对缝隙的干扰

## 下载

[**Releases**](https://github.com/Nuoili/lattice/releases/latest) 里有打包好的 `Lattice-v1.0.0-win64.zip` —— 解压后双击 `Lattice/Lattice.exe` 即可，**不需要安装 Python**。

> `_internal` 是运行时依赖，**整个 `Lattice` 目录要一起保留**，只拷 exe 单独跑不起来。

## 快速开始

```text
双击 Lattice.exe
    → 立刻按方案 1 排好，底部弹提示告诉排了几个窗口
    → 同时自动拉起常驻进程（托盘出现图标）
```

之后：

| 操作 | 效果 |
| --- | --- |
| `Ctrl+Alt+Shift+1 / 2 / 3` | 切换方案 1 / 2 / 3 |
| `Ctrl+Alt+Shift+Q` | 退出常驻 |
| 托盘图标左键单击 | 直接应用方案 1 |
| 托盘图标右键 | 切换方案 / 打开配置文件夹 / 退出 |

热键用 `RegisterHotKey` **独占注册**，注册成功后其他软件收不到这组键。三组都被占用时会自动降级（`Ctrl+Alt+Shift+数字` → `Win+Alt+数字` → `Ctrl+Alt+数字`），实际生效的键位写在 `lattice.log` 里。

## 卸载

双击 `卸载器.bat`，确认后它会做完三件事：

1. 结束运行中的 `Lattice.exe`
2. 删掉开机自启项（`启动文件夹\Lattice-startup.bat`）
3. 把整个程序目录送进回收站 —— **可从回收站还原**，不是硬删

Lattice 是绿色免安装程序，不写注册表、不装服务，所以「卸载」= 删目录 + 清自启项。配置文件 `lattice.json` 就在程序目录里，想留着自己的布局方案，卸载前先拷出来。

## 目录结构

```text
lattice/
├── Lattice.exe        # 双击这个
├── _internal/         # PyInstaller 运行时依赖，必须跟 exe 待在一起
├── 卸载器.bat          # 卸载：结束进程 + 删开机自启 + 整个目录送回收站
├── lattice.py         # 全部源码（单文件）
├── lattice.json       # 你的布局方案
├── lattice.ico        # 图标
└── lattice.log        # 运行日志（出问题看这个）
```

从源码运行：

```bash
python lattice.py            # 应用方案 1 + 拉起常驻
python lattice.py -p 2       # 应用方案 2
python lattice.py --once     # 只排一次，不拉起常驻
python lattice.py --list     # 列出可见窗口 + 所有方案（含每格期望的程序）
python lattice.py --dry -p 2 # 预演：只报分配结果，不移动窗口
python lattice.py --capture  # 打印当前布局的百分比
```

更细的排障说明、热键降级细节、日志字段含义见 [`使用手册.txt`](使用手册.txt)。

## 配置你自己的布局

`lattice.json` 里每个区块：

```json
{
  "name": "工作三分屏（大左 + 右上 + 右下）",
  "hotkey": "1",
  "monitor": "primary",
  "zones": [
    { "rect": [0, 0, 62.3, 100],    "process": "chrome|msedge|firefox|*" },
    { "rect": [62.3, 0, 37.7, 63],  "process": "WorkBuddy|*" },
    { "rect": [62.3, 63, 37.7, 37], "process": "Weixin|WeChat|*" }
  ],
  "margin": 2,
  "gap": 2
}
```

| 字段 | 说明 |
| --- | --- |
| `rect` | `[左边%, 上边%, 宽%, 高%]`，相对**工作区**（自动避开任务栏 / 顶栏 / dock） |
| `process` | exe 名正则，多个用竖线分隔；末尾再加 `*` 表示「列出的程序都没开时用其他窗口顶上」 |
| `title` | 可选，窗口标题正则，用于区分同一程序的多个窗口 |
| `margin` | 窗口与屏幕边缘的距离，px |
| `gap` | 相邻窗口之间的缝隙，px（两侧各让一半，缝宽正好 = `gap`） |

**不想手算百分比**：先把窗口手动拖成想要的样子，双击 `Lattice-capture.bat`，当前布局会作为新方案追加进 `lattice.json`。

改完存盘即生效，不用重新打包。

### `process` 的挑选顺序

1. 写明了程序名的格子先抢 —— 否则「任意窗口」的格子会把指定窗口占掉
2. 剩下的格子从剩余窗口里补，按 z 序（最近用过的优先）—— 所以主位（最大那块）往往落在你当前正在用的程序上

## 关于缝隙：为什么是「实心内容之间」的距离

`gap = 2` 指的是**两块看得见的实心内容之间**的距离，不是「窗口矩形之间」的距离。每个程序自带的边缘宽度都不一样：

| 程序 | 边缘宽度 | 成因 |
| --- | --- | --- |
| Chrome | 7px | 系统隐形拖拽边框 + DWM 阴影带（画在矩形内侧） |
| MuMu 模拟器 | 3px | 自己的深色边框 + 阴影带 |
| WorkBuddy | 0px | Electron 无边框窗口，矩形边就是内容边 |

照着「窗口矩形」排，Chrome 那一侧会凭空多出七八像素的缝，视觉上就「不一样宽」。

所以 Lattice 每次排位分三遍：

1. 先不补偿，把窗口矩形直接摆到目标位置
2. 抓一次屏，用 `PrintWindow(hwnd, hdc, PW_RENDERFULLCONTENT)` 拿到每个窗口**自己渲染**的像素，与屏幕上**真实**像素逐列 / 逐行比对，定位实心内容边
3. 按实测到的可见边再摆一次，然后整理层叠顺序

这套量法对任何程序都成立，**不需要维护一张按 exe 名分的补偿表**。

### 两个容易量错的地方

**阴影带 ≠ 内容。** DWM 阴影画在窗口矩形内侧，和「应用自己画的实心黑」在像素上长得一样。分不开的后果：MuMu 被拉成细高条时画面两侧有黑边，那块黑会被误判成「透明边」白白让位，窗口矩形压到邻居身上，缝隙就变 0。

判据：

- 自渲染黑 **且** 屏幕也黑 → 实心内容，算
- 自渲染黑 **但** 屏幕非黑 → 透底（DWM 阴影 / 壁纸），不算

同时以 DWM 的 `DWMWA_EXTENDED_FRAME_BOUNDS` 为**扫描下界**，那条界之外的像素一律不算内容（阴影只会出现在那里），实测值再取 `max(实测, DWM)` 兜底。

**窗口不会自己乱动。** 实测 Chrome / MuMu / WorkBuddy 摆好后 3 秒内 rect 不变，所以不需要反复校正循环。看到位置跑偏基本是两处代码同时排位（例如连按热键）。

### 层叠顺序

排完会顺手整理 z 序：靠左的窗口沉到最底、同一列里靠下的也沉下去。DWM 的阴影会往窗口外面溢出（Chrome 5px、MuMu 12px），压在右邻身上就会把那条缝撑宽；让压在上层的那一侧盖住它，两侧的缝宽才是同一个数。

> Chrome 收到移动消息后会异步跳到前面抢焦点，所以摆位后要再压两次 z 序。

### 实测结果

三窗口（Chrome / WorkBuddy / MuMu），`gap = 2`，逐像素核对接缝原始 RGB：

| 方案 | 接缝 | 实测 |
| --- | --- | --- |
| 1 | 水平 Chrome \| WorkBuddy | 2 px |
| 1 | 水平 Chrome \| MuMu | 2 px |
| 1 | 垂直 WorkBuddy / MuMu | 2 px |
| 3 | 水平 Chrome \| WorkBuddy | 2 px |
| 3 | 水平 WorkBuddy \| MuMu | 2 px |

## 打包

需要 Python 3.8+ 和 PyInstaller。源码零第三方依赖，PyInstaller 只是为了不装 Python 也能跑。

```bat
python -m PyInstaller --noconfirm --clean --onedir ^
  --name Lattice ^
  --icon "<abs>\lattice.ico" ^
  --manifest "<abs>\lattice.manifest" ^
  --distpath "<abs>\_build\dist" --workpath "<abs>\_build\work" --specpath "<abs>\_build" ^
  "<abs>\lattice.py"
```

两个注意点：

- `--icon` / `--manifest` **必须写绝对路径**，否则会「打包成功」但生成残缺 exe
- 用 `--onedir` 而不是 `--onefile`：单文件模式每次运行都要解压到 `%TEMP%\_MEIxxxx`，退出时删不掉就会弹 `Failed to remove temporary directory`。目录模式不解压、启动更快，代价是 exe 不能单独拷走

`lattice.manifest` 只做两件事：声明 PerMonitorV2 DPI 感知（高分屏下排版不糊）、声明支持 Windows 10/11。

## 已知限制

- **仅 Windows**：用到 DWM、`RegisterHotKey`、`PrintWindow` 等 Win32 API，没有做跨平台抽象
- **没有 `--windowed`**：exe 是控制台子系统程序。双击时 Windows 会给它开一个控制台，于是启动时会判断：控制台是本次新建的（双击 / 启动文件夹）→ 自己 `SW_HIDE` 藏起来；控制台是复用的（在 cmd 里跑）→ 保持原样。想在双击时故意留着窗口看输出：`Lattice.exe --console`
- **托盘图标被折叠进 `^` 溢出区**时，拖到任务栏上就固定住了（Windows 的行为，不是程序问题）

## 许可

[MIT](LICENSE)
