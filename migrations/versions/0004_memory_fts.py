"""0004_memory_fts: FTS5 index over memory_items + sync triggers.

The user query NEVER reaches the MATCH operator directly: retrieval builds a
sanitized MATCH expression from normalized tokens (see
jarvis.adapters.persistence.fts).

Tokenizer note: the review document proposed
``tokenize='unicode61 "remove_diacritics=2"'``, but that form is a parse error
on SQLite 3.45.1 (the runtime's version). ``remove_diacritics 2`` (space
separated, no '=' or quotes) is the documented directive form and gives the
same behavior: diacritic removal + case folding, so "situacao" finds
"Situação". Validated empirically before writing this migration.

Sync-trigger note: FTS5's special ``'delete'`` command
(``INSERT INTO ft(ft, ...) VALUES('delete', ...)``) only works on
contentless/external-content FTS5 tables — on a regular FTS5 table like
``memory_fts`` it raises ``OperationalError: SQL logic error`` (verified on
SQLite 3.45.1, 2026-09-30). The triggers below therefore use plain
``DELETE FROM memory_fts WHERE rowid = ...`` plus re-INSERT, keyed on the
content table's rowid (stable across UPDATEs; the TEXT primary key ``id``
is carried in the UNINDEXED ``item_id`` column for the retrieval JOIN).
Soft-deleted rows (status = 'deleted') are kept out of the index.
"""

from __future__ import annotations

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

_CREATE_FTS = (
    "CREATE VIRTUAL TABLE memory_fts "
    "USING fts5(item_id UNINDEXED, title, content, tokenize='unicode61 remove_diacritics 2')"
)

_SYNC_INSERT = """
CREATE TRIGGER memory_fts_ai
AFTER INSERT ON memory_items
BEGIN
    INSERT INTO memory_fts (rowid, item_id, title, content)
    VALUES (new.rowid, new.id, new.title, new.content);
END;
"""

_SYNC_DELETE = """
CREATE TRIGGER memory_fts_ad
AFTER DELETE ON memory_items
BEGIN
    DELETE FROM memory_fts WHERE rowid = old.rowid;
END;
"""

_SYNC_UPDATE = """
CREATE TRIGGER memory_fts_au
AFTER UPDATE OF title, content, status ON memory_items
WHEN old.title <> new.title OR old.content <> new.content OR old.status <> new.status
BEGIN
    DELETE FROM memory_fts WHERE rowid = old.rowid;
    INSERT INTO memory_fts (rowid, item_id, title, content)
    SELECT new.rowid, new.id, new.title, new.content
    WHERE new.status NOT IN ('deleted');
END;
"""


def upgrade() -> None:
    op.execute(_CREATE_FTS)
    op.execute(_SYNC_INSERT)
    op.execute(_SYNC_DELETE)
    op.execute(_SYNC_UPDATE)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS memory_fts_au")
    op.execute("DROP TRIGGER IF EXISTS memory_fts_ad")
    op.execute("DROP TRIGGER IF EXISTS memory_fts_ai")
    op.execute("DROP TABLE IF EXISTS memory_fts")
