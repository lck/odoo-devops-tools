# odoo-devops-tools

A small set of utilities for **local Odoo development** and **simple Odoo deployments**.

The main entry point is **`odt-env`**, a CLI that provisions an Odoo workspace from a **single project file**.

---

## System requirements

- **git**: https://git-scm.com/install/
- **uv** (Python package & project manager): https://docs.astral.sh/uv/getting-started/installation/

---

## Installation

Using `uv`:

```bash
uv tool install odoo-devops-tools
```

Verify:

```bash
odt-env --help
```

---

## Usage

### 1. Minimal example

This is the minimal example for provisioning a workspace with Odoo 18.

#### 1.1. Create a project file

Create a file named `odoo-project.ini`.

> **Note**
> odoo-project.ini is only an example filename used in this README.
> The project file can have a different name.

```ini
[odoo]
version = 18.0

[config]
db_host = 127.0.0.1
db_name = odoo
db_user = odoo
db_password = odoo
```

#### 1.2. Create the workspace from the project file

Run `odt-env` against the project file:

```bash
odt-env odoo-project.ini --sync-all --create-venv
```

#### 1.3. Optional: Load the project file from Git

Instead of passing a local file, the `INI` argument can point to a file stored in a Git repository:

```bash
odt-env --sync-all --create-venv \
  git::https://github.com/lck/odoo-devops-tools.git//examples/odoo18-minimal.ini
```

After provisioning, the workspace has the following structure:

```text
ROOT/
├── odoo-project.ini      # project definition
├── compose.yml           # generated sample Docker Compose file when --build-docker-image is used
├── odoo/                 # Git-managed Odoo source repository; not required when [odoo].path is used
├── odoo-addons/          # addon repositories from [addons.<name>] sections; unused in this minimal example
├── odoo-backups/         # backups created by helper scripts
├── odoo-configs/         # generated configuration, including odoo-server.conf
├── odoo-data/            # Odoo data directory
├── odoo-docker/          # generated Docker build context
├── odoo-logs/            # runtime logs
├── odoo-scripts/         # generated helper scripts
│   ├── run.sh            # start Odoo in the foreground
│   ├── instance.sh       # manage Odoo as a background service (start|stop|restart|status)
│   ├── test.sh           # run Odoo tests
│   ├── shell.sh          # open an Odoo shell
│   ├── backup.sh         # create a timestamped ZIP backup in ROOT/odoo-backups/
│   ├── restore.sh        # restore a backup into the configured database
│   ├── update.sh         # update modules, auto-detecting addons to update using file-content hashes stored in the DB
├── venv/                 # Python virtual environment
└── wheelhouse/           # wheelhouse for offline installs
```

#### 1.4. Start Odoo

When the workspace is ready, start Odoo:

```bash
./odoo-scripts/run.sh
```

On Windows, use the `.bat` variants instead:

```bat
odoo-scripts\run.bat
```

The server starts with the generated configuration from `ROOT/odoo-configs/odoo-server.conf`.

After the server starts, Odoo is available at http://localhost:8069.

---

### 2. Adding extra addons from Git and local folders

To extend Odoo with additional functionality, you can add extra addons through `[addons.<name>]` sections.

In this example, we add two addon repositories, `OCA/web` and `OCA/helpdesk`, and one local folder, `odoo-addons/my-custom-addons`, containing custom Odoo addons.

#### 2.1. Update the project file

Add the extra addons to the `odoo-project.ini` file.

```ini
[odoo]
version = 18.0

[addons.oca-web]
repo = https://github.com/OCA/web.git
branch = ${odoo:version}

[addons.oca-helpdesk]
repo = https://github.com/OCA/helpdesk.git
branch = ${odoo:version}

[addons.my-custom-addons]
path = odoo-addons/my-custom-addons

[config]
db_host = 127.0.0.1
db_name = odoo
db_user = odoo
db_password = odoo
```

#### 2.2. Update the workspace

After changing the project file, run `odt-env` again to update the workspace:

```bash
odt-env odoo-project.ini --sync-all --create-venv
```

This clones the Git-based addons into `ROOT/odoo-addons/oca-web/` and `ROOT/odoo-addons/oca-helpdesk/`.

