from invoice_processing.db import session


def test_a_url_with_a_password_resolves_to_the_same_engine_twice():
    # create_engine connects lazily, so no server is needed. str(engine.url)
    # masks the password, which once made every call a pool rebuild.
    url = "postgresql+pg8000://user:secret@localhost:1/none"
    first = session.get_engine(url)
    try:
        assert session.get_engine(url) is first
    finally:
        first.dispose()
        session._engine = None
