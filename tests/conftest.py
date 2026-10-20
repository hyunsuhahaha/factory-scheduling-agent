import pytest
from fakeredis import FakeAsyncRedis


@pytest.fixture
async def redis():
    r = FakeAsyncRedis()
    yield r
    await r.aclose()
