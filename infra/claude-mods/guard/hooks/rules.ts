// Разбор вызова инструмента и три запрета мода guard (план — ИТОГ PDL-11, разделы 1–3).
//
// Здесь нет вызовов $: всё, что нужно от системы (HOME, папка сессии, текущая ветка,
// настоящий путь файла), приходит через Env. Поэтому правила проверяются тестами
// без настоящего диска и без настоящего git.
//
// Это страховка от ошибок, а не от умысла: команда из файла-скрипта, закодированная
// строка или программа, которая сама ходит в сеть, мимо пройдут — так задумано.

export type RuleId =
  | 'push-main'
  | 'push-branch-unknown'
  | 'push-force'
  | 'pr-merge'
  | 'gh-api-write'
  | 'mcp-github-write'
  | 'ssh'
  | 'docker-remote'
  | 'legacy-host'
  | 'legacy-repo'
  | 'legacy-script'
  | 'protected-path'
  | 'claude-plugin'
  | 'claude-nested'
  | 'guard-failed'

export type Violation = { rule: RuleId; why: string }

export type Env = {
  home: string
  cwd: string
  // Текущая ветка репозитория в папке; null — определить не удалось.
  branchOf: (dir: string) => Promise<string | null>
  // Путь после раскрытия ссылок; undefined — путь никуда не ведёт или узнать не удалось.
  realPath: (path: string) => Promise<string | undefined>
}

const PUSH_SOURCE = 'CLAUDE.md, раздел «В конце работы»: отправка в main, слияние PR и force-push — только владелец; навык zone-guard'
const LEGACY_SOURCE = 'CLAUDE.md, раздел «Запреты»; навык prod-safety'
const SETTINGS_SOURCE = 'правила агентов: ~/.claude, настройки Claude Code и Multica не менять; навык zone-guard'

export const RULES: Record<RuleId, { title: string; source: string }> = {
  'push-main': { title: 'отправка в main или master', source: PUSH_SOURCE },
  'push-branch-unknown': { title: 'git push без явной ветки, а текущую ветку определить не удалось', source: PUSH_SOURCE },
  'push-force': { title: 'force-push, удаление или массовая перезапись веток', source: PUSH_SOURCE },
  'pr-merge': { title: 'слияние PR', source: PUSH_SOURCE },
  'gh-api-write': { title: 'запись в ветку или слияние через GitHub API', source: PUSH_SOURCE },
  'mcp-github-write': { title: 'запись в ветку или слияние через инструмент GitHub', source: PUSH_SOURCE },
  ssh: { title: 'SSH и удалённый вход', source: LEGACY_SOURCE },
  'docker-remote': { title: 'удалённый Docker или Docker Hub', source: LEGACY_SOURCE },
  'legacy-host': { title: 'обращение к хосту старой системы', source: LEGACY_SOURCE },
  'legacy-repo': { title: 'работа со старым репозиторием', source: 'CLAUDE.md, раздел «Запреты»' },
  'legacy-script': {
    title: 'запуск скрипта, который пишет во внешние сервисы старой системы',
    source: 'CLAUDE.md, раздел «Запреты»; bot/SOURCE.md и n8n/SOURCE.md, раздел «Скрипты с записью во внешние сервисы»',
  },
  'protected-path': { title: 'правка ~/.claude, ~/.multica, ~/claude-mods или настроек Claude Code', source: SETTINGS_SOURCE },
  'claude-plugin': { title: 'управление модами и настройками Claude Code', source: SETTINGS_SOURCE },
  'claude-nested': { title: 'запуск Claude Code без модов или с другими настройками', source: SETTINGS_SOURCE },
  'guard-failed': {
    title: 'мод guard не смог проверить вызов, а в нём есть опасные слова',
    source: 'мод guard, infra/claude-mods/guard/README.md',
  },
}

export function denyText(v: Violation): string {
  const rule = RULES[v.rule]
  const lines = [
    `guard: действие запрещено — ${rule.title}.`,
    `Почему: ${v.why}.`,
    `Правило: ${rule.source}.`,
  ]
  if (v.rule === 'push-branch-unknown') lines.push('Подсказка: укажи ветку явно: git push -u origin <ветка задачи>.')
  lines.push(
    'Не обходи запрет другой формой команды. Если задачу без этого не выполнить — напиши комментарий в задаче, что нужно и зачем, и переведи её в blocked. Решает владелец.',
  )
  return lines.join('\n')
}

// ───────────────────────── Разбор команды Bash ─────────────────────────

export type Word = { text: string; dynamic: boolean }

export type SimpleCommand = {
  words: Word[]
  redirects: Word[]
  heredocs: string[]
  // Команда стоит на верхнем уровне строки (не внутри $(…), `…`, <(…)): только там учитывается cd.
  top: boolean
}

type Pending = { delimiter: string; strip: boolean; target: SimpleCommand }

class Parser {
  i = 0
  out: SimpleCommand[] = []
  pending: Pending[] = []
  constructor(readonly src: string) {}

