"""تشغيل الموقع للإنتاج (Render وغيره)."""

import logging
import os

from waitress import serve

from app import DATA_DIR, UPLOAD_DIR, app, init_db

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("mazad")


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", "5050"))
    log.info("Starting on port %s data=%s uploads=%s", port, DATA_DIR, UPLOAD_DIR)
    serve(
        app,
        host="0.0.0.0",
        port=port,
        threads=12,
        ident="mazad-alfadhli",
        channel_timeout=180,
        url_scheme="https" if os.environ.get("RENDER") else "http",
    )
