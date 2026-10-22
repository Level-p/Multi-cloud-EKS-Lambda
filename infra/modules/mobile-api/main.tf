# ---------------------------------------------------------------------------
# Mobile API: CloudFront (WAF, modules/edge) -> API Gateway REST (Cognito authorizer, throttling, logs)
#             -> Lambda alias "live" (in the VPC) -> RDS PostgreSQL (IAM token) / S3 (pre-signed POST)
#
# The pipeline deploys new code as a new Lambda version and shifts the "live" alias with a canary (10 %,
# CloudWatch check, then 100 % or rollback: .github/scripts/lambda_canary.sh), so Terraform ignores those fields.
# The Lambda needs no internet: PostgreSQL is in the VPC, and both the IAM database token and the S3
# pre-signed POST are signed locally with the execution role's credentials.
# CloudFront adds X-Origin-Verify; without it the Lambda answers 403 (except /health), so the
# execute-api URL cannot be used to skip the WAF.
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

# ENIs in the VPC (and basic logging)
resource "aws_iam_role_policy_attachment" "lambda_vpc" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

# Least privilege: its own database user, uploads/ only, its own logs, X-Ray
resource "aws_iam_role_policy" "lambda" {
  name = "mobile-api"
  role = aws_iam_role.lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["rds-db:connect"], Resource = var.db_connect_arn },
      { Effect = "Allow", Action = ["s3:PutObject"], Resource = "${var.media_bucket_arn}/uploads/*" },
      { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.lambda.arn}:*" },
      { Effect = "Allow", Action = ["xray:PutTraceSegments", "xray:PutTelemetryRecords"], Resource = "*" },
    ]
  })
}

resource "aws_lambda_function" "api" {
  #checkov:skip=CKV_AWS_116:Synchronous API calls: a dead-letter queue does not apply
  #checkov:skip=CKV_AWS_173:Environment variables hold no secrets except the origin header value (encrypted at rest by Lambda)
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
  reserved_concurrent_executions = var.reserved_concurrency # caps PostgreSQL connections (one per instance); -1 = none

  vpc_config {
    subnet_ids         = var.subnet_ids
    security_group_ids = [var.security_group_id]
  }

  environment {
    variables = {
      DB_HOST              = var.db_host
      DB_PORT              = tostring(var.db_port)
      DB_NAME              = "mobile"
      DB_USER              = var.db_user
      DB_SSLROOTCERT       = "global-bundle.pem" # RDS CA bundle, packaged next to the code by the pipeline
      MEDIA_BUCKET         = var.media_bucket
      MEDIA_CDN_DOMAIN     = var.media_cdn_domain
      ORIGIN_VERIFY_SECRET = var.origin_verify_secret
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

  depends_on = [aws_iam_role_policy_attachment.lambda_vpc]
}

resource "aws_lambda_alias" "live" {
  name             = "live"
  function_name    = aws_lambda_function.api.function_name
  function_version = aws_lambda_function.api.version

  lifecycle {
    ignore_changes = [function_version, routing_config]
  }
}

# ---------------- API Gateway (REST: Cognito authorizer, usage throttling) ----------------
resource "aws_api_gateway_rest_api" "mobile" {
  name = "${var.name}-mobile"
  endpoint_configuration {
    types = ["REGIONAL"] # CloudFront (modules/edge) is the edge
  }
}

resource "aws_api_gateway_authorizer" "cognito" {
  name            = "cognito"
  rest_api_id     = aws_api_gateway_rest_api.mobile.id
  type            = "COGNITO_USER_POOLS"
  provider_arns   = [var.user_pool_arn]
  identity_source = "method.request.header.Authorization"
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

resource "aws_api_gateway_method" "proxy" {
  rest_api_id   = aws_api_gateway_rest_api.mobile.id
  resource_id   = aws_api_gateway_resource.proxy.id
  http_method   = "ANY"
  authorization = "COGNITO_USER_POOLS"
  authorizer_id = aws_api_gateway_authorizer.cognito.id
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
    aws_api_gateway_integration.this, aws_api_gateway_authorizer.cognito]))
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
      requestId = "$context.requestId", ip = "$context.identity.sourceIp", user = "$context.authorizer.claims.sub",
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
