"""
ssm_params.py - SSM パラメータ get/put ヘルパー（control-plane 用）

game-stack/functions/_shared/ssm_params.py に同一内容の複製がある
（Terraform root module が別のため import 不可。修正時は両方を同期させること）。
"""


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
