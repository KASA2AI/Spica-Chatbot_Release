# Spica Chatbot

Spica Chatbot 是一个本地桌面 Galgame 风格语音聊天应用。它用 PySide6 做透明悬浮窗口，用 OpenAI 兼容接口生成日语角色回复，再把回复拆成可播放片段，驱动立绘差分选择和 GPT-SoVITS 语音合成。

当前默认入口是桌面悬浮 UI：`webui_qt.py` -> `ui/qt_overlay.py`。`templates/index.html` 是旧版 Web/Flask 页面模板，当前仓库没有对应 Flask 路由，默认不再使用。

## 功能概览

- 透明置顶桌面 Overlay，包含立绘、对白框、输入框、窗口控制和设置面板。
- OpenAI 兼容 LLM 客户端，支持标准 Responses API，并对 DeepSeek 这类 Chat Completions 兼容客户端做降级适配。
- 短期上下文记忆和 SQLite 长期记忆。
- 本地工具调用示例：时间、模拟天气、四则计算器。
- 流式生成播放：LLM 增量文本 -> 断句播放单元 -> 并行立绘选择和 TTS -> 按顺序播放。
- GPT-SoVITS 本地日语语音合成，按情绪选择参考音频。
- 唱歌触发：识别“唱一首/唱一下/来一首”等请求，搜索网易云歌曲，分离人声和伴奏，再用 Applio/RVC 转成 Spica 声线并混音播放。
- 本地投票式立绘差分选择，支持 8 套服装、3 类手部动作、27 个表情编号。
- 可选中文麦克风识别，依赖 `speech_recognition` 和系统麦克风后端。

## 目录结构

```text
Spica-Chatbot/
├── webui_qt.py                    # 桌面入口，处理 Linux Qt 依赖和输入法环境
├── ui/
│   └── qt_overlay.py              # PySide6 透明 Overlay UI、流式播放和音频播放
├── agent/
│   ├── simple_agent.py            # SimpleAgent 门面，组装 LLM、记忆、工具、TTS、立绘服务
│   ├── runtime.py                 # 同步 voice pipeline 编排
│   ├── nodes.py                   # 同步 pipeline 节点：prompt、LLM、记忆、立绘、TTS、响应
│   ├── streaming_pipeline.py      # 流式生成、断句、并行 TTS/立绘、事件输出
│   ├── state.py                   # AgentState / AgentServices 数据结构
│   ├── prompt_builder.py          # Spica 角色 prompt 构建
│   ├── reply_parser.py            # 模型 JSON 回复解析和情绪归一化
│   └── character_loader.py        # 角色卡和对话者设定加载
├── memory/
│   ├── store.py                   # SQLite 长期记忆
│   ├── recent.py                  # 内存短期对话上下文
│   ├── extractor.py               # 从用户输入抽取可保存记忆
│   └── control.py                 # 记忆保存、去重、裁剪控制
├── agent_tools/
│   ├── function_tools/
│   │   ├── router.py              # LLM function tool schema、启发式路由和执行
│   │   ├── local_tools.py         # 时间、计算器、todo 等独立工具示例
│   │   └── song/                  # 唱歌请求解析、网易云检索、伴奏分离、RVC 转声线和混音
│   ├── tts/
│   │   ├── base.py                # TTSAdapter 协议
│   │   ├── schemas.py             # TTSRequest / TTSResult
│   │   ├── manager.py             # 根据配置构建 TTS adapter
│   │   ├── adapters/              # 前端适配层：current GPT-SoVITS / dummy
│   │   ├── gptsovits/
│   │   │   └── service.py         # GPT-SoVITS 后端封装、切句、写 wav、模型预热
│   │   └── vendors/
│   │       └── GPT-SoVITS-v2pro-20250604-nvidia50/ # 上游语音引擎占位目录
│   ├── visual/
│   │   └── diff_service.py        # 立绘差分选择、服装选择、图片路径解析
├── common/
│   └── timing.py                  # 通用计时日志
├── examples/
│   └── llm_demo.py                # 命令行记忆示例
├── config/
│   ├── tts_config.json            # GPT-SoVITS 路径、参考音频、合成参数
│   └── visual_config.json         # 差分根目录、对白框、服装和选择策略
├── spica_data/                    # 发布包只保留空目录骨架，素材由用户自行放入
│   ├── Spica_skill/               # 角色卡：SKILL.md/self.md/persona.md/meta.json
│   ├── voice/                     # TTS 情绪参考音频和 prompt
│   └── diffs/                     # 立绘差分、差分规则、UI 贴图
├── static/generated_voice/        # GPT-SoVITS 输出 wav，运行时生成
├── static/generated_song/         # 唱歌缓存和最终混音 wav，运行时生成
├── tests/                         # 单元测试和流水线 smoke test
├── run_ibus.sh                    # Linux ibus 输入法启动脚本
```

