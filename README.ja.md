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

# Windows ユーザーは以下の分割ファイルを参照：
#   requirements-windows-base.txt   基本動作
#   requirements-windows-app.txt    音声合成 + 歌唱（constraints-windows-app.txt と併用）
#   requirements-windows-heavy.txt  GPU 重量ランタイム
```

> 音声合成 / 歌唱はやや重いローカルランタイムを使用し、インストール容量が大きいため、GPU 搭載マシンでのインストールを推奨します。

### 4. エンジンとモデルのダウンロード（リポジトリには含まれません）

音声合成 / 歌唱エンジン（GPT-SoVITS・RVC のランタイムコード + 重み）はサイズが大きく、認識モデルや立ち絵差分と合わせて**コードリポジトリには同梱されていません**。別途ダウンロードし、対応するディレクトリに展開してください——**いずれかのパックが欠けると、その機能は起動しません**：

| 内容 | 展開先 | 目安サイズ |
| --- | --- | ---: |
| 音声合成エンジン + Spica の声（GPT-SoVITS slim） | `artifacts/tts_slim/` | ~1.4 GB |
| 歌唱エンジン + Spica の歌声（RVC slim） | `artifacts/rvc_slim/` | ~620 MB |
| 音声認識などのモデル | `spica_data/models/` | ~1.6 GB |
| TTS リファレンス音声 | `spica_data/voice/` | ~12 MB |
| Spica 立ち絵差分 | `spica_data/diffs/` | ~720 MB |

> 📦 **ダウンロード先：** _（未記入 —— クラウドストレージ / Release アセット / HuggingFace リンク）_

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

_（ライセンス未定 —— 記入してください。上記で参照しているコンポーネントの個別ライセンスにご注意ください。）_
