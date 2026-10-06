"""Tiny signup service used by the Hermie demo."""
import re


def validate_phone(phone: str) -> bool:
    """Accept a US phone number such as 555-010-0101 or 5550100101."""
    # BUG: rejects numbers that contain dashes, so 555-010-0101 is refused.
    if "-" in phone:
        return False
    return re.fullmatch(r"\d{10}", phone) is not None


def validate_email(email: str) -> bool:
    return re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", email) is not None


def create_app():
    # Flask is imported here so that importing this module needs no dependency.
    from flask import Flask, jsonify, request

    app = Flask(__name__)

    @app.post("/signup")
    def signup():
        data = request.get_json(force=True)
        if not validate_email(data.get("email", "")):
            return jsonify(error="invalid email"), 400
        if not validate_phone(data.get("phone", "")):
            return jsonify(error="invalid phone"), 400
        return jsonify(ok=True), 201

    return app


if __name__ == "__main__":
    create_app().run(port=5000)
