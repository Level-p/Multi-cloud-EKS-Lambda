# ---------------------------------------------------------------------------
# The mobile API's public name: https://api.<zone> -> API Gateway (regional custom domain, TLS 1.2) -> Lambda
# The default execute-api URL is switched off (modules/mobile-api), so this name, with the WAF on the stage, is the
# only way in.
# ---------------------------------------------------------------------------

resource "aws_api_gateway_domain_name" "api" {
  domain_name              = var.api_host
  regional_certificate_arn = aws_acm_certificate_validation.regional.certificate_arn
  security_policy          = "TLS_1_2"
  endpoint_configuration {
    types = ["REGIONAL"]
  }
}

resource "aws_api_gateway_base_path_mapping" "api" {
  api_id      = var.api_rest_api_id
  stage_name  = var.api_stage_name
  domain_name = aws_api_gateway_domain_name.api.domain_name
}
