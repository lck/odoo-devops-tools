# Tests

Install the test dependency once:

```bash
python -m pip install -e ".[test]"
```

Run with standard pytest commands from the repository root:

```bash
pytest                         # Everything available on this machine
pytest -m "not integration"    # Fast tests, no real Docker/Odoo required
pytest -m integration          # Only real integrations
pytest tests/test_git.py       # One functional area
pytest tests/test_git.py -k clone  # Match test names
pytest tests/test_git.py::GitSourceSelection::test_shallow_clone_fast_forward_and_divergence  # One test
pytest tests/test_bundle.py    # Only portable bundle integration
```

Tests are organized by feature (`test_cli.py`, `test_ini.py`, `test_git.py`,
`test_docker.py`, `test_venv.py`, `test_bundle.py`, etc.). `integration` tests run real
Odoo and databases. They use Docker Compose to provision PostgreSQL and their
build tools. Without a usable Linux Docker Engine, these tests are **skipped**.

On Windows, a separate native integration test requires an explicitly isolated
PostgreSQL database (`odoo:odoo` at `127.0.0.1:5432`), `uv`, `psql`, `pg_dump`, and
`ODT_TEST_ISOLATED_POSTGRES=1`. This guard prevents accidentally modifying a
personal database. GitHub Actions provisions this test instance.