## 快速启动

1. 准备 GPT-SoVITS。

   发布包里的 `agent_tools/tts/vendors/GPT-SoVITS-v2pro-20250604-nvidia50/` 是空占位目录，不包含上游 GPT-SoVITS 代码、虚拟环境、模型权重或运行产物。使用前需要把匹配的 GPT-SoVITS v2Pro / nvidia50 版本解压或克隆到这个目录，完成后应至少能看到：

   ```text
   agent_tools/tts/vendors/GPT-SoVITS-v2pro-20250604-nvidia50/
   ├── requirements.txt
   ├── GPT_SoVITS/inference_webui.py
   ├── GPT_weights_v2ProPlus/
   └── SoVITS_weights_v2ProPlus/
   ```

   默认 `config/tts_config.json` 指向：

   - `agent_tools/tts/vendors/GPT-SoVITS-v2pro-20250604-nvidia50/GPT_weights_v2ProPlus/spcia-e25.ckpt`
   - `agent_tools/tts/vendors/GPT-SoVITS-v2pro-20250604-nvidia50/SoVITS_weights_v2ProPlus/spcia_e12_s1932.pth`

   如果你的目录名、权重文件名或版本不同，修改 `config/tts_config.json` 里的 `gptsovits_root`、`gpt_model_path` 和 `sovits_model_path`。

2. 准备唱歌功能依赖。

   唱歌功能默认读取 `agent_tools/function_tools/song/song_config.json`。发布包里的 `agent_tools/function_tools/song/Applio/` 不包含上游 Applio/RVC 代码、分离模型或声线权重，使用前需要自行放入对应环境和模型文件。默认配置需要：

   ```text
   agent_tools/function_tools/song/Applio/
   ├── logs/spica/spica_200e_57000s.pth
   ├── logs/spica/spica.index
   └── assets/uvr5_weights/UVR-MDX-NET-Inst_HQ_3.onnx
   ```

   如果模型路径、设备或混音参数不同，修改 `agent_tools/function_tools/song/song_config.json`。唱歌运行产物会写入 `static/generated_song/`，可以直接删除后重新生成。

3. 准备 `spica_data` 素材目录。

   发布包里的 `spica_data/` 只保留空目录和空子目录，不包含角色卡、立绘差分、参考音频、SQLite 记忆库或生成音频。需要按下面约定补齐：

   - `spica_data/Spica_skill/`：放入 `SKILL.md`、`self.md`、`persona.md`、`meta.json`；也可以用 `SPICA_SKILL_DIR` 或 `SPICA_CHARACTER_PROFILE` 指向其他角色设定。
   - `spica_data/voice/{happy,angry,sad,surprised}/`：每个情绪目录放 `prompt.txt` 和 `config/tts_config.json` 中配置的参考 wav；`refs/` 目录放入 GPT-SoVITS 需要的补充参考音频。
   - `spica_data/diffs/`：放入 `expression_hand_pose_rules.json`、`preview_png.png`、`ui/_mw_filter01.png`，以及各服装目录和表情 PNG；路径需与 `config/visual_config.json` 保持一致。
   - `spica_data/memory.sqlite3` 不需要手动创建，运行时会自动生成或迁移。

