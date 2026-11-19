# gemini-buddy-mcp

Claude Code などの MCP クライアントから Gemini を「相棒」として呼ぶための、stdio 型の MCP サーバーです。[agy-media](https://github.com/holmesfems/agy-media)（スキル版）の中身をすべて MCP のツールに移しました。

- **相談・メディア解析・コードレビュー**は、サブスクでログイン済みの [Antigravity CLI](https://antigravity.google/)（`agy`）をサブプロセスで動かします。子プロセスの環境変数から `GEMINI_API_KEY`・`GOOGLE_API_KEY` を消すので、API キーの課金には流れません。
- **画像・音声の生成**は、`GEMINI_API_KEY` を使って `google-genai` SDK で行います（API のクレジット枠）。画像は agy の組み込みの生成（サブスク）でも作れます。
- **機械的な下ごしらえと計測はローカルで**：ffmpeg での切り出し・コマ画像化・座標の目盛り、無音区間・ラウドネス・スペクトログラム、声と BGM の音量差、OpenCV での追跡、座標の描き込み確認。
- どのツールも `session_id` を受け取れます。会話・解析したメディアの要約・作ったファイルは `~/.gemini-buddy-mcp/sessions/<session_id>.json` に残り、同じ `session_id` を渡すと続きから頼めます。
- ツールの返り値はテキストとファイルのパスだけで、画像や音声のバイナリは返しません。

> English: A stdio MCP server that lets Claude consult Gemini through the subscription-authenticated `agy` CLI (API keys are stripped from its environment) for second opinions, media analysis and code review; generate images and speech with the `google-genai` SDK; and prepare / measure media locally (ffmpeg, numpy, OpenCV). Every tool shares a persistent `session_id`. Tools return text and file paths, never binary data.

## ツール

### Gemini に頼む

| ツール | 使う枠 | すること |
|---|---|---|
| `gemini_consult(prompt, file_paths?, session_id?, model?)` | agy | コードや設計のセカンドオピニオン。`file_paths` は Gemini が自分で開いて読む |
| `gemini_analyze_media(file_path, prompt, session_id?, model?, more_files?)` | agy | 動画・音声・画像を見て・聞いての文字起こし、話者分け、画面の動き、BGM が場面に合うか、おおよその座標。答えの要約をセッションに残す |
| `gemini_review(repo, base?, commit?, staged?, intent?, models?, allow_secret_like?, session_id?, out_dir?)` | agy | git の差分を読むだけでレビュー。複数のモデルを並べると、同じ場所の指摘をまとめる |
| `gemini_generate_image(prompt, output_path, session_id?, backend="api")` | API / agy | 画像を作って保存し、パスを返す。`backend="agy"` でサブスク（1 枚 50 秒ほど） |
| `gemini_generate_audio(text, output_path, voice_name="Kore", session_id?)` | API | TTS で読み上げて保存し、パスと長さ（秒）を返す。`text` には読む文だけを書く（「Say cheerfully:」のような指示も読み上げられる） |
| `gemini_list_sessions(limit=20)` | — | 保存したセッションを新しい順に（`session_id`・更新日時・タイトル・関係するファイル） |

### ローカルの下ごしらえと計測（モデルは使わない）

| ツール | すること |
|---|---|
| `media_clip(video_path, start, end, …)` | agy に渡す小さな mp4（既定 720p・30fps）を切り出す |
| `media_frames(video_path, start, end, fps, crop?, grid?, sheet?, …)` | 範囲をコマ画像にし、`index.json`（各コマの元動画での秒・切り抜きのずれ）を書く。`grid=100` で**元動画の座標**の目盛りを描く。`sheet` で一覧画像 |
| `media_audio(src_path, start, end, …)` | `audio.wav`（モノラル 16 kHz）・波形・スペクトログラム・ラウドネス（LUFS）・音のある区間／無音区間（元ファイルの秒） |
| `media_mix_balance(mix_path, voice_path, …)` | 声だけのトラックを完成品に位置合わせして差し引き、声と BGM の差と、声が埋もれる区間（`buried_spans`）を出す |
| `media_track(video_path, start, end, box, …)` | OpenCV のテンプレートマッチングで 1 つの物を追う（全フレームの中心座標と一致度） |
| `media_overlay(index_path, track_paths?, points?, …)` | 座標をコマに描き込んで並べた確認用の画像を作る。Gemini の答え（`points`）と追跡の結果を色違いで重ねられる |

- `session_id` を省くと新しいセッションを作ります（ローカルのツールは作りません）。タイトルは最初のプロンプトを 60 文字に縮めたものです。
- ローカルのツールの出力先を省くと、`~/.gemini-buddy-mcp/work/<session_id>/`（セッションなしなら `_misc/`）の下に新しいフォルダを作ります。
- サーバーは MCP の `instructions` で、使い方の注意（下の「確かめ方」）をクライアントに渡します。

## 確かめ方（agy の答えをそのまま信じない）

- **座標**：`media_frames` の目盛り付きのコマで読ませ、`media_overlay` で描き込んで自分の目で見る。読ませるモデルは `gemini-4-neon-high`。
- **時刻**：Gemini が返す時刻は 0.5〜1 秒ずれる。`media_audio` の `sound_spans` を使う（無音区間は発話の区切りとほぼ一致し、字幕との差は 0.05 秒以内だった）。
- **BGM の音量**：`media_mix_balance` の数値で判断する。トーク動画の目安は BGM が声より 12〜20 dB 下。Gemini は「BGM が大きめ」という全体の傾向は当てたが、「声が埋もれている」と挙げた時刻は外れていた。曲調が場面に合うかの判断は Gemini に任せてよいが、1 つの意見として扱う。
- **発音の違和感**：候補として扱い、人が聞いて確かめる（モデルごとに挙げる語がばらばら）。
- **コードレビュー**：指摘は 1 件ずつ実際のコードでたどってから報告する。`agreed`（同じ場所を挙げたモデルの数）が多くても確かめずに本物扱いしない。逆に 1 モデルだけの指摘にも本物はある。`intent` には作者の意図だけを書き、Claude の意見は混ぜない。

## 必要なもの

- Python 3.10+
- `agy`（インストールしてログイン済み。`agy models` でモデルが出ること）
- 生成ツールを API で使うなら `GEMINI_API_KEY`
- ローカルのツールには Pillow・numpy・OpenCV・ffmpeg（`[media]` で入る。ffmpeg は PATH に無ければ imageio-ffmpeg のものを使う）
- `gemini_review` には git

## 入れ方

```powershell
git clone https://github.com/holmesfems/gemini-buddy-mcp
```

```powershell
cd gemini-buddy-mcp
```

```powershell
python -m venv .venv
```

```powershell
.venv\Scripts\python -m pip install -e ".[media]"
```

## Claude Code への登録

venv の中の実行ファイルを指定します（パスは clone した場所に合わせてください）。`--scope user` にすると、どのプロジェクトからも使えます。

```powershell
claude mcp add gemini-buddy --scope user -e GEMINI_API_KEY=$env:GEMINI_API_KEY -- "C:\path\to\gemini-buddy-mcp\.venv\Scripts\gemini-buddy-mcp.exe"
```

macOS / Linux：

```bash
claude mcp add gemini-buddy --scope user -e GEMINI_API_KEY="$GEMINI_API_KEY" -- /path/to/gemini-buddy-mcp/.venv/bin/gemini-buddy-mcp
```

uv を使うなら、clone せずに：

```bash
claude mcp add gemini-buddy --scope user -e GEMINI_API_KEY="$GEMINI_API_KEY" -- uvx --from "gemini-buddy-mcp[media] @ git+https://github.com/holmesfems/gemini-buddy-mcp" gemini-buddy-mcp
```

- API での生成を使わないなら `-e GEMINI_API_KEY=...` は省けます（ほかはすべて agy のログインとローカルで動きます）。
- agy の答えは 10 秒〜数分かかります（コードレビューは 1 モデルで 2〜4 分）。Claude Code 側が先に打ち切るときは、起動前に `MCP_TOOL_TIMEOUT`（ミリ秒）を長めにしてください。

## 設定（環境変数）

| 変数 | 既定 | 意味 |
|---|---|---|
| `GEMINI_API_KEY` | — | API での生成に使うキー（agy には渡さない） |
| `GEMINI_BUDDY_CONSULT_MODEL` | `gemini-4-neon-high` | `gemini_consult` の agy モデル |
| `GEMINI_BUDDY_MEDIA_MODEL` | `gemini-4-hydrogen-high` | `gemini_analyze_media` の agy モデル |
| `GEMINI_BUDDY_AGY_IMAGE_MODEL` | `gemini-4-hydrogen-high` | `backend="agy"` の画像生成で使う agy モデル |
| `GEMINI_BUDDY_IMAGE_MODEL` | `gemini-nano-banana-2.1` | API の画像生成モデル |
| `GEMINI_BUDDY_TTS_MODEL` | `gemini-3.8-flash-tts` | API の読み上げモデル |
| `GEMINI_BUDDY_AGY_TIMEOUT` | `600` | agy を待つ秒数（レビューは 1 モデル 900 秒） |
| `GEMINI_BUDDY_AGY` | PATH の `agy` | agy の実行ファイル |
| `GEMINI_BUDDY_HOME` | `~/.gemini-buddy-mcp` | セッションと作業フォルダを置く場所 |
| `AGY_BRAIN_DIR` | `~/.gemini/antigravity-cli/brain` | agy が画像を保存するフォルダ（`backend="agy"` で取り出す） |

API の画像・TTS のモデル ID が違っていて失敗したときは、エラーにその API キーで使える近い名前のモデルの一覧が付きます。それを環境変数に入れてください。

## Gemini 4 のモデルの比べ（agy-media での実測、agy 1.5.12）

同じ素材で Argon・Neon・Hydrogen（いずれも High）を比べ、既定のモデルを決めた。

| 試したこと | Argon | Neon | Hydrogen | 使うモデル |
|---|---|---|---|---|
| 音声 40 秒の文字起こし・話者分け | 本文・話者とも正しい／70 秒 | 同じ／103 秒 | 同じ／49 秒 | Hydrogen |
| 発音の違和感の指摘 | 12 件（多すぎる） | 3 件 | 3 件 | どれも候補扱い |
| 動画 40 秒（ロゴ・タイトル・セリフ） | 正しい／86 秒 | 正しい／52 秒 | 正しい／45 秒 | Hydrogen |
| 目盛り付きコマの座標（6 か所） | 字幕を 140 px 外す | 2 px 以内 | 縦に 8 px ずれ | Neon |
| コードレビュー（4 つ埋めたバグ） | 4/4／161 秒 | 4/4 ＋本物の競合 1 件／217 秒 | 4/4／90 秒 | Neon |

- Flash 系のモデルは、音声のあと出力が途切れて失敗した。
- ゲーム録画の敵の位置は、目盛りなしでは約 100 px ずれた。目盛りありでも、動いている間は 50〜90 px ずれる。おおよその位置や動きの流れには使えるが、精密な位置合わせには `media_track` を使う（エフェクトが激しく重なる対象は見失いやすい）。
- コードレビューで差が出たのは、埋めていない本物の不具合（保存の競合）で、見つけたのは Neon だけだった。

## 安全のために

- agy は `--mode plan`（読むだけ）で動かし、`--dangerously-skip-permissions` は付けません。agy が読めるのは、渡したファイルのあるフォルダ（`--add-dir`）だけです。`backend="agy"` の画像生成だけは、agy が自分のフォルダに画像を書けるよう `accept-edits` で動かし、ほかのファイルに触れないよう指示します。
- 頭なしの agy はコマンドを実行できないので、切り出しなどは `media_*` で済ませてから渡してください。`denied_actions` が返ったら、agy が何かを実行しようとした印です。
- `gemini_review` のコードは Google に送られます。`.env`・鍵・credentials のファイルは差分から外し（大文字小文字は問わない）、差分のどの行にでも API キーや秘密鍵らしい文字列があれば、何も送らずに `STOPPED` を返します。ただし agy はリポジトリ全体を読めるので、秘密を置いたファイルがあるなら agy の settings.json の deny（`read_file(regex:.*\.env.*)` など）に足してください。
- `session_id` は UUID しか受け付けません（セッションのフォルダの外を指せないように）。

## テスト

agy も API キーも使わないテスト（ローカルのツールは ffmpeg で作った動画・音声で確かめる）：

```powershell
.venv\Scripts\python -m unittest discover -s tests
```
