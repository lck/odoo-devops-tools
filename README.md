# odoo-compose

**Build reproducible Odoo workspaces.**

`odoo-compose`, provided by the `odoo-devops-tools` package, builds and maintains reproducible Odoo workspaces from a single project definition.

A basic workspace definition with addons can look like this:

```ini
[odoo]
version = 19.0

[addons.oca-web]
repo = https://github.com/OCA/web.git
branch = ${odoo:version}

[addons.oca-helpdesk]
repo = https://github.com/OCA/helpdesk.git
branch = ${odoo:version}
```

From this configuration, `odoo-compose` can generate a workspace such as:

```text
ROOT/
├── docker/                 # generated Docker artifacts
│   ├── local/              # Docker workflow artifacts
│   │   └── configs/        # generated local Odoo config
│   └── deploy/             # self-contained deploy build context
├── odoo/                   # Odoo source
├── odoo-addons/            # addon sources
│   ├── oca-web/
│   └── oca-helpdesk/
├── odoo-configs/           # generated Odoo configuration
├── odoo-data/              # Odoo data directory
├── odoo-logs/              # runtime logs
├── odoo-backups/           # backups created by CLI commands
├── wheelhouse/             # offline Python wheelhouse
├── venv/                   # Python virtual environment
├── compose.yaml            # Docker Compose file
└── odoo-project.ini        # workspace configuration
```

## Key features

