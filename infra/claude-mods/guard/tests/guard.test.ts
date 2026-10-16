import type { On } from 'claude-code'
import { describe, expect, mock, test } from 'claude-code/testing'

// Хуки теста стоят под модом и отвечают за систему: окружение, папку сессии, git, диск
// и сам инструмент. Настоящий Bash, git и диск не трогаются.

const HOME = '/Users/test'
const CWD = '/Users/test/multica_workspaces/demo/workdir/dealer-platform'
const LOG_DIR = `${HOME}/Library/Logs/claude-guard`
const NOW = Date.UTC(2026, 9, 16, 12, 0, 0)
const FAKE_TOKEN = 'fake-token-0000-not-a-secret'

type World = { files: Map<string, string>; calls: unknown[]; gitRuns: { argv: readonly string[]; cwd?: string }[] }

type Options = {
  // Текущая ветка по папке; null — git не смог её назвать.
  branches?: Record<string, string | null>
  // Ссылки: путь → куда ведёт.
  links?: Record<string, string>
  failStat?: boolean
  failGit?: boolean
}

function world(on: On, session: string, options: Options = {}): World {
  const files = new Map<string, string>()
  const calls: unknown[] = []
  const gitRuns: World['gitRuns'] = []
  mock.env(on, { HOME })
  mock.clock(on, { now: NOW })
  on('session.id', () => ({ value: session }))
  on('session.cwd', () => ({ value: CWD }))
  on('fs.exists', ($, e) => ({ value: files.has(e.path) }))
  on('fs.read', ($, e) => ({ value: files.get(e.path) ?? '' }))
  on('fs.write', ($, e) => {
    files.set(e.path, e.text)
    return { value: undefined }
  })
  on('fs.stat', ($, e) => {
    if (options.failStat) throw new Error('stat failed')
    const links = options.links ?? {}
    for (const [from, to] of Object.entries(links)) {
      if (e.path === from || e.path.startsWith(from + '/')) {
        return { value: { kind: 'dir', size: 0, mtimeMs: 0, isLink: true, realPath: to + e.path.slice(from.length) } }
      }
    }
    return { value: { kind: 'file', size: 0, mtimeMs: 0, isLink: false, realPath: e.path } }
  })
  on('process.run', ($, e) => {
    gitRuns.push({ argv: e.argv, cwd: e.init?.cwd })
    if (options.failGit) throw new Error('git failed')
    const branches = options.branches ?? {}
    const dir = e.init?.cwd ?? CWD
    const branch = dir in branches ? branches[dir] : 'agent/agent/abc'
    return {
      value: branch === null
        ? { exitCode: 128, stdout: '', stderr: 'fatal: ref HEAD is not a symbolic ref' }
        : { exitCode: 0, stdout: `${branch}\n`, stderr: '' },
    }
  })
  on('tool.call', ($, e) => {
    calls.push(e)
    return { result: { stdout: 'done', stderr: '', interrupted: false } }
  })
  return { files, calls, gitRuns }
}

type Call = { tool: string; [key: string]: unknown }

const bash = (command: string): Call => ({ tool: 'Bash', command })

// Текст запрета: от плагина — deny, от сессии — text c isError.
function denial(ran: { deny?: string; text?: string; isError?: boolean }): string | undefined {
  if (ran.deny !== undefined) return ran.deny
  if (ran.isError === true && typeof ran.text === 'string' && ran.text.includes('guard:')) return ran.text
  return undefined
}

function logRules(files: Map<string, string>, session: string): string[] {
  const text = files.get(`${LOG_DIR}/${session}.jsonl`) ?? ''
  return text.trim().split('\n').filter(Boolean).map(line => JSON.parse(line).rule)
}

// ───────────── Должно запрещаться ─────────────

