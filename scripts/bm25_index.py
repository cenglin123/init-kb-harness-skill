#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bm25_index.py — BM25 稀疏索引构建与查询（零 API、零嵌入库依赖）

数据源：直接扫描 vault 语料（用户笔记 + office 提取 sidecar + memory，
经 common.scan_indexable_notes），按 embed.chunk_text 相同规则分块。
不依赖 embeddings.sqlite——默认安装即可用。
分块粒度可配（EMBED_CHUNK_CHARS/OVERLAP），缓存记录分块签名，变更后自动全量重算。

v2 实现（index_version=2）：
- 倒排索引：postings 按 term_id 组织，查询只遍历查询词的 posting 链
  （v1 为全库线性扫描，语料增长后查询秒级 → 毫秒级）
- 增量构建：文档词频缓存 bm25_doc_cache.json.gz 记录每文件内容 hash，
  --build 只重新分词变化的文件，未变文件直接复用缓存（维护不再全量重扫）
- 并行分词：语料文件数达到阈值时用 ProcessPoolExecutor 按文件粒度分发
  （worker 为纯函数，Windows spawn 安全；并行失败自动回退串行）
- 分词瘦身：中文去掉 unigram，保留整词 + bigram，token 量约减 40%

用法:
    python .meta/scripts/bm25_index.py --build      # 增量构建（利用词频缓存）
    python .meta/scripts/bm25_index.py --build --rebuild  # 忽略缓存全量重建
    python .meta/scripts/bm25_index.py --query "xxx" --top-k 10  # BM25 检索
"""

import sys, json, argparse, re, gzip, os, hashlib, tempfile, pickle
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))
from common import VAULT_ROOT, scan_indexable_notes, rel_path, ScanCache
from embed import chunk_text

# v2.1：序列化用 pickle（gzip 压缩）。索引/缓存是纯基础类型结构，json 逐键编码
# 曾占增量构建 87% 耗时（cProfile 实测 40.6s/46.3s）；pickle C 实现快一个量级。
# protocol=4，Python 3.8+ 全兼容。旧 .json.gz 文件在构建时清理。
BM25_INDEX_PATH = VAULT_ROOT / '.meta' / 'bm25_index.pkl.gz'
BM25_CACHE_PATH = VAULT_ROOT / '.meta' / 'bm25_doc_cache.pkl.gz'
_LEGACY_JSON_FILES = (VAULT_ROOT / '.meta' / 'bm25_index.json.gz',
                      VAULT_ROOT / '.meta' / 'bm25_doc_cache.json.gz')

INDEX_VERSION = 2
CACHE_VERSION = 2
# BM25 用自己固定的分块粒度：不能被可选语义层的 provider 改变——否则开/关语义层
# 会静默改变基础检索的粒度与排序。签名只随这里的常量变。
BM25_CHUNK_CHARS = 3000
BM25_CHUNK_OVERLAP = 300
CHUNK_SIG = f'bm25-{BM25_CHUNK_CHARS}/{BM25_CHUNK_OVERLAP}'
K1 = 1.5
B = 0.75
# 语料文件数低于该阈值时串行分词（进程池启动开销不划算）；否则并行
PARALLEL_MIN_FILES = 32

_TOKEN_RE = re.compile(r'[一-鿿]+|[a-zA-Z0-9]+')


def _write_gzip_atomically(path: Path, payload):
    """pickle + gzip write, then atomically publish it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=path.parent,
        prefix=f'.{path.name}.',
        suffix='.tmp',
        delete=False,
    ) as tmp:
        temp_path = Path(tmp.name)
    try:
        with gzip.open(temp_path, 'wb') as f:
            pickle.dump(payload, f, protocol=4)
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def tokenize(text):
    """Simple tokenizer: split on whitespace + Chinese character-level bigrams.

    中文 token 产出整词 + 相邻二元组（不产 unigram：IDF 低、噪声大，token 量省约 40%）。
    """
    result = []
    for token in _TOKEN_RE.findall(text.lower()):
        result.append(token)
        if not token.isascii() and len(token) > 1:
            for i in range(len(token) - 1):
                result.append(token[i:i + 2])
    return result


def _tokenize_texts(texts):
    """进程池 worker：纯函数，无模块级依赖（Windows spawn 只 pickle 本函数）。"""
    return [tokenize(t) for t in texts]


def _term_freqs(tokens):
    tf = {}
    for t in tokens:
        tf[t] = tf.get(t, 0) + 1
    return tf


def _load_cache():
    """载入文档词频缓存；缺失或版本不符时返回空（触发全量重算）。"""
    if not BM25_CACHE_PATH.exists():
        return {}
    try:
        with gzip.open(BM25_CACHE_PATH, 'rb') as f:
            cache = pickle.load(f)
        if cache.get('cache_version') != CACHE_VERSION:
            return {}
        if cache.get('chunk_sig') != CHUNK_SIG:
            print("  [cache] 分块规则已变化，词频缓存作废，全量重算", file=sys.stderr)
            return {}
        return cache.get('docs', {})
    except Exception as e:
        print(f"  [cache] 词频缓存损坏，忽略（{e}）", file=sys.stderr)
        return {}


