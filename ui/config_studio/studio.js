(() => {
  "use strict";

  const SUPPORTED_LOCALES = Object.freeze(["zh-CN", "en", "ja"]);
  const STATIC_COPY = Object.freeze({
    "document.title": Object.freeze({
      "zh-CN": "Spica 本地配置中心",
      "en": "Spica Local Configuration Studio",
      "ja": "Spica ローカル設定センター",
    }),
    "a11y.skip_to_content": Object.freeze({
      "zh-CN": "跳到配置内容",
      "en": "Skip to configuration",
      "ja": "設定内容へスキップ",
    }),
    "bootstrap.eyebrow": Object.freeze({
      "zh-CN": "本机会话",
      "en": "Local session",
      "ja": "ローカルセッション",
    }),
    "bootstrap.title": Object.freeze({
      "zh-CN": "粘贴启动授权",
      "en": "Paste startup authorization",
      "ja": "起動用認証情報を貼り付け",
    }),
    "bootstrap.help": Object.freeze({
      "zh-CN": "授权仅在内存中短暂有效、只能使用一次。它不会保存到浏览器。",
      "en": "This authorization is held in memory for a short time, can be used only once, and is never stored by the browser.",
      "ja": "この認証情報は短時間だけメモリに保持され、一度しか使用できません。ブラウザーには保存されません。",
    }),
    "bootstrap.label": Object.freeze({
      "zh-CN": "一次性启动授权",
      "en": "One-time startup authorization",
      "ja": "一度限りの起動用認証情報",
    }),
    "bootstrap.submit": Object.freeze({
      "zh-CN": "建立本机会话",
      "en": "Start local session",
      "ja": "ローカルセッションを開始",
    }),
    "brand.subtitle": Object.freeze({
      "zh-CN": "本地配置中心",
      "en": "Local Configuration Studio",
      "ja": "ローカル設定センター",
    }),
    "search.placeholder": Object.freeze({
      "zh-CN": "搜索字段、负责模块或来源…",
      "en": "Search fields, owners, or sources…",
      "ja": "項目、担当モジュール、取得元を検索…",
    }),
    "view.complexity": Object.freeze({
      "zh-CN": "字段复杂度",
      "en": "Field complexity",
      "ja": "項目の詳細度",
    }),
    "view.basic": Object.freeze({
      "zh-CN": "基础设置",
      "en": "Basic",
      "ja": "基本",
    }),
    "view.advanced": Object.freeze({
      "zh-CN": "高级设置",
      "en": "Advanced",
      "ja": "詳細",
    }),
    "session.connecting": Object.freeze({
      "zh-CN": "建立本地会话",
      "en": "Starting local session",
      "ja": "ローカルセッションを開始中",
    }),
    "nav.aria": Object.freeze({
      "zh-CN": "配置中心导航",
      "en": "Configuration Studio navigation",
      "ja": "設定センターのナビゲーション",
    }),
    "nav.configuration": Object.freeze({
      "zh-CN": "配置管理",
      "en": "Configuration",
      "ja": "設定管理",
    }),
    "nav.overview": Object.freeze({
      "zh-CN": "概览",
      "en": "Overview",
      "ja": "概要",
    }),
    "nav.switches": Object.freeze({
      "zh-CN": "功能开关",
      "en": "Feature switches",
      "ja": "機能スイッチ",
    }),
    "nav.categories": Object.freeze({
      "zh-CN": "分类配置",
      "en": "Configuration by category",
      "ja": "カテゴリ別設定",
    }),
    "nav.character": Object.freeze({
      "zh-CN": "角色数据",
      "en": "Character data",
      "ja": "キャラクターデータ",
    }),
    "nav.safety": Object.freeze({
      "zh-CN": "安全与维护",
      "en": "Security and maintenance",
      "ja": "セキュリティと保守",
    }),
    "nav.secrets": Object.freeze({
      "zh-CN": "密钥与环境覆盖",
      "en": "Secrets and environment overrides",
      "ja": "シークレットと環境変数の上書き",
    }),
    "nav.self_check": Object.freeze({
      "zh-CN": "自检",
      "en": "Self-check",
      "ja": "セルフチェック",
    }),
    "nav.restore": Object.freeze({
      "zh-CN": "恢复备份",
      "en": "Restore backups",
      "ja": "バックアップから復元",
    }),
    "effect.eyebrow": Object.freeze({
      "zh-CN": "生效时间",
      "en": "When changes take effect",
      "ja": "反映タイミング",
    }),
    "effect.next_launch": Object.freeze({
      "zh-CN": "下一次 Spica 启动",
      "en": "Next Spica launch",
      "ja": "次回の Spica 起動時",
    }),
    "effect.no_runtime_claim": Object.freeze({
      "zh-CN": "配置中心不推测已经运行的 Spica 正在使用什么值。",
      "en": "The Studio does not infer which values a currently running Spica process is using.",
      "ja": "設定センターは、実行中の Spica が使用している値を推測しません。",
    }),
    "overview.eyebrow": Object.freeze({
      "zh-CN": "仅限本机访问",
      "en": "Local access only",
      "ja": "ローカルアクセス専用",
    }),
    "overview.title": Object.freeze({
      "zh-CN": "把复杂配置，收进一个安静的地方。",
      "en": "Keep complex configuration in one calm place.",
      "ja": "複雑な設定を、ひとつの落ち着いた場所に。",
    }),
    "overview.lede": Object.freeze({
      "zh-CN": "字段来自生产配置规则；来源、默认值与下一次启动值彼此分开，修改前始终先预览。",
      "en": "Fields come from production configuration rules. Sources, defaults, and next-launch values remain distinct, and every change is previewed before it is saved.",
      "ja": "項目は本番の設定ルールから生成されます。取得元、既定値、次回起動時の値は区別され、変更内容は保存前に必ず確認できます。",
    }),
    "health.config": Object.freeze({
      "zh-CN": "配置健康",
      "en": "Configuration health",
      "ja": "設定の状態",
    }),
    "health.loading": Object.freeze({
      "zh-CN": "正在读取…",
      "en": "Loading…",
      "ja": "読み込み中…",
    }),
    "health.waiting_catalog": Object.freeze({
      "zh-CN": "等待本机负责模块返回安全的字段目录快照。",
      "en": "Waiting for the local owner to return a safe catalog snapshot.",
      "ja": "ローカルの担当モジュールから安全なカタログのスナップショットが返るのを待っています。",
    }),
    "health.effect_policy": Object.freeze({
      "zh-CN": "生效策略",
      "en": "Effect policy",
      "ja": "反映方法",
    }),
    "health.next_launch_value": Object.freeze({
      "zh-CN": "下次启动值",
      "en": "Next-launch value",
      "ja": "次回起動時の値",
    }),
    "health.reload_note": Object.freeze({
      "zh-CN": "只有负责模块明确支持按文件更新时间重读时，页面才会单独标注。",
      "en": "Live file reload is shown only when the owning module explicitly supports it.",
      "ja": "担当モジュールがファイル更新時の再読み込みを明示的にサポートする場合にのみ、その旨を表示します。",
    }),
    "health.self_check_summary": Object.freeze({
      "zh-CN": "自检摘要",
      "en": "Self-check summary",
      "ja": "セルフチェックの概要",
    }),
    "health.not_run": Object.freeze({
      "zh-CN": "尚未运行",
      "en": "Not run yet",
      "ja": "未実行",
    }),
    "health.self_check_note": Object.freeze({
      "zh-CN": "轻量检查不会下载模型；重检查需要逐项确认。",
      "en": "Light checks do not download models. Heavier checks require separate confirmation.",
      "ja": "軽量チェックではモデルをダウンロードしません。負荷の高いチェックは項目ごとの確認が必要です。",
    }),
    "catalog.eyebrow": Object.freeze({
      "zh-CN": "配置字段目录",
      "en": "Configuration catalog",
      "ja": "設定項目カタログ",
    }),
    "catalog.featured": Object.freeze({
      "zh-CN": "关键配置",
      "en": "Key configuration",
      "ja": "主な設定",
    }),
    "catalog.view_all": Object.freeze({
      "zh-CN": "查看全部",
      "en": "View all",
      "ja": "すべて表示",
    }),
    "source.eyebrow": Object.freeze({
      "zh-CN": "取值来源",
      "en": "Value sources",
      "ja": "値の取得元",
    }),
    "source.priority_title": Object.freeze({
      "zh-CN": "配置来源顺序",
      "en": "Source priority",
      "ja": "取得元の優先順位",
    }),
    "source.env_override": Object.freeze({
      "zh-CN": "环境变量覆盖",
      "en": "Environment override",
      "ja": "環境変数による上書き",
    }),
    "source.env_layers": Object.freeze({
      "zh-CN": "继承环境 → 仓库文件 → 父目录文件",
      "en": "Inherited environment → repository file → parent-directory file",
      "ja": "継承した環境 → リポジトリのファイル → 親ディレクトリのファイル",
    }),
    "source.app_yaml": Object.freeze({
      "zh-CN": "类型化应用配置文件",
      "en": "Typed application configuration file",
      "ja": "型付きアプリケーション設定ファイル",
    }),
    "source.schema_default": Object.freeze({
      "zh-CN": "配置规则默认值（Schema）",
      "en": "Schema default",
      "ja": "スキーマの既定値",
    }),
    "source.schema_fallback": Object.freeze({
      "zh-CN": "生产配置的最终回退值",
      "en": "Final fallback from production configuration rules",
      "ja": "本番設定ルールによる最終的なフォールバック値",
    }),
    "source.override_warning": Object.freeze({
      "zh-CN": "存在环境变量覆盖；修改 app.yaml 不会改变对应的下一次启动值。",
      "en": "An environment override is active. Editing app.yaml will not change the corresponding next-launch value.",
      "ja": "環境変数による上書きが有効です。app.yaml を変更しても、対応する次回起動時の値は変わりません。",
    }),
    "switches.eyebrow": Object.freeze({
      "zh-CN": "功能总览",
      "en": "Feature overview",
      "ja": "機能の概要",
    }),
    "switches.title": Object.freeze({
      "zh-CN": "功能开关",
      "en": "Feature switches",
      "ja": "機能スイッチ",
    }),
    "switches.help": Object.freeze({
      "zh-CN": "集中查看类型化的启用开关、后端选择和负责模块的生效策略。",
      "en": "Review typed enable switches, backend selections, and each owner's effect policy in one place.",
      "ja": "型付きの有効化スイッチ、バックエンドの選択、担当モジュールごとの反映方法をまとめて確認できます。",
    }),
    "configuration.eyebrow": Object.freeze({
      "zh-CN": "应用配置",
      "en": "Application configuration",
      "ja": "アプリケーション設定",
    }),
    "configuration.title": Object.freeze({
      "zh-CN": "分类配置",
      "en": "Configuration by category",
      "ja": "カテゴリ別設定",
    }),
    "configuration.categories": Object.freeze({
      "zh-CN": "大语言模型（LLM）· 语音合成（TTS）· 语音识别（STT）· 屏幕理解/文字识别（OCR）· 唱歌 · 看动漫 · Galgame 陪玩 · 记忆 · 插件 · 流式播放/界面",
      "en": "Large language model (LLM) · Text-to-speech (TTS) · Speech-to-text (STT) · Screen understanding / OCR · Singing · Anime · Galgame companion · Memory · Plugins · Streaming / UI",
      "ja": "大規模言語モデル（LLM）・音声合成（TTS）・音声認識（STT）・画面理解／文字認識（OCR）・歌唱・アニメ視聴・Galgame 同伴・記憶・プラグイン・ストリーミング／UI",
    }),
    "configuration.recovery_only": Object.freeze({
      "zh-CN": "app.yaml 当前处于仅恢复模式（recovery-only）；字段写入、移除和预览均已关闭。若服务端同时开放应用配置与回滚能力，可在“恢复备份”中执行语义回滚。",
      "en": "app.yaml is in recovery-only mode. Field writes, unsets, and previews are disabled. If the server also exposes application rollback capability, use Restore backups for a semantic rollback.",
      "ja": "app.yaml は復元専用モードです。項目の書き込み、削除、プレビューは無効です。サーバーがアプリケーションのロールバック機能も公開している場合は、「バックアップから復元」で意味を確認してロールバックできます。",
    }),
    "configuration.manual_repair": Object.freeze({
      "zh-CN": "APP_YAML_MANUAL_REPAIR_REQUIRED：没有可用恢复备份时，停止 Spica 与配置中心，先备份 data/config/app.yaml，再按生产 AppConfig/PyYAML 规则修复语法或字段，随后重新启动配置中心验证。首个版本不提供默认重置。",
      "en": "No valid restore point is available. Stop Spica and the Studio, back up data/config/app.yaml, repair its syntax or fields according to production AppConfig/PyYAML rules, then restart the Studio to validate it. Version 1 does not provide a reset-to-default document.",
      "ja": "有効な復元ポイントがありません。Spica と設定センターを停止し、data/config/app.yaml をバックアップしてから、本番の AppConfig/PyYAML ルールに従って構文または項目を修復し、設定センターを再起動して確認してください。バージョン 1 には既定の文書へリセットする機能はありません。",
    }),
    "configuration.catalog_incomplete": Object.freeze({
      "zh-CN": "CATALOG_FIELDS_INCOMPLETE：字段目录的安全投影、行数或字节预算导致 AppConfig 展示不完整；新增或修改操作会安全关闭，已有文件覆盖值仍可通过“移除文件值”修复。配置中心不会擅自恢复默认值。",
      "en": "The AppConfig catalog is incomplete because of safe projection, row, or byte limits. Set operations fail closed; existing file overrides can still be repaired with Remove file value. The Studio never resets configuration to defaults on its own.",
      "ja": "安全な投影、行数、またはバイト数の上限により AppConfig カタログが不完全です。値の追加・変更は安全のため無効ですが、既存のファイル上書きは「ファイル値を削除」で修復できます。設定センターが独断で既定値へ戻すことはありません。",
    }),
    "configuration.category_toolbar": Object.freeze({
      "zh-CN": "配置分类",
      "en": "Configuration categories",
      "ja": "設定カテゴリ",
    }),
    "overlay.eyebrow": Object.freeze({
      "zh-CN": "界面偏好文件",
      "en": "UI preference file",
      "ja": "UI 設定ファイル",
    }),
    "overlay.title": Object.freeze({
      "zh-CN": "桌面浮层偏好",
      "en": "Desktop overlay preferences",
      "ja": "デスクトップオーバーレイ設定",
    }),
    "overlay.next_launch_tag": Object.freeze({
      "zh-CN": "下次启动生效",
      "en": "Takes effect on next launch",
      "ja": "次回起動時に反映",
    }),
    "plugins.eyebrow": Object.freeze({
      "zh-CN": "插件负责模块状态",
      "en": "Plugin owner status",
      "ja": "プラグイン担当モジュールの状態",
    }),
    "plugins.title": Object.freeze({
      "zh-CN": "插件状态",
      "en": "Plugin status",
      "ja": "プラグインの状態",
    }),
    "common.read_only": Object.freeze({
      "zh-CN": "只读",
      "en": "Read-only",
      "ja": "読み取り専用",
    }),
    "environment.eyebrow": Object.freeze({
      "zh-CN": "非应用配置项",
      "en": "Non-application settings",
      "ja": "アプリケーション設定外の項目",
    }),
    "environment.title": Object.freeze({
      "zh-CN": "环境专属设置",
      "en": "Environment-only settings",
      "ja": "環境変数専用の設定",
    }),
    "environment.help": Object.freeze({
      "zh-CN": "这些字段在 app.yaml 中没有对应的负责模块；配置中心只展示服务端返回的安全状态，不提供编辑或清除。",
      "en": "These settings have no corresponding owner in app.yaml. The Studio shows only the safe status returned by the server and does not offer editing or clearing.",
      "ja": "これらの設定には app.yaml 上の対応項目がありません。設定センターはサーバーが返す安全な状態だけを表示し、編集や削除は行いません。",
    }),
    "character.eyebrow": Object.freeze({
      "zh-CN": "角色数据文件",
      "en": "Character data files",
      "ja": "キャラクターデータファイル",
    }),
    "character.title": Object.freeze({
      "zh-CN": "角色数据",
      "en": "Character data",
      "ja": "キャラクターデータ",
    }),
    "character.help": Object.freeze({
      "zh-CN": "语音合成（TTS）、视觉表现和外部角色文档在正式类型化规则完成前保持只读。",
      "en": "Text-to-speech (TTS), visual presentation, and external character documents remain read-only until their production owners expose canonical typed schemas.",
      "ja": "音声合成（TTS）、ビジュアル表示、外部キャラクター文書は、本番の担当モジュールが正式な型付きスキーマを提供するまで読み取り専用です。",
    }),
    "character.completeness_unknown": Object.freeze({
      "zh-CN": "角色数据文件目录的完整性信息不可用；下方卡片可能不完整。",
      "en": "Character document completeness is unavailable. The cards below may be incomplete.",
      "ja": "キャラクター文書の完全性情報を取得できません。以下のカードは一部省略されている可能性があります。",
    }),
    "secrets.eyebrow": Object.freeze({
      "zh-CN": "敏感值只写",
      "en": "Write-only sensitive values",
      "ja": "書き込み専用の機密値",
    }),
    "secrets.title": Object.freeze({
      "zh-CN": "密钥与环境覆盖",
      "en": "Secrets and environment overrides",
      "ja": "シークレットと環境変数の上書き",
    }),
    "secrets.help": Object.freeze({
      "zh-CN": "页面只显示是否已配置；现有敏感明文永远不会返回浏览器。",
      "en": "The page shows only whether a value is configured. Existing plaintext secrets are never returned to the browser.",
      "ja": "このページには設定済みかどうかだけを表示します。既存のシークレットの平文がブラウザーへ返されることはありません。",
    }),
    "secrets.slots": Object.freeze({
      "zh-CN": "密钥槽位",
      "en": "Secret slots",
      "ja": "シークレットスロット",
    }),
    "secrets.status_source": Object.freeze({
      "zh-CN": "配置状态与来源",
      "en": "Configuration status and source",
      "ja": "設定状態と取得元",
    }),
    "secrets.repo_file": Object.freeze({
      "zh-CN": "仓库敏感文件",
      "en": "Repository sensitive file",
      "ja": "リポジトリの機密ファイル",
    }),
    "secrets.parent_file": Object.freeze({
      "zh-CN": "父目录文件 · 只读",
      "en": "Parent-directory file · read-only",
      "ja": "親ディレクトリのファイル・読み取り専用",
    }),
    "managed_document.repo_dotenv": Object.freeze({
      "zh-CN": "仓库 xiaosan.env",
      "en": "Repository xiaosan.env",
      "ja": "リポジトリの xiaosan.env",
    }),
    "managed_document.parent_dotenv": Object.freeze({
      "zh-CN": "父目录 xiaosan.env",
      "en": "Parent-directory xiaosan.env",
      "ja": "親ディレクトリの xiaosan.env",
    }),
    "secrets.override_sources": Object.freeze({
      "zh-CN": "环境覆盖来源",
      "en": "Environment override sources",
      "ja": "環境変数上書きの取得元",
    }),
    "secrets.non_sensitive_owner_overrides": Object.freeze({
      "zh-CN": "非敏感负责模块覆盖项",
      "en": "Non-sensitive owner overrides",
      "ja": "機密ではない担当モジュールの上書き",
    }),
    "secrets.loading_status": Object.freeze({
      "zh-CN": "正在读取安全状态…",
      "en": "Loading safe status…",
      "ja": "安全な状態を読み込み中…",
    }),
    "secrets.summarizing_sources": Object.freeze({
      "zh-CN": "正在汇总来源…",
      "en": "Summarizing sources…",
      "ja": "取得元を集計中…",
    }),
    "secrets.write_only_action": Object.freeze({
      "zh-CN": "只写操作",
      "en": "Write-only action",
      "ja": "書き込み専用操作",
    }),
    "secrets.slot": Object.freeze({
      "zh-CN": "槽位",
      "en": "Slot",
      "ja": "スロット",
    }),
    "secrets.new_value": Object.freeze({
      "zh-CN": "新值（只写，不回显）",
      "en": "New value (write-only; never shown again)",
      "ja": "新しい値（書き込み専用・再表示なし）",
    }),
    "secrets.preview_set": Object.freeze({
      "zh-CN": "预览设置 / 替换",
      "en": "Preview set / replace",
      "ja": "設定／置換をプレビュー",
    }),
    "secrets.preview_clear": Object.freeze({
      "zh-CN": "预览清除",
      "en": "Preview clear",
      "ja": "削除をプレビュー",
    }),
    "secrets.clear_only": Object.freeze({
      "zh-CN": "仅清除仓库覆盖项",
      "en": "Clear repository overrides only",
      "ja": "リポジトリの上書きのみ削除",
    }),
    "secrets.mapped_overrides": Object.freeze({
      "zh-CN": "映射到 app.yaml 的环境覆盖",
      "en": "Environment overrides mapped to app.yaml",
      "ja": "app.yaml に対応する環境変数の上書き",
    }),
    "self_check.eyebrow": Object.freeze({
      "zh-CN": "安全诊断",
      "en": "Safe diagnostics",
      "ja": "安全な診断",
    }),
    "self_check.title": Object.freeze({
      "zh-CN": "自检",
      "en": "Self-check",
      "ja": "セルフチェック",
    }),
    "self_check.help": Object.freeze({
      "zh-CN": "复用现有 self_check.py；默认不下载模型，也不运行真实重检查。",
      "en": "Reuses the existing self_check.py. Models are not downloaded and real heavy checks are not run by default.",
      "ja": "既存の self_check.py を再利用します。既定ではモデルをダウンロードせず、実際の負荷の高いチェックも実行しません。",
    }),
    "self_check.light_eyebrow": Object.freeze({
      "zh-CN": "轻量计划",
      "en": "Light plan",
      "ja": "軽量プラン",
    }),
    "self_check.light_title": Object.freeze({
      "zh-CN": "轻量自检",
      "en": "Light self-check",
      "ja": "軽量セルフチェック",
    }),
    "self_check.idle": Object.freeze({
      "zh-CN": "空闲",
      "en": "Idle",
      "ja": "待機中",
    }),
    "self_check.light_help": Object.freeze({
      "zh-CN": "只运行固定轻量计划；不会加入 --full、调用真实 LLM 或允许模型下载。",
      "en": "Runs only the fixed light plan. It does not add --full, call a real LLM, or allow model downloads.",
      "ja": "固定された軽量プランだけを実行します。--full の追加、実際の LLM の呼び出し、モデルのダウンロード許可は行いません。",
    }),
    "self_check.run_light": Object.freeze({
      "zh-CN": "运行轻量自检",
      "en": "Run light self-check",
      "ja": "軽量セルフチェックを実行",
    }),
    "self_check.cancel": Object.freeze({
      "zh-CN": "取消当前任务",
      "en": "Cancel current job",
      "ja": "現在のジョブをキャンセル",
    }),
    "self_check.heavy_eyebrow": Object.freeze({
      "zh-CN": "明确选择的重检查",
      "en": "Explicitly selected heavy checks",
      "ja": "明示的に選択した負荷の高いチェック",
    }),
    "self_check.heavy_title": Object.freeze({
      "zh-CN": "分项重检查",
      "en": "Heavy checks by subsystem",
      "ja": "サブシステム別の詳細チェック",
    }),
    "self_check.confirm_required": Object.freeze({
      "zh-CN": "需要明确确认",
      "en": "Explicit confirmation required",
      "ja": "明示的な確認が必要",
    }),
    "self_check.heavy_help": Object.freeze({
      "zh-CN": "从固定安全名单中选择子系统。浏览器不会自行声称已确认；服务端会为完整计划签发短期一次性凭据。",
      "en": "Choose subsystems from the fixed safety allowlist. The browser never claims consent on its own; the server issues a short-lived one-time receipt for the complete plan.",
      "ja": "固定された安全な許可リストからサブシステムを選択します。ブラウザーが確認済みと独断で扱うことはなく、サーバーがプラン全体に対する短時間・一度限りの確認情報を発行します。",
    }),
    "self_check.subsystems": Object.freeze({
      "zh-CN": "重检查子系统",
      "en": "Heavy-check subsystems",
      "ja": "詳細チェック対象のサブシステム",
    }),
    "self_check.call_llm": Object.freeze({
      "zh-CN": "调用真实 LLM（--llm）",
      "en": "Call a real LLM (--llm)",
      "ja": "実際の LLM を呼び出す（--llm）",
    }),
    "self_check.include_disabled": Object.freeze({
      "zh-CN": "检查已禁用子系统（--all）",
      "en": "Check disabled subsystems (--all)",
      "ja": "無効なサブシステムも確認する（--all）",
    }),
    "self_check.allow_downloads": Object.freeze({
      "zh-CN": "允许模型下载",
      "en": "Allow model downloads",
      "ja": "モデルのダウンロードを許可",
    }),
    "self_check.confirm_group": Object.freeze({
      "zh-CN": "重检查确认",
      "en": "Heavy-check confirmations",
      "ja": "詳細チェックの確認",
    }),
    "self_check.confirm_heavy": Object.freeze({
      "zh-CN": "我确认运行所选真实重检查",
      "en": "I confirm running the selected real heavy checks",
      "ja": "選択した実際の詳細チェックを実行することを確認します",
    }),
    "self_check.confirm_llm": Object.freeze({
      "zh-CN": "我单独确认调用真实 LLM",
      "en": "I separately confirm calling a real LLM",
      "ja": "実際の LLM を呼び出すことを個別に確認します",
    }),
    "self_check.confirm_disabled": Object.freeze({
      "zh-CN": "我单独确认检查已禁用子系统（--all）",
      "en": "I separately confirm checking disabled subsystems (--all)",
      "ja": "無効なサブシステムも確認することを個別に確認します（--all）",
    }),
    "self_check.confirm_downloads": Object.freeze({
      "zh-CN": "我单独确认允许模型下载",
      "en": "I separately confirm allowing model downloads",
      "ja": "モデルのダウンロードを許可することを個別に確認します",
    }),
    "self_check.run_heavy": Object.freeze({
      "zh-CN": "获取服务端确认并运行",
      "en": "Obtain server confirmation and run",
      "ja": "サーバーの確認を取得して実行",
    }),
    "self_check.monitor_eyebrow": Object.freeze({
      "zh-CN": "有界任务视图",
      "en": "Bounded job view",
      "ja": "制限付きジョブ表示",
    }),
    "self_check.job_status": Object.freeze({
      "zh-CN": "任务状态",
      "en": "Job status",
      "ja": "ジョブの状態",
    }),
    "self_check.persistence": Object.freeze({
      "zh-CN": "页面关闭后，任务仍由本地配置服务管理；重新打开页面可以继续查询。",
      "en": "The local configuration service continues to manage a job after this page closes. Reopen the page to query it again.",
      "ja": "ページを閉じた後も、ローカル設定サービスがジョブを管理します。ページを開き直すと状態を再確認できます。",
    }),
    "self_check.progress": Object.freeze({
      "zh-CN": "进度",
      "en": "Progress",
      "ja": "進行状況",
    }),
    "self_check.results": Object.freeze({
      "zh-CN": "结果",
      "en": "Results",
      "ja": "結果",
    }),
    "self_check.waiting": Object.freeze({
      "zh-CN": "等待任务",
      "en": "Waiting for a job",
      "ja": "ジョブを待機中",
    }),
    "self_check.no_results": Object.freeze({
      "zh-CN": "暂无结果",
      "en": "No results yet",
      "ja": "結果はまだありません",
    }),
    "self_check.no_raw_stderr": Object.freeze({
      "zh-CN": "不会显示原始标准错误输出。",
      "en": "Raw stderr is never displayed.",
      "ja": "stderr の原文は表示されません。",
    }),
    "restore.eyebrow": Object.freeze({
      "zh-CN": "恢复备份",
      "en": "Restore backups",
      "ja": "バックアップから復元",
    }),
    "restore.title": Object.freeze({
      "zh-CN": "恢复备份",
      "en": "Restore backups",
      "ja": "バックアップから復元",
    }),
    "restore.help": Object.freeze({
      "zh-CN": "只展示不透明标识和语义预览，不暴露原始内容、摘要值或文件大小。",
      "en": "Only opaque identifiers and semantic previews are shown. Raw content, hashes, and file sizes are never exposed.",
      "ja": "不透明な識別子と意味上のプレビューだけを表示します。元の内容、ハッシュ、ファイルサイズは公開しません。",
    }),
    "restore.app_file": Object.freeze({
      "zh-CN": "应用配置文件",
      "en": "Application configuration file",
      "ja": "アプリケーション設定ファイル",
    }),
    "restore.ui_file": Object.freeze({
      "zh-CN": "界面偏好文件",
      "en": "UI preference file",
      "ja": "UI 設定ファイル",
    }),
    "restore.sensitive_file": Object.freeze({
      "zh-CN": "整个敏感文件",
      "en": "Entire sensitive file",
      "ja": "機密ファイル全体",
    }),
    "restore.sensitive_scope": Object.freeze({
      "zh-CN": "这里回滚整个敏感配置文件，不是单个密钥。",
      "en": "This rolls back the entire sensitive configuration file, not an individual secret.",
      "ja": "これは個別のシークレットではなく、機密設定ファイル全体をロールバックします。",
    }),
    "inspector.eyebrow": Object.freeze({
      "zh-CN": "字段详细说明",
      "en": "Field details",
      "ja": "項目の詳細",
    }),
    "inspector.select": Object.freeze({
      "zh-CN": "选择一个字段",
      "en": "Select a field",
      "ja": "項目を選択",
    }),
    "inspector.empty": Object.freeze({
      "zh-CN": "点击配置项，查看类型、默认值、下次启动值、来源与负责模块。",
      "en": "Select a configuration item to review its type, default, next-launch value, source, and owner.",
      "ja": "設定項目を選択すると、型、既定値、次回起動時の値、取得元、担当モジュールを確認できます。",
    }),
    "editor.eyebrow": Object.freeze({
      "zh-CN": "类型化编辑",
      "en": "Typed editing",
      "ja": "型付き編集",
    }),
    "editor.title": Object.freeze({
      "zh-CN": "修改文件中的值",
      "en": "Change the file value",
      "ja": "ファイル内の値を変更",
    }),
    "editor.add_change": Object.freeze({
      "zh-CN": "加入变更",
      "en": "Add change",
      "ja": "変更に追加",
    }),
    "editor.remove_file_value": Object.freeze({
      "zh-CN": "移除文件值",
      "en": "Remove file value",
      "ja": "ファイル値を削除",
    }),
    "editor.changes": Object.freeze({
      "zh-CN": "变更",
      "en": "Changes",
      "ja": "変更",
    }),
    "editor.preview": Object.freeze({
      "zh-CN": "预览变更",
      "en": "Preview changes",
      "ja": "変更をプレビュー",
    }),
    "common.cancel": Object.freeze({
      "zh-CN": "取消",
      "en": "Cancel",
      "ja": "キャンセル",
    }),
    "common.back_to_editing": Object.freeze({
      "zh-CN": "返回编辑",
      "en": "Back to editing",
      "ja": "編集に戻る",
    }),
    "common.back": Object.freeze({
      "zh-CN": "返回",
      "en": "Back",
      "ja": "戻る",
    }),
    "dialog.app.eyebrow": Object.freeze({
      "zh-CN": "变更预览",
      "en": "Change preview",
      "ja": "変更プレビュー",
    }),
    "dialog.app.title": Object.freeze({
      "zh-CN": "保存前语义差异",
      "en": "Semantic diff before saving",
      "ja": "保存前の意味上の差分",
    }),
    "dialog.app.commit": Object.freeze({
      "zh-CN": "保存 app.yaml",
      "en": "Save app.yaml",
      "ja": "app.yaml を保存",
    }),
    "dialog.overlay.eyebrow": Object.freeze({
      "zh-CN": "界面偏好预览",
      "en": "UI preference preview",
      "ja": "UI 設定のプレビュー",
    }),
    "dialog.overlay.title": Object.freeze({
      "zh-CN": "确认界面偏好变更",
      "en": "Confirm UI preference changes",
      "ja": "UI 設定の変更を確認",
    }),
    "dialog.overlay.commit": Object.freeze({
      "zh-CN": "保存 overlay_config.json",
      "en": "Save overlay_config.json",
      "ja": "overlay_config.json を保存",
    }),
    "dialog.sensitive.eyebrow": Object.freeze({
      "zh-CN": "敏感变更预览",
      "en": "Sensitive change preview",
      "ja": "機密設定の変更プレビュー",
    }),
    "dialog.sensitive.title": Object.freeze({
      "zh-CN": "确认敏感文件语义变更",
      "en": "Confirm semantic changes to the sensitive file",
      "ja": "機密ファイルの意味上の変更を確認",
    }),
    "dialog.sensitive.clear_confirm": Object.freeze({
      "zh-CN": "我确认清除这个密钥；提交前请签发一次性二次确认。",
      "en": "I confirm clearing this secret. Issue a one-time secondary confirmation before committing.",
      "ja": "このシークレットを削除することを確認します。反映前に一度限りの追加確認を発行してください。",
    }),
    "dialog.sensitive.commit": Object.freeze({
      "zh-CN": "提交敏感文档变更",
      "en": "Commit sensitive document change",
      "ja": "機密文書の変更を反映",
    }),
    "dialog.rollback.eyebrow": Object.freeze({
      "zh-CN": "回滚预览",
      "en": "Rollback preview",
      "ja": "ロールバックのプレビュー",
    }),
    "dialog.rollback.title": Object.freeze({
      "zh-CN": "确认语义回滚",
      "en": "Confirm semantic rollback",
      "ja": "意味を確認してロールバック",
    }),
    "dialog.rollback.confirm": Object.freeze({
      "zh-CN": "我确认按以上语义回滚，并理解系统会先为当前状态生成一份新的恢复备份。",
      "en": "I confirm this semantic rollback and understand that a new restore backup of the current state will be created first.",
      "ja": "上記の内容でロールバックすること、および現在の状態の復元バックアップが先に作成されることを理解し、確認します。",
    }),
    "dialog.rollback.commit": Object.freeze({
      "zh-CN": "执行回滚",
      "en": "Run rollback",
      "ja": "ロールバックを実行",
    }),
    "language_switch.label": Object.freeze({
      "zh-CN": "界面语言",
      "en": "Interface language",
      "ja": "表示言語",
    }),
  });

  const MESSAGE_COPY = Object.freeze({
    "session.ready": Object.freeze({
      "zh-CN": "本机安全会话",
      "en": "Secure local session",
      "ja": "安全なローカルセッション",
    }),
    "session.unavailable": Object.freeze({
      "zh-CN": "会话不可用",
      "en": "Session unavailable",
      "ja": "セッションを利用できません",
    }),
    "dynamic.yes": Object.freeze({
      "zh-CN": "是",
      "en": "Yes",
      "ja": "はい",
    }),
    "dynamic.no": Object.freeze({
      "zh-CN": "否",
      "en": "No",
      "ja": "いいえ",
    }),
    "dynamic.none": Object.freeze({
      "zh-CN": "无",
      "en": "None",
      "ja": "なし",
    }),
    "dynamic.unavailable": Object.freeze({
      "zh-CN": "不可用",
      "en": "Unavailable",
      "ja": "利用不可",
    }),
    "dynamic.unknown": Object.freeze({
      "zh-CN": "未知",
      "en": "Unknown",
      "ja": "不明",
    }),
    "dynamic.not_written": Object.freeze({
      "zh-CN": "未写入",
      "en": "Not written",
      "ja": "未記入",
    }),
    "dynamic.current_value": Object.freeze({
      "zh-CN": "当前值",
      "en": "Current value",
      "ja": "現在の値",
    }),
    "dynamic.default_value": Object.freeze({
      "zh-CN": "默认值",
      "en": "Default value",
      "ja": "既定値",
    }),
    "dynamic.next_launch_value": Object.freeze({
      "zh-CN": "下次启动值",
      "en": "Next-launch value",
      "ja": "次回起動時の値",
    }),
    "dynamic.value_source": Object.freeze({
      "zh-CN": "值的来源",
      "en": "Value source",
      "ja": "値の取得元",
    }),
    "dynamic.owner": Object.freeze({
      "zh-CN": "负责模块",
      "en": "Owner",
      "ja": "担当モジュール",
    }),
    "dynamic.effect_policy": Object.freeze({
      "zh-CN": "生效策略",
      "en": "Effect policy",
      "ja": "反映方法",
    }),
    "dynamic.path_health": Object.freeze({
      "zh-CN": "路径健康",
      "en": "Path health",
      "ja": "パスの状態",
    }),
    "dynamic.allowed_values": Object.freeze({
      "zh-CN": "可选值",
      "en": "Allowed values",
      "ja": "選択可能な値",
    }),
    "dynamic.allowed_range": Object.freeze({
      "zh-CN": "允许范围",
      "en": "Allowed range",
      "ja": "許容範囲",
    }),
    "dynamic.dependencies": Object.freeze({
      "zh-CN": "依赖条件",
      "en": "Dependencies",
      "ja": "依存条件",
    }),
    "dynamic.editing_status": Object.freeze({
      "zh-CN": "编辑状态",
      "en": "Editing status",
      "ja": "編集状態",
    }),
    "dynamic.prepare_rollback": Object.freeze({
      "zh-CN": "准备回滚",
      "en": "Prepare rollback",
      "ja": "ロールバックを準備",
    }),
    "dynamic.no_restore_points": Object.freeze({
      "zh-CN": "暂无恢复点。",
      "en": "No restore points available.",
      "ja": "復元ポイントはありません。",
    }),
    "dynamic.restore_point": Object.freeze({
      "zh-CN": "恢复点",
      "en": "Restore point",
      "ja": "復元ポイント",
    }),
    "dynamic.time_unavailable": Object.freeze({
      "zh-CN": "时间不可用",
      "en": "Time unavailable",
      "ja": "時刻を取得できません",
    }),
    "locale.unsaved_confirmation": Object.freeze({
      "zh-CN": "切换语言会重新加载页面，并丢失尚未保存的变更。是否继续？",
      "en": "Changing the language reloads the page and discards unsaved changes. Continue?",
      "ja": "表示言語を切り替えるとページが再読み込みされ、未保存の変更は破棄されます。続行しますか？",
    }),
    "dynamic.unknown_config_key": Object.freeze({
      "zh-CN": "未知配置键",
      "en": "Unknown configuration key",
      "ja": "不明な設定キー",
    }),
    "dynamic.effect_next_launch_sentence": Object.freeze({
      "zh-CN": "下次 Spica 启动生效。",
      "en": "Takes effect on the next Spica launch.",
      "ja": "次回の Spica 起動時に反映されます。",
    }),
    "dynamic.effect_owner_sentence": Object.freeze({
      "zh-CN": "按负责模块策略生效。",
      "en": "Takes effect according to the owner policy.",
      "ja": "担当モジュールの方針に従って反映されます。",
    }),
    "sensitive.still_shadowed": Object.freeze({
      "zh-CN": " · 仍被更高优先级覆盖",
      "en": " · Still overridden by a higher-priority source",
      "ja": " · より優先度の高い取得元に引き続き上書きされています",
    }),
    "sensitive.content_changed": Object.freeze({
      "zh-CN": "有变化",
      "en": "Changed",
      "ja": "変更あり",
    }),
    "sensitive.content_unchanged": Object.freeze({
      "zh-CN": "无变化",
      "en": "Unchanged",
      "ja": "変更なし",
    }),
    "self_check.mode_full": Object.freeze({
      "zh-CN": "重检查",
      "en": "Heavy check",
      "ja": "詳細チェック",
    }),
    "self_check.mode_light": Object.freeze({
      "zh-CN": "轻量检查",
      "en": "Light check",
      "ja": "軽量チェック",
    }),
    "self_check.technical_detail": Object.freeze({
      "zh-CN": "技术详情",
      "en": "Technical detail",
      "ja": "技術詳細",
    }),
    "self_check.stderr_count_truncated": Object.freeze({
      "zh-CN": "（计数已截断）",
      "en": " (count truncated)",
      "ja": "（件数は打ち切られました）",
    }),
  });
  const HEAVY_CHECKS = Object.freeze([
    "tts",
    "stt",
    "moondream",
    "ocr",
    "song_uvr",
    "song_rvc",
    "llm",
  ]);
  const ACTIVE_JOB_STATUSES = new Set(["QUEUED", "RUNNING", "CANCELLING"]);
  const JOB_STATUSES = new Set([
    ...ACTIVE_JOB_STATUSES,
    "PASS",
    "UNVERIFIED",
    "DEGRADED",
    "FAIL",
    "CANCELLED",
    "INTERNAL_ERROR",
  ]);
  const RESULT_STATUSES = new Set([
    "PASS",
    "UNVERIFIED",
    "DEGRADED",
    "FAIL",
    "SKIPPED_DISABLED",
  ]);
  const MAX_RENDERED_RESULTS = 12;
  const MAX_RENDERED_PROGRESS = 12;
  const MAX_STRUCTURED_ITEMS = 256;
  const SELF_CHECK_POLL_MS = 1200;
  const RESTORE_ROUTES = Object.freeze({
    app: Object.freeze({
      list: `/api/v1/app/restore-points`,
      rollback: `/api/v1/app/rollbacks`,
    }),
    overlay: Object.freeze({
      list: `/api/v1/overlay/restore-points`,
      rollback: `/api/v1/overlay/rollbacks`,
    }),
    sensitive: Object.freeze({
      list: `/api/v1/sensitive/restore-points`,
      rollback: `/api/v1/sensitive/rollbacks`,
    }),
  });

  const FIELD_PRESENTATIONS = Object.freeze({
    "llm.provider": Object.freeze({
      "zh-CN": Object.freeze({
        title: "大语言模型（LLM）提供方式",
        description: "决定主对话与默认总结任务使用哪一种模型适配器。它必须与服务地址和模型名称相匹配。",
        advice: "不确定时保持当前值；更换提供方式前先确认目标服务兼容现有接口。",
      }),
      en: Object.freeze({
        title: "Large language model (LLM) provider",
        description: "Selects the model adapter used for primary conversations and summary tasks that do not specify a separate model. It must match the service URL and model name.",
        advice: "Keep the current value if unsure. Before changing providers, confirm that the target service supports the existing API contract.",
      }),
      ja: Object.freeze({
        title: "大規模言語モデル（LLM）プロバイダー",
        description: "通常の会話と、専用モデルを指定していない要約処理で使用するモデルアダプターを選びます。サービス URL とモデル名に合う必要があります。",
        advice: "不明な場合は現在の値を維持してください。変更前に、接続先が既存の API 仕様に対応していることを確認してください。",
      }),
    }),
    "llm.model": Object.freeze({
      "zh-CN": Object.freeze({
        title: "主对话模型",
        description: "指定 Spica 日常对话以及未单独指定模型的任务所使用的模型名称。",
        advice: "更强的模型通常质量更高，但响应时间和调用成本也可能增加。",
      }),
      en: Object.freeze({
        title: "Primary conversation model",
        description: "Specifies the model used for Spica's everyday conversations and for tasks that do not select another model.",
        advice: "More capable models often improve quality, but may also increase response time and usage cost.",
      }),
      ja: Object.freeze({
        title: "メイン会話モデル",
        description: "Spica の通常の会話と、別のモデルを指定していない処理で使用するモデルを指定します。",
        advice: "高性能なモデルほど品質が向上する傾向がありますが、応答時間や利用コストが増える場合があります。",
      }),
    }),
    "llm.base_url": Object.freeze({
      "zh-CN": Object.freeze({
        title: "模型服务地址",
        description: "指定大语言模型服务的 API 地址；未填写时由所选提供方式使用自身默认地址。",
        advice: "这里只填写服务地址，不要填写 API 密钥。地址错误会导致所有依赖主模型的功能不可用。",
      }),
      en: Object.freeze({
        title: "Model service URL",
        description: "Specifies the API URL of the large language model service. When empty, the selected provider uses its default URL.",
        advice: "Enter only the service URL here, not an API key. An incorrect URL disables every feature that depends on the primary model.",
      }),
      ja: Object.freeze({
        title: "モデルサービス URL",
        description: "大規模言語モデルサービスの API URL を指定します。空欄の場合は、選択したプロバイダーの既定 URL が使われます。",
        advice: "ここにはサービス URL だけを入力し、API キーは入力しないでください。URL が誤っていると、メインモデルを使うすべての機能が利用できなくなります。",
      }),
    }),
    "llm.reasoning_effort": Object.freeze({
      "zh-CN": Object.freeze({
        title: "主模型推理强度",
        description: "控制支持该能力的主模型是否启用额外推理，以及使用多高的推理强度。",
        advice: "提高强度可能改善复杂问题表现，但通常会增加首字等待时间；不确定时保持当前值。",
      }),
      en: Object.freeze({
        title: "Primary model reasoning effort",
        description: "Controls whether supported primary models use additional reasoning and how much reasoning effort they apply.",
        advice: "Higher effort may improve complex answers, but usually increases the wait for the first response. Keep the current value if unsure.",
      }),
      ja: Object.freeze({
        title: "メインモデルの推論強度",
        description: "対応するメインモデルで追加推論を使うか、およびその推論強度を設定します。",
        advice: "強度を上げると複雑な問題の品質が向上する場合がありますが、通常は最初の応答までの時間が延びます。不明な場合は現在の値を維持してください。",
      }),
    }),
    "memory.provider": Object.freeze({
      "zh-CN": Object.freeze({
        title: "记忆存储方式",
        description: "决定角色长期记忆由哪一个存储实现负责保存和读取。",
        advice: "更换后可能无法直接看到原存储中的历史记忆，迁移前应先备份。",
      }),
      en: Object.freeze({
        title: "Memory storage provider",
        description: "Selects the storage implementation responsible for saving and retrieving the character's long-term memories.",
        advice: "After switching providers, existing memories in the previous store may no longer be visible. Back them up before migrating.",
      }),
      ja: Object.freeze({
        title: "記憶ストレージのプロバイダー",
        description: "キャラクターの長期記憶を保存・取得するストレージ実装を選びます。",
        advice: "変更後は以前のストレージにある記憶が表示されない場合があります。移行前にバックアップしてください。",
      }),
    }),
    "memory.recent_memory_turns": Object.freeze({
      "zh-CN": Object.freeze({
        title: "短期记忆保留轮数",
        description: "决定内存中最多保留多少轮最近对话，供后续上下文读取。",
        advice: "数值越大越不容易忘记刚才的对话，但会增加上下文长度和资源消耗。",
      }),
      en: Object.freeze({
        title: "Recent conversation retention",
        description: "Sets the maximum number of recent conversation turns retained in memory for later context.",
        advice: "A larger value helps retain recent discussion, but increases context length and resource use.",
      }),
      ja: Object.freeze({
        title: "直近の会話を保持するターン数",
        description: "後続の文脈で使うために、メモリ内へ保持する直近の会話ターン数の上限を設定します。",
        advice: "値を大きくすると直前の会話を覚えやすくなりますが、文脈長とリソース使用量が増えます。",
      }),
    }),
    "memory.recent_context_limit": Object.freeze({
      "zh-CN": Object.freeze({
        title: "每次注入的近期对话数",
        description: "决定生成回复时从短期记忆中最多取多少轮对话放入提示词。",
        advice: "通常不应超过短期记忆保留轮数；过大会挤占其他上下文空间。",
      }),
      en: Object.freeze({
        title: "Recent turns added to each prompt",
        description: "Sets how many recent conversation turns can be read from short-term memory and added to a response prompt.",
        advice: "This should normally not exceed recent conversation retention. A large value leaves less room for other context.",
      }),
      ja: Object.freeze({
        title: "プロンプトに追加する直近ターン数",
        description: "応答生成時に短期記憶から取り出し、プロンプトへ追加する直近の会話ターン数の上限を設定します。",
        advice: "通常は直近の会話を保持するターン数以下にしてください。値が大きすぎると、ほかの文脈に使える領域が減ります。",
      }),
    }),
    "memory.long_term_memory_limit": Object.freeze({
      "zh-CN": Object.freeze({
        title: "每次读取的长期记忆数",
        description: "决定每次回复最多检索多少条相关的长期记忆。",
        advice: "增加数量可能补充更多背景，也可能引入相关性较低的内容。",
      }),
      en: Object.freeze({
        title: "Long-term memories retrieved per response",
        description: "Sets the maximum number of relevant long-term memories retrieved for each response.",
        advice: "Retrieving more items may provide additional background, but can also introduce less relevant information.",
      }),
      ja: Object.freeze({
        title: "応答ごとに取得する長期記憶数",
        description: "各応答で取得する関連性の高い長期記憶の最大件数を設定します。",
        advice: "件数を増やすと背景情報が増える一方、関連性の低い内容が混ざる場合があります。",
      }),
    }),
    "memory.long_term_memory_budget_chars": Object.freeze({
      "zh-CN": Object.freeze({
        title: "长期记忆字符预算",
        description: "限制一次提示词中所有长期记忆合计可占用的字符数。",
        advice: "预算过小会截掉细节，过大则会增加模型上下文和响应开销。",
      }),
      en: Object.freeze({
        title: "Long-term memory character budget",
        description: "Limits the combined number of characters that all long-term memories may occupy in a single prompt.",
        advice: "A small budget can omit details; a large budget increases model context and response overhead.",
      }),
      ja: Object.freeze({
        title: "長期記憶の文字数上限",
        description: "1 回のプロンプト内で、すべての長期記憶が使用できる合計文字数を制限します。",
        advice: "小さすぎると詳細が省かれ、大きすぎるとモデルの文脈量と応答負荷が増えます。",
      }),
    }),
    "memory.recent_turn_char_limit": Object.freeze({
      "zh-CN": Object.freeze({
        title: "单轮近期对话字符上限",
        description: "限制每一轮近期对话注入提示词时最多保留多少字符。",
        advice: "较小值更节省上下文，较大值更适合包含长文本的对话。",
      }),
      en: Object.freeze({
        title: "Character limit per recent turn",
        description: "Limits how many characters are retained from each recent conversation turn when it is added to a prompt.",
        advice: "A smaller value saves context space; a larger value is more suitable for conversations containing long passages.",
      }),
      ja: Object.freeze({
        title: "直近 1 ターンあたりの文字数上限",
        description: "直近の各会話ターンをプロンプトへ追加するときに保持する文字数を制限します。",
        advice: "小さい値は文脈を節約でき、大きい値は長文を含む会話に適しています。",
      }),
    }),
    "memory.max_long_term_memories": Object.freeze({
      "zh-CN": Object.freeze({
        title: "长期记忆总量上限",
        description: "限制一个角色与会话范围内可保留的活跃长期记忆数量。",
        advice: "降低上限可能淘汰旧记忆；调整前建议先确认现有记忆保留需求。",
      }),
      en: Object.freeze({
        title: "Total long-term memory limit",
        description: "Limits the number of active long-term memories retained within one character and conversation scope.",
        advice: "Lowering the limit may remove older memories. Review retention needs before changing it.",
      }),
      ja: Object.freeze({
        title: "長期記憶の総数上限",
        description: "1 つのキャラクターと会話の範囲で保持できる有効な長期記憶の件数を制限します。",
        advice: "上限を下げると古い記憶が削除される場合があります。変更前に保持要件を確認してください。",
      }),
    }),
    "character.interlocutor_name": Object.freeze({
      "zh-CN": Object.freeze({
        title: "对话者称呼",
        description: "指定 Spica 在对话中如何称呼当前用户；未填写时使用角色层默认称呼。",
        advice: "填写自然称呼即可，不需要加入引号或提示词格式。",
      }),
      en: Object.freeze({
        title: "Name used for the conversation partner",
        description: "Specifies how Spica addresses the current user. When empty, the character-level default is used.",
        advice: "Enter a natural name or form of address without quotation marks or prompt formatting.",
      }),
      ja: Object.freeze({
        title: "会話相手の呼び名",
        description: "Spica が現在のユーザーをどのように呼ぶかを指定します。空欄の場合はキャラクター側の既定値が使われます。",
        advice: "自然な名前や呼び方を入力してください。引用符やプロンプト用の書式は不要です。",
      }),
    }),
    "character.profile_override": Object.freeze({
      "zh-CN": Object.freeze({
        title: "角色设定覆盖文本",
        description: "用自定义文本覆盖角色包提供的默认角色设定。",
        advice: "这是高级选项；内容会直接影响角色表现，不确定时保持未填写。",
      }),
      en: Object.freeze({
        title: "Character profile override",
        description: "Replaces the default character profile supplied by the character package with custom text.",
        advice: "This is an advanced setting and directly affects character behavior. Leave it empty if unsure.",
      }),
      ja: Object.freeze({
        title: "キャラクター設定の上書き",
        description: "キャラクターパッケージが提供する既定のキャラクター設定を、独自のテキストで置き換えます。",
        advice: "キャラクターの振る舞いに直接影響する上級設定です。不明な場合は空欄のままにしてください。",
      }),
    }),
    "character.skill_dir": Object.freeze({
      "zh-CN": Object.freeze({
        title: "角色技能目录",
        description: "指定角色技能资料所在目录，由角色装配流程在下次启动时读取。",
        advice: "只填写可信且确实存在的目录；配置中心仅检查路径健康，不提供目录浏览。",
      }),
      en: Object.freeze({
        title: "Character skills directory",
        description: "Specifies the directory containing character skill material, which is read during character assembly on the next launch.",
        advice: "Use only a trusted directory that exists. Config Studio checks path health but does not provide directory browsing.",
      }),
      ja: Object.freeze({
        title: "キャラクタースキルのディレクトリ",
        description: "キャラクターの組み立て処理が次回起動時に読み込む、スキル資料のディレクトリを指定します。",
        advice: "実在する信頼済みのディレクトリだけを指定してください。Config Studio はパスの状態を確認しますが、ディレクトリ参照機能は提供しません。",
      }),
    }),
    "character.package_dir": Object.freeze({
      "zh-CN": Object.freeze({
        title: "角色包目录",
        description: "指定当前启用的角色包目录；未填写时使用 Spica 的默认角色数据。",
        advice: "切换角色包会同时影响角色资料及其 TTS、视觉数据来源。",
      }),
      en: Object.freeze({
        title: "Character package directory",
        description: "Specifies the active character package directory. When empty, Spica's default character data is used.",
        advice: "Changing the character package also changes the sources of character information, TTS data, and visual data.",
      }),
      ja: Object.freeze({
        title: "キャラクターパッケージのディレクトリ",
        description: "現在使用するキャラクターパッケージのディレクトリを指定します。空欄の場合は Spica の既定キャラクターデータが使われます。",
        advice: "パッケージを切り替えると、キャラクター情報に加えて TTS と表示用データの参照元も変わります。",
      }),
    }),
    "character.character_id": Object.freeze({
      "zh-CN": Object.freeze({
        title: "已解析的角色标识",
        description: "由角色包负责模块在装配时计算，用于区分角色身份和记忆命名空间。",
        advice: "这是只读派生结果，不应作为手工切换角色的入口。",
      }),
      en: Object.freeze({
        title: "Resolved character identifier",
        description: "A derived identifier calculated from the character package during assembly and used to separate character identities and memory namespaces.",
        advice: "This is a read-only derived result and should not be used as the control for switching characters.",
      }),
      ja: Object.freeze({
        title: "解決済みキャラクター ID",
        description: "組み立て時にキャラクターパッケージから算出され、キャラクターの識別と記憶の名前空間の分離に使われる派生値です。",
        advice: "読み取り専用の派生結果です。キャラクターを切り替える操作には使用しないでください。",
      }),
    }),
    "character.character_profile": Object.freeze({
      "zh-CN": Object.freeze({
        title: "已解析的角色设定",
        description: "角色包与覆盖配置合并后，实际交给对话系统的角色设定。",
        advice: "这是只读派生结果；需要修改时应调整角色包或角色设定覆盖文本。",
      }),
      en: Object.freeze({
        title: "Resolved character profile",
        description: "The effective character profile passed to the conversation system after combining the package data and profile override.",
        advice: "This is a read-only derived result. Edit the character package or the character profile override to change it.",
      }),
      ja: Object.freeze({
        title: "解決済みキャラクター設定",
        description: "キャラクターパッケージと上書き設定を統合した後、会話システムへ渡される実際のキャラクター設定です。",
        advice: "読み取り専用の派生結果です。変更する場合は、キャラクターパッケージまたは上書き設定を編集してください。",
      }),
    }),
    "character.character_name": Object.freeze({
      "zh-CN": Object.freeze({
        title: "已解析的角色名称",
        description: "由当前角色包解析得到的显示名称和对话身份名称。",
        advice: "这是只读派生结果；需要更名时应修改正确的角色数据负责模块。",
      }),
      en: Object.freeze({
        title: "Resolved character name",
        description: "The display and conversation identity name resolved from the current character package.",
        advice: "This is a read-only derived result. Change the responsible character data source if the name needs to be updated.",
      }),
      ja: Object.freeze({
        title: "解決済みキャラクター名",
        description: "現在のキャラクターパッケージから取得した、表示と会話上の識別に使う名前です。",
        advice: "読み取り専用の派生結果です。名前を変更する場合は、該当するキャラクターデータを編集してください。",
      }),
    }),
    "character.dialog_display_language": Object.freeze({
      "zh-CN": Object.freeze({
        title: "对话框显示语言",
        description: "决定对话框显示日语原文还是提示词生成的中文译文；不会改变实际语音语言。",
        advice: "该选项只影响文字显示，TTS、记忆和事件仍保留原有语言语义。",
      }),
      en: Object.freeze({
        title: "Dialogue display language",
        description: "Chooses whether the dialogue box shows the original Japanese text or the Chinese translation generated for the prompt. It does not change the spoken language.",
        advice: "This affects text display only. TTS, memories, and events retain their existing language semantics.",
      }),
      ja: Object.freeze({
        title: "会話欄の表示言語",
        description: "会話欄に日本語の原文と、プロンプトで生成した中国語訳のどちらを表示するかを選びます。音声の言語は変わりません。",
        advice: "文字表示だけに影響します。TTS、記憶、イベントの言語上の意味は従来どおり維持されます。",
      }),
    }),
    "stream.play_unit_min_chars": Object.freeze({
      "zh-CN": Object.freeze({
        title: "最短播放分段字符数",
        description: "控制流式回复至少积累多少字符后才形成一个可播放语音片段。",
        advice: "较小值更快开始说话但片段更碎；较大值更连贯但首段等待更久。",
      }),
      en: Object.freeze({
        title: "Minimum speech segment length",
        description: "Sets how many characters a streaming response must accumulate before it can form a playable speech segment.",
        advice: "A smaller value starts speech sooner but creates more fragments; a larger value is smoother but delays the first segment.",
      }),
      ja: Object.freeze({
        title: "音声セグメントの最小文字数",
        description: "ストリーミング応答を再生可能な音声セグメントにするまでに、最低限蓄積する文字数を設定します。",
        advice: "小さい値は話し始めが早い一方で断片化しやすく、大きい値は滑らかになる一方で最初の再生が遅くなります。",
      }),
    }),
    "stream.play_unit_max_chars": Object.freeze({
      "zh-CN": Object.freeze({
        title: "最长播放分段字符数",
        description: "限制单个流式语音片段最多包含多少字符。",
        advice: "应大于最短分段字符数；过大会增加单段生成和播放等待。",
      }),
      en: Object.freeze({
        title: "Maximum speech segment length",
        description: "Limits the number of characters included in a single streaming speech segment.",
        advice: "Keep it greater than the minimum segment length. A very large value increases generation and playback delay per segment.",
      }),
      ja: Object.freeze({
        title: "音声セグメントの最大文字数",
        description: "1 つのストリーミング音声セグメントに含める文字数を制限します。",
        advice: "最小文字数より大きく設定してください。大きすぎると、各セグメントの生成と再生開始までの時間が延びます。",
      }),
    }),
    "stream.visual_stream_workers": Object.freeze({
      "zh-CN": Object.freeze({
        title: "视觉流处理线程数",
        description: "决定流式回复中可并行处理视觉表现任务的处理线程数量。",
        advice: "增加数量可能提升并行度，也会增加 CPU 和内存压力；普通设备保持默认即可。",
      }),
      en: Object.freeze({
        title: "Visual stream worker count",
        description: "Sets how many workers may process visual presentation tasks in parallel during a streaming response.",
        advice: "More workers may improve parallelism but also increase CPU and memory use. The default is suitable for typical systems.",
      }),
      ja: Object.freeze({
        title: "表示ストリームのワーカー数",
        description: "ストリーミング応答中に、表示演出の処理を並列実行できるワーカー数を設定します。",
        advice: "数を増やすと並列性が上がる場合がありますが、CPU とメモリの使用量も増えます。一般的な環境では既定値を維持してください。",
      }),
    }),
    "galgame.summary_model": Object.freeze({
      "zh-CN": Object.freeze({
        title: "剧情总结模型",
        description: "指定 galgame 剧情后台总结使用的模型；未填写时复用主对话模型。",
        advice: "可选择更快、更便宜的模型，但总结质量会影响后续剧情回忆。",
      }),
      en: Object.freeze({
        title: "Galgame story summary model",
        description: "Specifies the model used for background summaries of galgame story content. When empty, the primary conversation model is reused.",
        advice: "A faster or less costly model can be used, but summary quality affects later recall of the story.",
      }),
      ja: Object.freeze({
        title: "Galgame シナリオ要約モデル",
        description: "Galgame のシナリオをバックグラウンドで要約するモデルを指定します。空欄の場合はメイン会話モデルを再利用します。",
        advice: "高速または低コストのモデルも利用できますが、要約品質は後のシナリオ想起に影響します。",
      }),
    }),
    "galgame.summary_trigger_chars": Object.freeze({
      "zh-CN": Object.freeze({
        title: "剧情总结触发字符数",
        description: "未总结剧情累计到这个字符量附近时，触发一次后台剧情总结。",
        advice: "较小值总结更频繁，较大值会让临时剧情缓冲更长。",
      }),
      en: Object.freeze({
        title: "Story summary trigger size",
        description: "Starts a background story summary when unsummarized story content approaches this character count.",
        advice: "A smaller value summarizes more often; a larger value keeps a longer temporary story buffer.",
      }),
      ja: Object.freeze({
        title: "シナリオ要約を開始する文字数",
        description: "未要約のシナリオがこの文字数付近まで蓄積すると、バックグラウンド要約を開始します。",
        advice: "小さい値では要約頻度が上がり、大きい値では一時的なシナリオバッファが長くなります。",
      }),
    }),
    "galgame.ocr_interval_seconds": Object.freeze({
      "zh-CN": Object.freeze({
        title: "剧情文字识别间隔",
        description: "控制 galgame 陪玩模式完成一次文字识别后，等待多久再开始下一次采样。",
        advice: "间隔太长可能漏过快速翻页，太短会增加 CPU、GPU 和截图压力。",
      }),
      en: Object.freeze({
        title: "Story text recognition interval",
        description: "Sets how long galgame companion mode waits after one text recognition pass finishes before starting the next sample.",
        advice: "A long interval may miss rapidly changing pages; a short interval increases CPU, GPU, and capture load.",
      }),
      ja: Object.freeze({
        title: "シナリオ文字認識の間隔",
        description: "Galgame 同伴モードで 1 回の文字認識が完了してから、次の取得を始めるまでの待機時間を設定します。",
        advice: "間隔が長いと速いページ送りを見逃す可能性があり、短いと CPU、GPU、画面取得の負荷が増えます。",
      }),
    }),
    "galgame.reaction_mode": Object.freeze({
      "zh-CN": Object.freeze({
        title: "剧情主动反应频率",
        description: "决定 Spica 是否以及多频繁地对识别到的剧情主动发表评论。",
        advice: "希望安静陪看时选择关闭；提高频率会增加模型调用和语音打断机会。",
      }),
      en: Object.freeze({
        title: "Proactive story reaction frequency",
        description: "Controls whether, and how often, Spica proactively comments on recognized story content.",
        advice: "Choose the disabled option for quiet viewing. Higher frequency increases model calls and the chance that speech interrupts play.",
      }),
      ja: Object.freeze({
        title: "シナリオへの自発反応頻度",
        description: "認識したシナリオに Spica が自発的にコメントするか、およびその頻度を設定します。",
        advice: "静かに見守ってほしい場合は無効にしてください。頻度を上げるとモデル呼び出しと音声による中断が増えます。",
      }),
    }),
    "galgame.reaction_table": Object.freeze({
      "zh-CN": Object.freeze({
        title: "剧情反应分级规则",
        description: "为各反应档位自定义最低分数、时间窗口次数和冷却时间。",
        advice: "这是高级结构化配置；填写后会替代对应档位的内置规则，建议成组核对。",
      }),
      en: Object.freeze({
        title: "Story reaction tier rules",
        description: "Customizes the minimum score, number of reactions per time window, and cooldown for each reaction tier.",
        advice: "This is an advanced structured setting. Supplied entries replace the built-in rules for their tiers, so review them as a group.",
      }),
      ja: Object.freeze({
        title: "シナリオ反応の段階別ルール",
        description: "各反応段階の最低スコア、時間枠内の回数、クールダウン時間を設定します。",
        advice: "上級者向けの構造化設定です。入力した内容は該当段階の組み込みルールを置き換えるため、まとめて確認してください。",
      }),
    }),
    "galgame.reaction_judge_enabled": Object.freeze({
      "zh-CN": Object.freeze({
        title: "启用模型判断剧情反应",
        description: "启用后由专用大语言模型判断当前剧情是否值得主动回应，而不是仅使用本地词表评分。",
        advice: "会增加模型调用和延迟；启用前应同时确认判断模型与评分阈值。",
      }),
      en: Object.freeze({
        title: "Use a model to judge story reactions",
        description: "When enabled, a dedicated LLM decides whether the current story merits a proactive response instead of relying only on local keyword scoring.",
        advice: "This adds model calls and latency. Confirm the judging model and score thresholds before enabling it.",
      }),
      ja: Object.freeze({
        title: "モデルによるシナリオ反応判定",
        description: "有効にすると、ローカルのキーワード採点だけでなく、専用 LLM が現在のシナリオへ自発反応する価値があるかを判定します。",
        advice: "モデル呼び出しと遅延が増えます。有効化する前に、判定モデルとスコアしきい値を確認してください。",
      }),
    }),
    "galgame.reaction_judge_model": Object.freeze({
      "zh-CN": Object.freeze({
        title: "剧情反应判断模型",
        description: "指定判断剧情是否值得回应的模型；未填写时回落到主对话模型。",
        advice: "适合使用响应快的小模型；切换模型后应重新确认判断质量。",
      }),
      en: Object.freeze({
        title: "Story reaction judging model",
        description: "Specifies the model that judges whether story content merits a response. When empty, it falls back to the primary conversation model.",
        advice: "A responsive smaller model is suitable. Recheck judging quality after changing models.",
      }),
      ja: Object.freeze({
        title: "シナリオ反応判定モデル",
        description: "シナリオに反応する価値があるかを判定するモデルを指定します。空欄の場合はメイン会話モデルへフォールバックします。",
        advice: "応答の速い小型モデルが適しています。モデル変更後は判定品質を再確認してください。",
      }),
    }),
    "galgame.reaction_judge_base_url": Object.freeze({
      "zh-CN": Object.freeze({
        title: "剧情反应判断服务地址",
        description: "指定剧情反应判断模型使用的独立 API 地址；未填写时沿用主模型地址。",
        advice: "可将判断流量分离到独立服务，避免与主对话模型争用。",
      }),
      en: Object.freeze({
        title: "Story reaction judging service URL",
        description: "Specifies a separate API URL for the story reaction judging model. When empty, the primary model URL is reused.",
        advice: "Use this to route judging traffic to a separate service and avoid contention with the primary conversation model.",
      }),
      ja: Object.freeze({
        title: "シナリオ反応判定サービス URL",
        description: "シナリオ反応判定モデル用の独立した API URL を指定します。空欄の場合はメインモデルの URL を再利用します。",
        advice: "判定処理を別サービスへ分離し、メイン会話モデルとの競合を避けたい場合に使用します。",
      }),
    }),
    "galgame.reaction_judge_reasoning_effort": Object.freeze({
      "zh-CN": Object.freeze({
        title: "剧情判断推理强度",
        description: "控制剧情反应判断模型的额外推理强度，与主对话模型的设置相互独立。",
        advice: "较低强度响应更快；提高强度前应确认实际判断质量确有改善。",
      }),
      en: Object.freeze({
        title: "Story judging reasoning effort",
        description: "Controls additional reasoning effort for the story reaction judging model independently of the primary conversation model setting.",
        advice: "Lower effort responds faster. Increase it only after confirming that judging quality improves in practice.",
      }),
      ja: Object.freeze({
        title: "シナリオ判定の推論強度",
        description: "シナリオ反応判定モデルの追加推論強度を、メイン会話モデルとは独立して設定します。",
        advice: "低い強度ほど高速です。実際に判定品質が向上することを確認してから強度を上げてください。",
      }),
    }),
    "galgame.reaction_reply_char_limit": Object.freeze({
      "zh-CN": Object.freeze({
        title: "剧情主动回应字数上限",
        description: "限制 Spica 对剧情主动发表评论时，生成指令要求的回复长度。",
        advice: "数值越小越像短吐槽，越大越可能打断游戏节奏。",
      }),
      en: Object.freeze({
        title: "Proactive story reply length limit",
        description: "Sets the requested maximum length of Spica's generated proactive comments about story content.",
        advice: "A smaller value produces brief reactions; a larger value is more likely to interrupt the pace of the game.",
      }),
      ja: Object.freeze({
        title: "シナリオへの自発コメント文字数上限",
        description: "Spica がシナリオへ自発的にコメントするとき、生成指示で求める応答の最大長を設定します。",
        advice: "小さい値ほど短い感想になり、大きい値ほどゲーム進行を中断しやすくなります。",
      }),
    }),
    "galgame.reaction_budget_window_seconds": Object.freeze({
      "zh-CN": Object.freeze({
        title: "剧情反应统计窗口",
        description: "指定统计主动反应次数时使用的滚动时间窗口长度，单位为秒。",
        advice: "它与分级规则中的窗口次数共同决定一段时间内最多回应多少次。",
      }),
      en: Object.freeze({
        title: "Story reaction counting window",
        description: "Specifies the rolling time window, in seconds, used to count proactive reactions.",
        advice: "Together with the per-tier window count, this determines how many reactions are allowed over a period of time.",
      }),
      ja: Object.freeze({
        title: "シナリオ反応の集計時間枠",
        description: "自発反応の回数を集計するローリング時間枠を秒単位で指定します。",
        advice: "段階別ルールの時間枠内回数と組み合わさり、一定時間内に反応できる最大回数を決めます。",
      }),
    }),
    "galgame.reaction_excerpt_line_char_limit": Object.freeze({
      "zh-CN": Object.freeze({
        title: "单行剧情摘录上限",
        description: "限制主动反应指令中每一行剧情文本最多保留的字符数。",
        advice: "较小值更节省提示词，但可能丢失单句中的重要细节。",
      }),
      en: Object.freeze({
        title: "Character limit per story excerpt line",
        description: "Limits how many characters are retained from each story line included in a proactive reaction prompt.",
        advice: "A smaller value saves prompt space but may omit important details within a line.",
      }),
      ja: Object.freeze({
        title: "シナリオ抜粋 1 行あたりの文字数上限",
        description: "自発反応用プロンプトに含める各シナリオ行の文字数を制限します。",
        advice: "小さい値はプロンプトを節約できますが、1 行内の重要な詳細が失われる場合があります。",
      }),
    }),
    "galgame.reaction_excerpt_total_char_limit": Object.freeze({
      "zh-CN": Object.freeze({
        title: "剧情摘录总字符上限",
        description: "限制一次主动反应指令中所有剧情摘录合计的字符数。",
        advice: "应不小于单行上限；过大会增加判断与回复的上下文成本。",
      }),
      en: Object.freeze({
        title: "Total story excerpt character limit",
        description: "Limits the combined number of characters in all story excerpts included in one proactive reaction prompt.",
        advice: "Keep it at least as large as the per-line limit. A large value increases context cost for judging and replying.",
      }),
      ja: Object.freeze({
        title: "シナリオ抜粋の合計文字数上限",
        description: "1 回の自発反応用プロンプトに含める、すべてのシナリオ抜粋の合計文字数を制限します。",
        advice: "1 行あたりの上限以上に設定してください。大きすぎると、判定と応答に使う文脈量が増えます。",
      }),
    }),
    "galgame.prompt_context_recent_limit": Object.freeze({
      "zh-CN": Object.freeze({
        title: "提示词近期剧情条目数",
        description: "限制注入提示词的近期剧情总结、选项和共同经历条目数量。",
        advice: "提高数量有助于回顾更多剧情，也会占用更多模型上下文。",
      }),
      en: Object.freeze({
        title: "Recent story items added to prompts",
        description: "Limits the number of recent story summaries, choices, and shared-experience entries added to a prompt.",
        advice: "A larger value recalls more story context but uses more of the model's context window.",
      }),
      ja: Object.freeze({
        title: "プロンプトに追加する直近シナリオ項目数",
        description: "プロンプトへ追加する直近のシナリオ要約、選択肢、共有体験の項目数を制限します。",
        advice: "値を増やすとより多くのシナリオを振り返れますが、モデルの文脈領域を多く使用します。",
      }),
    }),
    "galgame.game_buffer_tail_limit": Object.freeze({
      "zh-CN": Object.freeze({
        title: "未总结剧情尾部保留行数",
        description: "限制提示词中最多保留多少行尚未总结的近期剧情；原始总结输入不会因此丢失。",
        advice: "当前值为 0 时表示不限制；总结偶尔失败时，设置合理上限可防止提示词持续增长。",
      }),
      en: Object.freeze({
        title: "Unsummarized story tail line limit",
        description: "Limits how many recent unsummarized story lines are added to prompts. It does not discard the original summary input.",
        advice: "A value of 0 means unlimited. A practical limit prevents prompts from growing continuously when summaries occasionally fail.",
      }),
      ja: Object.freeze({
        title: "未要約シナリオ末尾の行数上限",
        description: "プロンプトに追加する、まだ要約されていない直近のシナリオ行数を制限します。元の要約入力は失われません。",
        advice: "0 は無制限を表します。要約が一時的に失敗したときもプロンプトが増え続けないよう、適切な上限を設定できます。",
      }),
    }),
    "galgame.play_history_card_max_chars": Object.freeze({
      "zh-CN": Object.freeze({
        title: "陪玩履历卡字符上限",
        description: "限制写入对话上下文的陪玩履历卡最大长度。",
        advice: "更长不一定带来更多有效信息，通常保持足以概括共同经历的长度即可。",
      }),
      en: Object.freeze({
        title: "Companion play-history card length",
        description: "Limits the maximum length of the companion play-history card added to conversation context.",
        advice: "More text does not always provide more useful information. A length sufficient to summarize shared experiences is usually best.",
      }),
      ja: Object.freeze({
        title: "同伴プレイ履歴カードの文字数上限",
        description: "会話の文脈へ追加する同伴プレイ履歴カードの最大長を制限します。",
        advice: "長いほど有用とは限りません。共有体験を要約できる程度の長さを保つのが適切です。",
      }),
    }),
    "tts.enabled": Object.freeze({
      "zh-CN": Object.freeze({
        title: "启用语音合成（TTS）",
        description: "决定下次启动是否装配语音合成引擎；关闭后仍可显示文字回复。",
        advice: "关闭可节省模型加载时间和显存，但 Spica 将不再朗读回复。",
      }),
      en: Object.freeze({
        title: "Enable text-to-speech (TTS)",
        description: "Controls whether the text-to-speech engine is assembled on the next launch. Text responses remain available when it is disabled.",
        advice: "Disabling it saves model loading time and GPU memory, but Spica will no longer read responses aloud.",
      }),
      ja: Object.freeze({
        title: "音声合成（TTS）を有効化",
        description: "次回起動時に音声合成エンジンを組み立てるかを設定します。無効にしても文字の応答は表示できます。",
        advice: "無効にするとモデルの読み込み時間と GPU メモリを節約できますが、Spica は応答を読み上げなくなります。",
      }),
    }),
    "stt.backend": Object.freeze({
      "zh-CN": Object.freeze({
        title: "语音识别（STT）后端",
        description: "决定用户语音由本地 Faster-Whisper 还是兼容的在线识别路径处理。",
        advice: "本地识别更可控且不依赖网络；选择其他后端前确认其网络与隐私要求。",
      }),
      en: Object.freeze({
        title: "Speech-to-text (STT) backend",
        description: "Selects whether user speech is processed by local Faster-Whisper or a compatible online recognition path.",
        advice: "Local recognition is more controllable and does not require a network connection. Review network and privacy requirements before choosing another backend.",
      }),
      ja: Object.freeze({
        title: "音声認識（STT）バックエンド",
        description: "ユーザーの音声をローカルの Faster-Whisper と互換オンライン認識経路のどちらで処理するかを選びます。",
        advice: "ローカル認識は管理しやすく、ネットワークに依存しません。別のバックエンドを選ぶ前に、通信とプライバシーの要件を確認してください。",
      }),
    }),
    "stt.mic_backend": Object.freeze({
      "zh-CN": Object.freeze({
        title: "麦克风采集后端",
        description: "决定语音输入使用 ReSpeaker 硬件路径还是通用麦克风路径；自动值会按桌面平台选择。",
        advice: "除非正在排查设备兼容问题，否则保持自动选择。",
      }),
      en: Object.freeze({
        title: "Microphone capture backend",
        description: "Selects the ReSpeaker hardware path or the generic microphone path for speech input. The automatic option follows the desktop platform.",
        advice: "Keep automatic selection unless you are troubleshooting device compatibility.",
      }),
      ja: Object.freeze({
        title: "マイク入力バックエンド",
        description: "音声入力に ReSpeaker ハードウェア経路と汎用マイク経路のどちらを使うかを選びます。自動設定ではデスクトップ環境に合わせて選択されます。",
        advice: "デバイス互換性を調査する場合を除き、自動選択を維持してください。",
      }),
    }),
    "stt.model": Object.freeze({
      "zh-CN": Object.freeze({
        title: "语音识别模型",
        description: "指定 Faster-Whisper 使用的模型名称或已下载的本地模型目录。",
        advice: "更大模型通常更准确，但加载更慢且占用更多显存。",
      }),
      en: Object.freeze({
        title: "Speech recognition model",
        description: "Specifies the Faster-Whisper model name or the directory of a model already downloaded locally.",
        advice: "Larger models are generally more accurate, but load more slowly and use more GPU memory.",
      }),
      ja: Object.freeze({
        title: "音声認識モデル",
        description: "Faster-Whisper で使うモデル名、またはダウンロード済みローカルモデルのディレクトリを指定します。",
        advice: "大きいモデルほど一般に高精度ですが、読み込みが遅く、GPU メモリの使用量も増えます。",
      }),
    }),
    "stt.device": Object.freeze({
      "zh-CN": Object.freeze({
        title: "语音识别运行设备",
        description: "决定语音识别模型在显卡还是处理器等设备上运行。",
        advice: "显卡通常更快；显存不足或无兼容显卡时才考虑处理器。",
      }),
      en: Object.freeze({
        title: "Speech recognition device",
        description: "Selects the compute device, such as a GPU or CPU, used to run the speech recognition model.",
        advice: "A GPU is usually faster. Consider a CPU only when no compatible GPU is available or GPU memory is insufficient.",
      }),
      ja: Object.freeze({
        title: "音声認識の実行デバイス",
        description: "音声認識モデルを実行する GPU や CPU などの計算デバイスを選びます。",
        advice: "通常は GPU の方が高速です。対応 GPU がない場合や GPU メモリが不足する場合に CPU を検討してください。",
      }),
    }),
    "stt.compute_type": Object.freeze({
      "zh-CN": Object.freeze({
        title: "语音识别计算精度",
        description: "控制语音识别推理使用的数值精度和量化方式。",
        advice: "低精度通常更省资源，但必须与设备和运行库兼容。",
      }),
      en: Object.freeze({
        title: "Speech recognition compute precision",
        description: "Controls the numeric precision and quantization mode used for speech recognition inference.",
        advice: "Lower precision generally uses fewer resources, but it must be compatible with the selected device and runtime.",
      }),
      ja: Object.freeze({
        title: "音声認識の計算精度",
        description: "音声認識の推論で使用する数値精度と量子化方式を設定します。",
        advice: "低い精度は一般にリソースを節約できますが、選択したデバイスと実行環境に対応している必要があります。",
      }),
    }),
    "stt.language": Object.freeze({
      "zh-CN": Object.freeze({
        title: "语音识别语言",
        description: "告诉语音识别模型主要应按哪种语言解释输入语音。",
        advice: "设置为主要对话语言可减少误识别；配置值保持后端支持的语言代码。",
      }),
      en: Object.freeze({
        title: "Speech recognition language",
        description: "Tells the speech recognition model which language should primarily be used to interpret incoming speech.",
        advice: "Select the main conversation language to reduce recognition errors, using a language code supported by the backend.",
      }),
      ja: Object.freeze({
        title: "音声認識の言語",
        description: "入力音声を主にどの言語として解釈するかを音声認識モデルへ指定します。",
        advice: "誤認識を減らすため、主な会話言語を選んでください。設定値にはバックエンドが対応する言語コードを使います。",
      }),
    }),
    "stt.beam_size": Object.freeze({
      "zh-CN": Object.freeze({
        title: "语音识别搜索宽度",
        description: "控制识别时同时比较多少个候选结果。",
        advice: "提高可能改善部分识别结果，但会增加计算时间。",
      }),
      en: Object.freeze({
        title: "Speech recognition search width",
        description: "Controls how many candidate transcriptions are compared during recognition.",
        advice: "A larger value may improve some recognition results, but increases processing time.",
      }),
      ja: Object.freeze({
        title: "音声認識の探索幅",
        description: "音声認識時に同時比較する文字起こし候補の数を設定します。",
        advice: "値を増やすと一部の認識結果が改善する場合がありますが、処理時間も増えます。",
      }),
    }),
    "stt.vad_filter": Object.freeze({
      "zh-CN": Object.freeze({
        title: "启用识别器语音活动过滤",
        description: "决定 Faster-Whisper 是否在转写前额外过滤可能没有人声的片段。",
        advice: "已有录音端语音活动检测时通常无需重复开启；误切短句时应保持关闭。",
      }),
      en: Object.freeze({
        title: "Enable recognizer voice activity filtering",
        description: "Controls whether Faster-Whisper additionally filters segments that may not contain speech before transcription.",
        advice: "This is usually unnecessary when voice activity detection already runs during recording. Keep it disabled if short utterances are being cut off.",
      }),
      ja: Object.freeze({
        title: "認識器の音声区間フィルターを有効化",
        description: "Faster-Whisper が文字起こし前に、音声を含まない可能性のある区間を追加で除外するかを設定します。",
        advice: "録音側ですでに音声区間検出を行う場合は通常不要です。短い発話が欠ける場合は無効のままにしてください。",
      }),
    }),
    "stt.warmup_on_startup": Object.freeze({
      "zh-CN": Object.freeze({
        title: "启动时预热语音识别",
        description: "决定启动阶段是否提前加载并预热语音识别模型。",
        advice: "开启可减少第一次说话的等待，但会延长启动并提前占用资源。",
      }),
      en: Object.freeze({
        title: "Warm up speech recognition at startup",
        description: "Controls whether the speech recognition model is loaded and warmed up during startup.",
        advice: "Enabling this reduces the first-use delay, but makes startup longer and allocates resources earlier.",
      }),
      ja: Object.freeze({
        title: "起動時に音声認識をウォームアップ",
        description: "起動処理中に音声認識モデルを事前に読み込み、ウォームアップするかを設定します。",
        advice: "有効にすると初回利用時の待ち時間を短縮できますが、起動時間が延び、リソースも早い段階から使用します。",
      }),
    }),
    "stt.download_root": Object.freeze({
      "zh-CN": Object.freeze({
        title: "语音识别模型目录",
        description: "指定预下载模型或模型缓存所在目录；未填写时使用 Faster-Whisper 的默认缓存。",
        advice: "网络受限时可指向已准备好的本地目录；配置中心不会替你下载模型。",
      }),
      en: Object.freeze({
        title: "Speech recognition model directory",
        description: "Specifies the directory containing predownloaded models or the model cache. When empty, Faster-Whisper uses its default cache.",
        advice: "In a restricted network environment, point to a prepared local directory. Config Studio does not download models for you.",
      }),
      ja: Object.freeze({
        title: "音声認識モデルのディレクトリ",
        description: "事前にダウンロードしたモデル、またはモデルキャッシュを置くディレクトリを指定します。空欄の場合は Faster-Whisper の既定キャッシュが使われます。",
        advice: "ネットワークが制限されている場合は、準備済みのローカルディレクトリを指定できます。Config Studio はモデルを自動でダウンロードしません。",
      }),
    }),
    "screen.enabled": Object.freeze({
      "zh-CN": Object.freeze({
        title: "启用屏幕理解",
        description: "决定下次启动是否提供本地屏幕截图分析能力。",
        advice: "关闭可避免加载相关视觉模型，但看屏和依赖它的工具将不可用。",
      }),
      en: Object.freeze({
        title: "Enable screen understanding",
        description: "Controls whether local screenshot analysis is available after the next launch.",
        advice: "Disable this to avoid loading the vision model; screen inspection and dependent tools will then be unavailable.",
      }),
      ja: Object.freeze({
        title: "画面認識を有効にする",
        description: "次回起動時に、ローカルでのスクリーンショット解析を利用できるようにするかを設定します。",
        advice: "無効にすると視覚モデルの読み込みを避けられますが、画面確認とそれに依存するツールは利用できなくなります。",
      }),
    }),
    "screen.provider": Object.freeze({
      "zh-CN": Object.freeze({
        title: "屏幕理解提供方式",
        description: "决定截图分析由哪一个本地视觉适配器处理。",
        advice: "必须与模型、设备和精度设置兼容；当前链路不应配置为上传截图的服务。",
      }),
      en: Object.freeze({
        title: "Screen-understanding provider",
        description: "Selects the local vision adapter that processes screenshots.",
        advice: "It must be compatible with the selected model, device, and precision. This pipeline should not be configured to upload screenshots.",
      }),
      ja: Object.freeze({
        title: "画面認識の処理方式",
        description: "スクリーンショットを処理するローカル視覚アダプターを選択します。",
        advice: "選択したモデル、デバイス、精度との互換性が必要です。この処理経路をスクリーンショットの外部送信に使わないでください。",
      }),
    }),
    "screen.model_id": Object.freeze({
      "zh-CN": Object.freeze({
        title: "屏幕理解模型",
        description: "指定本地屏幕分析使用的视觉模型标识。",
        advice: "更换模型可能改变显存需求、输出格式和识别效果。",
      }),
      en: Object.freeze({
        title: "Screen-understanding model",
        description: "Specifies the vision model identifier used for local screen analysis.",
        advice: "Changing the model may affect VRAM requirements, output format, and recognition quality.",
      }),
      ja: Object.freeze({
        title: "画面認識モデル",
        description: "ローカルでの画面解析に使用する視覚モデルの識別子を指定します。",
        advice: "モデルを変更すると、必要なVRAM、出力形式、認識品質が変わる場合があります。",
      }),
    }),
    "screen.revision": Object.freeze({
      "zh-CN": Object.freeze({
        title: "屏幕模型版本",
        description: "固定屏幕理解模型使用的具体版本，避免上游更新导致行为悄然变化。",
        advice: "只有在验证过新版本兼容性后才建议修改。",
      }),
      en: Object.freeze({
        title: "Screen model revision",
        description: "Pins the screen-understanding model to a specific revision so upstream updates do not silently change its behavior.",
        advice: "Change this only after verifying that the new revision is compatible.",
      }),
      ja: Object.freeze({
        title: "画面認識モデルのバージョン",
        description: "上流の更新で動作が予期せず変わらないよう、使用するモデルのバージョンを固定します。",
        advice: "新しいバージョンの互換性を確認した後にのみ変更してください。",
      }),
    }),
    "screen.device": Object.freeze({
      "zh-CN": Object.freeze({
        title: "屏幕理解运行设备",
        description: "决定屏幕视觉模型在哪个计算设备上运行。",
        advice: "视觉模型通常适合显卡；改为处理器会明显降低速度。",
      }),
      en: Object.freeze({
        title: "Screen-understanding device",
        description: "Selects the compute device used to run the screen vision model.",
        advice: "Vision models generally perform best on a GPU; using a CPU can be substantially slower.",
      }),
      ja: Object.freeze({
        title: "画面認識の実行デバイス",
        description: "画面認識モデルを実行する計算デバイスを選択します。",
        advice: "視覚モデルは通常GPUでの実行に適しています。CPUに変更すると大幅に遅くなる場合があります。",
      }),
    }),
    "screen.dtype": Object.freeze({
      "zh-CN": Object.freeze({
        title: "屏幕模型计算精度",
        description: "控制屏幕视觉模型加载和推理时使用的数值精度。",
        advice: "较低精度可节省显存，但需确认硬件支持并观察识别质量。",
      }),
      en: Object.freeze({
        title: "Screen model precision",
        description: "Controls the numeric precision used when loading and running the screen vision model.",
        advice: "Lower precision can reduce VRAM use, but confirm hardware support and check recognition quality.",
      }),
      ja: Object.freeze({
        title: "画面認識モデルの計算精度",
        description: "画面認識モデルの読み込みと推論に使用する数値精度を設定します。",
        advice: "低い精度ではVRAM使用量を抑えられますが、ハードウェア対応状況と認識品質を確認してください。",
      }),
    }),
    "screen.max_side": Object.freeze({
      "zh-CN": Object.freeze({
        title: "屏幕图像最大边长",
        description: "截图送入视觉模型前会按此上限缩放最长边。",
        advice: "数值越大保留的细节越多，但推理更慢、显存占用也更高；一般保持默认值。",
      }),
      en: Object.freeze({
        title: "Maximum screen-image side length",
        description: "Limits the longest side of a screenshot before it is sent to the vision model.",
        advice: "Larger values preserve more detail but take longer and use more VRAM. The default is suitable for most cases.",
      }),
      ja: Object.freeze({
        title: "画面画像の最大辺長",
        description: "視覚モデルへ渡す前に、スクリーンショットの長辺をこの上限まで縮小します。",
        advice: "値を大きくすると細部を保てますが、処理時間とVRAM使用量が増えます。通常は既定値を推奨します。",
      }),
    }),
    "screen.reasoning": Object.freeze({
      "zh-CN": Object.freeze({
        title: "启用屏幕模型推理模式",
        description: "决定支持该能力的屏幕模型是否使用额外推理过程分析画面。",
        advice: "可能提升复杂画面理解，但会增加响应时间。",
      }),
      en: Object.freeze({
        title: "Enable screen-model reasoning",
        description: "Controls whether supported screen models use an additional reasoning process to analyze the image.",
        advice: "This may improve understanding of complex screens, but it also increases response time.",
      }),
      ja: Object.freeze({
        title: "画面認識モデルの推論モードを有効にする",
        description: "対応する画面認識モデルで、画像解析時に追加の推論処理を使用するかを設定します。",
        advice: "複雑な画面の理解が向上する場合がありますが、応答時間も長くなります。",
      }),
    }),
    "screen.preload": Object.freeze({
      "zh-CN": Object.freeze({
        title: "启动时预加载屏幕模型",
        description: "决定下次启动时是否提前加载屏幕理解模型。",
        advice: "开启可减少第一次看屏等待，但会延长启动并提前占用显存。",
      }),
      en: Object.freeze({
        title: "Preload the screen model at startup",
        description: "Controls whether the screen-understanding model is loaded in advance on the next launch.",
        advice: "Enabling this reduces the first screen-inspection delay, but lengthens startup and reserves VRAM earlier.",
      }),
      ja: Object.freeze({
        title: "起動時に画面認識モデルを事前読み込みする",
        description: "次回起動時に画面認識モデルをあらかじめ読み込むかを設定します。",
        advice: "有効にすると最初の画面確認が速くなりますが、起動時間が延び、早い段階からVRAMを使用します。",
      }),
    }),
    "screen.ocr_enabled": Object.freeze({
      "zh-CN": Object.freeze({
        title: "在屏幕分析中启用文字识别（OCR）",
        description: "决定屏幕观察结果是否同时包含本地文字识别内容。",
        advice: "关闭可减少部分计算，但含文字界面的理解能力会下降。",
      }),
      en: Object.freeze({
        title: "Enable text recognition (OCR) in screen analysis",
        description: "Controls whether screen observations also include text recognized locally from the image.",
        advice: "Disabling this can reduce some processing, but lowers understanding of text-heavy interfaces.",
      }),
      ja: Object.freeze({
        title: "画面解析で文字認識（OCR）を有効にする",
        description: "画面の観察結果に、画像からローカルで認識した文字も含めるかを設定します。",
        advice: "無効にすると一部の処理を減らせますが、文字の多い画面を理解しにくくなります。",
      }),
    }),
    "screen.ocr_engine": Object.freeze({
      "zh-CN": Object.freeze({
        title: "屏幕结果中的文字识别标签",
        description: "记录屏幕分析结果所使用的文字识别引擎标签；真正的 OCR 实现由 OCR 提供方式决定。",
        advice: "这是描述性标签，不要把它误当成切换 OCR 后端的设置。",
      }),
      en: Object.freeze({
        title: "Text-recognition label in screen results",
        description: "Records the text-recognition engine label shown in screen-analysis results; the OCR provider setting selects the actual implementation.",
        advice: "This is a descriptive label, not the setting that switches the OCR backend.",
      }),
      ja: Object.freeze({
        title: "画面解析結果の文字認識ラベル",
        description: "画面解析結果に記録する文字認識エンジンのラベルです。実際のOCR実装はOCRの処理方式で選択します。",
        advice: "これは説明用のラベルであり、OCRバックエンドを切り替える設定ではありません。",
      }),
    }),
    "screen.capture_format": Object.freeze({
      "zh-CN": Object.freeze({
        title: "屏幕截图格式",
        description: "决定本地屏幕分析管线内部使用的截图编码格式。",
        advice: "当前负责模块只支持既定格式，保持当前值最稳妥。",
      }),
      en: Object.freeze({
        title: "Screenshot format",
        description: "Selects the image encoding used internally by the local screen-analysis pipeline.",
        advice: "The current owner supports only the expected format, so retaining the current value is safest.",
      }),
      ja: Object.freeze({
        title: "スクリーンショット形式",
        description: "ローカルの画面解析処理で内部的に使用する画像形式を選択します。",
        advice: "現在の管理モジュールは所定の形式のみをサポートするため、現在の値を維持するのが安全です。",
      }),
    }),
    "screen.infer_timeout_sec": Object.freeze({
      "zh-CN": Object.freeze({
        title: "屏幕分析超时时间",
        description: "限制一次屏幕视觉推理最多等待多少秒。",
        advice: "过短会让慢设备频繁超时，过长则会让失败任务占用资源更久。",
      }),
      en: Object.freeze({
        title: "Screen-analysis timeout",
        description: "Limits how many seconds one screen-vision inference may take.",
        advice: "A value that is too low causes frequent timeouts on slower devices; one that is too high keeps failed work occupied longer.",
      }),
      ja: Object.freeze({
        title: "画面解析のタイムアウト",
        description: "1回の画面認識推論を待機する最大時間を秒単位で設定します。",
        advice: "短すぎると低速な機器で頻繁にタイムアウトし、長すぎると失敗した処理が長く残ります。",
      }),
    }),
    "screen.log_timing": Object.freeze({
      "zh-CN": Object.freeze({
        title: "记录屏幕分析耗时",
        description: "决定日志是否记录屏幕截图、文字识别和视觉推理的耗时信息。",
        advice: "排查性能问题时建议开启；它不会把截图内容写入日志。",
      }),
      en: Object.freeze({
        title: "Log screen-analysis timings",
        description: "Controls whether logs include timing information for screen capture, text recognition, and vision inference.",
        advice: "Enable this when investigating performance. It does not write screenshot contents to the log.",
      }),
      ja: Object.freeze({
        title: "画面解析の処理時間を記録する",
        description: "画面取得、文字認識、視覚推論にかかった時間をログへ記録するかを設定します。",
        advice: "性能問題の調査時には有効化を推奨します。スクリーンショットの内容はログに記録されません。",
      }),
    }),
    "screen.debug_save_images": Object.freeze({
      "zh-CN": Object.freeze({
        title: "保存屏幕调试图片",
        description: "决定调试模式是否把处理过的截图保存到本地用于排查。",
        advice: "截图可能包含隐私内容，除非明确调试需要，否则应保持关闭。",
      }),
      en: Object.freeze({
        title: "Save screen-debug images",
        description: "Controls whether debug mode saves processed screenshots locally for troubleshooting.",
        advice: "Screenshots may contain private information. Keep this disabled unless the images are explicitly needed for debugging.",
      }),
      ja: Object.freeze({
        title: "画面デバッグ画像を保存する",
        description: "デバッグ時に、処理済みのスクリーンショットを調査用としてローカルへ保存するかを設定します。",
        advice: "画像には個人情報が含まれる場合があります。明確なデバッグ目的がない限り無効のままにしてください。",
      }),
    }),
    "ocr.provider": Object.freeze({
      "zh-CN": Object.freeze({
        title: "文字识别（OCR）提供方式",
        description: "决定 galgame 与看屏链路实际使用哪一个本地文字识别实现。",
        advice: "这是切换 OCR 后端的正式设置；修改后应验证识别质量和运行库兼容性。",
      }),
      en: Object.freeze({
        title: "Text-recognition (OCR) provider",
        description: "Selects the local text-recognition implementation used by the galgame and screen-inspection pipelines.",
        advice: "This is the official OCR backend selector. After changing it, verify recognition quality and runtime compatibility.",
      }),
      ja: Object.freeze({
        title: "文字認識（OCR）の処理方式",
        description: "Galgameと画面確認で実際に使用するローカル文字認識実装を選択します。",
        advice: "OCRバックエンドを切り替える正式な設定です。変更後は認識品質と実行環境の互換性を確認してください。",
      }),
    }),
    "ocr.fallback_provider": Object.freeze({
      "zh-CN": Object.freeze({
        title: "文字识别备用方式",
        description: "主 OCR 实现不可用时，指定回退使用的本地识别实现。",
        advice: "备用方式应经过安装和基础识别验证，否则回退仍会失败。",
      }),
      en: Object.freeze({
        title: "Fallback text-recognition provider",
        description: "Selects the local recognition implementation used when the primary OCR provider is unavailable.",
        advice: "Install and verify the fallback provider first; otherwise fallback attempts will still fail.",
      }),
      ja: Object.freeze({
        title: "文字認識の予備処理方式",
        description: "主なOCR実装が利用できない場合に使用するローカル認識実装を選択します。",
        advice: "予備の実装も事前にインストールして基本動作を確認してください。未確認の場合、切り替えても失敗します。",
      }),
    }),
    "ocr.trt.fp16": Object.freeze({
      "zh-CN": Object.freeze({
        title: "TensorRT 半精度模式",
        description: "决定 OCR 的 TensorRT 执行路径是否使用 FP16 精度。",
        advice: "可降低显存并提升速度，但启用前应确认设备支持并做识别一致性验证。",
      }),
      en: Object.freeze({
        title: "TensorRT half-precision mode",
        description: "Controls whether the TensorRT OCR execution path uses FP16 precision.",
        advice: "This can reduce VRAM use and improve speed, but confirm device support and recognition consistency before enabling it.",
      }),
      ja: Object.freeze({
        title: "TensorRT半精度モード",
        description: "OCRのTensorRT実行経路でFP16精度を使用するかを設定します。",
        advice: "VRAM使用量を減らし高速化できる場合がありますが、有効化前に機器の対応と認識結果の一貫性を確認してください。",
      }),
    }),
    "ocr.trt.engine_cache_dir": Object.freeze({
      "zh-CN": Object.freeze({
        title: "TensorRT 引擎缓存目录",
        description: "指定 OCR TensorRT 编译引擎和缓存文件的保存目录。",
        advice: "目录需要可写且空间充足；清空后下一次运行可能重新编译并等待较久。",
      }),
      en: Object.freeze({
        title: "TensorRT engine cache directory",
        description: "Specifies where compiled OCR TensorRT engines and cache files are stored.",
        advice: "The directory must be writable and have sufficient space. Clearing it may cause a lengthy rebuild on the next run.",
      }),
      ja: Object.freeze({
        title: "TensorRTエンジンのキャッシュフォルダー",
        description: "OCR用TensorRTエンジンのコンパイル結果とキャッシュを保存する場所を指定します。",
        advice: "書き込み可能で十分な空き容量が必要です。消去すると次回実行時に再構築され、時間がかかる場合があります。",
      }),
    }),
    "ocr.trt.timing_cache": Object.freeze({
      "zh-CN": Object.freeze({
        title: "启用 TensorRT 时序缓存",
        description: "决定 TensorRT 是否保存并复用算子调优结果。",
        advice: "通常应保持开启，以减少重复构建引擎的时间。",
      }),
      en: Object.freeze({
        title: "Enable the TensorRT timing cache",
        description: "Controls whether TensorRT stores and reuses operator-tuning results.",
        advice: "Keep this enabled in most cases to avoid repeating engine-build tuning work.",
      }),
      ja: Object.freeze({
        title: "TensorRTタイミングキャッシュを有効にする",
        description: "TensorRTが演算の調整結果を保存して再利用するかを設定します。",
        advice: "エンジン構築時の調整を繰り返さないため、通常は有効のままにしてください。",
      }),
    }),
    "ocr.trt.profiles": Object.freeze({
      "zh-CN": Object.freeze({
        title: "TensorRT 输入尺寸配置",
        description: "为 OCR 检测和识别网络提供最小、常用和最大输入尺寸范围。",
        advice: "这是高级结构化设置；错误尺寸会导致引擎构建或推理失败。",
      }),
      en: Object.freeze({
        title: "TensorRT input-size profiles",
        description: "Defines minimum, typical, and maximum input-size ranges for OCR detection and recognition networks.",
        advice: "This is an advanced structured setting. Invalid dimensions can cause engine building or inference to fail.",
      }),
      ja: Object.freeze({
        title: "TensorRT入力サイズ設定",
        description: "OCRの検出・認識ネットワーク向けに、最小、標準、最大の入力サイズ範囲を定義します。",
        advice: "高度な構造化設定です。不正なサイズを指定すると、エンジン構築や推論が失敗する場合があります。",
      }),
    }),
    "ocr.trt.device_id": Object.freeze({
      "zh-CN": Object.freeze({
        title: "TensorRT 显卡编号",
        description: "指定 OCR TensorRT 执行路径使用哪一块显卡。",
        advice: "单显卡设备通常保持当前值；多显卡时应与实际设备编号对应。",
      }),
      en: Object.freeze({
        title: "TensorRT GPU index",
        description: "Selects which GPU the TensorRT OCR execution path uses.",
        advice: "Keep the current value on single-GPU systems. On multi-GPU systems, match it to the intended device index.",
      }),
      ja: Object.freeze({
        title: "TensorRTのGPU番号",
        description: "OCRのTensorRT実行経路で使用するGPUを指定します。",
        advice: "GPUが1台の場合は通常そのままにします。複数ある場合は、使用する機器の番号に合わせてください。",
      }),
    }),
    "platform.os": Object.freeze({
      "zh-CN": Object.freeze({
        title: "桌面运行平台选择",
        description: "决定桌面运行时装配 Linux 或 Windows 的窗口、截图和启动器实现；自动值会按平台判断。",
        advice: "这是桌面功能选择，不影响配置中心自身的安全平台检测；通常保持自动。",
      }),
      en: Object.freeze({
        title: "Desktop runtime platform",
        description: "Selects the Linux or Windows windowing, capture, and launcher implementations for the desktop runtime; the automatic option detects the platform.",
        advice: "This controls desktop feature assembly, not Config Studio's security-platform detection. Automatic selection is recommended.",
      }),
      ja: Object.freeze({
        title: "デスクトップ実行環境の選択",
        description: "デスクトップ実行時に使用するLinuxまたはWindows向けのウィンドウ、画面取得、起動機能を選択します。自動では実行環境を判定します。",
        advice: "これはデスクトップ機能の構成用で、設定センターの安全確認には影響しません。通常は自動を推奨します。",
      }),
    }),
    "anime.enabled": Object.freeze({
      "zh-CN": Object.freeze({
        title: "启用看动漫功能",
        description: "决定下次启动是否装配番剧搜索、下载与播放相关能力。",
        advice: "关闭时不会提供相关工具；开启前应准备播放器和所需下载组件。",
      }),
      en: Object.freeze({
        title: "Enable anime watching",
        description: "Controls whether anime search, download, and playback features are assembled on the next launch.",
        advice: "When disabled, the related tools are unavailable. Before enabling it, prepare a media player and the required download components.",
      }),
      ja: Object.freeze({
        title: "アニメ視聴機能を有効にする",
        description: "次回起動時に、アニメの検索、ダウンロード、再生機能を組み込むかを設定します。",
        advice: "無効にすると関連ツールは利用できません。有効化する前に、動画プレイヤーと必要なダウンロード機能を準備してください。",
      }),
    }),
    "anime.download_dir": Object.freeze({
      "zh-CN": Object.freeze({
        title: "动漫下载目录",
        description: "指定番剧文件和下载中间内容保存到哪个目录。",
        advice: "请确保目录所在磁盘空间充足，并避免指向包含重要文件的目录。",
      }),
      en: Object.freeze({
        title: "Anime download directory",
        description: "Specifies where anime files and intermediate download data are stored.",
        advice: "Make sure the disk has enough free space, and do not point this at a directory containing important files.",
      }),
      ja: Object.freeze({
        title: "アニメのダウンロード先",
        description: "アニメファイルとダウンロード途中のデータを保存するフォルダーを指定します。",
        advice: "十分な空き容量があることを確認し、重要なファイルが入ったフォルダーは指定しないでください。",
      }),
    }),
    "anime.player_command": Object.freeze({
      "zh-CN": Object.freeze({
        title: "视频播放器命令",
        description: "指定播放已下载番剧时调用的播放器程序名称。",
        advice: "程序必须已安装并可由 Spica 正常启动；这里不是任意系统命令入口。",
      }),
      en: Object.freeze({
        title: "Video player command",
        description: "Specifies the media-player program used to play downloaded anime.",
        advice: "The program must be installed and launchable by Spica. This field is not an arbitrary system-command entry point.",
      }),
      ja: Object.freeze({
        title: "動画プレイヤーのコマンド",
        description: "ダウンロード済みのアニメを再生する動画プレイヤーのプログラム名を指定します。",
        advice: "プログラムがインストール済みで、Spicaから起動できる必要があります。任意のシステムコマンドを入力する欄ではありません。",
      }),
    }),
    "anime.bilibili_spaces": Object.freeze({
      "zh-CN": Object.freeze({
        title: "Bilibili 番剧来源空间",
        description: "列出允许搜索番剧内容的 Bilibili 空间标识。",
        advice: "这是结构化列表；仅添加可信且确实用于番剧内容的来源。",
      }),
      en: Object.freeze({
        title: "Bilibili anime-source spaces",
        description: "Lists the Bilibili space identifiers permitted as anime search sources.",
        advice: "This is a structured list. Add only trusted spaces that actually provide anime content.",
      }),
      ja: Object.freeze({
        title: "Bilibiliのアニメ取得元スペース",
        description: "アニメ検索の取得元として許可するBilibiliスペースの識別子を一覧で指定します。",
        advice: "構造化された一覧です。信頼でき、実際にアニメを提供している取得元だけを追加してください。",
      }),
    }),
    "anime.mikan_base_urls": Object.freeze({
      "zh-CN": Object.freeze({
        title: "Mikan 番剧来源地址",
        description: "列出搜索 Mikan 番剧信息时允许使用的基础地址。",
        advice: "地址必须是可访问且可信的站点；不可用来源会拖慢搜索。",
      }),
      en: Object.freeze({
        title: "Mikan anime-source addresses",
        description: "Lists the base addresses permitted when searching Mikan for anime information.",
        advice: "Use only accessible, trusted sites. Unavailable sources can slow down searches.",
      }),
      ja: Object.freeze({
        title: "Mikanのアニメ取得元アドレス",
        description: "Mikanでアニメ情報を検索する際に使用できる基本アドレスを一覧で指定します。",
        advice: "アクセス可能で信頼できるサイトだけを指定してください。利用できない取得元は検索を遅くします。",
      }),
    }),
    "anime.quality": Object.freeze({
      "zh-CN": Object.freeze({
        title: "动漫首选画质",
        description: "指定搜索和选择番剧资源时优先匹配的画质。",
        advice: "更高画质会占用更多下载时间、带宽和磁盘空间。",
      }),
      en: Object.freeze({
        title: "Preferred anime quality",
        description: "Sets the video quality preferred when searching for and selecting anime releases.",
        advice: "Higher quality requires more download time, bandwidth, and disk space.",
      }),
      ja: Object.freeze({
        title: "アニメの優先画質",
        description: "アニメを検索・選択する際に優先する画質を設定します。",
        advice: "高い画質ほど、ダウンロード時間、通信量、保存容量が多く必要です。",
      }),
    }),
    "anime.subtitle_preference": Object.freeze({
      "zh-CN": Object.freeze({
        title: "字幕偏好顺序",
        description: "按顺序列出选择番剧资源时优先匹配的字幕类型。",
        advice: "越靠前优先级越高；没有匹配时仍可能选择其他可用资源。",
      }),
      en: Object.freeze({
        title: "Subtitle preference order",
        description: "Lists subtitle types in the order they should be preferred when selecting an anime release.",
        advice: "Items near the top have higher priority. If none match, another available release may still be selected.",
      }),
      ja: Object.freeze({
        title: "字幕の優先順",
        description: "アニメを選択する際に優先する字幕の種類を順番に指定します。",
        advice: "上にある項目ほど優先されます。一致するものがない場合は、別の利用可能な候補が選ばれることがあります。",
      }),
    }),
    "anime.source_timeout_seconds": Object.freeze({
      "zh-CN": Object.freeze({
        title: "单个番剧来源超时时间",
        description: "限制查询一个番剧来源时最多等待多少秒。",
        advice: "网络较慢时可适当增加；过大会延长无响应来源造成的等待。",
      }),
      en: Object.freeze({
        title: "Per-source anime timeout",
        description: "Limits how many seconds a query to one anime source may take.",
        advice: "Increase this moderately on slow networks. A large value makes unresponsive sources delay the search longer.",
      }),
      ja: Object.freeze({
        title: "アニメ取得元ごとのタイムアウト",
        description: "1つのアニメ取得元への問い合わせを待つ最大時間を秒単位で設定します。",
        advice: "通信が遅い場合は適度に増やせます。大きすぎると、応答しない取得元による待ち時間が長くなります。",
      }),
    }),
    "anime.resolve_budget_seconds": Object.freeze({
      "zh-CN": Object.freeze({
        title: "番剧解析总时间预算",
        description: "限制一次番剧来源解析流程允许使用的总时间。",
        advice: "应不小于单来源超时；来源较多时需要留出足够预算。",
      }),
      en: Object.freeze({
        title: "Total anime-resolution time budget",
        description: "Limits the total time available to one anime-source resolution process.",
        advice: "This should not be lower than the per-source timeout. Allow enough time when several sources are configured.",
      }),
      ja: Object.freeze({
        title: "アニメ取得処理の合計時間上限",
        description: "1回のアニメ取得元の解析処理に使用できる合計時間を制限します。",
        advice: "取得元ごとのタイムアウト以上に設定し、取得元が多い場合は十分な時間を確保してください。",
      }),
    }),
    "anime.qbittorrent_url": Object.freeze({
      "zh-CN": Object.freeze({
        title: "qBittorrent 服务地址",
        description: "指定连接 qBittorrent Web API 的本机或受信任服务地址。",
        advice: "确认地址仅指向你控制的实例；密码应放在正确的密钥槽位。",
      }),
      en: Object.freeze({
        title: "qBittorrent service address",
        description: "Specifies the local or trusted qBittorrent Web API service address.",
        advice: "Make sure it points only to an instance you control. Store the password in the designated secret slot.",
      }),
      ja: Object.freeze({
        title: "qBittorrentサービスのアドレス",
        description: "接続するローカルまたは信頼済みのqBittorrent Web APIアドレスを指定します。",
        advice: "自分が管理する環境だけを指定してください。パスワードは所定のシークレット欄に保存します。",
      }),
    }),
    "anime.qbittorrent_username": Object.freeze({
      "zh-CN": Object.freeze({
        title: "qBittorrent 用户名",
        description: "指定登录 qBittorrent Web API 使用的用户名。",
        advice: "这里只填写用户名，不要把密码写入 app.yaml。",
      }),
      en: Object.freeze({
        title: "qBittorrent username",
        description: "Specifies the username used to sign in to the qBittorrent Web API.",
        advice: "Enter only the username here. Do not store the password in app.yaml.",
      }),
      ja: Object.freeze({
        title: "qBittorrentのユーザー名",
        description: "qBittorrent Web APIへのログインに使用するユーザー名を指定します。",
        advice: "ここにはユーザー名だけを入力し、パスワードをapp.yamlに保存しないでください。",
      }),
    }),
    "anime.auto_play_threshold_seconds": Object.freeze({
      "zh-CN": Object.freeze({
        title: "自动播放等待阈值",
        description: "预计可播放等待时间不超过此值时允许自动开始播放，单位为秒。",
        advice: "降低可减少意外自动播放，提高则更积极地等待并自动打开播放器。",
      }),
      en: Object.freeze({
        title: "Automatic-play wait threshold",
        description: "Allows playback to start automatically when the estimated wait until playable is no longer than this many seconds.",
        advice: "Lower this to reduce unexpected automatic playback; raise it to wait more readily and open the player automatically.",
      }),
      ja: Object.freeze({
        title: "自動再生の待機しきい値",
        description: "再生可能になるまでの予想待機時間がこの秒数以内の場合に、自動再生を許可します。",
        advice: "意図しない自動再生を減らすには小さくし、より積極的に待機してプレイヤーを開くには大きくします。",
      }),
    }),
    "anime.qbittorrent_poll_seconds": Object.freeze({
      "zh-CN": Object.freeze({
        title: "下载进度轮询间隔",
        description: "控制检查 qBittorrent 下载进度的时间间隔，单位为秒。",
        advice: "间隔过短会增加请求频率，过长则会让进度和完成提示更新较慢。",
      }),
      en: Object.freeze({
        title: "Download-progress polling interval",
        description: "Sets how often qBittorrent download progress is checked, in seconds.",
        advice: "Very short intervals increase request frequency; long intervals delay progress and completion updates.",
      }),
      ja: Object.freeze({
        title: "ダウンロード進捗の確認間隔",
        description: "qBittorrentのダウンロード進捗を確認する間隔を秒単位で設定します。",
        advice: "短すぎると問い合わせ回数が増え、長すぎると進捗や完了通知の更新が遅くなります。",
      }),
    }),
    "anime.stall_timeout_minutes": Object.freeze({
      "zh-CN": Object.freeze({
        title: "下载停滞判定时间",
        description: "下载在多长时间内没有有效进展后视为停滞，单位为分钟。",
        advice: "网络不稳定时可适当增加，过大则会延迟发现真正失败的任务。",
      }),
      en: Object.freeze({
        title: "Download-stall timeout",
        description: "Sets how many minutes without meaningful progress cause a download to be considered stalled.",
        advice: "Increase this moderately on unstable networks. A very large value delays detection of genuinely failed downloads.",
      }),
      ja: Object.freeze({
        title: "ダウンロード停滞の判定時間",
        description: "有効な進捗がない状態が何分続くとダウンロード停滞と判断するかを設定します。",
        advice: "通信が不安定な場合は適度に増やせます。大きすぎると、実際の失敗を検出するまで時間がかかります。",
      }),
    }),
    "anime.ytdlp_format": Object.freeze({
      "zh-CN": Object.freeze({
        title: "yt-dlp 下载格式规则",
        description: "指定从视频来源选择画面、音频和清晰度时使用的 yt-dlp 格式表达式。",
        advice: "这是高级原始规则；错误表达式会导致下载失败，不确定时保持当前值。",
      }),
      en: Object.freeze({
        title: "yt-dlp format rule",
        description: "Specifies the yt-dlp format expression used to select video, audio, and resolution from video sources.",
        advice: "This is an advanced raw rule. An invalid expression causes downloads to fail, so keep the current value if unsure.",
      }),
      ja: Object.freeze({
        title: "yt-dlpのダウンロード形式ルール",
        description: "動画取得元から映像、音声、解像度を選択するためのyt-dlp形式式を指定します。",
        advice: "高度な生の設定です。不正な式ではダウンロードに失敗するため、不明な場合は現在の値を維持してください。",
      }),
    }),
    "anime.ytdlp_min_rate_kib_per_second": Object.freeze({
      "zh-CN": Object.freeze({
        title: "yt-dlp 最低下载速度",
        description: "下载速度持续低于该值时可触发低速处理，单位为 KiB/s；具体零值语义由负责模块决定。",
        advice: "网络较慢时不要设得过高，否则正常下载也可能被判定为异常。",
      }),
      en: Object.freeze({
        title: "Minimum yt-dlp download rate",
        description: "Sets the KiB/s rate below which slow-download handling may be triggered; the owner defines the exact meaning of zero.",
        advice: "Do not set this too high on slower networks, or normal downloads may be treated as abnormal.",
      }),
      ja: Object.freeze({
        title: "yt-dlpの最低ダウンロード速度",
        description: "低速時の処理を開始する基準をKiB/s単位で設定します。0の詳しい意味は管理モジュールの規則に従います。",
        advice: "通信が遅い環境では高く設定しすぎないでください。通常のダウンロードまで異常と判断される場合があります。",
      }),
    }),
    "anime.cookies_file": Object.freeze({
      "zh-CN": Object.freeze({
        title: "视频站点 Cookies 文件",
        description: "指定 yt-dlp 读取登录 Cookies 的文件位置，用于需要登录的清晰度或内容。",
        advice: "该文件可能包含敏感会话信息，应限制权限且不要提交到版本库。",
      }),
      en: Object.freeze({
        title: "Video-site Cookies file",
        description: "Specifies the Cookies file that yt-dlp reads for quality levels or content that require sign-in.",
        advice: "This file may contain sensitive session information. Restrict its permissions and never commit it to version control.",
      }),
      ja: Object.freeze({
        title: "動画サイトのCookieファイル",
        description: "ログインが必要な画質やコンテンツに対して、yt-dlpが読み込むCookieファイルを指定します。",
        advice: "機密性の高いセッション情報を含む場合があります。権限を制限し、バージョン管理には登録しないでください。",
      }),
    }),
    "anime.library_file": Object.freeze({
      "zh-CN": Object.freeze({
        title: "动漫资料库文件",
        description: "指定保存番剧资料、下载状态和播放记录的本地资料库文件。",
        advice: "不要手工并发编辑该文件；迁移路径前应先备份现有资料。",
      }),
      en: Object.freeze({
        title: "Anime library file",
        description: "Specifies the local library file that stores anime metadata, download state, and playback history.",
        advice: "Do not edit this file concurrently by hand. Back up the existing library before moving it.",
      }),
      ja: Object.freeze({
        title: "アニメライブラリーファイル",
        description: "アニメ情報、ダウンロード状態、再生履歴を保存するローカルライブラリーファイルを指定します。",
        advice: "このファイルを手作業で同時編集しないでください。保存場所を移す前に既存データをバックアップしてください。",
      }),
    }),
    "song": Object.freeze({
      "zh-CN": Object.freeze({
        title: "唱歌功能高级配置",
        description: "保存唱歌负责模块的动态覆盖结构；其中只有具备正式负责模块契约的固定项可安全编辑。",
        advice: "自定义声音和模型键保持原名；没有类型化规则的字段仅供查看。",
      }),
      en: Object.freeze({
        title: "Advanced singing configuration",
        description: "Contains dynamic overrides for the singing owner; only fixed items with an official owner contract can be edited safely.",
        advice: "Custom voice and model keys retain their original names. Fields without a typed contract are shown read-only.",
      }),
      ja: Object.freeze({
        title: "歌唱機能の詳細設定",
        description: "歌唱機能の管理モジュール向けの動的な上書き設定です。正式な管理規則がある固定項目だけを安全に編集できます。",
        advice: "独自の音声・モデルキーは元の名前で表示されます。型付き規則のない項目は読み取り専用です。",
      }),
    }),
    "plugins": Object.freeze({
      "zh-CN": Object.freeze({
        title: "插件清单",
        description: "列出下次启动准备启用或禁用的插件名称及状态。",
        advice: "只配置已安装且可信的插件；配置中心不会导入插件来验证内容。",
      }),
      en: Object.freeze({
        title: "Plugin list",
        description: "Lists the plugins to be enabled or disabled on the next launch.",
        advice: "Configure only installed, trusted plugins. Config Studio does not import plugins to inspect them.",
      }),
      ja: Object.freeze({
        title: "プラグイン一覧",
        description: "次回起動時に有効または無効にするプラグイン名と状態を一覧で指定します。",
        advice: "インストール済みで信頼できるプラグインだけを設定してください。設定センターは確認のためにプラグインを読み込みません。",
      }),
    }),
    "max_tool_rounds": Object.freeze({
      "zh-CN": Object.freeze({
        title: "单轮最大工具调用轮数",
        description: "限制一次对话中模型连续调用工具并继续推理的最大轮数。",
        advice: "提高可支持更长的工具链，但会增加延迟、调用次数和副作用机会。",
      }),
      en: Object.freeze({
        title: "Maximum tool-call rounds per turn",
        description: "Limits how many consecutive rounds of tool calls and follow-up reasoning the model may perform in one conversation turn.",
        advice: "A higher value supports longer tool chains, but increases latency, call volume, and opportunities for side effects.",
      }),
      ja: Object.freeze({
        title: "1ターンの最大ツール呼び出し回数",
        description: "1回の対話で、モデルがツール呼び出しと追加推論を連続して行える最大回数を制限します。",
        advice: "値を増やすと長いツール連携が可能になりますが、待ち時間、呼び出し回数、副作用の機会も増えます。",
      }),
    }),
  });

  const KNOWN_DYNAMIC_FIELD_PRESENTATIONS = Object.freeze({
    "song[\"enabled\"]": Object.freeze({
      "zh-CN": Object.freeze({
        title: "启用点歌与演唱",
        description: "决定下次启动是否提供点歌、歌曲处理和演唱能力。",
        advice: "关闭后不会装配唱歌链路；其他唱歌动态字段保持原始名称并只读展示。",
      }),
      en: Object.freeze({
        title: "Enable song requests and singing",
        description: "Controls whether song requests, song processing, and singing are available after the next launch.",
        advice: "When disabled, the singing pipeline is not assembled. Other dynamic singing fields keep their original names and remain read-only.",
      }),
      ja: Object.freeze({
        title: "曲のリクエストと歌唱を有効にする",
        description: "次回起動時に、曲のリクエスト、楽曲処理、歌唱機能を利用できるようにするかを設定します。",
        advice: "無効にすると歌唱処理は組み込まれません。その他の動的な歌唱項目は元の名前のまま読み取り専用で表示されます。",
      }),
    }),
  });

  const CATEGORY_LABELS = Object.freeze({
    "all": Object.freeze({
      "zh-CN": "全部",
      "en": "All",
      "ja": "すべて",
    }),
    "llm": Object.freeze({
      "zh-CN": "大语言模型（LLM）",
      "en": "Large language model (LLM)",
      "ja": "大規模言語モデル（LLM）",
    }),
    "memory": Object.freeze({
      "zh-CN": "记忆",
      "en": "Memory",
      "ja": "記憶",
    }),
    "character": Object.freeze({
      "zh-CN": "角色与称呼",
      "en": "Character and names",
      "ja": "キャラクターと呼び名",
    }),
    "stream": Object.freeze({
      "zh-CN": "流式播放",
      "en": "Streaming playback",
      "ja": "ストリーミング再生",
    }),
    "galgame": Object.freeze({
      "zh-CN": "Galgame 陪玩",
      "en": "Galgame companion",
      "ja": "Galgame 同伴",
    }),
    "tts": Object.freeze({
      "zh-CN": "语音合成（TTS）",
      "en": "Text-to-speech (TTS)",
      "ja": "音声合成（TTS）",
    }),
    "stt": Object.freeze({
      "zh-CN": "语音识别（STT）",
      "en": "Speech-to-text (STT)",
      "ja": "音声認識（STT）",
    }),
    "screen": Object.freeze({
      "zh-CN": "屏幕理解",
      "en": "Screen understanding",
      "ja": "画面理解",
    }),
    "ocr": Object.freeze({
      "zh-CN": "文字识别（OCR）",
      "en": "Optical character recognition (OCR)",
      "ja": "文字認識（OCR）",
    }),
    "platform": Object.freeze({
      "zh-CN": "运行平台",
      "en": "Runtime platform",
      "ja": "実行プラットフォーム",
    }),
    "anime": Object.freeze({
      "zh-CN": "看动漫",
      "en": "Anime",
      "ja": "アニメ視聴",
    }),
    "song": Object.freeze({
      "zh-CN": "点歌与演唱",
      "en": "Song requests and singing",
      "ja": "曲のリクエストと歌唱",
    }),
    "plugins": Object.freeze({
      "zh-CN": "插件",
      "en": "Plugins",
      "ja": "プラグイン",
    }),
    "max_tool_rounds": Object.freeze({
      "zh-CN": "工具调用",
      "en": "Tool calls",
      "ja": "ツール呼び出し",
    }),
    "other": Object.freeze({
      "zh-CN": "其他",
      "en": "Other",
      "ja": "その他",
    }),
  });

  const CONTROL_LABELS = Object.freeze({
    "switch": Object.freeze({
      "zh-CN": "开关",
      "en": "Switch",
      "ja": "スイッチ",
    }),
    "select": Object.freeze({
      "zh-CN": "单项选择",
      "en": "Single choice",
      "ja": "単一選択",
    }),
    "number": Object.freeze({
      "zh-CN": "数字",
      "en": "Number",
      "ja": "数値",
    }),
    "text": Object.freeze({
      "zh-CN": "文本",
      "en": "Text",
      "ja": "テキスト",
    }),
    "structured": Object.freeze({
      "zh-CN": "结构化数据",
      "en": "Structured data",
      "ja": "構造化データ",
    }),
    "field": Object.freeze({
      "zh-CN": "字段",
      "en": "Field",
      "ja": "項目",
    }),
    "boolean": Object.freeze({
      "zh-CN": "布尔值",
      "en": "Boolean",
      "ja": "真偽値",
    }),
    "integer": Object.freeze({
      "zh-CN": "整数",
      "en": "Integer",
      "ja": "整数",
    }),
    "string": Object.freeze({
      "zh-CN": "文本",
      "en": "Text",
      "ja": "テキスト",
    }),
    "object": Object.freeze({
      "zh-CN": "对象",
      "en": "Object",
      "ja": "オブジェクト",
    }),
    "array": Object.freeze({
      "zh-CN": "列表",
      "en": "List",
      "ja": "リスト",
    }),
    "null": Object.freeze({
      "zh-CN": "空值",
      "en": "Null",
      "ja": "null 値",
    }),
  });

  const SOURCE_LABELS = Object.freeze({
    "env_override": Object.freeze({
      "zh-CN": "环境变量覆盖",
      "en": "Environment override",
      "ja": "環境変数による上書き",
    }),
    "secret_tainted_env_override": Object.freeze({
      "zh-CN": "敏感环境变量覆盖",
      "en": "Sensitive environment override",
      "ja": "機密値を含む環境変数の上書き",
    }),
    "file": Object.freeze({
      "zh-CN": "配置文件",
      "en": "Configuration file",
      "ja": "設定ファイル",
    }),
    "default": Object.freeze({
      "zh-CN": "默认值",
      "en": "Default",
      "ja": "既定値",
    }),
    "unavailable": Object.freeze({
      "zh-CN": "不可用",
      "en": "Unavailable",
      "ja": "利用不可",
    }),
    "legacy_owner_active": Object.freeze({
      "zh-CN": "旧版负责模块",
      "en": "Legacy owner",
      "ja": "旧形式の担当モジュール",
    }),
    "owner_derived": Object.freeze({
      "zh-CN": "负责模块派生",
      "en": "Derived by owner",
      "ja": "担当モジュールが算出",
    }),
    "legacy_document": Object.freeze({
      "zh-CN": "旧版配置文件",
      "en": "Legacy configuration file",
      "ja": "旧形式の設定ファイル",
    }),
    "app_config": Object.freeze({
      "zh-CN": "应用配置",
      "en": "Application configuration",
      "ja": "アプリケーション設定",
    }),
    "default_fallback": Object.freeze({
      "zh-CN": "默认回退",
      "en": "Default fallback",
      "ja": "既定値へのフォールバック",
    }),
    "package_blocked": Object.freeze({
      "zh-CN": "角色包被阻止",
      "en": "Character package blocked",
      "ja": "キャラクターパッケージをブロック",
    }),
    "external_package_unresolved": Object.freeze({
      "zh-CN": "外部角色包未解析",
      "en": "External character package unresolved",
      "ja": "外部キャラクターパッケージを解決できません",
    }),
    "package_override": Object.freeze({
      "zh-CN": "角色包覆盖",
      "en": "Character package override",
      "ja": "キャラクターパッケージによる上書き",
    }),
    "ui_owner_document": Object.freeze({
      "zh-CN": "界面偏好文件",
      "en": "UI preference file",
      "ja": "UI 設定ファイル",
    }),
    "inherited": Object.freeze({
      "zh-CN": "启动进程环境",
      "en": "Startup process environment",
      "ja": "起動プロセスの環境",
    }),
    "repo_dotenv": Object.freeze({
      "zh-CN": "仓库 xiaosan.env",
      "en": "Repository xiaosan.env",
      "ja": "リポジトリの xiaosan.env",
    }),
    "parent_dotenv": Object.freeze({
      "zh-CN": "父目录 xiaosan.env",
      "en": "Parent-directory xiaosan.env",
      "ja": "親ディレクトリの xiaosan.env",
    }),
    "synthetic": Object.freeze({
      "zh-CN": "合成测试环境",
      "en": "Synthetic test environment",
      "ja": "合成テスト環境",
    }),
    "no_override": Object.freeze({
      "zh-CN": "没有环境覆盖",
      "en": "No environment override",
      "ja": "環境変数による上書きなし",
    }),
    "unknown": Object.freeze({
      "zh-CN": "来源未知",
      "en": "Unknown source",
      "ja": "取得元不明",
    }),
  });

  const EFFECT_LABELS = Object.freeze({
    "next_spica_launch": Object.freeze({
      "zh-CN": "下次 Spica 启动生效",
      "en": "Takes effect on the next Spica launch",
      "ja": "次回の Spica 起動時に反映",
    }),
    "owner_mtime_reload": Object.freeze({
      "zh-CN": "负责模块检测到文件更新后重读",
      "en": "Reloaded when the owner detects a file update",
      "ja": "担当モジュールがファイル更新を検知すると再読み込み",
    }),
    "owner_derived_on_next_launch": Object.freeze({
      "zh-CN": "下次启动时由负责模块计算",
      "en": "Calculated by the owner on the next launch",
      "ja": "次回起動時に担当モジュールが算出",
    }),
    "legacy_owner_on_next_launch": Object.freeze({
      "zh-CN": "下次启动仍由旧版负责模块读取",
      "en": "Read by the legacy owner on the next launch",
      "ja": "次回起動時に旧形式の担当モジュールが読み込み",
    }),
    "unavailable": Object.freeze({
      "zh-CN": "生效策略不可用",
      "en": "Effect policy unavailable",
      "ja": "反映方法を取得できません",
    }),
    "owner-specific": Object.freeze({
      "zh-CN": "按负责模块的规则生效",
      "en": "Follows the owner's policy",
      "ja": "担当モジュールのルールに従って反映",
    }),
  });

  const OWNER_LABELS = Object.freeze({
    "ConfigManager/AppConfig": Object.freeze({
      "zh-CN": "应用配置管理器",
      "en": "Application configuration manager",
      "ja": "アプリケーション設定マネージャー",
    }),
    "PluginManifest/AppConfig": Object.freeze({
      "zh-CN": "插件清单管理器",
      "en": "Plugin manifest manager",
      "ja": "プラグインマニフェスト管理",
    }),
    "CharacterPackage/AppHost": Object.freeze({
      "zh-CN": "角色包装配流程",
      "en": "Character package assembly",
      "ja": "キャラクターパッケージの組み立て処理",
    }),
    "Legacy configuration owner": Object.freeze({
      "zh-CN": "旧版配置负责模块",
      "en": "Legacy configuration owner",
      "ja": "旧形式の設定担当モジュール",
    }),
    "Production platform fold": Object.freeze({
      "zh-CN": "桌面平台选择器",
      "en": "Desktop platform selector",
      "ja": "デスクトッププラットフォーム選択",
    }),
    "SongConfigOwner/AppConfig": Object.freeze({
      "zh-CN": "唱歌配置负责模块",
      "en": "Singing configuration owner",
      "ja": "歌唱設定の担当モジュール",
    }),
    "ConfigManager/unrecognized": Object.freeze({
      "zh-CN": "未识别配置项",
      "en": "Unrecognized configuration item",
      "ja": "未認識の設定項目",
    }),
    "spica.core.character/CharacterPackage": Object.freeze({
      "zh-CN": "角色包读取器",
      "en": "Character package reader",
      "ja": "キャラクターパッケージ読み込み",
    }),
    "agent_tools.tts/load_tts_config": Object.freeze({
      "zh-CN": "角色语音配置读取器",
      "en": "Character voice configuration reader",
      "ja": "キャラクター音声設定の読み込み",
    }),
    "agent_tools.visual/VisualDiffService": Object.freeze({
      "zh-CN": "角色视觉配置读取器",
      "en": "Character visual configuration reader",
      "ja": "キャラクタービジュアル設定の読み込み",
    }),
    "spica.config.overlay_owner/OverlayConfig": Object.freeze({
      "zh-CN": "桌面浮层偏好管理器",
      "en": "Desktop overlay preference manager",
      "ja": "デスクトップオーバーレイ設定管理",
    }),
    "spica.plugins.manifest": Object.freeze({
      "zh-CN": "插件清单管理器",
      "en": "Plugin manifest manager",
      "ja": "プラグインマニフェスト管理",
    }),
    "spica.config.runtime_env": Object.freeze({
      "zh-CN": "运行时缓存管理器",
      "en": "Runtime cache manager",
      "ja": "ランタイムキャッシュ管理",
    }),
    "spica.config.manager/ReSpeaker consumers": Object.freeze({
      "zh-CN": "ReSpeaker 配置使用方",
      "en": "ReSpeaker configuration consumers",
      "ja": "ReSpeaker 設定の利用モジュール",
    }),
    "production owner": Object.freeze({
      "zh-CN": "生产配置负责模块",
      "en": "Production configuration owner",
      "ja": "本番設定の担当モジュール",
    }),
  });

  const STATUS_LABELS = Object.freeze({
    "QUEUED": Object.freeze({
      "zh-CN": "排队中",
      "en": "Queued",
      "ja": "待機中",
    }),
    "RUNNING": Object.freeze({
      "zh-CN": "运行中",
      "en": "Running",
      "ja": "実行中",
    }),
    "CANCELLING": Object.freeze({
      "zh-CN": "正在取消",
      "en": "Cancelling",
      "ja": "キャンセル中",
    }),
    "PASS": Object.freeze({
      "zh-CN": "通过",
      "en": "Pass",
      "ja": "合格",
    }),
    "UNVERIFIED": Object.freeze({
      "zh-CN": "未验证",
      "en": "Unverified",
      "ja": "未確認",
    }),
    "DEGRADED": Object.freeze({
      "zh-CN": "降级可用",
      "en": "Degraded",
      "ja": "制限付きで利用可能",
    }),
    "FAIL": Object.freeze({
      "zh-CN": "失败",
      "en": "Fail",
      "ja": "失敗",
    }),
    "CANCELLED": Object.freeze({
      "zh-CN": "已取消",
      "en": "Cancelled",
      "ja": "キャンセル済み",
    }),
    "INTERNAL_ERROR": Object.freeze({
      "zh-CN": "内部错误",
      "en": "Internal error",
      "ja": "内部エラー",
    }),
    "SKIPPED_DISABLED": Object.freeze({
      "zh-CN": "已跳过（功能关闭）",
      "en": "Skipped (feature disabled)",
      "ja": "スキップ（機能が無効）",
    }),
    "present": Object.freeze({
      "zh-CN": "已安装",
      "en": "Installed",
      "ja": "インストール済み",
    }),
    "missing": Object.freeze({
      "zh-CN": "缺失",
      "en": "Missing",
      "ja": "見つかりません",
    }),
    "unsafe": Object.freeze({
      "zh-CN": "不安全",
      "en": "Unsafe",
      "ja": "安全ではありません",
    }),
    "healthy": Object.freeze({
      "zh-CN": "正常",
      "en": "Healthy",
      "ja": "正常",
    }),
    "invalid": Object.freeze({
      "zh-CN": "无效",
      "en": "Invalid",
      "ja": "無効",
    }),
    "unavailable": Object.freeze({
      "zh-CN": "不可用",
      "en": "Unavailable",
      "ja": "利用不可",
    }),
    "external_read_only": Object.freeze({
      "zh-CN": "外部文件只读",
      "en": "External file · read-only",
      "ja": "外部ファイル・読み取り専用",
    }),
    "configured": Object.freeze({
      "zh-CN": "已配置",
      "en": "Configured",
      "ja": "設定済み",
    }),
    "not_configured": Object.freeze({
      "zh-CN": "未配置",
      "en": "Not configured",
      "ja": "未設定",
    }),
    "default": Object.freeze({
      "zh-CN": "使用默认值",
      "en": "Using default",
      "ja": "既定値を使用",
    }),
    "enabled": Object.freeze({
      "zh-CN": "已启用",
      "en": "Enabled",
      "ja": "有効",
    }),
    "disabled": Object.freeze({
      "zh-CN": "已关闭",
      "en": "Disabled",
      "ja": "無効",
    }),
    "PRIVATE": Object.freeze({
      "zh-CN": "权限安全",
      "en": "Private permissions",
      "ja": "安全な権限",
    }),
    "MISSING": Object.freeze({
      "zh-CN": "文件不存在",
      "en": "File missing",
      "ja": "ファイルなし",
    }),
    "TOO_PERMISSIVE": Object.freeze({
      "zh-CN": "权限过宽",
      "en": "Permissions too broad",
      "ja": "権限が広すぎます",
    }),
    "INVALID": Object.freeze({
      "zh-CN": "内容无效",
      "en": "Invalid content",
      "ja": "内容が無効",
    }),
    "UNAVAILABLE": Object.freeze({
      "zh-CN": "无法检查",
      "en": "Unable to check",
      "ja": "確認できません",
    }),
    "UNKNOWN": Object.freeze({
      "zh-CN": "状态未知",
      "en": "Unknown status",
      "ja": "状態不明",
    }),
    "unknown": Object.freeze({
      "zh-CN": "状态未知",
      "en": "Unknown status",
      "ja": "状態不明",
    }),
  });

  const LEVEL_LABELS = Object.freeze({
    "basic": Object.freeze({
      "zh-CN": "基础设置",
      "en": "Basic",
      "ja": "基本",
    }),
    "advanced": Object.freeze({
      "zh-CN": "高级设置",
      "en": "Advanced",
      "ja": "詳細",
    }),
  });

  const PATH_KIND_LABELS = Object.freeze({
    "file": Object.freeze({
      "zh-CN": "文件",
      "en": "File",
      "ja": "ファイル",
    }),
    "directory": Object.freeze({
      "zh-CN": "目录",
      "en": "Directory",
      "ja": "ディレクトリ",
    }),
    "unknown": Object.freeze({
      "zh-CN": "未知类型",
      "en": "Unknown type",
      "ja": "種類不明",
    }),
  });

  const OVERLAY_PRESENTATIONS = Object.freeze({
    "default_character_scale": Object.freeze({
      "zh-CN": Object.freeze({
        title: "角色立绘默认缩放",
        description: "控制桌面浮层中角色立绘初次显示时的整体缩放比例。",
        advice: "数值过大可能遮挡对话框或超出屏幕，建议小幅调整后重新启动查看。",
      }),
      en: Object.freeze({
        title: "Default character-image scale",
        description: "Controls the initial overall scale of the character image in the desktop overlay.",
        advice: "A large value may cover the dialogue area or extend beyond the screen. Adjust it in small steps and check after restarting.",
      }),
      ja: Object.freeze({
        title: "キャラクター画像の既定拡大率",
        description: "デスクトップオーバーレイでキャラクター画像を最初に表示する際の全体的な拡大率を設定します。",
        advice: "大きすぎると会話欄を隠したり画面外にはみ出したりします。少しずつ変更し、再起動後に確認してください。",
      }),
    }),
    "default_ui_scale": Object.freeze({
      "zh-CN": Object.freeze({
        title: "界面默认缩放",
        description: "控制桌面浮层按钮、文字和面板的整体显示比例。",
        advice: "高分辨率屏幕可适当增大，小屏幕可适当减小。",
      }),
      en: Object.freeze({
        title: "Default interface scale",
        description: "Controls the overall scale of buttons, text, and panels in the desktop overlay.",
        advice: "Increase it on high-resolution displays or reduce it on smaller screens.",
      }),
      ja: Object.freeze({
        title: "画面部品の既定拡大率",
        description: "デスクトップオーバーレイのボタン、文字、パネル全体の表示倍率を設定します。",
        advice: "高解像度画面では大きめ、小さい画面では小さめに調整できます。",
      }),
    }),
    "default_typewriter_speed": Object.freeze({
      "zh-CN": Object.freeze({
        title: "打字机动画速度",
        description: "控制对话文字逐字显示的默认速度倍率。",
        advice: "数值越大显示越快；希望完整阅读动画时不要设置过高。",
      }),
      en: Object.freeze({
        title: "Typewriter animation speed",
        description: "Controls the default speed at which dialogue text appears character by character.",
        advice: "Higher values display text faster. Avoid setting it too high if you want to follow the full animation.",
      }),
      ja: Object.freeze({
        title: "タイプライター表示の速度",
        description: "会話文を1文字ずつ表示する際の既定速度を設定します。",
        advice: "値が大きいほど速く表示されます。表示演出を最後まで見たい場合は上げすぎないでください。",
      }),
    }),
    "character_label_height_scale": Object.freeze({
      "zh-CN": Object.freeze({
        title: "角色与对话区域高度比例",
        description: "调整角色立绘和对话标签共同布局时使用的高度比例。",
        advice: "这是布局微调项；过大或过小都可能造成留白或重叠。",
      }),
      en: Object.freeze({
        title: "Character-and-dialogue height ratio",
        description: "Adjusts the height ratio used to lay out the character image and dialogue label together.",
        advice: "This is a fine layout adjustment. Values that are too high or low may cause excess empty space or overlap.",
      }),
      ja: Object.freeze({
        title: "キャラクターと会話領域の高さ比率",
        description: "キャラクター画像と会話ラベルをまとめて配置する際に使用する高さの比率を調整します。",
        advice: "レイアウトの微調整項目です。大きすぎても小さすぎても、余白や重なりが生じる場合があります。",
      }),
    }),
    "overlay_initial_height_scale": Object.freeze({
      "zh-CN": Object.freeze({
        title: "浮层初始高度比例",
        description: "控制桌面浮层第一次创建时相对基础高度的放大比例。",
        advice: "只影响初始布局，建议小幅调整并在常用分辨率下检查。",
      }),
      en: Object.freeze({
        title: "Initial overlay height ratio",
        description: "Controls how much the desktop overlay's base height is scaled when it is first created.",
        advice: "This affects only the initial layout. Adjust it in small steps and check it at your usual screen resolution.",
      }),
      ja: Object.freeze({
        title: "オーバーレイの初期高さ比率",
        description: "デスクトップオーバーレイを初めて作成する際に、基準の高さをどの程度拡大するかを設定します。",
        advice: "初期レイアウトだけに影響します。少しずつ変更し、普段使う画面解像度で確認してください。",
      }),
    }),
    "character_max_height_ratio": Object.freeze({
      "zh-CN": Object.freeze({
        title: "角色立绘最大高度比例",
        description: "限制角色立绘相对当前浮层高度最多可以占多高。",
        advice: "降低可避免立绘压住界面，提高则能显示更大的角色形象。",
      }),
      en: Object.freeze({
        title: "Maximum character-image height ratio",
        description: "Limits how much of the current overlay height the character image may occupy.",
        advice: "Lower it to keep the character from covering interface elements; raise it to display a larger character image.",
      }),
      ja: Object.freeze({
        title: "キャラクター画像の最大高さ比率",
        description: "現在のオーバーレイの高さに対して、キャラクター画像が占められる最大割合を制限します。",
        advice: "画面部品への重なりを避けるには下げ、キャラクターを大きく表示するには上げます。",
      }),
    }),
    "spica_voice_volume": Object.freeze({
      "zh-CN": Object.freeze({
        title: "Spica 对话音量",
        description: "控制普通对话语音播放时使用的音量比例。",
        advice: "该值只调整 Spica 的对话音量，不会改变系统主音量。",
      }),
      en: Object.freeze({
        title: "Spica dialogue volume",
        description: "Controls the volume used when playing Spica's regular dialogue voice.",
        advice: "This adjusts only Spica's dialogue volume; it does not change the system master volume.",
      }),
      ja: Object.freeze({
        title: "Spicaの会話音量",
        description: "Spicaの通常会話の音声を再生する際の音量を設定します。",
        advice: "Spicaの会話音量だけを調整し、システム全体の音量は変更しません。",
      }),
    }),
  });

  const DOCUMENT_PRESENTATIONS = Object.freeze({
    "character_package": Object.freeze({
      "zh-CN": "当前角色包",
      "en": "Current character package",
      "ja": "現在のキャラクターパッケージ",
    }),
    "character_tts": Object.freeze({
      "zh-CN": "角色语音合成（TTS）数据",
      "en": "Character text-to-speech (TTS) data",
      "ja": "キャラクター音声合成（TTS）データ",
    }),
    "character_visual": Object.freeze({
      "zh-CN": "角色视觉表现数据",
      "en": "Character visual presentation data",
      "ja": "キャラクターのビジュアル表示データ",
    }),
    "overlay_preferences": Object.freeze({
      "zh-CN": "桌面浮层偏好",
      "en": "Desktop overlay preferences",
      "ja": "デスクトップオーバーレイ設定",
    }),
  });

  const READONLY_REASON_LABELS = Object.freeze({
    "owner_schema_unavailable": Object.freeze({
      "zh-CN": "负责模块尚未提供类型化编辑规则",
      "en": "The owner does not yet expose a typed editing schema",
      "ja": "担当モジュールが型付き編集スキーマをまだ提供していません",
    }),
    "external_read_only": Object.freeze({
      "zh-CN": "外部角色文件只允许查看安全元数据",
      "en": "Only safe metadata from external character files can be viewed",
      "ja": "外部キャラクターファイルは安全なメタデータのみ表示できます",
    }),
    "no_app_yaml_owner": Object.freeze({
      "zh-CN": "app.yaml 中没有对应字段",
      "en": "No corresponding field exists in app.yaml",
      "ja": "app.yaml に対応する項目がありません",
    }),
    "runtime_derived": Object.freeze({
      "zh-CN": "由启动装配流程计算，只读",
      "en": "Calculated during startup assembly; read-only",
      "ja": "起動時の組み立て処理で算出されるため読み取り専用です",
    }),
    "legacy_owner_active": Object.freeze({
      "zh-CN": "旧版配置文件仍在生效，编辑已安全关闭",
      "en": "A legacy configuration file is active; editing fails closed",
      "ja": "旧形式の設定ファイルが有効なため、編集は安全のため無効です",
    }),
    "resolution_unavailable": Object.freeze({
      "zh-CN": "下一次启动值无法解析",
      "en": "The next-launch value could not be resolved",
      "ja": "次回起動時の値を解決できません",
    }),
    "read_only": Object.freeze({
      "zh-CN": "只读",
      "en": "Read-only",
      "ja": "読み取り専用",
    }),
  });

  const ENVIRONMENT_SETTING_LABELS = Object.freeze({
    "runtime_cache.cache_root": Object.freeze({
      "zh-CN": "运行时缓存目录",
      "en": "Runtime cache directory",
      "ja": "ランタイムキャッシュディレクトリ",
    }),
    "respeaker.tuning_path": Object.freeze({
      "zh-CN": "ReSpeaker 调校文件",
      "en": "ReSpeaker tuning file",
      "ja": "ReSpeaker 調整ファイル",
    }),
    "respeaker.require_hardware_vad": Object.freeze({
      "zh-CN": "要求 ReSpeaker 硬件语音检测",
      "en": "Require ReSpeaker hardware voice activity detection",
      "ja": "ReSpeaker のハードウェア音声区間検出を必須にする",
    }),
    "respeaker.input_device_index": Object.freeze({
      "zh-CN": "ReSpeaker 输入设备编号",
      "en": "ReSpeaker input device index",
      "ja": "ReSpeaker 入力デバイス番号",
    }),
    "respeaker.end_silence_seconds": Object.freeze({
      "zh-CN": "ReSpeaker 结束静音时长",
      "en": "ReSpeaker end-of-speech silence",
      "ja": "ReSpeaker の発話終了無音時間",
    }),
  });

  const SECRET_SLOT_LABELS = Object.freeze({
    "openai_api_key": Object.freeze({
      "zh-CN": "主对话模型 API 密钥",
      "en": "Main conversation model API key",
      "ja": "メイン会話モデルの API キー",
    }),
    "judge_api_key": Object.freeze({
      "zh-CN": "剧情反应判断 API 密钥",
      "en": "Story-reaction judge API key",
      "ja": "物語リアクション判定用 API キー",
    }),
    "bilibili_cookie": Object.freeze({
      "zh-CN": "Bilibili 登录 Cookie",
      "en": "Bilibili login cookie",
      "ja": "Bilibili ログイン Cookie",
    }),
    "qbittorrent_password": Object.freeze({
      "zh-CN": "qBittorrent 密码",
      "en": "qBittorrent password",
      "ja": "qBittorrent パスワード",
    }),
  });

  const SENSITIVE_COMMAND_LABELS = Object.freeze({
    "set_secret": Object.freeze({
      "zh-CN": "设置密钥",
      "en": "Set secret",
      "ja": "シークレットを設定",
    }),
    "clear_secret": Object.freeze({
      "zh-CN": "清除密钥",
      "en": "Clear secret",
      "ja": "シークレットを削除",
    }),
    "clear_mapped_override": Object.freeze({
      "zh-CN": "清除已映射的环境覆盖",
      "en": "Clear mapped environment override",
      "ja": "対応する環境変数の上書きを削除",
    }),
  });

  const SECRET_CHANGE_LABELS = Object.freeze({
    "unchanged": Object.freeze({
      "zh-CN": "保持不变",
      "en": "Unchanged",
      "ja": "変更なし",
    }),
    "will_set": Object.freeze({
      "zh-CN": "将新增",
      "en": "Will be set",
      "ja": "新規設定",
    }),
    "will_clear": Object.freeze({
      "zh-CN": "将清除",
      "en": "Will be cleared",
      "ja": "削除予定",
    }),
    "will_replace": Object.freeze({
      "zh-CN": "将替换",
      "en": "Will be replaced",
      "ja": "置換予定",
    }),
  });

  const LANE_LABELS = Object.freeze({
    "app": Object.freeze({
      "zh-CN": "应用配置",
      "en": "Application configuration",
      "ja": "アプリケーション設定",
    }),
    "overlay": Object.freeze({
      "zh-CN": "桌面浮层偏好",
      "en": "Desktop overlay preferences",
      "ja": "デスクトップオーバーレイ設定",
    }),
    "sensitive": Object.freeze({
      "zh-CN": "敏感配置文件",
      "en": "Sensitive configuration file",
      "ja": "機密設定ファイル",
    }),
  });

  const CHECK_LABELS = Object.freeze({
    "config": Object.freeze({
      "zh-CN": "配置解析",
      "en": "Configuration resolution",
      "ja": "設定の解決",
    }),
    "gpu": Object.freeze({
      "zh-CN": "显卡与计算环境",
      "en": "GPU and compute environment",
      "ja": "GPU と計算環境",
    }),
    "secrets": Object.freeze({
      "zh-CN": "密钥状态",
      "en": "Secret status",
      "ja": "シークレットの状態",
    }),
    "tts": Object.freeze({
      "zh-CN": "语音合成（TTS）",
      "en": "Text-to-speech (TTS)",
      "ja": "音声合成（TTS）",
    }),
    "stt": Object.freeze({
      "zh-CN": "语音识别（STT）",
      "en": "Speech-to-text (STT)",
      "ja": "音声認識（STT）",
    }),
    "moondream": Object.freeze({
      "zh-CN": "屏幕理解模型（Moondream）",
      "en": "Screen understanding model (Moondream)",
      "ja": "画面理解モデル（Moondream）",
    }),
    "ocr": Object.freeze({
      "zh-CN": "文字识别（OCR）",
      "en": "Optical character recognition (OCR)",
      "ja": "文字認識（OCR）",
    }),
    "song_uvr": Object.freeze({
      "zh-CN": "歌曲人声分离（UVR）",
      "en": "Vocal separation (UVR)",
      "ja": "ボーカル分離（UVR）",
    }),
    "song_rvc": Object.freeze({
      "zh-CN": "歌声转换（RVC）",
      "en": "Singing voice conversion (RVC)",
      "ja": "歌声変換（RVC）",
    }),
    "llm": Object.freeze({
      "zh-CN": "大语言模型（LLM）",
      "en": "Large language model (LLM)",
      "ja": "大規模言語モデル（LLM）",
    }),
    "unknown": Object.freeze({
      "zh-CN": "未知检查",
      "en": "Unknown check",
      "ja": "不明なチェック",
    }),
  });

  const HEALTH_ISSUE_LABELS = Object.freeze({
    "BACKGROUND_ASSET_INVALID": Object.freeze({
      "zh-CN": "背景图资源缺失或已损坏，页面将使用渐变背景",
      "en": "The background image is missing or damaged; the page will use its gradient fallback",
      "ja": "背景画像が見つからないか破損しているため、グラデーション背景を使用します",
    }),
    "CONFIG_RESOLUTION_ERROR": Object.freeze({
      "zh-CN": "配置无法完整解析",
      "en": "Configuration could not be fully resolved",
      "ja": "設定を完全に解決できません",
    }),
    "ENVIRONMENT_VALUE_TAINTED": Object.freeze({
      "zh-CN": "环境覆盖中包含敏感值，页面已隐藏内容",
      "en": "An environment override contains sensitive data; its value is hidden",
      "ja": "環境変数の上書きに機密値が含まれるため、値を非表示にしました",
    }),
    "SENSITIVE_DOCUMENT_PERMISSION_TOO_PERMISSIVE": Object.freeze({
      "zh-CN": "仓库敏感文件权限过宽",
      "en": "Repository sensitive-file permissions are too broad",
      "ja": "リポジトリの機密ファイルの権限が広すぎます",
    }),
    "SENSITIVE_DOCUMENT_PERMISSION_UNSAFE": Object.freeze({
      "zh-CN": "仓库敏感文件的路径或所有者不安全",
      "en": "The repository sensitive-file path or owner is unsafe",
      "ja": "リポジトリの機密ファイルのパスまたは所有者が安全ではありません",
    }),
    "SENSITIVE_DOCUMENT_PARSE_INVALID": Object.freeze({
      "zh-CN": "仓库敏感文件内容无法解析",
      "en": "The repository sensitive file could not be parsed",
      "ja": "リポジトリの機密ファイルを解析できません",
    }),
    "SENSITIVE_DOCUMENT_PARSE_UNAVAILABLE": Object.freeze({
      "zh-CN": "仓库敏感文件暂时无法检查",
      "en": "The repository sensitive file could not be checked",
      "ja": "リポジトリの機密ファイルを確認できません",
    }),
    "LEGACY_ENV_ENTRY_PRESENT": Object.freeze({
      "zh-CN": "仓库敏感文件中仍有已退役的环境变量",
      "en": "The repository sensitive file still contains retired environment entries",
      "ja": "リポジトリの機密ファイルに廃止済みの環境変数が残っています",
    }),
    "PARENT_ENV_PERMISSION_TOO_PERMISSIVE": Object.freeze({
      "zh-CN": "父目录敏感文件权限过宽",
      "en": "Parent-directory sensitive-file permissions are too broad",
      "ja": "親ディレクトリの機密ファイルの権限が広すぎます",
    }),
    "PARENT_ENV_PERMISSION_UNSAFE": Object.freeze({
      "zh-CN": "父目录敏感文件的路径或所有者不安全",
      "en": "The parent-directory sensitive-file path or owner is unsafe",
      "ja": "親ディレクトリの機密ファイルのパスまたは所有者が安全ではありません",
    }),
    "PARENT_ENV_PARSE_INVALID": Object.freeze({
      "zh-CN": "父目录敏感文件内容无法解析",
      "en": "The parent-directory sensitive file could not be parsed",
      "ja": "親ディレクトリの機密ファイルを解析できません",
    }),
    "PARENT_ENV_PARSE_UNAVAILABLE": Object.freeze({
      "zh-CN": "父目录敏感文件暂时无法检查",
      "en": "The parent-directory sensitive file could not be checked",
      "ja": "親ディレクトリの機密ファイルを確認できません",
    }),
    "PARENT_LEGACY_ENV_ENTRY_PRESENT": Object.freeze({
      "zh-CN": "父目录敏感文件中仍有已退役的环境变量",
      "en": "The parent-directory sensitive file still contains retired environment entries",
      "ja": "親ディレクトリの機密ファイルに廃止済みの環境変数が残っています",
    }),
    "LEGACY_PLUGINS_DOCUMENT_PRESENT": Object.freeze({
      "zh-CN": "已退役的插件配置文件重新出现",
      "en": "The retired plugin configuration file has reappeared",
      "ja": "廃止済みのプラグイン設定ファイルが再び存在しています",
    }),
    "LEGACY_SCREEN_DOCUMENT_PRESENT": Object.freeze({
      "zh-CN": "已退役的屏幕配置文件重新出现",
      "en": "The retired screen configuration file has reappeared",
      "ja": "廃止済みの画面設定ファイルが再び存在しています",
    }),
    "LEGACY_SONG_DOCUMENT_PRESENT": Object.freeze({
      "zh-CN": "已退役的唱歌配置文件重新出现",
      "en": "The retired singing configuration file has reappeared",
      "ja": "廃止済みの歌唱設定ファイルが再び存在しています",
    }),
  });

  const DYNAMIC_COPY = Object.freeze({
    "runtime.001": Object.freeze({
      "zh-CN": "配置健康检查报告了需要注意的问题",
      "en": "Configuration health checks reported issues that need attention",
      "ja": "構成の健全性チェックで確認が必要な問題が報告されました",
    }),
    "runtime.002": Object.freeze({
      "zh-CN": "正在验证一次性授权…",
      "en": "Verifying the one-time grant…",
      "ja": "ワンタイム認可を確認しています…",
    }),
    "runtime.003": Object.freeze({
      "zh-CN": "授权无效、已过期或尝试次数已用尽。",
      "en": "The grant is invalid, expired, or has exhausted its allowed attempts.",
      "ja": "認可が無効か期限切れ、または試行回数の上限に達しています。",
    }),
    "runtime.004": Object.freeze({
      "zh-CN": "使用建议：@@0@@",
      "en": "Guidance: @@0@@",
      "ja": "使用上のヒント：@@0@@",
    }),
    "runtime.005": Object.freeze({
      "zh-CN": "生效策略：@@0@@",
      "en": "Activation policy: @@0@@",
      "ja": "適用ポリシー：@@0@@",
    }),
    "runtime.006": Object.freeze({
      "zh-CN": "@@0@@ = @@1@@",
      "en": "@@0@@ = @@1@@",
      "ja": "@@0@@ = @@1@@",
    }),
    "runtime.007": Object.freeze({
      "zh-CN": "无额外依赖",
      "en": "No additional dependencies",
      "ja": "追加の依存条件はありません",
    }),
    "runtime.008": Object.freeze({
      "zh-CN": "依赖条件：@@0@@",
      "en": "Dependencies: @@0@@",
      "ja": "依存条件：@@0@@",
    }),
    "runtime.009": Object.freeze({
      "zh-CN": "配置键：@@0@@",
      "en": "Configuration key: @@0@@",
      "ja": "設定キー：@@0@@",
    }),
    "runtime.010": Object.freeze({
      "zh-CN": "生效策略：@@0@@",
      "en": "Activation policy: @@0@@",
      "ja": "適用ポリシー：@@0@@",
    }),
    "runtime.011": Object.freeze({
      "zh-CN": "配置键：@@0@@",
      "en": "Configuration key: @@0@@",
      "ja": "設定キー：@@0@@",
    }),
    "runtime.012": Object.freeze({
      "zh-CN": " · 路径@@0@@",
      "en": " · Path @@0@@",
      "ja": " · パス@@0@@",
    }),
    "runtime.013": Object.freeze({
      "zh-CN": "配置键",
      "en": "Configuration key",
      "ja": "設定キー",
    }),
    "runtime.014": Object.freeze({
      "zh-CN": "用途",
      "en": "Purpose",
      "ja": "用途",
    }),
    "runtime.015": Object.freeze({
      "zh-CN": "暂无专用说明。",
      "en": "No dedicated description is available.",
      "ja": "専用の説明はありません。",
    }),
    "runtime.016": Object.freeze({
      "zh-CN": "使用建议",
      "en": "Guidance",
      "ja": "使用上のヒント",
    }),
    "runtime.017": Object.freeze({
      "zh-CN": "此动态字段保持原始名称和负责模块说明。",
      "en": "This dynamic field retains its raw name and owner description.",
      "ja": "この動的フィールドでは、元の名前と担当モジュールの説明をそのまま使用します。",
    }),
    "runtime.018": Object.freeze({
      "zh-CN": "字段类型",
      "en": "Field type",
      "ja": "フィールド型",
    }),
    "runtime.019": Object.freeze({
      "zh-CN": "显示级别",
      "en": "Display level",
      "ja": "表示レベル",
    }),
    "runtime.020": Object.freeze({
      "zh-CN": "文件中的值",
      "en": "File value",
      "ja": "ファイル内の値",
    }),
    "runtime.021": Object.freeze({
      "zh-CN": "环境变量",
      "en": "Environment variable",
      "ja": "環境変数",
    }),
    "runtime.022": Object.freeze({
      "zh-CN": "可编辑 · 保存后按负责模块的生效策略处理",
      "en": "Editable · After saving, changes follow the owner's activation policy",
      "ja": "編集可 · 保存後、担当モジュールの適用ポリシーに従います",
    }),
    "runtime.023": Object.freeze({
      "zh-CN": "仅恢复模式 · 仅允许符合服务端能力的语义回滚",
      "en": "Recovery-only mode · Only semantic rollback allowed by server capabilities is available",
      "ja": "復元専用モード · サーバー機能で許可されたセマンティックロールバックのみ実行できます",
    }),
    "runtime.024": Object.freeze({
      "zh-CN": "服务端写入能力未开放",
      "en": "Server-side write capability is unavailable",
      "ja": "サーバー側の書き込み機能は有効になっていません",
    }),
    "runtime.025": Object.freeze({
      "zh-CN": "写入结构值（关闭则写入 null）",
      "en": "Write a structured value (turn off to write null)",
      "ja": "構造化値を書き込む（オフにすると null を書き込みます）",
    }),
    "runtime.026": Object.freeze({
      "zh-CN": "最多 256 项；已达到客户端安全上限。",
      "en": "Maximum 256 items; the client safety limit has been reached.",
      "ja": "最大 256 項です。クライアントの安全上限に達しました。",
    }),
    "runtime.027": Object.freeze({
      "zh-CN": "移除",
      "en": "Remove",
      "ja": "削除",
    }),
    "runtime.028": Object.freeze({
      "zh-CN": "新增一项",
      "en": "Add item",
      "ja": "項目を追加",
    }),
    "runtime.029": Object.freeze({
      "zh-CN": "新增键",
      "en": "Add key",
      "ja": "キーを追加",
    }),
    "runtime.030": Object.freeze({
      "zh-CN": "显式写入 null",
      "en": "Explicitly write null",
      "ja": "null を明示的に書き込む",
    }),
    "runtime.031": Object.freeze({
      "zh-CN": "关闭 null 后按字段类型写值；空字符串不是 null。",
      "en": "When null is off, a value is written according to the field type; an empty string is not null.",
      "ja": "null をオフにするとフィールド型に応じた値を書き込みます。空文字列は null ではありません。",
    }),
    "runtime.032": Object.freeze({
      "zh-CN": "@@0@@ 的候选文件值",
      "en": "Candidate file value for @@0@@",
      "ja": "@@0@@ の候補ファイル値",
    }),
    "runtime.033": Object.freeze({
      "zh-CN": "该结构化字段需要专用的类型化编辑器，本页暂不能修改。",
      "en": "This structured field requires a dedicated typed editor and cannot currently be changed on this page.",
      "ja": "この構造化フィールドには専用の型付きエディターが必要なため、現在このページでは変更できません。",
    }),
    "runtime.034": Object.freeze({
      "zh-CN": "app.yaml 处于仅恢复模式；新增或修改、移除文件值与预览均已关闭。",
      "en": "app.yaml is in recovery-only mode; adding, editing, or removing file values and previewing are disabled.",
      "ja": "app.yaml は復元専用モードです。ファイル値の追加・変更・削除とプレビューは無効です。",
    }),
    "runtime.035": Object.freeze({
      "zh-CN": "服务端未开放应用配置写入能力（app_config_write）；此处保持只读。",
      "en": "The server has not enabled application configuration writes (app_config_write); this area remains read-only.",
      "ja": "サーバーでアプリ設定の書き込み機能（app_config_write）が有効になっていないため、ここは読み取り専用です。",
    }),
    "runtime.036": Object.freeze({
      "zh-CN": "CATALOG_FIELDS_INCOMPLETE：新增或修改操作会安全关闭；现有文件覆盖的移除修复路径仍可用。",
      "en": "CATALOG_FIELDS_INCOMPLETE: Add and update operations fail closed; existing file overrides can still be removed as a repair path.",
      "ja": "CATALOG_FIELDS_INCOMPLETE：追加・変更操作は安全のため無効です。既存のファイル上書きを削除する修復経路は引き続き利用できます。",
    }),
    "runtime.037": Object.freeze({
      "zh-CN": "FIELD_AUTHORING_INCOMPLETE：服务端未确认完整的字段编辑投影；新增或修改已关闭，现有文件覆盖仍可移除。",
      "en": "FIELD_AUTHORING_INCOMPLETE: The server has not confirmed a complete field-authoring projection; adding and updating are disabled, while existing file overrides can still be removed.",
      "ja": "FIELD_AUTHORING_INCOMPLETE：サーバーが完全なフィールド編集プロジェクションを確認できていません。追加・変更は無効ですが、既存のファイル上書きは引き続き削除できます。",
    }),
    "runtime.038": Object.freeze({
      "zh-CN": "变更只保存在本页内存中，预览后才可保存。",
      "en": "Changes are stored only in this page's memory and can be saved only after previewing.",
      "ja": "変更はこのページのメモリ内にのみ保持され、プレビュー後に保存できます。",
    }),
    "runtime.039": Object.freeze({
      "zh-CN": "已加入本页草稿；尚未写入文件。",
      "en": "Added to this page's draft; no file has been written yet.",
      "ja": "このページの下書きに追加しました。まだファイルには書き込まれていません。",
    }),
    "runtime.040": Object.freeze({
      "zh-CN": "将移除文件覆盖，并回落到环境覆盖或默认值的负责模块规则。",
      "en": "The file override will be removed, falling back to the owner's environment override or default rules.",
      "ja": "ファイルの上書きを削除し、担当モジュールの環境変数による上書きまたはデフォルト値の規則にフォールバックします。",
    }),
    "runtime.041": Object.freeze({
      "zh-CN": "@@0@@ 项",
      "en": "@@0@@ items",
      "ja": "@@0@@ 件",
    }),
    "runtime.042": Object.freeze({
      "zh-CN": "类型化字段",
      "en": "Typed field",
      "ja": "型付きフィールド",
    }),
    "runtime.043": Object.freeze({
      "zh-CN": "@@0@@ → @@1@@ · 下次启动值 @@2@@ → @@3@@",
      "en": "@@0@@ → @@1@@ · Next-launch value @@2@@ → @@3@@",
      "ja": "@@0@@ → @@1@@ · 次回起動時の値 @@2@@ → @@3@@",
    }),
    "runtime.044": Object.freeze({
      "zh-CN": "环境变量覆盖仍控制下次启动值",
      "en": "An environment override still controls the next-launch value",
      "ja": "環境変数による上書きが引き続き次回起動時の値を決定します",
    }),
    "runtime.045": Object.freeze({
      "zh-CN": "候选文档无语义变化。",
      "en": "The candidate document has no semantic changes.",
      "ja": "候補ドキュメントに意味上の変更はありません。",
    }),
    "runtime.046": Object.freeze({
      "zh-CN": "候选已通过生产配置负责模块校验；确认后才会原子发布。",
      "en": "The candidate passed validation by the production configuration owner; it will be published atomically only after confirmation.",
      "ja": "候補は本番構成の担当モジュールによる検証に合格しています。確認後にのみアトミックに公開されます。",
    }),
    "runtime.047": Object.freeze({
      "zh-CN": "候选与当前文档相同；保存不会创建恢复点。",
      "en": "The candidate is identical to the current document; saving will not create a restore point.",
      "ja": "候補は現在のドキュメントと同一です。保存しても復元ポイントは作成されません。",
    }),
    "runtime.048": Object.freeze({
      "zh-CN": "预览失败：@@0@@",
      "en": "Preview failed: @@0@@",
      "ja": "プレビューに失敗しました：@@0@@",
    }),
    "runtime.049": Object.freeze({
      "zh-CN": "app.yaml 已原子保存；按配置负责模块提示在下次 Spica 启动生效。",
      "en": "app.yaml was saved atomically; it will take effect on the next Spica launch as indicated by the configuration owner.",
      "ja": "app.yaml はアトミックに保存されました。構成の担当モジュールの案内どおり、次回の Spica 起動時に有効になります。",
    }),
    "runtime.050": Object.freeze({
      "zh-CN": "保存失败：@@0@@。本页草稿仍保留，请重新预览。",
      "en": "Save failed: @@0@@. The page draft has been retained; please preview it again.",
      "ja": "保存に失敗しました：@@0@@。このページの下書きは保持されています。もう一度プレビューしてください。",
    }),
    "runtime.051": Object.freeze({
      "zh-CN": "配置目录暂无可展示字段。",
      "en": "No fields are available to display in the configuration catalog.",
      "ja": "構成カタログに表示できるフィールドはありません。",
    }),
    "runtime.052": Object.freeze({
      "zh-CN": "没有匹配当前筛选的字段。",
      "en": "No fields match the current filters.",
      "ja": "現在の絞り込み条件に一致するフィールドはありません。",
    }),
    "runtime.053": Object.freeze({
      "zh-CN": "查看 @@0@@ 配置详情",
      "en": "View configuration details for @@0@@",
      "ja": "@@0@@ の設定詳細を表示",
    }),
    "runtime.054": Object.freeze({
      "zh-CN": "角色数据文件目录已省略 @@0@@ 份文档；下方仅显示其余安全内容。",
      "en": "@@0@@ documents were omitted from the character data file catalog; only the remaining safe content is shown below.",
      "ja": "キャラクターデータファイルのカタログから @@0@@ 件のドキュメントが省略されました。以下には残りの安全な内容のみを表示します。",
    }),
    "runtime.055": Object.freeze({
      "zh-CN": "@@0@@ · @@1@@ 个字段",
      "en": "@@0@@ · @@1@@ fields",
      "ja": "@@0@@ · @@1@@ 件のフィールド",
    }),
    "runtime.056": Object.freeze({
      "zh-CN": "字符串",
      "en": "Strings",
      "ja": "文字列",
    }),
    "runtime.057": Object.freeze({
      "zh-CN": "集合",
      "en": "Collections",
      "ja": "コレクション",
    }),
    "runtime.058": Object.freeze({
      "zh-CN": "深度",
      "en": "Depth",
      "ja": "深さ",
    }),
    "runtime.059": Object.freeze({
      "zh-CN": "不支持值",
      "en": "Unsupported values",
      "ja": "サポートされていない値",
    }),
    "runtime.060": Object.freeze({
      "zh-CN": "字节预算",
      "en": "Byte budget",
      "ja": "バイト予算",
    }),
    "runtime.061": Object.freeze({
      "zh-CN": "安全投影截断：@@0@@；当前卡片仅展示截断后的安全投影。",
      "en": "Safe projection truncated: @@0@@; this card shows only the truncated safe projection.",
      "ja": "安全なプロジェクションが切り詰められました：@@0@@。このカードには切り詰め後の安全なプロジェクションのみを表示します。",
    }),
    "runtime.062": Object.freeze({
      "zh-CN": "查看只读内容",
      "en": "View read-only content",
      "ja": "読み取り専用の内容を表示",
    }),
    "runtime.063": Object.freeze({
      "zh-CN": "文件名",
      "en": "File name",
      "ja": "ファイル名",
    }),
    "runtime.064": Object.freeze({
      "zh-CN": "未公开",
      "en": "Not disclosed",
      "ja": "非公開",
    }),
    "runtime.065": Object.freeze({
      "zh-CN": "来源",
      "en": "Source",
      "ja": "ソース",
    }),
    "runtime.066": Object.freeze({
      "zh-CN": "是否为外部文件",
      "en": "External file",
      "ja": "外部ファイルかどうか",
    }),
    "runtime.067": Object.freeze({
      "zh-CN": "健康检查代码",
      "en": "Health check code",
      "ja": "健全性チェックコード",
    }),
    "runtime.068": Object.freeze({
      "zh-CN": "只读原因",
      "en": "Read-only reason",
      "ja": "読み取り専用の理由",
    }),
    "runtime.069": Object.freeze({
      "zh-CN": "负责模块没有返回可查看字段；这里只展示安全元数据。",
      "en": "The owner returned no viewable fields; only safe metadata is shown here.",
      "ja": "担当モジュールから表示可能なフィールドが返されなかったため、ここには安全なメタデータのみを表示します。",
    }),
    "runtime.070": Object.freeze({
      "zh-CN": "当前没有可安全展示的角色文档。",
      "en": "No character documents can currently be displayed safely.",
      "ja": "現在、安全に表示できるキャラクタードキュメントはありません。",
    }),
    "runtime.071": Object.freeze({
      "zh-CN": "桌面浮层负责模块的元数据不可用；写入保持关闭。",
      "en": "Desktop overlay owner metadata is unavailable; writes remain disabled.",
      "ja": "デスクトップオーバーレイの担当モジュールのメタデータを利用できないため、書き込みは無効のままです。",
    }),
    "runtime.072": Object.freeze({
      "zh-CN": "界面偏好字段",
      "en": "UI preference fields",
      "ja": "UI 設定フィールド",
    }),
    "runtime.073": Object.freeze({
      "zh-CN": "预览",
      "en": "Preview",
      "ja": "プレビュー",
    }),
    "runtime.074": Object.freeze({
      "zh-CN": "桌面浮层负责模块没有可编辑字段。",
      "en": "The desktop overlay owner has no editable fields.",
      "ja": "デスクトップオーバーレイの担当モジュールに編集可能なフィールドはありません。",
    }),
    "runtime.075": Object.freeze({
      "zh-CN": "每次只保存一个由负责模块定义的固定字段；不会实时修改运行中的桌面浮层。",
      "en": "Each save writes one fixed field defined by the owner; it does not update the running desktop overlay in real time.",
      "ja": "保存ごとに担当モジュールが定義した固定フィールドを 1 つだけ書き込みます。実行中のデスクトップオーバーレイはリアルタイムでは変更されません。",
    }),
    "runtime.076": Object.freeze({
      "zh-CN": "服务端未开放桌面浮层写入能力（overlay_write）；页面保持只读。",
      "en": "The server has not enabled desktop overlay writes (overlay_write); the page remains read-only.",
      "ja": "サーバーでデスクトップオーバーレイの書き込み機能（overlay_write）が有効になっていないため、ページは読み取り専用です。",
    }),
    "runtime.077": Object.freeze({
      "zh-CN": "请输入有限数字。",
      "en": "Please enter a finite number.",
      "ja": "有限の数値を入力してください。",
    }),
    "runtime.078": Object.freeze({
      "zh-CN": "@@0@@：@@1@@ → @@2@@。@@3@@",
      "en": "@@0@@: @@1@@ → @@2@@. @@3@@",
      "ja": "@@0@@：@@1@@ → @@2@@。@@3@@",
    }),
    "runtime.079": Object.freeze({
      "zh-CN": "桌面浮层偏好已原子保存；下次 Spica 启动生效。",
      "en": "Desktop overlay preferences were saved atomically; they take effect the next time Spica starts.",
      "ja": "デスクトップオーバーレイ設定をアトミックに保存しました。次回の Spica 起動時に反映されます。",
    }),
    "runtime.080": Object.freeze({
      "zh-CN": "保存失败：@@0@@。请重新预览。",
      "en": "Save failed: @@0@@. Please preview again.",
      "ja": "保存に失敗しました：@@0@@。もう一度プレビューしてください。",
    }),
    "runtime.081": Object.freeze({
      "zh-CN": "未命名插件",
      "en": "Unnamed plugin",
      "ja": "名前のないプラグイン",
    }),
    "runtime.082": Object.freeze({
      "zh-CN": "已列入配置：@@0@@ · 下次启动：@@1@@ · 健康检查：@@2@@",
      "en": "Configured: @@0@@ · Next launch: @@1@@ · Health check: @@2@@",
      "ja": "設定済み：@@0@@ · 次回起動：@@1@@ · ヘルスチェック：@@2@@",
    }),
    "runtime.083": Object.freeze({
      "zh-CN": "@@0@@ · @@1@@ · 只读",
      "en": "@@0@@ · @@1@@ · Read-only",
      "ja": "@@0@@ · @@1@@ · 読み取り専用",
    }),
    "runtime.084": Object.freeze({
      "zh-CN": "当前没有已配置的插件状态。",
      "en": "No configured plugin status is available.",
      "ja": "設定済みプラグインの状態はありません。",
    }),
    "runtime.085": Object.freeze({
      "zh-CN": "环境变量：@@0@@ · 值的来源：@@1@@ · 环境层：@@2@@",
      "en": "Environment variable: @@0@@ · Value source: @@1@@ · Environment layer: @@2@@",
      "ja": "環境変数：@@0@@ · 値のソース：@@1@@ · 環境レイヤー：@@2@@",
    }),
    "runtime.086": Object.freeze({
      "zh-CN": "安全值：@@0@@",
      "en": "Safe value: @@0@@",
      "ja": "安全な値：@@0@@",
    }),
    "runtime.087": Object.freeze({
      "zh-CN": "未配置；生产配置负责模块将使用自身默认值。",
      "en": "Not configured; the production configuration owner will use its own default.",
      "ja": "未設定です。実運用の設定管理モジュールが自身のデフォルト値を使用します。",
    }),
    "runtime.088": Object.freeze({
      "zh-CN": "负责模块契约异常 · 已强制只读",
      "en": "Owner contract error · Forced read-only",
      "ja": "管理モジュールの契約エラー · 読み取り専用に強制",
    }),
    "runtime.089": Object.freeze({
      "zh-CN": "服务端没有返回环境专属设置。",
      "en": "The server did not return environment-specific settings.",
      "ja": "サーバーから環境固有の設定が返されませんでした。",
    }),
    "runtime.090": Object.freeze({
      "zh-CN": "配置状态不可用。",
      "en": "Configuration status is unavailable.",
      "ja": "設定状態を取得できません。",
    }),
    "runtime.091": Object.freeze({
      "zh-CN": "文件权限：@@0@@ · 内容解析：@@1@@ · 旧版条目：@@2@@",
      "en": "File permissions: @@0@@ · Content parsing: @@1@@ · Legacy entries: @@2@@",
      "ja": "ファイル権限：@@0@@ · 内容の解析：@@1@@ · 旧形式の項目：@@2@@",
    }),
    "runtime.092": Object.freeze({
      "zh-CN": "当前没有映射到 app.yaml 的环境覆盖。",
      "en": "There are no environment overrides mapped to app.yaml.",
      "ja": "app.yaml に対応する環境オーバーライドはありません。",
    }),
    "runtime.093": Object.freeze({
      "zh-CN": "受影响的配置键不可用",
      "en": "Affected configuration keys are unavailable",
      "ja": "影響を受ける設定キーを取得できません",
    }),
    "runtime.094": Object.freeze({
      "zh-CN": "预览清理",
      "en": "Preview removal",
      "ja": "削除をプレビュー",
    }),
    "runtime.095": Object.freeze({
      "zh-CN": "仓库 xiaosan.env 中没有可清理的 app.yaml 映射环境覆盖。",
      "en": "The repository xiaosan.env has no removable environment overrides mapped to app.yaml.",
      "ja": "リポジトリの xiaosan.env には、削除できる app.yaml 対応の環境オーバーライドがありません。",
    }),
    "runtime.096": Object.freeze({
      "zh-CN": "写入能力未开放；仅显示安全来源摘要。",
      "en": "Write capability is unavailable; only a safe source summary is shown.",
      "ja": "書き込み機能は利用できません。安全なソース概要のみ表示します。",
    }),
    "runtime.097": Object.freeze({
      "zh-CN": "命令类型",
      "en": "Command type",
      "ja": "コマンド種別",
    }),
    "runtime.098": Object.freeze({
      "zh-CN": "目标",
      "en": "Target",
      "ja": "対象",
    }),
    "runtime.099": Object.freeze({
      "zh-CN": "密钥变化",
      "en": "Secret change",
      "ja": "シークレットの変更",
    }),
    "runtime.100": Object.freeze({
      "zh-CN": "受影响的配置键",
      "en": "Affected configuration keys",
      "ja": "影響を受ける設定キー",
    }),
    "runtime.101": Object.freeze({
      "zh-CN": "修改前的下次启动值",
      "en": "Next-launch value before the change",
      "ja": "変更前の次回起動値",
    }),
    "runtime.102": Object.freeze({
      "zh-CN": "修改后的下次启动值",
      "en": "Next-launch value after the change",
      "ja": "変更後の次回起動値",
    }),
    "runtime.103": Object.freeze({
      "zh-CN": "最终生效来源",
      "en": "Winning source",
      "ja": "最終的に有効なソース",
    }),
    "runtime.104": Object.freeze({
      "zh-CN": "仍被更高优先级覆盖",
      "en": "Still shadowed by a higher-priority source",
      "ja": "引き続き優先度の高いソースに上書きされています",
    }),
    "runtime.105": Object.freeze({
      "zh-CN": "是否同时收紧文件权限",
      "en": "Harden file permissions at the same time",
      "ja": "同時にファイル権限を厳格化するか",
    }),
    "runtime.106": Object.freeze({
      "zh-CN": "解析错误",
      "en": "Parse error",
      "ja": "解析エラー",
    }),
    "runtime.107": Object.freeze({
      "zh-CN": "敏感配置文件已按负责模块协议保存；现有密钥明文从未返回页面。",
      "en": "The sensitive configuration file was saved according to the owner protocol; existing secret plaintext was never returned to the page.",
      "ja": "機密設定ファイルを管理モジュールのプロトコルに従って保存しました。既存シークレットの平文がページに返されることはありません。",
    }),
    "runtime.108": Object.freeze({
      "zh-CN": "提交失败：@@0@@。请重新预览。",
      "en": "Commit failed: @@0@@. Please preview again.",
      "ja": "確定に失敗しました：@@0@@。もう一度プレビューしてください。",
    }),
    "runtime.109": Object.freeze({
      "zh-CN": "@@0@@的写入或回滚能力未开放。",
      "en": "Write or rollback capability is unavailable for @@0@@.",
      "ja": "@@0@@の書き込みまたはロールバック機能は利用できません。",
    }),
    "runtime.110": Object.freeze({
      "zh-CN": "恢复点 · @@0@@",
      "en": "Restore point · @@0@@",
      "ja": "復元ポイント · @@0@@",
    }),
    "runtime.111": Object.freeze({
      "zh-CN": "恢复点暂时不可查询。",
      "en": "Restore points are temporarily unavailable.",
      "ja": "復元ポイントを一時的に取得できません。",
    }),
    "runtime.112": Object.freeze({
      "zh-CN": "配置范围",
      "en": "Configuration scope",
      "ja": "設定範囲",
    }),
    "runtime.113": Object.freeze({
      "zh-CN": "回滚范围",
      "en": "Rollback scope",
      "ja": "ロールバック範囲",
    }),
    "runtime.114": Object.freeze({
      "zh-CN": "整个敏感配置文件（ManagedDocument）",
      "en": "Entire sensitive configuration file (ManagedDocument)",
      "ja": "機密設定ファイル全体（ManagedDocument）",
    }),
    "runtime.115": Object.freeze({
      "zh-CN": "保持不变",
      "en": "Unchanged",
      "ja": "変更なし",
    }),
    "runtime.116": Object.freeze({
      "zh-CN": " · 最终生效来源 @@0@@ → @@1@@",
      "en": " · Winning source @@0@@ → @@1@@",
      "ja": " · 最終的に有効なソース @@0@@ → @@1@@",
    }),
    "runtime.117": Object.freeze({
      "zh-CN": "@@0@@",
      "en": "@@0@@",
      "ja": "@@0@@",
    }),
    "runtime.118": Object.freeze({
      "zh-CN": "已映射的环境覆盖",
      "en": "Mapped environment overrides",
      "ja": "対応付けられた環境オーバーライド",
    }),
    "runtime.119": Object.freeze({
      "zh-CN": "未由配置中心管理的内容",
      "en": "Content not managed by Config Studio",
      "ja": "設定スタジオが管理していない内容",
    }),
    "runtime.120": Object.freeze({
      "zh-CN": "@@0@@ · 变化数量 @@1@@",
      "en": "@@0@@ · Number of changes @@1@@",
      "ja": "@@0@@ · 変更数 @@1@@",
    }),
    "runtime.121": Object.freeze({
      "zh-CN": "变化的配置键",
      "en": "Changed configuration keys",
      "ja": "変更された設定キー",
    }),
    "runtime.122": Object.freeze({
      "zh-CN": "下次启动值发生变化的配置键",
      "en": "Configuration keys with changed next-launch values",
      "ja": "次回起動値が変わる設定キー",
    }),
    "runtime.123": Object.freeze({
      "zh-CN": "安全截断的省略数量",
      "en": "Items omitted by safe truncation",
      "ja": "安全な切り詰めで省略された件数",
    }),
    "runtime.124": Object.freeze({
      "zh-CN": "变化的配置键省略 @@0@@ · 下次启动值字段省略 @@1@@",
      "en": "Changed configuration keys omitted: @@0@@ · Next-launch value fields omitted: @@1@@",
      "ja": "変更された設定キーの省略数：@@0@@ · 次回起動値フィールドの省略数：@@1@@",
    }),
    "runtime.125": Object.freeze({
      "zh-CN": "变化的配置键省略 @@0@@",
      "en": "Changed configuration keys omitted: @@0@@",
      "ja": "変更された設定キーの省略数：@@0@@",
    }),
    "runtime.126": Object.freeze({
      "zh-CN": "回滚预览失败：@@0@@",
      "en": "Rollback preview failed: @@0@@",
      "ja": "ロールバックのプレビューに失敗しました：@@0@@",
    }),
    "runtime.127": Object.freeze({
      "zh-CN": "@@0@@已按语义确认回滚。",
      "en": "The semantic rollback of @@0@@ was confirmed.",
      "ja": "@@0@@のセマンティックロールバックを確認しました。",
    }),
    "runtime.128": Object.freeze({
      "zh-CN": "回滚失败：@@0@@。请重新准备。",
      "en": "Rollback failed: @@0@@. Please prepare it again.",
      "ja": "ロールバックに失敗しました：@@0@@。もう一度準備してください。",
    }),
    "runtime.129": Object.freeze({
      "zh-CN": "@@0@@ 项需要注意",
      "en": "@@0@@ items need attention",
      "ja": "@@0@@ 件の確認が必要です",
    }),
    "runtime.130": Object.freeze({
      "zh-CN": "负责模块解析正常",
      "en": "Owner resolution is healthy",
      "ja": "管理モジュールの解決は正常です",
    }),
    "runtime.131": Object.freeze({
      "zh-CN": "生产配置负责模块已返回一致的下次启动快照。",
      "en": "The production configuration owner returned a consistent next-launch snapshot.",
      "ja": "実運用の設定管理モジュールが一貫した次回起動スナップショットを返しました。",
    }),
    "runtime.132": Object.freeze({
      "zh-CN": "服务端能力尚未开放；没有启动任何自检任务。",
      "en": "The server capability is not available; no self-check job was started.",
      "ja": "サーバー機能はまだ利用できません。セルフチェックタスクは開始されていません。",
    }),
    "runtime.133": Object.freeze({
      "zh-CN": "新的自检已安全停用；仍可查询或取消已有任务。",
      "en": "New self-checks are safely disabled; existing jobs can still be queried or cancelled.",
      "ja": "新しいセルフチェックは安全に無効化されています。既存タスクの確認やキャンセルは引き続き可能です。",
    }),
    "runtime.134": Object.freeze({
      "zh-CN": "@@0@@ 秒",
      "en": "@@0@@ seconds",
      "ja": "@@0@@ 秒",
    }),
    "runtime.135": Object.freeze({
      "zh-CN": "等待进度",
      "en": "Waiting for progress",
      "ja": "進行状況を待っています",
    }),
    "runtime.136": Object.freeze({
      "zh-CN": "没有运行中的检查",
      "en": "No checks are running",
      "ja": "実行中のチェックはありません",
    }),
    "runtime.137": Object.freeze({
      "zh-CN": "已安全丢弃 @@0@@ 行 stderr@@1@@；原文不会进入页面。",
      "en": "Safely discarded @@0@@ lines of stderr@@1@@; the original text is not shown on the page.",
      "ja": "stderr を @@0@@ 行、安全に破棄しました@@1@@。元のテキストはページに表示されません。",
    }),
    "runtime.138": Object.freeze({
      "zh-CN": "没有向页面返回原始 stderr。",
      "en": "Raw stderr was not returned to the page.",
      "ja": "元の stderr はページに返されていません。",
    }),
    "runtime.139": Object.freeze({
      "zh-CN": "自检状态暂时无法刷新；将继续重试。",
      "en": "The self-check status cannot be refreshed temporarily; retries will continue.",
      "ja": "セルフチェックの状態を一時的に更新できません。再試行を続けます。",
    }),
    "runtime.140": Object.freeze({
      "zh-CN": "轻量自检已排队。",
      "en": "The lightweight self-check was queued.",
      "ja": "軽量セルフチェックをキューに追加しました。",
    }),
    "runtime.141": Object.freeze({
      "zh-CN": "轻量自检未启动：@@0@@",
      "en": "The lightweight self-check did not start: @@0@@",
      "ja": "軽量セルフチェックを開始できませんでした：@@0@@",
    }),
    "runtime.142": Object.freeze({
      "zh-CN": "重检查已由服务端确认并排队。",
      "en": "The full check was confirmed by the server and queued.",
      "ja": "詳細チェックはサーバーで確認され、キューに追加されました。",
    }),
    "runtime.143": Object.freeze({
      "zh-CN": "重检查未启动：@@0@@",
      "en": "The full check did not start: @@0@@",
      "ja": "詳細チェックを開始できませんでした：@@0@@",
    }),
    "runtime.144": Object.freeze({
      "zh-CN": "已请求取消自检任务。",
      "en": "Self-check cancellation was requested.",
      "ja": "セルフチェックタスクのキャンセルを要求しました。",
    }),
    "runtime.145": Object.freeze({
      "zh-CN": "取消失败：@@0@@",
      "en": "Cancellation failed: @@0@@",
      "ja": "キャンセルに失敗しました：@@0@@",
    }),
    "runtime.146": Object.freeze({
      "zh-CN": "已有自检任务暂时无法查询。",
      "en": "Existing self-check jobs are temporarily unavailable.",
      "ja": "既存のセルフチェックタスクを一時的に取得できません。",
    }),
    "runtime.147": Object.freeze({
      "zh-CN": "无法读取配置目录",
      "en": "Unable to read the configuration directory",
      "ja": "設定ディレクトリを読み取れません",
    }),
    "runtime.148": Object.freeze({
      "zh-CN": "请从配置中心启动器重新打开此页面。",
      "en": "Please reopen this page from the Config Studio launcher.",
      "ja": "設定スタジオのランチャーからこのページを開き直してください。",
    }),
    "runtime.149": Object.freeze({
      "zh-CN": "安全会话未建立；未读取任何配置。",
      "en": "A secure session was not established; no configuration was read.",
      "ja": "安全なセッションを確立できませんでした。設定は一切読み取られていません。",
    }),
    "runtime.150": Object.freeze({
      "zh-CN": "配置中心无法建立安全的本机会话。",
      "en": "Config Studio could not establish a secure local session.",
      "ja": "設定スタジオは安全なローカルセッションを確立できませんでした。",
    }),
  });

  function localeFromLocation() {
    const searchParams = new URL(window.location.href).searchParams;
    const requested = searchParams.get("lang");
    return SUPPORTED_LOCALES.includes(requested) ? requested : "zh-CN";
  }

  const state = {
    locale: localeFromLocation(),
    csrf: null,
    meta: null,
    fields: [],
    managedDocuments: [],
    managedDocumentsOmitted: null,
    pluginStatuses: [],
    environmentOnlySettings: [],
    level: "basic",
    category: "all",
    query: "",
    appWriteEnabled: false,
    appRecoveryOnly: true,
    catalogFieldsComplete: false,
    appWriteBusy: false,
    appEditorDirty: false,
    selectedField: null,
    draftOperations: new Map(),
    appPreview: null,
    overlayWriteEnabled: false,
    overlayWriteBusy: false,
    overlayEditorDirty: false,
    overlayPreview: null,
    sensitiveWriteEnabled: false,
    sensitiveWriteBusy: false,
    sensitivePreview: null,
    secretSlots: new Map(),
    rollbackEnabled: false,
    rollbackBusy: false,
    rollbackConfirmation: null,
    selfCheckEnabled: false,
    selfCheckJobsEnabled: false,
    selfCheckBusy: false,
    selfCheckJob: null,
    selfCheckPollTimer: null,
  };

  const byId = (id) => document.getElementById(id);
  const all = (selector) => Array.from(document.querySelectorAll(selector));
  const structuredReaders = new WeakMap();
  let fieldHelpSequence = 0;

  function localizedCopy(key, fallback = key) {
    return registryValue(STATIC_COPY, key)
      || registryValue(MESSAGE_COPY, key)
      || fallback;
  }

  function localizedMessage(key, replacements = []) {
    const rendered = registryValue(DYNAMIC_COPY, key) || key;
    return rendered.split(/@@(\d+)@@/).map((part, index) => (
      index % 2 === 0 ? part : String(replacements[Number(part)])
    )).join("");
  }

  function applyStaticTranslations() {
    document.title = localizedCopy("document.title");
    all("[data-i18n]").forEach((element) => {
      element.textContent = localizedCopy(element.dataset.i18n, element.textContent);
    });
    all("[data-i18n-placeholder]").forEach((element) => {
      element.setAttribute(
        "placeholder",
        localizedCopy(element.dataset.i18nPlaceholder, element.getAttribute("placeholder") || ""),
      );
    });
    all("[data-i18n-aria-label]").forEach((element) => {
      element.setAttribute(
        "aria-label",
        localizedCopy(element.dataset.i18nAriaLabel, element.getAttribute("aria-label") || ""),
      );
    });
    all("[data-check-label]").forEach((element) => {
      element.textContent = localizedProtocolLabel(
        CHECK_LABELS,
        element.dataset.checkLabel,
      );
    });
  }

  function syncLocaleControls() {
    document.documentElement.lang = state.locale;
    all(".language-button").forEach((button) => {
      const active = button.dataset.locale === state.locale;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", String(active));
    });
  }

  function hasUnsavedLocaleState() {
    const secretInput = byId("secret-value-input");
    return state.draftOperations.size > 0
      || state.appEditorDirty
      || Boolean(state.appPreview)
      || state.overlayEditorDirty
      || Boolean(state.overlayPreview)
      || Boolean(state.sensitivePreview)
      || Boolean(state.rollbackConfirmation)
      || state.appWriteBusy
      || state.overlayWriteBusy
      || state.sensitiveWriteBusy
      || state.rollbackBusy
      || Boolean(secretInput && secretInput.value.length > 0);
  }

  function requestLocaleChange(locale) {
    if (!SUPPORTED_LOCALES.includes(locale) || locale === state.locale) return;
    if (
      hasUnsavedLocaleState()
      && !window.confirm(localizedCopy("locale.unsaved_confirmation"))
    ) return;
    const destination = new URL(window.location.href);
    destination.searchParams.set("lang", locale);
    window.location.assign(destination.toString());
  }

  function text(value) {
    if (value === null || value === undefined) return "—";
    if (typeof value === "boolean") return value ? "true" : "false";
    if (typeof value === "object") return JSON.stringify(value);
    return String(value);
  }

  function boundedText(value, maximum = 180) {
    const rendered = typeof value === "string" ? value : String(value ?? "");
    const safe = rendered.replace(/[\u0000-\u001f\u007f]/g, " ").trim();
    return safe.length > maximum ? `${safe.slice(0, maximum - 1)}…` : safe;
  }

  function registryValue(registry, value) {
    const key = String(value ?? "");
    if (!Object.hasOwn(registry, key)) return null;
    const item = registry[key];
    if (
      item
      && typeof item === "object"
      && SUPPORTED_LOCALES.every((locale) => Object.hasOwn(item, locale))
    ) {
      return item[state.locale] || item["zh-CN"];
    }
    return item;
  }

  function localizedProtocolLabel(registry, value) {
    const code = boundedText(value || "unknown", 128);
    return registryValue(registry, code) || code;
  }

  function localizedProtocolLabelWithCode(registry, value) {
    const code = boundedText(value || "unknown", 128);
    const label = registryValue(registry, code);
    return label && label !== code ? `${label}（${code}）` : code;
  }

  function localizedHealthIssue(issue) {
    const code = boundedText(issue && issue.code ? issue.code : "HEALTH_ISSUE_UNKNOWN", 96);
    const label = registryValue(HEALTH_ISSUE_LABELS, code)
      || localizedMessage("runtime.001");
    return `${label}（${code}）`;
  }

  function setSession(kind, label) {
    const root = byId("session-state");
    if (!root) return;
    root.setAttribute("aria-label", label);
    const dot = root.querySelector(".status-dot");
    dot.className = `status-dot status-dot--${kind}`;
    root.querySelector("span:last-child").textContent = label;
  }

  function toast(message) {
    const item = document.createElement("div");
    item.className = "toast";
    item.textContent = message;
    byId("toast-region").append(item);
    window.setTimeout(() => item.remove(), 4600);
  }

  function bootstrapToken() {
    const fragment = new URLSearchParams(window.location.hash.replace(/^#/, ""));
    return fragment.get("bootstrap");
  }

  async function exchangeBootstrapToken(token, { clearFragment = false } = {}) {
    if (typeof token !== "string" || token.length < 22 || token.length > 256) {
      if (clearFragment) {
        history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
      }
      return false;
    }
    try {
      const response = await fetch("/api/v1/session/bootstrap", {
        method: "POST",
        credentials: "same-origin",
        headers: { "X-Spica-Bootstrap": token },
      });
      if (clearFragment) {
        history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
      }
      if (!response.ok) throw new Error("bootstrap rejected");
      const payload = await response.json();
      state.csrf = payload.csrf_token || null;
      return true;
    } catch (_error) {
      if (clearFragment) {
        history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
      }
      return false;
    }
  }

  async function refreshCsrf() {
    const payload = await getJson("/api/v1/session/csrf");
    if (!payload || typeof payload.csrf_token !== "string" || !payload.csrf_token) {
      throw new Error("session refresh rejected");
    }
    state.csrf = payload.csrf_token;
  }

  async function establishSession() {
    const fragmentToken = bootstrapToken();
    if (
      fragmentToken
      && await exchangeBootstrapToken(fragmentToken, { clearFragment: true })
    ) return;
    try {
      await refreshCsrf();
    } catch (_error) {
      await waitForManualBootstrap();
    }
  }

  function waitForManualBootstrap() {
    const gate = byId("manual-bootstrap");
    const input = byId("manual-bootstrap-token");
    const submit = byId("manual-bootstrap-submit");
    const status = byId("manual-bootstrap-status");
    gate.hidden = false;
    input.value = "";
    input.focus();
    return new Promise((resolve) => {
      const attempt = async () => {
        if (submit.disabled) return;
        const token = input.value.trim();
        input.value = "";
        submit.disabled = true;
        status.textContent = localizedMessage("runtime.002");
        const accepted = await exchangeBootstrapToken(token);
        submit.disabled = false;
        if (!accepted) {
          status.textContent = localizedMessage("runtime.003");
          input.focus();
          return;
        }
        gate.hidden = true;
        status.textContent = "";
        resolve();
      };
      submit.addEventListener("click", attempt);
      input.addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          attempt();
        }
      });
    });
  }

  async function getJson(path) {
    const response = await fetch(path, {
      credentials: "same-origin",
      headers: state.csrf ? { "X-Spica-CSRF": state.csrf } : {},
    });
    if (!response.ok) throw new Error(`request failed: ${response.status}`);
    return response.json();
  }

  async function postJson(path, body) {
    if (!state.csrf) throw new Error("CSRF_UNAVAILABLE");
    const response = await fetch(path, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-Spica-CSRF": state.csrf,
      },
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      let code = `HTTP_${response.status}`;
      try {
        const payload = await response.json();
        if (payload && payload.error && typeof payload.error.code === "string") {
          code = boundedText(payload.error.code, 64);
        }
      } catch (_error) {
        // Stable HTTP status fallback; response bodies are never surfaced.
      }
      throw new Error(code);
    }
    return response.json();
  }

  function fieldPath(field) {
    if (typeof field.path === "string") return field.path;
    if (typeof field.display_path === "string") return field.display_path;
    if (Array.isArray(field.path)) {
      return field.path.map((part) => {
        if (Object.hasOwn(part, "name")) return part.name;
        if (Object.hasOwn(part, "key")) return `[${JSON.stringify(part.key)}]`;
        if (Object.hasOwn(part, "index")) return `[${part.index}]`;
        return "?";
      }).join(".");
    }
    return "unknown";
  }

  function presentationForField(field) {
    const path = fieldPath(field);
    if (Object.hasOwn(FIELD_PRESENTATIONS, path)) {
      return registryValue(FIELD_PRESENTATIONS, path);
    }
    if (Object.hasOwn(KNOWN_DYNAMIC_FIELD_PRESENTATIONS, path)) {
      return registryValue(KNOWN_DYNAMIC_FIELD_PRESENTATIONS, path);
    }
    return null;
  }

  function attachFieldHelp(target, field) {
    const presentation = presentationForField(field);
    if (!presentation) return;
    fieldHelpSequence += 1;
    const tooltip = document.createElement("span");
    tooltip.id = `field-help-${fieldHelpSequence}`;
    tooltip.className = "field-help-tooltip";
    tooltip.setAttribute("role", "tooltip");
    const heading = document.createElement("strong");
    heading.textContent = presentation.title;
    const description = document.createElement("span");
    description.textContent = presentation.description;
    const advice = document.createElement("span");
    advice.className = "field-help-tooltip__advice";
    advice.textContent = localizedMessage("runtime.004", [presentation.advice]);
    const effect = document.createElement("span");
    effect.textContent = localizedMessage("runtime.005", [localizedProtocolLabel(EFFECT_LABELS, field.effect_policy || "owner-specific")]);
    const dependencies = document.createElement("span");
    const dependencyText = Array.isArray(field.dependencies) && field.dependencies.length
      ? field.dependencies.slice(0, 16).map((item) => (
        localizedMessage("runtime.006", [boundedText(item.display_path || localizedCopy("dynamic.unknown_config_key"), 128), text(item.expected_value)])
      )).join(" · ")
      : localizedMessage("runtime.007");
    dependencies.textContent = localizedMessage("runtime.008", [dependencyText]);
    const technical = document.createElement("code");
    technical.textContent = localizedMessage("runtime.009", [fieldPath(field)]);
    tooltip.append(heading, description, advice, effect, dependencies, technical);
    target.append(tooltip);
    target.setAttribute("aria-describedby", tooltip.id);
  }

  function presentationForOverlayField(field) {
    if (!field || typeof field.display_path !== "string") return null;
    return registryValue(OVERLAY_PRESENTATIONS, field.display_path);
  }

  function attachOverlayFieldHelp(row, input, field, presentation) {
    if (!presentation) return;
    fieldHelpSequence += 1;
    const tooltip = document.createElement("span");
    tooltip.id = `field-help-${fieldHelpSequence}`;
    tooltip.className = "field-help-tooltip";
    tooltip.setAttribute("role", "tooltip");
    const heading = document.createElement("strong");
    heading.textContent = presentation.title;
    const description = document.createElement("span");
    description.textContent = presentation.description;
    const advice = document.createElement("span");
    advice.className = "field-help-tooltip__advice";
    advice.textContent = localizedMessage("runtime.004", [presentation.advice]);
    const effect = document.createElement("span");
    effect.textContent = localizedMessage("runtime.010", [localizedProtocolLabel(EFFECT_LABELS, field.effect_policy || "next_spica_launch")]);
    const technical = document.createElement("code");
    technical.textContent = localizedMessage("runtime.011", [field.display_path]);
    tooltip.append(heading, description, advice, effect, technical);
    row.append(tooltip);
    input.setAttribute("aria-describedby", tooltip.id);
  }

  function categoryOf(field) {
    const path = fieldPath(field);
    return path.split(".")[0] || "other";
  }

  function sourceClass(kind) {
    if (kind === "env_override" || kind === "secret_tainted_env_override") {
      return "source-pill source-pill--env";
    }
    if (kind === "default") return "source-pill source-pill--default";
    return "source-pill";
  }

  function pathHealthText(field) {
    const health = field.path_health;
    if (!health || typeof health !== "object") return "—";
    return [
      localizedProtocolLabelWithCode(STATUS_LABELS, health.status),
      boundedText(health.code || "unknown", 64),
      localizedProtocolLabelWithCode(PATH_KIND_LABELS, health.expected_kind),
    ].join(" · ");
  }

  function fieldRow(field) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "field-row";
    button.dataset.path = fieldPath(field);
    button.innerHTML = `
      <span class="field-row__name"><strong></strong><code class="field-key"></code><small></small></span>
      <span class="field-value"></span>
      <span class="${sourceClass(field.source_kind)}"></span>`;
    const presentation = presentationForField(field);
    button.querySelector("strong").textContent = presentation
      ? presentation.title
      : fieldPath(field);
    const rawKey = button.querySelector(".field-key");
    rawKey.textContent = fieldPath(field);
    rawKey.hidden = !presentation;
    const pathSummary = field.path_health
      ? localizedMessage("runtime.012", [localizedProtocolLabel(STATUS_LABELS, field.path_health.status)])
      : "";
    const control = field.control || field.value_type || "field";
    button.querySelector("small").textContent = `${localizedProtocolLabel(CONTROL_LABELS, control)} · ${localizedProtocolLabel(OWNER_LABELS, field.owner || "production owner")}${pathSummary}`;
    button.querySelector(".field-value").textContent = text(field.next_launch_value);
    button.querySelector(".source-pill").textContent = localizedProtocolLabel(
      SOURCE_LABELS,
      field.source_kind,
    );
    attachFieldHelp(button, field);
    button.addEventListener("click", () => inspectField(field, button));
    return button;
  }

  function inspectField(field, row) {
    all(".field-row.is-selected").forEach((item) => item.classList.remove("is-selected"));
    if (row) row.classList.add("is-selected");
    const presentation = presentationForField(field);
    byId("inspector-title").textContent = presentation
      ? presentation.title
      : fieldPath(field);
    const details = [
      [localizedMessage("runtime.013"), fieldPath(field)],
      [localizedMessage("runtime.014"), presentation ? presentation.description : field.description || localizedMessage("runtime.015")],
      [localizedMessage("runtime.016"), presentation ? presentation.advice : localizedMessage("runtime.017")],
      [localizedMessage("runtime.018"), localizedProtocolLabelWithCode(CONTROL_LABELS, field.control || field.value_type)],
      [localizedMessage("runtime.019"), localizedProtocolLabelWithCode(LEVEL_LABELS, field.level || "advanced")],
      [localizedMessage("runtime.020"), field.file_present ? text(field.file_value) : localizedCopy("dynamic.not_written")],
      [localizedCopy("source.schema_default"), text(field.default_value)],
      [localizedCopy("dynamic.next_launch_value"), text(field.next_launch_value)],
      [localizedCopy("dynamic.value_source"), localizedProtocolLabelWithCode(SOURCE_LABELS, field.source_kind)],
      [localizedMessage("runtime.021"), field.environment_variable || "—"],
      [localizedCopy("dynamic.owner"), localizedProtocolLabelWithCode(OWNER_LABELS, field.owner || "—")],
      [localizedCopy("dynamic.effect_policy"), localizedProtocolLabelWithCode(EFFECT_LABELS, field.effect_policy || "—")],
      [localizedCopy("dynamic.path_health"), pathHealthText(field)],
      [
        localizedCopy("dynamic.allowed_values"),
        Array.isArray(field.literal_choices) && field.literal_choices.length
          ? field.literal_choices.map(text).join(" · ")
          : "—",
      ],
      [
        localizedCopy("dynamic.allowed_range"),
        field.minimum !== null || field.maximum !== null
          ? `${field.minimum ?? "−∞"} … ${field.maximum ?? "+∞"}`
          : "—",
      ],
      [
        localizedCopy("dynamic.dependencies"),
        Array.isArray(field.dependencies) && field.dependencies.length
          ? field.dependencies.map((item) => `${item.display_path} = ${text(item.expected_value)}`).join(" · ")
          : "—",
      ],
      [
        localizedCopy("dynamic.editing_status"),
        field.editable && appAuthoringEnabled()
          ? localizedMessage("runtime.022")
          : field.editable
            ? state.appRecoveryOnly
              ? localizedMessage("runtime.023")
              : localizedMessage("runtime.024")
            : localizedProtocolLabelWithCode(READONLY_REASON_LABELS, field.unsupported_reason || "read_only"),
      ],
    ];
    const list = document.createElement("dl");
    list.className = "detail-list";
    details.forEach(([name, value]) => {
      const item = document.createElement("div");
      item.className = "detail-item";
      const term = document.createElement("dt");
      const description = document.createElement("dd");
      term.textContent = name;
      description.textContent = text(value);
      item.append(term, description);
      list.append(item);
    });
    byId("inspector-content").replaceChildren(list);
    renderAppFieldEditor(field);
    if (byId("inspector-title").offsetParent !== null) {
      byId("inspector-title").focus({ preventScroll: true });
    }
  }

  function defaultForStructuredSchema(schema) {
    if (!schema || typeof schema !== "object") return null;
    if (Object.hasOwn(schema, "default")) return schema.default;
    if (Array.isArray(schema.anyOf)) {
      if (schema.anyOf.some((branch) => branch && branch.type === "null")) {
        return null;
      }
      return defaultForStructuredSchema(schema.anyOf[0]);
    }
    if (schema.type === "array") return [];
    if (schema.type === "object") return {};
    if (schema.type === "boolean") return false;
    if (schema.type === "integer" || schema.type === "number") return 0;
    if (schema.type === "string") return "";
    return null;
  }

  function structuredPrimitive(schema, value) {
    let control;
    if (Array.isArray(schema.enum) && schema.enum.length) {
      control = document.createElement("select");
      schema.enum.forEach((choice, index) => {
        const option = document.createElement("option");
        option.value = String(index);
        option.textContent = text(choice);
        option.selected = Object.is(choice, value);
        control.append(option);
      });
      structuredReaders.set(control, () => schema.enum[Number(control.value)]);
      return control;
    }
    control = document.createElement("input");
    control.autocomplete = "off";
    if (schema.type === "boolean") {
      control.type = "checkbox";
      control.checked = value === true;
      structuredReaders.set(control, () => control.checked === true);
      return control;
    }
    if (schema.type === "integer" || schema.type === "number") {
      control.type = "number";
      control.step = schema.type === "integer" ? "1" : "any";
      if (typeof schema.minimum === "number") control.min = String(schema.minimum);
      if (typeof schema.maximum === "number") control.max = String(schema.maximum);
      if (typeof value === "number" && Number.isFinite(value)) {
        control.value = String(value);
      }
      structuredReaders.set(control, () => {
        const number = Number(control.value);
        if (!Number.isFinite(number)) throw new Error("FIELD_VALUE_INVALID");
        if (schema.type === "integer" && !Number.isSafeInteger(number)) {
          throw new Error("FIELD_VALUE_INVALID");
        }
        return number;
      });
      return control;
    }
    if (schema.type === "string") {
      control.type = "text";
      control.spellcheck = false;
      control.value = typeof value === "string" ? value : "";
      structuredReaders.set(control, () => control.value);
      return control;
    }
    return null;
  }

  function renderStructuredEditor(schema, value, depth = 0) {
    if (!schema || typeof schema !== "object" || depth > 8) return null;
    if (Array.isArray(schema.anyOf)) {
      const nonNull = schema.anyOf.find((branch) => branch && branch.type !== "null");
      const nullable = schema.anyOf.some((branch) => branch && branch.type === "null");
      if (!nonNull || !nullable) return null;
      const root = document.createElement("div");
      root.className = "structured-optional";
      const toggleLabel = document.createElement("label");
      const toggle = document.createElement("input");
      toggle.type = "checkbox";
      toggle.checked = value !== null && value !== undefined;
      const toggleCopy = document.createElement("span");
      toggleCopy.textContent = localizedMessage("runtime.025");
      toggleLabel.append(toggle, toggleCopy);
      const childRoot = document.createElement("div");
      let child = null;
      const renderChild = () => {
        childRoot.replaceChildren();
        child = toggle.checked
          ? renderStructuredEditor(
            nonNull,
            value ?? defaultForStructuredSchema(nonNull),
            depth + 1,
          )
          : null;
        if (child) childRoot.append(child);
      };
      toggle.addEventListener("change", renderChild);
      renderChild();
      root.append(toggleLabel, childRoot);
      structuredReaders.set(root, () => {
        if (!toggle.checked) return null;
        const reader = child && structuredReaders.get(child);
        if (!reader) throw new Error("FIELD_EDITOR_UNAVAILABLE");
        return reader();
      });
      return root;
    }

    const primitive = structuredPrimitive(schema, value);
    if (primitive) return primitive;

    if (schema.type === "array" && schema.items) {
      const root = document.createElement("div");
      root.className = "structured-array";
      const rows = document.createElement("div");
      rows.className = "structured-rows";
      const entries = [];
      let add = null;
      const limitNote = document.createElement("p");
      limitNote.className = "operation-result";
      limitNote.textContent = localizedMessage("runtime.026");
      limitNote.hidden = true;
      const activeEntries = () => entries.filter((entry) => rows.contains(entry.row));
      const syncLimit = () => {
        const atLimit = activeEntries().length >= MAX_STRUCTURED_ITEMS;
        if (add) add.disabled = atLimit;
        limitNote.hidden = !atLimit;
      };
      const addEntry = (itemValue) => {
        if (activeEntries().length >= MAX_STRUCTURED_ITEMS) {
          syncLimit();
          return false;
        }
        const row = document.createElement("div");
        row.className = "structured-item";
        const child = renderStructuredEditor(schema.items, itemValue, depth + 1);
        if (!child) return false;
        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "structured-remove";
        remove.textContent = localizedMessage("runtime.027");
        remove.addEventListener("click", () => {
          row.remove();
          syncLimit();
        });
        row.append(child, remove);
        rows.append(row);
        entries.push({ row, child });
        syncLimit();
        return true;
      };
      const initialItems = Array.isArray(value) ? value : [];
      if (initialItems.length > MAX_STRUCTURED_ITEMS || !initialItems.every(addEntry)) {
        return null;
      }
      add = document.createElement("button");
      add.type = "button";
      add.className = "structured-add";
      add.textContent = localizedMessage("runtime.028");
      add.addEventListener("click", () => {
        addEntry(defaultForStructuredSchema(schema.items));
        syncLimit();
      });
      root.append(rows, add, limitNote);
      syncLimit();
      structuredReaders.set(root, () => entries
        .filter((entry) => rows.contains(entry.row))
        .map((entry) => {
          const reader = structuredReaders.get(entry.child);
          if (!reader) throw new Error("FIELD_EDITOR_UNAVAILABLE");
          return reader();
        }));
      return root;
    }

    if (schema.type === "object") {
      const root = document.createElement("div");
      root.className = "structured-object";
      const current = value && typeof value === "object" && !Array.isArray(value)
        ? value
        : {};
      const fixedEntries = [];
      const properties = schema.properties && typeof schema.properties === "object"
        ? schema.properties
        : {};
      const propertyEntries = Object.entries(properties);
      if (propertyEntries.length > MAX_STRUCTURED_ITEMS) return null;
      let fixedPropertiesComplete = true;
      propertyEntries.forEach(([name, childSchema]) => {
        const row = document.createElement("label");
        row.className = "structured-property";
        const caption = document.createElement("span");
        caption.textContent = boundedText(name, 128);
        const child = renderStructuredEditor(
          childSchema,
          Object.hasOwn(current, name)
            ? current[name]
            : defaultForStructuredSchema(childSchema),
          depth + 1,
        );
        if (!child) {
          fixedPropertiesComplete = false;
          return;
        }
        row.append(caption, child);
        root.append(row);
        fixedEntries.push({ name, child });
      });
      if (!fixedPropertiesComplete) return null;
      const dynamicEntries = [];
      const additional = schema.additionalProperties;
      let dynamicRows = null;
      if (additional && typeof additional === "object") {
        dynamicRows = document.createElement("div");
        dynamicRows.className = "structured-rows";
        let add = null;
        const limitNote = document.createElement("p");
        limitNote.className = "operation-result";
        limitNote.textContent = localizedMessage("runtime.026");
        limitNote.hidden = true;
        const activeEntries = () => dynamicEntries.filter(
          (entry) => dynamicRows.contains(entry.row),
        );
        const syncLimit = () => {
          const atLimit = activeEntries().length >= MAX_STRUCTURED_ITEMS;
          if (add) add.disabled = atLimit;
          limitNote.hidden = !atLimit;
        };
        const addDynamic = (key, itemValue) => {
          if (activeEntries().length >= MAX_STRUCTURED_ITEMS) {
            syncLimit();
            return false;
          }
          const row = document.createElement("div");
          row.className = "structured-map-row";
          const keyInput = document.createElement("input");
          keyInput.type = "text";
          keyInput.autocomplete = "off";
          keyInput.spellcheck = false;
          keyInput.maxLength = 128;
          keyInput.placeholder = "key";
          keyInput.value = typeof key === "string" ? key : "";
          const child = renderStructuredEditor(additional, itemValue, depth + 1);
          if (!child) return false;
          const remove = document.createElement("button");
          remove.type = "button";
          remove.className = "structured-remove";
          remove.textContent = localizedMessage("runtime.027");
          remove.addEventListener("click", () => {
            row.remove();
            syncLimit();
          });
          row.append(keyInput, child, remove);
          dynamicRows.append(row);
          dynamicEntries.push({ row, keyInput, child });
          syncLimit();
          return true;
        };
        const initialEntries = Object.entries(current)
          .filter(([name]) => !Object.hasOwn(properties, name));
        if (
          initialEntries.length > MAX_STRUCTURED_ITEMS
          || !initialEntries.every(([name, itemValue]) => addDynamic(name, itemValue))
        ) return null;
        add = document.createElement("button");
        add.type = "button";
        add.className = "structured-add";
        add.textContent = localizedMessage("runtime.029");
        add.addEventListener("click", () => {
          addDynamic("", defaultForStructuredSchema(additional));
          syncLimit();
        });
        root.append(dynamicRows, add, limitNote);
        syncLimit();
      }
      structuredReaders.set(root, () => {
        const result = {};
        fixedEntries.forEach(({ name, child }) => {
          const reader = structuredReaders.get(child);
          if (!reader) throw new Error("FIELD_EDITOR_UNAVAILABLE");
          result[name] = reader();
        });
        dynamicEntries.filter(
          (entry) => dynamicRows && dynamicRows.contains(entry.row),
        ).forEach((entry) => {
          const key = entry.keyInput.value.trim();
          if (!key || Object.hasOwn(result, key)) throw new Error("FIELD_VALUE_INVALID");
          const reader = structuredReaders.get(entry.child);
          if (!reader) throw new Error("FIELD_EDITOR_UNAVAILABLE");
          result[key] = reader();
        });
        return result;
      });
      return root;
    }
    return null;
  }

  function readScalarControl(field, control) {
    const kind = control.dataset.valueKind;
    if (kind === "boolean") return control.checked === true;
    if (kind === "literal") {
      const index = Number(control.value);
      if (!Number.isSafeInteger(index) || !Array.isArray(field.literal_choices)) {
        throw new Error("FIELD_VALUE_INVALID");
      }
      return field.literal_choices[index];
    }
    if (kind === "number") {
      const value = Number(control.value);
      if (!Number.isFinite(value)) throw new Error("FIELD_VALUE_INVALID");
      return value;
    }
    if (kind === "text") return control.value;
    throw new Error("FIELD_VALUE_INVALID");
  }

  function scalarEditor(field) {
    const current = field.file_present ? field.file_value : field.next_launch_value;
    const withNullableToggle = (control) => {
      if (field.nullable !== true) return control;
      const root = document.createElement("div");
      root.className = "nullable-scalar";
      const label = document.createElement("label");
      const toggle = document.createElement("input");
      toggle.type = "checkbox";
      toggle.checked = current === null;
      const copy = document.createElement("span");
      copy.textContent = localizedMessage("runtime.030");
      const hint = document.createElement("p");
      hint.className = "operation-result";
      hint.textContent = localizedMessage("runtime.031");
      const syncNullable = () => {
        control.disabled = toggle.checked;
      };
      toggle.addEventListener("change", syncNullable);
      label.append(toggle, copy);
      root.append(label, control, hint);
      syncNullable();
      structuredReaders.set(root, () => {
        if (toggle.checked) return null;
        return readScalarControl(field, control);
      });
      return root;
    };
    if (field.control === "switch") {
      const input = document.createElement("input");
      input.type = "checkbox";
      input.checked = current === true;
      input.dataset.valueKind = "boolean";
      return withNullableToggle(input);
    }
    if (field.control === "select" && Array.isArray(field.literal_choices)) {
      const select = document.createElement("select");
      select.dataset.valueKind = "literal";
      field.literal_choices.forEach((choice, index) => {
        const option = document.createElement("option");
        option.value = String(index);
        option.textContent = text(choice);
        option.selected = Object.is(choice, current);
        select.append(option);
      });
      return withNullableToggle(select);
    }
    if (field.control === "number") {
      const input = document.createElement("input");
      input.type = "number";
      input.step = "any";
      input.dataset.valueKind = "number";
      if (typeof field.minimum === "number") input.min = String(field.minimum);
      if (typeof field.maximum === "number") input.max = String(field.maximum);
      if (typeof current === "number" && Number.isFinite(current)) {
        input.value = String(current);
      }
      return withNullableToggle(input);
    }
    if (field.control === "text") {
      const input = document.createElement("input");
      input.type = "text";
      input.autocomplete = "off";
      input.spellcheck = false;
      input.dataset.valueKind = "text";
      input.value = typeof current === "string" ? current : "";
      return withNullableToggle(input);
    }
    if (field.control === "structured" && field.structured_schema) {
      return renderStructuredEditor(field.structured_schema, current);
    }
    return null;
  }

  function renderAppFieldEditor(field) {
    const editor = byId("field-editor");
    const controlRoot = byId("field-editor-control");
    state.appEditorDirty = false;
    state.selectedField = field;
    controlRoot.replaceChildren();
    editor.hidden = !field.editable;
    if (!field.editable) {
      syncAppControls();
      return;
    }
    const control = scalarEditor(field);
    if (control) {
      const label = document.createElement(
        field.control === "structured" || control.classList.contains("nullable-scalar")
          ? "div"
          : "label",
      );
      label.className = "field-editor__label";
      const caption = document.createElement("span");
      caption.textContent = localizedMessage("runtime.032", [fieldPath(field)]);
      control.classList.add("field-editor-value");
      label.append(caption, control);
      controlRoot.append(label);
    } else {
      const unsupported = document.createElement("p");
      unsupported.className = "operation-result";
      unsupported.textContent = localizedMessage("runtime.033");
      controlRoot.append(unsupported);
    }
    byId("field-editor-status").textContent = state.appRecoveryOnly
      ? localizedMessage("runtime.034")
      : !state.appWriteEnabled
        ? localizedMessage("runtime.035")
        : !state.catalogFieldsComplete
          ? localizedMessage("runtime.036")
          : !fieldAuthoringComplete(field)
            ? localizedMessage("runtime.037")
            : localizedMessage("runtime.038");
    syncAppControls();
  }

  function appAuthoringEnabled() {
    return state.appWriteEnabled && state.appRecoveryOnly === false;
  }

  function fieldAuthoringComplete(field) {
    return Boolean(field && field.authoring_complete === true);
  }

  function renderAppRecoveryState() {
    const notice = byId("app-recovery-notice");
    notice.hidden = !state.appRecoveryOnly;
    syncAppManualRepairGuidance(false);
    if (!state.appRecoveryOnly) return;
    state.appPreview = null;
    state.draftOperations.clear();
    const dialog = byId("app-preview-dialog");
    if (dialog.open) dialog.close();
    syncAppControls();
  }

  function renderCatalogCompletenessState() {
    byId("catalog-fields-incomplete-notice").hidden = state.catalogFieldsComplete;
  }

  function selectedScalarValue() {
    const field = state.selectedField;
    const control = byId("field-editor-control").querySelector(".field-editor-value");
    if (!field || !control) throw new Error("FIELD_EDITOR_UNAVAILABLE");
    const structuredReader = structuredReaders.get(control);
    if (structuredReader) return structuredReader();
    return readScalarControl(field, control);
  }

  function appDraftOperationsAllowed() {
    return state.catalogFieldsComplete || Array.from(
      state.draftOperations.values(),
    ).every((operation) => operation && operation.kind === "unset");
  }

  function invalidateAppPreview() {
    state.appPreview = null;
    const dialog = byId("app-preview-dialog");
    if (dialog.open) dialog.close();
    byId("app-preview-changes").replaceChildren();
    byId("operation-result").textContent = "";
  }

  function queueAppSet() {
    const field = state.selectedField;
    if (
      !appAuthoringEnabled()
      || state.appWriteBusy
      || !field
      || !field.editable
      || state.catalogFieldsComplete !== true
      || field.authoring_complete !== true
      || !Array.isArray(field.path)
    ) return;
    try {
      state.draftOperations.set(fieldPath(field), {
        kind: "set",
        path: field.path,
        value: selectedScalarValue(),
      });
      state.appEditorDirty = false;
      invalidateAppPreview();
      byId("field-editor-status").textContent = localizedMessage("runtime.039");
    } catch (error) {
      byId("field-editor-status").textContent = boundedText(error.message, 64);
    }
    syncAppControls();
  }

  function queueAppUnset() {
    const field = state.selectedField;
    if (
      !appAuthoringEnabled()
      || state.appWriteBusy
      || !field
      || !field.editable
      || !field.file_present
      || !Array.isArray(field.path)
    ) return;
    state.draftOperations.set(fieldPath(field), {
      kind: "unset",
      path: field.path,
    });
    state.appEditorDirty = false;
    invalidateAppPreview();
    byId("field-editor-status").textContent = localizedMessage("runtime.040");
    syncAppControls();
  }

  function syncAppControls() {
    const field = state.selectedField;
    const scalarAvailable = Boolean(
      field
      && (
        ["switch", "select", "number", "text"].includes(field.control)
        || (
          field.control === "structured"
          && field.structured_schema
          && byId("field-editor-control").querySelector(".field-editor-value")
        )
      ),
    );
    const canEdit = Boolean(
      field && field.editable && appAuthoringEnabled() && !state.appWriteBusy,
    );
    const canSet = canEdit
      && state.catalogFieldsComplete
      && fieldAuthoringComplete(field);
    byId("field-editor-set").disabled = !canSet || !scalarAvailable;
    byId("field-editor-unset").disabled = !canEdit || !field.file_present;
    byId("change-count").textContent = localizedMessage("runtime.041", [state.draftOperations.size]);
    byId("app-preview-button").disabled = !appAuthoringEnabled()
      || state.appWriteBusy
      || !appDraftOperationsAllowed()
      || state.draftOperations.size === 0;
    byId("app-preview-commit").disabled = !appAuthoringEnabled()
      || state.appWriteBusy
      || !appDraftOperationsAllowed()
      || !state.appPreview;
  }

  function renderAppPreview(preview) {
    const changes = Array.isArray(preview.changes) ? preview.changes : [];
    const rows = changes.slice(0, 64).map((change) => {
      const item = document.createElement("li");
      const title = document.createElement("strong");
      const values = document.createElement("span");
      title.textContent = boundedText(change.display_path || localizedMessage("runtime.042"), 128);
      values.textContent = localizedMessage("runtime.043", [text(change.file_value_before), text(change.file_value_after), text(change.next_launch_value_before), text(change.next_launch_value_after)]);
      item.append(title, values);
      if (change.file_value_shadowed || change.semantic_warning) {
        const warning = document.createElement("small");
        warning.textContent = boundedText(
          change.semantic_warning || localizedMessage("runtime.044"),
          180,
        );
        item.append(warning);
      }
      return item;
    });
    byId("app-preview-changes").replaceChildren(...rows);
    if (!rows.length) byId("app-preview-changes").textContent = localizedMessage("runtime.045");
  }

  async function previewAppChanges() {
    if (
      !appAuthoringEnabled()
      || state.appWriteBusy
      || !appDraftOperationsAllowed()
      || state.draftOperations.size === 0
    ) return;
    state.appWriteBusy = true;
    syncAppControls();
    try {
      const preview = await postJson("/api/v1/app/previews", {
        operations: Array.from(state.draftOperations.values()),
      });
      if (
        !preview
        || typeof preview.preview_id !== "string"
        || !Array.isArray(preview.changes)
      ) throw new Error("PREVIEW_INVALID");
      state.appPreview = preview;
      renderAppPreview(preview);
      byId("operation-result").textContent = preview.changed
        ? localizedMessage("runtime.046")
        : localizedMessage("runtime.047");
      byId("app-preview-dialog").showModal();
    } catch (error) {
      state.appPreview = null;
      toast(localizedMessage("runtime.048", [boundedText(error.message, 64)]));
    } finally {
      state.appWriteBusy = false;
      syncAppControls();
    }
  }

  async function commitAppPreview() {
    const preview = state.appPreview;
    if (!appAuthoringEnabled()
      || state.appWriteBusy
      || !appDraftOperationsAllowed()
      || !preview
    ) return;
    state.appWriteBusy = true;
    syncAppControls();
    try {
      await postJson("/api/v1/app/commits", { preview_id: preview.preview_id });
      state.draftOperations.clear();
      state.appPreview = null;
      byId("app-preview-dialog").close();
      await reloadStudioData();
      toast(localizedMessage("runtime.049"));
    } catch (error) {
      state.appPreview = null;
      byId("operation-result").textContent = localizedMessage("runtime.050", [boundedText(error.message, 64)]);
    } finally {
      state.appWriteBusy = false;
      syncAppControls();
    }
  }

  function searchTextForField(field) {
    const presentation = presentationForField(field) || {};
    return [
      presentation.title || "",
      presentation.description || "",
      presentation.advice || "",
      fieldPath(field),
      field.owner || "",
      field.source_kind || "",
      field.description || "",
      Array.isArray(field.literal_choices) ? field.literal_choices.join(" ") : "",
    ].join(" ").normalize("NFKC").toLocaleLowerCase();
  }

  function visibleFields() {
    const query = state.query.trim().normalize("NFKC").toLocaleLowerCase();
    return state.fields.filter((field) => {
      const category = categoryOf(field);
      const levelOk = state.level === "advanced" || (field.level || "basic") === "basic";
      const categoryOk = state.category === "all" || category === state.category;
      return levelOk && categoryOk && (
        !query || searchTextForField(field).includes(query)
      );
    });
  }

  function renderFeatured() {
    const root = byId("featured-fields");
    root.setAttribute("aria-busy", "false");
    root.replaceChildren(...state.fields.slice(0, 4).map(fieldRow));
    if (!state.fields.length) root.textContent = localizedMessage("runtime.051");
  }

  function renderCatalog() {
    const root = byId("catalog-fields");
    root.replaceChildren(...visibleFields().map(fieldRow));
    if (!root.children.length) {
      const empty = document.createElement("div");
      empty.className = "empty-state";
      empty.textContent = localizedMessage("runtime.052");
      root.append(empty);
    }
  }

  function renderCategories() {
    const categories = ["all", ...new Set(state.fields.map(categoryOf))];
    const root = byId("category-filter");
    root.replaceChildren(...categories.map((category) => {
      const button = document.createElement("button");
      button.type = "button";
      const active = category === state.category;
      button.className = `filter-chip${active ? " is-active" : ""}`;
      button.setAttribute("aria-pressed", String(active));
      button.textContent = registryValue(CATEGORY_LABELS, category) || category;
      button.addEventListener("click", () => {
        state.category = category;
        renderCategories();
        renderCatalog();
      });
      return button;
    }));
  }

  function renderSwitches() {
    const fields = state.fields.filter((field) => field.control === "switch" || fieldPath(field).endsWith(".backend"));
    byId("switch-grid").replaceChildren(...fields.map((field) => {
      const card = document.createElement("button");
      card.type = "button";
      card.className = "setting-card";
      const presentation = presentationForField(field);
      card.setAttribute(
        "aria-label",
        localizedMessage("runtime.053", [presentation ? presentation.title : fieldPath(field)]),
      );
      const copy = document.createElement("div");
      const title = document.createElement("h3");
      const rawKey = document.createElement("code");
      rawKey.className = "field-key";
      const detail = document.createElement("p");
      title.textContent = presentation ? presentation.title : fieldPath(field);
      rawKey.textContent = fieldPath(field);
      rawKey.hidden = !presentation;
      detail.textContent = `${localizedProtocolLabel(SOURCE_LABELS, field.source_kind)} · ${localizedProtocolLabel(EFFECT_LABELS, field.effect_policy || "owner-specific")}`;
      copy.append(title, rawKey, detail);
      const toggle = document.createElement("span");
      toggle.className = `toggle${field.next_launch_value === true ? " is-on" : ""}`;
      toggle.setAttribute("aria-hidden", "true");
      card.append(copy, toggle);
      attachFieldHelp(card, field);
      card.addEventListener("click", () => inspectField(field));
      return card;
    }));
  }

  function renderManagedDocuments() {
    const root = byId("character-documents");
    const completenessNotice = byId("managed-documents-incomplete-notice");
    const completenessCopy = byId("managed-documents-incomplete-copy");
    completenessNotice.hidden = state.managedDocumentsOmitted === 0;
    completenessCopy.textContent = Number.isSafeInteger(
      state.managedDocumentsOmitted,
    )
      ? localizedMessage("runtime.054", [state.managedDocumentsOmitted])
      : localizedCopy("character.completeness_unknown");
    const cards = state.managedDocuments
      .filter((documentInfo) => documentInfo.id !== "overlay_preferences")
      .map((documentInfo) => {
      const card = document.createElement("article");
      card.className = "document-card";
      const title = document.createElement("h3");
      title.textContent = registryValue(DOCUMENT_PRESENTATIONS, documentInfo.id)
        || documentInfo.title
        || documentInfo.id
        || localizedCopy("character.eyebrow");
      const owner = document.createElement("p");
      const health = documentInfo.health || {};
      const fieldCount = Array.isArray(documentInfo.fields) ? documentInfo.fields.length : 0;
      owner.textContent = localizedMessage("runtime.055", [localizedProtocolLabel(OWNER_LABELS, documentInfo.owner || "production owner"), fieldCount]);
      const status = document.createElement("p");
      status.textContent = `${localizedProtocolLabel(STATUS_LABELS, health.status)} · ${localizedProtocolLabel(EFFECT_LABELS, documentInfo.effect_policy || "owner-specific")}`;
      const documentTruncation = documentInfo.truncation
        && typeof documentInfo.truncation === "object"
        ? documentInfo.truncation
        : {};
      const truncationDetails = [
        ["strings", localizedMessage("runtime.056")],
        ["collections", localizedMessage("runtime.057")],
        ["depth", localizedMessage("runtime.058")],
        ["unsupported", localizedMessage("runtime.059")],
        ["total_bytes", localizedMessage("runtime.060")],
      ].flatMap(([key, label]) => {
        const count = documentTruncation[key];
        return Number.isSafeInteger(count) && count > 0
          ? [`${label} ${count}`]
          : [];
      });
      card.append(title, owner, status);
      if (truncationDetails.length) {
        const warning = document.createElement("p");
        warning.className = "document-card__warning";
        warning.textContent = localizedMessage("runtime.061", [truncationDetails.join(" · ")]);
        card.append(warning);
      }
      const details = document.createElement("details");
      details.className = "document-card__details";
      const summary = document.createElement("summary");
      summary.textContent = localizedMessage("runtime.062");
      const metadata = document.createElement("dl");
      metadata.className = "document-card__metadata";
      [
        [localizedMessage("runtime.063"), documentInfo.basename || localizedMessage("runtime.064")],
        [localizedMessage("runtime.065"), localizedProtocolLabelWithCode(SOURCE_LABELS, documentInfo.source_kind)],
        [localizedMessage("runtime.066"), localizedCopy(documentInfo.external === true ? "dynamic.yes" : "dynamic.no")],
        [localizedMessage("runtime.067"), health.code || localizedCopy("dynamic.none")],
        [localizedMessage("runtime.068"), localizedProtocolLabelWithCode(READONLY_REASON_LABELS, documentInfo.unsupported_reason || "read_only")],
      ].forEach(([label, value]) => {
        const row = document.createElement("div");
        const term = document.createElement("dt");
        const description = document.createElement("dd");
        term.textContent = label;
        description.textContent = text(value);
        row.append(term, description);
        metadata.append(row);
      });
      const fields = Array.isArray(documentInfo.fields)
        ? documentInfo.fields
        : [];
      const fieldList = document.createElement("div");
      fieldList.className = "document-field-list";
      fieldList.setAttribute("role", "list");
      fields.forEach((field) => {
        const row = document.createElement("article");
        row.className = "document-field-row";
        row.setAttribute("role", "listitem");
        const heading = document.createElement("strong");
        heading.textContent = text(field.display_path);
        const valueType = document.createElement("small");
        valueType.textContent = localizedProtocolLabel(CONTROL_LABELS, field.value_type);
        const currentLabel = document.createElement("span");
        currentLabel.textContent = localizedCopy("dynamic.current_value");
        const currentValue = document.createElement("code");
        currentValue.textContent = text(field.current_value);
        const defaultLabel = document.createElement("span");
        defaultLabel.textContent = localizedCopy("dynamic.default_value");
        const defaultValue = document.createElement("code");
        defaultValue.textContent = text(field.default_value);
        row.append(
          heading,
          valueType,
          currentLabel,
          currentValue,
          defaultLabel,
          defaultValue,
        );
        fieldList.append(row);
      });
      if (!fields.length) {
        fieldList.textContent = localizedMessage("runtime.069");
      }
      details.append(summary, metadata, fieldList);
      card.append(details);
      return card;
      });
    root.replaceChildren(...cards);
    if (!cards.length) root.textContent = localizedMessage("runtime.070");
  }

  function renderOverlayDocument() {
    state.overlayEditorDirty = false;
    const documentInfo = state.managedDocuments.find(
      (documentInfo) => documentInfo.id === "overlay_preferences",
    );
    const root = byId("overlay-fields");
    if (!documentInfo || !Array.isArray(documentInfo.fields)) {
      root.textContent = localizedMessage("runtime.071");
      byId("overlay-operation-result").textContent = "";
      return;
    }
    const rows = documentInfo.fields.slice(0, 32).map((field) => {
      const row = document.createElement("div");
      row.className = "overlay-field";
      const presentation = presentationForOverlayField(field);
      const label = document.createElement("label");
      const caption = document.createElement("span");
      caption.textContent = presentation
        ? presentation.title
        : boundedText(field.display_path || localizedMessage("runtime.072"), 128);
      const rawKey = document.createElement("code");
      rawKey.className = "field-key";
      rawKey.textContent = field.display_path;
      rawKey.hidden = !presentation;
      const input = document.createElement("input");
      input.type = "number";
      input.step = "any";
      if (typeof field.minimum === "number") input.min = String(field.minimum);
      if (typeof field.maximum === "number") input.max = String(field.maximum);
      if (typeof field.current_value === "number" && Number.isFinite(field.current_value)) {
        input.value = String(field.current_value);
      }
      input.disabled = !state.overlayWriteEnabled || field.editable !== true;
      label.append(caption, rawKey, input);
      const preview = document.createElement("button");
      preview.type = "button";
      preview.className = "secondary-button";
      preview.textContent = localizedMessage("runtime.073");
      preview.disabled = input.disabled || state.overlayWriteBusy;
      preview.addEventListener("click", () => previewOverlayField(field, input));
      row.append(label, preview);
      attachOverlayFieldHelp(row, input, field, presentation);
      return row;
    });
    root.replaceChildren(...rows);
    if (!rows.length) root.textContent = localizedMessage("runtime.074");
    byId("overlay-operation-result").textContent = state.overlayWriteEnabled
      ? localizedMessage("runtime.075")
      : localizedMessage("runtime.076");
  }

  async function previewOverlayField(field, input) {
    if (!state.overlayWriteEnabled || state.overlayWriteBusy) return;
    const value = Number(input.value);
    if (!Number.isFinite(value)) {
      byId("overlay-operation-result").textContent = localizedMessage("runtime.077");
      return;
    }
    state.overlayWriteBusy = true;
    renderOverlayDocument();
    try {
      const preview = await postJson("/api/v1/overlay/previews", {
        key: field.display_path,
        value,
      });
      if (!preview || typeof preview.preview_id !== "string") {
        throw new Error("PREVIEW_INVALID");
      }
      state.overlayPreview = preview;
      byId("overlay-preview-summary").textContent = localizedMessage("runtime.078", [boundedText(preview.key, 128), text(preview.file_value_before), text(preview.file_value_after), localizedCopy(preview.effect_policy === "next_spica_launch" ? "dynamic.effect_next_launch_sentence" : "dynamic.effect_owner_sentence")]);
      byId("overlay-preview-dialog").showModal();
    } catch (error) {
      state.overlayPreview = null;
      byId("overlay-operation-result").textContent = localizedMessage("runtime.048", [boundedText(error.message, 64)]);
    } finally {
      state.overlayWriteBusy = false;
      renderOverlayDocument();
      syncOverlayControls();
    }
  }

  function syncOverlayControls() {
    byId("overlay-preview-commit").disabled = !state.overlayWriteEnabled
      || state.overlayWriteBusy
      || !state.overlayPreview;
  }

  async function commitOverlayPreview() {
    const preview = state.overlayPreview;
    if (!state.overlayWriteEnabled || state.overlayWriteBusy || !preview) return;
    state.overlayWriteBusy = true;
    syncOverlayControls();
    try {
      await postJson("/api/v1/overlay/commits", {
        preview_id: preview.preview_id,
      });
      state.overlayPreview = null;
      byId("overlay-preview-dialog").close();
      await reloadStudioData();
      toast(localizedMessage("runtime.079"));
    } catch (error) {
      state.overlayPreview = null;
      byId("overlay-preview-summary").textContent = localizedMessage("runtime.080", [boundedText(error.message, 64)]);
    } finally {
      state.overlayWriteBusy = false;
      renderOverlayDocument();
      syncOverlayControls();
    }
  }

  function renderPluginStatuses() {
    const root = byId("plugin-statuses");
    const rows = state.pluginStatuses.slice(0, 256).map((plugin) => {
      const row = document.createElement("article");
      row.className = "readonly-status-row";
      row.setAttribute("role", "listitem");
      const head = document.createElement("div");
      head.className = "readonly-status-row__head";
      const name = document.createElement("strong");
      name.textContent = boundedText(plugin.name || localizedMessage("runtime.081"), 80);
      const packageStatus = ["present", "missing", "unsafe"].includes(
        plugin.package_status,
      ) ? plugin.package_status : "unknown";
      const status = document.createElement("span");
      status.className = `source-pill${packageStatus === "present" ? "" : " source-pill--env"}`;
      status.textContent = localizedProtocolLabel(STATUS_LABELS, packageStatus);
      head.append(name, status);
      const semantics = document.createElement("p");
      const nextLaunch = plugin.next_launch_enabled === true
        ? "enabled"
        : plugin.next_launch_enabled === false
          ? "disabled"
          : "unavailable";
      semantics.textContent = localizedMessage("runtime.082", [localizedCopy(plugin.configured === true ? "dynamic.yes" : "dynamic.no"), localizedProtocolLabel(STATUS_LABELS, nextLaunch), boundedText(plugin.package_health_code || "PLUGIN_HEALTH_UNKNOWN", 96)]);
      const owner = document.createElement("p");
      owner.textContent = localizedMessage("runtime.083", [localizedProtocolLabel(OWNER_LABELS, plugin.owner || "production owner"), localizedProtocolLabel(EFFECT_LABELS, plugin.effect_policy || "unavailable")]);
      row.append(head, semantics, owner);
      return row;
    });
    root.replaceChildren(...rows);
    if (!rows.length) root.textContent = localizedMessage("runtime.084");
  }

  function renderEnvironmentOnlySettings() {
    const root = byId("environment-only-settings");
    const rows = state.environmentOnlySettings.slice(0, 256).map((setting) => {
      const row = document.createElement("article");
      row.className = "readonly-status-row";
      row.setAttribute("role", "listitem");
      const head = document.createElement("div");
      head.className = "readonly-status-row__head";
      const name = document.createElement("strong");
      const settingId = boundedText(
        setting.id || setting.environment_variable || "unknown",
        96,
      );
      const settingLabel = registryValue(ENVIRONMENT_SETTING_LABELS, setting.id);
      name.textContent = settingLabel ? `${settingLabel}（${settingId}）` : settingId;
      const status = document.createElement("span");
      status.className = setting.configured === true
        ? "source-pill source-pill--env"
        : "source-pill source-pill--default";
      status.textContent = localizedProtocolLabel(
        STATUS_LABELS,
        setting.configured === true ? "configured" : "default",
      );
      head.append(name, status);
      const source = document.createElement("p");
      source.textContent = localizedMessage("runtime.085", [boundedText(setting.environment_variable || localizedCopy("dynamic.unavailable"), 128), localizedProtocolLabel(SOURCE_LABELS, setting.source_kind || "unknown"), localizedProtocolLabel(SOURCE_LABELS, setting.environment_layer || "no_override")]);
      const value = document.createElement("p");
      value.textContent = setting.configured === true
        ? localizedMessage("runtime.086", [boundedText(text(setting.configured_value), 180)])
        : localizedMessage("runtime.087");
      const owner = document.createElement("p");
      const readOnlyContract = setting.editable === false
        ? localizedProtocolLabel(READONLY_REASON_LABELS, setting.unsupported_reason || "read_only")
        : localizedMessage("runtime.088");
      owner.textContent = `${localizedProtocolLabel(OWNER_LABELS, setting.owner || "production owner")} · ${localizedProtocolLabel(EFFECT_LABELS, setting.effect_policy || "unavailable")} · ${readOnlyContract}`;
      row.append(head, source, value, owner);
      return row;
    });
    root.replaceChildren(...rows);
    if (!rows.length) root.textContent = localizedMessage("runtime.089");
  }

  function renderSensitiveStatus(meta) {
    const sensitive = meta.sensitive_document || {};
    const slots = sensitive.secret_slots || {};
    const sources = sensitive.secret_sources || {};
    const allowedSources = new Set(["inherited", "repo_dotenv", "parent_dotenv"]);
    const slotRows = Object.keys(slots).slice(0, 8).map((slot) => {
      const item = document.createElement("li");
      const name = document.createElement("span");
      const status = document.createElement("strong");
      const source = allowedSources.has(sources[slot]) ? sources[slot] : "unknown";
      const label = registryValue(SECRET_SLOT_LABELS, slot);
      name.textContent = label ? `${label}（${boundedText(slot, 64)}）` : boundedText(slot, 64);
      status.textContent = `${localizedProtocolLabel(STATUS_LABELS, slots[slot] === true ? "configured" : "not_configured")} · ${localizedProtocolLabel(SOURCE_LABELS, source)}`;
      item.append(name, status);
      return item;
    });
    byId("secret-slot-status").replaceChildren(...slotRows);
    if (!slotRows.length) byId("secret-slot-status").textContent = localizedMessage("runtime.090");
    state.secretSlots = new Map(
      Object.keys(slots).slice(0, 32).map((slot) => [slot, slots[slot] === true]),
    );
    const slotSelect = byId("secret-slot-select");
    const selectedSlot = slotSelect.value;
    const options = Array.from(state.secretSlots).map(([slot, configured]) => {
      const option = document.createElement("option");
      option.value = slot;
      const label = registryValue(SECRET_SLOT_LABELS, slot);
      const slotName = label ? `${label}（${boundedText(slot, 64)}）` : boundedText(slot, 64);
      option.textContent = `${slotName} · ${localizedProtocolLabel(STATUS_LABELS, configured ? "configured" : "not_configured")}`;
      option.selected = slot === selectedSlot;
      return option;
    });
    slotSelect.replaceChildren(...options);

    const healthText = (documentStatus) => {
      const status = documentStatus || {};
      const legacyCount = Array.isArray(status.legacy_entries)
        ? status.legacy_entries.length
        : 0;
      return localizedMessage("runtime.091", [localizedProtocolLabelWithCode(STATUS_LABELS, status.permission_health || "UNKNOWN"), localizedProtocolLabelWithCode(STATUS_LABELS, status.parse_health || "UNKNOWN"), legacyCount]);
    };
    byId("repo-env-health").textContent = healthText(sensitive);
    byId("parent-env-health").textContent = healthText(
      meta.parent_environment_document,
    );

    const sourceCounts = new Map();
    state.fields.forEach((field) => {
      if (!field.environment_variable) return;
      const source = field.environment_layer || "no_override";
      sourceCounts.set(source, (sourceCounts.get(source) || 0) + 1);
    });
    const sourceSummary = Array.from(sourceCounts.entries())
      .slice(0, 8)
      .map(([source, count]) => `${localizedProtocolLabel(SOURCE_LABELS, source)} ${count}`)
      .join(" · ");
    byId("override-source-summary").textContent = sourceSummary || localizedMessage("runtime.092");
    renderMappedOverrides(sensitive);
    syncSensitiveControls();
  }

  function renderMappedOverrides(sensitive) {
    const root = byId("mapped-overrides");
    const rows = Array.isArray(sensitive.managed_overrides)
      ? sensitive.managed_overrides
        .filter((item) => item && item.repo_defined === true)
        .slice(0, 64)
      : [];
    const cards = rows.map((item) => {
      const row = document.createElement("div");
      row.className = "mapped-override-row";
      const copy = document.createElement("div");
      const name = document.createElement("strong");
      const fields = document.createElement("small");
      name.textContent = boundedText(item.environment_variable, 128);
      fields.textContent = Array.isArray(item.affected_fields)
        ? item.affected_fields.slice(0, 8).map((field) => boundedText(field, 128)).join(" · ")
        : localizedMessage("runtime.093");
      copy.append(name, fields);
      const clear = document.createElement("button");
      clear.type = "button";
      clear.className = "secondary-button";
      clear.textContent = localizedMessage("runtime.094");
      clear.disabled = !state.sensitiveWriteEnabled || state.sensitiveWriteBusy;
      clear.addEventListener("click", () => previewMappedOverrideClear(item));
      row.append(copy, clear);
      return row;
    });
    root.replaceChildren(...cards);
    if (!cards.length) {
      root.textContent = state.sensitiveWriteEnabled
        ? localizedMessage("runtime.095")
        : localizedMessage("runtime.096");
    }
  }

  async function previewMappedOverrideClear(item) {
    if (
      !state.sensitiveWriteEnabled
      || state.sensitiveWriteBusy
      || !item
      || item.repo_defined !== true
      || typeof item.environment_variable !== "string"
    ) return;
    await previewSensitiveCommand({
      kind: "clear_mapped_override",
      environment_variable: item.environment_variable,
    });
  }

  function syncSensitiveControls() {
    const slot = byId("secret-slot-select").value;
    const canWrite = state.sensitiveWriteEnabled
      && !state.sensitiveWriteBusy
      && state.secretSlots.has(slot);
    byId("secret-slot-select").disabled = !state.sensitiveWriteEnabled
      || state.sensitiveWriteBusy
      || state.secretSlots.size === 0;
    byId("secret-value-input").disabled = !canWrite;
    byId("secret-set-preview").disabled = !canWrite
      || byId("secret-value-input").value.length === 0;
    byId("secret-clear-preview").disabled = !canWrite
      || state.secretSlots.get(slot) !== true;
    const clearRequired = Boolean(
      state.sensitivePreview
      && state.sensitivePreview.command_kind === "clear_secret",
    );
    byId("sensitive-clear-confirm-row").hidden = !clearRequired;
    byId("sensitive-preview-commit").disabled = !state.sensitiveWriteEnabled
      || state.sensitiveWriteBusy
      || !state.sensitivePreview
      || (clearRequired && !byId("sensitive-clear-confirm").checked);
  }

  function renderSensitivePreview(preview) {
    const targetLabel = registryValue(SECRET_SLOT_LABELS, preview.target);
    const target = targetLabel
      ? `${targetLabel}（${text(preview.target)}）`
      : text(preview.target);
    const details = [
      [localizedMessage("runtime.097"), localizedProtocolLabel(SENSITIVE_COMMAND_LABELS, preview.command_kind)],
      [localizedMessage("runtime.098"), target],
      [localizedMessage("runtime.099"), localizedProtocolLabel(SECRET_CHANGE_LABELS, preview.secret_change || "unchanged")],
      [localizedMessage("runtime.100"), Array.isArray(preview.affected_fields) ? preview.affected_fields.join(" · ") : "—"],
      [localizedMessage("runtime.101"), text(preview.before_next_launch)],
      [localizedMessage("runtime.102"), text(preview.after_next_launch)],
      [localizedMessage("runtime.103"), `${localizedProtocolLabel(SOURCE_LABELS, preview.winning_source_before)} → ${localizedProtocolLabel(SOURCE_LABELS, preview.winning_source_after)}`],
      [localizedMessage("runtime.104"), localizedCopy(preview.still_shadowed === true ? "dynamic.yes" : "dynamic.no")],
      [localizedMessage("runtime.105"), localizedCopy(preview.permission_hardening === true ? "dynamic.yes" : "dynamic.no")],
      [localizedMessage("runtime.106"), `${text(preview.resolution_error_before)} → ${text(preview.resolution_error_after)}`],
    ];
    const list = document.createElement("dl");
    list.className = "detail-list";
    details.forEach(([name, value]) => {
      const item = document.createElement("div");
      item.className = "detail-item";
      const term = document.createElement("dt");
      const description = document.createElement("dd");
      term.textContent = name;
      description.textContent = boundedText(value, 256);
      item.append(term, description);
      list.append(item);
    });
    byId("sensitive-preview-summary").replaceChildren(list);
  }

  async function previewSensitiveCommand(command) {
    if (!state.sensitiveWriteEnabled || state.sensitiveWriteBusy) return;
    state.sensitiveWriteBusy = true;
    syncSensitiveControls();
    try {
      const preview = await postJson("/api/v1/sensitive/previews", { command });
      if (
        !preview
        || typeof preview.preview_id !== "string"
        || typeof preview.command_kind !== "string"
      ) throw new Error("PREVIEW_INVALID");
      state.sensitivePreview = preview;
      byId("sensitive-clear-confirm").checked = false;
      renderSensitivePreview(preview);
      byId("sensitive-preview-dialog").showModal();
    } catch (error) {
      state.sensitivePreview = null;
      byId("sensitive-operation-result").textContent = localizedMessage("runtime.048", [boundedText(error.message, 64)]);
    } finally {
      state.sensitiveWriteBusy = false;
      syncSensitiveControls();
    }
  }

  async function previewSecretSet() {
    if (!state.sensitiveWriteEnabled || state.sensitiveWriteBusy) return;
    const slot = byId("secret-slot-select").value;
    const secretInput = byId("secret-value-input");
    const value = secretInput.value;
    secretInput.value = "";
    syncSensitiveControls();
    if (!state.secretSlots.has(slot) || !value) return;
    await previewSensitiveCommand({ kind: "set_secret", slot, value });
  }

  async function previewSecretClear() {
    if (!state.sensitiveWriteEnabled || state.sensitiveWriteBusy) return;
    const slot = byId("secret-slot-select").value;
    if (!state.secretSlots.has(slot) || state.secretSlots.get(slot) !== true) return;
    await previewSensitiveCommand({ kind: "clear_secret", slot });
  }

  async function commitSensitivePreview() {
    const preview = state.sensitivePreview;
    if (!state.sensitiveWriteEnabled || state.sensitiveWriteBusy || !preview) return;
    state.sensitiveWriteBusy = true;
    syncSensitiveControls();
    try {
      let confirmationReceipt = null;
      if (preview.command_kind === "clear_secret") {
        if (!byId("sensitive-clear-confirm").checked) return;
        const confirmation = await postJson(
          `/api/v1/sensitive/previews/${encodeURIComponent(preview.preview_id)}/confirm-clear`,
          {},
        );
        if (!confirmation || typeof confirmation.confirmation_receipt !== "string") {
          throw new Error("CONFIRMATION_INVALID");
        }
        confirmationReceipt = confirmation.confirmation_receipt;
      }
      const body = { preview_id: preview.preview_id };
      if (confirmationReceipt) body.confirmation_receipt = confirmationReceipt;
      await postJson("/api/v1/sensitive/commits", body);
      state.sensitivePreview = null;
      byId("sensitive-clear-confirm").checked = false;
      byId("sensitive-preview-dialog").close();
      await reloadStudioData();
      toast(localizedMessage("runtime.107"));
    } catch (error) {
      state.sensitivePreview = null;
      byId("sensitive-operation-result").textContent = localizedMessage("runtime.108", [boundedText(error.message, 64)]);
    } finally {
      state.sensitiveWriteBusy = false;
      renderSensitiveStatus(state.meta || {});
      syncSensitiveControls();
    }
  }

  function restoreLaneEnabled(lane) {
    if (!state.rollbackEnabled) return false;
    if (lane === "app") return state.appWriteEnabled && state.rollbackEnabled;
    if (lane === "overlay") return state.overlayWriteEnabled && state.rollbackEnabled;
    if (lane === "sensitive") {
      return state.sensitiveWriteEnabled && state.rollbackEnabled;
    }
    return false;
  }

  function syncAppManualRepairGuidance(hasValidRestorePoint) {
    const guidance = byId("app-manual-repair-guidance");
    guidance.hidden = !state.appRecoveryOnly || hasValidRestorePoint === true;
  }

  function renderRestorePoints(lane, points) {
    const root = byId(`restore-${lane}`);
    const availablePoints = Array.isArray(points) ? points : [];
    if (lane === "app") {
      syncAppManualRepairGuidance(availablePoints.length > 0);
    }
    if (!restoreLaneEnabled(lane)) {
      root.textContent = localizedMessage("runtime.109", [localizedProtocolLabel(LANE_LABELS, lane)]);
      return;
    }
    const rows = availablePoints.slice(0, 5).map((point) => {
      const row = document.createElement("div");
      row.className = "restore-point-row";
      const copy = document.createElement("div");
      const id = document.createElement("strong");
      const created = document.createElement("small");
      id.textContent = localizedMessage("runtime.110", [boundedText(point.restore_point_id, 64)]);
      const milliseconds = Number(point.created_at_ns) / 1000000;
      created.textContent = Number.isFinite(milliseconds)
        ? new Date(milliseconds).toLocaleString()
        : localizedCopy("dynamic.time_unavailable");
      copy.append(id, created);
      const prepare = document.createElement("button");
      prepare.type = "button";
      prepare.className = "secondary-button";
      prepare.textContent = localizedCopy("dynamic.prepare_rollback");
      prepare.disabled = state.rollbackBusy;
      prepare.addEventListener("click", () => prepareRollback(lane, point));
      row.append(copy, prepare);
      return row;
    });
    root.replaceChildren(...rows);
    if (!rows.length) root.textContent = localizedCopy("dynamic.no_restore_points");
  }

  async function loadRestorePoints() {
    for (const lane of Object.keys(RESTORE_ROUTES)) {
      if (!restoreLaneEnabled(lane)) {
        renderRestorePoints(lane, []);
        continue;
      }
      try {
        const payload = await getJson(RESTORE_ROUTES[lane].list);
        renderRestorePoints(lane, payload.restore_points);
      } catch (_error) {
        byId(`restore-${lane}`).textContent = localizedMessage("runtime.111");
      }
    }
  }

  function renderRollbackPreview(lane, confirmation) {
    const lines = [];
    lines.push([localizedMessage("runtime.112"), localizedProtocolLabel(LANE_LABELS, lane)]);
    lines.push([localizedCopy("dynamic.restore_point"), confirmation.restore_point_id]);
    if (lane === "sensitive") {
      lines.push([localizedMessage("runtime.113"), localizedMessage("runtime.114")]);
      const secretChanges = Array.isArray(confirmation.secret_changes)
        ? confirmation.secret_changes.map((item) => {
          const slotLabel = registryValue(SECRET_SLOT_LABELS, item.slot);
          const slot = slotLabel
            ? `${slotLabel}（${item.slot}）`
            : item.slot;
          return `${slot}：${localizedProtocolLabel(SECRET_CHANGE_LABELS, item.change)}`;
        })
        : [];
      lines.push([localizedCopy("secrets.slots"), secretChanges.join(" · ") || localizedMessage("runtime.115")]);
      const overrides = Array.isArray(confirmation.override_changes)
        ? confirmation.override_changes.map((item) => (
          `${item.environment_variable}: ${text(item.before_next_launch)} → ${text(item.after_next_launch)}`
          + localizedMessage("runtime.116", [localizedProtocolLabel(SOURCE_LABELS, item.winning_source_before), localizedProtocolLabel(SOURCE_LABELS, item.winning_source_after)])
          + localizedMessage("runtime.117", [item.still_shadowed ? localizedCopy("sensitive.still_shadowed") : ""])
        ))
        : [];
      lines.push([localizedMessage("runtime.118"), overrides.join(" · ") || localizedMessage("runtime.115")]);
      lines.push([localizedMessage("runtime.119"), localizedMessage("runtime.120", [localizedCopy(confirmation.unmanaged_content_changed === true ? "sensitive.content_changed" : "sensitive.content_unchanged"), text(confirmation.unmanaged_change_count)])]);
      lines.push([localizedMessage("runtime.105"), localizedCopy(confirmation.permission_hardening === true ? "dynamic.yes" : "dynamic.no")]);
    } else {
      lines.push([
        localizedMessage("runtime.121"),
        Array.isArray(confirmation.changed_fields)
          ? confirmation.changed_fields.join(" · ") || localizedCopy("dynamic.none")
          : localizedCopy("dynamic.unavailable"),
      ]);
      if (lane === "app") {
        lines.push([
          localizedMessage("runtime.122"),
          Array.isArray(confirmation.next_launch_changed_fields)
            ? confirmation.next_launch_changed_fields.join(" · ") || localizedCopy("dynamic.none")
            : localizedCopy("dynamic.unavailable"),
        ]);
      }
      lines.push([localizedMessage("runtime.119"), localizedMessage("runtime.120", [localizedCopy(confirmation.unmanaged_content_changed === true ? "sensitive.content_changed" : "sensitive.content_unchanged"), text(confirmation.unmanaged_change_count)])]);
      if (confirmation.truncation && confirmation.truncation.truncated === true) {
        const changedOmitted = Number.isSafeInteger(
          confirmation.truncation.changed_fields_omitted,
        ) ? confirmation.truncation.changed_fields_omitted : localizedCopy("dynamic.unavailable");
        const nextLaunchOmitted = Number.isSafeInteger(
          confirmation.truncation.next_launch_changed_fields_omitted,
        ) ? confirmation.truncation.next_launch_changed_fields_omitted : localizedCopy("dynamic.unavailable");
        lines.push([
          localizedMessage("runtime.123"),
          lane === "app"
            ? localizedMessage("runtime.124", [changedOmitted, nextLaunchOmitted])
            : localizedMessage("runtime.125", [changedOmitted]),
        ]);
      }
    }
    lines.push([
      localizedMessage("runtime.106"),
      `${text(confirmation.resolution_error_before)} → ${text(confirmation.resolution_error_after)}`,
    ]);
    const list = document.createElement("dl");
    list.className = "detail-list";
    lines.forEach(([name, value]) => {
      const item = document.createElement("div");
      item.className = "detail-item";
      const term = document.createElement("dt");
      const description = document.createElement("dd");
      term.textContent = name;
      description.textContent = boundedText(value, 512);
      item.append(term, description);
      list.append(item);
    });
    byId("rollback-preview-summary").replaceChildren(list);
  }

  async function prepareRollback(lane, point) {
    if (
      !restoreLaneEnabled(lane)
      || state.rollbackBusy
      || !point
      || typeof point.restore_point_id !== "string"
    ) return;
    state.rollbackBusy = true;
    syncRollbackControls();
    try {
      const routes = RESTORE_ROUTES[lane];
      const confirmation = await postJson(
        `${routes.list}/${encodeURIComponent(point.restore_point_id)}/prepare-rollback`,
        {},
      );
      if (
        !confirmation
        || typeof confirmation.confirmation_receipt !== "string"
        || confirmation.restore_point_id !== point.restore_point_id
      ) throw new Error("CONFIRMATION_INVALID");
      state.rollbackConfirmation = { lane, confirmation };
      byId("rollback-confirm").checked = false;
      renderRollbackPreview(lane, confirmation);
      byId("rollback-preview-dialog").showModal();
    } catch (error) {
      state.rollbackConfirmation = null;
      byId("restore-operation-result").textContent = localizedMessage("runtime.126", [boundedText(error.message, 64)]);
    } finally {
      state.rollbackBusy = false;
      syncRollbackControls();
    }
  }

  function syncRollbackControls() {
    byId("rollback-preview-commit").disabled = state.rollbackBusy
      || !state.rollbackConfirmation
      || !byId("rollback-confirm").checked;
  }

  async function commitRollback() {
    const record = state.rollbackConfirmation;
    if (
      !record
      || !restoreLaneEnabled(record.lane)
      || state.rollbackBusy
      || !byId("rollback-confirm").checked
    ) return;
    state.rollbackBusy = true;
    syncRollbackControls();
    try {
      const confirmation = record.confirmation;
      await postJson(RESTORE_ROUTES[record.lane].rollback, {
        confirmation_receipt: confirmation.confirmation_receipt,
      });
      state.rollbackConfirmation = null;
      byId("rollback-confirm").checked = false;
      byId("rollback-preview-dialog").close();
      await reloadStudioData();
      toast(localizedMessage("runtime.127", [localizedProtocolLabel(LANE_LABELS, record.lane)]));
    } catch (error) {
      state.rollbackConfirmation = null;
      byId("restore-operation-result").textContent = localizedMessage("runtime.128", [boundedText(error.message, 64)]);
    } finally {
      state.rollbackBusy = false;
      syncRollbackControls();
      await loadRestorePoints();
    }
  }

  function applyMeta(meta) {
    state.meta = meta;
    const health = meta.health || {};
    const issues = Array.isArray(health.issues) ? health.issues : [];
    byId("config-health").textContent = issues.length ? localizedMessage("runtime.129", [issues.length]) : localizedMessage("runtime.130");
    byId("health-detail").textContent = issues.length ? localizedHealthIssue(issues[0]) : localizedMessage("runtime.131");
    byId("health-meter").className = issues.length ? "is-warning" : "is-healthy";
    const hasOverride = state.fields.some((field) => field.file_value_shadowed);
    byId("override-warning").hidden = !hasOverride;
    state.appWriteEnabled = Boolean(
      meta.capabilities && meta.capabilities.app_config_write === true,
    );
    state.overlayWriteEnabled = Boolean(
      meta.capabilities && meta.capabilities.overlay_write === true,
    );
    state.sensitiveWriteEnabled = Boolean(
      meta.capabilities && meta.capabilities.sensitive_write === true,
    );
    state.rollbackEnabled = Boolean(
      meta.capabilities && meta.capabilities.rollback === true,
    );
    state.selfCheckEnabled = Boolean(
      meta.capabilities && meta.capabilities.self_check === true,
    );
    state.selfCheckJobsEnabled = Boolean(
      meta.capabilities && meta.capabilities.self_check_jobs === true,
    );
    renderSensitiveStatus(meta);
    if (!state.selfCheckJobsEnabled) {
      byId("self-check-job-summary").textContent = localizedMessage("runtime.132");
    } else if (!state.selfCheckEnabled) {
      byId("self-check-job-summary").textContent = localizedMessage("runtime.133");
    }
    syncAppControls();
    syncOverlayControls();
    syncSensitiveControls();
    syncRollbackControls();
    syncSelfCheckControls();
  }

  function validSelfCheckJob(job) {
    return Boolean(
      job
      && typeof job === "object"
      && typeof job.job_id === "string"
      && /^[A-Za-z0-9_-]{8,128}$/.test(job.job_id)
      && typeof job.status === "string"
      && JOB_STATUSES.has(job.status)
      && (job.mode === "light" || job.mode === "full")
      && Array.isArray(job.progress)
      && Array.isArray(job.results),
    );
  }

  function replaceSummaryList(root, items, emptyMessage) {
    const children = items.map((label) => {
      const item = document.createElement("li");
      item.textContent = label;
      return item;
    });
    if (!children.length) {
      const empty = document.createElement("li");
      empty.textContent = emptyMessage;
      children.push(empty);
    }
    root.replaceChildren(...children);
  }

  function stopSelfCheckPolling() {
    if (state.selfCheckPollTimer !== null) {
      window.clearTimeout(state.selfCheckPollTimer);
      state.selfCheckPollTimer = null;
    }
  }

  function scheduleSelfCheckPoll() {
    stopSelfCheckPolling();
    if (
      !state.selfCheckJobsEnabled
      || !state.selfCheckJob
      || !ACTIVE_JOB_STATUSES.has(state.selfCheckJob.status)
    ) return;
    state.selfCheckPollTimer = window.setTimeout(pollSelfCheck, SELF_CHECK_POLL_MS);
  }

  function renderSelfCheckJob(job) {
    if (!validSelfCheckJob(job)) throw new Error("SELF_CHECK_JOB_INVALID");
    state.selfCheckJob = job;
    const active = ACTIVE_JOB_STATUSES.has(job.status);
    const duration = Number.isFinite(job.duration_s) && job.duration_s >= 0
      ? localizedMessage("runtime.134", [Math.min(job.duration_s, 86400).toFixed(1)])
      : "—";
    const status = boundedText(job.status, 32);
    const displayStatus = localizedProtocolLabel(STATUS_LABELS, status);
    const badge = byId("self-check-status-badge");
    badge.textContent = displayStatus;
    badge.dataset.status = status;
    byId("self-check-job-status").textContent = displayStatus;
    const modeLabel = localizedCopy(
      job.mode === "full" ? "self_check.mode_full" : "self_check.mode_light",
    );
    byId("self-check-job-summary").textContent = `${modeLabel} · ${duration}${job.error_code ? ` · ${boundedText(job.error_code, 64)}` : ""}`;
    byId("self-check-summary").textContent = displayStatus;
    byId("self-check-monitor").setAttribute("aria-busy", String(active));

    const progress = job.progress.slice(0, MAX_RENDERED_PROGRESS).map((item) => {
      const name = item && typeof item.name === "string" ? item.name : "unknown";
      return `${localizedProtocolLabelWithCode(CHECK_LABELS, name)} · ${localizedProtocolLabel(STATUS_LABELS, "RUNNING")}`;
    });
    replaceSummaryList(byId("self-check-progress"), progress, active ? localizedMessage("runtime.135") : localizedMessage("runtime.136"));

    const results = job.results.slice(0, MAX_RENDERED_RESULTS).map((item) => {
      const name = item && typeof item.name === "string" ? boundedText(item.name, 48) : "unknown";
      const resultStatus = item && RESULT_STATUSES.has(item.status) ? item.status : "UNVERIFIED";
      const reason = item && typeof item.reason === "string" ? boundedText(item.reason, 160) : "";
      return `${localizedProtocolLabelWithCode(CHECK_LABELS, name)} · ${localizedProtocolLabel(STATUS_LABELS, resultStatus)}${reason ? ` — ${localizedCopy("self_check.technical_detail")}：${reason}` : ""}`;
    });
    replaceSummaryList(byId("self-check-results"), results, localizedCopy("self_check.no_results"));

    const totalLines = Number.isSafeInteger(job.stderr_total_line_count)
      ? Math.min(Math.max(job.stderr_total_line_count, 0), 1000000)
      : 0;
    byId("self-check-output-note").textContent = totalLines
      ? localizedMessage("runtime.137", [totalLines, job.stderr_truncated ? localizedCopy("self_check.stderr_count_truncated") : ""])
      : localizedMessage("runtime.138");
    syncSelfCheckControls();
    scheduleSelfCheckPoll();
  }

  async function pollSelfCheck() {
    const jobId = state.selfCheckJob && state.selfCheckJob.job_id;
    if (!state.selfCheckJobsEnabled || !jobId) return;
    try {
      const job = await getJson(`/api/v1/self-check/jobs/${encodeURIComponent(jobId)}`);
      renderSelfCheckJob(job);
    } catch (_error) {
      toast(localizedMessage("runtime.139"));
      if (state.selfCheckJob && ACTIVE_JOB_STATUSES.has(state.selfCheckJob.status)) {
        state.selfCheckPollTimer = window.setTimeout(pollSelfCheck, SELF_CHECK_POLL_MS);
      }
    }
  }

  async function loadSelfChecks() {
    if (!state.selfCheckJobsEnabled) return;
    const payload = await getJson("/api/v1/self-check/jobs");
    const jobs = payload && Array.isArray(payload.jobs) ? payload.jobs.slice(0, 20) : [];
    const job = jobs.find((item) => validSelfCheckJob(item) && ACTIVE_JOB_STATUSES.has(item.status))
      || jobs.find(validSelfCheckJob);
    if (job) renderSelfCheckJob(job);
  }

  function selectedHeavyChecks() {
    const selected = new Set(
      all('input[name="self-check-heavy"]:checked').map((input) => input.value),
    );
    return HEAVY_CHECKS.filter((name) => selected.has(name));
  }

  function syncSelfCheckControls() {
    const jobActive = Boolean(
      state.selfCheckJob && ACTIVE_JOB_STATUSES.has(state.selfCheckJob.status),
    );
    const canStart = state.selfCheckEnabled && !state.selfCheckBusy && !jobActive;
    byId("self-check-light-start").disabled = !canStart;
    byId("self-check-heavy-controls").disabled = !canStart;

    const selected = selectedHeavyChecks();
    const enableLlm = byId("self-check-enable-llm");
    const llmSelected = selected.includes("llm");
    enableLlm.disabled = !canStart || !llmSelected;
    if (!llmSelected) enableLlm.checked = false;
    const confirmLlm = byId("self-check-confirm-llm");
    confirmLlm.disabled = !canStart || !enableLlm.checked;
    if (!enableLlm.checked) confirmLlm.checked = false;

    const includeDisabled = byId("self-check-include-disabled");
    const confirmIncludeDisabled = byId("self-check-confirm-include-disabled");
    confirmIncludeDisabled.disabled = !canStart || !includeDisabled.checked;
    if (!includeDisabled.checked) confirmIncludeDisabled.checked = false;

    const allowDownloads = byId("self-check-allow-downloads");
    const confirmDownloads = byId("self-check-confirm-downloads");
    confirmDownloads.disabled = !canStart || !allowDownloads.checked;
    if (!allowDownloads.checked) confirmDownloads.checked = false;

    const confirmed = byId("self-check-confirm-heavy").checked
      && (!enableLlm.checked || confirmLlm.checked)
      && (!includeDisabled.checked || confirmIncludeDisabled.checked)
      && (!allowDownloads.checked || confirmDownloads.checked);
    byId("self-check-heavy-start").disabled = !canStart || !selected.length || !confirmed;

    const cancel = byId("self-check-cancel");
    const canCancel = state.selfCheckJobsEnabled
      && jobActive
      && !state.selfCheckBusy
      && state.selfCheckJob.status !== "CANCELLING";
    cancel.hidden = !state.selfCheckJobsEnabled || !jobActive;
    cancel.disabled = !canCancel;
  }

  function heavyCommand() {
    return {
      mode: "full",
      only: selectedHeavyChecks(),
      llm: byId("self-check-enable-llm").checked,
      include_disabled: byId("self-check-include-disabled").checked,
      allow_model_downloads: byId("self-check-allow-downloads").checked,
    };
  }

  function heavyAcknowledgements() {
    return {
      full: byId("self-check-confirm-heavy").checked,
      llm: byId("self-check-confirm-llm").checked,
      include_disabled: byId("self-check-confirm-include-disabled").checked,
      model_downloads: byId("self-check-confirm-downloads").checked,
    };
  }

  function confirmationMatches(command, confirmation) {
    const semantic = confirmation && confirmation.semantic;
    return Boolean(
      semantic
      && semantic.mode === "full"
      && Array.isArray(semantic.checks)
      && semantic.checks.length === command.only.length
      && semantic.checks.every((name, index) => name === command.only[index])
      && semantic.llm === command.llm
      && semantic.include_disabled === command.include_disabled
      && semantic.allow_model_downloads === command.allow_model_downloads,
    );
  }

  async function runLightSelfCheck() {
    if (!state.selfCheckEnabled || state.selfCheckBusy) return;
    state.selfCheckBusy = true;
    syncSelfCheckControls();
    try {
      const job = await postJson("/api/v1/self-check/jobs", { mode: "light" });
      renderSelfCheckJob(job);
      toast(localizedMessage("runtime.140"));
    } catch (error) {
      toast(localizedMessage("runtime.141", [boundedText(error.message, 64)]));
    } finally {
      state.selfCheckBusy = false;
      syncSelfCheckControls();
    }
  }

  async function runHeavySelfCheck() {
    if (!state.selfCheckEnabled || state.selfCheckBusy) return;
    const command = heavyCommand();
    if (!command.only.length || byId("self-check-heavy-start").disabled) return;
    state.selfCheckBusy = true;
    syncSelfCheckControls();
    try {
      const confirmation = await postJson("/api/v1/self-check/confirm", {
        ...command,
        acknowledgements: heavyAcknowledgements(),
      });
      if (
        typeof confirmation.confirmation_receipt !== "string"
        || !confirmation.confirmation_receipt
        || !confirmationMatches(command, confirmation)
      ) {
        throw new Error("CONFIRMATION_INVALID");
      }
      const job = await postJson("/api/v1/self-check/jobs", {
        ...command,
        confirmation_receipt: confirmation.confirmation_receipt,
      });
      byId("self-check-confirm-heavy").checked = false;
      byId("self-check-confirm-llm").checked = false;
      byId("self-check-confirm-include-disabled").checked = false;
      byId("self-check-confirm-downloads").checked = false;
      renderSelfCheckJob(job);
      toast(localizedMessage("runtime.142"));
    } catch (error) {
      toast(localizedMessage("runtime.143", [boundedText(error.message, 64)]));
    } finally {
      state.selfCheckBusy = false;
      syncSelfCheckControls();
    }
  }

  async function cancelSelfCheck() {
    const jobId = state.selfCheckJob && state.selfCheckJob.job_id;
    if (!state.selfCheckJobsEnabled || !jobId || state.selfCheckBusy) return;
    state.selfCheckBusy = true;
    syncSelfCheckControls();
    try {
      const job = await postJson(
        `/api/v1/self-check/jobs/${encodeURIComponent(jobId)}/cancel`,
        {},
      );
      renderSelfCheckJob(job);
      toast(localizedMessage("runtime.144"));
    } catch (error) {
      toast(localizedMessage("runtime.145", [boundedText(error.message, 64)]));
    } finally {
      state.selfCheckBusy = false;
      syncSelfCheckControls();
    }
  }

  function showView(name) {
    all(".nav-item").forEach((button) => {
      const active = button.dataset.view === name;
      button.classList.toggle("is-active", active);
      if (active) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    });
    let activePanel = null;
    all(".view-panel").forEach((panel) => {
      const active = panel.dataset.panel === name;
      panel.classList.toggle("is-active", active);
      if (active) activePanel = panel;
    });
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    byId("workspace").scrollTo({ top: 0, behavior: reducedMotion ? "auto" : "smooth" });
    const heading = activePanel && activePanel.querySelector("h1");
    if (heading) {
      heading.setAttribute("tabindex", "-1");
      heading.focus({ preventScroll: true });
    }
  }

  function bindUi() {
    applyStaticTranslations();
    all(".language-button").forEach((button) => button.addEventListener("click", () => {
      requestLocaleChange(button.dataset.locale);
    }));
    syncLocaleControls();
    all(".nav-item").forEach((button) => button.addEventListener("click", () => showView(button.dataset.view)));
    all("[data-view-target]").forEach((button) => button.addEventListener("click", () => showView(button.dataset.viewTarget)));
    all(".view-button").forEach((button) => button.addEventListener("click", () => {
      state.level = button.dataset.level;
      all(".view-button").forEach((item) => {
        const active = item === button;
        item.classList.toggle("is-active", active);
        item.setAttribute("aria-pressed", String(active));
      });
      renderCatalog();
    }));
    byId("field-search").addEventListener("input", (event) => {
      state.query = event.target.value;
      renderCatalog();
    });
    byId("field-editor-control").addEventListener("input", () => {
      state.appEditorDirty = true;
    });
    byId("field-editor-control").addEventListener("change", () => {
      state.appEditorDirty = true;
    });
    byId("overlay-fields").addEventListener("input", () => {
      state.overlayEditorDirty = true;
    });
    byId("field-editor-set").addEventListener("click", queueAppSet);
    byId("field-editor-unset").addEventListener("click", queueAppUnset);
    byId("app-preview-button").addEventListener("click", previewAppChanges);
    byId("app-preview-commit").addEventListener("click", commitAppPreview);
    byId("app-preview-dialog").addEventListener("close", () => {
      state.appPreview = null;
      syncAppControls();
    });
    byId("overlay-preview-commit").addEventListener("click", commitOverlayPreview);
    byId("overlay-preview-dialog").addEventListener("close", () => {
      state.overlayPreview = null;
      syncOverlayControls();
    });
    byId("secret-slot-select").addEventListener("change", syncSensitiveControls);
    byId("secret-value-input").addEventListener("input", syncSensitiveControls);
    byId("secret-set-preview").addEventListener("click", previewSecretSet);
    byId("secret-clear-preview").addEventListener("click", previewSecretClear);
    byId("sensitive-clear-confirm").addEventListener("change", syncSensitiveControls);
    byId("sensitive-preview-commit").addEventListener("click", commitSensitivePreview);
    byId("sensitive-preview-dialog").addEventListener("close", () => {
      state.sensitivePreview = null;
      byId("sensitive-clear-confirm").checked = false;
      syncSensitiveControls();
    });
    byId("rollback-confirm").addEventListener("change", syncRollbackControls);
    byId("rollback-preview-commit").addEventListener("click", commitRollback);
    byId("rollback-preview-dialog").addEventListener("close", () => {
      state.rollbackConfirmation = null;
      byId("rollback-confirm").checked = false;
      syncRollbackControls();
    });
    byId("self-check-light-start").addEventListener("click", runLightSelfCheck);
    byId("self-check-heavy-start").addEventListener("click", runHeavySelfCheck);
    byId("self-check-cancel").addEventListener("click", cancelSelfCheck);
    [
      ...all('input[name="self-check-heavy"]'),
      byId("self-check-enable-llm"),
      byId("self-check-include-disabled"),
      byId("self-check-allow-downloads"),
      byId("self-check-confirm-heavy"),
      byId("self-check-confirm-llm"),
      byId("self-check-confirm-include-disabled"),
      byId("self-check-confirm-downloads"),
    ].forEach((input) => input.addEventListener("change", syncSelfCheckControls));
    syncAppControls();
    syncOverlayControls();
    syncSensitiveControls();
    syncRollbackControls();
    syncSelfCheckControls();
    document.addEventListener("keydown", (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLocaleLowerCase() === "k") {
        event.preventDefault();
        byId("field-search").focus();
      }
    });
  }

  async function start() {
    bindUi();
    try {
      await establishSession();
      await reloadStudioData();
      setSession("ok", localizedCopy("session.ready"));
      if (state.selfCheckJobsEnabled) {
        try {
          await loadSelfChecks();
        } catch (_error) {
          toast(localizedMessage("runtime.146"));
        }
      }
    } catch (_error) {
      setSession("error", localizedCopy("session.unavailable"));
      byId("config-health").textContent = localizedMessage("runtime.147");
      byId("health-detail").textContent = localizedMessage("runtime.148");
      byId("featured-fields").setAttribute("aria-busy", "false");
      byId("featured-fields").textContent = localizedMessage("runtime.149");
      toast(localizedMessage("runtime.150"));
    }
  }

  async function reloadStudioData() {
    const [meta, catalog] = await Promise.all([
      getJson("/api/v1/meta"),
      getJson("/api/v1/catalog"),
    ]);
    state.fields = Array.isArray(catalog.fields) ? catalog.fields : [];
    state.catalogFieldsComplete = catalog.fields_complete === true;
    state.managedDocuments = Array.isArray(catalog.managed_documents)
      ? catalog.managed_documents
      : [];
    const managedDocumentsOmitted = catalog.truncation
      && typeof catalog.truncation === "object"
      ? catalog.truncation.managed_documents_omitted
      : null;
    state.managedDocumentsOmitted = Number.isSafeInteger(managedDocumentsOmitted)
      && managedDocumentsOmitted >= 0
      && managedDocumentsOmitted <= MAX_STRUCTURED_ITEMS
      ? managedDocumentsOmitted
      : null;
    state.pluginStatuses = Array.isArray(catalog.plugin_statuses)
      ? catalog.plugin_statuses
      : [];
    state.environmentOnlySettings = Array.isArray(
      catalog.environment_only_settings,
    ) ? catalog.environment_only_settings : [];
    const metaHealth = meta && typeof meta.health === "object" ? meta.health : null;
    state.appRecoveryOnly = catalog.recovery_only !== false;
    if (!metaHealth || metaHealth.recovery_only !== false) {
      state.appRecoveryOnly = true;
    }
    state.selectedField = null;
    byId("field-editor").hidden = true;
    applyMeta(meta);
    renderAppRecoveryState();
    renderCatalogCompletenessState();
    renderFeatured();
    renderCategories();
    renderCatalog();
    renderSwitches();
    renderManagedDocuments();
    renderOverlayDocument();
    renderPluginStatuses();
    renderEnvironmentOnlySettings();
    await loadRestorePoints();
  }

  window.addEventListener("DOMContentLoaded", start, { once: true });
})();