const DENIED: [string, Call, string, Options?][] = [
  // Запрет 1: main, force-push, слияние PR.
  ['push в main', bash('git push origin main'), 'push-main'],
  ['push HEAD:main', bash('git push origin HEAD:main'), 'push-main'],
  ['push ветки в refs/heads/main', bash('git push origin agent/x:refs/heads/main'), 'push-main'],
  ['push в master', bash('git push upstream master'), 'push-main'],
  ['push по URL в main', bash('git push https://github.com/Vitaalii/dealer-platform.git feature:main'), 'push-main'],
  ['push через git -C', bash('git -C /tmp/repo push origin main'), 'push-main'],
  ['push через git -c', bash('git -c core.askPass=true push origin main'), 'push-main'],
  ['push с путём к git и присваиванием', bash('FOO=1 /usr/bin/git push origin main'), 'push-main'],
  ['push внутри bash -c', bash('bash -c "git push origin main"'), 'push-main'],
  ['push внутри sh -lc', bash("sh -lc 'cd x && git push origin main'"), 'push-main'],
  ['push внутри eval', bash('eval git push origin main'), 'push-main'],
  ['push внутри $(…)', bash('echo $(git push origin main)'), 'push-main'],
  ['push после ; и в цепочке', bash('git add . ; git commit -m "x" && git push origin main'), 'push-main'],
  ['push без ветки, текущая — main', bash('git push'), 'push-main', { branches: { [CWD]: 'main' } }],
  ['push origin HEAD, текущая — main', bash('git push -u origin HEAD'), 'push-main', { branches: { [CWD]: 'main' } }],
  ['cd в репозиторий на main и push', bash('cd /tmp/other && git push'), 'push-main', { branches: { '/tmp/other': 'main' } }],
  ['push без ветки, ветку не узнать', bash('git push origin'), 'push-branch-unknown', { branches: { [CWD]: null } }],
  ['push из папки $VAR', bash('cd "$REPO" && git push'), 'push-branch-unknown'],
  ['push --force', bash('git push --force origin agent/x'), 'push-force'],
  ['push -f', bash('git push -f origin agent/x'), 'push-force'],
  ['push -uf', bash('git push -uf origin agent/x'), 'push-force'],
  ['push --force-with-lease', bash('git push --force-with-lease=agent/x origin agent/x'), 'push-force'],
  ['push с плюсом', bash('git push origin +agent/x'), 'push-force'],
  ['push удаление через :ветка', bash('git push origin :agent/x'), 'push-force'],
  ['push --delete', bash('git push origin --delete agent/x'), 'push-force'],
  ['push --mirror', bash('git push --mirror origin'), 'push-force'],
  ['push --all', bash('git push --all origin'), 'push-force'],
  ['push через Monitor', { tool: 'Monitor', command: 'git push origin main', description: 'x' }, 'push-main'],
  ['find -exec git push', bash('find . -maxdepth 0 -exec git push origin main \\;'), 'push-main'],
  ['xargs git push', bash('echo main | xargs git push origin'), 'push-branch-unknown', { branches: { [CWD]: null } }],
  ['gh pr merge', bash('gh pr merge 12 --squash'), 'pr-merge'],
  ['gh pr merge --auto', bash('gh pr merge --auto --merge'), 'pr-merge'],
  ['gh -R … pr merge', bash('gh -R Vitaalii/dealer-platform pr merge 3'), 'pr-merge'],
  ['gh repo sync', bash('gh repo sync'), 'gh-api-write'],
  ['gh api PUT …/merge', bash('gh api -X PUT repos/Vitaalii/dealer-platform/pulls/3/merge'), 'gh-api-write'],
  ['gh api POST merges', bash('gh api --method=POST repos/o/r/merges -f base=main -f head=x'), 'gh-api-write'],
  ['gh api -f на git/refs (POST сам)', bash('gh api repos/o/r/git/refs/heads/main -f sha=abc'), 'gh-api-write'],
  ['gh api запись файла', bash('gh api -XPUT repos/o/r/contents/README.md --input body.json'), 'gh-api-write'],
  ['gh api graphql mergePullRequest', bash(`gh api graphql -f query='mutation { mergePullRequest(input: {pullRequestId: "x"}) { clientMutationId } }'`), 'gh-api-write'],
  ['MCP merge_pull_request', { tool: 'mcp__github__merge_pull_request', owner: 'o', repo: 'r', pullNumber: 1 }, 'mcp-github-write'],
  ['MCP push_files', { tool: 'mcp__github__push_files', branch: 'main' }, 'mcp-github-write'],

  // Запрет 2: старая система и SSH.
  ['ssh', bash('ssh root@example.invalid uptime'), 'ssh'],
  ['scp', bash('scp file host:/tmp/'), 'ssh'],
  ['ssh через путь и env', bash('env LANG=C /usr/bin/ssh host'), 'ssh'],
  ['sshpass', bash('sshpass -p x ssh host'), 'ssh'],
  ['rsync на хост', bash('rsync -av ./dist host:/srv/www'), 'ssh'],
  ['rsync -e ssh', bash('rsync -e ssh ./a ./b'), 'ssh'],
  ['docker -H', bash('docker -H tcp://10.0.0.1:2375 ps'), 'docker-remote'],
  ['DOCKER_HOST=…', bash('DOCKER_HOST=ssh://host docker ps'), 'docker-remote'],
  ['docker push', bash('docker push someone/image:latest'), 'docker-remote'],
  ['docker login', bash('docker login'), 'docker-remote'],
  ['curl к dockhost', bash('curl -s https://obut-5oaj-5i2u.gw-1a.dockhost.net/webhook'), 'legacy-host'],
  ['wget к Битриксу', bash('wget https://example.bitrix24.ru/rest/'), 'legacy-host'],
  ['ProstoSMS', bash('curl https://api.sms-prosto.ru/'), 'legacy-host'],
  ['Telegram API', bash('curl https://api.telegram.org/bot/getMe'), 'legacy-host'],
  ['MAX API', bash('curl -X POST https://platform-api.max.ru/subscriptions'), 'legacy-host'],
  ['Docker Hub', bash('curl https://hub.docker.com/v2/repositories/'), 'legacy-host'],
  ['python с адресом в heredoc', bash("python3 - <<'EOF'\nimport urllib.request\nurllib.request.urlopen('https://x.dockhost.net')\nEOF"), 'legacy-host'],
  ['WebFetch на dockhost', { tool: 'WebFetch', url: 'https://mgak-wacd-ospe.gw-1a.dockhost.net/', prompt: 'x' }, 'legacy-host'],
  ['git clone старого репозитория', bash('git clone https://github.com/Vitaalii/unifiedbot'), 'legacy-repo'],
  ['git clone unifiedbot-n8n по SSH', bash('git clone git@github.com:Vitaalii/unifiedbot-n8n.git'), 'legacy-repo'],
  ['gh repo clone site_graber', bash('gh repo clone Vitaalii/site_graber'), 'legacy-repo'],
  ['gh api к старому репозиторию', bash('gh api repos/Vitaalii/unifiedbot/contents/README.md'), 'legacy-repo'],
  ['WebFetch старого репозитория', { tool: 'WebFetch', url: 'https://github.com/Vitaalii/unifiedbot', prompt: 'x' }, 'legacy-repo'],
  ['скрипт бота с записью', bash('python3 bot/scripts/register_webhooks.py --apply'), 'legacy-script'],
  ['скрипт NocoDB из папки', bash('cd n8n/nocodb && python push_tables.py'), 'legacy-script'],
  ['скрипт n8n напрямую', bash('./n8n/flow/cabinet-web-entry/api/run_combo_once.py'), 'legacy-script'],
  ['скрипт через uv run', bash('uv run python n8n/nocodb/debrand_testruns.py'), 'legacy-script'],

  // Запрет 3: сам мод, ~/.claude, ~/.multica, настройки.
  ['Write в ~/.claude/settings.json', { tool: 'Write', file_path: `${HOME}/.claude/settings.json`, content: '{}' }, 'protected-path'],
  ['Edit в ~/.claude/plugins', { tool: 'Edit', file_path: '~/.claude/plugins/installed_plugins.json', old_string: 'a', new_string: 'b' }, 'protected-path'],
  ['Write в ~/claude-mods', { tool: 'Write', file_path: `${HOME}/claude-mods/dealer-platform/infra/claude-mods/guard/hooks/register.ts`, content: '' }, 'protected-path'],
  ['Write в ~/.multica', { tool: 'Write', file_path: `${HOME}/.multica/config.json`, content: '{}' }, 'protected-path'],
  ['Write в .claude/settings.local.json проекта', { tool: 'Write', file_path: `${CWD}/.claude/settings.local.json`, content: '{}' }, 'protected-path'],
  ['Write по ссылке в ~/.claude', { tool: 'Write', file_path: `${CWD}/innocent/settings.json`, content: '{}' }, 'protected-path', { links: { [`${CWD}/innocent`]: `${HOME}/.claude` } }],
  ['Write с ../ в ~/.claude', { tool: 'Write', file_path: `${CWD}/../../../../.claude/CLAUDE.md`, content: '' }, 'protected-path'],
  ['NotebookEdit в ~/.claude', { tool: 'NotebookEdit', notebook_path: `${HOME}/.claude/x.ipynb`, new_source: '' }, 'protected-path'],
  ['rm в ~/.claude', bash('rm -rf ~/.claude/plugins'), 'protected-path'],
  ['перенаправление в $HOME/.claude', bash('echo "{}" > $HOME/.claude/settings.json'), 'protected-path'],
  ['tee в настройки проекта', bash('echo x | tee .claude/settings.json'), 'protected-path'],
  ['sed -i в ~/.multica', bash("sed -i '' 's/a/b/' ~/.multica/config.json"), 'protected-path'],
  ['cp в /Users/test/claude-mods', bash('cp x.ts /Users/test/claude-mods/dealer-platform/x.ts'), 'protected-path'],
  ['git -C ~/claude-mods pull', bash('git -C ~/claude-mods/dealer-platform pull --ff-only'), 'protected-path'],
  ['cd ~/claude-mods && git checkout', bash('cd ~/claude-mods/dealer-platform && git checkout other'), 'protected-path'],
  ['claude plugin disable', bash('claude plugin disable guard@dealer-platform-guard --scope project'), 'claude-plugin'],
  ['claude plugin uninstall', bash('claude plugin uninstall guard@dealer-platform-guard'), 'claude-plugin'],
  ['claude plugin marketplace remove', bash('claude plugin marketplace remove dealer-platform-guard'), 'claude-plugin'],
  ['claude config set', bash('claude config set -g theme dark'), 'claude-plugin'],
  ['claude --bare', bash('claude --bare -p "сделай"'), 'claude-nested'],
  ['claude --safe-mode', bash('claude -p "x" --safe-mode'), 'claude-nested'],
  ['claude --setting-sources', bash('claude -p x --setting-sources=project'), 'claude-nested'],
  ['claude --plugin-dir', bash('claude --plugin-dir /tmp/empty -p x'), 'claude-nested'],
  ['CLAUDE_CONFIG_DIR=… claude', bash('CLAUDE_CONFIG_DIR=/tmp/c claude -p x'), 'claude-nested'],
]