  // Читает список команд до закрывающего символа (')' для $(…), null — до конца).
  list(closer: ')' | null, top: boolean): void {
    let depth = 0
    let cmd = this.fresh(top)
    let word: Word | null = null
    let redirectNext = false
    let heredocNext: { strip: boolean } | null = null
    const s = this.src

    const endWord = () => {
      if (word === null) return
      if (heredocNext !== null) {
        this.pending.push({ delimiter: word.text, strip: heredocNext.strip, target: cmd })
        heredocNext = null
      } else if (redirectNext) {
        cmd.redirects.push(word)
        redirectNext = false
      } else {
        cmd.words.push(word)
      }
      word = null
    }
    const endCommand = () => {
      endWord()
      if (cmd.words.length > 0 || cmd.redirects.length > 0) this.out.push(cmd)
      cmd = this.fresh(top)
    }
    const add = (text: string, dynamic = false) => {
      if (word === null) word = { text: '', dynamic: false }
      word.text += text
      if (dynamic) word.dynamic = true
    }

    while (this.i < s.length) {
      const c = s[this.i]!
      const rest = s.slice(this.i)

      if (c === '\\') {
        if (s[this.i + 1] === '\n') this.i += 2
        else {
          add(s[this.i + 1] ?? '')
          this.i += 2
        }
        continue
      }
      if (c === "'") {
        const end = s.indexOf("'", this.i + 1)
        const stop = end === -1 ? s.length : end
        add(s.slice(this.i + 1, stop))
        if (word === null) word = { text: '', dynamic: false }
        this.i = stop + 1
        continue
      }
      if (rest.startsWith("$'")) {
        let j = this.i + 2
        let text = ''
        while (j < s.length && s[j] !== "'") {
          if (s[j] === '\\' && j + 1 < s.length) {
            text += s[j + 1]
            j += 2
          } else text += s[j++]
        }
        add(text)
        this.i = j + 1
        continue
      }
      if (c === '"') {
        this.i++
        if (word === null) word = { text: '', dynamic: false }
        while (this.i < s.length && s[this.i] !== '"') {
          const d = s[this.i]!
          if (d === '\\' && this.i + 1 < s.length) {
            const n = s[this.i + 1]!
            add('"\\$`\n'.includes(n) ? (n === '\n' ? '' : n) : d + n)
            this.i += 2
          } else if (d === '$' || d === '`') {
            add(...this.expansion())
          } else {
            add(d)
            this.i++
          }
        }
        this.i++
        continue
      }
      if (c === '$' || c === '`') {
        add(...this.expansion())
        continue
      }
      if ((c === '<' || c === '>') && s[this.i + 1] === '(') {
        // Подстановка процесса <(…) / >(…): содержимое — отдельные команды.
        const start = this.i
        this.i += 2
        this.list(')', false)
        add(s.slice(start, this.i), true)
        continue
      }
      if (c === '#' && word === null) {
        while (this.i < s.length && s[this.i] !== '\n') this.i++
        continue
      }
      if (c === '\n') {
        endCommand()
        this.i++
        this.heredocBodies()
        continue
      }
      if (c === ' ' || c === '\t') {
        endWord()
        this.i++
        continue
      }
      if (c === '(') {
        endCommand()
        depth++
        this.i++
        continue
      }
      if (c === ')') {
        endCommand()
        this.i++
        if (depth === 0 && closer === ')') return
        depth = Math.max(0, depth - 1)
        continue
      }
      const op = /^(&&|\|\||;;|\|&|[;&|])/.exec(rest)
      if (op && !(c === '&' && s[this.i + 1] === '>')) {
        endCommand()
        this.i += op[0].length
        continue
      }
      const redir = /^(<<<|<<-|<<|&>>|&>|>>|>\||>&|<&|<>|>|<)/.exec(rest)
      if (redir) {
        // Номер потока перед стрелкой (2>…) — не слово команды.
        if (word !== null && /^\d+$/.test(word.text) && !word.dynamic) word = null
        endWord()
        const r = redir[0]
        this.i += r.length
        if (r === '<<' || r === '<<-') heredocNext = { strip: r === '<<-' }
        else if (r === '>&' || r === '<&') {
          // 2>&1, >&2: дальше номер потока, а не файл.
          const fd = /^\s*[-\d]+/.exec(s.slice(this.i))
          if (fd) this.i += fd[0].length
          else redirectNext = true
        } else if (r !== '<<<' && r !== '<' && r !== '<>') redirectNext = true
        continue
      }
      add(c)
      this.i++
    }
    endCommand()
  }

  fresh(top: boolean): SimpleCommand {
    return { words: [], redirects: [], heredocs: [], top }
  }

