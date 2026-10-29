#!/usr/bin/env python3
"""Pinned operator-only fixture. No source, checksum, proxy or toolchain overrides."""
import argparse
import base64
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import time
import zipfile

HERE = Path(__file__).resolve().parent
MANIFEST_HASH = '93dd7cfcef1fda941cbc845019d06c301081508107b2145492dea846b249f852'
PATCH_HASH = '2726df0c443a1fc04f473700bff8ebeeec0301539ff0c843c615cbaa7ae508a8'
PINS = ('b0f84e5e341385e6601509ffc64cdcb2dffc9e13', 'a24856755dbfeb8785c0d63543e056cc404898be', 'f91333f29daf4ed657c48d017b5a56b49e547fcb')
TREE = '7b37eb85dcfe6e6c504e8ef53325a905cd8b310a'
ACCEPTED = 'c9cf50ce1b22f3148e400ce5a517b733c5af6be5'
URL = 'https://github.com/supernovae/SwarmOS.git'
TRACKER = 'internal/tracker/github_test.go'
INTEGRATION = 'internal/fork/org_read_credential_integration_test.go'
PATCH_PATHS = (TRACKER, INTEGRATION)
PREIMAGE = dict(base='b2b9089a06b6c891037cbb8f11645332a1b6c67b', stages=['0caf53f80764a35b0ff1b1608402449953f01e99', '0503b1d8fbce7138e82a7093aa9454fb5d658f81', '25c4f897d35cd0f61be58827b9f0e7799248dacb'], blob='a4675791d938880db7f3b0a07864e009f1a18394', sha256='5da7d0caed7aceb17b8fb99e8555041066fe819fa89495ac464c6c119da883bb')
MAX_JSON = 4 * 1024 * 1024
MAX_FILES = 150000
MAX_BYTES = 1024 * 1024 * 1024
GO_VERSION = 'go1.26.10'


class Refused(Exception):
    pass


def require(ok, reason):
    if not ok:
        raise Refused(reason)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def safe_path(path, directory=False):
    p = Path(os.path.abspath(path))
    for part in (p, *p.parents):
        require(not part.is_symlink(), 'symlink path refused')
    s = p.stat()
    require(stat.S_ISDIR(s.st_mode) if directory else stat.S_ISREG(s.st_mode), 'nonregular input')
    return p


def read_file(path, limit=MAX_JSON):
    p = safe_path(path)
    require(p.stat().st_size <= limit, 'input size limit')
    return p.read_bytes()


def executable(name):
    found = shutil.which(name)
    require(found is not None, 'required executable unavailable')
    return str(safe_path(Path(found).resolve()))


def disjoint(a, b):
    a, b = Path(a).resolve(), Path(b).resolve()
    require(a != b and a not in b.parents and b not in a.parents, 'overlapping paths')


def fresh(path, others=()):
    p = Path(os.path.abspath(path))
    safe_path(p.parent, True)
    for other in others:
        disjoint(p, other)
    p.mkdir(mode=0o700)  # Exclusive; never reuse an operator directory.
    return p


def remove_owned(root):
    # Caller retains the root created by fresh, never takes cleanup targets from JSON.
    root = Path(root)
    require(not root.is_symlink(), 'substituted owned root')
    if not root.exists():
        return
    for directory, dirs, files in os.walk(root, followlinks=False):
        os.chmod(directory, 0o700)
        for name in files:
            p = Path(directory) / name
            if not p.is_symlink():
                os.chmod(p, 0o600)
    shutil.rmtree(root)


def base_env(home):
    return {'PATH': '/usr/bin:/bin', 'HOME': str(home), 'XDG_CONFIG_HOME': str(home),
            'LANG': 'C', 'LC_ALL': 'C', 'TZ': 'UTC',
            'GIT_OPTIONAL_LOCKS': '0', 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
            'GIT_TERMINAL_PROMPT': '0', 'GIT_ASKPASS': '/bin/false',
            'GIT_AUTHOR_NAME': 'Fixture', 'GIT_AUTHOR_EMAIL': 'fixture@example.invalid',
            'GIT_COMMITTER_NAME': 'Fixture', 'GIT_COMMITTER_EMAIL': 'fixture@example.invalid',
            'GIT_AUTHOR_DATE': '2000-01-01T00:00:00Z', 'GIT_COMMITTER_DATE': '2000-01-01T00:00:00Z'}


