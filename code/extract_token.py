"""Codeノード「トークン取り出し」の正本ロジック.

http_tokenノードのレスポンス（JSONオブジェクト）からaccess_tokenを
フラットな出力変数として取り出す。新ランタイムではHTTPノードのbodyのような
オブジェクトのネスト参照がテンプレートで解決できないため、このノードを挟む。

入力（Difyのvariables）:
    token_body : http_token ノードの body 出力（dict またはJSON文字列）
出力:
    access_token : Bearerヘッダ用のアクセストークン
"""

import json


def as_dict(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip().startswith("{"):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def main(token_body):
    body = as_dict(token_body)
    return {
        "access_token": str(body.get("access_token", "") or ""),
        "token_ok": 1 if body.get("access_token") else 0,
    }
