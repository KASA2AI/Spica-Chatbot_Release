[简体中文](README.md) · [English](README.en.md) · **日本語**

# Spica

**ローカルで動くデスクトップ音声ロールプレイ・コンパニオンアプリ。** キャラクターは Spica（辻倉朱比華）——透明なデスクトップ立ち絵で、リアルタイム音声で話しかけられる相棒です。おしゃべりしたり、一緒に galgame をプレイしたり、一緒にアニメを観たり、歌ってくれたり。すべてあなた自身のマシン上で動作し、画面の内容がアップロードされることはありません。

> 👉 **ホームページ：[www.acgkasa.me](https://www.acgkasa.me/)** —— デモ動画・デバッグログ・動画チュートリアルはこちら。

---

## ✨ 機能

- **🎙️ 音声対話** —— ローカル音声認識 + キャラクター音声合成で、Spica とリアルタイムに会話（テキスト入力も可）。
- **🎮 galgame 実況の相棒** —— Spica があなたのプレイを見守り、今のシナリオを認識（OCR）し、一緒にツッコミを入れ、一緒に遊んだ思い出を覚えています。
- **📺 一緒にアニメ鑑賞** —— 作品を探す → ダウンロード（マグネット / bilibili）→ 再生。日中バイリンガル字幕対応。
- **🎵 歌う** —— Spica 自身の声で歌ってもらえます。
- **👀 画面を見る** —— スクリーンショットから画面の内容を認識（ローカル OCR + 視覚モデル、**一切アップロードしません**）。
- **💬 自分から話しかける** —— ちょうどいいタイミングで自分から話しかけてきます。
- **🖥️ デスクトップオーバーレイ** —— PySide6 の透明な立ち絵レイヤー。複数の衣装差分（制服 / 私服 / パジャマ …）。
- **クロスプラットフォーム** —— Linux と Windows の両方で動作。

---

## 🚀 インストール

### 1. 動作環境

- **Python 3.11**
- **NVIDIA GPU 推奨**（音声合成 / 音声認識 / 画面 OCR は GPU の方が快適です。CPU のみでも動作しますが遅くなります）
- OS：Linux または Windows 10/11

### VRAM 使用量の実測（2026-07-09）

テスト環境：NVIDIA GeForce RTX 4090 24GB、`nvidia-smi` を約 0.2 秒間隔でサンプリング。テスト前のデスクトップ/システムのベースラインは約 `2193 MiB`。下表は Spica 関連プロセスツリーの VRAM ピークです。

| 機能 | VRAM ピーク |
| --- | ---: |
| TTS GPT-SoVITS 4 感情 warmup | `2020 MiB` |
| STT faster-whisper warmup | `2080 MiB` |
| OCR RapidOCR フルフレーム | `1114 MiB` |
| 画面理解 OCR + Moondream HF | `5284 MiB` |
| 歌唱の音源分離 audio-separator | `2768 MiB` |
| RVC サブプロセス 20s | `2140 MiB` |
| 全重量級パイプライン重畳ピーク（TTS + STT + OCR/Moondream + 音源分離 + RVC） | `11116 MiB` |

カード全体の最大 `used` は `13340 MiB`。テスト前ベースラインを差し引くと、Spica のソフトウェアプロセスツリーのピークは約 `10.9 GiB` です。LLM 対話 / 要約 / ツッコミ judge はデフォルトでリモートの OpenAI 互換エンドポイントを使用し、ローカル VRAM はほとんど消費しません。アニメのダウンロードと再生は主に qBittorrent / VLC に依存し、Python の CUDA プロセスのピークには含まれていません。

### 2. クローン

```bash
git clone https://github.com/KASA2AI/Spica-Chatbot_Release.git
cd Spica-Chatbot_Release
```

### 3. 依存関係のインストール

専用の仮想環境（conda / venv）の使用を推奨します。プラットフォームに合わせてインストールしてください：

```bash
# ベース + 音声認識 + 画面認識
pip install -r requirements-stt.txt
pip install -r requirements-screen.txt

# ローカル設定スタジオ（Windows では requirements-windows-base.txt に含まれます）
python -m pip install -r requirements-config-studio.txt

# Windows ユーザーは以下の分割ファイルを参照：
#   requirements-windows-base.txt   基本動作
#   requirements-windows-app.txt    音声合成 + 歌唱（constraints-windows-app.txt と併用）
#   requirements-windows-heavy.txt  GPU 重量ランタイム
```

> 音声合成 / 歌唱はやや重いローカルランタイムを使用し、インストール容量が大きいため、GPU 搭載マシンでのインストールを推奨します。

### 4. モデルアセットパックのダウンロード（大きなファイルはリポジトリに含まれません）

**エンジンのソースコード（GPT-SoVITS / RVC）はすでにリポジトリに含まれており、`git clone` で入手できます。** このステップでは、git に置くのに適さない大きなファイル——モデルの重み、音声認識モデル、TTS リファレンス音声、立ち絵差分——を補うだけです。モデルアセットパックをダウンロードし、プロジェクトのルートに展開してください。重みは対応するエンジンのディレクトリに自動的に配置されます：

```bash
# Spica-Chatbot_Release/ の中で実行
unzip spica_full_assets_*.zip
```

展開後のディレクトリはこのようになります：

```text
Spica-Chatbot_Release/
  artifacts/
    tts_slim/
    rvc_slim/
  spica_data/
    models/
    voice/
    diffs/
```

アーカイブを `spica_data/` や `artifacts/` の中に展開しないでください。`spica_data/spica_data/...` のような入れ子構造になり、プログラムがモデルを見つけられなくなります。

パックの内容（すべて git にない大きなファイル）：

| 内容 | 展開先 | 目安サイズ |
| --- | --- | ---: |
| 音声合成エンジンの重み + Spica の声（GPT-SoVITS slim） | `artifacts/tts_slim/` | ~1.4 GB |
| 歌唱エンジンの重み + Spica の歌声（RVC slim） | `artifacts/rvc_slim/` | ~620 MB |
| 音声認識などのモデル | `spica_data/models/` | ~1.6 GB |
| TTS リファレンス音声 | `spica_data/voice/` | ~12 MB |
| Spica 立ち絵差分 | `spica_data/diffs/` | ~720 MB |

以下のコマンドで正しく配置されたか確認できます：

```bash
test -f artifacts/tts_slim/base/GPT_SoVITS/pretrained_models/chinese-hubert-base/pytorch_model.bin
test -f artifacts/rvc_slim/base/rvc/models/predictors/rmvpe.pt
test -f spica_data/models/faster-whisper-large-v3-turbo/model.bin
test -d spica_data/voice/happy
test -d spica_data/diffs
```

`artifacts/trt/` はダウンロード・アップロード不要です。GPU アーキテクチャや CUDA/TensorRT/ONNXRuntime のバージョンに依存するローカルの TensorRT エンジン/タイミングキャッシュで、必要なときにローカルで自動生成されます。

> 📦 **モデルアセットパックのダウンロード：** [Baidu Netdisk](https://pan.baidu.com/s/1EFq7t8Lxcy9kDNL7MzU1gg?pwd=nzjy)、抽出コード：`nzjy`

### 5. 外部プログラム（必要に応じて）

- **アニメ鑑賞** に必要：[qBittorrent](https://www.qbittorrent.org/)（Web UI を有効化）、[ffmpeg](https://ffmpeg.org/)、[VLC](https://www.videolan.org/)
- **マイクアレイ**（任意）：ReSpeaker（未接続の場合は通常のマイクを使用）

### 6. シークレットの設定

プロジェクトルートに `xiaosan.env` を作成し、必要な項目を記入します：

```env
OPENAI_API_KEY=あなたの LLM API キー     # 必須：対話用
JUDGE_API_KEY=                          # 任意：未記入なら上のキーを再利用
BILIBILI_COOKIE=                        # 任意：アニメを bilibili からダウンロードする場合
QBITTORRENT_PASSWORD=                   # 任意：アニメをマグネットでダウンロードする場合
```

> `xiaosan.env` はすでに `.gitignore` に含まれており、コミットされません。

### 7. 実行

```bash
python webui_qt.py
```

機能のオン/オフ（アニメ / 歌唱 / 画面認識の有効化、音声デバイス、GPU など）は `data/config/app.yaml` で調整します。

### 8. ローカル設定スタジオ

ローカル設定スタジオは、Spica とは別に起動するブラウザー管理画面です。Spica と同時には起動せず、リモートアクセスにも公開されません。既定では `127.0.0.1:8765` だけで待ち受け、標準ブラウザーを自動で開きます：

```bash
python scripts/config_studio.py
```

ポートを明示的に指定するか、ブラウザーの自動起動を無効にできます：

```bash
python scripts/config_studio.py --port 8765
python scripts/config_studio.py --no-open-browser
```

`--no-open-browser` を使う場合は、端末に表示されたローカル URL を開き、ページへ一度限りの起動認証を貼り付けます。右上の切り替えで `中文`、`English`、`日本語` を選べます。言語切り替えは画面の説明だけを変更し、設定キーや値は変更しません。終了するには、起動した端末で `Ctrl+C` を押してください。

---

## 🙏 謝辞

Spica は以下の優れたオープンソースプロジェクトの上に成り立っています：

- [GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS) —— 音声合成
- [Applio / RVC](https://github.com/IAHispano/Applio) —— 歌声変換
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) —— 音声認識
- [RapidOCR](https://github.com/RapidAI/RapidOCR) —— 画面テキスト認識
- [Moondream](https://github.com/vikhyat/moondream) —— 視覚理解
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) —— 動画ダウンロード

各コンポーネントの著作権は原作者に帰属します。それぞれのライセンスに従ってください。

---

## 📄 ライセンス

本プロジェクトは **Spica ソースアベイラブル・ライセンス（Source-Available License）** を採用しています——**ソースは公開されていますがオープンソースではありません**：ローカルでの使用・改変・貢献目的の fork は許可されますが、**書面による許可なく、再配布・商用利用・ビルド／ストア公開は禁止**です。Spica のキャラクター、声・歌声モデル、立ち絵などのアセットは本プロジェクトと共に使う場合のみ利用できます。全条項は [LICENSE](LICENSE) を参照してください。

> 同梱の第三者コンポーネント（GPT-SoVITS / RVC / faster-whisper / RapidOCR / Moondream / yt-dlp など）はそれぞれ独自のライセンスの下にあり、本ライセンスの**対象外**です——「謝辞」および各コンポーネントのディレクトリ内の LICENSE を参照してください。
>
> 商用利用は [www.acgkasa.me](https://www.acgkasa.me/) までご連絡ください。
