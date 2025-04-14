
from flask import Flask
app = Flask(__name__)

@app.route("/")
def index():
    return "HybridBot Render Deployment Live!"

if __name__ == "__main__":
    app.run()
