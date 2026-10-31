# LINE セグメント配信（CRM）— Dify ワークフローテンプレート

Googleスプレッドシートを顧客台帳にして、**セグメントごとに違う頻度・違う文面**でLINEメッセージを自動配信するDifyワークフローテンプレートです。LLM不使用のテンプレート埋め込み方式なので、文面がブレず追加コストもゼロ。**文面・セグメント・頻度・Discordレポートの文言まで、すべてスプレッドシート編集だけで運用**できます。

- 対象プラグイン: **LINE Message Sender (shineos)** ＋ **google_sheets (omluc)** — インポート時に自動インストールされます
- 顧客データ: **無料のGoogleスプレッドシート**（4タブ: master / campaign / history / settings）
- 認証: **サービスアカウント（JSONキー1個）** — OAuth同意画面・トークン失効管理は不要

```
[スプレッドシート] master / campaign / history / settings
      ↓ google_sheetsプラグイン（サービスアカウント認証）
[Dify] 対象者抽出（頻度・重複・ボットID検出） → テンプレート文面生成 → LINE Push
      ↓
[history] 自動記録 → [Discord] 集計レポート（1枚カード）
```

ワークフローにはセットアップ手順のメモ4枚が付属しています。

---

## ステップ1: スプレッドシートを用意する

1. Googleスプレッドシートを新規作成し、**4つのタブ**を作ります（タブ名は英字・大文字小文字まで一致させてください）。

   | タブ | 役割 | ヘッダ（1行目） |
   |---|---|---|
   | `master` | 顧客マスター（人が編集） | `user_id / display_name / segment / frequency / context / memo` |
   | `campaign` | セグメント別文面テンプレート（人が編集） | `segment / template_text / enabled` |
   | `history` | 送信履歴（ワークフローが追記のみ） | `sent_at / user_id / segment / status / message_text / request_id` |
   | `settings` | Discordカードの文言（任意・無くても動作） | `key / value` |

2. 動作確認用のデータは `samples/` のCSV（master / campaign / settings）を流用できます。

3. **重要な列の意味**

   | 項目 | 説明 |
   |---|---|
   | `user_id` | 宛先の**友だちユーザーID（`U...`）**。Get Bot Infoが返す「ボット自身のuserId」は宛先にできません（テンプレートが自動検出して「ボット自身のID N件」と報告します） |
   | `segment` | `campaign` タブの `segment` と同じ値にすると、その文面が使われます |
   | `frequency` | 送信間隔（**日数**）。`7` なら前回成功から7日経過後に再送 |
   | `context` | 文面の `{context}` に入る文字列（空可） |
   | `display_name` | 文面の `{display_name}` に入る名前（空なら「お客様」） |

   同じ `user_id` でも **`segment` が違えば別文面を1回の実行で届けられます**（同一実行内の重複除外は「UID×セグメント」単位）。

4. 文面変更・キャンペーン切替・顧客追加は、**このスプレッドシート編集だけで完結**します（ワークフローには触りません）。

## ステップ2: サービスアカウントを作成する

