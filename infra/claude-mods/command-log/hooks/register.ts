import type { EngineInterface, Register, ToolCallInput, ToolCallResult } from 'claude-code'

// Мод только пишет журнал: каждый вызов уходит дальше через next(e) без изменений,
// а любая ошибка журнала глотается. Журнал — ~/Library/Logs/claude-command-log/<сессия>.jsonl.
//
// В журнал попадают только time, session, cwd, tool, program (для Bash) и result.
// Аргументы, полный текст команды, окружение, содержимое файлов, промпты и пути
// из Edit/Write не пишутся никогда.

const LOG_DIR = 'Library/Logs/claude-command-log'

// $.fs.write пишет файл целиком и отказывает выше 4 МиБ: держим запас и,
// дойдя до него, начинаем следующую часть <сессия>.2.jsonl, <сессия>.3.jsonl…
const PART_LIMIT = 3 * 1024 * 1024

// Имя программы — только безопасные символы и не длиннее 64; иначе "?".
const SAFE_PROGRAM = /^[A-Za-z0-9._+-]{1,64}$/
const ENV_ASSIGNMENT = /^[A-Za-z_][A-Za-z0-9_]*=/

type Entry = {
  time: string
  session: string
  cwd: string
  tool: string
  program?: string
  result: 'ok' | 'error'
}

type SessionLog = {
  part: number
  text: string
  // Записи одной сессии идут строго по очереди: параллельные вызовы инструментов
  // дописывают строку в память, а файл каждый раз переписывается целиком.
  queue: Promise<void>
}

// Первое слово команды, без ведущих присваиваний вида NAME=value (в них бывают
// токены) и без пути к программе. Всё, что не похоже на имя программы, — "?".
export function programOf(command: string): string {
  const words = command.trim().split(/\s+/)
  const first = words.find(word => !ENV_ASSIGNMENT.test(word)) ?? ''
  const name = first.split('/').pop() ?? ''
  return SAFE_PROGRAM.test(name) ? name : '?'
}

function safeFileName(session: string): string {
  return session.replace(/[^A-Za-z0-9-]/g, '_').slice(0, 128) || 'unknown'
}

function partPath(dir: string, session: string, part: number): string {
  const base = safeFileName(session)
  return part === 1 ? `${dir}/${base}.jsonl` : `${dir}/${base}.${part}.jsonl`
}

const logs = new Map<string, Promise<SessionLog>>()

// При перезагрузке модуля память пуста: продолжаем уже записанный файл, а не затираем его.
async function open($: EngineInterface, dir: string, session: string): Promise<SessionLog> {
  let part = 1
  while (await $.fs.exists(partPath(dir, session, part + 1))) part++
  const path = partPath(dir, session, part)
  const text = (await $.fs.exists(path)) ? await $.fs.read(path) : ''
  return { part, text, queue: Promise.resolve() }
}

async function write($: EngineInterface, e: ToolCallInput, ran: ToolCallResult): Promise<void> {
  const home = await $.env.get('HOME')
  if (home === undefined || home === '') return
  const dir = `${home}/${LOG_DIR}`

  const session = await $.session.id()
  const entry: Entry = {
    time: new Date(await $.clock.now()).toISOString(),
    session,
    cwd: await $.session.cwd(),
    tool: String(e.tool),
    ...(e.tool === 'Bash' ? { program: programOf(e.command) } : {}),
    result: ran.deny !== undefined || ran.isError === true ? 'error' : 'ok',
  }
  const line = JSON.stringify(entry) + '\n'

  let pending = logs.get(session)
  if (pending === undefined) {
    pending = open($, dir, session)
    logs.set(session, pending)
    pending.catch(() => logs.delete(session))
  }
  const log = await pending

  if (log.text.length + line.length > PART_LIMIT) {
    log.part++
    log.text = ''
  }
  log.text += line
  const path = partPath(dir, session, log.part)
  const text = log.text
  log.queue = log.queue.catch(() => {}).then(() => $.fs.write(path, text))
  await log.queue
}

export const register: Register = on => {
  on('tool.call', async ($, e, next) => {
    const ran = await next(e)
    try {
      await write($, e, ran)
    } catch {
      // Журнал не должен мешать работе: ошибка записи не меняет результат вызова.
    }
    return ran
  }).catch(($, e, next) => next(e))
}
