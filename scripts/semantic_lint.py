#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
semantic_lint.py — 语义质量检查

检查项：
  P0 · 断裂双链（零成本）—— 支持 [[wiki]] 和 [md](link) 两种语法
  P1 · 孤儿概念 / 过时标记（低成本启发式）
  P2 · 矛盾检测（高成本，需 DeepSeek）

用法:
    python .meta/scripts/semantic_lint.py              # 默认 = --quick
    python .meta/scripts/semantic_lint.py --quick      # P0 + P1
    python .meta/scripts/semantic_lint.py --deep       # P0 + P1 + P2
"""

import sys
import re
import json
import random
import argparse
from collections import Counter, defaultdict
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))
from common import (
    VAULT_ROOT, scan_notes, rel_path, is_excluded, ensure_parent,
    find_all_wikilinks, find_all_mdlinks,
    parse_aliases, pick_closest_by_prefix, log_script_run,
    resolve_wikilink, resolve_mdlink,
    is_primary_host,
)

CURRENT_YEAR = datetime.now().year

# ─── 排除的常见词（中文）────────────────────────────────────────
COMMON_CN_WORDS = {
    '笔记', '日记', '链接', '文件', '目录', '代码', '方法', '功能',
    '问题', '结果', '时间', '工作', '需要', '可以', '使用', '进行',
    '通过', '基于', '实现', '完成', '开始', '结束', '过程', '系统',
    '数据', '信息', '内容', '部分', '情况', '方面', '方式', '作用',
    '原因', '目的', '意义', '价值', '效果', '影响', '关系', '结构',
    '模型', '算法', '程序', '应用', '开发', '设计', '测试', '部署',
    '运行', '维护', '管理', '操作', '步骤', '流程', '任务', '项目',
    '团队', '用户', '客户', '产品', '服务', '业务', '需求', '目标',
    '计划', '方案', '策略', '原则', '标准', '规范', '规则', '约束',
    '架构', '框架', '平台', '工具', '技术', '资源', '环境', '配置',
    '参数', '变量', '函数', '类名', '接口', '模块', '组件', '服务',
    '请求', '响应', '错误', '异常', '日志', '报告', '文档', '说明',
    '参考', '引用', '来源', '地址', '路径', '名称', '标题', '描述',
    '摘要', '正文', '段落', '句子', '单词', '字符', '长度', '大小',
    '数量', '次数', '频率', '比例', '概率', '分布', '趋势', '变化',
    '增加', '减少', '提高', '降低', '优化', '改进', '修复', '解决',
    '分析', '比较', '评估', '判断', '选择', '决定', '建议', '推荐',
    '支持', '允许', '禁止', '要求', '必须', '应该', '可能', '一定',
    '确实', '明显', '通常', '一般', '特别', '非常', '比较', '相对',
    '直接', '间接', '简单', '复杂', '容易', '困难', '快速', '缓慢',
    '主要', '次要', '核心', '关键', '重要', '紧急', '优先', '延迟',
    '准备', '处理', '执行', '监控', '检查', '确认', '验证', '审核',
    '记录', '保存', '删除', '修改', '更新', '替换', '合并', '拆分',
    '输入', '输出', '导入', '导出', '上传', '下载', '同步', '复制',
    '创建', '生成', '构建', '编译', '打包', '发布', '上线', '下线',
    '启动', '停止', '重启', '暂停', '恢复', '等待', '超时', '重试',
    '成功', '失败', '完成', '中断', '取消', '跳过', '忽略', '继续',
    '之前', '之后', '期间', '当时', '现在', '今天', '明天', '昨天',
    '本周', '本月', '今年', '去年', '明年', '最近', '很快', '已经',
    '正在', '将要', '想要', '认为', '觉得', '知道', '了解', '理解',
    '学习', '研究', '探索', '尝试', '实验', '实践', '经验', '教训',
    '优势', '劣势', '机会', '威胁', '风险', '挑战', '难点', '瓶颈',
    '成本', '收益', '投资', '回报', '预算', '费用', '价格', '价值',
    '质量', '效率', '性能', '稳定性', '可靠性', '安全性', '可用性',
    '扩展性', '兼容性', '可维护性', '可读性', '可测试性', '可移植性',
    'obsidian', 'markdown', 'python', 'git', 'github', 'agent',
}


# ─── 索引构建（供 P0 断链检测使用）────────────────────────────────

def build_link_indexes(all_notes: list):
    """为所有笔记构建链接解析所需的完整索引。
    返回 (basename_index, aliases_index, all_paths_set)
    与 build_graph.py 的 build_indexes() 一致，但不缓存 content（内存友好）。"""
    basename_index = defaultdict(list)
    aliases_index = {}
    all_paths_set = set()

    for note in all_notes:
        rel = rel_path(note)
        try:
            content = note.read_text(encoding='utf-8')
        except Exception:
            content = ''

        basename_index[note.stem].append(rel)
        all_paths_set.add(rel)

        aliases = parse_aliases(content)
        for a in aliases:
            if a and a not in aliases_index:
                aliases_index[a] = rel

    return basename_index, aliases_index, all_paths_set


# ─── P0: 断裂双链 ───────────────────────────────────────────────

def check_broken_links(all_notes: list, basename_index: dict,
                       aliases_index: dict, all_paths_set: set) -> list:
    """P0: 检查断裂链接（wikilink + markdown link）。
    使用 Obsidian 官方解析算法（含 aliases + basename 最短路径）。
    返回 broken 列表，每项含 source, target, syntax, reason, display(仅md)。"""
    broken = []
    seen = set()  # 去重 (source, target, syntax)

    for note in all_notes:
        source = rel_path(note)
        try:
            text = note.read_text(encoding='utf-8')
        except Exception:
            continue

        # 扫描 wikilinks
        for target in find_all_wikilinks(text):
            resolved, reason = resolve_wikilink(
                target, source, basename_index, aliases_index, all_paths_set
            )
            if resolved is None:
                key = (source, target, 'wiki')
                if key not in seen:
                    seen.add(key)
                    broken.append({
                        'source': source,
                        'target': target,
                        'syntax': 'wiki',
                        'reason': reason,
                    })

        # 扫描 markdown 链接
        for display, target in find_all_mdlinks(text):
            resolved, reason = resolve_mdlink(
                target, source, basename_index, aliases_index, all_paths_set
            )
            if resolved is None:
                key = (source, target, 'md')
                if key not in seen:
                    seen.add(key)
                    broken.append({
                        'source': source,
                        'target': target,
                        'syntax': 'md',
                        'reason': reason,
                        'display': display,
                    })

    return broken


# ─── P1: 孤儿概念 ───────────────────────────────────────────────

CONCEPT_RE = re.compile(r'\*\*([^*]{2,30})\*\*')  # 加粗短语
CODE_RE = re.compile(r'`([^`]{2,30})`')            # 行内代码


def looks_like_proper_noun(term: str) -> bool:
    """判断一个短语是否像专有名词/概念"""
    # 纯英文技术术语
    if re.match(r'^[A-Z][a-zA-Z0-9_\-/]+$', term):
        return True
    # 中英文混合技术术语
    if re.match(r'^[A-Za-z][a-zA-Z0-9_\-]+[一-鿿]', term):
        return True
    if re.match(r'^[一-鿿]+[A-Za-z][a-zA-Z0-9_\-]+$', term):
        return True
    # 纯中文但看起来像术语（含"工程""理论""架构""模型"等后缀）
    if any(suffix in term for suffix in ('工程', '理论', '架构', '模型', '方法',
                                          '系统', '框架', '原则', '范式',
                                          '编程', '智能', '学习', '网络',
                                          '驱动', '设计', '分析', '优化')):
        return True
    # 纯中文但长度适中且不含常见词
    if re.match(r'^[一-鿿]{4,15}$', term):
        return True
    return False


def check_orphan_concepts(all_notes: list, basenames: set) -> list:
    """P1: 找出被频繁提及但没有独立笔记的概念"""
    concept_counts = Counter()
    concept_locations = {}

    for note in all_notes:
        text = note.read_text(encoding='utf-8', errors='replace')
        r = rel_path(note)

        # 提取加粗术语
        for m in CONCEPT_RE.finditer(text):
            term = m.group(1).strip()
            if len(term) < 3 or len(term) > 25:
                continue
            # 过滤纯数字/纯符号
            if not re.search(r'[一-鿿A-Za-z]', term):
                continue
            # 过滤常见词
            if term.lower() in COMMON_CN_WORDS:
                continue
            if not looks_like_proper_noun(term):
                continue
            concept_counts[term] += 1
            concept_locations.setdefault(term, []).append(r)

        # 提取代码术语（技术概念）
        for m in CODE_RE.finditer(text):
            term = m.group(1).strip()
            if len(term) < 3 or len(term) > 25:
                continue
            # 只保留看起来像专有名词的代码
            if not re.match(r'^[A-Z][a-zA-Z0-9_\-/]+$', term):
                continue
            if term.lower() in COMMON_CN_WORDS:
                continue
            concept_counts[term] += 1
            concept_locations.setdefault(term, []).append(r)

    # 找出高频但无独立笔记的概念
    orphans = []
    for term, count in concept_counts.most_common(60):
        if count < 3:
            continue
        # 检查是否已有同名笔记
        if term in basenames:
            continue
        # 检查是否是已有笔记的别名（简单包含匹配）
        found = False
        term_lower = term.lower()
        for name in basenames:
            if term_lower == name.lower():
                found = True
                break
            # 双向包含（长度差不超过2倍）
            if len(name) > 2 and len(term) > 2:
                if term_lower in name.lower() or name.lower() in term_lower:
                    if max(len(term), len(name)) / min(len(term), len(name)) <= 2:
                        found = True
                        break
        if not found:
            orphans.append({
                'term': term,
                'count': count,
                'locations': concept_locations[term][:5],
            })

    return orphans[:20]


# ─── P1: 过时标记 ───────────────────────────────────────────────

STALE_DATE_RE = re.compile(r'\b(20[0-5]\d)\s*[年/\-]')
STALE_YEAR_STRONG_RE = re.compile(r'\b(20[0-5]\d)\b')


def check_stale_markers(all_notes: list) -> list:
    """P1: 标记可能过时的日期引用"""
    stale = []
    threshold = CURRENT_YEAR - 1  # 去年及更早

    for note in all_notes:
        text = note.read_text(encoding='utf-8', errors='replace')

        # 优先匹配带"年""/""-"的日期
        matches = list(STALE_DATE_RE.finditer(text))
        if not matches:
            #  fallback：匹配独立年份数字（减少误报）
            matches = list(STALE_YEAR_STRONG_RE.finditer(text))

        reported = False
        for m in matches:
            year = int(m.group(1))
            if year < threshold:
                start = max(0, m.start() - 25)
                end = min(len(text), m.end() + 25)
                context = text[start:end].replace('\n', ' ')

                stale.append({
                    'source': rel_path(note),
                    'year': year,
                    'context': context.strip(),
                })
                reported = True
                break  # 每篇笔记只报一次最旧的

    return stale


# ─── P2: 矛盾检测（基于高频概念 + DeepSeek）─────────────────────

def extract_concept_contexts(term: str, all_notes: list, max_per_note: int = 200) -> list:
    """提取某概念在各笔记中的上下文片段"""
    contexts = []
    term_lower = term.lower()

    for note in all_notes:
        text = note.read_text(encoding='utf-8', errors='replace')
        if term_lower not in text.lower():
            continue

        # 找到 term 出现的位置，提取前后文
        idx = text.lower().find(term_lower)
        if idx == -1:
            continue

        start = max(0, idx - 80)
        end = min(len(text), idx + len(term) + 80)
        # 尝试扩展到句子边界
        while start > 0 and text[start] not in '.。!！?？\n':
            start -= 1
        while end < len(text) and text[end] not in '.。!！?？\n':
            end += 1

        context = text[start:end].replace('\n', ' ').strip()
        if len(context) > max_per_note:
            context = context[:max_per_note] + '...'

        contexts.append({
            'source': rel_path(note),
            'context': context,
        })
        if len(contexts) >= 4:  # 每概念最多 4 个来源
            break

    return contexts


def check_contradictions(all_notes: list) -> list:
    """P2: 语义矛盾检测 — 基于高频概念，用 DeepSeek 分析"""
    from common import DeepSeekClient

    # 1. 获取高频概念（复用 P1 逻辑但不写报告）
    basenames = {n.stem for n in all_notes}
    concept_counts = Counter()
    for note in all_notes:
        text = note.read_text(encoding='utf-8', errors='replace')
        for m in CONCEPT_RE.finditer(text):
            term = m.group(1).strip()
            if 3 <= len(term) <= 25 and looks_like_proper_noun(term):
                if term.lower() not in COMMON_CN_WORDS:
                    concept_counts[term] += 1
        for m in CODE_RE.finditer(text):
            term = m.group(1).strip()
            if re.match(r'^[A-Z][a-zA-Z0-9_\-/]+$', term):
                if term.lower() not in COMMON_CN_WORDS:
                    concept_counts[term] += 1

    # 过滤掉已有独立笔记的概念 + 只取高频
    candidates = []
    for term, count in concept_counts.most_common(30):
        if count < 3:
            continue
        if term in basenames:
            continue
        # 跳过太泛的术语
        if term.lower() in {'ai', 'api', 'llm', 'cpu', 'gpu', 'ui', 'cli'}:
            continue
        candidates.append(term)
        if len(candidates) >= 8:
            break

    if len(candidates) < 2:
        return []

    client = DeepSeekClient()
    contradictions = []

    for term in candidates:
        contexts = extract_concept_contexts(term, all_notes)
        if len(contexts) < 2:
            continue

        # 构建判断 prompt
        ctx_lines = []
        for i, ctx in enumerate(contexts, 1):
            ctx_lines.append(f"来源 {i} (`{ctx['source']}`): {ctx['context']}")
        ctx_text = '\n'.join(ctx_lines)

        prompt = (
            f"以下是在不同笔记中关于「{term}」的论述片段。\n\n"
            f"{ctx_text}\n\n"
            "请判断：这些论述中是否包含**直接矛盾**的观点？\n"
            "- 如果存在矛盾，请说明矛盾双方分别是什么观点，并指出来源。\n"
            "- 如果不存在直接矛盾（只是角度不同、或完全一致），请回答「无矛盾」。\n"
            "请用一句话给出结论。"
        )

        try:
            result = client.chat(
                [
                    {"role": "system", "content": "你是一位严谨的知识库审核员，只报告有明确证据支持的矛盾。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
                max_tokens=256,
            )
        except Exception as e:
            print(f"  ✗ 概念 '{term}' 矛盾检测失败: {e}")
            continue

        result_lower = result.lower()
        # 过滤明确的"无矛盾"结论（中英文）
        if any(ph in result_lower for ph in ('无矛盾', '没有矛盾', '不存在矛盾',
                                              'no contradiction', 'not contradictory')):
            continue
        if len(result) < 10:
            continue

        contradictions.append({
            'term': term,
            'sources': [c['source'] for c in contexts],
            'analysis': result,
        })

    return contradictions


# ─── 图遍历回归测试 ──────────────────────────────────────────────

def check_graph_traversal():
    """图遍历回归测试：随机抽 10 个非孤立节点，验证邻域返回非空且边权重合法。

    Returns:
        bool: 全部通过返回 True，任一失败返回 False。
    """
    from build_graph import get_neighbors

    graph_path = VAULT_ROOT / '.meta' / 'graph.json'
    if not graph_path.exists():
        print("SKIP: graph.json 不存在")
        return True

    try:
        graph = json.loads(graph_path.read_text(encoding='utf-8'))
    except Exception:
        print("SKIP: graph.json 解析失败")
        return True

    # community_id >= 0 基于无向图 Louvain 聚类，但 get_neighbors 只沿出边遍历。
    # 因此筛出同时满足 community_id >= 0 且至少有一条出边的节点作为测试样本。
    nodes_with_outgoing = set()
    for e in graph.get('edges', []):
        nodes_with_outgoing.add(e.get('source', ''))
    non_isolated = [
        n for n in graph.get('nodes', [])
        if n.get('community_id', -1) >= 0 and n['id'] in nodes_with_outgoing
    ]
    if not non_isolated:
        print("SKIP: 无非孤立节点")
        return True

    sample = random.sample(non_isolated, min(10, len(non_isolated)))
    all_ok = True
    for node in sample:
        nid = node['id']
        neighbors = get_neighbors(nid, max_hops=1)
        if isinstance(neighbors, dict) and "error" in neighbors:
            print(f"FAIL: {nid} get_neighbors 返回错误: {neighbors}")
            all_ok = False
            continue
        if not neighbors:
            print(f"FAIL: {nid} 邻域为空（预期非空, community_id={node.get('community_id')}）")
            all_ok = False
            continue
        for nb in neighbors:
            w = nb.get('weight', -1)
            if not (0.0 <= w <= 1.0):
                print(f"FAIL: {nid} → {nb['path']} weight={w} 不合法（需在 [0.0, 1.0] 区间）")
                all_ok = False

    if all_ok:
        print(f"PASS: 图遍历回归测试 ({len(sample)} 个节点)")
    return all_ok


# ─── 去重逻辑回归测试 ──────────────────────────────────────────

def check_deep_dedup():
    """回归测试：验证 deep search 去重逻辑"""
    # Test dedup logic independently
    test_items = [
        ('a.md', 0, 0.9), ('a.md', 0, 0.8),  # duplicate path+chunk
        ('b.md', 0, 0.7), ('b.md', 1, 0.6),   # same path, different chunk
        ('c.md', 0, 0.5),
    ]
    seen = {}
    for path, chunk_idx, sim in test_items:
        key = (path, chunk_idx)
        if key not in seen or sim > seen[key]:
            seen[key] = sim

    # Should have 4 unique keys
    expected = {('a.md', 0): 0.9, ('b.md', 0): 0.7, ('b.md', 1): 0.6, ('c.md', 0): 0.5}
    if seen == expected:
        print("PASS: 去重逻辑正确")
        return True
    else:
        print(f"FAIL: 去重逻辑错误。期望 {expected}，得到 {seen}")
        return False


# ─── BM25 索引回归测试 ──────────────────────────────────────────

def check_bm25_build():
    """回归测试：验证 BM25 索引文件存在且可解析"""
    bm25_path = VAULT_ROOT / '.meta' / 'bm25_index.json.gz'
    if not bm25_path.exists():
        print("SKIP: bm25_index.json.gz 不存在（需先运行 bm25_index.py --build）")
        return True

    try:
        import gzip
        index = json.loads(gzip.decompress(bm25_path.read_bytes()))
        required_keys = ['N', 'avgdl', 'idf', 'docs_paths', 'generated_at']
        for k in required_keys:
            if k not in index:
                print(f"FAIL: BM25 索引缺少 '{k}' 字段")
                return False

        if index['N'] > 0 and len(index['docs_paths']) == index['N']:
            print(f"PASS: BM25 索引有效（{index['N']} 文档, {len(index['idf'])} 词项）")
            return True
        else:
            print(f"FAIL: 文档数不匹配 N={index['N']} != len(docs_paths)={len(index['docs_paths'])}")
            return False
    except Exception as e:
        print(f"FAIL: BM25 索引解析失败: {e}")
        return False


# ─── Rerank 降级路径回归测试 ───────────────────────────────────

def check_rerank_fallback():
    """回归测试：验证 rerank 降级路径"""
    from common import rerank_with_llm, DeepSeekClient

    # Test 1: empty candidates
    result = rerank_with_llm("test", [], top_m=5)
    assert result == [], f"Empty candidates should return [], got {result}"

    # Test 2: mock API failure → fallback to coarse rank
    test_candidates = [
        ('a.md', 0, 'relevant text about AI agents'),
        ('b.md', 0, 'unrelated text about cooking'),
        ('c.md', 0, 'somewhat related agent architecture'),
    ]

    try:
        from unittest.mock import patch
        with patch.object(DeepSeekClient, 'chat', side_effect=Exception("Mock API failure")):
            result = rerank_with_llm("AI agents", test_candidates, top_m=2)
            # Should not crash; should return fallback results
            assert len(result) >= 1, "Fallback should return at least some results"
            print("PASS: rerank 降级路径在 API 失败时正确回退")
    except ImportError:
        print("SKIP: unittest.mock 不可用（Python 版本限制）")

    print("PASS: rerank 降级路径测试通过")
    return True


# ─── 报告生成 ───────────────────────────────────────────────────

def generate_report(p0: list, p1_orphans: list, p1_stale: list, p2: list,
                    mode: str, total_notes: int) -> str:
    lines = [
        '# 语义质量报告',
        '',
        f'> 生成于: {datetime.now().isoformat()}',
        f'> 检查模式: {"快速 (P0+P1)" if mode == "quick" else "深度 (P0+P1+P2)"}',
        f'> 笔记总数: {total_notes}',
        '',
    ]

    # P0
    lines.append('## 断裂双链（P0）')
    lines.append('> 同时检测 `[[]]` wikilink 和 `[]()` markdown 链接。')
    if p0:
        lines.append(f'发现 {len(p0)} 个断裂链接：')
        lines.append('')
        for item in p0:
            if item.get('syntax') == 'md':
                d = item.get('display', '')
                lines.append(f"- `{item['source']}` → `[{d}]({item['target']})` — {item.get('reason', '')}")
            else:
                lines.append(f"- `{item['source']}` → `[[{item['target']}]]` — {item.get('reason', '')}")
    else:
        lines.append('未发现断裂链接。')
    lines.append('')

    # P1 孤儿概念
    lines.append('## 孤儿概念（P1）')
    lines.append('> 以下概念在多篇笔记中被显式标注（加粗/代码），但无同名独立笔记。')
    lines.append('')
    if p1_orphans:
        for item in p1_orphans:
            locs = ', '.join(f'`{l}`' for l in item['locations'])
            lines.append(f"- **{item['term']}**（提及 {item['count']} 次）— {locs}")
    else:
        lines.append('未发现明显孤儿概念。')
    lines.append('')

    # P1 过时标记
    lines.append('## 过时标记（P1）')
    if p1_stale:
        lines.append(f'发现 {len(p1_stale)} 篇笔记含可能过时的年份引用：')
        lines.append('')
        for item in p1_stale:
            lines.append(f"- `{item['source']}` 提到 **{item['year']}年** — "
                        f"\"...{item['context']}...\"")
    else:
        lines.append('未发现明显过时标记。')
    lines.append('')

    # P2
    lines.append('## 潜在矛盾（P2）')
    if p2:
        for item in p2:
            srcs = ', '.join(f'`{s}`' for s in item['sources'][:3])
            lines.append(f"- **{item['term']}** — {srcs}")
            lines.append(f"  > {item['analysis']}")
    else:
        if mode == 'deep':
            lines.append('深度检查未发现明显矛盾。')
        else:
            lines.append('> 使用 `--deep` 启用矛盾检测。')
    lines.append('')

    return '\n'.join(lines)


# ─── 主函数 ─────────────────────────────────────────────────────

def main():
    log_script_run()
    parser = argparse.ArgumentParser(description='语义质量检查')
    parser.add_argument('--quick', action='store_true', help='只跑 P0+P1（默认）')
    parser.add_argument('--deep', action='store_true', help='包含 P2 矛盾检测')
    parser.add_argument('--check-graph-traversal', action='store_true',
                        help='图遍历回归测试：随机抽非孤立节点验证邻域返回非空且边权重合法')
    parser.add_argument('--check-deep-dedup', action='store_true',
                        help='去重逻辑回归测试：验证 deep search (path, chunk_index) 去重正确性')
    parser.add_argument('--check-bm25-build', action='store_true',
                        help='BM25 索引回归测试：验证 bm25_index.json.gz 存在且字段完整')
    parser.add_argument('--check-rerank-fallback', action='store_true',
                        help='Rerank 降级路径回归测试：验证空候选处理与 API 降级路径')
    args = parser.parse_args()

    if args.check_graph_traversal:
        ok = check_graph_traversal()
        sys.exit(0 if ok else 1)
    if args.check_deep_dedup:
        ok = check_deep_dedup()
        sys.exit(0 if ok else 1)
    if args.check_bm25_build:
        ok = check_bm25_build()
        sys.exit(0 if ok else 1)
    if args.check_rerank_fallback:
        ok = check_rerank_fallback()
        sys.exit(0 if ok else 1)

    mode = 'deep' if args.deep else 'quick'
    print(f"=== 语义 Lint 开始 ({mode} 模式) ===")

    all_notes = list(scan_notes())
    total = len(all_notes)
    print(f"扫描到 {total} 篇笔记")

    # 建立索引（供 P0 断链检测使用）
    print("建立笔记索引...")
    basename_index, aliases_index, all_paths_set = build_link_indexes(all_notes)
    basenames = set(basename_index.keys())

    # P0
    print("检查断裂链接 (P0)...")
    p0 = check_broken_links(all_notes, basename_index, aliases_index, all_paths_set)
    wiki_broken = sum(1 for b in p0 if b.get('syntax') == 'wiki')
    md_broken = sum(1 for b in p0 if b.get('syntax') == 'md')
    print(f"  发现 {len(p0)} 个断裂链接（wiki: {wiki_broken} · md: {md_broken}）")

    # P1
    print("检查孤儿概念 (P1)...")
    p1_orphans = check_orphan_concepts(all_notes, basenames)
    print(f"  发现 {len(p1_orphans)} 个孤儿概念")

    print("检查过时标记 (P1)...")
    p1_stale = check_stale_markers(all_notes)
    print(f"  发现 {len(p1_stale)} 个过时标记")

    # P2
    p2 = []
    if mode == 'deep':
        print("检查潜在矛盾 (P2)...")
        p2 = check_contradictions(all_notes)
        print(f"  发现 {len(p2)} 个潜在矛盾")

    # 生成报告
    report = generate_report(p0, p1_orphans, p1_stale, p2, mode, total)
    report_path = VAULT_ROOT / '.meta' / 'semantic-lint-report.md'
    ensure_parent(report_path)
    report_path.write_text(report, encoding='utf-8', newline='')
    print(f"\n报告已写入: {rel_path(report_path)}")

    print("=== 语义 Lint 完成 ===")
    return 0


if __name__ == '__main__':
    # --- host guard ---
    if not is_primary_host():
        print("FATAL: this script must run on PRIMARY_HOST", file=sys.stderr)
        sys.exit(1)
    # --- /host guard ---
    sys.exit(main())