Both Git-based addon directories and the local folder `ROOT/odoo-addons/my-custom-addons/` are then added to the generated `addons_path`.

If any of these addon sources contains a `requirements.txt` file, `odt-env` automatically installs the listed dependencies into the Python virtual environment.

#### 2.3. Optional: Use full clones instead of shallow clones

By default, `odt-env` uses shallow, single-branch clones for Git repositories.

In most cases, shallow clones are the right choice, especially for third-party addons and for the main Odoo repository.

A full clone usually only makes sense for custom addons that are actively being developed, where access to the full Git history is useful.

If you need the full Git history, set `shallow = false` in the relevant section and run `odt-env` again with a sync option.

If you set `commit`, `odt-env` automatically ignores `shallow` and fetches enough history to check out the requested commit.

Example:

```ini
[addons.my-custom-addons]
repo = https://github.com/example/my-custom-addons.git
branch = 18.0
shallow = false
```

#### 2.4. Optional: Pin Odoo or an addon to a specific commit

By default, git repositories are tracked by branch.

If you need a reproducible workspace tied to an exact Git revision, you can also specify `commit` in the relevant `[odoo]` or `[addons.<name>]` section.

Example for Odoo:

```ini
[odoo]
version = 18.0
repo = https://github.com/odoo/odoo.git
branch = 18.0
commit = e6ec487
```

Example for an addon repository:

```ini
[addons.oca-web]
repo = https://github.com/OCA/web.git
branch = ${odoo:version}
commit = abcdef1
```

> **Note**
> when `commit` is set, `shallow` is ignored automatically, because a shallow clone may not contain the requested commit.

After changing the project file, run `odt-env` again to update the workspace:

```bash
odt-env odoo-project.ini --sync-all --create-venv
```

#### 2.5. Update database and run Odoo

Once the workspace has been updated, refresh installed modules:

```bash
./odoo-scripts/update.sh
```

Then start Odoo:

```bash
./odoo-scripts/run.sh
```

#### 2.6. Optional: Use an existing local Odoo checkout

If you already maintain the Odoo source code locally, point `[odoo]` to that directory with `path`.

```ini
[odoo]
version = 18.0
path = ../odoo
```

Relative paths are resolved relative to `ROOT/`. When `path` is set, `odt-env` uses that directory as the Odoo source for dependency collection, editable installation, generated helper scripts, and generated `addons_path`. Git sync for Odoo is skipped, including when `--sync-odoo` or `--sync-all` is used.

Use `path` only with `version`; do not combine it with `repo`, `branch`, `commit`, or `shallow`.

---

### 3. Using system Python instead of managed Python

By default, `odt-env` uses `uv` to install and manage the requested Python version.

If you already have a suitable system Python installed, you can disable managed Python.

#### 3.1. Update the project file

Disable managed Python by adding `python_version = 3.11` and `managed_python = false` to the `odoo-project.ini` file.

> **Note**
> Set `python_version` to the Python version you want to use from your local system.
> In the example below, 3.11 is only illustrative.

```ini
[virtualenv]
managed_python = false
python_version = 3.11

[odoo]
version = 18.0

[config]
db_host = 127.0.0.1
db_name = odoo
db_user = odoo
db_password = odoo
```

#### 3.2. Update the workspace

After changing the project file, run `odt-env` again to update the workspace:

```bash
odt-env odoo-project.ini --sync-all --create-venv
```

This recreates the virtual environment at `ROOT/venv` using the system Python.

---

### 4. Managing Python requirements

The `[virtualenv]` section can be used to add new Python packages, pin specific versions, and override packages collected from Odoo or addon repository `requirements.txt` files.

Use:

- `requirements` to add extra packages or pin an explicit version
- `requirements_ignore` to skip packages that would otherwise be collected from repository requirements files

When a package is listed in `requirements`, `odt-env` automatically gives that package priority by ignoring the same package name from collected repository requirements. This means you can usually pin a package version just by adding it to `requirements`.

#### 4.1. Add or pin packages

Use `requirements` to install additional packages or to force a specific version:

```ini
[virtualenv]
requirements =
  requests==2.32.3
  boto3==1.35.99

[odoo]
version = 18.0

[config]
db_host = 127.0.0.1
db_name = odoo
db_user = odoo
db_password = odoo
```

