import os
from deaddit import create_app

app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("DEADDIT_WEB_PORT") or os.environ.get("PORT") or 5001)
    app.run(host="0.0.0.0", port=port)
