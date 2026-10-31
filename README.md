# netatmo-client-influx



Reads your Netatmo Weather Station through the Netatmo API and writes every reading
to **InfluxDB 1.x**.



![netatmo-influx-datenfluss.svg](./netatmo-influx-datenfluss.svg)



Netatmo Weather Station dashboard for Grafana

![Screenshot](./screenshot.png)



This is a fork of [karaktaka/netatmo-client-influx](https://github.com/karaktaka/netatmo-client-influx),
which targets InfluxDB 2.x. This fork writes to InfluxDB 1.x through the `influxdb` Python client instead.

## How it works

The client runs as a long-lived process. Every `interval` seconds it

1. refreshes the OAuth2 access token when needed (stored in a token file),
2. calls `getstationsdata` for all stations of your account,
3. writes one point per sensor value to InfluxDB.

It is built to keep running unattended:

- A failed write (InfluxDB down, database missing, …) is logged and retried with the next run.
- Values the API does not deliver are skipped instead of invalidating the whole write.
- An unreachable station or module still gets its status written (`reachable` = `false`), so outages
  show up in Grafana.
- If Netatmo rejects the access token, it is refreshed once and the request is repeated.
- `SIGTERM` and `SIGINT` (e.g. `docker stop`, Ctrl+C) end the client within about a second.

## Prerequisites

- Python 3.12 and [uv](https://docs.astral.sh/uv/), or Docker

- InfluxDB 1.x with a database for the data, e.g.

  ```bash
  influx -execute 'CREATE DATABASE netatmo'
  ```

- A Netatmo app and a refresh token:

  1. Create an app at <https://dev.netatmo.com/apps>.
  2. In the app, scroll to **Token generator** and generate a token with the scope `read_station`.
  3. Note the client ID, the client secret and the **refresh token**.

## Configuration

Settings come from a YAML file, from environment variables, or both. Environment variables win.

### config.yaml

Copy `src/config.yaml.template` to `src/config.yaml` and fill it in:

```yaml
interval: 600          # seconds between two runs
loglevel: "INFO"       # DEBUG, INFO, WARNING, ...

netatmo:
  client_id: "..."
  client_secret: "..."
  refresh_token: "..."  # only needed for the first start, see "Token file"

influx:
  influx_host: "localhost"
  influx_port: "8086"
  influx_db: "netatmo"
  influx_ssl: ""
  influx_username: "user"
  influx_password: "pass"
```

By default the file is read from next to `client.py`. Use `-c /path/to/config.yaml` to point elsewhere.

### Environment variables

| Variable                | Config key               | Default     |
| ----------------------- | ------------------------ | ----------- |
| `INTERVAL`              | `interval`               | `300`       |
| `LOGLEVEL`              | `loglevel`               | `INFO`      |
| `NETATMO_CLIENT_ID`     | `netatmo.client_id`      |             |
| `NETATMO_CLIENT_SECRET` | `netatmo.client_secret`  |             |
| `NETATMO_REFRESH_TOKEN` | `netatmo.refresh_token`  |             |
| `INFLUX_HOST`           | `influx.influx_host`     | `localhost` |
| `INFLUX_PORT`           | `influx.influx_port`     | `8086`      |
| `INFLUX_DB`             | `influx.influx_db`       | `netatmo`   |
| `INFLUX_SSL`            | `influx.influx_ssl`      | off (HTTP)  |
| `INFLUX_USERNAME`       | `influx.influx_username` | `user`      |
| `INFLUX_PASSWORD`       | `influx.influx_password` | `pass`      |

The InfluxDB defaults only apply when an `influx:` section exists in the config file.
When you configure everything through environment variables, set all `INFLUX_*` variables.

**SSL:** set `INFLUX_SSL` / `influx_ssl` to `true` (also accepted: `1`, `yes`, `y`, `on`) to connect via
HTTPS. Any other value, including an empty one, means plain HTTP.

### Command line

| Option                | Meaning                                                   |
| --------------------- | --------------------------------------------------------- |
| `-c`, `--config-file` | path to the config file                                   |
| `-t`, `--token-file`  | path to the token file, default `data/token.json`         |
| `-v`, `-vv`, `-vvv`   | log level WARNING, INFO, DEBUG (overridden by `LOGLEVEL`) |

### Token file

Netatmo rotates the refresh token. The client therefore stores the current access token,
refresh token and expiry time in a token file (default `data/token.json`, relative to the working
directory) and prefers it over the configured refresh token.

- The directory must exist and be writable by the user running the client.
- The refresh token from the config is only used while no token file exists.
- If the refresh token has expired, the log says so ("Refresh token expired. …"). Generate a new one in
  the Netatmo developer console, set it as `NETATMO_REFRESH_TOKEN` and **delete the token file**,
  otherwise the expired token in the file keeps taking precedence.

## Running

### With uv

```bash
uv sync
mkdir -p data
uv run src/client.py
```

### With Docker

The CI publishes an image to `ghcr.io/hb9uqs/netatmo-client-influx` (`latest` from `main`, plus a tag for
each version tag). A complete setup with InfluxDB 1.x:

```yaml
services:
  influxdb:
    image: docker.io/influxdb:1.12-alpine
    restart: unless-stopped
    environment:
      - INFLUXDB_DB=netatmo
      - INFLUXDB_HTTP_AUTH_ENABLED=true
      - INFLUXDB_ADMIN_USER=admin
      - INFLUXDB_ADMIN_PASSWORD=change-me
      - INFLUXDB_USER=netatmo
      - INFLUXDB_USER_PASSWORD=change-me-too
    volumes:
      - influxdb:/var/lib/influxdb

  netatmo-client-influx:
    image: ghcr.io/hb9uqs/netatmo-client-influx:latest
    restart: unless-stopped
    environment:
      - INTERVAL=600
      - NETATMO_CLIENT_ID=...
      - NETATMO_CLIENT_SECRET=...
      - NETATMO_REFRESH_TOKEN=...
      - INFLUX_HOST=influxdb
      - INFLUX_PORT=8086
      - INFLUX_DB=netatmo
      - INFLUX_USERNAME=netatmo
      - INFLUX_PASSWORD=change-me-too
    volumes:
      - netatmo-data:/app/data
    depends_on:
      - influxdb

volumes:
  influxdb:
  netatmo-data:
```

The token file lives in the named volume `netatmo-data`, which is writable for the container user
(UID `10001`) without further setup. If you prefer a bind mount such as `./data:/app/data`, make the
directory writable for that user first: `mkdir data && sudo chown 10001 data`.

The image is built with `uv sync --locked` and starts with `uv run --no-sync`, so the container never
resolves or downloads packages at runtime. `docker stop` ends it cleanly.

#### Health check

The image has a Docker `HEALTHCHECK`. After every run in which the client fetched the data from Netatmo
and wrote everything to InfluxDB, it renews a heartbeat file (`HEARTBEAT_FILE`, `/tmp/netatmo-heartbeat`
in the image). The container turns **unhealthy** when no such run happened for two intervals plus a
minute, e.g. because the refresh token expired or InfluxDB is unreachable, and is **healthy** again after
the next good run. A process that is running but no longer delivers data thus becomes visible:

```bash
docker ps                                   # STATUS shows (healthy) / (unhealthy)
docker inspect --format '{{json .State.Health}}' <container>
```

`restart: unless-stopped` does not restart an unhealthy container; the log tells why runs fail.
Outside Docker no heartbeat is written unless `HEARTBEAT_FILE` is set.

`docker-compose-dev.yml` builds the image from source for local development.

## Data in InfluxDB

Every value becomes its own measurement with a single field `value`:

| Measurements                                                 | Tags                                |
| ------------------------------------------------------------ | ----------------------------------- |
| Sensor values: `temperature`, `humidity`, `pressure`, `absolutepressure`, `co2`, `noise`, `rain`, `sum_rain_1`, `sum_rain_24`, `windstrength`, `windangle`, `guststrength`, `gustangle`, `min_temp`, `max_temp`, `max_wind_str`, `temp_trend`, `pressure_trend`, … | `home`, `station`, `module`, `type` |
| Device status: `reachable` (base station and modules), `rf_status` (also the base station's Wi-Fi signal), `battery_percent`, `battery_vp`, `co2_calibrating` | `home`, `station`, `module`, `type` |
| Station location: `altitude`, `latitude`, `longitude`, `country`, `city`, `timezone` | `home`, `station`, `type`           |

Which measurements exist depends on your modules; the client writes whatever the API returns.
Sensor values carry the measurement time reported by Netatmo (second precision); `min_temp`, `max_temp`
and `max_wind_str` are stored at the time the extreme occurred. Station location values are stamped with
the time of the run. When a station or module is unreachable, the API sends no readings for it; only its
status values are written, also stamped with the time of the run.

To spot outages, e.g. a module whose battery died:

```sql
SELECT last("value") FROM "reachable" GROUP BY "station", "module"
```

Example query:

```sql
SELECT mean("value") FROM "temperature" WHERE "module" = 'Outdoor' AND time > now() - 24h GROUP BY time(1h)
```

## Grafana dashboard

`dashboard.json` comes from the upstream project. Its queries are written in **Flux** against the bucket
`netatmo`. To use it with InfluxDB 1.x, enable Flux in InfluxDB (`flux-enabled = true` in the `[http]`
section, available from 1.8) and add the InfluxDB datasource in Grafana with the query language set to Flux.
Then import the dashboard and select that datasource.

## Development

Every push to `main`, every version tag and every pull request runs the `check` job in
`.github/workflows/build.yml` (syntax, ruff, tests). The Docker image is only built when it passes.
To run the same checks locally:

```bash
python -m compileall -q src tests
uvx ruff@0.17.3 check src tests
uvx ruff@0.17.3 format --check src tests
uv run --locked --with pytest pytest
```

- Pushes to `main` and version tags `v*` publish the Docker image.
- After changing `pyproject.toml`, even just the version, run `uv lock` and commit `uv.lock` with it.
  CI and the image build use `--locked` and fail if the two do not match.
- ruff skips some checks on Windows, e.g. whether a file with a shebang is executable. A local run on
  Windows can pass while CI on Linux fails.

## Credits

Original project by Dennis Bernardy: [karaktaka/netatmo-client-influx](https://github.com/karaktaka/netatmo-client-influx).

