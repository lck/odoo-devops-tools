# odoo-devops-tools

**Generate reproducible Odoo workspaces.**

`odoo-devops-tools` is a toolkit for Odoo development and deployment workflows.  
Its main command, **`odt-env`**, reads a project definition and generates a reproducible workspace:

```text
ROOT/
├── odoo/                 # Odoo source
├── odoo-addons/          # Git or local addon sources
├── odoo-configs/         # generated Odoo configuration
├── odoo-scripts/         # run, test, shell, backup, restore, update
├── odoo-data/            # Odoo data directory
├── odoo-logs/            # runtime logs
├── odoo-backups/         # backups created by helper scripts
├── docker/               # generated Docker artifacts
│   ├── local/            # local-development build context
│   └── deploy/           # self-contained deploy build context, when requested
├── wheelhouse/           # offline Python wheelhouse
├── venv/                 # Python virtual environment
├── dist/                 # portable workspace bundles, when requested
├── compose.yaml          # local-development Docker Compose file
├── odoo-project.ini      # project definition
└── .odt-env/             # provisioning metadata and project snapshots
```

Use it when you want a repeatable Odoo setup with custom or third-party addons, onboarding new developers,
offline installs, or lightweight deployment artifacts that can be used directly or integrated into existing CI/CD pipelines.

---

## System requirements

- **git**: https://git-scm.com/install/
- **uv**: https://docs.astral.sh/uv/getting-started/installation/
- **Docker**: https://docs.docker.com/get-docker/ — optional, needed only for Docker image and Compose workflows.

---

## Installation

Install the CLI with `uv`:

```bash
uv tool install -U odoo-devops-tools
```

Verify the installation:

```bash
odt-env --help
```

---

## Quick start

Create `odoo-project.ini` from the default template and provision a new Odoo 18 workspace in one step:

```bash
odt-env --init-project --root ./odoo18-workspace --sync-all --create-venv \
  --set odoo:version=18.0 \
  --set config:db_host=127.0.0.1 \
  --set config:db_name=odoo \
  --set config:db_user=odoo \
  --set config:db_password=odoo
```

> **Note**
> Make sure PostgreSQL is running at the configured host and the configured database user exists.

Start Odoo with the generated script:

```bash
cd odoo18-workspace
./odoo-scripts/run.sh
```

Odoo starts with the generated configuration from `./odoo-configs/odoo-server.conf` and is available at http://localhost:8069.

---

## Usage

The examples below continue from the workspace created in the Quick start section.

The generated `odoo-project.ini` is the main project definition:

```ini
[virtualenv]
managed_python = true
python_version =
build_constraints =
requirements =
requirements_ignore =

[odoo]
version = 18.0
repo = https://github.com/odoo/odoo.git
branch = 18.0
commit =
shallow = true

[docker]
base_image = odoo:18.0
compose_project_name =
db_service = db
odoo_service = odoo

[config]
db_host = 127.0.0.1
db_name = odoo
db_user = odoo
db_password = odoo
```

Edit this file when you want to add extra addons, change configuration values, pin repositories or adjust Python dependency handling.

### 1. Adding extra addons

To extend Odoo with additional functionality, add extra addons through `[addons.<name>]` sections in `odoo-project.ini`.

In this example, we add two Git-based addon repositories, `OCA/web` and `OCA/helpdesk`.

#### 1.1. Update the project file

Edit `odoo-project.ini` in the workspace root and add these addon sections:

```ini
[addons.oca-web]
repo = https://github.com/OCA/web.git
branch = ${odoo:version}

[addons.oca-helpdesk]
repo = https://github.com/OCA/helpdesk.git
branch = ${odoo:version}
```

The rest of the generated project file can stay unchanged.

#### 1.2. Update the workspace

After changing the project file, run `odt-env` again from the workspace root:

```bash
odt-env --sync-all --create-venv
```

