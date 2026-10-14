from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from nyerk import db
from nyerk.seed import seed, seed_party_finder, seed_reaction_groups

NY = ZoneInfo("America/New_York")


def ny(*args) -> int:
    return int(datetime(*args, tzinfo=NY).timestamp())


@pytest.fixture()
def s():
    db.init_engine(":memory:")
    with db.session() as session:
        seed(session)
        seed_party_finder(session)
        seed_reaction_groups(session, GUILD)
        session.flush()
        yield session


@pytest.fixture(autouse=True)
def _fresh_search_data():
    """Each test has its own database; don't reuse search data from another."""
    from nyerk.web import search
    search.forget_data()
    yield


from nyerk.seed import TEST_GUILD as GUILD  # noqa: E402  the tests' Discord server


def G(s):
    """The test server's first game (AION 2, from seed)."""
    from nyerk import games
    return games.first(s, GUILD)
