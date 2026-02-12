"""
One-time script: creates a Daytona snapshot with a Next.js app pre-installed via bun.

    bun create next-app@latest clone-app --yes
    cd clone-app && bun add framer-motion lucide-react react-icons

That's the whole snapshot. On boot, just `bun dev` and you're live.

Run once:  python3 create_snapshot.py
"""

import os
import sys

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

api_key = os.getenv("DAYTONA_API_KEY")
if not api_key:
    print("ERROR: DAYTONA_API_KEY not set in .env")
    sys.exit(1)

from daytona_sdk import Daytona, DaytonaConfig, CreateSnapshotParams, Image, Resources

SNAPSHOT_NAME = "wc-nextjs-app-router"

daytona = Daytona(DaytonaConfig(api_key=api_key, target="us"))

# Delete if exists
try:
    existing = daytona.snapshot.get(SNAPSHOT_NAME)
    print(f"Snapshot '{SNAPSHOT_NAME}' exists (state: {existing.state}). Deleting...")
    daytona.snapshot.delete(existing)
    print("Deleted.")
except Exception:
    pass

image = (
    Image.base("node:20-slim")
    .run_commands(
        "apt-get update && apt-get install -y git curl && rm -rf /var/lib/apt/lists/*",
        "npm install -g bun",
    )
    .workdir("/home/daytona")
    .env({"CI": "true"})
    .run_commands(
        # Create Next.js app with all defaults (TS, Tailwind, App Router, ESLint)
        "bun create next-app@latest clone-app --yes 2>&1",
    )
    .workdir("/home/daytona/clone-app")
    .run_commands(
        # Add animation/icon libs
        "bun add framer-motion lucide-react react-icons 2>&1",
        # Verify
        "ls -la",
        "cat package.json",
    )
)

print(f"Creating snapshot '{SNAPSHOT_NAME}'...")
print("bun create next-app@latest clone-app --yes + extras\n")

snapshot = daytona.snapshot.create(
    CreateSnapshotParams(
        name=SNAPSHOT_NAME,
        image=image,
        resources=Resources(cpu=2, memory=4, disk=8),
    ),
    on_logs=lambda chunk: print(chunk, end=""),
    timeout=0,
)

print(f"\nSnapshot: {snapshot.name} (state: {snapshot.state})")