def run_bounded(argv, cwd, env, deadline, limit=8192, allowed=(0,)):
    require(time.monotonic() < deadline, 'phase deadline exceeded')
    proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    sel = selectors.DefaultSelector()
    output = [bytearray(), bytearray()]
    for i, stream in enumerate((proc.stdout, proc.stderr)):
        os.set_blocking(stream.fileno(), False)
        sel.register(stream, selectors.EVENT_READ, i)
    try:
        while sel.get_map():
            remaining = deadline - time.monotonic()
            require(remaining > 0, 'subprocess timeout')
            for key, _ in sel.select(min(remaining, 0.1)):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    sel.unregister(key.fileobj)
                else:
                    output[key.data].extend(chunk)
                    require(len(output[0]) <= limit and len(output[1]) <= 8192,
                            'subprocess output limit')
        proc.wait(timeout=max(0.001, deadline - time.monotonic()))
        require(proc.returncode in allowed, 'subprocess failed (diagnostics suppressed)')
        return proc.returncode, bytes(output[0])
    except subprocess.TimeoutExpired:
        raise Refused('subprocess timeout') from None
    finally:
        # Also kill descendants after a successful leader exit, including closed-pipe children.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        sel.close()
        proc.stdout.close()
        proc.stderr.close()


class Git:
    def __init__(self, home, deadline, network=False):
        self.exe = executable('git')
        self.env = base_env(home)
        self.deadline = deadline
        self.flags = ['-c', 'credential.helper=', '-c', 'credential.interactive=false',
                      '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false',
                      '-c', 'tag.gpgsign=false', '-c', 'core.attributesFile=/dev/null',
                      '-c', 'core.autocrlf=false', '-c', 'core.abbrev=40',
                      '-c', 'core.fsmonitor=false', '-c', 'core.untrackedCache=false',
                      '-c', 'merge.conflictStyle=merge', '-c', 'rerere.enabled=false',
                      '-c', 'merge.autoStash=false', '-c', 'protocol.allow=never',
                      '-c', 'protocol.https.allow=' + ('always' if network else 'never'),
                      '-c', 'protocol.file.allow=' + ('never' if network else 'always'),
                      '-c', 'http.followRedirects=false', '-c', 'http.extraHeader=',
                      '-c', 'http.sslVerify=true', '-c', 'http.lowSpeedLimit=1024',
                      '-c', 'http.lowSpeedTime=30']

    def run(self, repo, *args, allowed=(0,), limit=MAX_JSON):
        return run_bounded([self.exe, *self.flags, *args], repo, self.env, self.deadline,
                           limit, allowed)

    def out(self, repo, *args):
        return self.run(repo, *args)[1].decode().strip()


def load_manifest():
    raw = read_file(HERE / 'candidate-manifest.json')
    patch = read_file(HERE / 'combined-candidate.patch')
    require(digest(raw) == MANIFEST_HASH and digest(patch) == PATCH_HASH, 'immutable seed mismatch')
    m = json.loads(raw)
    require(set(m) == {'base', 'pr298_head', 'pr313_head', 'accepted_test_head', 'expected_tree',
                       'patch_sha256', 'product_repository', 'scope'}, 'manifest fields')
    require(tuple(m[k] for k in ('base', 'pr298_head', 'pr313_head')) == PINS and
            m['expected_tree'] == TREE and m['accepted_test_head'] == ACCEPTED and
            m['product_repository'] == 'supernovae/SwarmOS' and m['patch_sha256'] == PATCH_HASH,
            'manifest values')
    return m, patch


