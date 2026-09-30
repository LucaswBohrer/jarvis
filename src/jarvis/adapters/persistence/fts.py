"""FTS5 query building and index maintenance for memory retrieval.

Security rule: the user's raw query text NEVER reaches the MATCH operator.
It is normalized (lowercase, accent-stripped, [^a-z0-9] -> space), split into
tokens, truncated, and rebuilt as a conjunction of quoted prefix terms:
`"tok1"* "tok2"*`. No user input can inject FTS5 syntax — quotes, parens,
colons, NEAR/OR operators and column filters are all destroyed by the
normalizer before the MATCH expression is assembled.

The unicode61 tokenizer is configured with `remove_diacritics 2`, so indexed
tokens are already folded the same way: "situacao" matches "Situação".
"""

from __future__ import annotations

import unicodedata

from sqlalchemy import text

from .database import Database

# Hard caps for the rebuilt MATCH expression. MemoryQuery already bounds the
# raw query at 500 chars; these caps bound what reaches MATCH.
MAX_FTS_TOKENS = 10
MAX_TOKEN_CHARS = 32

REBUILD_SQL = "INSERT INTO memory_fts(memory_fts) VALUES('rebuild')"


def normalize_text(text: str) -> str:
    """Lowercase, strip diacritics, keep only a-z0-9 runs separated by spaces."""
    folded = unicodedata.normalize("NFKD", text.casefold())
    stripped = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return "".join(ch if ("a" <= ch <= "z" or "0" <= ch <= "9") else " " for ch in stripped)


def build_fts_match_query(user_text: str) -> str:
    """Build a safe MATCH expression from raw user text.

    Returns "" when no usable token survives normalization (caller must then
    skip the FTS query and return []).
    """
    tokens = normalize_text(user_text).split()
    terms = [token[:MAX_TOKEN_CHARS] for token in tokens[:MAX_FTS_TOKENS] if token]
    if not terms:
        return ""
    return " ".join(f'"{term}"*' for term in terms)


async def rebuild_memory_fts(db: Database) -> int:
    """Rebuild the FTS index from memory_items. Idempotent.

    Returns the number of rows in memory_fts after the rebuild.
    """
    async with db.session() as session:
        async with session.begin():
            await session.execute(text(REBUILD_SQL))
            result = await session.execute(text("SELECT COUNT(*) FROM memory_fts"))
            count: int = int(result.scalar_one())
    return count