This clones the addons into `ROOT/odoo-addons/oca-web/` and `ROOT/odoo-addons/oca-helpdesk/`.

Addon directories are then added to the generated `addons_path`.

If any of these addon sources contains a `requirements.txt` file, `odt-env` automatically installs the listed dependencies into the Python virtual environment.

#### 1.3. Install modules and run Odoo

After updating the workspace, start Odoo with modules from the newly added addon repositories:

```bash
./odoo-scripts/run.sh -i web_notify,helpdesk_mgmt
```

---

### 2. Using system Python instead of managed Python

By default, `odt-env` uses `uv` to install and manage the requested Python version.

If you already have a suitable system Python installed, you can disable managed Python.

#### 2.1. Update the project file

Disable managed Python by adding `python_version = 3.11` and `managed_python = false` to the `odoo-project.ini` file.

> **Note**
> Set `python_version` to the Python version you want to use from your local system.
> In the example below, 3.11 is only illustrative.

```ini
[virtualenv]
managed_python = false
python_version = 3.11
```

#### 2.2. Update the workspace

After changing the project file, run `odt-env` again from the workspace root:

```bash
odt-env --sync-all --create-venv
```

This recreates the virtual environment at `ROOT/venv` using the system Python.

---

### 3. Managing Python requirements

The `[virtualenv]` section can be used to add new Python packages, pin specific versions, and override packages collected from Odoo or addon repository `requirements.txt` files.

Use:

- `requirements` to add extra packages or pin an explicit version
- `requirements_ignore` to skip packages that would otherwise be collected from repository requirements files

When a package is listed in `requirements`, `odt-env` automatically gives that package priority by ignoring the same package name from collected repository requirements. This means you can usually pin a package version just by adding it to `requirements`.

#### 3.1. Add or pin packages

Use `requirements` to install additional packages or to force a specific version:

```ini
[virtualenv]
requirements =
  requests==2.32.3
  boto3==1.35.99
```

In this example, both packages are added to the virtual environment and pinned to the specified versions.

#### 3.2. Override a package with a different one

If you want to replace a package with a different distribution name, add the replacement to `requirements` and skip the original package with `requirements_ignore`.

Example:

```ini
[virtualenv]
requirements =
  psycopg2-binary==2.9.9
requirements_ignore =
  psycopg2
```

In this example, `odt-env` installs `psycopg2-binary==2.9.9` and skips `psycopg2` when collecting repository requirements.

---

### 4. Creating portable workspace bundles

A portable workspace bundle is a ZIP archive containing everything needed to recreate an Odoo workspace without cloning repositories or downloading Python packages:

```text
odoo/
odoo-addons/
wheelhouse/
manifest.json
odoo-project.ini
```

The bundle does not contain the virtual environment, database data, logs, backups, generated scripts, generated configuration files, Docker artifacts, Git metadata, or provisioning history.
Those machine-specific outputs are recreated on the target machine.

#### 4.1. Create a bundle on the build machine

On an internet-connected build machine, sync the sources, build the wheelhouse, and create the bundle in one command:

```bash
odt-env --sync-all --create-venv --create-bundle
```

When no output path is supplied, the bundle is written to:

```text
ROOT/dist/ROOT-NAME.odt.zip
```

You can also select an explicit output file:

```bash
odt-env --sync-all --create-venv --create-bundle ./artifacts/odoo18-production.odt.zip
```

#### 4.1.1. Including uncommitted changes

By default, bundle creation stops when a bundled Git repository has uncommitted changes.

To intentionally include those changes in the bundle, use:

```bash
odt-env --sync-all --create-venv --create-bundle --allow-dirty-bundle
```

#### 4.2. Create the workspace on the target machine

Copy the ZIP to the target machine and import it into an empty directory:

