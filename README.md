# Nova F-Droid repository

F-Droid repository for [Nova for Android](https://github.com/confeden/Nova-Android) (`com.brent.nova`).
It carries the developer-signed `-fdroid.apk` from each GitHub release — the same file
F-Droid verifies — so an app installed from a release updates from here without reinstalling.

**Add to F-Droid:** open <https://nova-app.eu/fdroid/repo> on the phone, or add manually:

```
https://nova-app.eu/fdroid/repo?fingerprint=3f145f0cc617fa93c3af95afd2dcbd0c0f115870013a6a21cf0827ccb4db6067
```

Mirror (same key, used by the client automatically):
`https://confeden.github.io/nova-fdroid/fdroid/repo`

## Как добавить в F-Droid

Откройте <https://nova-app.eu/fdroid/repo> на телефоне и нажмите «Добавить в F-Droid», либо
F-Droid → Настройки → Репозитории → «+» и адрес выше.

## How it works

`.github/workflows/update.yml` checks the latest Nova-Android release every 15 minutes. When its
`-fdroid.apk` is new, `fdroid/build.py` downloads it, refuses it unless the sha256 matches the
release digest and the only signer is the owner's certificate
(`8440f6c63f26d461fefa6f7556303dac762768f9daeb31de01598ff55dc9336f`), signs a new index with
`fdroidserver` and force-pushes the tree to `gh-pages`. nova-app.eu pulls that branch.
