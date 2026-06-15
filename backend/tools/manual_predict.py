"""交互式单场预测工具。"""

import json
import os
import sys
from datetime import datetime, timedelta

BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
os.chdir(BACKEND_DIR)
sys.path.insert(0, BACKEND_DIR)

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from sqlalchemy import text

from app.db.session import SessionLocal
from prediction.predict import predict_fixture


console = Console()


def _date_range(date_str: str) -> tuple[str, str]:
    d = datetime.strptime(date_str, "%Y-%m-%d")
    start = d + timedelta(hours=10, minutes=10)
    end = start + timedelta(hours=24)
    return start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S")


def _load_matches_for_date(db, date_str: str) -> list[dict]:
    start, end = _date_range(date_str)
    rows = db.execute(
        text(
            """
            SELECT f.id, f.home_name, f.away_name, f.league_name,
                   f.date, f.status_short,
                   CASE WHEN p.fixture_id IS NULL THEN 0 ELSE 1 END AS predicted,
                   p.llm_win, p.llm_win_pct
            FROM fixtures f
            JOIN leagues l ON f.league_id = l.id
            LEFT JOIN predictions p ON p.fixture_id = f.id
            WHERE f.date >= :start AND f.date < :end
              AND l.enabled = 1
            ORDER BY f.date
            """
        ),
        {"start": start, "end": end},
    ).fetchall()
    return [dict(r._mapping) for r in rows]


def _render_match_list(matches: list[dict], date_str: str) -> None:
    table = Table(title=f"{date_str} 比赛列表", show_header=True, header_style="bold cyan")
    table.add_column("序号", justify="right", style="cyan", no_wrap=True)
    table.add_column("fixture_id", style="magenta", no_wrap=True)
    table.add_column("时间", no_wrap=True)
    table.add_column("联赛")
    table.add_column("状态", no_wrap=True)
    table.add_column("比赛")
    table.add_column("已有预测")

    for idx, m in enumerate(matches, start=1):
        predicted = "无"
        if m.get("predicted"):
            predicted = f"{m.get('llm_win') or '-'} {m.get('llm_win_pct') or ''}".strip()
        table.add_row(
            str(idx),
            str(m.get("id")),
            _fmt_date(m.get("date"))[11:16] if m.get("date") else "-",
            m.get("league_name") or "-",
            m.get("status_short") or "-",
            f"{m.get('home_name') or '-'} vs {m.get('away_name') or '-'}",
            predicted,
        )
    console.print(table)


def _choose_fixture_id(db) -> int | None:
    today = datetime.now().strftime("%Y-%m-%d")
    date_str = Prompt.ask("请输入比赛日期", default=today)
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        console.print("[red]日期格式错误，请使用 YYYY-MM-DD。[/red]")
        return None

    matches = _load_matches_for_date(db, date_str)
    if not matches:
        console.print(f"[yellow]{date_str} 没有找到启用联赛的比赛。[/yellow]")
        raw = Prompt.ask("可直接输入 fixture_id，或回车退出", default="")
        return int(raw) if raw.strip().isdigit() else None

    _render_match_list(matches, date_str)
    raw = Prompt.ask("请输入序号或 fixture_id")
    if not raw.strip().isdigit():
        console.print("[red]请输入数字序号或 fixture_id。[/red]")
        return None

    value = int(raw)
    if 1 <= value <= len(matches):
        return int(matches[value - 1]["id"])
    return value


def _load_prediction(db, fixture_id: int) -> dict | None:
    row = db.execute(
        text(
            """
            SELECT p.fixture_id, p.match_date, p.model_group,
                   p.win_home, p.win_draw, p.win_away, p.over25_prob,
                   p.top3_scores, p.lambda_home, p.lambda_away, p.handicap,
                   p.llm_win, p.llm_score, p.llm_win_pct,
                   p.llm_brief, p.llm_core_data, p.llm_deep_report,
                   p.llm_handicap_num, p.llm_handicap_team, p.llm_handicap_pct,
                   p.llm_ou_line, p.llm_ou_type, p.llm_ou_pct,
                   p.home_name, p.away_name, p.league_name,
                   p.home_logo, p.away_logo,
                   f.status_short
            FROM predictions p
            LEFT JOIN fixtures f ON p.fixture_id = f.id
            WHERE p.fixture_id = :fid
            """
        ),
        {"fid": fixture_id},
    ).fetchone()
    if not row:
        return None
    data = dict(row._mapping)
    if isinstance(data.get("top3_scores"), str):
        try:
            data["top3_scores"] = json.loads(data["top3_scores"])
        except Exception:
            pass
    return data


def _fmt_pct(value) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value) * 100:.1f}%"
    except Exception:
        return str(value)


