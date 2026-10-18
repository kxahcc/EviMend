"""Readable pilot evidence, using only terminal exported runs and raw ledgers."""
from __future__ import annotations
import collections
import csv
import hashlib
import io
import json
import sqlite3
import tempfile
import zipfile
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

root = Path(__file__).resolve().parents[2]
out = root / 'runs/exports/phase2'
record = json.loads((out / 'index.json').read_bytes())
rows = []
comparisons = collections.defaultdict(list)
for entry in record['results']:
    folder = out / entry['result']
    summary = json.loads((folder / 'summary.json').read_bytes())
    assert summary['report_sha256'] == entry['report_sha256']
    with zipfile.ZipFile(folder / 'evidence.zip') as archive:
        agent = json.loads(archive.read('run/agent-report.json'))
        report = json.loads(archive.read('run/competition-report.json'))
        profile = json.loads(archive.read('run/competition-config.json'))
        ledger_bytes = archive.read('run/model-calls.sqlite') if entry['category'] == 'ood' else None
    actions = collections.Counter(a['tool_name'] for a in agent['actions'] if a['state'] == 'completed')
    row = {k: entry[k] for k in ('run_id', 'task_id', 'category', 'variant', 'status', 'candidate_count', 'stopped_reason', 'result')}
    u = summary['usage']
    row.update(source_pass=summary['status'] == 'source_verified' and summary['accepted'],
        model_calls=u['completed_calls'], tool_calls=summary['metrics']['agent_chain_length'],
        cache_hit_input_tokens=u['cache_hit_tokens'], cache_miss_input_tokens=u['cache_miss_tokens'],
        output_tokens=u['known_output_tokens'], total_tokens=u['known_total_tokens'],
        estimated_cny=summary['model_cost']['estimated_ceiling_cny'], agent_wall_ms=summary['timing']['agent_wall_ms'],
        cli_wall_ms=entry.get('cli_wall_ms'), first_candidate_pass=summary['metrics']['first_attempt_pass'],
        skill_reads=actions['read_skill'], rule_queries=actions['consult_rule'],
        draft_edits=actions['edit_source'],
        obligations=len(report['obligations']), decisions=len(report['decisions']),
        context_plans=len(agent['working_context_plans']), usage_complete=u['usage_complete'])
    if entry['category'] == 'ood':
        # Terminal exported ledger only: immutable opens the closed main image
        # without creating WAL/SHM files or touching the original run database.
        assert ledger_bytes is not None
        with tempfile.TemporaryDirectory(prefix='pilot-ledger-', dir=root / '.dfx-local/tmp') as temporary:
            ledger = Path(temporary) / 'model-calls.sqlite'
            ledger.write_bytes(ledger_bytes)
            db = sqlite3.connect(ledger.as_uri() + '?mode=ro&immutable=1', uri=True)
            try:
                raw = db.execute('SELECT message_json FROM model_calls WHERE call_id=?', (f"model-{u['completed_calls']}",)).fetchone()
            finally:
                db.close()
        message = json.loads(raw[0]) if raw and raw[0] else {}
        data = message.get('data', message)
        content = data.get('content', data.get('text', ''))
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        row['no_fabricated_patch_or_root'] = entry['expectation_met'] and not row['draft_edits']
        row['autonomous_abstention_complete'] = row['no_fabricated_patch_or_root'] and not entry['stopped_reason'] and bool(content.strip())
        if content.strip():
            (folder / 'MODEL-RESPONSE.md').write_text(content + '\n', encoding='utf-8')
    elif entry['category'] == 'ablation':
        same = json.loads(json.dumps(profile['config']))
        same.pop('static_skills_enabled')
        same.pop('policy_features')
        corpus = agent['knowledge_profile']['approval']['snapshot_sha256']
        comparisons[entry['task_id']].append({'variant': entry['variant'], 'common_config': same,
            'source_snapshot_sha256': summary['source_snapshot_sha256'], 'crash_sha256': profile['task']['crash_sha256'],
            'corpus_sha256': corpus, 'offered_tools_sha256': hashlib.sha256(json.dumps(agent['offered_tools'], sort_keys=True).encode()).hexdigest()})
    rows.append(row)
    text = ['# 本次实验结果', '', f"任务：{row['task_id']}；组：{row['variant']}；运行：{row['run_id']}。", '',
        f"原报告状态：{row['status']}；停止原因：{row['stopped_reason'] or '正常模型结束'}。", '',
        f"模型调用{row['model_calls']}，工具调用{row['tool_calls']}；缓存命中输入{row['cache_hit_input_tokens']}、未命中输入{row['cache_miss_input_tokens']}、输出{row['output_tokens']} Token。", '',
        f"估算费用{row['estimated_cny']}元；Agent耗时{row['agent_wall_ms']}ms；包含启动/导出的CLI耗时{row['cli_wall_ms']}ms。", '',
        '[原报告与证据](README.md) · [根因记录](root-cause.json) · [检查记录](verification.json)', '',
        '完整HarmonyOS应用未执行；该小规模实验不构成显著性或泛化证明。', '']
    (folder / 'RESULT.md').write_text('\n'.join(text), encoding='utf-8')
