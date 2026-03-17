from app.security import SecretBox


def test_secret_box_roundtrip() -> None:
    box = SecretBox()
    encrypted = box.encrypt("super-secret")
    assert encrypted != "super-secret"
    assert box.decrypt(encrypted) == "super-secret"
