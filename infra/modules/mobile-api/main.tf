# ---------------------------------------------------------------------------
# Mobile API: https://api.<zone> (API Gateway custom domain + WAF, modules/edge) -> API Gateway REST (throttling,
#             access logs) -> Lambda alias "live" -> the production services (accounts, farm, shop) at their public
#             URLs, through the WAF like any client, with the user's own token
#
# The Lambda owns no data and checks no passwords: it is the mobile app's gateway to the same services the web
# uses, so every rule lives in one place. No VPC, no database: nothing to connect to but HTTPS.
# The pipeline deploys new code as a new Lambda version and shifts the "live" alias with a canary (10 %,
# CloudWatch check, then 100 % or rollback: .github/scripts/lambda_canary.sh), so Terraform ignores those fields.
# The default execute-api URL is switched off: api.<zone>, where the WAF is, is the only way in.
# ---------------------------------------------------------------------------

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}
data "aws_partition" "current" {}

# ---------------- Lambda ----------------
data "archive_file" "bootstrap" {
  type        = "zip"
  source_file = "${path.module}/bootstrap-handler/handler.py"
  output_path = "${path.root}/.build/mobile-bootstrap.zip"
}

resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${var.name}-mobile-api"
  retention_in_days = 30
}

resource "aws_iam_role" "lambda" {
  name = "${var.name}-mobile-api"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

# Least privilege: its own logs and X-Ray, nothing else (the services do the work)
resource "aws_iam_role_policy" "lambda" {
  name = "mobile-api"
  role = aws_iam_role.lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.lambda.arn}:*" },
      { Effect = "Allow", Action = ["xray:PutTraceSegments", "xray:PutTelemetryRecords"], Resource = "*" },
    ]
  })
}

resource "aws_lambda_function" "api" {
  #checkov:skip=CKV_AWS_116:Synchronous API calls: a dead-letter queue does not apply
  #checkov:skip=CKV_AWS_173:Environment variables hold no secrets except the X-Mobile-Gateway value (encrypted at rest by Lambda)
  #checkov:skip=CKV_AWS_117:No VPC on purpose: the function only calls the public service endpoints (through the WAF)
  function_name                  = "${var.name}-mobile-api"
  role                           = aws_iam_role.lambda.arn
  runtime                        = "python3.12"
  architectures                  = ["x86_64"]
  handler                        = "handler.handler" # the pipeline's package uses agripulse_mobile.handler.handler
  filename                       = data.archive_file.bootstrap.output_path
  source_code_hash               = data.archive_file.bootstrap.output_base64sha256
  memory_size                    = 512
  timeout                        = 10
  publish                        = true
  reserved_concurrent_executions = var.reserved_concurrency # -1 = none (new accounts' quota is 10, all unreserved)

  environment {
    variables = {
      ACCOUNTS_URL          = var.service_urls["accounts"]
      FARM_URL              = var.service_urls["farm"]
      SHOP_URL              = var.service_urls["shop"]
      UPSTREAM_TIMEOUT      = "8"
      MOBILE_GATEWAY_SECRET = var.mobile_gateway_secret
    }
  }

  tracing_config {
    mode = "Active"
  }

  logging_config {
    log_format = "JSON"
    log_group  = aws_cloudwatch_log_group.lambda.name
  }

  # Code, handler and published versions are owned by the mobile-backend pipeline
  lifecycle {
    ignore_changes = [filename, source_code_hash, handler]
  }
}

resource "aws_lambda_alias" "live" {
  name             = "live"
  function_name    = aws_lambda_function.api.function_name
  function_version = aws_lambda_function.api.version

  lifecycle {
    ignore_changes = [function_version, routing_config]
  }
}

# ---------------- API Gateway (REST: throttling, access logs; the services check the tokens) ----------------
resource "aws_api_gateway_rest_api" "mobile" {
  name = "${var.name}-mobile"
  endpoint_configuration {
    types = ["REGIONAL"] # the custom domain api.<zone> and the WAF are in modules/edge
  }
  disable_execute_api_endpoint = true # no way around the WAF
}