```bash
odt-env --create-from-bundle ./odoo18-production.odt.zip \
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
6. regenerates configuration files and helper scripts.

`ROOT` must be empty. If `--root` is omitted, the current working directory is used and must be empty. The imported manifest is saved as `ROOT/.odt-env/imported-bundle-manifest.json`.

> **Compatibility note**
> A wheelhouse is platform- and architecture-dependent. Create and import a bundle on compatible systems, for example Linux x86-64 to Linux x86-64. The target machine must have `uv` and access to the configured Python version. When `[virtualenv].managed_python = true`, `uv` may still need network access if that Python interpreter is not already installed or cached. For a fully disconnected target, install the required Python interpreter beforehand or use `managed_python = false`.

#### 4.3. Manual wheelhouse workflow

The existing manual workflow remains available. After preparing a complete workspace on the build machine, copy the whole workspace and run this command from the copied root:

```bash
odt-env --create-venv-from-wheelhouse
```

This recreates `ROOT/venv`, skips dependency compilation and wheelhouse building, and installs strictly from the existing `ROOT/wheelhouse/` and `all-requirements.lock.txt`.

### 5. Local Docker development and deploy contexts

`odt-env` generates Docker artifacts for two distinct workflows:

- `ROOT/docker/local/` is the local-development build context. It is generated by default during normal online workspace provisioning, together with `ROOT/compose.yaml`.
- `ROOT/docker/deploy/` is a self-contained deployment build context. It is generated only when `--create-docker-deploy` is requested.

The local context never copies addon source code into the image. Addon directories are bind-mounted from the workspace so edits on the host are visible inside the running Odoo container. The deploy context does the opposite: it stages addon modules into the build context so the resulting image is self-contained.

#### 5.1. Start a local Docker development workspace

For example, create an Odoo 19 workspace and sync its configured addons:

```bash
odt-env --init-project --root ./odoo19 --sync-addons
```

This generates the usual workspace outputs plus:

```text
odoo19/
├── docker/
│   └── local/
│       ├── Dockerfile
│       ├── .dockerignore
│       ├── requirements/
│       └── configs/
│           └── odoo.conf
└── compose.yaml
```

Start PostgreSQL and Odoo directly with Docker Compose:

```bash
cd ./odoo19
docker compose up
```

The `odoo` service uses `build.context: ./docker/local`, so Compose builds the local image automatically when needed. No separate `odt-env` Docker build command is required.

Configured addon sources are mounted into the container under `/mnt/extra-addons/`. For example:

```yaml
services:
  odoo:
    build:
      context: ./docker/local
    volumes:
      - ./docker/local/configs:/etc/odoo:ro
      - type: bind
        source: ./odoo-addons/oca-web
        target: /mnt/extra-addons/oca-web
      - type: bind
        source: ./odoo-addons/oca-helpdesk
        target: /mnt/extra-addons/oca-helpdesk
      - odoo-data:/var/lib/odoo
