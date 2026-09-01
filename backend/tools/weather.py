"""天气及海拔数据获取。

天气使用 OpenWeatherMap，海拔使用 Open-Elevation。返回值保持为 JSON 字符串，
与原始脚本的调用约定一致。

取数口径：按**比赛开球时刻**取天气，而不是当前实况——当前实况对几小时后才开赛的
比赛没有参考价值。

  * 开球时刻落在预报窗口内：用 ``/data/2.5/forecast``（免费档，5 天 / 3 小时步长）
    中时间上最接近开球的那一格。
  * 超出预报窗口（5 天以外 / 已开赛）或接口不可用：降级为 ``/data/2.5/weather``
    当前实况，并在 ``weather.note`` 里写明原因，避免被误当成开球时刻的天气。

注意：One Call 3.0 / 2.5 需要单独订阅（免费 key 返回 401），故不使用。
"""

import json
from datetime import datetime, timedelta, timezone

import requests

FORECAST_URL = "https://api.openweathermap.org/data/2.5/forecast"
CURRENT_URL = "https://api.openweathermap.org/data/2.5/weather"
ELEVATION_URL = "https://api.open-elevation.com/api/v1/lookup"

# 免费档预报只覆盖约 5 天；超出这个范围就不请求预报接口，直接走当前实况
FORECAST_FETCH_WINDOW_SECONDS = int(5.5 * 24 * 3600)
# 预报步长 3 小时，取最接近开球时刻的一格；偏差超过 90 分钟说明该时刻未被覆盖
MAX_STEP_OFFSET_SECONDS = 90 * 60


def _local_text(ts, tz_offset=0):
    """把 UTC 时间戳按目标地点偏移转成 'YYYY-MM-DD HH:MM' 当地时间文本。"""
    if not ts:
        return None
    return (
        datetime.fromtimestamp(int(ts), tz=timezone.utc) + timedelta(seconds=int(tz_offset or 0))
    ).strftime("%Y-%m-%d %H:%M")


def _elevation(lat, lon):
    """查询海拔，失败返回 None（不影响天气数据）。"""
    try:
        resp = requests.get(
            ELEVATION_URL,
            params={"locations": f"{lat},{lon}"},
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json()["results"][0]["elevation"]
    except (requests.exceptions.RequestException, KeyError, IndexError, TypeError, ValueError):
        pass
    return None


def _pack_weather(payload: dict) -> dict:
    """把 forecast 的某一格或 current 响应统一打包成固定字段。"""
    main = payload.get("main") or {}
    wind = payload.get("wind") or {}
    weather_list = payload.get("weather") or [{}]
    return {
        "description": (weather_list[0] or {}).get("description", ""),
        "temperature_celsius": main.get("temp"),
        "feels_like_celsius": main.get("feels_like"),
        "temp_min": main.get("temp_min"),
        "temp_max": main.get("temp_max"),
        "humidity_percent": main.get("humidity"),
        "pressure_hpa": main.get("pressure"),
        "wind_speed_m_s": wind.get("speed"),
        "pop": payload.get("pop"),
    }


def get_weather_and_elevation(city_name: str, api_key: str, kickoff_ts: int | None = None) -> str:
    """获取指定城市在**开球时刻**的天气和海拔，返回 JSON 字符串。

    :param city_name: 城市名（英文），如 "Degerfors"
    :param api_key: OpenWeatherMap API key
    :param kickoff_ts: 开球时刻的 UTC 时间戳（fixtures.timestamp）。为 None 时等价于取当前实况
    """
    if not city_name or not api_key:
        return json.dumps(
            {"status": "error", "message": "city_name or OpenWeatherMap API key is missing"},
            ensure_ascii=False,
        )

    common = {"appid": api_key, "units": "metric", "lang": "en"}
    now_ts = int(datetime.now(timezone.utc).timestamp())
    lead = int(kickoff_ts) - now_ts if kickoff_ts else None

    coord = {}
    tz_offset = 0
    city_label = None
    country = None
    pack = None
    source = "current"
    valid_at = None
    offset_minutes = None
    note = None

    # 1) 开球时刻落在预报窗口内 → 取最接近的那一格
    if lead is not None and -MAX_STEP_OFFSET_SECONDS <= lead <= FORECAST_FETCH_WINDOW_SECONDS:
        try:
            resp = requests.get(
                FORECAST_URL, params={"q": city_name, **common}, timeout=15
            )
            if resp.status_code == 200:
                data = resp.json()
                steps = data.get("list") or []
                if steps:
                    step = min(steps, key=lambda s: abs(int(s.get("dt") or 0) - int(kickoff_ts)))
                    diff = abs(int(step.get("dt") or 0) - int(kickoff_ts))
                    if diff <= MAX_STEP_OFFSET_SECONDS:
                        city_info = data.get("city") or {}
                        coord = city_info.get("coord") or {}
                        tz_offset = int(city_info.get("timezone") or 0)
                        city_label = city_info.get("name")
                        country = city_info.get("country")
                        pack = _pack_weather(step)
                        source = "forecast"
                        valid_at = _local_text(step.get("dt"), tz_offset)
                        offset_minutes = round(diff / 60)
        except (requests.exceptions.RequestException, KeyError, TypeError, ValueError):
            pack = None

    # 2) 当前实况兜底（超出预报窗口 / 预报接口不可用 / 未传开球时刻）
    if pack is None:
        try:
            resp = requests.get(CURRENT_URL, params={"q": city_name, **common}, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            coord = data["coord"]
            tz_offset = int(data.get("timezone") or 0)
            city_label = data.get("name")
            country = (data.get("sys") or {}).get("country")
            pack = _pack_weather(data)
            pack["pop"] = None
            source = "current"
            valid_at = _local_text(data.get("dt"), tz_offset)
            if lead is not None and lead > FORECAST_FETCH_WINDOW_SECONDS:
                note = (
                    f"开球时刻距今约 {lead // 86400} 天，超出 5 天预报范围，"
                    f"不代表开球时天气"
                )
            elif lead is not None and lead < 0:
                note = "比赛已开赛，预报不再覆盖该时刻"
        except (requests.exceptions.RequestException, KeyError, TypeError, ValueError) as exc:
            return json.dumps(
                {"status": "error", "message": f"Weather API request failed: {exc}"},
                ensure_ascii=False,
            )

    lat = coord.get("lat")
    lon = coord.get("lon")
    if lat is None or lon is None:
        return json.dumps(
            {"status": "error", "message": "Weather API returned an incomplete response"},
            ensure_ascii=False,
        )

    pack.update(
        {
            "source": source,
            "valid_at": valid_at,
            "offset_minutes": offset_minutes,
            "note": note,
        }
    )
    combined_result = {
        "status": "success",
        "location": {
            "city": city_label or city_name,
            "country": country,
            "coordinates": {"latitude": lat, "longitude": lon},
            "elevation_meters": _elevation(lat, lon),
        },
        "weather": pack,
    }
    return json.dumps(combined_result, ensure_ascii=False, indent=2)
