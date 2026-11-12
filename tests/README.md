# Tests

Run tests from the repository root with uv (which installs the `dev` dependencies automatically):

```bash
uv run pytest                             # Everything available on this machine
uv run pytest -m "not integration"        # Fast tests, no real Docker/Odoo required
uv run pytest -m integration              # Only real integrations
uv run pytest tests/test_git.py           # One functional area
uv run pytest tests/test_git.py -k clone  # Match test names
uv run pytest tests/test_bundle.py        # Only portable bundle integration
```

Tests are organized by feature (`test_cli.py`, `test_ini.py`, `test_git.py`,
`test_docker.py`, `test_venv.py`, `test_bundle.py`, etc.). `integration` tests run real
Odoo and databases. They use Docker Compose to provision PostgreSQL and their
build tools. Without a usable Linux Docker Engine, these tests are **skipped**.

On Windows, a separate native integration test requires an explicitly isolated
PostgreSQL database (`odoo:odoo` at `127.0.0.1:5432`), `uv`, `psql`, `pg_dump`, and
`ODT_TEST_ISOLATED_POSTGRES=1`. This guard prevents accidentally modifying a
personal database. GitHub Actions provisions this test instance.