4. 准备 Python 环境。

   项目当前脚本示例使用 `/home/san/anaconda3/envs/gptsovits/bin/python3.11`。如果新建环境，建议 Python 3.10 或 3.11，并安装 GPT-SoVITS 所需依赖。

   ```bash
   cd /home/san/ai_code/Spica-Chatbot
   pip install openai httpx python-dotenv PySide6 soundfile numpy pytest
   pip install -r agent_tools/tts/vendors/GPT-SoVITS-v2pro-20250604-nvidia50/requirements.txt
   ```

   可选语音输入：

   ```bash
   pip install SpeechRecognition PyAudio
   ```

5. 创建 `xiaosan.env`。

   发布包中的 `xiaosan.env` 会保留变量名并清空 `=` 后面的值。使用前填入自己的 OpenAI 兼容接口配置，不要提交真实密钥。

   ```env
   OPENAI_API_KEY=你的密钥
   OPENAI_BASE_URL=https://api.openai.com/v1
   MODEL=gpt-4.1-mini
   ```

   可选环境变量：

   | 变量 | 默认值 | 作用 |
   | --- | --- | --- |
   | `RECENT_MEMORY_TURNS` | `3` | 内存短期记忆保留轮数 |
   | `RECENT_CONTEXT_LIMIT` | `3` | 每次 prompt 注入的短期上下文轮数 |
   | `LONG_TERM_MEMORY_LIMIT` | `5` | SQLite 长期记忆检索条数 |
   | `LONG_TERM_MEMORY_BUDGET_CHARS` | `1200` | 每次 prompt 注入的长期记忆字符预算 |
   | `RECENT_TURN_CHAR_LIMIT` | `360` | 单轮短期上下文注入字符上限 |
   | `MAX_LONG_TERM_MEMORIES` | `200` | 单个 conversation 保留的长期记忆上限 |
   | `MAX_TOOL_ROUNDS` | `3` | LLM 工具调用最大轮数 |
   | `SPICA_SKILL_DIR` | `spica_data/Spica_skill` | 默认角色卡目录 |
   | `SPICA_USER_NAME` | `麦` | 默认对话者名称，会映射原角色卡中的速川麦/麦 |
   | `SPICA_CHARACTER_PROFILE` | 读取默认角色卡 | 覆盖角色设定 |
   | `PLAY_UNIT_MIN_CHARS` | `18` | 流式播放单元最小长度 |
   | `PLAY_UNIT_MAX_CHARS` | `96` | 流式播放单元最大长度 |
   | `VISUAL_STREAM_WORKERS` | `2` | 流式立绘选择线程数 |

6. 启动桌面 Overlay。

   ```bash
   /home/san/anaconda3/envs/gptsovits/bin/python webui_qt.py
   ```

   Linux ibus 环境可以使用：

   ```bash
   ./run_ibus.sh
   ```

7. 命令行记忆测试。

   ```bash
   python examples/llm_demo.py
   ```

8. 运行测试。

   ```bash
   python -m pytest tests
   ```

   不建议直接在仓库根目录运行无参数 `pytest`，因为它可能递归扫描内置 `agent_tools/tts/vendors/GPT-SoVITS-v2pro-20250604-nvidia50/runtime/` 里的第三方包。

## 配置说明

### 角色卡与对话对象

默认启动时，`agent/simple_agent.py` 会读取 `spica_data/Spica_skill/` 下的 `SKILL.md`、`self.md`、`persona.md` 和 `meta.json`，作为 Spica 的角色卡注入 prompt。

