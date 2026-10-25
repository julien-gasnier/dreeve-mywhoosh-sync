# dreeve-mywhoosh-sync

`dreeve-mywhoosh-sync` automatically downloads your new [MyWhoosh](https://www.mywhoosh.com/) activities and drops them into the `watch/` directory of [Dreeve](https://github.com/robiningelbrecht/dreeve) when running in `IMPORT_MODE=files`. Dreeve will import them automatically.

> **Note:** This tool uses MyWhoosh's unofficial API, which may change without notice.

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) and [Docker Compose](https://docs.docker.com/compose/)
- A working Dreeve instance with `IMPORT_MODE=files` and a shared `watch/` directory
- A MyWhoosh account with email and password

## Installation (step by step)

### 1) Clone this extension into your Dreeve installation

From the root directory of your Dreeve installation (the one containing `docker-compose.yml`):

```bash
git clone https://github.com/julien-gasnier/dreeve-mywhoosh-sync.git
```

### 2) Configure environment variables

Copy the example file and fill in your credentials:

```bash
cp dreeve-mywhoosh-sync/.env.example .env.dreeve-mywhoosh-sync
# Edit .env.dreeve-mywhoosh-sync with your MyWhoosh credentials
```

If your account was created with Google or Apple, set a password in your MyWhoosh account settings first.

### 3) Add the service to `docker-compose.yml`

Add this service to your existing `docker-compose.yml`:

```yaml
  dreeve-mywhoosh-sync:
    build:
      context: ./dreeve-mywhoosh-sync
    container_name: dreeve-mywhoosh-sync
    restart: unless-stopped
    volumes:
      - ./watch:/watch
      - ./dreeve-mywhoosh-sync/state:/state
    env_file: ./.env.dreeve-mywhoosh-sync
    environment:
      - WATCH_DIR=/watch
      - STATE_DIR=/state
    networks:
      - dreeve-network
```

If you'd rather use your main `.env` file, replace `env_file: ./.env.dreeve-mywhoosh-sync` with `env_file: ./.env`.

**Optional (recommended to avoid duplicates):** To detect activities already imported into Dreeve, mount the database read-only and set `DREEVE_DB`:

```yaml
  dreeve-mywhoosh-sync:
    build:
      context: ./dreeve-mywhoosh-sync
    container_name: dreeve-mywhoosh-sync
    restart: unless-stopped
    volumes:
      - ./watch:/watch
      - ./dreeve-mywhoosh-sync/state:/state
      - ./storage/database:/db:ro
    env_file: ./.env.dreeve-mywhoosh-sync
    environment:
      - WATCH_DIR=/watch
      - STATE_DIR=/state
      - DREEVE_DB=/db/strava.db
    networks:
      - dreeve-network
```

### 4) Start the service

```bash
docker compose up -d --build dreeve-mywhoosh-sync
```

### 5) Verify it's working

Check login and list your recent activities:

```bash
docker compose run --rm dreeve-mywhoosh-sync --check
```

Follow logs:

```bash
docker compose logs -f dreeve-mywhoosh-sync
```

Dry run first (recommended):

```bash
docker compose run --rm dreeve-mywhoosh-sync --once --dry-run
```

Manual sync:

```bash
docker compose run --rm dreeve-mywhoosh-sync --once
```

## Duplicate prevention

- **File already in watch/**: skips if `mywhoosh_<activityFileId>.*` already exists in `watch/`
- **Local state**: tracks successfully downloaded activities in `state/synced.json`
- **Dreeve database check (optional)**: if `DREEVE_DB` points to a read-only `storage/database/strava.db`, it skips activities already imported. Uses time+distance matching with up to 3h tolerance to handle UTC/local time differences from older imports
- **Explicit exclusion**: add `activityFileId`s to `MYWHOOSH_IGNORED_ACTIVITY_FILE_IDS` to skip them permanently

## Configuration

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `MYWHOOSH_USERNAME` | Yes | - | Your MyWhoosh account email |
| `MYWHOOSH_PASSWORD` | Yes | - | Your MyWhoosh account password |
| `MYWHOOSH_ACCESS_TOKEN` | No | - | Access token (saved automatically after login) |
| `MYWHOOSH_MAX_PAGES` | No | `0` | Number of pages to fetch (`0` = all pages) |
| `MYWHOOSH_SYNC_INTERVAL_SECONDS` | No | `900` | Sync interval in seconds |
| `DREEVE_DB` | No | `/db/strava.db` | Path to read-only Dreeve database (optional, only if mounted) |
| `MYWHOOSH_IGNORED_ACTIVITY_FILE_IDS` | No | - | Comma-separated `activityFileId`s to skip permanently |
| `WATCH_DIR` | No | `/watch` | Target directory for downloaded files |
| `STATE_DIR` | No | `/state` | Directory for local sync state |

## Tips

- Files dropped in `watch/` are imported by Dreeve every ~5 minutes. **Do not delete them manually** — Dreeve consumes them.
- Run with `--dry-run` first to see what would be downloaded.
- The database mount is **not required**. Without it, the script relies on local state and files in `watch/`.

## License

<a href="https://www.buymeacoffee.com/juliengasnb" target="_blank"><img src="https://cdn.buymeacoffee.com/buttons/v2/default-violet.png" alt="Buy Me a Coffee" style="height: 60px !important;width: 217px !important;" ></a>
