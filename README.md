# PR Stuns

An AI code reviewer that watches your pull requests and comments on them.

PR Stuns polls the repositories you list, sends each changed file to an LLM
(Claude directly, or any model on OpenRouter), and posts the findings back on
the pull request. It runs on your own machine or server with your own API
keys. There is no hosted service and nothing to pay us: the code is MIT
licensed and free to use, change and redistribute.

It works with **Bitbucket Cloud**, **GitHub** and **GitLab**.

## How it works

Each run does the following for every repository in `config.yaml`:

1. **Lists open pull requests** (up to `max_prs`).
2. **Decides whether each PR needs a review.**
   - A PR it has never seen is always reviewed.
   - A PR it has already reviewed is only reviewed again when a commit
     message contains the trigger text, `(REVIEW)` by default. This keeps the
     bot quiet while a developer is still pushing work-in-progress commits.
3. **Gathers context**: PR title and description, the list of changed files,
   the diff and (size-capped) content of each file, earlier review comments
   and the developers' replies, and, if configured, the linked Jira or Goby
   ticket.
4. **Reviews each changed file** with the LLM, skipping binaries, lock files,
   generated code and anything matching your exclusion rules.
5. **Posts comments** on the PR: findings are attached to the relevant file
   and line where the model points at one, alongside one summary comment
   listing the files that need attention. If nothing was found, the summary
   simply says so.
6. **Records the PR as processed** in `processed_prs/` so it is not reviewed
   twice.

Things it does to stay useful rather than noisy:

- **No duplicate comments.** New findings are compared against comments
  already on the PR and near-identical ones are dropped.
- **Conversation aware.** Issues a developer has replied to as fixed, or that
  the platform marks resolved, are not raised again.
- **Ticket aware.** With Jira or Goby configured, the ticket is included in
  the prompt and the model is asked to flag a diff that clearly does not do
  what the ticket asks.
- **Flow diagram.** Optionally adds a flowchart of the changed code to the
  summary comment, as an image.
- **Clean files are skipped.** A file the model has no remarks on gets no
  comment.

## Quick start

Requires Python 3.8 or newer.

```bash
git clone https://github.com/matogen/pr-stuns.git
cd pr-stuns
python3 -m venv venv
source venv/bin/activate
pip install -e .

cp config_sample.yaml config.yaml    # which repos to review, which model to use
cp users_sample.yml users.yml        # credentials
```

Edit both files (see below), check your credentials, then do a single run:

```bash
python test_credentials.py    # verifies the Bitbucket and GitHub tokens in users.yml
pr-reviewer --one-shot
```

`config.yaml` and `users.yml` are gitignored. Keep it that way: they hold
your tokens.

### Try it without an API key

Set `model_name: dummy` under `llm:` (see `config_test.yaml`). The dummy
model returns a canned response, so you can confirm the bot can read your PRs
and post comments before spending anything on a real model. Point it at a
test repository: it does post that canned response.

## Configuration

### Repositories (`config.yaml`)

```yaml
repositories:
  - type: bitbucket
    workspace: your-workspace
    repo_slug: your-repo
    username: review-bot          # name of an entry in users.yml
    max_prs: 5
    pr_state: OPEN

  - type: github
    owner: your-org
    repo: your-repo
    user: my-org                  # name of an entry in users.yml
    max_prs: 5

  - type: gitlab
    url: https://gitlab.example.com
    project_id: 12345
    token: your-gitlab-token      # GitLab tokens are set inline
    max_prs: 3
```

Useful per-repository options (all optional):

| Option | What it does |
| --- | --- |
| `exclude_patterns` | Skip files whose path contains any of these strings |
| `exclude_extensions` | Skip files ending with any of these extensions |
| `exclude_regex` | Skip files whose path matches any of these regexes |
| `commit_filtering.review_trigger` | Commit-message text that requests a re-review (default `(REVIEW)`) |
| `conversation_tracking` | Tune how earlier comments and replies are taken into account |
| `remove_watcher` | Stop the bot account from watching PRs it comments on (default `true`) |
| `jira_instance` / `goby_instance` | Which ticket system entry in `users.yml` this repo uses |

`config_sample.yaml` shows every option with comments.

### Credentials (`users.yml`)

```yaml
users:
  review-bot:
    bitbucket:
      email: bot@example.com
      api_token: your-atlassian-api-token
  my-org:
    github:
      token: your-github-token
```

A repository entry refers to a user by name, so one token can serve many
repositories. Credentials written inline in `config.yaml` still work and take
precedence; `python migrate_secrets.py` moves inline secrets into `users.yml`
for you.

Use a dedicated bot account. Comments are posted under the account whose
token you supply.

**Bitbucket token scopes.** Create the token at
<https://id.atlassian.com/manage-profile/security/api-tokens> with
**Create API token with scopes** and select:

- `read:user:bitbucket`
- `read:repository:bitbucket`
- `read:pullrequest:bitbucket`
- `write:pullrequest:bitbucket`

A token created without scopes will authenticate but get a 401 on every
endpoint. The `email` field is required with an API token.

**GitHub and GitLab.** The token needs to read the repository and its pull
requests and to write pull request comments.

### Model (`config.yaml`)

Claude, directly through the Anthropic API:

```yaml
llm:
  model_name: claude-opus-5-5
  api_key: your-anthropic-api-key     # or set ANTHROPIC_API_KEY
  max_tokens: 8000
  system_prompt: |
    You are an expert code reviewer...
```

Any model on OpenRouter:

```yaml
llm:
  provider: openrouter
  model_name: x-ai/grok-4.7
  api_key: your-openrouter-api-key    # or set OPENROUTER_API_KEY
  max_tokens: 8000
```

The `system_prompt` is where you set the tone and priorities of the review.
The one in `config_sample.yaml` is tuned for few, high-value comments.

Your code is sent to whichever provider you configure here. Check that this
is acceptable for the repositories you point the bot at.

### Ticket context (optional)

The ticket key is read from the PR branch name, so `feature/ops-42-login`
links to ticket `OPS-42`. Add the ticket system to `users.yml`:

```yaml
jira:
  default:
    base_url: 'https://your-org.atlassian.net'
    email: 'bot@example.com'
    api_token: 'your-jira-api-token'

goby:
  default:
    base_url: 'https://your-team.goby.chat'
    api_key: 'gk_<publicId>_<secret>'   # scope: tasks:read
```

You can define several named instances and choose one per repository with
`jira_instance:` or `goby_instance:`. Goby is tried first, then Jira. A
missing key, unknown ticket or API error just means the review runs without
ticket context.

### Flow diagram (optional)

PR Stuns can add a flowchart of the changed code to its summary comment, so a
reviewer sees how control flows through the change before reading the diff.
It is off by default and costs one extra LLM call per reviewed PR.

```yaml
flow_diagram:
  enabled: true
  mode: image                  # or: mermaid
  renderer_url: https://mermaid.ink
  max_nodes: 25
```

- `mode: image` posts the diagram as a picture. It works on Bitbucket, GitHub
  and GitLab. Nothing is uploaded to your git host: the diagram text is
  encoded into the image URL and drawn by the renderer when someone opens the
  PR.
- `mode: mermaid` posts a `mermaid` code block, which GitHub and GitLab draw
  themselves. Bitbucket Cloud shows it as plain text, so use `image` there.

A repository entry can switch it on or off for itself with
`flow_diagram: true` / `false`, or override individual keys.

**Privacy.** In image mode the diagram (function names and the shape of your
logic, not the source code) is sent to `renderer_url`, which defaults to the
public mermaid.ink service. For private code, run your own
[mermaid.ink](https://github.com/jihchi/mermaid.ink) instance and set
`renderer_url` to it, or use `mode: mermaid`.

No diagram is posted when the change has no real control flow to draw (docs,
config, renames), when the model's answer is not a valid flowchart, or when
the diagram is too large to fit in an image URL.

### Comment template

```yaml
comment_template: |
  ### 🤖 AI Code Review

  {remarks}

  _This comment was generated automatically by an AI code review tool._
```

## Running it

```bash
pr-reviewer --one-shot            # review once and exit
pr-reviewer --monitor             # keep running, using the monitoring: block in config.yaml
pr-reviewer --monitor -i 15       # same, checking every 15 minutes
pr-reviewer --continuous          # keep running, checking every minute
pr-reviewer --config other.yaml   # use a different config file
```

The `monitoring:` block in `config.yaml` sets the default interval, how often
old tracking entries are cleared, and an optional maximum number of runs.

### As a background service

`run_continuous.py` reviews every 10 minutes and shuts down cleanly on
SIGTERM. `watchdog.sh` starts, stops and restarts it and is safe to call from
cron:

```bash
./watchdog.sh start      # start if not already running
./watchdog.sh status
./watchdog.sh restart    # pick up new code or config
./watchdog.sh stop
```

```cron
0 * * * * /path/to/pr-stuns/watchdog.sh
@reboot   /path/to/pr-stuns/watchdog.sh
```

The watchdog expects the virtualenv at `venv/` inside the project folder.
Logs go to `pr_reviewer.log` (rotated) and `watchdog.log`.

### Managing what has been reviewed

```bash
pr-reviewer --list-prs            # PRs recorded as reviewed
pr-reviewer --list-repo-ids       # tracking file used for each repository
pr-reviewer --reset-repo my-repo  # forget one repository's PRs
pr-reviewer --reset-all-repos     # forget every repository's PRs
pr-reviewer --clean               # delete all tracking; everything is reviewed again
```

## For developers on a reviewed repository

- Open a PR and the bot reviews it on its next run.
- To ask for another look after pushing fixes, include `(REVIEW)` in a commit
  message.
- Reply to a comment to say it is fixed or not applicable and the bot will
  not raise it again.

## Project layout

```
pr_reviewer/
  __main__.py     command-line entry point
  core/           the review loop, prompts, de-duplication, PR tracking, flow diagram
  clients/        Bitbucket, GitHub, GitLab, Jira and Goby API clients
  llm/            Claude, OpenRouter and dummy model backends
  config/         config.yaml and users.yml loading
  context/        extra repository context for prompts
tests/            unit tests
```

## Development

```bash
pip install -e ".[dev]"
pytest
```

Issues and pull requests are welcome.

## Licence

MIT. See [LICENSE](LICENSE). Provided as is, with no warranty: review
comments come from a language model and can be wrong, so treat them as a
second opinion and not as a gate.
