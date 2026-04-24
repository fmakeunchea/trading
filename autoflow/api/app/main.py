import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .incident_sync import run_forever as sync_forever
from .routers import (
    bot_control,
    health,
    incidents,
    kill_switch,
    orders,
    positions,
    smoke_test,
    strategies,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(sync_forever())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="AutoFlow Trader API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(positions.router)
app.include_router(orders.router)
app.include_router(incidents.router)
app.include_router(strategies.router)
app.include_router(smoke_test.router)
app.include_router(kill_switch.router)
app.include_router(bot_control.router)