Prompt 还会额外注入固定对话对象设定：当前输入始终视为 `SPICA_USER_NAME` 指定的人说的话，默认是 `麦`。角色卡里原本属于速川麦/麦的恋爱、同居、家人、重逢等人物事迹会在运行时映射成当前对话者名称；小麦、麦田、麦畑等普通词不会被替换。

桌面 Overlay 的设置面板里也可以临时编辑用户名。长期记忆只补充当前用户名的偏好、两人的相处细节或项目设置，不能覆盖角色卡和当前用户名的身份。

### 记忆控制

长期记忆写入路径现在统一为：规则抽取候选记忆 -> 过滤覆盖系统/角色卡的危险记忆 -> 按语义 key upsert 去重 -> 必要时按重要度裁剪。同步回复和流式回复共用同一套逻辑。

SQLite 记忆表会自动迁移新增字段：`memory_key`、`memory_type`、`source`、`confidence`、`pinned`、`status`。旧数据库可以直接继续使用。

### TTS 配置

`config/tts_config.json` 控制 GPT-SoVITS 的路径、模型权重、输出目录、预热策略和情绪参考音频。

关键字段：

- `provider`：TTS provider，默认 `gptsovits_current`；测试可切到 `dummy`。
- `gptsovits_root`：内置 GPT-SoVITS 根目录。
- `gpt_model_path` / `sovits_model_path`：默认 GPT 和 SoVITS 权重。
- `output_dir`：wav 输出目录，默认 `static/generated_voice`。
- `warmup_on_startup`：Overlay 启动后是否预热模型。
- `tts_params.sentence_chunking`：是否把长文本切成多个 TTS chunk。
- `emotions`：`happy`、`angry`、`sad`、`surprised` 的参考音频和 prompt。

### 唱歌配置

`agent_tools/function_tools/song/song_config.json` 控制歌曲搜索、缓存、UVR 伴奏分离、Applio/RVC 声线转换和最终混音。

关键字段：

- `enabled`：是否启用唱歌功能。
- `generated_root`：唱歌缓存和最终 wav 输出目录，默认 `static/generated_song`。
- `applio_root`：Applio/RVC 根目录。
- `search.limit` / `search.bitrate`：网易云搜索条数和音频码率。
- `separator.model_filename`：UVR 分离模型文件名。
- `separator.swap_stems`：交换分离出的 vocal/instrumental stem，适配不同分离模型输出。
- `rvc.voice_model`：默认 RVC 声线名称。
- `rvc.voices.<name>.model_path` / `index_path`：声线模型和索引文件。
- `mix.instrumental_gain` / `vocal_gain` / `normalize_peak`：伴奏、人声和峰值归一化参数。

Overlay 中输入类似 `spica唱歌 青花瓷`、`唱一下 周杰伦 的 稻香` 或 `来一首 十年` 会直接进入唱歌流程；唱歌准备或播放期间再次发送消息会取消当前歌曲。

### 立绘配置

`config/visual_config.json` 控制差分素材、服装、对白框和角色布局。

关键字段：

- `diff_root`：差分根目录，默认 `spica_data/diffs`。
- `rules_path`：表情和手部动作规则 JSON。
- `costume_mode`：`random` 或 `fixed`。
- `selected_costume`：固定服装模式下使用的服装。
- `segments`：非流式回答的断句和合并策略。
- `selection`：差分平滑策略。
- `dialog`：对白框颜色、滤镜、说话人名称。
- `character`：默认表情、默认手部动作、角色显示位置。

## 整体架构图

