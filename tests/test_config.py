from agent_runtime.db import sqlalchemy_url


def test_sqlalchemy_url_conversion():
    assert sqlalchemy_url("postgresql://u:p@db/x") == "postgresql+asyncpg://u:p@db/x"
