output "output_path" {
  description = "生成した zip のパス。lambda_function モジュールの filename に渡す"
  value       = data.archive_file.this.output_path
}

output "output_base64sha256" {
  description = "zip の base64 SHA256。lambda_function モジュールの source_code_hash に渡す"
  value       = data.archive_file.this.output_base64sha256
}
