# PS5 Used Price Watch

定期的に中古PS5の検索ページをチェックし、**2万円台の候補**をまとめる GitHub Actions 用プロジェクトです。

> 注意: フリマ各社のページ構造・アクセス制限・利用規約は変わります。これは検索ページを低頻度で確認する補助ツールで、販売中・動作品を完全保証するものではありません。最終的には掲載ページで在庫、価格、動作、付属品を確認してください。

## 対象サイト
- メルカリ
- Yahoo!オークション
- Yahoo!フリマ
- ラクマ
- ハードオフ・オフモール
- ジモティー

検索語は `config.json` で変更できます。初期設定では初期型PS5（CFI-1000/1100/1200）とデジタル版も含め、2万円台を探します。ディスク版・デジタル版の両方を対象にし、ジャンク・故障・動作未確認の出品も検索します。

## 動かし方
1. GitHubで新しい空のリポジトリ（例: `ps5-used-watch`）を作成。
2. このZIPを展開して中身をリポジトリにアップロード。
3. リポジトリの **Settings → Actions → General** で Actions を許可。
4. **Actions → PS5 Used Watch → Run workflow** で初回実行。
5. 定期実行は UTC の `0,30 * * * *`（日本時間では毎時00分・30分）です。GitHub Actions の混雑時は遅れることがあります。

## 通知（任意）
Discord Webhook を使う場合:
1. DiscordでWebhookを作成。
2. GitHubリポジトリの **Settings → Secrets and variables → Actions → New repository secret** で `DISCORD_WEBHOOK_URL` を登録。
3. 次回実行から新規候補がDiscordに通知されます。

Webhookを設定しない場合、結果は Actions のログと `watch-results.json` アーティファクトで確認できます。

## 判定について
- 価格が20,000〜29,999円の検索結果を候補にします。
- **ジャンク品・故障品・動作未確認品も検索対象**です。通知では該当語を含む商品に「⚠️要確認」を付けます。
- 「動作確認済み」「完動品」などの語があるものは優先度を上げますが、文言だけで動作を保証するものではありません。
- 同じURLは state ファイルで重複通知を抑えます。
- 検索ページのHTMLが変わると結果が取れなくなることがあります。ログに各サイトの取得状況が出ます。

## ローカル実行
Python 3.11 以降:
```bash
pip install -r requirements.txt
python watcher.py
```
