import asyncio
from concurrent.futures import ProcessPoolExecutor
from io import BytesIO
from typing import Literal
from uuid import uuid4

from fastapi import FastAPI, UploadFile, HTTPException, Request, Header, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from cachetools import TTLCache


project_path = 'model'

app = FastAPI()
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter


def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded):
    response = JSONResponse(
        {'detail': f'Rate limit exceeded: {exc.detail}'}, status_code=429
    )
    response = request.app.state.limiter._inject_headers(
        response, request.state.view_rate_limit
    )
    return response


app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)

processpool_executor = ProcessPoolExecutor(max_workers=3)
index_file = open('index.html', 'r').read()


@app.get(
    '/',
    response_class=HTMLResponse,
    description='The home page.',
)
def get():
    return index_file


class StatusResponse(BaseModel):
    status: Literal['ok']


@app.get(
    '/api/status',
    response_model=StatusResponse,
    description='A dummy endpoint.',
)
def get_status():
    return StatusResponse(status='ok')


def evaluate_image(*args):
    from deepdanbooru import deepdanbooru
    print('evaluating image')
    dd_model = deepdanbooru.project.load_model_from_project(project_path, compile_model=False)
    dd_tags = deepdanbooru.project.load_tags_from_project(project_path)
    return [*deepdanbooru.commands.evaluate_image(*args, dd_model, dd_tags, 0.5)]


class CheckImageResultResponse(BaseModel):
    class ResultEntry(BaseModel):
        tag: str
        score: float
    result: list[ResultEntry]


def valid_upload_length(content_length: int = Header(..., lt=20_000_000)):  # ~20MB
    return content_length


@app.post(
    '/api/check-image',
    responses={
        200: { 'model': CheckImageResultResponse },
        500: {},
    },
    dependencies=[Depends(valid_upload_length)],
    description='Synchronous API to obtain the tags for an image. User uploads an image to this endpoint '
                'and waits for result to be available. Requests may fail with timeout if the processing time '
                'is too long.',
)
@limiter.limit('2/minute')
async def post_check_image(request: Request, file: UploadFile):
    content = await file.read()
    try:
        result = await asyncio.get_event_loop().run_in_executor(
            processpool_executor,
            evaluate_image,
            BytesIO(content)
        )
        return { 'result': [ { 'tag': k, 'score': float(v) } for k, v in result ]  }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f'error in processing: {e.args[0]}') from e


# NOTE: this stops working if one has many workers
# token => (error, result)
check_image_async_results = TTLCache(maxsize=1000, ttl=600) # seconds


class CheckImageResultTokenResult(BaseModel):
    token: str


@app.post(
    '/api/check-image-async',
    status_code=201,
    responses={
        201: { 'model': CheckImageResultTokenResult },
    },
    dependencies=[Depends(valid_upload_length)],
    description='Asynchronous API to obtain the tags for an image. User uploads an image to this endpoint '
                'and obtains a token. They need to use this token to poll for the results.',
)
@limiter.limit('2/minute')
async def post_check_image_async(request: Request, file: UploadFile):
    content = await file.read()
    token = str(uuid4())

    async def task():
        try:
            #result = [('1girl', 0.9999815), ('brown_eyes', 0.9935829), ('brown_hair', 0.9981822), ('cloud', 0.8572262), ('coin', 0.8918674), ('day', 0.880931), ('flower', 0.6760002), ('hair_flower', 0.5090158), ('hair_ornament', 0.82948065), ('lens_flare', 0.901165), ('moon', 0.68162006), ('outdoors', 0.77569973), ('school_uniform', 0.9495816), ('short_hair', 0.87471175), ('sky', 0.9528719), ('solo', 0.99046326), ('sun', 0.9986738), ('sweater_vest', 0.8973359), ('upper_body', 0.84423125), ('v-neck', 0.776837), ('white_flower', 0.61222994), ('misaka_mikoto', 0.9999269), ('rating:safe', 0.999196)]
            result = await asyncio.get_event_loop().run_in_executor(
                processpool_executor,
                evaluate_image,
                BytesIO(content)
            )
            check_image_async_results[token] = (None, result)
        except Exception as e:
            check_image_async_results[token] = (f'{e.args[0]}', None)

    asyncio.create_task(task())
    check_image_async_results.expire()
    check_image_async_results[token] = (None, None)
    return JSONResponse({ 'token': token }, status_code=201)


@app.get(
    '/api/check-image-async',
    responses={
        200: { 'model': CheckImageResultResponse },
        204: {},
        404: {},
        500: {},
    },
    description='Gets the tag estimation results using the token.',
)
async def get_check_image_async(token: str):
    if (value := check_image_async_results.get(token)) is not None:
        error, result = value
        if error is None and result is None:  # waiting
            raise HTTPException(status_code=204, detail='still processing')
        elif error is not None:
            raise HTTPException(status_code=500, detail=f'error in processing: {error}')
        return { 'result': [ { 'tag': k, 'score': float(v) } for k, v in result ]  }
    raise HTTPException(status_code=404, detail='there is no tasks with this token or it has expired')

