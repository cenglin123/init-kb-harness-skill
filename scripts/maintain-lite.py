#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
maintain-lite.py — init-kb-harness 精简维护管线（Phase 1）

剥离 dream / synthesis_index / knowledge_map / bm25_index / semantic_lint / gc
（Phase 2/3 解锁时换完整 maintain.py）。
Phase 1b 脚本（detect_renames / check_sidecar / build_graph / health_report）
用存在性守护——Phase 1a 最小安装时自动跳过，1b 安装后自动纳入。

用法:
    python maintain-lite.py            # 增量
    python maintain-lite.py --full     # 全量
    python maintain-lite.py --no-git   # 跳过 git 提交（调试用）
    python maintain-lite.py --no-git --skip-changelog
"""

import sys
import subprocess
import re
import json
import argparse
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import VAULT_ROOT, ENV, git, git_available, is_primary_host, log_script_run

SCRIPTS_DIR = Path(__file__).resolve().parent


def summarize_command_output(output: str) -> list:
    lines = [line.rstrip() for line in output.splitlines()]
    bullets = []

    queued = None
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        m = re.search(r'待嵌入:\s*(\d+)\s*篇', stripped)
        if m:
            count = int(m.group(1))
            if count > 0:
                bullets.append(f"更新 {count} 篇笔记的 embedding")
            continue

        m = re.search(r'Git 检测到\s*(\d+)\s*个重命名', stripped)
        if m:
            count = int(m.group(1))
            if count > 0:
                bullets.append(f"识别并处理 {count} 个 git 检测到的重命名")
            continue

        m = re.search(r'Hash 匹配发现\s*(\d+)\s*个重命名', stripped)
        if m:
            count = int(m.group(1))
            if count > 0:
                bullets.append(f"补充识别 {count} 个基于内容哈希的重命名")
            continue

        m = re.search(r'归档\s*(\d+)\s*个已删除文件的元数据', stripped)
        if m:
            count = int(m.group(1))
            if count > 0:
                bullets.append(f"归档 {count} 个已删除笔记的伴生元数据")
            continue

        m = re.search(r'(?:处理|共)\s*(\d+)\s*篇笔记', stripped)
        if m:
            queued = int(m.group(1))
            continue

        if '✓ 完成 summaries / tags / links' in stripped and queued:
            bullets.append(f"重建 {queued} 篇笔记的 summaries / tags / links")
            continue

        m = re.search(r'✓ 完成 manifest\.md \+ manifest/ \((\d+) 分类\) \+ topics\.md', stripped)
        if m:
            categories = int(m.group(1))
            bullets.append(f"重建索引：manifest / topics，覆盖 {categories} 个分类")
            continue

        m = re.search(r'✓ health-report\.md \((\d+) 篇,\s*(\d+) 孤儿,\s*(\d+) 收件箱\)', stripped)
        if m:
            total = int(m.group(1))
            orphans = int(m.group(2))
            inbox = int(m.group(3))
            bullets.append(f"刷新健康报告：{total} 篇笔记、{orphans} 篇孤儿、{inbox} 项收件箱")
            continue

    deduped = []
    seen = set()
    for bullet in bullets:
        if bullet not in seen:
            deduped.append(bullet)
            seen.add(bullet)
    return deduped


def build_changelog_bullets(full, outputs):
    bullets = []
    for key in ('rename', 'embed', 'summarize', 'index', 'health'):
        bullets.extend(summarize_command_output(outputs.get(key, '')))

    if full:
        bullets.insert(0, '执行全量维护，重算全部派生索引与元数据')

    deduped = []
    seen = set()
    for bullet in bullets:
        if bullet not in seen:
            deduped.append(bullet)
            seen.add(bullet)

    return deduped[:4]


def format_outputs_for_failure(outputs):
    parts = []
    for key in ('rename', 'embed', 'summarize', 'index', 'health'):
        out = outputs.get(key, '').strip()
        if out:
            parts.append(f"[{key}]\n{out}")
    return '\n\n'.join(parts)


def log_subprocess_result(label, result):
    text = (result.stdout or '') + (result.stderr or '')
    if text.strip():
        print(text, end='' if text.endswith('\n') else '\n')
    return text


def run_script(name, full=False, extra_args=None):
    """运行 SCRIPTS_DIR 下脚本；full 时追加 --full。"""
    cmd = [sys.executable, str(SCRIPTS_DIR / name)]
    if full:
        cmd.append('--full')
    if extra_args:
        cmd.extend(extra_args)
    return subprocess.run(
        cmd, cwd=str(VAULT_ROOT),
        capture_output=True, text=True, encoding='utf-8', errors='replace'
    )


def update_current_status(path, start):
    if not path.exists():
        return  # CURRENT.md 已废除，静默跳过
    text = path.read_text(encoding='utf-8')
    new_date = f"**日期**：{start.strftime('%Y-%m-%d')}"
    text = re.sub(r'^\*\*日期\*\*：.*$', new_date, text, count=1, flags=re.MULTILINE)
    text = re.sub(
        r'^\*\*阶段\*\*：.*$',
        '**阶段**：当前无进行中的维护任务；仓库处于可按需维护状态',
        text,
        count=1,
        flags=re.MULTILINE,
    )
    path.write_text(text, encoding='utf-8', newline='')


def generate_file_dates(git_ok):
    """生成 .meta/file-dates.json —— 每文件 git last-commit 时间戳字典。
    从机通过 OneDrive 同步此文件，无需本地 git 即可获取内容年龄。"""
    dates = {}
    if git_ok:
        db_path = VAULT_ROOT / '.meta' / 'embeddings.sqlite'
        if db_path.exists():
            import sqlite3
            conn = sqlite3.connect(str(db_path))
            paths = [row[0] for row in conn.execute("SELECT DISTINCT path FROM embeddings")]
            conn.close()

            for path in paths:
                try:
                    result = subprocess.run(
                        ['git', 'log', '-1', '--format=%at', '--', path],
                        capture_output=True, text=True, cwd=str(VAULT_ROOT),
                        timeout=5
                    )
                    if result.returncode == 0 and result.stdout.strip():
                        dates[path] = float(result.stdout.strip())
                except Exception:
                    pass

    dates_path = VAULT_ROOT / '.meta' / 'file-dates.json'
    dates_path.write_text(json.dumps(dates, ensure_ascii=False), encoding='utf-8')


# ---------------------------------------------------------------------------
# Memory 健康维护 — workflow frequency 自动化 + 衰减预警
# ---------------------------------------------------------------------------
_WORKFLOW_HALF_LIFE_DAYS = 90
_WORKFLOW_DECAY_FREQ_THRESHOLD = 0.05
_WORKFLOW_FREQ_WINDOW_DAYS = 30


def _update_workflow_frequency():
    """扫描 workflow 文件，通过 search_sessions.py 统计最近 30 天触发次数，
    更新 frontmatter frequency 和 last_used 字段。"""
    try:
        import frontmatter as fm
    except ImportError:
        print("  [warn] memory frequency: python-frontmatter 未安装，跳过")
        return

    workflows_dir = VAULT_ROOT / '.meta' / 'memory' / 'workflows'
    if not workflows_dir.exists():
        return

    sessions_script = SCRIPTS_DIR / 'search_sessions.py'
    if not sessions_script.exists():
        print("  [warn] memory frequency: search_sessions.py not found, skipping")
        return

    now = datetime.now()
    cutoff_date = (now - timedelta(days=_WORKFLOW_FREQ_WINDOW_DAYS)).strftime('%Y-%m-%d')

    for wf_file in workflows_dir.glob('*.md'):
        if wf_file.name == 'README.md':
            continue

        try:
            post = fm.load(str(wf_file))
        except Exception:
            continue

        triggers = post.get('triggers', [])
        if not triggers:
            continue

        pattern = '|'.join(triggers)
        try:
            result = subprocess.run(
                [sys.executable, str(sessions_script),
                 pattern, '--json', '--type', 'user',
                 '--since', cutoff_date, '--limit', '9999'],
                capture_output=True, text=True, timeout=30,
                cwd=str(VAULT_ROOT),
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            print(f"  [warn] memory frequency: search_sessions.py unavailable for {wf_file.name}, skipping")
            continue

        if result.returncode != 0:
            print(f"  [warn] memory frequency: search_sessions.py error for {wf_file.name}, skipping")
            continue

        try:
            hits = json.loads(result.stdout) if result.stdout.strip() else []
        except json.JSONDecodeError:
            continue

        count = len(hits) if isinstance(hits, list) else 0
        new_freq = min(count / _WORKFLOW_FREQ_WINDOW_DAYS, 1.0)
        last_used_str = now.strftime('%Y-%m-%d')

        old_freq = post.get('frequency')
        if old_freq is not None and abs(float(old_freq) - new_freq) < 0.001:
            if post.get('last_used') != last_used_str:
                post['last_used'] = last_used_str
                fm.dump(post, str(wf_file))
            continue

        post['frequency'] = round(new_freq, 1)
        post['last_used'] = last_used_str
        fm.dump(post, str(wf_file))


def _insert_changelog_entry_safe(changelog_path, start, full, bullets, skip_changelog):
    """安全插入 CHANGELOG 条目；changelog_append.py 不存在则跳过。"""
    if skip_changelog or not bullets:
        return False
    try:
        sys.path.insert(0, str(SCRIPTS_DIR))
        from changelog_append import insert_changelog_entry
    except ImportError:
        return False
    title = f"维护（{'全量' if full else '增量'}）"
    return insert_changelog_entry(
        changelog_path,
        start.strftime('%Y-%m-%d %H:%M'),
        'agent',
        title,
        bullets,
    )


def update_maintenance_docs(changelog_path, current_path, start, skip_changelog=False):
    """维护文档刷新——lite 版不做本库 CHANGELOG 历史清理（cleanup_empty_changelog_entries），
    避免对本库外的 vault 误删条目。仅安全地更新 CURRENT.md。"""
    update_current_status(current_path, start)


def main(full=False, no_git=False, skip_changelog=False):
    log_script_run()
    start = datetime.now()
    print(f"=== 维护开始 @ {start.isoformat()} ===")
    print(f"模式: {'全量 (--full)' if full else '增量'}  (maintain-lite)")

    # 主从检测
    if not is_primary_host():
        primary = ENV.get('PRIMARY_HOST', '(未设置)')
        print(f"\n⚠️  当前设备 ≠ 主机（{primary}），进入只读模式。")
        print(f"   - ask.py 可用")
        print(f"   - 维护需要在主机运行")
        return 1

    # Git 检测（除非 --no-git）
    git_ok = git_available()
    if not git_ok and not no_git:
        print(f"\n⚠️  未找到 Git 仓库。请先 git init，或使用 --no-git 跳过")
        return 1

    outputs = {}

    # 1/5 · 提交用户改动
    if git_ok and not no_git:
        print("\n[1/5] 提交用户未保存改动 ...")
        st = git('status', '--porcelain', check=False)
        if st.stdout.strip():
            git('add', '-u', check=False)
            git('commit', '-m', f'user: snapshot before agent run {start.isoformat()}', '--allow-empty', check=False)
            print("  ✓ 已打包提交")
        else:
            print("  ✓ 无未提交改动")
    else:
        print("\n[1/5] Git 已跳过")

    # [Phase 2/3] GC — 维护完整 maintain.py 解锁

    # 2/5 · 重命名检测（Phase 1b 守护）
    if (SCRIPTS_DIR / 'detect_renames.py').exists():
        print("\n[2/5] 检测重命名 ...")
        rename_result = run_script('detect_renames.py')
        outputs['rename'] = log_subprocess_result('rename', rename_result)
        if rename_result.returncode != 0:
            print("  ⚠️  detect_renames 失败（非致命）")
    else:
        print("\n[2/5] detect_renames 未安装（Phase 1a 最小安装），跳过")

    # Phase 1b · 校验伴生文件 source（守护）
    if (SCRIPTS_DIR / 'check_sidecar_sources.py').exists():
        print("\n[+] 校验伴生文件 source ...")
        sidecar_result = run_script('check_sidecar_sources.py', extra_args=['--fix'])
        outputs['sidecar_sources'] = log_subprocess_result('sidecar_sources', sidecar_result)
        if sidecar_result.returncode != 0:
            print("  ⚠️  check_sidecar_sources 失败（非致命）")

    # 3/5 · 嵌入（后台启动，与摘要并行）
    print("\n[3/5] 生成嵌入 + 摘要 / tag / 关联（并行）...")
    embed_cmd = [sys.executable, str(SCRIPTS_DIR / 'embed.py')]
    if full:
        embed_cmd.append('--full')
    embed_proc = subprocess.Popen(
        embed_cmd, cwd=str(VAULT_ROOT),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding='utf-8', errors='replace'
    )

    summarize_result = run_script('summarize.py', full=full)
    outputs['summarize'] = log_subprocess_result('summarize', summarize_result)

    embed_stdout, embed_stderr = embed_proc.communicate()
    embed_text = (embed_stdout or '') + (embed_stderr or '')
    if embed_text.strip():
        print(embed_text, end='' if embed_text.endswith('\n') else '\n')
    outputs['embed'] = embed_text
    if embed_proc.returncode != 0:
        print("  ✗ embed 失败，中止")
        return 1

    if summarize_result.returncode != 0:
        print("  ✗ summarize 失败，中止")
        return 1

    # 5/5 · 构建索引
    print("\n[5/5] 构建索引 ...")
    index_result = run_script('build_index.py')
    outputs['index'] = log_subprocess_result('index', index_result)
    if index_result.returncode != 0:
        print("  ✗ build_index 失败")
        return 1

    # Phase 1b · 全局图谱（守护）
    if (SCRIPTS_DIR / 'build_graph.py').exists():
        print("\n[+] 构建全局图谱 ...")
        graph_result = run_script('build_graph.py')
        outputs['graph'] = log_subprocess_result('graph', graph_result)
        if graph_result.returncode != 0:
            print("  ⚠️  build_graph 失败（非致命）")

    # [Phase 2/3] BM25 索引 — 维护完整 maintain.py 解锁
    # [Phase 2/3] 知识地图 (knowledge_map.py) — 维护完整 maintain.py 解锁

    # Phase 1b · 健康报告（守护）
    if (SCRIPTS_DIR / 'health_report.py').exists():
        print("\n[+] 生成健康报告 ...")
        health_result = run_script('health_report.py')
        outputs['health'] = log_subprocess_result('health', health_result)
        if health_result.returncode != 0:
            print("  ⚠️  health_report 失败（非致命）")

    # [Phase 2/3] 合成索引 (synthesis_index.py) — 维护完整 maintain.py 解锁
    # [Phase 2/3] 语义 Lint (semantic_lint) — 维护完整 maintain.py 解锁

    # 附加 · 文件日期清单（供从机检索时间衰减用）
    generate_file_dates(git_ok)

    changelog_path = VAULT_ROOT / 'docs' / 'CHANGELOG.md'
    current_path = VAULT_ROOT / 'docs' / 'CURRENT.md'
    update_maintenance_docs(changelog_path, current_path, start, skip_changelog=skip_changelog)

    # 附加 · workflow frequency 更新（非关键路径）
    print("\n[+] 更新 workflow frequency ...")
    try:
        _update_workflow_frequency()
    except Exception as e:
        print(f"  [warn] workflow frequency update failed: {e}")

    # [Phase 2/3] Dream 报告 (dream.py) — 维护完整 maintain.py 解锁

    # 提交 Agent 产物
    if git_ok and not no_git:
        print("\n提交 Agent 产物 ...")
        st = git('status', '--porcelain', check=False)
        if st.stdout.strip():
            bullets = build_changelog_bullets(full, outputs)
            appended = _insert_changelog_entry_safe(
                changelog_path, start, full, bullets, skip_changelog
            )
            git('add', '.meta', '.index', check=False)
            if (VAULT_ROOT / 'docs' / 'syntheses.md').exists():
                git('add', 'docs/syntheses.md', check=False)
            if (VAULT_ROOT / 'docs' / 'CURRENT.md').exists():
                git('add', 'docs/CURRENT.md', check=False)
            if appended:
                git('add', 'docs/CHANGELOG.md', check=False)
            mode = 'full' if full else 'incremental'
            git('commit', '-m', f'agent: {mode} maintenance (lite) {start.isoformat()}', '--allow-empty', check=False)
            print("  ✓ 已提交")
        else:
            print("  ✓ 无 Agent 产物变化")
    if format_outputs_for_failure(outputs):
        pass

    elapsed = (datetime.now() - start).total_seconds()
    print(f"\n=== 维护完成（lite），耗时 {elapsed:.1f}s ===")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='init-kb-harness 精简维护管线 (Phase 1)')
    parser.add_argument('--full', action='store_true', help='全量重置')
    parser.add_argument('--no-git', action='store_true', help='跳过 git 提交')
    parser.add_argument('--skip-changelog', action='store_true', help='跳过 CHANGELOG 读写')
    args = parser.parse_args()
    sys.exit(main(
        full=args.full,
        no_git=args.no_git,
        skip_changelog=args.skip_changelog,
    ))
