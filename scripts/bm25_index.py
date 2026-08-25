#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bm25_index.py — BM25 稀疏索引构建与查询（零 API、零嵌入库依赖）

数据源：直接扫描 vault 语料（用户笔记 + office 提取 sidecar + memory，
经 common.scan_indexable_notes），按 embed.chunk_text 相同规则分块。
不依赖 embeddings.sqlite——简化版（lite）默认安装即可用。

用法:
    python .meta/scripts/bm25_index.py --build      # 扫描语料全量重建 BM25 索引
    python .meta/scripts/bm25_index.py --query "xxx" --top-k 10  # BM25 检索
"""

import sys, json, argparse, re, gzip, os, tempfile
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))
from common import VAULT_ROOT, scan_indexable_notes, rel_path
from embed import chunk_text

BM25_INDEX_PATH = VAULT_ROOT / '.meta' / 'bm25_index.json.gz'

# Simple BM25 implementation (avoid external dependency issues on Windows)
# BM25 score = Σ IDF(qi) * (f(qi,D) * (k1+1)) / (f(qi,D) + k1*(1-b+b*|D|/avgdl))

K1 = 1.5
B = 0.75


def _write_index_atomically(index):
    """Write a complete gzip index, then atomically publish it."""
    BM25_INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=BM25_INDEX_PATH.parent,
        prefix=f'.{BM25_INDEX_PATH.name}.',
        suffix='.tmp',
        delete=False,
    ) as tmp:
        temp_path = Path(tmp.name)

    try:
        with gzip.open(temp_path, 'wt', encoding='utf-8') as f:
            json.dump(index, f, ensure_ascii=False, separators=(',', ':'))
        os.replace(temp_path, BM25_INDEX_PATH)
    finally:
        temp_path.unlink(missing_ok=True)

def tokenize(text):
    """Simple tokenizer: split on whitespace + Chinese character-level bigrams"""
    # Split on non-word characters
    tokens = re.findall(r'[一-鿿]+|[a-zA-Z0-9]+', text.lower())
    result = []
    for token in tokens:
        if re.match(r'^[一-鿿]+$', token):
            # Chinese: character bigrams + unigrams
            result.append(token)  # whole word
            for i in range(len(token)):
                result.append(token[i])  # unigram
                if i < len(token) - 1:
                    result.append(token[i:i+2])  # bigram
        else:
            result.append(token)
    return result


def _iter_corpus_chunks():
    """扫描语料并分块，产出 (path, chunk_index, chunk_text)。"""
    for md in scan_indexable_notes(scope='all'):
        rel = rel_path(md)
        try:
            content = md.read_text(encoding='utf-8')
        except Exception as e:
            print(f"  [skip] 无法读取: {rel} ({e})", file=sys.stderr)
            continue
        for idx, chunk in enumerate(chunk_text(content)):
            yield rel, idx, chunk


def build_index():
    """直接扫描 vault 语料（笔记 + office 提取件 + memory），构建 BM25 索引"""
    start = datetime.now()

    # First pass: tokenize all docs, build term->id mapping and doc frequencies
    term_to_id = {}
    doc_freqs = {}  # term_id -> number of docs containing it
    docs_paths = []
    docs_chunks = []
    docs_lens = []
    docs_raw_terms = []  # list of {term_id: freq} per doc
    total_dl = 0

    for path, chunk_idx, text in _iter_corpus_chunks():
        tokens = tokenize(text)
        term_freqs = {}
        for t in tokens:
            tid = term_to_id.get(t)
            if tid is None:
                tid = len(term_to_id)
                term_to_id[t] = tid
            term_freqs[tid] = term_freqs.get(tid, 0) + 1

        docs_paths.append(path)
        docs_chunks.append(chunk_idx)
        docs_lens.append(len(tokens))
        docs_raw_terms.append(term_freqs)
        total_dl += len(tokens)

        for tid in term_freqs:
            doc_freqs[tid] = doc_freqs.get(tid, 0) + 1

    N = len(docs_paths)
    if N == 0:
        # 语料为空时清除旧索引，避免命中已删除内容的陈旧结果
        BM25_INDEX_PATH.unlink(missing_ok=True)
        print("⚠️  语料为空（无笔记 / office 提取件 / memory），已清除旧 BM25 索引。")
        return
    avgdl = total_dl / N

    # Compute IDF for each term (as list indexed by term_id)
    num_terms = len(term_to_id)
    idf_list = [0.0] * num_terms
    for tid, df in doc_freqs.items():
        idf_list[tid] = max(0.0, (N - df + 0.5) / (df + 0.5))

    # Compact per-document terms: [[term_id, freq], ...] sorted by term_id
    docs_terms = []
    for tf in docs_raw_terms:
        compact = sorted([[tid, freq] for tid, freq in tf.items()])
        docs_terms.append(compact)

    # Serialize
    index = {
        'k1': K1, 'b': B,
        'N': N, 'avgdl': avgdl,
        'term_to_id': term_to_id,
        'idf': idf_list,
        'docs_paths': docs_paths,
        'docs_chunks': docs_chunks,
        'docs_lens': docs_lens,
        'docs_terms': docs_terms,
        'generated_at': datetime.now().isoformat(),
    }

    _write_index_atomically(index)
    elapsed = (datetime.now() - start).total_seconds()
    print(f"BM25 索引构建完成：{N} 文档，{len(term_to_id)} 词项，{elapsed:.1f}s")


def search(query, top_k=10):
    """BM25 检索，返回 [(path, chunk_index, score), ...]"""
    if not BM25_INDEX_PATH.exists():
        print("错误：bm25_index.json.gz 不存在，请先运行 --build", file=sys.stderr)
        return []

    with gzip.open(BM25_INDEX_PATH, 'rt', encoding='utf-8') as f:
        index = json.load(f)

    query_tokens = tokenize(query)

    k1, b = index['k1'], index['b']
    avgdl = index['avgdl']
    term_to_id = index['term_to_id']
    idf_list = index['idf']
    docs_paths = index['docs_paths']
    docs_chunks = index['docs_chunks']
    docs_lens = index['docs_lens']
    docs_terms = index['docs_terms']

    # Map query tokens to (term_id, idf) pairs (skip unknown terms)
    q_terms = []
    for qt in query_tokens:
        tid = term_to_id.get(qt)
        if tid is not None and tid < len(idf_list):
            q_terms.append((tid, idf_list[tid]))

    if not q_terms:
        return []

    # Build set of query term IDs for fast lookup
    q_tid_set = {tid for tid, _ in q_terms}

    scores = []
    for i, doc_term_pairs in enumerate(docs_terms):
        score = 0.0
        dl = docs_lens[i]
        for tid, freq in doc_term_pairs:
            if tid not in q_tid_set:
                continue
            # Find the idf for this term
            idf_val = idf_list[tid]
            numerator = freq * (k1 + 1)
            denominator = freq + k1 * (1 - b + b * dl / avgdl)
            score += idf_val * numerator / denominator

        if score > 0:
            scores.append((docs_paths[i], docs_chunks[i], score))

    scores.sort(key=lambda x: -x[2])
    return scores[:top_k]


def main():
    parser = argparse.ArgumentParser(description='BM25 索引构建与查询')
    parser.add_argument('--build', action='store_true', help='全量重建 BM25 索引')
    parser.add_argument('--query', type=str, help='BM25 检索查询')
    parser.add_argument('--top-k', type=int, default=10, help='返回结果数（默认 10）')
    args = parser.parse_args()

    if args.build:
        build_index()
        return

    if args.query:
        results = search(args.query, top_k=args.top_k)
        for path, chunk_idx, score in results:
            print(f"[{score:.4f}] {path} (chunk {chunk_idx})")
        if not results:
            print("未找到结果。")
        return

    parser.print_help()


if __name__ == '__main__':
    main()
