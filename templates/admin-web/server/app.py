"""관리 도구의 health·사용자 API와 정적 화면을 제공한다."""
from contextlib import asynccontextmanager
import os
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
# [option:login]
from auth import require_admin
# [/option:login]
# [option:db]
import db
# [/option:db]

ROOT = Path(__file__).resolve().parent.parent


@asynccontextmanager
async def lifespan(app):
    # [option:db]
    db.initialize()
    # [/option:db]
    yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.get('/api/health')
def health():
    return {'ok': True, 'stage': 'starter', 'probe': os.environ.get('DEV_INITIALIZATION_PROBE', '')}


# [option:login]
@app.get('/api/me')
def me(user=Depends(require_admin)):
    return {'user': user}
# [/option:login]


@app.get('/')
def home():
    return FileResponse(ROOT / 'static' / 'index.html')


app.mount('/static', StaticFiles(directory=ROOT / 'static'), name='static')

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host=os.environ.get('HOST', '127.0.0.1'), port=int(os.environ.get('PORT', '{{PORT}}')))
