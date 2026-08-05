#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dream.py — Dream 机制：记忆活性扫描 + 唤醒检测 + 报告生成

用法:
    python .meta/scripts/dream.py           # 扫描并生成 .meta/dream-report.md
    python .meta/scripts/dream.py --full    # 预留（当前行为相同）
"""

import sys
import argparse
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import VAULT_ROOT, git_available, git

# ---------------------------------------------------------------------------
# 阈值常量（权威源：.meta/memory/MEMORY.md 分层半衰期表；改表时同步这里）
# ---------------------------------------------------------------------------
WORKFLOW_HALF_LIFE_DAYS = 90       # Procedural workflow 半衰期
WORKFLOW_DECAY_DAYS = 180          # 2× 半衰期 = 衰减判定阈值
WORKFLOW_DECAY_FREQ = 0.05         # frequency 衰减阈值
WORKFLOW_BORDERLINE_FREQ = 0.03    # 临界区间下限

FEEDBACK_DORMANT_DAYS = 180        # active→dormant：180d 无确认
FEEDBACK_ARCHIVE_DAYS = 365        # dormant→archive：365d 无确认
FEEDBACK_STABLE_CONFIRMATIONS = 3  # 双语义：(a) STABLE 信号（健康保留，dream 原义）(b) 晋升阈值（建议内化，晋升候选段派生条件）

PROJECT_STALE_DAYS = 90            # complete 项目 >90d → STALE

# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def get_field(post, key, default=None):
    """优先 top-level frontmatter，fallback metadata. 子键"""
    if key in post:
        return post[key]
    meta = post.get('metadata', {})
    if isinstance(meta, dict) and key in meta:
        return meta[key]
    return default


def parse_date(value):
    """解析日期字符串为 datetime，失败返回 None"""
    if value is None or value == '':
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.strptime(str(value)[:10], '%Y-%m-%d')
    except (ValueError, TypeError):
        return None


def days_since(dt):
    """距离现在的天数；None 返回 None"""
    if dt is None:
        return None
    return (datetime.now() - dt).days


def git_last_modified(file_rel_path):
    """获取文件最后一次 git 修改日期（排除当前未提交的修改），失败返回 None"""
    try:
        result = subprocess.run(
            ['git', 'log', '-1', '--format=%aI', '--', file_rel_path],
            capture_output=True, text=True, timeout=5,
            cwd=str(VAULT_ROOT),
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
        if result.returncode == 0 and result.stdout.strip():
            return parse_date(result.stdout.strip()[:10])
    except Exception:
        pass
    return None


def file_mtime_date(file_path):
    """获取文件系统 mtime，失败返回 None"""
    try:
        ts = file_path.stat().st_mtime
        return datetime.fromtimestamp(ts)
    except Exception:
        return None


def try_load_frontmatter(file_path):
    """尝试加载 markdown frontmatter，失败返回空 dict"""
    try:
        import frontmatter as fm
        return fm.load(str(file_path))
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# 通道判定
# ---------------------------------------------------------------------------

def channel_a_workflow(wf_file):
    """通道 A：workflow 记忆"""
    post = try_load_frontmatter(wf_file)
    freq = post.get('frequency')
    last_used_str = post.get('last_used')
    last_used = parse_date(last_used_str)
    ds = days_since(last_used)

    if freq is None or last_used is None:
        return ('UNKNOWN', f"缺少 frequency 或 last_used", None)

    if freq < WORKFLOW_DECAY_FREQ and ds is not None and ds > WORKFLOW_DECAY_DAYS:
        return ('DECAY', f"frequency={freq:.3f}, {ds}d 未使用", '候选归档')
    elif WORKFLOW_BORDERLINE_FREQ <= freq < WORKFLOW_DECAY_FREQ:
        return ('BORDERLINE', f"frequency={freq:.3f} (临界)", None)
    else:
        return ('ACTIVE', None, None)


def channel_b_feedback(fb_file):
    """通道 B：feedback 记忆"""
    post = try_load_frontmatter(fb_file)
    liveness = get_field(post, 'liveness', 'active')
    last_confirmed_str = get_field(post, 'last_confirmed')
    confirmed_count = get_field(post, 'confirmed_count', 0)
    last_confirmed = parse_date(last_confirmed_str)

    # Touch 安全网（design #1）：frontmatter 缺 last_confirmed 时，fallback 到 git 最后修改日期。
    # 复用已有 git_last_modified()（channel_c_project 在用）。只补日期让衰减判定能跑，不虚增 confirmed_count（git 修改 ≠ 确认有效）。
    if last_confirmed is None:
        rel = str(fb_file.relative_to(VAULT_ROOT)).replace('\\', '/')
        git_lc = git_last_modified(rel)
        if git_lc is not None:
            last_confirmed = git_lc
            ds = days_since(last_confirmed)
            ds_note = f"{ds}d 未确认（last_confirmed 由 git fallback）"
        else:
            return ('UNKNOWN', "缺少确认记录（frontmatter + git 均无）", None)
    else:
        ds = days_since(last_confirmed)
        ds_note = f"{ds}d 未确认"

    if liveness == 'active' and ds is not None and ds > FEEDBACK_DORMANT_DAYS:
        return ('DORMANT', ds_note, '建议降级: active → dormant')
    elif liveness == 'dormant' and ds is not None and ds > FEEDBACK_ARCHIVE_DAYS:
        return ('ARCHIVE', ds_note, '建议归档')
    elif liveness == 'active' and confirmed_count >= FEEDBACK_STABLE_CONFIRMATIONS and ds is not None and ds <= FEEDBACK_DORMANT_DAYS:
        # STABLE 同时产出晋升建议（B2：通用化，不预设落点）；晋升候选段从 stable 派生（design #3）
        return ('STABLE', f"confirmed_count={confirmed_count} | {ds_note}",
                f"confirmed_count={confirmed_count}，建议审阅是否内化")
    elif liveness == 'dormant' and ds is not None and ds <= FEEDBACK_ARCHIVE_DAYS:
        return ('DORMANT', f"{ds_note}（仍在观望期）", None)
    elif liveness == 'active' and ds is not None and ds <= FEEDBACK_DORMANT_DAYS:
        return ('ACTIVE', None, None)
    else:
        return ('ACTIVE', None, None)


def channel_c_project(proj_file):
    """通道 C：project 记忆"""
    post = try_load_frontmatter(proj_file)
    status = get_field(post, 'status', 'active')
    completed_at_str = get_field(post, 'completed_at')
    completed_at = parse_date(completed_at_str)

    # fallback 链
    if completed_at is None:
        meta_date = get_field(post, 'date')
        completed_at = parse_date(meta_date)
    if completed_at is None:
        # git log last modified
        rel = str(proj_file.relative_to(VAULT_ROOT)).replace('\\', '/')
        completed_at = git_last_modified(rel)
    if completed_at is None:
        completed_at = file_mtime_date(proj_file)

    ds = days_since(completed_at)

    if status == 'complete' and ds is not None and ds > PROJECT_STALE_DAYS:
        desc = get_field(post, 'description', '') or post.get('name', '')
        return ('STALE', f"完成 {ds}d 前 | {desc}", '建议审阅原文后决定是否归档')
    elif status == 'complete' and ds is not None and ds <= PROJECT_STALE_DAYS:
        return ('FRESH', None, None)
    elif status == 'active':
        return ('ACTIVE', None, None)
    else:
        return ('ACTIVE', None, None)


def channel_d_reference(ref_file):
    """通道 D：reference 记忆（待激活）"""
    # 当前 reference/ 目录为空，代码骨架预留
    return ('INACTIVE', 'reference/ 目录暂缓建设', None)


# ---------------------------------------------------------------------------
# 唤醒检测
# ---------------------------------------------------------------------------

def detect_wake():
    """扫描 .archive/，检测是否有被修改过的归档文件（排除当前 maintain 提交）"""
    archive_dir = VAULT_ROOT / '.meta' / 'memory' / '.archive'
    if not archive_dir.exists():
        return []

    wakes = []
    for f in archive_dir.rglob('*.md'):
        rel = str(f.relative_to(VAULT_ROOT)).replace('\\', '/')
        dt = git_last_modified(rel)
        if dt is not None:
            wakes.append((rel, dt))
    return wakes


# ---------------------------------------------------------------------------
# 报告生成
# ---------------------------------------------------------------------------

def generate_report(results, wakes):
    """生成 .meta/dream-report.md"""
    now = datetime.now()
    total = sum(len(v) for v in results.values())
    suggestions = (
        len(results['archive_candidates']) +
        len(results['downgrade_suggestions']) +
        len(results['project_stale']) +
        len(wakes)
    )
    unclassified = len(results['unclassified'])

    lines = [
        '---',
        'type: dream-report',
        f'generated_at: {now.isoformat()}',
        'source: dream.py',
        'model: dream.py v1.0',
        '---',
        '',
        '# Dream 报告',
        '',
        f'> 生成时间：{now.strftime("%Y-%m-%d %H:%M")} | 扫描文件 {total} 个 | 建议操作 {suggestions} 条 | 未分类 {unclassified} 个',
        '> **所有建议均为提案，不自动执行。**',
        '',
    ]

    # 归档候选
    lines.append('## 归档候选')
    lines.append('')
    if results['archive_candidates']:
        lines.append('| 文件 | 类型 | 原因 | 最后活跃 |')
        lines.append('|------|------|------|---------|')
        for item in results['archive_candidates']:
            lines.append(f"| {item['file']} | {item['type']} | {item['reason']} | {item.get('last_active', '-')} |")
    else:
        lines.append('（无）')
    lines.append('')

    # 降级建议
    lines.append('## 降级建议')
    lines.append('')
    if results['downgrade_suggestions']:
        lines.append('| 文件 | 类型 | 当前状态 | 建议状态 | 原因 |')
        lines.append('|------|------|---------|---------|------|')
        for item in results['downgrade_suggestions']:
            lines.append(f"| {item['file']} | {item['type']} | {item['current']} | {item['suggested']} | {item['reason']} |")
    else:
        lines.append('（无）')
    lines.append('')

    # 项目总结候选
    lines.append('## 项目总结候选')
    lines.append('')
    if results['project_stale']:
        for item in results['project_stale']:
            lines.append(f"### {item['file']}")
            lines.append(f"> {item.get('reason', '')}")
            lines.append('')
    else:
        lines.append('（无）')
    lines.append('')

    # 唤醒检测
    lines.append('## 唤醒检测')
    lines.append('')
    if wakes:
        lines.append('| 文件 | 最后修改 | 建议 |')
        lines.append('|------|---------|------|')
        for path, dt in wakes:
            lines.append(f"| {path} | {dt.strftime('%Y-%m-%d')} | 建议移回活跃区 |")
    else:
        lines.append('（无）')
    lines.append('')

    # 临界 / 未分类
    lines.append('## 临界 / 未分类')
    lines.append('')
    combined = results['borderline'] + results['unknown'] + results['unclassified']
    if combined:
        for item in combined:
            lines.append(f"- `{item['file']}` — {item.get('reason', '未分类')}")
    else:
        lines.append('（无）')
    lines.append('')

    # 晋升候选（design #3：从 stable 派生，不设独立容器——单一来源，避免双重计数）
    # STABLE 信号被复用为"建议内化"的行动信号；此段为 auto-candidate，是否内化由人工判断（S7 解决 STABLE 语义冲突）
    promote_candidates = [e for e in results['stable'] if e.get('suggestion')]
    lines.append('## 晋升候选（建议内化为制度）')
    lines.append('')
    lines.append('> 以下 feedback 已达 `confirmed_count ≥ 3` 且近期仍被确认（STABLE）。auto-candidate——是否内化、内化到哪个落点（rules / AGENTS.md / user/role.md / 章程），请人工判断。')
    lines.append('')
    if promote_candidates:
        lines.append('| 文件 | confirmed_count | 主题 |')
        lines.append('|------|-----------------|------|')
        for item in promote_candidates:
            cc = item.get('suggestion', '').replace('confirmed_count=', '').split('，')[0].strip()
            topic = item.get('reason', '').split('|')[0].strip()
            lines.append(f"| `{item['file']}` | {cc} | {topic[:40]} |")
    else:
        lines.append('（无）')
    lines.append('')

    # 活跃条目
    lines.append('## 活跃条目')
    lines.append('')
    active_all = results['active'] + results['fresh'] + results['stable'] + results['dormant_keeping']
    if active_all:
        if len(active_all) > 20:
            lines.append(f'> {len(active_all)} 个条目处于活跃状态（已折叠）')
        else:
            for item in active_all:
                lines.append(f"- `{item['file']}` — {item.get('signal', 'ACTIVE')}")
    else:
        lines.append('（无）')
    lines.append('')

    report_path = VAULT_ROOT / '.meta' / 'dream-report.md'
    report_path.write_text('\n'.join(lines), encoding='utf-8', newline='')
    return report_path


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main(full=False):
    """扫描所有记忆文件，生成 Dream 报告"""
    # full 预留（当前行为相同）
    _ = full

    memory_root = VAULT_ROOT / '.meta' / 'memory'
    now = datetime.now()

    # 结果容器
    results = {
        'archive_candidates': [],   # 通道 A DECAY + 通道 B ARCHIVE
        'downgrade_suggestions': [], # 通道 B DORMANT (需降级)
        'project_stale': [],        # 通道 C STALE
        'borderline': [],           # 通道 A BORDERLINE
        'unknown': [],              # UNKNOWN
        'active': [],               # 通道 A ACTIVE
        'fresh': [],                # 通道 C FRESH
        'stable': [],               # 通道 B STABLE
        'dormant_keeping': [],      # 通道 B DORMANT (保持)
        'unclassified': [],         # 非四通道
    }

    # 扫描各通道
    for wf_file in (memory_root / 'workflows').glob('*.md'):
        if wf_file.name == 'README.md':
            continue
        signal, reason, suggestion = channel_a_workflow(wf_file)
        entry = {'file': str(wf_file.relative_to(VAULT_ROOT)).replace('\\', '/'),
                 'type': 'workflow', 'signal': signal, 'reason': reason}
        if signal == 'DECAY':
            entry['last_active'] = str(wf_file.stat().st_mtime)
            if suggestion:
                entry['suggestion'] = suggestion
            results['archive_candidates'].append(entry)
        elif signal == 'BORDERLINE':
            results['borderline'].append(entry)
        elif signal == 'UNKNOWN':
            results['unknown'].append(entry)
        else:
            results['active'].append(entry)

    for fb_file in (memory_root / 'feedback').glob('*.md'):
        signal, reason, suggestion = channel_b_feedback(fb_file)
        entry = {'file': str(fb_file.relative_to(VAULT_ROOT)).replace('\\', '/'),
                 'type': 'feedback', 'signal': signal, 'reason': reason}
        if signal == 'ARCHIVE':
            if suggestion:
                entry['suggestion'] = suggestion
            results['archive_candidates'].append(entry)
        elif signal == 'DORMANT' and suggestion and '降级' in suggestion:
            entry['current'] = 'active'
            entry['suggested'] = 'dormant'
            results['downgrade_suggestions'].append(entry)
        elif signal == 'DORMANT':
            results['dormant_keeping'].append(entry)
        elif signal == 'STABLE':
            if suggestion:
                entry['suggestion'] = suggestion  # 带晋升建议，报告时从 stable 派生晋升候选段（design #3）
            results['stable'].append(entry)
        elif signal == 'UNKNOWN':
            results['unknown'].append(entry)
        else:
            results['active'].append(entry)

    for proj_file in (memory_root / 'project').glob('*.md'):
        signal, reason, suggestion = channel_c_project(proj_file)
        entry = {'file': str(proj_file.relative_to(VAULT_ROOT)).replace('\\', '/'),
                 'type': 'project', 'signal': signal, 'reason': reason}
        if signal == 'STALE':
            if suggestion:
                entry['reason'] = reason
            results['project_stale'].append(entry)
        elif signal == 'FRESH':
            results['fresh'].append(entry)
        elif signal == 'UNKNOWN':
            results['unknown'].append(entry)
        else:
            results['active'].append(entry)

    for ref_file in (memory_root / 'reference').glob('*.md'):
        signal, reason, _ = channel_d_reference(ref_file)
        entry = {'file': str(ref_file.relative_to(VAULT_ROOT)).replace('\\', '/'),
                 'type': 'reference', 'signal': signal, 'reason': reason}
        results['unclassified'].append(entry)

    # 非四通道 catch-all（user/ 为用户画像目录，不参与衰减，不报未分类）
    known_dirs = {'workflows', 'feedback', 'project', 'reference', 'user'}
    for f in memory_root.rglob('*.md'):
        if f.name == 'MEMORY.md':
            continue
        parts = f.relative_to(memory_root).parts
        if parts and parts[0] in known_dirs:
            continue  # already handled above
        if '.archive' in parts:
            continue  # handled by wake detection
        results['unclassified'].append({
            'file': str(f.relative_to(VAULT_ROOT)).replace('\\', '/'),
            'type': 'unknown',
            'signal': 'UNCLASSIFIED',
            'reason': '非四通道类型',
        })

    # 唤醒检测
    wakes = detect_wake()

    # 生成报告
    report_path = generate_report(results, wakes)
    print(f"  Dream 报告已生成: {report_path.relative_to(VAULT_ROOT)}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Dream 机制：记忆活性扫描 + 报告生成')
    parser.add_argument('--full', action='store_true', help='全量模式（预留，当前行为相同）')
    args = parser.parse_args()
    main(full=args.full)
