# Anonymity

This project is published without an author, and the public page is generated
from a live private registry. Both facts create obligations. They are listed
here so a fork can keep them, or knowingly drop them.

## What the repository must never contain

- Hostnames, IP addresses, domains of the estate being run
- Project, product, company or personal names
- E-mail addresses of any kind
- Tokens, keys, `.env` files, database files or snapshots
- Absolute paths containing a username (`/Users/…`, `/home/…`)
- Screenshots with a browser chrome, a terminal prompt or a window title

`.gitignore` blocks the file classes. The leak guard blocks the string classes.
Neither blocks a careless commit message.

## The leak guard

`tools/redact.py` runs on every publish and in CI on every push. It is
**structural**, not name-based: it matches the *shape* of an identifier rather
than a list of known secrets, because a list of known secrets committed to a
public repository is itself the disclosure.

Patterns rejected:

| Class | Example shape |
|---|---|
| e-mail | `local@domain.tld` |
| IPv4 literal | `203.0.113.7` |
| bearer / hex secret | 32+ hex or base64url characters in one run |
| private key | `-----BEGIN … PRIVATE KEY-----` |
| home path | `/Users/<name>`, `/home/<name>` |
| domain | anything matching `x.tld` outside `ALLOWED_DOMAINS` |
| store / vendor ids | 9–12 digit bare numeric ids in prose |

The guard **fails closed**: on any match, the publisher writes nothing, pushes
nothing, and exits non-zero naming the pattern and the line.

Add estate-specific strings — your real project names, your host — in a
denylist file kept **outside** the repository:

```bash
python3 tools/publish.py --db … --denylist /etc/agent-estate/denylist.txt
```

## The projection

The public page never renders a record directly. `tools/redact.py` builds a
*projection*: an allowlist of fields per table, with

- project names replaced by stable pseudonyms (`holding-A`, `holding-B`, …),
- agent names replaced by `agent-01`, `agent-02`, …,
- model names generalised to a class (`frontier, reasoning` / `mid-tier, fast`),
- free text truncated to its shape, with identifiers struck through,
- counts, severities, confidences, statuses and measured values **unaltered**.

The pseudonym map is derived from a salted hash of the real name, with the salt
outside the repository. Same estate, same pseudonyms across publishes; a
different estate produces different letters. Nothing in the published output
reverses to a name.

## Git hygiene

Commit metadata leaks more than most people expect — a real name, a work
e-mail, and a timezone that narrows a country from every timestamp.

```bash
git config user.name  "agent-estate"
git config user.email "agent-estate@users.noreply.github.com"
git config commit.gpgsign false
```

Commit with a fixed timezone so the pattern of your working hours does not
become a location:

```bash
TZ=UTC git commit -m "…"
```

The publisher does this for you when it commits.

## The account is the weak point

The repository host knows who created the account regardless of what the commits
say. A repository living under a personal namespace is not anonymous, whatever
its contents. If anonymity matters:

- a fresh account with a fresh e-mail, or an organisation whose member list is
  set to private;
- no forks or stars from the personal account;
- no cross-posting from a personal handle;
- consider that a domain's WHOIS, a TLS certificate's history and a server's
  reverse DNS all outlive a rename.

None of this defeats a determined observer with subpoena power. It defeats
casual correlation, which is the actual threat here.