checks = []
for task, values in comparisons.items():
    complete = len(values) == 3
    common = [{k: v for k, v in item.items() if k != 'variant'} for item in values]
    equal = complete and all(item == common[0] for item in common[1:])
    checks.append({'task_id': task, 'complete_three_variants': complete, 'same_common_configuration_corpus_tools_input': equal,
        'variants': [item['variant'] for item in values]})
    if complete and not equal:
        raise ValueError(f'Actual comparison identity mismatch: {task}')
totals = {'completed_runs': len(rows), 'planned_runs': record['planned_runs'],
    'estimated_cny': str(sum((Decimal(row['estimated_cny']) for row in rows), Decimal(0))),
    'ood_no_fabricated_patch_or_root': sum(row.get('no_fabricated_patch_or_root', False) for row in rows),
    'ood_autonomous_abstention_complete': sum(row.get('autonomous_abstention_complete', False) for row in rows),
    'ablation_source_passes': sum(row['source_pass'] for row in rows if row['category'] == 'ablation')}
final_ood = {row['task_id']: row for row in rows if row['category'] == 'ood'}
totals['ood_final_cases'] = len(final_ood)
totals['ood_final_complete_abstentions'] = sum(row['autonomous_abstention_complete'] for row in final_ood.values())
positive = [row for row in rows if row['category'] == 'ood_positive']
totals['ood_positive_controls'] = len(positive)
totals['ood_positive_source_passes'] = sum(row['source_pass'] for row in positive)
fields = sorted({key for row in rows for key in row})
stream = io.StringIO(newline='')
writer = csv.DictWriter(stream, fieldnames=fields)
writer.writeheader()
writer.writerows(rows)
(out / 'results.csv').write_text(stream.getvalue(), encoding='utf-8-sig', newline='')
(out / 'evaluation.json').write_text(json.dumps({'generated_at_utc': datetime.now(UTC).isoformat(),
    'totals': totals, 'actual_comparison_checks': checks, 'results': rows}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
lines = ['# OOD与小规模消融结果', '', f"预定{record['planned_runs']}次，已完成{len(rows)}次；模型费用估算{totals['estimated_cny']}元。", '',
    '[逐次统计CSV](results.csv) · [评估和实际可比性核验](evaluation.json) · [预登记协议](protocol.json)', '',
    '模型OOD的“未虚构补丁/根因”和“自主完整结束”分别记录，预算终止不算自主拒答通过。', '',
    '| 任务 | 组 | 结果 | 停止原因 | 模型调用 | 命中输入 | 未命中输入 | 输出 | 估算元 | 产物 |',
    '|---|---|---|---|---:|---:|---:|---:|---:|---|']
for row in rows:
    lines.append(f"| {row['task_id']} | {row['variant']} | {row['status']} | {row['stopped_reason'] or '—'} | {row['model_calls']} | {row['cache_hit_input_tokens']} | {row['cache_miss_input_tokens']} | {row['output_tokens']} | {row['estimated_cny']} | [打开]({row['result']}/RESULT.md) |")
lines += ['', '比较保持完整thinking历史，不检验前沿上下文剪枝或自动学习；只有两例、每组一次，费用受实际缓存命中及运行顺序影响。设计/实现能力、运行消费与效果收益分别解释。', '']
(out / 'README.md').write_text('\n'.join(lines), encoding='utf-8')
print(json.dumps(totals, ensure_ascii=False))
