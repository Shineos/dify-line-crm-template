"""Google OAuth セットアップツール（line-crm-template 用）.

サブコマンド:
    auth    ブラウザ承認 → リフレッシュトークンを自動取得して .env.local に保存
    select  Drive APIでスプレッドシートを一覧表示し、選択したIDを .env.local に保存
    init    選択中スプレッドシートに master/campaign/history タブとテストデータを投入

使い方:
    python3 google_setup.py auth                 # ブラウザが開く。承認すると自動で続行
    python3 google_setup.py select               # 一覧から番号で選択
    python3 google_setup.py select --pick 1      # 一覧の1番を自動選択
    python3 google_setup.py init                 # タブ作成 + テストデータ投入

セキュリティ:
    - 送信先は https の Google 公式ホストのみ（ホスト許可リストで検証）。
    - 認証情報は .env.local（gitignore済み）からのみ読み書きする。ソースに書かない。
    - ローカルサーバーはリダイレクト受信専用で、外部に送信しない。
"""

import argparse
import base64
import http.server
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env.local"

# このツールが通信してよいホスト（https限定・Google公式のみ）
ALLOWED_HOSTS = frozenset(
    {
        "oauth2.googleapis.com",
        "accounts.google.com",
        "sheets.googleapis.com",
        "www.googleapis.com",
    }
)

REDIRECT_HOST = "127.0.0.1"
REDIRECT_PORT = 8899
SCOPES = (
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.metadata.readonly",
)

TAB_HEADERS = {
    "master": ["user_id", "display_name", "segment", "frequency", "context", "memo"],
    "campaign": ["segment", "template_text", "enabled"],
    "history": ["sent_at", "user_id", "segment", "status", "message_text", "request_id"],
    "settings": ["key", "value"],
}
TEST_ROWS = {
    "master": [
        ["Udemo0000000000000000000000000001", "田中 太郎", "vip", "7", "先月ご購入のA商品", "デモ用（実送信前に自分のLINE IDへ置換）"],
        ["Udemo0000000000000000000000000002", "佐藤 花子", "active", "30", "", "デモ用"],
        ["Udemo0000000000000000000000000003", "鈴木 一郎", "dormant", "90", "半年ぶりのご連絡", "デモ用"],
    ],
    "campaign": [
        ["vip", "【先行案内】{display_name}、いつもご利用ありがとうございます。{context}の新作をVIPのお客様に先にお知らせします。", "TRUE"],
        ["active", "{display_name}、今週のおすすめ情報をお届けします。{context}", "TRUE"],
        ["dormant", "{display_name}様、お久しぶりです。最近の新商品をまとめました。ぜひご覧ください。", "TRUE"],
    ],
    "history": [],
    "settings": [
        ["report_title", "LINE配信レポート"],
        ["report_title_dry", "LINE配信レポート（dry-run）"],
        ["label_mode", "実行モード"],
        ["value_mode_real", "本番配信"],
        ["value_mode_dry", "ドライラン（送信なし）"],
        ["label_result", "送信結果"],
        ["value_result", "成功 {success} / 失敗 {failed}"],
        ["value_result_dry", "送信なし（計画のみ）"],
        ["label_count", "送信対象"],
        ["value_count", "{count}件"],
        ["label_breakdown", "成功内訳"],
        ["label_failed_ids", "失敗ID（次回自動リトライ）"],
        ["footer_text", "Power by Shineos Inc."],
        ["footer_url", "https://shineos.com/"],
    ],
}


# ---------------------------------------------------------------- 基盤

def load_env():
    values = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                values[key] = value
    return values


def upsert_env(key, value):
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    prefix = "%s=" % key
    kept = [line for line in lines if not line.strip().startswith(prefix)]
    kept.append("%s=%s" % (key, value))
    ENV_FILE.write_text("\n".join(kept) + "\n", encoding="utf-8")
    os.chmod(ENV_FILE, 0o600)


def request_json(url, payload=None, method=None, token=None, form=False):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
        raise SystemExit("許可リスト外のURLには接続しません: %s" % url)
    if form:
        data = urllib.parse.urlencode(payload or {}).encode("utf-8")
        request = urllib.request.Request(url, data=data, method=method or "POST")
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
    else:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"))
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read().decode("utf-8", "replace")
            return response.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8", "replace")
        try:
            return error.code, json.loads(raw)
        except json.JSONDecodeError:
            return error.code, {"raw": raw[:400]}


