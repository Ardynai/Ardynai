#!/usr/bin/env python3
"""Public-only profile updater. No token is read or sent. Python 3.10+."""
import argparse
import json
import os
from pathlib import Path
import re
import tempfile
import unicodedata
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler

USER = 'Ardynai'
API = 'https://api.github.com'
# Owner-requested display exclusions; fork counts and following remain unchanged.
EXCLUDED_PUBLIC_PROJECTS = frozenset({'daimon-memory', 'dev-plat-agenthub'})
BLOCKS = ('PUBLIC-PROJECTS', 'PUBLIC-FORKS', 'PUBLIC-FOLLOWING')

class UnsafeResponse(ValueError):
    pass

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise UnsafeResponse('API redirect refused')


def fetch(path):
    # No ambient proxy or authentication handlers, no credentials or API token.
    request = Request(API + path, headers={
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        'User-Agent': 'Ardynai-public-profile-updater',
    })
    with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=30) as response:
        if response.status != 200:
            raise UnsafeResponse('Unexpected API status')
        payload = response.read(8_000_001)
        if len(payload) > 8_000_000:
            raise UnsafeResponse('Response too large')
        return json.loads(payload)


def pages(endpoint, get=fetch):
    result = []
    # Numbered pagination avoids following untrusted Link URLs and search's 1000 limit.
    for page in range(1, 1001):
        separator = '&' if '?' in endpoint else '?'
        batch = get(f'{endpoint}{separator}per_page=100&page={page}')
        if not isinstance(batch, list) or len(batch) > 100:
            raise UnsafeResponse('Malformed page')
        result.extend(batch)
        if len(batch) < 100:
            return result
    raise UnsafeResponse('Pagination safety bound reached; refusing partial result')


def safe_text(text):
    if not isinstance(text, str):
        raise UnsafeResponse('Invalid text')
    text = ' '.join(''.join(c for c in text if not unicodedata.category(c).startswith('C') or c in '\n\r\t').split())[:240]
    # Escape all ASCII punctuation as entities: no HTML, Markdown links/images,
    # comments, mentions, or syntax can be injected by a repository description.
    return ''.join(f'&#{ord(c)};' if not c.isalnum() and c != ' ' else c for c in text)


def collect(get=fetch):
    repos = pages(f'/users/{USER}/repos?type=owner&sort=full_name&direction=asc', get)
    profile = get(f'/users/{USER}')
    if not isinstance(profile, dict) or profile.get('login', '').lower() != USER.lower() or type(profile.get('following')) is not int or profile['following'] < 0:
        raise UnsafeResponse('Invalid public profile following count')
    following_count = profile['following']
    following = pages(f'/users/{USER}/following', get)
    names = set()
    projects = []
    forks = 0
    ids = set()
    for repo in repos:
        if not isinstance(repo, dict) or type(repo.get('private')) is not bool or type(repo.get('fork')) is not bool:
            raise UnsafeResponse('Invalid repository record')
        # Defense in depth: never render private records, even in test fixtures.
        if repo['private']:
            continue
        name = repo.get('name', '')
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', name):
            raise UnsafeResponse('Invalid repository name')
        if not isinstance(repo.get('owner'), dict) or repo['owner'].get('login', '').lower() != USER.lower():
            raise UnsafeResponse('Unexpected repository owner')
        if name.lower() in names:
            raise UnsafeResponse('Duplicate repository across pages; retry later')
        names.add(name.lower())
        if repo['fork']:
            forks += 1
        elif name.lower() != USER.lower() and name.lower() not in EXCLUDED_PUBLIC_PROJECTS:
            description = repo.get('description')
            if description is not None and not isinstance(description, str):
                raise UnsafeResponse('Invalid description')
            projects.append((name, description or ''))
    logins = []
    for person in following:
        if not isinstance(person, dict):
            raise UnsafeResponse('Invalid following record')
        login = person.get('login', '')
        if not isinstance(login, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,38}', login):
            raise UnsafeResponse('Invalid login')
        if login.lower() in ids:
            raise UnsafeResponse('Duplicate following record; retry later')
        ids.add(login.lower())
        logins.append(login)
    projects.sort(key=lambda p: (p[0].lower(), p[0]))
    logins.sort(key=lambda s: (s.lower(), s))
    project_lines = [f'- [{safe_text(n)}](https://github.com/{USER}/{n})' + (f' · {safe_text(d)}' if d else '') for n, d in projects]
    following_lines = [f'- [{safe_text(n)}](https://github.com/{n})' for n in logins[:12]]
    return {
        'PUBLIC-PROJECTS': f'### Public projects ({len(projects)})\n\n' + ('\n'.join(project_lines) or 'No public original repositories yet.'),
        'PUBLIC-FORKS': f'[**{forks:,} public forks**](https://github.com/{USER}?tab=repositories&type=fork) · Explore my forked repositories.',
        'PUBLIC-FOLLOWING': f'### Following ({following_count})\n\n[View who I follow](https://github.com/{USER}?tab=following)\n\nFrom my following list:\n\n' + ('\n'.join(following_lines) or 'No public following entries.'),
    }