// ───────────── Должно проходить ─────────────

const ALLOWED: [string, Call, Options?][] = [
  ['push своей ветки с -u', bash('git push -u origin agent/agent/abc')],
  ['push origin HEAD со своей ветки', bash('git push -u origin HEAD')],
  ['push без ветки со своей ветки', bash('git push')],
  ['push HEAD:своя-ветка', bash('git push origin HEAD:agent/agent/abc')],
  ['push с -o (не force)', bash('git push -o ci.skip origin agent/agent/abc')],
  ['ветка, коммит, push цепочкой', bash('git checkout -b agent/agent/abc && git add infra/ && git commit -m "PDL-12: мод guard" && git push -u origin agent/agent/abc')],
  ['main в сообщении коммита', bash('git commit -m "fix main menu: push to main запрещён"')],
  ['commit через heredoc', bash(`git commit -m "$(cat <<'EOF'\nPDL-12: мод guard (запрет push в main)\n\n1) тесты\nEOF\n)"`)],
  ['merge и rebase локально', bash('git fetch origin && git merge origin/main && git rebase origin/main')],
  ['git pull', bash('git pull --ff-only origin main')],
  ['git log/diff по main', bash('git log --oneline main..HEAD && git diff --name-only main...HEAD')],
  ['gh pr create в main', bash('gh pr create --base main --title "PDL-12: мод guard" --body-file pr.md')],
  ['gh pr view/list/diff/checks', bash('gh pr view 12 && gh pr list && gh pr diff 12 && gh pr checks 12')],
  ['gh pr comment', bash('gh pr comment 12 --body-file c.md')],
  ['gh api чтение', bash('gh api repos/Vitaalii/dealer-platform/pulls/3')],
  ['gh api чтение contents', bash('gh api repos/Vitaalii/dealer-platform/contents/README.md')],
  ['multica комментарий', bash('multica issue comment add 01a1 --content-file ./reply.md --output table && rm ./reply.md')],
  ['multica статус', bash('multica issue status 01a1 in_review')],
  ['multica с адресом в тексте', bash('multica issue get 01a1 --output json | grep dockhost')],
  ['чтение снимков с адресами', bash('grep -rn dockhost.net bot/ n8n/ | head -20')],
  ['cat снимка', bash('cat bot/SOURCE.md')],
  ['git grep по старому репозиторию', bash('git grep -n "Vitaalii/unifiedbot" -- docs/')],
  ['sed без -i', bash("sed -n '1,40p' landing/SOURCE.md")],
  ['pytest снимков', bash('cd bot && pytest tests -q')],
  ['node --test', bash('node --test landing/tests')],
  ['ssh-keygen', bash('ssh-keygen -l -f key.pub')],
  ['claude plugin validate', bash('claude plugin validate infra/claude-mods/guard')],
  ['claude plugin test', bash('claude plugin test infra/claude-mods/guard')],
  ['claude plugin list и версия', bash('claude plugin list && claude --version')],
  ['чтение ~/.claude', bash('ls ~/.claude && cat ~/.claude/settings.json')],
  ['Write в рабочую копию мода', { tool: 'Write', file_path: `${CWD}/infra/claude-mods/guard/hooks/register.ts`, content: '' }],
  ['Write в память агента', { tool: 'Write', file_path: `${HOME}/.claude/projects/x/memory/a.md`, content: 'note' }],
  ['Edit обычного файла', { tool: 'Edit', file_path: 'docs/handoff/x.md', old_string: 'a', new_string: 'b' }],
  ['Write в .claude/skills проекта', { tool: 'Write', file_path: `${CWD}/.claude/skills/x/SKILL.md`, content: '' }],
  ['Read ~/.claude', { tool: 'Read', file_path: `${HOME}/.claude/settings.json` }],
  ['WebFetch к GitHub', { tool: 'WebFetch', url: 'https://github.com/Vitaalii/dealer-platform', prompt: 'x' }],
  ['rm в рабочей папке', bash('rm -rf node_modules dist')],
  ['docker локально', bash('docker ps')],
  ['перенаправления 2>&1 и в файл', bash('npm test 2>&1 | tee test.log > /dev/null')],
  ['MCP-инструмент без записи', { tool: 'mcp__github__get_pull_request', owner: 'o', repo: 'r', pullNumber: 1 }],
]

