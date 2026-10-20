from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fakeredis import FakeAsyncRedis

from factory_agent.api import create_app
from factory_agent.config import Settings
from factory_agent.worker import Worker


@pytest.fixture
def settings(tmp_path):
    return Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
        jwt_secret="test-secret-0123456789abcdef0123456789",
        admin_emails=["admin@example.com"],
        max_running_per_user=1,
        max_queued_per_user=3,
        solver_threads=2,
        lease_seconds=30,
        create_schema=True,
    )


@pytest.fixture
async def redis():
    r = FakeAsyncRedis()
    yield r
    await r.aclose()


@pytest.fixture
async def app(settings, redis):
    app = create_app(settings, redis=redis, run_worker=False)
    async with app.router.lifespan_context(app):
        yield app


@pytest.fixture
async def client(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
def worker(app, settings):
    pool = ThreadPoolExecutor(max_workers=1)
    yield Worker(app.state.db, app.state.queues.by_name["cpu"], settings, worker_id="test", executor=pool)
    pool.shutdown()


async def login(client, email="user@example.com", password="password123") -> dict:
    await client.post("/auth/register", json={"email": email, "password": password})
    r = await client.post("/auth/token", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture
def gpu_worker(app, settings):
    pool = ThreadPoolExecutor(max_workers=1)
    yield Worker(app.state.db, app.state.queues.by_name["gpu"], settings, worker_id="test-gpu", executor=pool, devices=["cpu"])
    pool.shutdown()
