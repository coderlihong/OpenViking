"""Validate current spec task references and evidence gates, not product behavior.

Run with the repository Python: python docs/spec/tools/check_spec_status.py.
Historical implementation evidence is immutable and excluded from current-state
validation. A successful check never changes product acceptance to PASS.
"""
from pathlib import Path
import json
import re


def check(root: Path) -> dict:
    data = json.loads((root / 'feature-status.json').read_text())
    spec = (root / 'openviking-enterprise-kb-spec.md').read_text()
    cases = (root / 'openviking-enterprise-kb-test-cases.md').read_text()
    backlog = (root / 'openviking-enterprise-kb-backlog.md').read_text()
    readme = (root / 'README.md').read_text()
    errors = []

    def require(condition, message):
        if not condition:
            errors.append(message)

    tasks = {task['id']: task for task in data['tasks']}
    features = {feature['id']: feature for feature in data['features']}
    require(len(tasks) == len(data['tasks']), 'Duplicate task IDs')
    require(set(features) == {f'F{i}' for i in range(1, 16)}, 'Expected F1–F15')
    require(set(data['flows']) == {'FLOW-D1', 'FLOW-D2', 'FLOW-W1', 'FLOW-W2'}, 'Expected four FLOWs')
    require(all(f'v{data["version"]}' in text for text in [spec, cases, readme]), 'Spec/cases/README version mismatch')
    latest = data.get('latest_implementation')
    if latest:
        for filename in ['README.md', 'openviking-enterprise-kb-spec.md',
                         'openviking-enterprise-kb-backlog.md', 'openviking-enterprise-kb-test-cases.md',
                         'openviking-enterprise-kb-implementation.md']:
            current = re.search(r'^> 当前执行状态：\s*(I\d+)\b', (root / filename).read_text(), re.M)
            require(current is not None and current.group(1) == latest,
                    f'{filename}: current execution does not match ledger {latest}')
        report = data.get('latest_scoped_report')
        require(bool(report) and (root / report).is_file(), 'Latest execution report is missing')
    tc_ids = set(re.findall(r'^\|\s*(TC-\d+\.\d+)\s*\|', cases, re.M))
    flow_ids = set(re.findall(r'^\|\s*(FLOW-[A-Z0-9-]+)\s*\|', cases, re.M))
    for task_id, task in tasks.items():
        require(task_id in backlog, f'{task_id} missing from readable backlog')
        require(task['implementation_state'] in {'OPEN', 'PARTIAL', 'DONE'}, f'{task_id} invalid implementation state')
        require(bool(task['work'].strip() and task['done_when'].strip()), f'{task_id} lacks concrete work/done condition')
        require(bool(task['acceptance']), f'{task_id} has no acceptance mapping')
        require(set(task['features']) <= set(features), f'{task_id} unknown Feature')
        require(set(task['depends_on']) <= set(tasks), f'{task_id} unknown prerequisite')
        for assertion in task['acceptance']:
            require(assertion in tc_ids or assertion in flow_ids, f'{task_id} unknown acceptance {assertion}')
        if task['implementation_state'] == 'DONE' or task['product_acceptance'] == 'PASS':
            reports = task.get('completion_evidence', [])
            require(bool(reports), f'{task_id} completion requires explicit reports')
            for report in reports:
                require((root / report).is_file(), f'{task_id} missing completion report {report}')
    for feature_id, feature in features.items():
        require(bool(feature['tasks']), f'{feature_id} no remaining/delivery tasks')
        require(set(feature['tasks']) <= set(tasks), f'{feature_id} unknown task')
        for task_id in feature['tasks']:
            if task_id in tasks:
                require(feature_id in tasks[task_id]['features'], f'{feature_id}/{task_id} mapping mismatch')
        if feature['product_acceptance'] == 'PASS':
            require(bool(feature.get('acceptance_report')), f'{feature_id} PASS lacks full acceptance report')
    visiting, visited = set(), set()

    def visit(task_id):
        if task_id in visiting:
            errors.append(f'Task dependency cycle at {task_id}')
            return
        if task_id in visited or task_id not in tasks:
            return
        visiting.add(task_id)
        for dependency in tasks[task_id]['depends_on']:
            visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in tasks:
        visit(task_id)
    require('create_namespace(' not in spec, 'Obsolete SDK name create_namespace')
    require('9.2 最终 Wiki 执行接口' in spec and '36.1 共同发布' in spec, 'Missing execution/publication contract')
    for filename in ['README.md', 'openviking-enterprise-kb-spec.md',
                     'openviking-enterprise-kb-backlog.md', 'openviking-enterprise-kb-test-cases.md']:
        for target in re.findall(r'\]\(([^)]+)\)', (root / filename).read_text()):
            if '://' not in target and not target.startswith('#'):
                require((root / target.split('#')[0]).is_file(), f'{filename}: broken link {target}')
    if errors:
        raise ValueError('\n'.join(errors))
    return {'status': 'PASS', 'scope': 'documentation consistency only, not product acceptance',
            'version': data['version'], 'features': len(features), 'tasks': len(tasks),
            'cross_acceptance_cases': len({x for x in tc_ids if x.startswith('TC-16.')}),
            'flows': data['flows']}


if __name__ == '__main__':
    print(json.dumps(check(Path(__file__).resolve().parents[1]), ensure_ascii=False, indent=2))