describe('guard: должно запрещаться', () => {
  for (const [name, call, rule, options] of DENIED) {
    test(`${name} → ${rule}`, async ($, on) => {
      const w = world(on, 'deny', options)
      const ran = await $.tool.call(call as never)
      const text = denial(ran as never)
      expect(text).toBeDefined()
      expect(w.calls.length).toBe(0)
      expect(logRules(w.files, 'deny')).toEqual([rule])
    })
  }
})

describe('guard: должно проходить', () => {
  for (const [name, call, options] of ALLOWED) {
    test(name, async ($, on) => {
      const w = world(on, 'allow', options)
      const ran = await $.tool.call(call as never)
      expect(denial(ran as never)).toBeUndefined()
      expect(w.calls.length).toBe(1)
      expect(w.calls[0]).toEqual(expect.objectContaining(call))
      expect(w.files.size).toBe(0)
    })
  }
})

describe('guard: текст запрета и журнал', () => {
  test('текст запрета по шаблону и без текста команды', async ($, on) => {
    const w = world(on, 'text-1')
    const command = `GH_TOKEN=${FAKE_TOKEN} git push origin main`
    const text = denial((await $.tool.call(bash(command) as never)) as never) ?? ''

    expect(text.startsWith('guard: действие запрещено — отправка в main или master.')).toBe(true)
    expect(text.includes('Почему: ')).toBe(true)
    expect(text.includes('Правило: CLAUDE.md')).toBe(true)
    expect(text.includes('переведи её в blocked')).toBe(true)
    expect(text.includes(FAKE_TOKEN)).toBe(false)
    expect(text.includes(command)).toBe(false)
    expect(w.calls.length).toBe(0)
  })

  test('подсказка указать ветку, если её не узнать', async ($, on) => {
    world(on, 'hint-1', { failGit: true })
    const text = denial((await $.tool.call(bash('git push') as never)) as never) ?? ''
    expect(text.includes('git push -u origin <ветка задачи>')).toBe(true)
  })

  test('ветка спрашивается у git в папке из cd и -C', async ($, on) => {
    const w = world(on, 'dir-1')
    await $.tool.call(bash('cd sub && git push') as never)
    await $.tool.call(bash('git -C /tmp/r push') as never)
    expect(w.gitRuns.map(r => r.cwd)).toEqual([`${CWD}/sub`, '/tmp/r'])
    expect(w.gitRuns[0]?.argv).toEqual(['git', 'symbolic-ref', '--short', 'HEAD'])
  })

  test('журнал: только time, session, cwd, tool, rule', async ($, on) => {
    const w = world(on, 'log-1')
    await $.tool.call(bash(`curl -H "Authorization: Bearer ${FAKE_TOKEN}" https://x.dockhost.net`) as never)
    await $.tool.call({ tool: 'Write', file_path: `${HOME}/.claude/settings.json`, content: FAKE_TOKEN } as never)

    const raw = [...w.files.values()].join('')
    expect(raw.includes(FAKE_TOKEN)).toBe(false)
    expect(raw.includes('dockhost')).toBe(false)
    expect(raw.includes('settings.json')).toBe(false)
    const lines = raw.trim().split('\n').map(line => JSON.parse(line))
    expect(lines).toEqual([
      { time: new Date(NOW).toISOString(), session: 'log-1', cwd: CWD, tool: 'Bash', rule: 'legacy-host' },
      { time: new Date(NOW).toISOString(), session: 'log-1', cwd: CWD, tool: 'Write', rule: 'protected-path' },
    ])
  })

  test('без ответа fs.stat путь проверяется по написанию', async ($, on) => {
    const w = world(on, 'stat-1', { failStat: true })
    const denied = await $.tool.call({ tool: 'Write', file_path: `${HOME}/.claude/settings.json`, content: '' } as never)
    const allowed = await $.tool.call({ tool: 'Write', file_path: `${CWD}/README.md`, content: '' } as never)
    expect(denial(denied as never)).toBeDefined()
    expect(denial(allowed as never)).toBeUndefined()
    expect(w.calls.length).toBe(1)
  })
})

