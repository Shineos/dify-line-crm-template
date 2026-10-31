"""Codeノード「結果集計と履歴行生成」（google_sheetsプラグイン版）の正本ロジック.

入力（Difyのvariables）:
    planned             : gsheets_plan の planned（送信対象の一覧）
    results             : Iterationの出力（失敗した要素は null）
    dry_run             : 開始ノードの入力
    plan_summary        : gsheets_plan の計画サマリ
    discord_webhook_url : 環境変数 DISCORD_WEBHOOK_URL
    admin_line_id       : 環境変数 ADMIN_LINE_ID
    history_data_rows   : gsheets_plan の history_data_rows（既存の履歴データ行数）
出力:
    append_body_json    : batch_update 用のdata引数（[{"range": ..., "values": ...}]）
    history_count / success_count / failed_count / report_text /
    line_enabled / discord_enabled / discord_url / discord_body_json
"""

import json
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))
ALLOWED_WEBHOOK_HOSTS = ("discord.com", "discordapp.com")
HISTORY_TAB = "history"
HISTORY_COLUMNS = 6  # A〜F

# Discord埋め込み（embed）の装飾
SITE_URL = "https://shineos.com/"

# Discordカード文言の既定値（settingsタブで上書き可能）
DEFAULT_SETTINGS = {
    "report_title": "LINE配信レポート",
    "report_title_dry": "LINE配信レポート（dry-run）",
    "label_mode": "実行モード",
    "value_mode_real": "本番配信",
    "value_mode_dry": "ドライラン（送信なし）",
    "label_result": "送信結果",
    "value_result": "成功 {success} / 失敗 {failed}",
    "value_result_dry": "送信なし（計画のみ）",
    "label_count": "送信対象",
    "value_count": "{count}件",
    "label_breakdown": "成功内訳",
    "label_failed_ids": "失敗ID（次回自動リトライ）",
    "footer_text": "Power by Shineos Inc.",
    "footer_url": "https://shineos.com/",
}


def get_setting(settings, key):
    """settingsタブの値を取り出す（未設定・https以外のURLは既定値へフォールバック）."""
    value = str(settings.get(key, "") or "").strip()
    if not value:
        return DEFAULT_SETTINGS[key]
    if key == "footer_url" and not value.lower().startswith("https://"):
        return DEFAULT_SETTINGS[key]
    return value
COLOR_DRY_RUN = 3447003   # 青
COLOR_SUCCESS = 3066993   # 緑
COLOR_PARTIAL = 15105570  # オレンジ（失敗あり）


