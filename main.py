from dotenv import load_dotenv
import os
import redis
from pymongo import MongoClient
from pymongo.errors import PyMongoError
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from redis.exceptions import RedisError
from pydantic import BaseModel, Field
from datetime import datetime
from typing import List
from urllib.parse import unquote
import logging
import json

# Load environment variables
load_dotenv()

# Environment Variables
MONGO_URL = os.getenv("MONGO_URL")
REDIS_HOST = os.getenv("REDIS_HOST")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6380"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD")

# FastAPI App Initialization
app = FastAPI()

# CORS Configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],  # Adjust if frontend deployed elsewhere
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Redis Setup
def get_redis_client():
    return redis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        password=REDIS_PASSWORD,
        ssl=True,  # Required for Azure Redis
        decode_responses=True
    )

# MongoDB Setup
def get_mongo_collections():
    client = MongoClient(MONGO_URL)
    db = client["game_leaderboard"]
    return db["scores"], db["score_history"]

# Initialize Redis and MongoDB Connections
r = get_redis_client()
scores_collection, history_collection = get_mongo_collections()

# Models
class PlayerScore(BaseModel):
    player: str = Field(..., min_length=1)
    score: int = Field(..., ge=0)

class LeaderboardEntry(BaseModel):
    rank: int
    player: str
    score: int

class HistoryItem(BaseModel):
    player: str
    score: int
    timestamp: datetime

# Health Check Endpoints
@app.get("/ping")
def ping():
    return {"status": "ok"}

@app.get("/redis-health")
def redis_health():
    try:
        r.ping()
        return {"status": "connected", "message": "Azure Redis is working!"}
    except RedisError as e:
        return {"status": "error", "message": str(e)}

@app.get("/mongo-health")
def mongo_health():
    try:
        scores_collection, _ = get_mongo_collections()
        count = scores_collection.count_documents({})
        return {"status": "connected", "documents_in_scores": count}
    except PyMongoError as e:
        return {"status": "error", "message": str(e)}

# API Endpoints

@app.get("/")
def root():
    return {"message": "FastAPI backend for Game Leaderboard is running!"}

@app.post("/score", response_model=dict)
def update_score(data: PlayerScore):
    try:
        r.zadd("leaderboard", {data.player: data.score})
        rank = r.zrevrank("leaderboard", data.player)
        score = r.zscore("leaderboard", data.player)

        scores_collection.update_one(
            {"player": data.player},
            {"$set": {"score": data.score}},
            upsert=True
        )

        timestamp = datetime.utcnow()
        history_collection.insert_one({
            "player": data.player,
            "score": data.score,
            "timestamp": timestamp
        })

        r.delete(f"history:{data.player}")

        return {
            "message": f"Score updated for {data.player}",
            "player": data.player,
            "rank": int(rank) + 1 if rank is not None else None,
            "score": int(score) if score is not None else None
        }
    except Exception as e:
        logging.error(f"Error updating score: {e}")
        raise HTTPException(status_code=500, detail="Internal Server Error")


@app.get("/leaderboard", response_model=List[LeaderboardEntry])
def get_leaderboard():
    try:
        top_players = r.zrevrange("leaderboard", 0, 9, withscores=True)
        return [
            {"rank": i + 1, "player": player, "score": int(score)}
            for i, (player, score) in enumerate(top_players)
        ]
    except Exception as e:
        logging.error(f"Error retrieving leaderboard: {e}")
        raise HTTPException(status_code=500, detail="Failed to retrieve leaderboard")


@app.get("/history/{player_name}", response_model=List[HistoryItem])
def get_player_history(player_name: str):
    try:
        player_name = unquote(player_name)
        cache_key = f"history:{player_name}"
        cached_data = r.get(cache_key)

        if cached_data:
            return json.loads(cached_data)

        history_cursor = history_collection.find(
            {"player": player_name}, {"_id": 0}
        ).sort("timestamp", -1)

history = [
    {
        "player": item["player"],
        "score": item["score"],
        "timestamp": (
            item["timestamp"].isoformat()
            if isinstance(item["timestamp"], datetime)
            else str(item["timestamp"])
        )
    }
    for item in history_cursor
]

        if history:
            r.setex(cache_key, 3600, json.dumps(history))

        return history

    except Exception as e:
        logging.error(f"Error fetching history for {player_name}: {e}")
        raise HTTPException(status_code=500, detail="Failed to retrieve history")


@app.get("/all-scores", response_model=List[dict])
def get_all_scores():
    try:
        return list(scores_collection.find({}, {"_id": 0}))
    except PyMongoError as e:
        logging.error(f"Mongo error: {e}")
        raise HTTPException(status_code=500, detail="Failed to get all scores")


@app.delete("/reset", response_model=dict)
def reset_all():
    try:
        r.delete("leaderboard")
        keys = r.keys("history:*")
        if keys:
            r.delete(*keys)

        scores_collection.delete_many({})
        history_collection.delete_many({})
        return {"message": "All leaderboard and history data reset successfully"}
    except Exception as e:
        logging.error(f"Error during reset: {e}")
        raise HTTPException(status_code=500, detail="Reset failed")


@app.delete("/history/{player}", response_model=dict)
def delete_player_history(player: str):
    try:
        player = unquote(player)
        result = history_collection.delete_many({"player": player})
        r.delete(f"history:{player}")
        return {"message": f"Deleted {result.deleted_count} history records for player '{player}'"}
    except Exception as e:
        logging.error(f"Error deleting history for {player}: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete player history")


@app.get("/cache/{key}", response_model=dict)
def get_redis_cache(key: str):
    try:
        value = r.get(key)
        if value is None:
            return {"key": key, "value": None, "message": "Key not found in Redis"}
        return {"key": key, "value": json.loads(value), "message": "Key found in Redis"}
    except Exception as e:
        logging.error(f"Error reading cache for key '{key}': {e}")
        raise HTTPException(status_code=500, detail="Failed to read Redis key")