```mermaid
flowchart TD
    U[用户输入或麦克风识别] --> UI[PySide6 Overlay<br/>ui/qt_overlay.py]
    UI --> CW[ChatWorker<br/>后台线程]
    CW --> AG[SimpleAgent<br/>agent/simple_agent.py]
    AG --> SVC[AgentServices<br/>LLM/TTS/Visual/Memory/Tools]

    SVC --> MEM1[RecentMemory<br/>短期上下文]
    SVC --> MEM2[SQLiteMemoryStore<br/>长期记忆]
    SVC --> LLM[OpenAI 兼容 LLM]
    SVC --> TOOL[本地工具<br/>时间/天气/计算]
    SVC --> VIS[VisualDiffService<br/>立绘差分]
    SVC --> TTS[GPTSoVITSTool<br/>语音合成]
    UI --> SONG[SongPipeline<br/>歌曲检索/分离/RVC/混音]

    TTS --> GSV[GPT-SoVITS<br/>inference_webui]
    GSV --> WAV[static/generated_voice/*.wav]
    VIS --> IMG[spica_data/diffs/*.png]
    SONG --> SONGWAV[static/generated_song/final/*.wav]

    WAV --> UI
    SONGWAV --> UI
    IMG --> UI
    UI --> OUT[对白打字机<br/>立绘切换<br/>音频播放]
```

## 同步流水线逻辑图

`SimpleAgent.run_voice()` 使用 `runtime.run_voice_pipeline()`，适合一次性返回完整结果。

```mermaid
flowchart TD
    A[AgentState<br/>conversation_id/user_input] --> B[validate_input_node<br/>清洗输入]
    B --> C[load_recent_context_node<br/>读取短期上下文]
    C --> D[retrieve_long_term_memory_node<br/>SQLite 关键词检索]
    D --> E[build_prompt_node<br/>拼接 SYSTEM/角色/记忆/当前输入]
    E --> F[call_llm_node<br/>Responses API 或 Chat Completions]
    F --> G{需要工具?}
    G -- 是 --> H[run_local_tool<br/>执行本地工具]
    H --> I[_build_tool_followup_prompt<br/>工具结果回填]
    I --> F
    G -- 否 --> J[parse_reply_node<br/>解析 JSON answer/emotion]
    J --> K[save_recent_context_node<br/>写短期记忆]
    K --> L[extract_memory_node<br/>规则抽取长期记忆]
    L --> M[build_visual_node<br/>选择立绘 cue]
    M --> N[synthesize_tts_node<br/>合成 wav]
    N --> O[build_response_node<br/>封装 payload]
```

## 流式播放局部架构图

Overlay 默认使用 `SimpleAgent.stream_voice()`。它不会把 token 直接交给 UI，而是等到形成可播放句段后再输出 `unit_ready`。

```mermaid
flowchart TD
    A[stream_voice_events] --> B[status: thinking]
    B --> C[_produce_stream_events<br/>后台生产线程]
    C --> D[validate/load memory/build prompt]
    D --> E[_prepare_prompt_for_streaming<br/>必要时先做工具探测]
    E --> F[_iter_response_text<br/>LLM 流式文本]
    F --> G[JsonAnswerExtractor<br/>从 JSON 中增量抽取 answer]
    G --> H[PlayUnitSplitter<br/>按句号/问号/长度拆播放单元]
    H --> I[submit_unit]
    I --> J1[visual_executor<br/>build_unit_visual_payload]
    I --> J2[tts_executor<br/>synthesize 单句音频]
    J1 --> K[_finalize_unit]
    J2 --> K
    K --> L[按 index 排序输出 unit_ready]
    L --> M[Overlay _pump_stream_playback]
    M --> N[立绘切换 + 打字机 + QMediaPlayer]
    F --> O[done<br/>保存记忆并返回 timing]
```

事件类型：

| 事件 | 说明 |
| --- | --- |
| `status` | 当前状态，例如 thinking 或 tools |
| `unit_ready` | 一个可播放单元，包含文本、音频路径、立绘 cue、耗时 |
| `done` | 完整回答、最终情绪、总单元数和 timing |
| `error` | 流水线异常 |

## 立绘选择局部架构图

立绘选择完全在本地完成，不调用模型。核心入口是 `VisualDiffService.build_visual_payload()` 和 `build_unit_visual_payload()`。

