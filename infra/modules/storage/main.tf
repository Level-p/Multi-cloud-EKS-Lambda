# ---------------------------------------------------------------------------
# Media storage: the S3 bucket of the whole project
#   uploads/   farmers' photos (pre-signed POST from the mobile API Lambda), scanned by
#              GuardDuty Malware Protection
#   catalog/   crop guides and app images published by the team
#   CloudFront (modules/edge) serves catalog/ and ONLY the uploads GuardDuty tagged NO_THREATS_FOUND;
#   the bucket policy that says so is in modules/edge too, because it names the distribution
# ---------------------------------------------------------------------------

data "aws_caller_identity" "current" {}

resource "aws_s3_bucket" "media" {
  bucket        = "${var.name}-media-${data.aws_caller_identity.current.account_id}"
  force_destroy = !var.deletion_protection
}

resource "aws_s3_bucket_public_access_block" "media" {
  bucket                  = aws_s3_bucket.media.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "media" {
  bucket = aws_s3_bucket.media.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "media" {
  bucket = aws_s3_bucket.media.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "media" {
  bucket = aws_s3_bucket.media.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "media" {
  bucket = aws_s3_bucket.media.id
  rule {
    id     = "tidy"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }
  depends_on = [aws_s3_bucket_versioning.media]
}

# Browsers (web clients) may upload with the pre-signed POST; reads go through CloudFront
resource "aws_s3_bucket_cors_configuration" "media" {
  bucket = aws_s3_bucket.media.id
  cors_rule {
    allowed_methods = ["POST"]
    allowed_origins = var.allowed_origins
    allowed_headers = ["*"]
    max_age_seconds = 300
  }
}

# ---- GuardDuty Malware Protection for S3: every new upload is scanned and tagged ----
resource "aws_iam_role" "malware_scan" {
  name = "${var.name}-guardduty-malware-scan"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "malware-protection-plan.guardduty.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "malware_scan" {
  name = "scan-media-uploads"
  role = aws_iam_role.malware_scan.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "ManageEventBridgeRule"
        Effect    = "Allow"
        Action    = ["events:PutRule", "events:DeleteRule", "events:PutTargets", "events:RemoveTargets"]
        Resource  = "arn:aws:events:${var.region}:${data.aws_caller_identity.current.account_id}:rule/DO-NOT-DELETE-AmazonGuardDutyMalwareProtectionS3*"
        Condition = { StringLike = { "events:ManagedBy" = "malware-protection-plan.guardduty.amazonaws.com" } }
      },
      {
        # Read-only calls carry no ManagedBy key, so they cannot share the condition above
        Sid      = "MonitorEventBridgeRule"
        Effect   = "Allow"
        Action   = ["events:DescribeRule", "events:ListTargetsByRule"]
        Resource = "arn:aws:events:${var.region}:${data.aws_caller_identity.current.account_id}:rule/DO-NOT-DELETE-AmazonGuardDutyMalwareProtectionS3*"
      },
      {
        # GuardDuty validates that the bucket is in this account before it creates the plan
        Sid      = "CheckBucketOwnership"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = aws_s3_bucket.media.arn
      },
      {
        Sid      = "BucketNotifications"
        Effect   = "Allow"
        Action   = ["s3:PutBucketNotification", "s3:GetBucketNotification"]
        Resource = aws_s3_bucket.media.arn
      },
      {
        Sid      = "ValidationObject"
        Effect   = "Allow"
        Action   = ["s3:PutObject"]
        Resource = "${aws_s3_bucket.media.arn}/malware-protection-resource-validation-object"
      },
      {
        Sid      = "ScanAndTag"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:GetObjectVersion", "s3:GetObjectTagging", "s3:GetObjectVersionTagging", "s3:PutObjectTagging", "s3:PutObjectVersionTagging"]
        Resource = "${aws_s3_bucket.media.arn}/*"
      },
    ]
  })
}

resource "aws_guardduty_malware_protection_plan" "media" {
  role = aws_iam_role.malware_scan.arn

  protected_resource {
    s3_bucket {
      bucket_name     = aws_s3_bucket.media.id
      object_prefixes = ["uploads/"]
    }
  }

  actions {
    tagging {
      status = "ENABLED" # GuardDutyMalwareScanStatus = NO_THREATS_FOUND | THREATS_FOUND | ...
    }
  }

  depends_on = [aws_iam_role_policy.malware_scan]
}