def as_dict(value):
    """ツール出力をdictへ正規化する（リスト包み・JSON文字列にも耐える）.

    実機確認: プラグインのjson出力は create_json_message 1件でも [{...}] に
    ラップされて返るため、list の場合は先頭要素を取り出す。
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return value[0]
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                return as_dict(json.loads(stripped))
            except Exception:
                return {}
    return {}


def safe_webhook_url(raw):
    """Discord Webhookのみ許可する（https限定・ホスト検証でSSRFを防ぐ）."""
    url = (raw or "").strip()
    if not url.lower().startswith("https://"):
        return ""
    host = url[len("https://"):].split("/", 1)[0].split("?", 1)[0].lower()
    if "@" in host:
        return ""
    host = host.split(":", 1)[0]
    for allowed in ALLOWED_WEBHOOK_HOSTS:
        if host == allowed or host.endswith("." + allowed):
            return url
    return ""


def safe_line_id(raw):
    """LINE宛先ID（U/C/R始まり）のみ許可する。空や不正値は無効扱い."""
    value = (raw or "").strip()
    if value and value[:1].upper() in ("U", "C", "R"):
        return value
    return ""


def format_description(plan_summary):
    """plan_summaryの各行をDiscord向けに整形する（スキップ行は<>で囲む）."""
    lines = []
    for line in (plan_summary or "").splitlines():
        if line.startswith("スキップ:"):
            line = "<" + line + ">"
        lines.append(line)
    return "\n\n".join(lines)[:2000]


def build_discord_payload(is_dry, plan_summary, planned_count, success, failed, segment_counts, failed_ids, settings):
    """Discord Webhook用の埋め込み（embed）ペイロードを組み立てる.

    文言ラベルはsettingsタブ（key/value）で上書き可能。未設定のキーは既定値を使用。
    """
    if is_dry:
        color = COLOR_DRY_RUN
        title = get_setting(settings, "report_title_dry")
        result_value = get_setting(settings, "value_result_dry")
        breakdown_value = "なし"
    else:
        color = COLOR_SUCCESS if failed == 0 else COLOR_PARTIAL
        title = get_setting(settings, "report_title")
        result_fmt = get_setting(settings, "value_result")
        try:
            result_value = result_fmt.format(success=success, failed=failed)
        except (KeyError, IndexError, ValueError):
            result_value = DEFAULT_SETTINGS["value_result"].format(success=success, failed=failed)
        breakdown_value = (
            " / ".join("%s %d" % (key, segment_counts[key]) for key in sorted(segment_counts)) or "なし"
        )

    count_fmt = get_setting(settings, "value_count")
    try:
        count_value = count_fmt.format(count=planned_count)
    except (KeyError, IndexError, ValueError):
        count_value = DEFAULT_SETTINGS["value_count"].format(count=planned_count)

    fields = [
        {"name": get_setting(settings, "label_mode"), "value": get_setting(settings, "value_mode_dry") if is_dry else get_setting(settings, "value_mode_real"), "inline": True},
        {"name": get_setting(settings, "label_result"), "value": result_value, "inline": True},
        {"name": get_setting(settings, "label_count"), "value": count_value, "inline": True},
    ]
    if not is_dry:
        fields.append({"name": get_setting(settings, "label_breakdown"), "value": breakdown_value, "inline": True})
        if failed_ids:
            preview = ", ".join(failed_ids[:10])
            if len(failed_ids) > 10:
                preview += " 他%d件" % (len(failed_ids) - 10)
            fields.append({"name": get_setting(settings, "label_failed_ids"), "value": preview[:1000], "inline": False})

    # リンク行（dry-run/本番とも表示。ユーザー確定仕様）
    link_text = get_setting(settings, "footer_text")
    link_url = get_setting(settings, "footer_url")
    fields.append({"name": "\u200b", "value": "[%s](%s)" % (link_text, link_url), "inline": False})

    embed = {
        "title": title,
        "color": color,
        "description": format_description(plan_summary),
        "fields": fields,
    }
    return {"embeds": [embed]}


def main(planned, results, dry_run, plan_summary, discord_webhook_url, admin_line_id="", history_data_rows=0, planned_count=0, settings_json=""):
    is_dry = str(dry_run).strip().lower() in ("true", "1", "yes", "on")
    tasks = planned if isinstance(planned, list) else []
    outputs = results if isinstance(results, list) else []
    try:
        data_rows = int(history_data_rows)
    except (TypeError, ValueError):
        data_rows = 0
    if data_rows < 0:
        data_rows = 0
    now_label = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")

    rows = []
    success = 0
    failed = 0
    failed_ids = []
    segment_counts = {}

    if not is_dry:
        for index, task in enumerate(tasks):
            task = task if isinstance(task, dict) else {}
            user_id = str(task.get("user_id", "") or "")
            segment = str(task.get("segment", "") or "")
            text = str(task.get("message_text", "") or "")

            # Iterationは失敗時にnullを返す（continue-on-error）
            payload = as_dict(outputs[index]) if index < len(outputs) else {}
            if str(payload.get("status", "")).lower() == "sent":
                success += 1
                segment_counts[segment] = segment_counts.get(segment, 0) + 1
                rows.append(
                    [now_label, user_id, segment, "success", text, str(payload.get("request_id", "") or "")]
                )
            else:
                failed += 1
                failed_ids.append(user_id)
                rows.append([now_label, user_id, segment, "failed", text, ""])

    if is_dry:
        report = (
            "【LINE配信 dry-run】" + now_label + "\n"
            + (plan_summary or "") + "\n送信は行っていません。"
        )
    else:
        breakdown = " / ".join("%s %d" % (key, segment_counts[key]) for key in sorted(segment_counts)) or "なし"
        report = (
            "【LINE配信レポート】" + now_label + "\n"
            + (plan_summary or "") + "\n"
            + "結果: 成功 %d / 失敗 %d\n" % (success, failed)
            + "成功内訳: " + breakdown
        )
        if failed_ids:
            preview = ", ".join(failed_ids[:5])
            more = " 他%d件" % (len(failed_ids) - 5) if len(failed_ids) > 5 else ""
            report += "\n失敗ID: " + preview + more + "（次回自動リトライ）"

    webhook = safe_webhook_url(discord_webhook_url)
    settings = as_dict(settings_json)
    discord_payload = build_discord_payload(is_dry, plan_summary, planned_count, success, failed, segment_counts, failed_ids, settings)

    # batch_update用: 履歴はヘッダ(1行) + 既存データ行の次の行から追記する
    append_payload = []
    if rows:
        start_row = data_rows + 2  # 1行目=ヘッダ
        end_row = start_row + len(rows) - 1
        append_payload = [
            {
                "range": "%s!A%d:%s%d" % (HISTORY_TAB, start_row, chr(ord("A") + HISTORY_COLUMNS - 1), end_row),
                "values": rows,
            }
        ]

    return {
        "append_body_json": json.dumps(append_payload, ensure_ascii=False) if append_payload else "",
        "history_count": len(rows),
        "success_count": success,
        "failed_count": failed,
        "report_text": report,
        "line_enabled": 1 if safe_line_id(admin_line_id) else 0,
        "discord_enabled": 1 if webhook else 0,
        "discord_url": webhook,
        "discord_body_json": json.dumps(discord_payload, ensure_ascii=False),
    }
