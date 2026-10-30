"""분리형 앱의 health·readiness·사용자 API와 정적 화면을 제공한다."""
from contextlib import asynccontextmanager
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import Depends, FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
# [option:login]
from backend.auth import require_admin
# [/option:login]
# [option:db]
from backend import db
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
    result = {'ok': True, 'stage': 'starter', 'probe': os.environ.get('DEV_INITIALIZATION_PROBE', ''), 'db': 'disabled'}
    # [option:db]
    result['db'] = db.status()
    # [/option:db]
    return result


@app.get('/api/ready')
def ready():
    return health()


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
