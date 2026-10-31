import argparse
import json
import logging
import signal
import sys
import threading
import time
from datetime import UTC, datetime
from enum import Enum
from os import getenv
from pathlib import Path

import requests
import yaml
from influxdb import InfluxDBClient
from influxdb.exceptions import InfluxDBClientError, InfluxDBServerError

from helpers import configure_logging
from netatmo_api import (
    NetatmoAPI,
    NetatmoAPIError,
    NetatmoAuthError,
    NetatmoAuthErrorTokenExpired,
    NetatmoThrottlingError,
)


class VerbosityLevel(Enum):
    NOTSET = 0
    WARNING = 1
    INFO = 2
    DEBUG = 3


def parse_config(_config_file=None) -> dict:
    if _config_file is None:
        _config_file = Path(__file__).parent / "config.yaml"

    try:
        with open(_config_file, "r", encoding="utf-8") as _f:
            _config = yaml.safe_load(_f)
    except FileNotFoundError:
        return {}
    except yaml.YAMLError as _error:
        if hasattr(_error, "problem_mark"):
            _mark = _error.problem_mark
            print("Error in configuration")
            print(f"Error position: ({_mark.line + 1}:{_mark.column + 1})")
        sys.exit(1)
    else:
        return _config


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("-c", "--config-file", dest="config_file", type=str, nargs="?", required=False, default=None)
    parser.add_argument("-t", "--token-file", dest="token_file", type=str, nargs="?", default="data/token.json")
    parser.add_argument("-v", "--verbose", dest="verbosity", action="count", default=0)

    return parser.parse_args()


def safe_list_get(_input_list: list, _idx: int, _default=None) -> str | int | float | None:
    try:
        return _input_list[_idx]
    except IndexError:
        return _default


def check_value(_val: float | str) -> float | str:
    if type(_val) is int:
        return float(_val)
    return _val


def to_bool(_val) -> bool:
    # config and environment deliver strings; "False" or "n" must not count as true
    if isinstance(_val, bool):
        return _val
    return str(_val).strip().lower() in ("1", "true", "yes", "y", "on")


def short(_error: Exception, _limit: int = 300) -> str:
    # one line, at most _limit characters
    _text = " ".join(str(_error).split())
    return _text if len(_text) <= _limit else _text[: _limit - 3] + "..."


stop = threading.Event()


def shutdown(_signum, _frame):
    # signal handlers are called with (signum, frame)
    stop.set()


def wait(seconds: float) -> None:
    # wait in short slices: a signal handler only runs between them on some platforms (e.g. Windows)
    deadline = time.monotonic() + seconds
    while not stop.is_set() and (remaining := deadline - time.monotonic()) > 0:
        stop.wait(min(remaining, 1))


def get_sensor_data(
    _sensor_data: dict, _home_name: str, _station_name: str, _module_name: str, _module_type: str
) -> list:
    _measurements = []
    _date_times = {}

    if _sensor_data is not None:
        _time = _sensor_data.pop("time_utc")
        # for the first 5 minutes of each day there is no max* data for neither wind nor temperature sensors
        # a missing date_* key must not abort the run; the value is then stored at the measurement time
        if _module_type == "NAModule2" and "max_wind_str" in _sensor_data:
            _date_times = {"date_max_wind_str": _sensor_data.pop("date_max_wind_str", _time)}
        if _module_type not in ["NAModule3", "NAModule2"] and all(k in _sensor_data for k in ("max_temp", "min_temp")):
            _date_times = {
                "date_max_temp": _sensor_data.pop("date_max_temp", _time),
                "date_min_temp": _sensor_data.pop("date_min_temp", _time),
            }
        for _sensor, _value in _sensor_data.items():
            # a point without a field value is invalid line protocol and makes InfluxDB reject the whole write
            if _value is None:
                continue
            _measurements.append(
                {
                    "measurement": _sensor.lower() if _sensor.lower() != "wifi_status" else "rf_status",
                    "tags": {
                        "home": _home_name,
                        "station": _station_name,
                        "module": _module_name,
                        "type": _module_type,
                    },
                    "fields": {"value": check_value(_value)},
                    "time": (
                        _time
                        if _sensor not in ["max_temp", "min_temp", "max_wind_str"]
                        else _date_times.get(f"date_{_sensor}")
                    ),
                }
            )
    return _measurements


