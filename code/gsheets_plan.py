"""Codeノード「対象者抽出と文面生成」（google_sheetsプラグイン版）の正本ロジック.

入力（Difyのvariables）:
    sheets_raw     : google_sheets バッチ_get ツールの出力（JSON）
    segment_filter : 開始ノードの入力（空なら全セグメント）
    dry_run        : 開始ノードの入力（Trueなら送信タスクを空にする）
出力:
    planned / send_tasks / planned_count / overflow_count /
    skipped_recent / skipped_no_template / history_data_rows / plan_summary

batch_get の応答形式（Sheets API準拠を想定し、文字列ラップにも耐える）:
    {"spreadsheetId": "...", "valueRanges": [
        {"range": "master!A2:F6",   "values": [[...], ...]},
        {"range": "campaign!A2:C5", "values": [[...], ...]},
        {"range": "history!A2:F99", "values": [[...], ...]},
        {"range": "settings!A2:B99", "values": [[key, value], ...]},  # 任意タブ
    ]}
"""

import json
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))
MAX_SEND_PER_RUN = 100
DEFAULT_FREQ_DAYS = 30
DEFAULT_NAME = "お客様"


def as_dict(value):
    """ツール出力をdictへ正規化する（リスト包み・JSON文字列にも耐える）."""
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


def cell(row, index):
    if not isinstance(row, list) or index >= len(row):
        return ""
    value = row[index]
    return "" if value is None else str(value).strip()


def split_value_ranges(sheets_raw):
    """batch_get応答をタブ名ごとの行配列に分解する（範囲名のパターンに耐える）."""
    payload = as_dict(sheets_raw)

    # プラグインがJSON文字列でラップして返す場合に備えて一段復号
    for key in ("content", "result", "text", "data"):
        inner = payload.get(key)
        if isinstance(inner, str) and inner.strip().startswith("{"):
            parsed = as_dict(inner)
            if parsed:
                payload = parsed
            break

    result = {"master": [], "campaign": [], "history": [], "settings": []}
    ranges = payload.get("valueRanges")
    if not isinstance(ranges, list):
        return result

    for item in ranges:
        if not isinstance(item, dict):
            continue
        values = item.get("values")
        values = values if isinstance(values, list) else []
        tab = str(item.get("range", "")).split("!")[0].strip("'").strip()
        if tab in result:
            result[tab] = values
    return result


def extract_bot_user_id(value):
    """Get Bot Info出力（リスト包み/JSON文字列にも対応）からボット自身のuserIdを取り出す."""
    if isinstance(value, list) and value and isinstance(value[0], dict):
        value = value[0]
    if isinstance(value, dict):
        return str(value.get("userId", "") or "").strip()
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("{") or text.startswith("["):
            try:
                return extract_bot_user_id(json.loads(text))
            except Exception:
                return ""
        return text
    return ""


def parse_dt(text):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime((text or "").strip(), fmt)
        except ValueError:
            pass
    return None


def freq_days(row):
    try:
        days = int(float(cell(row, 3)))
    except (TypeError, ValueError):
        return DEFAULT_FREQ_DAYS
    return days if days > 0 else DEFAULT_FREQ_DAYS


def is_enabled(row):
    return cell(row, 2).lower() not in ("false", "0", "no", "off", "無効")


