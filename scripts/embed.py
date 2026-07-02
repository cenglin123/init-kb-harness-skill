#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
embed.py — 生成笔记嵌入并存入 .meta/embeddings.sqlite

用法: python .meta/scripts/embed.py [--full]
"""

import sys
import re
import sqlite3
import struct
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import (
    VAULT_ROOT, scan_indexable_notes, content_hash, rel_path,
    ZhipuEmbedClient, ENV,
)

DB_PATH = VAULT_ROOT / '.meta' / 'embeddings.sqlite'
CHUNK_CHARS = 3000
CHUNK_OVERLAP = 300


def _sliding_chunks(text: str) -> list:
    if len(text) <= CHUNK_CHARS:
        return [text]
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + CHUNK_CHARS, len(text))
        chunks.append(text[start:end])
        if end == len(text):
            break
        start = end - CHUNK_OVERLAP
    return chunks


def chunk_text(text: str) -> list:
    """按 H2 标题分块；超长段落再按字符滑动切分。

    中文 1 char ≈ 0.7 token，3000 char ≈ 2100 token。
    """
    h2_matches = list(re.finditer(r'(?m)^##\s', text))
    if not h2_matches:
        return _sliding_chunks(text)

    sections = []
    if h2_matches[0].start() > 0:
        prefix = text[:h2_matches[0].start()]
        if prefix.strip():
            sections.append(prefix)

    for i, match in enumerate(h2_matches):
        start = match.start()
        end = h2_matches[i + 1].start() if i + 1 < len(h2_matches) else len(text)
        sections.append(text[start:end])

    chunks = []
    for section in sections:
        chunks.extend(_sliding_chunks(section))
    return chunks


def init_db():
    con = sqlite3.connect(str(DB_PATH))
    con.execute("""
        CREATE TABLE IF NOT EXISTS embeddings (
            path TEXT NOT NULL,
            chunk_index INTEGER NOT NULL,
            content_hash TEXT NOT NULL,
            chunk_text TEXT NOT NULL,
            embedding BLOB NOT NULL,
            model TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            PRIMARY KEY (path, chunk_index)
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_hash ON embeddings(content_hash)")
    con.commit()
    return con


def vec_to_blob(vec: list) -> bytes:
    return struct.pack(f'{len(vec)}f', *vec)


def blob_to_embedding(blob: bytes) -> list:
    dim = len(blob) // 4
    return list(struct.unpack(f'{dim}f', blob))


def main(full=False):
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = init_db()

    processed = {}
    for row in db.execute("SELECT DISTINCT path, content_hash FROM embeddings"):
        processed[row[0]] = row[1]

    targets = []
    current_paths = set()
    for md in scan_indexable_notes(scope='all'):
        rel = rel_path(md)
        current_paths.add(rel)
        try:
            content = md.read_text(encoding='utf-8')
        except Exception as e:
            print(f"  [skip] 无法读取: {rel} ({e})")
            continue
        h = content_hash(md)
        if not full and processed.get(rel) == h:
            continue
        targets.append((rel, h, content))

    # 清理已删除文件
    stale = [p for p in processed if p not in current_paths]
    if stale:
        print(f"  清理已删除的文件: {len(stale)} 条")
        for p in stale:
            db.execute("DELETE FROM embeddings WHERE path=?", (p,))
        db.commit()

    if not targets:
        print("  无需更新的笔记")
        db.close()
        return 0

    print(f"  待嵌入: {len(targets)} 篇")
    client = ZhipuEmbedClient()

    for i, (rel, h, content) in enumerate(targets, 1):
        db.execute("DELETE FROM embeddings WHERE path=?", (rel,))
        chunks = chunk_text(content)
        tag = f"(分块 {len(chunks)})" if len(chunks) > 1 else ""
        print(f"  [{i}/{len(targets)}] {rel} {tag}")
        vecs = client.embed_batch(chunks)
        now = datetime.now().isoformat()
        inserted = 0
        for idx, (chunk, vec) in enumerate(zip(chunks, vecs)):
            if vec is None:
                print(f"    [skip chunk {idx}] embedding 失败（可能 token 超限）")
                continue
            db.execute(
                "INSERT INTO embeddings VALUES (?,?,?,?,?,?,?)",
                (rel, idx, h, chunk, vec_to_blob(vec), client.model, now)
            )
            inserted += 1
        db.commit()
        if inserted == 0:
            print(f"    ⚠  本文件所有分块均失败，未写入任何向量")

    db.close()
    print(f"  ✓ 完成 embeddings.sqlite")
    return 0


if __name__ == "__main__":
    # --- host guard ---
    from pathlib import Path
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from common import is_primary_host
    if not is_primary_host():
        print("FATAL: this script must run on PRIMARY_HOST", file=sys.stderr)
        sys.exit(1)
    # --- /host guard ---
    full = '--full' in sys.argv
    sys.exit(main(full=full))