```

Changes to addon source files on the host are therefore immediately visible inside the running container. Odoo may still need a process restart or a module update depending on the type of change.

If an addon `requirements.txt`, `[virtualenv].requirements`, Docker base image, or another image-level dependency changes, rerun `odt-env` to refresh `docker/local/` and then rebuild through Compose:

```bash
odt-env --sync-addons
docker compose up --build
```

Local database connection options in `[config]`, such as `db_host = 127.0.0.1`, are for the non-Docker workspace. They are intentionally not copied into `docker/local/configs/odoo.conf`; the local container connects to the generated Compose database service instead.

#### 5.2. Skip local Docker generation

Local Docker artifacts are generated by default during normal online provisioning, similarly to config files and helper scripts. Disable them explicitly when they are not wanted:

```bash
odt-env --sync-all --create-venv --no-local-docker
```

`--no-local-docker` skips regeneration of `ROOT/docker/local/` and `ROOT/compose.yaml`. Existing files are left in place.

#### 5.3. Generate a deploy Docker build context

Use `--create-docker-deploy` when you need a self-contained image context for CI/CD, testing, staging, production, or another non-local deployment workflow:

```bash
odt-env --sync-addons --create-docker-deploy
```

This additionally creates:

```text
ROOT/docker/deploy/
├── Dockerfile
├── .dockerignore
├── addons/
└── requirements/
```

Addon modules are staged under `docker/deploy/addons/` and copied into `/mnt/extra-addons/` by the generated Dockerfile. Runtime Compose configuration is not generated for the deploy context.

`odt-env` does not build or push the deploy image. Build it with Docker or your CI/CD system, for example:

```bash
docker build -t mycompany/odoo:19.0 docker/deploy
```

`[docker].base_image` controls the base image used by both local and deploy Dockerfiles.

In CI, where the local development context is unnecessary, combine the options:

```bash
odt-env --sync-addons --create-docker-deploy --no-local-docker
docker build -t mycompany/odoo:19.0 docker/deploy
```

---

## Command-line reference

### Syntax

```text
odt-env [INI] [OPTIONS]
```

If no arguments are specified, `odt-env` prints help and exits.

### Positional arguments

- `INI` — project definition file. It can be:

  - a local filesystem path, for example:

    ```bash
    odt-env /path/to/odoo-project.ini --sync-all --create-venv
    ```

  - a remote INI loaded from a Git repository, for example:

    ```bash
    odt-env 'git+https://github.com/lck/odoo-devops-tools.git//examples/odoo-project.ini?ref=main' --sync-all --create-venv
    odt-env 'git+git@github.com:company/repo.git//examples/odoo-project.ini?ref=main' --sync-all --create-venv
    ```

    Syntax:

    ```text
    git+REPO_URL//PATH/TO/PROJECT.ini?ref=REF
    ```

  - a remote INI loaded from a URL, for example:

    ```bash
    odt-env 'https://github.com/lck/odoo-devops-tools/blob/main/examples/odoo-project.ini' --sync-all --create-venv
    odt-env 'https://raw.githubusercontent.com/lck/odoo-devops-tools/main/examples/odoo-project.ini' --sync-all --create-venv
    ```

#### Default project file convention

If no positional `INI` file is provided and no `-i/--include` option is used, `odt-env` looks for `ROOT/odoo-project.ini`.
This is similar to how Docker Compose uses `compose.yaml` by convention.

For example, this command:

```bash
odt-env --root ./existing-workspace --sync-all --create-venv
```

is equivalent to passing the default project file explicitly:

```bash
odt-env ./existing-workspace/odoo-project.ini --sync-all --create-venv
```

#### INI includes

Use `-i INI` / `--include INI` to include additional project layers. The option can be repeated.

```bash
odt-env odoo-project.ini -i local-overrides.ini -i extra-addons.ini --sync-all --create-venv
```

Project layers are processed from left to right. Later layers override earlier layers.
Validation is performed only after all layers have been merged.
The merged project file is saved as `ROOT/odoo-project.ini`.

### Paths and outputs

- `--root ROOT` — workspace root directory. Default: the directory containing a local INI file, or the current working directory for a remote INI or omitted INI. In include mode, the default is the directory of the first local source, or the current working directory when the first source is remote.
- `--init-project` — create `ROOT/odoo-project.ini` from the bundled default template if it does not already exist. This is only valid when `INI` is omitted and no `-i/--include` is provided. Existing project files are not overwritten.
- `--include INI`, `-i INI` — include an additional project INI layer; can be repeated. Later layers override earlier layers.
- `--extra-var KEY=VALUE`, `-e KEY=VALUE` — override or inject a value in the optional `[vars]` section; can be repeated
- `--set SECTION:KEY=VALUE`, `-S SECTION:KEY=VALUE` — override a value that is already present in the INI file; can be repeated. New options are allowed only in the `[config]` section.
- `--no-configs` — do not generate config files
- `--no-scripts` — do not generate helper scripts under `ROOT/odoo-scripts/`
- `--no-data-dir` — do not create the Odoo data directory
- `--no-provisioning-log` — do not write provisioning metadata under `ROOT/.odt-env/`
- `--show-last-run` — print metadata from `ROOT/.odt-env/last-provisioning.json` and exit without provisioning

### Repository sync

- `--sync-odoo` — sync only the Git-managed Odoo source; when `[odoo].path` is used, the local path is reused and Git sync is skipped
- `--sync-addons` — sync only `ROOT/odoo-addons/*`
- `--sync-all` — sync both Odoo and addons

> **Note**
> If any target repository contains local uncommitted changes, `odt-env` aborts the sync operation.
> Commit, stash, or discard the changes before running a sync command.

### Python, virtual environment, and wheelhouse

Online virtual environment provisioning:

- `--create-venv` — recreate `ROOT/venv` and refresh the wheelhouse; if `ROOT/venv` already exists, it is deleted and created again

Offline deployment from a prebuilt wheelhouse:

- `--create-venv-from-wheelhouse` — recreate `ROOT/venv` from an existing `ROOT/wheelhouse/` and `all-requirements.lock.txt`, install strictly offline, and skip lock compilation and wheelhouse build. This is useful after preparing dependencies on an internet-connected build machine and copying the workspace to a target machine without internet access.

Maintenance:

- `--clear-pip-wheel-cache` — remove all items from pip's wheel cache

### Portable workspace bundles

- `--create-bundle [BUNDLE]` — create a verified portable ZIP containing Odoo sources, configured addon sources, a sanitized `odoo-project.ini`, and `ROOT/wheelhouse/`. If `BUNDLE` is omitted, the output is `ROOT/dist/ROOT-NAME.odt.zip`. Relative explicit output paths are resolved from the current working directory.
- `--allow-dirty-bundle` — allow `--create-bundle` to snapshot Git repositories with uncommitted changes. Without this option, dirty repositories abort bundle creation.
- `--create-from-bundle BUNDLE` — verify and extract a portable bundle into an empty `ROOT`, then recreate `ROOT/venv` using the bundled wheelhouse. This offline deployment path intentionally skips local Docker generation; `--create-docker-deploy` cannot be combined with it.

### Docker generation

- Local Docker generation is enabled by default. It regenerates `ROOT/docker/local/` and `ROOT/compose.yaml`; addon sources are bind-mounted for development.
- `--no-local-docker` — skip regeneration of `ROOT/docker/local/` and `ROOT/compose.yaml`. Existing files are not deleted.
- `--create-docker-deploy` — generate a self-contained deployment build context under `ROOT/docker/deploy/`. Addon modules are staged into the context.

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
- `[docker]` — optional local/deploy Docker generation settings
- `[config]` — optional Odoo server configuration values

### General rules

- The project file can have any filename when passed explicitly. When `INI` is omitted, `odt-env` uses the existing `ROOT/odoo-project.ini`; if it is missing, use `--init-project` to create it explicitly from the bundled default template. Remote INI sources and merged include layers are materialized as `ROOT/odoo-project.ini`.
- INI interpolation is supported, so values such as `${odoo:version}` can be reused across sections.
- Multiple INI layers can be composed with `-i/--include`. Later layers override earlier layers; multi-line values are replaced as whole option values, not appended.
- The optional `[vars]` section is useful for reusable values referenced as `${vars:name}`.
- Values from `[vars]` can be overridden or injected from the CLI with `-e name=value` / `--extra-var name=value`.
- Values that already exist in the INI file can be overridden directly with `-S section:key=value` / `--set section:key=value`.
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

- `base_image` — Docker image used as the base image in both generated Dockerfiles. Default: `odoo:${odoo:version}`.
- `compose_project_name` — optional top-level name written to the local-development `ROOT/compose.yaml`.
- `odoo_service` — Docker Compose service name for Odoo. Default: `odoo`.
- `db_service` — Docker Compose service name for PostgreSQL. Default: `db`.

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