```mermaid
flowchart TD
    A[回答文本或当前播放单元] --> B[split_segments<br/>按标点断句]
    B --> C[analyze_visual_text<br/>命中 signal lexicon]
    C --> D[EMOTION_GROUP_PRIORS<br/>叠加模型情绪先验]
    D --> E[score_expression<br/>按 group/subtype/关键词/强度打分]
    E --> F[choose_hand_pose_for_expression<br/>normal/arms_crossed/index_finger]
    F --> G[normalize_selection<br/>校验 expression_id 和 hand_pose]
    G --> H[resolve_expression_image<br/>服装/动作目录/face001_id.png]
    H --> I[cue<br/>image_path/image_url/reason]
```

差分素材当前约定：

- 服装目录：`spica_data/diffs/<服装名>/`
- 手部动作目录：`抱肩`、`普通动作`、`竖食指`
- 表情文件名匹配：`*face001_<id>.png`
- 规则文件：`spica_data/diffs/expression_hand_pose_rules.json`

## TTS 局部架构图

TTS 分成两层：`TTSAdapter` 是前端协议，pipeline 只依赖 `TTSRequest` / `TTSResult`；`GPTSoVITSTool` 是当前 GPT-SoVITS 后端实现，由 `CurrentGPTSoVITSAdapter` 包装后接入 pipeline。

```mermaid
flowchart TD
    A[pipeline] --> B[TTSRequest]
    B --> C[build_tts_adapter<br/>读取 provider]
    C --> D[CurrentGPTSoVITSAdapter]
    D --> E[GPTSoVITSTool.synthesize]
    E --> F[reload_config<br/>读取 tts_config.json]
    F --> G[_lazy_import<br/>导入 GPT-SoVITS]
    G --> H[_ensure_models<br/>切换 GPT/SoVITS 权重]
    H --> I[get_tts_wav<br/>逐 chunk 合成]
    I --> J[TTSResult<br/>audio_url/audio_path/chunks/timing]
```

集成的上游入口：

- `change_gpt_weights(gpt_path=...)`
- `change_sovits_weights(sovits_path=..., prompt_language=..., text_language=...)`
- `get_tts_wav(...)`

## 主要模块职责

| 文件 | 职责 |
| --- | --- |
| `webui_qt.py` | 启动前检查 Linux `libxcb-cursor.so.0`，处理 Qt 输入法兼容，然后进入 Overlay |
| `ui/qt_overlay.py` | UI、线程、设置面板、流式事件消费、打字机、立绘显示、QMediaPlayer 音频播放 |
| `agent/simple_agent.py` | 读取环境变量，初始化 OpenAI 客户端、记忆、工具，向 UI 暴露同步和流式接口 |
| `agent/runtime.py` / `agent/nodes.py` | 同步链路编排和每个处理节点 |
| `agent/streaming_pipeline.py` | 流式 LLM、JSON answer 增量抽取、播放单元拆分、并行 TTS/立绘、事件队列 |
| `agent/prompt_builder.py` | 构造 Spica 系统提示词，要求模型输出 JSON |
| `agent/reply_parser.py` | 解析模型 JSON，失败时用启发式情绪兜底 |
| `memory/store.py` | SQLite 长期记忆表、关键词检索、use_count 更新 |
| `memory/recent.py` | 每个 conversation_id 的最近 N 轮对话 |
| `memory/extractor.py` | 用规则识别“记住、我喜欢、叫我”等可保存事实 |
| `agent_tools/function_tools/router.py` | 定义 LLM function tool schema，判断是否启用工具，执行本地工具 |
| `agent_tools/function_tools/local_tools.py` | 时间、计算器、todo 等本地工具示例 |
| `agent_tools/function_tools/song/` | 唱歌请求解析、网易云歌曲检索、音频下载、伴奏分离、RVC 转声线和混音 |
| `agent_tools/visual/diff_service.py` | 本地投票式表情和手部动作选择，解析差分图片 |
| `agent_tools/tts/base.py` / `schemas.py` | TTS 前端协议和标准请求/返回结构 |
| `agent_tools/tts/manager.py` / `adapters/` | 根据配置选择 TTS provider，并适配同步/流式 pipeline |
| `agent_tools/tts/gptsovits/service.py` | GPT-SoVITS 后端模型加载、情绪参考音频选择、切句和 wav 输出 |
| `templates/index.html` | 旧 Web UI 模板，包含 SSE 播放逻辑，但当前不是默认入口 |

