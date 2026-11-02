"""검사 파일 설명 캐시의 재사용·파일별 갱신과 계획 안내의 설정 조회 횟수를 확인한다."""
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import regression


with tempfile.TemporaryDirectory() as folder:
    repo = Path(folder)
    tests = repo / 'tests'
    tests.mkdir()
    first = tests / 'test_first.py'
    second = tests / 'test_second.py'
    first.write_text('"""첫 설명"""\n', encoding='utf-8')
    second.write_text('"""다른 설명"""\n', encoding='utf-8')
    with patch.object(regression.issues, 'list_projects', return_value=[
            {'key': 'CACHE', 'local_path': str(repo)}]), \
            patch.object(regression, 'test_files', return_value=[first, second]), \
            patch.object(regression.registry, 'file_numbers', return_value={}), \
            patch.object(regression.ast, 'parse', wraps=regression.ast.parse) as parse:
        rows = regression.catalog('CACHE')
        assert parse.call_count == 2
        assert next(r for r in rows if r['name'] == first.name)['description'] == '첫 설명'
        parse.reset_mock()
        assert regression.catalog('CACHE') == rows
        assert parse.call_count == 0

        # 크기만 달라져도 해당 파일만 다시 읽는다.
        previous = first.stat()
        first.write_text('"""길어진 새 설명"""\n', encoding='utf-8')
        os.utime(first, ns=(previous.st_atime_ns, previous.st_mtime_ns))
        rows = regression.catalog('CACHE')
        assert parse.call_count == 1
        assert next(r for r in rows if r['name'] == first.name)['description'] == '길어진 새 설명'
        parse.reset_mock()

        # 같은 크기라도 mtime이 달라지면 설명을 갱신한다.
        previous = second.stat()
        second.write_text('"""바뀐 설명"""\n', encoding='utf-8')
        assert second.stat().st_size == previous.st_size
        os.utime(second, ns=(previous.st_atime_ns, previous.st_mtime_ns + 1_000_000_000))
        rows = regression.catalog('CACHE')
        assert parse.call_count == 1
        assert next(r for r in rows if r['name'] == second.name)['description'] == '바뀐 설명'

    js = tests / 'test_script.js'
    js.write_text('// JS 설명\n', encoding='utf-8')
    assert regression._file_doc(js) == 'JS 설명'
    with patch.object(Path, 'read_text', side_effect=AssertionError('캐시를 다시 읽음')):
        assert regression._file_doc(js) == 'JS 설명'
    missing = tests / 'test_missing.py'
    assert regression._file_doc(missing) == '개별 회귀 검사'
    assert missing.resolve() not in regression._DOC_CACHE
    invalid = tests / 'test_invalid.py'
    invalid.write_text('def (\n', encoding='utf-8')
    assert regression._file_doc(invalid) == '개별 회귀 검사'

checks = [{'key': 'agent_related', 'mode': 'auto'},
          {'key': 'file:test_first.py', 'mode': 'on'}]
with patch.object(regression, 'settings', return_value={'checks': checks}) as settings:
    instructions = regression.planning_instructions()
    settings.assert_called_once_with()
    assert 'agent_related=auto' in instructions
    assert 'file:test_first.py=on' in instructions

print('OK — 설명 캐시 재사용·파일별 갱신·설정 1회 조회')
