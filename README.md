# WaOS apps

The official catalog of apps for [WaOS](https://github.com/waos-dev/waos), the source
`list.waos.app`. WaOS comes with it among its package sources; it can be removed like any other.

The catalog builds nothing. Each app is packaged and released by its own repository, signed with
its publisher's key. This repository only says which apps to list and where they come from, and an
hourly workflow collects their latest packages.

It is also the reference for running a source of your own: copy it, change the list, point it at
your bucket.

## What is here

- [`catalog.toml`](catalog.toml): the apps, each with its id, its source and its publisher's key.
- [`collect.py`](collect.py): collects each app's latest package and writes the catalog's index.
  Python's standard library only.
- [`.github/workflows/collect.yml`](.github/workflows/collect.yml): runs it every hour, on a change
  to either file, and by hand.

## How it works

A WaOS package source is a folder that holds an `index.json`, with the packages it lists beside
it:

```json
{
  "programs": [
    {
      "id": "dev.waos.inspector",
      "name": "Inspector",
      "version": "0.5.0",
      "package": "dev.waos.inspector/0.5.0/dev.waos.inspector-0.5.0.wapk",
      "sha256": "<the package file's SHA-256, 64 hex digits>"
    }
  ]
}
```

`package` is found relative to the index's URL. A website is a source by its address alone: WaOS
reads `list.waos.app` at `https://list.waos.app/.well-known/waos/index.json`, the folder where a
site says what it offers one kind of client (RFC 8615), so the catalog keeps everything it serves
there. A source named `github.com/<owner>/<repo>` is read from that repository's latest release,
at `https://github.com/<owner>/<repo>/releases/latest/download/index.json`.

On each run, for each app in `catalog.toml`, `collect.py`:

1. reads its source's index and finds the app's latest version there;
2. if that version is newer than the one the catalog lists, downloads the package and checks its
   SHA-256 against the source's index;
3. runs `waos package verify <file> --publisher <key>`, which opens the package as WaOS does and
   refuses it unless it is signed with the key `catalog.toml` names, and checks the package is the
   id and version the source said;
4. copies it to R2 at `.well-known/waos/<id>/<version>/<file>`. A package there is never
   changed.

Then it writes the catalog's `index.json`, listing each app's latest collected version, last of
all. An app whose source has no release yet, or does not list it, is skipped. A package that fails
a check keeps the app at its current listing and fails the run, so someone looks. When nothing is
new, the run stops after reading the sources, before it installs `waos`.

## Getting an app listed

1. Release your app as a source. In its repository, package it with your signing key:

   ```sh
   WAOS_SIGNING_KEY="$(cat my-key.pem)" waos package
   ```

   This writes `target/waos/<id>-<version>.wapk` and the `target/waos/index.json` that lists it.
   Attach both to a GitHub release; `github.com/<you>/<repo>` is then a source of your app. (A
   key is an Ed25519 private key in PEM, for example from
   `openssl genpkey -algorithm ed25519`. Keep it secret, and keep it: every update must be signed
   with it.)

2. Find your publisher key, the public half:

   ```sh
   waos package verify target/waos/<id>-<version>.wapk
   ```

   prints it as `publisher`, 64 hex digits.

3. Open a pull request adding your app to `catalog.toml`:

   ```toml
   [[app]]
   id = "com.example.myapp"
   source = "github.com/you/myapp"
   publisher = "<your publisher key>"
   ```

Once merged, the next run collects your latest release, and every later one as you release it.

## Running a catalog of your own

1. Copy this repository (fork it, or use it as a template).
2. Make a Cloudflare R2 bucket and serve it publicly at a domain of yours.
3. In `collect.py`, set `BUCKET` to the bucket's name and `SERVED_AT` to its public URL.
4. Give the repository two secrets: `CLOUDFLARE_API_TOKEN`, a token that can write to R2, and
   `CLOUDFLARE_ACCOUNT_ID`.
5. List your apps in `catalog.toml`, and enable Actions on the copy (a fork's scheduled workflows
   start off).

Your catalog is then the source `<your domain>`: type that into WaOS to add it.

To try it without R2, collect into a folder:

```sh
python3 collect.py --out catalog --waos <path to waos>
```

This writes the index and the packages to `catalog/.well-known/waos/`, as the bucket holds them,
reading the current index from there too, so a second run says "Nothing new." Serve `catalog/`
on this machine (`python3 -m http.server -d catalog`) and `http://localhost:8000` is a source WaOS
can add. Python 3.11 or later; `waos` from
`cargo install --git https://github.com/waos-dev/waos waos-cli --locked`.

## Trust

- **Each package is signed by its publisher.** The catalog never holds anyone's signing key, and
  cannot make or change a package.
- **The catalog pins each app's publisher key.** A package signed with any other key is not
  collected, so an app's source taken over upstream cannot reach WaOS through the catalog. A key
  changes only by a reviewed change to `catalog.toml`.
- **WaOS keeps the key it first saw for each app.** It installs an update only when it is signed
  with that key, whatever source it comes from.

The index itself is not signed: its SHA-256s tie each download to what it lists, and each package's
signature says who made it.