1. [Google Cloud Console](https://console.cloud.google.com/) → **IAMと管理 → サービスアカウント → 作成**（名前は任意、ロールは空でOK）
2. 作成したサービスアカウント →「**キー**」タブ →「鍵を追加」→「新しい鍵を作成」→ **JSON** をダウンロード
3. スプレッドシートの「**共有**」で、JSON内の `client_email`（例: `xxx@xxx.iam.gserviceaccount.com`）を**編集者**として追加

> 読み取りだけ成功して書き込みが403になる場合は、共有ロールが「閲覧者」になっていません。「編集者」に変更してください。

## ステップ3: テンプレートをインストールする

1. Difyのスタジオ →「**テンプレートから作成**」（またはDSLインポート）で本テンプレートを選択
2. 必要なプラグイン（google_sheets / LINE Message Sender）は**自動でインストール**されます
3. **ツール → google_sheets → 認可** で、ダウンロードした**JSONファイルの中身をそのまま貼り付け**
4. **ツール → LINE Message Sender → 認可** で、LINE Developersコンソールの**長期チャネルアクセストークン**を貼り付け

## ステップ4: 環境変数を設定する

ワークフローの環境変数に以下を登録します:

| 変数名 | 値 | 型 |
|---|---|---|
| `SPREADSHEET_ID` | スプレッドシートURLの `/d/` と `/edit` の間の文字列 | string |
| `ADMIN_LINE_ID` | （任意）管理レポートのLINE宛先。未設定ならLINE通知をスキップ | string |
| `DISCORD_WEBHOOK_URL` | （任意）Discord Webhook URL。未設定ならDiscord通知をスキップ | secret |

管理レポートは **LINE / Discord のどちらか一方でも運用できます**（両方空でも、レポート出力のみで正常完了します）。

## ステップ5: テストする（2段階）

1. **ドライラン** — 開始ノードの `dry_run` は初期値 `true`。このまま実行すると**送信ゼロ**で「計画: 送信対象 N件（セグメント別内訳）」のレポートだけ返します（履歴も追記されません）
2. **実送信** — `master` の `user_id` を実在する受信者に置き換え、`dry_run=false` で実行 → LINE着信 → `history` に `success` 記録（`request_id`付き）→ Discordに緑カード

> 宛先が無効な場合も、1件の失敗で全体は止まりません（`failed` 記録 → 次回自動リトライ）。

## 定期実行

- **手動実行**: キャンバス右上の「実行」
- **Difyのトリガー**: トリガーノード対応バージョンなら、開始ノード前にスケジュールを追加
- **外部cron → API**: アプリAPIキーで `POST /v1/workflows/run` を実行（`dry_run` は明示的に `false` を渡す）

## Discordレポート（settingsタブでカスタマイズ）

送信結果はDiscordに**集計カード1枚**で通知されます（セグメント別に集計）:

```
LINE配信レポート
計画: 送信対象 3件（active 1 / dormant 1 / vip 1）
（空行）
<スキップ: 最近送信済 0・文面未定義 0>
実行モード    送信結果       送信対象
本番配信      成功 3 / 失敗 0   3件
成功内訳
active 1 / dormant 1 / vip 1
Power by Shineos Inc.   ← https://shineos.com/ へのリンク
```

- **カードの色**: 全成功=緑 ／ 失敗あり=オレンジ ／ dry-run=青
- **文言は `settings` タブで変更可能**: `report_title`（タイトル）、`label_result`（項目名）、`value_result`（値の書式。`{success}` `{failed}` が使えます）、`label_count` / `value_count`（`{count}`）、`footer_text` / `footer_url` など
- セグメント名をシートで変更すると、**レポートの集計表示も自動的に新しい名前になります**
- セキュリティ上、Webhook URLは **https かつ discord.com 系のみ**送信します（Codeノードで検証）

## トラブルシューティング

| 症状 | 対処 |
|---|---|
| `invalid_client` / `invalid_grant` | サービスアカウントJSONの誤り。再作成して再認可 |
| 書き込みが403（`history`が記録されない） | シート共有のロールが「閲覧者」。**編集者**に変更 |
| 読み取りが403 | シートがSAに共有されていない。`client_email` を共有に追加 |
| `Unable to parse range` | タブ名の全角・空白・大文字小文字のずれ（`master`/`campaign`/`history`/`settings`） |
| レポートに「ボット自身のID N件」 | 宛先にGet Bot Infoの`userId`（ボット自身）が含まれている。友だちのUIDに置き換えてください |
| LINE送信が400「can't send messages to yourself」 | ボット自身への送信は不可。別の受信者でテストしてください |
| Discordが届かない | URLが `https://discord.com/api/webhooks/...` 形式か確認。https以外は仕様でスキップ |
| 再実行したら全員「最近送信済」 | 頻度制御が正常動作。再送したい場合は `history` の該当行を削除 |

## セキュリティと注意事項

- スプレッドシートは**公開共有しない**でください（サービスアカウント経由でのみアクセスします）
- 認証情報（SA JSON・チャネルアクセストークン）はDifyの認可画面（暗号化保存）のみに入力し、ワークフローやシートに直書きしない
- 商業メッセージの配信は**特定電子メール法**と**LINE公式アカウントの規約**の対象です。配信前にオプトインを確認してください
- Push送信は**LINEの月次メッセージ枠**を消費します（1実行あたり最大100件、超過分は翌回に繰り越し）

## 開発（リポジトリから改修する場合）

```
line-segment-broadcast-gsheets.yml   DifyにインポートするDSL（生成物）
code/*.py                            Codeノードの正本ロジック
build_dsl.py                         code/*.py をYAMLの code: ブロックへ注入
tests/test_code_nodes.py             ロジック単体テスト + DSL整合性チェック
google_setup.py                      シートの初期化ツール（タブ＋サンプルデータ）
samples/*.csv                        スプレッドシートの雛形
```

ロジックを変更するときは `code/*.py` を編集してから:

```sh
python3 build_dsl.py          # YAMLへ注入
python3 -m pytest tests -q    # 50件のテスト（YMLと正本の不一致も検出）
```

## 作者

- **shineos** — https://shineos.com/
- 関連プラグイン: [LINE Message Sender](https://github.com/Shineos/dify-line-message-sender)（Dify Marketplace公開中）
