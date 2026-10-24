#!/usr/bin/env python3
"""Exercise visual skill pinning, host activation, and failure isolation offline."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1]
PACKAGE_NAMES = ('archify', 'visual-explainer')


class VisualSkillsInstall(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='visual-skills-', dir=SOURCE)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / 'home'
        self.repo = self.root / 'repo'
        self.upstreams = self.root / 'upstreams'
        self.home.mkdir()
        self.upstreams.mkdir()
        shutil.copytree(SOURCE / 'scripts', self.repo / 'scripts')
        shutil.copytree(SOURCE / 'codex', self.repo / 'codex')
        self.fakebin = self.root / 'bin'
        self.fakebin.mkdir()
        self.cli_log = self.root / 'claude-calls.jsonl'
        self.cli_state = self.root / 'claude-state'
        self.cli_state.mkdir()
        self._write_fake_claude()
        (self.cli_state / 'marketplaces.json').write_text(json.dumps([{'name': 'other-marketplace', 'source': 'directory', 'path': '/user/other-marketplace', 'installLocation': '/user/other-marketplace'}]))
        (self.cli_state / 'plugins.json').write_text(json.dumps([{'id': 'unrelated@other-marketplace', 'version': '9', 'scope': 'user', 'enabled': True, 'installPath': '/user/other-plugin'}]))
        self.env = dict(os.environ)
        self.env.pop('CODEX_HOME', None)
        self.env.update({
            'CLAUDE_CONFIG_DIR': str(self.home / '.claude'),
            'PATH': str(Path(sys.executable).parent) + os.pathsep + os.environ.get('PATH', ''),
            'PYTHONPATH': str(self.fakebin) + os.pathsep + os.environ.get('PYTHONPATH', ''),
            'CODING_AGENT_SYNC_HOME': str(self.home),
            'FAKE_CLAUDE_STATE': str(self.cli_state),
            'FAKE_CLAUDE_SCRIPT': str(self.fakebin / 'claude-fake.sh'),
            'FAKE_CLAUDE_LOG': str(self.cli_log),
        })
        self._create_upstreams()
        self.set_pins('v1')
        self.protected = {
            self.home / '.claude/settings.json': b'{"enabledPlugins":{"unrelated":true}}\n',
            self.home / '.claude/auth.json': b'opaque claude auth bytes\0',
            self.home / '.codex/config.toml': b'model = "user-choice"\n',
            self.home / '.codex/auth.json': b'opaque codex auth bytes\0',
            self.home / '.agents/skills/subtraction/SKILL.md': b'pre-existing unrelated skill\n',
        }
        for path, contents in self.protected.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(contents)

    def _write_fake_claude(self):
        script = self.fakebin / 'claude-fake.sh'
        script.write_text(r'''#!/bin/bash
set -euo pipefail
state="$FAKE_CLAUDE_STATE"
log="$FAKE_CLAUDE_LOG"
markets="$state/marketplaces.json"
plugins="$state/plugins.json"
jq -nc --args '$ARGS.positional' -- "$@" >> "$log"
printf '%s' "${CLAUDE_CONFIG_DIR:-<unset>}" > "$state/seen-claude-config-dir"
if [[ "$*" == "plugin list --json" ]]; then
    cat "$plugins"
    exit 0
fi
if [[ "$*" == "plugin marketplace list --json" ]]; then
    count_file="$state/market-list-count"
    count=1
    [[ ! -f "$count_file" ]] || count=$(( $(cat "$count_file") + 1 ))
    printf '%s' "$count" > "$count_file"
    if [[ "${FAKE_CLAUDE_MUTATE_MARKET_ON_LIST:-}" == "$count" ]]; then
        jq '. + [{"name":"visual-explainer-marketplace","source":"directory","path":"/foreign/concurrent-marketplace","installLocation":"/foreign/concurrent-marketplace"}]' "$markets" > "$markets.tmp"
        mv "$markets.tmp" "$markets"
    fi
    cat "$markets"
    exit 0
fi
if [[ "$1 $2 $3" == "plugin marketplace add" ]]; then
    source_path=""
    for arg in "${@:4}"; do
        [[ "$arg" == --* || "$arg" == user ]] || source_path="$arg"
    done
    name=$(jq -r '.name' "$source_path/.claude-plugin/marketplace.json")
    jq --arg name "$name" --arg path "$source_path" 'map(select(.name != $name)) + [{"name":$name,"source":"directory","path":$path,"installLocation":$path}]' "$markets" > "$markets.tmp"
    mv "$markets.tmp" "$markets"
    exit 0
fi
if [[ "$1 $2 $3" == "plugin marketplace remove" ]]; then
    name=""
    for arg in "${@:4}"; do
        [[ "$arg" == --* || "$arg" == user ]] || name="$arg"
    done
    jq --arg name "$name" 'map(select(.name != $name))' "$markets" > "$markets.tmp"
    mv "$markets.tmp" "$markets"
    jq --arg suffix "@$name" 'map(select((.id | endswith($suffix)) | not))' "$plugins" > "$plugins.tmp"
    mv "$plugins.tmp" "$plugins"
    exit 0
fi
if [[ "$1 $2" == "plugin install" ]]; then
    if [[ "${FAKE_CLAUDE_FAIL_ON:-}" == install || ( "${FAKE_CLAUDE_FAIL_ON:-}" == install-once && ! -e "$state/failed-once" ) ]]; then
        : > "$state/failed-once"
        echo 'synthetic plugin install failure' >&2
        exit 17
    fi
    plugin_id="$3"
    name="${plugin_id%@*}"
    market="${plugin_id#*@}"
    source=$(jq -r --arg name "$market" '.[] | select(.name == $name) | .path' "$markets")
    plugin_source="$source/plugins/$name"
    install="$state/cache/$market/$name"
    if [[ -e "$install" ]]; then chmod -R u+w "$install"; rm -rf "$install"; fi
    mkdir -p "$(dirname "$install")"
    cp -R "$plugin_source" "$install"
    version=$(jq -r '.version // "fixture"' "$plugin_source/.claude-plugin/plugin.json")
    jq --arg id "$plugin_id" --arg version "$version" --arg install "$install" 'map(select(.id != $id)) + [{"id":$id,"version":$version,"scope":"user","enabled":true,"installPath":$install}]' "$plugins" > "$plugins.tmp"
    mv "$plugins.tmp" "$plugins"
    if [[ -n "${FAKE_CLAUDE_SWAP_CODEX_COPY:-}" ]]; then
        target="${FAKE_CLAUDE_SWAP_CODEX_COPY%%|*}"
        source="${FAKE_CLAUDE_SWAP_CODEX_COPY#*|}"
        chmod -R u+w "$target"
        rm -rf "$target"
        ln -s "$source" "$target"
    fi
    exit 0
fi
echo "unsupported fake Claude command: $*" >&2
exit 64
''')
        sitecustomize = self.fakebin / 'sitecustomize.py'
        sitecustomize.write_text('''import os, subprocess
_real_run = subprocess.run
def _run(command, *args, **kwargs):
    if isinstance(command, (list, tuple)) and command and command[0] == "claude":
        command = ["/bin/bash", os.environ["FAKE_CLAUDE_SCRIPT"], *command[1:]]
    return _real_run(command, *args, **kwargs)
subprocess.run = _run
''')

    def _git(self, repo, *args):
        return subprocess.run(['git', '-C', str(repo), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def _create_upstreams(self):
        self.arch_repo = self.upstreams / 'archify.git'
        self.visual_repo = self.upstreams / 'visual-explainer.git'
        for repo in (self.arch_repo, self.visual_repo):
            repo.mkdir()
            self._git(repo, 'init', '-q')
            self._git(repo, 'config', 'user.email', 'fixture@example.invalid')
            self._git(repo, 'config', 'user.name', 'Visual fixture')
        self._write_archify('v1')
        self._write_visual('v1')
        for repo in (self.arch_repo, self.visual_repo):
            self._git(repo, 'add', '.')
            self._git(repo, 'commit', '-qm', 'fixture v1')
            self._git(repo, 'tag', 'v1')
        self._write_archify('v2')
        self._write_visual('v2')
        for repo in (self.arch_repo, self.visual_repo):
            self._git(repo, 'add', '.')
            self._git(repo, 'commit', '-qm', 'fixture v2')
            self._git(repo, 'tag', 'v2')

    def _write_archify(self, version):
        base = self.arch_repo / 'archify'
        (base / 'scripts').mkdir(parents=True, exist_ok=True)
        (self.arch_repo / 'LICENSE').write_text('fixture license\n')
        (base / 'SKILL.md').write_text(f'# Archify {version}\n')
        renderer = base / 'scripts/render.py'
        renderer.write_text(f'print("archify {version}")\n')
        renderer.chmod(0o755)

    def _write_visual(self, version):
        (self.visual_repo / '.claude-plugin').mkdir(parents=True, exist_ok=True)
        plugin = self.visual_repo / 'plugins/visual-explainer'
        (plugin / '.claude-plugin').mkdir(parents=True, exist_ok=True)
        (plugin / 'commands').mkdir(parents=True, exist_ok=True)
        (plugin / 'assets').mkdir(parents=True, exist_ok=True)
        (self.visual_repo / 'LICENSE').write_text('fixture license\n')
        (self.visual_repo / '.claude-plugin/marketplace.json').write_text(json.dumps({
            'name': 'visual-explainer-marketplace',
            'owner': {'name': 'Offline fixture'},
            'plugins': [{'name': 'visual-explainer', 'source': './plugins/visual-explainer'}],
        }, indent=2) + '\n')
        (plugin / '.claude-plugin/plugin.json').write_text(json.dumps({
            'name': 'visual-explainer', 'version': version, 'description': 'Fixture plugin',
        }, indent=2) + '\n')
        (plugin / 'SKILL.md').write_text(f'# Visual Explainer {version}\n')
        (plugin / 'commands/render.md').write_text(f'# Render {version}\n')
        (plugin / 'assets/example.svg').write_text(f'<svg><!-- {version} --></svg>\n')

    @staticmethod
    def _tree_digest(root):
        """Independent implementation of the committed path/mode/content framing."""
        h = hashlib.sha256()
        for path in sorted((p for p in root.rglob('*') if p.is_file()), key=lambda p: p.relative_to(root).as_posix()):
            rel = path.relative_to(root).as_posix().encode()
            mode = b'0755' if path.stat().st_mode & 0o111 else b'0644'
            h.update(rel + b'\0' + mode + b'\0' + hashlib.sha256(path.read_bytes()).digest() + b'\0')
        return h.hexdigest()

    def _revision_tree(self, repo, revision, paths, destination):
        destination.mkdir(parents=True)
        archive = subprocess.run(['git', '-C', str(repo), 'archive', '--format=tar', revision, *paths],
                                 check=True, capture_output=True).stdout
        import io, tarfile
        with tarfile.open(fileobj=io.BytesIO(archive), mode='r:') as tar:
            tar.extractall(destination, filter='data')
        return destination

    def pin_revision(self, package, version):
        return self._git(self.arch_repo if package == 'archify' else self.visual_repo, 'rev-parse', version)

    def set_pins(self, version):
        manifest_path = self.repo / 'codex/subtraction-skills.json'
        manifest = json.loads(manifest_path.read_text())
        visual = manifest.setdefault('visuals', {'packages': {}})
        packages = visual.setdefault('packages', {})
        specs = {
            'archify': (self.arch_repo, ['LICENSE', 'archify'], ['LICENSE', 'archify']),
            'visual-explainer': (self.visual_repo, ['LICENSE', '.claude-plugin', 'plugins/visual-explainer'],
                                 ['LICENSE', '.claude-plugin', 'plugins/visual-explainer']),
        }
        for name, (repo, paths, digest_paths) in specs.items():
            revision = self._git(repo, 'rev-parse', version)
            checkout = self.root / f'checkout-{name}-{version}'
            if checkout.exists():
                shutil.rmtree(checkout)
            self._revision_tree(repo, revision, digest_paths, checkout)
            packages[name] = {
                'repository': repo.as_uri(), 'tag': version, 'revision': revision,
                'paths': paths, 'integrity': 'sha256:' + self._tree_digest(checkout),
            }
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')

    def run_installer(self, *args, success=True):
        result = subprocess.run(['bash', str(self.repo / 'scripts/profile-install.sh'), '--visuals', *args],
                                cwd=self.repo, env=self.env, text=True, capture_output=True, timeout=30)
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        return result

    def call_log(self):
        if not self.cli_log.exists():
            return []
        return [json.loads(line) for line in self.cli_log.read_text().splitlines()]

    def mutation_calls(self):
        return [args for args in self.call_log() if args[:3] in (
            ['plugin', 'marketplace', 'add'], ['plugin', 'marketplace', 'remove'])
            or args[:2] in (['plugin', 'install'], ['plugin', 'uninstall'])]

    def snapshot_base(self):
        return self.home / '.local/share/coding-agent-environment/visual-skills'

    def links(self):
        return {
            'codex_archify': self.home / '.agents/skills/archify',
            'codex_visual': self.home / '.agents/skills/visual-explainer',
            'claude_archify': self.home / '.claude/skills/archify',
        }

    def native_state_snapshot(self):
        return {p.relative_to(self.cli_state).as_posix(): p.read_bytes() for p in self.cli_state.rglob('*') if p.is_file() and p.name not in {'failed-once', 'market-list-count', 'seen-claude-config-dir'}}

    def state_snapshot(self):
        return {p.relative_to(self.home).as_posix(): (p.is_symlink(), os.readlink(p) if p.is_symlink() else p.read_bytes())
                for p in self.home.rglob('*') if p.is_file() or p.is_symlink()}

    def assert_protected_unchanged(self):
        for path, contents in self.protected.items():
            self.assertEqual(path.read_bytes(), contents, str(path))

    def test_fresh_apply_repeat_check_and_dry_run_preserve_unrelated_state(self):
        before = self.state_snapshot()
        manifest_path = self.repo / 'codex/subtraction-skills.json'
        original_manifest = manifest_path.read_bytes()
        invalid_manifest = json.loads(original_manifest)
        invalid_manifest['visuals']['packages']['archify']['repository'] = 'file:///missing/local/upstream'
        manifest_path.write_text(json.dumps(invalid_manifest, indent=2) + '\n')
        result = self.run_installer('--dry-run')
        manifest_path.write_bytes(original_manifest)
        self.assertEqual(self.state_snapshot(), before)
        self.assertEqual(self.mutation_calls(), [])
        self.assertIn('archify', result.stdout.lower())
        self.assertEqual((self.cli_state / 'seen-claude-config-dir').read_text(), str(self.home / '.claude'))
        self.run_installer()
        installed = self.state_snapshot()
        native_installed = self.native_state_snapshot()
        for name, link in self.links().items():
            if name == 'codex_visual':
                self.assertTrue(link.is_dir(), name)
                self.assertFalse(link.is_symlink(), name)
                self.assertIn('Visual Explainer v1', (link / 'SKILL.md').read_text())
            else:
                self.assertTrue(link.is_symlink(), name)
                self.assertEqual(link.resolve().name, 'archify')
        self.assertTrue((self.home / '.claude/skills/archify/SKILL.md').is_file())
        self.assertTrue((self.links()['codex_visual'] / 'commands/render.md').is_file())
        plugin_rows = json.loads((self.cli_state / 'plugins.json').read_text())
        plugin = next(row for row in plugin_rows if row['id'] == 'visual-explainer@visual-explainer-marketplace')
        self.assertIn('unrelated@other-marketplace', {row['id'] for row in plugin_rows})
        marketplace_rows = json.loads((self.cli_state / 'marketplaces.json').read_text())
        self.assertIn('other-marketplace', {row['name'] for row in marketplace_rows})
        self.assertIn('Visual Explainer v1', Path(plugin['installPath'], 'SKILL.md').read_text())
        self.assert_protected_unchanged()
        for repo in (self.arch_repo, self.visual_repo):
            repo.rename(repo.with_name(repo.name + '.offline'))
        self.run_installer('--check')
        sync = subprocess.run(['bash', str(self.repo / 'scripts/sync-check.sh'), '--visuals'],
                              cwd=self.repo, env=self.env, text=True, capture_output=True)
        self.assertEqual(sync.returncode, 0, sync.stdout + sync.stderr)
        self.assertEqual((self.cli_state / 'seen-claude-config-dir').read_text(), str(self.home / '.claude'))
        for offline in self.upstreams.glob('*.offline'):
            offline.rename(offline.with_name(offline.name.removesuffix('.offline')))
        self.run_installer()
        self.assertEqual(self.state_snapshot(), installed)
        self.assertEqual(self.native_state_snapshot(), native_installed)
        self.assert_protected_unchanged()
        checked = self.state_snapshot()
        self.run_installer('--check')
        self.assertEqual(self.state_snapshot(), checked)

    def test_tree_digest_uses_unambiguous_content_boundaries(self):
        left = self.root / 'digest-left'
        right = self.root / 'digest-right'
        left.mkdir()
        right.mkdir()
        (left / 'a').write_bytes(b'x')
        (left / 'b').write_bytes(b'y')
        (right / 'a').write_bytes(b'x\0b\0' + b'0644\0y')
        spec = importlib.util.spec_from_file_location('visual_skills_digest', self.repo / 'scripts/visual-skills.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertNotEqual(module.digest_tree(left), module.digest_tree(right))

    def test_foreign_directory_or_wrong_link_refuses_before_other_mutation(self):
        for kind in ('directory', 'link'):
            with self.subTest(kind=kind):
                shutil.rmtree(self.home / '.agents/skills/archify', ignore_errors=True)
                (self.home / '.agents/skills').mkdir(parents=True, exist_ok=True)
                foreign = self.home / '.agents/skills/archify'
                if kind == 'directory':
                    foreign.mkdir()
                    (foreign / 'mine').write_bytes(b'leave me')
                else:
                    destination = self.home / 'foreign-destination'
                    destination.write_bytes(b'leave me')
                    foreign.symlink_to(destination)
                before = self.state_snapshot()
                self.run_installer(success=False)
                self.assertEqual(self.state_snapshot(), before)
                self.assertFalse((self.home / '.agents/skills/visual-explainer').exists())
                self.assertEqual(self.mutation_calls(), [])
                self.assert_protected_unchanged()
                if foreign.is_symlink():
                    foreign.unlink()
                else:
                    shutil.rmtree(foreign)

    def test_pending_pin_change_is_refused_without_mutation(self):
        self.env['FAKE_CLAUDE_FAIL_ON'] = 'install-once'
        self.run_installer(success=False)
        managed = self.state_snapshot()
        native = self.native_state_snapshot()
        self.set_pins('v2')
        result = self.run_installer(success=False)
        self.assertIn('Complete the pending pinned operation', result.stderr)
        self.assertEqual(managed, self.state_snapshot())
        self.assertEqual(native, self.native_state_snapshot())
        self.set_pins('v1')
        self.run_installer()
        self.run_installer('--check')

    def test_disabled_native_plugin_is_reported_as_drift(self):
        self.run_installer()
        path = self.cli_state / 'plugins.json'
        plugins = json.loads(path.read_text())
        item = next(row for row in plugins if row['id'] == 'visual-explainer@visual-explainer-marketplace')
        item['enabled'] = False
        path.write_text(json.dumps(plugins))
        before = self.state_snapshot()
        native_before = self.native_state_snapshot()
        self.run_installer('--check', success=False)
        self.assertEqual(self.state_snapshot(), before)
        self.assertEqual(self.native_state_snapshot(), native_before)

    def test_interrupted_pending_activation_can_be_retried(self):
        helper = self.repo / 'scripts/visual-skills.py'
        spec = importlib.util.spec_from_file_location('visual_skills_pending', helper)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        import sys
        def interrupt(_args):
            raise SystemExit('synthetic process termination after prepared state')
        real_run = subprocess.run
        def route_fake(command, *args, **kwargs):
            if isinstance(command, (list, tuple)) and command and command[0] == 'claude':
                command = ['/bin/bash', str(self.fakebin / 'claude-fake.sh'), *command[1:]]
            return real_run(command, *args, **kwargs)
        with mock.patch.dict(os.environ, self.env, clear=False), \
             mock.patch.object(subprocess, 'run', side_effect=route_fake), \
             mock.patch.object(module, 'run_claude', side_effect=interrupt), \
             mock.patch.object(sys, 'argv', [str(helper), '--repo-root', str(self.repo)]):
            with self.assertRaises(SystemExit):
                module.main()
        receipt = json.loads((self.snapshot_base() / 'receipt.json').read_text())
        self.assertIn('pending', receipt)
        self.run_installer()
        self.assertIn(b'Archify v1', (self.links()['codex_archify'] / 'SKILL.md').read_bytes())
        self.assertTrue((self.snapshot_base() / 'receipt.json').is_file())

    def test_interrupted_codex_copy_publish_retains_old_copy_and_retry_completes(self):
        self.run_installer()
        target = self.links()['codex_visual']
        old_bytes = (target / 'SKILL.md').read_bytes()
        self.set_pins('v2')
        helper = self.repo / 'scripts/visual-skills.py'
        spec = importlib.util.spec_from_file_location('visual_skills_copy_interrupt', helper)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        real_run = subprocess.run
        real_rename = os.rename
        def route_fake(command, *args, **kwargs):
            if isinstance(command, (list, tuple)) and command and command[0] == 'claude':
                command = ['/bin/bash', str(self.fakebin / 'claude-fake.sh'), *command[1:]]
            return real_run(command, *args, **kwargs)
        interrupted = False
        def rename_then_interrupt(source, destination, *args, **kwargs):
            nonlocal interrupted
            result = real_rename(source, destination, *args, **kwargs)
            if (not interrupted and Path(destination) == target
                    and Path(source).name.startswith('.codex-copy-')):
                interrupted = True
                raise SystemExit('synthetic process termination after generated copy publish')
            return result
        with mock.patch.dict(os.environ, self.env, clear=False), \
             mock.patch.object(subprocess, 'run', side_effect=route_fake), \
             mock.patch.object(module.os, 'rename', side_effect=rename_then_interrupt), \
             mock.patch.object(sys, 'argv', [str(helper), '--repo-root', str(self.repo)]):
            with self.assertRaises(SystemExit):
                module.main()
        self.assertTrue(interrupted)
        self.assertIn(b'Visual Explainer v2', (target / 'SKILL.md').read_bytes())
        retained = list(self.snapshot_base().glob('retained-*'))
        self.assertEqual(len(retained), 1)
        self.assertEqual((retained[0] / 'SKILL.md').read_bytes(), old_bytes)
        pending = json.loads((self.snapshot_base() / 'receipt.json').read_text())
        self.assertIn('pending', pending)
        self.assertEqual(Path(pending['pending']['copy']['retained']), retained[0])
        self.run_installer()
        final = json.loads((self.snapshot_base() / 'receipt.json').read_text())
        self.assertNotIn('pending', final)
        self.assertIn('Visual Explainer v2', (target / 'SKILL.md').read_text())
        self.assertTrue(retained[0].is_dir())
        plugin_rows = json.loads((self.cli_state / 'plugins.json').read_text())
        plugin = next(row for row in plugin_rows if row['id'] == 'visual-explainer@visual-explainer-marketplace')
        self.assertIn('Visual Explainer v2', Path(plugin['installPath'], 'SKILL.md').read_text())

    def test_modified_pending_copy_stage_is_preserved_and_refused_on_retry(self):
        self.run_installer()
        target = self.links()['codex_visual']
        self.set_pins('v2')
        helper = self.repo / 'scripts/visual-skills.py'
        spec = importlib.util.spec_from_file_location('visual_skills_modified_stage', helper)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        real_run = subprocess.run
        real_rename = os.rename
        def route_fake(command, *args, **kwargs):
            if isinstance(command, (list, tuple)) and command and command[0] == 'claude':
                command = ['/bin/bash', str(self.fakebin / 'claude-fake.sh'), *command[1:]]
            return real_run(command, *args, **kwargs)
        def interrupt_before_publish(source, destination, *args, **kwargs):
            if Path(destination) == target and Path(source).name.startswith('.codex-copy-'):
                raise SystemExit('synthetic termination with staged copy pending')
            return real_rename(source, destination, *args, **kwargs)
        with mock.patch.dict(os.environ, self.env, clear=False), \
             mock.patch.object(subprocess, 'run', side_effect=route_fake), \
             mock.patch.object(module.os, 'rename', side_effect=interrupt_before_publish), \
             mock.patch.object(sys, 'argv', [str(helper), '--repo-root', str(self.repo)]):
            with self.assertRaises(SystemExit):
                module.main()
        receipt_path = self.snapshot_base() / 'receipt.json'
        pending = json.loads(receipt_path.read_text())
        stage = Path(pending['pending']['copy']['stage'])
        self.assertTrue(stage.is_dir())
        staged_file = stage / 'SKILL.md'
        staged_file.chmod(0o644)
        staged_file.write_bytes(b'foreign change to staged copy\n')
        changed = staged_file.read_bytes()
        self.run_installer(success=False)
        self.assertEqual(staged_file.read_bytes(), changed)
        self.assertTrue(stage.is_dir())
        self.assertTrue((self.snapshot_base() / 'receipt.json').is_file())

    def test_native_install_cannot_replace_generated_copy_with_symlink(self):
        visual_source = self.snapshot_base() / 'visual-explainer' / self.pin_revision('visual-explainer', 'v1') / 'plugins/visual-explainer'
        target = self.links()['codex_visual']
        self.env['FAKE_CLAUDE_SWAP_CODEX_COPY'] = f'{target}|{visual_source}'
        result = self.run_installer(success=False)
        self.assertIn('physical generated', (result.stdout + result.stderr).lower())
        self.assertTrue(target.is_symlink())
        self.assertEqual(target.resolve(), visual_source.resolve())
        receipt = json.loads((self.snapshot_base() / 'receipt.json').read_text())
        self.assertIn('pending', receipt)

    def test_modified_generated_codex_copy_is_refused_without_overwrite(self):
        self.run_installer()
        self.cli_log.write_text('')
        target = self.links()['codex_visual']
        changed = target / 'SKILL.md'
        changed.chmod(0o644)
        changed.write_bytes(b'user modification\n')
        before = changed.read_bytes()
        self.run_installer('--check', success=False)
        self.run_installer(success=False)
        self.assertEqual(changed.read_bytes(), before)
        self.assertTrue(target.is_dir())
        self.assertFalse(target.is_symlink())
        self.assertEqual(self.mutation_calls(), [])

    def test_concurrent_foreign_marketplace_change_during_staging_is_preserved(self):
        self.env['FAKE_CLAUDE_MUTATE_MARKET_ON_LIST'] = '2'
        result = self.run_installer(success=False)
        self.assertIn('changed during staging', result.stderr)
        self.assertFalse(any(os.path.lexists(path) for path in self.links().values()))
        marketplaces = json.loads((self.cli_state / 'marketplaces.json').read_text())
        foreign = next(row for row in marketplaces if row['name'] == 'visual-explainer-marketplace')
        self.assertEqual(foreign['path'], '/foreign/concurrent-marketplace')
        self.assertFalse((self.snapshot_base() / 'receipt.json').exists())
        self.assertEqual(self.mutation_calls(), [])
        self.assert_protected_unchanged()

    def test_corrupt_snapshot_is_refused_without_replacing_active_links(self):
        self.run_installer()
        arch_link = self.links()['codex_archify']
        target = arch_link.resolve()
        changed_file = target / 'SKILL.md'
        changed_file.chmod(0o644)
        changed_file.write_text('# altered outside pin\n')
        mutated = changed_file.read_bytes()
        self.run_installer('--check', success=False)
        self.run_installer(success=False)
        self.assertEqual(changed_file.read_bytes(), mutated)
        self.assertTrue(arch_link.is_symlink())
        self.assertEqual(arch_link.resolve(), target)
        self.assert_protected_unchanged()

    def test_failed_fetch_during_upgrade_keeps_active_paths_and_registration(self):
        self.run_installer()
        plugin_before = (self.cli_state / 'plugins.json').read_bytes()
        self.set_pins('v2')
        manifest_path = self.repo / 'codex/subtraction-skills.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['visuals']['packages']['archify']['revision'] = '0' * 40
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
        self.run_installer(success=False)
        for name, link in self.links().items():
            if name == 'codex_visual':
                self.assertTrue(link.is_dir(), name)
                self.assertFalse(link.is_symlink(), name)
            else:
                self.assertTrue(link.is_symlink(), name)
        self.assertEqual((self.cli_state / 'plugins.json').read_bytes(), plugin_before)
        self.assertIn('v1', (self.links()['codex_archify'] / 'SKILL.md').read_text())
        self.assertTrue((self.snapshot_base() / 'archify' / self.pin_revision('archify', 'v1')).exists())
        self.assert_protected_unchanged()

    def test_claude_failure_reports_partial_progress_and_retains_recovery_state(self):
        self.run_installer()
        previous_plugins = (self.cli_state / 'plugins.json').read_bytes()
        self.set_pins('v2')
        self.env['FAKE_CLAUDE_FAIL_ON'] = 'install-once'
        result = self.run_installer(success=False)
        self.assertIn('synthetic plugin install failure', result.stderr)
        pending_path = self.snapshot_base() / 'receipt.json'
        self.assertTrue(pending_path.is_file())
        pending = json.loads(pending_path.read_text())
        self.assertTrue(pending.get('pending'), 'failed activation must retain a resumable pending record')
        self.assertTrue((self.snapshot_base() / 'archify' / self.pin_revision('archify', 'v2')).exists())
        self.assertIn('v2', (self.links()['codex_archify'] / 'SKILL.md').read_text())
        self.assertTrue((self.snapshot_base() / 'archify' / self.pin_revision('archify', 'v1')).exists())
        self.assert_protected_unchanged()
        self.assertTrue(any('marketplace' in line.lower() or 'install' in line.lower() for line in (result.stdout + result.stderr).splitlines()))
        # The one-shot failure has cleared; retry reconciles the persisted pending operation.
        self.run_installer()
        final = json.loads(pending_path.read_text())
        self.assertNotIn('pending', final)
        plugin_rows = json.loads((self.cli_state / 'plugins.json').read_text())
        self.assertIn('unrelated@other-marketplace', {row['id'] for row in plugin_rows})
        plugin = next(row for row in plugin_rows if row['id'] == 'visual-explainer@visual-explainer-marketplace')
        self.assertIn('Visual Explainer v2', Path(plugin['installPath'], 'SKILL.md').read_text())
        self.assertTrue(self.call_log())

    def test_pin_upgrade_switches_codex_and_claude_paths_and_keeps_old_snapshots(self):
        self.run_installer()
        old_codex = self.links()['codex_archify'].resolve()
        old_visual = self.links()['codex_visual']
        old_visual_bytes = (old_visual / 'SKILL.md').read_bytes()
        old_claude = self.links()['claude_archify'].resolve()
        old_plugin = Path(next(row for row in json.loads((self.cli_state / 'plugins.json').read_text()) if row['id'] == 'visual-explainer@visual-explainer-marketplace')['installPath'])
        self.set_pins('v2')
        self.run_installer()
        new_codex = self.links()['codex_archify'].resolve()
        new_visual = self.links()['codex_visual']
        new_claude = self.links()['claude_archify'].resolve()
        new_plugin = Path(next(row for row in json.loads((self.cli_state / 'plugins.json').read_text()) if row['id'] == 'visual-explainer@visual-explainer-marketplace')['installPath'])
        self.assertNotEqual(new_codex, old_codex)
        self.assertNotEqual(new_claude, old_claude)
        self.assertIn('v2', (new_codex / 'SKILL.md').read_text())
        self.assertTrue(new_visual.is_dir())
        self.assertFalse(new_visual.is_symlink())
        self.assertIn('Visual Explainer v2', (new_visual / 'SKILL.md').read_text())
        self.assertNotEqual((new_visual / 'SKILL.md').read_bytes(), old_visual_bytes)
        self.assertIn('v2', (new_claude / 'SKILL.md').read_text())
        self.assertIn('Visual Explainer v2', (new_plugin / 'SKILL.md').read_text())
        self.assertTrue(old_codex.exists())
        self.assertTrue(old_claude.exists())
        self.assertTrue(old_plugin.exists())
        self.assert_protected_unchanged()


if __name__ == '__main__':
    unittest.main()