def replace_blocks(original, content):
    spans = []
    for name in BLOCKS:
        start, end = f'<!-- {name}:START -->'.encode(), f'<!-- {name}:END -->'.encode()
        if original.count(start) != 1 or original.count(end) != 1:
            raise UnsafeResponse(f'Expected exactly one marker pair: {name}')
        a, b = original.index(start) + len(start), original.index(end)
        if b < a:
            raise UnsafeResponse('Reversed markers')
        spans.append((a, b, ('\n' + content[name] + '\n').encode('utf-8')))
    spans.sort()
    if any(spans[i][1] >= spans[i + 1][0] for i in range(len(spans) - 1)):
        raise UnsafeResponse('Overlapping markers')
    for a, b, value in reversed(spans):
        original = original[:a] + value + original[b:]
    return original


def update_many(paths, get=fetch):
    paths = [Path(path) for path in paths]
    if not paths or len({str(path.resolve()) for path in paths}) != len(paths):
        raise UnsafeResponse('Expected distinct README paths')
    originals = {}
    for path in paths:
        if path.is_symlink() or not path.is_file():
            raise UnsafeResponse('README must be a regular existing file')
        originals[path] = path.read_bytes()
        # Validate every file and marker before fetching or writing anything.
        replace_blocks(originals[path], dict.fromkeys(BLOCKS, ''))
    content = collect(get)
    results = {path: replace_blocks(original, content) for path, original in originals.items()}
    changed = [path for path in paths if results[path] != originals[path]]
    staged = {}
    backups = {}
    replaced = []

    def stage(path, payload):
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.profile-', delete=False) as f:
            temporary = f.name
            try:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
                os.chmod(temporary, path.stat().st_mode & 0o777)
            except BaseException:
                os.unlink(temporary)
                raise
        return temporary

    try:
        for path in changed:
            staged[path] = stage(path, results[path])
            backups[path] = stage(path, originals[path])
        if any(path.is_symlink() or path.read_bytes() != originals[path] for path in paths):
            raise UnsafeResponse('README changed during update; refusing overwrite')
        for path in changed:
            os.replace(staged[path], path)
            replaced.append(path)
    except BaseException:
        # Filesystem replacement is atomic per file, not across files. Restore any
        # replaced file on a caught failure. Workflow never commits a failed run.
        for path in reversed(replaced):
            os.replace(backups[path], path)
        raise
    finally:
        for temporary in list(staged.values()) + list(backups.values()):
            if os.path.exists(temporary):
                os.unlink(temporary)
    return bool(changed)


def update(path, get=fetch):
    return update_many([path], get)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--readme', type=Path, action='append', help='Repeat for each README; defaults to README.md and README-static.md')
    args = parser.parse_args()
    try:
        print('Updated public profile sections.' if update_many(args.readme or [Path('README.md'), Path('README-static.md')]) else 'Public profile sections unchanged.')
    except Exception as error:
        # Do not echo untrusted response bodies or credentials into logs.
        raise SystemExit(f'Update aborted ({type(error).__name__}); no changes should be committed.')
