# ============================================================
# modules/lambda_package - Lambda デプロイパッケージ（zip）の生成
# ============================================================
# 「ハンドラディレクトリの .py + _shared/ の共有モジュールを zip 直下に同梱」
# という archive_file の定型パターンを共通化する。
# modules/lambda_function には畳まない（同 main.tf の設計判断どおり
# archive_file はデータソースのため呼び出し元管轄とし、zip 生成の定型のみ
# をこのモジュールに切り出す）。
# 子モジュール内の path.module はこのディレクトリを指すため、
# パスはすべて呼び出し元が自身の path.module 起点で渡す。

data "archive_file" "this" {
  type        = "zip"
  output_path = var.output_path

  # ハンドラ本体
  dynamic "source" {
    for_each = fileset(var.source_dir, var.source_pattern)
    content {
      content  = file("${var.source_dir}/${source.value}")
      filename = source.value
    }
  }

  # 共有モジュール（zip 直下に同梱し、ハンドラから直接 import できるようにする）
  dynamic "source" {
    for_each = toset(var.shared_files)
    content {
      content  = file("${var.shared_dir}/${source.value}")
      filename = source.value
    }
  }
}
