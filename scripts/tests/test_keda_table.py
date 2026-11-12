"""The KEDA table the docs show matches the charts' rendered ScaledObjects."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import keda_table


def test_the_written_table_is_what_the_goldens_render() -> None:
    assert keda_table.main(["--check"]) == 0


def test_a_wrong_row_fails_the_check(tmp_path: Path, monkeypatch) -> None:
    planted = tmp_path / "keda-table.md"
    planted.write_text(keda_table.table().replace("| 2-12 |", "| 2-99 |", 1), encoding="utf-8")
    monkeypatch.setattr(keda_table, "OUT", planted)
    monkeypatch.setattr(keda_table, "REPO_ROOT", tmp_path)
    assert keda_table.main(["--check"]) == 1


def test_a_profile_with_no_scaledobject_reads_off() -> None:
    assert keda_table.describe(None) == ("off", "", "", "")
