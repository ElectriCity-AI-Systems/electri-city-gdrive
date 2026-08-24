"""Sequential runner for every enabled user-configured sync pair."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterable

from electridrive.config import SyncPair
from electridrive.sync.rules import SyncRules
from electridrive.sync.twoway import SyncReport, TwoWaySyncEngine

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class PairOutcome:
    pair: SyncPair
    report: SyncReport


@dataclass
class ConfiguredSyncReport:
    configured_pairs: int = 0
    enabled_pairs: int = 0
    successful_pairs: int = 0
    failed_pairs: int = 0
    outcomes: list[PairOutcome] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def run_configured_pairs(
    client,
    db,
    pairs: Iterable[SyncPair],
    *,
    rules: SyncRules | None = None,
    log_cb=None,
) -> ConfiguredSyncReport:
    """Run all enabled pairs, continuing after every pair-level failure."""
    configured = list(pairs)
    aggregate = ConfiguredSyncReport(configured_pairs=len(configured))
    for index, pair in enumerate(configured, start=1):
        if not pair.enabled:
            continue
        aggregate.enabled_pairs += 1
        if log_cb:
            try:
                log_cb(
                    f"Sync pair {index}/{len(configured)}: "
                    f"{pair.local_path} <-> {pair.remote_folder}"
                )
            except Exception:
                LOGGER.warning("Configured sync log callback failed", exc_info=True)
        try:
            report = TwoWaySyncEngine(
                client, db, pair, rules=rules, log_cb=log_cb
            ).run()
        except Exception as exc:
            # Pair isolation is a correctness requirement: a broken selected
            # root must never prevent later configured roots from running.
            LOGGER.exception("Configured sync pair failed: %s", pair.local_path)
            report = SyncReport(failed=1, errors=[f"pair execution failed: {exc}"])

        aggregate.outcomes.append(PairOutcome(pair, report))
        if report.failed or report.unexplained_omissions:
            aggregate.failed_pairs += 1
            if report.errors:
                aggregate.errors.extend(
                    f"{pair.local_path}: {error}" for error in report.errors
                )
            else:
                aggregate.errors.append(
                    f"{pair.local_path}: pair failed without an error detail"
                )
        else:
            aggregate.successful_pairs += 1
    return aggregate
