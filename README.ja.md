[简体中文](README.md) · [English](README.en.md) · **日本語**

# Spica Chatbot

**デスクトップに暮らす音声キャラクター。** 透明な立ち絵との会話、ギャルゲーの付き添い、アニメ視聴、歌唱に対応。自分のキャラクター、声、会話ウィンドウに着せ替えられます。

[公式サイト・デモ](https://www.acgkasa.me/) · [設定・カスタマイズ](docs/README.ja.md) · [キャラクター素材](https://pan.baidu.com/s/1RDoz_iRvFJdB71Aia3EPaw?pwd=j2u6)

## ✨ 機能

- **会話と記憶**：文字入力、マイク会話、自発的な発話、キャラクターごとの記憶。
- **複数キャラクター**：カード、衣装、表情、声をパックで読み込み、再起動で切り替え。
- **動く立ち絵**：目の原画による視線追従とまばたき。
- **会話スタイル**：配色、背景、動く文末マーカーを独立したパックで変更。
- **デスクトップでの活動**：ギャルゲー、画面理解、アニメ視聴、歌唱。

Windows / Linux 向けのデスクトップ専用版です。音声・画面認識はローカルで動作します。リモート会話モデルを使う場合、会話に必要なテキスト、カード、記憶は設定したサービスへ送信されます。

## 🚀 はじめる

**Python 3.11、Git、Conda** を用意します。音声などの重い機能には NVIDIA GPU を推奨します。

```bash
git clone https://github.com/KASA2AI/Spica-Chatbot_Release.git
cd Spica-Chatbot_Release
conda create -n spica python=3.11 -y
conda activate spica
python -m pip install --upgrade pip
python -m pip install -r docs/requirements/requirements-windows-base.txt
```

Ubuntu / Debian では先に `build-essential python3-dev portaudio19-dev ffmpeg` をインストールします。基本依存リストは Linux でも利用できます。

初回は[起動設定](docs/README.ja.md#setup)で API を設定し、小さなサンプルで文字会話を試してください。追加の依存とモデルは[音声設定](docs/README.ja.md#voice)にあります。設定後の起動方法：

```bash
conda activate spica
python -X utf8 webui_qt.py
```

Windows では `powershell -ExecutionPolicy Bypass -File .\scripts\windows\run_spica.ps1` も利用できます。既定の Conda 環境は `spica` です。

## 📦 キャラクターとスタイルのダウンロード

[百度網盤のキャラクター素材パック](https://pan.baidu.com/s/1RDoz_iRvFJdB71Aia3EPaw?pwd=j2u6) · 抽出コード：**`j2u6`**

ZIP は先に展開します。キャラクターは `Desktop-Packs/Characters/`、スタイルは `Desktop-Packs/Dialogue-Styles/` に置くと整理できます。設定から `meta.json` を含むキャラクターフォルダーと `style.json` を含むスタイルフォルダーを別々に読み込み、**重启程序**（再起動）を押します。

大きなモデルや完全なキャラクター素材は Git に含まれません。キャラクターパックとは別に共有音声エンジン・認識モデルが必要です。小さな[静止画サンプル](Desktop-Packs/Characters/Examples/static/)と[動くサンプル](Desktop-Packs/Characters/Examples/eye-rig/)は同梱されています。

## 📖 ドキュメント

- [設定・カスタマイズ](docs/README.ja.md)：起動、カード、声、動く立ち絵、会話スタイル。
- [キャラクター形式リファレンス](docs/CHARACTER_PACKS.md) · [会話スタイルリファレンス](docs/DIALOGUE_STYLES.md) — 中国語。
- [依存リスト](docs/requirements/)：基本、GPU、音声、オプション機能。

## ライセンス

コードは [Source-Available ライセンス](LICENSE)です。イラスト、声、その他の第三者素材には各自のライセンスが適用されます。パックを共有する際は作者と出典を記載してください。

[GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS)、[Applio](https://github.com/IAHispano/Applio)、[faster-whisper](https://github.com/SYSTRAN/faster-whisper)、[RapidOCR](https://github.com/RapidAI/RapidOCR)、[Moondream](https://github.com/vikhyat/moondream)、[yt-dlp](https://github.com/yt-dlp/yt-dlp) に感謝します。
