"""공개 게임의 health·점수 API와 정적 화면을 제공한다."""
from contextlib import asynccontextmanager
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
# [option:scores]
from pydantic import BaseModel, Field
import db

class Score(BaseModel):
    name: str = Field(min_length=1, max_length=20, pattern=r'\S')
    score: int = Field(ge=0, le=1_000_000, strict=True)
# [/option:scores]

ROOT = Path(__file__).resolve().parent.parent


@asynccontextmanager
async def lifespan(app):
    # [option:scores]
    db.initialize()
    # [/option:scores]
    yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.get('/api/health')
def health():
    return {'ok': True, 'stage': 'starter', 'probe': os.environ.get('DEV_INITIALIZATION_PROBE', '')}


# [option:scores]
@app.get('/api/scores')
def scores():
    return db.top_scores()


@app.post('/api/scores')
def save_score(value: Score):
    db.save(value.name.strip(), value.score)
    return {'ok': True}
# [/option:scores]


@app.get('/')
def home():
    return FileResponse(ROOT / 'static' / 'index.html')


app.mount('/static', StaticFiles(directory=ROOT / 'static'), name='static')

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host=os.environ.get('HOST', '127.0.0.1'), port=int(os.environ.get('PORT', '{{PORT}}')))
