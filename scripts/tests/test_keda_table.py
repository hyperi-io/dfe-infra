"""The KEDA table the docs show matches the charts' rendered ScaledObjects."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import keda_table


def test_the_written_table_is_what_the_goldens_render() -> None:
    assert keda_table.main(["--check"]) == 0


def test_a_wrong_row_fails_the_check(tmp_path: Path, monkeypatch) -> None:
    planted = tmp_path / "keda-table.md"
    planted.write_text(
        keda_table.table().replace("| 2-12 |", "| 2-99 |", 1), encoding="utf-8"
    )
    monkeypatch.setattr(keda_table, "OUT", planted)
    monkeypatch.setattr(keda_table, "REPO_ROOT", tmp_path)
    assert keda_table.main(["--check"]) == 1


def test_a_profile_with_no_scaledobject_reads_off() -> None:
    assert keda_table.describe(None) == ("off", "", "", "")


ON = ("1-3", "70%", "yes", "")


@pytest.mark.parametrize(
    ("described", "cells"),
    [
        (
            {"slim": ON, "single": ON, "scale": ON},
            ["1-3", "1-3", "1-3", "70%", "slim, single, scale", ""],
        ),
        (
            {
                "slim": ("off", "", "", ""),
                "single": ON,
                "scale": ("2-12", "70%", "no", ""),
            },
            ["off", "1-3", "2-12", "70%", "single", ""],
        ),
        (
            {"slim": ("1-2", "60%", "no", ""), "single": ON, "scale": ON},
            [
                "1-2",
                "1-3",
                "1-3",
                "slim 60%, single 70%, scale 70%",
                "single, scale",
                "",
            ],
        ),
        (
            {
                "slim": ("1-2", "70%", "no", ""),
                "single": ("1-3", "70%", "no", "kafka"),
                "scale": ON,
            },
            ["1-2", "1-3", "1-3", "70%", "scale", "single kafka"],
        ),
    ],
    ids=["uniform", "off-on-slim", "cpu-differs", "other-trigger"],
)
def test_one_row_per_app_folds_what_the_profiles_share(
    described: dict[str, tuple[str, str, str, str]], cells: list[str]
) -> None:
    assert keda_table.app_row("dfe-x", described) == ["`dfe-x`", *cells]


def test_the_table_drops_an_other_triggers_column_nobody_fills() -> None:
    assert "Other triggers" not in keda_table.table().splitlines()[0]
