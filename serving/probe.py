import time

import requests

BASE = "http://localhost:8000/v1"


def wait_ready(timeout: int = 600) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        try:
            r = requests.get(f"{BASE}/models", timeout=5)
            if r.status_code == 200:
                return True
        except requests.RequestException:
            pass
        time.sleep(5)
    return False


def probe() -> None:
    healthy = wait_ready()
    print("healthy" if healthy else "unhealthy")
    raise SystemExit(0 if healthy else 1)


if __name__ == "__main__":
    probe()