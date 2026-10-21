"""从预测运行日志回填 ``predictions.llm_model`` 的临时脚本。

日志中的 LLM 成功记录本身没有 fixture_id，例如：

    预测 LLM 成功: provider=primary, model=gemini-3.8-flash-high

同一次预测随后会记录 ``预测完成 fixt=<id>``，脚本以这条完成记录把模型
和比赛关联起来。默认只预览，不写数据库；确认预览结果后加 ``--apply``。

示例（在 backend 目录执行）：

    python3 tools/backfill_llm_model_from_logs.py
    python3 tools/backfill_llm_model_from_logs.py \
        --model gemini-3.8-flash-high --apply

脚本不覆盖已有的 llm_model，除非显式传入 ``--overwrite``。
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import bindparam, text


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.db.session import SessionLocal  # noqa: E402


TIMESTAMP_RE = re.compile(r"^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
SUCCESS_RE = re.compile(
    r"预测 LLM 成功:\s*provider=(?P<provider>[^,\s]+),\s*model=(?P<model>\S+)"
)
PREDICTION_DONE_RE = re.compile(r"预测完成\s+fixt=(?P<fixture_id>\d+)\b")
API_PREDICTION_DONE_RE = re.compile(
    r"单场预测完成\s+fixture_id=(?P<fixture_id>\d+)\b"
)

# 成功日志和预测完成日志通常相邻；这个上限用于防止日志截断或重启后误关联。
DEFAULT_MAX_SUCCESS_AGE_SECONDS = 15 * 60


@dataclass(frozen=True)
class LogLine:
    timestamp: datetime
    path: Path
    line_number: int
    text: str


@dataclass(frozen=True)
class SuccessEvent:
    timestamp: datetime
    provider: str
    model: str
    path: Path
    line_number: int


@dataclass(frozen=True)
class MatchEvent:
    fixture_id: int
    provider: str
    model: str
    success: SuccessEvent
    completed_at: datetime
    completed_path: Path
    completed_line_number: int


def _log_files(paths: list[Path]) -> list[Path]:
    """展开日志文件或目录，并去重排序。"""
    files: set[Path] = set()
    for path in paths:
        path = path.expanduser()
        if path.is_dir():
            files.update(path.glob("football_*.log"))
        elif path.is_file():
            files.add(path)
        else:
            raise FileNotFoundError(f"日志路径不存在: {path}")
    return sorted(files, key=lambda item: str(item))


def _read_log_lines(paths: list[Path]) -> list[LogLine]:
    records: list[LogLine] = []
    for path in _log_files(paths):
        with path.open("r", encoding="utf-8", errors="replace") as log_file:
            for line_number, raw_line in enumerate(log_file, start=1):
                line = raw_line.rstrip("\n")
                match = TIMESTAMP_RE.match(line)
                if not match:
                    continue
                records.append(
                    LogLine(
                        timestamp=datetime.strptime(
                            match.group("timestamp"), "%Y-%m-%d %H:%M:%S"
                        ),
                        path=path,
                        line_number=line_number,
                        text=line,
                    )
                )

    # 按日志中的时间排序；同一秒内用文件名和行号保证确定性。
    records.sort(key=lambda item: (item.timestamp, str(item.path), item.line_number))
    return records


def _done_fixture_id(line: str) -> int | None:
    """兼容 prediction 层和 API 层两种完成日志格式。"""
    for pattern in (PREDICTION_DONE_RE, API_PREDICTION_DONE_RE):
        match = pattern.search(line)
        if match:
            return int(match.group("fixture_id"))
    return None


def extract_matches(
    records: list[LogLine],
    max_success_age: timedelta,
) -> tuple[list[MatchEvent], list[SuccessEvent]]:
    """把成功事件和其后的预测完成事件配对。

    使用最近的未配对成功事件，是因为正常日志顺序是：
    ``预测 LLM 成功`` -> ``预测完成 fixt=...`` -> ``单场预测完成``。
    同时保留所有 provider 的成功事件参与配对，最后再按 provider 过滤，
    避免 fallback 成功记录把前一个 primary 事件错误地配给下一场比赛。
    """
    pending: list[SuccessEvent] = []
    matches: list[MatchEvent] = []

    for record in records:
        success_match = SUCCESS_RE.search(record.text)
        if success_match:
            pending.append(
                SuccessEvent(
                    timestamp=record.timestamp,
                    provider=success_match.group("provider"),
                    model=success_match.group("model"),
                    path=record.path,
                    line_number=record.line_number,
                )
            )
            continue

        fixture_id = _done_fixture_id(record.text)
        if fixture_id is None:
            continue

        # 选择时间上最近的成功事件，正常情况下就是前一两行的记录。
        candidate_index = None
        for index in range(len(pending) - 1, -1, -1):
            age = record.timestamp - pending[index].timestamp
            if timedelta(0) <= age <= max_success_age:
                candidate_index = index
                break
        if candidate_index is None:
            continue

        success = pending.pop(candidate_index)
        matches.append(
            MatchEvent(
                fixture_id=fixture_id,
                provider=success.provider,
                model=success.model,
                success=success,
                completed_at=record.timestamp,
                completed_path=record.path,
                completed_line_number=record.line_number,
            )
        )

    return matches, pending


def _latest_by_fixture(
    matches: list[MatchEvent],
    provider: str,
    model: str | None,
    fixture_ids: set[int] | None,
) -> tuple[dict[int, MatchEvent], dict[int, list[MatchEvent]]]:
    grouped: dict[int, list[MatchEvent]] = defaultdict(list)
    for event in matches:
        if event.provider != provider:
            continue
        if model is not None and event.model != model:
            continue
        if fixture_ids is not None and event.fixture_id not in fixture_ids:
            continue
        grouped[event.fixture_id].append(event)

    latest = {
        fixture_id: max(events, key=lambda item: item.completed_at)
        for fixture_id, events in grouped.items()
    }
    return latest, grouped


def _load_predictions(db, fixture_ids: set[int]) -> dict[int, dict]:
    if not fixture_ids:
        return {}
    rows = db.execute(
        text(
            """
            SELECT fixture_id, home_name, away_name, llm_model
            FROM predictions
            WHERE fixture_id IN :fixture_ids
            """
        ).bindparams(bindparam("fixture_ids", expanding=True)),
        {"fixture_ids": sorted(fixture_ids)},
    ).fetchall()
    return {int(row.fixture_id): dict(row._mapping) for row in rows}


def _parse_fixture_ids(raw_ids: list[str]) -> set[int] | None:
    if not raw_ids:
        return None
    fixture_ids: set[int] = set()
    for raw_id in raw_ids:
        for part in raw_id.split(","):
            part = part.strip()
            if not part.isdigit():
                raise ValueError(f"fixture_id 必须是整数: {part}")
            fixture_ids.add(int(part))
    return fixture_ids


def _build_parser() -> argparse.ArgumentParser:
    default_log_dir = BACKEND_DIR / "logs"
    parser = argparse.ArgumentParser(description="从运行日志回填 predictions.llm_model")
    parser.add_argument(
        "--log",
        dest="log_paths",
        action="append",
        type=Path,
        help="日志文件或目录，可重复传入；默认读取 backend/logs/football_*.log",
    )
    parser.add_argument(
        "--provider",
        default="primary",
        help="只处理指定 provider 的成功记录（默认: primary）",
    )
    parser.add_argument(
        "--model",
        help="只处理指定 model；不传则处理日志中所有模型",
    )
    parser.add_argument(
        "--fixture-id",
        dest="fixture_ids",
        action="append",
        default=[],
        help="只处理指定比赛，可传多次或用逗号分隔",
    )
    parser.add_argument(
        "--max-success-age",
        type=int,
        default=DEFAULT_MAX_SUCCESS_AGE_SECONDS,
        metavar="SECONDS",
        help=f"成功日志到完成日志的最大间隔（默认: {DEFAULT_MAX_SUCCESS_AGE_SECONDS}）",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="覆盖已有的非空 llm_model；默认只填充 NULL/空字符串",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="实际提交数据库更新；默认只预览，不写库",
    )
    parser.set_defaults(default_log_dir=default_log_dir)
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    if args.max_success_age <= 0:
        parser.error("--max-success-age 必须大于 0")

    try:
        fixture_ids = _parse_fixture_ids(args.fixture_ids)
        log_paths = args.log_paths or [args.default_log_dir]
        records = _read_log_lines(log_paths)
        matched_events, unmatched_successes = extract_matches(
            records,
            timedelta(seconds=args.max_success_age),
        )
    except (FileNotFoundError, ValueError) as exc:
        parser.error(str(exc))

    latest, grouped = _latest_by_fixture(
        matched_events,
        provider=args.provider,
        model=args.model,
        fixture_ids=fixture_ids,
    )

    print(f"读取日志行: {len(records)}")
    print(f"成功并关联到比赛的记录: {len(matched_events)}")
    print(f"未关联完成记录的成功日志: {len(unmatched_successes)}")
    print(f"筛选后唯一比赛: {len(latest)}（provider={args.provider}, model={args.model or '全部'}）")

    conflicts = {
        fixture_id: sorted({event.model for event in events})
        for fixture_id, events in grouped.items()
        if len({event.model for event in events}) > 1
    }
    if conflicts:
        print(f"警告：{len(conflicts)} 场比赛日志中出现多个模型，将采用最后一次成功记录：")
        for fixture_id, models in sorted(conflicts.items()):
            print(f"  fixture_id={fixture_id}: {', '.join(models)}")

    if not latest:
        print("没有可回填的比赛。")
        return 0

    db = SessionLocal()
    try:
        predictions = _load_predictions(db, set(latest))
        missing = sorted(set(latest) - set(predictions))
        if missing:
            print(f"警告：predictions 中找不到 {len(missing)} 场比赛，将跳过：{missing}")

        updates: list[tuple[int, str, str]] = []
        skipped_existing = 0
        for fixture_id, event in sorted(latest.items()):
            row = predictions.get(fixture_id)
            if row is None:
                continue
            current = row.get("llm_model")
            if not args.overwrite and current is not None and str(current).strip():
                skipped_existing += 1
                continue
            updates.append(
                (
                    fixture_id,
                    event.model,
                    f"{row.get('home_name') or '-'} vs {row.get('away_name') or '-'}",
                )
            )

        print(f"数据库中已有非空 llm_model，跳过: {skipped_existing}")
        print(f"准备回填: {len(updates)}")
        for fixture_id, model_name, match_name in updates:
            print(f"  fixture_id={fixture_id} | {match_name} | llm_model={model_name}")

        if not args.apply:
            print("当前为 dry-run，未写入数据库；确认无误后加 --apply 执行。")
            return 0

        for fixture_id, model_name, _ in updates:
            where = "WHERE fixture_id = :fixture_id"
            if not args.overwrite:
                where += " AND (llm_model IS NULL OR TRIM(llm_model) = '')"
            db.execute(
                text(f"UPDATE predictions SET llm_model = :model_name {where}"),
                {"fixture_id": fixture_id, "model_name": model_name},
            )
        db.commit()
        print(f"已提交数据库更新: {len(updates)} 条")
        return 0
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