In this example, both packages are added to the virtual environment and pinned to the specified versions.

#### 4.2. Override a package with a different one

If you want to replace a package with a different distribution name, add the replacement to `requirements` and skip the original package with `requirements_ignore`.

Example:

```ini
[virtualenv]
requirements =
  psycopg2-binary==2.9.9
requirements_ignore =
  psycopg2

[odoo]
version = 18.0

[config]
db_host = 127.0.0.1
db_name = odoo
db_user = odoo
db_password = odoo
```

In this example, `odt-env` installs `psycopg2-binary==2.9.9` and skips `psycopg2` when collecting repository requirements.

---

### 5. Simple offline deployment using a prebuilt wheelhouse

This example shows a simple deployment workflow:

1. On an internet-connected build machine, prepare the workspace and build the wheelhouse.
2. Copy the prepared workspace to the target machine.
3. On the target machine, recreate the virtual environment strictly offline from the existing wheelhouse.

#### 5.1. Prepare the workspace on the build machine

On the build machine, run `odt-env` normally:

```bash
odt-env odoo-project.ini --sync-all --create-venv
```

This syncs Odoo and addon repositories, resolves and locks Python dependencies, and builds `ROOT/wheelhouse/` for offline installation.

After that, transfer the prepared workspace to the target machine. The simplest approach is to copy the entire `ROOT/` directory.

#### 5.2. Recreate the virtual environment on the target machine

On the target machine, run:

```bash
odt-env /path/to/odoo-project.ini --create-venv-from-wheelhouse
```

This recreates `ROOT/venv`, skips lock compilation and wheelhouse build, and performs a strict offline install from the existing `ROOT/wheelhouse/`.

This is useful for simple deployments where Python dependencies are prepared on a connected build machine, while the target machine creates the virtual environment without internet access.

---

### 6. Building custom Docker image

