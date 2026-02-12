import os
import time
from dotenv import load_dotenv
from daytona import Daytona, DaytonaConfig, CreateSandboxFromSnapshotParams

# 1. Setup Environment
load_dotenv()
api_key = os.getenv("DAYTONA_API_KEY")

if not api_key:
    raise ValueError("❌ DAYTONA_API_KEY is missing from your .env file!")

config = DaytonaConfig(api_key=api_key)
daytona = Daytona(config)

print("🚀 Initializing Public Daytona Sandbox...")

# THE FIX: Defining public=True at creation time
params = CreateSandboxFromSnapshotParams(
    language="typescript",
    public=True
)

sandbox = daytona.create(params)

def run_cmd(command):
    """Helper to execute and return clean output."""
    res = sandbox.process.exec(command)
    return res.result.strip()

try:
    # 2. Preparation & Cleanup
    print(f"✅ Sandbox Created: {sandbox.id}")
    print("🧹 Cleaning up existing processes and ensuring Bun is ready...")
    run_cmd("pkill -f next || true")
    run_cmd("pkill -f bun || true")
    
    # Install/Update Bun
    run_cmd("curl -fsSL https://bun.sh/install | bash")
    # Adding bun to path for the current session
    BUN_BIN = "/home/daytona/.bun/bin/bun"
    
    # 3. Project Scaffolding
    PROJECT_PATH = "/home/daytona/my-app"
    if "MISSING" in run_cmd(f"ls {PROJECT_PATH} || echo 'MISSING'"):
        print("📦 Creating Next.js project (this takes ~30-60s)...")
        # Using the absolute path to bun to avoid command not found errors
        run_cmd(f"{BUN_BIN} create next-app@latest {PROJECT_PATH} --typescript --tailwind --eslint --app --use-bun --yes")
    
    # 4. Dependency Installation
    print("🛠️ Mending dependencies (running bun install)...")
    run_cmd(f"{BUN_BIN} install --cwd {PROJECT_PATH}")

    # 5. Start Next.js Server
    print("🔥 Starting Next.js server on 0.0.0.0:3000...")
    LOG_FILE = f"{PROJECT_PATH}/server.log"
    # -H 0.0.0.0 is critical for the proxy to route traffic correctly
    start_cmd = (
        f"nohup {BUN_BIN} --cwd {PROJECT_PATH} --bun next dev -p 3000 -H 0.0.0.0 > {LOG_FILE} 2>&1 &"
    )
    run_cmd(start_cmd)

    # 6. Public Exposure Link
    # Since we set public=True in params, this link will be accessible to everyone
    preview = sandbox.get_preview_link(3000)
    
    print("\n" + "="*60)
    print(f"✅ SETUP SUCCESSFUL - PUBLIC ACCESS ENABLED")
    print(f"🌐 ACCESS URL: {preview.url}")
    print("="*60)

    # 7. Real-time Log Tailing
    print("📋 Tailing logs (Waiting for server to be ready)...")
    last_log_content = ""
    
    while True:
        current_logs = run_cmd(f"tail -n 15 {LOG_FILE} || echo 'Waiting for log file...'")
        
        if current_logs != last_log_content:
            new_lines = current_logs.replace(last_log_content, "").strip()
            if new_lines:
                print(f"[SERVER]: {new_lines}")
            last_log_content = current_logs
            
        time.sleep(2)

except KeyboardInterrupt:
    print("\n👋 Log tailing stopped. The server remains active and public in the cloud.")
except Exception as e:
    print(f"❌ Script Error: {e}")