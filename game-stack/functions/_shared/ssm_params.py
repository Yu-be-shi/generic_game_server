"""
ssm_params.py - SSM パラメータ get/put ヘルパー（全 Lambda 共有・単一ソース）

control-plane/main.tf は archive_file の dynamic "source" でこのファイルを
そのまま discord_control Lambda の zip に取り込む（コピーは存在しない）。
"""

import logging

logger = logging.getLogger()


def ssm_get_parameter(ssm_client, name: str):
    """
    SSM の Parameter dict をそのまま返す（Value, LastModifiedDate 等を含む）。
    ParameterNotFound の場合は None を返す。他の例外は呼び出し元に伝播する。
    """
    try:
        return ssm_client.get_parameter(Name=name)["Parameter"]
    except ssm_client.exceptions.ParameterNotFound:
        return None


def ssm_get(ssm_client, name: str, default=None):
    """SSM パラメータの値だけを返す簡易版。ParameterNotFound → default。"""
    param = ssm_get_parameter(ssm_client, name)
    return param["Value"] if param else default


def ssm_put(ssm_client, name: str, value: str) -> None:
    """SSM パラメータを String 型で上書き保存する（Overwrite=True 固定）。"""
    ssm_client.put_parameter(Name=name, Value=value, Type="String", Overwrite=True)


def ssm_put_safe(ssm_client, name: str, value: str) -> bool:
    """
    SSM パラメータを上書き保存する。失敗しても例外を送出せず継続する（ログに警告）。
    呼び出し元が権限不足等で落ちたくない箇所（起動時のベストエフォート書き込み等）向け。

    Returns:
        True  書き込み成功
        False 書き込み失敗（warning ログを出力済み）
    """
    try:
        ssm_put(ssm_client, name, value)
        return True
    except Exception:
        logger.warning("SSM put_parameter 失敗: name=%s value=%s", name, value, exc_info=True)
        return False
