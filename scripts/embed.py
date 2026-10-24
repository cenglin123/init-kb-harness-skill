#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
embed.py — 生成笔记嵌入并存入 .meta/embeddings.sqlite

语义层是本套件的基础能力（office/非结构化仓库靠它召回）。来源二选一：
    http    任意 OpenAI 兼容 /embeddings 端点（本地 Ollama / vLLM / LM Studio，或云端）
    builtin 内置 fastembed 模型（bge-small-zh，兜底，零 API）

并发策略（效率优先）：
    http    跨文件线程池（MAINTAIN_CONCURRENCY），API 往返密集
    builtin 按 64 篇一批做扁平批量推理——ONNX 内部已多线程，再叠 Python 线程只会抢核

安全策略（不做自动清库）：
    · 先探针验证来源，再碰数据库——来源不可用时库原样不动
    · 配置/维度/分块与库不一致 → 拒绝增量，提示显式 --full
    · --full 先写入 embeddings_staging，**全部成功**后才在一条事务里换表
    · 单篇任一 chunk 失败即整篇放弃（保留旧行、不落新 hash），下一轮自动重试

用法:
    python .meta/scripts/embed.py            # 增量
    python .meta/scripts/embed.py --full     # 全量重建（配置变更后必须）
"""

import sys
import re
import sqlite3
import struct
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import (
    VAULT_ROOT, scan_indexable_notes, content_hash, rel_path,
    ENV, MAINTAIN_CONCURRENCY, llm_pipeline_check,
    embed_settings, embed_unavailable_hint, embed_chunk_defaults,
    get_embeddings_client,
)

DB_PATH = VAULT_ROOT / '.meta' / 'embeddings.sqlite'


def _env_int(name: str, default: int) -> int:
    raw = (ENV.get(name) or '').strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"  ⚠️  {name}='{raw}' 不是整数，回落 {default}")
        return default


def _chunk_params() -> tuple:
    d_chars, d_overlap = embed_chunk_defaults()
    chars = _env_int('EMBED_CHUNK_CHARS', d_chars)
    overlap = _env_int('EMBED_CHUNK_OVERLAP', d_overlap)
    if chars <= 0 or overlap < 0 or overlap >= chars:
        print(f"  ⚠️  分块参数非法（chars={chars}, overlap={overlap}），"
              f"回落 {d_chars}/{d_overlap}")
        chars, overlap = d_chars, d_overlap
    return chars, overlap


# 分块粒度可配（BM25 用自己固定的粒度，不受这里影响）
CHUNK_CHARS, CHUNK_OVERLAP = _chunk_params()
# builtin 扁平批量的文件批大小（控内存峰值）
FILE_BATCH = _env_int('EMBED_FILE_BATCH', 64)


def _sliding_chunks(text: str, chars: int = None, overlap: int = None) -> list:
    chars = CHUNK_CHARS if chars is None else chars
    overlap = CHUNK_OVERLAP if overlap is None else overlap
    if len(text) <= chars:
        return [text]
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + chars, len(text))
        chunks.append(text[start:end])
        if end == len(text):
            break
        start = end - overlap
    return chunks


def chunk_text(text: str, chars: int = None, overlap: int = None) -> list:
    """按 H2 标题分块；超长段落再按字符滑动切分。

    默认值跟嵌入来源走（http 3000 / builtin 450），可用 EMBED_CHUNK_* 覆盖。
    BM25 传自己的固定粒度，不受语义层配置影响。
    """
    h2_matches = list(re.finditer(r'(?m)^##\s', text))
    if not h2_matches:
        return _sliding_chunks(text, chars, overlap)

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
        chunks.extend(_sliding_chunks(section, chars, overlap))
    return chunks


def _create_embeddings_table(con, name: str):
    con.execute(f"""
        CREATE TABLE IF NOT EXISTS {name} (
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


def init_db():
    con = sqlite3.connect(str(DB_PATH))
    # WAL：允许 ask（读）与 embed（写）并行；busy_timeout 兜底
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    _create_embeddings_table(con, 'embeddings')
    con.execute("CREATE INDEX IF NOT EXISTS idx_hash ON embeddings(content_hash)")
    con.execute("""
        CREATE TABLE IF NOT EXISTS embed_meta (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            model TEXT NOT NULL,
            dim INTEGER NOT NULL,
            chunk_chars INTEGER NOT NULL,
            chunk_overlap INTEGER NOT NULL,
            provider TEXT NOT NULL,
            generated_at TEXT NOT NULL
        )
    """)
    # 兼容本会话早期版本建的 embed_meta（无 provider 列）
    cols = {r[1] for r in con.execute("PRAGMA table_info(embed_meta)")}
    if 'provider' not in cols:
        con.execute("ALTER TABLE embed_meta ADD COLUMN provider TEXT NOT NULL DEFAULT ''")
    con.commit()
    return con


def read_meta(con):
    try:
        row = con.execute(
            "SELECT model, dim, chunk_chars, chunk_overlap FROM embed_meta WHERE id=1"
        ).fetchone()
    except sqlite3.Error:
        return None
    if not row:
        return None
    return {'model': row[0], 'dim': row[1],
            'chunk_chars': row[2], 'chunk_overlap': row[3]}


def write_meta(con, model: str, dim: int, provider: str):
    con.execute("DELETE FROM embed_meta")
    con.execute(
        "INSERT INTO embed_meta (id, model, dim, chunk_chars, chunk_overlap, provider, generated_at) "
        "VALUES (1,?,?,?,?,?,?)",
        (model, dim, CHUNK_CHARS, CHUNK_OVERLAP, provider, datetime.now().isoformat()))


def vec_to_blob(vec: list) -> bytes:
    return struct.pack(f'{len(vec)}f', *vec)


def blob_to_embedding(blob: bytes) -> list:
    dim = len(blob) // 4
    return list(struct.unpack(f'{dim}f', blob))


def _write_file(con, table: str, rel: str, h: str, chunks: list, vecs: list,
                model: str) -> int:
    """写入一篇笔记的分块（全有或全无）。

    任一 chunk 缺向量 → 整篇不写（调用方保留旧行与旧 hash，下一轮重试），
    避免留下"缺块但 hash 已更新"的永久空洞。
    """
    if len(vecs) != len(chunks) or any(v is None for v in vecs):
        return -1
    con.execute(f"DELETE FROM {table} WHERE path=?", (rel,))
    now = datetime.now().isoformat()
    for idx, (chunk, vec) in enumerate(zip(chunks, vecs)):
        con.execute(
            f"INSERT INTO {table} VALUES (?,?,?,?,?,?,?)",
            (rel, idx, h, chunk, vec_to_blob(vec), model, now)
        )
    con.commit()
    return len(chunks)


def _collect_targets(db, table, full):
    """扫描语料，返回 (targets, current_paths, unreadable)。targets 元素 (rel,h,content)。"""
    processed = {}
    for row in db.execute(f"SELECT DISTINCT path, content_hash FROM {table}"):
        processed[row[0]] = row[1]

    targets, current_paths, unreadable = [], set(), []
    for md in scan_indexable_notes(scope='all'):
        rel = rel_path(md)
        current_paths.add(rel)

        allowed, reason = llm_pipeline_check(md)
        if not allowed:
            if reason == 'llm_skip':
                db.execute(f"DELETE FROM {table} WHERE path=?", (rel,))
            size_mb = md.stat().st_size / 1048576
            print(f"  [挂起·{reason}] {rel}（{size_mb:.2f} MB 超阈值）——本轮不嵌入，待用户决定")
            continue

        try:
            content = md.read_text(encoding='utf-8')
        except Exception as e:
            print(f"  [skip] 无法读取: {rel} ({e})")
            unreadable.append(rel)
            continue
        h = content_hash(md)
        if not full and processed.get(rel) == h:
            continue
        targets.append((rel, h, content))
    db.commit()
    return processed, targets, current_paths, unreadable


def _embed_threaded(client, targets):
    """HTTP：跨文件线程池。返回 {rel: (h, chunks, vecs)}。"""
    def _one(item):
        rel, h, content = item
        chunks = chunk_text(content)
        return rel, h, chunks, client.embed_batch(chunks)

    out = {}
    with ThreadPoolExecutor(max_workers=MAINTAIN_CONCURRENCY) as pool:
        futures = {pool.submit(_one, t): t[0] for t in targets}
        for done, fut in enumerate(as_completed(futures), 1):
            rel = futures[fut]
            try:
                rel, h, chunks, vecs = fut.result()
            except Exception as e:
                print(f"  [{done}/{len(targets)}] ✗ {rel} embedding 失败: {e}")
                out[rel] = (None, None, None)
                continue
            tag = f"(分块 {len(chunks)})" if len(chunks) > 1 else ""
            print(f"  [{done}/{len(targets)}] {rel} {tag}")
            out[rel] = (h, chunks, vecs)
    return out


def _embed_batched(client, targets):
    """builtin：按文件批做扁平批量推理（ONNX 内部多线程，避免线程争核）。"""
    out = {}
    total = len(targets)
    for base in range(0, total, FILE_BATCH):
        batch = targets[base:base + FILE_BATCH]
        chunked = [(rel, h, chunk_text(content)) for rel, h, content in batch]
        flat = [(rel, c) for rel, _, chunks in chunked for c in chunks]
        vecs = client.embed_batch([c for _, c in flat])
        it = iter(vecs)
        for rel, h, chunks in chunked:
            out[rel] = (h, chunks, [next(it, None) for _ in chunks])
        print(f"  [{min(base + len(batch), total)}/{total}] 已嵌入 {len(batch)} 篇"
              f"（{len(flat)} 块）")
    return out


def _activate(db, client, dim):
    """一条事务换表：删除旧向量 → 迁入 staging → 更新元数据。"""
    with db:
        db.execute("DELETE FROM embeddings")
        db.execute("INSERT INTO embeddings SELECT * FROM embeddings_staging")
        db.execute("DROP TABLE embeddings_staging")
        write_meta(db, client.model, dim, client.provider)


def main(full=False):
    cfg = embed_settings()
    if not cfg['enabled']:
        print(f"  ✗ 语义层未启用：{embed_unavailable_hint(cfg)}")
        # 显式 off 视为用户决定（返回 0），其余（缺依赖/半配置）视为配置错误
        return 0 if cfg['reason'] == 'off' else 1

    try:
        client = get_embeddings_client(cfg)
        dim = client.probe()   # 来源不可用在这里中止，库不会被改动
    except Exception as e:
        where = cfg['base'] if cfg['provider'] == 'http' else 'builtin'
        print(f"  ✗ 嵌入来源不可用（{where} · {cfg['model']}）：{e}")
        return 1

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = init_db()
    meta = read_meta(db)
    has_rows = db.execute("SELECT 1 FROM embeddings LIMIT 1").fetchone() is not None
    mismatch = (meta is None and has_rows) or (
        meta is not None and (
            meta['model'] != client.model or meta['dim'] != dim
            or meta['chunk_chars'] != CHUNK_CHARS
            or meta['chunk_overlap'] != CHUNK_OVERLAP))
    if mismatch and not full:
        old = ('无元数据' if meta is None else
               f"{meta['model']} · {meta['dim']} 维 · {meta['chunk_chars']}/{meta['chunk_overlap']}")
        print(f"  ✗ 嵌入配置与库不一致（库内：{old}；当前：{client.model} · {dim} 维 · "
              f"{CHUNK_CHARS}/{CHUNK_OVERLAP}）")
        print("    请显式重建：python .meta/scripts/embed.py --full"
              "（先写临时表，成功后才切换，旧向量在成功前不受影响）")
        db.close()
        return 2

    table = 'embeddings'
    if full:
        table = 'embeddings_staging'
        db.execute("DROP TABLE IF EXISTS embeddings_staging")
        _create_embeddings_table(db, table)
        db.commit()
        print(f"  ⟳ 全量重建：写入 {table}（完成前不触碰现用向量）")

    try:
        processed, targets, current_paths, unreadable = _collect_targets(db, table, full)

        # 清理已删除文件（增量路径；--full 靠换表天然清理）
        if not full:
            stale = [p for p in processed if p not in current_paths]
            if stale:
                print(f"  清理已删除的文件: {len(stale)} 条")
                for p in stale:
                    db.execute(f"DELETE FROM {table} WHERE path=?", (p,))
                db.commit()

        if not targets and not unreadable:
            print("  无需更新的笔记")
            if full:
                staged = db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
                if staged:
                    _activate(db, client, dim)
                    print(f"  ✓ 已切换为新一代向量（{client.model} · {dim} 维）")
                else:
                    print("  ⚠️  没有可嵌入的内容，保留原有向量不动（未切换）")
                    db.execute("DROP TABLE IF EXISTS embeddings_staging")
                    db.commit()
            elif read_meta(db) is None and not has_rows:
                write_meta(db, client.model, dim, client.provider)
                db.commit()
            db.close()
            return 0

        print(f"  待嵌入: {len(targets)} 篇（{client.provider} · {client.model} · "
              f"{dim} 维 · {'并发 ' + str(MAINTAIN_CONCURRENCY) if client.provider == 'http' else '扁平批量'}）")

        results = (_embed_batched if client.provider == 'builtin' else _embed_threaded)(client, targets)

        inserted_files = 0
        failed_files = list(unreadable)
        for n, (rel, h, content) in enumerate(targets, 1):
            h_res, chunks, vecs = results.get(rel, (None, None, None))
            if h_res is None:
                failed_files.append(rel)
                continue
            # 失败时保留旧 hash（下一轮重试），成功才落新 hash
            store_h = h if not any(v is None for v in vecs) else (processed.get(rel) or '')
            written = _write_file(db, table, rel, store_h, chunks, vecs, client.model)
            if written < 0:
                failed_files.append(rel)
                print(f"  ⚠  {rel}: 有分块未成功，保留旧向量并下轮重试")
            else:
                inserted_files += 1

        # --full 是"整套换代"：有任何一篇没写成功就不切换
        if inserted_files == 0 or (full and failed_files):
            print(f"  ✗ 本轮有 {len(failed_files)} 篇未写入成功，保留原库不动")
            if full:
                db.execute("DROP TABLE IF EXISTS embeddings_staging")
                db.commit()
            db.close()
            return 1

        if full:
            _activate(db, client, dim)
            print(f"  ✓ 已切换为新一代向量（{client.model} · {dim} 维）")
        elif read_meta(db) is None or meta is None:
            write_meta(db, client.model, dim, client.provider)
            db.commit()

        db.close()
        print(f"  ✓ 完成 embeddings.sqlite（{inserted_files} 篇）")
        return 0
    except BaseException:
        # 任何异常/中断都不留半成品 staging
        if full:
            try:
                db.execute("DROP TABLE IF EXISTS embeddings_staging")
                db.commit()
            except Exception:
                pass
        raise
    finally:
        try:
            db.close()
        except Exception:
            pass


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