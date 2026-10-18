
import os
import math
import time
import uuid
from collections import defaultdict, deque
from threading import Lock

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware


EMAIL = "23f1000224@ds.study.iitm.ac.in"
RATE_LIMIT = 9
WINDOW_SECONDS = 10

ASSIGNED_ORIGIN = "https://app-ihv8vx.example.com"

# Set this environment variable to the exact origin of your exam page
# if it differs from the default below.
EXAM_PAGE_ORIGIN = os.getenv(
    "EXAM_PAGE_ORIGIN",
    "https://exam.sanand.workers.dev",
)

ALLOWED_ORIGINS = list(set([
    ASSIGNED_ORIGIN,
    EXAM_PAGE_ORIGIN,
]))

api = FastAPI(title="Middleware Stack API")


# Middleware 3: Per-client rate limiter
class RateLimitMiddleware:
    def __init__(self, app):
        self.app = app
        self.requests = defaultdict(deque)
        self.lock = Lock()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # CORS preflight requests should not consume the rate limit.
        if scope["method"] == "OPTIONS":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        client_id = headers.get(b"x-client-id", b"anonymous").decode(
            "utf-8", errors="replace"
        )
        now = time.monotonic()

        with self.lock:
            bucket = self.requests[client_id]

            while bucket and now - bucket[0] >= WINDOW_SECONDS:
                bucket.popleft()

            if len(bucket) >= RATE_LIMIT:
                retry_after = max(
                    1,
                    math.ceil(WINDOW_SECONDS - (now - bucket[0])),
                )

                response = JSONResponse(
                    status_code=429,
                    content={"detail": "Rate limit exceeded"},
                    headers={"Retry-After": str(retry_after)},
                )

                await response(scope, receive, send)
                return

            bucket.append(now)

        await self.app(scope, receive, send)


# Middleware 2: CORS is configured below around the API.
# Only the exact configured origins receive ACAO headers.


# Middleware 1: Request context and response request ID
class RequestContextMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        incoming_id = headers.get(b"x-request-id")

        if incoming_id:
            request_id = incoming_id.decode(
                "utf-8", errors="replace"
            )
        else:
            request_id = str(uuid.uuid4())

        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_request_id(message):
            if message["type"] == "http.response.start":
                response_headers = list(message.get("headers", []))
                response_headers.append(
                    (b"x-request-id", request_id.encode("utf-8"))
                )
                message["headers"] = response_headers

            await send(message)

        await self.app(scope, receive, send_with_request_id)


@api.get("/ping")
async def ping(request: Request):
    return {
        "email": EMAIL,
        "request_id": request.state.request_id,
    }


# Compose the middleware stack.
# Request context is outermost so even 429 responses have X-Request-ID.
# CORS wraps the rate limiter so preflight and rate-limit responses
# receive the appropriate CORS headers.
rate_limited_api = RateLimitMiddleware(api)

cors_api = CORSMiddleware(
    app=rate_limited_api,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["X-Request-ID", "X-Client-Id", "Content-Type"],
    expose_headers=["X-Request-ID", "Retry-After"],
)

app = RequestContextMiddleware(cors_api)
