from fastapi import FastAPI

from movie_agent.api.routes import router

app = FastAPI(title="Movie Agent API", version="0.1.0")
app.include_router(router)
