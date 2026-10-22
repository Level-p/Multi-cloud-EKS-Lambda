import json
from types import SimpleNamespace

import pytest

from app.db import iam_token_provider
from app.sql_bootstrap import database_statements, plan, quote_ident, role_statements

USERS = {"agripulse": "agripulse_web", "incidents": "agripulse_incidents", "mobile": "agripulse_mobile"}


def test_each_app_owns_only_its_own_database():
    assert plan({"DB_USERS": json.dumps(USERS)}) == USERS
    assert database_statements("agripulse", "agripulse_web", exists=False) == [
        'CREATE DATABASE "agripulse" OWNER "agripulse_web"',
        'REVOKE CONNECT, TEMPORARY ON DATABASE "agripulse" FROM PUBLIC']
    assert database_statements("agripulse", "agripulse_web", exists=True)[0] == 'ALTER DATABASE "agripulse" OWNER TO "agripulse_web"'


def test_users_sign_in_with_iam_only():
    sql = role_statements("agripulse_web", "agripulse_admin")
    assert sql == ['CREATE ROLE "agripulse_web" LOGIN', 'GRANT rds_iam TO "agripulse_web"', 'GRANT "agripulse_web" TO "agripulse_admin"']
    assert not any("PASSWORD" in s or "SUPERUSER" in s for s in sql)


@pytest.mark.parametrize("bad", ['x"; DROP DATABASE agripulse; --', "Web", "a b", "", "x'--", "1abc"])
def test_names_cannot_inject_sql(bad):
    with pytest.raises(ValueError):
        quote_ident(bad)
    with pytest.raises(ValueError):
        plan({"DB_USERS": json.dumps({"agripulse": bad})})


def test_every_new_connection_gets_a_fresh_iam_token():
    calls = []
    rds = SimpleNamespace(generate_db_auth_token=lambda **kw: calls.append(kw) or f"token-{len(calls)}")
    hook = iam_token_provider(rds, "db.example", 5432, "agripulse_web", "eu-west-2")
    first, second = {}, {}
    hook(None, None, [], first)
    hook(None, None, [], second)
    assert (first["password"], second["password"]) == ("token-1", "token-2")
    assert calls[0] == {"DBHostname": "db.example", "Port": 5432, "DBUsername": "agripulse_web", "Region": "eu-west-2"}
