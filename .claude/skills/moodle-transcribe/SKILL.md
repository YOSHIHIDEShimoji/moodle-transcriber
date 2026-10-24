---
name: moodle-transcribe
description: Moodle の講義動画 URL（1 本〜複数）を受け取り、moodle-transcriber（python main.py）を起動・監視・検品し、失敗した動画は原因を切り分けて再実行、最後に講義まとめ（と課題があれば課題ファイル）を出力ディレクトリと ~/Downloads に書き出す。「文字起こしして」「この講義まとめて」「Moodle の動画を文字に」と moodle.gs.chiba-u.jp 等の URL が渡されたら使う。動画のダウンロードはしない。専用 Chrome（別プロファイル）で再生しページ内で音声を取るので、本人の Chrome や Spotify には影響しない。
---

# moodle-transcribe

役割分担: **Python が自律的に働く（再生・録音・文字起こし・終了検出・保存）。Claude は起動係・監視係・検品係・要約係。**
Python の処理を Claude が肩代わりしない（30 秒ごとの推論を仲介すると遅く、5 時間制限でも止まる）。
既往の障害 3 件はすべて「成功と報告するが実際は失敗」型だった。だから **run-summary.json と本文を必ず読んで判定する**。ログの「done!」を信用しない。

リポジトリ: `~/my-projects/moodle-transcriber`（pyenv `moodle-transcriber-3.11.9`、`.python-version` で自動選択）。
経緯・実測値・失敗パターンの一次資料: `docs/fix-plan-tab-id.md`。運用手順: `USAGE.md`。

## 0. 入力を整える

- URL は **`mod/scorm/player.php?a=NNN...`（プレーヤー URL）** を渡す。`view.php?id=NNN` は page エンジンではプレーヤーへ自動遷移しない場合があり未対応（渡されたら本人に player.php の URL を聞く）。重複・空行を除き、順序は渡された順を保つ。
- **実行前に必ず本人へ確認する**: 本数・URL 一覧・推定時間（動画長の合計 + 1 本あたり約 1.5 分。長さ不明なら「1 本 10〜20 分として」と書く）を 1 メッセージで示し、「実行」の返事を待つ。本人が「そのまま実行して」と明示している場合だけ省略可。Chrome を占有し音声出力を切り替える処理なので、無断で始めない。
- 無人で数時間規模になりそうなら、CLI（`claude --resume`）への切り替えを 1 行だけ提案する（`~/.claude/CLAUDE.md` の規則）。ブロッキングにはしない。

## 1. 事前確認（すべて読み取りのみ）

既定は **page エンジン**: 専用 Chrome（別プロファイル・別プロセス）を Playwright で動かし、ページ内で音声を取る。
**本人の Chrome には一切触らない。Spotify 等が鳴っていても混ざらない。BlackHole も不要。**

```bash
cd ~/my-projects/moodle-transcriber && python .claude/skills/moodle-transcribe/preflight.py
```

合格条件（page）: `playwright` が入っている、`Google Chrome.app` がある、whisper-server とモデルがある、他の main.py が走っていない。終了コード 0 = 合格。
`profile: ... 初回` と出たら、実行開始時に専用ブラウザがログイン画面を出すので本人にログインしてもらう（下記 2）。

従来方式（`--engine blackhole`）を明示されたときだけ `preflight.py --engine blackhole` を使う（本人の Chrome の Moodle タブ・BlackHole・複数出力装置・Spotify 停止が必要）。

## 2. 実行

URL を scratchpad の `urls.txt` に書き、**アプリ内ターミナル（run_in_terminal）で起動**する（本人が見え、Ctrl+C で止められる。サンドボックス外なので権限で詰まらない）。ターミナルが無ければ Bash の run_in_background。

```bash
cd ~/my-projects/moodle-transcriber && python -u main.py --url-file <scratchpad>/urls.txt < /dev/null
```

- `< /dev/null` でリネームプロンプト（60 秒待ち）をスキップする。
- 出力は `out/YYYYMMDD/<講義タイトル>.txt`、サマリーは同ディレクトリの `run-summary.json`（URL ごとに書き直される）。
- 既定モデルは `large-v3-turbo`。急ぐ・試すときだけ `--model small`。
- **ログイン**: 専用ブラウザのセッションが無い／切れていると、ログに `login required` が出て専用ブラウザのウィンドウが前面に来る。
  本人に「専用ブラウザ（右上の Chrome ウィンドウ）でログインしてください」と伝え、ログインを待つ（既定 10 分、`--login-timeout`）。
  Claude はログインしない。ログイン後は自動でプレーヤーに戻って続行する。
- 専用ブラウザのウィンドウは最小化しないよう本人に伝える（別デスクトップへ移すのは可）。ログイン済みなら `--headless` も使える。
- 講義の音はスピーカーから鳴らない（ページ内キャプチャ）。鳴らないのは正常。

## 3. 監視（ポーリングしない）

`logs/moodle-transcriber.log` を Monitor で `tail -f` し、次のシグネチャだけ拾う。タイムアウトは最長（30 分）で、切れたら再アーム。

```
play triggered|not started|gave up|login required|player not reached|page capture|tab not found|blind mode|stopping|qa:|Traceback|ERROR|never played|not processed
```

