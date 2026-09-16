#!/usr/bin/env python3
"""Builds the Nova F-Droid repository from the latest GitHub release of Nova-Android.

Two modes, both driven by .github/workflows/update.yml:

  check  On the plain runner, standard library only. Is the -fdroid.apk of the
         latest release already in the published repo? Writes build/tag/name/
         url/sha256 to $GITHUB_OUTPUT.
  build  Inside ubuntu:26.04 with fdroidserver installed. Downloads the APK,
         refuses anything not signed by the owner's key, merges it into the
         previously published repo, signs a new index and lays out the gh-pages
         tree in --out.

The primary address is https://nova-app.eu/fdroid/repo: that server pulls the
gh-pages branch by itself. GitHub Pages serves the same tree as the mirror.
"""
import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SOURCE_REPO = 'confeden/Nova-Android'
MIRROR_REPO = os.environ.get('GITHUB_REPOSITORY') or 'confeden/nova-fdroid'
APP_ID = 'com.brent.nova'
ASSET_RE = re.compile(r'-fdroid\.apk$')
# SHA-256 of the owner's APK signing certificate — the value F-Droid pins in
# AllowedAPKSigningKeys. The index signs whatever the repo holds, so this check
# is what stands between a swapped release asset and users' phones.
ALLOWED_SIGNER = '8440f6c63f26d461fefa6f7556303dac762768f9daeb31de01598ff55dc9336f'
# SHA-256 of the repo index certificate. Users pin it when they add the repo;
# a different key in the secret would silently fork the repo for everyone.
REPO_FINGERPRINT = '3f145f0cc617fa93c3af95afd2dcbd0c0f115870013a6a21cf0827ccb4db6067'
KEY_ALIAS = 'nova-fdroid'
KEEP_VERSIONS = 2
PRIMARY_URL = 'https://nova-app.eu/fdroid/repo'
MIRROR_URL = 'https://confeden.github.io/nova-fdroid/fdroid/repo'

HERE = Path(__file__).resolve().parent


def log(msg):
    print(msg, flush=True)


def api(url):
    req = urllib.request.Request(url, headers={
        'Accept': 'application/vnd.github+json',
        'User-Agent': 'nova-fdroid-builder',
    })
    token = os.environ.get('GH_TOKEN')
    if token:
        req.add_header('Authorization', f'Bearer {token}')
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def latest_asset():
    rel = api(f'https://api.github.com/repos/{SOURCE_REPO}/releases/latest')
    for a in rel.get('assets') or []:
        if not ASSET_RE.search(a['name']):
            continue
        digest = a.get('digest') or ''
        if not digest.startswith('sha256:'):
            sys.exit(f'{a["name"]}: GitHub reports no sha256 digest')
        return {'tag': rel['tag_name'], 'name': a['name'],
                'url': a['browser_download_url'], 'sha256': digest[len('sha256:'):]}
    return None


def published_state():
    url = f'https://api.github.com/repos/{MIRROR_REPO}/contents/state.json?ref=gh-pages'
    try:
        data = api(url)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {}
        raise
    return json.loads(base64.b64decode(data['content']))


def cmd_check(args):
    asset = latest_asset()
    state = published_state()
    out = {'build': 'false'}
    if asset is None:
        log(f'latest release of {SOURCE_REPO} carries no -fdroid.apk; nothing to do')
    elif asset['sha256'] == state.get('asset_sha256') and not args.force:
        log(f'{asset["name"]} ({asset["tag"]}) is already published')
    else:
        log(f'to build: {asset["name"]} ({asset["tag"]}), published: {state.get("tag") or "nothing"}')
        out = {'build': 'true', **asset}
    gh_out = os.environ.get('GITHUB_OUTPUT')
    if gh_out:
        with open(gh_out, 'a', encoding='utf-8') as f:
            for k, v in out.items():
                f.write(f'{k}={v}\n')


def run(cmd, **kw):
    log('$ ' + ' '.join(str(c) for c in cmd))
    return subprocess.run(cmd, check=True, **kw)


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def apk_signers(path):
    r = subprocess.run(['apksigner', 'verify', '--print-certs', str(path)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f'apksigner rejects {path.name}:\n{r.stdout}{r.stderr}')
    return set(re.findall(r'certificate SHA-256 digest: ([0-9a-f]{64})', r.stdout))


def download(url, dest):
    for attempt in range(1, 4):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'nova-fdroid-builder'})
            with urllib.request.urlopen(req, timeout=300) as r, open(dest, 'wb') as f:
                shutil.copyfileobj(r, f, 1 << 20)
            return
        except (urllib.error.URLError, TimeoutError) as e:
            log(f'download attempt {attempt} failed: {e}')
            time.sleep(10 * attempt)
    sys.exit(f'could not download {url}')