def main(sheets_raw, segment_filter, dry_run, bot_user_id=""):
    is_dry = str(dry_run).strip().lower() in ("true", "1", "yes", "on")
    parsed = split_value_ranges(sheets_raw)
    master_rows = parsed["master"]
    campaign_rows = parsed["campaign"]
    history_rows = parsed["history"]

    # settings: Discordカードの文言（key/valueペア。無くても既定値で動く）
    settings = {}
    for row in parsed["settings"]:
        key = cell(row, 0)
        if key:
            settings[key] = cell(row, 1)
    settings_json = json.dumps(settings, ensure_ascii=False)

    now = datetime.now(JST).replace(tzinfo=None)

    # segment -> 文面テンプレート（有効な最初の行を採用）
    templates = {}
    for row in campaign_rows:
        segment = cell(row, 0)
        template = cell(row, 1)
        if segment and template and is_enabled(row) and segment not in templates:
            templates[segment] = template

    # user_id -> 最終成功送信日時（失敗は数えない＝翌回リトライ）
    last_sent = {}
    for row in history_rows:
        if cell(row, 3).lower() != "success":
            continue
        user_id = cell(row, 1)
        sent_at = parse_dt(cell(row, 0))
        if not user_id or sent_at is None:
            continue
        if user_id not in last_sent or sent_at > last_sent[user_id]:
            last_sent[user_id] = sent_at

    wanted = (segment_filter or "").strip()
    bot_id = extract_bot_user_id(bot_user_id)

    planned = []
    seen = set()
    skipped_recent = 0
    skipped_no_template = 0
    skipped_duplicate = 0
    skipped_filter = 0
    skipped_bad_id = 0
    skipped_bot = 0

    for row in master_rows:
        user_id = cell(row, 0)
        if not user_id:
            continue
        if bot_id and user_id.lower() == bot_id.lower():
            # 宛先がボット自身（Get Bot InfoのuserId等）→ LINEが送信拒否するため除外
            skipped_bot += 1
            continue

        segment = cell(row, 2)
        if wanted and segment != wanted:
            skipped_filter += 1
            continue
        dedup_key = (user_id, segment)
        if dedup_key in seen:
            # 同一実行内の「同一UID×同一セグメント」のみ重複とみなす
            # （UIDが同じでもsegmentが違えば別文面を届ける。ユーザー確定仕様）
            skipped_duplicate += 1
            continue
        seen.add(dedup_key)
        if user_id[:1].upper() not in ("U", "C", "R"):
            skipped_bad_id += 1
            continue

        template = templates.get(segment)
        if not template:
            skipped_no_template += 1
            continue

        days = freq_days(row)
        last = last_sent.get(user_id)
        elapsed_days = -1
        if last is not None:
            elapsed_seconds = (now - last).total_seconds()
            if elapsed_seconds < days * 86400:
                skipped_recent += 1
                continue
            elapsed_days = int(elapsed_seconds // 86400)

        name = cell(row, 1) or DEFAULT_NAME
        text = template.replace("{display_name}", name)
        text = text.replace("{context}", cell(row, 4))

        planned.append(
            {
                "user_id": user_id,
                "segment": segment,
                "display_name": name,
                "message_text": text,
                "elapsed_days": elapsed_days,
            }
        )

    planned.sort(key=lambda item: (item["elapsed_days"], item["user_id"]))
    overflow = len(planned) - MAX_SEND_PER_RUN
    if overflow < 0:
        overflow = 0
    planned = planned[:MAX_SEND_PER_RUN]

    counts = {}
    for item in planned:
        counts[item["segment"]] = counts.get(item["segment"], 0) + 1
    breakdown = " / ".join("%s %d" % (key, counts[key]) for key in sorted(counts)) or "なし"

    parts = ["計画: 送信対象 %d件（%s）" % (len(planned), breakdown)]
    if overflow:
        parts.append("上限超過で翌回へ繰越 %d件" % overflow)
    skips = ["最近送信済 %d" % skipped_recent, "文面未定義 %d" % skipped_no_template]
    if skipped_duplicate:
        skips.append("重複 %d" % skipped_duplicate)
    if skipped_filter:
        skips.append("セグメント外 %d" % skipped_filter)
    if skipped_bad_id:
        skips.append("不正ID %d" % skipped_bad_id)
    if skipped_bot:
        skips.append("ボット自身のID %d" % skipped_bot)
    parts.append("スキップ: " + "・".join(skips))
    if is_dry:
        parts.append("【ドライラン: 送信しません】")

    return {
        "planned": planned,
        "send_tasks": [] if is_dry else list(planned),
        "planned_count": len(planned),
        "overflow_count": overflow,
        "skipped_recent": skipped_recent,
        "skipped_no_template": skipped_no_template,
        "skipped_bot": skipped_bot,
        "history_data_rows": len(history_rows),
        "settings_json": settings_json,
        "plan_summary": "\n".join(parts),
    }
