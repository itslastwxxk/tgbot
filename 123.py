import asyncio
import logging
import os
from dotenv import load_dotenv
import redis.asyncio as redis

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL")
REDIS_TOKEN = os.getenv("REDIS_TOKEN")

async def migrate():
    client = redis.from_url(REDIS_URL, password=REDIS_TOKEN, decode_responses=True)
    await client.ping()
    logger.info("Redis connected.")

    count = 0
    async for key in client.scan_iter(match="user:*:balance", count=100):
        parts = key.split(":")
        if len(parts) != 3 or parts != "balance":
            continue

        user_id = int(parts)
        balance_key = key
        username_key = f"user:{user_id}:username"

        pipe = client.pipeline()
        pipe.get(balance_key)
        pipe.get(username_key)
        balance_raw, username_raw = await pipe.execute()

        balance = float(balance_raw) if balance_raw else 0.0
        username = (username_raw or "").lower()
        if not username:
            username = "без_username"

        # Пишем в хеш
        await client.hset(f"user:{user_id}", mapping={"balance": str(balance), "username": username})

        # Удаляем старые ключи
        await client.delete(balance_key, username_key)

        count += 1
        if count % 100 == 0:
            logger.info(f"Migrated {count} users...")

    logger.info(f"Migration complete. Total users migrated: {count}")

if __name__ == "__main__":
    asyncio.run(migrate())
