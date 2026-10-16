import type { EngineInterface, Register, ToolCallInput } from 'claude-code'
import { checkCall, denyText, looksDangerous, type Env, type RuleId, type ToolCall } from './rules'

// Мод-сторож: смотрит на вызов инструмента до запуска и отвечает { deny }, если вызов
// нарушает один из трёх запретов (rules.ts). Остальное уходит дальше через next(e).
//
// Каждый запрет пишется в журнал ~/Library/Logs/claude-guard/<сессия>.jsonl:
// time, session, cwd, tool, rule. Текст команды, пути и содержимое файлов не пишутся.

const LOG_DIR = 'Library/Logs/claude-guard'
const PART_LIMIT = 3 * 1024 * 1024
const GIT_TIMEOUT_MS = 5_000

type Entry = { time: string; session: string; cwd: string; tool: string; rule: RuleId }
type SessionLog = { part: number; text: string; queue: Promise<void> }

function safeFileName(session: string): string {
  return session.replace(/[^A-Za-z0-9-]/g, '_').slice(0, 128) || 'unknown'
}

function partPath(dir: string, session: string, part: number): string {
  const base = safeFileName(session)
  return part === 1 ? `${dir}/${base}.jsonl` : `${dir}/${base}.${part}.jsonl`
}

const logs = new Map<string, Promise<SessionLog>>()

async function open($: EngineInterface, dir: string, session: string): Promise<SessionLog> {
  let part = 1
  while (await $.fs.exists(partPath(dir, session, part + 1))) part++
  const path = partPath(dir, session, part)
  const text = (await $.fs.exists(path)) ? await $.fs.read(path) : ''
  return { part, text, queue: Promise.resolve() }
}

async function writeLog($: EngineInterface, home: string, cwd: string, e: ToolCallInput, rule: RuleId): Promise<void> {
  const dir = `${home}/${LOG_DIR}`
  const session = await $.session.id()
  const entry: Entry = {
    time: new Date(await $.clock.now()).toISOString(),
    session,
    cwd,
    tool: String(e.tool),
    rule,
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

function envOf($: EngineInterface, home: string, cwd: string): Env {
  return {
    home,
    cwd,
    branchOf: async dir => {
      try {
        const run = await $.process.run(['git', 'symbolic-ref', '--short', 'HEAD'], { cwd: dir, timeoutMs: GIT_TIMEOUT_MS })
        const branch = run.stdout.trim()
        return run.exitCode === 0 && branch !== '' ? branch : null
      } catch {
        return null
      }
    },
    realPath: async path => {
      try {
        return (await $.fs.stat(path, { resolve: true })).realPath
      } catch {
        return undefined
      }
    },
  }
}

export const register: Register = on => {
  on('tool.call', async ($, e, next) => {
    const home = (await $.env.get('HOME')) ?? ''
    const cwd = await $.session.cwd()
    // Без HOME защищённые папки не вычислить: решаем как при собственной ошибке мода.
    if (home === '') throw new Error('HOME is not set')

    const violation = await checkCall(e as unknown as ToolCall, envOf($, home, cwd))
    if (violation === null) return next(e)

    try {
      await writeLog($, home, cwd, e, violation.rule)
    } catch {
      // Журнал не должен менять решение: запрет остаётся запретом.
    }
    return { deny: denyText(violation) }
  }).catch(($, e, next) => {
    // Хук упал, превысил время или вызов пришёл из-под его же $-вызова.
    // Здесь $ недоступен: решаем только по тексту вызова (ИТОГ PDL-11, раздел 3).
    if (next.called) return next(e)
    if (looksDangerous(e as unknown as ToolCall)) return { deny: denyText({ rule: 'guard-failed', why: 'внутренняя ошибка мода guard' }) }
    return next(e)
  })
}
