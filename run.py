import sys

def print_usage():
    print("Competitor Monitoring System CLI Runner (Python Backend)")
    print("Usage:")
    print("  python run.py scan     - Run a manual daily crawler scan cycle")
    print("  python run.py cron     - Run the scheduled background cron daemon")
    print("  python run.py server   - Run the FastAPI backend API server")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print_usage()
        sys.exit(1)

    action = sys.argv[1].lower()

    if action == "scan":
        import asyncio
        from src.pipeline import run_scan
        asyncio.run(run_scan())
    elif action == "cron":
        import subprocess
        subprocess.run([sys.executable, "-m", "src.scheduler.cron"])
    elif action == "server":
        import uvicorn
        # reload=False avoids orphaned Windows workers that keep serving stale code on :3456
        uvicorn.run("src.dashboard.server:app", host="0.0.0.0", port=3456, reload=False)
    else:
        print(f"Unknown action: {action}")
        print_usage()
        sys.exit(1)
