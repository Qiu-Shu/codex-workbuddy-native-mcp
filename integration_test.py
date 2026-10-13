"""Live test of the exact stdio MCP entry point. Uses disposable synthetic files."""
import asyncio
import json
import os
import secrets
import sys
import tomllib
import uuid
from datetime import timedelta
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent


def payload(result):
    if result.isError:
        raise RuntimeError(str(result.content))
    return result.structuredContent or json.loads(result.content[0].text)


async def main():
    folder = ROOT / 'work' / ('integration-' + uuid.uuid4().hex[:8])
    folder.mkdir(parents=True)
    seed = {'marker': uuid.uuid4().hex, 'values': [secrets.randbelow(1000) for _ in range(5)]}
    source = folder / '输入.json'
    source.write_text(json.dumps(seed), encoding='utf-8')
    report = {'directory': str(folder), 'tasks': []}
    if '--registered' in sys.argv:
        codex_dir = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')
        config = tomllib.loads((codex_dir / 'config.toml').read_text(encoding='utf-8'))['mcp_servers']['workbuddy']
        assert config.get('enabled', True)
        params = StdioServerParameters(command=config['command'], args=config['args'], env=config.get('env'))
        report['registered_config_verified'] = True
    else:
        params = StdioServerParameters(command=sys.executable, args=[str(ROOT / 'local_bridge.py')])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=700)) as session:
            await session.initialize()
            report['tools'] = [t.name for t in (await session.list_tools()).tools]
            report['connection'] = payload(await session.call_tool('workbuddy_status', {}))
            print(json.dumps({'tools': report['tools'], 'connection': report['connection']}), flush=True)
            assert report['connection']['connected']
            async def run(prompt):
                start = payload(await session.call_tool('workbuddy_start', {
                    'prompt': prompt, 'cwd': str(folder), 'timeout_seconds': 240}))
                print('Started ' + start['task_id'], flush=True)
                while True:
                    result = payload(await session.call_tool('workbuddy_wait', {
                        'task_id': start['task_id'], 'timeout_seconds': 30}))
                    if result['state'] != 'running':
                        break
                    print('Still working ' + start['task_id'], flush=True)
                report['tasks'].append(result)
                (folder / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
                print(json.dumps(result, ensure_ascii=False), flush=True)
                assert result['state'] == 'completed', result
                return result
            await run('Use Read to read 输入.json in the authorized directory. Calculate the sum of values yourself. '
                      'Use Write to create result.json with exactly marker (copied from input), total (number), '
                      'and status="draft". Do not alter the input. No shell, no Python. You must actually write the file.')
            result_file = folder / 'result.json'
            result = json.loads(result_file.read_text(encoding='utf-8-sig'))
            assert result == {'marker': seed['marker'], 'total': sum(seed['values']), 'status': 'draft'}, result
            await run('Read result.json, then use the Edit file tool to change only its status value from draft to verified. '
                      'Preserve marker and total. Do not use Write or a shell for this edit.')
            result = json.loads(result_file.read_text(encoding='utf-8-sig'))
            assert result == {'marker': seed['marker'], 'total': sum(seed['values']), 'status': 'verified'}, result
            assert json.loads(source.read_text(encoding='utf-8')) == seed
            web = await run('联网实测：必须先用 WebSearch 搜索 IANA example domains，再用 WebFetch 读取 '
                            'https://www.iana.org/help/example-domains 。用中文说明 example.com/example.org 的用途，'
                            '是否可注册或转让，附上实际访问的官方来源链接。不要修改任何文件；不要凭记忆回答。')
            tool_names = [e['tool'].casefold() for e in web['tool_events'] if e['status'] == 'completed']
            assert 'websearch' in tool_names and 'webfetch' in tool_names, tool_names
            assert 'iana.org' in web['answer']
            assert not any(e['status'] == 'failed' for task in report['tasks'] for e in task['tool_events'])
            assert not any(not p['allowed'] for task in report['tasks'] for p in task.get('permissions', []))
            report['passed'] = True
            (folder / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            print('ALL CHECKS PASSED: ' + str(folder / 'report.json'), flush=True)


if __name__ == '__main__':
    asyncio.run(main())