def fetch_fastlane(tag, work):
    """Store texts and changelogs live in the app repo; copy them for this tag."""
    src = work / 'src'
    run(['git', 'clone', '--quiet', '--depth', '1', '--branch', tag, '--filter=blob:none',
         '--sparse', f'https://github.com/{SOURCE_REPO}.git', str(src)])
    run(['git', '-C', str(src), 'sparse-checkout', 'set', 'fastlane/metadata/android'])
    base = src / 'fastlane' / 'metadata' / 'android'
    dest = work / 'metadata' / APP_ID
    for locale in sorted(p for p in base.iterdir() if p.is_dir()):
        shutil.copytree(locale, dest / locale.name)
    shutil.rmtree(src)


def key_fingerprint(keystore, password):
    der = subprocess.run(['keytool', '-exportcert', '-alias', KEY_ALIAS, '-keystore', str(keystore),
                          '-storepass', password], check=True, capture_output=True).stdout
    return hashlib.sha256(der).hexdigest()


def qr_data_uri(text):
    """Inline PNG: a separate file in repo/ would be picked up by `fdroid update`."""
    import io
    import qrcode
    buf = io.BytesIO()
    qrcode.make(text, border=2).save(buf)
    return 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode()


def landing(version_name, version_code):
    add = f'fdroidrepos://nova-app.eu/fdroid/repo?fingerprint={REPO_FINGERPRINT}'
    qr = qr_data_uri(f'{PRIMARY_URL}?fingerprint={REPO_FINGERPRINT}')
    return f'''<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Nova — репозиторий F-Droid</title>
<meta name="robots" content="noindex">
<link rel="icon" href="icons/icon.png">
<style>
:root {{ color-scheme: dark; }}
body {{ margin: 0; background: #0b0f1a; color: #e6e9f2; font: 16px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }}
main {{ max-width: 560px; margin: 0 auto; padding: 32px 16px 48px; }}
.head {{ display: flex; align-items: center; gap: 14px; }}
.head img {{ width: 56px; height: 56px; border-radius: 14px; }}
h1 {{ font-size: 24px; margin: 0; }}
.ver {{ color: #9aa3b8; font-size: 14px; }}
.btn {{ display: block; margin: 24px 0 8px; padding: 14px 18px; border-radius: 12px; text-align: center;
        font-weight: 600; color: #fff; text-decoration: none; background: linear-gradient(135deg, #3b5bdb, #9b6dff); }}
.hint {{ color: #9aa3b8; font-size: 14px; margin: 0 0 24px; }}
h2 {{ font-size: 16px; margin: 28px 0 8px; }}
code {{ display: block; padding: 10px 12px; border-radius: 10px; background: #151b2b; word-break: break-all; font-size: 13px; }}
.qr {{ display: block; width: 220px; max-width: 100%; margin: 12px 0; border-radius: 10px; background: #fff; padding: 8px; }}
a {{ color: #8fb0ff; }}
</style>
</head>
<body>
<main>
<div class="head"><img src="icons/icon.png" alt=""><div><h1>Nova для Android</h1>
<div class="ver">Репозиторий F-Droid · версия {version_name} ({version_code})</div></div></div>

<a class="btn" href="{add}">Добавить в F-Droid</a>
<p class="hint">Кнопка открывает F-Droid (или Droid-ify, Neo Store) на телефоне, где он уже установлен.
После добавления Nova появится в каталоге, а обновления будут приходить через него.</p>

<h2>Добавить вручную</h2>
<p class="hint">F-Droid → Настройки → Репозитории → «+», адрес:</p>
<code>{PRIMARY_URL}?fingerprint={REPO_FINGERPRINT}</code>
<img class="qr" src="{qr}" alt="QR-код адреса репозитория">

<h2>Если nova-app.eu недоступен</h2>
<p class="hint">Зеркало на GitHub Pages с тем же ключом. F-Droid и так переключается на него сам;
добавлять вручную нужно, только если основной адрес не открывается вовсе.</p>
<code>{MIRROR_URL}?fingerprint={REPO_FINGERPRINT}</code>

<p class="hint">Здесь те же APK, что в <a href="https://github.com/{SOURCE_REPO}/releases">релизах на GitHub</a>,
подписанные ключом автора. Приложение, установленное из релиза, обновляется отсюда без переустановки.</p>
</main>
</body>
</html>
'''


