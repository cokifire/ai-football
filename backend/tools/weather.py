"""天气及海拔数据获取。

天气使用 OpenWeatherMap，海拔使用 Open-Elevation。返回值保持为 JSON 字符串，
与原始脚本的调用约定一致。
"""

import json

import requests


def get_weather_and_elevation(city_name: str, api_key: str) -> str:
    """获取指定城市的当前天气和海拔，返回 JSON 字符串。"""
    if not city_name or not api_key:
        return json.dumps(
            {"status": "error", "message": "city_name or OpenWeatherMap API key is missing"},
            ensure_ascii=False,
        )

    weather_url = "https://api.openweathermap.org/data/2.5/weather"
    weather_params = {
        "q": city_name,
        "appid": api_key,
        "units": "metric",
        "lang": "en",
    }

    try:
        weather_resp = requests.get(weather_url, params=weather_params, timeout=10)
        weather_resp.raise_for_status()
        weather_data = weather_resp.json()
        lat = weather_data["coord"]["lat"]
        lon = weather_data["coord"]["lon"]
    except (requests.exceptions.RequestException, KeyError, TypeError, ValueError) as exc:
        return json.dumps(
            {"status": "error", "message": f"Weather API request failed: {exc}"},
            ensure_ascii=False,
        )

    elevation_val = None
    try:
        elevation_url = "https://api.open-elevation.com/api/v1/lookup"
        elevation_resp = requests.get(
            elevation_url,
            params={"locations": f"{lat},{lon}"},
            timeout=10,
        )
        if elevation_resp.status_code == 200:
            elevation_data = elevation_resp.json()
            elevation_val = elevation_data["results"][0]["elevation"]
    except (requests.exceptions.RequestException, KeyError, IndexError, TypeError, ValueError):
        # 海拔服务失败不影响天气数据返回。
        pass

    try:
        combined_result = {
            "status": "success",
            "location": {
                "city": weather_data.get("name"),
                "country": weather_data.get("sys", {}).get("country"),
                "coordinates": {"latitude": lat, "longitude": lon},
                "elevation_meters": elevation_val,
            },
            "weather": {
                "description": weather_data["weather"][0]["description"],
                "temperature_celsius": weather_data["main"]["temp"],
                "feels_like_celsius": weather_data["main"]["feels_like"],
                "temp_min": weather_data["main"]["temp_min"],
                "temp_max": weather_data["main"]["temp_max"],
                "humidity_percent": weather_data["main"]["humidity"],
                "pressure_hpa": weather_data["main"]["pressure"],
                "wind_speed_m_s": weather_data.get("wind", {}).get("speed"),
            },
        }
    except (KeyError, IndexError, TypeError):
        return json.dumps(
            {"status": "error", "message": "Weather API returned an incomplete response"},
            ensure_ascii=False,
        )

    return json.dumps(combined_result, ensure_ascii=False, indent=2)
