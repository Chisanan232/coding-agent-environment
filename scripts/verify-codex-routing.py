#!/usr/bin/env python3
"""Prove named role routing from fresh native session records, outside Git."""
import argparse
import datetime
import json
import os
from pathlib import Path
import subprocess
import time
import tomllib

ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', required=True)
    args = parser.parse_args()
    output = Path(args.output_dir).resolve()
    if output == ROOT or ROOT in output.parents:
        raise ValueError('Runtime evidence must be stored outside the repository.')
    if output.exists():
        raise ValueError('Use a new evidence directory; existing state is not modified.')
    output.mkdir(parents=True, mode=0o700)
    os.chmod(output, 0o700)
    home = Path(os.environ.get('CODEX_HOME', str(Path(os.environ.get('CODING_AGENT_SYNC_HOME', str(Path.home()))) / '.codex')))
    env = dict(os.environ, CODEX_HOME=str(home))
    subprocess.run(['bash', str(ROOT / 'scripts/profile-install.sh'), '--routing', '--check'], check=True, env=env)
    config = tomllib.loads((home / 'config.toml').read_text())
    expected = {}
    for role in ('architect', 'implementer', 'reviewer'):
        body = tomllib.loads((home / config['agents'][role]['config_file']).read_text())
        expected[role] = (body['model'], body['model_reasoning_effort'])
    command = ['codex', 'exec', '--json', '--sandbox', 'read-only', '--output-last-message', str(output / 'last-message.txt')]
    # Disable only actually configured user MCP servers for this tiny routing
    # probe. No provider, role, reasoning or default-delegate overrides.
    for name in config.get('mcp_servers', {}):
        command += ['-c', 'mcp_servers.' + name + '.enabled=false']
    command.append('Fresh routing verification only. Spawn exactly three independent read-only agents using named architect, implementer, reviewer roles with fork_turns="none". Each must reply only with its role name, use no tools, edit nothing, and spawn no agents. Wait for all three to finish. Do not override models or reasoning. Report completion.')
    started = time.time()
    with (output / 'events.jsonl').open('w') as events, (output / 'stderr.txt').open('w') as errors:
        subprocess.run(command, cwd=ROOT, stdout=events, stderr=errors, check=True, timeout=300, env=env)
    events = [json.loads(line) for line in (output / 'events.jsonl').read_text().splitlines()]
    parent = next(e['thread_id'] for e in events if e.get('type') == 'thread.started')
    actual, root = {}, None
    for path in (home / 'sessions').rglob('*.jsonl'):
        if path.stat().st_mtime < started - 2:
            continue
        with path.open() as stream:
            meta = json.loads(next(stream)).get('payload', {})
            spawn = meta.get('source', {})
            spawn = spawn.get('subagent', {}).get('thread_spawn', {}) if isinstance(spawn, dict) else {}
            role = spawn.get('agent_role') if spawn.get('parent_thread_id') == parent else None
            is_root = meta.get('id') == parent
            if role not in expected and not is_root:
                continue
            for line in stream:
                event = json.loads(line)
                if event.get('type') == 'turn_context':
                    context = event['payload']
                    record = {'model': context['model'], 'reasoning': context.get('effort'),
                              'provider': meta.get('model_provider'), 'thread_id': meta['id']}
                    if is_root:
                        root = record
                    else:
                        if role in actual:
                            raise ValueError('Duplicate role execution in routing probe.')
                        actual[role] = record
                    break
    if set(actual) != set(expected):
        raise ValueError('Missing native child records; file presence or model narration is not proof.')
    if root is None:
        raise ValueError('Missing fresh native parent turn-context record.')
    for role, record in actual.items():
        if (record['model'], record['reasoning']) != expected[role]:
            raise ValueError('Recorded model/reasoning diverged for ' + role)
    result = {'verified_at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'codex_version': subprocess.check_output(['codex', '--version'], text=True, env=env).strip(),
              'root': root, 'roles': actual,
              'max_concurrent_subagents': config['agents']['max_concurrent_threads_per_session']}
    (output / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({k: v for k, v in result.items() if k not in ('root', 'roles')}))
    for role, record in actual.items():
        print(role + ': ' + record['model'] + ' / ' + record['reasoning'])


if __name__ == '__main__':
    main()
