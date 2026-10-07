# MIDI to MSCZ — 项目交接文档

更新日期：2026-09-11  
当前版本：`0.4.2`  
仓库：<https://github.com/Jeatricy/midi_to_mscz>  
交接基线提交：`ee142cf`（`Recognize layered barline arpeggios`）

## 1. 项目目标与硬约束

本项目把 Song Master Pro 5 等扒谱工具生成的一个或多个 MIDI stem 整理为可继续编辑的 MuseScore `.mscz` 钢琴谱。输入通常存在攻击偏拍、速度抖动、错误调号、踏板长尾和特殊节奏误量化；程序先建立统一节拍时间线、识别特殊节奏语义，再生成 MusicXML，并调用 MuseScore 转为 MSCZ。

产品硬约束：

- 输出固定为一个 Piano part、两个谱表（高音与低音）。
- 每个输入 MIDI 代表一个独立声部。
- 同一 MIDI 内的重叠视为踏板尾音；前一个攻击裁到下一个攻击，不当作复调。
- 同一谱表最多分配 4 个 MIDI，对应 MuseScore 最多 4 个独立 voice。
- 多个 MIDI 分配到同一谱表时不能跨来源合并或互相裁短。
- 相邻攻击之间不写休止；短音也延长到同一 MIDI 的下一次攻击。
- 不确定位置写入转换报告，让用户在 MuseScore 中人工复核。

## 2. 用户入口

### WebUI（主要入口）

Windows 双击 `run_webui.pyw`。它启动仅监听 `127.0.0.1` 的本地服务并打开浏览器。WebUI 支持多 MIDI 分谱表、移调、力度过滤、时间对齐、自动/手动 BPM/拍号/调号/弱起、特殊节奏开关，以及原始音频复核。

结果使用浏览器 File System Access API 直接写到用户选择的目录。文件写入、关闭并校验 size/SHA-256 后，前端才通知后端删除任务输入、输出和日志。该能力依赖最新版 Edge 或 Chrome。

### 命令行

安装后运行 `midi-to-mscz --help`。典型调用：

```powershell
midi-to-mscz `
  --treble "Vocals.midi" `
  --bass "Piano.midi" `
  --bpm auto `
  --time-signature auto `
  --key-signature auto `
  -o "result.mscz"
```

完整参数和界面说明见 `README.md`。

## 3. 安装与开发机状态

支持 Python `3.10` 或 `3.11`：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

启用音频复核：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[audio]"
```

`audio` extra 包含 `basic-pitch==0.4.0` 和 `demucs==4.1.0`。NVIDIA GPU 可先安装 CUDA PyTorch：

```powershell
.\.venv\Scripts\python.exe -m pip install torch==2.7.0 --index-url https://download.pytorch.org/whl/cu128
.\.venv\Scripts\python.exe -m pip install -e ".[audio]"
```

开发机已验证 Python 3.11.4、MuseScore 4、Demucs 4.1.0、PyTorch 2.7.0 + CUDA 12.8、RTX 4080 和 Basic Pitch 0.4.0。HTDemucs 权重缓存在当前用户目录，不在仓库中；新机器首次使用会下载。

MuseScore 常见路径为 `C:\Program Files\MuseScore 4\bin\MuseScore4.exe`。不要手写 `.mscz`；先生成 MusicXML，再由 MuseScore CLI 导入。

## 4. 代码结构

| 文件 | 职责 |
| --- | --- |
| `models.py` | 设置、中间事件、谱面、元数据、报告和音频数据类 |
| `midi_io.py` | MIDI 解析、note 配对、移调、力度过滤、手动偏移 |
| `metadata.py` | 自动 BPM、拍号、调号、弱起及元数据时间平移 |
| `quantize.py` | 延迟、裁剪、特殊节奏、量化、单声部和 voice 分配 |
| `musicxml.py` | 双谱表 MusicXML；休止、tie、tuplet、grace、arpeggiate |
| `musescore.py` | 调用 MuseScore 并校验 MSCZ |
| `audio_verify.py` | 音频对齐、DSP、Basic Pitch、Demucs 和可信度 |
| `pipeline.py` | 转换编排与临时目录生命周期 |
| `webui.py` | 本地服务、上传、任务、结果确认与清理 |
| `cli.py` / `__main__.py` | CLI 和模块入口 |

前端在 `src/midi_to_mscz/web/`。根目录 `run_webui.pyw` 是 Windows 双击启动器；不用 `.bat`，以降低安全软件误拦截。

## 5. 转换流水线

```text
InputSpec[] -> midi_io -> optional audio_verify -> metadata
            -> quantize -> MusicXML -> MuseScore -> report + MSCZ