def verify_objects(git, repo, pins):
    safe_path(repo, True)
    require(not (Path(repo) / 'objects/info/alternates').exists() and
            not (Path(repo) / '.git/objects/info/alternates').exists(), 'object alternates refused')
    require(not git.out(repo, 'rev-parse', '--is-shallow-repository') == 'true', 'shallow source')
    for pin in pins:
        require(re.fullmatch('[0-9a-f]{40}', pin), 'invalid pin')
        require(git.out(repo, 'cat-file', '-t', pin) == 'commit', 'pin is not commit')
        require(git.out(repo, 'rev-parse', pin) == pin, 'pin mismatch')
        git.run(repo, 'rev-list', '--objects', '--missing=error', pin)
    git.run(repo, 'fsck', '--full', '--no-reflogs', '--no-dangling')


def prepare_sources(output):
    load_manifest()
    root = fresh(output, (HERE,))
    try:
        git = Git(root, time.monotonic() + 90, True)
        git.run(root, 'init', '--bare', '.')
        git.deadline = min(git.deadline, time.monotonic() + 60)
        git.run(root, 'fetch', '--no-tags', '--no-recurse-submodules', '--no-write-fetch-head', URL, *PINS)
        verify_objects(git, root, PINS)
        return root
    except BaseException:
        remove_owned(root)
        raise


def tracked_snapshot(git, repo):
    entries = git.run(repo, 'ls-files', '-z')[1].split(b'\0')
    result = {}
    for name in entries:
        if name:
            path = os.fsdecode(name)
            p = Path(repo) / path
            require(not p.is_symlink(), 'tracked symlink refused')
            result[path] = digest(read_file(p, MAX_BYTES))
    return result


def verify_index_worktree(git, candidate, exclude=()):
    # Compare bytes and executable modes directly: chmod can make Git's stat
    # cache report a clean file dirty until an index refresh. Never refresh or
    # rewrite the admitted index just to clear that timestamp ambiguity.
    entries = git.run(candidate, 'ls-files', '--stage', '-z')[1].split(b'\0')
    for entry in entries:
        if not entry:
            continue
        metadata, filename = entry.split(b'\t', 1)
        mode, oid, stage = metadata.decode().split()
        name = os.fsdecode(filename)
        if name in exclude:
            continue
        require(stage == '0' and mode in ('100644', '100755'), 'unsupported index entry')
        path = Path(candidate) / name
        data = read_file(path, MAX_BYTES)
        actual = hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()
        require(actual == oid, 'index/worktree content drift')
        executable_mode = bool(path.stat().st_mode & 0o111)
        require(executable_mode == (mode == '100755'), 'index/worktree mode drift')


def source_state(git, candidate, expected_tree):
    safe_path(Path(candidate) / '.git', True)
    require(git.out(candidate, 'write-tree') == expected_tree, 'candidate tree mismatch')
    require(not git.out(candidate, 'ls-files', '-u'), 'unmerged candidate')
    verify_index_worktree(git, candidate)
    require(not git.out(candidate, 'ls-files', '--others', '--exclude-standard'), 'untracked candidate')
    require(not git.out(candidate, 'ls-files', '--others', '--ignored', '--exclude-standard'), 'ignored candidate')
    return {'tree': expected_tree, 'files': tracked_snapshot(git, candidate),
            'index': digest(read_file(Path(candidate) / '.git/index', MAX_JSON))}


def check_patch(patch, paths=PATCH_PATHS):
    text = patch.decode('utf-8')
    headers = re.findall(r'^diff --git a/(\S+) b/(\S+)$', text, re.M)
    require(len(headers) == len(paths) and {a for a, b in headers if a == b} == set(paths), 'patch scope')
    require(not re.search(r'^(GIT binary patch|Binary files|rename |copy |old mode|new mode|deleted file)', text, re.M), 'patch kind')
    for mode in re.findall(r'^new file mode (\d+)$', text, re.M):
        require(mode == '100644', 'patch mode')
    require(all(p.startswith('+++ b/') or p == '+++ /dev/null' for p in text.splitlines() if p.startswith('+++ ')), 'patch paths')