  // $(…), `…`, ${…}, $NAME. Возвращает исходный текст как часть слова (dynamic).
  expansion(): [string, boolean] {
    const s = this.src
    const start = this.i
    if (s.startsWith('$(', this.i)) {
      this.i += 2
      this.list(')', false)
      return [s.slice(start, this.i), true]
    }
    if (s[this.i] === '`') {
      let j = this.i + 1
      let inner = ''
      while (j < s.length && s[j] !== '`') {
        if (s[j] === '\\' && j + 1 < s.length) {
          inner += s[j + 1]
          j += 2
        } else inner += s[j++]
      }
      this.i = j + 1
      const nested = new Parser(inner)
      nested.list(null, false)
      for (const cmd of nested.out) this.out.push({ ...cmd, top: false })
      return [s.slice(start, this.i), true]
    }
    if (s.startsWith('${', this.i)) {
      let depth = 0
      let j = this.i
      while (j < s.length) {
        if (s[j] === '{') depth++
        else if (s[j] === '}' && --depth === 0) break
        j++
      }
      this.i = j + 1
      return [s.slice(start, this.i), true]
    }
    const name = /^\$([A-Za-z_][A-Za-z0-9_]*|[0-9@*#?$!-])/.exec(s.slice(this.i))
    if (name) {
      this.i += name[0].length
      return [name[0], true]
    }
    this.i++
    return ['$', false]
  }

  heredocBodies(): void {
    const s = this.src
    while (this.pending.length > 0) {
      const h = this.pending.shift()!
      const body: string[] = []
      while (this.i < s.length) {
        const nl = s.indexOf('\n', this.i)
        const end = nl === -1 ? s.length : nl
        const line = s.slice(this.i, end)
        this.i = end + 1
        if ((h.strip ? line.replace(/^\t+/, '') : line) === h.delimiter) break
        body.push(line)
      }
      h.target.heredocs.push(body.join('\n'))
    }
  }
}

export function parseCommand(command: string): SimpleCommand[] {
  const p = new Parser(command)
  p.list(null, true)
  return p.out
}

// ───────────────────────── Программа и её аргументы ─────────────────────────

const ASSIGNMENT = /^[A-Za-z_][A-Za-z0-9_]*=/
const SHELLS = new Set(['bash', 'sh', 'zsh', 'dash', 'ksh', 'fish'])
const KEYWORDS = new Set(['{', '}', '!', 'then', 'do', 'else', 'elif', 'if', 'while', 'until', 'exec', 'builtin', 'command', 'nohup', 'time', 'noglob'])

export type Invocation = {
  program: string
  args: Word[]
  assignments: string[]
  cmd: SimpleCommand
}

function basename(path: string): string {
  return path.replace(/\/+$/, '').split('/').pop() ?? ''
}

// Убирает присваивания NAME=… и обёртки (env, sudo, nohup, time, nice, timeout, xargs…).
export function invocationOf(cmd: SimpleCommand): Invocation | null {
  const words = cmd.words
  const assignments: string[] = []
  let i = 0
  const skipOptions = (withValue: Set<string>) => {
    while (i < words.length && words[i]!.text.startsWith('-') && words[i]!.text !== '-') {
      const w = words[i]!.text
      i++
      if (w === '--') break
      if (withValue.has(w)) i++
    }
  }
  for (;;) {
    while (i < words.length && ASSIGNMENT.test(words[i]!.text)) assignments.push(words[i++]!.text)
    if (i >= words.length) return null
    const name = basename(words[i]!.text)
    if (KEYWORDS.has(name)) {
      i++
      if (name === 'command' || name === 'time') skipOptions(new Set())
      continue
    }
    if (name === 'env') {
      i++
      skipOptions(new Set(['-u', '--unset', '-C', '--chdir', '-S', '--split-string']))
      continue
    }
    if (name === 'sudo' || name === 'doas') {
      i++
      skipOptions(new Set(['-u', '-g', '-h', '-p', '-U', '-C', '-D', '-r', '-t']))
      continue
    }
    if (name === 'nice' || name === 'caffeinate' || name === 'stdbuf' || name === 'unbuffer') {
      i++
      skipOptions(new Set(['-n', '-w', '-t']))
      continue
    }
    if (name === 'timeout' || name === 'gtimeout') {
      i++
      skipOptions(new Set(['-s', '--signal', '-k', '--kill-after']))
      i++ // длительность
      continue
    }
    if (name === 'xargs') {
      i++
      skipOptions(new Set(['-I', '-n', '-P', '-L', '-s', '-E', '-d', '-a']))
      continue
    }
    return { program: name, args: words.slice(i + 1), assignments, cmd }
  }
}

// ───────────────────────── Пути ─────────────────────────

function foldPath(path: string): string {
  const parts: string[] = []
  for (const part of path.split('/')) {
    if (part === '' || part === '.') continue
    if (part === '..') parts.pop()
    else parts.push(part)
  }
  return '/' + parts.join('/')
}

// Раскрывает ~, $HOME, ${HOME}; относительный путь — от папки dir. null — путь не вычислить.
export function resolvePath(raw: string, home: string, dir: string | null): string | null {
  let p = raw
  if (p === '~' || p.startsWith('~/')) p = home + p.slice(1)
  p = p.replace(/^\$\{HOME\}|^\$HOME(?![A-Za-z0-9_])/, home)
  if (p.includes('$') || p.includes('`')) return null
  if (!p.startsWith('/')) {
    if (dir === null) return null
    p = `${dir}/${p}`
  }
  return foldPath(p)
}

const SETTINGS_FILE = /(^|\/)\.claude\/settings(\.local)?\.json$/i

export function isProtectedPath(path: string, home: string): boolean {
  const p = path.toLowerCase()
  const h = foldPath(home).toLowerCase()
  if (SETTINGS_FILE.test(p)) return true
  const under = (dir: string) => p === dir || p.startsWith(dir + '/')
  if (under(`${h}/.claude`)) {
    // Память агента Claude Code пишет сам: ~/.claude/projects/<проект>/memory/.
    const rest = p.slice(`${h}/.claude/`.length)
    return !/^projects\/[^/]+\/memory(\/|$)/.test(rest)
  }
  return under(`${h}/claude-mods`) || under(`${h}/.multica`)
}

function protectedWord(word: Word, home: string, dir: string | null): boolean {
  const candidates = [word.text]
  const eq = word.text.indexOf('=')
  if (word.text.startsWith('-') && eq !== -1) candidates.push(word.text.slice(eq + 1))
  for (const raw of candidates) {
    if (SETTINGS_FILE.test(raw.replace(/\/+$/, ''))) return true
    if (/\.claude\/settings(\.local)?\.json/i.test(raw)) return true
    const abs = resolvePath(raw, home, dir)
    if (abs !== null && isProtectedPath(abs, home)) return true
    // Путь не вычислить ($VAR и т. п.), но в нём явно названа защищённая папка домашней папки.
    if (abs === null && /(~|\$\{?HOME\}?)\/(\.claude|\.multica|claude-mods)(\/|$)/i.test(raw)) return true
  }
  return false
}

// ───────────────────────── Списки запретов ─────────────────────────

// Хосты старой системы (ИТОГ PDL-11, раздел 1, запрет 2). Только имена, без ключей.
const LEGACY_HOSTS: [RegExp, string][] = [
  [/dockhost\.net/i, 'dockhost.net'],
  [/bitrix24\.[a-z]{2,}/i, 'bitrix24'],
  [/sms-prosto\.ru/i, 'sms-prosto.ru'],
  [/hub\.docker\.com/i, 'hub.docker.com'],
  [/registry-1\.docker\.io/i, 'registry-1.docker.io'],
  [/api\.telegram\.org/i, 'api.telegram.org'],
  [/platform-api\.max\.ru/i, 'platform-api.max.ru'],
]

const LEGACY_REPO = /vitaalii\/(unifiedbot-n8n|unifiedbot|site_graber)(?![A-Za-z0-9_-])/i

// bot/SOURCE.md и n8n/SOURCE.md, раздел «Скрипты с записью во внешние сервисы».
const LEGACY_SCRIPTS = new Set([
  'register_webhooks.py',
  'bx_delete_test_deals.py',
  'issue_cabinet_link.py',
  'apply_workflow_patch.py',
  'build_session_logs.py',
  'create_channel_credentials.py',
  'install_eval_webhooks.py',
  'eval_webhook_switch.py',
  'run_combo_once.py',
  'check_car_search_vs_reference.py',
  'run_save_crm_test_once.py',
  'push_tables.py',
  'add_intents_v2.py',
  'add_v301_draft_blocks.py',
  'add_needcheck_v303.py',
  'add_greeting_missingdata_v304.py',
  'fix_credit_goal_v305.py',
  'add_cabinet_v306.py',
  'add_cabinet_tactic_v307.py',
  'apply_prompt_edits_v308.py',
  'apply_prompt_edits_dedup.py',
  'apply_prompt_edits_link.py',
  'apply_prompt_edits_marker.py',
  'apply_prompt_edits_mirror.py',
  'apply_prompt_edits_prinyal.py',
  'apply_prompt_edits_verbatim.py',
  'cleanup_stale_notes.py',
  'fix_need_check_ok_scope.py',
  'apply_cabinet_finalize_ban.py',
  'apply_debrand_blocks.py',
  'apply_plate_error_branches.py',
  'debrand_testruns.py',
])

const INTERPRETERS = /^(python[0-9.]*|node|bun|deno|bash|sh|zsh|uv|poetry|pipenv|pdm|source|\.)$/

// Программы, которые только читают локальные файлы или печатают текст: адрес в их аргументах — не обращение по сети.
const READERS = new Set([
  'grep', 'egrep', 'fgrep', 'rg', 'cat', 'head', 'tail', 'less', 'more', 'awk', 'wc', 'ls', 'find',
  'echo', 'printf', 'sort', 'uniq', 'cut', 'tr', 'diff', 'jq', 'file', 'stat', 'basename', 'dirname',
  'realpath', 'test', '[', 'true', 'false', 'pwd', 'cd', 'multica',
])

// Подкоманды git, которые ходят в сеть или настраивают, куда ходить. Остальные работают с локальным репозиторием.
const GIT_NETWORK = new Set(['clone', 'fetch', 'pull', 'push', 'ls-remote', 'remote', 'submodule', 'send-email', 'request-pull', 'archive', 'config', 'svn', 'p4'])

const SSH_PROGRAMS = new Set(['ssh', 'scp', 'sftp', 'mosh', 'autossh', 'sshpass'])

const WRITERS = new Set([
  'rm', 'rmdir', 'mv', 'cp', 'ln', 'tee', 'chmod', 'chown', 'chflags', 'touch', 'truncate', 'install',
  'unlink', 'mkdir', 'rsync', 'dd', 'ditto', 'patch', 'curl', 'wget', 'tar', 'unzip', 'open', 'trash',
])

const GIT_READ = new Set(['status', 'log', 'show', 'diff', 'grep', 'blame', 'rev-parse', 'symbolic-ref', 'ls-files', 'describe', 'shortlog', 'help'])

const CLAUDE_PLUGIN_DENY = new Set(['disable', 'uninstall', 'remove', 'rm', 'install', 'i', 'update', 'enable', 'configure'])
const CLAUDE_MARKETPLACE_DENY = new Set(['add', 'remove', 'rm', 'update'])
const CLAUDE_NESTED_FLAGS = /^--(bare|safe-mode|setting-sources|plugin-dir|settings)(=|$)/

// ───────────────────────── git ─────────────────────────

type GitCall = { sub: string; args: Word[]; dir: string | null }

function gitCall(inv: Invocation, home: string, dir: string | null): GitCall | null {
  const a = inv.args
  let i = 0
  let d = dir
  while (i < a.length && a[i]!.text.startsWith('-')) {
    const w = a[i]!
    if (w.text === '-C') {
      const next = a[i + 1]
      d = next === undefined || next.dynamic ? null : resolvePath(next.text, home, d)
      i += 2
    } else if (w.text === '-c' || w.text === '--git-dir' || w.text === '--work-tree' || w.text === '--namespace' || w.text === '--config-env') {
      if (w.text !== '-c' && w.text !== '--config-env') d = null
      i += 2
    } else {
      if (w.text.startsWith('--git-dir=') || w.text.startsWith('--work-tree=')) d = null
      i++
    }
  }
  const sub = a[i]
  if (sub === undefined) return null
  return { sub: sub.text, args: a.slice(i + 1), dir: d }
}

const PUSH_VALUE_OPTIONS = new Set(['--repo', '-o', '--push-option', '--receive-pack', '--exec'])
const PROTECTED_BRANCH = /^(main|master)$/i

async function checkPush(git: GitCall, env: Env): Promise<Violation | null> {
  const positionals: Word[] = []
  for (let i = 0; i < git.args.length; i++) {
    const w = git.args[i]!.text
    if (w === '--') {
      positionals.push(...git.args.slice(i + 1))
      break
    }
    if (w.startsWith('--')) {
      const name = w.split('=')[0]!
      if (/^--(force|force-with-lease|force-if-includes)$/.test(name)) return { rule: 'push-force', why: 'в git push есть ключ принудительной отправки' }
      if (/^--(mirror|all|branches|delete|prune)$/.test(name)) return { rule: 'push-force', why: `в git push есть ключ ${name}` }
      if (PUSH_VALUE_OPTIONS.has(w)) i++
      continue
    }
    if (w.startsWith('-') && w.length > 1) {
      if (/^-[a-zA-Z]*f/.test(w) && !w.startsWith('-o')) return { rule: 'push-force', why: 'в git push есть ключ -f (принудительная отправка)' }
      if (/^-[a-zA-Z]*d/.test(w) && !w.startsWith('-o')) return { rule: 'push-force', why: 'в git push есть ключ -d (удаление ветки)' }
      if (w === '-o') i++
      continue
    }
    positionals.push(git.args[i]!)
  }

  const refspecs = positionals.slice(1)
  let needCurrent = refspecs.length === 0
  for (const spec of refspecs) {
    let text = spec.text
    if (text.startsWith('+')) return { rule: 'push-force', why: 'в git push ветка с «+» (принудительная перезапись)' }
    if (text.startsWith(':')) return { rule: 'push-force', why: 'git push с «:ветка» удаляет ветку на GitHub' }
    if (text.includes('*')) return { rule: 'push-force', why: 'git push с шаблоном «*» может задеть main' }
    if (spec.dynamic) {
      needCurrent = true
      continue
    }
    const colon = text.lastIndexOf(':')
    const src = colon === -1 ? text : text.slice(0, colon)
    let dst = colon === -1 ? text : text.slice(colon + 1)
    if (dst === '') dst = src
    if (colon === -1 && (src === 'HEAD' || src === '@')) {
      needCurrent = true
      continue
    }
    dst = dst.replace(/^refs\/heads\//, '')
    if (PROTECTED_BRANCH.test(dst)) return { rule: 'push-main', why: `в git push цель — ветка ${dst}` }
  }
  if (!needCurrent) return null

  if (git.dir === null) return { rule: 'push-branch-unknown', why: 'не удалось понять, в какой папке выполняется git push' }
  const branch = await env.branchOf(git.dir)
  if (branch === null) return { rule: 'push-branch-unknown', why: 'не удалось определить текущую ветку' }
  if (PROTECTED_BRANCH.test(branch)) return { rule: 'push-main', why: `git push без ветки отправляет текущую ветку, а это ${branch}` }
  return null
}

// ───────────────────────── gh ─────────────────────────

const GH_API_VALUE_OPTIONS = new Set(['-X', '--method', '-H', '--header', '-f', '-F', '--field', '--raw-field', '--input', '-q', '--jq', '-t', '--template', '--hostname', '--cache', '-p', '--preview'])
const GH_API_WRITE_PATH = /\/merges(\/|$|\?)|\/pulls\/[^/]+\/merge(\/|$|\?)|\/git\/refs|\/contents\/|\/branches\/[^?]*\/protection/
const GH_GRAPHQL_MERGE = /mergePullRequest|enablePullRequestAutoMerge|updateRef|deleteRef|createCommitOnBranch|mergeBranch/

function checkGh(inv: Invocation): Violation | null {
  const positionals: string[] = []
  const a = inv.args
  for (let i = 0; i < a.length && positionals.length < 2; i++) {
    const w = a[i]!.text
    if (w === '-R' || w === '--repo') i++
    else if (!w.startsWith('-')) positionals.push(w)
  }
  const [group, action] = positionals
  if (group === 'pr' && action === 'merge') return { rule: 'pr-merge', why: 'команда gh pr merge' }
  if (group === 'repo' && action === 'sync') return { rule: 'gh-api-write', why: 'команда gh repo sync перезаписывает ветку' }
  if (group !== 'api') return null

  let method: string | null = null
  let hasFields = false
  let endpoint: string | null = null
  const all = a.map(w => w.text)
  const start = all.indexOf('api') + 1
  for (let i = start; i < all.length; i++) {
    const w = all[i]!
    if (w === '-X' || w === '--method') method = (all[++i] ?? '').toUpperCase()
    else if (/^-X./.test(w)) method = w.slice(2).toUpperCase()
    else if (w.startsWith('--method=')) method = w.slice(9).toUpperCase()
    else if (['-f', '-F', '--field', '--raw-field', '--input'].includes(w)) {
      hasFields = true
      i++
    } else if (/^--(field|raw-field|input)=/.test(w) || /^-[fF]./.test(w)) hasFields = true
    else if (GH_API_VALUE_OPTIONS.has(w)) i++
    else if (!w.startsWith('-') && endpoint === null) endpoint = w
  }
  // Без -X gh сам шлёт POST, если есть поля (-f/-F/--input) — gh api --help.
  const effective = method ?? (hasFields ? 'POST' : 'GET')
  const writes = effective !== 'GET' && effective !== 'HEAD'
  if (endpoint === 'graphql') {
    if (all.some(w => GH_GRAPHQL_MERGE.test(w))) return { rule: 'gh-api-write', why: 'в запросе gh api graphql — слияние или запись в ветку' }
    return null
  }
  if (writes && endpoint !== null && GH_API_WRITE_PATH.test(endpoint)) return { rule: 'gh-api-write', why: 'gh api с записью в ветки, файлы или слиянием' }
  return null
}

// ───────────────────────── Проверка одной команды ─────────────────────────

type Ctx = { env: Env; dir: string | null; depth: number }

function wordsText(inv: Invocation): string {
  return [...inv.cmd.words, ...inv.cmd.redirects].map(w => w.text).join(' ')
}

function legacyHost(text: string): string | null {
  for (const [re, name] of LEGACY_HOSTS) if (re.test(text)) return name
  return null
}

async function checkInvocation(inv: Invocation, ctx: Ctx): Promise<Violation | null> {
  const { env } = ctx
  const p = inv.program
  const args = inv.args.map(w => w.text)

  // Строка после bash -c / eval / find -exec проверяется как отдельная команда.
  if (SHELLS.has(p)) {
    const ci = args.findIndex(w => /^-[a-zA-Z]*c[a-zA-Z]*$/.test(w))
    if (ci !== -1 && args[ci + 1] !== undefined) {
      const v = await checkBash(args[ci + 1]!, { ...ctx, depth: ctx.depth + 1 })
      if (v) return v
    }
    for (const body of inv.cmd.heredocs) {
      const v = await checkBash(body, { ...ctx, depth: ctx.depth + 1 })
      if (v) return v
    }
  }
  if (p === 'eval' && args.length > 0) {
    const v = await checkBash(args.join(' '), { ...ctx, depth: ctx.depth + 1 })
    if (v) return v
  }
  if (p === 'find') {
    for (let i = 0; i < args.length; i++) {
      if (!/^-(exec|execdir|ok|okdir)$/.test(args[i]!)) continue
      const end = args.findIndex((w, j) => j > i && (w === ';' || w === '+'))
      const inner = inv.args.slice(i + 1, end === -1 ? undefined : end)
      const nested = invocationOf({ words: inner, redirects: [], heredocs: [], top: false })
      if (nested) {
        const v = await checkInvocation(nested, ctx)
        if (v) return v
      }
    }
  }

  // Запрет 3: вложенный Claude Code, его моды и настройки.
  if (inv.assignments.some(a => a.startsWith('CLAUDE_CONFIG_DIR=')) || (p === 'export' && args.some(a => a.startsWith('CLAUDE_CONFIG_DIR=')))) {
    return { rule: 'claude-nested', why: 'задаётся CLAUDE_CONFIG_DIR — другая папка настроек Claude Code' }
  }
  if (p === 'claude') {
    const flag = args.find(w => CLAUDE_NESTED_FLAGS.test(w))
    if (flag !== undefined) return { rule: 'claude-nested', why: `запуск claude с ключом ${flag.split('=')[0]}` }
    const pos = args.filter(w => !w.startsWith('-'))
    if ((pos[0] === 'plugin' || pos[0] === 'plugins') && pos[1] !== undefined) {
      if (CLAUDE_PLUGIN_DENY.has(pos[1])) return { rule: 'claude-plugin', why: `команда claude plugin ${pos[1]}` }
      if (pos[1] === 'marketplace' && pos[2] !== undefined && CLAUDE_MARKETPLACE_DENY.has(pos[2])) {
        return { rule: 'claude-plugin', why: `команда claude plugin marketplace ${pos[2]}` }
      }
    }
    if (pos[0] === 'config' && pos[1] !== undefined && /^(set|add|remove|unset)$/.test(pos[1])) {
      return { rule: 'claude-plugin', why: `команда claude config ${pos[1]}` }
    }
  }

  // Запрет 3: запись в защищённые пути.
  for (const r of inv.cmd.redirects) {
    if (protectedWord(r, env.home, ctx.dir)) return { rule: 'protected-path', why: 'перенаправление вывода в защищённый файл' }
  }
  const sedInPlace = p === 'sed' && args.some(w => /^(-i|--in-place)/.test(w) || /^-[a-zA-Z]*i/.test(w))
  const perlInPlace = p === 'perl' && args.some(w => /^-[a-zA-Z]*i/.test(w))
  if (WRITERS.has(p) || sedInPlace || perlInPlace) {
    if (inv.args.some(w => protectedWord(w, env.home, ctx.dir))) return { rule: 'protected-path', why: `команда ${p} с путём в защищённой папке` }
  }

  // Запрет 2: SSH и удалённый Docker.
  if (SSH_PROGRAMS.has(p)) return { rule: 'ssh', why: `программа ${p}` }
  if (p === 'rsync' && args.some(w => /^(-e|--rsh)(=|$)/.test(w) || /^[^/\s-][^/\s]*:/.test(w) || w.startsWith('rsync://'))) {
    return { rule: 'ssh', why: 'rsync на удалённый хост' }
  }
  if (p === 'docker' || p === 'docker-compose') {
    if (inv.assignments.some(a => a.startsWith('DOCKER_HOST='))) return { rule: 'docker-remote', why: 'docker с DOCKER_HOST' }
    if (args.some(w => /^(-H|--host|-c|--context)(=|$)/.test(w))) return { rule: 'docker-remote', why: 'docker с удалённым хостом или контекстом' }
    const pos = args.filter(w => !w.startsWith('-'))
    if (pos[0] === 'login' || pos[0] === 'push' || (pos[0] === 'context' && pos[1] === 'use')) {
      return { rule: 'docker-remote', why: `команда docker ${pos[0]}` }
    }
  }

  // Запрет 2: скрипты старой системы, которые пишут во внешние сервисы.
  if (LEGACY_SCRIPTS.has(p) || (INTERPRETERS.test(p) && args.some(w => LEGACY_SCRIPTS.has(basename(w))))) {
    return { rule: 'legacy-script', why: 'скрипт из списка «Скрипты с записью во внешние сервисы»' }
  }

  // Запрет 2: хосты и репозитории старой системы — для всех, кроме программ, которые только читают файлы.
  const git = p === 'git' ? gitCall(inv, env.home, ctx.dir) : null
  const sedReads = p === 'sed' && !sedInPlace
  const reader = READERS.has(p) || sedReads || (git !== null && !GIT_NETWORK.has(git.sub))
  if (!reader) {
    const text = wordsText(inv) + '\n' + inv.cmd.heredocs.join('\n') + '\n' + inv.assignments.join(' ')
    const host = legacyHost(text)
    if (host !== null) return { rule: 'legacy-host', why: `в команде ${p} упомянут хост старой системы (${host})` }
    const helpOnly = (p === 'gh' || p === 'git') && (args.includes('--help') || args.includes('-h'))
    if (LEGACY_REPO.test(text) && !helpOnly) return { rule: 'legacy-repo', why: `в команде ${p} упомянут старый репозиторий` }
  }

  // Запрет 1: main, force-push, слияние PR.
  if (git !== null) {
    if (git.dir !== null && isProtectedPath(git.dir, env.home) && !GIT_READ.has(git.sub)) {
      return { rule: 'protected-path', why: `git ${git.sub} в защищённой папке` }
    }
    if (git.sub === 'push') {
      const v = await checkPush(git, env)
      if (v) return v
    }
  }
  if (p === 'gh') {
    const v = checkGh(inv)
    if (v) return v
  }
  return null
}

export async function checkBash(command: string, ctx: Ctx): Promise<Violation | null> {
  if (ctx.depth > 5) return { rule: 'guard-failed', why: 'слишком глубокая вложенность команд' }
  let dir = ctx.dir
  for (const cmd of parseCommand(command)) {
    const inv = invocationOf(cmd)
    if (inv === null) continue
    const v = await checkInvocation(inv, { ...ctx, dir })
    if (v) return v
    if (cmd.top && (inv.program === 'cd' || inv.program === 'pushd')) {
      const target = inv.args.find(w => !w.text.startsWith('-'))
      if (target === undefined) dir = ctx.env.home
      else dir = target.dynamic ? null : resolvePath(target.text, ctx.env.home, dir)
    }
  }
  return null
}

// ───────────────────────── Вызов инструмента ─────────────────────────

export type ToolCall = { tool: string; [key: string]: unknown }

const MCP_GITHUB_WRITE = /merge_pull_request|push_files|create_or_update_file/i
const EDIT_TOOLS: Record<string, string> = { Edit: 'file_path', Write: 'file_path', NotebookEdit: 'notebook_path' }

async function checkEditPath(raw: string, env: Env): Promise<Violation | null> {
  const abs = resolvePath(raw, env.home, env.cwd)
  const candidates: string[] = abs === null ? [] : [abs]
  if (abs !== null) {
    const real = await env.realPath(abs)
    if (real !== undefined) candidates.push(real)
    else {
      // Нового файла ещё нет: смотрим, куда ведёт его папка.
      const slash = abs.lastIndexOf('/')
      const parent = await env.realPath(abs.slice(0, slash) || '/')
      if (parent !== undefined) candidates.push(`${parent}/${abs.slice(slash + 1)}`)
    }
  }
  if (SETTINGS_FILE.test(raw) || candidates.some(c => isProtectedPath(c, env.home))) {
    return { rule: 'protected-path', why: 'путь файла — в защищённой папке или это файл настроек Claude Code' }
  }
  return null
}

export async function checkCall(e: ToolCall, env: Env): Promise<Violation | null> {
  const tool = String(e.tool)
  if ((tool === 'Bash' || tool === 'Monitor') && typeof e.command === 'string') {
    return checkBash(e.command, { env, dir: env.cwd, depth: 0 })
  }
  const pathKey = EDIT_TOOLS[tool]
  if (pathKey !== undefined && typeof e[pathKey] === 'string') return checkEditPath(e[pathKey] as string, env)
  if (tool === 'WebFetch' && typeof e.url === 'string') {
    const host = legacyHost(e.url)
    if (host !== null) return { rule: 'legacy-host', why: `WebFetch на хост старой системы (${host})` }
    if (LEGACY_REPO.test(e.url)) return { rule: 'legacy-repo', why: 'WebFetch на старый репозиторий' }
  }
  if (tool.startsWith('mcp__') && MCP_GITHUB_WRITE.test(tool)) {
    return { rule: 'mcp-github-write', why: 'инструмент GitHub с записью в ветку или слиянием' }
  }
  return null
}

// ───────────────────────── Если мод сам упал ─────────────────────────

// Решение без вызовов $ — только по тексту вызова (ИТОГ PDL-11, раздел 3).
const DANGEROUS = /push|merge|ssh|scp|sftp|rsync|docker|dockhost|bitrix|sms-prosto|telegram|max\.ru|unifiedbot|site_graber|\.claude|claude-mods|\.multica|plugin|claude_config_dir|--bare|--safe-mode|--setting|_v30\d\.py|register_webhooks|bx_delete/i

export function looksDangerous(e: ToolCall): boolean {
  const tool = String(e.tool)
  if (tool.startsWith('mcp__')) return DANGEROUS.test(tool) || MCP_GITHUB_WRITE.test(tool)
  const fields = ['command', 'file_path', 'notebook_path', 'url']
  return fields.some(key => typeof e[key] === 'string' && DANGEROUS.test(e[key] as string))
}