```

顺序不能随意交换：读取并保留 `source_index`；音频复核在量化前；估计来源延迟；只裁共同整小节前导；建立含弱起/变拍的小节线；先识别特殊语义；强制每个 `(source, staff)` 为连续单声部；不同 MIDI 固定到不同 voice；最后拆时值和 tie。

## 6. 核心算法与不变量

### 一份 MIDI 就是一条声部

- 同一攻击附近的多个音组成和弦，和弦内音符取得共同结束时间。
- 前一事件结束在下一逻辑攻击；note-off 更晚时记为踏板尾音裁剪。
- 前一音过短时也延长到下一攻击，活动区间内部不产生休止。
- 倚音时值为 0，不推进时间游标。
- 连音最后成员若持续出 tuplet 边界，会生成组外 continuation 并用 tie 连接。
- 不同 MIDI 即使同音高同拍也必须独立，不能去重。

### 特殊节奏优先

当前覆盖倚音/快速装饰、普通与分层琶音、区域 Swing、常见 3/5/6/7/9 及部分 10–13 比例连音、二进制网格和 `free` 保留。不要把所有偏拍音直接四舍五入；未知快速攻击吸到同一格会静默丢失信息。

### 小节线琶音

`0.4.2` 支持 `单音 -> 单音或和弦层 -> 落地和弦` 的踏板分层滚奏。检测器只在真实小节线附近启用，并要求同一来源/谱表、三个隔离攻击层、保守时间跨度、严格上/下行、至少 4 音且一层为和弦、总音域至少一八度、前层延音跨过后层和小节线、前后无过近攻击、末层紧邻强拍。通过后整组锚到下一小节开头并吸收终端和弦。该规则在通用琶音前执行，避免 strict 模式截取不完整子集。

真实样例验收：第 49 小节 `[61,65,68,82]`、第 51 小节 `[60,63,68,72,82]`、第 53 小节 `[61,65,68,73,82]` 均为 up arpeggio；第 50、52 小节无错误碎音。normal/aggressive 另保留第 65 小节两音琶音，strict 按设计拒绝含糊的两音琶音。

### 小节线与拍号

所有判断小节的逻辑必须共用 meter timeline，不能用 `beat % 4`。弱起和临时变拍会改变边界。琶音锚定、连音跨小节保护、复核小节编号与 MusicXML measure specs 必须一致。非法弱起（长度大于等于首小节容量）按无弱起处理。

### 元数据

- BPM 自动模式读取并平滑 tempo map，消除 Song Master 每小节附近的微小抖动，但保留持续真实变速。
- 拍号自动模式采用最详细的 MIDI time-signature map，支持临时变拍。
- 调号自动模式结合 MIDI key 标签和音高类别分布，错误标签只作弱先验。
- 当前内容推断主要确定全曲初始调；中途自动转调只有明确 MIDI key 事件时可靠。
- 无可靠拍号事件时保守回退 4/4 并提示复核，尚未实现纯重音/攻击分布的拍号动态规划。

### MusicXML

- 只创建一个 Piano part，内部 `staves=2`。
- 高音 voice 为 1–4，低音 voice 为 5–8。
- 每条 voice 的小节时间严格闭合；用 `<backup>` 回到小节起点写下一声部。
- 非标准音长拆成合法 type/dot 组合并 tie，不能只写近似 `<type>`，否则 MuseScore 会改写 duration。
- tie 同时写 `<tie>` 和 `<notations><tied>`。
- tuplet 成员必须在同一 voice、同一小节内闭合；跨小节候选保留展开并报告。
- 琶音和弦的每个 pitch 都写 `<arpeggiate>`，MuseScore 渲染为一个完整符号。

## 7. 原始音频复核

音频复核是保守过滤器，不是再次盲目扒谱：

- 先做全曲和分窗时间对齐。
- 计算目标音高能量、谐波支持和起音证据。
- 用 Basic Pitch 输出交叉验证。
- Demucs 将完整混音分为 `vocals.wav` 与 `no_vocals.wav`，按 MIDI 的 `audio_role` 选择证据。
- 结合力度离群、极短孤立音和大跳等结构风险计算可信度。

自动删除需要很强的多重反证；DSP-only 模式不自动删除。琶音、倚音、连音、和弦、短独立起音受硬保护。原始音高和用户移调后的音高都会核对，避免正确八度修正被误删。疑似漏音只报告，不自动创建。

音频可能很大，WebUI 使用独立流式上传入口。任务保存确认后，上传音频、MIDI、MSCZ 和日志一起清理。

## 8. 已完成的关键验收

历史开发中完成过以下验证；仓库后来按用户要求清除了测试目录和样例，因此这些不是当前仓库内可重复执行的正式测试：

- 曾有 70+ 个 unittest 覆盖 MIDI IO、量化、MusicXML、MuseScore、CLI、WebUI、元数据和特殊节奏。
- 真实 Vocal + Piano 两 stem 音符守恒，固定为 treble voice 1 / bass voice 5，同来源无 gap/overlap。
- MuseScore 4 的 MusicXML -> MSCZ -> MusicXML 往返验证。
- 第 49/51/53 小节琶音在 strict/normal/aggressive 三档均正确。
- normal 模式往返 1160 个记谱语义项一致，单声部重叠为 0。
- 延迟重复 5:4 连音往返保持两组、10 个攻击和正确 bracket/tie。
- 1.25/1.75 拍非标准音长拆分和 tie 保持总时值。
- C 大调 E/F# trill 不会在缺少正确 accidental-mark 时破坏性折叠。
- 五条重叠音线超出一个谱表 4 voice 时明确拒绝，不移动攻击。
- Demucs 4.1 + CUDA GPU 实际分离成功，vocal/no-vocal 输出可读取。

接手后应尽快恢复不含版权素材的合成回归测试。

## 9. WebUI 与临时文件安全

后端不接受浏览器提交任意本机路径。保存目录由浏览器授权；写入后重新读取并校验 size/SHA-256，成功后才调用 `/api/jobs/{id}/saved` 清理服务端任务目录。

必须保持的协议：

- 结果使用 `/api/results/{id}`，不使用旧 `/api/download/{id}`。
- 保存确认在文件写入、关闭和校验成功之后发送。
- 清理失败不能假装成功，应保留结果以便重试。
- 重复 saved/ack 应幂等。
- running 任务不能当作已保存清理。
- token、Host、Origin 和 job id 都要校验。
- 不支持目录 API 时不要退回无法确认落盘的自动下载清理逻辑。

正常退出会清理临时根目录；强杀进程仍可能遗留系统临时文件，是可继续加强的方向。