| ログ | 意味 | 対処 |
|---|---|---|
| `login required` | 専用ブラウザがログイン画面 | 本人にログインを依頼（Claude はしない）。10 分で諦める |
| `player not reached` | ログイン待ちタイムアウト or 想定外のページ | 本人にブラウザの状態を確認してもらい、該当 URL だけ再実行 |
| `page capture: no_video` | 動画要素が見つからない（SCORM の構成違い） | ページ構成を本人と確認。`--engine blackhole` も選択肢 |
| `not started (attempt n/5)` → `gave up` | `play()` しても再生が始まらない | 該当 URL だけ再実行。続くなら専用ブラウザで手動再生できるか本人に確認 |
| `tab not found` / `blind mode` | （blackhole エンジンのみ）タブ照合失敗 / JS が video に届かない | `docs/fix-plan-tab-id.md` 参照 |
| `qa: ...` WARNING | 自己診断の警告 | 4 の検品で判断 |
| `video never played` | 未再生で中止。保存・離脱は押していない | 再実行 |
| `Traceback` | クラッシュ | ログ全文を読んで修正 |

Python の終了コード: 0 = 全 ok、2 = failed があるか未処理 URL が残った。

## 4. 検品（run-summary.json と本文を読む）

```bash
cd ~/my-projects/moodle-transcriber && python .claude/skills/moodle-transcribe/show_summary.py out/YYYYMMDD/run-summary.json
```

- `total` が渡した URL 数と一致しているか（取りこぼし検知）。
- `failed`: 空・幻覚のみ・未再生・中断。**該当 URL だけ**を新しい `urls.txt` にして 2 を再実行する（同じ出力ディレクトリに `_2` 付きで保存される。元の失敗ファイルは残しておき、報告で明記する）。2 回失敗したら原因切り分けに移り、本人に報告する。
- `warn`: 本文を開いて判断する。「字/分が少ない」は無音の多い動画でも出る。「同一文の連発」は逆相音声か BGM のみ区間の幻覚。本文が講義として読めれば採用し、警告内容を報告に書く。
- `ok` でも先頭と末尾を数行ずつ読む。英語の歌詞・「ご視聴ありがとうございました」の連発・無関係な内容が無いか。

## 5. 講義まとめと課題

**まとめファイル**: `out/YYYYMMDD/講義まとめ.md`。構成は固定:

```
# 講義まとめ（YYYY-MM-DD）
## 全体
（科目名・形式・何本・合計時間・評価方法が分かれば 3〜5 行）
## 1. <講義タイトル>
- URL: ...
- 長さ: mm:ss / 文字起こし: N 行
- 要点: 3〜7 個（講師の主張・具体例・用語。固有名詞は本文の表記を使う）
- 検品メモ: ok / warn の内容（問題なければ「問題なし」）
## 2. ...
```

**課題ファイル**: 課題が **ある場合だけ** `out/YYYYMMDD/課題.md` を書く。無ければ作らず、報告に「課題なし（根拠: …）」と書く。
課題の判定材料は 2 つ。(a) 本文中の「レポート」「提出」「小テスト」「締切」「課題」の言及、(b) コースページ。
コースページ本文は、最後の動画の「活動から抜ける」で専用ブラウザが `course/view.php` に遷移していれば
**Python が自動で `out/YYYYMMDD/course-page.txt` に保存している**。まずそれを読む。

無い場合（最後の動画が失敗した等）は、コース URL（`course-page.txt` の 1 行目、または本人に聞く）を専用ブラウザで開いて読む:

```bash
cd ~/my-projects/moodle-transcriber && python tab_text.py --engine page --url "https://moodle.gs.chiba-u.jp/moodle/course/view.php?id=XXXXX" --max 8000
```

（専用プロファイルの Chrome を一時的に起こして読む。本人の Chrome には触らない。従来方式なら `--pattern course/view.php` で本人の Chrome のタブを読む。）
課題.md には、課題名・内容・提出先・締切（分かる範囲）・根拠の引用元（動画名 + タイムスタンプ or コースページ）を書く。推測で締切を埋めない。

## 6. ダウンロードフォルダへコピー

`~/Downloads/moodle-transcriber-YYYYMMDD/` を作り、**この実行の**成果物だけをコピーする（run-summary.json の `output` に列挙された本文 + 講義まとめ.md + 課題.md + run-summary.json）。同日の別実行の古いファイルは混ぜない。`rm` は使わない。

```bash
cd ~/my-projects/moodle-transcriber && python .claude/skills/moodle-transcribe/copy_to_downloads.py out/YYYYMMDD/run-summary.json
```

## 7. 報告

1 メッセージで: 表（動画 / 状態 / 行数 / 保存先）、まとめと課題ファイルのパス（リポジトリ内と Downloads の両方）、課題の有無と根拠、warn の中身、再実行したものとその結果。失敗が残っていれば原因の仮説と次の一手。

## 守ること

- 操作するのは**専用ブラウザだけ**。本人の Chrome には触らない（従来方式を明示されたときだけ例外）。
- 専用ブラウザでも、渡された URL の処理に必要な操作だけ（遷移・再生・保存・離脱）。ログインしない、別サイトへ行かない。
- 文字起こし中は専用ブラウザのウィンドウを閉じない・最小化しないよう本人に伝える。
- `git commit` / `push` はこのスキルの範囲外。コードを直した場合も本人の指示があるときだけ。
- Python 側の修正が必要になったら、`test_tool.py` を回し（`python test_tool.py`）、ロジック変更なら merge 前に `test-auditor` を回す（`~/.claude/CLAUDE.md`）。