def assemble_candidate(source, output, *, pins=PINS, tree=TREE, preimage=PREIMAGE,
                       patch=None, paths=PATCH_PATHS, accepted=ACCEPTED):
    # Keyword expectations are for synthetic in-process tests only; CLI exposes no overrides.
    if patch is None:
        _, patch = load_manifest()
    source = safe_path(source, True)
    check_patch(patch, paths)
    root = fresh(output, (source, HERE))
    try:
        git = Git(root, time.monotonic() + 60)
        verify_objects(git, source, pins)
        git.run(root, 'init', '.')
        git.run(root, 'fetch', '--no-tags', '--no-recurse-submodules', '--no-write-fetch-head', str(source), *pins)
        git.run(root, 'checkout', '--detach', pins[0])
        # No repository-controlled merge drivers or marker-size attributes.
        for pin in pins:
            listing = git.out(root, 'ls-tree', '-r', '--name-only', pin)
            for name in listing.splitlines():
                if name.endswith('.gitattributes'):
                    attributes = git.out(root, 'show', f'{pin}:{name}')
                    require(not re.search(r'\b(?:merge|conflict-marker-size)(?:=|\s|$)', attributes), 'merge attributes refused')
        merge = ['merge', '--no-ff', '--no-edit', '--no-gpg-sign', '--strategy=ort', '-Xno-renames']
        git.run(root, *merge, pins[1])
        require(not git.out(root, 'status', '--porcelain'), 'first merge drift')
        require(git.out(root, 'merge-base', 'HEAD', pins[2]) == preimage['base'], 'merge-base mismatch')
        code, _ = git.run(root, *merge, pins[2], allowed=(0, 1, 2))
        require(code == 1, 'second merge must conflict')
        stages = git.out(root, 'ls-files', '-u').splitlines()
        require(stages == [f'100644 {oid} {i}\t{paths[0]}' for i, oid in enumerate(preimage['stages'], 1)], 'conflict stages mismatch')
        conflict = read_file(root / paths[0])
        require(git.out(root, 'hash-object', paths[0]) == preimage['blob'] and
                digest(conflict) == preimage['sha256'], 'conflict preimage mismatch')
        require([line for line in conflict.decode().splitlines() if re.match(r'^(<<<<<<<|=======|>>>>>>>)', line)] ==
                ['<<<<<<< HEAD', '=======', '>>>>>>> ' + pins[2]], 'conflict markers mismatch')
        before = tracked_snapshot(git, root)
        # All automatically merged nonconflict entries must agree with index.
        verify_index_worktree(git, root, (paths[0],))
        patchfile = root.parent / (root.name + '.patch-tmp')
        fd = os.open(patchfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(patch)
            git.run(root, 'apply', '--check', str(patchfile))
            git.run(root, 'apply', str(patchfile))
        finally:
            patchfile.unlink()
        after_patch = tracked_snapshot(git, root)
        for name in before:
            if name not in paths:
                require(before[name] == after_patch[name], 'nonconflict drift')
        require(set(git.out(root, 'ls-files', '--others', '--exclude-standard').splitlines()) == {paths[1]}, 'unexpected untracked patch output')
        for path in paths:
            require(not re.search(rb'^(<<<<<<<|=======|>>>>>>>)', read_file(root / path), re.M), 'remaining markers')
        git.run(root, 'add', '--', *paths)
        source_state(git, root, tree)
        # Comparison is supplemental; never fetch or use the accepted head as source.
        if git.run(source, 'cat-file', '-e', accepted, allowed=(0, 1, 128))[0] == 0:
            require(git.out(source, 'rev-parse', accepted+'^{tree}') == tree, 'accepted tree differs')
        return root
    except BaseException:
        remove_owned(root)
        raise


def h1(files):
    outer = hashlib.sha256()
    for name, data in sorted(files.items()):
        require('\n' not in name, 'newline in hashed filename')
        outer.update((digest(data) + '  ' + name + '\n').encode())
    return 'h1:' + base64.b64encode(outer.digest()).decode()


def unescape(value):
    require(re.fullmatch(r'(?:[^!A-Z]|![a-z])+', value) is not None, 'module escaping')
    return re.sub('!([a-z])', lambda m: m[1].upper(), value)


def escape(value):
    return re.sub('[A-Z]', lambda m: '!' + m[0].lower(), value)


def sums_from(data):
    sums = {}
    for line in data.decode().splitlines():
        fields = line.split()
        require(len(fields) == 3 and re.fullmatch(r'h1:[A-Za-z0-9+/]{43}=', fields[2]), 'approved sum syntax')
        key = tuple(fields[:2])
        require(key not in sums or sums[key] == fields[2], 'conflicting checksum')
        sums[key] = fields[2]
    return sums


def file_inventory(root, deadline):
    root = safe_path(root, True)
    result, total = {}, 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs:
            require(not (Path(directory) / name).is_symlink(), 'cache symlink')
        for name in files:
            require(time.monotonic() < deadline, 'inventory deadline')
            path = Path(directory) / name
            data = read_file(path, MAX_BYTES)
            total += len(data)
            require(total <= MAX_BYTES and len(result) < MAX_FILES, 'cache quota')
            result[path.relative_to(root).as_posix()] = digest(data)
    return result


def hash_cache_inventory(cache, approved, modules, deadline):
    """Validate every downloaded metadata/source byte, including metadata-only versions."""
    cache = safe_path(cache, True)
    inventory = file_inventory(cache, deadline)
    mods, zips, extracted, infos, ziphashes = {}, {}, {}, {}, {}
    for name in inventory:
        require(time.monotonic() < deadline, 'inventory deadline')
        if name.startswith('cache/download/sumdb/'):
            continue  # Sumdb transport records are evidence, never checksum authority.
        if name.startswith('cache/download/'):
            rel = name[len('cache/download/'):]
            if rel.endswith('/@v/list'):
                require(all(re.fullmatch(r'v[0-9A-Za-z.+-]+', x) for x in (cache / name).read_text().splitlines()), 'cache version list')
                continue
            m = re.fullmatch(r'(.+)/@v/([^/]+)\.(mod|info|zip|ziphash|lock)', rel)
            require(m is not None, 'unknown cache record')
            key = (unescape(m[1]), unescape(m[2]))
            kind = m[3]
            data = read_file(cache / name, MAX_BYTES)
            if kind == 'mod':
                require(approved.get((key[0], key[1]+'/go.mod')) == h1({'go.mod': data}), 'unapproved module metadata')
                mods[key] = name
            elif kind == 'zip':
                prefix = key[0] + '@' + key[1] + '/'
                files = {}
                total = 0
                with zipfile.ZipFile(cache / name) as archive:
                    require(len(archive.infolist()) <= MAX_FILES, 'zip entry quota')
                    for entry in archive.infolist():
                        require(entry.filename.startswith(prefix), 'zip module prefix')
                        tail = entry.filename[len(prefix):]
                        require(tail and all(p not in ('', '.', '..') for p in tail.split('/')) and
                                '\\' not in tail and '\n' not in tail and not entry.is_dir(), 'unsafe zip entry')
                        mode = entry.external_attr >> 16
                        require(stat.S_IFMT(mode) in (0, stat.S_IFREG), 'special zip entry')
                        require(entry.filename not in files, 'duplicate zip entry')
                        total += entry.file_size
                        require(total <= MAX_BYTES and time.monotonic() < deadline, 'zip quota/deadline')
                        files[entry.filename] = archive.read(entry)
                value = h1(files)
                require(approved.get(key) == value, 'unapproved module content')
                zips[key] = value
            elif kind == 'info':
                info = json.loads(data)
                require(isinstance(info, dict) and info.get('Version') == key[1] and isinstance(info.get('Time'), str), 'module info version')
                infos[key] = name
            elif kind == 'ziphash':
                ziphashes[key] = data.decode().strip()
            else:
                require(not data, 'nonempty lock record')
        else:
            # Module roots end at the first @; later @ characters belong to files.
            m = re.fullmatch(r'([^@]+)@([^/@]+)/(.+)', name)
            require(m is not None, 'unknown extracted cache path')
            key = (unescape(m[1]), unescape(m[2]))
            extracted.setdefault(key, {})[key[0]+'@'+key[1]+'/'+m[3]] = read_file(cache / name, MAX_BYTES)
    for key in set(zips) | set(extracted) | set(ziphashes):
        require(key in mods and key in zips and key in extracted and key in ziphashes, 'incomplete source cache')
        require(zips[key] == h1(extracted[key]) == ziphashes[key], 'zip/extracted/hash mismatch')
    for key in modules:
        require(tuple(key) in mods and tuple(key) in zips and tuple(key) in infos, 'package module cache incomplete')
    return inventory


def json_records(data):
    require(len(data) <= MAX_JSON, 'JSON limit')
    decoder, offset, records = json.JSONDecoder(), 0, []
    text = data.decode()
    while offset < len(text):
        if text[offset].isspace():
            offset += 1
            continue
        value, offset = decoder.raw_decode(text, offset)
        require(isinstance(value, dict), 'JSON record type')
        records.append(value)
    return records


def tool_roots(data):
    records = json_records(data)
    require(len(records) == 1, 'module JSON count')
    doc = records[0]
    require(not doc.get('Replace') and doc.get('Go') == '1.26.10' and
            doc.get('Toolchain') in (None, '', 'go1.26.10') and
            not any(doc.get(k) for k in ('Godebug', 'Exclude', 'Retract', 'Ignore')), 'module directives')
    tools = doc.get('Tool', []) or []
    require(isinstance(tools, list), 'tools type')
    paths = [x.get('Path') for x in tools if isinstance(x, dict)]
    require(len(paths) == len(tools) and len(paths) == len(set(paths)) and
            all(isinstance(p, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._~+/-]*', p) and
                '.' in p.split('/')[0] and all(x not in ('', '.', '..') for x in p.split('/')) for p in paths), 'tool import paths')
    return ['./cmd/swarm', *sorted(paths)]


def load_package_closure(data, candidate, cache, roots):
    records = json_records(data)
    packages, modules = {}, set()
    main_root = None
    for p in records:
        name = p.get('ImportPath')
        require(isinstance(name, str) and name not in packages and not any(p.get(x) for x in ('Error', 'DepsErrors', 'Incomplete')), 'invalid package closure')
        imports = p.get('Imports', [])
        require(isinstance(imports, list) and all(isinstance(x, str) for x in imports), 'package imports')
        module = p.get('Module')
        selected = None
        if module:
            require(not module.get('Replace') and not module.get('Error'), 'replaced/error module')
            if module.get('Main'):
                require(Path(module['Dir']).resolve() == candidate, 'main module location')
                require(Path(p['Dir']).resolve().is_relative_to(candidate), 'main package location')
                main_root = module['Path'] + '/cmd/swarm' if main_root is None else main_root
            else:
                selected = (module.get('Path'), module.get('Version'))
                require(all(isinstance(x, str) and x for x in selected), 'module identity')
                expected = cache / (escape(selected[0]) + '@' + escape(selected[1]))
                require(Path(module['Dir']).resolve() == expected and Path(p['Dir']).resolve().is_relative_to(expected), 'external module location')
                modules.add(selected)
        else:
            require(p.get('Standard') is True, 'unowned package')
        packages[name] = {'imports': sorted(imports), 'module': selected}
    require(main_root is not None and main_root in packages and all(r in packages for r in roots[1:]), 'missing coordinator/tool roots')
    for record in packages.values():
        require(all(i in packages or i == 'C' for i in record['imports']), 'unresolved import')
    # Canonical JSON representation makes descriptor comparisons type-stable.
    return json.loads(json.dumps({'packages': dict(sorted(packages.items())), 'modules': sorted(modules)}))


def go_env(root, online):
    env = base_env(root / 'home')
    env.update({'GOENV': 'off', 'GOWORK': 'off', 'GOFLAGS': '', 'GOTOOLCHAIN': 'local',
                'GOPROXY': 'https://proxy.golang.org' if online else 'off',
                'GOSUMDB': 'sum.golang.org' if online else 'off', 'GOVCS': '*:off',
                'GOAUTH': 'off', 'GOTELEMETRY': 'off',
                'GOPRIVATE': '', 'GONOPROXY': 'none', 'GONOSUMDB': 'none',
                'GOPATH': str(root / 'gopath'), 'GOMODCACHE': str(root / 'modules'),
                'GOCACHE': str(root / 'build-cache'), 'TMPDIR': str(root / 'tmp'),
                'GOOS': 'linux', 'GOARCH': 'amd64', 'CGO_ENABLED': '0'})
    return env


@contextlib.contextmanager
def nonwritable(candidate):
    modes = []
    try:
        for directory, dirs, files in os.walk(candidate, followlinks=False):
            dirs[:] = [d for d in dirs if d != '.git']
            for path in [Path(directory), *(Path(directory)/f for f in files)]:
                safe_path(path, path.is_dir())
                mode = stat.S_IMODE(path.stat().st_mode)
                modes.append((path, mode))
                path.chmod(mode & ~0o222)
        yield
    finally:
        for path, mode in reversed(modes):
            if path.exists() and not path.is_symlink():
                path.chmod(mode)


class GoSession:
    def __init__(self, candidate, root, tree, deadline, go=None):
        self.candidate, self.root, self.tree = candidate, root, tree
        self.deadline = deadline
        self.git = Git(root / 'home', deadline)
        self.go = go or executable('go')
        self.state = source_state(self.git, candidate, tree)
        self.approved_mod = read_file(candidate / 'go.mod')
        self.approved_sum = read_file(candidate / 'go.sum')
        self.git_metadata = file_inventory(candidate / '.git', deadline)
        self.cache_check = None
        self.sums = sums_from(self.approved_sum)

    def check(self):
        require(file_inventory(self.candidate / '.git', self.deadline) == self.git_metadata, 'Git metadata drift')
        require(read_file(self.candidate/'go.mod') == self.approved_mod and
                read_file(self.candidate/'go.sum') == self.approved_sum, 'module bytes changed')
        require(source_state(self.git, self.candidate, self.tree) == self.state, 'source drift')

    def run(self, args, online, limit=MAX_JSON):
        self.check()
        try:
            return run_bounded([self.go, *args], self.candidate, go_env(self.root, online), self.deadline, limit)[1]
        finally:
            self.check()  # Check even failed commands, timeout and denied writes.
            if self.cache_check is not None:
                self.cache_check()

    def version(self):
        require(self.run(['version'], False).decode().strip() == 'go version go1.26.10 linux/amd64', 'wrong local Go version')

    def closure(self, roots, online):
        return load_package_closure(self.run(['list', '-mod=readonly', '-deps', '-json', *roots], online),
                                    self.candidate, self.root/'modules', roots)

    def inventory(self, closure):
        return hash_cache_inventory(self.root/'modules', self.sums, closure['modules'], self.deadline)


def atomic_json(path, value):
    tmp = path.with_suffix('.tmp')
    with tmp.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, sort_keys=True, separators=(',', ':'))
    tmp.replace(path)


