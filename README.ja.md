[简体中文](README.md) · [English](README.en.md) · **日本語**

# Spica Chatbot · デスクトップペット設定とキャラクターパック制作

Spica は透明なデスクトップ上で会話するキャラクターパートナーです。文字入力と音声会話、キャラクターや衣装の切り替え、ノベルゲームの共遊、アニメ視聴、歌唱に対応します。キャラクター設定、立ち絵、声、必要に応じた記憶を一つのフォルダーにまとめ、会話ウィンドウの外観は別に選択できます。

このリポジトリは Windows／Linux 用の単体デスクトップペットです。複数キャラクター、表情差分、目のアニメーション、独立した会話ウィンドウスタイル、キャラクター別の記憶に対応します。Mobile、ロボット、Hub、運動制御のインターフェースはありません。互換パックの制作にもそれらのサービス設定は不要です。

[公式サイト・デモ](https://www.acgkasa.me/) · [キャラクター形式の詳細](docs/CHARACTER_PACKS.md) · [会話スタイルの詳細](docs/DIALOGUE_STYLES.md)

推奨設定と外部リンクは **2026-09-08** に確認しました。README の三言語対応は、会話音声の三言語対応を意味しません。現在の会話音声は日本語を中心に構成され、会話文の表示は日本語または中国語を選択します。

## 目次

1. [必要なモデルを選ぶ](#1-必要なモデルを選ぶ)
2. [実行環境をインストールする](#2-実行環境をインストールする)
3. [API とアプリを設定する](#3-api-とアプリを設定する)
4. [初回起動とフォルダーの場所](#4-初回起動とフォルダーの場所)
5. [キャラクターカードを作成する](#5-キャラクターカードを作成する)
6. [立ち絵と metajson を用意する](#6-立ち絵と-metajson-を用意する)
7. [会話用の声を追加する](#7-会話用の声を追加する)
8. [マイクの音声認識と歌唱](#8-マイクの音声認識と歌唱)
9. [目のアニメーションパックを作る](#9-目のアニメーションパックを作る)
10. [背景と会話ウィンドウのスタイル](#10-背景と会話ウィンドウのスタイル)
11. [インポート切り替え削除記憶](#11-インポート切り替え削除記憶)
12. [動作確認とトラブルシューティング](#12-動作確認とトラブルシューティング)

## 1. 必要なモデルを選ぶ

**最初はキーボード会話と静止画サンプルを動かし、その後に音声、目のアニメーション、歌唱を追加する順序を推奨します。** 声のモデルがなくてもキャラクター設定と立ち絵のパックは作れます。以下の「モデル」はそれぞれ役割が異なります。

| 機能 | 最初に使う構成 | 設定／配置場所 | キャラクターごとに必要か |
| --- | --- | --- | --- |
| 会話 LLM | `deepseek-v4-flash`、応答待ちを抑えるため thinking を無効化 | `app.yaml` の `llm`、キーは `xiaosan.env` | 共有 |
| マイク STT | faster-whisper の `large-v3-turbo`、CTranslate2 形式 | `stt.model` でモデルの完全なディレクトリを指定 | 共有 |
| 会話 TTS | GPT-SoVITS **v2ProPlus**、既存の対応した v2Pro 一式も利用可能 | GPT `.ckpt`、SoVITS `.pth`、参照録音 | 固有の声を使う場合は必要 |
| 歌声 RVC | RVC v2 推論用 `.pth` と任意の対応 `.index` | `singing/`。TTS の `.pth` とは別 | 任意 |
| 静止立ち絵 | 衣装・表情ごとに整理した透過 PNG | `visuals/` とマニフェストの対応表 | 必要 |
| 目のアニメーション | `spica-eye-rig`、開眼／閉眼画像と標定 JSON | `model/`、`pack_format: 2` | 任意 |
| 画面理解 | 既存の RapidOCR／Moondream アダプター | 本機の `screen`／`ocr` | 任意、共有 |

DeepSeek のキーは[公式コンソール](https://platform.deepseek.com/)で取得します。現行モデル ID と互換 API は[公式クイックスタート](https://api-docs.deepseek.com/)で確認できます。これはデスクトップ会話の設定例で、モデルの優劣ランキングではありません。別の互換サービスを使う場合はストリーミングとツール呼び出しの動作を確認してください。

画面表示、STT、TTS は本機で処理します。遠隔 LLM を設定した場合、会話文、必要なキャラクター設定や記憶、会話に利用する画面認識結果の文章・説明はそのサービスへ送られます。「画面認識がローカル」という説明は、会話全体がオフラインという意味ではありません。

## 2. 実行環境をインストールする

### 2.1 キーボード会話用の基本環境

Git、Conda、**Python 3.11** を用意します。Windows は PowerShell／Anaconda Prompt、Linux はターミナルを使います。コマンドはプロジェクトのルートで実行し、新しいターミナルでは毎回 `conda activate spica` を実行してください。

```bash
git clone https://github.com/KASA2AI/Spica-Chatbot_Release.git
cd Spica-Chatbot_Release
conda create -n spica python=3.11 -y
conda activate spica
python -m pip install --upgrade pip
python -m pip install -r requirements-windows-base.txt
python scripts/windows/check_imports.py
```

Ubuntu／Debian でマイク入力の依存パッケージをビルドする場合、先にシステム側を用意します。

```bash
sudo apt-get install build-essential python3-dev portaudio19-dev ffmpeg
```

基本の Python 依存リストは、ファイル名に `windows` とありますが Linux のデスクトップ実行でも利用します。PyAudio のビルドに失敗したらシステムの開発用ヘッダーを確認してください。**通常のマイクには `mic_backend: generic` を指定します。** Linux の `auto` は ReSpeaker 用経路を選ぶ設定で、任意の USB マイクを自動選択する意味ではありません。

### 2.2 NVIDIA GPU、音声、歌唱の追加依存

基本環境の後に、次の順序で実行します。リポジトリ既存の Windows GPU 構成に合わせ、Python 3.11、NumPy 1.26.x、PyTorch CUDA 12.4 を使用します。Windows の `jieba_fast` ビルドには MSVC Build Tools の C++ ツール、Linux ではコンパイラーが必要です。

```bash
python -m pip uninstall -y onnxruntime
python -m pip install -r requirements-windows-heavy.txt
python -m pip install torch==2.5.1 torchaudio==2.5.1 torchvision==0.20.1 --index-url https://download.pytorch.org/whl/cu124
python -m pip install -c constraints-windows-app.txt -r requirements-windows-app.txt
python -m pip install -c constraints-windows-app.txt --no-deps audio-separator==0.44.2
python -c "import numpy, torch; print(numpy.__version__, torch.__version__, torch.cuda.is_available())"
```

確認出力は NumPy `1.26.4`、PyTorch `2.5.1+cu124`、対応 NVIDIA 環境で CUDA が `True` となる構成です。これは依存の確認なので、音声については後で実際の合成も確認します。CPU ユーザーは基本の文字モードから始められ、立ち絵の表示だけに TensorRT や音声モデルを導入する必要はありません。

`onnxruntime` と `onnxruntime-gpu` は同じ Python モジュールを提供します。GPU 版に入れ替えた後で基本リストを再インストールすると、CPU 版で上書きされる場合があります。この固定環境では `audio-separator` を `--no-deps` で別途導入します。制約なしの一括更新で NumPy を 2.x に上げないでください。

音声処理には PATH 上の `ffmpeg` 実行ファイルが必要です。`ffmpeg-python` だけでは実行ファイルは入りません。`ffmpeg -version` で確認します。アニメ視聴には Web UI を有効にした qBittorrent と VLC も使いますが、基本会話が動いてから設定して構いません。

## 3. API とアプリを設定する

### 3.1 キーファイル

ルートに UTF-8 の **`xiaosan.env`** を作成します。Windows で `xiaosan.env.txt` になっていないか確認してください。先頭の値を自分の DeepSeek API キーに置き換え、他は空欄で構いません。

```dotenv
OPENAI_API_KEY=REPLACE_WITH_YOUR_DEEPSEEK_API_KEY
JUDGE_API_KEY=
BILIBILI_COOKIE=
QBITTORRENT_PASSWORD=
```

変数名は共通の互換アダプターに合わせて `OPENAI_API_KEY` ですが、この例では **DeepSeek のキー**を入れます。`JUDGE_API_KEY` はゲーム共遊時の判断用モデルに別キーを使う場合の設定で、省略時は主キーを使います。キャラクターカード、マニフェスト、画面写真、共有パックにキーを入れないでください。

### 3.2 アプリ設定の例

**`data/config/app.yaml`** をエディターで開きます。新規ユーザーは次の文字モード設定から始められます。既存ユーザーは関連するフィールドだけをマージし、他の設定を保持してください。YAML のインデントはスペースを使います。ここでの `none` は文字列で、空値は `null` です。

```yaml
llm:
  provider: openai_compatible
  model: deepseek-v4-flash
  base_url: https://api.deepseek.com/v1
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
  backend: faster_whisper
  mic_backend: generic
  model: spica_data/models/faster-whisper-large-v3-turbo
  device: cpu
  compute_type: int8
  language: zh
  warmup_on_startup: false

screen:
  enabled: false
anime:
  enabled: false
song:
  enabled: false
```

| フィールド | 入力内容 |
| --- | --- |
| `llm.model` | `deepseek-v4-flash` など実際の API モデル ID。声のファイル名ではありません。 |
| `llm.base_url` | チャットサイトではなく API の基点 URL。`/chat/completions` は追加しません。 |
| `reasoning_effort` | 本アダプターの `none` は DeepSeek thinking を無効化します。`default` は制御パラメーターを送信しません。供給元を変える場合は対応を確認します。 |
| `system_turn_reasoning_effort` | 自発発話用の独立設定。`null` は主設定を継承し、この例は `none` です。 |
| `interlocutor_name` | キャラクターから呼ばれたい名前。後から設定画面で変更し、再起動で適用できます。 |
| `dialog_display_language` | `zh` は中国語訳、`ja` は日本語原文。ここに `en` の選択肢はありません。 |
| `character.package_dir` | インポート処理が記録する**インストール済みディレクトリ**。元フォルダー、ZIP、モデル単体は指定しません。後述の初回コマンドで設定します。 |
| `tts.enabled` | 最初は `false`。音声と依存を用意した後で `true` にして再起動します。 |
| `stt.language` | マイクに話すユーザーの言語。`zh`／`ja`／`en` などで、キャラクターの発音言語とは別です。 |

上の例は中国語字幕です。日本語原文を表示する場合は `dialog_display_language: ja`、日本語でマイクへ話す場合は `stt.language: ja` に変更してください。

旧環境変数の `MODEL`、`OPENAI_BASE_URL`、`REASONING_EFFORT`、`SPICA_USER_NAME`、`SPICA_SKILL_DIR`、`SPICA_CHARACTER_PROFILE` などは YAML より優先します。キーは `xiaosan.env`、通常設定は YAML／設定画面へまとめる構成を推奨します。名前の保存時に `SPICA_USER_NAME` の上書きが通知された場合は、プロセス環境または dotenv の旧項目を削除し、再起動してから保存してください。

## 4. 初回起動とフォルダーの場所

### 4.1 大きなキャラクター素材を用意せずに試す

先に上の文字モード設定を保存してください。次のコマンドはリポジトリ同梱の小さな静止画サンプルをインストールし、選択状態を保存します。完全な Spica の声や原作立ち絵は不要です。本機の選択キャラクターを変更するため、既存ユーザーは設定画面を使っても構いません。

```bash
python -c "from pathlib import Path; from spica.host.character_packages import import_character_folder; from spica.config.manager import ConfigManager; p = import_character_folder('Desktop-Packs/Characters/Examples/static', Path('data/runtime/characters')); ConfigManager().update({'character': {'package_dir': p.package_root, 'profile_override': None}}); print(p.name)"
python webui_qt.py
```

サンプルの立ち絵と会話ウィンドウが表示されたら、文字を入力して試します。この段階ではマイク、画面認識、歌唱を有効にしません。サンプル画像は形式説明用の小さなオリジナル図で、完全な Spica／Sana の美術素材ではありません。

### 4.2 展開先を理解する

```text
Spica-Chatbot_Release/
├── xiaosan.env
├── data/config/app.yaml
├── data/config/tts.yaml
├── Desktop-Packs/
│   ├── Characters/
│   │   ├── Examples/static/
│   │   ├── Examples/eye-rig/
│   │   └── my-character/
│   └── Dialogue-Styles/
│       ├── spica/
│       ├── sana/
│       └── my-style/
├── artifacts/tts_slim/base/
├── artifacts/rvc_slim/base/
├── spica_data/models/faster-whisper-large-v3-turbo/
├── data/runtime/characters/<slug>/<revision>/
├── data/runtime/character_states/<slug>/
└── data/runtime/dialogue_styles/<style_id>/<revision>/
```

`Desktop-Packs/` は編集・共有する**元フォルダー**、`data/runtime/characters/` は管理されたインストール先、`character_states/` はキャラクター別の設定と関連個人状態です。記憶は共有 SQLite データベースにも保存されるため、移行には「個人セーブのエクスポート」を優先し、一つのディレクトリだけで全記憶が揃うとは考えないでください。

Chatbot は旧内蔵アセットとの互換性を維持します。キャラクター設定は `spica_data/Spica_skill/`、立ち絵と参照音声は `spica_data/diffs/` と `spica_data/voice/`、音声モデルは `artifacts/tts_slim/characters/spcia/`、歌声モデルは `artifacts/rvc_slim/characters/spica/` にあります。新しいキャラクターをインポートすると `data/runtime/characters/` の独立したコピーを使用します。新しく作る元パックは `Desktop-Packs/` にまとめ、旧ディレクトリへ重複コピーする必要はありません。内蔵キャラクターの旧データベースは引き続き `spica_data/` にあるため、ディレクトリごと削除しないでください。 旧 TTS ディレクトリの `spcia` は実際に残っている綴りです。正確なモデルファイル名は `data/config/tts.yaml` で確認してください。

既存の[プロジェクトアセット配布先](https://pan.baidu.com/s/1EFq7t8Lxcy9kDNL7MzU1gg?pwd=nzjy)の抽出コードは `nzjy` です。世代により構成が違うため、最上位のディレクトリを確認してから展開します。**キャラクター／スタイル集の ZIP は共有エンジンや STT モデルのインストーラーではありません。** 最上位が `Characters/` と `Dialogue-Styles/` なら `Desktop-Packs/` の中へ、既に `Desktop-Packs/` があればその階層をルートへマージし、同じディレクトリを二重にしないでください。

Git のクローンには大きな Spica／Sana の声や立ち絵は含まれません。小さな `Examples/` とスタイル例は同梱されます。利用できる権利を持つ独自素材だけでキャラクターを制作することもできます。

### 4.3 ローカル設定スタジオ（Config Studio）

基本依存関係には設定スタジオも含まれています。古い環境に単独で追加する場合は、次の専用依存関係を先にインストールしてください。

```bash
python -m pip install -r requirements-config-studio.txt
python scripts/config_studio.py --port 8765
```

別途起動するブラウザー設定ツールで、既定では `127.0.0.1:8765` のみに接続を受け付けます。手動で開く場合は `--no-open-browser` を付け、ターミナルに表示された一度限りの起動認証を使います。ポート使用中なら `--port 8767` に変更してください。ページの言語メニューで **中文 / English / 日本語** を選べます。言語切り替えは画面の説明だけを変更し、設定キーや値は変更しません。保存後はデスクトップを再起動し、使い終えたらターミナルで `Ctrl+C` を押して設定スタジオを終了します。ブラウザーの表示言語はキャラクターの発話言語とは別です。

## 5. キャラクターカードを作成する

### 5.1 推奨するオープンソースの編集サイト

[CCEditor オンライン版](https://lenml.github.io/CCEditor/?lang=ja)を推奨します。[ソースとライセンス](https://github.com/lenML/CCEditor)も公開されています。SillyTavern を既に使っている場合は、その[キャラクター編集画面](https://docs.sillytavern.app/usage/characters/)でも構いません。カードを作るためのツールであり、別のチャットフロントエンドの導入は Spica の必須条件ではありません。

オリジナル設定から作るか、作者が用途を許可しているコミュニティカードを利用します。文章を確認してから JSON を出力してください。カード情報を埋め込んだ PNG を受け取った場合は、編集サイトで開き、文章フィールドを出力・コピーします。**SillyTavern の PNG カードや V2／V3 JSON 全体は直接インポートできません。`meta.json` に改名しても Spica パックにはなりません。**

### 5.2 外部カードから何を移すか

[静止画サンプル](Desktop-Packs/Characters/Examples/static/)をコピーして `Desktop-Packs/Characters/my-character/` を作ると始めやすくなります。V2／V3 JSON では本文は通常 `data` 内、旧形式では最上位に置かれます。以下の対応で整理してください。

| 外部カード | Spica の保存先 | 記入の考え方 |
| --- | --- | --- |
| `name` | `meta.json` の `name`、`char_name` | 一覧表示名と実際のキャラクター名を分けられます。 |
| `description` | `persona.md` の身元・経歴 | 誰なのか、ユーザーとの関係、必要な背景を具体的に書きます。 |
| `personality` | `persona.md` の性格・振る舞い | 形容詞だけでなく、場面ごとの反応や話し方を書きます。 |
| `scenario` | `persona.md` の現在の関係・場面 | 継続するデスクトップ会話に合わせ、一回限りのシナリオ制約を整理します。 |
| `mes_example` | `persona.md` の会話例 | `{{user}}` と `{{char}}` を使った短い例を用意します。 |
| `first_mes`／別の挨拶 | 任意の口調の例 | アプリ起動時の挨拶として自動実行はされません。 |
| `character_book`／世界設定 | `worldbook.md` に要約し、マニフェストで宣言 | 普通の背景文章です。キーワード発動、挿入深度、正規表現スクリプトは実行しません。 |
| 作者・出典・ライセンス | `README.md`、`LICENSE`、`sources.tsv` | 元作者と各素材の利用条件を残します。 |

酒場用のシステムプリセット、脱獄テンプレート、拡張スクリプトを丸ごと移す必要はありません。Spica は発話言語、翻訳、表情情報、ツール呼び出しを管理します。カードでは主に人物像、関係性、話し方を定義してください。

### 5.3 書き換えて使える persona.md

UTF-8 で保存します。具体的な事実と少数の会話例から始め、実際の応答を見て調整してください。

```markdown
# 身元
あなたは {{char}}。デスクトップに住むオリジナルキャラクターで、{{user}} と話している。
二人は親しい友人で、これまでの普通の会話を自然に続けられる。

# 性格と口調
好奇心が強く穏やかで、ときどき軽くからかう。心配するときは相手の具体的な気持ちに応える。
{{user}} の行動、台詞、人生経験を勝手に作らない。
日常の返事は自然で短めにし、アプリの日本語音声と字幕の形式に従う。

# 好みと関係
星空と温かいお茶が好き。{{user}} を対等な友人として接する。
知らないことは正直に伝え、推測を共有した思い出として話さない。

# 会話例
{{user}}：今日は少し疲れた。
{{char}}：お疲れさま。少し休んで、お茶でも飲もうか。
{{user}}：昨日話したこと、覚えている？
{{char}}：覚えていることから、一緒に振り返ってみよう。
```

最初の `worldbook.md` は短い文章で構いません。

```markdown
{{char}} はデスクトップの小さな家を「星の小屋」と呼んでいる。
旅人は {{user}} の以前の呼び名で、同じ人物を指す。
```

後述の `user_aliases` に `旅人` を宣言し、読み込み時に現在のユーザー名へ置き換えます。新しい文章には直接 `{{user}}` を使うのが簡単です。別名は文字列置換なので、無関係な単語にも含まれる短すぎる文字を登録しないでください。

カードは `SKILL.md`、`self.md`、`persona.md` を組み合わせられ、少なくとも一つ必要です。新しいパックなら `persona.md` だけでも十分です。実行スクリプトや Codex スキルではなく、キャラクターの文章です。`self.md` に手書きした背景も共有エクスポートに含まれ、個人データベースの記憶として自動除外はされません。

## 6. 立ち絵と meta.json を用意する

### 6.1 元パックの構造

```text
my-character/
├── meta.json
├── persona.md
├── worldbook.md
├── README.md
├── LICENSE
├── visuals/
│   ├── idle.png
│   └── happy.png
├── voice/
│   ├── gpt.ckpt
│   ├── sovits.pth
│   └── reference.wav
└── singing/
    ├── model.pth
    └── model.index
```

`voice/` と `singing/` は後から追加する任意項目です。まずはカード、世界設定、二枚の画像だけで始めます。未準備の音声設定は項目ごと省略し、存在しない仮のパスを入れないでください。

### 6.2 完全な静止画マニフェスト

以下を `meta.json` として保存します。JSON はコメント、末尾の余分なカンマ、シングルクォートを許可しません。

```json
{
  "pack_format": 1,
  "slug": "my-character",
  "version": "1.0.0",
  "name": "Hoshi",
  "char_name": "ホシ",
  "user_aliases": [
    "旅人"
  ],
  "worldbook_file": "worldbook.md",
  "visuals": {
    "renderer": "sprite",
    "default_costume": "casual",
    "sprites": {
      "idle": "visuals/idle.png",
      "happy": "visuals/happy.png"
    },
    "costumes": [
      {
        "id": "casual",
        "label": "Casual",
        "default_sprite": "idle",
        "emotions": {
          "happy": [
            "happy"
          ]
        }
      }
    ]
  }
}
```

| フィールド | 意味・変更方法 |
| --- | --- |
| `pack_format` | 静止画は `1`、目のアニメーションは `2`。 |
| `slug` | 継続的な識別子。`hoshi` のような英小文字・数字・ハイフンを推奨します。更新は同じ ID、別キャラクターは新しい ID を使います。 |
| `version` | 作者の表示版番号。例 `1.0.1`。素材の変更でもインストール版の識別値が変わります。 |
| `name`／`char_name` | 一覧名／実際の発言者名と `{{char}}` の置換値。 |
| `worldbook_file` | 任意の世界設定ファイル。存在しない場合は項目を削除します。 |
| `sprites` | 画像 ID と相対パスの対応。別の設定からはこの ID を参照します。 |
| `default_costume` | `costumes[].id` のいずれかと一致させます。 |
| `default_sprite` | `sprites` にある画像 ID。PNG パスを直接記入する欄ではありません。 |

パスは `meta.json` の位置を基準に `/` で記入します。ドライブ名、`/home/...`、`../...`、シンボリックリンクは使いません。Windows で衝突するため、大文字小文字だけが違うファイル名や ID も避けてください。画像名を変えたらマニフェストも更新します。

### 6.3 立ち絵の準備

1. キャラクターの外側が本当に alpha 透過した PNG を使います。白背景は透明ではありません。
2. 同じ構図の差分は**画布、人物位置、拡大率、足元の基準線**を揃え、表情や手の形だけを変えます。ずれると切り替え時に人物が跳ねます。
3. 普通の静止画はインポート時に比率を保ち、`1024×1024` の透明画布の中央下寄せに配置されます。会話 UI や場面背景を人物画像に描き込まないでください。
4. `visuals/casual/`、`visuals/school/` などで衣装を整理できます。中景・アップも別衣装として扱えますが、フォルダー名だけでは登録されず JSON の対応設定が必要です。
5. 感情キーは `happy`、`angry`、`sad`、`surprised`。最初は通常と笑顔だけでよく、未設定の感情は衣装の既定画像へ戻ります。

### 6.4 衣装や表情を追加する

`visuals/school/idle.png` と `visuals/school/happy.png` を置きます。既存 `sprites` に `"school-idle": "visuals/school/idle.png"` と `"school-happy": "visuals/school/happy.png"` を追加し、既存の `costumes` 配列に次を追加してください。

```json
{
  "id": "school",
  "label": "School",
  "default_sprite": "school-idle",
  "emotions": {
    "happy": [
      "school-happy"
    ]
  }
}
```

初期衣装を制服にするなら `default_costume` を `school` にします。感情追加も画像、画像 ID、感情マッピングの順です。一つの感情に複数 ID を登録できます。手のポーズと三桁の表情 ID を使う詳細制御は任意の上級設定で、[形式の詳細](docs/CHARACTER_PACKS.md)を参照してください。初めから Spica の全差分ルールを複製する必要はありません。

## 7. 会話用の声を追加する

### 7.1 必要な二つのモデル

会話用パックは **GPT-SoVITS v2Pro／v2ProPlus** に対応します。新しく用意する場合は **v2ProPlus** を出発点とし、既存の v2Pro 一式は本来のバージョンを維持します。

- GPT `.ckpt`：音声の意味表現側のモデル。`tts.gpt` で指定します。
- SoVITS `.pth`：対応する声の生成モデル。`tts.sovits` で指定します。
- 参照 WAV と正確な書き起こし：`tts.reference` に指定する声・口調の見本です。

声の作者から対応した一式を入手するか、利用権を持つ録音で [GPT-SoVITS 公式プロジェクト](https://github.com/RVC-Boss/GPT-SoVITS)に従って学習します。Spica は推論を担当し、学習画面は含みません。RVC の `.pth`、任意の学習途中チェックポイント、LoRA、非対応版は、拡張子や `model_version` の変更だけでは変換できません。

微調整済みモデルがなければ、同じ世代の上流ベースモデルと参照音声から試すこともできます。ただし、キャラクター専用の学習済み声と同じ類似度を保証するものではありません。発音と安定性を実際に聞いて判断してください。

### 7.2 共有推論モデル

二つのキャラクターモデルだけでは共有依存は揃いません。同梱 slim エンジンのソースを残し、その下の `pretrained_models/` に完全な基盤モデルを置きます。プロジェクトのアセットパックを使うか、[上流モデルリポジトリ](https://huggingface.co/lj1995/GPT-SoVITS)から v2Pro 関連部分を取得できます。

```bash
python -c "from huggingface_hub import snapshot_download; snapshot_download('lj1995/GPT-SoVITS', local_dir='artifacts/tts_slim/base/GPT_SoVITS/pretrained_models', allow_patterns=['chinese-hubert-base/*', 'chinese-roberta-wwm-ext-large/*', 'sv/*', 's1v3.ckpt', 'v2Pro/*'])"
```

```text
artifacts/tts_slim/base/GPT_SoVITS/pretrained_models/
├── chinese-hubert-base/                 (complete model folder)
├── chinese-roberta-wwm-ext-large/       (complete model folder)
├── sv/pretrained_eres2netv2w24s4ep4.ckpt
├── s1v3.ckpt
└── v2Pro/
    ├── s2Gv2Pro.pth
    └── s2Gv2ProPlus.pth
```

`chinese-*` の二つのディレクトリは共通依存なので、日本語キャラクターでも削除しません。ベースモデルを試す場合は `s1v3.ckpt` と `s2Gv2ProPlus.pth` を**自分のパック内へコピー**し、実際の名前で参照してください。学習済み声を使う場合は、その作者の対応したファイルを使用します。

### 7.3 参照録音と設定

最初は **3～10 秒程度**の、単独話者・背景音楽なし・明瞭な WAV を用意します。`text` はその音声が実際に話している原文です。翻訳文や「今後話してほしい内容」を書く欄ではありません。次の文章は例なので、自分の録音の正確な書き起こしへ必ず変更してください。

既存の `meta.json` の最上位へ次の `tts` を追加します。人物情報や立ち絵設定は保持します。

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
      "text": "今日は一緒にお話しできて、とてもうれしいです。",
      "language": "日文"
    },
    "parameters": {
      "top_k": 5,
      "top_p": 1.0,
      "temperature": 1.0,
      "speed": 1.0
    }
  }
}
```

`model_version` は実際の `v2Pro` または `v2ProPlus`。`reference.language` は参照録音、`target_language` は合成の言語です。選択値は文字どおり **`日文`／`中文`／`英文`** で、日本語 README でも `日本語` に翻訳してはいけません。現在の会話音声は日本語が中心で、ここを `英文` にするだけでは会話全体が英語になりません。

`top_k`、`top_p`、`temperature` は例の値、`speed` は `1.0` から始めます。一つの参照で安定して発声してから、感情やパラメーターを増やしてください。

任意で `tts` 内に `emotions` を追加できます。

```json
{
  "happy": {
    "audio": "voice/happy.wav",
    "text": "会えてうれしいです。",
    "language": "日文"
  },
  "sad": {
    "audio": "voice/sad.wav",
    "text": "もう少しだけ、ここにいてください。",
    "language": "日文"
  }
}
```

上の WAV は実在する必要があり、原文も自分の録音に合わせます。省略した感情は基本の参照を使います。各参照に `additional_audio` 配列を追加することもできますが、不要なら省略してください。

最後に本機の `app.yaml` で `tts.enabled: true` とし、変更したキャラクターを再インポートして再起動します。短い日本語を一文合成して確認してください。インポートは形式とファイルを検証する処理で、成功しただけでは実際の発声確認にはなりません。

## 8. マイクの音声認識と歌唱

### 8.1 音声認識 STT

[CTranslate2 形式の large-v3-turbo](https://huggingface.co/dropbox-dash/faster-whisper-large-v3-turbo)を完全なディレクトリとして取得します。

```bash
python -c "from huggingface_hub import snapshot_download; snapshot_download('dropbox-dash/faster-whisper-large-v3-turbo', local_dir='spica_data/models/faster-whisper-large-v3-turbo')"
```

`model.bin`、`config.json`、`tokenizer.json` などの実ファイルが必要です。Git LFS のポインターだけでは動きません。既に完全なモデルがあれば再利用できます。本アプリは [faster-whisper](https://github.com/SYSTRAN/faster-whisper)を使うため、任意の Whisper `.pt` を代わりに置くことはできません。

NVIDIA は `stt.device: cuda` と `compute_type: float16`、CPU は `device: cpu` と `compute_type: int8` を設定します。モデル取得後は `warmup_on_startup: true` にすると初回ロード待ちを減らせます。`language` はユーザーが話す言語で、中国語 `zh`、日本語 `ja`、英語 `en` です。OS の入力／出力デバイスも確認し、短い発話で認識を試してください。

### 8.2 RVC の歌声

会話と歌声は別モデルです。[Applio／RVC](https://github.com/IAHispano/Applio)で推論できる RVC v2 `.pth` を `singing/model.pth` に、対応索引があれば `singing/model.index` に置きます。マニフェスト最上位へ追加します。

```json
{
  "rvc": {
    "engine": "rvc_v2",
    "model": "singing/model.pth",
    "index": "singing/model.index",
    "index_rate": 0.5,
    "transpose": 0,
    "protect": 0.33
  }
}
```

索引がない場合は `index` フィールドを削除し、存在しないファイルを指定しません。`transpose` は半音単位の移調で、話速ではありません。`index_rate` と `protect` は例の値から始めます。本機には共有 RVC エンジン、HuBERT／RMVPE などのモデル、音源分離依存も必要で、アセットパックの `artifacts/rvc_slim/base/` が共有配置先です。

本機の `song.enabled: true` を設定し、インポートと再起動を行います。`rvc` を省略したキャラクターの歌唱は無効になります。パックは本機で無効にした機能を勝手に有効化しません。API キー、STT、GPU 選択、Python パスは本機の設定に残します。

## 9. 目のアニメーションパックを作る

**現在の対応形式は `spica-eye-rig` による原画の目のアニメーション**です。マウスへの視線追従、自然なまばたき、差分の後に動く既定画像へ戻る動作に対応します。Cubism プレイヤーではないため `.model3.json`／`.moc3` は直接使えず、全身の骨格・物理や自動リップシンクも含みません。

まず [eye-rig サンプル](Desktop-Packs/Characters/Examples/eye-rig/)をコピーし、そのままの画像で動作を確認してから自作絵へ変更します。

```text
my-character-live2d/
├── meta.json
├── persona.md
├── worldbook.md
└── model/
    ├── character.eyerig.json
    ├── open.png
    └── closed.png
```

静止画パックの ID、名前、カード、世界設定、任意の声はそのまま使います。`pack_format` と `visuals` 全体を次の断片で置き換え、他の必須項目は残してください。

```json
{
  "pack_format": 2,
  "visuals": {
    "renderer": "eye-rig",
    "default_costume": "casual",
    "sprites": {
      "idle": "model/open.png"
    },
    "eye_rigs": {
      "idle": "model/character.eyerig.json"
    },
    "costumes": [
      {
        "id": "casual",
        "label": "Casual",
        "default_sprite": "idle"
      }
    ]
  }
}
```

### 9.1 原画を用意する

`open.png` と `closed.png` は、**同じサイズ・姿勢・画布を持つ完全な RGBA 人物画像**で、目の開閉だけを変えます。閉眼画像を目だけの小さなパッチへ切り抜かないでください。標定対象の眼部矩形は二枚とも完全に不透明にし、人物外側だけを透過させます。画布は一辺 4096 ピクセル以下です。

### 9.2 自分の絵を標定する

例の [character.eyerig.json](Desktop-Packs/Characters/Examples/eye-rig/model/character.eyerig.json)を編集します。既存の座標は同梱 `200×200` の図専用です。別の人物へそのまま流用せず、測り直してください。

| フィールド | 測定・入力する内容 |
| --- | --- |
| `canvas.width/height` | 二枚の完全な原画の実寸。 |
| `textures.open/closed` | **rig JSON のあるディレクトリ**からの相対パス。例は `open.png`／`closed.png`。 |
| `gaze.origin` | 基準位置 `[x,y]`。通常は両目の間です。 |
| `gaze.maximum_offset` | 最大移動量 `[dx,dy]`。上限は横 32、縦 16 ピクセルで、小さな値から調整します。 |
| `gaze.strength/smoothing_ms` | 追従強度と平滑化時間。最初は `0.7`／`95`。 |
| `blink` | まばたきの長さ、初回待ち、ランダム間隔。まずは例のまま使います。 |
| `eyes[].bounds` | 眼とまつげを覆う `[x,y,width,height]`。幅と高さは各 512 以下。 |
| `eyes[].iris` | 虹彩の**左右 x 境界** `[left_x,right_x]`。二次元位置ではありません。 |
| `eyes[].contour` | 左から右へ `[x,上まぶたy,下まぶたy,まつげ上端y]`、最低三点。 |
| `eyes[].closedLine` | 閉眼線の `[x,y]`、最低二点。開眼輪郭の左右端を覆います。 |

完全な原画の左上を原点とし、x は右、y は下へ増えます。画面上の位置や百分率を入れないでください。曲線の x は増加順、点はすべて `bounds` 内、まつげ上端 ≤ 上まぶた ≤ 下まぶたです。虹彩の左右には移動余白を残します。透明部分まで矩形を広げて回避せず、検証が示す座標を修正してください。

衣装ごとに異なる既定画像と rig を用意し、`eye_rigs` で画像 ID と対応付けられます。rig のない表情は静止差分のまま使えます。動くパックも静止画と同じカード、名前置換、記憶の規則を使います。

## 10. 背景と会話ウィンドウのスタイル

### 10.1 キャラクターの場面背景

必要なら `backgrounds/room.png` と `ui/settings-cg.png` を追加し、最上位へ次をマージします。

```json
{
  "backgrounds": {
    "room": "backgrounds/room.png"
  },
  "settings_background": "ui/settings-cg.png",
  "scenes": {
    "mobile": {
      "background": "room",
      "background_position": [
        0.5,
        0.5
      ]
    },
    "robot": {
      "background": "room",
      "background_position": [
        0.5,
        0.5
      ]
    }
  }
}
```

Chatbot はパック互換性のためにこのフィールドを保持し、エクスポートできますが、Mobile／ロボット用背景の表示や端末への接続は行いません。Hub アドレスやスマートフォン用インターフェースを追加する必要もありません。デスクトップの会話背景や色は、以下の独立したスタイルパックで設定します。

### 10.2 独立した会話スタイルを作る

`Desktop-Packs/Dialogue-Styles/spica/` または `sana/` を構造の参考にします。自作素材へ変更したら `style_id` と名前も変更し、一つの独立フォルダーにまとめます。

```text
my-style/
├── style.json
└── images/
    ├── surface.png
    ├── nameplate.png
    └── tail.png
```

アニメーションを持つ最小の `style.json` です。

```json
{
  "format": "spica-dialogue-style",
  "format_version": 1,
  "style_id": "my-style",
  "name": "Hoshi Pink",
  "version": "1.0.0",
  "author": "Your name",
  "images": {
    "surface": "images/surface.png",
    "tail": "images/tail.png"
  },
  "colors": {
    "accent": "#E94E8B"
  },
  "tail": {
    "placement": "corner",
    "columns": 5,
    "rows": 4,
    "frames": 20,
    "frame_ms": 50,
    "width": 28,
    "height": 28
  }
}
```

`surface.png` は透過の会話背景、`nameplate.png` は任意の名前欄背景です。後者を使う場合は `images` に `"nameplate": "images/nameplate.png"` を追加します。実際の発言者名はアプリが描画するため、固定の人物名を背景へ描き込まないでください。「Character Name」のような装飾文字は画像に含められます。

この例の `tail.png` は **横 5 列 × 縦 4 行、計 20 フレーム**です。左から右へ進み、行が終わると次の行を再生します。各 50 ms なので一周は約一秒です。画素寸法は行列数で割り切れる必要があります。Sana 原作の意図的な空白フレームは残せます。静止マーカーなら列、行、フレーム数をすべて `1` にします。

`placement: corner` は会話領域の右下、`inline` は文末です。文字表示が完了すると現れ、連続する分割文にも見えるだけの間を設けます。色、文字、余白は `colors/text/layout` で調整し、全項目と範囲は [JSON Schema](docs/dialogue-style.schema.json)を参照してください。画像は `images/` 以下の宣言済み PNG に限り、一枚 8 MiB 以下、一辺 4096 以下、800 万画素以下です。

スタイルは外観と部分的な配置を変更し、既存のボタン機能と設定構造は維持します。パック内スクリプトは実行しません。同名のキャラクターをインポートしても、そのスタイルが自動で選ばれるわけではありません。

## 11. インポート、切り替え、削除、記憶

### 11.1 普段の使い方

1. 歯車の設定を開き、**导入角色文件夹**（キャラクターフォルダーをインポート）を選びます。`meta.json` を直接含む階層を指定してください。
2. 完了の表示を待ちます。大きな音声パックは重みをコピーするので、元フォルダーとインストール先の両方の容量が必要です。
3. **对话框样式**（会話スタイル）で、必要なら `style.json` を含むディレクトリもインポートします。
4. 呼ばれたい名前を入力し、Enter またはフォーカスを外して保存します。
5. **重启程序**（再起動）をクリックします。キャラクター、名前、スタイルが読み直されます。会話中でも現在の再生を止め、バックグラウンドが安全に終了してから再起動します。

既存のキャラクターやスタイルはドロップダウンで選べます。衣装は通常すぐに反映されますが、現在の返答と音声が終わるまでは一時的に変更を無効化し、後続文が旧衣装を復元することを防ぎます。

### 11.2 一覧から取り除く

キャラクター／スタイル一覧を開く → 対象行の **×** → **确定移除**（削除を確認）。キャンセルなら選択は変わりません。内蔵の既定項目には × がありません。

この操作は一覧から外すもので、**元パック、インストール済み素材、記憶を保持し、ディスク容量は解放しません**。保存済みの選択項目を外すと、現在の会話はロード済み資源を利用し、次回起動時にその項目の内蔵 Spica 既定値へ戻ります。後日元フォルダーを手動インポートすれば一覧に復帰します。

### 11.3 共有と個人セーブ

目的のキャラクターをインポートして再起動した後、次のどちらかを選びます。

| 操作 | 内容 | 用途 |
| --- | --- | --- |
| **导出分享包**（共有パック） | カード、宣言した画像／rig／音声、世界設定、作者資料。アプリが生成した個人セーブは除外 | 配布権を持つキャラクターの共有 |
| **导出个人存档**（個人セーブ） | 上記に加え、そのキャラクターの長期記憶、最近の普通の会話、ゲーム共遊の記憶、衣装 | 自分の別の PC へ移行 |

どちらも**まだ存在しない新しい出力フォルダー名**を指定します。会話スタイルは別途添付してください。出力フォルダーを ZIP にし、受け取った側は展開して対象フォルダーをインポートします。プロジェクト全体や `data/runtime/` をそのまま公開しないでください。

更新時は `slug` を維持すると本機の記憶を継続します。個人セーブはそのキャラクターの本機セーブがない場合に初期化用として読み込み、移行先の既存記憶を強制上書きしません。静止画版と動く版で同じ `slug` を使うと同じ人物として扱います。独立した記憶が必要なら新しい `slug` を使ってください。

## 12. 動作確認とトラブルシューティング

### 12.1 作者が確認する項目

- 元フォルダーからインポートし、再起動後の名前、既定衣装、キャラクター設定を確認する。
- ユーザー名を変更して再起動し、次の返答とその名前を含む音声が正しいか確認する。
- 表情切り替えで人物が跳ねないか、動くパックのまばたきや追従幅が適切か確認する。
- 声ありパックは短文、長めの文、複数感情を実際に聞く。インポート成功だけで終わらせない。
- 文末アニメーション、入力、送信、設定、再起動の操作を確認する。
- 共有パックに個人会話、キー、デバイス設定がなく、作者情報と素材の利用条件が揃っているか確認する。

### 12.2 よくある問題

| 症状 | 最初の確認先 |
| --- | --- |
| 401／認証エラー | `base_url` の供給元に対応するキーか、dotenv がルートにあるか。 |
| 404／モデルがない | API の正式 ID と基点 URL か。チャットサイトや廃止された別名を入れていないか。 |
| 保存しても旧設定 | 再起動したか、プロセス環境／dotenv に旧上書きが残っていないか。 |
| 字幕だけで無音 | `tts.enabled`、`tts` 宣言、対応した二つのモデル、参照原文。文字サンプルは元から無音です。 |
| 立ち絵がない | 対象階層、既定 ID、画像の相対パスと大文字小文字。 |
| 動くパックを拒否される | サイズ、不透明な眼部矩形、虹彩余白、曲線座標。Cubism は非対応です。 |
| 最初の返事が遅い | LLM 待ち、STT 初回取得、TTS ウォームアップ、中国語訳待ちを区別する。取得・予熱後に再測定し、DeepSeek は `none` を試せます。 |
| Linux で ReSpeaker エラー | 普通のマイクなら `mic_backend: generic`。 |
| CUDA／DLL エラー | Python 環境、ドライバー、固定依存、CPU／GPU ONNX Runtime の競合。 |
| インストール済みスタイル破損 | 完全な元スタイルを再インポートして修復し、再起動する。 |
| 一覧削除後も容量が同じ | 素材と記憶を保持する一覧削除なので正常です。 |
| ZIP をインポートできない | 展開して個々のパックを選ぶ。ZIP や集合の親ディレクトリは選びません。 |

### 12.3 追加機能の設定

アニメは `anime` に qBittorrent Web UI、プレイヤー、保存先を設定します。パスワードや必要な Bilibili Cookie は `xiaosan.env` に置きます。画面観察は `screen/ocr` でローカルアダプターを選択し、文字会話と音声の後に重い視覚モデルを有効にしてください。待ち時間と VRAM は機材、モデル、入力長によって変わり、一度の測定値は最低要件の保証にはなりません。

### 12.4 ライセンスと上流プロジェクト

Spica 自体は [LICENSE](LICENSE) の **Source-Available ライセンス**を使用し、無条件に再配布できるオープンソースライセンスではありません。プロジェクトの人物、声、原作画像には個別の利用範囲があります。自作コミュニティパックには作者、出典、ライセンスを明記し、第三者素材の配布条件を確認してください。本書は既存ライセンスを変更しません。

[GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS)、[Applio](https://github.com/IAHispano/Applio)、[faster-whisper](https://github.com/SYSTRAN/faster-whisper)、[RapidOCR](https://github.com/RapidAI/RapidOCR)、[Moondream](https://github.com/vikhyat/moondream)、[yt-dlp](https://github.com/yt-dlp/yt-dlp)に感謝します。それぞれ独自のライセンスで提供されています。カード記述は [SillyTavern 公式ガイド](https://docs.sillytavern.app/usage/core-concepts/characterdesign/)も参考にでき、Spica が実際に読み込む項目は[キャラクター JSON Schema](docs/character-pack.schema.json)で確認できます。
