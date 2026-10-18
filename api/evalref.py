"""D-08's context-local public forecast view. No forecast or demand is generated here.

The benchmark uses d0..d3 on days 0..3, then d3's own target dates on days 4..6.
Missing target dates stop the day's transaction; normal app references remain unchanged.
"""
from __future__ import annotations

from contextlib import contextmanager

from . import publicref, refdata


class ForecastGap(ValueError):
    """A carried series lacks a target date in the forecast selected for this evaluation day."""

    def __init__(self, store, sku, day, run_id):
        self.store, self.sku, self.day, self.run_id = store, sku, day, run_id
        super().__init__(f'forecast gap: store {store}, product {sku}, day {day}, run {run_id}')


def latest_issued(ref) -> dict:
    """The four installed issue runs, selected by D-08 without refitting or reading future sales."""
    selected = {}
    for day in range(7):
        issued_day = min(day, 3)
        run = ref.forecast_runs.get(issued_day)
        if run is None or run['cutoff_day'] >= issued_day:
            raise ValueError(f'D-08 requires forecast run d{issued_day}, issued before day {issued_day}')
        selected[day] = run
    return selected


class EvaluationRef(publicref.PublicRef):
    def _check_points(self, day, store, sku, points):
        run_id = self._run_of_day.get(day)
        if run_id is not None:
            for target in range(day, int(self.calendar['horizon_end']) + 1):
                if target not in points:
                    raise ForecastGap(store, sku, target, run_id)

    def _load_points(self, day, store, sku):
        points, providers = super()._load_points(day, store, sku)
        if points or self.carries(store, sku):
            self._check_points(day, store, sku, points)
        return points, providers

    def forecast_of_product(self, day, sku):
        points = super().forecast_of_product(day, sku)
        if day in self._run_of_day:
            # Validate even a series with every point missing, using reference history's carrying set.
            stores = publicref._read('SELECT DISTINCT store FROM ref_sales_day WHERE dataset_version = :v '
                                     'AND sku = :k ORDER BY store', v=self.dataset_version, k=sku)
            for row in stores:
                store = row['store']
                self._check_points(day, store, sku, points.get(store, {}))
        return points


@contextmanager
def forecast_rule(dataset_version: str):
    """Use a fresh D-08 view for this context and restore its predecessor on every exit."""
    ref = refdata.get(dataset_version)
    if not isinstance(ref, publicref.PublicRef):
        raise ValueError('D-08 forecast_rule requires an installed public reference')
    view = ref.fresh_view(forecast_runs=latest_issued(ref), ref_type=EvaluationRef)
    with refdata.override(dataset_version, view):
        yield view
