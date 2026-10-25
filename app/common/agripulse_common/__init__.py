"""Code shared by the AgriPulse microservices (accounts, farm, shop, incidents), copied into each image.

  auth       one account for every service: RS256 tokens signed by the accounts service, verified everywhere
  web        the FastAPI app every service starts from: security headers, CSRF, health, metrics, sign-in redirects
  db         PostgreSQL with IAM database authentication (RDS), SQLite in tests and scans
  cache      Redis cache-aside (Valkey in the cluster)
  services   calls between services (public API with the user's token, internal API with the service token)
  slack      posting a report to the operations Slack channel
  telemetry  OpenTelemetry traces and logs to the collector in the cluster
"""