def _fmt_date(value) -> str:
    if not value:
        return "-"
    return value.strftime("%Y-%m-%d %H:%M:%S") if hasattr(value, "strftime") else str(value)


def _render_prediction(data: dict) -> None:
    title = f"{data.get('home_name') or '-'} vs {data.get('away_name') or '-'}"
    subtitle = (
        f"Fixture #{data.get('fixture_id')} | {data.get('league_name') or '-'} | "
        f"{_fmt_date(data.get('match_date'))} | 状态 {data.get('status_short') or '-'}"
    )
    console.print(Panel(subtitle, title=title, border_style="cyan"))

    final = Table(title="LLM 最终预测", show_header=True, header_style="bold magenta")
    final.add_column("项目", style="cyan", no_wrap=True)
    final.add_column("结果", style="bold white")
    final.add_row("胜平负", f"{data.get('llm_win') or '-'} {data.get('llm_win_pct') or ''}".strip())
    final.add_row(
        "让球",
        f"{data.get('llm_handicap_team') or '-'} {data.get('llm_handicap_num') or '-'} "
        f"{data.get('llm_handicap_pct') or ''}".strip(),
    )
    final.add_row(
        "大小球",
        f"{data.get('llm_ou_type') or '-'} {data.get('llm_ou_line') or '-'} "
        f"{data.get('llm_ou_pct') or ''}".strip(),
    )
    final.add_row("比分", data.get("llm_score") or "-")
    console.print(final)

    xgb = Table(title="XGBoost 参考", show_header=True, header_style="bold blue")
    xgb.add_column("项目", style="cyan", no_wrap=True)
    xgb.add_column("值")
    xgb.add_row("模型组", data.get("model_group") or "-")
    xgb.add_row(
        "胜平负概率",
        f"主胜 {_fmt_pct(data.get('win_home'))} | 平局 {_fmt_pct(data.get('win_draw'))} | 客胜 {_fmt_pct(data.get('win_away'))}",
    )
    xgb.add_row("大小球", f"大 2.5: {_fmt_pct(data.get('over25_prob'))}")
    xgb.add_row(
        "预期进球",
        f"主 {data.get('lambda_home'):.2f} | 客 {data.get('lambda_away'):.2f}"
        if data.get("lambda_home") is not None and data.get("lambda_away") is not None
        else "-",
    )
    xgb.add_row("让球参考", data.get("handicap") or "-")
    top3 = data.get("top3_scores") or []
    if isinstance(top3, list):
        top3_text = "  ".join(f"{x.get('score')}({_fmt_pct(x.get('prob'))})" for x in top3 if isinstance(x, dict))
    else:
        top3_text = str(top3)
    xgb.add_row("Top3 比分", top3_text or "-")
    console.print(xgb)

    analysis = "\n".join(
        line
        for line in [
            f"[bold]一句话:[/bold] {data.get('llm_brief')}" if data.get("llm_brief") else "",
            f"[bold]核心数据:[/bold] {data.get('llm_core_data')}" if data.get("llm_core_data") else "",
            f"[bold]深度分析:[/bold] {data.get('llm_deep_report')}" if data.get("llm_deep_report") else "",
        ]
        if line
    )
    if analysis:
        console.print(Panel(analysis, title="分析摘要", border_style="green"))


def main() -> int:
    db = SessionLocal()
    try:
        console.print(Panel("先选择日期查看比赛，再输入序号或 fixture_id 执行单场预测。", title="手动预测", border_style="cyan"))
        fixture_id = _choose_fixture_id(db)
        if fixture_id is None:
            console.print("[yellow]已取消。[/yellow]")
            return 0

        fixture = db.execute(
            text(
                """
                SELECT id, home_name, away_name, league_name, date, status_short
                FROM fixtures
                WHERE id = :fid
                """
            ),
            {"fid": fixture_id},
        ).fetchone()
        if not fixture:
            console.print(f"[red]未找到比赛 fixture_id={fixture_id}[/red]")
            return 1

        f = dict(fixture._mapping)
        console.print(
            f"[cyan]开始预测:[/cyan] {f.get('home_name')} vs {f.get('away_name')} "
            f"({f.get('league_name')}, {_fmt_date(f.get('date'))}, {f.get('status_short')})"
        )

        result = predict_fixture(fixture_id, db=db)
        if result is None:
            console.print("[red]预测失败：数据不足、模型缺失、LLM 未返回完整预测，或比赛不存在。[/red]")
            return 2

        data = _load_prediction(db, fixture_id)
        if not data:
            console.print("[red]预测执行完成，但未读取到 predictions 记录。[/red]")
            return 3

        _render_prediction(data)
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
