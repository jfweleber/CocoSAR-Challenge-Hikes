from app import create_app
from app.config import ProdConfig

app = create_app(ProdConfig)
