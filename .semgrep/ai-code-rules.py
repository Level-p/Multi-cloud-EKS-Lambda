# Test cases for ai-code-rules.yml: `semgrep --test .semgrep/`
# "ruleid" lines must be reported, "ok" lines must not.
import hmac
import os
import pickle
import random
import secrets
import subprocess

import anthropic
import httpx
import yaml
from sqlalchemy import text


def shell_things(user_input):
    # ruleid: ai-subprocess-shell-true
    subprocess.run(f"ls {user_input}", shell=True)
    # ok: ai-subprocess-shell-true
    subprocess.run(["ls", user_input], check=True)
    # ruleid: ai-subprocess-shell-true
    os.system("rm -rf /tmp/x")


def eval_things(data):
    # ruleid: ai-eval-exec
    return eval(data)


def deserialise(blob, doc):
    # ruleid: ai-unsafe-deserialisation
    pickle.loads(blob)
    # ruleid: ai-unsafe-deserialisation
    yaml.load(doc)
    # ok: ai-unsafe-deserialisation
    yaml.safe_load(doc)


def tls(url):
    # ruleid: ai-tls-verification-disabled
    httpx.get(url, verify=False)
    # ok: ai-tls-verification-disabled
    httpx.get(url)


def sql(db, patient_id):
    # ruleid: ai-sql-built-with-strings
    db.execute(f"SELECT * FROM patients WHERE id = '{patient_id}'")
    # ruleid: ai-sql-built-with-strings
    db.execute(text(f"DELETE FROM readings WHERE id = {patient_id}"))
    # ok: ai-sql-built-with-strings
    db.execute(text("SELECT * FROM patients WHERE id = :id"), {"id": patient_id})


# ruleid: ai-hardcoded-secret
API_KEY = "sk-live-0123456789abcdef"
# ok: ai-hardcoded-secret
API_KEY_ENV = os.environ.get("API_KEY", "")


def make_session_token():
    # ruleid: ai-insecure-randomness-for-secrets
    return str(random.random())


def make_session_token_safely():
    # ok: ai-insecure-randomness-for-secrets
    return secrets.token_urlsafe(32)


def check_token(token, expected_token):
    # ruleid: ai-secret-compared-with-equals
    if token == expected_token:
        return True
    # ok: ai-secret-compared-with-equals
    return hmac.compare_digest(token.encode(), expected_token.encode())


def log_patient(log, patient):
    # ruleid: ai-phi-in-logs
    log.info("admitted", extra={"name": patient.name})
    # ok: ai-phi-in-logs
    log.info("admitted", extra={"patient_id": str(patient.id)})


def llm_runs_code(client):
    reply = client.messages.create(model="claude-opus-5-5", max_tokens=100, messages=[])
    command = reply.content[0].text
    # ruleid: llm-output-executed
    subprocess.run(command.split())


def llm_key():
    # ruleid: llm-api-key-in-code
    return anthropic.Anthropic(api_key="sk-ant-api03-xxxxxxxx")
