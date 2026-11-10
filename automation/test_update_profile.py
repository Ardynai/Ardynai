import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch
import update_profile as updater


def repo(name, fork=False, private=False, description='A project'):
    return dict(name=name, fork=fork, private=private, owner={'login': 'Ardynai'}, description=description)


def fixture(repos=(), people=()):
    def get(path):
        if path == '/users/Ardynai':
            return {'login':'Ardynai', 'following':len(people)}
        page = int(path.split('page=')[-1])
        data = repos if '/repos?' in path else people
        return list(data[(page-1)*100:page*100])
    return get


def document():
    return b'\xef\xbb\xbf# Approved featured/private text\r\n' + b'\n'.join(
        f'<!-- {n}:START -->\nold\n<!-- {n}:END -->'.encode() for n in updater.BLOCKS
    ) + b'\r\nKEEP THIS EXACTLY\n'


class UpdateTests(unittest.TestCase):
    def test_more_than_1000_forks_and_pagination(self):
        data = updater.collect(fixture([repo(f'fork-{i}', True) for i in range(1205)] + [repo('new')], [{'login': f'person-{i}'} for i in range(205)]))
        self.assertIn('1,205 public forks', data['PUBLIC-FORKS'])
        self.assertIn('Following (205)', data['PUBLIC-FOLLOWING'])
        self.assertIn('/Ardynai/new)', data['PUBLIC-PROJECTS'])

    def test_following_count_independent_of_sample(self):
        base = fixture(people=[{'login':f'person-{i}'} for i in range(383)])
        def get(path):
            return {'login':'Ardynai', 'following':384} if path == '/users/Ardynai' else base(path)
        rendered = updater.collect(get)['PUBLIC-FOLLOWING']
        self.assertIn('Following (384)', rendered)
        self.assertIn('From my following list:', rendered)
        self.assertEqual(12, rendered.count('\n- ['))

    def test_private_and_forks_not_in_projects(self):
        data = updater.collect(fixture([repo('secret', private=True), repo('fork', True), repo('public')]))
        self.assertNotIn('secret', str(data))
        self.assertNotIn('/Ardynai/fork)', data['PUBLIC-PROJECTS'])
        self.assertIn('/Ardynai/public)', data['PUBLIC-PROJECTS'])

    def test_untrusted_description_and_name(self):
        data = updater.collect(fixture([repo('a_b', description='![x](https://evil) <script> @someone\n<!-- PUBLIC-FORKS:END -->')]))
        rendered = data['PUBLIC-PROJECTS']
        self.assertNotIn('<script>', rendered)
        self.assertNotIn('![', rendered)
        self.assertNotIn('@someone', rendered)
        self.assertNotIn('<!--', rendered)
        self.assertIn('a&#95;b', rendered)

    def test_preserve_bytes_and_remove_newly_private(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'README.md'
            path.write_bytes(document())
            updater.update(path, fixture([repo('new')]))
            self.assertIn(b'/Ardynai/new)', path.read_bytes())
            updater.update(path, fixture([repo('new', private=True)]))
            current = path.read_bytes()
            self.assertNotIn(b'/Ardynai/new)', current)
            for name in updater.BLOCKS:
                a, b = f'<!-- {name}:START -->'.encode(), f'<!-- {name}:END -->'.encode()
                lo, hi = current.index(a)+len(a), current.index(b)
                current = current[:lo]+b'\nold\n'+current[hi:]
            self.assertEqual(document(), current)

    def test_dual_file_snapshot_and_self_exclusion(self):
        with tempfile.TemporaryDirectory() as d:
            paths = [Path(d) / n for n in ('README.md', 'README-static.md')]
            originals = [document(), b'STATIC ONLY\n' + document()]
            for path, original in zip(paths, originals):
                path.write_bytes(original)
            get = fixture([repo('Ardynai'), repo('project')])
            with patch.object(updater, 'collect', wraps=updater.collect) as collect:
                self.assertTrue(updater.update_many(paths, get))
                self.assertEqual(1, collect.call_count)
            for path, original in zip(paths, originals):
                result = path.read_bytes()
                self.assertIn(b'/Ardynai/project)', result)
                self.assertNotIn(b'/Ardynai/Ardynai)', result)
                for name in updater.BLOCKS:
                    start = f'<!-- {name}:START -->'.encode()
                    end = f'<!-- {name}:END -->'.encode()
                    a, b = result.index(start)+len(start), result.index(end)
                    result = result[:a]+b'\nold\n'+result[b:]
                self.assertEqual(original, result)
            before = paths[0].read_bytes()
            paths[1].write_bytes(b'invalid markers')
            with patch.object(updater, 'collect') as collect:
                with self.assertRaises(updater.UnsafeResponse):
                    updater.update_many(paths, get)
                collect.assert_not_called()
            self.assertEqual(before, paths[0].read_bytes())

    def test_dual_file_replace_failure_rolls_back(self):
        with tempfile.TemporaryDirectory() as d:
            paths = [Path(d) / n for n in ('README.md', 'README-static.md')]
            for path in paths:
                path.write_bytes(document())
            original_replace = updater.os.replace
            calls = 0
            def replace(source, destination):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError('simulated replacement failure')
                return original_replace(source, destination)
            with patch.object(updater.os, 'replace', side_effect=replace):
                with self.assertRaises(OSError):
                    updater.update_many(paths, fixture([repo('new')]))
            for path in paths:
                self.assertEqual(document(), path.read_bytes())

    def test_error_keeps_file_identical(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'README.md'
            path.write_bytes(document())
            def error(path):
                if path == '/users/Ardynai':
                    return {'login':'Ardynai', 'following':0}
                if '/following?' in path:
                    raise HTTPError(path, 403, 'rate limit', {}, None)
                return [repo('new')]
            with self.assertRaises(HTTPError):
                updater.update(path, error)
            self.assertEqual(document(), path.read_bytes())
            self.assertEqual(['README.md'], [p.name for p in Path(d).iterdir()])

    def test_marker_rejection(self):
        data = dict.fromkeys(updater.BLOCKS, 'new')
        for bad in (b'', document()+b'<!-- PUBLIC-FORKS:START -->', document().replace(b'<!-- PUBLIC-FORKS:END -->', b'')):
            with self.assertRaises(updater.UnsafeResponse):
                updater.replace_blocks(bad, data)

    def test_malformed_page_duplicate_owner_and_name(self):
        for get in (lambda path: {'error':'oops'}, fixture([repo('same'), repo('same')]), fixture([repo('bad/name')]), fixture([dict(repo('x'), owner={'login':'other'})])):
            with self.assertRaises(updater.UnsafeResponse):
                updater.collect(get)

    def test_deterministic_and_idempotent(self):
        self.assertEqual(updater.collect(fixture([repo('b'), repo('a')])), updater.collect(fixture([repo('a'), repo('b')])))
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'README.md'
            path.write_bytes(document())
            self.assertTrue(updater.update(path, fixture()))
            self.assertFalse(updater.update(path, fixture()))

    def test_no_redirect(self):
        with self.assertRaises(updater.UnsafeResponse):
            updater.NoRedirect().redirect_request(None, None, 301, '', {}, 'https://evil.test')

    def test_no_credentials_or_proxy(self):
        with patch.object(updater, 'build_opener') as opener:
            response = opener.return_value.open.return_value.__enter__.return_value
            response.status = 200
            response.read.return_value = b'[]'
            self.assertEqual([], updater.fetch('/users/Ardynai/repos'))
            request = opener.return_value.open.call_args.args[0]
            self.assertNotIn('Authorization', request.headers)
            self.assertEqual({}, opener.call_args.args[0].proxies)

if __name__ == '__main__':
    unittest.main()
