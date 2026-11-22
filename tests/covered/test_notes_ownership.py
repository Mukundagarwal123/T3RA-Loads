"""No Turvo-driven write path may touch the user-owned notes columns."""

from __future__ import annotations

import inspect

from t3ra_unified.covered import repository as repo


def test_turvo_write_paths_never_name_user_owned_columns():
    for writer in (repo.upsert_final_record, repo.overwrite_preserving_rates, repo.batch_update_preserving_rates):
        source = inspect.getsource(writer)
        for column in repo.USER_OWNED_COLUMNS:
            assert f" {column} =" not in source and f" {column}," not in source, (writer.__name__, column)

    overwritten = {column for column, _ in repo._OVERWRITE_COLUMNS + repo._RATE_ASSIGNMENTS}
    assert not overwritten & set(repo.USER_OWNED_COLUMNS)
