[简体中文](README.md) · [English](README.en.md) · **日本語** · [ホーム](../README.ja.md)

# 設定とカスタマイズ：サンプルから作る

文字会話から始め、キャラクター、声、会話スタイルを追加します。コマンドは有効化した `spica` 環境で、**プロジェクトのルート**から実行してください。基本依存は先に[メイン README](../README.ja.md)の手順でインストールします。

[起動](#setup) · [読み込み](#import) · [カードと立ち絵](#character) · [音声](#voice) · [アニメーション](#animation) · [会話スタイル](#dialogue) · [本機設定の変更](#settings)

<a id="setup"></a>
## 1. 文字会話を起動する

UTF-8 の `xiaosan.env` を作り、自分の API キーを記入します。この例は [DeepSeek 互換 API](https://api-docs.deepseek.com/)を使います。キーは[コンソール](https://platform.deepseek.com/)で取得し、別のサービスではモデルと接続先を変更してください。

```dotenv
OPENAI_API_KEY=REPLACE_WITH_YOUR_API_KEY
```

`data/config/app.yaml` に次の項目を反映し、ほかの設定は残します。モデルが未配置でも試せるよう、追加機能は最初は無効にします。

```yaml
llm:
  provider: openai_compatible
  model: deepseek-v4-flash
  base_url: https://api.deepseek.com
  reasoning_effort: none
  system_turn_reasoning_effort: none
character:
  interlocutor_name: Friend
  dialog_display_language: zh
  package_dir: null
  profile_override: null
tts:
  enabled: false
stt:
  backend: qwen_asr
  mic_backend: generic
  model: models/stt/Qwen3-ASR-1.7B
  device: cpu
  compute_type: float32
  language: zh
  warmup_on_startup: false
screen:
  enabled: false
anime:
  enabled: false
song:
  enabled: false
```

初回は同梱の小さな静止画サンプルを読み込んで起動します。すでにキャラクターを設定している場合は最後の行だけ実行します。

```bash
python -c "from pathlib import Path; from spica.host.character_packages import import_character_folder; from spica.config.manager import ConfigManager; p = import_character_folder('Desktop-Packs/Characters/Examples/static', Path('data/runtime/characters')); ConfigManager().update({'character': {'package_dir': p.package_root, 'profile_override': None}}); print(p.name)"
python -X utf8 webui_qt.py
```

入力に文字で返答があれば成功です。字幕は `zh` が中国語、`ja` が日本語で、音声言語の切り替えではありません。普通のマイクは `mic_backend: generic` を使います。キーは `xiaosan.env` に置き、古い環境変数 `MODEL`、`OPENAI_BASE_URL`、`SPICA_USER_NAME` の競合値は取り除きます。

<a id="import"></a>
## 2. 既存のキャラクターとスタイルを使う

1. [キャラクター素材パック](https://pan.baidu.com/s/1RDoz_iRvFJdB71Aia3EPaw?pwd=j2u6)をダウンロードし、コード **`j2u6`** で ZIP を展開します。
2. 歯車の設定 → **导入角色文件夹** → `meta.json` を直接含むフォルダーを選びます。
3. 会話スタイルは別に、`style.json` を含むフォルダーを読み込みます。
4. 完了後、**重启程序**（再起動）を押します。キャラクターとスタイルは独立して選択し、再起動後に反映されます。

元データは `Desktop-Packs/Characters/` と `Desktop-Packs/Dialogue-Styles/` に置くと整理できます。インストール済みのコピーはアプリが `data/runtime/` で管理します。ZIP や集合フォルダーではなく個別のパックを選んでください。デスクトップ版はモバイル／ロボット用背景を表示しません。会話の背景は[スタイル](#dialogue)で変更します。

<a id="character"></a>
## 3. カードと立ち絵を作る

[静止画サンプル](../Desktop-Packs/Characters/Examples/static/)を `Desktop-Packs/Characters/my-character/` にコピーし、構造を残します。

```text
my-character/
├── meta.json
├── persona.md
├── worldbook.md
└── visuals/
    ├── idle.png
    └── happy.png
```

オープンソースの [CCEditor](https://lenml.github.io/CCEditor/)（[ソース](https://github.com/lenML/CCEditor)）でカードを書くか、`persona.md` を直接編集します。外部カードの内容は次のように移します。

| 内容 | 記入先 |
| --- | --- |
| 名前 | `meta.json` の `name` は一覧名、`char_name` はキャラクター名 |
| 身元、性格、口調、会話例 | `persona.md` |
| 世界観、人物関係 | `worldbook.md`。`worldbook_file` の参照を残す |
| 独立したキャラクター ID | `slug` を `my-character` に変更。同じキャラクターの更新では維持 |

最小の `persona.md` の例：

```markdown
あなたは {{char}}。デスクトップで {{user}} と過ごしています。
穏やかで好奇心があり、自然で短い返答をします。ときどき軽い冗談を言います。
ユーザーは {{user}} と呼び、原作主人公の名前は使いません。
```

SillyTavern / CCEditor の PNG カードや V2/V3 JSON からは本文を取り出してください。`meta.json` に改名するだけでは変換できません。

`idle.png` と `happy.png` を、同じキャンバスサイズ・人物位置の透明 PNG に置き換えます。表情を増やすには `visuals.sprites` に画像を登録し、衣装の `emotions` で `"happy": ["happy"]` のように ID を参照します。対応感情は `happy/angry/sad/surprised`。衣装追加は `costumes` の一項目をコピーし、`id`、`label`、`default_sprite` と画像の対応を変更します。

パスはパック内の相対パスと `/` を使い、絶対パス、`..`、大文字小文字だけが違うファイル名を避けます。保存後に再インポートして再起動します。追加項目は[形式リファレンス](CHARACTER_PACKS.md)を参照してください。

<a id="voice"></a>
## 4. 音声を追加する（任意）

| 用途 | 必要なモデル |
| --- | --- |
| 会話の声 | GPT-SoVITS **v2Pro / v2ProPlus** の対応する GPT `.ckpt`、SoVITS `.pth`、参照 WAV |
| マイク認識 | `Qwen3-ASR-1.7B` モデル一式 |
| 歌声 | RVC v2 `.pth`、任意で対応する `.index`。会話モデルとは別 |

**実行環境は一度だけ準備します。** 基本依存の後、NVIDIA / Python 3.11 では以下の順に実行します。NumPy 1.26.x を維持し、別ワーカー用の `requirements-rvc.txt` は同じ環境へ入れないでください。


Windows は既存のアプリ環境で `python scripts/windows/setup_environment.py --profile full --install` を実行し、`--check` で確認します。[Windows ガイド](WINDOWS.md)を参照してください。以下の手動コマンドは Linux の主環境用です。Linux の Qwen は別のワーカー環境を維持します。

```bash
python -m pip uninstall -y onnxruntime
python -m pip install -r docs/requirements/requirements-windows-heavy.txt
python -m pip install torch==2.5.1 torchaudio==2.5.1 torchvision==0.20.1 --index-url https://download.pytorch.org/whl/cu124
python -m pip install -c docs/requirements/constraints-windows-app.txt -r docs/requirements/requirements-windows-app.txt
python -m pip install -c docs/requirements/constraints-windows-app.txt --no-deps audio-separator==0.44.2
```

`ffmpeg -version` が動くことを確認します。共有音声モデルと STT はキャラクターパックの外に置きます。不足分は [GPT-SoVITS](https://huggingface.co/lj1995/GPT-SoVITS) と [Qwen3-ASR モデル](https://huggingface.co/Qwen/Qwen3-ASR-1.7B)から取得します。すでに完全なファイルがある場合は省略できます。

```bash
python -c "from huggingface_hub import snapshot_download; snapshot_download('lj1995/GPT-SoVITS', local_dir='artifacts/tts_slim/base/GPT_SoVITS/pretrained_models', allow_patterns=['chinese-hubert-base/*', 'chinese-roberta-wwm-ext-large/*', 'sv/*', 's1v3.ckpt', 'v2Pro/*'])"
python -c "from huggingface_hub import snapshot_download; snapshot_download('Qwen/Qwen3-ASR-1.7B', local_dir='models/stt/Qwen3-ASR-1.7B')"
```

**キャラクターごとの声を準備します。** 作者提供の対応モデルと、雑音のない約 3～10 秒の参照音声を `voice/` に置きます。既存の `meta.json` の最上位へ `tts` を追加してください。次の断片でファイル全体を置き換えないでください。

```json
{
  "tts": {
    "engine": "gptsovits",
    "model_version": "v2ProPlus",
    "gpt": "voice/gpt.ckpt",
    "sovits": "voice/sovits.pth",
    "target_language": "日文",
    "reference": {
      "audio": "voice/reference.wav",
      "text": "今日は一緒にお話しできて、うれしいです。",
      "language": "日文"
    }
  }
}
```

実際のモデル版を指定し、`text` は録音どおりの原文に変更します。`日文/中文/英文` は固定の列挙値です。現在の会話音声は日本語が中心で、この項目だけでは会話全体の言語は変わりません。学習方法は [GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS)を参照してください。

本機の `tts.enabled` を `true` にしてキャラクターを再インポートし、再起動後に短い一文を試します。GPU 認識では `stt.device: cuda`、`compute_type: float16`、話す言語に合わせ `language: zh` / `ja` / `en` を指定します。歌唱には共有 RVC モデルと本機の `song.enabled: true` も必要です。[声と RVC の項目](CHARACTER_PACKS.md#加入角色声音)を参照してください。

<a id="animation"></a>
## 5. 動く立ち絵を作る

[目のアニメーションサンプル](../Desktop-Packs/Characters/Examples/eye-rig/)をコピーし、`slug` とカードを変更します。`spica-eye-rig` はまばたきと視線追従に対応します。ネイティブ Cubism は別の任意の[パック形式](CUBISM_PACKS.md)を使います。eye-rig とは別に設定してください。

1. `model/open.png` と `closed.png` を差し替えます。同じ寸法・姿勢・位置の完全な透明原画を使い、目だけを変更します。
2. `model/character.eyerig.json` の `canvas` を原画寸法に、`gaze.origin` を両目の間に設定します。`eyes` の範囲、虹彩、開眼・閉眼曲線を自分の絵に合わせて標定し直してください。
3. `pack_format: 2`、`renderer: eye-rig`、`eye_rigs` の対応を維持し、読み込みと再起動後にまばたき・視線・衣装切り替えを確認します。

座標の原点は原画左上です。両画像の眼部 `bounds` は完全に不透明である必要があります。標定項目と複数衣装は[動的パックのリファレンス](CHARACTER_PACKS.md#live2d-包当前支持的是眼部原画动画)にあります。

<a id="dialogue"></a>
## 6. 会話スタイルを作る

[`Desktop-Packs/Dialogue-Styles/sana/`](../Desktop-Packs/Dialogue-Styles/sana/)を `Desktop-Packs/Dialogue-Styles/my-style/` にコピーし、次の構造を残します。

```text
my-style/
├── style.json
└── images/
    ├── surface.png
    ├── nameplate.png
    └── tail.png
```

| 変更したい部分 | 編集箇所 |
| --- | --- |
| 一覧での識別・表示 | `style_id` を `my-style` にし、`name` と `author` を変更 |
| 会話と名前の背景 | `surface.png` と `nameplate.png`。透明度を残す |
| ボタンと文字の色 | `colors.accent`、`colors.text`。例：`#E94E8B` |
| 文字サイズ、余白、透明度 | `text.size`、`layout.left/right`、`layout.surface_opacity` |
| 文末アニメーション | `tail.png` と `tail` のグリッド設定 |

Sana サンプルは **5 列 × 4 行、20 フレーム、1 フレーム 50ms**。左から右へ、上の行から順に再生します。画像サイズは列数・行数で割り切れる必要があります。静止マーカーは `columns/rows/frames` をすべて `1` にします。`placement: corner` は右下、`inline` は文末です。装飾文字は背景へ描き込めますが、実際のキャラクター名はアプリが描画します。

設定でこのフォルダーを別途読み込み、再起動します。ボタンの動作と設定画面の配置は共通です。画像は宣言済みの `images/` 内 PNG とし、各 ≤8 MiB、各辺 ≤4096、総画素数 ≤800 万。全項目は[スタイルリファレンス](DIALOGUE_STYLES.md)を参照してください。

## 7. 削除と共有

一覧の **× → 确定移除** は項目を取り除き、元データと記憶を残します。後から再インポートできます。**导出分享包** はアプリが保存した個人記憶を含めず、**导出个人存档** は記憶も移行するためのものです。新しいフォルダーへ出力して ZIP にします。カードに直接書いた個人情報を確認し、素材の出典を記載してください。呼び名、キャラクター、スタイルの変更は再起動後に反映されます。

<a id="settings"></a>
## 8. 本機設定の変更

デスクトップの設定画面は 2 つのタブに分かれています。

- **角色与外观（キャラクターと外観）**：キャラクター、衣装、会話枠、呼び名、拡大率、透明度、音量。
- **应用设置（アプリ設定）**：API 接続先、モデル、推論強度、音声認識、読み上げ、画面理解、歌、アニメ、ゲーム中の反応。デバイス、起動時のウォームアップ、初期値への復元は **高级选项（詳細設定）** にあります。

API キーを入力したら **保存密钥（キーを保存）**、その他の変更は **保存应用设置（アプリ設定を保存）→ 重启程序（再起動）** を押します。キー欄が空なら既存のキーを保持します。接続テストはモデル一覧だけを確認し、会話を送信しません。キー未設定の初回起動時はアプリ設定を自動表示します。

通常設定は本機の `data/config/app.yaml`、キーは `xiaosan.env` に保存され、キャラクターを切り替えても保持されます。共有パックにキーは含まれません。旧環境変数や旧設定ファイルが優先される項目は出所を表示し、競合する保存を拒否します。該当する上書き設定を削除して再起動してください。初期値への復元はこのページの入力だけを変更し、保存が必要です。キャラクター、記憶、キーは削除しません。ブラウザー設定ツールは廃止されました。

## 困ったとき

- **立ち絵が出ない**：`meta.json` のある階層、画像の対応、再起動の完了を確認します。
- **声が出ない**：対応モデル、参照原文、共有モデル、`tts.enabled` を確認します。インポート成功だけでは合成成功は保証されません。
- **古い名前やモデルのまま**：環境変数の上書きを確認し、保存後に再起動します。
- **Windows のパスエラー**：本機 YAML は `C:/Spica/models/file`、パック内は相対パスを使い、ZIP は展開してから読み込みます。

## Qwen3-ASR: local / cloud

Local ASR uses **Qwen3-ASR-1.7B**. Windows reuses the single app environment
prepared by the [Windows setup](WINDOWS.md); leave the ASR Python setting empty.
Linux retains a separate Qwen worker environment. For Linux, prepare Python 3.11
with matching PyTorch, then run:

```bash
python -m pip install -r docs/requirements/requirements-qwen-asr.txt
python -c "from huggingface_hub import snapshot_download; snapshot_download('Qwen/Qwen3-ASR-1.7B', local_dir='models/stt/Qwen3-ASR-1.7B')"
```

In **设置 → 应用设置**, choose 本地 Qwen3-ASR-1.7B, set 本地模型目录 and
本地识别 Python to the downloaded directory and that environment's Python
executable (Linux: `bin/python`; Windows uses the app Python). CPU uses
`float32`; a supported NVIDIA GPU can use `bfloat16` or `float16`.
Save and restart. The application does not download models while chatting.

For cloud ASR, no local Qwen environment or weights are needed. Save the
**百炼语音识别 API Key**, select **百炼 Qwen 云端**, choose the matching account
region, save and restart. Valid utterances are uploaded to that service and
may incur charges; startup/self-check does not upload audio or verify billing.
There is no automatic fallback from local to cloud.

旧 Whisper/Google 配置会读取为本地 Qwen 默认配置；请重新配置模型目录（Windows 使用当前 Python），
或明确选择云端。密钥保存在本机 xiaosan.env，不放在 app.yaml 或角色包内。
普通 USB/系统麦克风在两个平台均可使用；只有使用 ReSpeaker 硬件 VAD 时才选择该选项。


[Current optional Home and standalone desktop configuration (中文)](README.md#独立桌面整合后的入口) · [Home setup](HOME.md) · [Local/cloud ASR](README.md#语音识别本地-qwen3-asr--百炼云端)