def cmd_build(args):
    work = Path(args.work).resolve()
    out = Path(args.out).resolve()
    prev = Path(args.prev).resolve() if args.prev else None
    for d in (work, out):
        if d.exists():
            shutil.rmtree(d)
    repo = work / 'repo'
    if prev and (prev / 'fdroid' / 'repo').is_dir():
        shutil.copytree(prev / 'fdroid' / 'repo', repo)
        log(f'continuing from the published repo: {sorted(p.name for p in repo.glob("*.apk"))}')
    else:
        repo.mkdir(parents=True)
        log('no published repo yet: starting empty')

    password = os.environ['FDROID_KEYSTORE_PASS']
    keystore = work / 'keystore.p12'
    keystore.write_bytes(base64.b64decode(os.environ['FDROID_KEYSTORE_B64']))
    keystore.chmod(0o600)
    fp = key_fingerprint(keystore, password)
    if fp != REPO_FINGERPRINT:
        sys.exit(f'keystore fingerprint {fp} is not the published {REPO_FINGERPRINT}')

    shutil.copy(HERE / 'config.yml', work / 'config.yml')
    (work / 'config.yml').chmod(0o600)
    shutil.copy(HERE / 'icon.png', work / 'icon.png')
    (work / 'metadata').mkdir()
    shutil.copy(HERE / 'metadata' / f'{APP_ID}.yml', work / 'metadata' / f'{APP_ID}.yml')
    fetch_fastlane(args.tag, work)

    tmp = work / 'download.apk'
    download(args.url, tmp)
    got = sha256_of(tmp)
    if got != args.sha256:
        sys.exit(f'{args.name}: sha256 {got} does not match the release digest {args.sha256}')
    signers = apk_signers(tmp)
    if signers != {ALLOWED_SIGNER}:
        sys.exit(f'{args.name}: signed by {sorted(signers)}, expected only {ALLOWED_SIGNER}')

    from fdroidserver import common
    app_id, version_code, version_name = common.get_apk_id(str(tmp))
    if app_id != APP_ID:
        sys.exit(f'{args.name}: package {app_id}, expected {APP_ID}')
    version_code = int(version_code)
    tmp.replace(repo / f'{APP_ID}_{version_code}.apk')
    log(f'added {APP_ID} {version_name} ({version_code}), signer and digest verified')

    # fdroidserver picks changelogs/<CurrentVersionCode>.txt as "what's new" before
    # it would derive that code from the APKs, so a binary repo has to state it.
    with open(work / 'metadata' / f'{APP_ID}.yml', 'a', encoding='utf-8') as f:
        f.write(f"\nCurrentVersion: '{version_name}'\nCurrentVersionCode: {version_code}\n")
    # The app's fastlane tree carries no icon; without one the client shows a blank tile.
    icon = work / 'metadata' / APP_ID / 'en-US' / 'images' / 'icon.png'
    if not icon.exists():
        icon.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(HERE / 'icon.png', icon)

    apks = sorted(repo.glob(f'{APP_ID}_*.apk'),
                  key=lambda p: int(p.stem.rsplit('_', 1)[1]), reverse=True)
    for old in apks[KEEP_VERSIONS:]:
        log(f'dropping {old.name}')
        old.unlink()

    (repo / 'index.html').write_text(landing(version_name, version_code), encoding='utf-8')

    env = dict(os.environ, FDROID_KEYSTORE_PASS=password)
    run(['fdroid', 'update', '--verbose'], cwd=work, env=env)

    index = json.loads((repo / 'index-v2.json').read_text(encoding='utf-8'))
    versions = index['packages'][APP_ID]['versions']
    codes = sorted(v['manifest']['versionCode'] for v in versions.values())
    if version_code not in codes:
        sys.exit(f'index-v2.json lists {codes}, not {version_code}')
    if index['repo'].get('address') != PRIMARY_URL:
        sys.exit(f'index address is {index["repo"].get("address")}')
    log(f'index lists versions {codes}')

    out.mkdir(parents=True)
    # Same layout as the server: fdroidserver insists a mirror ends in /fdroid.
    shutil.copytree(repo, out / 'fdroid' / 'repo')
    (out / '.nojekyll').write_text('')
    (out / 'index.html').write_text(
        '<!DOCTYPE html><meta charset="utf-8">'
        '<meta http-equiv="refresh" content="0; url=fdroid/repo/">'
        '<a href="fdroid/repo/">fdroid/repo/</a>\n', encoding='utf-8')
    (out / 'state.json').write_text(json.dumps({
        'tag': args.tag, 'asset': args.name, 'asset_sha256': args.sha256,
        'version_name': version_name, 'version_code': version_code, 'versions': codes,
        'built': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    }, indent=2) + '\n', encoding='utf-8')
    log(f'site tree ready in {out}')


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='mode', required=True)
    c = sub.add_parser('check')
    c.add_argument('--force', default='false')
    b = sub.add_parser('build')
    for k in ('tag', 'name', 'url', 'sha256'):
        b.add_argument('--' + k, required=True)
    b.add_argument('--prev', help='checkout of the previously published gh-pages branch')
    b.add_argument('--work', default='work')
    b.add_argument('--out', default='site')
    args = ap.parse_args()
    if args.mode == 'check':
        args.force = str(args.force).lower() == 'true'
        cmd_check(args)
    else:
        cmd_build(args)


if __name__ == '__main__':
    main()
