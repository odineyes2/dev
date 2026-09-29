"""dev — 코딩 에이전트용 이슈 게시판(dev.lomebrote.com). 사람은 화면, 에이전트는 REST/MCP로 쓴다."""
from contextlib import asynccontextmanager

from fastapi import FastAPI

import db


@asynccontextmanager
async def lifespan(app):
    db.init()
    yield


app = FastAPI(title="dev", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/api/health")
def health():
    return {"ok": True}
