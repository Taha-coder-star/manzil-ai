import pytest

from deploy.activate_rls import runtime_url, url_from_line


def test_runtime_url_retains_host_database_and_options():
    admin = "postgresql://admin:old@db.example.invalid:5432/demo?sslmode=require"
    url = runtime_url(admin, "a+b/c")
    assert url == (
        "postgresql://manzil_app:a%2Bb%2Fc@db.example.invalid:5432/demo"
        "?sslmode=require"
    )
    assert "old" not in url


def test_environment_parser_accepts_one_quoted_url():
    line = 'DATABASE_URL="postgresql://admin:old@db.example.invalid/demo"\n'
    assert url_from_line(["PORT=8000\n", line]) == (
        line, "postgresql://admin:old@db.example.invalid/demo", '"'
    )


@pytest.mark.parametrize("lines", [
    [],
    ["DATABASE_URL=one\n", "DATABASE_URL=two\n"],
    ["DATABASE_URL='unfinished\n"],
])
def test_environment_parser_rejects_ambiguous_values(lines):
    with pytest.raises(ValueError):
        url_from_line(lines)
