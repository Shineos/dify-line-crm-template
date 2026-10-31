"""Codeノード②「結果集計と履歴生成」の正本ロジック.

DifyのCodeノードはこのファイルの内容がそのまま実行される。
build_dsl.py が line-segment-broadcast.yml の該当ノードへ注入する。

入力（Difyのvariables）:
    planned             : Codeノード①の planned（送信対象の一覧）
    results             : Iterationの出力（失敗した要素は null）
    dry_run             : 開始ノードの入力
    plan_summary        : Codeノード①の計画サマリ
    discord_webhook_url : 環境変数 DISCORD_WEBHOOK_URL
    admin_line_id       : 環境変数 ADMIN_LINE_ID
出力:
    append_body_json / history_count / success_count / failed_count /
    report_text / line_enabled / discord_enabled / discord_url / discord_body_json
"""

import json
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))
ALLOWED_WEBHOOK_HOSTS = ("discord.com", "discordapp.com")

# Discord埋め込み（embed）の装飾
SITE_URL = "https://shineos.com/"
FOOTER_TEXT = "Power by Shineos Inc."
COLOR_DRY_RUN = 3447003   # 青
COLOR_SUCCESS = 3066993   # 緑
COLOR_PARTIAL = 15105570  # オレンジ（失敗あり）


def as_dict(value):
    """ツール出力（dict / リスト包み / JSON文字列）をdictに正規化する."""
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


def build_discord_payload(is_dry, plan_summary, planned_count, success, failed, segment_counts, failed_ids):
    """Discord Webhook用の埋め込み（embed）ペイロードを組み立てる."""
    if is_dry:
        color = COLOR_DRY_RUN
        title = "LINE配信レポート（dry-run）"
        result_value = "送信なし（計画のみ）"
        breakdown_value = "なし"
    else:
        color = COLOR_SUCCESS if failed == 0 else COLOR_PARTIAL
        title = "LINE配信レポート"
        result_value = "成功 %d / 失敗 %d" % (success, failed)
        breakdown_value = (
            " / ".join("%s %d" % (key, segment_counts[key]) for key in sorted(segment_counts)) or "なし"
        )

    fields = [
        {"name": "実行モード", "value": "ドライラン（送信なし）" if is_dry else "本番配信", "inline": True},
        {"name": "送信結果", "value": result_value, "inline": True},
        {"name": "送信対象", "value": "%d件" % planned_count, "inline": True},
    ]
    if not is_dry:
        fields.append({"name": "成功内訳", "value": breakdown_value, "inline": True})
        if failed_ids:
            preview = ", ".join(failed_ids[:10])
            if len(failed_ids) > 10:
                preview += " 他%d件" % (len(failed_ids) - 10)
            fields.append({"name": "失敗ID（次回自動リトライ）", "value": preview[:1000], "inline": False})

    # リンク行（dry-run/本番とも表示。ユーザー確定仕様）
    fields.append({"name": "\u200b", "value": "[Power by Shineos Inc.](%s)" % SITE_URL, "inline": False})

    embed = {
        "title": title,
        "color": color,
        "description": format_description(plan_summary),
        "fields": fields,
    }
    return {"embeds": [embed]}


def main(planned, results, dry_run, plan_summary, discord_webhook_url, admin_line_id="", planned_count=0):
    is_dry = str(dry_run).strip().lower() in ("true", "1", "yes", "on")
    tasks = planned if isinstance(planned, list) else []
    outputs = results if isinstance(results, list) else []
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
    line_id = safe_line_id(admin_line_id)
    discord_payload = build_discord_payload(is_dry, plan_summary, planned_count, success, failed, segment_counts, failed_ids)

    return {
        "append_body_json": json.dumps({"values": rows}, ensure_ascii=False) if rows else "",
        "history_count": len(rows),
        "success_count": success,
        "failed_count": failed,
        "report_text": report,
        "line_enabled": 1 if line_id else 0,
        "discord_enabled": 1 if webhook else 0,
        "discord_url": webhook,
        "discord_body_json": json.dumps(discord_payload, ensure_ascii=False),
    }