def build_index(rebuild=False):
    """扫描 vault 语料构建 BM25 索引。rebuild=True 时忽略缓存全量重算。"""
    start = datetime.now()

    scan_cache = ScanCache()
    cache = {} if rebuild else _load_cache()

    # ── 1 & 2. 借助共享 scan-cache 分类：缓存命中免读正文 / 需重算 ──
    corpus_rels_order = []  # [rel, ...] 保持扫描顺序
    cached_docs = {}        # rel -> cache entry（hash 命中）
    pending = []            # [(rel, content, h)] 需要分词
    for md in scan_indexable_notes(scope='all'):
        rel = rel_path(md)
        try:
            h = scan_cache.get_sha256(md, rel)
        except Exception as e:
            print(f"  [skip] 无法读取: {rel} ({e})", file=sys.stderr)
            continue
        entry = cache.get(rel)
        if not rebuild and entry and entry.get('hash') == h:
            cached_docs[rel] = entry
            corpus_rels_order.append(rel)
        else:
            try:
                content = md.read_text(encoding='utf-8')
            except Exception as e:
                print(f"  [skip] 无法读取: {rel} ({e})", file=sys.stderr)
                continue
            pending.append((rel, content, h))
            corpus_rels_order.append(rel)

    scan_cache.save()

    # ── 3. 对待算文件分块 + 分词（文件数达标时并行） ──
    chunks_by_rel = {}  # rel -> [[chunk_idx, doc_len, {term: freq}], ...]
    if pending:
        texts_by_rel = {rel: chunk_text(content, BM25_CHUNK_CHARS, BM25_CHUNK_OVERLAP)
                        for rel, content, _ in pending}
        token_lists = {}  # rel -> [[token, ...], ...]
        if len(pending) >= PARALLEL_MIN_FILES:
            try:
                from concurrent.futures import ProcessPoolExecutor
                workers = max(1, (os.cpu_count() or 2) - 1)
                with ProcessPoolExecutor(max_workers=workers) as pool:
                    for (rel, _, _), per_chunk in zip(
                            pending, pool.map(_tokenize_texts,
                                              [texts_by_rel[rel] for rel, _, _ in pending],
                                              chunksize=1)):
                        token_lists[rel] = per_chunk
            except Exception as e:
                print(f"  [parallel] 并行分词失败，回退串行（{e}）", file=sys.stderr)
                token_lists = {}
        for rel, _, h in pending:
            if rel not in token_lists:
                token_lists[rel] = _tokenize_texts(texts_by_rel[rel])
            chunks_payload = [
                {'idx': idx, 'len': len(tokens), 'tf': _term_freqs(tokens)}
                for idx, tokens in enumerate(token_lists[rel])
            ]
            chunks_by_rel[rel] = chunks_payload
            cache[rel] = {'hash': h, 'chunks': chunks_payload}

    # ── 4. 合并（缓存命中 + 新算），组装倒排索引 ──
    corpus_rels = set(corpus_rels_order)
    cache = {rel: cache[rel] for rel in cache if rel in corpus_rels}  # 清理已删除文件的缓存残留

    def iter_doc_chunks():
        for rel in corpus_rels_order:
            entry = cached_docs.get(rel)
            if entry is not None:
                for ch in entry['chunks']:
                    yield rel, ch['idx'], ch['len'], ch['tf']
            else:
                for ch in chunks_by_rel[rel]:
                    yield rel, ch['idx'], ch['len'], ch['tf']

    term_to_id = {}
    doc_freqs = {}
    docs_paths, docs_chunks, docs_lens = [], [], []
    postings = []  # tid -> [[doc_idx, tf], ...]
    total_dl = 0

    for rel, idx, dl, tf in iter_doc_chunks():
        doc_i = len(docs_paths)
        docs_paths.append(rel)
        docs_chunks.append(idx)
        docs_lens.append(dl)
        total_dl += dl
        for term, freq in tf.items():
            tid = term_to_id.get(term)
            if tid is None:
                tid = len(term_to_id)
                term_to_id[term] = tid
                postings.append([])
            postings[tid].append([doc_i, freq])
            doc_freqs[tid] = doc_freqs.get(tid, 0) + 1

    N = len(docs_paths)
    if N == 0:
        # 语料为空时清除旧索引与缓存，避免命中已删除内容的陈旧结果
        BM25_INDEX_PATH.unlink(missing_ok=True)
        BM25_CACHE_PATH.unlink(missing_ok=True)
        print("⚠️  语料为空（无笔记 / office 提取件 / memory），已清除 BM25 索引与词频缓存。")
        return

    idf_list = [0.0] * len(term_to_id)
    for tid, df in doc_freqs.items():
        idf_list[tid] = max(0.0, (N - df + 0.5) / (df + 0.5))

    index = {
        'index_version': INDEX_VERSION,
        'k1': K1, 'b': B,
        'N': N, 'avgdl': total_dl / N,
        'term_to_id': term_to_id,
        'idf': idf_list,
        'postings': postings,
        'docs_paths': docs_paths,
        'docs_chunks': docs_chunks,
        'docs_lens': docs_lens,
        'generated_at': datetime.now().isoformat(),
    }

    _write_gzip_atomically(BM25_INDEX_PATH, index)
    _write_gzip_atomically(BM25_CACHE_PATH, {
        'cache_version': CACHE_VERSION, 'chunk_sig': CHUNK_SIG, 'docs': cache})
    for legacy in _LEGACY_JSON_FILES:
        legacy.unlink(missing_ok=True)  # v2.0 json 格式残留清理
    elapsed = (datetime.now() - start).total_seconds()
    reused = len(cached_docs)
    recomputed = len(pending)
    print(f"BM25 索引构建完成：{N} 文档，{len(term_to_id)} 词项，{elapsed:.1f}s"
          f"（复用缓存 {reused}，重新分词 {recomputed}）")


