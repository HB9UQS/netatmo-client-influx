"""Conversion of Netatmo dashboard data into InfluxDB points."""

import time

import pytest
from influxdb.line_protocol import make_lines

from client import get_sensor_data, short, to_bool, write_heartbeat


def test_heartbeat_holds_the_time_until_healthy(tmp_path):
    path = tmp_path / "heartbeat"
    before = int(time.time())
    write_heartbeat(str(path), 660)
    valid_until = int(path.read_text())
    assert before + 660 <= valid_until <= int(time.time()) + 660
    # replaced atomically, no temporary file left behind
    assert [p.name for p in tmp_path.iterdir()] == ["heartbeat"]


def test_missing_date_of_extreme_falls_back_to_measurement_time():
    data = {"time_utc": NOW, "WindStrength": 12, "max_wind_str": 30}
    result = points(data, module_type="NAModule2")
    assert result["max_wind_str"]["time"] == NOW
    assert result["windstrength"]["time"] == NOW


def test_short_keeps_one_line_and_limits_length():
    assert short(ValueError("API request failed\n  with status 500")) == "API request failed with status 500"
    long_text = short(ValueError("<html>" + "x" * 1000 + "</html>"))
    assert len(long_text) == 300
    assert long_text.endswith("...")


@pytest.mark.parametrize("value", [True, "true", "True", "1", "yes", "y", "on"])
def test_to_bool_true(value):
    assert to_bool(value) is True


@pytest.mark.parametrize("value", [False, None, "", "false", "False", "0", "n", "no", "off"])
def test_to_bool_false(value):
    assert to_bool(value) is False


NOW = 1761900000


def points(data: dict, module_type: str = "NAModule1") -> dict:
    return {p["measurement"]: p for p in get_sensor_data(dict(data), "Home", "Station", "Outdoor", module_type)}


def test_one_point_per_value_with_tags():
    result = points({"time_utc": NOW, "Temperature": 8.1, "Humidity": 80})
    assert set(result) == {"temperature", "humidity"}
    assert result["humidity"]["fields"] == {"value": 80.0}
    assert result["temperature"]["tags"] == {
        "home": "Home",
        "station": "Station",
        "module": "Outdoor",
        "type": "NAModule1",
    }
    assert result["temperature"]["time"] == NOW


def test_missing_values_are_skipped():
    data = {"time_utc": NOW, "Temperature": 8.1, "rf_status": None, "battery_vp": None}
    result = points(data)
    assert set(result) == {"temperature"}
    # every line must carry a field, otherwise InfluxDB rejects the whole write
    for line in make_lines({"points": list(result.values())}, precision="s").splitlines():
        assert len(line.split(" ")) == 3


def test_min_max_temperature_keep_their_own_time():
    data = {
        "time_utc": NOW,
        "Temperature": 8.1,
        "min_temp": 5.0,
        "max_temp": 9.2,
        "date_min_temp": NOW - 20000,
        "date_max_temp": NOW - 10000,
    }
    result = points(data)
    assert "date_min_temp" not in result
    assert result["min_temp"]["time"] == NOW - 20000
    assert result["max_temp"]["time"] == NOW - 10000


def test_wifi_status_is_stored_as_rf_status():
    result = points({"time_utc": NOW, "wifi_status": 50}, module_type="NAMain")
    assert "rf_status" in result
