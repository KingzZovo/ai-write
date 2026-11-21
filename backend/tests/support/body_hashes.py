"""SQLite-only emulation of the PostgreSQL a1001949 stored-hash invariant."""
import hashlib

from sqlalchemy import event


def install_sqlite_body_hashes(engine):
    if engine.dialect.name != 'sqlite':
        raise ValueError('test body-hash emulation requires SQLite')
    def connect(connection, _record):
        connection.create_function('sha256_utf8', 1,
            lambda value: hashlib.sha256((value or '').encode()).hexdigest())
    event.listen(engine, 'connect', connect)
    with engine.begin() as db:
        connect(db.connection.driver_connection, None)
        db.exec_driver_sql('''CREATE TRIGGER saved_hash_insert AFTER INSERT ON chapters BEGIN
            UPDATE chapters SET content_hash=sha256_utf8(NEW.content_text) WHERE id=NEW.id;
        END''')
        db.exec_driver_sql('''CREATE TRIGGER saved_hash_update AFTER UPDATE OF content_text, content_hash ON chapters
            WHEN NEW.content_hash IS NOT sha256_utf8(NEW.content_text) BEGIN
            UPDATE chapters SET content_hash=sha256_utf8(NEW.content_text) WHERE id=NEW.id;
        END''')
        db.exec_driver_sql('UPDATE chapters SET content_hash=sha256_utf8(content_text)')
