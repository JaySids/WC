import os
import time
from dotenv import load_dotenv
from daytona_sdk import Daytona, DaytonaConfig, CreateSandboxFromSnapshotParams

# 1. Load API Key
load_dotenv()
api_key = os.getenv("DAYTONA_API_KEY")

if not api_key:
    raise ValueError("Daytona_API_KEY not found in .env file.")

# 2. Initialize Client
config = DaytonaConfig(api_key=api_key)
daytona = Daytona(config)

def host_public_react_server():
    print("🚀 Creating public sandbox from Next.js template...")
    
    try:
        params = CreateSandboxFromSnapshotParams(
            language="typescript",
            repository_url="https://github.com/vercel/next.js/tree/canary/examples/hello-world",
            public=True 
        )
        
        sandbox = daytona.create(params)
        print(f"✅ Sandbox created: {sandbox.id}")

        # 3. Setup Environment
        print("📦 Running: npm install (this may take a minute)...")
        sandbox.process.exec("npm install")
        
        print("⚡ Starting Next.js in background...")
        # Using '&' at the end of the string tells the shell to run it in the background
        # 'nohup' ensures it keeps running even if the initial session detaches
        sandbox.process.exec("nohup npm run dev > /dev/null 2>&1 &")

        # 4. Final Output
        print("⏳ Waiting for server to compile...")
        time.sleep(15) # Next.js takes a bit longer to be ready for requests

        preview_info = sandbox.get_preview_link(3000)
        
        print("\n" + "="*60)
        print(f"🌐 PUBLIC PREVIEW URL:")
        print(f"   {preview_info.url}")
        print("="*60)
        print("Success! Anyone can now access this React server.")
        
    except Exception as e:
        print(f"❌ Error: {e}")

if __name__ == "__main__":
    host_public_react_server()