if __name__ == "__main__":
    client_id = None
    client_secret = None
    refresh_token = None
    influx_host = None
    influx_port = None
    influx_db = None
    influx_ssl = None
    influx_username = None
    influx_password = None

    args = parse_args()
    config = parse_config(args.config_file)

    # always handle SIGTERM: docker stop sends it and kills the process 10 s later
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    interval = int(config.get("interval", "300"))  # interval in seconds; default are 5 Minutes
    log_level = config.get("loglevel", "INFO")  # set loglevel by Name

    if "netatmo" in config:
        client_id = config.get("netatmo").get("client_id", None)
        client_secret = config.get("netatmo").get("client_secret", None)
        refresh_token = config.get("netatmo").get("refresh_token", None)

    if "influx" in config:
        influx_host = config.get("influx").get("influx_host", "localhost")
        influx_port = config.get("influx").get("influx_port", "8086")
        influx_db = config.get("influx").get("influx_db", "netatmo")
        influx_ssl = config.get("influx").get("influx_ssl", "n")
        influx_username = config.get("influx").get("influx_username", "user")
        influx_password = config.get("influx").get("influx_password", "pass")

    # Environment Variables takes precedence over config if set
    # global
    interval = int(getenv("INTERVAL", interval))
    log_level = getenv("LOGLEVEL", VerbosityLevel(args.verbosity).name if args.verbosity > 0 else log_level)
    # netatmo
    client_id = getenv("NETATMO_CLIENT_ID", client_id)
    client_secret = getenv("NETATMO_CLIENT_SECRET", client_secret)
    # refresh_token needs to be persisted in the config, but can be set as env var for first run
    refresh_token = getenv("NETATMO_REFRESH_TOKEN", refresh_token)
    # influx
    influx_host = getenv("INFLUX_HOST", influx_host)
    influx_port = getenv("INFLUX_PORT", influx_port)
    influx_db = getenv("INFLUX_DB", influx_db)
    influx_ssl = getenv("INFLUX_SSL", influx_ssl)
    influx_username = getenv("INFLUX_USERNAME", influx_username)
    influx_password = getenv("INFLUX_PASSWORD", influx_password)

    # set logging level
    logger = logging.getLogger(__name__)
    log = configure_logging(logger, log_level)

    api = NetatmoAPI(
        client_id=client_id,
        client_secret=client_secret,
        refresh_token=refresh_token,
        token_file=args.token_file,
        log_level=log_level,
    )

    # the client does not connect on creation, so one instance serves all runs
    client = InfluxDBClient(
        host=influx_host,
        port=influx_port,
        username=influx_username,
        password=influx_password,
        database=influx_db,
        ssl=to_bool(influx_ssl),
    )

    log.info("Netatmo Crawler ready...")
    while not stop.is_set():
        try:
            api.get_stations_data()
            stations = api.get_stations()

            for station in stations.values():
                measurements = []

                log.debug(f"Station Data: {station}")
                home_name = station.get("home_name", "Unknown")
                station_name = station.get("station_name", "Unknown")
                station_module_name = station.get("module_name", "Unknown")
                station_module_type = station.get("type", "Unknown")
                station_place = station.get("place", {})
                station_long_lat = station_place.get("location", [])

                station_data = {
                    "altitude": station_place.get("altitude"),
                    "country": station_place.get("country", "Unknown"),
                    "city": station_place.get("city", "Unknown"),
                    "timezone": station_place.get("timezone", "Unknown"),
                    "longitude": safe_list_get(station_long_lat, 0),
                    "latitude": safe_list_get(station_long_lat, 1),
                }

                for key, value in station_data.items():
                    if value is None:
                        continue
                    measurements.append(
                        {
                            "measurement": key,
                            "tags": {"home": home_name, "station": station_name, "type": station_module_type},
                            "fields": {"value": check_value(value)},
                            "time": int(datetime.now(UTC).timestamp()),
                        }
                    )

                station_sensor_data = station.get("dashboard_data")

                if station_sensor_data is None:
                    # unreachable station: no readings, but still record its status and handle its modules
                    log.warning(f"No data from station {station_name}, writing status only")
                    station_sensor_data = {"time_utc": int(datetime.now(UTC).timestamp())}

                for sensor in ["wifi_status", "reachable", "co2_calibrating"]:
                    station_sensor_data.update({sensor: station.get(sensor)})

                measurements += get_sensor_data(
                    station_sensor_data, home_name, station_name, station_module_name, station_module_type
                )

                for module in station.get("modules", []):
                    log.debug(f"Module Data: {module}")
                    module_name = module.get("module_name")
                    module_type = module.get("type")

                    module_sensor_data = module.get("dashboard_data")

                    if module_sensor_data is None:
                        log.warning(f"No data from module {module_name}, writing status only")
                        module_sensor_data = {"time_utc": int(datetime.now(UTC).timestamp())}

                    for sensor in ["rf_status", "battery_vp", "battery_percent", "reachable"]:
                        module_sensor_data.update({sensor: module.get(sensor)})

                    measurements += get_sensor_data(
                        module_sensor_data, home_name, station_name, module_name, module_type
                    )
                # a failed write must not stop the crawler; the next run writes fresh data
                try:
                    client.write_points(measurements, time_precision="s", database=influx_db)
                except (InfluxDBClientError, InfluxDBServerError, requests.exceptions.RequestException) as error:
                    log.error(f"Cannot write to InfluxDB for station {station_name}: {error}")
        # the cause goes into the error line; API errors can carry a whole HTML page, so it is shortened
        except (json.decoder.JSONDecodeError, requests.exceptions.JSONDecodeError) as error:
            log.error(f"JSON Decode Error: {short(error)}. Retry in {interval} second(s)...")
        except NetatmoThrottlingError as error:
            log.error(f"API Throttling: {short(error)}. Retry in {interval} second(s)...")
        except NetatmoAPIError as error:
            log.error(f"API Error: {short(error)}. Retry in {interval} second(s)...")
        except NetatmoAuthError as error:
            log.error(f"Auth Error: {short(error)}. Retry in {interval} second(s)...")
        except NetatmoAuthErrorTokenExpired:
            log.error(
                "Refresh token expired. Generate a new one in the Netatmo developer console, set it as "
                f"NETATMO_REFRESH_TOKEN and delete {args.token_file} (it takes precedence over the config). "
                f"Retry in {interval} second(s)..."
            )
        except Exception:
            # last resort: keep the loop alive and log the traceback
            log.exception(f"Unexpected error. Retry in {interval} second(s)...")
        finally:
            wait(interval)
    client.close()
    log.info("Netatmo Crawler stopped.")