* **Reproducible workspaces** — define and recreate an Odoo workspace from a single INI configuration.
* **Docker workflows** — generate local Docker Compose environments with optional [Mailpit](https://mailpit.axllent.org/) mail testing and self-contained deploy build contexts.
* **Portable workflows** — create verified bundles with sources and Python wheels for reproducible or offline use.
* **Python dependency management** — manage Python versions, virtual environments and requirements.
* **Composable configuration** — combine INI layers, variables, and CLI overrides.

`odoo-compose` focuses on creating and maintaining the Odoo workspace and its derived artifacts. Infrastructure provisioning and deployment orchestration remain outside its scope.

---

## System requirements

- **git**: https://git-scm.com/install/
- **uv**: https://docs.astral.sh/uv/getting-started/installation/
- **Docker**: https://docs.docker.com/get-docker/ — optional, for Docker workflows.

---

## Installation

Install the CLI with `uv`:

```bash
uv tool install -U odoo-devops-tools
```

Verify the installation:

```bash
odoo-compose --help
```

---

## Usage

`odoo-devops-tools` supports two development workflows, each with its own CLI command:

- **Docker workflow (`odoo-compose`)** — run Odoo and PostgreSQL with the generated Docker Compose environment.
- **Native development with venv (`odoo-compose-venv`)** — run Odoo directly on the host using a generated Python virtual environment.

Choose the workflow that fits your environment. Both use the same `odoo-project.ini` project definition.

## 1. Docker workflow

Create a new workspace:

```bash
odoo-compose init ./odoo19
```

This creates `./odoo19/odoo-project.ini` with this initial content:

```ini
[odoo]
version = 19.0
```

Then start the Docker Compose environment:

```bash
cd ./odoo19
docker compose up -d --build --wait
```

Optionally initialize the Odoo database from the command line instead of through the web interface:

```bash
odoo-compose run -i base --without-demo=all --stop-after-init
```

Docker Compose runs both Odoo and PostgreSQL. Odoo is available at http://localhost:8069.

> **Note**
> By default, generated Compose services use `restart: unless-stopped`; to prevent a development stack from automatically starting after a host or Docker restart, run:
> 
> ```bash
> odoo-compose --set docker:restart=no
> docker compose up -d --build --wait
> ```

### 1.1. Adding extra addons

To extend Odoo with additional functionality, add extra addons through `[addons.<name>]` sections in `odoo-project.ini`.

In this example, we add two Git-based addon repositories, `OCA/web` and `OCA/helpdesk`:

```ini
[addons.oca-web]
repo = https://github.com/OCA/web.git
branch = ${odoo:version}

[addons.oca-helpdesk]
repo = https://github.com/OCA/helpdesk.git
branch = ${odoo:version}
```

Sync the addon repositories and regenerate the Docker environment:

```bash
odoo-compose --sync-addons
docker compose up -d --build --wait
```

The repositories are cloned into `ROOT/odoo-addons/` and bind-mounted into the Odoo container. Python dependencies from their `requirements.txt` files are included when rebuilding the Docker image.

Install the new modules:

```bash
odoo-compose run -i web_notify,helpdesk_mgmt --without-demo=all --stop-after-init
```

For subsequent addon updates, use:

```bash
odoo-compose update
```

### 1.2. Running Odoo from workspace source

By default, the generated Docker workflow runs the Odoo provided by `[docker].base_image`.

Use `odoo_source = workspace` when you need to run Odoo from the workspace source.

For example:

```ini
[odoo]
version = 19.0
repo = git@github.com:my-company/odoo.git
branch = 19.0-custom-odoo

[docker]
odoo_source = workspace
```

Sync the Odoo source and generate the Docker artifacts:

```bash
odoo-compose --sync-all
docker compose up -d --build --wait
```

The resolved Odoo source is bind-mounted read-only at `/opt/odoo`. The Odoo Python source from the base image is removed while its installed dependencies remain available.

### 1.3. Mail testing with Mailpit

Enable Mailpit in the local Docker workflow when you need to inspect or test email without sending it to a real SMTP server:

```bash
odoo-compose --set docker:mailpit_enabled=true
docker compose up -d --build --wait
```

Mailpit is available at http://localhost:8025 by default. When running multiple workspaces, override the host web UI port, for example:

```bash
odoo-compose --set docker:mailpit_enabled=true --set docker:mailpit_webui_port=8026
docker compose up -d --build --wait
```

The generated local Odoo configuration sends outgoing email to `mailpit:1025`. When Mailpit is enabled, local Docker SMTP connection settings (`smtp_server`, `smtp_port`, `smtp_ssl`, `smtp_user`, and `smtp_password`) are intentionally overridden; the deploy configuration keeps the values from `[config]` unchanged.

Mailpit also exposes POP3 inside the Compose network at `mailpit:1110`, using `odoo` / `odoo` credentials. This can be used to test Odoo incoming mail and fetchmail flows by configuring an Odoo Incoming Mail Server with POP3, server `mailpit`, port `1110`, and SSL/TLS disabled.

Mailpit is generated only for the local Docker workflow and is not included in the deploy build context.

### 1.4. Sharing host directories

Use `[docker.share.<name>]` sections to expose host directories to the Odoo container as read-write bind mounts:

```ini
[docker.share.exchange]
source = /srv/integration/exchange
target = /mnt/exchange
```

`source` may be an absolute host path or a path relative to the workspace root. `target` must be an absolute path inside the Odoo container.

`odoo-compose` does not create the source directory or change its ownership or permissions; host provisioning or the deployment process must prepare the directory before Docker starts.

### 1.5. Limiting Docker Compose resources

You can optionally set CPU and memory limits for each service in the generated `compose.yaml` using `[docker]` in `odoo-project.ini`:

```ini
[docker]
odoo_cpus = 2.0
odoo_mem_limit = 4g

db_cpus = 1.0
db_mem_limit = 2g

mailpit_enabled = true
mailpit_cpus = 0.5
mailpit_mem_limit = 256m
```

Each option is independent and optional. If an option is missing or empty, `odoo-compose` omits that limit from the Compose service; no CPU or memory limit is imposed by the generator. CPU values must be positive decimal numbers (such as `0.5` or `2.0`). Memory limits must be positive amounts (such as `256m`, `2g`, or `4g`); zero and negative values are rejected. A memory limit is a hard container limit: exceeding it may cause an out-of-memory termination, so size it for your workload.

Regenerate the Docker configuration after editing the INI, then start the services:

```bash
odoo-compose
docker compose up -d --build --wait
```

### 1.6. Creating a Docker deploy build context

Use `--create-docker-deploy` to generate a self-contained Docker build context for CI/CD, testing, staging, production, or another non-local deployment workflow.

Addon modules are staged into the build context so the resulting image does not depend on bind-mounted workspace sources:

```bash
odoo-compose --sync-addons --create-docker-deploy
```

When `[docker].odoo_source = workspace`, sync the Odoo source as well:

```bash
odoo-compose --sync-all --create-docker-deploy
```

This additionally creates:

```text
ROOT/docker/deploy/
├── Dockerfile
├── .dockerignore
├── addons/
├── odoo/                    # only with odoo_source = workspace
├── requirements/
└── configs/
    └── odoo.conf
```

Addon modules are staged under `docker/deploy/addons/` and copied into `/mnt/extra-addons/` by the generated Dockerfile.

With `odoo_source = workspace`, the resolved Odoo source is also staged under `docker/deploy/odoo/` and copied into `/opt/odoo/`, so the deploy image is self-contained.

---

## 2. Native development with venv

In this workflow, Odoo runs directly on the host using a generated Python virtual environment.

Create the workspace with the Odoo source and virtual environment:

```bash
odoo-compose-venv init ./odoo19 --sync-all --create-venv \
  --set config:db_host=127.0.0.1 \
  --set config:db_name=odoo \
  --set config:db_user=odoo \
  --set config:db_password=odoo
```

> **Note**
> Make sure PostgreSQL is running at the configured host and the configured database user exists.

This creates `./odoo19/odoo-project.ini` with this initial content:

```ini
[odoo]
version = 19.0

[config]
db_host = 127.0.0.1
db_name = odoo
db_user = odoo
db_password = odoo
```

Then start Odoo with the CLI:

```bash
cd ./odoo19
odoo-compose-venv run
```

Odoo starts with the generated configuration from `./odoo-configs/odoo-server.conf` and is available at http://localhost:8069.

### 2.1. Adding extra addons

To extend Odoo with additional functionality, add extra addons through `[addons.<name>]` sections in `odoo-project.ini`.

In this example, we add two Git-based addon repositories, `OCA/web` and `OCA/helpdesk`:

```ini
[addons.oca-web]
repo = https://github.com/OCA/web.git
branch = ${odoo:version}

[addons.oca-helpdesk]
repo = https://github.com/OCA/helpdesk.git
branch = ${odoo:version}
```

Sync the addon repositories and recreate the Python environment to include their dependencies:

```bash
odoo-compose-venv --sync-all --create-venv
```

The repositories are cloned into `ROOT/odoo-addons/` and automatically added to the generated `addons_path`. Python dependencies from their `requirements.txt` files are included in the virtual environment.

Install the new modules:

```bash
odoo-compose-venv run -i web_notify,helpdesk_mgmt
```

For subsequent addon updates, use:

```bash
odoo-compose-venv update
```

The `update` command uses `click-odoo-update` with the workspace Odoo configuration.

### 2.2. Using system Python instead of managed Python

By default, `odoo-compose` uses `uv` to install and manage the requested Python version.

If you already have a suitable system Python installed, you can disable managed Python.

#### 2.2.1. Update the project file

Disable managed Python by adding `python_version = 3.11` and `managed_python = false` to the `odoo-project.ini` file.

> **Note**
> Set `python_version` to the Python version you want to use from your local system.
> In the example below, 3.11 is only illustrative.

```ini
[virtualenv]
managed_python = false
python_version = 3.11
```

#### 2.2.2. Update the workspace

After changing the project file, run `odoo-compose` again from the workspace root:

```bash
odoo-compose-venv --sync-all --create-venv
```

This recreates the virtual environment at `ROOT/venv` using the system Python.


---

## 3. Managing Python requirements

The `[virtualenv]` section controls additional Python dependencies used when provisioning both the native virtual environment and generated Docker images.

Use it to add new packages, pin specific versions, and override packages collected from Odoo or addon repository `requirements.txt` files.

Use:

- `requirements` to add extra packages or pin an explicit version
- `constraints` to restrict dependency versions without installing packages by themselves
- `build_constraints` to restrict build-time dependency versions
- `requirements_ignore` to skip packages that would otherwise be collected from repository requirements files

When a package is listed in `requirements`, `odoo-compose` automatically gives that package priority over the same package name from collected repository requirements and `constraints`. This means you can usually pin a package version just by adding it to `requirements`.

### 3.1. Add or pin packages

Use `requirements` to install additional packages or to force a specific version:

```ini
[virtualenv]
requirements =
  requests==2.32.3
  boto3==1.35.99
```

In this example, both packages are included in the generated dependency set and pinned to the specified versions.

### 3.2. Constrain dependency versions

Use `constraints` to restrict versions selected by the dependency resolver without adding those packages to the installation set:

```ini
[virtualenv]
constraints =
  urllib3<2
  lxml<6
```

A constraint only applies when the package is required by another dependency. If the same package is listed explicitly in `requirements`, the explicit requirement takes priority.

### 3.3. Override a package with a different one

If you want to replace a package with a different distribution name, add the replacement to `requirements` and skip the original package with `requirements_ignore`.

Example:

```ini
[virtualenv]
requirements =
  psycopg2-binary==2.9.9
requirements_ignore =
  psycopg2
```

In this example, `odoo-compose` installs `psycopg2-binary==2.9.9` and skips `psycopg2` when collecting repository requirements.

---

## 4. Creating portable workspace bundles

Portable bundles are useful when you want to prepare an Odoo workspace on an internet-connected machine and reproduce it on another compatible machine without cloning repositories or downloading Python packages again.

A portable workspace bundle is a ZIP archive containing:

```text
odoo/
odoo-addons/
wheelhouse/
manifest.json
odoo-project.ini
```

The bundle does not contain the virtual environment, database data, logs, backups, generated configuration files, Docker artifacts, Git metadata, or provisioning history.

Those machine-specific outputs are recreated on the target machine.

### 4.1. Create a bundle on the build machine

On an internet-connected build machine, sync the sources, build the wheelhouse, and create the bundle in one command:

```bash
odoo-compose-venv --sync-all --create-venv --create-bundle
```

When no output path is supplied, the bundle is written to:

```text
ROOT/dist/ROOT-NAME.odt.zip
```

You can also select an explicit output file:

```bash
odoo-compose-venv --sync-all --create-venv --create-bundle ./artifacts/odoo18-production.odt.zip
```

#### 4.1.1. Including uncommitted changes

By default, bundle creation stops when a bundled Git repository has uncommitted changes.

To intentionally include those changes in the bundle, use:

```bash
odoo-compose-venv --sync-all --create-venv --create-bundle --allow-dirty-bundle
```

### 4.2. Create the workspace on the target machine

Copy the ZIP to the target machine and import it into an empty directory:

```bash
odoo-compose-venv --create-from-bundle ./odoo18-production.odt.zip \
  --root ./odoo18-prod \
  --set config:db_host=127.0.0.1 \
  --set config:db_name=odoo \
  --set config:db_user=odoo \
  --set config:db_password=odoo
```

The import operation:

1. verifies the bundle format, platform, and CPU architecture;
2. rejects unsafe ZIP paths, duplicate entries, and symbolic links;
3. verifies every bundled file using its size and SHA-256 checksum;
4. extracts Odoo, addons, the sanitized project INI, and the wheelhouse;
5. recreates `ROOT/venv` strictly from the bundled wheelhouse;
6. regenerates workspace configuration files.

`ROOT` must be empty. If `--root` is omitted, the current working directory is used and must be empty. The imported manifest is saved as `ROOT/.odt-env/imported-bundle-manifest.json`.

> **Compatibility note**
> A wheelhouse is platform- and architecture-dependent. Create and import a bundle on compatible systems, for example Linux x86-64 to Linux x86-64. The target machine must have `uv` and access to the configured Python version. When `[virtualenv].managed_python = true`, `uv` may still need network access if that Python interpreter is not already installed or cached. For a fully disconnected target, install the required Python interpreter beforehand or use `managed_python = false`.

### 4.3. Manual wheelhouse workflow

The existing manual workflow remains available. After preparing a complete workspace on the build machine, copy the whole workspace and run this command from the copied root:

```bash
odoo-compose-venv --create-venv-from-wheelhouse
```

This recreates `ROOT/venv`, skips dependency compilation and wheelhouse building, and installs strictly from the existing `ROOT/wheelhouse/` and `all-requirements.lock.txt`.

---

## Commands

Commands perform workspace operations directly through the installed `odoo-devops-tools` CLI. Use `odoo-compose` for Docker workspaces and `odoo-compose-venv` for native workspaces. The `init` command creates the project INI when missing; other commands use the existing `ROOT/odoo-project.ini`; pass `--root ROOT` to select a different workspace. Each command has its own help: `odoo-compose <command> --help` (or `odoo-compose-venv <command> --help`).

### Shell completion

Both `odoo-compose` and `odoo-compose-venv` support `TAB` completion for
commands, their CLI options and filesystem paths. Generate a completion
script for your shell (no additional Python dependencies are needed):

**Bash** (`~/.bashrc`):

```bash
eval "$(odoo-compose completion bash)"
eval "$(odoo-compose-venv completion bash)"
```

**Zsh** (`~/.zshrc`, after enabling the Zsh completion system):

```zsh
autoload -Uz compinit && compinit
eval "$(odoo-compose completion zsh)"
eval "$(odoo-compose-venv completion zsh)"
```

**Fish** (`~/.config/fish/config.fish`):

```fish
odoo-compose completion fish | source
odoo-compose-venv completion fish | source
```

**PowerShell** (add to your `$PROFILE`):

```powershell
odoo-compose completion powershell | Out-String | Invoke-Expression
odoo-compose-venv completion powershell | Out-String | Invoke-Expression
```

Add the lines for your shell to its startup file and open a new terminal, or
run them in the current session to enable completion immediately. For example,
`odoo-compose init --sy<TAB>` completes `--sync-odoo`, `--sync-addons` or
`--sync-all`.

### `init`

Create a Docker or native workspace using the default project template. `init` supports the existing provisioning options, including `--sync-all`, `--create-venv` (native) and `--set`:

```bash
odoo-compose init ./odoo19 --sync-addons
odoo-compose-venv init ./odoo19 --sync-all --create-venv --set odoo:version=19.0
```

Existing project INI files are preserved. Run `odoo-compose init --help` or `odoo-compose-venv init --help` for all options.

### `run`, `shell`, `update` and `test`

Run Odoo, open an interactive shell or update installed addons directly from the workspace. Additional arguments are forwarded to Odoo or `click-odoo-update` as appropriate:

```bash
# Docker
odoo-compose run -i base --stop-after-init
odoo-compose shell
odoo-compose update --all
odoo-compose test -i sale --test-tags /sale

# Native
odoo-compose-venv run -i base --stop-after-init
odoo-compose-venv shell
odoo-compose-venv update --all
odoo-compose-venv test -i sale --test-tags /sale
```

`update --all` invokes `click-odoo-update --update-all`. The `test` command runs Odoo with `--test-enable --stop-after-init` in both workflows. Docker commands execute one-off containers; native commands use the existing workspace virtualenv and generated `odoo-server.conf`. The installed CLI must be available; generated wrappers are not required. Set `ODOO_DOCKER_SUDO=1` on Linux if Docker needs `sudo`.

### `backup-db` and `restore-db`

Create a PostgreSQL custom-format dump, or restore it to `[config].db_name` (Docker defaults to `odoo`):

```bash
odoo-compose backup-db
odoo-compose backup-db ./odoo-backups/pre-upgrade.dump
odoo-compose restore-db ./odoo-backups/pre-upgrade.dump
odoo-compose restore-db --force --copy ./odoo-backups/pre-upgrade.dump
odoo-compose restore-db --move ./odoo-backups/pre-upgrade.dump

# Native (odoo-compose-venv only): optional PostgreSQL client override
export POSTGRES_BIN=/usr/lib/postgresql/16/bin
odoo-compose-venv backup-db
odoo-compose-venv restore-db ./odoo-backups/pre-upgrade.dump
```

Use `odoo-compose-venv` instead for native development. The default dump name is `ROOT/odoo-backups/<database>_<timestamp>.dump`. An existing target database is never overwritten without `--force`. By default, `restore-db` uses **copy** semantics: Odoo database identity is regenerated and selected enterprise metadata is reset. Choose `--move` to preserve the original identity. Stop the Odoo service before restoring its active database.

For native database commands, `[config].db_name` comes from `odoo-project.ini`; if absent, provisioning generates a workspace-specific name and saves it there. PostgreSQL connection settings come from `[config]` or standard `PG*` environment variables (`PGHOST`, `PGPORT`, `PGUSER`, `PGPASSWORD`, etc.); `.pgpass` is also supported.

### `backup-filestore` and `restore-filestore`

Backup the selected database filestore to a `.tar.gz` archive, or restore it:

```bash
odoo-compose backup-filestore
odoo-compose backup-filestore ./odoo-backups/pre-upgrade-filestore.tar.gz
odoo-compose restore-filestore ./odoo-backups/pre-upgrade-filestore.tar.gz
odoo-compose restore-filestore --force ./odoo-backups/pre-upgrade-filestore.tar.gz
```

Use `odoo-compose-venv` for the native workflow. The default archive name is `ROOT/odoo-backups/<database>_<timestamp>_filestore.tar.gz`. An existing target filestore is protected unless `--force` is given. For native workspaces, the path is read from `[config].data_dir` or defaults to `ROOT/odoo-data`. For Docker, the filestore is accessed through the one-off Odoo container. Stop Odoo before restoring an active filestore.

### `restic`, `backup-filestore-restic` and `restore-filestore-restic`

Restic enables incremental filestore snapshots and deduplication. Set `RESTIC_REPOSITORY` and either `RESTIC_PASSWORD_FILE` or `RESTIC_PASSWORD` first. In Docker, the CLI runs Restic in a one-off container and bind-mounts an absolute local host repository; remote Restic repositories are also supported. For native use, `RESTIC_BIN` can select the installed Restic executable (see the example below).

```bash
export RESTIC_REPOSITORY=/srv/restic/odoo
export RESTIC_PASSWORD_FILE=/etc/restic/odoo-password

odoo-compose restic init
odoo-compose backup-filestore-restic
odoo-compose restic snapshots
odoo-compose restic check
odoo-compose restore-filestore-restic --force latest
odoo-compose restore-filestore-restic --force SNAPSHOT_ID

# Native (odoo-compose-venv only): optional Restic executable override
export RESTIC_BIN=/opt/restic/restic
odoo-compose-venv backup-filestore-restic
odoo-compose-venv restic snapshots
```

Replace `odoo-compose` with `odoo-compose-venv` for native operations. Snapshots are tagged `odoo`, `filestore` and `db:<database>`; set `RESTIC_BACKUP_ID` for an extra `backup-id:<value>` tag. `RESTIC_HOST` overrides the default host name used for snapshot grouping. To restore a different database's snapshot, set `RESTIC_SOURCE_DB_NAME`. Restore protects existing filestores unless `--force` is used. For `latest`, snapshot lookup also filters by host and original filestore path. A Restic restore should be run while Odoo is stopped.

### `neutralize`

Neutralize a production database restored into a development or test workspace:

```bash
# Docker workflow
odoo-compose neutralize

# Native workflow
odoo-compose-venv neutralize
```

Both commands use the current `ROOT/odoo-project.ini` and run without regenerating workspace artifacts. To run from outside the workspace, add `--root /path/to/workspace`.

For Odoo 16.0 and newer, neutralization uses Odoo's built-in mechanism and the `neutralize.sql` scripts supplied by installed modules. This disables or redirects supported production-side effects such as scheduled actions, outgoing email, and external integrations.

For Odoo 13.0 through 15.0, where Odoo's built-in neutralization is unavailable, the command performs a minimal compatibility neutralization of scheduled actions and configured outgoing/incoming mail servers. The Docker workflow uses the Odoo ORM; the native workflow uses PostgreSQL commands.

#### Workspace SQL and INI interpolation

After standard neutralization, the command executes optional SQL files from `ROOT/neutralize/`:

```text
neutralize/
├── 01-delete-fetchmail-servers.sql
├── 02-disable-outgoing-mail-servers.sql
└── 03-set-ir-config-params.sql
```

Only `*.sql` files directly inside this directory are executed, in lexicographic filename order. SQL execution stops on the first error; subdirectories are not traversed. If the directory is missing, only the standard Odoo/legacy neutralization runs.

Custom SQL supports `${section:option}` references to the current `ROOT/odoo-project.ini`, including nested `[vars]` references. Each reference becomes a **quoted PostgreSQL text literal**, so **do not add SQL quotes around it**. For example:

`odoo-project.ini`:

```ini
[vars]
base_url = https://staging.example.com
```

`neutralize/03-set-ir-config-params.sql`:

```sql
INSERT INTO ir_config_parameter (key, value)
VALUES ('web.base.url', ${vars:base_url})
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
```

As in INI files, `\${section:option}` inserts a literal `${section:option}`; paired backslashes before `${...}` yield literal backslashes, while `$` and `$$` remain unchanged. An unknown reference causes an error before the affected SQL file is sent to PostgreSQL.

During normal execution, resolved SQL is streamed directly to `psql` without being saved to disk. Original SQL files are never modified. Ready-to-adapt examples are available under `examples/neutralize/`.

#### SQL preview (`--dry-run`)

To inspect interpolated workspace SQL **without connecting to Docker, Odoo, or PostgreSQL**, use:

```bash
odoo-compose neutralize --dry-run
odoo-compose neutralize --dry-run ./preview-sql
odoo-compose-venv neutralize --dry-run
odoo-compose-venv neutralize --dry-run ./preview-sql
```

Without an explicit output directory, preview files are written to `ROOT/.odt-env/neutralize-preview/`. The preview includes **only** custom `ROOT/neutralize/*.sql` files, not Odoo's built-in neutralization. No SQL is executed.

> **Security note**
> Preview files may contain interpolated passwords or tokens. Protect or delete them after inspection. On Unix-like systems, newly created preview SQL files have permissions `0600`.

#### Restore and neutralize

Neutralization is a separate step after database and filestore restore. For example:

**Docker workflow:**

```bash
docker compose stop odoo
odoo-compose restore-db --copy ./odoo-backups/production.dump
odoo-compose restore-filestore ./odoo-backups/production-filestore.tar.gz
odoo-compose neutralize
docker compose up -d odoo
```

**Native workflow:**

```bash
odoo-compose-venv restore-db --copy ./odoo-backups/production.dump
odoo-compose-venv restore-filestore ./odoo-backups/production-filestore.tar.gz
odoo-compose-venv neutralize
```

---

## Command-line reference

### Syntax

```text
{odoo-compose|odoo-compose-venv} [INI] [OPTIONS]
{odoo-compose|odoo-compose-venv} init [ROOT] [OPTIONS]
{odoo-compose|odoo-compose-venv} run [--root ROOT] [ODOO_ARGS...]
{odoo-compose|odoo-compose-venv} shell [--root ROOT] [ODOO_ARGS...]
{odoo-compose|odoo-compose-venv} update [--root ROOT] [--all] [ODOO_ARGS...]
{odoo-compose|odoo-compose-venv} test [--root ROOT] [ODOO_ARGS...]
{odoo-compose|odoo-compose-venv} neutralize [--root ROOT] [--dry-run [OUTPUT_DIR]]
{odoo-compose|odoo-compose-venv} backup-db [--root ROOT] [OUTPUT]
{odoo-compose|odoo-compose-venv} restore-db [--root ROOT] [--force] [--copy|--move] BACKUP.dump
{odoo-compose|odoo-compose-venv} backup-filestore [--root ROOT] [OUTPUT]
{odoo-compose|odoo-compose-venv} restore-filestore [--root ROOT] [--force] FILESTORE.tar.gz
{odoo-compose|odoo-compose-venv} restic [--root ROOT] RESTIC_COMMAND [ARGS...]
{odoo-compose|odoo-compose-venv} backup-filestore-restic [--root ROOT] [RESTIC_ARGS...]
{odoo-compose|odoo-compose-venv} restore-filestore-restic [--root ROOT] [--force] [SNAPSHOT]
{odoo-compose|odoo-compose-venv} completion {bash|zsh|fish|powershell}
```

Use **`odoo-compose`** for Docker operations and **`odoo-compose-venv`**
for native Odoo. Both commands share project configuration, repository syncing,
and workspace root, but generate only their own artifacts. `odoo-compose-venv`
generates native configuration.

If no arguments are specified, `odoo-compose` and `odoo-compose-venv` treats the current working directory as ROOT and, when `ROOT/odoo-project.ini` exists,
regenerates the workspace artifacts without syncing repositories or recreating the virtual environment.

### Project definition (`INI`)

`INI` is the optional project definition source used by `odoo-compose`. It can be:

  - a local filesystem path, for example:

    ```bash
    odoo-compose /path/to/odoo-project.ini
    ```

  - a remote INI loaded from a Git repository, for example:

    ```bash
    odoo-compose 'git+https://github.com/lck/odoo-devops-tools.git//examples/odoo-project.ini?ref=main'
    odoo-compose 'git+git@github.com:company/repo.git//examples/odoo-project.ini?ref=main'
    ```

    Syntax:

    ```text
    git+REPO_URL//PATH/TO/PROJECT.ini?ref=REF
    ```

  - a remote INI loaded from a URL, for example:

    ```bash
    odoo-compose 'https://github.com/lck/odoo-devops-tools/blob/main/examples/odoo-project.ini'
    odoo-compose 'https://raw.githubusercontent.com/lck/odoo-devops-tools/main/examples/odoo-project.ini'
    ```

#### Default project file convention

If no positional `INI` file is provided and no `-i/--include` option is used, `odoo-compose` looks for `ROOT/odoo-project.ini`.

This is similar to how Docker Compose uses `compose.yaml` by convention.

For example, this command:

```bash
odoo-compose --root ./existing-workspace
```

is equivalent to passing the default project file explicitly:

```bash
odoo-compose ./existing-workspace/odoo-project.ini
```

#### INI includes

Use `-i INI` / `--include INI` to include additional project layers. The option can be repeated.

```bash
odoo-compose base-odoo-project.ini -i local-overrides.ini -i extra-addons.ini
```

Project layers are processed from left to right. Later layers override earlier layers.

Validation is performed only after all layers have been merged.

The merged project file is saved as `ROOT/odoo-project.ini`, replacing any existing file at that path.

### Paths and outputs

- `--root ROOT` — workspace root directory. Default: the directory containing a local INI file, or the current working directory for a remote INI or omitted INI. In include mode, the default is the directory of the first local source, or the current working directory when the first source is remote.
- `--include INI`, `-i INI` — include an additional project INI layer; can be repeated. Later layers override earlier layers.
- `--extra-var KEY=VALUE`, `-e KEY=VALUE` — override or inject a value in the optional `[vars]` section; can be repeated.
- `--set SECTION:KEY=VALUE`, `-S SECTION:KEY=VALUE` — set or override a supported project option; can be repeated. Missing supported sections/options are created automatically. Structured sections (`[virtualenv]`, `[odoo]`, `[addons.<name>]`, `[docker]`, and `[docker.share.<name>]`) accept only documented keys; `[config]` remains open to standard Odoo configuration options except `addons_path`.
- `--no-configs` — do not generate config files (venv target).
- `--no-data-dir` — do not create the Odoo data directory.
- `--no-provisioning-log` — do not write provisioning metadata under `ROOT/.odt-env/`.
- `--show-last-run` — print metadata from `ROOT/.odt-env/last-provisioning.json` and exit without provisioning.

### Repository sync

- `--sync-odoo` — sync only the Git-managed Odoo source; when `[odoo].path` is used, the local path is reused and Git sync is skipped.
- `--sync-addons` — sync only `ROOT/odoo-addons/*`.
- `--sync-all` — sync both Odoo and addons.

> **Note**
> If any target repository contains local uncommitted changes, `odoo-compose` aborts the sync operation.
> Commit, stash, or discard the changes before running a sync command.

### Python, virtual environment, and wheelhouse

Online virtual environment provisioning:

- `--create-venv` — `odoo-compose-venv` only: explicitly create or recreate `ROOT/venv` and refresh the wheelhouse. Without a venv-provisioning option, no virtual environment is created or modified.

Offline deployment from a prebuilt wheelhouse:

- `--create-venv-from-wheelhouse` — recreate `ROOT/venv` from an existing `ROOT/wheelhouse/` and `all-requirements.lock.txt`, install strictly offline, and skip lock compilation and wheelhouse build. This is useful after preparing dependencies on an internet-connected build machine and copying the workspace to a target machine without internet access.

Maintenance:

- `--clear-pip-wheel-cache` — remove all items from pip's wheel cache.

### Portable workspace bundles

- `--create-bundle [BUNDLE]` — `odoo-compose-venv` only: create a verified portable ZIP containing Odoo sources, configured addon sources, a sanitized `odoo-project.ini`, and `ROOT/wheelhouse/`. If `BUNDLE` is omitted, the output is `ROOT/dist/ROOT-NAME.odt.zip`. Relative explicit output paths are resolved from the current working directory.
- `--allow-dirty-bundle` — allow `--create-bundle` to snapshot Git repositories with uncommitted changes. Without this option, dirty repositories abort bundle creation.
- `--create-from-bundle BUNDLE` — `odoo-compose-venv` only: verify and extract a portable bundle into an empty `ROOT`, then recreate `ROOT/venv` using the bundled wheelhouse. This offline deployment path intentionally skips Docker workflow generation; `--create-docker-deploy` cannot be combined with it.

### Docker generation

- `odoo-compose` runs the Docker workflow. It regenerates `ROOT/docker/local/` and `ROOT/compose.yaml`; addon sources are bind-mounted from the workspace into the Odoo container. With `[docker].odoo_source = workspace`, the resolved Odoo source is also bind-mounted at `/opt/odoo` and used as the runtime Odoo source. `[docker.share.<name>]` sections add read-write host-directory bind mounts without creating missing source paths. Set `[docker].mailpit_enabled = true` to add a persistent Mailpit service for local SMTP and POP3 testing. Database/filestore backup and restore operations are available as [commands](#commands).
- `--create-docker-deploy` — `odoo-compose` only: generate a self-contained deployment build context under `ROOT/docker/deploy/`. Addon modules are staged into the context; with `[docker].odoo_source = workspace`, the resolved Odoo source is staged as well.

### Other options

- `--version` — show the installed `odoo-compose` version and exit.

---

## Project file reference

The `odoo-compose` project file is an INI file that describes the Odoo workspace to create.

At minimum, the project file must contain this section:

- `[odoo]`

The following sections are supported:

- `[vars]` — optional reusable variables for INI interpolation
- `[virtualenv]` — optional Python and dependency settings
- `[odoo]` — required Odoo source settings
- `[addons.<name>]` — optional addon sources
- `[docker]` — optional Docker workflow/deploy generation settings
- `[docker.share.<name>]` — optional host-directory shares mounted into the Odoo container
- `[config]` — optional Odoo server configuration values

### General rules

- The project file can have any filename when passed explicitly. When `INI` is omitted, `odoo-compose` uses the existing `ROOT/odoo-project.ini`; if it is missing, use `init` to create it explicitly from the bundled default template. Remote INI sources and merged include layers are materialized as `ROOT/odoo-project.ini`.
- INI interpolation is supported, so values such as `${odoo:version}` can be reused across sections.
- Use `\${section:key}` for a literal `${section:key}`.
- Multiple INI layers can be composed with `-i/--include`. Later layers override earlier layers; multi-line values are replaced as whole option values, not appended.
- The optional `[vars]` section is useful for reusable values referenced as `${vars:name}`.
- Values from `[vars]` can be overridden or injected from the CLI with `-e name=value` / `--extra-var name=value`.
- Supported project options can be set or overridden directly with `-S section:key=value` / `--set section:key=value`, even when the section or option is omitted from the INI file. Structured sections accept only their documented keys.
- Multi-line values are used for lists such as `requirements`, `constraints`, `build_constraints`, and `requirements_ignore`.

### `[vars]`

This section is optional.

Use it for reusable values that you want to interpolate in other sections.

A major advantage of `[vars]` is that its values can also be overridden directly from the CLI with `-e KEY=VALUE` / `--extra-var KEY=VALUE`. This makes it easy to keep a single project file and adjust things like Odoo version, branch, commit, or database name per run without editing the file.

Example:

```ini
[vars]
branch = 18.0
db = odoo

[odoo]
version = 18.0
branch = ${vars:branch}

[config]
db_name = ${vars:db}
db_user = odoo
db_password = odoo
```

CLI override example:

```bash
odoo-compose-venv odoo-project.ini --sync-all --create-venv -e branch=dev -e db=odoo_dev
```

### `[virtualenv]`

This section is optional.

- `python_version` — Python version for the virtual environment. If omitted, `odoo-compose` chooses a default version based on the selected Odoo version.
- `managed_python` — whether `uv` should install and manage Python automatically. Default: `true`.
- `requirements` — additional Python requirements to install. Multi-line list.
- `constraints` — dependency constraints used during resolution without installing packages by themselves. Explicit `requirements` take priority over matching constraints. Multi-line list.
- `build_constraints` — additional build constraints used during dependency compilation. Multi-line list.
- `requirements_ignore` — package names to ignore when collecting requirements from addon repositories. Multi-line list.

Example:

```ini
[virtualenv]
managed_python = false
python_version = 3.11
requirements =
  lxml>=6
  psycopg2-binary==2.9.9
constraints =
  urllib3<2
requirements_ignore =
  psycopg2
```

### `[odoo]`

This section is required.

- `version` — Odoo version in `X.0` format, for example `18.0`. Required.
- `path` — local Odoo source directory. Relative paths are resolved relative to `ROOT/`.
- `repo` — Git repository URL for Odoo. Default: the official Odoo repository.
- `branch` — Git branch to check out. Default: the same value as `version`.
- `commit` — optional Git commit to check out after fetching the selected branch. When set, the repository is pinned to that exact revision.
- `shallow` — whether to use a shallow clone. Default: `true`. Ignored when `commit` is set.

Odoo must use exactly one of these source modes:

- local Odoo source: `version` + `path`
- Git-managed Odoo source: `version` + optional `repo`, `branch`, `commit`, and `shallow`

Git-managed example:

```ini
[odoo]
version = 18.0
repo = https://github.com/odoo/odoo.git
branch = 18.0
commit = e6ec487
shallow = true
```

Local source example:

```ini
[odoo]
version = 18.0
path = ../odoo
```

### `[addons.<name>]`

Addon sections are optional. You can define as many as needed.

Each addon must use exactly one of these source types:

- local addon path: `path`
- Git repository: `repo` + `branch` (+ optional `commit` and `shallow`)

Rules:

- For a local addon, use only `path`.
- For a Git addon, `repo` and `branch` are required.
- `commit` is optional for a Git addon. When set, the repository is pinned to that exact revision.
- `shallow` is optional for Git addons and defaults to `true`. It is ignored when `commit` is set.
- Relative local paths are resolved relative to `ROOT/`.
- Git-based addons are cloned into `ROOT/odoo-addons/<name>/`.
- All configured addon directories are automatically appended to the generated `addons_path`.

Examples:

```ini
[addons.my-custom-addons]
path = odoo-addons/my-custom-addons

[addons.oca-web]
repo = https://github.com/OCA/web.git
branch = ${odoo:version}
commit = abcdef1
```

### `[docker]`

This section is optional.

- `restart` — restart policy for Odoo, PostgreSQL and optional Mailpit (`no`, `always`, `unless-stopped`, or `on-failure`). Default: `unless-stopped`.
- `base_image` — Docker image used as the base image in generated Dockerfiles. Default: `odoo:${odoo:version}`.
- `odoo_source` — selects the Odoo source for Docker. `image` uses Odoo from `base_image`; `workspace` uses the workspace Odoo source. Default: `image`.
- `postgres_version` — PostgreSQL major version used by the local Docker Compose database service. Default: `16`.
- `restic_version` — version of `restic/restic` in generated local and deploy Dockerfiles. Default: `0.19.1`. An explicitly set value must be a concrete version; empty values and `latest` are invalid.
- `mailpit_enabled` — enables a persistent Mailpit service in the local Docker workflow for SMTP and POP3 mail testing. Default: `false`. It does not affect the deploy build context.
- `mailpit_version` — version of `ghcr.io/axllent/mailpit` in local Compose. Default: `v1.31.3`. An explicitly set value must be a concrete version; empty values and `latest` are invalid.
- `mailpit_webui_port` — host port used for the Mailpit web UI. Default: `8025`. The internal Mailpit web UI port remains `8025`.
- `odoo_cpus`, `db_cpus`, `mailpit_cpus` — optional CPU limits for the respective Compose services; positive decimal CPU counts such as `0.5` or `2.0`.
- `odoo_mem_limit`, `db_mem_limit`, `mailpit_mem_limit` — optional memory limits for the respective Compose services; positive amounts such as `256m` or `4g`.

Example:

```ini
[docker]
restart = no
postgres_version = 18
restic_version = 0.19.1
mailpit_enabled = true
mailpit_version = v1.31.3
mailpit_webui_port = 18025
```

### `[docker.share.<name>]`

Each section defines one read-write host-directory bind mount for the generated Odoo Compose service.

- `source` — required host directory. Relative paths are resolved relative to `ROOT/`; absolute paths are preserved.
- `target` — required absolute path inside the Odoo container.

The generated mount always sets `bind.create_host_path: false`. The source directory must therefore be created or mounted, and its permissions prepared, by host provisioning or the deployment process before Docker starts. `odoo-compose` does not create, `chmod`, or `chown` share sources.

Example:

```ini
[docker.share.exchange]
source = /mnt/nfs/exchange
target = /mnt/exchange
```

The same share can be configured through the CLI:

```bash
odoo-compose \
  --set docker.share.exchange:source=/mnt/nfs/exchange \
  --set docker.share.exchange:target=/mnt/exchange
```

### `[config]`

This section is optional.

When present, it contains Odoo server configuration values written into `ROOT/odoo-configs/odoo-server.conf`.

For `odoo-compose-venv`, omitting `[config]` still generates a valid config file with generated values such as `addons_path` and `data_dir`. The Docker workflow does not generate `odoo-server.conf`.

You can define standard Odoo configuration options here.

Special rules:

- `addons_path` must not be set in `[config]`. `odoo-compose` always generates it automatically.
- `data_dir` may be set in `[config]`. If provided, it overrides the default data directory location.

Example:

```ini
[config]
db_host = 127.0.0.1
db_port = 5432
db_name = odoo
db_user = odoo
db_password = odoo
http_port = 8069
```
