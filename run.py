from sync_worker import start_worker
from web import create_app

app = create_app()
start_worker()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8099)
