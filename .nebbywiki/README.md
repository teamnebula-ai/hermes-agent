# What is this directory?

This is a **nebby** wiki: documentation compiled from this repository's own source, kept current
automatically. A maintainer of this repo installed it on purpose.

Source and docs: https://github.com/teamnebula-ai/teamwiki

## For agents and humans reading the repo

Start at [quickstart.md](quickstart.md). It is the entry point and links to every section —
architecture, APIs, data models, workflows, operations. Pages cite the files they were derived from,
so you can jump straight to the real code.

Treat it as a fast map, not as authority. It is generated text and can lag the code it describes.
When the wiki and the source disagree, the source is right.

## Do not hand-edit these pages

Every `.md` file here is regenerated. Edits are silently overwritten on the next build. To fix
something the wiki gets wrong, fix the code, then run `nebby build`.

This README is the exception — builds never touch it.

## What each file is

| Path | What it is |
|------|------------|
| `quickstart.md` | entry point; read this first |
| `architecture/`, `api/`, `workflows/`, … | section pages, created only when there is something to say |
| `config.json` | nebby's settings for this repo (committed) |
| `.last-update.json` | the commit the wiki was last built from |
| `nebby.db` | local run history — gitignored, never shared |
| `.gitignore` | keeps the run history out of git |

A `post-commit` git hook in `.git/hooks/` rebuilds the wiki in the background after a commit. It
skips commits that only touched this directory, debounces to at most one build a minute, and never
blocks your commit.

## Commands

```sh
nebby build      # rebuild now (surgical after the first run — only stale pages change)
nebby status     # config and recent runs
nebby doctor     # is the wiki stale, or is something misconfigured?
nebby uninstall  # remove the pointer, the hook, and optionally this directory
```

If `nebby` is not on your PATH, install it: `npm i -g teamnebula-ai/teamwiki`. That also
installs the older `teamwiki` command, which is the same program under its previous name.

## Not want this?

`nebby uninstall` removes the instruction-file pointer and the git hook, and `--purge` deletes
this directory too. Nothing else in the repository is touched.
