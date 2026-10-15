import type { On } from 'claude-code'
import { describe, expect, mock, test } from 'claude-code/testing'

// Выдуманный токен: его не должно быть ни в одной строке журнала.
const FAKE_TOKEN = 'fake-token-0000-not-a-secret'

const HOME = '/Users/test'
const LOG_DIR = `${HOME}/Library/Logs/claude-command-log`
const NOW = Date.UTC(2026, 9, 15, 12, 0, 0)

type World = {
  files: Map<string, string>
  calls: unknown[]
}

// Хуки на вызовы $ отвечают { value }.
// Мир под модом: файловая система в памяти, сессия, часы, окружение и сам инструмент.
// Хуки теста стоят под модом, так что настоящий диск и настоящий Bash не трогаются.
function world(on: On, session: string, options: { failWrite?: boolean; isError?: boolean } = {}): World {
  const files = new Map<string, string>()
  const calls: unknown[] = []
  mock.env(on, { HOME })
  mock.clock(on, { now: NOW })
  on('session.id', () => ({ value: session }))
  on('session.cwd', () => ({ value: '/Users/test/multica_workspaces/demo/workdir' }))
  on('fs.exists', ($, e) => ({ value: files.has(e.path) }))
  on('fs.read', ($, e) => ({ value: files.get(e.path) ?? '' }))
  on('fs.write', ($, e) => {
    if (options.failWrite) throw new Error('disk full')
    files.set(e.path, e.text)
    return { value: undefined }
  })
  on('tool.call', ($, e) => {
    calls.push(e)
    return options.isError
      ? { result: 'boom', isError: true }
      : { result: { stdout: 'done', stderr: '', interrupted: false } }
  })
  return { files, calls }
}

function entries(files: Map<string, string>, session: string): Record<string, unknown>[] {
  const text = files.get(`${LOG_DIR}/${session}.jsonl`) ?? ''
  return text.trim().split('\n').filter(Boolean).map(line => JSON.parse(line))
}

describe('command-log', () => {
  test('вызов Bash уходит дальше без изменений и не запрещается', async ($, on) => {
    const w = world(on, 'pass-1')
    const command = `curl -H "Authorization: Bearer ${FAKE_TOKEN}" https://example.invalid`
    const ran = await $.tool.call({ tool: 'Bash', command, description: 'Fetch' })

    expect(ran.deny).toBeUndefined()
    expect(ran.isError).toBeUndefined()
    expect(ran.result).toEqual({ stdout: 'done', stderr: '', interrupted: false })
    expect(w.calls.length).toBe(1)
    expect(w.calls[0]).toEqual(expect.objectContaining({ tool: 'Bash', command, description: 'Fetch' }))
  })

  test('для Bash пишется только первое слово, токена в журнале нет', async ($, on) => {
    const w = world(on, 'token-1')
    await $.tool.call({ tool: 'Bash', command: `curl -H "Authorization: Bearer ${FAKE_TOKEN}" https://example.invalid` })

    const log = [...w.files.values()].join('')
    expect(log.includes(FAKE_TOKEN)).toBe(false)
    expect(log.includes('Authorization')).toBe(false)
    expect(log.includes('example.invalid')).toBe(false)
    expect(entries(w.files, 'token-1')).toEqual([
      {
        time: new Date(NOW).toISOString(),
        session: 'token-1',
        cwd: '/Users/test/multica_workspaces/demo/workdir',
        tool: 'Bash',
        program: 'curl',
        result: 'ok',
      },
    ])
  })

  test('присваивание перед командой и путь к программе в журнал не попадают', async ($, on) => {
    const w = world(on, 'env-1')
    await $.tool.call({ tool: 'Bash', command: `API_TOKEN=${FAKE_TOKEN} /usr/local/bin/gh pr list` })
    await $.tool.call({ tool: 'Bash', command: `"$(cat secret)" --flag` })

    const log = [...w.files.values()].join('')
    expect(log.includes(FAKE_TOKEN)).toBe(false)
    expect(log.includes('/usr/local/bin')).toBe(false)
    expect(log.includes('secret')).toBe(false)
    expect(entries(w.files, 'env-1').map(entry => entry.program)).toEqual(['gh', '?'])
  })

  test('для Write путь и содержимое файла не пишутся', async ($, on) => {
    const w = world(on, 'write-1')
    await $.tool.call({ tool: 'Write', file_path: '/Users/test/private/notes.txt', content: FAKE_TOKEN })

    const log = [...w.files.values()].join('')
    expect(log.includes(FAKE_TOKEN)).toBe(false)
    expect(log.includes('notes.txt')).toBe(false)
    const [entry] = entries(w.files, 'write-1')
    expect(entry).toEqual(expect.objectContaining({ tool: 'Write', result: 'ok' }))
    expect(Object.keys(entry ?? {}).sort()).toEqual(['cwd', 'result', 'session', 'time', 'tool'])
  })

  test('ошибка инструмента записывается как error, результат не меняется', async ($, on) => {
    const w = world(on, 'error-1', { isError: true })
    const ran = await $.tool.call({ tool: 'Bash', command: 'false' })

    expect(ran.isError).toBe(true)
    expect(ran.deny).toBeUndefined()
    expect(entries(w.files, 'error-1').map(entry => entry.result)).toEqual(['error'])
  })

  test('ошибка записи журнала не роняет и не запрещает вызов', async ($, on) => {
    const w = world(on, 'fail-1', { failWrite: true })
    const ran = await $.tool.call({ tool: 'Bash', command: 'ls -la' })

    expect(ran.deny).toBeUndefined()
    expect(ran.result).toEqual({ stdout: 'done', stderr: '', interrupted: false })
    expect(w.calls.length).toBe(1)
    expect(w.files.size).toBe(0)
  })

  test('строки копятся в одном файле сессии, параллельные вызовы не теряются', async ($, on) => {
    const w = world(on, 'many-1')
    await Promise.all([
      $.tool.call({ tool: 'Bash', command: 'pwd' }),
      $.tool.call({ tool: 'Bash', command: 'ls' }),
      $.tool.call({ tool: 'Bash', command: 'git status' }),
    ])

    expect([...w.files.keys()]).toEqual([`${LOG_DIR}/many-1.jsonl`])
    expect(entries(w.files, 'many-1').map(entry => entry.program).sort()).toEqual(['git', 'ls', 'pwd'])
  })
})
