"""Codeノードのロジック単体テストとDSL整合性チェック.

Difyへインポートせずに、code/*.py を直接importして検証する。
DSL側の `code:` ブロックが正本ファイルと一致していることも確認する（ドリフト検出）。

実行方法:
    python3 -m pytest tests -q
    （pytestが無い場合） python3 tests/test_code_nodes.py
"""

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CODE_DIR = ROOT / "code"
DSL_PATH = ROOT / "line-segment-broadcast.yml"
JST = timezone(timedelta(hours=9))


def load_module(name):
    spec = importlib.util.spec_from_file_location(name, CODE_DIR / ("%s.py" % name))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


plan = load_module("plan_targets")
report = load_module("build_report")
extract_token = load_module("extract_token")
gsheets_plan = load_module("gsheets_plan")
gsheets_report = load_module("gsheets_report")


def _value_ranges(master, campaign, history):
    """batch_get想定の応答を作る."""
    return {
        "spreadsheetId": "test",
        "valueRanges": [
            {"range": "master!A2:F99", "values": master},
            {"range": "campaign!A2:C99", "values": campaign},
            {"range": "history!A2:F99", "values": history},
        ],
    }


def _ago(days):
    return (datetime.now(JST) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def _master():
    return {
        "values": [
            ["U001", "田中様", "vip", "7", "先月ご購入のA商品"],
            ["U002", "佐藤様", "active", "30", ""],
            ["U003", "鈴木様", "dormant", "90", "休眠"],
            ["U004", "高橋様", "unknown", "30", ""],
            ["U001", "重複行", "vip", "7", ""],
            ["X999", "不正ID", "vip", "7", ""],
        ]
    }


def _campaign():
    return {
        "values": [
            ["vip", "【先行案内】{display_name}、{context}の新作を先にお知らせします。", "TRUE"],
            ["active", "{display_name}、お得な情報です。{context}", "TRUE"],
            ["dormant", "{display_name}様、お久しぶりです。", "TRUE"],
        ]
    }


def _history():
    return {
        "values": [
            [_ago(40), "U002", "active", "success", "old", "req-1"],
            [_ago(5), "U003", "dormant", "success", "recent", "req-2"],
            [_ago(3), "U002", "active", "failed", "failed", ""],
        ]
    }


# ---------------------------------------------------------------- Codeノード①

def test_plan_selects_due_recipients():
    out = plan.main(_master(), _campaign(), _history(), "", False)

    assert out["planned_count"] == 2
    assert [p["user_id"] for p in out["planned"]] == ["U001", "U002"]
    assert len(out["send_tasks"]) == 2
    assert out["skipped_recent"] == 1          # U003: 90日間隔に対し5日前
    assert out["skipped_no_template"] == 1     # U004: unknownセグメント
    assert "送信対象 2件" in out["plan_summary"]
    assert "重複 1" in out["plan_summary"]
    assert "不正ID 1" in out["plan_summary"]


def test_plan_renders_placeholders():
    out = plan.main(_master(), _campaign(), _history(), "", False)
    vip, active = out["planned"]
    assert vip["message_text"] == "【先行案内】田中様、先月ご購入のA商品の新作を先にお知らせします。"
    assert vip["elapsed_days"] == -1
    assert active["message_text"] == "佐藤様、お得な情報です。"


def test_plan_missing_display_name_uses_default():
    master = {"values": [["U010", "", "vip", "7", ""]]}
    out = plan.main(master, _campaign(), {}, "", False)
    assert out["planned"][0]["message_text"].startswith("【先行案内】お客様、")


def test_plan_failed_history_does_not_block_retry():
    # U002の成功は40日前・失敗は3日前 → 成功のみで判定するので送信対象
    out = plan.main(_master(), _campaign(), _history(), "", False)
    assert "U002" in [p["user_id"] for p in out["planned"]]


def test_plan_dry_run_has_no_send_tasks():
    out = plan.main(_master(), _campaign(), _history(), "", True)
    assert out["send_tasks"] == []
    assert out["planned_count"] == 2
    assert "ドライラン" in out["plan_summary"]


def test_plan_segment_filter():
    out = plan.main(_master(), _campaign(), _history(), "vip", False)
    assert [p["user_id"] for p in out["planned"]] == ["U001"]
    assert out["skipped_no_template"] == 0


def test_plan_skips_disabled_template():
    master = {"values": [["U020", "様", "paused", "30", ""]]}
    campaign = {"values": [["paused", "停止中の文面", "FALSE"]]}
    out = plan.main(master, campaign, {}, "", False)
    assert out["planned_count"] == 0
    assert out["skipped_no_template"] == 1


def test_plan_empty_sheets_do_not_crash():
    out = plan.main({}, {}, {}, "", False)
    assert out["planned_count"] == 0
    assert out["send_tasks"] == []
    assert "送信対象 0件" in out["plan_summary"]


def test_plan_tolerates_json_string_bodies():
    out = plan.main(
        json.dumps(_master()), json.dumps(_campaign()), json.dumps(_history()), "", False
    )
    assert out["planned_count"] == 2


def test_plan_caps_batch_at_limit():
    master = {"values": [["U%03d" % i, "様", "vip", "1", ""] for i in range(130)]}
    out = plan.main(master, _campaign(), {}, "", False)
    assert out["planned_count"] == plan.MAX_SEND_PER_RUN
    assert out["overflow_count"] == 30


def test_plan_same_uid_different_segments_all_planned():
    # ユーザー確定仕様: 同一UIDでもセグメントが違えば1回の実行で全セグメント配信
    master = {"values": [
        ["U4755", "テイ", "vip", "7", ""],
        ["U4755", "テイ", "active", "30", ""],
        ["U4755", "テイ", "dormant", "90", ""],
    ]}
    out = plan.main(master, _campaign(), {}, "", False)
    assert out["planned_count"] == 3
    segs = [p["segment"] for p in out["planned"]]
    assert segs == ["vip", "active", "dormant"]
    texts = {p["segment"]: p["message_text"] for p in out["planned"]}
    assert texts["vip"].startswith("【先行案内】")
    assert texts["active"].startswith("テイ、お得な情報です。")
    assert texts["dormant"].startswith("テイ様、")
    # Discord側の集計は success 内訳でセグメント別に表示される


def test_gsheets_plan_same_uid_different_segments_all_planned():
    master = {"values": [
        ["U4755", "テイ", "vip", "7", ""],
        ["U4755", "テイ", "active", "30", ""],
        ["U4755", "テイ", "dormant", "90", ""],
    ]}
    out = gsheets_plan.main(_value_ranges(master["values"], _campaign()["values"], []), "", False)
    assert out["planned_count"] == 3


def test_plan_deduplicates_user_ids():
    master = {"values": [["U001", "A", "vip", "7", ""], ["U001", "B", "vip", "7", ""]]}
    out = plan.main(master, _campaign(), {}, "", False)
    assert out["planned_count"] == 1
    assert out["planned"][0]["display_name"] == "A"


# ---------------------------------------------------------------- Codeノード②

def _two_tasks():
    return [
        {"user_id": "U001", "segment": "vip", "message_text": "A"},
        {"user_id": "U002", "segment": "active", "message_text": "B"},
    ]


def test_report_counts_success_and_null_failure():
    results = [{"status": "sent", "to": "U001", "request_id": "r-1"}, None]
    out = report.main(_two_tasks(), results, False, "計画: 送信対象 2件", "", "")

    assert out["history_count"] == 2
    assert out["success_count"] == 1
    assert out["failed_count"] == 1
    assert "失敗 1" in out["report_text"]
    assert "U002" in out["report_text"]
    assert "次回自動リトライ" in out["report_text"]

    rows = json.loads(out["append_body_json"])["values"]
    assert rows[0][1:] == ["U001", "vip", "success", "A", "r-1"]
    assert rows[1][1:] == ["U002", "active", "failed", "B", ""]


def test_report_accepts_json_string_tool_output():
    results = [json.dumps({"status": "sent", "to": "U001", "request_id": "r-9"}), None]
    out = report.main(_two_tasks()[:1], results[:1], False, "", "", "")
    assert out["success_count"] == 1
    assert json.loads(out["append_body_json"])["values"][0][5] == "r-9"


def test_report_dry_run_writes_nothing():
    out = report.main(_two_tasks()[:1], [], True, "計画: 送信対象 1件", "", "")
    assert out["history_count"] == 0
    assert out["append_body_json"] == ""
    assert "dry-run" in out["report_text"]


def test_report_empty_plan():
    out = report.main([], [], False, "計画: 送信対象 0件", "", "")
    assert out["history_count"] == 0
    assert out["append_body_json"] == ""
    assert out["success_count"] == 0
    assert out["failed_count"] == 0


def test_report_accepts_list_wrapped_tool_output():
    # 実機確認: プラグインツールのjson出力は [{...}] にラップされて返る
    planned = [{"user_id": "U001", "segment": "vip", "message_text": "A"}]
    results = [[{"status": "sent", "to": "U001", "request_id": "r-list"}]]
    out = report.main(planned, results, False, "", "", "")
    assert out["success_count"] == 1
    rows = json.loads(out["append_body_json"])["values"]
    assert rows[0][3] == "success" and rows[0][5] == "r-list"


def test_discord_embed_structure():
    planned = [
        {"user_id": "U001", "segment": "vip", "message_text": "A"},
        {"user_id": "U002", "segment": "active", "message_text": "B"},
    ]
    results = [
        [{"status": "sent", "to": "U001", "request_id": "r1"}],
        None,
    ]
    summary = "計画: 送信対象 2件（vip 1 / active 1）\nスキップ: 最近送信済 0・文面未定義 0"
    out = report.main(planned, results, False, summary, "https://discord.com/api/webhooks/1/x", "Uadmin", 2)
    payload = json.loads(out["discord_body_json"])
    embed = payload["embeds"][0]
    assert embed["title"] == "LINE配信レポート"
    assert "url" not in embed  # タイトルのリンクは廃止（ユーザー指定）
    assert "footer" not in embed
    assert embed["color"] == report.COLOR_PARTIAL
    names = {field["name"]: field["value"] for field in embed["fields"]}
    assert names["実行モード"] == "本番配信"
    assert names["送信結果"] == "成功 1 / 失敗 1"
    assert names["送信対象"] == "2件"
    assert "U002" in names["失敗ID（次回自動リトライ）"]
    # タイムスタンプは表示しない（ユーザー指定）
    assert "timestamp" not in embed
    # 説明欄: 計画/スキップ(<>)/ドライラン の3行構成
    desc_lines = embed["description"].splitlines()
    assert desc_lines[0].startswith("計画: 送信対象 2件")
    assert desc_lines[1] == ""  # 1行空け
    assert any(line.startswith("<スキップ:") for line in desc_lines)
    assert any(line.startswith("【") is False or line for line in desc_lines)
    # フッター直上にクリック可能なリンク行
    assert "[Power by Shineos Inc.](https://shineos.com/)" in [field["value"] for field in embed["fields"]]

    out2 = report.main([], [], True, "計画: 送信対象 2件", "https://discord.com/api/webhooks/1/x", "", 2)
    embed2 = json.loads(out2["discord_body_json"])["embeds"][0]
    assert embed2["title"] == "LINE配信レポート（dry-run）"
    assert embed2["color"] == report.COLOR_DRY_RUN
    # タイトルのリンクはなし（ユーザー確定仕様）
    assert "url" not in embed2
    # リンク行はdry-run/本番とも表示
    assert "[Power by Shineos Inc.](https://shineos.com/)" in [field["value"] for field in embed2["fields"]]
    names2 = {field["name"]: field["value"] for field in embed2["fields"]}
    assert names2["送信結果"] == "送信なし（計画のみ）"

    out3 = report.main(planned[:1], [[{"status": "sent", "to": "U001", "request_id": "r"}]], False, "", "", "", 1)
    embed3 = json.loads(out3["discord_body_json"])["embeds"][0]
    assert embed3["color"] == report.COLOR_SUCCESS


def test_gsheets_discord_embed_structure():
    planned = [{"user_id": "U001", "segment": "vip", "message_text": "A"}]
    results = [[{"status": "sent", "to": "U001", "request_id": "r1"}]]
    out = gsheets_report.main(planned, results, False, "計画: 送信対象 1件", "https://discord.com/api/webhooks/1/x", "", 0, 1)
    embed = json.loads(out["discord_body_json"])["embeds"][0]
    assert "footer" not in embed
    assert "url" not in embed
    assert embed["color"] == gsheets_report.COLOR_SUCCESS
    names = {field["name"]: field["value"] for field in embed["fields"]}
    assert names["送信対象"] == "1件"


def test_gsheets_plan_outputs_settings_json():
    vr = _value_ranges([["U1", "a", "vip", "7", ""]], [], [])
    vr["valueRanges"].append({"range": "settings!A2:B99", "values": [["report_title", "カスタム"], ["footer_url", "https://shineos.com/"]]})
    out = gsheets_plan.main(vr, "", False)
    settings = json.loads(out["settings_json"])
    assert settings == {"report_title": "カスタム", "footer_url": "https://shineos.com/"}


def test_gsheets_report_custom_labels_from_settings():
    planned = [{"user_id": "U001", "segment": "優良", "message_text": "A"}]
    results = [[{"status": "sent", "to": "U001", "request_id": "r1"}]]
    settings_json = json.dumps({
        "report_title": "カスタム配信レポート",
        "label_result": "結果",
        "value_result": "OK {success} / NG {failed}",
        "label_count": "宛先数",
        "value_count": "計 {count} 件",
        "label_breakdown": "セグメント別",
        "footer_text": "Shineos Inc.",
        "footer_url": "https://shineos.com/",
    }, ensure_ascii=False)
    out = gsheets_report.main(planned, results, False, "計画: 送信対象 1件", "https://discord.com/api/webhooks/1/x", "", 0, 1, settings_json)
    embed = json.loads(out["discord_body_json"])["embeds"][0]
    fields = {field["name"]: field["value"] for field in embed["fields"]}
    assert embed["title"] == "カスタム配信レポート"
    assert fields["結果"] == "OK 1 / NG 0"
    assert fields["宛先数"] == "計 1 件"
    assert fields["セグメント別"] == "優良 1"


def test_gsheets_report_settings_defaults_when_missing():
    planned = [{"user_id": "U001", "segment": "vip", "message_text": "A"}]
    results = [[{"status": "sent", "to": "U001", "request_id": "r1"}]]
    out = gsheets_report.main(planned, results, False, "", "", "", 0, 1, "")
    embed = json.loads(out["discord_body_json"])["embeds"][0]
    fields = {field["name"]: field["value"] for field in embed["fields"]}
    assert embed["title"] == "LINE配信レポート"
    assert fields["送信結果"] == "成功 1 / 失敗 0"
    assert fields["送信対象"] == "1件"
    assert "footer" not in embed


def test_gsheets_report_rejects_non_https_footer_url():
    planned = [{"user_id": "U001", "segment": "vip", "message_text": "A"}]
    results = [[{"status": "sent", "to": "U001", "request_id": "r1"}]]
    settings_json = json.dumps({"footer_text": "危険", "footer_url": "http://evil.example"}, ensure_ascii=False)
    out = gsheets_report.main(planned, results, False, "", "", "", 0, 1, settings_json)
    embed = json.loads(out["discord_body_json"])["embeds"][0]
    fields = {field["name"]: field["value"] for field in embed["fields"]}
    # httpのURLは既定値（https）へフォールバックし、危険なURLは絶対に使われない
    assert "http://evil.example" not in json.dumps(embed, ensure_ascii=False)
    assert "[危険](https://shineos.com/)" in list(fields.values())


def test_gsheets_report_accepts_list_wrapped_tool_output():
    planned = [{"user_id": "U001", "segment": "vip", "message_text": "A"}]
    results = [[{"status": "sent", "to": "U001", "request_id": "r-list"}]]
    out = gsheets_report.main(planned, results, False, "", "", "", 0)
    assert out["success_count"] == 1
    payload = json.loads(out["append_body_json"])
    assert payload[0]["values"][0][3] == "success"


def test_report_missing_result_treated_as_failure():
    out = report.main(_two_tasks(), [], False, "", "", "")
    assert out["failed_count"] == 2
    assert out["success_count"] == 0


def test_discord_url_accepts_only_discord_webhooks():
    allowed = [
        "https://discord.com/api/webhooks/123/abc",
        "https://discordapp.com/api/webhooks/123/abc",
        "https://ptb.discord.com/api/webhooks/123/abc",
        "https://canary.discord.com/api/webhooks/123/abc",
    ]
    rejected = [
        "",
        "http://discord.com/api/webhooks/123/abc",
        "https://localhost/api/webhooks/123/abc",
        "https://127.0.0.1/hook",
        "https://10.0.0.5/hook",
        "https://192.168.1.10/hook",
        "https://169.254.169.254/hook",
        "https://evil.com/api/webhooks/123/abc",
        "https://discord.com.evil.com/hook",
        "https://discord.com@evil.com/hook",
        "https://user@discord.com/hook",
    ]
    for url in allowed:
        out = report.main([], [], False, "", url, "")
        assert out["discord_enabled"] == 1, url
        assert out["discord_url"] == url
        embed = json.loads(out["discord_body_json"])["embeds"][0]
        assert "[Power by Shineos Inc.](https://shineos.com/)" in [field["value"] for field in embed["fields"]]

    for url in rejected:
        out = report.main([], [], False, "", url, "")
        assert out["discord_enabled"] == 0, url
        assert out["discord_url"] == ""


# ---------------------------------------------------------------- トークン取り出し

def _dummy_token(suffix: str) -> str:
    """テスト用のダミートークン（実在しない値を実行時に組み立てる）."""
    return "-".join(["dummy", "access", suffix])


def test_extract_token_pulls_access_token():
    token = _dummy_token("one")
    out = extract_token.main({"access_token": token, "expires_in": 3599})
    assert out["access_token"] == token
    assert out["token_ok"] == 1


def test_extract_token_tolerates_string_and_empty():
    token = _dummy_token("two")
    assert extract_token.main(json.dumps({"access_token": token}))["access_token"] == token
    out = extract_token.main({})
    assert out["access_token"] == ""
    assert out["token_ok"] == 0
    out = extract_token.main(json.dumps({"error": "bad"}))
    assert out["token_ok"] == 0


# ---------------------------------------------------------------- google_sheetsプラグイン版

def _bot_id():
    return "U" + "1f31cc36a41893e7a3f5669ebe294619"[1:]


def test_plan_detects_bot_own_id():
    master = {"values": [
        [_bot_id(), "ボット", "vip", "7", ""],
        ["U001", "田中様", "vip", "7", ""],
    ]}
    out = plan.main(master, _campaign(), {}, "", False, _bot_id())
    assert out["planned_count"] == 1
    assert out["skipped_bot"] == 1
    assert "ボット自身のID 1" in out["plan_summary"]
    assert out["planned"][0]["user_id"] == "U001"


def test_plan_detects_bot_id_case_insensitive():
    master = {"values": [["U" + _bot_id()[1:].upper(), "ボット", "vip", "7", ""]]}
    out = plan.main(master, _campaign(), {}, "", False, _bot_id())
    assert out["planned_count"] == 0
    assert out["skipped_bot"] == 1


def test_gsheets_plan_detects_bot_own_id_list_wrapped():
    # Get Bot Infoのjson出力は [{...}] にラップされて返る
    bot_var = [{"userId": _bot_id(), "displayName": "bot"}]
    master = {"values": [[_bot_id(), "ボット", "vip", "7", ""]]}
    out = gsheets_plan.main(_value_ranges(master["values"], _campaign()["values"], []), "", False, bot_var)
    assert out["planned_count"] == 0
    assert out["skipped_bot"] == 1
    assert out["history_data_rows"] == 0


def test_gsheets_split_value_ranges():
    parsed = gsheets_plan.split_value_ranges(_value_ranges([["U1", "a", "vip", "7", ""]], [], []))
    assert parsed["master"][0][0] == "U1"
    assert parsed["campaign"] == []
    assert parsed["history"] == []


def test_gsheets_split_tolerates_list_wrapped_output():
    # 実機確認: プラグインの出力jsonは [{...}] にラップされて返る
    wrapped = [_value_ranges([["U1", "a", "vip", "7", ""]], [], [])]
    parsed = gsheets_plan.split_value_ranges(wrapped)
    assert parsed["master"][0][0] == "U1"


def test_gsheets_split_tolerates_string_and_missing():
    wrapped = json.dumps({"content": json.dumps(_value_ranges([["U1"]], [["vip", "x", "TRUE"]], []))})
    parsed = gsheets_plan.split_value_ranges(wrapped)
    assert parsed["master"][0][0] == "U1"
    assert parsed["campaign"][0][0] == "vip"
    assert gsheets_plan.split_value_ranges({}) == {"master": [], "campaign": [], "history": [], "settings": []}
    assert gsheets_plan.split_value_ranges({"valueRanges": "not-a-list"})["master"] == []


def test_gsheets_plan_selects_due_recipients():
    out = gsheets_plan.main(_value_ranges(_master()["values"], _campaign()["values"], _history()["values"]), "", False)
    assert out["planned_count"] == 2
    assert [p["user_id"] for p in out["planned"]] == ["U001", "U002"]
    assert out["history_data_rows"] == 3
    assert "ドライラン" not in out["plan_summary"]


def test_gsheets_plan_dry_run_and_filter():
    raw = _value_ranges(_master()["values"], _campaign()["values"], _history()["values"])
    out = gsheets_plan.main(raw, "", True)
    assert out["send_tasks"] == [] and out["planned_count"] == 2
    out = gsheets_plan.main(raw, "vip", False)
    assert [p["user_id"] for p in out["planned"]] == ["U001"]


def test_gsheets_report_appends_next_row_range():
    planned = [{"user_id": "U001", "segment": "vip", "message_text": "A"}]
    results = [{"status": "sent", "to": "U001", "request_id": "r-1"}]
    out = gsheets_report.main(planned, results, False, "", "", "Uadmin", 3)
    payload = json.loads(out["append_body_json"])
    # 既存データ3行 + ヘッダ1行 → 追記開始は5行目
    assert payload[0]["range"] == "history!A5:F5"
    assert payload[0]["values"][0][1:] == ["U001", "vip", "success", "A", "r-1"]
    assert out["line_enabled"] == 1


def test_gsheets_report_dry_run_writes_nothing():
    out = gsheets_report.main([{"user_id": "U001", "segment": "vip", "message_text": "A"}], [], True, "", "", "", 3)
    assert out["append_body_json"] == ""
    assert out["history_count"] == 0


# ---------------------------------------------------------------- 統合（①→Iteration→②）

def test_end_to_end_plan_send_report_pipeline():
    planned = plan.main(_master(), _campaign(), _history(), "", False)
    tasks = planned["send_tasks"]
    assert len(tasks) == 2

    # Iteration（continue-on-error）を模擬: 1件目の成功、2件目は異常なしで成功
    results = [
        {"status": "sent", "to": task["user_id"], "request_id": "req-%d" % index}
        for index, task in enumerate(tasks)
    ]
    out = report.main(
        planned["planned"],
        results,
        False,
        planned["plan_summary"],
        "https://discord.com/api/webhooks/123/abc",
        "Uadmin",
    )

    rows = json.loads(out["append_body_json"])["values"]
    assert [row[1] for row in rows] == [task["user_id"] for task in tasks]
    assert out["success_count"] == 2
    assert out["failed_count"] == 0
    assert out["history_count"] == 2
    assert out["discord_enabled"] == 1
    assert out["line_enabled"] == 1
    assert "送信対象 2件" in out["report_text"]


def test_line_enabled_only_for_valid_line_id():
    valid = ["U1234567890abcdef", "C1234567890abcdef", "R1234567890abcdef", " u123 "]
    invalid = ["", "   ", "X123", "12345", "admin@example.com"]
    for value in valid:
        out = report.main([], [], False, "", "", value)
        assert out["line_enabled"] == 1, value
    for value in invalid:
        out = report.main([], [], False, "", "", value)
        assert out["line_enabled"] == 0, value


def test_both_channels_can_be_disabled():
    out = report.main([], [], False, "計画: 送信対象 0件", "", "")
    assert out["line_enabled"] == 0
    assert out["discord_enabled"] == 0


def test_end_to_end_with_one_failed_send():
    planned = plan.main(_master(), _campaign(), _history(), "", False)
    tasks = planned["send_tasks"]
    results = [{"status": "sent", "to": tasks[0]["user_id"], "request_id": "ok"}, None]
    out = report.main(planned["planned"], results, False, planned["plan_summary"], "", "Uadmin")

    rows = json.loads(out["append_body_json"])["values"]
    assert [row[3] for row in rows] == ["success", "failed"]
    assert out["discord_enabled"] == 0


# ---------------------------------------------------------------- DSL整合性

def _dsl():
    with DSL_PATH.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def test_dsl_yaml_is_loadable_with_expected_graph():
    dsl = _dsl()
    assert dsl["kind"] == "app"
    assert dsl["app"]["mode"] == "workflow"

    nodes = {node["id"]: node for node in dsl["workflow"]["graph"]["nodes"]}
    edges = dsl["workflow"]["graph"]["edges"]

    assert "start" in nodes and "end" in nodes
    for edge in edges:
        assert edge["source"] in nodes, edge["source"]
        assert edge["target"] in nodes, edge["target"]

    # すべてのノードが開始から到達可能（孤立ノード検出）
    reachable = {"start"}
    changed = True
    while changed:
        changed = False
        for edge in edges:
            if edge["source"] in reachable and edge["target"] not in reachable:
                reachable.add(edge["target"])
                changed = True
        # Iteration配下のノードは親コンテナが到達可能なら到達可能（Difyはstart_node_idで接続）
        for node_id, node in nodes.items():
            parent = node.get("parentId")
            if parent and parent in reachable and node_id not in reachable:
                reachable.add(node_id)
                changed = True
    # メモ（custom-note）は実行フローに接続されないため到達性チェックから除外
    executable = {node_id for node_id, node in nodes.items() if node["data"].get("type") != "custom-note"}
    assert executable <= reachable, executable - reachable


def test_dsl_code_blocks_match_source_files():
    nodes = {node["id"]: node["data"] for node in _dsl()["workflow"]["graph"]["nodes"]}
    for node_id, filename in (
        ("code_token", "extract_token.py"),
        ("code_plan", "plan_targets.py"),
        ("code_report", "build_report.py"),
    ):
        expected = (CODE_DIR / filename).read_text(encoding="utf-8").rstrip("\n") + "\n"
        assert nodes[node_id]["code"] == expected, node_id


def test_dsl_contains_setup_notes():
    # テンプレートにはセットアップ・仕様の説明メモが4枚含まれる
    nodes = {node["id"]: node for node in _dsl()["workflow"]["graph"]["nodes"]}
    notes = [n for n in nodes.values() if n["data"].get("type") == "custom-note"]
    assert len(notes) >= 4
    # メモの表示サイズはdata.width/heightから読まれる（無いと折りたたみ表示になる）
    for note in notes:
        assert note["data"].get("width") and note["data"].get("height"), note["id"]
        # メモ本文はLexical EditorState JSON（root必須）。プレーンテキストだと空表示になる
        text = note["data"].get("text") or ""
        parsed = json.loads(text)
        assert "root" in parsed and parsed["root"].get("children"), note["id"]
        plain = "".join(
            child.get("text", "")
            for para in parsed["root"]["children"]
            for child in para.get("children", [])
        )
        assert plain.strip(), note["id"]
    joined = "".join(n["data"].get("text", "") for n in notes)
    assert "セットアップ手順" in joined
    assert "シートの役割" in joined
    assert "送信仕様" in joined
    assert "レポートと履歴" in joined


def test_dsl_declares_marketplace_dependencies():
    # プラグイン自動インストール用の依存宣言（marketplace固定・バージョン付き）
    with (ROOT / "line-segment-broadcast-gsheets.yml").open(encoding="utf-8") as handle:
        deps = yaml.safe_load(handle)["dependencies"]
    ids = [d["value"]["marketplace_plugin_unique_identifier"] for d in deps]
    assert any("omluc/google_sheets" in i for i in ids)
    assert any("shineos/line-message-sender" in i for i in ids)
    for d in deps:
        assert d["type"] == "marketplace"
        assert "@" in d["value"]["marketplace_plugin_unique_identifier"]


def test_dsl_declares_required_environment_variables():
    dsl = _dsl()
    names = {item["name"] for item in dsl["workflow"]["environment_variables"]}
    assert {
        "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET",
        "GOOGLE_REFRESH_TOKEN",
        "SPREADSHEET_ID",
        "ADMIN_LINE_ID",
        "DISCORD_WEBHOOK_URL",
    } == names


def test_dsl_iteration_collects_tool_output_with_continue_on_error():
    nodes = {node["id"]: node["data"] for node in _dsl()["workflow"]["graph"]["nodes"]}
    iteration = nodes["iter_send"]
    assert iteration["iterator_selector"] == ["code_plan", "send_tasks"]
    assert iteration["output_selector"] == ["tool_push", "json"]
    assert iteration["error_handle_mode"] == "continue-on-error"


def test_dsl_line_tool_bound_to_iteration_item():
    nodes = {node["id"]: node["data"] for node in _dsl()["workflow"]["graph"]["nodes"]}
    params = nodes["tool_push"]["tool_parameters"]
    assert params["to"] == {"type": "mixed", "value": "{{#iter_send.item.user_id#}}"}
    assert params["message_text"] == {"type": "mixed", "value": "{{#iter_send.item.message_text#}}"}
    # 新ランタイムでは tool_node_version: '2' が必須（無いとレガシーパスでクレデンシャルが渡らない）
    assert nodes["tool_push"].get("tool_node_version") == "2"
    assert nodes["tool_admin"].get("tool_node_version") == "2"


if __name__ == "__main__":
    failures = 0
    for name in sorted(globals()):
        fn = globals()[name]
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS %s" % name)
            except AssertionError as exc:
                failures += 1
                print("FAIL %s: %s" % (name, exc))
    sys.exit(1 if failures else 0)