## 数据和运行产物

- `spica_data/memory.sqlite3`：长期记忆数据库，运行时更新。
- `static/generated_voice/*.wav`：语音输出，运行时生成。
- `static/generated_song/`：唱歌原曲缓存、分离结果、RVC 人声和最终混音，运行时生成。
- `spica_data/diffs/`：立绘差分素材和规则，不是运行缓存；发布包只保留空目录骨架。
- `agent_tools/tts/vendors/GPT-SoVITS-v2pro-20250604-nvidia50/`：发布包只保留空目录，占位给用户放入上游语音引擎、依赖和权重。
- `agent_tools/function_tools/song/Applio/`：发布包不内置上游 Applio/RVC 环境、UVR 模型或声线权重。
- `xiaosan.env`：本地密钥配置；发布包只保留空值模板，不应提交真实密钥。

`.gitignore` 已忽略 Python 缓存、环境文件、生成语音和 SQLite 运行数据库。

## 测试覆盖

测试重点在无真实 LLM/TTS 的情况下验证核心逻辑：

- `tests/test_memory_store.py`：SQLite 记忆新增、检索、清空。
- `tests/test_prompt_builder.py`：prompt 分区和记忆抽取。
- `tests/test_recent_memory.py`：短期记忆只保留最近轮次。
- `tests/test_pipeline_smoke.py`：同步 pipeline、工具路由、DeepSeek Chat Completions 兼容。
- `tests/test_streaming_pipeline.py`：流式事件顺序、句段拆分、TTS 文本清洗、DeepSeek 流式兼容。
- `tests/test_song_trigger.py`：唱歌触发语句解析和非唱歌问题排除。
- `tests/test_tts_adapters.py`：TTS adapter 请求/返回映射和失败兜底。
- `tests/test_visual_classifier.py`：本地立绘分类器对说明、不满、悲伤语气的选择。

## 开发注意事项

- LLM 最终输出必须是 JSON：`answer`、`emotion`、`emotion_reason`。
- `answer` 应是适合直接朗读的自然日语，避免长公式和难读符号。
- 流式链路只播放完整单元，不播放裸 token。
- `agent_tools/visual/diff_service.py` 的 `image_url` 主要服务旧 Web UI；桌面 Overlay 使用 `image_path` 直接加载本地图片。
- `agent_tools/tts/vendors/GPT-SoVITS-v2pro-20250604-nvidia50/` 是上游语音引擎和权重目录，业务层只应通过 `agent_tools/tts/gptsovits/service.py` 访问。
- `agent_tools/function_tools/router.py` 的 `get_weather()` 当前是模拟数据，不是真实天气接口。

## 发布打包

运行下面命令会在本项目上一层生成 `Spica-Chatbot_release/`：

```bash
bash build_release.sh
```

脚本会复制项目代码和配置，排除 `.git`、IDE 配置、缓存、SQLite、生成 wav、原始 `spica_data` 文件和完整 GPT-SoVITS 目录；然后重新创建空的 `agent_tools/tts/vendors/GPT-SoVITS-v2pro-20250604-nvidia50/`、空的 `spica_data/` 子目录骨架，并生成 `=` 后为空的 `xiaosan.env`。如果用 Git 推送 release 目录，注意 Git 本身不会记录真正的空目录，需要按 README 重新创建这些目录或自行添加占位文件。
