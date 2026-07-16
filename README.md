# 体育館 空き通知（クラウド版 / GitHub Actions）

あなたのPCの状態に関係なく、GitHubのサーバー上で30分ごとに空きをチェックし、
「予約済→空き」に変わった枠だけGmailに通知します。PCはロック・スリープ・電源オフでOK。

- チェック: 30分ごと（GitHub Actionsのcron、UTC。JST 00:00〜05:00はスクリプトが自動でスキップ）
- 取得: headless Chromium (Playwright) で「ログイン不要の空き照会」を再現
- 通知条件: ある「施設×日付×時間」の空きコート数が 0→1以上（施設×日時で1通に集約）
- 送信: あなたのGmailからSMTP送信
- 状態保存: `state.json` をリポジトリに自動コミット（差分検知用）

## 含まれるファイル
```
scraper.py                       本体
requirements.txt                 依存(Playwright)
.github/workflows/check.yml      30分ごとのスケジュール実行
.gitignore
```

## セットアップ手順

### 1. GitHubリポジトリを作る
- GitHubにログイン（無ければ無料登録）。
- 新規リポジトリを作成。**Public 推奨**（PublicはActions実行時間が無制限。Privateは月2000分まででこの用途だと上限に近づきます）。
  ※ Publicでもメール等の秘密情報はコードに含まれず、後述のSecretsに安全に保管されます。
- この `gym-notifier-cloud` フォルダ内のファイルを**フォルダ構成そのまま**アップロード
  （`.github/workflows/check.yml` の階層を崩さないこと）。

### 2. Gmailアプリパスワードを発行
1. Googleアカウント → セキュリティ → **2段階認証を有効化**（未設定なら先に設定）。
2. 「アプリ パスワード」を開く（https://myaccount.google.com/apppasswords ）。
3. 名前を付けて生成 → 表示される**16桁**をコピー（スペースは不要）。

### 3. リポジトリにSecretsを登録
リポジトリの Settings → **Secrets and variables** → **Actions** → **New repository secret** で3つ登録:

| Name | Value |
|---|---|
| `GMAIL_ADDRESS` | r.shinoprivate@gmail.com |
| `GMAIL_APP_PASSWORD` | 手順2の16桁 |
| `NOTIFY_TO` | r.shinoprivate@gmail.com |

### 4. 書き込み権限を許可（状態コミット用）
Settings → Actions → **General** → 一番下 **Workflow permissions** → **Read and write permissions** を選択して保存。

### 5. 初回実行（ベースライン作成）
Actionsタブ → 「gym-availability-check」→ **Run workflow**（手動実行）。
初回は現在の空き状況を保存するだけで**通知は送りません**。以降30分ごとに自動実行され、
枠が「予約済→空き」に変わった時にメールが届きます。

## 動作確認
- Actionsタブで各実行の緑チェックとログを確認できます。
- リポジトリの `log.txt` に実行履歴（取得枠数・空き枠数・通知件数）が追記されます。
- `state.json` が更新されていれば差分検知が回っています。

## 注意
- GitHubのcronは混雑時に数分〜十数分遅れることがあります（個人利用なら実用上問題なし）。
- Publicリポジトリは**60日間コミットが無いとスケジュールが自動停止**します。本システムは
  30分ごとに `state.json` をコミットするため、稼働中は停止しません。
- **Cowork側の30分タスクは停止してください**（`gym-availability-notifier`）。両方動くと通知が二重になります。
- 予約は行いません（照会のみ）。ログインもしません。
- 監視対象・曜日時間の条件は `scraper.py` 冒頭の `FACILITIES` と抽出JS内のルール
  （土=全日 / 日=全日 / 木=19:00・20:00開始）で変更できます。
