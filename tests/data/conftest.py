"""Session fixtures for the data tests, each world built once: the natural (evaluation) world on the release seed
number, and the demo release world (designed lots and skips on the same natural world)."""
import pytest

from data import config as C
from data import seed as S

EVALUATION_SEED = 20261012  # the first of scenarios.evaluation.seeds, the release seed number


@pytest.fixture(scope="session")
def release_world():
    """The World internals of the demo scenario on the demo seed (the release reference world)."""
    return S.run_world(C.load()["scenarios"]["demo"]["seed"], "demo")


@pytest.fixture(scope="session")
def release_pair():
    """(seed.json, truth.json) dicts of the release world, as `python -m data.seed` writes them."""
    return S.generate()


@pytest.fixture(scope="session")
def evaluation_world():
    """The World internals of the evaluation scenario on seed 20261012."""
    return S.run_world(EVALUATION_SEED, "evaluation")


@pytest.fixture(scope="session")
def evaluation_pair():
    """(seed.json, truth.json) dicts of the same world."""
    return S.generate(EVALUATION_SEED, "evaluation")