describe('guard: сторож падает', () => {
  // HOME не задан — хук бросает ошибку, решение принимает .catch по тексту вызова.
  function broken(on: On): unknown[] {
    const calls: unknown[] = []
    mock.env(on, {})
    on('session.cwd', () => ({ value: CWD }))
    on('tool.call', ($, e) => {
      calls.push(e)
      return { result: { stdout: 'done', stderr: '', interrupted: false } }
    })
    return calls
  }

  for (const command of ['git push origin agent/x', 'gh pr merge 1', 'ssh host', 'curl https://x.dockhost.net', 'cat ~/.claude/settings.json', 'claude plugin list']) {
    test(`опасное слово — запрет: ${command.split(' ')[0]}`, async ($, on) => {
      const calls = broken(on)
      const text = denial((await $.tool.call(bash(command) as never)) as never) ?? ''
      expect(text.includes('мод guard не смог проверить вызов')).toBe(true)
      expect(calls.length).toBe(0)
    })
  }

  for (const call of [bash('ls -la'), bash('pytest -q'), { tool: 'Read', file_path: `${CWD}/README.md` }, { tool: 'Write', file_path: `${CWD}/a.md`, content: 'push to main' }]) {
    test(`без опасных слов — проходит: ${String(call.tool)} ${String(call.command ?? '')}`, async ($, on) => {
      const calls = broken(on)
      const ran = await $.tool.call(call as never)
      expect(denial(ran as never)).toBeUndefined()
      expect(calls.length).toBe(1)
    })
  }
})