_INDEX_CACHE = {}  # (resolved_path_str, mtime_ns, size) -> index dict


def _load_index():
    if not BM25_INDEX_PATH.exists():
        print("错误：bm25_index.pkl.gz 不存在，请先运行 --build", file=sys.stderr)
        return None
    try:
        st = BM25_INDEX_PATH.stat()
        cache_key = (str(BM25_INDEX_PATH.resolve()), st.st_mtime_ns, st.st_size)
    except OSError as e:
        print(f"错误：索引读取失败（{e}），请运行 --build 重建", file=sys.stderr)
        return None

    cached = _INDEX_CACHE.get(cache_key)
    if cached is not None:
        return cached

    try:
        with gzip.open(BM25_INDEX_PATH, 'rb') as f:
            index = pickle.load(f)
    except Exception as e:
        print(f"错误：索引读取失败（{e}），请运行 --build 重建", file=sys.stderr)
        return None
    if index.get('index_version') != INDEX_VERSION:
        print("错误：索引为旧版本格式，请运行 --build 重建（增量缓存会自动迁移）", file=sys.stderr)
        return None

    path_str = cache_key[0]
    for old_key in [k for k in _INDEX_CACHE if k[0] == path_str]:
        _INDEX_CACHE.pop(old_key, None)
    _INDEX_CACHE[cache_key] = index
    return index


def search(query, top_k=10):
    """BM25 检索，返回 [(path, chunk_index, score), ...]（按分数降序）。

    索引不可用（缺失/损坏/旧版本）时返回 None，与"查到了 0 条"区分开——
    调用方必须据此降级或报错，不能当作"没有相关内容"。
    """
    index = _load_index()
    if index is None:
        return None

    k1, b = index['k1'], index['b']
    avgdl = index['avgdl']
    term_to_id = index['term_to_id']
    idf_list = index['idf']
    postings = index['postings']
    docs_paths = index['docs_paths']
    docs_chunks = index['docs_chunks']
    docs_lens = index['docs_lens']

    q_terms = []
    for qt in tokenize(query):
        tid = term_to_id.get(qt)
        if tid is not None and tid < len(idf_list):
            q_terms.append((tid, idf_list[tid]))
    if not q_terms:
        return []

    # 倒排遍历：只访问查询词的 posting 链，评分聚合到文档
    scores = {}
    for tid, idf_val in q_terms:
        for doc_idx, freq in postings[tid]:
            dl = docs_lens[doc_idx]
            gain = idf_val * (freq * (k1 + 1)) / (freq + k1 * (1 - b + b * dl / avgdl))
            scores[doc_idx] = scores.get(doc_idx, 0.0) + gain

    ranked = sorted(scores.items(),
                    key=lambda kv: (-kv[1], docs_paths[kv[0]], docs_chunks[kv[0]]))
    return [(docs_paths[i], docs_chunks[i], s) for i, s in ranked[:top_k]]


def main():
    parser = argparse.ArgumentParser(description='BM25 索引构建与查询')
    parser.add_argument('--build', action='store_true', help='增量构建 BM25 索引（利用文档词频缓存）')
    parser.add_argument('--rebuild', action='store_true', help='忽略缓存，全量重建')
    parser.add_argument('--query', type=str, help='BM25 检索查询')
    parser.add_argument('--top-k', type=int, default=10, help='返回结果数（默认 10）')
    args = parser.parse_args()

    if args.build:
        build_index(rebuild=args.rebuild)
        return

    if args.query:
        results = search(args.query, top_k=args.top_k)
        if results is None:
            print("错误：BM25 索引不可用（缺失/损坏/旧版本），请先 --build 重建",
                  file=sys.stderr)
            sys.exit(2)
        for path, chunk_idx, score in results:
            print(f"[{score:.4f}] {path} (chunk {chunk_idx})")
        if not results:
            print("未找到结果。")
        return

    parser.print_help()


if __name__ == '__main__':
    main()
