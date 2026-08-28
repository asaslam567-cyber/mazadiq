"""تشغيل الموقع للإنتاج (Render وغيره)."""

import os

from waitress import serve

from app import app

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5050"))
    serve(app, host="0.0.0.0", port=port, threads=8, ident="mazad-alfadhli")