def get_access_token(env):
    for key in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN"):
        if not env.get(key):
            raise SystemExit(".env.local に %s がありません。先に `python3 google_setup.py auth` を実行してください" % key)
    status, body = request_json(
        "https://oauth2.googleapis.com/token",
        payload={
            "client_id": env["GOOGLE_CLIENT_ID"],
            "client_secret": env["GOOGLE_CLIENT_SECRET"],
            "refresh_token": env["GOOGLE_REFRESH_TOKEN"],
            "grant_type": "refresh_token",
        },
        form=True,
    )
    if status != 200 or "access_token" not in body:
        raise SystemExit("アクセストークン取得失敗（HTTP %s）: %s" % (status, json.dumps(body, ensure_ascii=False)[:300]))
    return body["access_token"]


# ---------------------------------------------------------------- auth

def cmd_auth(args):
    env = load_env()
    for key in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"):
        if not env.get(key):
            raise SystemExit(".env.local に %s がありません" % key)

    redirect_uri = "http://%s:%d" % (REDIRECT_HOST, REDIRECT_PORT)
    state = secrets.token_urlsafe(16)
    params = urllib.parse.urlencode(
        {
            "client_id": env["GOOGLE_CLIENT_ID"],
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
    )
    auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + params
    result = {}

    class Callback(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            if "code" in query and query.get("state", [""])[0] == state:
                result["code"] = query["code"][0]
                self.send_response(200)
            elif "error" in query:
                result["error"] = query["error"][0]
                self.send_response(400)
            else:
                self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            message = "認証成功。このタブは閉じて構いません。" if "code" in result else "認証が拒否されたか失敗しました。"
            self.wfile.write(message.encode("utf-8"))

        def log_message(self, *args):
            pass

    try:
        server = http.server.HTTPServer((REDIRECT_HOST, REDIRECT_PORT), Callback)
    except OSError as error:
        raise SystemExit("ポート %d を使用できません（%s）。他プロセスを確認してください" % (REDIRECT_PORT, error))

    print("ブラウザでGoogle承認画面を開きます。アカウントを選び「許可」を押してください。")
    print("（エラーになる場合はGCPでリダイレクトURI %s を登録してください）" % redirect_uri)
    if sys.platform == "darwin" and not args.no_open:
        subprocess.run(["open", auth_url], check=False)
    else:
        print("URL:", auth_url)

    deadline = time.time() + args.timeout
    while time.time() < deadline and not result:
        server.handle_request()  # 1リクエスト処理して戻る（favicon等は無視して再待機）
        if result.get("error") == "server_error" or "code" in result:
            break
    server.server_close()

    if "code" not in result:
        print("FAIL 承認を完了できませんでした（%s）" % (result.get("error") or "タイムアウト"))
        sys.exit(1)

    status, body = request_json(
        "https://oauth2.googleapis.com/token",
        payload={
            "code": result["code"],
            "client_id": env["GOOGLE_CLIENT_ID"],
            "client_secret": env["GOOGLE_CLIENT_SECRET"],
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        form=True,
    )
    refresh_token = body.get("refresh_token") or ""
    if status != 200 or not refresh_token:
        print("FAIL トークン交換失敗（HTTP %s）: %s" % (status, json.dumps(body, ensure_ascii=False)[:300]))
        sys.exit(1)

    upsert_env("GOOGLE_REFRESH_TOKEN", refresh_token)
    print("OK   リフレッシュトークンを取得し .env.local に保存しました")


# ---------------------------------------------------------------- select

def cmd_select(args):
    env = load_env()
    token = get_access_token(env)
    query = "mimeType='application/vnd.google-apps.spreadsheet' and trashed=false"
    url = (
        "https://www.googleapis.com/drive/v3/files?"
        + urllib.parse.urlencode(
            {
                "q": query,
                "orderBy": "modifiedTime desc",
                "fields": "files(id,name,modifiedTime)",
                "pageSize": 50,
            }
        )
    )
    status, body = request_json(url, token=token)
    if status != 200:
        raise SystemExit("一覧取得失敗（HTTP %s）: %s" % (status, json.dumps(body, ensure_ascii=False)[:300]))
    files = body.get("files", [])
    if not files:
        raise SystemExit("スプレッドシートが見つかりませんでした")

    print("スプレッドシート一覧（更新が新しい順）:")
    for index, item in enumerate(files, start=1):
        print("  %2d. %s  (%s)" % (index, item.get("name"), item.get("id")))

    chosen = None
    if args.id:
        chosen = next((item for item in files if item.get("id") == args.id), None)
        if chosen is None:
            print("WARN 指定IDは一覧に見つかりませんでしたが、そのまま設定します")
        target_id = args.id
    elif args.pick:
        if not (1 <= args.pick <= len(files)):
            raise SystemExit("--pick は 1〜%d で指定してください" % len(files))
        chosen = files[args.pick - 1]
        target_id = chosen["id"]
    else:
        answer = input("選択する番号: ").strip()
        if not (answer.isdigit() and 1 <= int(answer) <= len(files)):
            raise SystemExit("番号が不正です")
        chosen = files[int(answer) - 1]
        target_id = chosen["id"]

    upsert_env("SPREADSHEET_ID", target_id)
    label = chosen.get("name") if chosen else "(一覧外)"
    print("OK   SPREADSHEET_ID を保存: %s（%s）" % (target_id, label))


# ---------------------------------------------------------------- init

def cmd_init(args):
    env = load_env()
    if not env.get("SPREADSHEET_ID"):
        raise SystemExit(".env.local に SPREADSHEET_ID がありません。先に select を実行してください")
    token = get_access_token(env)
    base = "https://sheets.googleapis.com/v4/spreadsheets/%s" % env["SPREADSHEET_ID"]

    status, body = request_json(base + "?fields=sheets.properties.title", token=token)
    if status != 200:
        raise SystemExit("スプレッドシート取得失敗（HTTP %s）: %s" % (status, json.dumps(body, ensure_ascii=False)[:300]))
    existing = {sheet["properties"]["title"] for sheet in body.get("sheets", [])}
    print("既存タブ:", sorted(existing))

    missing = [title for title in TAB_HEADERS if title not in existing]
    if missing:
        requests = [{"addSheet": {"properties": {"title": title}}} for title in missing]
        status, body = request_json(base + ":batchUpdate", payload={"requests": requests}, token=token)
        if status != 200:
            raise SystemExit("タブ作成失敗（HTTP %s）: %s" % (status, json.dumps(body, ensure_ascii=False)[:300]))
        print("OK   タブを作成:", missing)

    for title in TAB_HEADERS:
        rows = TEST_ROWS.get(title, [])
        values = [TAB_HEADERS[title]] + rows
        status, body = request_json(
            "%s/values/%s!A1?valueInputOption=USER_ENTERED" % (base, title),
            payload={"values": values},
            method="PUT",
            token=token,
        )
        if status != 200:
            raise SystemExit("%s への書き込み失敗（HTTP %s）: %s" % (title, status, json.dumps(body, ensure_ascii=False)[:300]))
        print("OK   %s!A1 に %d 行を書き込み（ヘッダ%d + データ%d）" % (title, len(values), 1, len(rows)))

    print("DONE テストデータの投入が完了しました")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    auth = sub.add_parser("auth", help="ブラウザ承認でリフレッシュトークンを取得")
    auth.add_argument("--timeout", type=int, default=300, help="承認待ち秒数（既定300）")
    auth.add_argument("--no-open", action="store_true", help="ブラウザを開かずURL表示のみ")

    select = sub.add_parser("select", help="スプレッドシート一覧から選択して .env.local に保存")
    select.add_argument("--pick", type=int, help="一覧の番号を指定して選択")
    select.add_argument("--id", help="スプレッドシートIDを直接指定（一覧照合あり）")

    sub.add_parser("init", help="master/campaign/history タブとテストデータを投入")

    args = parser.parse_args()
    {"auth": cmd_auth, "select": cmd_select, "init": cmd_init}[args.command](args)


if __name__ == "__main__":
    main()