def prepare_dependencies(candidate, output, *, tree=TREE):
    candidate = safe_path(candidate, True)
    root = fresh(output, (candidate, HERE))
    try:
        for name in ('home', 'gopath', 'modules', 'build-cache', 'tmp'):
            (root/name).mkdir(mode=0o700)
        session = GoSession(candidate, root, tree, time.monotonic()+180)
        with nonwritable(candidate):
            session.version()
            roots = tool_roots(session.run(['mod', 'edit', '-json'], False))
            closure = session.closure(roots, True)
            inventory = session.inventory(closure)
            require(session.closure(roots, False) == closure, 'offline closure drift')
            require(session.inventory(closure) == inventory, 'offline cache drift')
            session.check()
            descriptor = {'schema': 1, 'candidate': str(candidate), 'tree': tree,
                          'mod': digest(session.approved_mod), 'sum': digest(session.approved_sum),
                          'go': session.go, 'version': GO_VERSION, 'root': str(root),
                          'root_identity': [root.stat().st_dev, root.stat().st_ino],
                          'cache_paths': {k: str(root/k) for k in ('modules', 'build-cache', 'home', 'gopath', 'tmp')},
                          'target': ['linux', 'amd64', '0'], 'roots': roots, 'closure': closure,
                          'inventory': inventory, 'git_metadata': session.git_metadata}
            atomic_json(root/'dependencies.json', descriptor)
        return root/'dependencies.json'
    except BaseException:
        remove_owned(root)
        raise


