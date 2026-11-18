# Trackers, code hosts and advisory registers

The runner's flow (`loop`, `delivery`, `review`, `screening`, `cleanup`, `secops`) never talks to YouTrack, GitLab or an advisory register directly. It talks to three interfaces, defined in [`runner/src/arceus_runner/integrations.py`](../runner/src/arceus_runner/integrations.py):

- **`IssueTracker`**: where tasks come from. The queue of tagged issues, who put the tag on, the issue's text and state, tags, comments, attachments, links and mentions.
- **`CodeHost`**: where the repositories live. The repositories of one group, merge requests, their discussion threads, CI runs and job logs, uploads, and the bot's git credentials. Git itself is not abstracted: the workspace is plain git repositories, cloned and pushed over HTTPS.
- **`AdvisoryRegister`**: for [advisory triage](secops.md) only, where a company keeps its open security advisories and where the verdicts on them may go. Built in: `files`, a folder of OSV records with an OpenVEX document for the verdicts, which needs no service. [Below](#adding-an-advisory-register) is how to plug in your own.

`config.yaml` picks the implementation of each with `type`:

```yaml
tracker:
  type: youtrack           # built in; or the path of your own module, e.g. acme_arceus.jira
  bot_user: agent-runner
  allowed_users: [j.doe]
  states: { queued: To Do, in_progress: In Progress, code_review: Code Review }
  url: https://youtrack.example.com     # from here on, keys of the type
  project: ACME
code_host:
  type: gitlab
  bot_user: acme-agent-bot
  git_author: { name: "ACME Agent", email: "acme-agent-bot@example.com" }
  url: https://gitlab.example.com
  group: clients/acme
```

The keys at the top of each section (`bot_user`, `allowed_users`, `tags`, `states`; `git_author`, `exclude_repos`, `branch_prefix`, `mr_label`) mean the same for every provider; the rest belong to the type. The advisory register is picked the same way, in `secops.register` ([secops.md](secops.md)).

## Adding a tracker or a code host

Jira, GitHub or anything else can be added without touching the runner's flow:

1. Write one module, using [`youtrack.py`](../runner/src/arceus_runner/youtrack.py) or [`gitlab.py`](../runner/src/arceus_runner/gitlab.py) as the example. It has three parts:
   - the config section: a subclass of `IssueTrackerConfig` or `CodeHostConfig` with your keys;
   - the adapter: a class with the methods of `IssueTracker` or `CodeHost`;
   - `TRACKER = TrackerType(config=..., build=...)` or `CODE_HOST = CodeHostType(...)`.
2. Register it. In this repository, add a line to `BUILTIN_TRACKERS` or `BUILTIN_CODE_HOSTS` in `integrations.py` and use its short name as `type`. Outside it, name your package in `RUNNER_EXTRA_PACKAGES` in `setup.conf` (below) and put its module path in `type`.
3. Run `arceus-runner check`. An unknown `type`, a missing key or a key the provider does not know is reported before anything starts.

What every implementation has to keep:

- **`trigger()` is the security boundary.** The runner starts work only when the person who put the `queue` tag on is in `allowed_users`. Return the author of that change as the tracker's own history records it, never the reporter, the assignee or the last editor. If the history does not say, return `None`: the issue is then not taken.
- **Requests go through `http.ServiceClient`.** It gives the service its own pause and backoff, and raises the errors the flow handles (`AuthError`, `NotFound`, `BadRequest`, `TransientError`).
- **Writes are safe to repeat.** The runner journals each write and repeats it after a crash. Its comments carry a marker it looks for before posting again, so `comments()` and `notes()` must return the bot's own comments with their text intact.
- **Ids are opaque.** Numbers or strings, the runner stores them and hands them back.

Some capabilities are optional. Without them the flow keeps working, with less:

| Capability | Without it |
|---|---|
| CI runs on the merge request (`MRStatus.ci_*`) | a red pipeline is never sent to the agent; people say it in a comment |
| Logs of failed jobs (`failed_jobs`) | the agent gets the link to the run only |
| Conflict detection (`MRStatus.conflict`) | `None` means "not known" and sends nothing; a conflict reaches the agent through people |
| Threaded replies | `reply()` posts a plain comment on the merge request instead |
| One thread by its id (`thread`) | a thread missing from the listing still gets the reply posted; a reply to a thread people deleted is retried until it goes through, and holds the task in publishing |
| Uploads to the code host (`supports_uploads`) | screenshots go to the ticket only |
| Attachments on the tracker (`supports_attachments`) | screenshots go to the merge requests only |
| Assignee (`assign_to_bot`) | nothing is assigned |
| Approval (`supports_approval`, `approve`, `revoke`; `code_host.approver`) | the review agent says what it found in a note and approves nothing |
| Links to a file at a commit (`file_url`) | a test run's report names the suspected file and commit without a link |

## Adding an advisory register

A company that keeps its advisories in a service of its own (a web app, a ticket queue, a database) writes one module for it, outside this repository: Arceus ships no client for any service's API. The module has the same three parts:

- the config section: a subclass of `AdvisoryRegisterConfig` with your keys (an address, a project key). A register that needs a secret sets `token_env` to the name of a variable in `secrets.env`; the runner reads it and hands it to `build`, and the agent never sees it;
- the adapter: a class with the members of `AdvisoryRegister`, below;
- `REGISTER = RegisterType(config=..., build=...)`, where `build(section, secret, http config, transport=None)` returns the adapter. `secops.register.type` is then the module's path, e.g. `acme_arceus.register`.

It is installed like a tracker or a code host of your own ([below](#installing-a-provider-of-your-own)).

What the adapter does, and nothing more; the runner keeps the rest (screening, the earlier verdicts, the send-once record, the report):

| Member | What it does |
|---|---|
| `open()` | every open advisory, each whole, as an `Advisory`: `id` (kept as given, a number or a string), `ref`, `title`, `text` (`None` when you could not get it: the run leaves that advisory out, and its report says your `problem`), `facts` (plain JSON the agent reads as it is), `identifiers`, `severity`, `exploited`, `changed` (moves when the content does) and `url`. Paging and separate reads of a text are yours. |
| `receives`, `max_evidence` | whether it takes verdicts at all (`False`: read-only, `post: true` is refused by `check`), and the longest evidence it takes, which the agent is told. |
| `send(advisory, repo, verdict, evidence)` | one verdict (`affected`, `fixed`, `not_affected`, `needs_review`) for one repository. `repo` is a `RepoAt`: its path under the group, the code host's group, its address on the code host and the commit read; name the project your own way from them. |
| `latest(advisory_id, repo)` | the verdict and evidence you hold as the repository's latest on the advisory, or `None`. Only used when the answer to a `send` was lost, so it is never sent twice. |
| `check(scope, post)` | for `arceus-runner check`: you can be reached, the credentials are good for reading (and for receiving, with `post`), and you cover the project (`scope` is the code host's group). `arceus-runner run` never calls it. |
| `close()` | the runner stops: close what you hold open. |

Errors are `http.py`'s, as for the other two. A refusal of a verdict is `BadRequest` or `NotFound` with your register's own words in `body`: the report shows them. No answer is `TransientError`: reading is tried again for half an hour, and any other error ends the run there, saying why. While sending, every error but a refusal is tried again for an hour; then the report says which verdicts did not go. `ServiceClient` gives you the pause and backoff. The in-memory register the tests use, [`runner/tests/memory_providers.py`](../runner/tests/memory_providers.py), is a small example, and [`advisory_files.py`](../runner/src/arceus_runner/advisory_files.py) a complete one.

## Installing a provider of your own

Both the runner and the web console read `config.yaml`, so both must import every module it names in `type`: the runner in `/opt/arceus-runner/venv`, the console in its own Docker image. A module of a single install goes in `setup.conf`, which is that install's own file and not part of this repository:

```sh
RUNNER_EXTRA_PACKAGES="git+https://git.example.com/acme/acme-arceus@4f2c9e1"
```

Pip requirements separated by spaces: a git URL pinned to a commit or tag, or a folder on the machine. `sudo ./setup.sh` builds each into a wheel in `/opt/arceus-runner/extra`, installs it into the runner's virtualenv and the same file into the console's image, and uninstalls one that leaves the list. The wheels are built without their dependencies: a module uses what the runner has (`httpx`, `pydantic`, `PyYAML`) and declares none, `arceus-runner` included. A changed list, or a changed file in a listed folder, installs again and rebuilds the image; a URL that names a branch is not followed, so pin it and change the pin to update. Then `arceus-runner check` and a restart of the runner.

Installed by hand with `pip`, a module is gone the next time setup makes the virtualenv or the image again, and with it the runner (an unknown `type` stops it) and the console's pages.