This example shows how `odt-env` builds a custom image by extending the standard [`Odoo Docker`](https://hub.docker.com/_/odoo/) images.

#### 6.1. Create a project file

Create a sample project file that demonstrates how to extend the [`Odoo Docker`](https://hub.docker.com/_/odoo/) image with extra addons.

```ini
[odoo]
version = 18.0

[docker]
target_image = mycompany/odoo:${odoo:version}

# Optional: publish Odoo on custom host ports in the generated compose.yml.
# These values are used as host-side Docker Compose ports only; Odoo inside
# the container keeps the standard Odoo Docker ports 8069 and 8072.
[config]
http_port = 18169
gevent_port = 18172

[addons.oca-web]
repo = https://github.com/OCA/web.git
branch = ${odoo:version}

[addons.oca-helpdesk]
repo = https://github.com/OCA/helpdesk.git
branch = ${odoo:version}
```

#### 6.2. Build the image

Run `odt-env` against the project file:

```bash
odt-env odoo-project.ini --sync-addons --build-docker-image
```

After the command completes, the new Docker image is available under the name configured by `[docker].target_image`.

`odt-env` also generates a sample `ROOT/compose.yml` file:

```yaml
name: mycompany-odoo-18.0

services:
  db:
    image: postgres:16
    restart: unless-stopped
    environment:
      POSTGRES_DB: postgres
      POSTGRES_USER: odoo
      POSTGRES_PASSWORD: odoo
    volumes:
      - odoo-db-data:/var/lib/postgresql/data

  odoo:
    image: mycompany/odoo:18.0
    restart: unless-stopped
    depends_on:
      - db
    ports:
      - "18169:8069"
      - "18172:8072"
    environment:
      HOST: db
      PORT: 5432
      USER: odoo
      PASSWORD: odoo
    volumes:
      - ./odoo-docker/configs:/etc/odoo:ro
      - odoo-data:/var/lib/odoo

volumes:
  odoo-db-data:
  odoo-data:
```

#### 6.3. Use the image with Docker Compose

The custom image can be used in the same way as a regular Odoo image.

Start the PostgreSQL service first:

```bash
docker compose up -d db
```

Initialize the Odoo database and install the initial modules.
This step uses the standard Odoo CLI and should be run before starting the long-running Odoo service for the first time:

```bash
docker compose run --rm odoo \
  -- \
  -c /etc/odoo/odoo.conf \
  -d odoo \
  -i helpdesk_mgmt,web \
  --without-demo=all \
  --stop-after-init
```

Replace `helpdesk_mgmt,web` with the modules that should be installed during the initial database bootstrap.

After the database has been initialized, start the Odoo service:

```bash
docker compose up -d odoo
```

If the custom image is rebuilt under the same tag, recreate the Odoo service so the running container uses the new image:

```bash
docker compose up -d --force-recreate odoo
```

---

## Command-line reference

### Syntax

```text
odt-env [OPTIONS] INI
```

If no options are specified, `odt-env` only regenerates configuration files and helper scripts.

### Positional arguments

- `INI` — required project file. This can be either:
  - a local filesystem path, for example `odoo-project.ini` or `/path/to/odoo-project.ini`
  - a Git-backed INI source, for example `git::https://github.com/lck/odoo-devops-tools.git//examples/odoo18-minimal.ini?ref=main`

Git-backed INI sources use this syntax:

```text
git::REPO_URL//PATH/TO/PROJECT.ini?ref=REF
```

The `?ref=REF` part is optional and can point to a branch, tag, or commit.

### Paths and outputs

- `--root ROOT` — workspace root directory. Default: the directory containing a local INI file, or the current working directory for a Git-backed INI.
- `-e KEY=VALUE`, `--extra-var KEY=VALUE` — override or inject a value in the optional `[vars]` section; can be repeated
- `--no-configs` — do not generate config files
- `--no-scripts` — do not generate helper scripts under `ROOT/odoo-scripts/`
- `--no-data-dir` — do not create the Odoo data directory

### Repository sync

- `--sync-odoo` — sync only the Git-managed Odoo source; when `[odoo].path` is used, the local path is reused and Git sync is skipped
- `--sync-addons` — sync only `ROOT/odoo-addons/*`
- `--sync-all` — sync both Odoo and addons

> **Note**
> If any target repository contains local uncommitted changes, `odt-env` aborts the sync operation.
> Commit, stash, or discard the changes before running a sync command.

### Python, virtual environment, and wheelhouse

- `--create-venv` — recreate `ROOT/venv` and refresh the wheelhouse; if `ROOT/venv` already exists, it is deleted and created again
- `--create-venv-from-wheelhouse` — recreate `ROOT/venv` from an existing `ROOT/wheelhouse/` and `all-requirements.lock.txt`, install strictly offline, and skip lock compilation and wheelhouse build
- `--clear-pip-wheel-cache` — remove all items from pip's wheel cache

### Docker image generation

- `--build-docker-image` — generate `ROOT/odoo-docker/`, generate and overwrite `ROOT/compose.yml`, and build the image configured by `[docker].target_image` by extending `[docker].base_image` (default: `odoo:${odoo:version}`)

### Other options

- `--version` — show the installed `odt-env` version and exit

---

## Project file reference

The `odt-env` project file is an INI file that describes the Odoo workspace to create.

At minimum, the project file must contain this section:

- `[odoo]`

The following sections are supported:

- `[vars]` — optional reusable variables for INI interpolation
- `[virtualenv]` — optional Python and dependency settings
- `[odoo]` — required Odoo source settings
- `[addons.<name>]` — optional addon sources
- `[docker]` — optional Docker image and Compose generation settings
- `[config]` — optional Odoo server configuration values

### General rules

- The project file can have any filename. In this README, `odoo-project.ini` is only an example.
- INI interpolation is supported, so values such as `${odoo:version}` can be reused across sections.
- The optional `[vars]` section is useful for reusable values referenced as `${vars:name}`.
- Values from `[vars]` can be overridden from the CLI with `-e name=value` / `--extra-var name=value`.
- Multi-line values are used for lists such as `requirements`, `build_constraints`, and `requirements_ignore`.

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
odt-env odoo-project.ini --sync-all --create-venv -e branch=dev -e db=odoo_dev
```

### `[virtualenv]`

This section is optional.

- `python_version` — Python version for the virtual environment. If omitted, `odt-env` chooses a default version based on the selected Odoo version.
- `managed_python` — whether `uv` should install and manage Python automatically. Default: `true`.
- `requirements` — additional Python requirements to install. Multi-line list.
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
- git repository: `repo` + `branch` (+ optional `commit` and `shallow`)

Rules:

- For a local addon, use only `path`.
- For a git addon, `repo` and `branch` are required.
- `commit` is optional for a git addon. When set, the repository is pinned to that exact revision.
- `shallow` is optional for git addons and defaults to `true`. It is ignored when `commit` is set.
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

It controls values used when `odt-env` builds a Docker image and generates `ROOT/compose.yml` together with `--build-docker-image`.

- `target_image` — Docker image name/tag to build and write into the generated Compose file. Required when `--build-docker-image` is used.
- `base_image` — Docker image name/tag used as the `FROM` image in the generated Dockerfile. Default: `odoo:${odoo:version}`.
- `compose_project_name` — optional top-level Docker Compose project name. If omitted, Docker Compose uses its normal project name resolution, usually the directory name.
- `odoo_service` — Docker Compose service name for Odoo. Default: `odoo`.
- `db_service` — Docker Compose service name for PostgreSQL. Default: `db`.

In most cases, set `target_image` and optionally customize `compose_project_name`; keep the service names at their defaults. Docker Compose uses the project name to namespace resources such as containers, networks, and named volumes, while short service names keep commands and internal hostnames simple.

Example:

```ini
[odoo]
version = 18.0

[docker]
target_image = mycompany/odoo:${odoo:version}
```

### `[config]`

This section is optional.

When present, it contains Odoo server configuration values written into `ROOT/odoo-configs/odoo-server.conf`.
When omitted, `odt-env` still generates a valid config file with generated values such as `addons_path` and `data_dir`.

You can define standard Odoo configuration options here.

Special rules:

- `addons_path` must not be set in `[config]`. `odt-env` always generates it automatically.
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

---

## Script reference

All generated scripts are available in both Unix (`.sh`) and Windows (`.bat`) variants.
The examples below use the Unix form.

### run

Starts Odoo in the foreground.

Any extra arguments are forwarded to the underlying command `odoo-bin`.

Examples:

```bash
./odoo-scripts/run.sh
./odoo-scripts/run.sh --dev=all
./odoo-scripts/run.sh -i sale,crm --without-demo=all
```

### instance

Manages Odoo as a background service on Unix-like systems.

Logs are written to `ROOT/odoo-logs/odoo-server.log` and the PID is stored in `ROOT/odoo-logs/odoo-server.pid`.

Examples:

```bash
./odoo-scripts/instance.sh start
./odoo-scripts/instance.sh stop
./odoo-scripts/instance.sh restart
./odoo-scripts/instance.sh status
```

### test

Runs Odoo tests.

The script always adds `--test-enable --stop-after-init`.

Any extra arguments are forwarded to the underlying command `odoo-bin`.

Examples:

```bash
./odoo-scripts/test.sh
./odoo-scripts/test.sh -i sale --test-tags /sale
```

### shell

Opens an Odoo shell.

Examples:

```bash
./odoo-scripts/shell.sh
```

### backup

Creates a timestamped ZIP backup under `ROOT/odoo-backups/`.

Any extra arguments are forwarded to the underlying command `click-odoo-backupdb` from [`click-odoo-contrib`](https://pypi.org/project/click-odoo-contrib/#click-odoo-backupdb-beta) package.

Examples:

```bash
./odoo-scripts/backup.sh
```

### restore

Restores a backup into the configured database.

The script always adds `--copy --neutralize`.

Any extra arguments are forwarded to the underlying command `click-odoo-restoredb` from [`click-odoo-contrib`](https://pypi.org/project/click-odoo-contrib/#click-odoo-restoredb-beta) package.

Examples:

```bash
./odoo-scripts/restore.sh ./odoo-backups/odoo_20260331_221443.zip
./odoo-scripts/restore.sh ./odoo-backups/odoo_20260331_221443.zip --force
```

### update

Updates an Odoo database automatically detecting addons to update based on a hash of their file content.

Any extra arguments are forwarded to the underlying command `click-odoo-update` from [`click-odoo-contrib`](https://pypi.org/project/click-odoo-contrib/#click-odoo-update-stable) package.

Examples:

```bash
./odoo-scripts/update.sh
./odoo-scripts/update.sh --update-all
```