def build_candidate(candidate, dependencies, output, *, tree=TREE):
    candidate = safe_path(candidate, True)
    descriptor_path = safe_path(dependencies)
    d = json.loads(read_file(descriptor_path))
    root = descriptor_path.parent
    # Never delete a cache root supplied in a descriptor. Only remove the caller-owned binary root.
    expected_fields = {'schema','candidate','tree','mod','sum','go','version','root','root_identity','cache_paths','target','roots','closure','inventory','git_metadata'}
    require(set(d) == expected_fields and d['schema'] == 1 and d['root'] == str(root) and
            descriptor_path.name == 'dependencies.json' and
            d['root_identity'] == [root.stat().st_dev, root.stat().st_ino] and
            d['candidate'] == str(candidate) and d['tree'] == tree and
            d['target'] == ['linux','amd64','0'] and d['version'] == GO_VERSION, 'descriptor identity')
    require(d['cache_paths'] == {k: str(root/k) for k in ('modules','build-cache','home','gopath','tmp')}, 'descriptor cache paths')
    for k in d['cache_paths']:
        safe_path(root/k, True)
    disjoint(candidate, root)
    require(d['go'] == executable('go'), 'substituted Go executable')
    out = fresh(output, (candidate, root, HERE))
    descriptor_bytes = read_file(descriptor_path)
    try:
        session = GoSession(candidate, root, tree, time.monotonic()+180, d['go'])
        require(d['git_metadata'] == session.git_metadata, 'stale Git descriptor')
        require(d['mod'] == digest(session.approved_mod) and d['sum'] == digest(session.approved_sum), 'stale module descriptor')
        def check_cache():
            require(read_file(descriptor_path) == descriptor_bytes, 'descriptor changed')
            require(session.inventory(d['closure']) == d['inventory'], 'cache inventory drift')
        session.cache_check = check_cache
        with nonwritable(candidate):
            check_cache()
            session.version()
            check_cache()
            roots = tool_roots(session.run(['mod', 'edit', '-json'], False))
            require(roots == d['roots'], 'tool roots drift')
            check_cache()
            require(session.closure(roots, False) == d['closure'], 'build closure drift')
            check_cache()
            try:
                session.run(['build', '-mod=readonly', '-o', str(out/'swarm.tmp'), './cmd/swarm'], False, 8192)
            finally:
                check_cache()
            safe_path(out/'swarm.tmp')
            session.check()
            (out/'swarm.tmp').replace(out/'swarm')
        return out/'swarm'
    except BaseException:
        descriptor_path.unlink(missing_ok=True)
        remove_owned(out)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('prepare-sources')
    p.add_argument('--output', required=True)
    p = sub.add_parser('assemble')
    p.add_argument('--source-repo', required=True)
    p.add_argument('--output', required=True)
    p = sub.add_parser('prepare-dependencies')
    p.add_argument('--candidate', required=True)
    p.add_argument('--output', required=True)
    p = sub.add_parser('build')
    p.add_argument('--candidate', required=True)
    p.add_argument('--dependencies', required=True)
    p.add_argument('--output', required=True)
    args = parser.parse_args()
    try:
        load_manifest()
        if args.command == 'prepare-sources':
            result = prepare_sources(args.output)
        elif args.command == 'assemble':
            result = assemble_candidate(args.source_repo, args.output)
        elif args.command == 'prepare-dependencies':
            result = prepare_dependencies(args.candidate, args.output)
        else:
            result = build_candidate(args.candidate, args.dependencies, args.output)
        print(result)
    except (Refused, OSError, ValueError, zipfile.BadZipFile) as exc:
        # Never expose child output, URLs, paths or environments in failure diagnostics.
        print('Fixture refused: ' + (str(exc) if isinstance(exc, Refused) else type(exc).__name__), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
