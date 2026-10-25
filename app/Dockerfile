# One image recipe for every AgriPulse service (accounts, farm, shop, incidents):
#   docker build -f app/Dockerfile --build-arg SERVICE=shop -t <registry>/agripulse/shop:<tag> app
# Base image pinned by digest; Python packages installed with --require-hashes; RDS CA bundle pinned by checksum.
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS build
ARG SERVICE
WORKDIR /build
COPY ${SERVICE}/requirements.txt .
RUN pip install --no-cache-dir --require-hashes --prefix=/install -r requirements.txt

FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
ARG SERVICE
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app AGRIPULSE_SERVICE=${SERVICE}
# RDS CA bundle: the services verify PostgreSQL's TLS certificate against it (sslmode=verify-full)
ADD --checksum=sha256:90a6f1a7978a1b1b096dca6c57511ca48d3b8cf052f8e704baf358f34ea2f21b \
    https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem /etc/ssl/rds/global-bundle.pem
RUN chmod 0644 /etc/ssl/rds/global-bundle.pem \
 && rm -rf /usr/local/lib/python3.12/site-packages/pip /usr/local/bin/pip* \
 && groupadd --gid 10001 app && useradd --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app
COPY --from=build /install /usr/local
WORKDIR /app
COPY --chown=root:root common/agripulse_common ./agripulse_common
COPY --chown=root:root ${SERVICE}/app ./app
USER 10001
EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-server-header", "--proxy-headers"]
