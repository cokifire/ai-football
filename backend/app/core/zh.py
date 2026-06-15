"""
中文字段兜底工具

约定：源表（leagues / teams / players / venues / fixture_events / fixture_statistics）
都有 *_zh 列；翻译脚本 translate_zh.py 写入 _zh 后，API 返回时自动用 _zh 替换原列。

使用方式：
- ORM 对象：zh_swap(obj) 按对象类自动选择映射，原字段名不变
- 反范式表（fixtures / standings / predictions）：通过 ID 批量回查源表
"""

ZH_MAPS = {
    "League": {"name": "name_zh", "country_name": "country_name_zh"},
    "Team": {"name": "name_zh", "country": "country_zh"},
    "Player": {"name": "name_zh"},
    "Venue": {"name": "name_zh", "city": "city_zh"},
    "FixtureEvent": {"type": "type_zh", "detail": "detail_zh"},
    "FixtureStatistic": {"stat_type": "stat_type_zh"},
}


def zh_swap(obj):
    """对单个 ORM 对象做 in-place 字段替换。session 配置 autoflush=False 且只读路径不 commit，不会污染 DB。"""
    if obj is None:
        return obj
    mapping = ZH_MAPS.get(type(obj).__name__)
    if not mapping:
        return obj
    for orig, zh in mapping.items():
        z = getattr(obj, zh, None)
        if z:
            setattr(obj, orig, z)
    return obj


def zh_swap_many(objs):
    if not objs:
        return objs
    for o in objs:
        zh_swap(o)
    return objs


def fixtures_apply_denorm_zh(db, fixtures):
    """fixtures 表的 league_name/home_name/away_name/venue_name 是反范式存的，按 ID 批量回查源表覆盖。"""
    if not fixtures:
        return fixtures
    from app.models.team import Team, Venue
    from app.models.league import League

    team_ids = set()
    league_ids = set()
    venue_ids = set()
    for f in fixtures:
        if f.home_id: team_ids.add(f.home_id)
        if f.away_id: team_ids.add(f.away_id)
        if f.league_id: league_ids.add(f.league_id)
        if f.venue_id: venue_ids.add(f.venue_id)

    teams = {}
    if team_ids:
        for t in db.query(Team.id, Team.name_zh).filter(Team.id.in_(team_ids)).all():
            teams[t.id] = t.name_zh
    leagues = {}
    if league_ids:
        for l in db.query(League.id, League.name_zh).filter(League.id.in_(league_ids)).all():
            leagues[l.id] = l.name_zh
    venues = {}
    if venue_ids:
        for v in db.query(Venue.id, Venue.name_zh, Venue.city_zh).filter(Venue.id.in_(venue_ids)).all():
            venues[v.id] = (v.name_zh, v.city_zh)

    for f in fixtures:
        z = teams.get(f.home_id)
        if z: f.home_name = z
        z = teams.get(f.away_id)
        if z: f.away_name = z
        z = leagues.get(f.league_id)
        if z: f.league_name = z
        v = venues.get(f.venue_id)
        if v:
            if v[0]: f.venue_name = v[0]
            if v[1]: f.venue_city = v[1]
    return fixtures


def standings_apply_denorm_zh(db, standings):
    """standings.team_name 反范式存球队名，按 team_id 回查 teams 表覆盖。"""
    if not standings:
        return standings
    from app.models.team import Team
    team_ids = {s.team_id for s in standings if s.team_id}
    if not team_ids:
        return standings
    teams = {}
    for t in db.query(Team.id, Team.name_zh).filter(Team.id.in_(team_ids)).all():
        if t.name_zh:
            teams[t.id] = t.name_zh
    for s in standings:
        z = teams.get(s.team_id)
        if z:
            s.team_name = z
    return standings