resource "aws_api_gateway_resource" "health" {
  rest_api_id = aws_api_gateway_rest_api.mobile.id
  parent_id   = aws_api_gateway_rest_api.mobile.root_resource_id
  path_part   = "health"
}

resource "aws_api_gateway_resource" "proxy" {
  rest_api_id = aws_api_gateway_rest_api.mobile.id
  parent_id   = aws_api_gateway_rest_api.mobile.root_resource_id
  path_part   = "{proxy+}"
}

resource "aws_api_gateway_method" "health" {
  rest_api_id   = aws_api_gateway_rest_api.mobile.id
  resource_id   = aws_api_gateway_resource.health.id
  http_method   = "GET"
  authorization = "NONE"
}

# Sign-in and the catalog are public; every other route needs the user's token, which the service checks
resource "aws_api_gateway_method" "proxy" {
  #checkov:skip=CKV_AWS_59:Authorization is the services' (signed tokens); public routes are sign-in and the catalog
  rest_api_id   = aws_api_gateway_rest_api.mobile.id
  resource_id   = aws_api_gateway_resource.proxy.id
  http_method   = "ANY"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "this" {
  for_each = {
    health = { resource = aws_api_gateway_resource.health.id, method = aws_api_gateway_method.health.http_method }
    proxy  = { resource = aws_api_gateway_resource.proxy.id, method = aws_api_gateway_method.proxy.http_method }
  }
  rest_api_id             = aws_api_gateway_rest_api.mobile.id
  resource_id             = each.value.resource
  http_method             = each.value.method
  type                    = "AWS_PROXY"
  integration_http_method = "POST"
  uri                     = aws_lambda_alias.live.invoke_arn
}

resource "aws_lambda_permission" "apigw" {
  statement_id  = "AllowApiGatewayInvokeLive"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api.function_name
  qualifier     = aws_lambda_alias.live.name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.mobile.execution_arn}/*/*"
}

resource "aws_api_gateway_deployment" "this" {
  rest_api_id = aws_api_gateway_rest_api.mobile.id
  triggers = {
    redeploy = sha1(jsonencode([aws_api_gateway_resource.proxy, aws_api_gateway_method.proxy, aws_api_gateway_method.health,
    aws_api_gateway_integration.this, aws_api_gateway_rest_api.mobile.disable_execute_api_endpoint]))
  }
  lifecycle {
    create_before_destroy = true
  }
}

# API Gateway needs one account-wide role to write access logs to CloudWatch
resource "aws_iam_role" "apigw_logs" {
  name = "${var.name}-apigateway-cloudwatch"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "apigateway.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy_attachment" "apigw_logs" {
  role       = aws_iam_role.apigw_logs.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AmazonAPIGatewayPushToCloudWatchLogs"
}

resource "aws_api_gateway_account" "this" {
  cloudwatch_role_arn = aws_iam_role.apigw_logs.arn
}

resource "aws_cloudwatch_log_group" "api_access" {
  name              = "/aws/apigateway/${var.name}-mobile"
  retention_in_days = 30
}

resource "aws_api_gateway_stage" "prod" {
  rest_api_id          = aws_api_gateway_rest_api.mobile.id
  deployment_id        = aws_api_gateway_deployment.this.id
  stage_name           = "prod"
  xray_tracing_enabled = true

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api_access.arn
    format = jsonencode({
      requestId = "$context.requestId", ip = "$context.identity.sourceIp",
      method    = "$context.httpMethod", path = "$context.resourcePath", status = "$context.status",
      latencyMs = "$context.responseLatency", error = "$context.error.message"
    })
  }

  depends_on = [aws_api_gateway_account.this]
}

resource "aws_api_gateway_method_settings" "all" {
  rest_api_id = aws_api_gateway_rest_api.mobile.id
  stage_name  = aws_api_gateway_stage.prod.stage_name
  method_path = "*/*"
  settings {
    metrics_enabled        = true
    logging_level          = "ERROR"
    data_trace_enabled     = false # never log request/response bodies (personal data)
    throttling_rate_limit  = 50
    throttling_burst_limit = 100
  }
}
